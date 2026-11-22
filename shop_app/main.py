r"""ShopLite - FastAPI web app for the fake shop and the pipeline behind it.

Run from the project folder:
    .\.venv\Scripts\uvicorn.exe shop_app.main:app --port 8000
Then open http://localhost:8000

Pages (plain HTML + JavaScript in shop_app/static, data comes from the JSON API below)
    /            Shop        - customers sign in, browse, checkout, watch their orders update live
    /admin       Admin       - live order board with actions, catalog, simulator + pipeline controls
    /dashboard   Dashboard   - live operational view (reads ShopDB directly)
    /analytics   Analytics   - business analytics from the gold layer (forecast, RFM, cohorts, LTV)
    /docs        API docs    - FastAPI's automatic interactive documentation (try any endpoint)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import duckdb
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import shop_db as db
from .live import feed

ROOT = db.PROJECT_ROOT
STATIC = Path(__file__).parent / "static"
LAKE = ROOT / "data_lake"
STATE = LAKE / "_state"
SIM_LOG = STATE / "simulator.log"
TABLES = ["categories", "customers", "products", "inventory", "orders", "order_items", "payments"]
GOLD = LAKE / "gold"
VENV_BIN = Path(sys.executable).parent
# step -> (command shown to the user, actual argv)
PIPELINE_STEPS = {
    "extract": ("python ingestion/extract.py", [sys.executable, "ingestion/extract.py"]),
    "check_bronze": ("python ingestion/check_bronze.py", [sys.executable, "ingestion/check_bronze.py"]),
    "silver": ("python transform/silver.py", [sys.executable, "transform/silver.py"]),
    "dbt_build": ("dbt build --project-dir dbt_shop --profiles-dir dbt_shop",
                  [str(VENV_BIN / "dbt"), "build", "--project-dir", "dbt_shop", "--profiles-dir", "dbt_shop"]),
    "export_gold": ("python transform/export_gold.py", [sys.executable, "transform/export_gold.py"]),
    "inject_bad_data": ("python source_app/inject_bad_data.py", [sys.executable, "source_app/inject_bad_data.py"]),
    "check_db": ("python source_app/check_db.py", [sys.executable, "source_app/check_db.py"]),
}
simulator: dict = {"proc": None, "interval": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(feed.run())  # start the live change feed
    yield
    task.cancel()
    if simulator["proc"] and simulator["proc"].poll() is None:
        simulator["proc"].terminate()  # don't leave the simulator running after the app stops


app = FastAPI(title="ShopLite", description="Fake ecommerce shop on ShopDB", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.exception_handler(db.ShopError)
async def shop_error(_: Request, exc: db.ShopError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ------------------------------------------------------------------ pages
@app.get("/", include_in_schema=False)
def page_shop():
    return FileResponse(STATIC / "shop.html")


@app.get("/admin", include_in_schema=False)
def page_admin():
    return FileResponse(STATIC / "admin.html")


@app.get("/dashboard", include_in_schema=False)
def page_dashboard():
    return FileResponse(STATIC / "dashboard.html")


@app.get("/analytics", include_in_schema=False)
def page_analytics():
    return FileResponse(STATIC / "analytics.html")


# ------------------------------------------------------------------ catalog + customers
class NewCustomer(BaseModel):
    first_name: str
    last_name: str
    email: str
    city: str = ""
    country: str = ""


@app.get("/api/categories", tags=["catalog"])
def list_categories():
    return db.categories()


@app.get("/api/products", tags=["catalog"])
def list_products(category_id: int | None = None, q: str | None = None):
    return db.products(category_id, q)


@app.get("/api/customers", tags=["customers"])
def search_customers(q: str = "", limit: int = Query(20, le=100)):
    return db.find_customers(q.strip(), limit)


@app.post("/api/customers", tags=["customers"])
def sign_up(body: NewCustomer):
    return db.new_customer(body.first_name, body.last_name, body.email, body.city, body.country)


@app.get("/api/customers/{customer_id}", tags=["customers"])
def get_customer(customer_id: int):
    c = db.customer(customer_id)
    if c is None:
        raise HTTPException(404, "Customer not found")
    return c


@app.get("/api/customers/{customer_id}/orders", tags=["customers"])
def customer_orders(customer_id: int):
    return db.list_orders(customer_id=customer_id, limit=50)


# ------------------------------------------------------------------ orders
class CartLine(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=db.MAX_QTY_PER_LINE)


class NewOrder(BaseModel):
    customer_id: int
    items: list[CartLine]
    pay_now: bool = True
    payment_method: str = "credit_card"
    coupon: str = ""


class StatusChange(BaseModel):
    status: str
    payment_method: str = "credit_card"


@app.post("/api/orders", tags=["orders"])
def place_order(body: NewOrder):
    return db.place_order(body.customer_id, [i.model_dump() for i in body.items],
                          body.pay_now, body.payment_method, body.coupon)


@app.get("/api/orders", tags=["orders"])
def list_orders(status: list[str] = Query(default=[]), limit: int = Query(100, le=500)):
    return db.list_orders(statuses=status or None, limit=limit)


@app.get("/api/orders/{order_id}", tags=["orders"])
def get_order(order_id: int):
    order = db.order_detail(order_id)
    if order is None:
        raise HTTPException(404, "Order not found")
    return order


@app.post("/api/orders/{order_id}/status", tags=["admin"])
def admin_change_status(order_id: int, body: StatusChange):
    return db.change_status(order_id, body.status, body.payment_method)


@app.post("/api/customers/{customer_id}/orders/{order_id}/cancel", tags=["customers"])
def customer_cancel(customer_id: int, order_id: int):
    return db.change_status(order_id, "cancelled", customer_id=customer_id)


class PriceChange(BaseModel):
    unit_price: float = Field(ge=0)


class Restock(BaseModel):
    quantity: int = Field(ge=1, le=10_000)


@app.post("/api/products/{product_id}/price", tags=["admin"])
def change_price(product_id: int, body: PriceChange):
    db.change_price(product_id, body.unit_price)
    return {"ok": True}


@app.post("/api/products/{product_id}/restock", tags=["admin"])
def restock(product_id: int, body: Restock):
    db.restock(product_id, body.quantity)
    return {"ok": True}


# ------------------------------------------------------------------ analytics + live
@app.get("/api/stats", tags=["analytics"])
def stats():
    return db.dashboard_stats()


@app.get("/api/events", tags=["analytics"])
async def events():
    """Server-Sent Events stream of every change in ShopDB (orders, customers, products)."""
    return StreamingResponse(feed.stream(feed.subscribe()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/analytics", tags=["analytics"])
def gold_analytics():
    """Business analytics from the GOLD layer (data_lake/gold/*.parquet, built by dbt + export_gold).
    Unlike /api/stats (live ShopDB), this is as fresh as the last pipeline run."""
    if not (GOLD / "mart_daily_sales.parquet").exists():
        raise HTTPException(404, "No gold data yet - run the pipeline: extract, silver, dbt build, export gold.")
    con = duckdb.connect()

    def g(table: str) -> str:
        return f"read_parquet('{(GOLD / f'{table}.parquet').as_posix()}')"

    def q(sql: str) -> list[dict]:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, map(db._jsonable, row))) for row in cur.fetchall()]

    kpis = q(f"""
        WITH d AS (SELECT * FROM {g('mart_daily_sales')} WHERE date_day < current_date)
        SELECT
          sum(revenue)        FILTER (WHERE date_day >= current_date - 30) AS revenue_30d,
          sum(revenue)        FILTER (WHERE date_day <  current_date - 30 AND date_day >= current_date - 60) AS revenue_prev_30d,
          sum(revenue_orders) FILTER (WHERE date_day >= current_date - 30) AS orders_30d,
          sum(revenue_orders) FILTER (WHERE date_day <  current_date - 30 AND date_day >= current_date - 60) AS orders_prev_30d,
          sum(gross_margin)   FILTER (WHERE date_day >= current_date - 30) AS margin_30d,
          sum(cancelled_orders + returned_orders) FILTER (WHERE date_day >= current_date - 30) AS lost_orders_30d,
          sum(orders)         FILTER (WHERE date_day >= current_date - 30) AS all_orders_30d
        FROM d
    """)[0]
    return {
        "built_at": db._jsonable(datetime.fromtimestamp((GOLD / "mart_daily_sales.parquet").stat().st_mtime)),
        "kpis": kpis,
        "forecast": q(f"SELECT date_day, kind, revenue, forecast FROM {g('mart_revenue_forecast')} ORDER BY date_day"),
        "category_monthly": q(f"""
            SELECT category_name, year_month, revenue FROM {g('mart_category_monthly')}
            WHERE year_month >= strftime(current_date - INTERVAL 6 MONTH, '%Y-%m') ORDER BY year_month, category_name
        """),
        "rfm": q(f"""
            SELECT segment, count(*) AS customers, round(avg(monetary), 2) AS avg_spend, round(avg(recency_days)) AS avg_recency
            FROM {g('mart_customer_rfm')} GROUP BY segment ORDER BY customers DESC
        """),
        "cohorts": q(f"""
            SELECT cohort, months_since_first, cohort_size, retention_pct FROM {g('mart_cohort_retention')}
            WHERE cohort >= strftime(current_date - INTERVAL 9 MONTH, '%Y-%m') AND months_since_first <= 6
            ORDER BY cohort, months_since_first
        """),
        "top_products": q(f"""
            SELECT revenue_rank, product_name, category_name, current_price, units_sold, revenue, margin_pct, return_rate, stock
            FROM {g('mart_product_performance')} ORDER BY revenue_rank LIMIT 10
        """),
        "top_customers": q(f"""
            SELECT l.customer_id, l.full_name, l.country, l.orders, l.historical_ltv, l.predicted_12m_revenue, r.segment
            FROM {g('mart_customer_ltv')} l LEFT JOIN {g('mart_customer_rfm')} r USING (customer_id)
            ORDER BY l.historical_ltv DESC LIMIT 10
        """),
    }


# ------------------------------------------------------------------ simulator + pipeline
def _sim_running() -> bool:
    return simulator["proc"] is not None and simulator["proc"].poll() is None


@app.get("/api/admin/simulator", tags=["admin"])
def simulator_status():
    tail = SIM_LOG.read_text(errors="replace").splitlines()[-10:] if SIM_LOG.exists() else []
    return {"running": _sim_running(), "interval": simulator["interval"], "log": tail}


@app.post("/api/admin/simulator/start", tags=["admin"])
def simulator_start(interval: float = Query(5, ge=1, le=60)):
    if not _sim_running():
        STATE.mkdir(parents=True, exist_ok=True)
        log = SIM_LOG.open("w")
        simulator["proc"] = subprocess.Popen(
            [sys.executable, "-u", "source_app/generator.py", "stream", "--interval", str(interval)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
        )
        simulator["interval"] = interval
    return simulator_status()


@app.post("/api/admin/simulator/stop", tags=["admin"])
def simulator_stop():
    if _sim_running():
        simulator["proc"].terminate()
        simulator["proc"].wait(timeout=10)
    simulator["proc"] = None
    return simulator_status()


@app.post("/api/admin/pipeline/{step}", tags=["admin"])
def run_pipeline_step(step: str):
    if step not in PIPELINE_STEPS:
        raise HTTPException(404, f"Unknown step. Choose from: {', '.join(PIPELINE_STEPS)}")
    shown, argv = PIPELINE_STEPS[step]
    out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=600,
                         env={**os.environ, "SHOP_ROOT": str(ROOT)})
    return {"command": shown, "exit_code": out.returncode, "output": (out.stdout + out.stderr).strip()}


@app.get("/api/admin/lake", tags=["admin"])
def lake_status():
    con = duckdb.connect()

    def count(path: Path | str) -> int:
        return con.execute(f"SELECT count(*) FROM read_parquet('{Path(path).as_posix()}')").fetchone()[0]

    layers = []
    for t in TABLES:
        bronze = LAKE / "bronze" / t
        files = list(bronze.rglob("*.parquet")) if bronze.exists() else []
        silver, quarantine = LAKE / "silver" / f"{t}.parquet", LAKE / "quarantine" / f"{t}.parquet"
        layers.append({
            "table": t, "bronze_files": len(files),
            "bronze_rows": count(bronze / "**" / "*.parquet") if files else 0,
            "silver_rows": count(silver) if silver.exists() else 0,
            "quarantined": count(quarantine) if quarantine.exists() else 0,
        })

    wm_file, dq_file, log_file = STATE / "watermarks.json", STATE / "dq_log.jsonl", STATE / "ingestion_log.jsonl"
    dq = [json.loads(x) for x in dq_file.read_text().splitlines() if x.strip()] if dq_file.exists() else []
    runs = [json.loads(x) for x in log_file.read_text().splitlines()[-14:]] if log_file.exists() else []
    return {
        "layers": layers,
        "watermarks": json.loads(wm_file.read_text()) if wm_file.exists() else {},
        "dq_latest": [r for r in dq if dq and r["run_at"] == dq[-1]["run_at"]],
        "recent_extracts": runs[::-1],
    }
