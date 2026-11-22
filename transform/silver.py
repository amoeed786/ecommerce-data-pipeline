"""Phase 3 - silver layer: bronze Parquet -> cleaned, deduplicated, quality-checked Parquet.

For each table (parents first), DuckDB does:
  1. DEDUP    bronze keeps every version of a record; keep only the newest one per primary key.
  2. CLEAN    trim/collapse whitespace, lowercase emails, standardise country names and status values.
  3. CHECK    ERROR rules  -> the row goes to data_lake/quarantine/<table>.parquet (with the reason)
              WARN rules   -> the row stays in silver, but is counted and reported
  4. WRITE    data_lake/silver/<table>.parquet

Silver is fully rebuilt from bronze on every run. That makes it idempotent (run it twice and you
get the same result) and simple to reason about; the data is small enough that this is fast.

Usage:
    python transform/silver.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from ingestion.extract import BRONZE_DIR, LAKE_DIR, STATE_DIR  # noqa: E402

SILVER_DIR = LAKE_DIR / "silver"
QUARANTINE_DIR = LAKE_DIR / "quarantine"
DQ_LOG_FILE = STATE_DIR / "dq_log.jsonl"

# Collapse runs of whitespace and trim; empty strings become NULL.
def tidy(col: str) -> str:
    return rf"nullif(regexp_replace(trim({col}), '\s+', ' ', 'g'), '')"


# Variants we accept for each country -> the one standard spelling.
# Keys are lowercase with everything except letters removed ("U.S.A." -> "usa").
COUNTRY_ALIASES = {
    "United States": ["unitedstates", "unitedstatesofamerica", "usa", "us", "america"],
    "United Kingdom": ["unitedkingdom", "uk", "greatbritain", "gb", "england"],
    "Pakistan": ["pakistan", "pk"],
    "Germany": ["germany", "deutschland", "de"],
    "Canada": ["canada", "ca"],
    "United Arab Emirates": ["unitedarabemirates", "uae", "emirates"],
}
COUNTRY_KEY = "lower(regexp_replace({col}, '[^A-Za-z]', '', 'g'))"

# ------------------------------------------------------------------ table rules
# clean:    SELECT over `src` (the deduplicated bronze rows). Can join `country_map`.
# errors:   rule name -> condition that is TRUE for a bad row (row is quarantined).
#           Orphan checks look at the silver tables built earlier in this run.
# warnings: rule name -> condition that is TRUE for a suspicious row (row is kept).
TABLES: list[dict] = [
    {
        "name": "categories", "pk": "category_id",
        "clean": f"SELECT * REPLACE ({tidy('category_name')} AS category_name) FROM src",
        "errors": {"category_name_missing": "category_name IS NULL"},
        "warnings": {},
    },
    {
        "name": "customers", "pk": "customer_id",
        "clean": f"""
            SELECT c.* REPLACE (
                {tidy('c.first_name')} AS first_name,
                {tidy('c.last_name')}  AS last_name,
                lower(trim(c.email))   AS email,
                {tidy('c.phone')}      AS phone,
                {tidy('c.city')}       AS city,
                coalesce(m.country, {tidy('c.country')}) AS country
            )
            FROM src c
            LEFT JOIN country_map m ON m.key = {COUNTRY_KEY.format(col='c.country')}
        """,
        "errors": {"name_missing": "first_name IS NULL OR last_name IS NULL"},
        "warnings": {
            "email_invalid": r"NOT regexp_matches(email, '^[^@\s]+@[^@\s]+\.[a-z]{2,}$')",
            "country_unknown": "country NOT IN (SELECT country FROM country_map)",
            "signup_in_future": "signup_date > CAST(_ingested_at AS DATE)",
        },
    },
    {
        "name": "products", "pk": "product_id",
        "clean": f"SELECT * REPLACE ({tidy('product_name')} AS product_name, upper(trim(sku)) AS sku) FROM src",
        "errors": {
            "negative_price": "unit_price < 0 OR unit_cost < 0",
            "category_missing": "category_id NOT IN (SELECT category_id FROM silver_categories)",
        },
        "warnings": {"sold_below_cost": "unit_cost > unit_price"},
    },
    {
        "name": "inventory", "pk": "product_id",
        "clean": "SELECT * FROM src",
        "errors": {"product_missing": "product_id NOT IN (SELECT product_id FROM silver_products)"},
        "warnings": {"negative_stock": "quantity_on_hand < 0"},
    },
    {
        "name": "orders", "pk": "order_id",
        "clean": f"""
            SELECT o.* REPLACE (
                lower(trim(o.status)) AS status,
                {tidy('o.shipping_city')} AS shipping_city,
                coalesce(m.country, {tidy('o.shipping_country')}) AS shipping_country
            )
            FROM src o
            LEFT JOIN country_map m ON m.key = {COUNTRY_KEY.format(col='o.shipping_country')}
        """,
        "errors": {
            "customer_missing": "customer_id NOT IN (SELECT customer_id FROM silver_customers)",
            "order_in_future": "order_date > _ingested_at",
            "bad_status": "status NOT IN ('pending','paid','shipped','delivered','cancelled','returned')",
        },
        "warnings": {
            "total_mismatch_items": """
                abs(total_amount - coalesce((SELECT sum(line_total) FROM src_order_items i
                                             WHERE i.order_id = checked.order_id), 0)) > 0.01
            """,
        },
    },
    {
        "name": "order_items", "pk": "order_item_id",
        "clean": "SELECT * FROM src",
        "errors": {
            "order_missing": "order_id NOT IN (SELECT order_id FROM silver_orders)",
            "product_missing": "product_id NOT IN (SELECT product_id FROM silver_products)",
            "bad_quantity": "quantity <= 0",
        },
        "warnings": {
            "line_total_mismatch": "abs(line_total - round(quantity * unit_price * (1 - discount), 2)) > 0.01",
        },
    },
    {
        "name": "payments", "pk": "payment_id",
        "clean": "SELECT * REPLACE (lower(trim(payment_method)) AS payment_method) FROM src",
        "errors": {"order_missing": "order_id NOT IN (SELECT order_id FROM silver_orders)"},
        "warnings": {
            "amount_mismatch_order": """
                abs(amount - (SELECT total_amount FROM silver_orders o
                              WHERE o.order_id = checked.order_id)) > 0.01
            """,
            "paid_before_order": """
                paid_at < (SELECT order_date FROM silver_orders o WHERE o.order_id = checked.order_id)
            """,
        },
    },
]


def bronze_glob(table: str) -> str:
    return (BRONZE_DIR / table / "**" / "*.parquet").as_posix()


def dedup_sql(table: str, pk: str) -> str:
    """Newest version of each record. Ties on updated_at are broken by the later batch."""
    return f"""
        SELECT * EXCLUDE (_source_table)
        FROM read_parquet('{bronze_glob(table)}', union_by_name = true)
        QUALIFY row_number() OVER (PARTITION BY {pk} ORDER BY updated_at DESC, _ingested_at DESC) = 1
    """


def write_parquet(db: duckdb.DuckDBPyConnection, relation: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    db.execute(f"COPY ({relation}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd)")
    tmp.replace(path)


def build_table(db: duckdb.DuckDBPyConnection, cfg: dict, loaded_at: datetime) -> dict:
    name, pk = cfg["name"], cfg["pk"]
    started = time.perf_counter()

    bronze_rows = db.execute(f"SELECT count(*) FROM read_parquet('{bronze_glob(name)}')").fetchone()[0]
    db.execute(f"CREATE OR REPLACE TEMP VIEW src AS {dedup_sql(name, pk)}")
    deduped = db.execute("SELECT count(*) FROM src").fetchone()[0]

    # Each ERROR rule that fires adds its name to the _dq_errors list.
    error_cases = ", ".join(
        f"CASE WHEN {cond} THEN '{rule}' END" for rule, cond in cfg["errors"].items()
    ) or "NULL"
    db.execute(f"CREATE OR REPLACE TEMP VIEW cleaned AS {cfg['clean']}")
    db.execute(f"""
        CREATE OR REPLACE TEMP TABLE checked AS
        SELECT *, list_filter([{error_cases}], lambda x: x IS NOT NULL) AS _dq_errors FROM cleaned
    """)

    # Primary key must be present and unique (dedup guarantees unique, so this catches NULLs).
    db.execute(f"""
        UPDATE checked SET _dq_errors = list_append(_dq_errors, 'pk_missing') WHERE {pk} IS NULL
    """)

    db.execute(f"""
        CREATE OR REPLACE TABLE silver_{name} AS
        SELECT * EXCLUDE (_dq_errors), TIMESTAMP '{loaded_at}' AS _silver_loaded_at
        FROM checked WHERE len(_dq_errors) = 0
    """)
    silver_rows = db.execute(f"SELECT count(*) FROM silver_{name}").fetchone()[0]

    error_counts = {
        rule: db.execute(f"SELECT count(*) FROM checked WHERE list_contains(_dq_errors, '{rule}')").fetchone()[0]
        for rule in [*cfg["errors"], "pk_missing"]
    }
    # Warnings are evaluated on the rows that made it into silver.
    warning_counts = {
        rule: db.execute(
            f"SELECT count(*) FROM checked WHERE len(_dq_errors) = 0 AND ({cond})"
        ).fetchone()[0]
        for rule, cond in cfg["warnings"].items()
    }

    write_parquet(db, f"SELECT * FROM silver_{name}", SILVER_DIR / f"{name}.parquet")
    quarantine_file = QUARANTINE_DIR / f"{name}.parquet"
    quarantined = deduped - silver_rows
    if quarantined:
        write_parquet(db, f"""
            SELECT * EXCLUDE (_dq_errors), array_to_string(_dq_errors, ', ') AS _dq_reason,
                   TIMESTAMP '{loaded_at}' AS _quarantined_at
            FROM checked WHERE len(_dq_errors) > 0
        """, quarantine_file)
    elif quarantine_file.exists():
        quarantine_file.unlink()  # problems from an earlier run are gone

    return {
        "table": name, "bronze_rows": bronze_rows, "deduped": deduped,
        "silver_rows": silver_rows, "quarantined": quarantined,
        "errors": {k: v for k, v in error_counts.items() if v},
        "warnings": {k: v for k, v in warning_counts.items() if v},
        "seconds": round(time.perf_counter() - started, 2),
    }


def main() -> None:
    if not BRONZE_DIR.exists():
        sys.exit("No bronze data - run ingestion/extract.py first.")

    loaded_at = datetime.now().replace(microsecond=0)
    db = duckdb.connect()  # in-memory: silver lives in Parquet files, not in a database file

    aliases = [(key, country) for country, keys in COUNTRY_ALIASES.items() for key in keys]
    db.execute("CREATE TABLE country_map (key VARCHAR, country VARCHAR)")
    db.executemany("INSERT INTO country_map VALUES (?, ?)", aliases)
    # Order-total check needs items before orders are built, so expose deduped items early.
    db.execute(f"CREATE TEMP VIEW src_order_items AS {dedup_sql('order_items', 'order_item_id')}")

    print(f"Silver build {loaded_at:%Y-%m-%d %H:%M:%S}")
    print(f"  {'table':<12} {'bronze':>8} {'dedup':>8} {'silver':>8} {'quarant.':>8}  issues")
    results = []
    for cfg in TABLES:
        r = build_table(db, cfg, loaded_at)
        results.append(r)
        issues = [f"ERROR {k}={v}" for k, v in r["errors"].items()]
        issues += [f"warn {k}={v}" for k, v in r["warnings"].items()]
        print(f"  {r['table']:<12} {r['bronze_rows']:>8,} {r['deduped']:>8,} {r['silver_rows']:>8,} "
              f"{r['quarantined']:>8,}  {'; '.join(issues) or 'clean'}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with DQ_LOG_FILE.open("a") as f:
        for r in results:
            f.write(json.dumps({"run_at": loaded_at.isoformat(), **r}) + "\n")

    total_q = sum(r["quarantined"] for r in results)
    print(f"\nSilver written to {SILVER_DIR.relative_to(PROJECT_ROOT)}"
          + (f"; {total_q} row(s) quarantined in {QUARANTINE_DIR.relative_to(PROJECT_ROOT)}" if total_q else ""))


if __name__ == "__main__":
    main()
