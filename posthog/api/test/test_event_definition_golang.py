from posthog.test.base import APIBaseTest
from unittest.mock import MagicMock

from parameterized import parameterized

from posthog.api.event_definition_generators.golang import GolangGenerator


class TestGolangGenerator(APIBaseTest):
    """Test the GolangGenerator class directly"""

    def setUp(self):
        super().setUp()
        self.generator = GolangGenerator()

    @parameterized.expand(
        [
            ("snake_case", "downloaded_file", "DownloadedFile"),
            ("kebab_case", "user-signed-up", "UserSignedUp"),
            ("dollar_prefix", "$pageview", "Pageview"),
            ("mixed_case", "API-Request", "APIRequest"),
            ("with_numbers", "test_123", "Test123"),
            ("multiple_underscores", "___test___", "Test"),
            ("empty_string", "", "Event"),
            ("starts_with_number", "123_start", "Event123Start"),
            ("only_numbers", "123456", "Event123456"),
        ]
    )
    def test_to_go_func_name(self, name, event_name, expected_output):
        """Test event name to Go exported identifier conversion"""
        result = self.generator._to_go_func_name(event_name)
        self.assertEqual(
            expected_output,
            result,
            f"{name} failed: Expected '{event_name}' to convert to '{expected_output}', got '{result}'",
        )

    @parameterized.expand(
        [
            ("snake_case", "file_name", "fileName"),
            ("kebab_case", "user-id", "userId"),
            ("underscore_with_abbrev", "api_key", "apiKey"),
            ("already_camelCase", "firstName", "firstname"),
            ("all_caps", "URL", "url"),
            ("with_numbers", "test_123", "test123"),
            ("empty", "", "value"),
            ("whitespace_only", "   ", "value"),
            ("reserved_keywords", "break", "break_"),
            ("reserved_mixed_case", "Func", "func_"),
            ("multiple_parts", "multi part argument", "multiPartArgument"),
            ("multi_part_with_reserved", "this will not break", "thisWillNotBreak"),
            ("trailing_underscore", "name_", "name"),
            ("mixed_separators", "user-id_name", "userIdName"),
            ("leading_underscore", "_private", "private"),
            ("starts_with_number", "123test", "param123test"),
            ("special_chars", "$name@#$value", "nameValue"),
            ("all_lowercase_no_sep", "filename", "filename"),
            ("consecutive_capitals", "HTTPResponse", "httpresponse"),
            ("only_special_chars", "@#$%", "value"),
        ]
    )
    def test_to_go_param_name(self, name, input_name, expected_output):
        """Test property name to Go parameter name conversion (camelCase)"""
        result = self.generator._to_go_param_name(input_name)
        self.assertEqual(
            expected_output,
            result,
            f"{name} failed: Expected '{input_name}' to convert to '{expected_output}', got '{result}'",
        )

    @parameterized.expand(
        [
            ("snake_case", "file_name", "FileName"),
            ("with_numbers", "test_123", "Test123"),
            ("empty", "", "Prop"),
            ("whitespace_only", "   ", "Prop"),
            ("mixed_separators", "user-id_name", "UserIdName"),
            ("leading_underscore", "_private", "Private"),
            ("starts_with_number", "123test", "123test"),
            ("special_chars", "$name@#$value", "NameValue"),
            ("consecutive_capitals", "HTTPResponse", "HTTPResponse"),
            ("only_special_chars", "@#$%", "Prop"),
        ]
    )
    def test_go_pascal_name_conversion(self, name, input_name, expected_output):
        result = self.generator._to_pascal_name(input_name)
        self.assertEqual(
            expected_output,
            result,
            f"{name} failed: Expected '{input_name}' to convert to '{expected_output}', got '{result}'",
        )

    def test_get_unique_name(self):
        """Test collision handling in parameter name generation"""
        used_names: set[str] = set()

        # First usage - no collision
        name1 = self.generator._get_unique_name("fileName", used_names)
        self.assertEqual(name1, "fileName")
        self.assertIn("fileName", used_names)

        # Second usage of same base - should get suffix
        name2 = self.generator._get_unique_name("fileName", used_names)
        self.assertEqual(name2, "fileName2")
        self.assertIn("fileName2", used_names)

        # Third usage
        name3 = self.generator._get_unique_name("fileName", used_names)
        self.assertEqual(name3, "fileName3")

    def test_generate_event_without_properties(self):
        """Test code generation for event without properties"""
        code = self.generator._generate_event_without_properties("simple_click")
        self.assertEqual(
            """// SimpleClickCapture creates a capture for the "simple_click" event.
// This event has no defined schema properties.
func SimpleClickCapture(distinctId string, properties ...posthog.Properties) posthog.Capture {
	props := posthog.Properties{}
	for _, p := range properties {
		for k, v := range p {
			props[k] = v
		}
	}

	return posthog.Capture{
		DistinctId: distinctId,
		Event:      "simple_click",
		Properties: props,
	}
}

// SimpleClickCaptureFromBase creates a posthog.Capture for the "simple_click" event
// starting from an existing base capture. The event name is overridden, and
// any additional properties can be passed via the properties parameter.
func SimpleClickCaptureFromBase(base posthog.Capture, properties ...posthog.Properties) posthog.Capture {
\tprops := posthog.Properties{}
\tfor _, p := range properties {
\t\tfor k, v := range p {
\t\t\tprops[k] = v
\t\t}
\t}

\tbase.Event = "simple_click"
\tif base.Properties == nil {
\t\tbase.Properties = posthog.Properties{}
\t}
\tbase.Properties = base.Properties.Merge(props)

\treturn base
}""",
            code.strip(),
        )

    def test_generate_event_with_properties(self):
        props = [
            self._create_mock_property("file_name", "String", required=True),
            self._create_mock_property("file_size", "Numeric", required=True),
            self._create_mock_property("is_active", "Boolean", required=False),
            self._create_mock_property("created_at", "DateTime", required=True),
            self._create_mock_property("tags", "Array", required=False),
            self._create_mock_property("metadata", "Object", required=False),
        ]

        code = self.generator._generate_event_with_properties("file_uploaded", props)  # type: ignore[arg-type]
        self.assertEqual(
            """// FileUploadedOption configures optional properties for a "file_uploaded" capture.
type FileUploadedOption func(*posthog.Capture)

// FileUploadedWithIsActive sets the "is_active" property on a "file_uploaded" event.
func FileUploadedWithIsActive(isActive bool) FileUploadedOption {
	return func(c *posthog.Capture) {
		if c.Properties == nil {
			c.Properties = posthog.Properties{}
		}
		c.Properties["is_active"] = isActive
	}
}

// FileUploadedWithMetadata sets the "metadata" property on a "file_uploaded" event.
func FileUploadedWithMetadata(metadata map[string]interface{}) FileUploadedOption {
	return func(c *posthog.Capture) {
		if c.Properties == nil {
			c.Properties = posthog.Properties{}
		}
		c.Properties["metadata"] = metadata
	}
}

// FileUploadedWithTags sets the "tags" property on a "file_uploaded" event.
func FileUploadedWithTags(tags []interface{}) FileUploadedOption {
	return func(c *posthog.Capture) {
		if c.Properties == nil {
			c.Properties = posthog.Properties{}
		}
		c.Properties["tags"] = tags
	}
}

// FileUploadedWithExtraProps adds additional properties to a "file_uploaded" event.
func FileUploadedWithExtraProps(props posthog.Properties) FileUploadedOption {
	return func(c *posthog.Capture) {
		if c.Properties == nil {
			c.Properties = posthog.Properties{}
		}
		for k, v := range props {
			c.Properties[k] = v
		}
	}
}

// FileUploadedCapture is a wrapper for the "file_uploaded" event.
// It manages the creation of the `posthog.Capture`. If you need control over this, please make use of
// the FileUploadedCaptureFromBase function.
// Required properties from the schema are explicit parameters; optional properties
// should be passed via FileUploadedWith* option functions.
func FileUploadedCapture(
	distinctId string,
	createdAt time.Time,
	fileName string,
	fileSize float64,
	options ...FileUploadedOption,
) posthog.Capture {
	props := posthog.Properties{
		"created_at": createdAt,
		"file_name": fileName,
		"file_size": fileSize,
	}

	c := posthog.Capture{
		DistinctId: distinctId,
		Event:      "file_uploaded",
		Properties: props,
	}

	for _, opt := range options {
		opt(&c)
	}

	return c
}

// FileUploadedCaptureFromBase creates a posthog.Capture for the "file_uploaded" event
// starting from an existing base capture. The event name is overridden, and
// required properties from the schema are merged on top. Optional properties
// should be passed via FileUploadedWith* option functions.
func FileUploadedCaptureFromBase(
	base posthog.Capture,
	createdAt time.Time,
	fileName string,
	fileSize float64,
	options ...FileUploadedOption,
) posthog.Capture {
	props := posthog.Properties{
		"created_at": createdAt,
		"file_name": fileName,
		"file_size": fileSize,
	}

	base.Event = "file_uploaded"
	if base.Properties == nil {
		base.Properties = posthog.Properties{}
	}
	base.Properties = base.Properties.Merge(props)

	for _, opt := range options {
		opt(&base)
	}

	return base
}""",
            code.strip(),
        )

    def test_generate_event_with_quoted_and_escaped_properties(self):
        props = [
            self._create_mock_property("esc'ap\"eing", "String", required=True),
        ]

        code = self.generator._generate_event_with_properties("creative_naming", props)  # type: ignore[arg-type]
        self.assertEqual(
            """// CreativeNamingOption configures optional properties for a "creative_naming" capture.
type CreativeNamingOption func(*posthog.Capture)

// CreativeNamingWithExtraProps adds additional properties to a "creative_naming" event.
func CreativeNamingWithExtraProps(props posthog.Properties) CreativeNamingOption {
	return func(c *posthog.Capture) {
		if c.Properties == nil {
			c.Properties = posthog.Properties{}
		}
		for k, v := range props {
			c.Properties[k] = v
		}
	}
}

// CreativeNamingCapture is a wrapper for the "creative_naming" event.
// It manages the creation of the `posthog.Capture`. If you need control over this, please make use of
// the CreativeNamingCaptureFromBase function.
// Required properties from the schema are explicit parameters; optional properties
// should be passed via CreativeNamingWith* option functions.
func CreativeNamingCapture(
	distinctId string,
	escApEing string,
	options ...CreativeNamingOption,
) posthog.Capture {
	props := posthog.Properties{
		"esc'ap\\"eing": escApEing,
	}

	c := posthog.Capture{
		DistinctId: distinctId,
		Event:      "creative_naming",
		Properties: props,
	}

	for _, opt := range options {
		opt(&c)
	}

	return c
}

// CreativeNamingCaptureFromBase creates a posthog.Capture for the "creative_naming" event
// starting from an existing base capture. The event name is overridden, and
// required properties from the schema are merged on top. Optional properties
// should be passed via CreativeNamingWith* option functions.
func CreativeNamingCaptureFromBase(
	base posthog.Capture,
	escApEing string,
	options ...CreativeNamingOption,
) posthog.Capture {
	props := posthog.Properties{
		"esc'ap\\"eing": escApEing,
	}

	base.Event = "creative_naming"
	if base.Properties == nil {
		base.Properties = posthog.Properties{}
	}
	base.Properties = base.Properties.Merge(props)

	for _, opt := range options {
		opt(&base)
	}

	return base
}""",
            code.strip(),
        )

    def test_full_generation_output(self):
        """
        This test 'globally' checks the output of the `generate` function.
        This is only done globally as the 'critical' parts have already been covered in other tests here.
        """
        event = MagicMock()
        event.id = "1"
        event.name = "simple_event"
        schema_map = {
            "1": [
                self._create_mock_property("user_id", "String", required=True),
                self._create_mock_property("count", "Numeric", required=False),
            ]
        }

        code = self.generator.generate([event], schema_map)  # type: ignore[arg-type]

        # Check header / imports
        self.assertIn("// Code generated by PostHog - DO NOT EDIT", code)
        self.assertIn("package typed", code)
        self.assertIn('"github.com/posthog/posthog-go"', code)
        self.assertNotIn('"time"', code, "time should not be imported as we do not have a DateTime property.")

        # Check event code
        self.assertIn("SimpleEventOption", code)
        self.assertIn("SimpleEventWithCount", code)
        self.assertIn("SimpleEventWithExtraProps", code)
        self.assertIn("SimpleEventCapture", code)
        self.assertIn("SimpleEventCaptureFromBase", code)

        # Check presence of usage guide
        self.assertIn("// USAGE GUIDE", code)

    def test_generate_event_with_optional_in_types(self):
        props = [
            self._create_mock_property("file_name", "String", required=True),
            self._create_mock_property("file_size", "Numeric", required=True, is_optional_in_types=True),
            self._create_mock_property("label", "String", required=False),
        ]

        code = self.generator._generate_event_with_properties("file_uploaded", props)  # type: ignore[arg-type]

        # file_size should become an option function (not a required param)
        self.assertIn("FileUploadedWithFileSize(fileSize float64)", code)
        # file_name should still be a required param
        self.assertIn("fileName string", code)
        # file_size should NOT appear as a required param in the capture function signature
        self.assertNotIn("fileSize float64,\n\toptions", code)

    def _create_mock_property(
        self, name: str, property_type: str, required: bool = False, is_optional_in_types: bool = False
    ) -> MagicMock:
        """Create a mock SchemaPropertyGroupProperty for testing"""
        prop = MagicMock()
        prop.name = name
        prop.property_type = property_type
        prop.is_required = required
        prop.is_optional_in_types = is_optional_in_types
        return prop
