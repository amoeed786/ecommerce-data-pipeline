"""Database layer for the shop app. Every write goes straight to ShopDB, like a real ecommerce
backend - and every write stamps updated_at, so the data pipeline picks it up on its next extract."""
from __future__ import annotations

import sys
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from source_app.db import get_connection  # noqa: E402

PAYMENT_METHODS = ["credit_card", "debit_card", "paypal", "bank_transfer", "cash_on_delivery"]
COUPONS = {"SAVE5": Decimal("0.05"), "SAVE10": Decimal("0.10"), "SAVE20": Decimal("0.20")}
MAX_QTY_PER_LINE = 20

# status -> statuses it may move to (same lifecycle the generator follows)
NEXT_STATUS = {
    "pending": ["paid", "cancelled"],
    "paid": ["shipped", "cancelled"],
    "shipped": ["delivered"],
    "delivered": ["returned"],
    "cancelled": [],
    "returned": [],
}
CUSTOMER_CANCELLABLE = {"pending", "paid"}


class ShopError(Exception):
    """A business-rule violation (out of stock, bad coupon, ...). The API turns it into HTTP 400."""


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def query(sql: str, *params) -> list[dict]:
    """Run a SELECT and return rows as JSON-friendly dicts."""
    conn = get_connection()
    try:
        cur = conn.cursor().execute(sql, *params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, map(_jsonable, row))) for row in cur.fetchall()]
    finally:
        conn.close()


def query_one(sql: str, *params) -> dict | None:
    rows = query(sql, *params)
    return rows[0] if rows else None


def scalar(sql: str, *params):
    conn = get_connection()
    try:
        return conn.cursor().execute(sql, *params).fetchval()
    finally:
        conn.close()


# ------------------------------------------------------------------ reads
def categories() -> list[dict]:
    return query("SELECT category_id, category_name FROM dbo.categories ORDER BY category_name")


def products(category_id: int | None = None, search: str | None = None) -> list[dict]:
    sql = """
        SELECT p.product_id, p.product_name, p.sku, p.unit_price, c.category_id, c.category_name,
               coalesce(i.quantity_on_hand, 0) AS stock
        FROM dbo.products p
        JOIN dbo.categories c ON c.category_id = p.category_id
        LEFT JOIN dbo.inventory i ON i.product_id = p.product_id
        WHERE p.is_active = 1
    """
    params: list = []
    if category_id:
        sql += " AND p.category_id = ?"
        params.append(category_id)
    if search:
        sql += " AND (p.product_name LIKE ? OR p.sku LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    return query(sql + " ORDER BY c.category_name, p.product_name", *params)


def find_customers(search: str = "", limit: int = 20) -> list[dict]:
    like = f"%{search}%"
    return query(
        """
        SELECT TOP (?) customer_id, first_name, last_name, email, city, country
        FROM dbo.customers
        WHERE ? = '' OR first_name + ' ' + last_name LIKE ? OR email LIKE ? OR CAST(customer_id AS varchar) = ?
        ORDER BY customer_id DESC
        """,
        limit, search, like, like, search,
    )


def customer(customer_id: int) -> dict | None:
    return query_one(
        "SELECT customer_id, first_name, last_name, email, phone, city, country, signup_date "
        "FROM dbo.customers WHERE customer_id = ?", customer_id,
    )


ORDER_LIST_SQL = """
    SELECT {top} o.order_id, o.customer_id, c.first_name + ' ' + c.last_name AS customer, o.order_date,
           o.status, o.total_amount, o.shipping_city, o.shipping_country, o.updated_at,
           (SELECT count(*) FROM dbo.order_items i WHERE i.order_id = o.order_id) AS items
    FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id
"""


def list_orders(statuses: list[str] | None = None, customer_id: int | None = None, limit: int = 100) -> list[dict]:
    sql = ORDER_LIST_SQL.format(top=f"TOP ({int(limit)})") + " WHERE o.order_date <= SYSDATETIME()"
    params: list = []
    if statuses:
        sql += f" AND o.status IN ({','.join('?' * len(statuses))})"
        params += statuses
    if customer_id:
        sql += " AND o.customer_id = ?"
        params.append(customer_id)
    return query(sql + " ORDER BY o.order_date DESC, o.order_id DESC", *params)


def order_detail(order_id: int) -> dict | None:
    head = query_one(ORDER_LIST_SQL.format(top="") + " WHERE o.order_id = ?", order_id)
    if head is None:
        return None
    head["email"] = scalar("SELECT email FROM dbo.customers WHERE customer_id = ?", head["customer_id"])
    head["lines"] = query(
        """
        SELECT i.product_id, p.product_name, i.quantity, i.unit_price, i.discount, i.line_total
        FROM dbo.order_items i JOIN dbo.products p ON p.product_id = i.product_id
        WHERE i.order_id = ? ORDER BY i.order_item_id
        """, order_id,
    )
    head["payment"] = query_one(
        "SELECT payment_method, amount, payment_status, paid_at FROM dbo.payments WHERE order_id = ?", order_id
    )
    head["next_status"] = NEXT_STATUS[head["status"]]
    return head


# ------------------------------------------------------------------ writes
def new_customer(first: str, last: str, email: str, city: str, country: str) -> dict:
    first, last, email = first.strip(), last.strip(), email.strip().lower()
    if not (first and last and "@" in email):
        raise ShopError("First name, last name and a valid email are required")
    if scalar("SELECT count(*) FROM dbo.customers WHERE email = ?", email):
        raise ShopError("An account with this email already exists")
    ts = now()
    conn = get_connection()
    try:
        cid = conn.cursor().execute(
            "INSERT INTO dbo.customers (first_name, last_name, email, city, country, signup_date, created_at, "
            "updated_at) OUTPUT INSERTED.customer_id VALUES (?,?,?,?,?,?,?,?)",
            first, last, email, city.strip() or None, country.strip() or None, ts.date(), ts, ts,
        ).fetchval()
        conn.commit()
    finally:
        conn.close()
    return customer(cid)


def place_order(customer_id: int, items: list[dict], pay_now: bool, method: str, coupon: str = "") -> dict:
    """items: [{product_id, quantity}]. Prices come from the database, never from the browser.

    Runs in one transaction. Inventory rows are locked (UPDLOCK) while we check stock, so two
    customers can't both buy the last unit at the same moment.
    """
    qty_by_product: dict[int, int] = {}
    for item in items:
        qty_by_product[int(item["product_id"])] = qty_by_product.get(int(item["product_id"]), 0) + int(item["quantity"])
    if not qty_by_product:
        raise ShopError("Your cart is empty")
    if any(q < 1 or q > MAX_QTY_PER_LINE for q in qty_by_product.values()):
        raise ShopError(f"Quantity per product must be between 1 and {MAX_QTY_PER_LINE}")

    coupon = (coupon or "").strip().upper()
    if coupon and coupon not in COUPONS:
        raise ShopError(f"Coupon {coupon!r} is not valid")
    discount = COUPONS.get(coupon, Decimal("0.00"))
    if pay_now and method not in PAYMENT_METHODS:
        raise ShopError("Unknown payment method")

    ts = now()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cust = cur.execute("SELECT city, country FROM dbo.customers WHERE customer_id = ?", customer_id).fetchone()
        if cust is None:
            raise ShopError("Customer not found")

        lines, total = [], Decimal("0.00")
        for product_id, qty in sorted(qty_by_product.items()):  # fixed lock order avoids deadlocks
            row = cur.execute(
                """
                SELECT p.product_name, p.unit_price, p.is_active, i.quantity_on_hand
                FROM dbo.products p
                JOIN dbo.inventory i WITH (UPDLOCK, ROWLOCK) ON i.product_id = p.product_id
                WHERE p.product_id = ?
                """, product_id,
            ).fetchone()
            if row is None or not row.is_active:
                raise ShopError(f"Product #{product_id} is not available")
            if row.quantity_on_hand < qty:
                raise ShopError(f"Only {max(row.quantity_on_hand, 0)} left of {row.product_name}")
            line_total = money(row.unit_price * qty * (1 - discount))
            lines.append((product_id, qty, row.unit_price, discount, line_total))
            total += line_total

        status = "paid" if pay_now else "pending"
        order_id = cur.execute(
            "INSERT INTO dbo.orders (customer_id, order_date, status, shipping_city, shipping_country, "
            "total_amount, created_at, updated_at) OUTPUT INSERTED.order_id VALUES (?,?,?,?,?,?,?,?)",
            customer_id, ts, status, cust.city, cust.country, total, ts, ts,
        ).fetchval()
        for product_id, qty, price, disc, line_total in lines:
            cur.execute(
                "INSERT INTO dbo.order_items (order_id, product_id, quantity, unit_price, discount, line_total, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                order_id, product_id, qty, price, disc, line_total, ts, ts,
            )
            cur.execute(
                "UPDATE dbo.inventory SET quantity_on_hand = quantity_on_hand - ?, updated_at = ? WHERE product_id = ?",
                qty, ts, product_id,
            )
        if pay_now:
            cur.execute(
                "INSERT INTO dbo.payments (order_id, payment_method, amount, payment_status, paid_at, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                order_id, method, total, "completed", ts, ts, ts,
            )
        conn.commit()
        return {"order_id": order_id, "status": status, "total_amount": float(total)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def change_status(order_id: int, new_status: str, method: str = "credit_card",
                  customer_id: int | None = None) -> dict:
    """Move an order along its lifecycle. When customer_id is given, it's the customer acting:
    they may only cancel their own order, and only before it ships."""
    ts = now()
    conn = get_connection()
    try:
        cur = conn.cursor()
        row = cur.execute(
            "SELECT status, total_amount, customer_id FROM dbo.orders WITH (UPDLOCK) WHERE order_id = ?", order_id
        ).fetchone()
        if row is None:
            raise ShopError("Order not found")
        status, total, owner = row
        if customer_id is not None:
            if owner != customer_id:
                raise ShopError("This is not your order")
            if new_status != "cancelled" or status not in CUSTOMER_CANCELLABLE:
                raise ShopError("Orders can only be cancelled before they ship")
        if new_status not in NEXT_STATUS[status]:
            raise ShopError(f"Order #{order_id} can't go from {status} to {new_status}")

        if status == "pending" and new_status == "paid":
            if method not in PAYMENT_METHODS:
                raise ShopError("Unknown payment method")
            cur.execute(
                "INSERT INTO dbo.payments (order_id, payment_method, amount, payment_status, paid_at, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                order_id, method, total, "completed", ts, ts, ts,
            )
        if new_status in ("cancelled", "returned"):
            cur.execute(
                "UPDATE dbo.payments SET payment_status = 'refunded', updated_at = ? WHERE order_id = ?", ts, order_id
            )
        if new_status == "cancelled":  # never shipped, so the units go back on the shelf
            cur.execute(
                """
                UPDATE inv SET quantity_on_hand = inv.quantity_on_hand + oi.quantity, updated_at = ?
                FROM dbo.inventory inv JOIN dbo.order_items oi ON oi.product_id = inv.product_id
                WHERE oi.order_id = ?
                """, ts, order_id,
            )
        cur.execute("UPDATE dbo.orders SET status = ?, updated_at = ? WHERE order_id = ?", new_status, ts, order_id)
        conn.commit()
        return {"order_id": order_id, "from": status, "to": new_status}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _update(sql: str, *params) -> int:
    conn = get_connection()
    try:
        n = conn.cursor().execute(sql, *params).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def change_price(product_id: int, new_price: float) -> None:
    if new_price < 0:
        raise ShopError("Price can't be negative")
    if not _update("UPDATE dbo.products SET unit_price = ?, updated_at = ? WHERE product_id = ?",
                   money(new_price), now(), product_id):
        raise ShopError("Product not found")


def restock(product_id: int, qty: int) -> None:
    if qty < 1:
        raise ShopError("Restock quantity must be positive")
    if not _update("UPDATE dbo.inventory SET quantity_on_hand = quantity_on_hand + ?, updated_at = ? "
                   "WHERE product_id = ?", qty, now(), product_id):
        raise ShopError("Product not found")


# ------------------------------------------------------------------ analytics
def dashboard_stats() -> dict:
    kpis = query_one("""
        SELECT
          (SELECT count(*) FROM dbo.orders WHERE CAST(order_date AS date) = CAST(GETDATE() AS date)
                                             AND order_date <= SYSDATETIME())                   AS orders_today,
          (SELECT coalesce(sum(total_amount), 0) FROM dbo.orders
             WHERE CAST(order_date AS date) = CAST(GETDATE() AS date) AND order_date <= SYSDATETIME()
               AND status NOT IN ('cancelled','returned'))                                      AS revenue_today,
          (SELECT count(*) FROM dbo.orders WHERE order_date >= DATEADD(minute, -5, SYSDATETIME())
                                             AND order_date <= SYSDATETIME())                   AS orders_last_5_min,
          (SELECT count(*) FROM dbo.orders WHERE status IN ('pending','paid','shipped'))        AS open_orders,
          (SELECT count(*) FROM dbo.customers)                                                  AS customers,
          (SELECT count(*) FROM dbo.customers WHERE signup_date = CAST(GETDATE() AS date))      AS new_customers_today
    """)
    kpis["avg_order_today"] = kpis["revenue_today"] / kpis["orders_today"] if kpis["orders_today"] else 0
    return {
        "kpis": kpis,
        "per_minute": query("""
            SELECT DATEADD(minute, DATEDIFF(minute, 0, order_date), 0) AS minute, count(*) AS orders
            FROM dbo.orders
            WHERE order_date >= DATEADD(minute, -60, SYSDATETIME()) AND order_date <= SYSDATETIME()
            GROUP BY DATEADD(minute, DATEDIFF(minute, 0, order_date), 0) ORDER BY minute
        """),
        "revenue_by_day": query("""
            SELECT CAST(order_date AS date) AS day, count(*) AS orders,
                   sum(CASE WHEN status NOT IN ('cancelled','returned') THEN total_amount ELSE 0 END) AS revenue
            FROM dbo.orders
            WHERE order_date >= DATEADD(day, -29, CAST(GETDATE() AS date)) AND order_date <= SYSDATETIME()
            GROUP BY CAST(order_date AS date) ORDER BY day
        """),
        "status_counts": query("SELECT status, count(*) AS orders FROM dbo.orders GROUP BY status ORDER BY orders DESC"),
        "category_revenue": query("""
            SELECT c.category_name, sum(i.line_total) AS revenue
            FROM dbo.order_items i
            JOIN dbo.orders o ON o.order_id = i.order_id
            JOIN dbo.products p ON p.product_id = i.product_id
            JOIN dbo.categories c ON c.category_id = p.category_id
            WHERE o.order_date >= DATEADD(day, -30, SYSDATETIME()) AND o.order_date <= SYSDATETIME()
              AND o.status NOT IN ('cancelled','returned')
            GROUP BY c.category_name ORDER BY revenue DESC
        """),
        "top_products": query("""
            SELECT TOP 5 p.product_name, sum(i.quantity) AS units, sum(i.line_total) AS revenue
            FROM dbo.order_items i
            JOIN dbo.orders o ON o.order_id = i.order_id
            JOIN dbo.products p ON p.product_id = i.product_id
            WHERE o.order_date >= DATEADD(day, -7, SYSDATETIME()) AND o.order_date <= SYSDATETIME()
              AND o.status NOT IN ('cancelled','returned')
            GROUP BY p.product_name ORDER BY units DESC
        """),
        "low_stock": query("""
            SELECT TOP 8 p.product_id, p.product_name, i.quantity_on_hand
            FROM dbo.inventory i JOIN dbo.products p ON p.product_id = i.product_id
            ORDER BY i.quantity_on_hand
        """),
        "recent_orders": list_orders(limit=12),
    }
