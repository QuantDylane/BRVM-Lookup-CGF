from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0015_garchfithistorique"),
    ]

    operations = [
        migrations.DeleteModel(name="GarchFitHistorique"),
        migrations.DeleteModel(name="GarchModel"),
    ]
