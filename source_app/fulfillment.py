"""Run the fulfillment autopilot on its own (the ShopLite web app already runs it in the background).

Autopilot vendors accept and pack, the dispatcher assigns riders, autopilot riders pick up and deliver.
Human-operated vendors/riders (is_bot = 0) are left alone - they act in their portals.

Usage:
    python source_app/fulfillment.py               # every 2 seconds until Ctrl+C
    python source_app/fulfillment.py --once        # a single pass
"""
import argparse
import time

import marketplace
from db import get_connection


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    conn = get_connection()
    print("Fulfillment autopilot running - Ctrl+C to stop.")
    try:
        while True:
            done = marketplace.engine_tick(conn)
            if done:
                print(f"[{marketplace.now():%H:%M:%S}] " + ", ".join(f"{k}: {v}" for k, v in done.items()))
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
