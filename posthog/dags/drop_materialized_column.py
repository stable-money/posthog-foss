from typing import Literal

import dagster


class DropMaterializedColumnConfig(dagster.Config):
    table: Literal["events", "person"] = "events"
    column_names: list[str]
    dry_run: bool = True


@dagster.op
def drop_materialized_columns_op(
    context: dagster.OpExecutionContext,
    config: DropMaterializedColumnConfig,
):
    raise RuntimeError(
        "Materialized columns are managed by the enterprise code this build does not "
        "contain, so this job cannot run here."
    )
