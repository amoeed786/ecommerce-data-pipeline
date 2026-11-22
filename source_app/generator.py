"""Fake ecommerce activity generator for ShopDB.

Usage:
    python source_app/generator.py seed      # one-time history: customers, products, ~1 year of orders
    python source_app/generator.py stream    # simulate live shoppers until Ctrl+C (marketplace orders;
                                             # vendors/riders move them - see source_app/marketplace.py)

The data is deliberately "realistic-messy" so later phases have something to analyse:
business growth over the year, weekend and Black-Friday/holiday peaks, evening shopping hours,
a few heavy-buyer customers and best-selling products (Pareto), cancellations and returns,
price changes and customers who move city (useful for SCD2 in a later phase).
"""
import argparse
import bisect
import math
import os
import random
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import accumulate

from faker import Faker

import marketplace
from db import get_connection

fake = Faker()

# category -> (min price, max price, product nouns)
CATEGORIES = {
    "Electronics": (25, 1500, ["Headphones", "Smartphone", "Laptop", "Smartwatch", "Speaker", "Tablet", "Camera", "Keyboard"]),
    "Fashion": (10, 250, ["T-Shirt", "Jeans", "Sneakers", "Jacket", "Dress", "Handbag", "Sunglasses"]),
    "Home & Kitchen": (8, 400, ["Blender", "Coffee Maker", "Cookware Set", "Desk Lamp", "Pillow", "Vacuum"]),
    "Books": (5, 60, ["Novel", "Cookbook", "Biography", "Textbook", "Comic"]),
    "Sports": (10, 500, ["Yoga Mat", "Dumbbells", "Football", "Tennis Racket", "Bike Helmet", "Running Shoes"]),
    "Beauty": (5, 120, ["Moisturizer", "Perfume", "Lipstick", "Shampoo", "Face Serum"]),
    "Toys": (5, 150, ["Puzzle", "Building Blocks", "Doll", "RC Car", "Board Game"]),
    "Grocery": (2, 50, ["Coffee Beans", "Green Tea", "Olive Oil", "Honey", "Dark Chocolate"]),
}
ADJECTIVES = ["Pro", "Classic", "Ultra", "Eco", "Smart", "Deluxe", "Mini", "Max", "Air", "Prime"]

# One market (MARKET_COUNTRY, default United States): every customer and order is in that country.
MARKET_CITIES = {
    "United States": ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix", "Philadelphia", "San Diego", "Dallas",
                      "Seattle", "Austin", "Boston", "Miami", "Atlanta", "Denver", "San Francisco"],
    "Pakistan": ["Karachi", "Lahore", "Islamabad", "Rawalpindi", "Faisalabad", "Multan", "Peshawar", "Quetta", "Sialkot", "Hyderabad"],
    "United Kingdom": ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Bristol", "Liverpool", "Edinburgh"],
}
MARKET = os.environ.get("MARKET_COUNTRY", "Pakistan")
LOCATIONS = {MARKET: MARKET_CITIES.get(MARKET, MARKET_CITIES["Pakistan"])}
COUNTRY_WEIGHTS = [1]

PAYMENT_METHODS = ["credit_card", "debit_card", "paypal", "bank_transfer", "cash_on_delivery"]
PAYMENT_WEIGHTS = [45, 20, 15, 10, 10]
# Fixed scale on purpose: pyodbc fast_executemany infers decimal scale from the first row.
DISCOUNTS = [Decimal("0.00"), Decimal("0.05"), Decimal("0.10"), Decimal("0.20")]
DISCOUNT_WEIGHTS = [70, 12, 12, 6]
HOUR_WEIGHTS = [1, .5, .3, .3, .3, .5, 1, 2, 3, 4, 4, 5, 6, 5, 5, 5, 5, 6, 7, 8, 8, 7, 5, 3]

SQL_INSERT_CUSTOMER = (
    "INSERT INTO dbo.customers (first_name, last_name, email, phone, city, country, signup_date, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?,?,?)"
)
SQL_INSERT_PRODUCT = (
    "INSERT INTO dbo.products (category_id, product_name, sku, unit_price, unit_cost, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?)"
)
SQL_INSERT_ORDER = (
    "INSERT INTO dbo.orders (customer_id, order_date, status, shipping_city, shipping_country, total_amount, created_at, updated_at) "
    "OUTPUT INSERTED.order_id VALUES (?,?,?,?,?,?,?,?)"
)
SQL_INSERT_ITEM = (
    "INSERT INTO dbo.order_items (order_id, product_id, quantity, unit_price, discount, line_total, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?,?)"
)
SQL_INSERT_PAYMENT = (
    "INSERT INTO dbo.payments (order_id, payment_method, amount, payment_status, paid_at, created_at, updated_at) "
    "VALUES (?,?,?,?,?,?,?)"
)


# ---------------------------------------------------------------- helpers
def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def now() -> datetime:
    return datetime.now().replace(microsecond=0)


def random_location() -> tuple[str, str]:
    country = random.choices(list(LOCATIONS), weights=COUNTRY_WEIGHTS)[0]
    return random.choice(LOCATIONS[country]), country


def popularity(entity_id: int, alpha: float) -> float:
    """Stable Pareto weight per id, so best-sellers / loyal customers stay the same across runs."""
    return random.Random(entity_id * 7919).paretovariate(alpha)


def ensure_categories(cur) -> dict[str, int]:
    for name in CATEGORIES:
        cur.execute(
            "IF NOT EXISTS (SELECT 1 FROM dbo.categories WHERE category_name = ?) "
            "INSERT INTO dbo.categories (category_name) VALUES (?)",
            name, name,
        )
    cur.execute("SELECT category_name, category_id FROM dbo.categories")
    return {name: cid for name, cid in cur.fetchall()}


def new_customer_row(signup: datetime) -> tuple:
    first, last = fake.first_name(), fake.last_name()
    email = f"{first}.{last}.{uuid.uuid4().hex[:6]}@{fake.free_email_domain()}".lower()
    city, country = random_location()
    signup = signup.replace(microsecond=0)
    return (first, last, email, fake.phone_number()[:30], city, country, signup.date(), signup, signup)


def new_product_rows(n: int, cat_map: dict[str, int], created_at: datetime, start_seq: int) -> list[tuple]:
    rows = []
    for i in range(n):
        cat = random.choice(list(CATEGORIES))
        lo, hi, nouns = CATEGORIES[cat]
        price = money(math.exp(random.uniform(math.log(lo), math.log(hi))))  # log-uniform: more cheap items
        cost = money(float(price) * random.uniform(0.40, 0.75))
        name = f"{fake.last_name()} {random.choice(ADJECTIVES)} {random.choice(nouns)}"
        sku = f"{cat[:3].upper()}-{start_seq + i:05d}"
        rows.append((cat_map[cat], name, sku, price, cost, created_at, created_at))
    return rows


def load_customers(cur) -> list:
    cur.execute("SELECT customer_id, created_at, city, country FROM dbo.customers ORDER BY created_at, customer_id")
    return cur.fetchall()


def load_products(cur) -> list:
    cur.execute("SELECT product_id, unit_price FROM dbo.products WHERE is_active = 1")
    return cur.fetchall()


def place_order(cur, customer, products, product_weights, order_dt, status, updated_at) -> list[tuple]:
    """Insert one order with its items (and payment when applicable). Returns (product_id, qty) pairs."""
    customer_id, _, city, country = customer
    n_items = random.choices([1, 2, 3, 4, 5], weights=[45, 28, 15, 8, 4])[0]
    chosen = {p[0]: p for p in random.choices(products, weights=product_weights, k=n_items)}.values()

    items, total = [], Decimal("0.00")
    for product_id, unit_price in chosen:
        qty = random.choices([1, 2, 3, 4], weights=[75, 15, 7, 3])[0]
        discount = random.choices(DISCOUNTS, weights=DISCOUNT_WEIGHTS)[0]
        line_total = money(unit_price * qty * (1 - discount))
        total += line_total
        items.append((product_id, qty, unit_price, discount, line_total))

    if random.random() < 0.10:  # occasionally shipped somewhere other than home (gift, travel)
        city, country = random_location()

    cur.execute(SQL_INSERT_ORDER, customer_id, order_dt, status, city, country, total, order_dt, updated_at)
    order_id = cur.fetchone()[0]
    cur.executemany(SQL_INSERT_ITEM, [(order_id, *item, order_dt, order_dt) for item in items])

    has_payment = status != "pending" and not (status == "cancelled" and random.random() < 0.5)
    if has_payment:
        method = random.choices(PAYMENT_METHODS, weights=PAYMENT_WEIGHTS)[0]
        pay_status = "refunded" if status in ("cancelled", "returned") else "completed"
        paid_at = min(order_dt + timedelta(minutes=random.randint(1, 30)), updated_at)
        cur.execute(SQL_INSERT_PAYMENT, order_id, method, total, pay_status, paid_at, paid_at, updated_at)

    return [(product_id, qty) for product_id, qty, *_ in items]


# ---------------------------------------------------------------- seed (history)
def order_timestamps(n: int, start: datetime, end: datetime) -> list[datetime]:
    days = (end.date() - start.date()).days
    day_list = [start.date() + timedelta(days=d) for d in range(days + 1)]
    weights = []
    for i, d in enumerate(day_list):
        w = 1 + 1.0 * i / max(days, 1)  # the shop grows ~2x over the period
        if d.weekday() >= 5:
            w *= 1.25  # weekends
        if (d.month == 11 and d.day >= 20) or (d.month == 12 and d.day <= 24):
            w *= 1.8  # Black Friday + holiday season
        weights.append(w)

    stamps = []
    for d in random.choices(day_list, weights=weights, k=n):
        ts = datetime.combine(d, datetime.min.time()) + timedelta(
            hours=random.choices(range(24), weights=HOUR_WEIGHTS)[0],
            minutes=random.randint(0, 59),
            seconds=random.randint(0, 59),
        )
        if start <= ts <= end:
            stamps.append(ts)
    return sorted(stamps)


def historical_status(order_dt: datetime, current: datetime) -> tuple[str, datetime]:
    age_days = (current - order_dt).total_seconds() / 86400
    if age_days < 0.5:
        status = random.choice(["pending", "paid"])
    elif age_days < 2:
        status = random.choice(["paid", "shipped"])
    elif age_days < 6:
        status = random.choices(["shipped", "delivered", "cancelled"], weights=[50, 45, 5])[0]
    else:
        status = random.choices(["delivered", "cancelled", "returned"], weights=[86, 7, 7])[0]
    offset_days = {
        "pending": 0, "paid": 0.02, "shipped": 1,
        "delivered": random.uniform(2, 6), "cancelled": random.uniform(0, 1), "returned": random.uniform(8, 20),
    }[status]
    return status, min(order_dt + timedelta(days=offset_days), current).replace(microsecond=0)


def seed(args) -> None:
    random.seed(args.random_seed)
    Faker.seed(args.random_seed)
    current = now()
    start = current - timedelta(days=args.days)

    conn = get_connection()
    cur = conn.cursor()
    cur.fast_executemany = True

    cur.execute("SELECT COUNT(*) FROM dbo.orders")
    if cur.fetchone()[0] > 0 and not args.force:
        print("ShopDB already has orders - skipping. Use --force to add another batch of history, "
              "or `python source_app/setup_db.py --reset` to start clean.")
        return

    t0 = time.time()
    cat_map = ensure_categories(cur)

    cur.execute("SELECT ISNULL(MAX(product_id), 0) FROM dbo.products")
    next_seq = cur.fetchone()[0] + 1
    cur.executemany(SQL_INSERT_PRODUCT, new_product_rows(args.products, cat_map, start, next_seq))
    cur.execute(
        "INSERT INTO dbo.inventory (product_id, quantity_on_hand, updated_at) "
        "SELECT p.product_id, ABS(CHECKSUM(NEWID())) % 450 + 50, ? FROM dbo.products p "
        "WHERE NOT EXISTS (SELECT 1 FROM dbo.inventory i WHERE i.product_id = p.product_id)",
        current,
    )
    print(f"  products:  {args.products}")

    customer_rows = []
    for _ in range(args.customers):
        if random.random() < 0.30:  # existed before the simulated period
            signup = fake.date_time_between(start_date=start - timedelta(days=730), end_date=start)
        else:
            signup = fake.date_time_between(start_date=start, end_date=current)
        customer_rows.append(new_customer_row(signup))
    cur.executemany(SQL_INSERT_CUSTOMER, customer_rows)
    conn.commit()
    print(f"  customers: {args.customers}")

    customers = load_customers(cur)
    signup_times = [c[1] for c in customers]
    cust_cum = list(accumulate(popularity(c[0], 1.5) for c in customers))
    products = load_products(cur)
    prod_weights = [popularity(p[0], 1.2) for p in products]

    placed = 0
    for ts in order_timestamps(args.orders, start, current):
        k = bisect.bisect_right(signup_times, ts)  # only customers who had signed up by then
        if k == 0:
            continue
        customer = random.choices(customers[:k], cum_weights=cust_cum[:k])[0]
        status, updated_at = historical_status(ts, current)
        place_order(cur, customer, products, prod_weights, ts, status, updated_at)
        placed += 1
        if placed % 1000 == 0:
            conn.commit()
            print(f"  orders:    {placed:,} ...")
    conn.commit()
    conn.close()
    print(f"Seed complete: {placed:,} orders over {args.days} days in {time.time() - t0:.0f}s.")


# ---------------------------------------------------------------- stream (live)
def advance_orders(cur, ts: datetime, limit: int) -> Counter:
    """(v1, pre-marketplace) Move open orders along: pending -> paid -> shipped -> delivered.
    No longer used by `stream`: in the marketplace, vendors and riders (or their autopilot) move orders."""
    changes = Counter()
    cur.execute(
        "SELECT TOP (?) order_id, status, total_amount FROM dbo.orders "
        "WHERE status IN ('pending','paid','shipped') ORDER BY NEWID()",
        limit,
    )
    for order_id, status, total in cur.fetchall():
        if status == "pending":
            new_status = "cancelled" if random.random() < 0.08 else "paid"
            if new_status == "paid":
                method = random.choices(PAYMENT_METHODS, weights=PAYMENT_WEIGHTS)[0]
                cur.execute(SQL_INSERT_PAYMENT, order_id, method, total, "completed", ts, ts, ts)
        elif status == "paid":
            new_status = "cancelled" if random.random() < 0.03 else "shipped"
            if new_status == "cancelled":
                cur.execute(
                    "UPDATE dbo.payments SET payment_status = 'refunded', updated_at = ? WHERE order_id = ?",
                    ts, order_id,
                )
        else:
            new_status = "delivered"
        cur.execute("UPDATE dbo.orders SET status = ?, updated_at = ? WHERE order_id = ?", new_status, ts, order_id)
        changes[new_status] += 1

    if random.random() < 0.15:  # a customer returns a recent delivery
        cur.execute(
            "SELECT TOP 1 order_id FROM dbo.orders WHERE status = 'delivered' "
            "AND order_date >= DATEADD(day, -30, ?) ORDER BY NEWID()",
            ts,
        )
        row = cur.fetchone()
        if row:
            cur.execute("UPDATE dbo.orders SET status = 'returned', updated_at = ? WHERE order_id = ?", ts, row[0])
            cur.execute(
                "UPDATE dbo.payments SET payment_status = 'refunded', updated_at = ? WHERE order_id = ?", ts, row[0]
            )
            changes["returned"] += 1
    return changes


def random_changes(cur, ts: datetime) -> list[str]:
    """Occasional master-data changes - these are what SCD2 will track later."""
    events = []
    if random.random() < 0.05:
        cur.execute("SELECT TOP 1 product_id, unit_price FROM dbo.products WHERE is_active = 1 ORDER BY NEWID()")
        pid, price = cur.fetchone()
        raw = max(99.0, float(price) * random.uniform(0.85, 1.15))
        step = 50 if raw < 1000 else 100 if raw < 10000 else 500 if raw < 100000 else 1000
        new_price = Decimal(int((raw + step - 1) // step) * step - 1)   # PKR retail price points: 349, 1,499, 24,999
        cur.execute("UPDATE dbo.products SET unit_price = ?, updated_at = ? WHERE product_id = ?", new_price, ts, pid)
        events.append(f"price change p#{pid} {price}->{new_price}")
    if random.random() < 0.05:
        city, country = random_location()
        cur.execute("SELECT TOP 1 customer_id FROM dbo.customers ORDER BY NEWID()")
        cid = cur.fetchone()[0]
        cur.execute(
            "UPDATE dbo.customers SET city = ?, country = ?, updated_at = ? WHERE customer_id = ?",
            city, country, ts, cid,
        )
        events.append(f"customer c#{cid} moved to {city}")
    cur.execute(
        "UPDATE dbo.inventory SET quantity_on_hand = quantity_on_hand + 300, updated_at = ? "
        "WHERE quantity_on_hand < 20",
        ts,
    )
    if cur.rowcount > 0:
        events.append(f"restocked {cur.rowcount} products")
    return events


def stream(args) -> None:
    conn = get_connection()
    cur = conn.cursor()
    cur.fast_executemany = True
    ensure_categories(cur)
    print(f"Streaming live shop activity every {args.interval}s - press Ctrl+C to stop.")

    tick = 0
    try:
        while args.ticks == 0 or tick < args.ticks:
            tick += 1
            ts = now()

            new_customers = random.choices([0, 1, 2], weights=[60, 30, 10])[0]
            if new_customers:
                cur.executemany(SQL_INSERT_CUSTOMER, [new_customer_row(ts) for _ in range(new_customers)])

            customers = load_customers(cur)
            cust_weights = [popularity(c[0], 1.5) for c in customers]
            products = load_products(cur)
            prod_weights = [popularity(p[0], 1.2) for p in products]

            # Marketplace orders: split per vendor into shipments, then vendors/riders (or you) take over.
            n_orders, skipped = random.randint(args.min_orders, args.max_orders), 0
            for _ in range(n_orders):
                customer = random.choices(customers, weights=cust_weights)[0]
                n_items = random.choices([1, 2, 3, 4], weights=[50, 28, 15, 7])[0]
                picks = {p[0] for p in random.choices(products, weights=prod_weights, k=n_items)}
                lines = [(pid, random.choices([1, 2, 3], weights=[80, 15, 5])[0]) for pid in picks]
                method = random.choices(marketplace.PAYMENT_METHODS, weights=PAYMENT_WEIGHTS)[0]
                coupon = random.choices(["", "SAVE5", "SAVE10", "SAVE20"], weights=[75, 10, 10, 5])[0]
                try:
                    marketplace.create_order(cur, customer[0], lines, method, coupon, ts)
                except marketplace.MarketplaceError:
                    skipped += 1  # e.g. out of stock - a real shopper would see an error too

            events = random_changes(cur, ts)
            if random.random() < 0.15:
                returned = marketplace.random_return(cur, ts)
                if returned:
                    events.append(f"order #{returned} returned")
            conn.commit()

            extra = f" | {'; '.join(events)}" if events else ""
            print(f"[{ts:%H:%M:%S}] tick {tick}: +{new_customers} customers, +{n_orders - skipped} orders"
                  f"{f' ({skipped} failed: out of stock)' if skipped else ''}{extra}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        conn.commit()
        print("\nStopped.")
    finally:
        conn.close()


# ---------------------------------------------------------------- CLI
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="generate historical data")
    p_seed.add_argument("--customers", type=int, default=2000)
    p_seed.add_argument("--products", type=int, default=300)
    p_seed.add_argument("--orders", type=int, default=15000)
    p_seed.add_argument("--days", type=int, default=365, help="how many days of history")
    p_seed.add_argument("--random-seed", type=int, default=42, help="for reproducible data")
    p_seed.add_argument("--force", action="store_true", help="seed even if orders already exist")
    p_seed.set_defaults(func=seed)

    p_stream = sub.add_parser("stream", help="simulate live activity")
    p_stream.add_argument("--interval", type=float, default=5, help="seconds between ticks")
    p_stream.add_argument("--min-orders", type=int, default=1)
    p_stream.add_argument("--max-orders", type=int, default=5)
    p_stream.add_argument("--ticks", type=int, default=0, help="stop after N ticks (0 = run forever)")
    p_stream.set_defaults(func=stream)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
