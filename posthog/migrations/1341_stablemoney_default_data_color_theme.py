from django.db import migrations

# Upstream's seeded palette (posthog/migrations/0537_data_color_themes.py).
# Matched exactly before we overwrite, so a theme an admin has already edited
# is left alone.
UPSTREAM_COLORS = [
    "#1d4aff",
    "#621da6",
    "#42827e",
    "#ce0e74",
    "#f14f58",
    "#7c440e",
    "#529a0a",
    "#0476fb",
    "#fe729e",
    "#35416b",
    "#41cbc4",
    "#b64b02",
    "#e4a604",
    "#a56eff",
    "#30d5c8",
]

# Slots 1-8 are a validated categorical set: every adjacent pair clears the
# colour-blind separation gate (worst 9.1 protan) and the normal-vision floor
# (worst 19.6) against a #ffffff card. Three sit under 3:1 on white, which is
# permitted only because PostHog always ships the relief — funnel steps carry
# direct labels and every insight has a "Detailed results" table, so colour is
# never the only channel.
#
# Slots 9-15 are deliberately upstream's. PostHog only reaches them past eight
# series, and a ninth series wants folding into "Other" rather than a ninth hue.
# Keep this list in sync with --data-color-* in
# frontend/src/styles/stablemoney-theme.css.
STABLEMONEY_COLORS = [
    "#2a78d6",
    "#eb6834",
    "#1baf7a",
    "#eda100",
    "#e87ba4",
    "#008300",
    "#4a3aa7",
    "#e34948",
    "#fe729e",
    "#35416b",
    "#41cbc4",
    "#b64b02",
    "#e4a604",
    "#a56eff",
    "#30d5c8",
]


def _swap(apps, old, new):
    DataColorTheme = apps.get_model("posthog", "DataColorTheme")
    # team__isnull=True is the global default; per-team themes are user data.
    DataColorTheme.objects.filter(team__isnull=True, name="Default Theme", colors=old).update(colors=new)


def apply_stablemoney_palette(apps, schema_editor):
    _swap(apps, UPSTREAM_COLORS, STABLEMONEY_COLORS)


def restore_upstream_palette(apps, schema_editor):
    _swap(apps, STABLEMONEY_COLORS, UPSTREAM_COLORS)


class Migration(migrations.Migration):
    dependencies = [("posthog", "1340_drop_userproductlist_reason_columns")]

    operations = [
        migrations.RunPython(apply_stablemoney_palette, restore_upstream_palette),
    ]
