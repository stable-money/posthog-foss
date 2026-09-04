"""
`funnelWindowBoundary` controls whether a funnel's conversion window is allowed to reach past
`date_to`. In "clip" (the default, PostHog's historical behaviour) every funnel event, later
steps included, must fall inside the date range, so someone entering near `date_to` is scored as
a drop-off before their conversion window has actually elapsed; in "extend" only the first step
is bound by the date range, and the remaining steps get their full conversion window even if it
reaches past `date_to`.
"""

from posthog.test.base import APIBaseTest, ClickhouseTestMixin, _create_event, _create_person

from posthog.schema import (
    DateRange,
    EventsNode,
    FunnelConversionWindowTimeUnit,
    FunnelsFilter,
    FunnelsQuery,
    StepOrderValue,
)

from products.product_analytics.backend.hogql_queries.funnels.funnels_query_runner import FunnelsQueryRunner


class TestFunnelWindowBoundary(ClickhouseTestMixin, APIBaseTest):
    maxDiff = None

    def _build_query(
        self,
        *,
        series: list[EventsNode],
        date_from: str,
        date_to: str,
        funnel_window_boundary: str | None = None,
        funnel_order_type: StepOrderValue = StepOrderValue.ORDERED,
        funnel_window_interval: int = 3,
    ) -> FunnelsQuery:
        funnels_filter_kwargs = {
            "funnelOrderType": funnel_order_type,
            "funnelWindowInterval": funnel_window_interval,
            "funnelWindowIntervalUnit": FunnelConversionWindowTimeUnit.DAY,
        }
        if funnel_window_boundary is not None:
            funnels_filter_kwargs["funnelWindowBoundary"] = funnel_window_boundary

        return FunnelsQuery(
            series=series,
            # explicitDate=True keeps date_to exactly as given, instead of padding it to the end
            # of the calendar day, so the boundary math below is exact.
            dateRange=DateRange(date_from=date_from, date_to=date_to, explicitDate=True),
            funnelsFilter=FunnelsFilter(**funnels_filter_kwargs),
        )

    def test_default_matches_clip_boundary_on_a_step_landing_after_date_to(self):
        # Regression guard: nothing about existing funnels should change. A person who enters
        # inside the date range but whose step two lands after date_to (though still inside their
        # 3-day conversion window) is a drop-off under "clip" -- and identically so with no
        # funnelWindowBoundary set at all.
        _create_person(distinct_ids=["p1"], team=self.team)
        _create_event(event="step one", distinct_id="p1", team=self.team, timestamp="2021-06-04 12:00:00")
        _create_event(event="step two", distinct_id="p1", team=self.team, timestamp="2021-06-06 12:00:00")

        series = [EventsNode(event="step one"), EventsNode(event="step two")]
        default_query = self._build_query(series=series, date_from="2021-06-01 00:00:00", date_to="2021-06-05 00:00:00")
        clip_query = self._build_query(
            series=series,
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="clip",
        )

        default_results = FunnelsQueryRunner(query=default_query, team=self.team).calculate().results
        clip_results = FunnelsQueryRunner(query=clip_query, team=self.team).calculate().results

        self.assertEqual(default_results[0]["count"], 1)
        self.assertEqual(default_results[1]["count"], 0)  # drop-off: step two never entered the scan
        self.assertEqual(default_results, clip_results)

    def test_extend_boundary_converts_step_landing_after_date_to(self):
        # Same person, same events as above -- only funnelWindowBoundary flips. Step two is now
        # visible (the scan reaches date_to + conversion window) and is still within the 3-day
        # window from step one, so they convert instead of dropping off.
        _create_person(distinct_ids=["p1"], team=self.team)
        _create_event(event="step one", distinct_id="p1", team=self.team, timestamp="2021-06-04 12:00:00")
        _create_event(event="step two", distinct_id="p1", team=self.team, timestamp="2021-06-06 12:00:00")

        query = self._build_query(
            series=[EventsNode(event="step one"), EventsNode(event="step two")],
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="extend",
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[1]["count"], 1)

    def test_extend_boundary_does_not_admit_entrant_whose_step_one_is_after_date_to(self):
        # The scan is widened, but entry stays pinned to date_to: someone whose only event is
        # after date_to must not appear at step 1 at all, extend or not.
        #
        # "in_range" exists so the funnel still returns rows. Asserting on a funnel that is
        # empty for everyone proves nothing — every step would read 0 if the query were broken
        # outright. With a converter present, a count of 1 means "late" specifically was excluded.
        _create_person(distinct_ids=["late"], team=self.team)
        _create_event(event="step one", distinct_id="late", team=self.team, timestamp="2021-06-06 08:00:00")

        _create_person(distinct_ids=["in_range"], team=self.team)
        _create_event(event="step one", distinct_id="in_range", team=self.team, timestamp="2021-06-02 00:00:00")
        _create_event(event="step two", distinct_id="in_range", team=self.team, timestamp="2021-06-02 06:00:00")

        query = self._build_query(
            series=[EventsNode(event="step one"), EventsNode(event="step two")],
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="extend",
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[1]["count"], 1)

    def test_extend_boundary_still_expires_conversion_window(self):
        # Widening the scan doesn't widen the window: a step two more than one conversion window
        # after step one is still a drop-off, even though the event itself lands inside the
        # widened scan range (before date_to + conversion window = 2021-06-08).
        _create_person(distinct_ids=["p1"], team=self.team)
        _create_event(event="step one", distinct_id="p1", team=self.team, timestamp="2021-06-01 00:00:00")
        # 5 days 10 hours later: past the 3-day window, but still before the widened scan's end.
        _create_event(event="step two", distinct_id="p1", team=self.team, timestamp="2021-06-06 10:00:00")

        query = self._build_query(
            series=[EventsNode(event="step one"), EventsNode(event="step two")],
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="extend",
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[1]["count"], 0)

    def test_extend_boundary_window_anchored_on_step_one_not_date_to(self):
        # Someone entering early in the range gets exactly their own conversion window, not one
        # stretched out to cover the whole scan. Step two lands 3.5 days after step one -- past
        # their 3-day window -- even though it's still comfortably before date_to itself.
        _create_person(distinct_ids=["p1"], team=self.team)
        _create_event(event="step one", distinct_id="p1", team=self.team, timestamp="2021-06-01 00:00:00")
        _create_event(event="step two", distinct_id="p1", team=self.team, timestamp="2021-06-04 12:00:00")

        query = self._build_query(
            series=[EventsNode(event="step one"), EventsNode(event="step two")],
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="extend",
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[1]["count"], 0)

    def test_extend_boundary_strict_order_clamps_entry_and_breaks_on_interrupt(self):
        # Strict order inspects every event, not just funnel steps -- entry still has to be
        # clamped on step_0 (same guarantee as the ordered funnels above), AND an unrelated event
        # landing after date_to, between step one and step two, still correctly breaks the chain.
        _create_person(distinct_ids=["interrupted"], team=self.team)
        _create_event(event="step one", distinct_id="interrupted", team=self.team, timestamp="2021-06-04 12:00:00")
        _create_event(event="random click", distinct_id="interrupted", team=self.team, timestamp="2021-06-06 08:00:00")
        _create_event(event="step two", distinct_id="interrupted", team=self.team, timestamp="2021-06-07 08:00:00")

        _create_person(distinct_ids=["late"], team=self.team)
        _create_event(event="step one", distinct_id="late", team=self.team, timestamp="2021-06-06 08:00:00")
        _create_event(event="step two", distinct_id="late", team=self.team, timestamp="2021-06-06 09:00:00")

        query = self._build_query(
            series=[EventsNode(event="step one"), EventsNode(event="step two")],
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="extend",
            funnel_order_type=StepOrderValue.STRICT,
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        # "late" never counts as an entrant (step_0 clamped to date_to); "interrupted" does, but
        # never reaches step two because "random click" breaks the strict sequence.
        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[1]["count"], 0)

    def test_extend_boundary_unordered_excludes_entrant_whose_earliest_event_is_after_date_to(self):
        # Unordered funnels have no designated first step, so entry can't be clamped on step_0 --
        # it's clamped on min(timestamp) instead, in FunnelUDF._inner_aggregation_query. That's a
        # different code path from the ordered/strict entry clamp above; exercise it separately.
        _create_person(distinct_ids=["valid"], team=self.team)
        _create_event(event="step two", distinct_id="valid", team=self.team, timestamp="2021-06-02 08:00:00")
        _create_event(event="step one", distinct_id="valid", team=self.team, timestamp="2021-06-02 10:00:00")

        _create_person(distinct_ids=["late"], team=self.team)
        _create_event(event="step two", distinct_id="late", team=self.team, timestamp="2021-06-06 08:00:00")
        _create_event(event="step one", distinct_id="late", team=self.team, timestamp="2021-06-06 09:00:00")

        query = self._build_query(
            series=[EventsNode(event="step one"), EventsNode(event="step two")],
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="extend",
            funnel_order_type=StepOrderValue.UNORDERED,
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        # "late"'s earliest matching event (either step) is after date_to, so they're excluded
        # entirely -- not just kept out of step 1, but out of the results altogether.
        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[1]["count"], 1)

    def test_extend_boundary_three_step_funnel_clamps_only_step_zero(self):
        # The entry clamp is ANDed onto step_0's condition only (FunnelEventQuery._get_step_col).
        # A 3-step funnel proves that: step two and step three land after date_to and still
        # count, as long as step one (the entrant) is inside the date range.
        _create_person(distinct_ids=["p1"], team=self.team)
        _create_event(event="step one", distinct_id="p1", team=self.team, timestamp="2021-06-04 12:00:00")
        _create_event(event="step two", distinct_id="p1", team=self.team, timestamp="2021-06-06 08:00:00")
        _create_event(event="step three", distinct_id="p1", team=self.team, timestamp="2021-06-06 20:00:00")

        query = self._build_query(
            series=[EventsNode(event="step one"), EventsNode(event="step two"), EventsNode(event="step three")],
            date_from="2021-06-01 00:00:00",
            date_to="2021-06-05 00:00:00",
            funnel_window_boundary="extend",
        )
        results = FunnelsQueryRunner(query=query, team=self.team).calculate().results

        self.assertEqual(results[0]["count"], 1)
        self.assertEqual(results[1]["count"], 1)
        self.assertEqual(results[2]["count"], 1)
