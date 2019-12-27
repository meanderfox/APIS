# -*- coding: utf-8 -*-
# Generated by Django 1.9.8 on 2016-12-27 17:30
from __future__ import unicode_literals

import datetime

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("registration", "0033_discount_used"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="attendeeRegEnd",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 29, 14, 919844)
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="event",
            name="attendeeRegStart",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 29, 33, 273653)
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="event",
            name="dealerRegEnd",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 30, 2, 634132)
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="event",
            name="dealerRegStart",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 30, 9, 48132)
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="event",
            name="onlineRegEnd",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 30, 13, 626466)
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="event",
            name="onlineRegStart",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 30, 18, 265170)
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="event",
            name="staffRegEnd",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 30, 22, 617158)
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="event",
            name="staffRegStart",
            field=models.DateTimeField(
                default=datetime.datetime(2016, 12, 27, 12, 30, 28, 51655)
            ),
            preserve_default=False,
        ),
    ]
