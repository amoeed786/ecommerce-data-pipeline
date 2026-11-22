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
| 2 | Ingestion: incremental extract SQL Server -> bronze Parquet | |
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
