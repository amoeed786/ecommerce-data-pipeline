"""Phase 2 - incremental extract: SQL Server (ShopDB) -> bronze Parquet.

How it works, per table:
  1. Read the table's last watermark from data_lake/_state/watermarks.json
     (none yet = full load).
  2. Pick an upper bound once per run: SQL Server's clock minus a small safety lag,
     so rows still being written in the current second are left for the next run.
  3. SELECT rows WHERE updated_at > watermark AND updated_at <= upper_bound.
  4. Append them as a new Parquet file under
         data_lake/bronze/<table>/ingest_date=YYYY-MM-DD/<table>_<batch_id>.parquet
     plus lineage columns (_batch_id, _ingested_at, _source_table).
  5. Only after the file is safely written, move the watermark to upper_bound.

Bronze is append-only: an order that changes status shows up again in a later batch.
Deduplication to the latest version is the silver layer's job (Phase 3).

Usage:
    python ingestion/extract.py                  # incremental, all tables
    python ingestion/extract.py --tables orders  # just some tables
    python ingestion/extract.py --full           # ignore watermarks, reload everything
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from source_app.db import get_connection  # noqa: E402

LAKE_DIR = PROJECT_ROOT / os.getenv("LAKE_DIR", "data_lake")
BRONZE_DIR = LAKE_DIR / "bronze"
STATE_DIR = LAKE_DIR / "_state"
WATERMARK_FILE = STATE_DIR / "watermarks.json"
RUN_LOG_FILE = STATE_DIR / "ingestion_log.jsonl"

# Parents before children, so a batch never holds an order whose customer isn't extracted yet.
TABLES = ["categories", "customers", "vendors", "riders", "products", "inventory",
          "orders", "shipments", "order_items", "payments", "users",
          "commission_rules", "payouts", "ledger_entries", "reviews"]
# Columns that must never leave the source system. (user_sessions and app_settings aren't extracted at all.)
EXCLUDED_COLUMNS = {"users": {"password_hash"}}
WATERMARK_COLUMN = "updated_at"
SAFETY_LAG_SECONDS = 5
FETCH_SIZE = 50_000
EPOCH = datetime(1900, 1, 1)


# ---------------------------------------------------------------- schema
def arrow_type(data_type: str, precision: int | None, scale: int | None) -> pa.DataType:
    """Map a SQL Server column type to an explicit Arrow type.

    Being explicit (instead of letting Arrow guess from the values) keeps every batch's
    schema identical, and keeps money as exact decimals rather than floats.
    """
    t = data_type.lower()
    if t in ("int", "smallint", "tinyint"):
        return pa.int32()
    if t == "bigint":
        return pa.int64()
    if t == "bit":
        return pa.bool_()
    if t in ("decimal", "numeric", "money", "smallmoney"):
        return pa.decimal128(precision or 19, scale or 4)
    if t in ("float", "real"):
        return pa.float64()
    if t == "date":
        return pa.date32()
    if t in ("datetime", "datetime2", "smalldatetime"):
        return pa.timestamp("us")
    if t in ("char", "varchar", "nchar", "nvarchar", "text", "ntext", "uniqueidentifier"):
        return pa.string()
    raise ValueError(f"No Arrow mapping for SQL type {data_type!r}")


def table_schema(cur, table: str) -> pa.Schema:
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        table,
    )
    rows = [r for r in cur.fetchall() if r[0] not in EXCLUDED_COLUMNS.get(table, set())]
    if not rows:
        raise RuntimeError(f"Table dbo.{table} not found")
    return pa.schema(
        pa.field(name, arrow_type(dtype, prec, scale), nullable=(nullable == "YES"))
        for name, dtype, prec, scale, nullable in rows
    )


# ---------------------------------------------------------------- state
def load_watermarks() -> dict[str, str]:
    if WATERMARK_FILE.exists():
        return json.loads(WATERMARK_FILE.read_text())
    return {}


def atomic_replace(src: Path, dst: Path, attempts: int = 40, wait: float = 0.25) -> None:
    """Swap a finished temp file into place (readers never see a half-written file).

    On Windows the swap fails with "access denied" if another process has `dst` open at that instant -
    e.g. the automatic pipeline's sensor or the admin page reading it. That's momentary, so retry for a
    few seconds instead of failing the run.
    """
    for attempt in range(attempts):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait)


def save_watermarks(marks: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WATERMARK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(marks, indent=2, sort_keys=True))
    atomic_replace(tmp, WATERMARK_FILE)


def log_run(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_FILE.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def pending_changes(conn) -> dict[str, datetime]:
    """Tables with rows the lake hasn't extracted yet -> newest pending updated_at.

    A cheap check (one MAX per table, all on indexed updated_at columns) that the automatic pipeline
    runs every 30 seconds to decide whether there's anything to do.
    """
    marks = load_watermarks()
    sql = " UNION ALL ".join(f"SELECT '{t}', MAX([{WATERMARK_COLUMN}]) FROM dbo.[{t}]" for t in TABLES)
    pending = {}
    for table, newest in conn.cursor().execute(sql).fetchall():
        if newest is not None and (table not in marks or newest > datetime.fromisoformat(marks[table])):
            pending[table] = newest
    return pending


# ---------------------------------------------------------------- extract
def extract_table(conn, table: str, low: datetime, high: datetime, batch_id: str,
                  ingested_at: datetime) -> tuple[int, Path | None]:
    cur = conn.cursor()
    schema = table_schema(cur, table)
    columns = ", ".join(f"[{name}]" for name in schema.names)
    cur.execute(
        f"SELECT {columns} FROM dbo.[{table}] "
        f"WHERE [{WATERMARK_COLUMN}] > ? AND [{WATERMARK_COLUMN}] <= ? "
        f"ORDER BY [{WATERMARK_COLUMN}]",
        low, high,
    )

    lineage = [
        pa.field("_batch_id", pa.string(), nullable=False),
        pa.field("_ingested_at", pa.timestamp("us"), nullable=False),
        pa.field("_source_table", pa.string(), nullable=False),
    ]
    out_schema = pa.schema(list(schema) + lineage)

    out_dir = BRONZE_DIR / table / f"ingest_date={ingested_at:%Y-%m-%d}"
    out_file = out_dir / f"{table}_{batch_id}.parquet"
    tmp_file = out_file.with_suffix(".parquet.tmp")

    writer = None
    total = 0
    try:
        while True:
            rows = cur.fetchmany(FETCH_SIZE)
            if not rows:
                break
            n = len(rows)
            arrays = [pa.array([r[i] for r in rows], type=f.type) for i, f in enumerate(schema)]
            arrays += [
                pa.array([batch_id] * n, pa.string()),
                pa.array([ingested_at] * n, pa.timestamp("us")),
                pa.array([table] * n, pa.string()),
            ]
            if writer is None:
                out_dir.mkdir(parents=True, exist_ok=True)
                writer = pq.ParquetWriter(tmp_file, out_schema, compression="snappy")
            writer.write_table(pa.Table.from_arrays(arrays, schema=out_schema))
            total += n
    finally:
        if writer is not None:
            writer.close()

    if total == 0:
        return 0, None
    atomic_replace(tmp_file, out_file)  # only a complete file ever gets the .parquet name
    return total, out_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental extract ShopDB -> bronze Parquet")
    parser.add_argument("--tables", nargs="+", choices=TABLES, default=TABLES)
    parser.add_argument("--full", action="store_true", help="ignore watermarks and reload everything")
    args = parser.parse_args()

    marks = load_watermarks()
    ingested_at = datetime.now().replace(microsecond=0)
    batch_id = ingested_at.strftime("%Y%m%dT%H%M%S")

    conn = get_connection()
    db_now = conn.cursor().execute("SELECT SYSDATETIME()").fetchval()
    high = db_now.replace(microsecond=0) - timedelta(seconds=SAFETY_LAG_SECONDS)

    print(f"Batch {batch_id}  (upper bound {high:%Y-%m-%d %H:%M:%S})")
    print(f"  {'table':<12} {'from':<20} {'rows':>9}  file")
    grand_total = 0
    for table in [t for t in TABLES if t in args.tables]:  # keep parent->child order
        low = EPOCH if args.full or table not in marks else datetime.fromisoformat(marks[table])
        started = time.perf_counter()
        rows, path = extract_table(conn, table, low, high, batch_id, ingested_at)
        seconds = round(time.perf_counter() - started, 2)

        # Advance the watermark even when 0 rows: nothing changed up to `high`.
        marks[table] = high.isoformat()
        save_watermarks(marks)
        log_run({
            "batch_id": batch_id, "table": table, "rows": rows, "seconds": seconds,
            "watermark_from": low.isoformat(), "watermark_to": high.isoformat(),
            "file": str(path.relative_to(PROJECT_ROOT)) if path else None,
            "mode": "full" if low == EPOCH else "incremental",
        })
        grand_total += rows
        shown_from = "(full load)" if low == EPOCH else f"{low:%Y-%m-%d %H:%M:%S}"
        shown_file = path.relative_to(LAKE_DIR) if path else "-"
        print(f"  {table:<12} {shown_from:<20} {rows:>9,}  {shown_file}")

    conn.close()
    print(f"Done: {grand_total:,} rows extracted.")


if __name__ == "__main__":
    main()
