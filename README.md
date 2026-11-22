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
| 3 | Silver: cleaning, dedup, data-quality checks | |
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
