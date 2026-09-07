"""Hand the hook model to the cdp app.

That app's migration names this node as its dependency, so it runs after this one and
picks the model up as this one puts it down. The table is untouched either way."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("ee", "0045_migrate_feature_flags_models"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="Hook"),
            ],
            # The tables stay. Only the app that owns the models changes.
            database_operations=[],
        ),
    ]
