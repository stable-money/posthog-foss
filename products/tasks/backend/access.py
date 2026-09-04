from enum import StrEnum
from typing import TYPE_CHECKING

from django.conf import settings

from posthog.models.user import User
from posthog.ph_client import feature_enabled_or_false, get_feature_flag_or_none

from products.tasks.backend.facade.contracts import DesktopAccessReason
from products.tasks.backend.metrics import observe_desktop_access_decision

if TYPE_CHECKING:
    from posthog.models.organization import Organization
    from posthog.models.team.team import Team

DESKTOP_ACCESS_OVERRIDE_FLAG = "posthog-desktop-access-override"


class DesktopAccessResolutionError(Exception):
    pass


class DesktopAccessDecision(StrEnum):
    ALLOWED = "allowed"
    STARTUP_PLAN = "startup_plan"
    PREPAID_CREDITS = "prepaid_credits"

    @property
    def allowed(self) -> bool:
        return self == self.ALLOWED

    @property
    def reason(self) -> DesktopAccessReason | None:
        if self == self.STARTUP_PLAN:
            return DesktopAccessReason.STARTUP_PLAN
        if self == self.PREPAID_CREDITS:
            return DesktopAccessReason.PREPAID_CREDITS
        return None


def get_desktop_access_decision(user: User, organization: "Organization") -> DesktopAccessDecision:
    if not user or not user.is_authenticated or not user.distinct_id:
        raise DesktopAccessResolutionError("Authentication is required to evaluate Desktop access")

    if settings.DEBUG:
        observe_desktop_access_decision(outcome="override")
        return DesktopAccessDecision.ALLOWED

    organization_id = str(organization.id)
    groups = {"organization": organization_id}
    group_properties = {"organization": {"id": organization_id}}
    override_enabled = get_feature_flag_or_none(
        DESKTOP_ACCESS_OVERRIDE_FLAG,
        str(user.distinct_id),
        groups=groups,
        group_properties=group_properties,
        only_evaluate_locally=False,
        send_feature_flag_events=False,
    )
    if not isinstance(override_enabled, bool):
        observe_desktop_access_decision(outcome="resolution_failure")
        raise DesktopAccessResolutionError("Could not evaluate the Desktop access override")
    if override_enabled:
        observe_desktop_access_decision(outcome="override")
        return DesktopAccessDecision.ALLOWED

    # The STARTUP_PLAN and PREPAID_CREDITS denial reasons below are both billing states,
    # read from the billing service. This tree has no billing service, so no organization
    # can ever be in either state and the branch could never fire -- it is removed rather
    # than left as a check that is structurally incapable of denying anything.
    observe_desktop_access_decision(outcome="allowed")
    return DesktopAccessDecision.ALLOWED


def has_loops_access(user: User, team: "Team | None" = None) -> bool:
    if not user.distinct_id:
        return False

    organization = team.organization if team is not None else getattr(user, "organization", None)
    organization_id = str(organization.id) if organization is not None else None
    return feature_enabled_or_false(
        "loops",
        str(user.distinct_id),
        groups={"organization": organization_id} if organization_id is not None else None,
        group_properties={"organization": {"id": organization_id}} if organization_id is not None else None,
        only_evaluate_locally=False,
        send_feature_flag_events=False,
    )
