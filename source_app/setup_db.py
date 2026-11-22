"""Create the ShopDB database and its tables.

Usage:
    python source_app/setup_db.py           # create if missing (safe to re-run)
    python source_app/setup_db.py --reset   # DROP and recreate ShopDB (deletes all data!)
"""
import argparse
import re
from pathlib import Path

from db import DATABASE, SERVER, get_connection

SCHEMA_FILES = [Path(__file__).with_name("schema.sql"),               # v1: shop tables
                Path(__file__).with_name("schema_marketplace.sql"),   # v2: vendors, riders, shipments, users
                Path(__file__).with_name("schema_payments.sql")]      # v3: online payments, commission, ledger, payouts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop and recreate the database")
    args = parser.parse_args()

    conn = get_connection("master", autocommit=True)
    cur = conn.cursor()
    if args.reset:
        print(f"Dropping database {DATABASE} ...")
        cur.execute(
            f"IF DB_ID(N'{DATABASE}') IS NOT NULL BEGIN "
            f"ALTER DATABASE [{DATABASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; "
            f"DROP DATABASE [{DATABASE}]; END"
        )
    cur.execute(f"IF DB_ID(N'{DATABASE}') IS NULL CREATE DATABASE [{DATABASE}]")
    conn.close()

    batches = [b for f in SCHEMA_FILES
               for b in re.split(r"^\s*GO\s*$", f.read_text(encoding="utf-8"), flags=re.M | re.I) if b.strip()]
    conn = get_connection(autocommit=True)
    cur = conn.cursor()
    for batch in batches:
        cur.execute(batch)
    cur.execute("SELECT name FROM sys.tables ORDER BY name")
    tables = [r[0] for r in cur.fetchall()]
    conn.close()
    print(f"{DATABASE} ready on {SERVER}. Tables: {', '.join(tables)}")


if __name__ == "__main__":
    main()
