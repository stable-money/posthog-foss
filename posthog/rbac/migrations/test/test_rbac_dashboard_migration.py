import pytest
from posthog.test.base import BaseTest

from posthog.constants import AvailableFeature
from posthog.models.organization import Organization, OrganizationMembership
from posthog.models.team.team import Team
from posthog.models.user import User
from posthog.rbac.migrations.rbac_dashboard_migration import rbac_dashboard_access_control_migration

from products.access_control.backend.models.access_control import AccessControl
from products.dashboards.backend.models.dashboard import Dashboard


@pytest.mark.ee
class TestRBACDashboardMigration(BaseTest):
    def setUp(self):
        super().setUp()
        # Enable access control for the organization
        self.organization.available_product_features = [
            {
                "key": AvailableFeature.ACCESS_CONTROL,
                "name": AvailableFeature.ACCESS_CONTROL,
            },
        ]
        self.organization.save()

        # Create additional users and team members
        self.user2 = User.objects.create_and_join(self.organization, "user2@posthog.com", "password123")
        self.user3 = User.objects.create_and_join(self.organization, "user3@posthog.com", "password123")

        # Get organization memberships
        self.user1_membership = OrganizationMembership.objects.get(user=self.user, organization=self.organization)
        self.user2_membership = OrganizationMembership.objects.get(user=self.user2, organization=self.organization)
        self.user3_membership = OrganizationMembership.objects.get(user=self.user3, organization=self.organization)

    def test_migrate_dashboard_with_restriction_level_37_no_privileges(self):
        """Test migrating a dashboard with restriction level 37 but no existing privileges"""
        # Create a dashboard with restriction level 37 (ONLY_COLLABORATORS_CAN_EDIT)
        dashboard = Dashboard.objects.create(
            team=self.team,
            name="Restricted Dashboard",
            restriction_level=Dashboard.RestrictionLevel.ONLY_COLLABORATORS_CAN_EDIT,
        )

        # Verify initial state
        self.assertEqual(dashboard.restriction_level, Dashboard.RestrictionLevel.ONLY_COLLABORATORS_CAN_EDIT)
        self.assertFalse(AccessControl.objects.filter(resource="dashboard", resource_id=str(dashboard.id)).exists())

        # Run migration
        rbac_dashboard_access_control_migration(self.organization.id)

        # Reload dashboard from database
        dashboard.refresh_from_db()

        # Verify dashboard restriction level was updated
        self.assertEqual(dashboard.restriction_level, Dashboard.RestrictionLevel.EVERYONE_IN_PROJECT_CAN_EDIT)

        # Verify default access control was created
        access_controls = AccessControl.objects.filter(resource="dashboard", resource_id=str(dashboard.id))
        self.assertEqual(access_controls.count(), 1)

        default_ac = access_controls.first()
        assert default_ac is not None
        self.assertEqual(default_ac.access_level, "viewer")
        self.assertEqual(default_ac.team_id, self.team.id)
        self.assertIsNone(default_ac.organization_member)
        self.assertIsNone(default_ac.role)

    def test_migrate_dashboard_with_restriction_level_21_ignored(self):
        """Test that dashboards with restriction level 21 are ignored"""
        # Create a dashboard with restriction level 21 (EVERYONE_IN_PROJECT_CAN_EDIT)
        dashboard = Dashboard.objects.create(
            team=self.team,
            name="Unrestricted Dashboard",
            restriction_level=Dashboard.RestrictionLevel.EVERYONE_IN_PROJECT_CAN_EDIT,
        )

        # Run migration
        rbac_dashboard_access_control_migration(self.organization.id)

        # Reload dashboard from database
        dashboard.refresh_from_db()

        # Verify dashboard restriction level was not changed
        self.assertEqual(dashboard.restriction_level, Dashboard.RestrictionLevel.EVERYONE_IN_PROJECT_CAN_EDIT)

        # Verify no access controls were created
        self.assertFalse(AccessControl.objects.filter(resource="dashboard", resource_id=str(dashboard.id)).exists())

    def test_migration_handles_multiple_teams_in_organization(self):
        """Test that migration works correctly with multiple teams in the organization"""
        # Create another team in the same organization
        team2 = Team.objects.create(organization=self.organization, name="Team 2")

        # Create dashboards in both teams
        dashboard1 = Dashboard.objects.create(
            team=self.team,
            name="Team 1 Dashboard",
            restriction_level=Dashboard.RestrictionLevel.ONLY_COLLABORATORS_CAN_EDIT,
        )
        dashboard2 = Dashboard.objects.create(
            team=team2,
            name="Team 2 Dashboard",
            restriction_level=Dashboard.RestrictionLevel.ONLY_COLLABORATORS_CAN_EDIT,
        )

        # Run migration
        rbac_dashboard_access_control_migration(self.organization.id)

        # Verify both dashboards were migrated
        dashboard1.refresh_from_db()
        dashboard2.refresh_from_db()

        self.assertEqual(dashboard1.restriction_level, Dashboard.RestrictionLevel.EVERYONE_IN_PROJECT_CAN_EDIT)
        self.assertEqual(dashboard2.restriction_level, Dashboard.RestrictionLevel.EVERYONE_IN_PROJECT_CAN_EDIT)

        # Verify access controls were created for both dashboards
        self.assertTrue(AccessControl.objects.filter(resource="dashboard", resource_id=str(dashboard1.id)).exists())
        self.assertTrue(AccessControl.objects.filter(resource="dashboard", resource_id=str(dashboard2.id)).exists())

    def test_migration_handles_organization_not_found(self):
        """Test that migration raises exception for non-existent organization"""
        with self.assertRaises(Organization.DoesNotExist):
            rbac_dashboard_access_control_migration(999999)

    def test_migration_is_idempotent(self):
        """Test that running the migration multiple times doesn't create duplicates"""
        # Create a dashboard with restriction level 37
        dashboard = Dashboard.objects.create(
            team=self.team,
            name="Idempotent Test Dashboard",
            restriction_level=Dashboard.RestrictionLevel.ONLY_COLLABORATORS_CAN_EDIT,
        )

        # Run migration first time
        rbac_dashboard_access_control_migration(self.organization.id)

        # Verify state after first migration
        dashboard.refresh_from_db()
        self.assertEqual(dashboard.restriction_level, Dashboard.RestrictionLevel.EVERYONE_IN_PROJECT_CAN_EDIT)
        access_controls_count = AccessControl.objects.filter(
            resource="dashboard", resource_id=str(dashboard.id)
        ).count()
        self.assertEqual(access_controls_count, 1)

        # Run migration second time
        rbac_dashboard_access_control_migration(self.organization.id)

        # Verify no duplicates were created
        final_access_controls_count = AccessControl.objects.filter(
            resource="dashboard", resource_id=str(dashboard.id)
        ).count()
        self.assertEqual(final_access_controls_count, access_controls_count)
