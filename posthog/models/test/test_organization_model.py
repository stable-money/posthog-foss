from datetime import datetime, timedelta

from posthog.test.base import BaseTest
from unittest import mock
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone

from parameterized import parameterized

from posthog.models import Organization, OrganizationInvite
from posthog.models.organization import BillingPeriod, OrganizationMembership
from posthog.models.user import User
from posthog.organization_caching import (
    get_cached_organization,
    get_cached_organization_membership,
    get_cached_organization_memberships,
)
from posthog.plugins.test.mock import mocked_plugin_requests_get
from posthog.plugins.test.plugin_archives import HELLO_WORLD_PLUGIN_GITHUB_ZIP
from posthog.redis import get_client

from products.cdp.backend.models.plugin import Plugin


class TestOrganization(BaseTest):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_organization_active_invites(self):
        self.assertEqual(self.organization.invites.count(), 0)
        self.assertEqual(self.organization.active_invites.count(), 0)

        OrganizationInvite.objects.create(organization=self.organization)
        self.assertEqual(self.organization.invites.count(), 1)
        self.assertEqual(self.organization.active_invites.count(), 1)

        expired_invite = OrganizationInvite.objects.create(organization=self.organization)
        OrganizationInvite.objects.filter(id=expired_invite.id).update(created_at=timezone.now() - timedelta(hours=73))
        self.assertEqual(self.organization.invites.count(), 2)
        self.assertEqual(self.organization.active_invites.count(), 1)

    @mock.patch("posthog.plugins.utils.requests.get", side_effect=mocked_plugin_requests_get)
    def test_plugins_are_preinstalled_on_self_hosted(self, mock_get):
        with self.is_cloud(False):
            with self.settings(PLUGINS_PREINSTALLED_URLS=["https://github.com/PostHog/helloworldplugin/"]):
                new_org, _, _ = Organization.objects.bootstrap(
                    self.user,
                    plugins_access_level=Organization.PluginsAccessLevel.INSTALL,
                )

        self.assertEqual(Plugin.objects.filter(organization=new_org, is_preinstalled=True).count(), 1)
        self.assertEqual(
            Plugin.objects.filter(organization=new_org, is_preinstalled=True).get().name,
            "helloworldplugin",
        )
        self.assertEqual(mock_get.call_count, 2)
        mock_get.assert_any_call(
            f"https://github.com/PostHog/helloworldplugin/archive/{HELLO_WORLD_PLUGIN_GITHUB_ZIP[0]}.zip",
            headers={},
        )

    @mock.patch("posthog.plugins.utils.requests.get", side_effect=mocked_plugin_requests_get)
    def test_plugins_are_not_preinstalled_on_cloud(self, mock_get):
        with self.is_cloud(True):
            with self.settings(PLUGINS_PREINSTALLED_URLS=["https://github.com/PostHog/helloworldplugin/"]):
                new_org, _, _ = Organization.objects.bootstrap(
                    self.user,
                    plugins_access_level=Organization.PluginsAccessLevel.INSTALL,
                )

        self.assertEqual(Plugin.objects.filter(organization=new_org, is_preinstalled=True).count(), 0)
        self.assertEqual(mock_get.call_count, 0)

    def test_plugins_access_level_is_determined_based_on_realm(self):
        with self.is_cloud(True):
            new_org, _, _ = Organization.objects.bootstrap(self.user)
            assert new_org.plugins_access_level == Organization.PluginsAccessLevel.CONFIG

        with self.is_cloud(False):
            new_org, _, _ = Organization.objects.bootstrap(self.user)
            assert new_org.plugins_access_level == Organization.PluginsAccessLevel.ROOT

    def test_default_anonymize_ips_based_on_deployment(self):
        # EU deployment should default to True
        with self.settings(CLOUD_DEPLOYMENT="EU"):
            eu_org, _, _ = Organization.objects.bootstrap(self.user, name="EU Org")
            self.assertTrue(eu_org.default_anonymize_ips)

        # US deployment should default to False
        with self.settings(CLOUD_DEPLOYMENT="US"):
            us_org, _, _ = Organization.objects.bootstrap(self.user, name="US Org")
            self.assertFalse(us_org.default_anonymize_ips)

        # No deployment setting should default to False
        with self.settings(CLOUD_DEPLOYMENT=None):
            no_deployment_org, _, _ = Organization.objects.bootstrap(self.user, name="No Deployment Org")
            self.assertFalse(no_deployment_org.default_anonymize_ips)

        # Explicit value should override deployment setting
        with self.settings(CLOUD_DEPLOYMENT="EU"):
            explicit_org, _, _ = Organization.objects.bootstrap(
                self.user, name="Explicit Org", default_anonymize_ips=False
            )
            self.assertFalse(explicit_org.default_anonymize_ips)

    @parameterized.expand(
        [
            ("eu_defaults_to_opted_out", "EU", None, False),
            ("us_defaults_to_opted_in", "US", None, True),
            ("unset_deployment_defaults_to_opted_in", None, None, True),
            ("explicit_value_overrides_eu_default", "EU", True, True),
        ]
    )
    def test_default_is_ai_training_opted_in_based_on_deployment(
        self, _name, cloud_deployment, explicit_value, expected
    ):
        with self.settings(CLOUD_DEPLOYMENT=cloud_deployment):
            extra_kwargs = {} if explicit_value is None else {"is_ai_training_opted_in": explicit_value}
            org, _, _ = Organization.objects.bootstrap(self.user, name=_name, **extra_kwargs)
            self.assertEqual(org.is_ai_training_opted_in, expected)

    def test_update_available_product_features_ignored_if_usage_info_exists(self):
        with self.is_cloud(False):
            new_org, _, _ = Organization.objects.bootstrap(self.user)

            new_org.available_product_features = [{"key": "test1", "name": "test1"}, {"key": "test2", "name": "test2"}]
            new_org.update_available_product_features()
            assert new_org.available_product_features == []

            new_org.available_product_features = [{"key": "test1", "name": "test1"}, {"key": "test2", "name": "test2"}]
            new_org.usage = {"events": {"usage": 1000, "limit": None}}
            new_org.update_available_product_features()
            assert new_org.available_product_features == [
                {"key": "test1", "name": "test1"},
                {"key": "test2", "name": "test2"},
            ]

    @parameterized.expand(
        [
            ("no_features", None, "free"),
            ("empty_features", [], "free"),
            ("unknown_feature_treated_as_paid", [{"key": "made_up_feature"}], "paid"),
            ("scale_feature_present", [{"key": "recordings_file_export"}], "paid"),
            ("multiple_scale_features", [{"key": "zapier"}, {"key": "group_analytics"}], "paid"),
            (
                "enterprise_only_feature_present",
                [{"key": "recordings_file_export"}, {"key": "role_based_access"}],
                "enterprise",
            ),
            ("access_control_flags_enterprise", [{"key": "access_control"}], "enterprise"),
            ("saml_flags_enterprise", [{"key": "saml"}], "enterprise"),
            ("scim_flags_enterprise", [{"key": "scim"}], "enterprise"),
            ("sso_enforcement_flags_enterprise", [{"key": "sso_enforcement"}], "enterprise"),
            ("role_based_access_flags_enterprise", [{"key": "role_based_access"}], "enterprise"),
            ("malformed_entries_ignored", [None, {}, {"key": None}], "free"),
        ]
    )
    def test_get_plan_tier(self, _name, available_product_features, expected_tier):
        self.organization.available_product_features = available_product_features
        self.organization.save()
        self.assertEqual(self.organization.get_plan_tier(), expected_tier)

    def test_session_age_caching(self):
        # Test caching when session_cookie_age is set
        self.organization.session_cookie_age = 3600
        self.organization.save()
        self.assertEqual(cache.get(f"org_session_age:{self.organization.id}"), 3600)

        # Test cache deletion when session_cookie_age is set to None
        self.organization.session_cookie_age = None
        self.organization.save()
        self.assertIsNone(cache.get(f"org_session_age:{self.organization.id}"))

        # Test cache update when session_cookie_age changes
        self.organization.session_cookie_age = 7200
        self.organization.save()
        self.assertEqual(cache.get(f"org_session_age:{self.organization.id}"), 7200)

    def test_access_cache_reuses_organization_and_membership_details(self):
        with self.settings(ORGANIZATION_ACCESS_CACHE_ENABLED=True):
            with self.assertNumQueries(1):
                membership = get_cached_organization_membership(self.organization.id, self.user)
            assert membership is not None
            assert membership.organization == self.organization
            assert membership.user == self.user

            with self.assertNumQueries(1):
                assert get_cached_organization_memberships(self.user)[0].organization == self.organization

            with self.assertNumQueries(0):
                assert get_cached_organization(self.organization.id) == self.organization
                assert get_cached_organization_membership(self.organization.id, self.user) == membership
                assert get_cached_organization_memberships(self.user)[0] == membership

    def test_access_cache_is_invalidated_when_organization_or_membership_changes(self):
        with self.settings(ORGANIZATION_ACCESS_CACHE_ENABLED=True):
            membership = get_cached_organization_membership(self.organization.id, self.user)
            assert membership is not None
            get_cached_organization_memberships(self.user)

            membership.level = OrganizationMembership.Level.ADMIN
            with self.captureOnCommitCallbacks(execute=True):
                membership.save()
            updated_membership = get_cached_organization_membership(self.organization.id, self.user)
            assert updated_membership is not None
            assert updated_membership.level == OrganizationMembership.Level.ADMIN
            assert get_cached_organization_memberships(self.user)[0].level == OrganizationMembership.Level.ADMIN

            self.organization.name = "Updated organization"
            self.organization.save()
            updated_organization = get_cached_organization(self.organization.id)
            assert updated_organization is not None
            assert updated_organization.name == "Updated organization"

            with self.captureOnCommitCallbacks(execute=True):
                membership.delete()
            assert get_cached_organization_membership(self.organization.id, self.user) is None
            assert get_cached_organization_memberships(self.user) == []

    def test_access_cache_is_invalidated_when_membership_is_created(self):
        with self.settings(ORGANIZATION_ACCESS_CACHE_ENABLED=True):
            new_user = User.objects.create_user(
                email="cache-membership@example.com", password="password", first_name="Cache"
            )

            # Cache both the missing individual membership and the user's empty membership list.
            assert get_cached_organization_membership(self.organization.id, new_user) is None
            assert get_cached_organization_memberships(new_user) == []

            with self.captureOnCommitCallbacks(execute=True):
                OrganizationMembership.objects.create(organization=self.organization, user=new_user)

            membership = get_cached_organization_membership(self.organization.id, new_user)
            assert membership is not None
            assert membership.user_id == new_user.id
            assert [item.id for item in get_cached_organization_memberships(new_user)] == [membership.id]

    @parameterized.expand(
        [
            ("valid_period", {"period": ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z"]}, True),
            (
                "valid_period_with_other_data",
                {"period": ["2024-01-01T00:00:00Z", "2024-02-01T00:00:00Z"], "events": {"usage": 1000}},
                True,
            ),
            ("no_usage", None, False),
            ("empty_usage", {}, False),
            ("no_period_key", {"events": {"usage": 1000}}, False),
            ("period_none", {"period": None}, False),
            ("period_empty_list", {"period": []}, False),
            ("period_one_element", {"period": ["2024-01-01T00:00:00Z"]}, False),
            ("period_invalid_date_format", {"period": ["invalid", "2024-02-01T00:00:00Z"]}, False),
            ("period_non_iso_format", {"period": ["01/01/2024", "02/01/2024"]}, False),
        ]
    )
    def test_current_billing_period(self, name, usage_data, should_return_period):
        self.organization.usage = usage_data
        self.organization.save()

        result = self.organization.current_billing_period

        if should_return_period:
            self.assertIsNotNone(result)
            assert result is not None  # Type narrowing for mypy
            self.assertIsInstance(result, BillingPeriod)
            self.assertIsInstance(result.start, datetime)
            self.assertIsInstance(result.end, datetime)
            self.assertLess(result.start, result.end)
        else:
            self.assertIsNone(result)

    def test_is_active_change_invalidates_llm_gateway_quota_cache(self):
        gateway_redis_url = "redis://llm-gateway-redis-org-active-test/"
        second_team = self.organization.teams.create(name="Second Team", api_token="second_token")
        other_organization = Organization.objects.create(name="Other Org")
        other_team = other_organization.teams.create(name="Other Team", api_token="other_token")

        billing_keys = [
            f"quota:code_usage_billing:team:{self.team.id}",
            f"quota:code_usage_billing:team:{second_team.id}",
        ]
        generation_keys = [
            f"quota:generation:team:{self.team.id}",
            f"quota:generation:team:{second_team.id}",
        ]
        other_generation_key = f"quota:generation:team:{other_team.id}"

        with self.settings(LLM_GATEWAY_REDIS_URL=gateway_redis_url):
            gateway_redis = get_client(gateway_redis_url)
            gateway_redis.mset(dict.fromkeys(billing_keys, "stale"))
            gateway_redis.set(other_generation_key, 4)

            with self.captureOnCommitCallbacks(execute=True):
                self.organization.is_active = False
                self.organization.save()

            assert gateway_redis.mget(billing_keys) == [None] * len(billing_keys)
            assert gateway_redis.mget(generation_keys) == [b"1"] * len(generation_keys)
            assert gateway_redis.get(other_generation_key) == b"4"
            gateway_redis.delete(other_generation_key)


class TestOrganizationMembership(BaseTest):
    @patch("posthoganalytics.capture")
    def test_event_sent_when_membership_level_changed(
        self,
        mock_capture,
    ):
        user = self._create_user("user1")
        organization = Organization.objects.create(name="Test Org")
        membership = OrganizationMembership.objects.create(user=user, organization=organization, level=1)
        mock_capture.assert_not_called()
        # change the level
        membership.level = 15
        membership.save()
        # check that the event was sent
        mock_capture.assert_called_once_with(
            event="membership level changed",
            distinct_id=user.distinct_id,
            properties={"new_level": 15, "previous_level": 1, "$set": mock.ANY},
            groups=mock.ANY,
        )
