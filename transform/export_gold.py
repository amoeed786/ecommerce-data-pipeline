"""Phase 4/7 - export the gold layer (dbt's star schema + analytics marts) to Parquet.

dbt builds gold inside warehouse/shop.duckdb. This copies every table in the `core` and `analytics`
schemas to data_lake/gold/<table>.parquet, which completes the bronze/silver/gold data lake and lets
any tool read gold without opening the DuckDB file (only one process can write to it at a time):
ShopLite's analytics page, Power BI ("Get data -> Parquet"), pandas, another DuckDB...

Usage:
    python transform/export_gold.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from ingestion.extract import atomic_replace  # noqa: E402
WAREHOUSE = PROJECT_ROOT / "warehouse" / "shop.duckdb"
GOLD_DIR = PROJECT_ROOT / "data_lake" / "gold"


def main() -> None:
    if not WAREHOUSE.exists():
        sys.exit("No warehouse yet - run dbt first:  dbt build --project-dir dbt_shop --profiles-dir dbt_shop")
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    tables = con.execute("""
        SELECT table_schema, table_name FROM information_schema.tables
        WHERE table_schema IN ('core', 'analytics') AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name
    """).fetchall()

    print(f"Exporting {len(tables)} gold tables to {GOLD_DIR.relative_to(PROJECT_ROOT)}")
    for schema, table in tables:
        target = GOLD_DIR / f"{table}.parquet"
        tmp = target.with_suffix(".parquet.tmp")
        con.execute(f"COPY {schema}.{table} TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
        atomic_replace(tmp, target)  # retries if the analytics page is reading this file right now
        rows = con.execute(f"SELECT count(*) FROM {schema}.{table}").fetchone()[0]
        print(f"  {schema + '.' + table:<36} {rows:>8,} rows")
    con.close()


if __name__ == "__main__":
    main()
