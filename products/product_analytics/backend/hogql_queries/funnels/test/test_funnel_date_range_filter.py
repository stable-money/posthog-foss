"""
`is_date_between` / `is_date_not_between` are the DATE-property equivalents of the existing
numeric `between` / `not_between` operators (posthog/hogql/property.py). Unlike `between`, they
coerce both sides to DateTime via `_force_datetime` before comparing, so this proves the range
actually filters real events end-to-end through a funnel query, not just that the generated AST
has the right shape.
"""

from posthog.test.base import APIBaseTest, ClickhouseTestMixin, _create_event, _create_person

from posthog.schema import DateRange, EventPropertyFilter, EventsNode, FunnelsQuery, PropertyOperator

from posthog.models import PropertyDefinition

from products.event_definitions.backend.models.property_definition import PropertyType
from products.product_analytics.backend.hogql_queries.funnels.funnels_query_runner import FunnelsQueryRunner

# Range shared by every test below. Chosen so "on the boundary" and "clearly outside" are
# unambiguous by inspection.
FROM_DATE = "2021-06-02 00:00:00"
TO_DATE = "2021-06-04 00:00:00"


class TestFunnelDateRangeFilter(ClickhouseTestMixin, APIBaseTest):
    maxDiff = None

    def setUp(self):
        # ClickhouseTestMixin sets CLASS_DATA_LEVEL_SETUP = False, so self.team only exists
        # per-test (via setUp), not at the setUpTestData classmethod stage -- this must be an
        # instance-level setUp, not a setUpTestData override.
        super().setUp()
        # Declaring this as a real DateTime PropertyDefinition (rather than leaving it an
        # untyped JSON string) routes the query through PropertySwapper, which wraps the column
        # in parseDateTime64BestEffortOrNull -- the harder code path _force_datetime has to
        # coexist with (see its docstring on the toString hop).
        PropertyDefinition.objects.create(
            team=self.team,
            name="engaged_at",
            type=PropertyDefinition.Type.EVENT,
            property_type=PropertyType.Datetime,
        )

    def _funnel_query(self, operator: PropertyOperator) -> FunnelsQuery:
        return FunnelsQuery(
            series=[
                EventsNode(
                    event="step one",
                    properties=[EventPropertyFilter(key="engaged_at", operator=operator, value=[FROM_DATE, TO_DATE])],
                ),
                EventsNode(event="step two"),
            ],
            # Wide enough to hold every fixture timestamp below without itself constraining
            # who enters the funnel -- only the property filter should do that.
            dateRange=DateRange(date_from="2021-05-01 00:00:00", date_to="2021-07-01 00:00:00", explicitDate=True),
        )

    def _step_one_count(self, operator: PropertyOperator) -> int:
        results = FunnelsQueryRunner(query=self._funnel_query(operator), team=self.team).calculate().results
        return results[0]["count"]

    def _step_two_count(self, operator: PropertyOperator) -> int:
        results = FunnelsQueryRunner(query=self._funnel_query(operator), team=self.team).calculate().results
        return results[1]["count"]

    def test_is_date_between_includes_only_people_inside_the_range(self):
        # NOTE: event `timestamp` is set equal to `engaged_at` purely so every fixture event
        # lands inside the funnel's own dateRange -- the filter under test compares the
        # `engaged_at` *property*, not the event timestamp, and the two are otherwise unrelated.
        _create_person(distinct_ids=["before"], team=self.team)
        _create_event(
            event="step one",
            distinct_id="before",
            team=self.team,
            timestamp="2021-06-01 00:00:00",
            properties={"engaged_at": "2021-06-01 00:00:00"},
        )

        _create_person(distinct_ids=["inside"], team=self.team)
        _create_event(
            event="step one",
            distinct_id="inside",
            team=self.team,
            timestamp="2021-06-03 12:00:00",
            properties={"engaged_at": "2021-06-03 12:00:00"},
        )

        _create_person(distinct_ids=["after"], team=self.team)
        _create_event(
            event="step one",
            distinct_id="after",
            team=self.team,
            timestamp="2021-06-05 00:00:00",
            properties={"engaged_at": "2021-06-05 00:00:00"},
        )

        count = self._step_one_count(PropertyOperator.IS_DATE_BETWEEN)

        # Only "inside" falls in [FROM_DATE, TO_DATE]; "before" and "after" are excluded.
        self.assertEqual(count, 1)

    def test_is_date_between_boundary_is_inclusive_on_both_ends(self):
        # property.py builds IS_DATE_BETWEEN as GtEq(expr, from) AND LtEq(expr, to) -- both
        # comparisons are inclusive, so a value exactly equal to either edge must still match.
        # Asserted here as a dedicated fixture (rather than folded into the range test above) so
        # the boundary claim doesn't ride on the same count as the "inside vs outside" one.
        _create_person(distinct_ids=["on_from"], team=self.team)
        _create_event(
            event="step one",
            distinct_id="on_from",
            team=self.team,
            timestamp=FROM_DATE,
            properties={"engaged_at": FROM_DATE},
        )
        _create_event(event="step two", distinct_id="on_from", team=self.team, timestamp="2021-06-02 01:00:00")

        _create_person(distinct_ids=["on_to"], team=self.team)
        _create_event(
            event="step one", distinct_id="on_to", team=self.team, timestamp=TO_DATE, properties={"engaged_at": TO_DATE}
        )
        _create_event(event="step two", distinct_id="on_to", team=self.team, timestamp="2021-06-04 01:00:00")

        results = (
            FunnelsQueryRunner(query=self._funnel_query(PropertyOperator.IS_DATE_BETWEEN), team=self.team)
            .calculate()
            .results
        )

        self.assertEqual(results[0]["count"], 2)  # both boundary values matched step one
        self.assertEqual(results[1]["count"], 2)  # and both went on to convert at step two

    def test_is_date_not_between_is_the_exact_complement(self):
        # Sharpest assertion in this file: for a fixed population, is_date_between and
        # is_date_not_between must partition it completely -- their step-one counts sum to the
        # unfiltered total, with zero overlap and zero people falling through the cracks.
        distinct_ids_and_props = [
            ("before", "2021-06-01 00:00:00"),  # outside -> not_between
            ("on_from", FROM_DATE),  # boundary -> between
            ("inside", "2021-06-03 12:00:00"),  # inside -> between
            ("on_to", TO_DATE),  # boundary -> between
            ("after", "2021-06-05 00:00:00"),  # outside -> not_between
        ]
        for distinct_id, engaged_at in distinct_ids_and_props:
            _create_person(distinct_ids=[distinct_id], team=self.team)
            _create_event(
                event="step one",
                distinct_id=distinct_id,
                team=self.team,
                timestamp=engaged_at,
                properties={"engaged_at": engaged_at},
            )
        # A person with the property missing entirely -- see the dedicated test below for why
        # this belongs on the not_between side.
        _create_person(distinct_ids=["missing"], team=self.team)
        _create_event(
            event="step one", distinct_id="missing", team=self.team, timestamp="2021-06-03 00:00:00", properties={}
        )

        # Asymmetric step two: 2 converters inside the range, 1 outside it.
        for distinct_id in ("on_from", "inside", "before"):
            _create_event(event="step two", distinct_id=distinct_id, team=self.team, timestamp="2021-06-06 00:00:00")

        # Unfiltered total: same fixture, no property filter on step one at all.
        unfiltered_query = FunnelsQuery(
            series=[EventsNode(event="step one"), EventsNode(event="step two")],
            dateRange=DateRange(date_from="2021-05-01 00:00:00", date_to="2021-07-01 00:00:00", explicitDate=True),
        )
        unfiltered_total = FunnelsQueryRunner(query=unfiltered_query, team=self.team).calculate().results[0]["count"]

        between_count = self._step_one_count(PropertyOperator.IS_DATE_BETWEEN)
        not_between_count = self._step_one_count(PropertyOperator.IS_DATE_NOT_BETWEEN)

        self.assertEqual(unfiltered_total, 6)
        self.assertEqual(between_count, 3)  # on_from, inside, on_to
        self.assertEqual(not_between_count, 3)  # before, after, missing
        self.assertEqual(between_count + not_between_count, unfiltered_total)

        # Counts alone cannot distinguish a real partition from a symmetric miscount that swaps
        # people between the two sides. Step two is deliberately asymmetric -- 2 converters on
        # the between side, 1 on the not_between side -- so a swap changes these numbers even
        # though the step-one totals would still sum correctly.
        self.assertEqual(self._step_two_count(PropertyOperator.IS_DATE_BETWEEN), 2)
        self.assertEqual(self._step_two_count(PropertyOperator.IS_DATE_NOT_BETWEEN), 1)

    def test_missing_property_is_excluded_from_between_and_caught_by_not_between(self):
        # property.py builds IS_DATE_NOT_BETWEEN as NOT(AND(GtEq, LtEq)) rather than
        # OR(Lt, Gt) specifically so a missing property is caught: each comparison is printed
        # ifNull(op, 0), so on a NULL property both sides of the AND are 0, the AND is false,
        # and NOT(false) is true. That makes "(not set)" satisfy is_date_not_between -- this is
        # the code's actual, documented semantics (see the comment beside IS_DATE_NOT_BETWEEN in
        # property.py), not an incidental gap: a property that was never set is not "in range",
        # so it belongs on the not_between side of the partition.
        #
        # This DIVERGES from the numeric NOT_BETWEEN, which prints OR(Lt, Gt) and so drops rows
        # whose property is missing. The divergence is deliberate; it is asserted from the other
        # side in test_property.py. Do not "fix" one to match the other without deciding which
        # semantics is right for both.
        #
        # "control" exists purely so the funnel has a nonzero-entrant baseline to compare
        # against: with zero step-0 entrants at all, FunnelsQueryRunner returns an empty
        # `results` list rather than step rows reading count=0, so an assertion of "missing
        # alone yields 0" would pass even if the query were broken outright.
        _create_person(distinct_ids=["missing"], team=self.team)
        _create_event(
            event="step one", distinct_id="missing", team=self.team, timestamp="2021-06-03 00:00:00", properties={}
        )
        _create_person(distinct_ids=["control"], team=self.team)
        _create_event(
            event="step one",
            distinct_id="control",
            team=self.team,
            timestamp="2021-06-03 00:00:00",
            properties={"engaged_at": "2021-06-03 00:00:00"},  # inside the range -> matches BETWEEN, not NOT_BETWEEN
        )

        # BETWEEN: only "control" matches -- if "missing" also matched, this would read 2.
        self.assertEqual(self._step_one_count(PropertyOperator.IS_DATE_BETWEEN), 1)
        # NOT_BETWEEN: "control" is inside the range so it's excluded here, leaving only
        # "missing" -- if "missing" didn't count as not-between, this would read 0.
        self.assertEqual(self._step_one_count(PropertyOperator.IS_DATE_NOT_BETWEEN), 1)

    def test_date_only_bounds_include_the_whole_final_day(self):
        """The form the date picker actually emits.

        Every other test here uses explicit `YYYY-MM-DD HH:MM:SS` fixtures, which never exercise
        the date-only bound the UI produces. Taken literally `<= '2021-06-04'` means midnight, so
        without the end-of-day widening in property.py everything on the final selected day except
        the first instant of it would silently vanish -- the single most likely way for this
        feature to be quietly wrong for users.
        """
        _create_person(distinct_ids=["late_on_final_day"], team=self.team)
        _create_event(
            event="step one",
            distinct_id="late_on_final_day",
            team=self.team,
            timestamp="2021-06-04 18:00:00",
            properties={"engaged_at": "2021-06-04 18:00:00"},
        )
        _create_person(distinct_ids=["day_after"], team=self.team)
        _create_event(
            event="step one",
            distinct_id="day_after",
            team=self.team,
            timestamp="2021-06-05 00:00:01",
            properties={"engaged_at": "2021-06-05 00:00:01"},
        )

        query = FunnelsQuery(
            series=[
                EventsNode(
                    event="step one",
                    properties=[
                        EventPropertyFilter(
                            key="engaged_at",
                            operator=PropertyOperator.IS_DATE_BETWEEN,
                            # date-only, exactly as the picker emits it
                            value=["2021-06-02", "2021-06-04"],
                        )
                    ],
                ),
                EventsNode(event="step two"),
            ],
            dateRange=DateRange(date_from="2021-05-01 00:00:00", date_to="2021-07-01 00:00:00", explicitDate=True),
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        # 18:00 on the final day is inside the range; one second into the next day is not.
        self.assertEqual(results[0]["count"], 1)
