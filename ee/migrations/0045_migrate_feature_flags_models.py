"""Point role feature flag access at the feature_flags app.

Feature flags moved out of the posthog app. Only the owning app changes: the column and
its foreign key constraint are the same ones 0001 created, so this is state only.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ee", "0041_migrate_dashboards_models"),
        ("feature_flags", "0002_migrate_feature_flags_models"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="featureflagroleaccess",
                    name="feature_flag",
                    field=models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="access",
                        related_query_name="access",
                        to="feature_flags.featureflag",
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
