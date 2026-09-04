"""Activity for generating LLM summary and saving the result."""

import time
from dataclasses import dataclass
from uuid import uuid4

import structlog
import temporalio

from posthog.models.event.util import create_event
from posthog.models.team import Team
from posthog.redis import get_async_client
from posthog.sync import database_sync_to_async
from posthog.temporal.ai_observability.trace_summarization import constants
from posthog.temporal.ai_observability.trace_summarization.models import (
    SummarizationActivityResult,
    SummarizeAndSaveInput,
    TextReprExpiredError,
)
from posthog.temporal.ai_observability.trace_summarization.state import delete_text_repr, load_text_repr
from posthog.temporal.common.heartbeat import Heartbeater

from products.ai_observability.backend.summarization.llm import summarize
from products.ai_observability.backend.summarization.llm.schema import SummarizationResponse
from products.ai_observability.backend.summarization.models import OpenAIModel, SummarizationMode

logger = structlog.get_logger(__name__)


@dataclass
class SaveSummaryEventContext:
    """Shared context for saving summary events to ClickHouse."""

    summary_result: SummarizationResponse
    text_repr_length: int
    trace_id: str
    trace_first_timestamp: str
    mode: str
    batch_run_id: str
    job_id: str
    job_name: str
    team: Team
    team_id: int


def _save_trace_summary_event(
    ctx: SaveSummaryEventContext,
    event_count: int,
) -> None:
    sr = ctx.summary_result
    summary_bullets_json = [bullet.model_dump() for bullet in sr.summary_bullets]
    summary_notes_json = [note.model_dump() for note in sr.interesting_notes]

    properties = {
        "$ai_trace_id": ctx.trace_id,
        "$ai_batch_run_id": ctx.batch_run_id,
        "$ai_clustering_job_id": ctx.job_id,
        "$ai_clustering_job_name": ctx.job_name,
        "$ai_summary_mode": ctx.mode,
        "$ai_summary_title": sr.title,
        "$ai_summary_flow_diagram": sr.flow_diagram,
        "$ai_summary_bullets": summary_bullets_json,
        "$ai_summary_interesting_notes": summary_notes_json,
        "$ai_text_repr_length": ctx.text_repr_length,
        "$ai_event_count": event_count,
        "trace_timestamp": ctx.trace_first_timestamp,
    }

    create_event(
        event_uuid=uuid4(),
        event=constants.EVENT_NAME_TRACE_SUMMARY,
        team=ctx.team,
        distinct_id=f"trace_summary_{ctx.team_id}",
        properties=properties,
    )


def _save_generation_summary_event(
    ctx: SaveSummaryEventContext,
    generation_id: str,
) -> None:
    sr = ctx.summary_result
    summary_bullets_json = [bullet.model_dump() for bullet in sr.summary_bullets]
    summary_notes_json = [note.model_dump() for note in sr.interesting_notes]

    properties = {
        "$ai_generation_id": generation_id,
        "$ai_trace_id": ctx.trace_id,
        "$ai_batch_run_id": ctx.batch_run_id,
        "$ai_clustering_job_id": ctx.job_id,
        "$ai_clustering_job_name": ctx.job_name,
        "$ai_summary_mode": ctx.mode,
        "$ai_summary_title": sr.title,
        "$ai_summary_flow_diagram": sr.flow_diagram,
        "$ai_summary_bullets": summary_bullets_json,
        "$ai_summary_interesting_notes": summary_notes_json,
        "$ai_text_repr_length": ctx.text_repr_length,
        "trace_timestamp": ctx.trace_first_timestamp,
    }

    create_event(
        event_uuid=uuid4(),
        event=constants.EVENT_NAME_GENERATION_SUMMARY,
        team=ctx.team,
        distinct_id=f"generation_summary_{ctx.team_id}",
        properties=properties,
    )


@temporalio.activity.defn
async def summarize_and_save_activity(input: SummarizeAndSaveInput) -> SummarizationActivityResult:
    """Read text_repr from Redis, call LLM, save event, embed, and clean up."""
    is_generation = input.generation_id is not None
    log = logger.bind(
        trace_id=input.trace_id, generation_id=input.generation_id, team_id=input.team_id, redis_key=input.redis_key
    )

    activity_start = time.monotonic()

    async with Heartbeater():
        # Step 1: Load text_repr from Redis
        redis_client = get_async_client()
        text_repr = await load_text_repr(redis_client, input.redis_key)
        if text_repr is None:
            raise TextReprExpiredError(f"Redis key expired or missing: {input.redis_key}")

        # Step 2: Generate summary using LLM
        mode_enum = SummarizationMode(input.mode)
        model_enum = OpenAIModel(input.model) if input.model else None

        t0 = time.monotonic()
        summary_result = await database_sync_to_async(summarize, thread_sensitive=False)(
            text_repr=text_repr,
            team_id=input.team_id,
            mode=mode_enum,
            model=model_enum,
            user_id=f"temporal-workflow-team-{input.team_id}",
            # The batch pipeline can wait for its summaries, so it takes the cheaper flex tier.
            # The provider falls back to the standard tier when flex is refused or stalls.
            flex=True,
        )
        llm_duration_s = time.monotonic() - t0
        log.info(
            "LLM summary generated",
            llm_duration_s=round(llm_duration_s, 2),
            text_repr_length=len(text_repr),
            model=input.model,
        )

        # Step 3: Save event to ClickHouse
        team = await database_sync_to_async(Team.objects.get, thread_sensitive=False)(id=input.team_id)
        t0 = time.monotonic()
        save_ctx = SaveSummaryEventContext(
            summary_result=summary_result,
            text_repr_length=len(text_repr),
            trace_id=input.trace_id,
            trace_first_timestamp=input.trace_first_timestamp,
            mode=input.mode,
            batch_run_id=input.batch_run_id,
            job_id=input.job_id,
            job_name=input.job_name,
            team=team,
            team_id=input.team_id,
        )
        if is_generation:
            assert input.generation_id is not None
            await database_sync_to_async(_save_generation_summary_event, thread_sensitive=False)(
                save_ctx, input.generation_id
            )
        else:
            await database_sync_to_async(_save_trace_summary_event, thread_sensitive=False)(save_ctx, input.event_count)
        save_duration_s = time.monotonic() - t0

        # Step 4: Clean up Redis key
        await delete_text_repr(redis_client, input.redis_key)

        total_duration_s = time.monotonic() - activity_start
        log.info(
            "Activity completed",
            total_duration_s=round(total_duration_s, 2),
            llm_duration_s=round(llm_duration_s, 2),
            save_duration_s=round(save_duration_s, 2),
            text_repr_length=len(text_repr),
        )

    return SummarizationActivityResult(
        trace_id=input.trace_id,
        success=True,
        generation_id=input.generation_id,
        text_repr_length=len(text_repr),
        event_count=input.event_count,
    )
