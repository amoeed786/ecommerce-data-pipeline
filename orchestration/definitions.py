r"""Phase 6 - Dagster orchestration of the whole pipeline.

Asset graph (each box is something that exists in the lake/warehouse, Dagster tracks when it was built):

    shopdb (SQL Server, external)
        -> bronze            ingestion/extract.py            + check: bronze reconciles with ShopDB
        -> silver            transform/silver.py             (metadata: rows, quarantined, DQ issues)
        -> dbt models        dbt build (staging -> core star schema -> analytics marts, with tests)
        -> gold_parquet      transform/export_gold.py

Run the UI (from the project root):
    $env:DAGSTER_HOME = "$PWD\.dagster_home"; .\.venv\Scripts\dagster.exe dev -m orchestration.definitions
then open http://localhost:3000 -> Assets -> "Materialize all", or let the schedule run it.
"""
# Note: no `from __future__ import annotations` here - Dagster inspects the real type hints.
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from dagster import (
    AssetCheckResult,
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    AssetSpec,
    DefaultScheduleStatus,
    Definitions,
    MaterializeResult,
    MetadataValue,
    ScheduleDefinition,
    asset,
    asset_check,
    define_asset_job,
    multi_asset,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, DbtProject, dbt_assets

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("SHOP_ROOT", str(ROOT))  # dbt resolves the warehouse + lake paths from this

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
SILVER_TABLES = ["categories", "customers", "customers_history", "products", "products_history",
                 "inventory", "orders", "order_items", "payments"]


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


# ------------------------------------------------------------------ jobs + schedule
full_pipeline = define_asset_job(
    "full_pipeline",
    selection=AssetSelection.all() - AssetSelection.keys("shopdb"),
    description="ShopDB -> bronze -> silver -> dbt gold -> gold Parquet",
)

every_30_minutes = ScheduleDefinition(
    job=full_pipeline,
    cron_schedule="*/30 * * * *",
    default_status=DefaultScheduleStatus.RUNNING,  # runs while `dagster dev` is up; toggle it in the UI
)


@asset(group_name="source", kinds={"sqlserver"}, description="ShopDB on SQL Server - written by the shop app and simulator.")
def shopdb() -> MaterializeResult:
    """Represents the source system. Nothing to compute - it's here so the graph starts at the source."""
    return MaterializeResult(metadata={"note": "external system, not built by the pipeline"})


defs = Definitions(
    assets=[shopdb, bronze, silver, shop_dbt, gold_parquet],
    asset_checks=[bronze_reconciles_with_shopdb],
    jobs=[full_pipeline],
    schedules=[every_30_minutes],
    resources={"dbt": DbtCliResource(project_dir=dbt_project)},
)
