r"""Start everything with one command.

    .\.venv\Scripts\python.exe start.py              # shop + autopilot + automatic pipeline
    .\.venv\Scripts\python.exe start.py --simulate   # ...plus fake shoppers placing orders

What runs:
  ShopLite      http://localhost:8000   storefront, vendor/rider/admin portals, live feed,
                                        fulfillment autopilot (vendors pack, riders deliver)
  Dagster       http://localhost:3000   automatic pipeline: a sensor checks ShopDB every 30 s and runs
                                        extract -> silver -> dbt -> gold export whenever there are changes
  Simulator     (with --simulate)       new customers and orders every few seconds

Every new entry then flows to the end by itself:
  order placed -> (admin Start / auto-approve) -> store packs -> rider delivers
               -> bronze -> silver -> gold -> /analytics refreshes itself.

Press Ctrl+C once to stop everything.
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BIN = Path(sys.executable).parent
LOGS = ROOT / "data_lake" / "_state" / "logs"


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and everything it started (dagster dev spawns a webserver and a daemon)."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", help="also run fake shoppers")
    parser.add_argument("--interval", type=float, default=5, help="seconds between simulator ticks")
    parser.add_argument("--port", type=int, default=8000, help="ShopLite port")
    parser.add_argument("--dagster-port", type=int, default=3000)
    parser.add_argument("--no-pipeline", action="store_true", help="don't start Dagster (no automatic pipeline)")
    args = parser.parse_args()

    for port, name in [(args.port, "ShopLite"), (args.dagster_port, "Dagster")]:
        if name == "Dagster" and args.no_pipeline:
            continue
        if not port_free(port):
            sys.exit(f"Port {port} ({name}) is already in use - is it already running? Stop it first "
                     f"(Ctrl+C in its terminal) or pick another port with --port / --dagster-port.")

    env = {**os.environ, "SHOP_ROOT": str(ROOT), "DAGSTER_HOME": str(ROOT / ".dagster_home"),
           "DAGSTER_PORT": str(args.dagster_port), "PYTHONUNBUFFERED": "1"}
    (ROOT / ".dagster_home").mkdir(exist_ok=True)
    (ROOT / ".dagster_home" / "dagster.yaml").touch()
    LOGS.mkdir(parents=True, exist_ok=True)

    procs: list[tuple[str, subprocess.Popen]] = []

    def launch(name: str, argv: list[str]) -> None:
        log = (LOGS / f"{name}.log").open("w")
        procs.append((name, subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)))

    launch("shoplite", [str(BIN / "uvicorn"), "shop_app.main:app", "--port", str(args.port)])
    if not args.no_pipeline:
        launch("dagster", [str(BIN / "dagster"), "dev", "-m", "orchestration.definitions", "-p", str(args.dagster_port)])
    if args.simulate:
        launch("simulator", [sys.executable, "-u", "source_app/generator.py", "stream", "--interval", str(args.interval)])

    print("Starting… (logs in data_lake/_state/logs/)")
    deadline = time.time() + 90
    waiting = {args.port: "ShopLite"} | ({} if args.no_pipeline else {args.dagster_port: "Dagster"})
    while waiting and time.time() < deadline:
        for port in [p for p in waiting if not port_free(p)]:
            print(f"  ✓ {waiting.pop(port)} is up")
        for name, p in procs:
            if p.poll() is not None:
                print(f"  ✗ {name} exited - see data_lake/_state/logs/{name}.log")
                for _, other in procs:
                    kill_tree(other)
                sys.exit(1)
        time.sleep(1)

    print(f"""
  Storefront   http://localhost:{args.port}
  Sign in      http://localhost:{args.port}/login   (demo buttons for customer / vendor / rider / admin)
  Admin        http://localhost:{args.port}/admin   (press Start on orders, or turn on auto-approve)
  Analytics    http://localhost:{args.port}/analytics   (refreshes itself after each pipeline run)""")
    if not args.no_pipeline:
        print(f"  Dagster      http://localhost:{args.dagster_port}   (Runs = every automatic pipeline run)")
    print(f"\n  Automatic pipeline: {'OFF (--no-pipeline)' if args.no_pipeline else 'ON - new entries reach gold within ~1-2 minutes'}")
    print(f"  Simulator:          {'ON' if args.simulate else 'off (add --simulate for fake shoppers)'}")
    print("\nPress Ctrl+C to stop everything.")

    try:
        while True:
            for name, p in procs:
                if p.poll() is not None:
                    print(f"\n✗ {name} stopped unexpectedly - see data_lake/_state/logs/{name}.log. Shutting down.")
                    raise KeyboardInterrupt
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopping…")
        for name, p in reversed(procs):
            kill_tree(p)
            print(f"  stopped {name}")


if __name__ == "__main__":
    if shutil.which("taskkill") is None and os.name == "nt":
        print("warning: taskkill not found; child processes may need to be closed manually")
    main()
