"""Live change feed: ShopDB -> every open browser tab, via Server-Sent Events (SSE).

One background task polls ShopDB every couple of seconds for rows whose updated_at moved past a
cursor - the same watermark idea the ingestion layer uses, just every 2 seconds instead of on demand.
Each change becomes an event that is pushed to all connected clients. That means changes show up
live no matter who made them: the simulator, another customer, or an admin.

Why poll instead of having the app announce its own writes? Because the simulator writes straight
to the database, bypassing the app. Watching the database catches every writer.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from . import shop_db as db

log = logging.getLogger("shop.live")

FEED_SQL = """
    SELECT 'order' AS kind, o.order_id AS id, o.updated_at, o.created_at, o.status,
           o.total_amount AS amount, o.customer_id, c.first_name + ' ' + c.last_name AS name,
           o.shipping_country AS detail
    FROM dbo.orders o JOIN dbo.customers c ON c.customer_id = o.customer_id
    WHERE o.updated_at >= ? AND o.updated_at <= SYSDATETIME() AND o.order_date <= SYSDATETIME()
    UNION ALL
    SELECT 'customer', customer_id, updated_at, created_at, NULL, NULL, customer_id,
           first_name + ' ' + last_name, coalesce(city, '?') + ', ' + coalesce(country, '?')
    FROM dbo.customers WHERE updated_at >= ? AND updated_at <= SYSDATETIME()
    UNION ALL
    SELECT 'product', product_id, updated_at, created_at, NULL, unit_price, NULL, product_name, sku
    FROM dbo.products WHERE updated_at >= ? AND updated_at <= SYSDATETIME()
    ORDER BY updated_at
"""


class ChangeFeed:
    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.clients: set[asyncio.Queue] = set()
        self.cursor: datetime | None = None
        self.seen_at_cursor: set[tuple] = set()  # rows already sent that sit exactly on the cursor second

    # ----- subscribers
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.clients.discard(q)

    # ----- polling
    def _reset_cursor(self) -> None:
        self.cursor = db.scalar("SELECT SYSDATETIME()").replace(microsecond=0)
        self.seen_at_cursor = set()

    def poll(self) -> list[dict]:
        """Return changes since the cursor. updated_at has 1-second precision, so we query with >=
        and skip rows already sent for the cursor's second - nothing is missed and nothing repeats."""
        if self.cursor is None:
            self._reset_cursor()
            return []
        conn = db.get_connection()
        try:
            cur = conn.cursor().execute(FEED_SQL, self.cursor, self.cursor, self.cursor)
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
                        for q in list(self.clients):
                            try:
                                q.put_nowait(event)
                            except asyncio.QueueFull:  # a stalled tab shouldn't block everyone else
                                pass
            except Exception:  # keep the feed alive through e.g. a DB restart
                log.exception("change feed poll failed")
            await asyncio.sleep(self.interval)

    async def stream(self, q: asyncio.Queue):
        """Async generator of SSE-formatted messages for one client."""
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # comment line stops proxies/browsers dropping an idle stream
        finally:
            self.unsubscribe(q)


feed = ChangeFeed()
