"""Create the tables for the models still pinned to the "ee" app label.

The mirror this fork tracks deletes the whole `ee/` directory, migrations included, but
five access control models keep `app_label = "ee"` and sixteen migrations across posthog
and nine products depend on nodes in the deleted history. Eleven more tables are still
named `ee_*` by the product migrations that took those models over, and those migrations
change state only, so nothing creates the tables either. Django builds the full graph
before it runs anything, so every `migrate` failed on the missing nodes.

This chain replaces that history with the smallest thing that resolves it. It is written
from the models in this tree, not copied from the enterprise migrations. Every node the
rest of the tree names exists here; the ones whose original work was a schema change to a
model this app no longer holds carry no operations, because this app creates each model at
its present shape rather than replaying the changes that got it there.

An existing database has 0001 through 0049 recorded already, so nothing here runs on it.
"""

import django.db.models.deletion
import django.contrib.postgres.fields
import django.contrib.postgres.indexes
from django.conf import settings
from django.db import migrations, models

import posthog.uuidt
import posthog.models.utils

import products.posthog_ai.backend.models.assistant


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("posthog", "0001_initial_squashed_0284_improved_caching_state_idx"),
        ("posthog", "__first__"),
    ]

    operations = [
        migrations.CreateModel(
            name="Role",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                ("name", models.CharField(max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "feature_flags_access_level",
                    models.PositiveSmallIntegerField(
                        choices=[(21, "Can only view"), (37, "Can always edit")], default=37
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="roles",
                        related_query_name="role",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="roles",
                        related_query_name="role",
                        to="posthog.organization",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="RoleMembership",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                ("joined_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization_member",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="role_memberships",
                        related_query_name="role_membership",
                        to="posthog.organizationmembership",
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="roles",
                        related_query_name="role",
                        to="ee.role",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="role_memberships",
                        related_query_name="role_membership",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="role",
            name="members",
            field=models.ManyToManyField(through="ee.RoleMembership", to=settings.AUTH_USER_MODEL),
        ),
        migrations.CreateModel(
            name="OrganizationResourceAccess",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "resource",
                    models.CharField(
                        choices=[
                            ("feature flags", "feature flags"),
                            ("experiments", "experiments"),
                            ("cohorts", "cohorts"),
                            ("data management", "data management"),
                            ("session recordings", "session recordings"),
                            ("insights", "insights"),
                            ("dashboards", "dashboards"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "access_level",
                    models.PositiveSmallIntegerField(
                        choices=[(21, "Can only view"), (37, "Can always edit")], default=37
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="resource_access",
                        to="posthog.organization",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "resource"), name="unique resource per organization"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="FeatureFlagRoleAccess",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("added_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "feature_flag",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access",
                        related_query_name="access",
                        to="posthog.featureflag",
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="feature_flag_access",
                        related_query_name="feature_flag_access",
                        to="ee.role",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("role", "feature_flag"), name="unique_feature_flag_and_role")
                ],
            },
        ),
        migrations.CreateModel(
            name="AccessControl",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                ("access_level", models.CharField(max_length=32)),
                ("resource", models.CharField(max_length=32)),
                ("resource_id", models.CharField(max_length=36, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "organization_member",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_controls",
                        related_query_name="access_controls",
                        to="posthog.organizationmembership",
                    ),
                ),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_controls",
                        related_query_name="access_controls",
                        to="posthog.team",
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access_controls",
                        related_query_name="access_controls",
                        to="ee.role",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("resource", "resource_id", "team", "organization_member", "role"),
                        name="unique resource per target",
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="rolemembership",
            constraint=models.UniqueConstraint(fields=("role", "user"), name="unique_user_and_role"),
        ),
        migrations.AddConstraint(
            model_name="role",
            constraint=models.UniqueConstraint(fields=("organization", "name"), name="unique_role_name"),
        ),
        migrations.CreateModel(
            name="Hook",
            fields=[
                (
                    "id",
                    models.CharField(
                        default=posthog.models.utils.generate_random_token,
                        max_length=50,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("event", models.CharField(db_index=True, max_length=64, verbose_name="Event")),
                ("resource_id", models.IntegerField(blank=True, null=True)),
                ("target", models.URLField(max_length=255, verbose_name="Target URL")),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("updated", models.DateTimeField(auto_now=True)),
                (
                    "team",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="rest_hooks", to="posthog.team"
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rest_hooks",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "ee_hook",
            },
        ),
        migrations.CreateModel(
            name="LLMTraceSummary",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True, null=True)),
                (
                    "trace_summary_type",
                    models.CharField(
                        choices=[("issues_search", "Issues Search")], default="issues_search", max_length=100
                    ),
                ),
                ("trace_id", models.CharField(help_text="Trace ID", max_length=255)),
                ("summary", models.CharField(help_text="Trace summary", max_length=1000)),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
            ],
            options={
                "db_table": "ee_llmtracesummary",
                "indexes": [
                    models.Index(
                        fields=["team", "trace_id", "trace_summary_type"], name="ee_llmtrace_team_id_93a147_idx"
                    )
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("team", "trace_id", "trace_summary_type"), name="unique_trace_summary"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="Conversation",
            fields=[
                ("deleted", models.BooleanField(blank=True, default=False, null=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("idle", "Idle"), ("in_progress", "In progress"), ("canceling", "Canceling")],
                        default="idle",
                        max_length=20,
                    ),
                ),
                (
                    "type",
                    models.CharField(
                        choices=[
                            ("assistant", "Assistant"),
                            ("tool_call", "Tool call"),
                            ("deep_research", "Deep research"),
                            ("slack", "Slack"),
                        ],
                        default="assistant",
                        max_length=20,
                    ),
                ),
                (
                    "title",
                    models.CharField(blank=True, help_text="Title of the conversation.", max_length=250, null=True),
                ),
                (
                    "is_internal",
                    models.BooleanField(
                        default=False,
                        help_text="Whether this conversation was created during an impersonated session (e.g., by support agents). Internal conversations are hidden from customers.",
                        null=True,
                    ),
                ),
                (
                    "slack_thread_key",
                    models.CharField(
                        blank=True,
                        help_text="Unique key for Slack thread: '{workspace_id}:{channel}:{thread_ts}'",
                        max_length=200,
                        null=True,
                    ),
                ),
                (
                    "slack_workspace_domain",
                    models.CharField(
                        blank=True,
                        help_text="Slack workspace subdomain (e.g. 'posthog' for posthog.slack.com)",
                        max_length=100,
                        null=True,
                    ),
                ),
                (
                    "approval_decisions",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Stores approval card metadata for dangerous operations (payload lives in checkpoint). Format: {proposal_id: {decision_status: 'pending' | 'approved' | 'rejected' | 'auto_rejected', tool_name: str, preview: str, ...}}",
                    ),
                ),
                (
                    "messages_json",
                    models.JSONField(
                        blank=True,
                        default=None,
                        help_text="Stored messages for non-LangGraph modes (e.g., sandbox).",
                        null=True,
                    ),
                ),
                (
                    "sandbox_task_id",
                    models.UUIDField(
                        blank=True, help_text="Permanent link to Task for sandbox conversations.", null=True
                    ),
                ),
                (
                    "sandbox_run_id",
                    models.UUIDField(
                        blank=True, help_text="Permanent link to current TaskRun for sandbox conversations.", null=True
                    ),
                ),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "ee_conversation",
            },
        ),
        migrations.CreateModel(
            name="AgentArtifact",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True, null=True)),
                ("deleted", models.BooleanField(blank=True, default=False, null=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.uuid7, editable=False, primary_key=True, serialize=False),
                ),
                (
                    "short_id",
                    models.CharField(
                        default=products.posthog_ai.backend.models.assistant.generate_short_id, max_length=4
                    ),
                ),
                ("name", models.CharField(max_length=400)),
                (
                    "type",
                    models.CharField(
                        choices=[("visualization", "Visualization"), ("notebook", "Notebook")], max_length=50
                    ),
                ),
                ("data", models.JSONField(help_text="Artifact content. Structure depends on artifact type.")),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
                (
                    "conversation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="artifacts", to="ee.conversation"
                    ),
                ),
            ],
            options={
                "db_table": "ee_agentartifact",
            },
        ),
        migrations.CreateModel(
            name="ConversationCheckpoint",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                (
                    "checkpoint_ns",
                    models.TextField(
                        default="",
                        help_text='Checkpoint namespace. Denotes the path to the subgraph node the checkpoint originates from, separated by `|` character, e.g. `"child|grandchild"`. Defaults to "" (root graph).',
                    ),
                ),
                ("checkpoint", models.JSONField(help_text="Serialized checkpoint data.", null=True)),
                ("metadata", models.JSONField(help_text="Serialized checkpoint metadata.", null=True)),
                (
                    "parent_checkpoint",
                    models.ForeignKey(
                        help_text="Parent checkpoint ID.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="children",
                        to="ee.conversationcheckpoint",
                    ),
                ),
                (
                    "thread",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="checkpoints", to="ee.conversation"
                    ),
                ),
            ],
            options={
                "db_table": "ee_conversationcheckpoint",
            },
        ),
        migrations.CreateModel(
            name="ConversationCheckpointBlob",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                (
                    "checkpoint_ns",
                    models.TextField(
                        default="",
                        help_text='Checkpoint namespace. Denotes the path to the subgraph node the checkpoint originates from, separated by `|` character, e.g. `"child|grandchild"`. Defaults to "" (root graph).',
                    ),
                ),
                (
                    "channel",
                    models.TextField(
                        help_text="An arbitrary string defining the channel name. For example, it can be a node name or a reserved LangGraph's enum."
                    ),
                ),
                ("version", models.TextField(help_text="Monotonically increasing version of the channel.")),
                ("type", models.TextField(help_text="Type of the serialized blob. For example, `json`.", null=True)),
                ("blob", models.BinaryField(null=True)),
                (
                    "checkpoint",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="blobs",
                        to="ee.conversationcheckpoint",
                    ),
                ),
                (
                    "thread",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="blobs",
                        to="ee.conversation",
                    ),
                ),
            ],
            options={
                "db_table": "ee_conversationcheckpointblob",
            },
        ),
        migrations.CreateModel(
            name="ConversationCheckpointWrite",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                ("task_id", models.UUIDField(help_text="Identifier for the task creating the checkpoint write.")),
                (
                    "idx",
                    models.IntegerField(
                        help_text="Index of the checkpoint write. It is an integer value where negative numbers are reserved for special cases, such as node interruption."
                    ),
                ),
                (
                    "channel",
                    models.TextField(
                        help_text="An arbitrary string defining the channel name. For example, it can be a node name or a reserved LangGraph's enum."
                    ),
                ),
                ("type", models.TextField(help_text="Type of the serialized blob. For example, `json`.", null=True)),
                ("blob", models.BinaryField(null=True)),
                (
                    "checkpoint",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="writes",
                        to="ee.conversationcheckpoint",
                    ),
                ),
            ],
            options={
                "db_table": "ee_conversationcheckpointwrite",
            },
        ),
        migrations.CreateModel(
            name="CoreMemory",
            fields=[
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.UUIDT, editable=False, primary_key=True, serialize=False),
                ),
                (
                    "text",
                    models.TextField(default="", help_text="Dumped core memory where facts are separated by newlines."),
                ),
                ("initial_text", models.TextField(default="", help_text="Scraped memory about the business.")),
                (
                    "scraping_status",
                    models.CharField(
                        blank=True,
                        choices=[("pending", "Pending"), ("completed", "Completed"), ("skipped", "Skipped")],
                        max_length=20,
                        null=True,
                    ),
                ),
                ("scraping_started_at", models.DateTimeField(null=True)),
                ("team", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
            ],
            options={
                "db_table": "ee_corememory",
            },
        ),
        migrations.AddIndex(
            model_name="conversation",
            index=models.Index(fields=["updated_at"], name="ee_conversa_updated_19e4e6_idx"),
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("slack_thread_key__isnull", False)),
                fields=("team", "slack_thread_key"),
                name="unique_team_slack_thread_key",
            ),
        ),
        migrations.AddIndex(
            model_name="agentartifact",
            index=models.Index(fields=["team", "short_id"], name="ee_agentart_team_id_d05587_idx"),
        ),
        migrations.AddIndex(
            model_name="agentartifact",
            index=models.Index(fields=["team", "conversation", "created_at"], name="ee_agentart_team_id_e402e1_idx"),
        ),
        migrations.AddConstraint(
            model_name="agentartifact",
            constraint=models.UniqueConstraint(fields=("team", "short_id"), name="unique_team_short_id"),
        ),
        migrations.AddConstraint(
            model_name="conversationcheckpoint",
            constraint=models.UniqueConstraint(fields=("id", "checkpoint_ns", "thread"), name="unique_checkpoint"),
        ),
        migrations.AddConstraint(
            model_name="conversationcheckpointblob",
            constraint=models.UniqueConstraint(
                fields=("thread_id", "checkpoint_ns", "channel", "version"), name="unique_checkpoint_blob"
            ),
        ),
        migrations.AddConstraint(
            model_name="conversationcheckpointwrite",
            constraint=models.UniqueConstraint(
                fields=("checkpoint_id", "task_id", "idx"), name="unique_checkpoint_write"
            ),
        ),
        migrations.CreateModel(
            name="TeamSessionSummariesConfig",
            fields=[
                (
                    "team",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to="posthog.team",
                    ),
                ),
                (
                    "product_context",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Free-form description of the team's product, used to tailor AI-generated session summaries. Injected into the system prompt of every summary generated for this team.",
                        max_length=10000,
                    ),
                ),
                (
                    "custom_tags",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Team-defined tags layered on top of the fixed taxonomy. Stored as a {name: description} mapping matching the AI_TAGS_FIXED_TAXONOMY shape. Names must be lowercase snake_case. Descriptions tell the LLM when to apply each tag.",
                    ),
                ),
            ],
            options={
                "db_table": "ee_teamsessionsummariesconfig",
            },
        ),
        migrations.CreateModel(
            name="SessionGroupSummary",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.uuid7, editable=False, primary_key=True, serialize=False),
                ),
                (
                    "title",
                    models.CharField(
                        default="Group summary", help_text="Title of the group session summary", max_length=2048
                    ),
                ),
                (
                    "session_ids",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=200),
                        help_text="List of session replay IDs included in this group summary",
                        size=1000,
                    ),
                ),
                (
                    "summary",
                    models.JSONField(
                        help_text="Group summary in JSON format (EnrichedSessionGroupSummaryPatternsList schema)"
                    ),
                ),
                (
                    "extra_summary_context",
                    models.JSONField(
                        blank=True,
                        help_text="Additional context passed to the summary (ExtraSummaryContext schema)",
                        null=True,
                    ),
                ),
                (
                    "run_metadata",
                    models.JSONField(
                        blank=True, help_text="Summary run metadata (SessionSummaryRunMeta schema)", null=True
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
            ],
            options={
                "db_table": "ee_group_session_summary",
                "indexes": [
                    models.Index(fields=["team", "-created_at"], name="ee_group_se_team_id_a105a8_idx"),
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["title"], name="idx_group_summary_title_gin", opclasses=["gin_trgm_ops"]
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SingleSessionSummary",
            fields=[
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "id",
                    models.UUIDField(default=posthog.uuidt.uuid7, editable=False, primary_key=True, serialize=False),
                ),
                ("session_id", models.CharField(help_text="Session replay ID", max_length=200)),
                (
                    "summary",
                    models.JSONField(help_text="Session summary in JSON format (SessionSummarySerializer schema)"),
                ),
                (
                    "exception_event_ids",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=200),
                        blank=True,
                        default=list,
                        help_text="List of event IDs where exceptions occurred for searchability",
                        size=100,
                    ),
                ),
                (
                    "extra_summary_context",
                    models.JSONField(
                        blank=True,
                        help_text="Additional context passed to the summary (ExtraSummaryContext schema)",
                        null=True,
                    ),
                ),
                (
                    "run_metadata",
                    models.JSONField(
                        blank=True, help_text="Summary run metadata (SessionSummaryRunMeta schema)", null=True
                    ),
                ),
                ("session_start_time", models.DateTimeField(blank=True, help_text="Session start time", null=True)),
                (
                    "session_duration",
                    models.IntegerField(blank=True, help_text="Session duration in seconds", null=True),
                ),
                (
                    "distinct_id",
                    models.CharField(
                        blank=True, help_text="Distinct ID of the session's user", max_length=200, null=True
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL
                    ),
                ),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="posthog.team")),
            ],
            options={
                "db_table": "ee_single_session_summary",
                "indexes": [
                    models.Index(fields=["team", "session_id"], name="ee_single_s_team_id_5e726a_idx"),
                    django.contrib.postgres.indexes.GinIndex(
                        fields=["exception_event_ids"], name="idx_exception_event_ids_gin"
                    ),
                ],
            },
        ),
    ]
