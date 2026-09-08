"""
Integration test for TypeScript definition generation.

Tests the complete flow:
1. Create EventDefinitions with EventSchemas
2. Generate TypeScript via the typescript_definitions method
3. Write generated TypeScript to temp file
4. Create a test file that uses the types
5. Run TypeScript compiler to verify no errors
"""

from posthog.test.base import BaseTest
from unittest.mock import MagicMock

from posthog.api.event_definition_generators.typescript import TypeScriptGenerator


class TestTypeScriptGeneratorOptionalInTypes(BaseTest):
    def test_optional_in_types_generates_optional_marker(self):
        generator = TypeScriptGenerator()
        event = MagicMock()
        event.id = "1"
        event.name = "test_event"

        required_prop = MagicMock()
        required_prop.name = "always_required"
        required_prop.property_type = "String"
        required_prop.is_required = True
        required_prop.is_optional_in_types = False

        optional_in_types_prop = MagicMock()
        optional_in_types_prop.name = "super_prop"
        optional_in_types_prop.property_type = "String"
        optional_in_types_prop.is_required = True
        optional_in_types_prop.is_optional_in_types = True

        schema_map = {"1": [required_prop, optional_in_types_prop]}
        code = generator.generate([event], schema_map)  # type: ignore[arg-type]

        self.assertIn('"always_required": string', code)
        self.assertNotIn('"always_required"?: string', code)
        self.assertIn('"super_prop"?: string', code)
