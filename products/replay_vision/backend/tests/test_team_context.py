import datetime as dt

from posthog.test.base import BaseTest

from products.posthog_ai.backend.models.assistant import CoreMemory
from products.replay_vision.backend.temporal.team_context import _MAX_PRODUCT_CONTEXT_LEN, fetch_product_context
from products.replay_vision.backend.temporal.types import ScannerLlmInputs


def _rows(*event_names: str) -> tuple[list[str], list[list[str]]]:
    return ["event_uuid", "event"], [[f"u{i}", name] for i, name in enumerate(event_names)]


class TestFetchProductContext(BaseTest):
    def test_prefers_core_memory_over_product_description(self):
        self.team.project.product_description = "Old description"
        self.team.project.save()
        CoreMemory.objects.create(team=self.team, text="Acme sells rockets to coyotes.")
        assert fetch_product_context(self.team) == "Acme sells rockets to coyotes."

    def test_falls_back_to_project_product_description(self):
        self.team.project.product_description = "A B2B invoicing tool."
        self.team.project.save()
        assert fetch_product_context(self.team) == "A B2B invoicing tool."

    def test_empty_when_neither_source_exists(self):
        assert fetch_product_context(self.team) == ""

    def test_sanitizes_control_chars_keeps_newlines_and_caps_length(self):
        CoreMemory.objects.create(team=self.team, text="fact one\x07\nfact two   spaced\n\n" + "x" * 9000)
        result = fetch_product_context(self.team)
        assert result.startswith("fact one\nfact two spaced\n")
        assert "\x07" not in result
        assert len(result) <= _MAX_PRODUCT_CONTEXT_LEN + 1  # cap plus the ellipsis


class TestScannerLlmInputsCompat:
    def test_loads_payloads_stored_before_the_context_fields_existed(self):
        payload = {
            "session_id": "sess-1",
            "team_id": 1,
            "events": {"columns": ["event"], "rows": [["$pageview"]]},
            "metadata": {
                "start_time": dt.datetime(2026, 5, 12, 10, 0, tzinfo=dt.UTC),
                "end_time": dt.datetime(2026, 5, 12, 10, 5, tzinfo=dt.UTC),
                "duration_seconds": 300.0,
                "active_seconds": 200.0,
                "inactive_seconds": 100.0,
            },
        }
        loaded = ScannerLlmInputs(**payload)
        assert loaded.product_context == ""
        assert loaded.event_descriptions == {}
