# -*- coding: utf-8 -*-
# Generated by Django 1.9.8 on 2017-05-19 02:01
from __future__ import unicode_literals

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('registration', '0050_attendee_surveyok'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='badge',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.CASCADE, to='registration.Badge'),
        ),
    ]
