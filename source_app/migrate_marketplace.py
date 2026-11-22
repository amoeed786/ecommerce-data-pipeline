"""Upgrade ShopDB to the marketplace model (v2) and backfill existing data. Safe to re-run.

What it does (each step only touches what's missing):
  1. Create the v2 tables/columns (schema_marketplace.sql)
  2. Seed vendors and riders (autopilot "bots" + one human-operated demo vendor and rider)
  3. Give every product a vendor, a rating, a description and sometimes a "was" price
  4. Backfill one shipment per vendor for every existing order, with timestamps that fit the
     order's history (so delivery-time analytics work for the past year too)
  5. Default settings + demo logins for every role

Usage:
    python source_app/migrate_marketplace.py
"""
import random
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from faker import Faker

from db import get_connection
from passwords import hash_password

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA_FILE = Path(__file__).with_name("schema_marketplace.sql")
PAYMENTS_SCHEMA_FILE = Path(__file__).with_name("schema_payments.sql")
DEMO_PASSWORD = "Demo@123"
ADMIN_PASSWORD = "Admin@123"

# name, tagline, city, country, categories served, autopilot?, prep seconds
VENDORS = [
    ("TechHub Electronics", "Genuine gadgets with official warranty", "Karachi", "Pakistan", ["Electronics"], 1, 40),
    ("Gadget Galaxy", "The latest tech at the best prices", "Dubai", "United Arab Emirates", ["Electronics"], 1, 25),
    ("StyleStreet", "Everyday fashion, delivered fast", "Lahore", "Pakistan", ["Fashion"], 1, 30),
    ("UrbanThreads", "Streetwear and sports apparel", "London", "United Kingdom", ["Fashion", "Sports"], 1, 35),
    ("HomeNest", "Make your house a home", "Toronto", "Canada", ["Home & Kitchen"], 1, 45),
    ("PageTurner Books", "Stories for every reader", "New York", "United States", ["Books"], 1, 20),
    ("FitLife Sports", "Gear up and get moving", "Berlin", "Germany", ["Sports"], 1, 30),
    ("GlowUp Beauty", "Skincare and cosmetics you'll love", "Islamabad", "Pakistan", ["Beauty"], 1, 20),
    ("ToyBox", "Fun for every age", "Manchester", "United Kingdom", ["Toys"], 1, 30),
    ("FreshMart", "Pantry staples and treats", "Karachi", "Pakistan", ["Grocery"], 1, 15),
    ("Kartora Demo Store", "Your own store - you accept and pack these orders", "Lahore", "Pakistan", [], 0, 30),
]
DEMO_VENDOR = "Kartora Demo Store"
BOT_RIDERS = 8

DESCRIPTIONS = {
    "Electronics": "Reliable performance with a sleek design. Ships with an official warranty.",
    "Fashion": "Comfortable everyday fit in quality fabric. Easy care, machine washable.",
    "Home & Kitchen": "Durable, easy to clean and built for daily use at home.",
    "Books": "A reader favourite - perfect for your shelf or as a gift.",
    "Sports": "Built for training and play, tested by athletes.",
    "Beauty": "Dermatologist tested, gentle on skin, cruelty free.",
    "Toys": "Safe, fun and educational. Suitable for ages 3+.",
    "Grocery": "Fresh, high-quality and carefully packed.",
}


def run_schema(cur) -> None:
    for f in (SCHEMA_FILE, PAYMENTS_SCHEMA_FILE):
        for batch in re.split(r"^\s*GO\s*$", f.read_text(encoding="utf-8"), flags=re.M | re.I):
            if batch.strip():
                cur.execute(batch)


def seed_vendors(cur, ts) -> dict[str, int]:
    for name, tagline, city, country, _, bot, prep in VENDORS:
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM dbo.vendors WHERE vendor_name = ?) "
            "INSERT INTO dbo.vendors (vendor_name, tagline, city, country, rating, is_bot, prep_seconds, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            name, name, tagline, city, country, Decimal(str(round(random.uniform(4.2, 4.9), 1))), bot, prep, ts, ts,
        )
    return dict(cur.execute("SELECT vendor_name, vendor_id FROM dbo.vendors").fetchall())


def assign_products(cur, vendor_ids: dict[str, int], ts) -> int:
    rows = cur.execute(
        "SELECT p.product_id, c.category_name FROM dbo.products p JOIN dbo.categories c ON c.category_id = p.category_id "
        "WHERE p.vendor_id IS NULL ORDER BY p.product_id"
    ).fetchall()
    by_cat: dict[str, list[int]] = {}
    for name, *_rest in VENDORS:
        for cat in _rest[3]:
            by_cat.setdefault(cat, []).append(vendor_ids[name])
    sold = dict(cur.execute("SELECT product_id, SUM(quantity) FROM dbo.order_items GROUP BY product_id").fetchall())
    updates = []
    for product_id, category in rows:
        if product_id % 12 == 0:
            vendor = vendor_ids[DEMO_VENDOR]
        else:
            options = by_cat.get(category) or list(vendor_ids.values())
            vendor = options[product_id % len(options)]
        rating = Decimal(str(round(min(5.0, random.triangular(3.3, 5.0, 4.6)), 1)))
        reviews = int((sold.get(product_id) or 0) * random.uniform(0.05, 0.25)) + random.randint(0, 12)
        updates.append((vendor, rating, reviews, DESCRIPTIONS.get(category), random.random() < 0.45, ts, product_id))
    for vendor, rating, reviews, desc, on_sale, ts_, pid in updates:
        cur.execute(
            "UPDATE dbo.products SET vendor_id = ?, rating = ?, review_count = ?, description = ?, "
            "list_price = CASE WHEN ? = 1 THEN ROUND(unit_price * ?, 2) ELSE NULL END, updated_at = ? WHERE product_id = ?",
            vendor, rating, reviews, desc, int(on_sale), Decimal(str(round(random.uniform(1.1, 1.6), 2))), ts_, pid,
        )
    return len(updates)


def seed_riders(cur, ts) -> list[int]:
    if not cur.execute("SELECT COUNT(*) FROM dbo.riders").fetchval():
        fake = Faker()
        cities = ["Karachi", "Lahore", "Islamabad", "Dubai", "London", "New York", "Toronto", "Berlin"]
        for i in range(BOT_RIDERS):
            cur.execute(
                "INSERT INTO dbo.riders (full_name, phone, city, vehicle, status, is_bot, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                fake.name(), fake.phone_number()[:30], cities[i % len(cities)],
                random.choice(["bike", "bike", "car", "van"]), "available", 1, ts, ts,
            )
        cur.execute(
            "INSERT INTO dbo.riders (full_name, phone, city, vehicle, status, is_bot, created_at, updated_at) "
            "VALUES ('Demo Rider', '+92 300 0000000', 'Lahore', 'bike', 'offline', 0, ?, ?)", ts, ts,
        )
    return [r[0] for r in cur.execute("SELECT rider_id FROM dbo.riders WHERE is_bot = 1").fetchall()]


def backfill_shipments(cur, bot_riders: list[int], current: datetime) -> int:
    groups = cur.execute(
        """
        SELECT o.order_id, o.status, o.order_date, o.updated_at, p.vendor_id
        FROM dbo.orders o
        JOIN dbo.order_items i ON i.order_id = o.order_id
        JOIN dbo.products p ON p.product_id = i.product_id
        WHERE NOT EXISTS (SELECT 1 FROM dbo.shipments s WHERE s.order_id = o.order_id)
        GROUP BY o.order_id, o.status, o.order_date, o.updated_at, p.vendor_id
        """
    ).fetchall()
    # order status -> how far its shipments got
    stage_of = {"pending": 0, "processing": 2, "paid": 2, "shipped": 5, "delivered": 6, "returned": 6, "cancelled": -1}
    status_of = {0: "pending", 2: "packing", 5: "picked_up", 6: "delivered"}
    rows = []
    for order_id, status, order_date, updated_at, vendor_id in groups:
        stage = stage_of.get(status, 0)
        # minutes for: confirm, vendor accept, pack, wait for rider, pickup, ride
        gaps = [random.uniform(2, 30), random.uniform(5, 60), random.uniform(20, 240),
                random.uniform(5, 45), random.uniform(5, 40), random.uniform(20, 180)]
        end = updated_at if status in ("delivered", "shipped", "paid", "processing") else current
        if status == "returned":
            end = order_date + timedelta(days=random.uniform(2, 5))
        span = max((end - order_date).total_seconds() / 60, 1)
        used = sum(gaps[:max(stage, 0)])
        if used > span * 0.95:  # squeeze the timeline so it fits before the order's last change
            gaps = [g * span * 0.95 / used for g in gaps]
        t = order_date
        stamps = []
        for g in gaps:
            t = t + timedelta(minutes=g)
            stamps.append(t.replace(microsecond=0))
        confirmed, accepted, ready, assigned, picked, delivered = stamps
        if status == "delivered":
            delivered = max(updated_at, picked)
        keep = lambda i, v: v if stage >= i else None  # noqa: E731
        ship_status = "cancelled" if stage < 0 else ("returned" if status == "returned" else status_of[stage])
        rider = random.choice(bot_riders) if stage >= 4 else None
        rows.append((
            order_id, vendor_id, rider, ship_status, Decimal(str(round(random.uniform(1.5, 4.5), 2))),
            keep(1, confirmed), keep(2, accepted), keep(3, ready), keep(4, assigned), keep(5, picked), keep(6, delivered),
            updated_at if stage < 0 else None, order_date, updated_at,
        ))
    cur.fast_executemany = True
    for i in range(0, len(rows), 2000):
        cur.executemany(
            "INSERT INTO dbo.shipments (order_id, vendor_id, rider_id, status, delivery_fee, confirmed_at, accepted_at, "
            "ready_at, assigned_at, picked_up_at, delivered_at, cancelled_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows[i:i + 2000],
        )
    cur.fast_executemany = False
    return len(rows)


def seed_users(cur, vendor_ids: dict[str, int], ts) -> list[tuple[str, str, str]]:
    created = []

    def add(email, password, role, name, customer_id=None, vendor_id=None, rider_id=None):
        if cur.execute("SELECT 1 FROM dbo.users WHERE email = ?", email).fetchone():
            return
        cur.execute(
            "INSERT INTO dbo.users (email, password_hash, role, display_name, customer_id, vendor_id, rider_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            email, hash_password(password), role, name, customer_id, vendor_id, rider_id, ts, ts,
        )
        created.append((role, email, password))

    add("admin@kartora.test", ADMIN_PASSWORD, "admin", "Store Admin")
    # demo customer: someone with a believable order history (8-25 orders)
    cust = cur.execute(
        "SELECT TOP 1 c.customer_id, c.first_name + ' ' + c.last_name FROM dbo.customers c "
        "JOIN dbo.orders o ON o.customer_id = c.customer_id GROUP BY c.customer_id, c.first_name, c.last_name "
        "HAVING COUNT(*) BETWEEN 8 AND 25 ORDER BY c.customer_id"
    ).fetchone()
    if cust:
        add("customer@kartora.test", DEMO_PASSWORD, "customer", cust[1], customer_id=cust[0])
    for name, vid in vendor_ids.items():
        slug = re.sub(r"[^a-z]", "", name.lower().split()[0])
        add("vendor@kartora.test" if name == DEMO_VENDOR else f"{slug}@kartora.test",
            DEMO_PASSWORD, "vendor", name, vendor_id=vid)
    rider = cur.execute("SELECT rider_id FROM dbo.riders WHERE is_bot = 0").fetchval()
    if rider:
        add("rider@kartora.test", DEMO_PASSWORD, "rider", "Demo Rider", rider_id=rider)
    return created


START_RATES = {"default": "0.10", "Electronics": "0.05", "Grocery": "0.06", "Books": "0.08", "Beauty": "0.12",
               "Fashion": "0.15"}


def seed_commission(cur) -> None:
    import payments as pay
    from decimal import Decimal
    if cur.execute("SELECT COUNT(*) FROM dbo.commission_rules").fetchval():
        return
    pay.set_rate(cur, "default", None, Decimal(START_RATES["default"]))
    for cat_name, cat_id in cur.execute("SELECT category_name, category_id FROM dbo.categories").fetchall():
        if cat_name in START_RATES:
            pay.set_rate(cur, "category", cat_id, Decimal(START_RATES[cat_name]))


def backfill_ledger(cur, ts: datetime) -> tuple[int, int, int]:
    """Sale + commission rows for every past delivered line, reversals for returned orders, and weekly payouts
    (already paid) for everything that became payable before this week. Set-based: fast for 30k+ lines."""
    import payments as pay
    hold_hours = int(pay.HOLD_DAYS * 24)
    added = cur.execute(f"""
        INSERT INTO dbo.ledger_entries (vendor_id, order_id, shipment_id, order_item_id, entry_type, amount,
                                        commission_rate, status, available_at, created_at, updated_at)
        SELECT s.vendor_id, i.order_id, s.shipment_id, i.order_item_id, t.entry_type,
               CASE t.entry_type WHEN 'sale' THEN i.line_total ELSE -ROUND(i.line_total * r.rate, 2) END,
               r.rate,
               CASE WHEN DATEADD(hour, {hold_hours}, s.delivered_at) <= ? THEN 'available' ELSE 'pending' END,
               DATEADD(hour, {hold_hours}, s.delivered_at), s.delivered_at, ?
        FROM dbo.order_items i
        JOIN dbo.shipments s ON s.shipment_id = i.shipment_id
        JOIN dbo.products p ON p.product_id = i.product_id
        CROSS APPLY (SELECT COALESCE(
            (SELECT rate FROM dbo.commission_rules WHERE scope = 'vendor' AND vendor_id = s.vendor_id),
            (SELECT rate FROM dbo.commission_rules WHERE scope = 'category' AND category_id = p.category_id),
            (SELECT rate FROM dbo.commission_rules WHERE scope = 'default'), 0.10) AS rate) r
        CROSS JOIN (VALUES ('sale'), ('commission')) t(entry_type)
        WHERE s.status IN ('delivered', 'returned') AND s.delivered_at IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM dbo.ledger_entries l WHERE l.order_item_id = i.order_item_id AND l.entry_type = t.entry_type)
    """, ts, ts).rowcount
    reversed_ = cur.execute("""
        INSERT INTO dbo.ledger_entries (vendor_id, order_id, shipment_id, order_item_id, entry_type, amount,
                                        commission_rate, status, available_at, created_at, updated_at)
        SELECT l.vendor_id, l.order_id, l.shipment_id, l.order_item_id, l.entry_type + '_reversal', -l.amount,
               l.commission_rate, 'available', o.updated_at, o.updated_at, ?
        FROM dbo.ledger_entries l JOIN dbo.orders o ON o.order_id = l.order_id
        WHERE o.status = 'returned' AND l.entry_type IN ('sale', 'commission')
          AND NOT EXISTS (SELECT 1 FROM dbo.ledger_entries r WHERE r.order_item_id = l.order_item_id
                          AND r.entry_type = l.entry_type + '_reversal')
    """, ts).rowcount
    # settle history: one paid payout per seller per week, for everything payable before this week started
    week_start = (ts - timedelta(days=ts.weekday())).replace(hour=0, minute=0, second=0)
    groups = cur.execute("""
        SELECT vendor_id, DATEADD(week, DATEDIFF(week, 0, available_at), 0) AS wk, SUM(amount), COUNT(*)
        FROM dbo.ledger_entries WHERE status = 'available' AND payout_id IS NULL AND available_at < ?
        GROUP BY vendor_id, DATEADD(week, DATEDIFF(week, 0, available_at), 0) HAVING SUM(amount) > 0
    """, week_start).fetchall()
    for vendor_id, wk, total, n in groups:
        paid_at = min(wk + timedelta(days=7), ts)  # never a payout dated in the future
        pid = cur.execute(
            "INSERT INTO dbo.payouts (vendor_id, amount, entry_count, status, reference, created_at, paid_at, updated_at) "
            "OUTPUT INSERTED.payout_id VALUES (?,?,?, 'paid', ?, ?, ?, ?)",
            vendor_id, total, n, f"HIST-{vendor_id}-{wk:%Y%m%d}", paid_at, paid_at, ts).fetchval()
        # same week key as the GROUP BY above (DATEDIFF(week) counts from Sunday - a plain date range would not match)
        cur.execute("UPDATE dbo.ledger_entries SET payout_id = ?, status = 'paid', updated_at = ? "
                    "WHERE vendor_id = ? AND status = 'available' AND payout_id IS NULL AND available_at < ? "
                    "AND DATEADD(week, DATEDIFF(week, 0, available_at), 0) = ?", pid, ts, vendor_id, week_start, wk)
    cur.execute("UPDATE dbo.payments SET provider = 'cod' WHERE provider IS NULL AND payment_method = 'cash_on_delivery'")
    cur.execute("UPDATE dbo.payments SET provider = 'simulator' WHERE provider IS NULL AND payment_status <> 'pending'")
    return added, reversed_, len(groups)


def main() -> None:
    random.seed(7)
    ts = datetime.now().replace(microsecond=0)
    conn = get_connection(autocommit=True)
    run_schema(conn.cursor())
    conn.close()
    print("1. schema: v2 tables and columns in place")

    conn = get_connection()
    cur = conn.cursor()
    vendor_ids = seed_vendors(cur, ts)
    print(f"2. vendors: {len(vendor_ids)}")
    print(f"3. products given a vendor/rating/description: {assign_products(cur, vendor_ids, ts)}")
    bot_riders = seed_riders(cur, ts)
    print(f"   riders: {len(bot_riders)} autopilot + 1 demo")
    conn.commit()

    n = backfill_shipments(cur, bot_riders, ts)
    linked = cur.execute(
        "UPDATE i SET shipment_id = s.shipment_id, updated_at = ? FROM dbo.order_items i "
        "JOIN dbo.products p ON p.product_id = i.product_id "
        "JOIN dbo.shipments s ON s.order_id = i.order_id AND s.vendor_id = p.vendor_id WHERE i.shipment_id IS NULL", ts,
    ).rowcount
    conn.commit()
    print(f"4. shipments backfilled: {n:,} (order lines linked: {linked:,})")

    from marketplace import SETTINGS_DEFAULTS
    for key, value in SETTINGS_DEFAULTS.items():
        cur.execute("IF NOT EXISTS (SELECT 1 FROM dbo.app_settings WHERE setting_key = ?) "
                    "INSERT INTO dbo.app_settings (setting_key, setting_value) VALUES (?, ?)", key, key, value)
    created = seed_users(cur, vendor_ids, ts)
    conn.commit()
    print("5. settings + demo logins")
    for role, email, password in created:
        print(f"     {role:<9} {email:<32} {password}")
    seed_commission(cur)
    added, reversed_, payouts = backfill_ledger(cur, ts)
    conn.commit()
    conn.close()
    print(f"6. commission + ledger: {added:,} sale/commission entries, {reversed_:,} return reversals, "
          f"{payouts:,} past weekly payouts settled")
    print("Done. Run ingestion/extract.py to bring the new tables into the lake.")


if __name__ == "__main__":
    main()
