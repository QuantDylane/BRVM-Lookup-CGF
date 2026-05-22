# Generated for ConseilSikafinance — logging quotidien du conseil Sikafinance.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0011_fondamentauxannuel"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConseilSikafinance",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("date_scrape", models.DateField(db_index=True)),
                (
                    "code",
                    models.CharField(
                        choices=[
                            ("ACHETER", "Acheter"),
                            ("RENFORCER", "Renforcer"),
                            ("CONSERVER", "Conserver"),
                            ("ALLEGER", "Alléger"),
                            ("VENDRE", "Vendre"),
                            ("INCONNU", "Inconnu"),
                        ],
                        default="INCONNU",
                        max_length=12,
                    ),
                ),
                ("libelle", models.CharField(blank=True, default="", max_length=20)),
                ("texte", models.TextField(blank=True, default="")),
                ("image_nom", models.CharField(blank=True, default="", max_length=100)),
                ("image_url", models.URLField(blank=True, default="", max_length=500)),
                ("source_url", models.URLField(blank=True, default="", max_length=500)),
                ("date_import", models.DateTimeField(auto_now=True)),
                (
                    "action",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conseils_sika",
                        to="dashboard.action",
                    ),
                ),
            ],
            options={
                "verbose_name": "Conseil Sikafinance",
                "verbose_name_plural": "Conseils Sikafinance",
                "ordering": ["-date_scrape"],
                "indexes": [
                    models.Index(
                        fields=["action", "-date_scrape"],
                        name="dashboard_c_action__5d3e78_idx",
                    )
                ],
                "unique_together": {("action", "date_scrape")},
            },
        ),
    ]
