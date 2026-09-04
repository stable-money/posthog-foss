from django.db import transaction

import structlog

from posthog.exceptions_capture import capture_exception
from posthog.models.organization import Organization

from products.access_control.backend.models.access_control import AccessControl

logger = structlog.get_logger(__name__)


# This migration is used to migrate over team permissions to the new RBAC system
# It will find all teams that have access control enabled and migrate them over to the new system
# It will also remove the access control from the team so we know it's been migrated
def rbac_team_access_control_migration(organization_id: int):
    logger.info("Starting RBAC team migrations", organization_id=organization_id)

    try:
        with transaction.atomic():
            organization = Organization.objects.get(id=organization_id)
            for team in organization.teams.all():
                try:
                    # Skip if access control is already disabled
                    if not team.access_control:
                        continue

                    # Create access control for the team
                    AccessControl.objects.create(
                        team=team,
                        access_level="none",
                        resource="project",
                        resource_id=team.id,
                    )

                    # Per-member access controls were converted from ExplicitTeamMembership,
                    # an enterprise model. This build ships neither that model nor the
                    # migrations that created its table, so there is nothing to convert.

                    # Disable access control for the team (so we know it's been migrated)
                    team.access_control = False
                    team.save()
                except Exception as e:
                    error_message = f"Failed to migrate team {team.id}"
                    logger.exception(error_message, exc_info=e)
                    capture_exception(e, additional_properties={"team_id": team.id, "organization_id": organization_id})
                    raise

        logger.info("Finished RBAC team migrations", organization_id=organization_id)
    except Exception as e:
        error_message = f"Failed to complete RBAC migration for organization {organization_id}"
        logger.exception(error_message, exc_info=e)
        capture_exception(e, additional_properties={"organization_id": organization_id})
        raise
