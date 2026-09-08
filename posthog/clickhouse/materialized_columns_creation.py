"""Create materialized columns, with no enterprise code.

The registry next door reads columns back off the cluster. This is the other half: adding
one. It is what `posthog/clickhouse/migrations/0019` and `0026` call to materialize the
group and session columns every install is supposed to have, and what the tests call to
put a column in front of a query and check the query uses it.

Nothing here is copied from the enterprise implementation. The names and the column
comment follow what a cluster that already holds these columns carries, which is the same
grammar `materialized_columns_registry` reads: `mat_` on the events table, `pmat_` on the
person table, and a short tag for any source column other than `properties`.

New columns for a running instance are chosen and filled by the slot mechanism instead --
see docs/internal/materialized-columns-without-ee.md. This module is the direct path, for
a migration that needs one specific column and for tests.
"""

import re
import dataclasses

from posthog.clickhouse.client import sync_execute
from posthog.clickhouse.materialized_column_types import (
    MATERIALIZATION_VALID_TABLES,
    ColumnName,
    TablesWithMaterializedColumns,
)
from posthog.clickhouse.materialized_columns_registry import (
    COMMENT_PREFIX,
    COMMENT_SEPARATOR,
    DEFAULT_TABLE_COLUMN,
    DiscoveredMaterializedColumn,
    get_materialized_columns,
    parse_column_comment,
)
from posthog.models.property import PropertyName, TableColumn
from posthog.settings import CLICKHOUSE_CLUSTER, CLICKHOUSE_DATABASE

# The tag that goes into a column's name for each source column. `properties` gets none,
# because a column with no tag has always meant that one.
TABLE_COLUMN_TAG: dict[TableColumn, str] = {
    "person_properties": "pp",
    "group0_properties": "gp0",
    "group1_properties": "gp1",
    "group2_properties": "gp2",
    "group3_properties": "gp3",
    "group4_properties": "gp4",
}

# ClickHouse column names take letters, digits and underscores. Property names take
# anything, except that a leading `$` is common enough to be worth keeping legible.
_UNSAFE_IN_COLUMN_NAME = re.compile(r"[^a-zA-Z0-9_$]")

# The columns every install materializes from a ClickHouse migration rather than on demand.
EVENTS_TABLE_DEFAULT_MATERIALIZED_COLUMNS = [
    "$group_0",
    "$group_1",
    "$group_2",
    "$group_3",
    "$group_4",
    "$session_id",
    "$window_id",
]


@dataclasses.dataclass(frozen=True)
class MaterializedColumnDetails:
    """What a column's comment records: which source column, which property, still on?"""

    table_column: TableColumn
    property_name: PropertyName
    is_disabled: bool = False

    def as_column_comment(self) -> str:
        parts = [COMMENT_PREFIX, self.table_column, self.property_name]
        if self.is_disabled:
            parts.append("disabled")
        return COMMENT_SEPARATOR.join(parts)


@dataclasses.dataclass(frozen=True)
class MaterializedColumn:
    """A column described by hand rather than read off the cluster.

    The registry returns its own type for columns it discovers. This one is for a caller
    that already knows what the column is -- a test standing one up, or code building the
    comment for a column it is about to add.
    """

    name: ColumnName
    details: MaterializedColumnDetails
    is_nullable: bool = False
    has_minmax_index: bool = False
    has_bloom_filter_index: bool = False
    has_ngram_lower_index: bool = False
    has_bloom_filter_lower_index: bool = False
    column_type: str = "String"

    @property
    def type(self) -> str:
        return self.column_type

    @property
    def property_name(self) -> PropertyName:
        return self.details.property_name

    @property
    def table_column(self) -> TableColumn:
        return self.details.table_column

    @property
    def is_disabled(self) -> bool:
        return self.details.is_disabled


def get_minmax_index_name(column: ColumnName) -> str:
    return f"minmax_{column}"


def get_bloom_filter_index_name(column: ColumnName) -> str:
    return f"bloom_filter_{column}"


def get_ngram_lower_index_name(column: ColumnName) -> str:
    return f"ngram_lower_{column}"


def get_bloom_filter_lower_index_name(column: ColumnName) -> str:
    return f"bloom_filter_lower_{column}"


def _clear_materialized_columns_cache(table: TablesWithMaterializedColumns | None = None) -> None:
    """Forget what the registry last read. Any DDL here calls it already."""
    get_materialized_columns.clear_cache()


def _data_table(table: TablesWithMaterializedColumns) -> str:
    """Where the column physically lives. Events are sharded; the rest are not."""
    return "sharded_events" if table == "events" else table


def _on_cluster() -> str:
    return f"ON CLUSTER '{CLICKHOUSE_CLUSTER}'"


def _column_name(
    table: TablesWithMaterializedColumns, table_column: TableColumn, property_name: PropertyName
) -> ColumnName:
    prefix = "pmat_" if table == "person" else "mat_"
    tag = TABLE_COLUMN_TAG.get(table_column)
    if tag:
        prefix += f"{tag}_"
    return prefix + _UNSAFE_IN_COLUMN_NAME.sub("_", property_name)


def _extract_expression(table_column: TableColumn, property_name: PropertyName, is_nullable: bool) -> str:
    escaped = property_name.replace("\\", "\\\\").replace("'", "\\'")
    if is_nullable:
        return f"JSONExtract({table_column}, '{escaped}', 'Nullable(String)')"
    return f"JSONExtractString({table_column}, '{escaped}')"


def materialize(
    table: TablesWithMaterializedColumns,
    property: PropertyName,
    column_name: ColumnName | None = None,
    table_column: TableColumn = DEFAULT_TABLE_COLUMN,
    create_minmax_index: bool = False,
    is_nullable: bool = False,
    column_type: str | None = None,
    create_bloom_filter_index: bool = False,
    create_ngram_lower_index: bool = False,
    create_bloom_filter_lower_index: bool = False,
) -> DiscoveredMaterializedColumn:
    """Add a materialized column for one property, and return it as the registry sees it.

    Returns the existing column unchanged if the property already has one of the same
    shape, and raises if one exists with a different nullability.
    """
    if table not in MATERIALIZATION_VALID_TABLES:
        raise ValueError(f"Cannot materialize property for table {table}")

    already = get_materialized_columns(table).get((property, table_column))
    if already is not None:
        if already.is_nullable != is_nullable:
            raise ValueError(
                f"Property {property} is already materialized on {table}.{table_column} with "
                f"is_nullable={already.is_nullable}, not {is_nullable}"
            )
        # Same property, same shape: hand back the column that is already there rather than
        # making a second one. Callers that run more than once -- a migration, a test that
        # does not own the whole database -- then do not have to care about the order.
        return already

    column = column_name or _column_name(table, table_column, property)
    existing_property = _property_of_existing_column(_data_table(table), column)
    if existing_property is not None and existing_property != property:
        raise ValueError(f"Column {column} on {table} already materializes {existing_property!r}, not {property!r}")

    details = MaterializedColumnDetails(table_column=table_column, property_name=property, is_disabled=False)
    expression = _extract_expression(table_column, property, is_nullable)
    declared_type = column_type or ("Nullable(String)" if is_nullable else "String")

    for target in _targets(table):
        sync_execute(
            f"ALTER TABLE {target} {_on_cluster()} "
            f"ADD COLUMN IF NOT EXISTS {column} {declared_type} MATERIALIZED {expression}"
        )
        sync_execute(
            f"ALTER TABLE {target} {_on_cluster()} COMMENT COLUMN {column} %(comment)s",
            {"comment": details.as_column_comment()},
        )

    data_table = _data_table(table)
    # A lower() index has to be built on the same expression the query uses, and the query
    # uses lower(<column>). ClickHouse will not put an ngram or token bloom filter on a
    # nullable expression, and an index over lower(assumeNotNull(<column>)) is legal but
    # never matches, so a nullable column gets no lower() index at all. The returned flags
    # say so, which is what keeps the planner from rewriting for an index that is not there.
    # A lower() index only fires when it carries the same expression the query does, and a
    # nullable column is read as coalesce(<column>, ''). That also happens to be what makes
    # an ngram index possible at all here, since ClickHouse will not build one over a
    # nullable expression but coalesce hands it a String.
    lowered = f"lower(coalesce({column}, ''))" if is_nullable else f"lower({column})"

    if create_minmax_index:
        _add_index(data_table, get_minmax_index_name(column), column, "minmax")
    if create_bloom_filter_index:
        _add_index(data_table, get_bloom_filter_index_name(column), column, "bloom_filter")
    if create_ngram_lower_index:
        _add_index(data_table, get_ngram_lower_index_name(column), lowered, "ngrambf_v1(4, 1024, 3, 0)")
    if create_bloom_filter_lower_index:
        _add_index(data_table, get_bloom_filter_lower_index_name(column), lowered, "bloom_filter")

    _clear_materialized_columns_cache()

    return DiscoveredMaterializedColumn(
        name=column,
        property_name=property,
        table_column=table_column,
        is_disabled=False,
        is_nullable=is_nullable,
        has_minmax_index=create_minmax_index,
        has_bloom_filter_index=create_bloom_filter_index,
        has_ngram_lower_index=create_ngram_lower_index,
        has_bloom_filter_lower_index=create_bloom_filter_lower_index,
        clickhouse_type=declared_type,
    )


def drop_column(table: TablesWithMaterializedColumns, column_name: ColumnName) -> None:
    for target in _targets(table):
        sync_execute(f"ALTER TABLE {target} {_on_cluster()} DROP COLUMN IF EXISTS {column_name}")
    _clear_materialized_columns_cache()


def _targets(table: TablesWithMaterializedColumns) -> list[str]:
    """The tables the column has to be added to: the data table, and the one queries read."""
    data_table = _data_table(table)
    return [data_table] if data_table == table else [data_table, table]


def _add_index(data_table: str, index_name: str, expression: str, index_type: str) -> None:
    sync_execute(
        f"ALTER TABLE {data_table} {_on_cluster()} "
        f"ADD INDEX IF NOT EXISTS {index_name} {expression} TYPE {index_type} GRANULARITY 1"
    )


def _property_of_existing_column(table: str, column: ColumnName) -> PropertyName | None:
    """Which property a column of this name already materializes, or None if there is none.

    The name has to stay a pure function of the property: it lands in generated SQL, so a
    disambiguating suffix would make every query that reads the column unrepeatable.
    """
    rows = sync_execute(
        "SELECT comment FROM system.columns WHERE database = %(database)s AND table = %(table)s AND name = %(column)s",
        {"database": CLICKHOUSE_DATABASE, "table": table, "column": column},
    )
    if not rows:
        return None
    comment = rows[0][0]
    if not comment.startswith(COMMENT_PREFIX):
        return None
    return parse_column_comment(comment)[1]
