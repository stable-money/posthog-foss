"""Arm the weekly dmat backfill schedule.

posthog/temporal/backfill_materialized_property/schedule.py builds the cron schedule but
nothing calls it: its own docstring says to register it "from a management command or
initialization hook", and there was no such command. Without it, slots requested through
the materialized-column slot API stay PENDING forever -- nothing allocates a column index,
runs the mutation, or moves them to READY.

Idempotent, per that module: running it twice is safe.
"""

import asyncio

from django.core.management.base import BaseCommand

import structlog

from posthog.temporal.backfill_materialized_property.schedule import (
    BACKFILL_SCHEDULE_ID,
    WEEKLY_DMAT_BACKFILL_CRON,
    create_or_update_weekly_dmat_backfill_schedule,
)
from posthog.temporal.common.client import async_connect

logger = structlog.get_logger(__name__)


class Command(BaseCommand):
    help = "Create or refresh the weekly dmat backfill Temporal schedule"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be registered without contacting Temporal.",
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            self.stdout.write(
                f"Would register schedule {BACKFILL_SCHEDULE_ID!r} with cron {WEEKLY_DMAT_BACKFILL_CRON!r}."
            )
            return

        async def register() -> None:
            client = await async_connect()
            await create_or_update_weekly_dmat_backfill_schedule(client)

        asyncio.run(register())
        self.stdout.write(
            self.style.SUCCESS(f"Registered schedule {BACKFILL_SCHEDULE_ID!r} with cron {WEEKLY_DMAT_BACKFILL_CRON!r}.")
        )
