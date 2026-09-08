import pytest
from posthog.test.base import BaseTest, cleanup_materialized_columns

from posthog.clickhouse.materialized_columns_creation import get_minmax_index_name, materialize
from posthog.clickhouse.materialized_columns_registry import get_materialized_columns


class TestMaterialize(BaseTest):
    def tearDown(self):
        cleanup_materialized_columns()
        super().tearDown()

    def test_column_name_is_a_pure_function_of_the_property(self):
        # The name lands in generated SQL. A disambiguating suffix would make every query
        # that reads the column unrepeatable, and no snapshot of one could ever match.
        first = materialize("events", "naming_probe")
        cleanup_materialized_columns()
        second = materialize("events", "naming_probe")

        assert first.name == "mat_naming_probe"
        assert second.name == first.name

    def test_materializing_the_same_property_twice_returns_the_same_column(self):
        first = materialize("events", "idempotent_probe", create_minmax_index=True)
        second = materialize("events", "idempotent_probe")

        assert second.name == first.name
        assert len([c for c in get_materialized_columns("events").values() if c.name == first.name]) == 1

    def test_materializing_with_a_different_nullability_is_refused(self):
        materialize("events", "shape_probe", is_nullable=False)

        with pytest.raises(ValueError, match="is_nullable"):
            materialize("events", "shape_probe", is_nullable=True)

    def test_the_registry_finds_the_index_that_was_just_created(self):
        column = materialize("events", "index_probe", create_minmax_index=True)

        found = get_materialized_columns("events")[("index_probe", "properties")]
        assert found.name == column.name
        assert found.has_minmax_index
        assert get_minmax_index_name(column.name) == f"minmax_{column.name}"
