from django.db import migrations


def trigger_backfill(apps, schema_editor):
    try:
        from ee.api.vercel.tasks import backfill_vercel_connectable_resources
    except ImportError:
        # No enterprise code in this build, so there is no Vercel integration and nothing
        # to backfill. The operation is elidable for the same reason.
        return

    backfill_vercel_connectable_resources.delay()


class Migration(migrations.Migration):
    dependencies = [
        ("posthog", "1073_migrate_dashboards_models"),
    ]

    operations = [
        migrations.RunPython(trigger_backfill, migrations.RunPython.noop, elidable=True),
    ]
