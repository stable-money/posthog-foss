"""Test bases for the features a licence used to unlock.

Upstream's version created a `License` row and let the organization read its features off
it. This build has no licence model, and `Organization.update_available_product_features`
therefore grants nothing. These bases hand the features over directly instead: the tests
that use them are checking what a feature does, not how it was paid for.
"""

from posthog.test.base import APIBaseTest

from posthog.constants import AvailableFeature
from posthog.models import Organization

LICENSE_REQUIRED_MESSAGE = (
    "This feature is part of the premium PostHog offering. Self-hosted licenses are no longer "
    "available for purchase. Please contact sales@posthog.com to discuss options."
)


# What an enterprise licence unlocked. Not every feature the enum names: a test that asserts
# a viewer cannot edit is asserting about a feature set, so granting more than the plan did
# changes the answer.
ENTERPRISE_FEATURES: list[AvailableFeature] = [
    AvailableFeature.ZAPIER,
    AvailableFeature.ORGANIZATIONS_PROJECTS,
    AvailableFeature.SOCIAL_SSO,
    AvailableFeature.INGESTION_TAXONOMY,
    AvailableFeature.PATHS_ADVANCED,
    AvailableFeature.CORRELATION_ANALYSIS,
    AvailableFeature.GROUP_ANALYTICS,
    AvailableFeature.BEHAVIORAL_COHORT_FILTERING,
    AvailableFeature.WHITE_LABELLING,
    AvailableFeature.SUBSCRIPTIONS,
    AvailableFeature.APP_METRICS,
    AvailableFeature.RECORDINGS_PLAYLISTS,
    AvailableFeature.RECORDINGS_FILE_EXPORT,
    AvailableFeature.RECORDINGS_PERFORMANCE,
    AvailableFeature.HIGH_FREQUENCY_ALERTS,
    AvailableFeature.REAL_TIME_ALERTS,
    AvailableFeature.ACCESS_CONTROL,
    AvailableFeature.SAML,
    AvailableFeature.SCIM,
    AvailableFeature.SSO_ENFORCEMENT,
    AvailableFeature.ROLE_BASED_ACCESS,
]


def grant_all_product_features(organization: Organization) -> None:
    """Give the organization the features an enterprise licence used to unlock."""
    organization.available_product_features = [
        {"key": feature.value, "name": feature.value.replace("_", " ").capitalize()} for feature in ENTERPRISE_FEATURES
    ]
    organization.save()


class LicensedTestMixin:
    # Kept so subclasses that set them still read naturally. Nothing here reads a key or a
    # plan any more; a class that wants the features gets all of them.
    CONFIG_LICENSE_KEY: str | None = "12345::67890"
    CONFIG_LICENSE_PLAN: str | None = "enterprise"
    CONFIG_SYNC_ORGANIZATION_FEATURES_ON_SETUP: bool = True
    CONFIG_FORCE_ACCESS_CONTROL_ON_SETUP: bool = False

    def license_required_response(self, message: str = LICENSE_REQUIRED_MESSAGE) -> dict[str, str | None]:
        return {
            "type": "server_error",
            "code": "payment_required",
            "detail": message,
            "attr": None,
        }

    @classmethod
    def setUpTestData(cls) -> None:
        parent_set_up_test_data = getattr(super(), "setUpTestData", None)
        if parent_set_up_test_data is not None:
            parent_set_up_test_data()
        organization = getattr(cls, "organization", None)
        if cls.CONFIG_LICENSE_PLAN and organization:
            grant_all_product_features(organization)


class APILicensedTest(LicensedTestMixin, APIBaseTest):
    def setUp(self):
        super().setUp()

        organization = getattr(self, "organization", None)
        if organization and self.CONFIG_SYNC_ORGANIZATION_FEATURES_ON_SETUP:
            grant_all_product_features(organization)
