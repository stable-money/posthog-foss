"""
`funnelHoldConstantBreakdown` holds the breakdown property constant across every step instead of
splitting the funnel by it: a person only converts if one single value carries them through the
whole funnel ("viewed product X then bought product X", never "viewed X then bought Y"). The
result is one funnel rather than one per value, and a person who could convert under several
values is still counted once. Mixpanel calls this "hold property constant".

The per-step chaining already exists upstream as `all_events` breakdown attribution; what this
adds is collapsing those per-value funnels back into a single, de-duplicated one.
"""

from posthog.test.base import APIBaseTest, ClickhouseTestMixin, _create_event, _create_person

from rest_framework.exceptions import ValidationError

from posthog.schema import (
    Breakdown,
    BreakdownAttributionType,
    BreakdownFilter,
    BreakdownType,
    DateRange,
    EventsNode,
    FunnelsFilter,
    FunnelsQuery,
    FunnelVizType,
)

from products.product_analytics.backend.hogql_queries.funnels.funnels_query_runner import FunnelsQueryRunner

DATE_FROM = "2021-06-01 00:00:00"
DATE_TO = "2021-06-10 00:00:00"


class TestFunnelHoldConstant(ClickhouseTestMixin, APIBaseTest):
    maxDiff = None

    def _journey(self, person: str, events: list[tuple[str, str | None, str]]) -> None:
        _create_person(distinct_ids=[person], team=self.team)
        for event, product, timestamp in events:
            _create_event(
                event=event,
                distinct_id=person,
                team=self.team,
                timestamp=timestamp,
                properties={} if product is None else {"product": product},
            )

    def _seed(self) -> None:
        # Same value at every step -- converts either way.
        self._journey("alice", [("view", "A", "2021-06-02 10:00:00"), ("buy", "A", "2021-06-02 11:00:00")])
        # Switched value between the steps -- converts on a plain funnel, not on a held-constant one.
        self._journey("bob", [("view", "A", "2021-06-02 10:00:00"), ("buy", "B", "2021-06-02 11:00:00")])
        # Two values, only one of which carries them through. Must be counted once, not twice.
        self._journey(
            "carol",
            [
                ("view", "A", "2021-06-02 10:00:00"),
                ("view", "B", "2021-06-02 11:00:00"),
                ("buy", "B", "2021-06-02 12:00:00"),
            ],
        )
        # Dropped off at step one.
        self._journey("dave", [("view", "A", "2021-06-02 10:00:00")])
        # Never carried the property at all -- has no value to hold, so must not appear.
        self._journey("erin", [("view", None, "2021-06-02 10:00:00"), ("buy", None, "2021-06-02 11:00:00")])

    def _query(self, *, hold: bool = False, **filter_kwargs) -> FunnelsQuery:
        return FunnelsQuery(
            series=[EventsNode(event="view"), EventsNode(event="buy")],
            dateRange=DateRange(date_from=DATE_FROM, date_to=DATE_TO),
            breakdownFilter=BreakdownFilter(breakdown="product", breakdown_type=BreakdownType.EVENT),
            funnelsFilter=FunnelsFilter(funnelHoldConstantBreakdown=hold, **filter_kwargs),
        )

    def _counts(self, query: FunnelsQuery) -> list[int]:
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results
        return [step["count"] for step in results]

    def test_holds_the_value_constant_across_steps(self):
        self._seed()

        # alice (A) and carol (B) each did both steps under one value. bob switched values, dave
        # never reached step two, erin never had the property.
        self.assertEqual(self._counts(self._query(hold=True)), [4, 2])

    def test_counts_a_person_once_even_when_several_values_could_carry_them(self):
        # carol converts under B and also enters under A. A breakdown funnel counts her in both
        # buckets; held constant she is one person.
        self._seed()

        held = self._counts(self._query(hold=True))

        split = (
            FunnelsQueryRunner(
                query=self._query(breakdownAttributionType=BreakdownAttributionType.ALL_EVENTS),
                team=self.team,
            )
            .calculate()
            .results
        )
        entered_per_value = sum(funnel[0]["count"] for funnel in split)

        self.assertEqual(held[0], 4)
        # The per-value funnels double-count carol (and admit erin under an empty value).
        self.assertEqual(entered_per_value, 6)

    def test_a_person_without_the_property_is_excluded(self):
        # erin did both steps but never carried `product`. Upstream folds a missing property into
        # an empty-string value, which would read as "converted while the property stayed the same".
        self._journey("erin", [("view", None, "2021-06-02 10:00:00"), ("buy", None, "2021-06-02 11:00:00")])

        # No row survives, and a funnel with no rows formats as [] (upstream's existing behaviour
        # for an empty funnel, not something this flag introduces).
        self.assertEqual(self._counts(self._query(hold=True)), [])

        # ...while a plain funnel still counts her, so this is the flag's doing and not the seed's.
        plain = self._query()
        plain.breakdownFilter = None
        self.assertEqual(self._counts(plain), [1, 1])

    def test_off_by_default_leaves_the_funnel_alone(self):
        # Regression guard: nothing about existing funnels changes when the flag is not set.
        self._seed()

        default = FunnelsQueryRunner(query=self._query(), team=self.team).calculate().results
        explicit_off = FunnelsQueryRunner(query=self._query(hold=False), team=self.team).calculate().results

        # A breakdown funnel returns a list of funnels; a held-constant one returns a single funnel.
        self.assertIsInstance(default[0], list)
        self.assertEqual(default, explicit_off)

    def test_rejects_breakdowns_that_cannot_be_held(self):
        for label, breakdown_filter in (
            ("no breakdown", None),
            (
                "cohort",
                BreakdownFilter(breakdown=[1], breakdown_type=BreakdownType.COHORT),
            ),
            (
                "multi-property",
                BreakdownFilter(
                    breakdowns=[
                        Breakdown(property="product", type=BreakdownType.EVENT),
                        Breakdown(property="plan", type=BreakdownType.EVENT),
                    ]
                ),
            ),
        ):
            with self.subTest(label), self.assertRaises(ValidationError):
                query = self._query(hold=True)
                query.breakdownFilter = breakdown_filter
                FunnelsQueryRunner(query=query, team=self.team).calculate()

    def test_rejects_visualisations_that_never_reach_the_collapse(self):
        # FunnelTrendsUDF builds its own inner aggregation, so the flag would quietly do nothing.
        for viz_type in (FunnelVizType.TRENDS, FunnelVizType.TIME_TO_CONVERT):
            with self.subTest(viz_type), self.assertRaises(ValidationError):
                FunnelsQueryRunner(query=self._query(hold=True, funnelVizType=viz_type), team=self.team).calculate()
