"""Hand the session summary models to the replay app.

The replay migration that takes them over names 0049 as its dependency, so it has run by
the time this one does."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ee", "0049_migrate_posthog_ai_models"),
        ("replay", "0001_migrate_replay_models"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="SessionGroupSummary"),
                migrations.DeleteModel(name="SingleSessionSummary"),
                migrations.DeleteModel(name="TeamSessionSummariesConfig"),
            ],
            # The tables stay. Only the app that owns the models changes.
            database_operations=[],
        ),
    ]
