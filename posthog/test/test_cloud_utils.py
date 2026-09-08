import pytest
from posthog.test.base import BaseTest

from posthog.cloud_utils import TEST_clear_instance_license_cache, get_cached_instance_license


class TestCloudUtils(BaseTest):
    @pytest.mark.ee
    def test_get_cached_instance_license_returns_correctly(self):
        TEST_clear_instance_license_cache()
        assert get_cached_instance_license() is None
