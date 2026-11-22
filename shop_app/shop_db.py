"""Database actions for the shop web app. Everything writes straight to ShopDB, exactly like a real
ecommerce backend would - and every write stamps updated_at, so the pipeline picks it up."""
from __future__ import annotations

import sys
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from source_app.db import get_connection  # noqa: E402

PAYMENT_METHODS = ["credit_card", "debit_card", "paypal", "bank_transfer", "cash_on_delivery"]

# status -> statuses it can move to (same lifecycle the generator follows)
NEXT_STATUS = {
    "pending": ["paid", "cancelled"],
    "paid": ["shipped", "cancelled"],
    "shipped": ["delivered"],
    "delivered": ["returned"],
    "cancelled": [],
    "returned": [],
}


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def query(sql: str, *params) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame (Decimals turned into floats for display/charts)."""
    conn = get_connection()
    try:
        cur = conn.cursor().execute(sql, *params)
        cols = [c[0] for c in cur.description]
        df = pd.DataFrame.from_records(cur.fetchall(), columns=cols)
    finally:
        conn.close()
    for col in df.columns:
        if len(df) and isinstance(df[col].iloc[0], Decimal):
            df[col] = df[col].astype(float)
    return df


def scalar(sql: str, *params):
    conn = get_connection()
    try:
        return conn.cursor().execute(sql, *params).fetchval()
    finally:
        conn.close()


# ------------------------------------------------------------------ storefront
def place_order(customer_id: int, cart: list[dict], pay_now: bool, method: str) -> int:
    """cart items: {product_id, quantity, unit_price, discount}. One transaction: all or nothing."""
    ts = now()
    conn = get_connection()
    try:
        cur = conn.cursor()
        city, country = cur.execute(
            "SELECT city, country FROM dbo.customers WHERE customer_id = ?", customer_id
        ).fetchone()
        lines = []
        for item in cart:
            price, disc = money(item["unit_price"]), money(item["discount"])
            lines.append((item["product_id"], item["quantity"], price, disc, money(price * item["quantity"] * (1 - disc))))
        total = sum((line[4] for line in lines), Decimal("0.00"))
        status = "paid" if pay_now else "pending"

        order_id = cur.execute(
            "INSERT INTO dbo.orders (customer_id, order_date, status, shipping_city, shipping_country, "
            "total_amount, created_at, updated_at) OUTPUT INSERTED.order_id VALUES (?,?,?,?,?,?,?,?)",
            customer_id, ts, status, city, country, total, ts, ts,
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
        return order_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def new_customer(first: str, last: str, email: str, city: str, country: str) -> int:
    ts = now()
    conn = get_connection()
    try:
        cid = conn.cursor().execute(
            "INSERT INTO dbo.customers (first_name, last_name, email, city, country, signup_date, created_at, "
            "updated_at) OUTPUT INSERTED.customer_id VALUES (?,?,?,?,?,?,?,?)",
            first, last, email, city, country, ts.date(), ts, ts,
        ).fetchval()
        conn.commit()
        return cid
    finally:
        conn.close()


# ------------------------------------------------------------------ back office
def change_status(order_id: int, new_status: str, method: str = "credit_card") -> None:
    ts = now()
    conn = get_connection()
    try:
        cur = conn.cursor()
        status, total = cur.execute(
            "SELECT status, total_amount FROM dbo.orders WHERE order_id = ?", order_id
        ).fetchone()
        if new_status not in NEXT_STATUS[status]:
            raise ValueError(f"Order {order_id} can't go from {status} to {new_status}")
        if status == "pending" and new_status == "paid":
            cur.execute(
                "INSERT INTO dbo.payments (order_id, payment_method, amount, payment_status, paid_at, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                order_id, method, total, "completed", ts, ts, ts,
            )
        if new_status in ("cancelled", "returned"):
            cur.execute(
                "UPDATE dbo.payments SET payment_status = 'refunded', updated_at = ? WHERE order_id = ?", ts, order_id
            )
        cur.execute("UPDATE dbo.orders SET status = ?, updated_at = ? WHERE order_id = ?", new_status, ts, order_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _update(sql: str, *params) -> None:
    conn = get_connection()
    try:
        conn.cursor().execute(sql, *params)
        conn.commit()
    finally:
        conn.close()


def change_price(product_id: int, new_price: float) -> None:
    _update("UPDATE dbo.products SET unit_price = ?, updated_at = ? WHERE product_id = ?",
            money(new_price), now(), product_id)


def restock(product_id: int, qty: int) -> None:
    _update("UPDATE dbo.inventory SET quantity_on_hand = quantity_on_hand + ?, updated_at = ? WHERE product_id = ?",
            qty, now(), product_id)


def move_customer(customer_id: int, city: str, country: str) -> None:
    _update("UPDATE dbo.customers SET city = ?, country = ?, updated_at = ? WHERE customer_id = ?",
            city, country, now(), customer_id)
