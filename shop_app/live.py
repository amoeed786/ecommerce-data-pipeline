"""Live change feed: ShopDB -> open browser tabs, via Server-Sent Events (SSE).

One background task polls ShopDB every 2 seconds for rows whose updated_at moved past a cursor (the
same watermark idea the ingestion layer uses) and pushes each change to the connected clients.
It watches the database rather than the app, so it sees every writer: shoppers, the simulator,
vendors, riders, the autopilot and admins.

Each subscriber only receives what they're allowed to see:
    admin     everything
    customer  their own orders and shipments (+ public price changes)
    vendor    shipments of their store
    rider     shipments assigned to them
    guest     public price changes only
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from . import shop_db as db

log = logging.getLogger("shop.live")

FEED_SQL = """
    SELECT 'order' AS kind, o.order_id AS id, o.order_id, o.updated_at, o.created_at, o.status, o.total_amount AS amount,
           o.customer_id, NULL AS vendor_id, NULL AS rider_id, c.first_name + ' ' + c.last_name AS name, o.shipping_country AS detail
    FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id
    WHERE o.updated_at >= ? AND o.updated_at <= SYSDATETIME() AND o.order_date <= SYSDATETIME()
    UNION ALL
    SELECT 'shipment', s.shipment_id, s.order_id, s.updated_at, s.created_at, s.status, NULL,
           o.customer_id, s.vendor_id, s.rider_id, v.vendor_name, r.full_name
    FROM dbo.shipments s JOIN dbo.orders o ON o.order_id = s.order_id JOIN dbo.vendors v ON v.vendor_id = s.vendor_id
    LEFT JOIN dbo.riders r ON r.rider_id = s.rider_id
    WHERE s.updated_at >= ? AND s.updated_at <= SYSDATETIME()
    UNION ALL
    SELECT 'customer', customer_id, NULL, updated_at, created_at, NULL, NULL, customer_id, NULL, NULL,
           first_name + ' ' + last_name, coalesce(city, '?') + ', ' + coalesce(country, '?')
    FROM dbo.customers WHERE updated_at >= ? AND updated_at <= SYSDATETIME()
    UNION ALL
    SELECT 'product', product_id, NULL, updated_at, created_at, NULL, unit_price, NULL, vendor_id, NULL, product_name, sku
    FROM dbo.products WHERE updated_at >= ? AND updated_at <= SYSDATETIME()
    ORDER BY updated_at
"""


def allowed(user: dict | None, ev: dict) -> bool:
    if ev["kind"] == "product":
        return True
    if user is None:
        return False
    role = user["role"]
    if role == "admin":
        return True
    if role == "customer":
        return ev["kind"] in ("order", "shipment") and ev["customer_id"] == user["customer_id"]
    if role == "vendor":
        return ev["kind"] == "shipment" and ev["vendor_id"] == user["vendor_id"]
    if role == "rider":
        return ev["kind"] == "shipment" and ev["rider_id"] == user["rider_id"]
    return False


class ChangeFeed:
    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.clients: dict[asyncio.Queue, dict | None] = {}
        self.cursor: datetime | None = None
        self.seen_at_cursor: set[tuple] = set()  # rows already sent that sit exactly on the cursor second

    def subscribe(self, user: dict | None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clients[q] = user
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.pop(q, None)

    def poll(self) -> list[dict]:
        """Changes since the cursor. updated_at has 1-second precision, so we query with >= and skip rows
        already sent for the cursor's second - nothing is missed and nothing repeats."""
        if self.cursor is None:
            self.cursor = db.scalar("SELECT SYSDATETIME()").replace(microsecond=0)
            self.seen_at_cursor = set()
            return []
        conn = db.get_connection()
        try:
            cur = conn.cursor().execute(FEED_SQL, *([self.cursor] * 4))
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        finally:
            conn.close()
        events = []
        for r in rows:
            key = (r["kind"], r["id"], r["updated_at"])
            if r["updated_at"] == self.cursor and key in self.seen_at_cursor:
                continue
            r["action"] = "created" if r["created_at"] == r["updated_at"] else "updated"
            events.append({k: db._jsonable(v) for k, v in r.items()})
        if rows:
            newest = rows[-1]["updated_at"]
            if newest != self.cursor:
                self.cursor, self.seen_at_cursor = newest, set()
            self.seen_at_cursor |= {(r["kind"], r["id"], r["updated_at"]) for r in rows if r["updated_at"] == newest}
        return events

    async def run(self) -> None:
        while True:
            try:
                if not self.clients:
                    self.cursor = None  # nobody listening: don't replay old changes when someone connects
                else:
                    events = await asyncio.to_thread(self.poll)
                    for event in events:
                        for q, user in list(self.clients.items()):
                            if allowed(user, event):
                                try:
                                    q.put_nowait(event)
                                except asyncio.QueueFull:  # a stalled tab mustn't block everyone else
                                    pass
            except Exception:
                log.exception("change feed poll failed")
            await asyncio.sleep(self.interval)

    async def stream(self, q: asyncio.Queue):
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            self.unsubscribe(q)


feed = ChangeFeed()
