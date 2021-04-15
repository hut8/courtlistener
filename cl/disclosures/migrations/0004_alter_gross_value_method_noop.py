# Generated by Django 3.1.7 on 2021-03-17 20:24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('disclosures', '0003_add_disclosure_api_permission'),
    ]

    operations = [
        migrations.AlterField(
            model_name='investment',
            name='gross_value_method',
            field=models.CharField(blank=True, choices=[('Q', 'Appraisal'), ('R', 'Cost (Real Estate Only)'), ('S', 'Assessment'), ('T', 'Cash Market'), ('U', 'Book Value'), ('V', 'Other'), ('W', 'Estimated'), ('-1', 'Failed Extraction')], help_text='Investment valuation method code (ex. Q = Appraisal)', max_length=5),
        ),
    ]