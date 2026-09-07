"""Hand the trace summary model to the ai_observability app.

That app's migration names this node as its dependency, so it runs after this one."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ee", "0046_migrate_cdp_models"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="LLMTraceSummary"),
            ],
            # The tables stay. Only the app that owns the models changes.
            database_operations=[],
        ),
    ]
