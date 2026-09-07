from types import SimpleNamespace

from posthog.test.base import APIBaseTest

from django.db import connection
from django.test.utils import CaptureQueriesContext

from rest_framework import status

from posthog.models import Organization, OrganizationMembership, User

from products.access_control.backend.models.role import Role, RoleMembership
from products.access_control.backend.presentation.role import RoleSerializer, role_memberships_prefetch


class TestRoleAPI(APIBaseTest):
    def setUp(self):
        super().setUp()
        # Role writes require org-admin-or-above; the default test membership is MEMBER level.
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()
        self.roles_url = f"/api/organizations/{self.organization.id}/roles/"

    def test_create_role(self):
        response = self.client.post(self.roles_url, {"name": "baseline-qa"})

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["name"] == "baseline-qa"
        assert data["created_by"] is None
        assert data["members"] == []
        assert data["is_default"] is False
        assert Role.objects.filter(id=data["id"], organization=self.organization).exists()

    def test_create_role_organization_is_taken_from_url(self):
        # "organization" isn't even a serializer field, but the IDOR this guards against is the
        # role ending up in the wrong org at all — so assert against the URL's org, not the body.
        response = self.client.post(self.roles_url, {"name": "baseline-qa", "organization": "garbage"})

        assert response.status_code == status.HTTP_201_CREATED
        role = Role.objects.get(id=response.json()["id"])
        assert role.organization_id == self.organization.id

    def test_create_duplicate_name_case_insensitive_400(self):
        Role.objects.create(organization=self.organization, name="Analyst")

        response = self.client.post(self.roles_url, {"name": "ANALYST"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json() == self.validation_error_response(
            "There is already a role with this name.", "unique", "name"
        )
        assert Role.objects.filter(organization=self.organization).count() == 1

    def test_create_duplicate_name_different_organization_is_allowed(self):
        other_org = Organization.objects.create(name="Other Org")
        Role.objects.create(organization=other_org, name="Analyst")

        response = self.client.post(self.roles_url, {"name": "Analyst"})

        assert response.status_code == status.HTTP_201_CREATED

    def test_create_as_non_admin_403(self):
        self.organization_membership.level = OrganizationMembership.Level.MEMBER
        self.organization_membership.save()

        response = self.client.post(self.roles_url, {"name": "baseline-qa"})

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert response.json() == self.permission_denied_response("Your organization access level is insufficient.")
        assert not Role.objects.filter(organization=self.organization).exists()

    def test_list_roles(self):
        Role.objects.create(organization=self.organization, name="Analyst")

        response = self.client.get(self.roles_url)

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["count"] == 1
        assert data["results"][0]["name"] == "Analyst"

    def test_list_does_not_leak_other_organizations_roles(self):
        other_org = Organization.objects.create(name="Other Org")
        Role.objects.create(organization=other_org, name="Hidden")
        Role.objects.create(organization=self.organization, name="Visible")

        response = self.client.get(self.roles_url)

        names = [role["name"] for role in response.json()["results"]]
        assert names == ["Visible"]

    def test_retrieve_role(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")

        response = self.client.get(f"{self.roles_url}{role.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == str(role.id)

    def test_retrieve_role_from_other_organization_404(self):
        other_org = Organization.objects.create(name="Other Org")
        role = Role.objects.create(organization=other_org, name="Hidden")

        response = self.client.get(f"{self.roles_url}{role.id}/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_rename_role(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")

        response = self.client.patch(f"{self.roles_url}{role.id}/", {"name": "Senior Analyst"})

        assert response.status_code == status.HTTP_200_OK
        role.refresh_from_db()
        assert role.name == "Senior Analyst"

    def test_rename_role_case_only_change_is_allowed(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")

        response = self.client.patch(f"{self.roles_url}{role.id}/", {"name": "analyst"})

        assert response.status_code == status.HTTP_200_OK
        role.refresh_from_db()
        assert role.name == "analyst"

    def test_rename_role_to_existing_name_400(self):
        Role.objects.create(organization=self.organization, name="Analyst")
        role = Role.objects.create(organization=self.organization, name="Viewer")

        response = self.client.patch(f"{self.roles_url}{role.id}/", {"name": "analyst"})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json() == self.validation_error_response(
            "There is already a role with this name.", "unique", "name"
        )

    def test_update_as_non_admin_403(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")
        self.organization_membership.level = OrganizationMembership.Level.MEMBER
        self.organization_membership.save()

        response = self.client.patch(f"{self.roles_url}{role.id}/", {"name": "Renamed"})

        assert response.status_code == status.HTTP_403_FORBIDDEN
        role.refresh_from_db()
        assert role.name == "Analyst"

    def test_update_role_from_other_organization_404(self):
        other_org = Organization.objects.create(name="Other Org")
        role = Role.objects.create(organization=other_org, name="Hidden")

        response = self.client.patch(f"{self.roles_url}{role.id}/", {"name": "Renamed"})

        assert response.status_code == status.HTTP_404_NOT_FOUND
        role.refresh_from_db()
        assert role.name == "Hidden"

    def test_delete_role(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")

        response = self.client.delete(f"{self.roles_url}{role.id}/")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not Role.objects.filter(id=role.id).exists()

    def test_delete_as_non_admin_403(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")
        self.organization_membership.level = OrganizationMembership.Level.MEMBER
        self.organization_membership.save()

        response = self.client.delete(f"{self.roles_url}{role.id}/")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert Role.objects.filter(id=role.id).exists()

    def test_delete_role_from_other_organization_404(self):
        other_org = Organization.objects.create(name="Other Org")
        role = Role.objects.create(organization=other_org, name="Hidden")

        response = self.client.delete(f"{self.roles_url}{role.id}/")

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert Role.objects.filter(id=role.id).exists()

    def test_role_members_includes_membership(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")
        other_user = User.objects.create_and_join(self.organization, "member@posthog.com", None)
        RoleMembership.objects.create(
            role=role, user=other_user, organization_member=other_user.organization_memberships.get()
        )

        response = self.client.get(f"{self.roles_url}{role.id}/")

        members = response.json()["members"]
        assert len(members) == 1
        assert members[0]["user"]["email"] == "member@posthog.com"

    def test_listing_roles_does_not_scale_queries_with_member_count(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")

        def _add_members(count: int, prefix: str) -> None:
            for i in range(count):
                user = User.objects.create_and_join(self.organization, f"{prefix}{i}@posthog.com", None)
                RoleMembership.objects.create(
                    role=role, user=user, organization_member=user.organization_memberships.get()
                )

        _add_members(2, "small")
        # Warm up first: the very first request of the test also populates process-level caches
        # (constance settings, rate-limit config), which would otherwise show up as a difference
        # in absolute query count that has nothing to do with member prefetching.
        assert self.client.get(self.roles_url).status_code == status.HTTP_200_OK

        with CaptureQueriesContext(connection) as few_members:
            response = self.client.get(self.roles_url)
        assert response.status_code == status.HTTP_200_OK

        _add_members(4, "more")
        with CaptureQueriesContext(connection) as many_members:
            response = self.client.get(self.roles_url)
        assert response.status_code == status.HTTP_200_OK

        # Same query count regardless of member count is the N+1 regression check: the prefetch
        # helper must be doing its job for this to hold as members grow from 2 to 6.
        assert len(many_members.captured_queries) == len(few_members.captured_queries)

    def test_admin_sees_created_by_regardless_of_visibility_setting(self):
        # members_can_see_org_members=False only restricts non-admins — an admin requester's
        # own visibility computation short-circuits to "everyone visible" before it ever touches
        # the (feature-gated) per-project resolution, so this needs no extra entitlement set up.
        self.organization.members_can_see_org_members = False
        self.organization.save()
        role = Role.objects.create(organization=self.organization, name="Analyst", created_by=self.user)

        response = self.client.get(f"{self.roles_url}{role.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["created_by"]["email"] == self.user.email


class TestRoleSerializerVisibilityRedaction(APIBaseTest):
    """Unit-level: exercises RoleSerializer's own context-driven redaction directly, without
    going through the (feature-gated, project-access-dependent) restricted-visibility facade —
    that facade is pre-existing code with its own test coverage; what belongs to this API is
    that the serializer honours visible_user_ids/visible_membership_ids when they're given."""

    def _view(self) -> SimpleNamespace:
        return SimpleNamespace(organization=self.organization)

    def test_created_by_is_none_when_creator_not_visible(self):
        role = Role.objects.create(organization=self.organization, name="Analyst", created_by=self.user)

        data = RoleSerializer(role, context={"view": self._view(), "visible_user_ids": set()}).data

        assert data["created_by"] is None

    def test_created_by_is_shown_when_creator_visible(self):
        role = Role.objects.create(organization=self.organization, name="Analyst", created_by=self.user)

        data = RoleSerializer(role, context={"view": self._view(), "visible_user_ids": {str(self.user.id)}}).data

        assert data["created_by"]["email"] == self.user.email

    def test_created_by_shown_when_visibility_unrestricted(self):
        role = Role.objects.create(organization=self.organization, name="Analyst", created_by=self.user)

        data = RoleSerializer(role, context={"view": self._view(), "visible_user_ids": None}).data

        assert data["created_by"]["email"] == self.user.email

    def test_members_excludes_non_visible_membership(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")
        visible_user = User.objects.create_and_join(self.organization, "visible@posthog.com", None)
        hidden_user = User.objects.create_and_join(self.organization, "hidden@posthog.com", None)
        visible_membership = visible_user.organization_memberships.get()
        RoleMembership.objects.create(role=role, user=visible_user, organization_member=visible_membership)
        RoleMembership.objects.create(
            role=role, user=hidden_user, organization_member=hidden_user.organization_memberships.get()
        )
        # Re-fetch through the real prefetch helper: the visible row actually gets serialized
        # below, and OrganizationMemberSerializer needs organization_member.last_login annotated
        # (see role_memberships_prefetch's docstring) or rendering it raises AttributeError.
        role = Role.objects.prefetch_related(role_memberships_prefetch()).get(id=role.id)

        data = RoleSerializer(
            role, context={"view": self._view(), "visible_membership_ids": {str(visible_membership.id)}}
        ).data

        assert [m["user"]["email"] for m in data["members"]] == ["visible@posthog.com"]

    def test_members_excludes_legacy_row_with_null_organization_member_when_restricted(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")
        legacy_user = User.objects.create_and_join(self.organization, "legacy@posthog.com", None)
        RoleMembership.objects.create(role=role, user=legacy_user, organization_member=None)

        data = RoleSerializer(role, context={"view": self._view(), "visible_membership_ids": set()}).data

        assert data["members"] == []

    def test_is_default_true_for_organizations_default_role(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")
        self.organization.default_role = role
        self.organization.save()

        data = RoleSerializer(role, context={"view": self._view()}).data

        assert data["is_default"] is True

    def test_is_default_false_otherwise(self):
        role = Role.objects.create(organization=self.organization, name="Analyst")

        data = RoleSerializer(role, context={"view": self._view()}).data

        assert data["is_default"] is False


class TestRoleCrossOrgAuthorization(APIBaseTest):
    """The organization must come from the URL, never from the session's active organization.

    The EE implementation this replaced carried a dedicated regression class for this, because it
    was a real vulnerability there. Every other test in this file uses a requester who belongs to
    exactly one organization, so none of them can tell the two sources apart: they agree. These
    deliberately make them disagree.
    """

    def setUp(self):
        super().setUp()
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()

        self.other_organization = Organization.objects.create(name="Other Org")
        self.other_membership = OrganizationMembership.objects.create(
            organization=self.other_organization, user=self.user, level=OrganizationMembership.Level.ADMIN
        )
        # Session is pointed at the FIRST org while every request below targets the second.
        self.user.current_organization = self.organization
        self.user.save()

    def test_role_is_created_in_the_url_organization_not_the_active_one(self):
        response = self.client.post(f"/api/organizations/{self.other_organization.id}/roles/", {"name": "cross-org"})

        assert response.status_code == status.HTTP_201_CREATED
        role = Role.objects.get(id=response.json()["id"])
        assert role.organization == self.other_organization
        assert role.organization != self.user.current_organization

    def test_admin_of_active_org_but_only_member_of_url_org_is_forbidden(self):
        # Admin here, plain member there. Authorization must follow the URL's org.
        self.other_membership.level = OrganizationMembership.Level.MEMBER
        self.other_membership.save()

        response = self.client.post(f"/api/organizations/{self.other_organization.id}/roles/", {"name": "cross-org"})

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not Role.objects.filter(organization=self.other_organization, name="cross-org").exists()

    def test_roles_listed_are_the_url_organizations(self):
        Role.objects.create(organization=self.organization, name="here")
        Role.objects.create(organization=self.other_organization, name="there")

        response = self.client.get(f"/api/organizations/{self.other_organization.id}/roles/")

        assert response.status_code == status.HTTP_200_OK
        assert [r["name"] for r in response.json()["results"]] == ["there"]


class TestRoleApiScope(APIBaseTest):
    """The API scope is part of the contract for already-issued credentials.

    Moving these viewsets onto a different scope_object would both lock out personal API keys
    already scoped organization:write and grant org-wide role administration to any credential
    holding a narrower scope. Neither is visible in a response body, so it needs its own assertion.
    """

    def test_viewsets_declare_the_organization_scope(self):
        from products.access_control.backend.presentation.role import RoleMembershipViewSet, RoleViewSet

        assert RoleViewSet.scope_object == "organization"
        assert RoleMembershipViewSet.scope_object == "organization"
