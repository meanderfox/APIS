# -*- coding: utf-8 -*-
# Generated by Django 1.9.8 on 2016-12-17 18:43
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('registration', '0028_order_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='dealer',
            name='emailed',
            field=models.BooleanField(default=False),
        ),
    ]