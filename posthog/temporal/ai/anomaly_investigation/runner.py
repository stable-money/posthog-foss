"""Single-pass anomaly investigation agent loop.

Kept small on purpose: we don't need LangGraph's conditional routing or the
streaming machinery from Max — just a tool-calling loop that terminates with a
structured report.

Budget: at most MAX_TOOL_CALLS tool invocations. After that the agent is told
to finalize with what it has. The loop exits either on a final assistant
message with no tool calls, or on budget exhaustion.
"""

from __future__ import annotations

import json
import uuid
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import posthoganalytics
from langchain_core.callbacks import BaseCallbackHandler
from posthoganalytics.ai.langchain.callbacks import CallbackHandler
from pydantic import ValidationError

from posthog.models import Team, User
from posthog.temporal.ai.anomaly_investigation.report import InvestigationReport, salvage_report

from products.alerts.backend.models.alert import AlertConfiguration

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS = 10
AGENT_MODEL = "claude-sonnet-5"
FINAL_REPORT_TOOL_NAME = "submit_investigation_report"
MAX_TOOL_RESULT_CHARS = 12_000  # ~3K tokens per call — keeps 10 calls well under the context limit.
# Sonnet 5 runs adaptive thinking by default, which counts against max_tokens —
# leave headroom so a thinking-heavy turn can't truncate the final report.
MAX_OUTPUT_TOKENS = 8192
# Per-request cap. The surrounding Temporal activity has its own (longer) deadline;
# this guards against a single stuck HTTP call hanging for the whole activity budget.
# Sized for adaptive-thinking turns, which run longer than plain tool-call turns.
LLM_REQUEST_TIMEOUT_SECONDS = 180.0


ToolHandler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class InvestigationRunResult:
    report: InvestigationReport
    tool_calls_used: int
    model: str


async def run_investigation(
    *,
    team: Team,
    user: User,
    anomaly_context: Any,  # str or list[{type, ...}] LangChain content blocks
    alert: AlertConfiguration | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> InvestigationRunResult:
    """Drive the agent loop to completion and return the structured report.

    ``anomaly_context`` accepts either a plain string or a list of content blocks
    (text + image for multimodal input). ``alert`` gives metric-specific tools a
    handle on the insight and detector_config. ``heartbeat`` is invoked once per
    iteration so the enclosing Temporal activity stays alive during long LLM calls.
    """
    # The investigation is an LLM agent loop built on the Max chat client, which is part of
    # the enterprise code this build does not contain. There is no reduced investigation to
    # return, so this fails explicitly instead of reporting an empty finding.
    raise RuntimeError(
        "Anomaly investigation needs the Max LLM client, which is not part of this build."
    )


def _build_callbacks(*, team: Team, alert: AlertConfiguration | None) -> list[BaseCallbackHandler]:
    callbacks: list[BaseCallbackHandler] = []
    client = posthoganalytics.default_client
    if client is None:
        return callbacks
    properties: dict[str, Any] = {
        "ai_product": "alert_investigation_agent",
        "team_id": team.id,
    }
    if alert is not None:
        properties["alert_id"] = str(alert.id)
    callbacks.append(
        CallbackHandler(
            client,
            distinct_id=str(team.id),
            trace_id=f"alert-investigation-{uuid.uuid4()}",
            properties=properties,
        )
    )
    return callbacks


def _final_report_args(tool_calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    for call in tool_calls:
        if call.get("name") == FINAL_REPORT_TOOL_NAME:
            return call.get("args") or {}
    return None


def _salvage_from_history(report_args_history: list[dict[str, Any]]) -> InvestigationReport | None:
    for args in reversed(report_args_history):
        report = salvage_report(args)
        if report is not None:
            return report
    return None


def _report_from_tool_calls(tool_calls: list[dict[str, Any]]) -> InvestigationReport | None:
    args = _final_report_args(tool_calls)
    if args is None:
        return None
    try:
        return InvestigationReport.model_validate(args)
    except ValidationError:
        return None


def _validation_error_summary(err: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(loc) for loc in detail['loc'])}: {detail['msg']}" for detail in err.errors()[:5])


def _parse_report_text(content: Any) -> InvestigationReport | None:
    text = _stringify(content).strip()
    # Try direct JSON; else find first/last brace.
    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
            return InvestigationReport.model_validate(parsed)
        except (ValueError, TypeError, ValidationError):
            continue
    return None


def _parse_report(content: Any) -> InvestigationReport:
    report = _parse_report_text(content)
    if report is not None:
        return report
    text = _stringify(content).strip()
    if not text:
        return _fallback_report("Agent returned no final message.")
    return _fallback_report("Agent final message was not valid InvestigationReport JSON.")


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])
    return candidates


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and "text" in item:
                chunks.append(item["text"])
            elif isinstance(item, str):
                chunks.append(item)
        return "".join(chunks)
    return str(content)


def _fallback_report(reason: str) -> InvestigationReport:
    return InvestigationReport(
        verdict="inconclusive",
        summary=reason,
        hypotheses=[],
        recommendations=["Review the insight manually — the agent could not produce a structured report."],
    )
