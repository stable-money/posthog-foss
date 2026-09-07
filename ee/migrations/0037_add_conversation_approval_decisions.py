"""Conversation approval decisions. That model now belongs to the posthog_ai app."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("ee", "0028_alter_conversation_type")]

    operations = []
