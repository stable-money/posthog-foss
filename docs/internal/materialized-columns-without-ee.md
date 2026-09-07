# Materialized columns without `ee/` — implementation spec

Status: proposal. Written against posthog-foss at `12ce1127`, measured against the
Stable Money production instance on 2026-09-07.

## Why this exists

Removing `ee/` removes the only implementation of materialized columns. Everything else
this fork drops is licence-gated and unused; this one is neither. It is doing real work,
it needs no PostHog Enterprise licence to run, and losing it is a pure performance
regression with no compliance benefit.

Measured on production:

| | |
|---|---|
| materialized columns on `events` | 106 with a materializer comment, 88 named `mat_*` |
| property lookups HogQL substitutes per query | 95 |
| `dmat_string_*` (the newer slot mechanism) | 0 — unused here |
| weekly creation job | armed: `0 5 * * SAT`, both instance settings `True` |

Without `ee`, `get_materialized_column_for_property()` returns `None` for every property.
The 88 columns stay on disk, are never read, and every property filter falls back to JSON
extraction out of the `properties` blob. Nothing errors. Queries just get slower.

## What is already on the MIT side

Most of the machinery has already been moved out of `ee/` upstream. Do not rebuild it.

| Component | Location | Lines |
|---|---|---|
| `MaterializedColumn` Protocol, name prefixes, valid tables | `posthog/clickhouse/materialized_column_types.py` | 30 |
| Gated facade with a working no-op branch | `posthog/clickhouse/materialized_columns.py` | 62 |
| Slot model and states | `posthog/models/materialized_column_slots.py` | 100 |
| Backfill Temporal activities | `posthog/temporal/backfill_materialized_property/activities.py` | 533 |
| Backfill Dagster job | `posthog/dags/backfill_materialized_column.py` | 201 |
| HogQL substitution, planning, property metadata | `posthog/hogql/property_planner.py`, `property_metadata.py`, `transforms/` | — |

What is missing is narrower than it looks: **the read-side registry**, and creation.

## The contract

`posthog/clickhouse/materialized_columns.py` already defines both sides. The `ee` branch
supplies an implementation; the `else` branch returns nothing. An implementation has to
satisfy exactly two functions:

```python
def get_materialized_column_for_property(
    table: TablesWithMaterializedColumns, table_column: TableColumn, property_name: PropertyName
) -> MaterializedColumn | None: ...

def get_enabled_materialized_columns_by_table() -> Mapping[
    TablesWithMaterializedColumns, Mapping[tuple[PropertyName, TableColumn], MaterializedColumn]
]: ...
```

`MaterializedColumn` is a `Protocol`, not a base class, so any object with the right shape
satisfies it:

```python
name: ColumnName
is_nullable: bool
has_minmax_index: bool
has_bloom_filter_index: bool
has_ngram_lower_index: bool
has_bloom_filter_lower_index: bool
@property
def type(self) -> str
```

Both functions must respect the `MATERIALIZED_COLUMNS_ENABLED` instance setting and return
nothing when it is off.

## Phase 1 — read-side registry (required)

Restores the 95 substitutions. **No DDL. Pure `SELECT`.** This is the whole value; do this
before considering anything else.

Discover columns from `system.columns` where the comment starts with `column_materializer`,
for each table in `MATERIALIZATION_VALID_TABLES` (`events`, `person`, `groups`), and build
Protocol-satisfying objects.

### Comment grammar — verified against production

Three shapes exist, and a real cluster carries all of them:

| Comment | Meaning |
|---|---|
| `column_materializer::<property>` | table column defaults to `properties`; enabled |
| `column_materializer::<table_column>::<property>` | enabled |
| `column_materializer::<table_column>::<property>::disabled` | disabled — must be excluded from the *enabled* registry |

88 of the production columns use the first, oldest form. An implementation that handles
only the three-part shape will miss nearly all of them. Reference parse:
`_parse_materializer_comment` in `posthog/models/data_deletion_request.py`.

Exclude the `column_materializer::elements_chain::*` family, as the deletion queries
already do.

### Index detection

The Protocol carries four index flags. Read them from `system.data_skipping_indices` for
the table, matching on index name. Naming is deterministic per column; confirm the live
names on a real cluster before fixing the convention in code, since existing indexes were
created by the previous implementation and must be recognised as-is.

### Caching

The current implementation memoizes for 15 minutes with background refresh. Match roughly;
these lookups sit in the HogQL compile path and must not issue a query per property. Cache
per table, invalidated on write.

### Acceptance

- Against a database carrying the production columns, `get_enabled_materialized_columns_by_table()["events"]` returns **95** entries.
- Column names, `is_nullable` and the four index flags match what `system.columns` and `system.data_skipping_indices` report.
- Disabled columns are absent from the enabled registry, and present in the unfiltered one.
- A HogQL query filtering on `properties.$session_id` compiles to the physical column, not `JSONExtract`.

## Phase 2 — creation (done)

Nothing was rebuilt. Upstream already ships an MIT-side successor to `materialize()`: a
per-team slot mechanism where `dmat_string_<idx>` columns are a pre-created pool and a
dict resolves `(team_id, slot_index) -> property_name` at read and write time. All of it
is already in this tree and free of `ee`:

| Piece | Location |
|---|---|
| Slot model and its states | `posthog/models/materialized_column_slots.py` |
| Request/list API | `posthog/api/materialized_column_slot.py` |
| Read path | `posthog/hogql/property_metadata.py` |
| Workflow and activities | `posthog/temporal/backfill_materialized_property/` |
| Weekly cron builder | `.../schedule.py` (`0 0 * * 0`) |

Two gaps stopped it working here, and both are now closed.

**The slot API silently lost its dedup.** `get_auto_materialized_property_names()` and the
`auto_materialized` endpoint both returned nothing without `ee`, so the API would hand out
a slot for a property that already had a legacy `mat_` column — materializing it twice by
two mechanisms. Both now read through `posthog.clickhouse.materialized_columns`, which
serves whichever registry is present.

**Nothing armed the schedule.** `schedule.py` says to register the cron "from a management
command or initialization hook", and no such command existed, so requested slots would have
sat in PENDING forever. `manage.py register_dmat_backfill_schedule` does it, with
`--dry-run`; it is idempotent.

### What changed about selection

The old `ee` path chose properties itself: weekly, from `system.query_log`, materializing
anything whose queries errored or where more than 9 exceeded 40s over 168 hours, capped at
100 per run. The slot path is deliberate instead — a staff user requests a property through
the API and the weekly workflow allocates, backfills and activates it.

That is a real behavioural change and worth stating plainly: this build no longer tunes
itself. Nobody chose the 95 columns currently in use; the old analyzer accumulated them.
Reinstating automatic selection means writing a policy that creates PENDING slots, which is
a small amount of judgement over `system.query_log` rather than any of the hard machinery.

## Explicitly out of scope

- Reimplementing sharded/query-node DDL orchestration, index creation, or backfill.
  Backfill is already MIT-side, and creation has an MIT-side successor.
- Copying any code out of `ee/`. The Enterprise licence forbids copying, publishing and
  distributing, and this repository is public and labelled MIT. Build against the Protocol
  and the comment grammar above — both of which are observable from a running cluster —
  not from the `ee` source.

## Risks

- **Naming must match exactly.** The 88 existing columns and their indexes were created by
  the previous implementation. A registry that derives names differently will not find
  them, and a creation path that names differently will create duplicates. Read the live
  cluster and match what is there.
- **Silent failure mode.** If the registry returns nothing, no error is raised anywhere —
  queries simply get slower. Any rollout needs a positive check that the substitution count
  is what it should be, not just that the service is up.
- **`MATERIALIZE_COLUMNS_SCHEDULE_CRON` lives in `ee/settings.py`.** Anything in Phase 2
  that relies on it needs its own setting on the MIT side.

## Sizing

Phase 1 is a few hundred lines including tests, no DDL, and testable against a ClickHouse
carrying representative columns. Phase 2 is wiring plus a selection policy. Neither is the
700-line rebuild that "port the materialized columns subtree" first suggested — because
most of that subtree has already moved to the MIT side upstream.
