# -*- coding: utf-8 -*-
# Generated by Django 1.11.23 on 2019-10-16 18:18
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('submissions', '0001_initial'),
    ]

    operations = [
        migrations.RenameField(
            model_name='submissionattributes',
            old_name='cgac_code',
            new_name='toptier_code',
        ),
    ]