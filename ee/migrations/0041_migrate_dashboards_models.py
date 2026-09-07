"""Dashboards moved to their own app. This app never held them."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("ee", "0038_alter_enterpriseeventdefinition_eventdefinition_ptr_and_more")]

    operations = []
