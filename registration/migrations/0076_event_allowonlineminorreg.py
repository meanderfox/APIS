# -*- coding: utf-8 -*-
# Generated by Django 1.11.6 on 2018-07-31 01:18
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("registration", "0075_dealer_logo"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="allowOnlineMinorReg",
            field=models.BooleanField(default=False),
        ),
    ]
