from typing import Literal

import dagster


class MaterializeColumnConfig(dagster.Config):
    table: Literal["events", "person"] = "events"
    table_column: Literal["properties", "group_properties", "person_properties"] = "properties"
    properties: list[str]
    backfill_period_days: int = 90
    dry_run: bool = False
    is_nullable: bool = True


@dagster.op
def create_materialized_columns_op(
    context: dagster.OpExecutionContext,
    config: MaterializeColumnConfig,
):
    raise RuntimeError(
        "Materialized columns are managed by the enterprise code this build does not "
        "contain, so this job cannot run here."
    )
