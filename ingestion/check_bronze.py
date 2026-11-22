"""Phase 2 - reconcile bronze Parquet against ShopDB.

For each table:
  files / batches / rows  - what's in bronze (rows include repeated versions of a record)
  keys                    - distinct primary keys in bronze
  missing                 - source rows (changed before the watermark) whose key isn't in bronze
  stale                   - source rows whose newest bronze version has an older updated_at
Both missing and stale must be 0. Rows changed after the watermark are expected to be
behind until the next extract, so they are only counted (as "pending").
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pyarrow as pa

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from ingestion.extract import BRONZE_DIR, TABLES, WATERMARK_FILE  # noqa: E402
from source_app.db import get_connection  # noqa: E402

PRIMARY_KEYS = {
    "categories": "category_id",
    "customers": "customer_id",
    "products": "product_id",
    "inventory": "product_id",
    "orders": "order_id",
    "order_items": "order_item_id",
    "payments": "payment_id",
}


def main() -> None:
    if not WATERMARK_FILE.exists():
        sys.exit("No watermarks yet - run ingestion/extract.py first.")
    marks = json.loads(WATERMARK_FILE.read_text())

    conn = get_connection()
    db = duckdb.connect()
    problems = 0

    print(f"  {'table':<12} {'files':>5} {'batches':>7} {'rows':>9} {'keys':>8} "
          f"{'source':>8} {'missing':>7} {'stale':>6} {'pending':>7}")
    for table in TABLES:
        pk = PRIMARY_KEYS[table]
        glob = (BRONZE_DIR / table / "**" / "*.parquet").as_posix()
        if table not in marks or not list((BRONZE_DIR / table).rglob("*.parquet")):
            print(f"  {table:<12} (no bronze data)")
            problems += 1
            continue

        # Source keys + updated_at, handed to DuckDB as an Arrow table.
        rows = conn.cursor().execute(f"SELECT [{pk}], updated_at FROM dbo.[{table}]").fetchall()
        source = pa.table({  # noqa: F841 - DuckDB reads this local variable by name
            "pk": pa.array([r[0] for r in rows], pa.int64()),
            "updated_at": pa.array([r[1] for r in rows], pa.timestamp("us")),
        })

        stats = db.execute(f"""
            WITH bronze AS (
                SELECT {pk} AS pk, updated_at, _batch_id, filename
                FROM read_parquet('{glob}', filename = true)
            ),
            latest AS (
                SELECT pk, max(updated_at) AS updated_at FROM bronze GROUP BY pk
            ),
            src AS (
                SELECT *, updated_at <= TIMESTAMP '{marks[table]}' AS covered FROM source
            )
            SELECT
                (SELECT count(DISTINCT filename) FROM bronze),
                (SELECT count(DISTINCT _batch_id) FROM bronze),
                (SELECT count(*) FROM bronze),
                (SELECT count(*) FROM latest),
                (SELECT count(*) FROM src),
                (SELECT count(*) FROM src LEFT JOIN latest l USING (pk)
                   WHERE src.covered AND l.pk IS NULL),
                (SELECT count(*) FROM src JOIN latest l USING (pk)
                   WHERE src.covered AND l.updated_at < src.updated_at),
                (SELECT count(*) FROM src WHERE NOT covered)
        """).fetchone()
        files, batches, total, keys, src_rows, missing, stale, pending = stats
        problems += missing + stale
        print(f"  {table:<12} {files:>5} {batches:>7} {total:>9,} {keys:>8,} "
              f"{src_rows:>8,} {missing:>7} {stale:>6} {pending:>7}")

    conn.close()
    print("\nOK - bronze is consistent with the source." if problems == 0
          else f"\n{problems} problem(s) found.")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
