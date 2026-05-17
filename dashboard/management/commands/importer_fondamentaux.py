"""Importe les fondamentaux annuels (bilans, compte de résultat, flux)
depuis le fichier Excel ``Données Modele HMM FSHMM.xlsx`` vers les tables
BilanActif, BilanPassif, CompteResultat, FluxTresorerie.

Usage:
    python manage.py importer_fondamentaux
    python manage.py importer_fondamentaux --force  # vide les import_excel d'abord
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from dashboard.strategie_hmm.importers.fondamentaux_loader import importer_fondamentaux


class Command(BaseCommand):
    help = "Importe les fondamentaux annuels depuis le fichier Excel raw."

    def add_arguments(self, parser):
        parser.add_argument(
            "--fichier",
            default=None,
            help="Chemin vers le fichier Excel (défaut: data/strategie_hmm/Données Modele HMM FSHMM.xlsx)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Vide les enregistrements 'excel_import' avant import.",
        )

    def handle(self, *args, **opts):
        fichier = Path(opts["fichier"]) if opts["fichier"] else None
        rapport = importer_fondamentaux(fichier=fichier, force=opts["force"])

        self.stdout.write(self.style.SUCCESS(
            f"Feuilles traitées : {len(rapport['feuilles_traitees'])}"
        ))
        for s in rapport["feuilles_traitees"]:
            self.stdout.write(f"  [OK]{s}")
        if rapport["feuilles_manquantes"]:
            self.stdout.write(self.style.WARNING("Feuilles manquantes / vides :"))
            for s in rapport["feuilles_manquantes"]:
                self.stdout.write(f"  [MISS]{s}")

        self.stdout.write(self.style.SUCCESS("Lignes créées par modèle :"))
        for m, n in rapport["compteurs_crees"].items():
            self.stdout.write(f"  {m}: {n}")

        self.stdout.write(
            f"Sociétés matchées : {rapport['n_societes_matchees']}/{rapport['n_societes_total']}"
        )
        if rapport["non_matchees"]:
            self.stdout.write(self.style.WARNING("Non matchées :"))
            for nm in rapport["non_matchees"]:
                self.stdout.write(f"  -{nm}")
