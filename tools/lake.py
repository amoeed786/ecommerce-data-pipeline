"""Query the data lake with plain SQL - no file paths needed.

Every Parquet table is available as a view named <layer>_<table>:
    bronze_orders, silver_orders, quarantine_orders, bronze_customers, silver_customers, ...

Usage (from the project folder):
    python tools/lake.py "select * from silver_orders order by order_id desc limit 5"
    python tools/lake.py "select order_id, status, _batch_id from bronze_orders where order_id = 15050"
    python tools/lake.py                      # no SQL: list the available views and their row counts
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

LAKE = Path(__file__).resolve().parents[1] / "data_lake"


def main() -> None:
    con = duckdb.connect()
    views = []
    for layer in ("bronze", "silver", "quarantine"):
        base = LAKE / layer
        if not base.exists():
            continue
        if layer == "bronze":
            sources = {d.name: (d / "**" / "*.parquet").as_posix() for d in base.iterdir()
                       if d.is_dir() and any(d.rglob("*.parquet"))}
        else:
            sources = {f.stem: f.as_posix() for f in base.glob("*.parquet")}
        for table, path in sorted(sources.items()):
            name = f"{layer}_{table}"
            con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}', union_by_name = true)")
            views.append(name)

    if len(sys.argv) < 2:
        print("Available views (rows):")
        for v in views:
            print(f"  {v:<26} {con.execute(f'SELECT count(*) FROM {v}').fetchone()[0]:>8,}")
        print('\nExample: python tools/lake.py "select * from silver_orders order by order_id desc limit 5"')
        return

    con.sql(" ".join(sys.argv[1:])).show(max_rows=50, max_width=250)


if __name__ == "__main__":
    main()
