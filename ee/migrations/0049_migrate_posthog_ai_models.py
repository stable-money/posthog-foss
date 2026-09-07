"""Hand the assistant models to the posthog_ai app.

Waits for the signals migration that moves its own reference to the conversation model
across. Until that has run, dropping the model here would leave that reference pointing
at nothing."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ee", "0047_migrate_ai_observability_models"),
        ("signals", "0027_migrate_posthog_ai_models"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="AgentArtifact"),
                migrations.DeleteModel(name="ConversationCheckpointWrite"),
                migrations.DeleteModel(name="ConversationCheckpointBlob"),
                migrations.DeleteModel(name="ConversationCheckpoint"),
                migrations.DeleteModel(name="Conversation"),
                migrations.DeleteModel(name="CoreMemory"),
            ],
            # The tables stay. Only the app that owns the models changes.
            database_operations=[],
        ),
    ]
