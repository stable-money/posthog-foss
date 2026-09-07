"""Queue materialization slots for the event properties that are hurting most.

The same policy the weekly `clickhouse_materialize_columns` task runs, exposed so it can be
inspected before it is trusted. `--dry-run` reports what would be queued and writes nothing.
"""

from django.core.management.base import BaseCommand

import structlog

from posthog.clickhouse.materialized_column_selection import (
    SELECTION_ANALYSIS_PERIOD_HOURS,
    SELECTION_MAX_PER_TEAM_PER_RUN,
    SELECTION_MIN_SLOW_QUERIES,
    SELECTION_SLOW_QUERY_MS,
    find_slot_candidates,
    propose_slots,
)

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Queue PENDING materialization slots for slow or failing event property reads"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report without creating slots.")
        parser.add_argument(
            "--show-candidates",
            action="store_true",
            help="List every candidate the query log turned up, before any filtering.",
        )
        parser.add_argument(
            "--max-per-team",
            type=int,
            default=SELECTION_MAX_PER_TEAM_PER_RUN,
            help=f"Ceiling per team for this run (default {SELECTION_MAX_PER_TEAM_PER_RUN}).",
        )

    def handle(self, *args, **options):
        self.stdout.write(
            f"Looking back {SELECTION_ANALYSIS_PERIOD_HOURS}h for properties with "
            f">={SELECTION_MIN_SLOW_QUERIES} queries over {SELECTION_SLOW_QUERY_MS}ms, or any failures."
        )

        if options["show_candidates"]:
            candidates = find_slot_candidates()
            self.stdout.write(f"\n{len(candidates)} candidate(s), worst first:")
            for candidate in candidates:
                self.stdout.write(
                    f"  team={candidate.team_id:<6} failed={candidate.failed_query_count:<5}"
                    f" slow={candidate.slow_query_count:<6} seen={candidate.query_count:<7}"
                    f" {candidate.property_name}"
                )

        queued = propose_slots(dry_run=options["dry_run"], max_per_team=options["max_per_team"])

        verb = "Would queue" if options["dry_run"] else "Queued"
        self.stdout.write(self.style.SUCCESS(f"\n{verb} {len(queued)} slot(s)."))
        for candidate in queued:
            self.stdout.write(f"  team={candidate.team_id}  {candidate.property_name}")
        if not options["dry_run"] and queued:
            self.stdout.write("The weekly dmat backfill workflow will allocate and fill them.")
