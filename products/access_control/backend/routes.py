"""Route registration for access_control. Auto-discovered by posthog/api/rest_router.py."""

from posthog.api.routing import RouterRegistry

from products.access_control.backend.presentation.role import RoleMembershipViewSet, RoleViewSet


def register_routes(routers: RouterRegistry) -> None:
    organization_roles_router = routers.organizations.register(
        r"roles", RoleViewSet, "organization_roles", ["organization_id"]
    )
    organization_roles_router.register(
        r"role_memberships",
        RoleMembershipViewSet,
        "organization_role_memberships",
        ["organization_id", "role_id"],
    )
