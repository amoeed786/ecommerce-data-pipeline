r"""Phase 6 - Dagster orchestration of the whole pipeline.

Asset graph (each box is something that exists in the lake/warehouse, Dagster tracks when it was built):

    shopdb (SQL Server, external)
        -> bronze            ingestion/extract.py            + check: bronze reconciles with ShopDB
        -> silver            transform/silver.py             (metadata: rows, quarantined, DQ issues)
        -> dbt models        dbt build (staging -> core star schema -> analytics marts, with tests)
        -> gold_parquet      transform/export_gold.py

AUTOMATIC: the `shopdb_changes` sensor checks ShopDB every 30 seconds. When there are rows the lake
hasn't seen, it starts `full_pipeline` (one run at a time, with a short cooldown) - so every new order,
status change or price change flows to gold by itself. Each run is logged to data_lake/_state/pipeline_runs.jsonl.

Easiest way to run everything (shop + autopilot + Dagster):   .\.venv\Scripts\python.exe start.py
Dagster only (from the project root):
    $env:DAGSTER_HOME = "$PWD\.dagster_home"; .\.venv\Scripts\dagster.exe dev -m orchestration.definitions
then open http://localhost:3000 (Automation tab shows the sensor; Runs shows each automatic run).
"""
# Note: no `from __future__ import annotations` here - Dagster inspects the real type hints.
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dagster import (
    AssetCheckResult,
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    AssetSpec,
    DagsterRunStatus,
    DefaultSensorStatus,
    Definitions,
    MaterializeResult,
    MetadataValue,
    RetryPolicy,
    RunRequest,
    RunsFilter,
    RunStatusSensorContext,
    SensorEvaluationContext,
    SkipReason,
    asset,
    asset_check,
    define_asset_job,
    multi_asset,
    run_failure_sensor,
    run_status_sensor,
    sensor,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, DbtProject, dbt_assets

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SHOP_ROOT", str(ROOT))  # dbt resolves the warehouse + lake paths from this
sys.path.insert(0, str(ROOT))
from ingestion.extract import STATE_DIR, load_watermarks, pending_changes  # noqa: E402
from source_app.db import get_connection  # noqa: E402

PIPELINE_RUNS_FILE = STATE_DIR / "pipeline_runs.jsonl"
COOLDOWN_SECONDS = int(os.getenv("PIPELINE_COOLDOWN_SECONDS", "30"))  # min gap between automatic runs

dbt_project = DbtProject(project_dir=ROOT / "dbt_shop", profiles_dir=ROOT / "dbt_shop")
dbt_project.prepare_if_dev()  # in `dagster dev`, (re)builds the dbt manifest on startup


def run_script(context: AssetExecutionContext, *args: str) -> str:
    """Run one of the pipeline scripts exactly like on the command line; fail the asset if it fails."""
    context.log.info(f"$ python {' '.join(args)}")
    out = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True)
    output = (out.stdout + out.stderr).strip()
    context.log.info(output)
    if out.returncode != 0:
        raise RuntimeError(f"{args[0]} failed (exit {out.returncode}):\n{output}")
    return output


# ------------------------------------------------------------------ ingestion + silver
@asset(group_name="ingestion", kinds={"python", "parquet"}, deps=[AssetKey("shopdb")],
       description="Incremental extract ShopDB -> data_lake/bronze (append-only, watermark based).")
def bronze(context: AssetExecutionContext) -> MaterializeResult:
    output = run_script(context, "ingestion/extract.py")
    rows = re.search(r"Done: ([\d,]+) rows", output)
    return MaterializeResult(metadata={
        "rows_extracted": int(rows.group(1).replace(",", "")) if rows else 0,
        "log": MetadataValue.md(f"```\n{output}\n```"),
    })


@asset_check(asset=bronze, description="Every source row changed before the watermark is in bronze, latest version.")
def bronze_reconciles_with_shopdb() -> AssetCheckResult:
    out = subprocess.run([sys.executable, "ingestion/check_bronze.py"], cwd=ROOT, capture_output=True, text=True)
    return AssetCheckResult(passed=out.returncode == 0, metadata={"report": MetadataValue.md(f"```\n{out.stdout}\n```")})


# One asset per silver file. Keys match dbt's default source keys (silver/<table>), so Dagster
# wires each dbt staging model to the exact silver table it reads.
SILVER_TABLES = ["categories", "customers", "customers_history", "vendors", "riders", "products",
                 "products_history", "inventory", "orders", "shipments", "order_items", "payments", "users"]


@multi_asset(
    specs=[AssetSpec(["silver", t], deps=[bronze], group_name="silver", kinds={"duckdb", "parquet"},
                     description=f"data_lake/silver/{t}.parquet") for t in SILVER_TABLES],
    name="silver",
    description="Dedup + clean + data-quality checks -> data_lake/silver (+ quarantine). One script builds all tables.",
)
def silver(context: AssetExecutionContext):
    output = run_script(context, "transform/silver.py")
    dq_file = ROOT / "data_lake" / "_state" / "dq_log.jsonl"
    lines = [json.loads(x) for x in dq_file.read_text().splitlines() if x.strip()]
    last = {r["table"]: r for r in lines if r["run_at"] == lines[-1]["run_at"]}
    for t in SILVER_TABLES:
        base = t.removesuffix("_history")
        r = last.get(base, {})
        meta = ({"history_versions": r.get("history_rows") or 0} if t.endswith("_history") else {
            "silver_rows": r.get("silver_rows", 0),
            "quarantined_rows": r.get("quarantined", 0),
            "dq_errors": MetadataValue.json(r.get("errors", {})),
            "dq_warnings": MetadataValue.json(r.get("warnings", {})),
        })
        if t == "categories":
            meta["log"] = MetadataValue.md(f"```\n{output}\n```")
        yield MaterializeResult(asset_key=AssetKey(["silver", t]), metadata=meta)


# ------------------------------------------------------------------ dbt (gold)
class ShopTranslator(DagsterDbtTranslator):
    """Group dbt models by layer in the Dagster UI (sources keep dbt's default keys: silver/<table>)."""

    def get_group_name(self, dbt_resource_props):
        fqn = dbt_resource_props.get("fqn", [])
        return f"gold_{fqn[2]}" if len(fqn) > 3 and fqn[1] == "marts" else f"dbt_{fqn[1]}" if len(fqn) > 2 else "dbt"


@dbt_assets(manifest=dbt_project.manifest_path, project=dbt_project, dagster_dbt_translator=ShopTranslator())
def shop_dbt(context: AssetExecutionContext, dbt: DbtCliResource):
    """dbt build: every model is an asset, every dbt test becomes an asset check."""
    yield from dbt.cli(["build"], context=context).stream()


@asset(group_name="gold_export", kinds={"duckdb", "parquet"}, deps=[shop_dbt],
       description="Copy the star schema + marts to data_lake/gold/*.parquet for apps and BI tools.")
def gold_parquet(context: AssetExecutionContext) -> MaterializeResult:
    output = run_script(context, "transform/export_gold.py")
    return MaterializeResult(metadata={"log": MetadataValue.md(f"```\n{output}\n```")})


# ------------------------------------------------------------------ job + automation
full_pipeline = define_asset_job(
    "full_pipeline",
    selection=AssetSelection.all() - AssetSelection.keys("shopdb"),
    description="ShopDB -> bronze -> silver -> dbt gold -> gold Parquet",
    op_retry_policy=RetryPolicy(max_retries=2, delay=10),  # ride out a momentary lock instead of failing the run
)
ACTIVE = [DagsterRunStatus.QUEUED, DagsterRunStatus.NOT_STARTED, DagsterRunStatus.STARTING, DagsterRunStatus.STARTED]


@sensor(job=full_pipeline, minimum_interval_seconds=30, default_status=DefaultSensorStatus.RUNNING,
        description="Starts the full pipeline whenever ShopDB has changes the lake hasn't extracted yet.")
def shopdb_changes(context: SensorEvaluationContext):
    """Event-driven micro-batching: new entries in ShopDB -> one pipeline run takes them all to gold.

    - skips while a run is in progress (changes made meanwhile are picked up by the next run)
    - waits COOLDOWN_SECONDS after the last run, so a busy shop doesn't run the pipeline back-to-back
    - run_key = newest change + current watermarks, so the same changes never trigger twice,
      but changes still pending after a run (made during it) do trigger the next one
    """
    instance = context.instance
    if instance.get_run_records(RunsFilter(job_name="full_pipeline", statuses=ACTIVE), limit=1):
        return SkipReason("A pipeline run is already in progress")
    last = instance.get_run_records(RunsFilter(job_name="full_pipeline",
                                               statuses=[DagsterRunStatus.SUCCESS, DagsterRunStatus.FAILURE]), limit=1)
    if last and last[0].end_time and time.time() - last[0].end_time < COOLDOWN_SECONDS:
        return SkipReason(f"Cooling down ({COOLDOWN_SECONDS}s between runs)")

    conn = get_connection()
    try:
        pending = pending_changes(conn)
    finally:
        conn.close()
    if not pending:
        return SkipReason("The lake is up to date with ShopDB")
    newest = max(pending.values())
    marks = load_watermarks()
    return RunRequest(
        run_key=f"{newest.isoformat()}|{min(marks.values(), default='none')}",
        tags={"trigger": "shopdb_changes", "changed_tables": ",".join(sorted(pending)),
              "newest_change": newest.isoformat()},
    )


def _log_run(context: RunStatusSensorContext, status: str) -> None:
    """Append one line per finished run, so the shop app can show the automatic pipeline's history."""
    run = context.dagster_run
    stats = context.instance.get_run_stats(run.run_id)
    started = datetime.fromtimestamp(stats.start_time) if stats.start_time else None
    ended = datetime.fromtimestamp(stats.end_time) if stats.end_time else datetime.now()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PIPELINE_RUNS_FILE.open("a") as f:
        f.write(json.dumps({
            "run_id": run.run_id, "status": status,
            "trigger": run.tags.get("trigger", "manual"),
            "changed_tables": run.tags.get("changed_tables", ""),
            "started_at": started.isoformat(timespec="seconds") if started else None,
            "finished_at": ended.isoformat(timespec="seconds"),
            "seconds": round((ended - started).total_seconds(), 1) if started else None,
        }) + "\n")


@run_status_sensor(run_status=DagsterRunStatus.SUCCESS, monitored_jobs=[full_pipeline],
                   default_status=DefaultSensorStatus.RUNNING, description="Logs successful pipeline runs.")
def log_pipeline_success(context: RunStatusSensorContext):
    _log_run(context, "success")


@run_failure_sensor(monitored_jobs=[full_pipeline], default_status=DefaultSensorStatus.RUNNING,
                    description="Logs failed pipeline runs.")
def log_pipeline_failure(context: RunStatusSensorContext):
    _log_run(context, "failure")


@asset(group_name="source", kinds={"sqlserver"}, description="ShopDB on SQL Server - written by the shop app and simulator.")
def shopdb() -> MaterializeResult:
    """Represents the source system. Nothing to compute - it's here so the graph starts at the source."""
    return MaterializeResult(metadata={"note": "external system, not built by the pipeline"})


defs = Definitions(
    assets=[shopdb, bronze, silver, shop_dbt, gold_parquet],
    asset_checks=[bronze_reconciles_with_shopdb],
    jobs=[full_pipeline],
    sensors=[shopdb_changes, log_pipeline_success, log_pipeline_failure],
    resources={"dbt": DbtCliResource(project_dir=dbt_project)},
)
