"""Enterprise event definitions. Those models now belong to the event_definitions app."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("ee", "0037_add_conversation_approval_decisions")]

    operations = []
