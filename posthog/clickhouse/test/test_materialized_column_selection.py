import pytest
from posthog.test.base import BaseTest
from unittest.mock import patch

from posthog.clickhouse.materialized_column_selection import SlotCandidate, find_slot_candidates, propose_slots
from posthog.models import MaterializedColumnSlot, PropertyDefinition
from posthog.models.materialized_column_slots import MAX_SLOTS_PER_TEAM


def _candidates(rows):
    with patch("posthog.clickhouse.client.sync_execute", return_value=rows):
        return find_slot_candidates()


class TestFindSlotCandidates:
    @pytest.mark.parametrize(
        "fragment,expected_property",
        [
            ("properties, 'plan'", "plan"),
            # Real generated SQL prefixes the column, and the property name is free text.
            ("properties, '$feature/new-nav'", "$feature/new-nav"),
            ("properties, 'utm_source'", "utm_source"),
        ],
    )
    def test_keeps_event_properties(self, fragment, expected_property):
        candidates = _candidates([(2, fragment, 100, 40, 0)])
        assert [c.property_name for c in candidates] == [expected_property]

    @pytest.mark.parametrize(
        "fragment",
        [
            # Only the events table's own properties column can take a slot. These share the
            # fragment shape and would otherwise queue slots that can never be filled.
            "person_properties, 'email'",
            "group0_properties, 'name'",
            "group1_properties, 'plan'",
        ],
    )
    def test_drops_columns_that_cannot_take_a_slot(self, fragment):
        assert _candidates([(2, fragment, 100, 40, 0)]) == []

    def test_drops_fragments_it_cannot_parse(self):
        assert _candidates([(2, "not a fragment", 100, 40, 0)]) == []

    def test_ranks_failures_above_slowness(self):
        rows = [
            (2, "properties, 'slow'", 900, 800, 0),
            (2, "properties, 'failing'", 20, 10, 3),
        ]
        assert [c.property_name for c in _candidates(rows)] == ["failing", "slow"]


class TestProposeSlots(BaseTest):
    def _property(self, name, team=None):
        return PropertyDefinition.objects.create(
            team=team or self.team,
            name=name,
            type=PropertyDefinition.Type.EVENT,
        )

    def _propose(self, candidates, materialized=frozenset(), **kwargs):
        with (
            patch(
                "posthog.clickhouse.materialized_column_selection.find_slot_candidates",
                return_value=candidates,
            ),
            patch(
                "posthog.api.materialized_column_slot.get_auto_materialized_property_names",
                return_value=set(materialized),
            ),
        ):
            return propose_slots(**kwargs)

    def _candidate(self, name, team=None):
        return SlotCandidate(
            team_id=(team or self.team).id,
            property_name=name,
            query_count=100,
            slow_query_count=40,
            failed_query_count=0,
        )

    def test_queues_a_slot_for_a_known_property(self):
        prop = self._property("plan")

        queued = self._propose([self._candidate("plan")])

        assert [c.property_name for c in queued] == ["plan"]
        slot = MaterializedColumnSlot.objects.get(team=self.team)
        assert slot.property_definition == prop
        # PENDING with no index: the backfill workflow owns allocation, this only queues.
        assert slot.slot_index is None

    def test_dry_run_writes_nothing(self):
        self._property("plan")

        queued = self._propose([self._candidate("plan")], dry_run=True)

        assert [c.property_name for c in queued] == ["plan"]
        assert not MaterializedColumnSlot.objects.exists()

    def test_skips_a_property_that_already_has_a_column(self):
        self._property("plan")

        assert self._propose([self._candidate("plan")], materialized={"plan"}) == []
        assert not MaterializedColumnSlot.objects.exists()

    def test_skips_a_property_the_taxonomy_has_never_seen(self):
        assert self._propose([self._candidate("never_captured")]) == []
        assert not MaterializedColumnSlot.objects.exists()

    def test_skips_a_property_that_is_already_slotted(self):
        prop = self._property("plan")
        MaterializedColumnSlot.objects.create(team=self.team, property_definition=prop)

        assert self._propose([self._candidate("plan")]) == []
        assert MaterializedColumnSlot.objects.count() == 1

    def test_stops_at_the_per_run_ceiling(self):
        names = [f"prop_{i}" for i in range(4)]
        for name in names:
            self._property(name)

        queued = self._propose([self._candidate(name) for name in names], max_per_team=2)

        assert len(queued) == 2
        assert MaterializedColumnSlot.objects.count() == 2

    def test_stops_at_the_teams_slot_capacity(self):
        # A full team must not queue more, or the backfill workflow finds slots it cannot allocate.
        for i in range(MAX_SLOTS_PER_TEAM):
            MaterializedColumnSlot.objects.create(team=self.team, property_definition=self._property(f"existing_{i}"))
        self._property("plan")

        assert self._propose([self._candidate("plan")], max_per_team=MAX_SLOTS_PER_TEAM + 1) == []
        assert MaterializedColumnSlot.objects.count() == MAX_SLOTS_PER_TEAM

    def test_ceiling_is_per_team(self):
        other_team = self.organization.teams.create(name="Other")
        self._property("plan")
        self._property("plan", team=other_team)

        queued = self._propose(
            [self._candidate("plan"), self._candidate("plan", team=other_team)],
            max_per_team=1,
        )

        assert len(queued) == 2
        assert MaterializedColumnSlot.objects.count() == 2
