import json

from unittest.mock import MagicMock, patch

from django.db import connection
from django.test.utils import CaptureQueriesContext

from parameterized import parameterized
from rest_framework import status

from posthog.constants import AvailableFeature
from posthog.models.organization import Organization, OrganizationMembership
from posthog.models.personal_api_key import PersonalAPIKey
from posthog.models.team.team import Team
from posthog.models.user import User
from posthog.models.utils import generate_random_token_personal, hash_key_value
from posthog.session_recordings.models.session_recording import SessionRecording
from posthog.test.licensed_base import APILicensedTest
from posthog.utils import render_template

from products.access_control.backend.facade.user_access_control import AccessSource
from products.access_control.backend.models.access_control import AccessControl
from products.access_control.backend.models.role import Role, RoleMembership
from products.ai_observability.backend.models.evaluations import Evaluation
from products.cohorts.backend.models.cohort import Cohort
from products.conversations.backend.models import Ticket
from products.dashboards.backend.models.dashboard import Dashboard
from products.feature_flags.backend.models.feature_flag import FeatureFlag
from products.notebooks.backend.models import Notebook
from products.product_analytics.backend.facade.models import Insight
from products.warehouse_sources.backend.models import DataWarehouseTable


class BaseAccessControlTest(APILicensedTest):
    def setUp(self):
        super().setUp()
        self.organization.available_product_features = [
            {"key": AvailableFeature.ACCESS_CONTROL, "name": AvailableFeature.ACCESS_CONTROL},
            {"key": AvailableFeature.ROLE_BASED_ACCESS, "name": AvailableFeature.ROLE_BASED_ACCESS},
        ]
        self.organization.save()

    def _put_project_access_control(self, data=None):
        payload = {"access_level": "admin"}

        if data:
            payload.update(data)

        return self.client.put(
            "/api/projects/@current/access_controls",
            payload,
        )

    def _put_global_access_control(self, data=None):
        payload = {"access_level": "editor"}
        if data:
            payload.update(data)

        return self.client.put(
            "/api/projects/@current/resource_access_controls",
            payload,
        )

    def _org_membership(self, level: OrganizationMembership.Level = OrganizationMembership.Level.ADMIN):
        self.organization_membership.level = level
        self.organization_membership.save()


class TestAccessControlProjectLevelAPI(BaseAccessControlTest):
    def test_project_change_rejected_if_not_org_admin(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self._put_project_access_control()
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()

    def test_project_change_accepted_if_org_admin(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self._put_project_access_control()
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_project_change_accepted_if_org_owner(self):
        self._org_membership(OrganizationMembership.Level.OWNER)
        res = self._put_project_access_control()
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_project_removed_with_null(self):
        self._org_membership(OrganizationMembership.Level.OWNER)
        res = self._put_project_access_control()
        res = self._put_project_access_control({"access_level": None})
        assert res.status_code == status.HTTP_204_NO_CONTENT

    def test_project_change_rejected_if_not_in_organization(self):
        self.organization_membership.delete()
        res = self._put_project_access_control(
            {"organization_member": str(self.organization_membership.id), "access_level": "admin"}
        )
        assert res.status_code == status.HTTP_404_NOT_FOUND, res.json()

    def test_project_change_rejected_if_bad_access_level(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self._put_project_access_control({"access_level": "bad"})
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert res.json()["detail"] == "Invalid access level. Must be one of: none, member, admin", res.json()

    def test_invalid_organization_member_id_error_message(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self._put_project_access_control({"organization_member": "not-a-valid-uuid", "access_level": "member"})
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert res.json()["attr"] == "organization_member"
        # Should not mention "UUID" in the error message
        assert "UUID" not in res.json()["detail"]
        # Should provide helpful guidance
        assert "organization member id" in res.json()["detail"]
        assert "/api/organizations/" in res.json()["detail"]

    def test_role_based_access_control_rejected_without_role_based_access_feature(self):
        # Drop ROLE_BASED_ACCESS, keep ACCESS_CONTROL — same shape as the UI gate
        self.organization.available_product_features = [
            {"key": AvailableFeature.ACCESS_CONTROL, "name": AvailableFeature.ACCESS_CONTROL},
        ]
        self.organization.save()

        self._org_membership(OrganizationMembership.Level.ADMIN)
        role = Role.objects.create(name="Engineering", organization=self.organization)

        res = self._put_project_access_control({"role": str(role.id), "access_level": "admin"})
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()
        assert "Role-based access" in res.json()["detail"]

        # Member-level writes still work — only the role-backed write is blocked
        res = self._put_project_access_control(
            {"organization_member": str(self.organization_membership.id), "access_level": "admin"}
        )
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_project_change_rejected_if_role_belongs_to_another_organization(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        other_organization = Organization.objects.create(name="Other organization")
        foreign_role = Role.objects.create(name="Foreign role", organization=other_organization)

        res = self._put_project_access_control({"role": str(foreign_role.id), "access_level": "admin"})
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert res.json()["detail"] == "The role must belong to the same organization as this project."

    def test_project_change_rejected_if_member_belongs_to_another_organization(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        other_organization = Organization.objects.create(name="Other organization")
        foreign_membership = OrganizationMembership.objects.create(
            organization=other_organization, user=self.user, level=OrganizationMembership.Level.MEMBER
        )

        res = self._put_project_access_control(
            {"organization_member": str(foreign_membership.id), "access_level": "admin"}
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert res.json()["detail"] == "The member must belong to the same organization as this project."


class TestAccessControlMinimumLevelValidation(BaseAccessControlTest):
    def test_action_access_level_cannot_be_below_viewer(self):
        """Test that action access level cannot be set below minimum 'viewer'"""
        self._org_membership(OrganizationMembership.Level.ADMIN)
        from products.actions.backend.models.action import Action

        action = Action.objects.create(team=self.team, name="test action")

        res = self.client.put(
            f"/api/projects/@current/actions/{action.id}/access_controls",
            {"access_level": "none"},
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert "cannot be set below the minimum 'viewer'" in res.json()["detail"]

    def test_action_access_level_accepts_viewer_and_above(self):
        """Test that action access level accepts viewer, editor, and manager"""
        self._org_membership(OrganizationMembership.Level.ADMIN)
        from products.actions.backend.models.action import Action

        action = Action.objects.create(team=self.team, name="test action")

        for level in ["viewer", "editor", "manager"]:
            res = self.client.put(
                f"/api/projects/@current/actions/{action.id}/access_controls",
                {"access_level": level},
            )
            assert res.status_code == status.HTTP_200_OK, f"Failed for level {level}: {res.json()}"

    def test_activity_log_access_level_cannot_be_above_viewer(self):
        """Test that activity_log access level cannot be set above maximum 'viewer'"""
        self._org_membership(OrganizationMembership.Level.ADMIN)

        for level in ["editor", "manager"]:
            res = self.client.put(
                "/api/projects/@current/resource_access_controls",
                {"resource": "activity_log", "access_level": level},
            )
            assert res.status_code == status.HTTP_400_BAD_REQUEST, f"Failed for level {level}: {res.json()}"
            assert "cannot be set above the maximum 'viewer'" in res.json()["detail"]

    def test_toolbar_access_level_cannot_be_above_viewer(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)

        for level in ["editor", "manager"]:
            res = self.client.put(
                "/api/projects/@current/resource_access_controls",
                {"resource": "toolbar", "access_level": level},
            )
            assert res.status_code == status.HTTP_400_BAD_REQUEST, f"Failed for level {level}: {res.json()}"
            assert "cannot be set above the maximum 'viewer'" in res.json()["detail"]


class TestAccessControlResourceLevelAPI(BaseAccessControlTest):
    def setUp(self):
        super().setUp()

        self.notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="0", title="first notebook"
        )

        self.other_user = self._create_user("other_user")
        self.other_user_notebook = Notebook.objects.create(
            team=self.team, created_by=self.other_user, short_id="1", title="first notebook"
        )

    def _get_access_controls(self):
        return self.client.get(f"/api/projects/@current/notebooks/{self.notebook.short_id}/access_controls")

    def _put_access_control(self, data=None, notebook_id=None):
        payload = {
            "access_level": "editor",
        }

        if data:
            payload.update(data)
        return self.client.put(
            f"/api/projects/@current/notebooks/{notebook_id or self.notebook.short_id}/access_controls",
            payload,
        )

    def test_get_access_controls(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self._get_access_controls()
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert res.json() == {
            "access_controls": [],
            "available_access_levels": ["none", "viewer", "editor", "manager"],
            "user_access_level": "manager",
            "default_access_level": "editor",
            "user_can_edit_access_levels": True,
            "minimum_access_level": "none",
            "maximum_access_level": "manager",
            # No rule anywhere above this notebook, so the resource's built-in default applies
            "inherited_access": {
                "access_level": "editor",
                "source": "system_default",
                "source_subject": None,
                "source_resource": "notebook",
                "source_resource_id": None,
                "source_display_name": None,
            },
        }

    def test_project_reports_no_inherited_level(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self.client.get("/api/projects/@current/access_controls")
        assert res.status_code == status.HTTP_200_OK, res.json()
        # Nothing sits above a project, so it must report no inherited level — that absence is what
        # keeps "No override" an object-default affordance rather than a project-level one
        assert res.json()["inherited_access"] is None

    def test_change_rejected_if_not_org_admin(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self._put_access_control(notebook_id=self.other_user_notebook.short_id)
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()

    def test_change_accepted_if_org_admin(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self._put_access_control(notebook_id=self.other_user_notebook.short_id)
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_change_accepted_if_creator_of_the_resource(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self._put_access_control(notebook_id=self.notebook.short_id)
        assert res.status_code == status.HTTP_200_OK, res.json()


class TestAccessControlObjectCap(BaseAccessControlTest):
    """
    Caps distinct objects with per-object access control overrides per (team, resource).
    See ACCESS_CONTROL_MAX_OBJECTS_PER_RESOURCE in posthog/rbac/user_access_control.py.
    """

    def setUp(self):
        super().setUp()
        self._org_membership(OrganizationMembership.Level.ADMIN)
        # Patch the cap to a small value so tests don't have to create 1000 rows.
        self.cap_patcher = patch(
            "products.access_control.backend.presentation.access_control.ACCESS_CONTROL_MAX_OBJECTS_PER_RESOURCE", 3
        )
        self.cap_patcher.start()
        self.addCleanup(self.cap_patcher.stop)

    def _make_dashboard(self, name: str) -> Dashboard:
        return Dashboard.objects.create(team=self.team, created_by=self.user, name=name)

    def _put_dashboard_ac(self, dashboard: Dashboard, payload: dict):
        return self.client.put(
            f"/api/projects/@current/dashboards/{dashboard.id}/access_controls",
            payload,
        )

    def _fill_to_cap(self, count: int) -> list[Dashboard]:
        """Create `count` dashboards each with one AC row (consuming `count` slots)."""
        dashboards = [self._make_dashboard(f"d{i}") for i in range(count)]
        for d in dashboards:
            res = self._put_dashboard_ac(d, {"access_level": "viewer"})
            assert res.status_code == status.HTTP_200_OK, res.json()
        return dashboards

    def test_new_object_rejected_at_cap(self):
        self._fill_to_cap(3)  # cap is 3
        new_dashboard = self._make_dashboard("d4")

        res = self._put_dashboard_ac(new_dashboard, {"access_level": "viewer"})

        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert "Reached the limit of 3 dashboards with access control overrides" in json.dumps(res.json())

    def test_additional_rule_on_existing_object_allowed_at_cap(self):
        dashboards = self._fill_to_cap(3)
        # Add a second AC row on an already-restricted dashboard (different role override).
        role = Role.objects.create(organization=self.organization, name="viewers")
        res = self._put_dashboard_ac(
            dashboards[0],
            {"role": str(role.id), "access_level": "editor"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_update_existing_rule_allowed_at_cap(self):
        dashboards = self._fill_to_cap(3)
        # Bump an existing default rule from viewer to editor.
        res = self._put_dashboard_ac(dashboards[0], {"access_level": "editor"})
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_delete_allowed_at_cap(self):
        dashboards = self._fill_to_cap(3)
        res = self._put_dashboard_ac(dashboards[0], {"access_level": None})
        assert res.status_code == status.HTTP_204_NO_CONTENT, res.content

    def test_resource_level_default_not_capped(self):
        # The 3-object cap is on resource_id IS NOT NULL rows; resource-level
        # (project-wide) defaults are unrelated and must not be blocked.
        self._fill_to_cap(3)
        res = self.client.put(
            "/api/projects/@current/resource_access_controls",
            {"resource": "dashboard", "access_level": "viewer"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_cap_is_per_resource_not_per_team(self):
        # Filling the dashboard slots must not block creating object-level rules for
        # other resources (notebooks here).
        self._fill_to_cap(3)
        notebook = Notebook.objects.create(team=self.team, created_by=self.user, short_id="nb1", title="nb1")
        res = self.client.put(
            f"/api/projects/@current/notebooks/{notebook.short_id}/access_controls",
            {"access_level": "viewer"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_below_cap_create_works(self):
        self._fill_to_cap(2)
        new_dashboard = self._make_dashboard("d3")
        res = self._put_dashboard_ac(new_dashboard, {"access_level": "viewer"})
        assert res.status_code == status.HTTP_200_OK, res.json()


class TestResourceAccessControlsSecurityValidation(BaseAccessControlTest):
    """
    Regression tests for privilege escalation via resource_access_controls endpoint.

    The resource_access_controls endpoint (is_resource_level=True) must only be available
    on the project viewset. If exposed on other viewsets (notebooks, dashboards, etc.),
    an attacker could use an object they own to bypass authorization checks and write
    arbitrary access controls for any resource, including the project itself.
    """

    def setUp(self):
        super().setUp()
        self.notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="0", title="attacker notebook"
        )

    def test_resource_access_controls_rejected_on_notebook_viewset(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self.client.put(
            f"/api/projects/@current/notebooks/{self.notebook.short_id}/resource_access_controls",
            {"resource": "project", "access_level": "admin"},
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert "Resource-level access controls can only be configured for projects" in res.json()["detail"]

    def test_resource_access_controls_get_rejected_on_notebook_viewset(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self.client.get(
            f"/api/projects/@current/notebooks/{self.notebook.short_id}/resource_access_controls",
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert "Resource-level access controls can only be configured for projects" in res.json()["detail"]

    def test_resource_access_controls_rejected_on_dashboard_viewset(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        dashboard = Dashboard.objects.create(team=self.team, created_by=self.user, name="attacker dashboard")
        res = self.client.put(
            f"/api/projects/@current/dashboards/{dashboard.id}/resource_access_controls",
            {"resource": "project", "access_level": "admin"},
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert "Resource-level access controls can only be configured for projects" in res.json()["detail"]

    def test_cannot_escalate_to_project_admin_via_own_notebook(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self.client.put(
            f"/api/projects/@current/notebooks/{self.notebook.short_id}/resource_access_controls",
            {
                "resource": "project",
                "resource_id": str(self.team.id),
                "organization_member": str(self.organization_membership.id),
                "access_level": "admin",
            },
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()

    def test_cannot_write_arbitrary_resource_controls_via_own_notebook(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self.client.put(
            f"/api/projects/@current/notebooks/{self.notebook.short_id}/resource_access_controls",
            {"resource": "dashboard", "access_level": "none"},
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()

    def test_resource_access_controls_allowed_on_project_viewset(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self.client.put(
            "/api/projects/@current/resource_access_controls",
            {"resource": "dashboard", "access_level": "editor"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()

    def test_resource_access_controls_with_spoofed_resource_id_rejected(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        dashboard = Dashboard.objects.create(team=self.team, created_by=self.user, name="target dashboard")
        res = self.client.put(
            "/api/projects/@current/resource_access_controls",
            {
                "resource": "dashboard",
                "resource_id": str(dashboard.id),
                "access_level": "none",
            },
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()
        assert "Cannot modify access controls for a resource different from the URL target" in res.json()["detail"]


class TestResourceAccessControlsViaProjectId(BaseAccessControlTest):
    """
    Tests that the resource_access_controls endpoint works correctly when
    addressed via an explicit project ID (e.g. PUT /api/projects/<id>/resource_access_controls)
    rather than the @current alias.
    """

    def setUp(self):
        super().setUp()
        self.notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="0", title="attacker notebook"
        )

    def _project_url(self, suffix: str) -> str:
        return f"/api/projects/{self.project.id}/{suffix}"

    def test_put_resource_access_controls_via_project_id_succeeds_for_admin(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self.client.put(
            self._project_url("resource_access_controls"),
            {"resource": "dashboard", "access_level": "editor"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert res.json()["resource"] == "dashboard"
        assert res.json()["access_level"] == "editor"

    def test_get_resource_access_controls_via_project_id_succeeds_for_admin(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self.client.get(self._project_url("resource_access_controls"))
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert "access_controls" in res.json()

    def test_put_resource_access_controls_via_project_id_rejected_for_member(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self.client.put(
            self._project_url("resource_access_controls"),
            {"resource": "dashboard", "access_level": "none"},
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()

    def test_put_resource_access_controls_via_project_id_creates_correct_access_control(self):
        from products.access_control.backend.models.access_control import AccessControl

        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self.client.put(
            self._project_url("resource_access_controls"),
            {"resource": "notebook", "access_level": "viewer"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()

        ac = AccessControl.objects.get(team=self.team, resource="notebook", resource_id=None)
        assert ac.access_level == "viewer"
        assert ac.organization_member is None
        assert ac.role is None

    def test_put_resource_access_controls_via_project_id_with_spoofed_resource_id_rejected(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        dashboard = Dashboard.objects.create(team=self.team, created_by=self.user, name="target")
        res = self.client.put(
            self._project_url("resource_access_controls"),
            {
                "resource": "dashboard",
                "resource_id": str(dashboard.id),
                "access_level": "none",
            },
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()
        assert "Cannot modify access controls for a resource different from the URL target" in res.json()["detail"]

    def test_member_cannot_escalate_via_notebook_to_project_resource_access_controls(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self.client.put(
            f"/api/projects/{self.project.id}/notebooks/{self.notebook.short_id}/resource_access_controls",
            {
                "resource": "project",
                "resource_id": str(self.team.id),
                "organization_member": str(self.organization_membership.id),
                "access_level": "admin",
            },
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()
        assert "Resource-level access controls can only be configured for projects" in res.json()["detail"]

    def test_delete_resource_access_control_via_project_id(self):
        from products.access_control.backend.models.access_control import AccessControl

        self._org_membership(OrganizationMembership.Level.ADMIN)

        res = self.client.put(
            self._project_url("resource_access_controls"),
            {"resource": "dashboard", "access_level": "viewer"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert AccessControl.objects.filter(team=self.team, resource="dashboard", resource_id=None).exists()

        res = self.client.put(
            self._project_url("resource_access_controls"),
            {"resource": "dashboard", "access_level": None},
        )
        assert res.status_code == status.HTTP_204_NO_CONTENT
        assert not AccessControl.objects.filter(team=self.team, resource="dashboard", resource_id=None).exists()


class TestUsersWithAccessAPI(BaseAccessControlTest):
    """Test the new users_with_access endpoint"""

    def setUp(self):
        super().setUp()

        # Create additional users for testing
        self.user2 = self._create_user("user2@example.com")
        self.user3 = self._create_user("user3@example.com")
        self.user4 = self._create_user("user4@example.com")

        # Create a notebook for testing
        self.notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="0", title="test notebook"
        )

        # Create a role for testing
        self.role = Role.objects.create(name="Test Role", organization=self.organization)

    def _get_users_with_access(self, notebook_id=None):
        return self.client.get(
            f"/api/projects/@current/notebooks/{notebook_id or self.notebook.short_id}/users_with_access"
        )

    def _put_notebook_access_control(self, notebook_id: str, data=None):
        payload = {
            "access_level": "editor",
        }
        if data:
            payload.update(data)
        return self.client.put(
            f"/api/projects/@current/notebooks/{notebook_id}/access_controls",
            payload,
        )

    def test_default_access_includes_all_org_members(self):
        """Test that by default all organization members have access"""
        self._org_membership(OrganizationMembership.Level.MEMBER)

        res = self._get_users_with_access()
        assert res.status_code == status.HTTP_200_OK, res.json()

        data = res.json()
        assert data["total_count"] == 4  # user, user2, user3, user4
        assert len(data["users"]) == 4
        # Check that all users are included with default access
        user_ids = [user["user_id"] for user in data["users"]]
        assert str(self.user.uuid) in user_ids
        assert str(self.user2.uuid) in user_ids
        assert str(self.user3.uuid) in user_ids
        assert str(self.user4.uuid) in user_ids

        # Check that creator has highest access level
        creator_user = next(user for user in data["users"] if user["user_id"] == str(self.user.uuid))
        assert creator_user["access_level"] == "manager"
        assert creator_user["access_source"] == AccessSource.CREATOR.value

        # Check that other users have default access level (not "none")
        other_users = [user for user in data["users"] if user["user_id"] != str(self.user.uuid)]
        for user in other_users:
            assert user["access_level"] != "none"

    def test_org_admin_has_highest_access(self):
        """Test that org admins get highest access level"""
        self._org_membership(OrganizationMembership.Level.ADMIN)

        # Create a notebook by another user so we can test org admin access
        other_notebook = Notebook.objects.create(
            team=self.team, created_by=self.user2, short_id="2", title="other notebook"
        )

        res = self.client.get(f"/api/projects/@current/notebooks/{other_notebook.short_id}/users_with_access")
        assert res.status_code == status.HTTP_200_OK, res.json()

        data = res.json()
        admin_user = next(user for user in data["users"] if user["user_id"] == str(self.user.uuid))
        assert admin_user["access_level"] == "manager"
        assert admin_user["access_source"] == AccessSource.ORGANIZATION_ADMIN.value

    def test_access_level_prioritization(self):
        """Test that higher access levels take precedence"""
        self._org_membership(OrganizationMembership.Level.ADMIN)

        # Give user2 explicit viewer access
        res = self._put_notebook_access_control(
            self.notebook.short_id,
            {
                "organization_member": str(self.user2.organization_memberships.get(organization=self.organization).id),
                "access_level": "viewer",
            },
        )
        assert res.status_code == status.HTTP_200_OK, res.json()

        # Make user2 org admin (should override explicit access)
        user2_membership = self.user2.organization_memberships.get(organization=self.organization)
        user2_membership.level = OrganizationMembership.Level.ADMIN
        user2_membership.save()

        res = self._get_users_with_access()
        assert res.status_code == status.HTTP_200_OK, res.json()

        data = res.json()
        user2_data = next(user for user in data["users"] if user["user_id"] == str(self.user2.uuid))
        assert user2_data["access_level"] == "manager"
        assert user2_data["access_source"] == AccessSource.ORGANIZATION_ADMIN.value

    def test_endpoint_requires_permission(self):
        """Test that the endpoint requires appropriate permissions"""
        # Set project-level access to "none" as admin first
        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self._put_project_access_control({"access_level": "none"})
        assert res.status_code == status.HTTP_200_OK, res.json()

        # Switch to member level
        self._org_membership(OrganizationMembership.Level.MEMBER)

        # Try to access another user's notebook
        other_notebook = Notebook.objects.create(
            team=self.team, created_by=self.user2, short_id="1", title="other notebook"
        )

        res = self._get_users_with_access(other_notebook.short_id)
        assert res.status_code == status.HTTP_403_FORBIDDEN

    def test_endpoint_returns_correct_user_data(self):
        """Test that the endpoint returns all required user data fields"""
        self._org_membership(OrganizationMembership.Level.MEMBER)

        res = self._get_users_with_access()
        assert res.status_code == status.HTTP_200_OK, res.json()

        data = res.json()
        user_data = data["users"][0]  # First user

        # Check all required fields are present
        assert "user_id" in user_data
        assert "access_level" in user_data
        assert "access_source" in user_data
        assert "organization_membership_id" in user_data
        assert "organization_membership_level" in user_data

        # Check data types
        assert isinstance(user_data["user_id"], str)
        assert isinstance(user_data["access_level"], str)
        assert isinstance(user_data["access_source"], str)

    def test_endpoint_works_with_different_resource_types(self):
        """Test that the endpoint works with different resource types (notebooks, dashboards, etc.)"""
        self._org_membership(OrganizationMembership.Level.MEMBER)

        # Test with dashboard
        dashboard = Dashboard.objects.create(team=self.team, created_by=self.user, name="test dashboard")

        res = self.client.get(f"/api/projects/@current/dashboards/{dashboard.id}/users_with_access")
        assert res.status_code == status.HTTP_200_OK, res.json()

        data = res.json()
        assert data["total_count"] >= 1
        assert any(user["user_id"] == str(self.user.uuid) for user in data["users"])

    def test_endpoint_handles_empty_organization(self):
        """Test that the endpoint handles organizations with no members gracefully"""
        self._org_membership(OrganizationMembership.Level.MEMBER)

        # Remove all other users from organization
        OrganizationMembership.objects.filter(organization=self.organization).exclude(user=self.user).delete()

        res = self._get_users_with_access()
        assert res.status_code == status.HTTP_200_OK, res.json()

        data = res.json()
        assert data["total_count"] == 1
        assert data["users"][0]["user_id"] == str(self.user.uuid)

    def test_only_active_users_included(self):
        """Test that only active users are included in the users_with_access endpoint"""
        self._org_membership(OrganizationMembership.Level.ADMIN)

        # Create an inactive user and add them to the organization
        inactive_user = self._create_user("inactive_user@example.com")
        inactive_user.is_active = False
        inactive_user.save()

        # Get users with access
        res = self._get_users_with_access()
        assert res.status_code == status.HTTP_200_OK, res.json()

        data = res.json()
        user_ids = [user["user_id"] for user in data["users"]]

        # Verify inactive user is not included
        assert str(inactive_user.uuid) not in user_ids

        # Verify active users are still included
        assert str(self.user.uuid) in user_ids
        assert str(self.user2.uuid) in user_ids
        assert str(self.user3.uuid) in user_ids
        assert str(self.user4.uuid) in user_ids


class TestGlobalAccessControlsPermissions(BaseAccessControlTest):
    def setUp(self):
        super().setUp()

        self.role = Role.objects.create(name="Engineers", organization=self.organization)
        self.role_membership = RoleMembership.objects.create(user=self.user, role=self.role)

    def test_admin_can_always_access(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        assert (
            self._put_global_access_control({"resource": "feature_flag", "access_level": "none"}).status_code
            == status.HTTP_200_OK
        )
        assert self.client.get("/api/projects/@current/feature_flags").status_code == status.HTTP_200_OK


class TestAccessControlPermissions(BaseAccessControlTest):
    """
    Test actual permissions being applied for a resource (notebooks as an example)
    """

    def setUp(self):
        super().setUp()
        self.other_user = self._create_user("other_user")

        self.other_user_notebook = Notebook.objects.create(
            team=self.team, created_by=self.other_user, title="not my notebook"
        )

        self.notebook = Notebook.objects.create(team=self.team, created_by=self.user, title="my notebook")

    def _post_notebook(self):
        return self.client.post("/api/projects/@current/notebooks/", {"title": "notebook"})

    def _patch_notebook(self, id: str):
        return self.client.patch(f"/api/projects/@current/notebooks/{id}", {"title": "new-title"})

    def _get_notebook(self, id: str):
        return self.client.get(f"/api/projects/@current/notebooks/{id}")

    def _put_notebook_access_control(self, notebook_id: str, data=None):
        payload = {
            "access_level": "editor",
        }

        if data:
            payload.update(data)
        return self.client.put(
            f"/api/projects/@current/notebooks/{notebook_id}/access_controls",
            payload,
        )

    def test_default_allows_all_access(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        assert self._get_notebook(self.other_user_notebook.short_id).status_code == status.HTTP_200_OK
        assert self._patch_notebook(id=self.other_user_notebook.short_id).status_code == status.HTTP_200_OK
        res = self._post_notebook()
        assert res.status_code == status.HTTP_201_CREATED
        assert self._patch_notebook(id=res.json()["short_id"]).status_code == status.HTTP_200_OK

    def test_rejects_all_access_without_project_access(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        assert self._put_project_access_control({"access_level": "none"}).status_code == status.HTTP_200_OK
        self._org_membership(OrganizationMembership.Level.MEMBER)

        assert self._get_notebook(self.other_user_notebook.short_id).status_code == status.HTTP_403_FORBIDDEN
        assert self._patch_notebook(id=self.other_user_notebook.short_id).status_code == status.HTTP_403_FORBIDDEN
        assert self._post_notebook().status_code == status.HTTP_403_FORBIDDEN

    def test_permits_access_with_member_control(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        assert self._put_project_access_control({"access_level": "none"}).status_code == status.HTTP_200_OK
        assert (
            self._put_project_access_control(
                {"access_level": "member", "organization_member": str(self.organization_membership.id)}
            ).status_code
            == status.HTTP_200_OK
        )
        self._org_membership(OrganizationMembership.Level.MEMBER)

        assert self._get_notebook(self.other_user_notebook.short_id).status_code == status.HTTP_200_OK
        assert self._patch_notebook(id=self.other_user_notebook.short_id).status_code == status.HTTP_200_OK
        assert self._post_notebook().status_code == status.HTTP_201_CREATED

    def test_org_level_endpoints_work(self):
        assert self.client.get("/api/organizations/@current/plugins").status_code == status.HTTP_200_OK


class TestAccessControlFiltering(BaseAccessControlTest):
    def setUp(self):
        super().setUp()
        self.other_user = self._create_user("other_user")

        self.other_user_notebook = Notebook.objects.create(
            team=self.team, created_by=self.other_user, title="not my notebook"
        )

        self.notebook = Notebook.objects.create(team=self.team, created_by=self.user, title="my notebook")

    def _put_notebook_access_control(self, notebook_id: str, data=None):
        payload = {
            "access_level": "editor",
        }

        if data:
            payload.update(data)
        return self.client.put(
            f"/api/projects/@current/notebooks/{notebook_id}/access_controls",
            payload,
        )

    def _get_notebooks(self):
        return self.client.get("/api/projects/@current/notebooks/")

    def test_default_allows_all_access(self):
        self._org_membership(OrganizationMembership.Level.MEMBER)
        assert len(self._get_notebooks().json()["results"]) == 2

    def test_list_notebooks_with_explicit_access(self):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        assert (
            self._put_notebook_access_control(self.other_user_notebook.short_id, {"access_level": "none"}).status_code
            == status.HTTP_200_OK
        )
        assert (
            self._put_notebook_access_control(
                self.other_user_notebook.short_id,
                {"organization_member": str(self.organization_membership.id), "access_level": "viewer"},
            ).status_code
            == status.HTTP_200_OK
        )
        self._org_membership(OrganizationMembership.Level.MEMBER)

        res = self._get_notebooks()
        assert len(res.json()["results"]) == 2


class TestAccessControlProjectFiltering(BaseAccessControlTest):
    """
    Projects are listed in multiple places and ways so we need to test all of them here
    """

    def setUp(self):
        super().setUp()
        self.other_team = Team.objects.create(organization=self.organization, name="other team")
        self.other_team_2 = Team.objects.create(organization=self.organization, name="other team 2")

    def _put_project_access_control_as_admin(self, team_id: int, data=None):
        self._org_membership(OrganizationMembership.Level.ADMIN)
        payload = {
            "access_level": "editor",
        }

        if data:
            payload.update(data)
        res = self.client.put(
            f"/api/projects/{team_id}/access_controls",
            payload,
        )

        self._org_membership(OrganizationMembership.Level.MEMBER)

        assert res.status_code == status.HTTP_200_OK, res.json()
        return res

    def _get_posthog_app_context(self):
        mock_template = MagicMock()
        with patch("posthog.utils.get_template", return_value=mock_template):
            mock_request = MagicMock()
            mock_request.user = self.user
            mock_request.GET = {}
            render_template("index.html", request=mock_request, context={})

            # Get the context passed to the template
            return json.loads(mock_template.render.call_args[0][0]["posthog_app_context"])

    def test_default_lists_all_projects(self):
        assert len(self.client.get("/api/projects").json()["results"]) == 3
        me_response = self.client.get("/api/users/@me").json()
        assert len(me_response["organization"]["teams"]) == 3

    def test_does_not_list_projects_without_access(self):
        self._put_project_access_control_as_admin(self.other_team.id, {"access_level": "none"})
        assert len(self.client.get("/api/projects").json()["results"]) == 2
        me_response = self.client.get("/api/users/@me").json()
        assert len(me_response["organization"]["teams"]) == 2

    def test_always_lists_all_projects_if_org_admin(self):
        self._put_project_access_control_as_admin(self.other_team.id, {"access_level": "none"})
        self._org_membership(OrganizationMembership.Level.ADMIN)
        assert len(self.client.get("/api/projects").json()["results"]) == 3
        me_response = self.client.get("/api/users/@me").json()
        assert len(me_response["organization"]["teams"]) == 3


# TODO: Add tests to check that a dashboard can't be edited if the user doesn't have access


class TestAccessControlScopeRequirements(BaseAccessControlTest):
    """
    Test that access control endpoints require the correct scopes
    """

    def setUp(self):
        super().setUp()
        self._org_membership(OrganizationMembership.Level.ADMIN)

    def test_access_controls_get_requires_access_control_read_scope(self):
        """Test that GET requests to access_controls endpoint require access_control:read scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key",
            secure_value=hash_key_value(key_value),
            scopes=["project:read"],  # Only project:read, no access_control:read
        )

        response = self.client.get(
            "/api/projects/@current/access_controls", headers={"authorization": f"Bearer {key_value}"}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "access_control:read" in response.json()["detail"]

    def test_resource_access_controls_get_requires_access_control_read_scope(self):
        """Test that GET requests to resource_access_controls endpoint require access_control:read scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key",
            secure_value=hash_key_value(key_value),
            scopes=["project:read"],  # Only project:read, no access_control:read
        )

        response = self.client.get(
            "/api/projects/@current/resource_access_controls", headers={"authorization": f"Bearer {key_value}"}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "access_control:read" in response.json()["detail"]

    def test_deprecated_global_access_controls_get_requires_access_control_read_scope(self):
        """Test that GET requests to deprecated global_access_controls endpoint require access_control:read scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key",
            secure_value=hash_key_value(key_value),
            scopes=["project:read"],  # Only project:read, no access_control:read
        )

        response = self.client.get(
            "/api/projects/@current/global_access_controls", headers={"authorization": f"Bearer {key_value}"}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "access_control:read" in response.json()["detail"]

    def test_access_controls_get_succeeds_with_access_control_read_scope(self):
        """Test that GET requests to access_controls endpoint succeed with access_control:read scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user, label="test_key", secure_value=hash_key_value(key_value), scopes=["access_control:read"]
        )

        response = self.client.get(
            "/api/projects/@current/access_controls", headers={"authorization": f"Bearer {key_value}"}
        )
        assert response.status_code == status.HTTP_200_OK

    def test_resource_access_controls_get_succeeds_with_access_control_read_scope(self):
        """Test that GET requests to resource_access_controls endpoint succeed with access_control:read scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user, label="test_key", secure_value=hash_key_value(key_value), scopes=["access_control:read"]
        )

        response = self.client.get(
            "/api/projects/@current/resource_access_controls", headers={"authorization": f"Bearer {key_value}"}
        )
        assert response.status_code == status.HTTP_200_OK

    def test_notebook_access_controls_get_requires_access_control_read_scope(self):
        """Test that GET requests to notebook access_controls endpoint require access_control:read scope"""
        notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="test-scope", title="test notebook"
        )

        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key",
            secure_value=hash_key_value(key_value),
            scopes=["project:read"],  # Only project:read, no access_control:read
        )

        response = self.client.get(
            f"/api/projects/@current/notebooks/{notebook.short_id}/access_controls",
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "access_control:read" in response.json()["detail"]

    def test_notebook_access_controls_get_succeeds_with_access_control_read_scope(self):
        """Test that GET requests to notebook access_controls endpoint succeed with access_control:read scope"""
        notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="test-scope", title="test notebook"
        )

        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user, label="test_key", secure_value=hash_key_value(key_value), scopes=["access_control:read"]
        )

        response = self.client.get(
            f"/api/projects/@current/notebooks/{notebook.short_id}/access_controls",
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_200_OK

    def test_notebook_access_controls_put_fails_with_only_read_scope(self):
        """Test that PUT requests to notebook access_controls endpoint fail with only access_control:read scope"""
        notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="test-scope", title="test notebook"
        )

        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key",
            secure_value=hash_key_value(key_value),
            scopes=["access_control:read"],  # Only read scope, no write permissions
        )

        response = self.client.put(
            f"/api/projects/@current/notebooks/{notebook.short_id}/access_controls",
            {"organization_member": str(self.organization_membership.id), "access_level": "viewer"},
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "access_control:write" in response.json()["detail"]

    def test_notebook_access_controls_put_succeeds_with_write_scope(self):
        """Test that PUT requests to notebook access_controls endpoint succeed with access_control:write scope"""
        notebook = Notebook.objects.create(
            team=self.team, created_by=self.user, short_id="test-scope", title="test notebook"
        )

        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key_write",
            secure_value=hash_key_value(key_value),
            scopes=["access_control:write"],  # Write scope required for PUT
        )

        response = self.client.put(
            f"/api/projects/@current/notebooks/{notebook.short_id}/access_controls",
            {"organization_member": str(self.organization_membership.id), "access_level": "viewer"},
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_200_OK

    def test_project_access_controls_put_fails_with_only_read_scope(self):
        """Test that PUT requests to project access_controls endpoint fail with only access_control:read scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key_project_read",
            secure_value=hash_key_value(key_value),
            scopes=["access_control:read"],  # Only read scope, no write permissions
        )

        response = self.client.put(
            f"/api/projects/@current/access_controls",
            {"access_level": "editor"},
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "access_control:write" in response.json()["detail"]

    def test_project_access_controls_put_succeeds_with_write_scope(self):
        """Test that PUT requests to project access_controls endpoint succeed with access_control:write scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key_project_write",
            secure_value=hash_key_value(key_value),
            scopes=["access_control:write"],  # Write scope required for PUT
        )

        response = self.client.put(
            f"/api/projects/@current/access_controls",
            {"access_level": "admin", "resource": "project", "resource_id": str(self.team.id)},
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_200_OK

    def test_resource_access_controls_put_fails_with_only_read_scope(self):
        """Test that PUT requests to resource_access_controls endpoint fail with only access_control:read scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key_global_read",
            secure_value=hash_key_value(key_value),
            scopes=["access_control:read"],  # Only read scope, no write permissions
        )

        response = self.client.put(
            f"/api/projects/@current/resource_access_controls",
            {"access_level": "editor", "resource": "notebook"},
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "access_control:write" in response.json()["detail"]

    def test_resource_access_controls_put_succeeds_with_write_scope(self):
        """Test that PUT requests to resource_access_controls endpoint succeed with access_control:write scope"""
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key_global_write",
            secure_value=hash_key_value(key_value),
            scopes=["access_control:write"],  # Write scope required for PUT
        )

        response = self.client.put(
            f"/api/projects/@current/resource_access_controls",
            {"access_level": "editor", "resource": "dashboard"},
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert response.status_code == status.HTTP_200_OK


class TestAccessControlDefaultsEndpoint(BaseAccessControlTest):
    def setUp(self):
        super().setUp()
        self._org_membership(OrganizationMembership.Level.ADMIN)

    def test_response_structure(self):
        """Verify the JSON response has all expected keys."""
        self._put_project_access_control({"access_level": "member"})
        self._put_global_access_control({"resource": "dashboard", "access_level": "viewer"})

        res = self.client.get("/api/projects/@current/access_control_defaults")
        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # Response: available levels, edit permission, project default, per-resource defaults
        expected_top_level_keys = {
            "available_project_levels",
            "available_resource_levels",
            "can_edit",
            "project_access_level",
            "resource_access_levels",
        }
        assert expected_top_level_keys <= set(data.keys())
        assert data["project_access_level"] == "member"

        # Resource entries: saved level and min/max constraints
        expected_resource_entry_keys = {"access_level", "minimum", "maximum"}
        assert expected_resource_entry_keys <= set(data["resource_access_levels"]["dashboard"].keys())
        assert data["resource_access_levels"]["dashboard"]["access_level"] == "viewer"

    def test_no_overrides_returns_default_project_level(self):
        """Without explicit defaults, project uses system default; resources have no saved level."""
        res = self.client.get("/api/projects/@current/access_control_defaults")
        data = res.json()
        from products.access_control.backend.facade.user_access_control import default_access_level

        assert data["project_access_level"] == default_access_level("project")
        for entry in data["resource_access_levels"].values():
            assert entry["access_level"] is None

    def test_all_resources_present(self):
        """All controllable resources appear in resource_access_levels."""
        res = self.client.get("/api/projects/@current/access_control_defaults")
        data = res.json()
        assert "dashboard" in data["resource_access_levels"]
        assert "feature_flag" in data["resource_access_levels"]
        assert "insight" in data["resource_access_levels"]

    def test_only_returns_current_team_defaults(self):
        """Access controls from other teams are not included."""
        from products.access_control.backend.models.access_control import AccessControl

        # Set defaults on current team
        self._put_project_access_control({"access_level": "member"})
        self._put_global_access_control({"resource": "dashboard", "access_level": "viewer"})

        # Create another team and set different defaults directly in DB
        other_team = Team.objects.create(organization=self.organization, name="Other Team")
        AccessControl.objects.create(
            team=other_team, resource="project", resource_id=str(other_team.id), access_level="admin"
        )
        AccessControl.objects.create(team=other_team, resource="dashboard", access_level="editor")

        # Request should only return current team's defaults
        res = self.client.get("/api/projects/@current/access_control_defaults")
        data = res.json()
        assert data["project_access_level"] == "member"
        assert data["resource_access_levels"]["dashboard"]["access_level"] == "viewer"


class TestAccessControlRolesEndpoint(BaseAccessControlTest):
    def setUp(self):
        super().setUp()
        self._org_membership(OrganizationMembership.Level.ADMIN)
        self.role = Role.objects.create(name="Engineering", organization=self.organization)

    def test_query_count_does_not_grow_with_roles(self):
        # Every role resolves through the walker from one shared pool; a per-role fetch creeping
        # back in (e.g. each subject loading the team's rules itself) would make this page N+1
        def query_count() -> int:
            with CaptureQueriesContext(connection) as ctx:
                res = self.client.get("/api/projects/@current/access_control_roles")
            assert res.status_code == status.HTTP_200_OK, res.json()
            return len(ctx.captured_queries)

        with_few = query_count()
        for i in range(10):
            Role.objects.create(name=f"Role {i}", organization=self.organization)
        assert query_count() <= with_few

    def _find_role(self, results, role_id):
        return next((r for r in results if str(r["role_id"]) == str(role_id)), None)

    def test_response_structure(self):
        """Verify the JSON response has all expected keys at each level."""
        self._put_project_access_control({"role": str(self.role.id), "access_level": "admin"})

        res = self.client.get("/api/projects/@current/access_control_roles")
        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # Response: available levels, edit permission, results list
        expected_top_level_keys = {"available_project_levels", "available_resource_levels", "can_edit", "results"}
        assert expected_top_level_keys <= set(data.keys())

        # Role entry: id, name, project access, per-resource access
        role_data = self._find_role(data["results"], self.role.id)
        assert role_data is not None
        expected_role_keys = {"role_id", "role_name", "project", "resources"}
        assert expected_role_keys <= set(role_data.keys())
        assert role_data["role_id"] == str(self.role.id)
        assert role_data["role_name"] == "Engineering"

        # Access entries: saved level, effective/inherited levels, constraints
        expected_access_entry_keys = {
            "access_level",
            "effective_access_level",
            "inherited_access",
            "minimum",
            "maximum",
        }
        assert expected_access_entry_keys <= set(role_data["project"].keys())
        assert expected_access_entry_keys <= set(role_data["resources"]["dashboard"].keys())

    def test_returns_all_roles(self):
        """All organization roles appear in the results list."""
        role2 = Role.objects.create(name="Support", organization=self.organization)

        res = self.client.get("/api/projects/@current/access_control_roles")
        data = res.json()
        assert len(data["results"]) == 2
        assert self._find_role(data["results"], self.role.id) is not None
        assert self._find_role(data["results"], role2.id) is not None

    def test_no_overrides_returns_nulls(self):
        """Without role-specific overrides, access_level is null."""
        res = self.client.get("/api/projects/@current/access_control_roles")
        role_data = self._find_role(res.json()["results"], self.role.id)
        assert role_data["project"]["access_level"] is None
        for entry in role_data["resources"].values():
            assert entry["access_level"] is None

    def test_project_defaults_populated_without_explicit_entries(self):
        """Project defaults should use hardcoded defaults when no AccessControl entries exist."""
        from products.access_control.backend.models.access_control import AccessControl

        # Ensure no access controls exist for this team
        AccessControl.objects.filter(team=self.team).delete()

        res = self.client.get("/api/projects/@current/access_control_roles")
        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # All roles should have effective project access from the hardcoded default
        role_data = self._find_role(data["results"], self.role.id)

        # Project: effective "admin" from hardcoded default (default_access_level("project") == "admin")
        assert role_data["project"]["access_level"] is None
        assert role_data["project"]["effective_access_level"] == "admin"
        assert role_data["project"]["inherited_access"]["access_level"] == "admin"
        assert role_data["project"]["inherited_access"]["source"] == "system_default"

        # Resources: no rules anywhere, so the built-in default applies and is attributed as such
        ff = role_data["resources"]["feature_flag"]
        assert ff["access_level"] is None
        assert ff["effective_access_level"] == "editor"
        assert ff["inherited_access"]["source"] == "system_default"


class TestAccessControlMembersEndpoint(BaseAccessControlTest):
    def setUp(self):
        super().setUp()
        self._org_membership(OrganizationMembership.Level.ADMIN)
        self.user2 = self._create_user("user2@example.com")
        self.user2_membership = self.user2.organization_memberships.get(organization=self.organization)
        self.role = Role.objects.create(name="Engineering", organization=self.organization)

    def _query_count(self, url: str) -> int:
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(url)
        assert res.status_code == status.HTTP_200_OK, res.json()
        return len(ctx.captured_queries)

    def test_query_count_does_not_grow_with_members(self):
        # Every member resolves through the walker; a per-member fetch creeping back in would make
        # this page N+1 (it was, before the rules were preloaded once for all subjects)
        for i in range(3):
            user = self._create_user(f"early{i}@example.com")
            membership = user.organization_memberships.get(organization=self.organization)
            RoleMembership.objects.create(user=user, role=self.role, organization_member=membership)
        with_few = self._query_count("/api/projects/@current/access_control_members")

        for i in range(12):
            user = self._create_user(f"late{i}@example.com")
            membership = user.organization_memberships.get(organization=self.organization)
            RoleMembership.objects.create(user=user, role=self.role, organization_member=membership)
        with_many = self._query_count("/api/projects/@current/access_control_members")

        assert with_many <= with_few

    def _find_member(self, results, membership_id):
        return next((m for m in results if str(m["organization_membership_id"]) == str(membership_id)), None)

    def test_response_structure(self):
        """Verify the JSON response has all expected keys at each level."""
        self._put_project_access_control(
            {"organization_member": str(self.user2_membership.id), "access_level": "admin"}
        )

        res = self.client.get("/api/projects/@current/access_control_members")
        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # Response: available levels, edit permission, results list
        expected_top_level_keys = {"available_project_levels", "available_resource_levels", "can_edit", "results"}
        assert expected_top_level_keys <= set(data.keys())

        # Member entry: user info, org level, project access, per-resource access
        member_data = self._find_member(data["results"], self.user2_membership.id)
        assert member_data is not None
        expected_member_keys = {"organization_membership_id", "user", "organization_level", "project", "resources"}
        assert expected_member_keys <= set(member_data.keys())
        assert member_data["organization_membership_id"] == str(self.user2_membership.id)

        # User object: identity fields
        expected_user_keys = {"uuid", "first_name", "last_name", "email"}
        assert expected_user_keys <= set(member_data["user"].keys())
        assert member_data["user"]["email"] == "user2@example.com"

        # Access entries: saved level, effective/inherited levels, constraints
        expected_access_entry_keys = {
            "access_level",
            "effective_access_level",
            "inherited_access",
            "minimum",
            "maximum",
        }
        assert expected_access_entry_keys <= set(member_data["project"].keys())
        assert expected_access_entry_keys <= set(member_data["resources"]["dashboard"].keys())

    def test_returns_all_active_members(self):
        """All org members are included in the response."""
        res = self.client.get("/api/projects/@current/access_control_members")
        data = res.json()
        member_ids = [str(m["organization_membership_id"]) for m in data["results"]]
        assert str(self.organization_membership.id) in member_ids
        assert str(self.user2_membership.id) in member_ids

    def test_no_overrides_returns_nulls(self):
        """Without member-specific overrides, access_level is null."""
        res = self.client.get("/api/projects/@current/access_control_members")
        member_data = self._find_member(res.json()["results"], self.user2_membership.id)
        assert member_data["project"]["access_level"] is None
        for entry in member_data["resources"].values():
            assert entry["access_level"] is None

    def test_project_defaults_populated_without_explicit_entries(self):
        """Project defaults should use hardcoded defaults when no AccessControl entries exist."""
        from products.access_control.backend.models.access_control import AccessControl

        # Ensure no access controls exist for this team
        AccessControl.objects.filter(team=self.team).delete()

        res = self.client.get("/api/projects/@current/access_control_members")
        assert res.status_code == status.HTTP_200_OK
        data = res.json()

        # All members should have effective project access from the hardcoded default
        member_data = self._find_member(data["results"], self.user2_membership.id)

        # Project: effective "admin" from hardcoded default (default_access_level("project") == "admin")
        assert member_data["project"]["access_level"] is None
        assert member_data["project"]["effective_access_level"] == "admin"
        assert member_data["project"]["inherited_access"]["access_level"] == "admin"
        assert member_data["project"]["inherited_access"]["source"] == "system_default"

        # Resources: no rules anywhere, so the built-in default applies and is attributed as such
        ff = member_data["resources"]["feature_flag"]
        assert ff["access_level"] is None
        assert ff["effective_access_level"] == "editor"
        assert ff["inherited_access"]["source"] == "system_default"


class TestAccessControlSubjectRulesEndpoints(BaseAccessControlTest):
    def setUp(self):
        super().setUp()
        self._org_membership(OrganizationMembership.Level.ADMIN)

    @parameterized.expand(
        [
            ("member_objects", "access_control_member_objects", "member_id"),
            ("member_properties", "access_control_member_properties", "member_id"),
            ("role_objects", "access_control_role_objects", "role_id"),
            ("role_properties", "access_control_role_properties", "role_id"),
            ("members_list", "access_control_members", "member_id"),
            ("roles_list", "access_control_roles", "role_id"),
        ]
    )
    def test_subject_from_another_organization_is_404(self, _name, endpoint, param):
        other_org = Organization.objects.create(name="Other org")
        other_user = User.objects.create_and_join(other_org, "other-org-user@posthog.com", None)
        other_membership = OrganizationMembership.objects.get(user=other_user, organization=other_org)
        other_role = Role.objects.create(name="Other org role", organization=other_org)

        subject_id = other_membership.id if param == "member_id" else other_role.id
        res = self.client.get(f"/api/projects/@current/{endpoint}?{param}={subject_id}")
        assert res.status_code == status.HTTP_404_NOT_FOUND, res.json()

    def test_member_filter_narrows_the_members_list_to_one_row(self):
        User.objects.create_and_join(self.organization, "second-member@posthog.com", None)
        res = self.client.get(
            f"/api/projects/@current/access_control_members?member_id={self.organization_membership.id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        results = res.json()["results"]
        assert [r["organization_membership_id"] for r in results] == [str(self.organization_membership.id)]

    def test_role_filter_narrows_the_roles_list_to_one_row(self):
        role = Role.objects.create(name="Engineering", organization=self.organization)
        Role.objects.create(name="Support", organization=self.organization)
        res = self.client.get(f"/api/projects/@current/access_control_roles?role_id={role.id}")
        assert res.status_code == status.HTTP_200_OK, res.json()
        results = res.json()["results"]
        assert [r["role_id"] for r in results] == [str(role.id)]

    def test_object_search_and_lookup(self):
        Dashboard.objects.create(team=self.team, name="Growth dashboard", created_by=self.user)
        insight = Insight.objects.create(
            team=self.team, derived_name="Weekly signups", saved=True, created_by=self.user
        )
        notebook = Notebook.objects.create(team=self.team, title="Q3 planning", created_by=self.user)

        res = self.client.get("/api/projects/@current/access_control_object_search?resource=notebook&search=q3")
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert [(r["id"], r["name"]) for r in res.json()["results"]] == [(str(notebook.id), "Q3 planning")]

        # An insight URL carries the short_id; the lookup returns the pk that rules store
        res = self.client.get(
            f"/api/projects/@current/access_control_object_search?resource=insight&id={insight.short_id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert [(r["id"], r["name"]) for r in res.json()["results"]] == [(str(insight.id), "Weekly signups")]

        # Notebook URLs carry a short_id too
        res = self.client.get(
            f"/api/projects/@current/access_control_object_search?resource=notebook&id={notebook.short_id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert [(r["id"], r["name"]) for r in res.json()["results"]] == [(str(notebook.id), "Q3 planning")]

        res = self.client.get("/api/projects/@current/access_control_object_search?resource=webhook")
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()

    def test_object_search_labels_a_ticket_by_its_number(self):
        ticket = Ticket.objects.create(team=self.team, ticket_number=66184)

        res = self.client.get("/api/projects/@current/access_control_object_search?resource=ticket&search=6618")
        assert res.status_code == status.HTTP_200_OK, res.json()
        # A bare number doesn't read as an object, so the label matches the ticket page's own title
        assert [(r["id"], r["name"]) for r in res.json()["results"]] == [(str(ticket.id), "Ticket: 66184")]

    def test_object_search_labels_an_evaluation_by_name(self):
        evaluation = Evaluation.objects.create(
            team=self.team,
            name="Helpful evaluator",
            evaluation_type="hog",
            evaluation_config={"source": "return true"},
            output_type="boolean",
        )

        res = self.client.get("/api/projects/@current/access_control_object_search?resource=evaluation&search=Helpful")

        assert res.status_code == status.HTTP_200_OK
        assert [(r["id"], r["name"]) for r in res.json()["results"]] == [(str(evaluation.id), "Helpful evaluator")]

    def test_object_search_finds_objects_whose_deleted_flag_is_null(self):
        # SessionRecording.deleted has no default, so rows carry NULL, which filter(deleted=False)
        # drops: the picker returned nothing at all for the resource
        recording = SessionRecording.objects.create(team=self.team, session_id="abc-123")
        assert recording.deleted is None

        res = self.client.get(
            "/api/projects/@current/access_control_object_search?resource=session_recording&search=abc-123"
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert [(r["id"], r["name"]) for r in res.json()["results"]] == [(str(recording.id), "abc-123")]

    def test_object_rules_write_addresses_objects_by_pk(self):
        notebook = Notebook.objects.create(team=self.team, title="Q3 planning", created_by=self.user)
        member_id = str(self.organization_membership.id)
        res = self.client.put(
            "/api/projects/@current/access_control_object_rules",
            {
                "resource": "notebook",
                "resource_id": str(notebook.id),
                "access_level": "viewer",
                "organization_member": member_id,
            },
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        rows = self.client.get(f"/api/projects/@current/access_control_member_objects?member_id={member_id}").json()[
            "results"
        ]
        assert [(r["resource"], r["resource_id"], r["name"]) for r in rows] == [
            ("notebook", str(notebook.id), "Q3 planning")
        ]

        res = self.client.put(
            "/api/projects/@current/access_control_object_rules",
            {
                "resource": "notebook",
                "resource_id": str(notebook.id),
                "access_level": None,
                "organization_member": member_id,
            },
        )
        assert res.status_code == status.HTTP_204_NO_CONTENT
        rows = self.client.get(f"/api/projects/@current/access_control_member_objects?member_id={member_id}").json()[
            "results"
        ]
        assert rows == []

    def test_object_rules_write_requires_access_control_write_scope(self):
        dashboard = Dashboard.objects.create(team=self.team, name="Scoped", created_by=self.user)
        key_value = generate_random_token_personal()
        PersonalAPIKey.objects.create(
            user=self.user,
            label="test_key",
            secure_value=hash_key_value(key_value),
            scopes=["access_control:read"],
        )

        res = self.client.put(
            "/api/projects/@current/access_control_object_rules",
            {"resource": "dashboard", "resource_id": str(dashboard.id), "access_level": "viewer"},
            headers={"authorization": f"Bearer {key_value}"},
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()
        assert "access_control:write" in res.json()["detail"]

    def test_object_rules_write_keeps_permission_and_level_validation(self):
        creator = User.objects.create_and_join(self.organization, "creator@posthog.com", None)
        dashboard = Dashboard.objects.create(team=self.team, name="Locked", created_by=creator)

        res = self.client.put(
            "/api/projects/@current/access_control_object_rules",
            {"resource": "dashboard", "resource_id": str(dashboard.id), "access_level": "bad"},
        )
        assert res.status_code == status.HTTP_400_BAD_REQUEST, res.json()

        self._org_membership(OrganizationMembership.Level.MEMBER)
        res = self.client.put(
            "/api/projects/@current/access_control_object_rules",
            {"resource": "dashboard", "resource_id": str(dashboard.id), "access_level": "viewer"},
        )
        assert res.status_code == status.HTTP_403_FORBIDDEN, res.json()

    def test_default_objects_returns_only_rows_without_a_subject(self):
        shared = Dashboard.objects.create(team=self.team, name="Shared dashboard", created_by=self.user)
        scoped = Dashboard.objects.create(team=self.team, name="Scoped dashboard", created_by=self.user)
        role = Role.objects.create(name="Engineering", organization=self.organization)
        AccessControl.objects.create(
            team=self.team, resource="dashboard", resource_id=str(shared.id), access_level="editor"
        )
        AccessControl.objects.create(
            team=self.team,
            resource="dashboard",
            resource_id=str(scoped.id),
            access_level="none",
            organization_member=self.organization_membership,
        )
        AccessControl.objects.create(
            team=self.team, resource="dashboard", resource_id=str(scoped.id), access_level="viewer", role=role
        )

        res = self.client.get("/api/projects/@current/access_control_default_objects")
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert [(r["resource_id"], r["access_level"]) for r in res.json()["results"]] == [(str(shared.id), "editor")]

    def test_member_objects_excludes_the_project_access_row(self):
        dashboard = Dashboard.objects.create(team=self.team, name="Growth", created_by=self.user)
        AccessControl.objects.create(
            team=self.team,
            resource="project",
            resource_id=str(self.team.id),
            access_level="admin",
            organization_member=self.organization_membership,
        )
        AccessControl.objects.create(
            team=self.team,
            resource="dashboard",
            resource_id=str(dashboard.id),
            access_level="editor",
            organization_member=self.organization_membership,
        )

        res = self.client.get(
            f"/api/projects/@current/access_control_member_objects?member_id={self.organization_membership.id}"
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        assert [r["resource"] for r in res.json()["results"]] == ["dashboard"]

    def test_object_names_resolve_with_raw_id_fallback(self):
        dashboard = Dashboard.objects.create(team=self.team, name="Growth dashboard", created_by=self.user)
        insight = Insight.objects.create(team=self.team, derived_name="Weekly signups", created_by=self.user)
        AccessControl.objects.create(
            team=self.team, resource="dashboard", resource_id=str(dashboard.id), access_level="editor"
        )
        AccessControl.objects.create(
            team=self.team, resource="insight", resource_id=str(insight.id), access_level="viewer"
        )
        AccessControl.objects.create(team=self.team, resource="dashboard", resource_id="999999", access_level="none")
        table = DataWarehouseTable.objects.create(
            team=self.team, name="events_parquet", format="Parquet", url_pattern="s3://bucket/events/*"
        )
        AccessControl.objects.create(
            team=self.team, resource="warehouse_table", resource_id=str(table.id), access_level="editor"
        )

        res = self.client.get("/api/projects/@current/access_control_default_objects")
        assert res.status_code == status.HTTP_200_OK, res.json()
        rows = {r["resource_id"]: r for r in res.json()["results"]}
        assert rows[str(dashboard.id)]["name"] == "Growth dashboard"
        # Insight.name is empty, so the derived name shows, with short_id alongside for linking
        assert rows[str(insight.id)]["name"] == "Weekly signups"
        assert rows[str(insight.id)]["short_id"] == insight.short_id
        # Resolved through _MODELS_NOT_IN_ENTITY_MAP, since search doesn't index warehouse tables
        assert rows[str(table.id)]["name"] == "events_parquet"
        # A rule pointing at a missing object keeps the raw id as its name
        assert rows["999999"]["name"] == "999999"


class TestCohortUsedInAccessControl(BaseAccessControlTest):
    def setUp(self):
        super().setUp()
        self.other_user = self._create_user("other_user")

    def _cohort_flag_filters(self, cohort_id: int) -> dict:
        return {"groups": [{"properties": [{"key": "id", "value": cohort_id, "type": "cohort"}]}]}

    def _create_cohort_with_restricted_flag(self) -> Cohort:
        # Two flags reference the cohort; "hidden-flag" gets object-level "none" access.
        # Leaves the current user as an org ADMIN.
        cohort = Cohort.objects.create(team=self.team, name="Target Cohort")
        FeatureFlag.objects.create(
            team=self.team,
            created_by=self.user,
            key="visible-flag",
            name="Visible Flag",
            filters=self._cohort_flag_filters(cohort.id),
        )
        hidden_flag = FeatureFlag.objects.create(
            team=self.team,
            created_by=self.other_user,
            key="hidden-flag",
            name="Hidden Flag",
            filters=self._cohort_flag_filters(cohort.id),
        )

        self._org_membership(OrganizationMembership.Level.ADMIN)
        res = self.client.put(
            f"/api/projects/@current/feature_flags/{hidden_flag.id}/access_controls",
            {"access_level": "none"},
        )
        assert res.status_code == status.HTTP_200_OK, res.json()
        return cohort

    def test_used_in_includes_restricted_flags_for_org_admins(self):
        cohort = self._create_cohort_with_restricted_flag()

        res = self.client.get(f"/api/projects/@current/cohorts/{cohort.id}/used_in")
        assert res.status_code == status.HTTP_200_OK, res.json()
        block = res.json()["feature_flags"]
        assert [flag["key"] for flag in block["results"]] == ["visible-flag", "hidden-flag"]
        assert block["total"] == 2
        assert block["has_more"] is False
