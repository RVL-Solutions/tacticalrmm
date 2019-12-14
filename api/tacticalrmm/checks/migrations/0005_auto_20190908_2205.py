# Generated by Django 2.2.4 on 2019-09-08 22:05

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('checks', '0004_standardcheck_cpuload'),
    ]

    operations = [
        migrations.AlterField(
            model_name='standardcheck',
            name='check_type',
            field=models.CharField(choices=[('disk', 'Disk Check'), ('ping', 'Ping Check'), ('cpuload', 'CPU Load Check'), ('memory', 'Memory Check')], max_length=30),
        ),
    ]