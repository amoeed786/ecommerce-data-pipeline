r"""ShopDB Live - a small web interface for the fake shop and the pipeline behind it.

Run:  .\.venv\Scripts\streamlit.exe run shop_app\app.py     (opens http://localhost:8501)

Pages
  Live monitor  - auto-refreshing view of what's happening in ShopDB right now
  Storefront    - be a customer: pick products, place an order
  Orders admin  - be the warehouse: pay / ship / deliver / cancel / return orders
  Catalog       - change prices, restock, move customers (changes that SCD2 will track later)
  Pipeline      - start/stop the simulator, run each pipeline step, inspect the data lake
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shop_db as db  # noqa: E402

ROOT = db.PROJECT_ROOT
PY = sys.executable
LAKE = ROOT / "data_lake"
STATE = LAKE / "_state"
SIM_LOG = STATE / "simulator.log"
TABLES = ["categories", "customers", "products", "inventory", "orders", "order_items", "payments"]
STATUS_ICON = {"pending": "🕓", "paid": "💳", "shipped": "🚚", "delivered": "✅", "cancelled": "❌", "returned": "↩️"}

def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


# ================================================================== Live monitor
def live_monitor() -> None:
    st.title("📈 Live monitor")
    every = st.sidebar.select_slider("Auto-refresh every", [2, 5, 10, 30, 60], value=5, format_func=lambda s: f"{s}s")
    st.caption(f"Reads ShopDB directly and refreshes every {every}s. Start the simulator on the Pipeline page "
               "(or place orders in the Storefront) to see it move.")

    @st.fragment(run_every=f"{every}s")
    def body() -> None:
        k = db.query("""
            SELECT
              (SELECT count(*) FROM dbo.orders WHERE CAST(order_date AS date) = CAST(GETDATE() AS date)) AS orders_today,
              (SELECT coalesce(sum(total_amount), 0) FROM dbo.orders
                 WHERE CAST(order_date AS date) = CAST(GETDATE() AS date)
                   AND status NOT IN ('cancelled','returned'))                                    AS revenue_today,
              (SELECT count(*) FROM dbo.orders WHERE status IN ('pending','paid','shipped'))       AS open_orders,
              (SELECT count(*) FROM dbo.customers)                                                 AS customers,
              (SELECT count(*) FROM dbo.customers WHERE signup_date = CAST(GETDATE() AS date))     AS new_customers,
              (SELECT count(*) FROM dbo.orders WHERE order_date >= DATEADD(minute, -5, SYSDATETIME())
                                                 AND order_date <= SYSDATETIME())                   AS last_5_min
        """).iloc[0]
        c = st.columns(6)
        c[0].metric("Orders today", f"{int(k.orders_today):,}")
        c[1].metric("Revenue today", fmt_money(float(k.revenue_today)))
        c[2].metric("Orders, last 5 min", f"{int(k.last_5_min):,}")
        c[3].metric("Open orders", f"{int(k.open_orders):,}")
        c[4].metric("Customers", f"{int(k.customers):,}")
        c[5].metric("New customers today", f"{int(k.new_customers):,}")

        left, right = st.columns([3, 2])
        with left:
            st.subheader("Latest orders")
            recent = db.query("""
                SELECT TOP 15 o.order_id, o.order_date, c.first_name + ' ' + c.last_name AS customer,
                       o.status, o.total_amount, o.shipping_country, o.updated_at
                FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id
                WHERE o.order_date <= SYSDATETIME()
                ORDER BY o.order_date DESC, o.order_id DESC
            """)
            recent["status"] = recent["status"].map(lambda s: f"{STATUS_ICON.get(s, '')} {s}")
            st.dataframe(recent, hide_index=True, width="stretch",
                         column_config={"total_amount": st.column_config.NumberColumn("total", format="$%.2f")})

            st.subheader("Orders per minute (last hour)")
            per_min = db.query("""
                SELECT DATEADD(minute, DATEDIFF(minute, 0, order_date), 0) AS minute, count(*) AS orders
                FROM dbo.orders
                WHERE order_date >= DATEADD(minute, -60, SYSDATETIME()) AND order_date <= SYSDATETIME()
                GROUP BY DATEADD(minute, DATEDIFF(minute, 0, order_date), 0)
                ORDER BY minute
            """)
            if per_min.empty:
                st.info("No orders in the last hour - start the simulator on the Pipeline page.")
            else:
                st.bar_chart(per_min, x="minute", y="orders", height=220)
        with right:
            st.subheader("Recently changed (any table)")
            changes = db.query("""
                SELECT TOP 12 * FROM (
                  SELECT 'order'    AS what, CAST(order_id AS varchar)    AS id, status AS detail, updated_at
                    FROM dbo.orders WHERE updated_at <= SYSDATETIME()
                  UNION ALL SELECT 'payment', CAST(payment_id AS varchar), payment_status, updated_at FROM dbo.payments
                  UNION ALL SELECT 'customer', CAST(customer_id AS varchar), city + ', ' + country, updated_at FROM dbo.customers
                  UNION ALL SELECT 'product', CAST(product_id AS varchar), 'price ' + CAST(unit_price AS varchar), updated_at FROM dbo.products
                ) x ORDER BY updated_at DESC
            """)
            st.dataframe(changes, hide_index=True, width="stretch")
            st.caption("Every one of these has a fresh `updated_at` - that's exactly what the next extract will pick up.")

            st.subheader("Orders by status")
            st.bar_chart(db.query("SELECT status, count(*) AS orders FROM dbo.orders GROUP BY status"),
                         x="status", y="orders", height=220)

            low = db.query("""
                SELECT TOP 8 p.product_id, p.product_name, i.quantity_on_hand
                FROM dbo.inventory i JOIN dbo.products p ON p.product_id = i.product_id
                ORDER BY i.quantity_on_hand
            """)
            st.subheader("Lowest stock")
            st.dataframe(low, hide_index=True, width="stretch")

        st.caption(f"Last refreshed {db.now():%H:%M:%S}")

    body()


# ================================================================== Storefront
@st.cache_data(ttl=60)
def catalog() -> pd.DataFrame:
    return db.query("""
        SELECT p.product_id, p.product_name, c.category_name AS category, p.unit_price, p.sku, i.quantity_on_hand AS stock
        FROM dbo.products p JOIN dbo.categories c ON c.category_id = p.category_id
        LEFT JOIN dbo.inventory i ON i.product_id = p.product_id
        WHERE p.is_active = 1 ORDER BY c.category_name, p.product_name
    """)


@st.cache_data(ttl=60)
def customer_list() -> pd.DataFrame:
    return db.query("""
        SELECT customer_id, first_name + ' ' + last_name + ' (#' + CAST(customer_id AS varchar) + ', '
               + coalesce(city, '?') + ')' AS label
        FROM dbo.customers ORDER BY customer_id DESC
    """)


def storefront() -> None:
    st.title("🛍️ Storefront")
    st.caption("Act as a shopper. Your order is written to ShopDB the moment you click Place order.")
    cart: list[dict] = st.session_state.setdefault("cart", [])

    customers = customer_list()
    with st.sidebar.expander("New customer"):
        with st.form("new_customer", clear_on_submit=True):
            first, last = st.text_input("First name"), st.text_input("Last name")
            email = st.text_input("Email")
            city, country = st.text_input("City"), st.text_input("Country", "Pakistan")
            if st.form_submit_button("Sign up") and first and last and email:
                try:
                    cid = db.new_customer(first, last, email, city, country)
                    customer_list.clear()
                    st.success(f"Customer #{cid} created")
                except Exception as e:  # e.g. duplicate email
                    st.error(str(e).split("]")[-1])
    labels = dict(zip(customers.label, customers.customer_id))
    who = st.selectbox("Shopping as", list(labels))

    products = catalog()
    left, right = st.columns([3, 2])
    with left:
        cat = st.radio("Category", ["All", *sorted(products.category.unique())], horizontal=True)
        shown = products if cat == "All" else products[products.category == cat]
        search = st.text_input("Search products")
        if search:
            shown = shown[shown.product_name.str.contains(search, case=False)]
        st.dataframe(shown, hide_index=True, width="stretch", height=300,
                     column_config={"unit_price": st.column_config.NumberColumn("price", format="$%.2f")})

        with st.form("add", clear_on_submit=True):
            c1, c2, c3 = st.columns([4, 1, 1])
            options = {f"{r.product_name} - ${r.unit_price:,.2f} ({r.stock} left)": r for r in shown.itertuples()}
            pick = c1.selectbox("Product", list(options)) if options else None
            qty = c2.number_input("Qty", 1, 20, 1)
            disc = c3.selectbox("Coupon", [0.0, 0.05, 0.10, 0.20], format_func=lambda d: f"{d:.0%}")
            if st.form_submit_button("Add to cart") and pick:
                r = options[pick]
                cart.append({"product_id": r.product_id, "product": r.product_name, "quantity": int(qty),
                             "unit_price": float(r.unit_price), "discount": disc})
                st.rerun()

    with right:
        st.subheader("🛒 Cart")
        if not cart:
            st.info("Cart is empty.")
            return
        df = pd.DataFrame(cart)
        df["line_total"] = (df.quantity * df.unit_price * (1 - df.discount)).round(2)
        st.dataframe(df[["product", "quantity", "unit_price", "discount", "line_total"]], hide_index=True,
                     width="stretch")
        st.metric("Total", fmt_money(df.line_total.sum()))
        pay_now = st.toggle("Pay now (otherwise order stays pending)", value=True)
        method = st.selectbox("Payment method", db.PAYMENT_METHODS, disabled=not pay_now)
        b1, b2 = st.columns(2)
        if b1.button("Place order", type="primary", width="stretch"):
            order_id = db.place_order(labels[who], cart, pay_now, method)
            st.session_state.cart = []
            catalog.clear()
            st.success(f"Order #{order_id} placed. Find it in Live monitor or Orders admin.")
            st.balloons()
        if b2.button("Empty cart", width="stretch"):
            st.session_state.cart = []
            st.rerun()


# ================================================================== Orders admin
def orders_admin() -> None:
    st.title("📦 Orders admin")
    st.caption("Act as the back office. Each button changes the order's status and stamps updated_at, so the "
               "next extract writes a new version of the order to bronze.")
    status_filter = st.multiselect("Status", list(db.NEXT_STATUS), default=["pending", "paid", "shipped"])
    if not status_filter:
        return
    marks = ",".join("?" * len(status_filter))
    orders = db.query(f"""
        SELECT TOP 200 o.order_id, o.order_date, c.first_name + ' ' + c.last_name AS customer,
               o.status, o.total_amount, o.updated_at
        FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id
        WHERE o.status IN ({marks}) ORDER BY o.order_date DESC
    """, *status_filter)
    st.dataframe(orders, hide_index=True, width="stretch", height=280,
                 column_config={"total_amount": st.column_config.NumberColumn("total", format="$%.2f")})

    default_id = int(orders.order_id.iloc[0]) if len(orders) else 1
    order_id = st.number_input("Order id", min_value=1, value=default_id, step=1)
    head = db.query("""
        SELECT o.*, c.first_name + ' ' + c.last_name AS customer, c.email
        FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id WHERE o.order_id = ?
    """, order_id)
    if head.empty:
        st.warning("No such order.")
        return
    o = head.iloc[0]
    st.subheader(f"Order #{order_id} - {STATUS_ICON.get(o.status, '')} {o.status}")
    st.write(f"**{o.customer}** ({o.email}) · ordered {o.order_date} · ships to {o.shipping_city}, "
             f"{o.shipping_country} · total **{fmt_money(o.total_amount)}**")
    c1, c2 = st.columns(2)
    c1.dataframe(db.query("""
        SELECT p.product_name, i.quantity, i.unit_price, i.discount, i.line_total
        FROM dbo.order_items i JOIN dbo.products p ON p.product_id = i.product_id WHERE i.order_id = ?
    """, order_id), hide_index=True, width="stretch")
    c2.dataframe(db.query("SELECT payment_method, amount, payment_status, paid_at FROM dbo.payments WHERE order_id = ?",
                          order_id), hide_index=True, width="stretch")

    nxt = db.NEXT_STATUS[o.status]
    if not nxt:
        st.info("This order is final.")
        return
    method = st.selectbox("Payment method (if paying)", db.PAYMENT_METHODS) if "paid" in nxt else "credit_card"
    for col, new in zip(st.columns(len(nxt)), nxt):
        if col.button(f"{STATUS_ICON[new]} Mark {new}", width="stretch"):
            db.change_status(order_id, new, method)
            st.toast(f"Order #{order_id}: {o.status} → {new}")
            st.rerun()


# ================================================================== Catalog
def catalog_admin() -> None:
    st.title("🏷️ Catalog & customers")
    st.caption("Master-data changes. Bronze keeps every old version, so later (SCD2) we can answer "
               "\"what was the price on the day of the order?\".")
    products = catalog()
    t1, t2 = st.tabs(["Products", "Customers"])
    with t1:
        opts = {f"#{r.product_id} {r.product_name} (${r.unit_price:,.2f}, stock {r.stock})": r for r in products.itertuples()}
        r = opts[st.selectbox("Product", list(opts))]
        c1, c2 = st.columns(2)
        with c1.form("price"):
            new_price = st.number_input("New price", min_value=0.0, value=float(r.unit_price), step=1.0)
            if st.form_submit_button("Change price"):
                db.change_price(r.product_id, new_price)
                catalog.clear()
                st.success(f"Price of #{r.product_id} changed {r.unit_price:,.2f} → {new_price:,.2f}")
        with c2.form("restock"):
            qty = st.number_input("Add units", min_value=1, value=100, step=10)
            if st.form_submit_button("Restock"):
                db.restock(r.product_id, int(qty))
                catalog.clear()
                st.success(f"Added {qty} units to #{r.product_id}")
    with t2:
        customers = customer_list()
        labels = dict(zip(customers.label, customers.customer_id))
        who = st.selectbox("Customer", list(labels))
        with st.form("move"):
            city, country = st.text_input("New city"), st.text_input("New country")
            if st.form_submit_button("Move customer") and city and country:
                db.move_customer(labels[who], city, country)
                customer_list.clear()
                st.success("Customer address updated")


# ================================================================== Pipeline
@st.cache_resource
def simulator() -> dict:
    return {"proc": None}


def run_step(label: str, args: list[str]) -> None:
    with st.spinner(f"Running {label}..."):
        out = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True)
    st.caption(f"$ python {' '.join(args)}")
    st.code((out.stdout + out.stderr).strip() or "(no output)", language="text")


def pipeline() -> None:
    st.title("🛠️ Pipeline")
    st.caption("Each button runs the same script you'd run in the terminal - the command is shown above its output.")

    st.subheader("1 · Shop simulator (source activity)")
    sim = simulator()
    running = sim["proc"] is not None and sim["proc"].poll() is None
    c1, c2, c3 = st.columns([1, 1, 3])
    interval = c3.slider("Seconds between ticks", 1, 30, 5)
    if c1.button("▶ Start", disabled=running, width="stretch"):
        STATE.mkdir(parents=True, exist_ok=True)
        log = SIM_LOG.open("w")
        sim["proc"] = subprocess.Popen([PY, "-u", "source_app/generator.py", "stream", "--interval", str(interval)],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        st.rerun()
    if c2.button("■ Stop", disabled=not running, width="stretch"):
        sim["proc"].terminate()
        sim["proc"] = None
        st.rerun()
    st.write("Simulator is **running** 🟢" if running else "Simulator is **stopped** ⚪")
    if SIM_LOG.exists():
        st.code("\n".join(SIM_LOG.read_text(errors="replace").splitlines()[-8:]) or "(starting...)", language="text")

    st.subheader("2 · Run pipeline steps")
    b = st.columns(5)
    actions = {
        "Extract → bronze": ["ingestion/extract.py"],
        "Check bronze": ["ingestion/check_bronze.py"],
        "Build silver": ["transform/silver.py"],
        "Inject messy data": ["source_app/inject_bad_data.py"],
        "Check source DB": ["source_app/check_db.py"],
    }
    clicked = [name for col, name in zip(b, actions) if col.button(name, width="stretch")]
    for name in clicked:
        run_step(name, actions[name])

    st.subheader("3 · Data lake")
    d = duckdb.connect()
    rows = []
    for t in TABLES:
        bronze = LAKE / "bronze" / t
        files = list(bronze.rglob("*.parquet")) if bronze.exists() else []
        silver = LAKE / "silver" / f"{t}.parquet"
        quarantine = LAKE / "quarantine" / f"{t}.parquet"

        def count(path: str) -> int:
            return d.execute(f"SELECT count(*) FROM read_parquet('{path}')").fetchone()[0]

        rows.append({
            "table": t,
            "bronze files": len(files),
            "bronze rows (all versions)": count((bronze / "**" / "*.parquet").as_posix()) if files else 0,
            "silver rows (latest, clean)": count(silver.as_posix()) if silver.exists() else 0,
            "quarantined": count(quarantine.as_posix()) if quarantine.exists() else 0,
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Watermarks** (`data_lake/_state/watermarks.json`)")
        wm = STATE / "watermarks.json"
        st.json(json.loads(wm.read_text()) if wm.exists() else {})
    with c2:
        st.markdown("**Latest data-quality results** (`data_lake/_state/dq_log.jsonl`)")
        dq = STATE / "dq_log.jsonl"
        if dq.exists():
            log = [json.loads(line) for line in dq.read_text().splitlines() if line.strip()]
            last = [r for r in log if r["run_at"] == log[-1]["run_at"]]
            st.dataframe(pd.DataFrame([{"table": r["table"], "silver": r["silver_rows"], "quarantined": r["quarantined"],
                                        "errors": ", ".join(f"{k}={v}" for k, v in r["errors"].items()),
                                        "warnings": ", ".join(f"{k}={v}" for k, v in r["warnings"].items())}
                                       for r in last]), hide_index=True, width="stretch")
            st.caption(f"Run at {last[0]['run_at']}")

    st.markdown("**Recent extract runs** (`data_lake/_state/ingestion_log.jsonl`)")
    log_file = STATE / "ingestion_log.jsonl"
    if log_file.exists():
        entries = [json.loads(line) for line in log_file.read_text().splitlines()[-21:]]
        st.dataframe(pd.DataFrame(entries)[["batch_id", "table", "mode", "rows", "watermark_from", "watermark_to", "seconds"]]
                     .iloc[::-1], hide_index=True, width="stretch")

    st.subheader("4 · Peek into a layer")
    c1, c2 = st.columns(2)
    layer = c1.radio("Layer", ["bronze", "silver", "quarantine"], horizontal=True)
    table = c2.selectbox("Table", TABLES, index=TABLES.index("orders"))
    path = (LAKE / "bronze" / table / "**" / "*.parquet") if layer == "bronze" else (LAKE / layer / f"{table}.parquet")
    try:
        order = "ORDER BY _ingested_at DESC" if layer == "bronze" else ""
        st.dataframe(d.execute(f"SELECT * FROM read_parquet('{path.as_posix()}') {order} LIMIT 200").df(),
                     width="stretch")
        st.caption(f"SELECT * FROM read_parquet('{path.relative_to(ROOT).as_posix()}') LIMIT 200")
    except duckdb.Error:
        st.info(f"Nothing in {layer}/{table} yet.")


if __name__ == "__main__":  # `streamlit run` executes this file as __main__
    st.set_page_config(page_title="ShopDB Live", page_icon="🛒", layout="wide")
    st.navigation([
        st.Page(live_monitor, title="Live monitor", icon="📈", default=True),
        st.Page(storefront, title="Storefront", icon="🛍️"),
        st.Page(orders_admin, title="Orders admin", icon="📦"),
        st.Page(catalog_admin, title="Catalog", icon="🏷️"),
        st.Page(pipeline, title="Pipeline", icon="🛠️"),
    ]).run()
