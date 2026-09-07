import uuid

from posthog.test.base import APIBaseTest
from unittest.mock import patch

from rest_framework import status

from posthog.models import Organization, OrganizationMembership, User

from products.access_control.backend.models.role import Role, RoleMembership


class TestRoleMembershipAPI(APIBaseTest):
    def setUp(self):
        super().setUp()
        # Membership writes require org-admin-or-above; the default test membership is MEMBER.
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()
        self.role = Role.objects.create(organization=self.organization, name="Analyst")
        self.memberships_url = f"/api/organizations/{self.organization.id}/roles/{self.role.id}/role_memberships/"

    def test_list_empty(self):
        response = self.client.get(self.memberships_url)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"] == []

    def test_create_membership(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)

        response = self.client.post(self.memberships_url, {"user_uuid": str(member.uuid)})

        assert response.status_code == status.HTTP_201_CREATED
        data = response.json()
        assert data["user"]["email"] == "member@posthog.com"
        assert data["organization_member"]["user"]["email"] == "member@posthog.com"
        assert RoleMembership.objects.filter(role=self.role, user=member).exists()
        membership = RoleMembership.objects.get(role=self.role, user=member)
        assert membership.organization_member == member.organization_memberships.get()

    def test_create_as_non_admin_403(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)
        self.organization_membership.level = OrganizationMembership.Level.MEMBER
        self.organization_membership.save()

        response = self.client.post(self.memberships_url, {"user_uuid": str(member.uuid)})

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert not RoleMembership.objects.filter(role=self.role, user=member).exists()

    def test_create_duplicate_member_400(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)
        RoleMembership.objects.create(
            role=self.role, user=member, organization_member=member.organization_memberships.get()
        )

        response = self.client.post(self.memberships_url, {"user_uuid": str(member.uuid)})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "User is already part of the role."
        assert RoleMembership.objects.filter(role=self.role, user=member).count() == 1

    def test_create_nonexistent_user_400(self):
        response = self.client.post(self.memberships_url, {"user_uuid": str(uuid.uuid4())})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "User does not exist."

    def test_create_inactive_user_400(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None, is_active=False)

        response = self.client.post(self.memberships_url, {"user_uuid": str(member.uuid)})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "User does not exist."

    def test_create_user_from_different_organization_400(self):
        other_org = Organization.objects.create(name="Other Org")
        other_user = User.objects.create_and_join(other_org, "other@posthog.com", None)

        response = self.client.post(self.memberships_url, {"user_uuid": str(other_user.uuid)})

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "User does not exist."

    def test_cannot_add_user_to_role_in_different_organization(self):
        # The role_id in the URL belongs to a different organization than organization_id — the
        # historical IDOR this API must not reopen: adding a member to *someone else's* role by
        # quoting a role_id that exists, just not under the org named in the URL.
        other_org = Organization.objects.create(name="Other Org")
        other_role = Role.objects.create(organization=other_org, name="Hidden")
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)

        response = self.client.post(
            f"/api/organizations/{self.organization.id}/roles/{other_role.id}/role_memberships/",
            {"user_uuid": str(member.uuid)},
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.json()["detail"] == "Role does not exist."
        assert not RoleMembership.objects.filter(role=other_role).exists()

    def test_list_with_role_org_mismatch_returns_empty_not_404(self):
        # requester IS a member of the URL's organization_id, but role_id names a role that
        # belongs to a different org — filter_rewrite_rules makes this an empty 200, not a 404.
        other_org = Organization.objects.create(name="Other Org")
        other_role = Role.objects.create(organization=other_org, name="Hidden")
        member = User.objects.create_and_join(
            other_org, "member@posthog.com", None, level=OrganizationMembership.Level.ADMIN
        )
        RoleMembership.objects.create(
            role=other_role, user=member, organization_member=member.organization_memberships.get()
        )

        response = self.client.get(f"/api/organizations/{self.organization.id}/roles/{other_role.id}/role_memberships/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"] == []

    def test_retrieve_with_role_org_mismatch_404(self):
        # Same mismatch, but retrieve of a specific membership goes through get_object_or_404
        # instead of a queryset filter, so it 404s rather than the list's empty-200.
        other_org = Organization.objects.create(name="Other Org")
        other_role = Role.objects.create(organization=other_org, name="Hidden")
        member = User.objects.create_and_join(other_org, "member@posthog.com", None)
        membership = RoleMembership.objects.create(
            role=other_role, user=member, organization_member=member.organization_memberships.get()
        )

        response = self.client.get(
            f"/api/organizations/{self.organization.id}/roles/{other_role.id}/role_memberships/{membership.id}/"
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_returns_404_for_nonexistent_organization(self):
        response = self.client.get(f"/api/organizations/{uuid.uuid4()}/roles/{self.role.id}/role_memberships/")

        assert response.status_code == status.HTTP_404_NOT_FOUND

    def test_list_returns_403_for_organization_requester_is_not_a_member_of(self):
        other_org = Organization.objects.create(name="Other Org")
        other_role = Role.objects.create(organization=other_org, name="Hidden")

        response = self.client.get(f"/api/organizations/{other_org.id}/roles/{other_role.id}/role_memberships/")

        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_retrieve_membership(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)
        membership = RoleMembership.objects.create(
            role=self.role, user=member, organization_member=member.organization_memberships.get()
        )

        response = self.client.get(f"{self.memberships_url}{membership.id}/")

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["id"] == str(membership.id)

    def test_cross_organization_role_members_not_leaked(self):
        other_org = Organization.objects.create(name="Other Org")
        other_role = Role.objects.create(organization=other_org, name="Analyst")
        other_member = User.objects.create_and_join(other_org, "other-member@posthog.com", None)
        RoleMembership.objects.create(
            role=other_role, user=other_member, organization_member=other_member.organization_memberships.get()
        )
        own_member = User.objects.create_and_join(self.organization, "own-member@posthog.com", None)
        RoleMembership.objects.create(
            role=self.role, user=own_member, organization_member=own_member.organization_memberships.get()
        )

        response = self.client.get(self.memberships_url)

        emails = [m["user"]["email"] for m in response.json()["results"]]
        assert emails == ["own-member@posthog.com"]

    def test_destroy_membership(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)
        membership = RoleMembership.objects.create(
            role=self.role, user=member, organization_member=member.organization_memberships.get()
        )

        response = self.client.delete(f"{self.memberships_url}{membership.id}/")

        assert response.status_code == status.HTTP_204_NO_CONTENT
        assert not RoleMembership.objects.filter(id=membership.id).exists()

    def test_destroy_as_non_admin_403(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)
        membership = RoleMembership.objects.create(
            role=self.role, user=member, organization_member=member.organization_memberships.get()
        )
        self.organization_membership.level = OrganizationMembership.Level.MEMBER
        self.organization_membership.save()

        response = self.client.delete(f"{self.memberships_url}{membership.id}/")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert RoleMembership.objects.filter(id=membership.id).exists()

    def test_no_update_action(self):
        member = User.objects.create_and_join(self.organization, "member@posthog.com", None)
        membership = RoleMembership.objects.create(
            role=self.role, user=member, organization_member=member.organization_memberships.get()
        )

        response = self.client.patch(f"{self.memberships_url}{membership.id}/", {"user_uuid": str(uuid.uuid4())})

        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


class TestRoleMembershipVisibilityFiltering(APIBaseTest):
    """RoleMembershipViewSet.safely_get_queryset is the only thing that hides members from a
    restricted requester on the list endpoint, and nothing else covered it.

    The facade it calls short-circuits to "everyone is visible" unless the org holds the
    ACCESS_CONTROL entitlement, so an unlicensed test org can never reach the filtering branch.
    Patching the facade is deliberate: the unit under test here is the viewset's filtering, not the
    facade's own resolution rules, which have their own tests.
    """

    def setUp(self):
        super().setUp()
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()
        self.role = Role.objects.create(organization=self.organization, name="Analyst")
        self.memberships_url = f"/api/organizations/{self.organization.id}/roles/{self.role.id}/role_memberships/"

        self.visible_user = User.objects.create_and_join(self.organization, "visible@posthog.com", None)
        self.hidden_user = User.objects.create_and_join(self.organization, "hidden@posthog.com", None)
        for user in (self.visible_user, self.hidden_user):
            RoleMembership.objects.create(
                role=self.role, user=user, organization_member=user.organization_memberships.get()
            )

    def _emails(self, response):
        return sorted(row["user"]["email"] for row in response.json()["results"])

    def test_unrestricted_requester_sees_every_member(self):
        with patch(
            "products.access_control.backend.presentation.role.restricted_visible_membership_ids", return_value=None
        ):
            response = self.client.get(self.memberships_url)

        assert response.status_code == status.HTTP_200_OK
        assert self._emails(response) == ["hidden@posthog.com", "visible@posthog.com"]

    def test_restricted_requester_sees_only_visible_members(self):
        visible_id = str(self.visible_user.organization_memberships.get().id)

        with patch(
            "products.access_control.backend.presentation.role.restricted_visible_membership_ids",
            return_value={visible_id},
        ):
            response = self.client.get(self.memberships_url)

        assert response.status_code == status.HTTP_200_OK
        assert self._emails(response) == ["visible@posthog.com"]

    def test_restricted_requester_seeing_nobody_gets_an_empty_list(self):
        with patch(
            "products.access_control.backend.presentation.role.restricted_visible_membership_ids", return_value=set()
        ):
            response = self.client.get(self.memberships_url)

        assert response.status_code == status.HTTP_200_OK
        assert response.json()["results"] == []
