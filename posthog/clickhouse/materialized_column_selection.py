"""Pick which event properties are worth materializing, from what queries actually did.

The enterprise materializer chose properties itself each week and created the columns
directly. The slot mechanism that replaces it only acts on slots someone has already
requested, so without a policy this build never materializes anything new: existing columns
keep working while the instance stops adapting to its own query patterns.

This module supplies the missing half. It reads `system.query_log`, finds event properties
that are still being pulled out of the JSON blob in queries that are slow or failing, and
queues them as PENDING slots. The weekly backfill workflow does the rest -- allocating a
column index, running the mutation and activating the slot.

It only ever creates PENDING rows. No DDL, no mutation, nothing that touches a column.
"""

import re
import dataclasses

import structlog

from posthog.settings.utils import get_from_env

logger = structlog.get_logger(__name__)

# How far back to look. A week smooths over weekly reporting patterns without letting a
# one-off investigation from a month ago justify a permanent column.
SELECTION_ANALYSIS_PERIOD_HOURS = get_from_env("MATERIALIZED_COLUMN_SELECTION_PERIOD_HOURS", 7 * 24, type_cast=int)

# A query slower than this counts against the property it read.
SELECTION_SLOW_QUERY_MS = get_from_env("MATERIALIZED_COLUMN_SELECTION_SLOW_QUERY_MS", 10_000, type_cast=int)

# How many slow queries before a property is worth a column. More than a handful, so a
# single bad afternoon does not spend a slot.
SELECTION_MIN_SLOW_QUERIES = get_from_env("MATERIALIZED_COLUMN_SELECTION_MIN_SLOW_QUERIES", 10, type_cast=int)

# Ceiling per run, per team. Each new slot costs a backfill mutation over the events table,
# so a run that queues dozens at once turns into a very long Sunday.
SELECTION_MAX_PER_TEAM_PER_RUN = get_from_env("MATERIALIZED_COLUMN_SELECTION_MAX_PER_RUN", 5, type_cast=int)

# Only the events table's own properties column can take a slot; person and group
# properties are materialized by other means.
SUPPORTED_TABLE_COLUMN = "properties"

# Generated SQL reads an unmaterialized property as `<column>properties, '<name>'`. Once a
# column exists the planner rewrites the access, so a property that stops appearing here is
# exactly one that no longer needs a slot.
_FRAGMENT = re.compile(r"^(?P<column>[a-z0-9_]*properties), '(?P<property>.+)'$", re.DOTALL)


@dataclasses.dataclass(frozen=True)
class SlotCandidate:
    team_id: int
    property_name: str
    query_count: int
    slow_query_count: int
    failed_query_count: int

    @property
    def score(self) -> tuple[int, int]:
        """Rank by damage done: failures first, then slow queries."""
        return (self.failed_query_count, self.slow_query_count)


def _parse_fragment(fragment: str) -> tuple[str, str] | None:
    match = _FRAGMENT.match(fragment.strip())
    if not match:
        return None
    return match.group("column"), match.group("property")


def find_slot_candidates(
    *,
    period_hours: int = SELECTION_ANALYSIS_PERIOD_HOURS,
    slow_query_ms: int = SELECTION_SLOW_QUERY_MS,
    min_slow_queries: int = SELECTION_MIN_SLOW_QUERIES,
) -> list[SlotCandidate]:
    """Event properties whose JSON reads are hurting, worst first.

    A property qualifies on either signal: queries reading it failed outright, or enough of
    them were slow. Failures count for more than slowness, since a query that dies helps
    nobody.
    """
    from posthog.clickhouse.client import sync_execute

    rows = sync_execute(
        """
        SELECT JSONExtractInt(log_comment, 'team_id') AS team_id,
               arrayJoin(extractAll(query, '[a-z0-9_]*properties, ''[^'']{1,200}''')) AS fragment,
               count() AS query_count,
               countIf(query_duration_ms > %(slow_query_ms)s) AS slow_query_count,
               countIf(exception_code != 0) AS failed_query_count
        FROM system.query_log
        WHERE event_time > now() - toIntervalHour(%(period_hours)s)
          AND JSONExtractInt(log_comment, 'team_id') > 0
          AND query ILIKE '%%JSONExtract%%'
        GROUP BY team_id, fragment
        HAVING slow_query_count >= %(min_slow_queries)s OR failed_query_count > 0
        """,
        {
            "period_hours": period_hours,
            "slow_query_ms": slow_query_ms,
            "min_slow_queries": min_slow_queries,
        },
    )

    candidates: list[SlotCandidate] = []
    for team_id, fragment, query_count, slow_query_count, failed_query_count in rows:
        parsed = _parse_fragment(fragment)
        if parsed is None:
            continue
        table_column, property_name = parsed
        # person_properties and groupN_properties reach the same fragment shape but cannot
        # take a slot.
        if table_column != SUPPORTED_TABLE_COLUMN:
            continue
        candidates.append(
            SlotCandidate(
                team_id=team_id,
                property_name=property_name,
                query_count=query_count,
                slow_query_count=slow_query_count,
                failed_query_count=failed_query_count,
            )
        )

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates


def propose_slots(*, dry_run: bool = False, max_per_team: int = SELECTION_MAX_PER_TEAM_PER_RUN) -> list[SlotCandidate]:
    """Queue PENDING slots for the worst candidates. Returns the ones queued.

    Skips anything already materialized by either mechanism, anything already slotted, and
    stops at the per-team ceiling. Creating a slot is the whole action -- the weekly
    workflow allocates the column and backfills it.
    """
    from posthog.api.materialized_column_slot import get_auto_materialized_property_names
    from posthog.models import MaterializedColumnSlot, PropertyDefinition
    from posthog.models.materialized_column_slots import MAX_SLOTS_PER_TEAM

    already_materialized = get_auto_materialized_property_names()
    queued: list[SlotCandidate] = []
    per_team: dict[int, int] = {}

    for candidate in find_slot_candidates():
        if candidate.property_name in already_materialized:
            continue
        if per_team.get(candidate.team_id, 0) >= max_per_team:
            continue

        property_definition = PropertyDefinition.objects.filter(
            team_id=candidate.team_id,
            name=candidate.property_name,
            type=PropertyDefinition.Type.EVENT,
        ).first()
        if property_definition is None:
            # A property the taxonomy has never seen cannot be slotted; it is also unlikely
            # to be worth a column.
            continue

        existing = MaterializedColumnSlot.objects.filter(team_id=candidate.team_id)
        if existing.filter(property_definition=property_definition).exists():
            continue
        if existing.count() >= MAX_SLOTS_PER_TEAM:
            logger.info(
                "materialized_column_selection.team_at_slot_capacity",
                team_id=candidate.team_id,
                property_name=candidate.property_name,
            )
            continue

        if not dry_run:
            MaterializedColumnSlot.objects.create(
                team_id=candidate.team_id,
                property_definition=property_definition,
            )
        per_team[candidate.team_id] = per_team.get(candidate.team_id, 0) + 1
        queued.append(candidate)

    logger.info(
        "materialized_column_selection.finished",
        queued=len(queued),
        dry_run=dry_run,
    )
    return queued
