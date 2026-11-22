"""Read queries and simple writes for ShopLite. Order-flow rules live in source_app/marketplace.py;
this module is the SQL behind each screen (storefront, customer, vendor, rider, admin)."""
from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from source_app import marketplace as mp  # noqa: E402
from source_app.db import get_connection  # noqa: E402

ShopError = mp.MarketplaceError

# ---- short-lived cache for the public catalog -------------------------------------------------
# Catalog pages are read far more than they change, and "sold" counts aggregate every order line.
# Results are reused for CATALOG_TTL seconds and dropped immediately when a seller/admin changes a
# product. Checkout never uses the cache: it re-reads price and stock under a lock.
CATALOG_TTL, STALE_OK, _CACHE_MAX = 15, 300, 500
_cache: dict[tuple, tuple[float, object]] = {}
_refreshing: set[tuple] = set()


def catalog_cached(fn):
    """Fresh for CATALOG_TTL s. After that, for up to STALE_OK s, visitors still get the cached copy instantly
    while ONE background thread rebuilds it ("stale-while-revalidate") - nobody waits for the database.
    Seller/admin edits call clear_catalog_cache(), so those always show immediately."""
    def compute(key, args, kwargs):
        try:
            value = fn(*args, **kwargs)
            if len(_cache) >= _CACHE_MAX:  # many different searches: start over rather than grow forever
                _cache.clear()
            _cache[key] = (time.monotonic(), value)
            return value
        finally:
            _refreshing.discard(key)

    def wrapper(*args, **kwargs):
        key = (fn.__name__, args, tuple(sorted(kwargs.items())))
        hit = _cache.get(key)
        age = time.monotonic() - hit[0] if hit else None
        if hit and age < CATALOG_TTL:
            return hit[1]
        if hit and age < STALE_OK:
            if key not in _refreshing:
                _refreshing.add(key)
                threading.Thread(target=compute, args=(key, args, kwargs), daemon=True).start()
            return hit[1]
        return compute(key, args, kwargs)
    wrapper.__name__ = fn.__name__
    return wrapper


def clear_catalog_cache() -> None:
    _cache.clear()


def warm_catalog() -> None:
    """Fill the cache for the pages most visitors open first (called at app startup).
    The calls must match how the API calls search_products (same positional arguments) to share cache keys."""
    def listing(category_id=None):
        return search_products(category_id, None, None, None, None, None, False, False, "popular", 40, 0)
    pages = [home, categories, listing]
    try:
        pages += [(lambda cid: lambda: listing(cid))(c["category_id"]) for c in categories()]
    except Exception:  # noqa: BLE001
        pass
    for page in pages:
        try:
            page()
        except Exception:  # noqa: BLE001 - warming is best-effort; the page will just build on first visit
            pass


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def query(sql: str, *params) -> list[dict]:
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


def execute(sql: str, *params) -> int:
    conn = get_connection()
    try:
        n = conn.cursor().execute(sql, *params).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


@contextmanager
def tx():
    """A cursor inside one transaction: commit on success, roll back on any error."""
    conn = get_connection()
    try:
        yield conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ================================================================== storefront (public)
PRODUCT_SQL = """
    SELECT p.product_id, p.product_name, p.sku, p.unit_price, p.list_price,
           CASE WHEN p.list_price > p.unit_price THEN CAST(ROUND(100 * (1 - p.unit_price / p.list_price), 0) AS INT) END AS discount_pct,
           coalesce(p.rating, 0) AS rating, p.review_count, p.description, p.created_at, p.is_active, p.image_url,
           p.brand, p.gallery,
           c.category_id, c.category_name, v.vendor_id, v.vendor_name, v.rating AS vendor_rating,
           coalesce(i.quantity_on_hand, 0) AS stock, coalesce(s.sold, 0) AS sold
    FROM dbo.products p
    JOIN dbo.categories c ON c.category_id = p.category_id
    JOIN dbo.vendors v ON v.vendor_id = p.vendor_id AND v.is_active = 1
    LEFT JOIN dbo.inventory i ON i.product_id = p.product_id
    LEFT JOIN (SELECT product_id, SUM(quantity) AS sold FROM dbo.order_items GROUP BY product_id) s ON s.product_id = p.product_id
    WHERE p.is_active = 1
"""
PHOTO_DIR = "/static/img/products/"


def with_images(rows: list[dict]) -> list[dict]:
    """gallery (JSON list of photo names) -> `images`: [{src, srcset}] in 240/480/960 WebP.
    A seller-uploaded photo (image_url) always comes first."""
    import json
    for r in rows:
        names = json.loads(r.pop("gallery", None) or "[]")
        imgs = [{"src": f"{PHOTO_DIR}{n}-480.webp",
                 "srcset": f"{PHOTO_DIR}{n}-240.webp 240w, {PHOTO_DIR}{n}-480.webp 480w, {PHOTO_DIR}{n}-960.webp 960w"}
                for n in names]
        url = r.get("image_url")
        if url and not url.startswith(PHOTO_DIR):
            imgs.insert(0, {"src": url, "srcset": None})
        r["images"] = imgs
    return rows


SORTS = {
    "popular": "sold DESC", "price_asc": "unit_price ASC", "price_desc": "unit_price DESC",
    "rating": "rating DESC, review_count DESC", "newest": "created_at DESC", "discount": "discount_pct DESC",
}


@catalog_cached
def categories() -> list[dict]:
    return query("""
        SELECT c.category_id, c.category_name, COUNT(p.product_id) AS products
        FROM dbo.categories c LEFT JOIN dbo.products p ON p.category_id = c.category_id AND p.is_active = 1
        GROUP BY c.category_id, c.category_name ORDER BY c.category_name
    """)


@catalog_cached
def search_products(category_id=None, q=None, vendor_id=None, min_price=None, max_price=None,
                    min_rating=None, on_sale=False, in_stock=False, sort="popular", limit=40, offset=0) -> dict:
    where, params = [], []
    if category_id:
        where.append("c.category_id = ?"); params.append(category_id)
    if vendor_id:
        where.append("v.vendor_id = ?"); params.append(vendor_id)
    if q:
        where.append("(p.product_name LIKE ? OR c.category_name LIKE ? OR v.vendor_name LIKE ? OR p.sku LIKE ? OR p.brand LIKE ?)")
        params += [f"%{q}%"] * 5
    if min_price is not None:
        where.append("p.unit_price >= ?"); params.append(min_price)
    if max_price is not None:
        where.append("p.unit_price <= ?"); params.append(max_price)
    if min_rating:
        where.append("p.rating >= ?"); params.append(min_rating)
    if on_sale:
        where.append("p.list_price > p.unit_price")
    if in_stock:
        where.append("i.quantity_on_hand > 0")
    base = PRODUCT_SQL + "".join(f" AND {w}" for w in where)
    total = scalar(f"SELECT COUNT(*) FROM ({base}) x", *params)
    rows = query(f"SELECT * FROM ({base}) x ORDER BY {SORTS.get(sort, SORTS['popular'])}, product_id "
                 f"OFFSET ? ROWS FETCH NEXT ? ROWS ONLY", *params, int(offset), int(limit))
    return {"total": total, "items": with_images(rows)}


@catalog_cached
def home() -> dict:
    def top(order: str, extra: str = "", n: int = 12):
        return with_images(query(f"SELECT TOP {n} * FROM ({PRODUCT_SQL} {extra}) x ORDER BY {order}"))
    return {
        "flash_deals": top("discount_pct DESC, sold DESC", "AND p.list_price > p.unit_price"),
        "best_sellers": top("sold DESC"),
        "top_rated": top("rating DESC, review_count DESC", "AND p.review_count >= 5"),
        "categories": categories(),
        "vendors": query("""
            SELECT v.vendor_id, v.vendor_name, v.tagline, v.rating, v.city, COUNT(p.product_id) AS products
            FROM dbo.vendors v LEFT JOIN dbo.products p ON p.vendor_id = v.vendor_id AND p.is_active = 1
            WHERE v.is_active = 1 GROUP BY v.vendor_id, v.vendor_name, v.tagline, v.rating, v.city ORDER BY v.rating DESC
        """),
    }


@catalog_cached
def product_detail(product_id: int) -> dict | None:
    import json
    p = query_one(f"SELECT x.*, pp.specs FROM ({PRODUCT_SQL}) x JOIN dbo.products pp ON pp.product_id = x.product_id "
                  f"WHERE x.product_id = ?", product_id)
    if p:
        with_images([p])
        p["specs"] = json.loads(p["specs"] or "{}")
        p["related"] = with_images(query(f"SELECT TOP 8 * FROM ({PRODUCT_SQL}) x WHERE category_id = ? AND product_id <> ? "
                                         f"ORDER BY sold DESC", p["category_id"], product_id))
        p["reviews"] = query("""
            SELECT TOP 8 r.review_id, r.rating, r.title, r.body, r.verified, r.helpful, r.created_at,
                   c.first_name + ' ' + LEFT(c.last_name, 1) + '.' AS author, c.city
            FROM dbo.reviews r LEFT JOIN dbo.customers c ON c.customer_id = r.customer_id
            WHERE r.product_id = ? ORDER BY r.helpful DESC, r.created_at DESC""", product_id)
        # star histogram scaled to the product's total review count (written reviews are a sample)
        hist = {r["rating"]: r["n"] for r in query(
            "SELECT rating, COUNT(*) AS n FROM dbo.reviews WHERE product_id = ? GROUP BY rating", product_id)}
        sample = sum(hist.values()) or 1
        p["rating_histogram"] = {s: round(100 * hist.get(s, 0) / sample) for s in (5, 4, 3, 2, 1)}
    return p


@catalog_cached
def vendor_public(vendor_id: int) -> dict | None:
    return query_one("""
        SELECT v.vendor_id, v.vendor_name, v.tagline, v.city, v.country, v.rating, v.created_at,
               (SELECT COUNT(*) FROM dbo.products p WHERE p.vendor_id = v.vendor_id AND p.is_active = 1) AS products,
               (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status = 'delivered') AS orders_delivered
        FROM dbo.vendors v WHERE v.vendor_id = ? AND v.is_active = 1
    """, vendor_id)


# ================================================================== customer
def customer_profile(customer_id: int) -> dict | None:
    return query_one("SELECT customer_id, first_name, last_name, email, phone, city, country, signup_date "
                     "FROM dbo.customers WHERE customer_id = ?", customer_id)


def update_profile(customer_id: int, phone: str, city: str, country: str) -> None:
    execute("UPDATE dbo.customers SET phone = ?, city = ?, country = ?, updated_at = ? WHERE customer_id = ?",
            phone or None, city or None, country or None, mp.now(), customer_id)


ORDER_HEAD = """
    SELECT o.order_id, o.customer_id, c.first_name + ' ' + c.last_name AS customer, c.email, o.order_date, o.status,
           o.total_amount, o.shipping_city, o.shipping_country, o.updated_at,
           p.payment_method, p.payment_status, p.failure_reason AS payment_failure, p.provider AS payment_provider,
           CASE WHEN p.payment_status = 'pending' AND p.payment_method <> 'cash_on_delivery' AND o.status = 'pending'
                THEN 1 ELSE 0 END AS awaiting_payment,
           (SELECT COUNT(*) FROM dbo.order_items i WHERE i.order_id = o.order_id) AS items,
           (SELECT COUNT(*) FROM dbo.shipments s WHERE s.order_id = o.order_id) AS shipment_count
    FROM dbo.orders o
    JOIN dbo.customers c ON c.customer_id = o.customer_id
    LEFT JOIN dbo.payments p ON p.order_id = o.order_id
"""


def customer_orders(customer_id: int, limit: int = 30) -> list[dict]:
    orders = query(f"SELECT TOP ({int(limit)}) * FROM ({ORDER_HEAD}) x WHERE customer_id = ? "
                   f"ORDER BY order_date DESC, order_id DESC", customer_id)
    if orders:
        ids = [o["order_id"] for o in orders]
        marks = ",".join("?" * len(ids))
        thumbs = query(f"""
            SELECT i.order_id, p.product_id, p.product_name, p.image_url, c.category_name, i.quantity
            FROM dbo.order_items i JOIN dbo.products p ON p.product_id = i.product_id
            JOIN dbo.categories c ON c.category_id = p.category_id WHERE i.order_id IN ({marks})
        """, *ids)
        for o in orders:
            o["lines"] = [t for t in thumbs if t["order_id"] == o["order_id"]]
    return orders


def order_detail(order_id: int) -> dict | None:
    o = query_one(f"SELECT * FROM ({ORDER_HEAD}) x WHERE order_id = ?", order_id)
    if o is None:
        return None
    o["lines"] = query("""
        SELECT i.order_item_id, i.product_id, p.product_name, p.image_url, c.category_name, i.quantity, i.unit_price, i.discount,
               i.line_total, i.shipment_id
        FROM dbo.order_items i JOIN dbo.products p ON p.product_id = i.product_id
        JOIN dbo.categories c ON c.category_id = p.category_id WHERE i.order_id = ? ORDER BY i.order_item_id
    """, order_id)
    o["shipments"] = query("""
        SELECT s.shipment_id, s.vendor_id, v.vendor_name, v.is_bot AS vendor_autopilot, s.status, s.rider_id,
               r.full_name AS rider_name, r.vehicle, r.phone AS rider_phone, s.delivery_fee,
               s.created_at, s.confirmed_at, s.accepted_at, s.ready_at, s.assigned_at, s.picked_up_at,
               s.delivered_at, s.cancelled_at, s.updated_at
        FROM dbo.shipments s JOIN dbo.vendors v ON v.vendor_id = s.vendor_id
        LEFT JOIN dbo.riders r ON r.rider_id = s.rider_id WHERE s.order_id = ? ORDER BY s.shipment_id
    """, order_id)
    o["can_cancel"] = o["status"] in ("pending", "processing") and all(
        s["status"] in mp.CANCELLABLE_SHIPMENT for s in o["shipments"])
    o["can_return"] = o["status"] == "delivered"
    return o


# ================================================================== vendor portal
def vendor_summary(vendor_id: int) -> dict:
    return query_one("""
        SELECT v.vendor_id, v.vendor_name, v.tagline, v.rating, v.is_bot, v.prep_seconds, v.city,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status = 'confirmed') AS to_accept,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status = 'packing') AS to_pack,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status IN ('ready','assigned')) AS awaiting_pickup,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status = 'picked_up') AS on_the_way,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status = 'pending') AS awaiting_admin,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND CAST(s.created_at AS date) = CAST(GETDATE() AS date)) AS orders_today,
          (SELECT coalesce(SUM(i.line_total), 0) FROM dbo.order_items i JOIN dbo.shipments s ON s.shipment_id = i.shipment_id
             WHERE s.vendor_id = v.vendor_id AND CAST(s.created_at AS date) = CAST(GETDATE() AS date)
               AND s.status NOT IN ('cancelled','returned')) AS sales_today,
          (SELECT coalesce(SUM(i.line_total), 0) FROM dbo.order_items i JOIN dbo.shipments s ON s.shipment_id = i.shipment_id
             WHERE s.vendor_id = v.vendor_id AND s.created_at >= DATEADD(day, -30, GETDATE())
               AND s.status NOT IN ('cancelled','returned')) AS sales_30d,
          (SELECT COUNT(*) FROM dbo.products p WHERE p.vendor_id = v.vendor_id AND p.is_active = 1) AS products,
          (SELECT COUNT(*) FROM dbo.products p JOIN dbo.inventory i ON i.product_id = p.product_id
             WHERE p.vendor_id = v.vendor_id AND i.quantity_on_hand < 20) AS low_stock
        FROM dbo.vendors v WHERE v.vendor_id = ?
    """, vendor_id)


def vendor_shipments(vendor_id: int, statuses: list[str], limit: int = 60) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    ships = query(f"""
        SELECT TOP ({int(limit)}) s.shipment_id, s.order_id, s.status, s.created_at, s.confirmed_at, s.accepted_at,
               s.ready_at, s.picked_up_at, s.delivered_at, s.updated_at, r.full_name AS rider_name,
               c.first_name AS customer_first_name, o.shipping_city, o.shipping_country,
               (SELECT SUM(line_total) FROM dbo.order_items i WHERE i.shipment_id = s.shipment_id) AS amount
        FROM dbo.shipments s JOIN dbo.orders o ON o.order_id = s.order_id
        JOIN dbo.customers c ON c.customer_id = o.customer_id
        LEFT JOIN dbo.riders r ON r.rider_id = s.rider_id
        WHERE s.vendor_id = ? AND s.status IN ({marks})
        ORDER BY CASE s.status WHEN 'confirmed' THEN 0 WHEN 'packing' THEN 1 ELSE 2 END, s.updated_at DESC
    """, vendor_id, *statuses)
    if ships:
        ids = [s["shipment_id"] for s in ships]
        lines = query(f"""
            SELECT i.shipment_id, p.product_id, p.product_name, p.sku, p.image_url, c.category_name, i.quantity, i.unit_price,
                   i.line_total FROM dbo.order_items i
            JOIN dbo.products p ON p.product_id = i.product_id
            JOIN dbo.categories c ON c.category_id = p.category_id WHERE i.shipment_id IN ({','.join('?' * len(ids))})
        """, *ids)
        for s in ships:
            s["lines"] = [l for l in lines if l["shipment_id"] == s["shipment_id"]]
    return ships


def vendor_products(vendor_id: int) -> list[dict]:
    return query(PRODUCT_SQL.replace("WHERE p.is_active = 1", "WHERE 1 = 1") +
                 " AND v.vendor_id = ? ORDER BY p.product_name", vendor_id)


def vendor_owns_product(vendor_id: int, product_id: int) -> bool:
    return bool(scalar("SELECT COUNT(*) FROM dbo.products WHERE product_id = ? AND vendor_id = ?", product_id, vendor_id))


def update_product(product_id: int, unit_price: float | None = None, list_price: float | None = None,
                   add_stock: int | None = None, is_active: bool | None = None) -> None:
    ts = mp.now()
    clear_catalog_cache()
    with tx() as cur:
        if unit_price is not None:
            if unit_price <= 0:
                raise ShopError("Price must be positive")
            cur.execute("UPDATE dbo.products SET unit_price = ?, updated_at = ? WHERE product_id = ?",
                        mp.money(unit_price), ts, product_id)
        if list_price is not None:
            cur.execute("UPDATE dbo.products SET list_price = ?, updated_at = ? WHERE product_id = ?",
                        mp.money(list_price) if list_price > 0 else None, ts, product_id)
        if is_active is not None:
            cur.execute("UPDATE dbo.products SET is_active = ?, updated_at = ? WHERE product_id = ?",
                        int(is_active), ts, product_id)
        if add_stock:
            if add_stock < 1:
                raise ShopError("Restock quantity must be positive")
            cur.execute("UPDATE dbo.inventory SET quantity_on_hand = quantity_on_hand + ?, updated_at = ? "
                        "WHERE product_id = ?", add_stock, ts, product_id)


def admin_products() -> list[dict]:
    return with_images(query("""
        SELECT p.product_id, p.product_name, p.sku, p.brand, p.unit_price, p.list_price, p.is_active, p.image_url, p.gallery,
               c.category_name, v.vendor_name, v.is_active AS vendor_active,
               coalesce(i.quantity_on_hand, 0) AS stock, coalesce(s.sold, 0) AS sold
        FROM dbo.products p
        JOIN dbo.categories c ON c.category_id = p.category_id
        LEFT JOIN dbo.vendors v ON v.vendor_id = p.vendor_id
        LEFT JOIN dbo.inventory i ON i.product_id = p.product_id
        LEFT JOIN (SELECT product_id, SUM(quantity) AS sold FROM dbo.order_items GROUP BY product_id) s ON s.product_id = p.product_id
        ORDER BY coalesce(s.sold, 0) DESC, p.product_id"""))


def set_product_image(product_id: int, image_url: str) -> None:
    clear_catalog_cache()
    if not execute("UPDATE dbo.products SET image_url = ?, updated_at = ? WHERE product_id = ?",
                   image_url, mp.now(), product_id):
        raise ShopError("Product not found")


def bulk_vendor_action(vendor_id: int, shipment_ids: list[int], action: str) -> dict:
    """Seller handles many parcels at once. 'accept', 'ready', or 'accept_ready' (accept + pack in one go,
    which hands the parcel straight to the rider dispatcher). Each parcel succeeds or fails on its own."""
    steps = {"accept": [mp.vendor_accept], "ready": [mp.vendor_ready],
             "accept_ready": [mp.vendor_accept, mp.vendor_ready]}.get(action)
    if steps is None:
        raise ShopError("Unknown bulk action")
    done, failed = [], []
    for sid in shipment_ids[:200]:
        try:
            with tx() as cur:
                status = cur.execute("SELECT status FROM dbo.shipments WHERE shipment_id = ? AND vendor_id = ?",
                                     sid, vendor_id).fetchval()
                if status is None:
                    raise ShopError("not your parcel")
                for step in steps:
                    if step is mp.vendor_accept and status != "confirmed":
                        continue  # already accepted - just pack it
                    step(cur, sid, vendor_id=vendor_id)
            done.append(sid)
        except ShopError as e:
            failed.append({"shipment_id": sid, "reason": str(e)})
    return {"done": done, "failed": failed}


def add_product(vendor_id: int, name: str, category_id: int, unit_price: float, unit_cost: float,
                stock: int, description: str) -> int:
    name = name.strip()
    if not name or unit_price <= 0 or unit_cost < 0 or stock < 0:
        raise ShopError("Name, a positive price, a cost and a stock level are required")
    ts = mp.now()
    with tx() as cur:
        cat = cur.execute("SELECT category_name FROM dbo.categories WHERE category_id = ?", category_id).fetchval()
        if cat is None:
            raise ShopError("Unknown category")
        seq = cur.execute("SELECT ISNULL(MAX(product_id), 0) + 1 FROM dbo.products").fetchval()
        pid = cur.execute(
            "INSERT INTO dbo.products (category_id, product_name, sku, unit_price, unit_cost, vendor_id, rating, "
            "review_count, description, created_at, updated_at) OUTPUT INSERTED.product_id "
            "VALUES (?,?,?,?,?,?,NULL,0,?,?,?)",
            category_id, name, f"{cat[:3].upper()}-{seq:05d}", mp.money(unit_price), mp.money(unit_cost),
            vendor_id, description.strip() or None, ts, ts,
        ).fetchval()
        cur.execute("INSERT INTO dbo.inventory (product_id, quantity_on_hand, updated_at) VALUES (?,?,?)", pid, stock, ts)
    clear_catalog_cache()
    return pid


# ================================================================== rider portal
def rider_summary(rider_id: int) -> dict:
    return query_one("""
        SELECT r.rider_id, r.full_name, r.phone, r.city, r.vehicle, r.status, r.is_bot,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.rider_id = r.rider_id AND s.status = 'delivered'
             AND CAST(s.delivered_at AS date) = CAST(GETDATE() AS date)) AS delivered_today,
          (SELECT coalesce(SUM(delivery_fee), 0) FROM dbo.shipments s WHERE s.rider_id = r.rider_id AND s.status = 'delivered'
             AND CAST(s.delivered_at AS date) = CAST(GETDATE() AS date)) AS earned_today,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.rider_id = r.rider_id AND s.status IN ('delivered','returned')) AS delivered_total,
          (SELECT coalesce(SUM(delivery_fee), 0) FROM dbo.shipments s WHERE s.rider_id = r.rider_id AND s.status IN ('delivered','returned')) AS earned_total,
          (SELECT AVG(DATEDIFF(second, s.picked_up_at, s.delivered_at)) FROM dbo.shipments s
             WHERE s.rider_id = r.rider_id AND s.delivered_at >= DATEADD(day, -7, GETDATE())) AS avg_ride_seconds
        FROM dbo.riders r WHERE r.rider_id = ?
    """, rider_id)


def rider_jobs(rider_id: int, active: bool = True, limit: int = 30) -> list[dict]:
    cond = "s.status IN ('assigned','picked_up')" if active else "s.status IN ('delivered','returned')"
    return query(f"""
        SELECT TOP ({int(limit)}) s.shipment_id, s.order_id, s.status, s.delivery_fee, s.assigned_at, s.picked_up_at,
               s.delivered_at, v.vendor_name, v.city AS pickup_city, c.first_name + ' ' + LEFT(c.last_name, 1) + '.' AS customer,
               c.phone AS customer_phone, o.shipping_city, o.shipping_country, p.payment_method, p.payment_status,
               (SELECT SUM(quantity) FROM dbo.order_items i WHERE i.shipment_id = s.shipment_id) AS units,
               (SELECT SUM(line_total) FROM dbo.order_items i WHERE i.shipment_id = s.shipment_id) AS parcel_value
        FROM dbo.shipments s JOIN dbo.vendors v ON v.vendor_id = s.vendor_id
        JOIN dbo.orders o ON o.order_id = s.order_id JOIN dbo.customers c ON c.customer_id = o.customer_id
        LEFT JOIN dbo.payments p ON p.order_id = o.order_id
        WHERE s.rider_id = ? AND {cond} ORDER BY coalesce(s.delivered_at, s.assigned_at) DESC
    """, rider_id)


def set_rider_status(rider_id: int, status: str) -> None:
    if status not in ("available", "offline"):
        raise ShopError("Status must be available or offline")
    active = scalar("SELECT COUNT(*) FROM dbo.shipments WHERE rider_id = ? AND status IN ('assigned','picked_up')", rider_id)
    if active:
        raise ShopError("Finish your current delivery first")
    execute("UPDATE dbo.riders SET status = ?, updated_at = ? WHERE rider_id = ?", status, mp.now(), rider_id)


# ================================================================== admin
def admin_overview() -> dict:
    return {
        "settings": settings(),
        "shipments_by_status": query("SELECT status, COUNT(*) AS n FROM dbo.shipments "
                                     "WHERE status IN ('pending','confirmed','packing','ready','assigned','picked_up') GROUP BY status"),
        "riders_by_status": query("SELECT status, COUNT(*) AS n FROM dbo.riders GROUP BY status"),
        "orders_by_status": query("SELECT status, COUNT(*) AS n FROM dbo.orders WHERE order_date <= SYSDATETIME() GROUP BY status"),
        "pending_orders": scalar("SELECT COUNT(*) FROM dbo.orders WHERE status = 'pending' AND order_date <= SYSDATETIME()"
                               " AND NOT EXISTS (SELECT 1 FROM dbo.payments p WHERE p.order_id = dbo.orders.order_id AND p.payment_status = 'pending' AND p.payment_method <> 'cash_on_delivery')"),
    }


def order_queue(statuses: list[str], limit: int = 100) -> list[dict]:
    marks = ",".join("?" * len(statuses))
    return query(f"""
        SELECT TOP ({int(limit)}) * FROM ({ORDER_HEAD}) x
        WHERE status IN ({marks}) AND order_date <= SYSDATETIME()
        ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END, order_date DESC
    """, *statuses)


@catalog_cached   # daily totals: fine to be up to 15 s old; refreshed in the background so nobody waits
def admin_kpis(days: int = 28) -> dict:
    """Daily operations series for the console's KPI cards (current period vs the one before)."""
    n = int(days)
    return {"days": query(f"""
        DECLARE @from date = DATEADD(day, -{n - 1}, CAST(GETDATE() AS date));
        WITH d AS (SELECT DATEADD(day, x.n, @from) AS day
                   FROM (SELECT TOP ({n}) ROW_NUMBER() OVER (ORDER BY (SELECT 1)) - 1 AS n FROM sys.all_objects) x),
        o AS (SELECT CAST(order_date AS date) AS day, COUNT(*) AS orders,
                     SUM(CASE WHEN status NOT IN ('cancelled','returned') THEN total_amount ELSE 0 END) AS gmv,
                     SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
              FROM dbo.orders WHERE order_date >= @from AND order_date <= SYSDATETIME() GROUP BY CAST(order_date AS date)),
        s AS (SELECT CAST(delivered_at AS date) AS day, COUNT(*) AS delivered,
                     AVG(DATEDIFF(second, confirmed_at, delivered_at)) / 60.0 AS fulfil_minutes
              FROM dbo.shipments WHERE delivered_at >= @from GROUP BY CAST(delivered_at AS date))
        SELECT d.day, COALESCE(o.orders, 0) AS orders, COALESCE(o.gmv, 0) AS gmv, COALESCE(o.cancelled, 0) AS cancelled,
               COALESCE(s.delivered, 0) AS delivered, s.fulfil_minutes
        FROM d LEFT JOIN o ON o.day = d.day LEFT JOIN s ON s.day = d.day ORDER BY d.day""")}


def admin_order_extra(order_id: int) -> dict:
    """Customer, payment and audit trail for the console's order drawer."""
    o = query_one("""
        SELECT c.customer_id, c.phone, c.city, c.signup_date,
               (SELECT COUNT(*) FROM dbo.orders x WHERE x.customer_id = c.customer_id) AS customer_orders,
               (SELECT COALESCE(SUM(total_amount), 0) FROM dbo.orders x WHERE x.customer_id = c.customer_id
                  AND x.status NOT IN ('cancelled','returned')) AS customer_spend,
               p.payment_id, p.amount AS paid_amount, p.refunded_amount, p.paid_at, p.provider_ref, o.created_at
        FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id
        LEFT JOIN dbo.payments p ON p.order_id = o.order_id WHERE o.order_id = ?""", order_id) or {}
    o["payment_events"] = query("SELECT event_type, provider, amount, received_at, result FROM dbo.payment_events "
                                "WHERE order_id = ? ORDER BY received_at", order_id)
    o["vendor_logins"] = query("SELECT s.shipment_id, u.email FROM dbo.shipments s JOIN dbo.users u ON u.vendor_id = s.vendor_id "
                               "WHERE s.order_id = ?", order_id)
    o["rider_logins"] = query("SELECT s.shipment_id, u.email FROM dbo.shipments s JOIN dbo.users u ON u.rider_id = s.rider_id "
                              "WHERE s.order_id = ?", order_id)
    return o


def vendor_prep_trends(days: int = 14) -> list[dict]:
    return query(f"""
        SELECT vendor_id, CAST(ready_at AS date) AS day, AVG(DATEDIFF(second, accepted_at, ready_at)) / 60.0 AS prep_minutes,
               COUNT(*) AS parcels
        FROM dbo.shipments WHERE ready_at >= DATEADD(day, -{int(days)}, CAST(GETDATE() AS date)) AND accepted_at IS NOT NULL
        GROUP BY vendor_id, CAST(ready_at AS date) ORDER BY vendor_id, day""")


def rider_week(days: int = 7) -> list[dict]:
    return query(f"""
        SELECT rider_id, CAST(delivered_at AS date) AS day, COUNT(*) AS delivered, SUM(delivery_fee) AS earned
        FROM dbo.shipments WHERE delivered_at >= DATEADD(day, -{int(days) - 1}, CAST(GETDATE() AS date))
        GROUP BY rider_id, CAST(delivered_at AS date)""")


def shipments_board(limit_per_status: int = 25) -> list[dict]:
    return query(f"""
        SELECT * FROM (
          SELECT s.shipment_id, s.order_id, s.status, s.updated_at, s.created_at, s.vendor_id, s.rider_id, v.vendor_name,
                 v.is_bot AS vendor_autopilot, r.full_name AS rider_name, r.vehicle, r.is_bot AS rider_autopilot, o.shipping_city,
                 (SELECT SUM(quantity) FROM dbo.order_items i WHERE i.shipment_id = s.shipment_id) AS units,
                 ROW_NUMBER() OVER (PARTITION BY s.status ORDER BY s.updated_at DESC) AS rn
          FROM dbo.shipments s JOIN dbo.vendors v ON v.vendor_id = s.vendor_id
          JOIN dbo.orders o ON o.order_id = s.order_id LEFT JOIN dbo.riders r ON r.rider_id = s.rider_id
          WHERE s.status IN ('pending','confirmed','packing','ready','assigned','picked_up')
             OR (s.status = 'delivered' AND s.delivered_at >= DATEADD(hour, -1, SYSDATETIME()))
        ) x WHERE rn <= {int(limit_per_status)} ORDER BY updated_at DESC
    """)


def vendors_admin() -> list[dict]:
    return query("""
        SELECT v.vendor_id, v.vendor_name, v.city, v.rating, v.is_active, v.is_bot, v.prep_seconds,
          (SELECT COUNT(*) FROM dbo.products p WHERE p.vendor_id = v.vendor_id) AS products,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status IN ('confirmed','packing')) AS to_handle,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.vendor_id = v.vendor_id AND s.status = 'delivered'
             AND s.delivered_at >= DATEADD(day, -30, GETDATE())) AS delivered_30d,
          (SELECT AVG(DATEDIFF(second, s.confirmed_at, s.ready_at)) FROM dbo.shipments s
             WHERE s.vendor_id = v.vendor_id AND s.ready_at >= DATEADD(day, -7, GETDATE())) AS avg_prep_seconds,
          (SELECT TOP 1 email FROM dbo.users u WHERE u.vendor_id = v.vendor_id) AS login_email
        FROM dbo.vendors v ORDER BY v.vendor_name
    """)


def riders_admin() -> list[dict]:
    return query("""
        SELECT r.rider_id, r.full_name, r.city, r.vehicle, r.status, r.is_bot,
          (SELECT TOP 1 s.shipment_id FROM dbo.shipments s WHERE s.rider_id = r.rider_id AND s.status IN ('assigned','picked_up')) AS current_shipment,
          (SELECT COUNT(*) FROM dbo.shipments s WHERE s.rider_id = r.rider_id AND s.status = 'delivered'
             AND CAST(s.delivered_at AS date) = CAST(GETDATE() AS date)) AS delivered_today,
          (SELECT TOP 1 email FROM dbo.users u WHERE u.rider_id = r.rider_id) AS login_email
        FROM dbo.riders r ORDER BY r.is_bot, r.full_name
    """)


def users_admin(limit: int = 200) -> list[dict]:
    return query(f"""
        SELECT TOP ({int(limit)}) u.user_id, u.email, u.role, u.display_name, u.is_active, u.last_login_at, u.created_at,
               u.customer_id, u.vendor_id, u.rider_id
        FROM dbo.users u ORDER BY CASE u.role WHEN 'admin' THEN 0 WHEN 'vendor' THEN 1 WHEN 'rider' THEN 2 ELSE 3 END, u.user_id
    """)


def settings() -> dict:
    with tx() as cur:
        return mp.get_settings(cur)


def set_flag(table: str, key_col: str, key: int, column: str, value) -> None:
    allowed = {("vendors", "is_bot"), ("vendors", "is_active"), ("riders", "is_bot"), ("users", "is_active")}
    if (table, column) not in allowed:
        raise ShopError("That setting can't be changed")
    clear_catalog_cache()  # store visibility / autopilot changes show up on the storefront right away
    if not execute(f"UPDATE dbo.{table} SET {column} = ?, updated_at = ? WHERE {key_col} = ?", value, mp.now(), key):
        raise ShopError("Not found")


# ================================================================== money: seller earnings + admin finance
LEDGER_SQL = """
    SELECT TOP ({n}) l.entry_id, l.order_id, l.shipment_id, l.entry_type, l.amount, l.commission_rate, l.status,
           l.available_at, l.created_at, l.payout_id, p.product_name
    FROM dbo.ledger_entries l
    LEFT JOIN dbo.order_items i ON i.order_item_id = l.order_item_id
    LEFT JOIN dbo.products p ON p.product_id = i.product_id
    WHERE l.vendor_id = ? ORDER BY l.created_at DESC, l.entry_id DESC
"""


def vendor_earnings(vendor_id: int) -> dict:
    with tx() as cur:
        balance = mp.pay.vendor_balances(cur, vendor_id)[0]
        rates = mp.pay.commission_rates(cur)
    return {
        "balance": balance,
        "hold_days": mp.pay.HOLD_DAYS,
        "min_payout": float(mp.pay.MIN_PAYOUT),
        "rates": [{"category_name": c["category_name"],
                   "rate": float(rates["vendor"].get(vendor_id) or rates["category"].get(c["category_id"]) or rates["default"]),
                   "source": "your store" if vendor_id in rates["vendor"] else
                             ("category" if c["category_id"] in rates["category"] else "default")}
                  for c in categories()],
        "ledger": query(LEDGER_SQL.format(n=60), vendor_id),
        "payouts": query("SELECT TOP 20 payout_id, amount, entry_count, status, reference, created_at, paid_at "
                         "FROM dbo.payouts WHERE vendor_id = ? ORDER BY created_at DESC", vendor_id),
        "sales_by_day": query("""
            SELECT CAST(created_at AS date) AS day,
                   SUM(CASE WHEN entry_type IN ('sale','sale_reversal') THEN amount ELSE 0 END) AS sales,
                   -SUM(CASE WHEN entry_type IN ('commission','commission_reversal') THEN amount ELSE 0 END) AS commission
            FROM dbo.ledger_entries WHERE vendor_id = ? AND created_at >= DATEADD(day, -29, CAST(GETDATE() AS date))
            GROUP BY CAST(created_at AS date) ORDER BY day
        """, vendor_id),
    }


def finance_overview() -> dict:
    with tx() as cur:
        balances = mp.pay.vendor_balances(cur)
        rates = mp.pay.commission_rates(cur)
    totals = query_one("""
        SELECT
          (SELECT COALESCE(SUM(total_amount), 0) FROM dbo.orders WHERE order_date >= DATEADD(day, -30, GETDATE())
             AND status NOT IN ('cancelled','returned')) AS gmv_30d,
          (SELECT COALESCE(-SUM(amount), 0) FROM dbo.ledger_entries WHERE entry_type IN ('commission','commission_reversal')
             AND created_at >= DATEADD(day, -30, GETDATE())) AS commission_30d,
          (SELECT COALESCE(-SUM(amount), 0) FROM dbo.ledger_entries WHERE entry_type IN ('commission','commission_reversal')) AS commission_all,
          (SELECT COALESCE(SUM(refunded_amount), 0) FROM dbo.payments WHERE payment_status = 'refunded'
             AND updated_at >= DATEADD(day, -30, GETDATE())) AS refunds_30d,
          (SELECT COALESCE(SUM(amount), 0) FROM dbo.payouts WHERE status = 'paid') AS paid_out_all,
          (SELECT COUNT(*) FROM dbo.payments WHERE payment_status = 'pending' AND payment_method <> 'cash_on_delivery') AS awaiting_online,
          (SELECT COALESCE(SUM(amount), 0) FROM dbo.payments WHERE payment_status = 'pending' AND payment_method = 'cash_on_delivery') AS cod_to_collect
    """)
    totals["owed_to_sellers"] = sum(b["pending"] + b["available"] + b["scheduled"] for b in balances)
    totals["ready_for_payout"] = sum(b["available"] for b in balances if b["available"] >= float(mp.pay.MIN_PAYOUT))
    cats = categories()
    vendors = query("SELECT vendor_id, vendor_name FROM dbo.vendors ORDER BY vendor_name")
    return {
        "totals": totals, "balances": balances, "hold_days": mp.pay.HOLD_DAYS, "min_payout": float(mp.pay.MIN_PAYOUT),
        "gateway": os.getenv("PAYMENT_GATEWAY", "sandbox"),
        "rules": {
            "default": float(rates["default"]),
            "categories": [{"category_id": c["category_id"], "category_name": c["category_name"],
                            "rate": float(rates["category"][c["category_id"]]) if c["category_id"] in rates["category"] else None}
                           for c in cats],
            "vendors": [{"vendor_id": v["vendor_id"], "vendor_name": v["vendor_name"],
                         "rate": float(rates["vendor"][v["vendor_id"]]) if v["vendor_id"] in rates["vendor"] else None}
                        for v in vendors],
        },
        "payouts": query("""SELECT TOP 40 p.payout_id, p.vendor_id, v.vendor_name, p.amount, p.entry_count, p.status,
                                   p.reference, p.created_at, p.paid_at
                            FROM dbo.payouts p JOIN dbo.vendors v ON v.vendor_id = p.vendor_id
                            ORDER BY CASE p.status WHEN 'scheduled' THEN 0 ELSE 1 END, p.created_at DESC"""),
        "commission_by_day": query("""
            SELECT CAST(created_at AS date) AS day, -SUM(amount) AS commission
            FROM dbo.ledger_entries WHERE entry_type IN ('commission','commission_reversal')
              AND created_at >= DATEADD(day, -29, CAST(GETDATE() AS date))
            GROUP BY CAST(created_at AS date) ORDER BY day"""),
    }


# ================================================================== live operations dashboard
def dashboard_stats() -> dict:
    kpis = query_one("""
        SELECT
          (SELECT count(*) FROM dbo.orders WHERE CAST(order_date AS date) = CAST(GETDATE() AS date) AND order_date <= SYSDATETIME()) AS orders_today,
          (SELECT coalesce(sum(total_amount), 0) FROM dbo.orders WHERE CAST(order_date AS date) = CAST(GETDATE() AS date)
             AND order_date <= SYSDATETIME() AND status NOT IN ('cancelled','returned')) AS revenue_today,
          (SELECT count(*) FROM dbo.orders WHERE order_date >= DATEADD(minute, -5, SYSDATETIME()) AND order_date <= SYSDATETIME()) AS orders_last_5_min,
          (SELECT count(*) FROM dbo.orders WHERE status IN ('pending','processing','paid','shipped')) AS open_orders,
          (SELECT count(*) FROM dbo.customers) AS customers,
          (SELECT count(*) FROM dbo.customers WHERE signup_date = CAST(GETDATE() AS date)) AS new_customers_today,
          (SELECT count(*) FROM dbo.shipments WHERE status = 'delivered' AND CAST(delivered_at AS date) = CAST(GETDATE() AS date)) AS delivered_today,
          (SELECT AVG(DATEDIFF(second, confirmed_at, delivered_at)) FROM dbo.shipments
             WHERE delivered_at >= DATEADD(hour, -24, SYSDATETIME()) AND confirmed_at IS NOT NULL) AS avg_fulfil_seconds
    """)
    kpis["avg_order_today"] = kpis["revenue_today"] / kpis["orders_today"] if kpis["orders_today"] else 0
    return {
        "kpis": kpis,
        "per_minute": query("""
            SELECT DATEADD(minute, DATEDIFF(minute, 0, order_date), 0) AS minute, count(*) AS orders FROM dbo.orders
            WHERE order_date >= DATEADD(minute, -60, SYSDATETIME()) AND order_date <= SYSDATETIME()
            GROUP BY DATEADD(minute, DATEDIFF(minute, 0, order_date), 0) ORDER BY minute
        """),
        "revenue_by_day": query("""
            SELECT CAST(order_date AS date) AS day, count(*) AS orders,
                   sum(CASE WHEN status NOT IN ('cancelled','returned') THEN total_amount ELSE 0 END) AS revenue
            FROM dbo.orders WHERE order_date >= DATEADD(day, -29, CAST(GETDATE() AS date)) AND order_date <= SYSDATETIME()
            GROUP BY CAST(order_date AS date) ORDER BY day
        """),
        "status_counts": query("SELECT status, count(*) AS orders FROM dbo.orders GROUP BY status ORDER BY orders DESC"),
        "pipeline": query("SELECT status, count(*) AS n FROM dbo.shipments WHERE status IN "
                          "('pending','confirmed','packing','ready','assigned','picked_up') GROUP BY status"),
        "category_revenue": query("""
            SELECT c.category_name, sum(i.line_total) AS revenue FROM dbo.order_items i
            JOIN dbo.orders o ON o.order_id = i.order_id JOIN dbo.products p ON p.product_id = i.product_id
            JOIN dbo.categories c ON c.category_id = p.category_id
            WHERE o.order_date >= DATEADD(day, -30, SYSDATETIME()) AND o.order_date <= SYSDATETIME()
              AND o.status NOT IN ('cancelled','returned') GROUP BY c.category_name ORDER BY revenue DESC
        """),
        "top_products": query("""
            SELECT TOP 5 p.product_name, sum(i.quantity) AS units, sum(i.line_total) AS revenue FROM dbo.order_items i
            JOIN dbo.orders o ON o.order_id = i.order_id JOIN dbo.products p ON p.product_id = i.product_id
            WHERE o.order_date >= DATEADD(day, -7, SYSDATETIME()) AND o.order_date <= SYSDATETIME()
              AND o.status NOT IN ('cancelled','returned') GROUP BY p.product_name ORDER BY units DESC
        """),
        "low_stock": query("""
            SELECT TOP 8 p.product_id, p.product_name, v.vendor_name, i.quantity_on_hand FROM dbo.inventory i
            JOIN dbo.products p ON p.product_id = i.product_id JOIN dbo.vendors v ON v.vendor_id = p.vendor_id
            WHERE p.is_active = 1 ORDER BY i.quantity_on_hand
        """),
        "recent_orders": query(f"SELECT TOP 12 * FROM ({ORDER_HEAD}) x WHERE order_date <= SYSDATETIME() "
                               f"ORDER BY order_date DESC, order_id DESC"),
    }
