import pytest
from unittest.mock import patch

from posthog.clickhouse.materialized_columns_registry import get_materialized_columns, parse_column_comment


def _run(columns, indices):
    """Drive the registry with fixed ClickHouse rows.

    CachedFunction defaults use_cache to `not TEST`, so cases do not leak into each other.
    """

    def fake(sql, params):
        return columns if "system.columns" in sql else indices

    with patch("posthog.clickhouse.client.sync_execute", side_effect=fake):
        return get_materialized_columns("events")


class TestParseColumnComment:
    @pytest.mark.parametrize(
        "comment,expected",
        [
            # The two-part form carries most long-lived columns on a real cluster. Accepting
            # only the three-part form drops them and silently stops property substitution.
            ("column_materializer::$group_0", ("properties", "$group_0", False)),
            ("column_materializer::person_properties::email", ("person_properties", "email", False)),
            ("column_materializer::properties::plan::disabled", ("properties", "plan", True)),
            ("column_materializer::elements_chain::tag", ("elements_chain", "tag", False)),
        ],
    )
    def test_parses_every_shape_a_cluster_carries(self, comment, expected):
        assert parse_column_comment(comment) == expected

    @pytest.mark.parametrize(
        "comment",
        ["", "column_materializer", "something_else::properties::plan", "column_materializer::properties::p::retired"],
    )
    def test_rejects_unknown_shapes(self, comment):
        # Raising matters: a silently skipped column is indistinguishable from one that
        # does not exist, and the caller would just produce slower queries.
        with pytest.raises(ValueError):
            parse_column_comment(comment)


class TestGetMaterializedColumns:
    def test_describes_the_column_it_finds(self):
        result = _run(
            [("mat_plan", "column_materializer::properties::plan", "Nullable(String)")],
            [("minmax", "mat_plan")],
        )
        column = result[("plan", "properties")]
        assert (column.name, column.type, column.is_nullable, column.has_minmax_index) == (
            "mat_plan",
            "Nullable(String)",
            True,
            True,
        )

    def test_matches_indices_by_expression_not_name(self):
        # The index is named for the property while the column is named mat_$ai_trace_id,
        # so keying off the index name finds nothing and every bloom filter flag is lost.
        result = _run(
            [("mat_$ai_trace_id", "column_materializer::properties::$ai_trace_id", "String")],
            [("bloom_filter", "`mat_$ai_trace_id`")],
        )
        assert result[("$ai_trace_id", "properties")].has_bloom_filter_index is True

    @pytest.mark.parametrize(
        "index_row,expected_flag",
        [
            (("bloom_filter", "lower(`mat_email`)"), "has_bloom_filter_lower_index"),
            (("ngrambf_v1", "lower(mat_email)"), "has_ngram_lower_index"),
            (("bloom_filter", "`mat_email`"), "has_bloom_filter_index"),
            (("minmax", "mat_email"), "has_minmax_index"),
        ],
    )
    def test_index_kind_and_lowering_select_the_right_flag(self, index_row, expected_flag):
        result = _run([("mat_email", "column_materializer::properties::email", "String")], [index_row])
        column = result[("email", "properties")]
        assert getattr(column, expected_flag) is True
        assert (
            sum(
                [
                    column.has_minmax_index,
                    column.has_bloom_filter_index,
                    column.has_ngram_lower_index,
                    column.has_bloom_filter_lower_index,
                ]
            )
            == 1
        )

    def test_ignores_indices_that_are_not_on_a_column(self):
        # A properties-group index belongs to no materialized column.
        result = _run(
            [("mat_email", "column_materializer::properties::email", "String")],
            [("bloom_filter", "mapKeys(properties_group_custom)")],
        )
        assert result[("email", "properties")].has_bloom_filter_index is False

    def test_excludes_disabled_and_elements_chain(self):
        result = _run(
            [
                ("mat_a", "column_materializer::properties::a", "String"),
                ("mat_b", "column_materializer::properties::b::disabled", "String"),
                ("mat_c", "column_materializer::elements_chain::c", "String"),
            ],
            [],
        )
        # A disabled column still exists physically, but must never be substituted in.
        assert set(result) == {("a", "properties")}

    def test_skips_the_index_query_when_there_are_no_columns(self):
        calls = []

        def fake(sql, params):
            calls.append(sql)
            return []

        with patch("posthog.clickhouse.client.sync_execute", side_effect=fake):
            assert get_materialized_columns("events") == {}
        # This sits in the HogQL compile path, so a wasted round trip is paid per query.
        assert len(calls) == 1
