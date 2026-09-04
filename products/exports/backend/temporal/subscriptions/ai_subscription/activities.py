import uuid
import dataclasses

import temporalio.activity
from structlog import get_logger
from temporalio.exceptions import ApplicationError

from posthog.dataclasses import frozen
from posthog.sync import database_sync_to_async

from products.exports.backend.models.subscription import Subscription, SubscriptionDelivery
from products.exports.backend.temporal.subscriptions.ai_subscription.delivery import (
    build_ai_subscription_report,
    build_ai_teams_card,
    build_chart_image_urls,
    send_email_ai_subscription_report,
    send_slack_ai_subscription_report,
)
from products.exports.backend.temporal.subscriptions.ai_subscription.report_pipeline import AiReportResult
from products.exports.backend.temporal.subscriptions.ai_subscription.spec_generator import PromptRejectedError
from products.exports.backend.temporal.subscriptions.delivery_common import (
    auto_disable_and_return,
    deliver_email,
    deliver_slack,
    strip_null_bytes,
)
from products.exports.backend.temporal.subscriptions.delivery_webhook import deliver_teams_webhook
from products.exports.backend.temporal.subscriptions.types import (
    AI_REPORT_CHARTS_KEY,
    AI_REPORT_DIAGNOSTICS_KEY,
    AI_REPORT_PROMPT_SNAPSHOT_KEY,
    AI_REPORT_SNAPSHOT_KEY,
    AI_REPORT_WINDOW_END_KEY,
    DeliverSubscriptionInputs,
    DeliverSubscriptionResult,
    GenerateAIReportInputs,
    GenerateAIReportResult,
    QueryErrorDetails,
    RecipientResult,
)

LOGGER = get_logger(__name__)


@frozen
class _DisableReason:
    key: str
    description: str


AI_CONSENT_REVOKED_DISABLE_REASON = _DisableReason(
    key="ai_consent_revoked",
    description="This subscription was disabled because the organization revoked AI data processing consent.",
)
AI_PROMPT_INVALID_DISABLE_REASON = _DisableReason(
    key="ai_prompt_invalid",
    description="This subscription was disabled because its prompt is no longer valid.",
)


async def _load_snapshot(delivery_id: uuid.UUID) -> dict | None:
    # Single read of the delivery's content_snapshot (both the AI report markdown and the
    # diagnostics live here). DoesNotExist is tolerated: a missing row just means "no report yet".
    @database_sync_to_async(thread_sensitive=False)
    def _read() -> dict | None:
        try:
            snapshot = SubscriptionDelivery.objects.values_list("content_snapshot", flat=True).get(pk=delivery_id)
        except SubscriptionDelivery.DoesNotExist:
            return None
        return snapshot if isinstance(snapshot, dict) else None

    return await _read()


def _snapshot_report(snapshot: dict | None) -> str | None:
    report = snapshot.get(AI_REPORT_SNAPSHOT_KEY) if snapshot else None
    return report if isinstance(report, str) and report else None


@frozen
class DiagnosticCounts:
    failed_step_count: int
    total_step_count: int
    query_errors: list[QueryErrorDetails]

    @property
    def error_types(self) -> list[str]:
        return sorted({error["type"] for error in self.query_errors if error["type"]})


def _query_error_details(
    error_type: str | None, error_code: str | None, error_message: str | None
) -> QueryErrorDetails:
    return {
        "type": error_type,
        "code": error_code,
        "message": error_message,
    }


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _tally_diagnostics(steps: list[QueryErrorDetails | None]) -> DiagnosticCounts:
    # One typed object keeps every failed query's type, code, and safe message paired. None marks a
    # successful step. The same representation is built from persisted and in-memory diagnostics.
    query_errors = [step for step in steps if step is not None]
    return DiagnosticCounts(
        failed_step_count=len(query_errors),
        total_step_count=len(steps),
        query_errors=query_errors,
    )


def _snapshot_diagnostic_counts(snapshot: dict | None) -> DiagnosticCounts:
    # The prior run's failure shape, read back from the persisted diagnostics on Temporal redispatch.
    diagnostics = snapshot.get(AI_REPORT_DIAGNOSTICS_KEY) if snapshot else None
    if not isinstance(diagnostics, list):
        return DiagnosticCounts(failed_step_count=0, total_step_count=0, query_errors=[])
    # Only well-formed dict entries count — a malformed one would inflate the total and mask an
    # all-failed report; `ok is not False` keeps a missing/None ok out of the failed set.
    return _tally_diagnostics(
        [
            None
            if d.get("ok") is not False
            else _query_error_details(
                _string_or_none(d.get("error_type")),
                _string_or_none(d.get("error_code")),
                _string_or_none(d.get("human_readable_error")),
            )
            for d in diagnostics
            if isinstance(d, dict)
        ]
    )


def _report_diagnostic_counts(result: AiReportResult) -> DiagnosticCounts:
    return _tally_diagnostics(
        [
            None if d.ok else _query_error_details(d.error_type, d.error_code, d.human_readable_error)
            for d in result.diagnostics
        ]
    )


async def _persist_ai_report(delivery_id: uuid.UUID, result: AiReportResult, prompt: str | None) -> None:
    @database_sync_to_async(thread_sensitive=False)
    def _write() -> None:
        # No DoesNotExist guard: create_delivery_record always writes this row before
        # generation runs, so a missing row is a wiring bug — let it raise loudly.
        delivery = SubscriptionDelivery.objects.get(pk=delivery_id)
        # LLM output and the user prompt can carry NUL bytes that Postgres text/jsonb reject;
        # scrub them here (payloads are small) as they are the untrusted inputs on this write path.
        delivery.content_snapshot = {
            **(delivery.content_snapshot or {}),
            AI_REPORT_SNAPSHOT_KEY: strip_null_bytes(result.markdown),
            AI_REPORT_DIAGNOSTICS_KEY: strip_null_bytes([dataclasses.asdict(d) for d in result.diagnostics]),
            AI_REPORT_WINDOW_END_KEY: result.window_end_utc,
            AI_REPORT_CHARTS_KEY: strip_null_bytes([dataclasses.asdict(chart) for chart in result.charts]),
            # prompt is None for non-AI subs; "" if cleared — omit either.
            **({AI_REPORT_PROMPT_SNAPSHOT_KEY: strip_null_bytes(prompt)} if prompt else {}),
        }
        delivery.save(update_fields=["content_snapshot", "last_updated_at"])

    await _write()


@temporalio.activity.defn
async def generate_ai_subscription_report(inputs: GenerateAIReportInputs) -> GenerateAIReportResult:
    # The "decide what to send" phase, split from delivery so the LLM runs once up front with
    # its own retry policy. Terminal failures (consent revoked, prompt invalid) auto-disable and
    # return aborted=True; transient errors bubble up for the activity's Temporal retry.
    subscription = await database_sync_to_async(
        Subscription.objects.select_related("created_by", "team", "team__organization").get,
        thread_sensitive=False,
    )(pk=inputs.subscription_id)

    # Idempotency on Temporal redispatch: if a prior attempt already produced the report,
    # don't re-bill the LLM — the point of the generate -> deliver split is one LLM run.
    # One snapshot read serves both the "already generated?" check and the prior failure shape.
    snapshot = await _load_snapshot(inputs.delivery_id)
    if _snapshot_report(snapshot) is not None:
        await LOGGER.ainfo("generate_ai_subscription_report.already_generated", subscription_id=subscription.id)
        counts = _snapshot_diagnostic_counts(snapshot)
        return GenerateAIReportResult(
            aborted=False,
            failed_step_count=counts.failed_step_count,
            total_step_count=counts.total_step_count,
            query_errors=counts.query_errors,
            target_type=subscription.target_type,
        )

    # Consent is gated once here, before any LLM cost — creation-time gates don't catch an
    # org that revokes AI-data-processing approval later. Auto-disable so it stops re-firing.
    if not subscription.team.organization.is_ai_data_processing_approved:
        LOGGER.warning("generate_ai_subscription_report.consent_revoked", subscription_id=subscription.id)
        aborted = await auto_disable_and_return(
            subscription,
            AI_CONSENT_REVOKED_DISABLE_REASON,
            [],
        )
        return GenerateAIReportResult(
            aborted=True, recipient_results=aborted.recipient_results, target_type=subscription.target_type
        )

    try:
        report_result = await build_ai_subscription_report(subscription)
    except PromptRejectedError as exc:
        # Structurally permanent: no creator, prompt now fails sanitization, or the
        # planner returned a malformed plan. Re-firing wastes LLM tokens every cycle.
        LOGGER.warning(
            "generate_ai_subscription_report.prompt_rejected",
            subscription_id=subscription.id,
            reason=str(exc),
        )
        # Seed a recipient result with the exception detail first — it carries planner
        # context that the disable reason (appended next by `auto_disable_and_return`)
        # doesn't.
        # PromptRejectedError messages are handcrafted rejections (empty/too long/no creator), safe to show.
        recipient_results = [
            RecipientResult(
                recipient=subscription.recipient_label,
                status="failed",
                error={"message": str(exc), "type": "PromptRejectedError"},
                human_readable_error=str(exc),
            )
        ]
        aborted = await auto_disable_and_return(
            subscription,
            AI_PROMPT_INVALID_DISABLE_REASON,
            recipient_results,
        )
        return GenerateAIReportResult(
            aborted=True, recipient_results=aborted.recipient_results, target_type=subscription.target_type
        )

    await _persist_ai_report(inputs.delivery_id, report_result, subscription.prompt)
    counts = _report_diagnostic_counts(report_result)
    return GenerateAIReportResult(
        aborted=False,
        failed_step_count=counts.failed_step_count,
        total_step_count=counts.total_step_count,
        query_errors=counts.query_errors,
        target_type=subscription.target_type,
    )


async def _deliver_ai_subscription(
    subscription: Subscription,
    inputs: DeliverSubscriptionInputs,
    recipient_results: list[RecipientResult],
) -> DeliverSubscriptionResult:
    # Ships the report generate_ai_subscription_report already produced (read back from the
    # delivery row) — no LLM work here. Transient send errors retry; terminal Slack errors auto-disable.
    if inputs.delivery_id is None:
        # The AI workflow always creates the delivery row and runs generation before
        # delivery, so a missing reference is a wiring bug, not a runtime state.
        raise ApplicationError(f"AI delivery for subscription {subscription.id} has no delivery_id", non_retryable=True)

    delivery_id = inputs.delivery_id
    snapshot = await _load_snapshot(delivery_id)
    markdown = _snapshot_report(snapshot)
    if markdown is None:
        # Generation persists the report before delivery is scheduled, so a missing report
        # means the row was lost. Non-retryable: re-running *delivery* can't regenerate the
        # report, so retrying just burns attempts — fail loud rather than ship an empty report.
        raise ApplicationError(
            f"AI report missing for subscription {subscription.id} (delivery {inputs.delivery_id})",
            non_retryable=True,
        )

    chart_images = await database_sync_to_async(build_chart_image_urls, thread_sensitive=False)(
        (snapshot or {}).get(AI_REPORT_CHARTS_KEY) or [], team_id=subscription.team_id
    )

    if subscription.target_type == Subscription.SubscriptionTarget.EMAIL:
        # Dedup key for MessagingRecord: stable across this run's retries, unique per run so a re-test re-sends.
        workflow_run_id = temporalio.activity.info().workflow_run_id
        if workflow_run_id is None:
            raise ApplicationError("AI email delivery requires a workflow run id", non_retryable=True)

        async def _send_email(email: str) -> None:
            await database_sync_to_async(send_email_ai_subscription_report, thread_sensitive=False)(
                email=email,
                subscription=subscription,
                markdown=markdown,
                delivery_run_id=workflow_run_id,
                delivery_id=delivery_id,
                charts=chart_images,
            )

        return await deliver_email(subscription, inputs, recipient_results, _send_email)
    if subscription.target_type == Subscription.SubscriptionTarget.SLACK:
        return await deliver_slack(
            subscription,
            recipient_results,
            lambda integration: send_slack_ai_subscription_report(
                subscription=subscription,
                markdown=markdown,
                integration=integration,
                delivery_id=delivery_id,
                charts=chart_images,
            ),
        )
    if subscription.target_type == Subscription.SubscriptionTarget.TEAMS:
        card = build_ai_teams_card(subscription, markdown, delivery_id=delivery_id)
        return await deliver_teams_webhook(subscription, recipient_results, body=card)
    # `validate_subscription_for_delivery` auto-disables unsupported targets up front,
    # so reaching here means an invariant was violated.
    raise ApplicationError(
        f"AI delivery reached an unsupported target {subscription.target_type!r}", non_retryable=True
    )
