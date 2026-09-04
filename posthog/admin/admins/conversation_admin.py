from django.contrib import admin
from django.db.models import Count, Sum
from django.db.models.functions import Length
from django.template.defaultfilters import filesizeformat
from django.urls import reverse
from django.utils.html import format_html

from structlog import get_logger

from products.posthog_ai.backend.models.assistant import Conversation

logger = get_logger()


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "team_link", "user", "status", "type", "title", "updated_at")
    list_select_related = ("team", "user")
    list_filter = ("status", "type")
    search_fields = ("id", "team__name", "user__email")
    autocomplete_fields = ("team", "user")
    # `task` uses raw_id rather than autocomplete: its admin lives in the tasks product and
    # isn't guaranteed registered when ConversationAdmin's system checks run (admin.E039).
    # raw_id_fields needs no registered target admin and still avoids the full-table <select>.
    raw_id_fields = ("task",)
    readonly_fields = ("checkpoint_storage",)
    ordering = ("-updated_at",)

    def has_add_permission(self, request) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        # Conversation is soft-deleted by the app; don't expose a cascading hard-delete here.
        return False

    @admin.display(description="Team")
    def team_link(self, conversation: Conversation):
        return format_html(
            '<a href="{}">{}</a>',
            reverse("admin:posthog_team_change", args=[conversation.team_id]),
            conversation.team.name,
        )

    @admin.display(description="Checkpoint storage")
    def checkpoint_storage(self, conversation: Conversation):
        # Change-page only — do not add to list_display: the bytea Length() sum would run per row.
        # Blobs hold the bulk of a thread's bytes; query them via `thread`, not the `checkpoint` FK.
        checkpoints = conversation.checkpoints.aggregate(
            total=Count("id"), namespaces=Count("checkpoint_ns", distinct=True)
        )
        blobs = conversation.blobs.aggregate(count=Count("id"), total_bytes=Sum(Length("blob")))
        return format_html(
            "{} checkpoints across {} namespace(s), {} blobs ({})",
            checkpoints["total"],
            checkpoints["namespaces"],
            blobs["count"] or 0,
            filesizeformat(blobs["total_bytes"] or 0),
        )
