"""Insert a handful of realistically messy records into ShopDB, so the silver layer has work to do.

Real source systems get data from web forms, imports and buggy integrations. This adds:
  - customers with padded/odd-case names, uppercase emails, "usa" / "U.K." / unknown countries,
    and an email with no domain ending                         -> silver CLEANS or WARNS
  - an order with status 'PAID' and shipping country 'Deutschland' -> silver CLEANS
  - an order whose total doesn't match its items, a line total that doesn't add up,
    and a payment for the wrong amount                           -> silver WARNS
  - an order dated 30 days in the future (clock bug), with its item and payment
                                                                -> silver QUARANTINES all three

Usage:
    python source_app/inject_bad_data.py
Then:  ingestion/extract.py  ->  transform/silver.py  and look at the issues column.
"""
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from db import get_connection


def main() -> None:
    conn = get_connection()
    cur = conn.cursor()
    now = datetime.now().replace(microsecond=0)
    tag = uuid.uuid4().hex[:6]

    def add_customer(first, last, email, city, country) -> int:
        cur.execute(
            "INSERT INTO dbo.customers (first_name, last_name, email, phone, city, country, signup_date, "
            "created_at, updated_at) OUTPUT INSERTED.customer_id VALUES (?,?,?,?,?,?,?,?,?)",
            first, last, email, " +1 (555) 010-0000 ", city, country, now.date(), now, now,
        )
        return cur.fetchone()[0]

    def add_order(customer_id, order_date, status, country, total, items, payment=None) -> int:
        cur.execute(
            "INSERT INTO dbo.orders (customer_id, order_date, status, shipping_city, shipping_country, "
            "total_amount, created_at, updated_at) OUTPUT INSERTED.order_id VALUES (?,?,?,?,?,?,?,?)",
            customer_id, order_date, status, "  Berlin ", country, total, now, now,
        )
        order_id = cur.fetchone()[0]
        for product_id, qty, price, line_total in items:
            cur.execute(
                "INSERT INTO dbo.order_items (order_id, product_id, quantity, unit_price, discount, line_total, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                order_id, product_id, qty, price, Decimal("0.00"), line_total, now, now,
            )
        if payment is not None:
            cur.execute(
                "INSERT INTO dbo.payments (order_id, payment_method, amount, payment_status, paid_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                order_id, " Credit_Card ", payment, "completed", now, now, now,
            )
        return order_id

    c1 = add_customer("  maria ", "GARCIA   lopez", f"  Maria.Garcia.{tag}@Example.COM ", " new   york ", "usa")
    c2 = add_customer("John", "Doe", f"john.doe.{tag}@gmail", "London", "U.K.")
    c3 = add_customer("Ada", "Lovelace", f"ada.{tag}@example.com", "Somewhere", "Narnia")

    product_id, price = cur.execute("SELECT TOP 1 product_id, unit_price FROM dbo.products ORDER BY product_id").fetchone()
    p2, price2 = cur.execute("SELECT TOP 1 product_id, unit_price FROM dbo.products ORDER BY product_id DESC").fetchone()

    # Cleanable: 'PAID' and 'Deutschland'. Warn: total 999.99 doesn't match items; payment amount is off;
    # second line's line_total is wrong.
    o1 = add_order(c1, now, "PAID", "Deutschland", Decimal("999.99"),
                   [(product_id, 2, price, price * 2), (p2, 1, price2, price2 + Decimal("5.00"))],
                   payment=Decimal("10.00"))
    # Quarantine: order date 30 days in the future -> its item and payment become orphans too.
    o2 = add_order(c2, now + timedelta(days=30), "paid", "United Kingdom", price,
                   [(product_id, 1, price, price)], payment=price)

    conn.commit()
    conn.close()
    print(f"Inserted messy customers {c1}, {c2}, {c3}; messy order {o1}; future-dated order {o2}.")
    print("Next: python ingestion/extract.py  then  python transform/silver.py")


if __name__ == "__main__":
    main()
