"""Discover materialized columns by reading ClickHouse, with no enterprise code.

The HogQL engine substitutes a physical column for a JSON property lookup whenever one
exists. Finding those columns was the only part of that path implemented in `ee/`, so a
build without it silently stops substituting: every property filter falls back to
extracting from the JSON blob, nothing errors, and queries just get slower.

This module restores the lookup. It only reads: `system.columns` for the columns and their
comments, and `system.data_skipping_indices` for the index flags. It creates nothing.
Creating new materialized columns is a separate concern -- see
docs/internal/materialized-columns-without-ee.md.

The comment grammar below is a property of a ClickHouse cluster that already holds
materialized columns, not of any particular implementation. Index detection deliberately
keys off each index's expression rather than its name, because names follow more than one
convention on a real cluster.
"""

import re
import dataclasses
from collections.abc import Mapping
from datetime import timedelta

from posthog.cache_utils import cache_for
from posthog.clickhouse.materialized_column_types import (
    MATERIALIZATION_VALID_TABLES,
    ColumnName,
    TablesWithMaterializedColumns,
)
from posthog.models.property import PropertyName, TableColumn

COMMENT_PREFIX = "column_materializer"
COMMENT_SEPARATOR = "::"
COMMENT_DISABLED_MARKER = "disabled"

# A comment with no table-column segment predates the segment being recorded, and always
# referred to the events table's `properties` column.
DEFAULT_TABLE_COLUMN: TableColumn = "properties"

# elements_chain materializations share the comment prefix but are not property lookups,
# so they never take part in property substitution.
EXCLUDED_TABLE_COLUMN = "elements_chain"

_INDEX_FLAGS = (
    "has_minmax_index",
    "has_bloom_filter_index",
    "has_ngram_lower_index",
    "has_bloom_filter_lower_index",
)

# Index names are not a reliable key: a cluster carries both "minmax_mat_$current_url"
# (named for the column) and "bloom_filter_$ai_trace_id" (named for the property, whose
# column is mat_$ai_trace_id). The index expression names the column directly, so match on
# that instead.
_LOWER_CALL = re.compile(r"^lower\((.+)\)$", re.IGNORECASE)

# A nullable column is read as coalesce(<column>, ''), so a lower() index over one carries
# that wrapper and the column name has to be recovered from inside it.
_COALESCE_CALL = re.compile(r"^coalesce\((.+?),\s*''\)$", re.IGNORECASE)

# ClickHouse's own type names for the n-gram/token bloom variants.
_NGRAM_INDEX_TYPES = frozenset({"ngrambf_v1", "tokenbf_v1"})


@dataclasses.dataclass(frozen=True)
class DiscoveredMaterializedColumn:
    """A materialized column found on the cluster.

    Satisfies the MaterializedColumn Protocol in posthog.clickhouse.materialized_column_types.
    """

    name: ColumnName
    property_name: PropertyName
    table_column: TableColumn
    is_disabled: bool
    is_nullable: bool
    has_minmax_index: bool
    has_bloom_filter_index: bool
    has_ngram_lower_index: bool
    has_bloom_filter_lower_index: bool
    clickhouse_type: str

    @property
    def type(self) -> str:
        return self.clickhouse_type


def parse_column_comment(comment: str) -> tuple[TableColumn, PropertyName, bool]:
    """Read (table column, property name, is_disabled) out of a materializer comment.

    Three shapes exist on a real cluster, and all three have to be handled:

        column_materializer::<property>                            table column defaults
        column_materializer::<table_column>::<property>
        column_materializer::<table_column>::<property>::disabled

    Raises ValueError for anything else, so an unrecognised comment is noticed rather than
    quietly dropping a column out of the registry.
    """
    parts = comment.split(COMMENT_SEPARATOR, 3)
    # Deliberately not a match statement: a bare name in a case pattern captures rather
    # than compares, so `case [COMMENT_PREFIX, prop]` would match any two-part comment and
    # rebind the prefix instead of checking it.
    if not parts or parts[0] != COMMENT_PREFIX:
        raise ValueError(f"unexpected materialized column comment: {comment!r}")
    if len(parts) == 2:
        return DEFAULT_TABLE_COLUMN, parts[1], False
    if len(parts) == 3:
        return parts[1], parts[2], False
    if len(parts) == 4 and parts[3] == COMMENT_DISABLED_MARKER:
        return parts[1], parts[2], True
    raise ValueError(f"unexpected materialized column comment: {comment!r}")


def _index_flags(database: str, tables: tuple[str, ...]) -> dict[str, dict[str, bool]]:
    """Which skipping indices exist, keyed by the column each one is built on.

    Indices live on the data table (`sharded_events`) while the comments live on the
    distributed one (`events`), so both are read and the results pooled. Expressions that
    are not a plain column, lower(column) or lower(coalesce(column, '')) -- mapKeys(...)
    over a properties group, say -- belong to no materialized column and are ignored.
    """
    from posthog.clickhouse.client import sync_execute

    rows = sync_execute(
        """
        SELECT type, expr FROM system.data_skipping_indices
        WHERE database = %(database)s AND table IN %(tables)s
        """,
        {"database": database, "tables": tables},
    )

    flags: dict[str, dict[str, bool]] = {}
    for index_type, expr in rows:
        target = expr.strip().strip("`")
        lowered = _LOWER_CALL.match(target)
        column = (lowered.group(1).strip().strip("`") if lowered else target).strip()
        unwrapped = _COALESCE_CALL.match(column)
        if unwrapped:
            column = unwrapped.group(1).strip().strip("`")
        if not column or "(" in column:
            continue

        entry = flags.setdefault(column, dict.fromkeys(_INDEX_FLAGS, False))
        if index_type == "minmax" and not lowered:
            entry["has_minmax_index"] = True
        elif index_type == "bloom_filter":
            entry["has_bloom_filter_lower_index" if lowered else "has_bloom_filter_index"] = True
        elif index_type in _NGRAM_INDEX_TYPES and lowered:
            entry["has_ngram_lower_index"] = True
    return flags


@cache_for(timedelta(minutes=15), background_refresh=True)
def get_materialized_columns(
    table: TablesWithMaterializedColumns,
) -> dict[tuple[PropertyName, TableColumn], DiscoveredMaterializedColumn]:
    """Every materialized column on `table`, including disabled ones.

    Disabled columns are returned so callers can report them; use
    get_enabled_materialized_columns for the set a query may actually substitute.

    Cached, because this sits behind the HogQL compile path and must not issue a query per
    property lookup.
    """
    from django.conf import settings

    from posthog.clickhouse.client import sync_execute

    database = settings.CLICKHOUSE_DATABASE
    rows = sync_execute(
        """
        SELECT name, comment, type
        FROM system.columns
        WHERE database = %(database)s AND table = %(table)s AND comment LIKE %(prefix)s
        """,
        {"database": database, "table": table, "prefix": f"{COMMENT_PREFIX}{COMMENT_SEPARATOR}%"},
    )
    if not rows:
        return {}

    index_flags = _index_flags(database, (table, f"sharded_{table}"))

    columns: dict[tuple[PropertyName, TableColumn], DiscoveredMaterializedColumn] = {}
    for name, comment, column_type in rows:
        table_column, property_name, is_disabled = parse_column_comment(comment)
        if table_column == EXCLUDED_TABLE_COLUMN:
            continue
        flags = index_flags.get(name, dict.fromkeys(_INDEX_FLAGS, False))
        columns[(property_name, table_column)] = DiscoveredMaterializedColumn(
            name=name,
            property_name=property_name,
            table_column=table_column,
            is_disabled=is_disabled,
            is_nullable=column_type.startswith("Nullable("),
            clickhouse_type=column_type,
            **flags,
        )
    return columns


def get_enabled_materialized_columns(
    table: TablesWithMaterializedColumns,
) -> Mapping[tuple[PropertyName, TableColumn], DiscoveredMaterializedColumn]:
    """Materialized columns on `table` that queries may substitute.

    Disabled columns are excluded: they still exist physically but must not be read.
    """
    return {key: column for key, column in get_materialized_columns(table).items() if not column.is_disabled}


def get_enabled_materialized_columns_by_table() -> Mapping[
    TablesWithMaterializedColumns, Mapping[tuple[PropertyName, TableColumn], DiscoveredMaterializedColumn]
]:
    return {table: get_enabled_materialized_columns(table) for table in MATERIALIZATION_VALID_TABLES}
