from posthog.test.base import APIBaseTest

from rest_framework import status

from posthog.models import IdentityProviderConfig, Organization, OrganizationMembership


class TestIdentityProviderConfigAPI(APIBaseTest):
    def _make_admin(self) -> None:
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

    def _enable_features(self, *features: str) -> None:
        self.organization.available_product_features = [{"key": f, "name": f} for f in features]
        self.organization.save()

    # List & retrieve

    def test_cannot_retrieve_config_from_other_org(self):
        self._make_admin()
        other_org = Organization.objects.create(name="Other")
        other_config = IdentityProviderConfig.objects.create(organization=other_org, name="Other Okta")
        response = self.client.get(f"/api/organizations/@current/identity_provider_configs/{other_config.id}")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # Create & permissions

    # SAML

    # SCIM

    # ID-JAG (XAA)

    # Deletion
