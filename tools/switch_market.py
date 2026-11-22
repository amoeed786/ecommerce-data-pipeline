"""Move the whole marketplace to one market: Pakistan, prices in PKR (one-time; refuses to run twice).

What changes
  - Catalog prices: USD x RATE, then retail rounding (Rs 349, Rs 1,499, Rs 24,999, Rs 349,999). Costs follow.
  - History (order lines, orders, payments, refunds, seller ledger, payouts) is re-derived from the converted
    line prices with the SAME rounding the app uses, so every reconciliation still balances to the paisa:
      line_total = round(qty x price x (1 - discount), 2), order total = sum of lines, payment = order total,
      sale = +line_total, commission = -round(line_total x locked rate, 2), reversals mirror, payout = its ledger rows.
    Rows that were deliberately inconsistent before (the injected bad-data demos) stay inconsistent.
  - Rider fees: Rs 150-350 per parcel.
  - People: customers, riders and stores in Pakistani cities, Pakistani names, +92 phone numbers.
    Login accounts (users table, TEST_ACCOUNTS.md) are not touched.

Usage:  python tools/switch_market.py [--rate 280]
"""
import argparse
import random
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from source_app.db import get_connection  # noqa: E402

COUNTRY = "Pakistan"
CITIES = ["Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar", "Hyderabad", "Sialkot", "Gujranwala", "Quetta"]
CITY_W = [24, 22, 11, 8, 8, 6, 6, 4, 4, 4, 3]
MALE = ["Ahmed", "Ali", "Hamza", "Usman", "Bilal", "Hassan", "Omar", "Saad", "Fahad", "Zain", "Danish", "Imran", "Kashif", "Faisal",
        "Asad", "Junaid", "Shahzaib", "Talha", "Haris", "Waqas", "Adeel", "Moiz", "Rehan", "Arslan", "Nabeel", "Sohail", "Yasir", "Umer"]
FEMALE = ["Ayesha", "Fatima", "Zainab", "Maryam", "Hira", "Sana", "Mahnoor", "Amna", "Iqra", "Rabia", "Sadia", "Mehwish", "Nimra",
          "Aiman", "Kiran", "Anum", "Sidra", "Laiba", "Hafsa", "Areeba", "Saba", "Noor", "Mariam", "Javeria", "Bushra", "Komal"]
LAST = ["Khan", "Ahmed", "Malik", "Hussain", "Sheikh", "Butt", "Qureshi", "Chaudhry", "Raza", "Siddiqui", "Iqbal", "Akhtar", "Mirza",
        "Javed", "Abbasi", "Shah", "Rana", "Aslam", "Farooq", "Anwar", "Baig", "Hashmi", "Rizvi", "Nadeem", "Saleem", "Zafar", "Awan"]


def pkr_retail(usd: float, rate: float) -> int:
    """Pakistani retail price points: ...49/...99 under 1,000, ...99 above."""
    x = usd * rate
    if x < 1000:
        step = 50
    elif x < 10000:
        step = 100
    elif x < 100000:
        step = 500
    else:
        step = 1000
    return max(99, int((x + step - 1) // step) * step - 1)


def phone() -> str:
    return f"+92 3{random.randint(0, 4)}{random.randint(0, 9)} {random.randint(1000000, 9999999)}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rate", type=float, default=280.0, help="PKR per USD")
    ap.add_argument("--seed", type=int, default=92)
    a = ap.parse_args()
    random.seed(a.seed)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("IF OBJECT_ID('dbo.migration_log','U') IS NULL CREATE TABLE dbo.migration_log (name VARCHAR(80) PRIMARY KEY, ran_at DATETIME2(0) NOT NULL)")
    if cur.execute("SELECT 1 FROM dbo.migration_log WHERE name = 'market_pkr'").fetchone():
        sys.exit("Already converted to PKR - nothing to do.")
    now = datetime.now().replace(microsecond=0)
    r = a.rate

    # ---- catalog: retail price points
    for pid, up, lp, uc in cur.execute("SELECT product_id, unit_price, list_price, unit_cost FROM dbo.products").fetchall():
        new_up = pkr_retail(float(up), r)
        new_lp = pkr_retail(float(lp), r) if lp is not None else None
        if new_lp is not None and new_lp <= new_up:
            new_lp = None
        new_uc = round(float(uc) / float(up) * new_up, 2) if up else round(float(uc) * r, 2)
        cur.execute("UPDATE dbo.products SET unit_price = ?, list_price = ?, unit_cost = ?, updated_at = ? WHERE product_id = ?",
                    new_up, new_lp, new_uc, now, pid)
    print("products: repriced in PKR")

    # ---- history, set-based, re-derived (see docstring)
    cur.execute(f"""
        DECLARE @r DECIMAL(10,2) = {r};
        SELECT order_item_id, CASE WHEN ABS(line_total - ROUND(quantity * unit_price * (1 - discount), 2)) <= 0.01 THEN 1 ELSE 0 END AS ok
          INTO #li FROM dbo.order_items;
        SELECT o.order_id, CASE WHEN ABS(o.total_amount - ISNULL(s.t, 0)) <= 0.01 THEN 1 ELSE 0 END AS ok
          INTO #ord FROM dbo.orders o LEFT JOIN (SELECT order_id, SUM(line_total) t FROM dbo.order_items GROUP BY order_id) s ON s.order_id = o.order_id;
        SELECT p.payment_id, CASE WHEN ABS(p.amount - o.total_amount) <= 0.01 THEN 1 ELSE 0 END AS ok,
               CASE WHEN p.refunded_amount > 0 AND p.refunded_amount >= p.amount - 0.01 THEN 1 ELSE 0 END AS full_refund
          INTO #pay FROM dbo.payments p JOIN dbo.orders o ON o.order_id = p.order_id;

        UPDATE dbo.order_items SET unit_price = unit_price * @r;
        UPDATE i SET line_total = CASE WHEN x.ok = 1 THEN ROUND(i.quantity * i.unit_price * (1 - i.discount), 2) ELSE i.line_total * @r END
          FROM dbo.order_items i JOIN #li x ON x.order_item_id = i.order_item_id;
        UPDATE o SET total_amount = CASE WHEN x.ok = 1 THEN ISNULL(s.t, 0) ELSE o.total_amount * @r END
          FROM dbo.orders o JOIN #ord x ON x.order_id = o.order_id
          LEFT JOIN (SELECT order_id, SUM(line_total) t FROM dbo.order_items GROUP BY order_id) s ON s.order_id = o.order_id;
        UPDATE p SET amount = CASE WHEN x.ok = 1 THEN o.total_amount ELSE p.amount * @r END
          FROM dbo.payments p JOIN #pay x ON x.payment_id = p.payment_id JOIN dbo.orders o ON o.order_id = p.order_id;
        UPDATE p SET refunded_amount = CASE WHEN x.full_refund = 1 THEN p.amount ELSE p.refunded_amount * @r END
          FROM dbo.payments p JOIN #pay x ON x.payment_id = p.payment_id WHERE p.refunded_amount > 0;
        UPDATE p SET amount = p.amount * @r FROM dbo.payments p WHERE p.payment_id NOT IN (SELECT payment_id FROM #pay);
        UPDATE dbo.payment_events SET amount = amount * @r WHERE amount IS NOT NULL;

        UPDATE l SET amount = CASE
              WHEN i.order_item_id IS NULL THEN l.amount * @r
              WHEN l.entry_type = 'sale' THEN i.line_total
              WHEN l.entry_type = 'sale_reversal' THEN -i.line_total
              WHEN l.entry_type = 'commission' THEN -ROUND(i.line_total * l.commission_rate, 2)
              WHEN l.entry_type = 'commission_reversal' THEN ROUND(i.line_total * l.commission_rate, 2)
              ELSE l.amount * @r END
          FROM dbo.ledger_entries l LEFT JOIN dbo.order_items i ON i.order_item_id = l.order_item_id;
        UPDATE p SET amount = s.t FROM dbo.payouts p JOIN (SELECT payout_id, SUM(amount) t FROM dbo.ledger_entries
          WHERE payout_id IS NOT NULL GROUP BY payout_id) s ON s.payout_id = p.payout_id WHERE s.t > 0;
        UPDATE dbo.shipments SET delivery_fee = 150 + 10 * ROUND((delivery_fee - 1.5) / 3.0 * 20, 0);
    """)
    cur.execute("UPDATE dbo.order_items SET updated_at = ?", now)
    cur.execute("UPDATE dbo.orders SET updated_at = ?", now)
    for t in ("payments", "ledger_entries", "payouts", "shipments"):
        cur.execute(f"UPDATE dbo.{t} SET updated_at = ?", now)
    print("history: order lines, orders, payments, ledger, payouts, rider fees converted")

    # ---- one market: Pakistani cities, names, phones (login accounts untouched)
    linked = {x[0] for x in cur.execute("SELECT customer_id FROM dbo.users WHERE customer_id IS NOT NULL").fetchall()}
    used_emails = set()
    for (cid,) in cur.execute("SELECT customer_id FROM dbo.customers").fetchall():
        city = random.choices(CITIES, CITY_W)[0]
        if cid in linked:
            cur.execute("UPDATE dbo.customers SET city = ?, country = ?, updated_at = ? WHERE customer_id = ?", city, COUNTRY, now, cid)
            continue
        first = random.choice(MALE if random.random() < .52 else FEMALE)
        last = random.choice(LAST)
        email = f"{first}.{last}{cid}@example.pk".lower()
        used_emails.add(email)
        cur.execute("UPDATE dbo.customers SET first_name = ?, last_name = ?, email = ?, phone = ?, city = ?, country = ?, updated_at = ? "
                    "WHERE customer_id = ?", first, last, email, phone(), city, COUNTRY, now, cid)
    cur.execute("UPDATE o SET shipping_city = c.city, shipping_country = ?, updated_at = ? FROM dbo.orders o "
                "JOIN dbo.customers c ON c.customer_id = o.customer_id", COUNTRY, now)
    linked_r = {x[0] for x in cur.execute("SELECT rider_id FROM dbo.users WHERE rider_id IS NOT NULL").fetchall()} \
        if cur.execute("SELECT COL_LENGTH('dbo.users','rider_id')").fetchval() else set()
    for (rid,) in cur.execute("SELECT rider_id FROM dbo.riders").fetchall():
        city = random.choices(CITIES[:6], CITY_W[:6])[0]
        name = None if rid in linked_r else f"{random.choice(MALE)} {random.choice(LAST)}"
        cur.execute("UPDATE dbo.riders SET full_name = COALESCE(?, full_name), phone = ?, city = ?, updated_at = ? WHERE rider_id = ?",
                    name, phone(), city, now, rid)
    for (vid,) in cur.execute("SELECT vendor_id FROM dbo.vendors").fetchall():
        cur.execute("UPDATE dbo.vendors SET city = ?, country = ?, updated_at = ? WHERE vendor_id = ?",
                    random.choices(CITIES[:5], CITY_W[:5])[0], COUNTRY, now, vid)
    cur.execute("INSERT INTO dbo.migration_log (name, ran_at) VALUES ('market_pkr', ?)", now)
    conn.commit()
    conn.close()
    print("people: Pakistani cities, names and +92 phones · done")


if __name__ == "__main__":
    main()
