"""Marketplace business rules - the single place that knows how an order moves.

Used by the web app (customers, vendors, riders, admin), the simulator (generator.py stream) and the
fulfillment engine (autopilot vendors/riders). Functions take a pyodbc cursor and do NOT commit;
the caller decides the transaction - except engine_tick(), which commits after each step.

Every status change is a conditional UPDATE ("... WHERE status = <expected>"), so two processes
racing for the same shipment can't both win, and replaying a step is harmless.

    shipment: pending -> confirmed -> packing -> ready -> assigned -> picked_up -> delivered
                 (admin start) (vendor accepts) (packed) (rider)  (pickup)    (drop-off)
    order:    pending -> processing -> shipped -> delivered        (+ cancelled / returned)
"""
from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

try:  # imported as source_app.marketplace (web app) or as plain `marketplace` (scripts in source_app/)
    from . import payments as pay
except ImportError:
    import payments as pay

PAYMENT_METHODS = ["credit_card", "debit_card", "paypal", "bank_transfer", "cash_on_delivery"]
COUPONS = {"SAVE5": Decimal("0.05"), "SAVE10": Decimal("0.10"), "SAVE20": Decimal("0.20")}
MAX_QTY_PER_LINE = 20
CANCELLABLE_SHIPMENT = ("pending", "confirmed", "packing")   # nothing has left the vendor yet
ACTIVE_SHIPMENT = ("pending", "confirmed", "packing", "ready", "assigned", "picked_up")
SETTINGS_DEFAULTS = {"auto_approve": "false", "sim_speed": "1"}


class MarketplaceError(Exception):
    """A business-rule violation (out of stock, wrong state, not your order...)."""


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


# ------------------------------------------------------------------ settings
def get_settings(cur) -> dict[str, str]:
    rows = dict(cur.execute("SELECT setting_key, setting_value FROM dbo.app_settings").fetchall())
    return {**SETTINGS_DEFAULTS, **rows}


def set_setting(cur, key: str, value: str) -> None:
    if key not in SETTINGS_DEFAULTS:
        raise MarketplaceError(f"Unknown setting {key!r}")
    cur.execute(
        "MERGE dbo.app_settings AS t USING (SELECT ? AS k, ? AS v) AS s ON t.setting_key = s.k "
        "WHEN MATCHED THEN UPDATE SET setting_value = s.v, updated_at = SYSDATETIME() "
        "WHEN NOT MATCHED THEN INSERT (setting_key, setting_value) VALUES (s.k, s.v);",
        key, value,
    )


# ------------------------------------------------------------------ checkout
def create_order(cur, customer_id: int, lines: list[tuple[int, int]], payment_method: str,
                 coupon: str = "", ts: datetime | None = None,
                 ship_city: str | None = None, ship_country: str | None = None,
                 pay_online_via_gateway: bool = False) -> dict:
    """Place an order. lines = [(product_id, quantity)]. Prices come from the database.

    Splits the order into one shipment per vendor, locks inventory rows while checking stock (so two
    buyers can't take the last unit), and records the payment:
      - cash on delivery: 'pending' until the rider delivers
      - online, pay_online_via_gateway=True (real customers): 'pending' until the gateway confirms; the order
        can't be started until then, and is cancelled if unpaid after PAYMENT_TIMEOUT_MINUTES
      - online, otherwise (the simulator's fake shoppers): recorded as already paid
    """
    ts = ts or now()
    qty: dict[int, int] = defaultdict(int)
    for product_id, q in lines:
        qty[int(product_id)] += int(q)
    if not qty:
        raise MarketplaceError("Your cart is empty")
    if any(q < 1 or q > MAX_QTY_PER_LINE for q in qty.values()):
        raise MarketplaceError(f"Quantity per product must be between 1 and {MAX_QTY_PER_LINE}")
    if payment_method not in PAYMENT_METHODS:
        raise MarketplaceError("Unknown payment method")
    coupon = (coupon or "").strip().upper()
    if coupon and coupon not in COUPONS:
        raise MarketplaceError(f"Coupon {coupon!r} is not valid")
    discount = COUPONS.get(coupon, Decimal("0.00"))

    cust = cur.execute("SELECT city, country FROM dbo.customers WHERE customer_id = ?", customer_id).fetchone()
    if cust is None:
        raise MarketplaceError("Customer not found")

    priced = []  # (product_id, vendor_id, qty, unit_price, line_total)
    for product_id in sorted(qty):  # fixed lock order avoids deadlocks between concurrent checkouts
        row = cur.execute(
            """
            SELECT p.product_name, p.unit_price, p.is_active, p.vendor_id, v.is_active AS vendor_active,
                   i.quantity_on_hand
            FROM dbo.products p
            JOIN dbo.inventory i WITH (UPDLOCK, ROWLOCK) ON i.product_id = p.product_id
            LEFT JOIN dbo.vendors v ON v.vendor_id = p.vendor_id
            WHERE p.product_id = ?
            """, product_id,
        ).fetchone()
        if row is None or not row.is_active or row.vendor_id is None or not row.vendor_active:
            raise MarketplaceError(f"Product #{product_id} is not available")
        if row.quantity_on_hand < qty[product_id]:
            raise MarketplaceError(f"Only {max(row.quantity_on_hand, 0)} left of {row.product_name}")
        line_total = money(row.unit_price * qty[product_id] * (1 - discount))
        priced.append((product_id, row.vendor_id, qty[product_id], row.unit_price, line_total))

    total = sum((p[4] for p in priced), Decimal("0.00"))
    order_id = cur.execute(
        "INSERT INTO dbo.orders (customer_id, order_date, status, shipping_city, shipping_country, total_amount, "
        "created_at, updated_at) OUTPUT INSERTED.order_id VALUES (?,?,?,?,?,?,?,?)",
        customer_id, ts, "pending", ship_city or cust.city, ship_country or cust.country, total, ts, ts,
    ).fetchval()

    shipment_ids = {}
    for vendor_id in sorted({p[1] for p in priced}):
        shipment_ids[vendor_id] = cur.execute(
            "INSERT INTO dbo.shipments (order_id, vendor_id, status, delivery_fee, created_at, updated_at) "
            "OUTPUT INSERTED.shipment_id VALUES (?,?,?,?,?,?)",
            order_id, vendor_id, "pending", money(random.uniform(1.5, 4.5)), ts, ts,
        ).fetchval()
    for product_id, vendor_id, q, price, line_total in priced:
        cur.execute(
            "INSERT INTO dbo.order_items (order_id, product_id, quantity, unit_price, discount, line_total, "
            "shipment_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            order_id, product_id, q, price, discount, line_total, shipment_ids[vendor_id], ts, ts,
        )
        cur.execute(
            "UPDATE dbo.inventory SET quantity_on_hand = quantity_on_hand - ?, updated_at = ? WHERE product_id = ?",
            q, ts, product_id,
        )
    if payment_method == "cash_on_delivery":
        pay_status, provider = "pending", "cod"
    elif pay_online_via_gateway:
        pay_status, provider = "pending", None  # provider is set when the gateway session starts
    else:
        pay_status, provider = "completed", "simulator"
    cur.execute(
        "INSERT INTO dbo.payments (order_id, payment_method, amount, payment_status, paid_at, provider, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?)",
        order_id, payment_method, total, pay_status, ts, provider, ts, ts,
    )
    return {"order_id": order_id, "status": "pending", "total_amount": float(total),
            "shipments": len(shipment_ids), "payment_status": pay_status,
            "needs_online_payment": pay_online_via_gateway and payment_method != "cash_on_delivery"}


# ------------------------------------------------------------------ order status
def refresh_order_status(cur, order_id: int, ts: datetime) -> str:
    """Derive the order's status from its shipments and save it if it changed."""
    statuses = [r[0] for r in cur.execute("SELECT status FROM dbo.shipments WHERE order_id = ?", order_id).fetchall()]
    current = cur.execute("SELECT status FROM dbo.orders WHERE order_id = ?", order_id).fetchval()
    if not statuses or current == "returned":
        return current
    s = set(statuses)
    if s == {"cancelled"}:
        new = "cancelled"
    elif s <= {"delivered", "cancelled", "returned"} and "delivered" in s:
        new = "delivered"
    elif s <= {"picked_up", "delivered", "cancelled"}:
        new = "shipped"
    elif s == {"pending"}:
        new = "pending"
    else:
        new = "processing"
    if new != current:
        cur.execute("UPDATE dbo.orders SET status = ?, updated_at = ? WHERE order_id = ?", new, ts, order_id)
        if new == "delivered":  # cash on delivery is collected now
            cur.execute(
                "UPDATE dbo.payments SET payment_status = 'completed', paid_at = ?, updated_at = ? "
                "WHERE order_id = ? AND payment_status = 'pending'", ts, ts, order_id,
            )
    return new


def _move(cur, shipment_id: int, frm: tuple[str, ...], to: str, stamp_col: str | None, ts: datetime,
          extra_sql: str = "", extra_params: tuple = ()) -> int:
    """Conditional status change for one shipment. Returns its order_id, or raises if it wasn't in `frm`."""
    marks = ",".join("?" * len(frm))
    stamp = f", {stamp_col} = ?" if stamp_col else ""
    row = cur.execute(
        f"UPDATE dbo.shipments SET status = ?, updated_at = ?{stamp}{extra_sql} "
        f"OUTPUT INSERTED.order_id WHERE shipment_id = ? AND status IN ({marks})",
        to, ts, *((ts,) if stamp_col else ()), *extra_params, shipment_id, *frm,
    ).fetchone()
    if row is None:
        state = cur.execute("SELECT status FROM dbo.shipments WHERE shipment_id = ?", shipment_id).fetchval()
        raise MarketplaceError(f"Shipment #{shipment_id} is {state or 'missing'}, can't move it to {to}")
    refresh_order_status(cur, row[0], ts)
    return row[0]


# ------------------------------------------------------------------ actions by role
def start_order(cur, order_id: int, ts: datetime | None = None) -> None:
    """Admin pushes an order into fulfillment: every pending shipment goes to its vendor."""
    ts = ts or now()
    if pay.awaiting_online_payment(cur, order_id):
        raise MarketplaceError(f"Order #{order_id} is waiting for the customer's online payment")
    n = cur.execute(
        "UPDATE dbo.shipments SET status = 'confirmed', confirmed_at = ?, updated_at = ? "
        "WHERE order_id = ? AND status = 'pending'", ts, ts, order_id,
    ).rowcount
    if n == 0:
        raise MarketplaceError(f"Order #{order_id} has nothing waiting to start")
    refresh_order_status(cur, order_id, ts)


def vendor_accept(cur, shipment_id: int, vendor_id: int | None = None, ts: datetime | None = None) -> None:
    _check_owner(cur, shipment_id, "vendor_id", vendor_id)
    _move(cur, shipment_id, ("confirmed",), "packing", "accepted_at", ts or now())


def vendor_ready(cur, shipment_id: int, vendor_id: int | None = None, ts: datetime | None = None) -> None:
    _check_owner(cur, shipment_id, "vendor_id", vendor_id)
    _move(cur, shipment_id, ("packing",), "ready", "ready_at", ts or now())


def assign_rider(cur, shipment_id: int, rider_id: int, ts: datetime | None = None) -> None:
    ts = ts or now()
    if not cur.execute("UPDATE dbo.riders SET status = 'busy', updated_at = ? WHERE rider_id = ? AND status = 'available'",
                       ts, rider_id).rowcount:
        raise MarketplaceError(f"Rider #{rider_id} is not available")
    _move(cur, shipment_id, ("ready",), "assigned", "assigned_at", ts, ", rider_id = ?", (rider_id,))


def rider_pickup(cur, shipment_id: int, rider_id: int | None = None, ts: datetime | None = None) -> None:
    _check_owner(cur, shipment_id, "rider_id", rider_id)
    _move(cur, shipment_id, ("assigned",), "picked_up", "picked_up_at", ts or now())


def rider_deliver(cur, shipment_id: int, rider_id: int | None = None, ts: datetime | None = None) -> None:
    ts = ts or now()
    _check_owner(cur, shipment_id, "rider_id", rider_id)
    _move(cur, shipment_id, ("picked_up",), "delivered", "delivered_at", ts)
    pay.record_delivery(cur, shipment_id, ts)  # credit the seller: sale - commission, held for the return window
    cur.execute(  # free the rider unless they still carry something
        "UPDATE r SET status = 'available', updated_at = ? FROM dbo.riders r "
        "WHERE r.rider_id = (SELECT rider_id FROM dbo.shipments WHERE shipment_id = ?) AND r.status = 'busy' "
        "AND NOT EXISTS (SELECT 1 FROM dbo.shipments s WHERE s.rider_id = r.rider_id AND s.status IN ('assigned','picked_up'))",
        ts, shipment_id,
    )


def cancel_order(cur, order_id: int, customer_id: int | None = None, ts: datetime | None = None) -> None:
    """Cancel before anything has left a vendor. Refunds online payments and puts stock back."""
    ts = ts or now()
    row = cur.execute("SELECT customer_id, status FROM dbo.orders WITH (UPDLOCK) WHERE order_id = ?", order_id).fetchone()
    if row is None:
        raise MarketplaceError("Order not found")
    if customer_id is not None and row.customer_id != customer_id:
        raise MarketplaceError("This is not your order")
    statuses = {r[0] for r in cur.execute("SELECT status FROM dbo.shipments WHERE order_id = ?", order_id).fetchall()}
    if row.status in ("cancelled", "returned", "delivered", "shipped") or not statuses <= set(CANCELLABLE_SHIPMENT):
        raise MarketplaceError("This order can no longer be cancelled - it is already on its way")
    cur.execute("UPDATE dbo.shipments SET status = 'cancelled', cancelled_at = ?, updated_at = ? WHERE order_id = ?",
                ts, ts, order_id)
    cur.execute(
        "UPDATE inv SET quantity_on_hand = inv.quantity_on_hand + oi.quantity, updated_at = ? "
        "FROM dbo.inventory inv JOIN dbo.order_items oi ON oi.product_id = inv.product_id WHERE oi.order_id = ?",
        ts, order_id,
    )
    pay.refund_payment(cur, order_id, ts)  # online: refunded through the gateway; unpaid/COD: marked failed
    cur.execute("UPDATE dbo.orders SET status = 'cancelled', updated_at = ? WHERE order_id = ?", ts, order_id)


def return_order(cur, order_id: int, customer_id: int | None = None, ts: datetime | None = None) -> None:
    ts = ts or now()
    row = cur.execute("SELECT customer_id, status FROM dbo.orders WITH (UPDLOCK) WHERE order_id = ?", order_id).fetchone()
    if row is None:
        raise MarketplaceError("Order not found")
    if customer_id is not None and row.customer_id != customer_id:
        raise MarketplaceError("This is not your order")
    if row.status != "delivered":
        raise MarketplaceError("Only delivered orders can be returned")
    if customer_id is not None:  # customers get the return window; admins can still accept a late return
        last = cur.execute("SELECT MAX(delivered_at) FROM dbo.shipments WHERE order_id = ?", order_id).fetchval()
        if last and last < ts - timedelta(days=pay.HOLD_DAYS):
            raise MarketplaceError(f"The {pay.HOLD_DAYS:g}-day return window for this order has ended")
    cur.execute("UPDATE dbo.shipments SET status = 'returned', updated_at = ? WHERE order_id = ? AND status = 'delivered'",
                ts, order_id)
    pay.refund_payment(cur, order_id, ts)  # give the customer their money back
    pay.record_return(cur, order_id, ts)   # and take the sale (net of commission) back from the seller
    cur.execute("UPDATE dbo.orders SET status = 'returned', updated_at = ? WHERE order_id = ?", ts, order_id)


def _check_owner(cur, shipment_id: int, column: str, owner_id: int | None) -> None:
    if owner_id is None:  # admin / engine acting
        return
    actual = cur.execute(f"SELECT {column} FROM dbo.shipments WHERE shipment_id = ?", shipment_id).fetchval()
    if actual != owner_id:
        raise MarketplaceError("This shipment is not assigned to you")


# ------------------------------------------------------------------ fulfillment engine (autopilot)
def engine_tick(conn, ts: datetime | None = None) -> dict[str, int]:
    """One pass of the autopilot. Call it every couple of seconds.

    - auto-approve on:  pending orders start by themselves after a few seconds
    - autopilot vendors (is_bot): accept, then pack after their prep time
    - dispatcher:       ready shipments get the next available rider (bots and online humans)
    - autopilot riders: pick up, then deliver
    Delays vary per shipment (derived from its id) and are divided by the sim_speed setting.
    """
    ts = ts or now()
    cur = conn.cursor()
    settings = get_settings(cur)
    speed = max(float(settings["sim_speed"]), 0.1)
    done = defaultdict(int)

    def due(col: str, base: int, spread: int) -> str:
        # e.g. "accepted_at <= now - (base + id % spread) / speed seconds"
        return f"s.{col} <= DATEADD(second, -CAST((({base} + s.shipment_id % {spread}) / {speed}) AS INT), ?)"

    def run(step: str, sql: str, params: tuple, action) -> None:
        for (sid,) in cur.execute(sql, *params).fetchall():
            try:
                action(cur, sid, ts=ts)
                conn.commit()
                done[step] += 1
            except MarketplaceError:
                conn.rollback()  # someone else moved it first - fine

    if settings["auto_approve"] == "true":
        for (oid,) in cur.execute(
            "SELECT TOP 50 o.order_id FROM dbo.orders o WHERE o.status = 'pending' "
            "AND o.order_date <= DATEADD(second, -CAST(3 / ? AS INT), ?) "
            "AND EXISTS (SELECT 1 FROM dbo.shipments s WHERE s.order_id = o.order_id AND s.status = 'pending') "
            "AND NOT EXISTS (SELECT 1 FROM dbo.payments p WHERE p.order_id = o.order_id AND p.payment_status = 'pending' "
            "                AND p.payment_method <> 'cash_on_delivery')",  # online orders wait for their payment
            speed, ts,
        ).fetchall():
            try:
                start_order(cur, oid, ts)
                conn.commit()
                done["auto_started"] += 1
            except MarketplaceError:
                conn.rollback()

    bot_vendor = "JOIN dbo.vendors v ON v.vendor_id = s.vendor_id AND v.is_bot = 1"
    run("vendor_accepted", f"SELECT TOP 50 s.shipment_id FROM dbo.shipments s {bot_vendor} "
        f"WHERE s.status = 'confirmed' AND {due('confirmed_at', 4, 8)}", (ts,), vendor_accept)
    run("vendor_packed", f"SELECT TOP 50 s.shipment_id FROM dbo.shipments s {bot_vendor} "
        f"WHERE s.status = 'packing' AND s.accepted_at <= DATEADD(second, "
        f"-CAST(v.prep_seconds * (0.5 + (s.shipment_id % 10) / 10.0) / {speed} AS INT), ?)", (ts,), vendor_ready)

    ready = [r[0] for r in cur.execute(
        "SELECT TOP 50 shipment_id FROM dbo.shipments WHERE status = 'ready' ORDER BY ready_at, shipment_id").fetchall()]
    riders = [r[0] for r in cur.execute(  # online humans get first pick, then autopilot riders at random
        "SELECT rider_id FROM dbo.riders WHERE status = 'available' ORDER BY is_bot, NEWID()").fetchall()]
    for sid, rid in zip(ready, riders):
        try:
            assign_rider(cur, sid, rid, ts)
            conn.commit()
            done["rider_assigned"] += 1
        except MarketplaceError:
            conn.rollback()

    bot_rider = "JOIN dbo.riders r ON r.rider_id = s.rider_id AND r.is_bot = 1"
    run("rider_picked_up", f"SELECT TOP 50 s.shipment_id FROM dbo.shipments s {bot_rider} "
        f"WHERE s.status = 'assigned' AND {due('assigned_at', 6, 8)}", (ts,), rider_pickup)
    run("rider_delivered", f"SELECT TOP 50 s.shipment_id FROM dbo.shipments s {bot_rider} "
        f"WHERE s.status = 'picked_up' AND {due('picked_up_at', 10, 20)}", (ts,), rider_deliver)
    # money housekeeping: earnings past the return window become payable; abandoned online payments expire
    n = pay.release_available(cur, ts)
    expired = pay.expire_unpaid_orders(cur, ts, cancel_order)
    conn.commit()
    if n:
        done["earnings_released"] += n
    if expired:
        done["unpaid_orders_expired"] += expired
    return dict(done)


def random_return(cur, ts: datetime) -> int | None:
    """Simulator helper: a customer returns a recent delivery (about 1 in 7 ticks)."""
    row = cur.execute(
        "SELECT TOP 1 order_id FROM dbo.orders WHERE status = 'delivered' AND order_date >= DATEADD(day, -30, ?) "
        "ORDER BY NEWID()", ts,
    ).fetchone()
    if row:
        return_order(cur, row[0], ts=ts)
        return row[0]
    return None
