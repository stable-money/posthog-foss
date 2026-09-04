"""Stage A — per-job sample $ai_evaluation events."""

from datetime import datetime

import structlog
from temporalio import activity

from posthog.hogql import ast
from posthog.hogql.constants import LimitContext
from posthog.hogql.parser import parse_select
from posthog.hogql.property import property_to_expr
from posthog.hogql.query import execute_hogql_query

from posthog.clickhouse.query_tagging import Feature, Product, tags_context
from posthog.models.team import Team
from posthog.sync import database_sync_to_async
from posthog.temporal.ai_observability.evaluation_clustering.models import SamplerActivityInputs, SamplerActivityResult
from posthog.temporal.common.heartbeat import Heartbeater

logger = structlog.get_logger(__name__)


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 string with a trailing ``Z`` into a datetime.

    Contract: only ``Z``-suffixed UTC strings produced by ``AIObservabilityEvaluationSamplerWorkflow``
    are supported — no other offset forms or bare naïve strings. Generalise only if a
    caller needs it.
    """
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _sample_and_embed_sync(inputs: SamplerActivityInputs) -> SamplerActivityResult:
    try:
        team = Team.objects.get(id=inputs.team_id)
    except Team.DoesNotExist:
        logger.info("Team not found, skipping eval sampler run", team_id=inputs.team_id, job_id=inputs.job_id)
        return SamplerActivityResult(team_id=inputs.team_id, job_id=inputs.job_id, sampled=0, embedded=0)

    window_start = _parse_iso(inputs.window_start)
    window_end = _parse_iso(inputs.window_end)

    # Translate the user-supplied property filters into a HogQL expression.
    # Eval jobs typically filter by $ai_evaluation_name or $ai_evaluation_runtime
    # to scope clustering to one evaluator; callers may also provide no filters at all.
    filter_expr: ast.Expr | None = None
    if inputs.event_filters:
        filter_exprs = [property_to_expr(f, team) for f in inputs.event_filters]
        filter_expr = ast.And(exprs=filter_exprs) if len(filter_exprs) > 1 else filter_exprs[0]

    query = parse_select(
        """
        SELECT
            toString(uuid) as event_uuid,
            properties.$ai_evaluation_name as eval_name,
            properties.$ai_evaluation_result as eval_result,
            properties.$ai_evaluation_applicable as eval_applicable,
            properties.$ai_evaluation_reasoning as eval_reasoning,
            properties.$ai_evaluation_id as eval_id
        FROM events
        WHERE event = '$ai_evaluation'
            AND timestamp >= {start_dt}
            AND timestamp < {end_dt}
            AND {filter_expr}
        -- perf: ORDER BY rand() is a full scan with a per-row random key;
        -- fine at today's 250 samples/hr/job, but revisit before a high-volume
        -- tenant opts in (consider reservoir sampling or bloom-pruned windows).
        ORDER BY rand()
        LIMIT {max_samples}
        """
    )

    with tags_context(product=Product.LLM_ANALYTICS, feature=Feature.QUERY, team_id=team.id):
        result = execute_hogql_query(
            query_type="EvalSamplingForClustering",
            query=query,
            placeholders={
                "start_dt": ast.Constant(value=window_start),
                "end_dt": ast.Constant(value=window_end),
                "filter_expr": filter_expr or ast.Constant(value=True),
                "max_samples": ast.Constant(value=inputs.max_samples),
            },
            team=team,
            limit_context=LimitContext.QUERY_ASYNC,
        )

    rows = result.results or []
    if not rows:
        logger.info(
            "eval_sampler_no_rows",
            team_id=team.id,
            job_id=inputs.job_id,
            window_start=inputs.window_start,
            window_end=inputs.window_end,
        )
        return SamplerActivityResult(team_id=team.id, job_id=inputs.job_id, sampled=0, embedded=0)

    logger.info(
        "eval_sampler_embedded",
        team_id=team.id,
        job_id=inputs.job_id,
        sampled=len(rows),
        embedded=0,
        window_start=inputs.window_start,
        window_end=inputs.window_end,
    )

    return SamplerActivityResult(
        team_id=team.id,
        job_id=inputs.job_id,
        sampled=len(rows),
        embedded=0,
    )


@activity.defn
async def sample_and_embed_for_job_activity(inputs: SamplerActivityInputs) -> SamplerActivityResult:
    """Sample up to N $ai_evaluation events from a window, compose text, enqueue embeddings.

    Runs hourly per active evaluation ClusteringJob. Pure function of the inputs —
    no dedupe state, no watermark.

    Exceptions are stringified before propagating so Temporal's failure serializer
    doesn't trip on cyclic references inside HogQL AST nodes (query objects hold
    back-references to their parents) or ClickHouse driver exception payloads
    (context pointers into the live connection). ``ValueError`` and ``TypeError``
    are re-raised unchanged so they still match the non-retryable list in
    ``SAMPLER_ACTIVITY_RETRY_POLICY`` — those never carry cyclic references and
    must fail fast.

    ``from None`` intentionally drops the cause chain for the same serializer
    reason; the original traceback is preserved in the structured log via
    ``logger.exception`` above, which is where debugging should start.
    """
    async with Heartbeater():
        try:
            return await database_sync_to_async(_sample_and_embed_sync, thread_sensitive=False)(inputs)
        except (ValueError, TypeError):
            raise
        except Exception as exc:
            logger.exception(
                "eval_sampler_activity_failed",
                team_id=inputs.team_id,
                job_id=inputs.job_id,
                error_type=type(exc).__name__,
            )
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from None
