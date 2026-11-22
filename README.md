# Ecommerce Data Pipeline (learning project)

An end-to-end, 100% free, local data pipeline:

```
SQL Server (ShopDB)  ->  Data lake (Parquet: bronze/silver/gold)  ->  DuckDB + dbt  ->  Dashboard
        ^                          orchestrated by Dagster
 fake shop activity (Faker)
```

## Roadmap

| Phase | What | Status |
|---|---|---|
| 1 | Source system: `ShopDB` on SQL Server + fake data generator | Done |
| 2 | Ingestion: incremental extract SQL Server -> bronze Parquet | Done |
| 3 | Silver: cleaning, dedup, data-quality checks | Done |
| ★ | Shop web app: live monitor, storefront, back office, pipeline controls | Done |
| 4 | Gold: dbt + DuckDB star schema (facts/dims), tests, docs | |
| 5 | Analytics: revenue, top products, CLV, RFM, cohorts | |
| 6 | Orchestration with Dagster | |
| 7 | Dashboard (Streamlit / Power BI) | |
| + | Bonus: CDC, SCD2, forecasting | |

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # then edit SQL_SERVER if needed
```

## Phase 1 - Source system

```powershell
.\.venv\Scripts\python.exe source_app\setup_db.py              # create ShopDB + tables (--reset to wipe)
.\.venv\Scripts\python.exe source_app\generator.py seed        # ~1 year of history
.\.venv\Scripts\python.exe source_app\generator.py stream      # live activity, Ctrl+C to stop
.\.venv\Scripts\python.exe source_app\check_db.py              # row counts + integrity checks
```

**Tables:** `categories`, `customers`, `products`, `inventory`, `orders`, `order_items`, `payments`.
Every table has `updated_at`, which Phase 2 uses as the watermark for incremental loads.

**Built-in realism:** business growth, weekend and holiday peaks, evening shopping hours,
Pareto best-sellers/loyal customers, order lifecycle (pending -> paid -> shipped -> delivered,
with cancellations and returns), price changes and customer relocations (for SCD2 later).

## Phase 2 - Ingestion (bronze)

```powershell
.\.venv\Scripts\python.exe ingestion\extract.py             # incremental extract, all tables
.\.venv\Scripts\python.exe ingestion\extract.py --full      # ignore watermarks, reload everything
.\.venv\Scripts\python.exe ingestion\check_bronze.py        # reconcile bronze vs ShopDB
```

**How it works:** each table has a watermark (the newest `updated_at` already extracted),
stored in `data_lake/_state/watermarks.json`. A run pulls rows with
`watermark < updated_at <= upper bound`, where the upper bound is SQL Server's clock minus
5 seconds, so rows still being written are picked up next time. The first run is a full load.

**Layout:**

```
data_lake/
  bronze/<table>/ingest_date=YYYY-MM-DD/<table>_<batch_id>.parquet
  _state/watermarks.json        # where each table is up to
  _state/ingestion_log.jsonl    # one line per table per run (rows, timing, range)
```

- **Append-only:** when an order changes status it is written again in a later batch, so bronze
  keeps every version. Silver (Phase 3) keeps the latest version of each record.
- **Lineage columns:** `_batch_id`, `_ingested_at`, `_source_table`.
- **Explicit schema:** Parquet types come from the SQL column types, so money stays an exact
  decimal and every batch has the same schema.
- **Safe writes:** a file only gets its `.parquet` name once it's complete, and the watermark only
  moves after the file is written. If a run crashes, the next one re-extracts that range.
- **Limitation:** rows deleted in the source aren't detected, because a watermark only sees
  inserts and updates. ShopDB never deletes rows. CDC (bonus phase) would handle deletes.
- **Limitation:** a watermark sees each row as it is *at extract time*. If an order goes
  pending -> paid -> shipped between two extracts, bronze only gets `shipped`. CDC fixes this too.

## Phase 3 - Silver (clean, dedup, quality checks)

```powershell
.\.venv\Scripts\python.exe transform\silver.py              # rebuild silver from bronze
.\.venv\Scripts\python.exe source_app\inject_bad_data.py    # optional: add messy rows to see the checks fire
```

For each table, parents first, DuckDB:

1. **Dedup:** keeps the newest version of each primary key (bronze has all versions).
2. **Clean:** trims and collapses whitespace, lowercases emails/status/payment method,
   uppercases SKUs, and maps country variants (`usa`, `U.K.`, `Deutschland`) to one spelling.
3. **Check:**
   - **ERROR** rules send the row to `data_lake/quarantine/<table>.parquet` with a `_dq_reason`.
     Examples: orphan foreign keys, order dated in the future, invalid status, negative price.
     Children of a quarantined row are quarantined too, as orphans.
   - **WARN** rules keep the row but count it. Examples: invalid email, unknown country,
     order total doesn't match its items, payment amount doesn't match the order.
4. **Write:** `data_lake/silver/<table>.parquet`. Results are appended to `data_lake/_state/dq_log.jsonl`.

Silver is fully rebuilt on each run, so it's idempotent: running it twice gives the same result.

## Shop web app

```powershell
.\.venv\Scripts\streamlit.exe run shop_app\app.py           # opens http://localhost:8501
```

| Page | What you can do |
|---|---|
| Live monitor | Auto-refreshing KPIs, latest orders, orders per minute, recently changed rows, low stock |
| Storefront | Pick a customer (or sign one up), add products to a cart, place an order |
| Orders admin | Move orders through pending -> paid -> shipped -> delivered (or cancel / return) |
| Catalog | Change prices, restock, move a customer to a new city (future SCD2 material) |
| Pipeline | Start/stop the simulator, run extract / silver / checks with one click, see watermarks, DQ results and any layer's data |

Everything the app does is a normal write to ShopDB with a fresh `updated_at`, so the next
extract picks it up, just like a real shop backend feeding a data pipeline.

## Command cheat sheet

All commands are run from the project folder: `cd C:\Projects\ecommerce-data-pipeline`

| Step | Command | What it does |
|---|---|---|
| Setup | `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` | Install libraries |
| Source | `.\.venv\Scripts\python.exe source_app\setup_db.py` | Create ShopDB + tables (`--reset` wipes) |
| Source | `.\.venv\Scripts\python.exe source_app\generator.py seed` | One year of fake history |
| Source | `.\.venv\Scripts\python.exe source_app\generator.py stream` | Live shop activity (Ctrl+C stops) |
| Source | `.\.venv\Scripts\python.exe source_app\inject_bad_data.py` | Add messy rows for silver to catch |
| Source | `.\.venv\Scripts\python.exe source_app\check_db.py` | Row counts + integrity checks |
| Bronze | `.\.venv\Scripts\python.exe ingestion\extract.py` | Incremental extract (`--full` reloads) |
| Bronze | `.\.venv\Scripts\python.exe ingestion\check_bronze.py` | Reconcile bronze vs ShopDB |
| Silver | `.\.venv\Scripts\python.exe transform\silver.py` | Dedup + clean + quality checks |
| App | `.\.venv\Scripts\streamlit.exe run shop_app\app.py` | Web interface on http://localhost:8501 |
| Explore | `.\.venv\Scripts\python.exe -c "import duckdb; duckdb.sql(\"select * from 'data_lake/silver/orders.parquet' limit 5\").show()"` | Query any Parquet file with SQL |
