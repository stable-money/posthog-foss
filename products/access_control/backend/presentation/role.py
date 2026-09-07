"""Organization Roles / Role Memberships REST API.

A Role is a named permission-group scoped to an organization; a RoleMembership
attaches one organization member to one role. Both are org-admin-write /
any-org-member-read, and every row is scoped to the organization named in the
URL. See ``products/access_control/backend/models/role.py`` for the models
this operates on.
"""

from typing import Any

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, QuerySet

from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework import mixins, serializers, viewsets
from rest_framework.exceptions import NotFound
from social_django.models import UserSocialAuth

from posthog.api.organization_member import OrganizationMemberSerializer
from posthog.api.routing import TeamAndOrgViewSetMixin
from posthog.api.shared import UserBasicSerializer
from posthog.models import OrganizationMembership
from posthog.models.webauthn_credential import WebauthnCredential
from posthog.permissions import OrganizationAdminWritePermissions, TimeSensitiveActionPermission

from products.access_control.backend.facade.subject_access_control import restricted_visible_membership_ids
from products.access_control.backend.models.role import Role, RoleMembership


def _organization_member_queryset() -> QuerySet:
    """OrganizationMembership rows ready for ``OrganizationMemberSerializer``.

    That serializer's ``last_login`` field reads a plain ``last_login`` attribute — there's no
    such column on the model, only ``user.last_login`` — so anywhere an OrganizationMembership
    reaches it (list, retrieve, or the one just created by a POST) needs this annotation or
    serialization raises AttributeError. Mirrors ``organization_members_base_queryset`` plus the
    same ``F("user__last_login")`` annotation ``OrganizationMemberViewSet`` itself uses.
    """
    return OrganizationMembership.objects.select_related("user").annotate(last_login=F("user__last_login"))


def _organization_member_user_related_prefetches() -> list[Prefetch]:
    """The nested prefetches ``OrganizationMemberSerializer.get_is_2fa_enabled`` /
    ``get_has_social_auth`` need off the member's user — TOTP devices (confirmed only),
    social-auth records, and WebAuthn credentials (verified only) — rooted at
    ``organization_member__user``. Mirrors the prefetch ``OrganizationMemberViewSet`` itself uses
    (posthog/api/organization_member.py), one level deeper.
    """
    return [
        Prefetch(
            "organization_member__user__totpdevice_set",
            queryset=TOTPDevice.objects.filter(confirmed=True),
        ),
        Prefetch("organization_member__user__social_auth", queryset=UserSocialAuth.objects.all()),
        Prefetch(
            "organization_member__user__webauthn_credentials",
            queryset=WebauthnCredential.objects.filter(verified=True),
        ),
    ]


def role_memberships_prefetch() -> Prefetch:
    """Prefetch for ``Role.roles`` (its ``RoleMembership`` rows) that also warms everything
    ``OrganizationMemberSerializer`` needs off the member's ``organization_member`` — the
    ``last_login`` annotation and the nested TOTP/social-auth/WebAuthn prefetches — so
    serializing a role's members never re-queries, or crashes on a missing annotation, per member.
    """
    return Prefetch(
        "roles",
        queryset=RoleMembership.objects.select_related("user").prefetch_related(
            Prefetch("organization_member", queryset=_organization_member_queryset()),
            *_organization_member_user_related_prefetches(),
        ),
    )


class RoleMembershipSerializer(serializers.ModelSerializer):
    user = UserBasicSerializer(read_only=True)
    organization_member = OrganizationMemberSerializer(read_only=True)
    user_uuid = serializers.UUIDField(write_only=True, required=True)

    class Meta:
        model = RoleMembership
        fields = [
            "id",
            "role_id",
            "organization_member",
            "user",
            "joined_at",
            "updated_at",
            "user_uuid",
        ]
        read_only_fields = [
            "id",
            "role_id",
            "organization_member",
            "user",
            "joined_at",
            "updated_at",
        ]

    def create(self, validated_data: dict[str, Any]) -> RoleMembership:
        user_uuid = validated_data.pop("user_uuid")
        organization_id = self.context["organization_id"]
        role_id = self.context["role_id"]

        try:
            # nosemgrep: idor-lookup-without-org (the org check right below closes this)
            role = Role.objects.get(id=role_id)
        # A malformed role_id reaches here as a string: the nested router's parent lookup is
        # [^/.]+, not UUID-shaped. Django's UUIDField.to_python raises its own ValidationError,
        # which is not a ValueError and which the global exception handler does not shape into a
        # 400 -- so it has to be caught here or the request 500s.
        except (Role.DoesNotExist, DjangoValidationError, ValueError):
            raise serializers.ValidationError("Role does not exist.")

        if str(role.organization_id) != str(organization_id):
            raise serializers.ValidationError("Role does not exist.")

        try:
            organization_membership = _organization_member_queryset().get(
                organization_id=role.organization_id, user__uuid=user_uuid, user__is_active=True
            )
        except OrganizationMembership.DoesNotExist:
            raise serializers.ValidationError("User does not exist.")

        try:
            # The insert needs its own savepoint: an IntegrityError marks the whole
            # surrounding transaction as needing rollback, so catching it and carrying on
            # raises TransactionManagementError on the next query whenever this runs inside
            # an atomic block.
            with transaction.atomic():
                return RoleMembership.objects.create(
                    role=role,
                    organization_member=organization_membership,
                    user=organization_membership.user,
                )
        except IntegrityError:
            raise serializers.ValidationError("User is already part of the role.")


class RoleSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)
    members = serializers.SerializerMethodField()
    is_default = serializers.SerializerMethodField()

    class Meta:
        model = Role
        fields = [
            "id",
            "name",
            "created_at",
            "created_by",
            "members",
            "is_default",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "created_by",
            "is_default",
        ]

    def get_members(self, role: Role) -> list[dict]:
        # role.roles is the RoleMembership related_name (not "memberships") — see the model.
        memberships = role.roles.all()
        visible_membership_ids = self.context.get("visible_membership_ids")
        if visible_membership_ids is not None:
            # Also drops legacy rows with organization_member=NULL for a restricted viewer —
            # str(None) never matches a real membership id, which is the intended fail-closed
            # behaviour rather than a bug to special-case.
            memberships = [
                membership
                for membership in memberships
                if str(membership.organization_member_id) in visible_membership_ids
            ]
        return RoleMembershipSerializer(memberships, many=True, context=self.context).data

    def get_is_default(self, role: Role) -> bool:
        try:
            organization = self.context["view"].organization
        except NotFound:
            return False
        return organization.default_role_id == role.id

    def validate_name(self, name: str) -> str:
        view = self.context["view"]
        queryset = Role.objects.filter(organization=view.organization, name__iexact=name)
        if self.instance is not None:
            queryset = queryset.exclude(pk=self.instance.pk)
        if queryset.exists():
            raise serializers.ValidationError("There is already a role with this name.", code="unique")
        return name

    def create(self, validated_data: dict[str, Any]) -> Role:
        # The org always comes from the URL, never the request body — "organization" isn't even
        # a serializer field, so a client can't set it either way.
        validated_data["organization"] = self.context["view"].organization
        return super().create(validated_data)

    def to_representation(self, instance: Role) -> dict:
        data = super().to_representation(instance)
        visible_user_ids = self.context.get("visible_user_ids")
        if visible_user_ids is not None and str(instance.created_by_id) not in visible_user_ids:
            data["created_by"] = None
        return data


class RoleViewSet(TeamAndOrgViewSetMixin, viewsets.ModelViewSet):
    # Must stay "organization", as the Enterprise implementation this replaced declared:
    # APIScopePermission derives the required PAT/OAuth scope straight from this attribute,
    # so changing it both locks out credentials already scoped organization:write and hands
    # org-wide role administration to any credential holding the narrower access_control:write.
    scope_object = "organization"
    serializer_class = RoleSerializer
    permission_classes = [OrganizationAdminWritePermissions, TimeSensitiveActionPermission]
    queryset = Role.objects.select_related("created_by").prefetch_related(role_memberships_prefetch())

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        visible_membership_ids = restricted_visible_membership_ids(self.organization, self.request.user)
        context["visible_membership_ids"] = visible_membership_ids
        if visible_membership_ids is None:
            context["visible_user_ids"] = None
        else:
            context["visible_user_ids"] = {
                str(user_id)
                for user_id in OrganizationMembership.objects.filter(id__in=visible_membership_ids).values_list(
                    "user_id", flat=True
                )
            }
        return context


class RoleMembershipViewSet(
    TeamAndOrgViewSetMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    # Must stay "organization", as the Enterprise implementation this replaced declared:
    # APIScopePermission derives the required PAT/OAuth scope straight from this attribute,
    # so changing it both locks out credentials already scoped organization:write and hands
    # org-wide role administration to any credential holding the narrower access_control:write.
    scope_object = "organization"
    serializer_class = RoleMembershipSerializer
    permission_classes = [OrganizationAdminWritePermissions, TimeSensitiveActionPermission]
    queryset = RoleMembership.objects.select_related("role", "user").prefetch_related(
        Prefetch("organization_member", queryset=_organization_member_queryset()),
        *_organization_member_user_related_prefetches(),
    )
    # RoleMembership has no direct organization FK, so the URL's organization_id parent-lookup
    # is rewritten to filter through the role instead. role_id itself is left unrewritten, so a
    # URL whose role_id/organization_id don't line up filters to empty on LIST (both clauses
    # apply) but 404s on RETRIEVE (DRF's get_object_or_404 on the same filtered queryset) —
    # different DRF code paths reaching different, both intentional, outcomes.
    filter_rewrite_rules = {"organization_id": "role__organization_id"}

    def safely_get_queryset(self, queryset: QuerySet) -> QuerySet:
        visible_membership_ids = restricted_visible_membership_ids(self.organization, self.request.user)
        if visible_membership_ids is not None:
            queryset = queryset.filter(organization_member_id__in=visible_membership_ids)
        return queryset
