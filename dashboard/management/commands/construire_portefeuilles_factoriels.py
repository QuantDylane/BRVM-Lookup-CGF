"""Construit les rendements journaliers des 13 portefeuilles factoriels
long-short à partir de la BD et les persiste dans
``RendementPortefeuilleFactoriel``.

Usage:
    python manage.py construire_portefeuilles_factoriels --depuis 2017-01-01
    python manage.py construire_portefeuilles_factoriels  # toute la période
"""
from datetime import date, datetime

from django.core.management.base import BaseCommand
from django.db.models import Max, Min

from dashboard.models import HistoriqueAction, RendementPortefeuilleFactoriel
from dashboard.strategie_hmm.core.portefeuilles_factoriels import (
    construire_rendements_factoriels,
)


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


class Command(BaseCommand):
    help = "Construit les rendements des 13 portefeuilles factoriels long-short."

    def add_arguments(self, parser):
        parser.add_argument("--depuis", type=_parse_date, default=None,
                            help="Date de début (YYYY-MM-DD)")
        parser.add_argument("--jusqua", type=_parse_date, default=None,
                            help="Date de fin (YYYY-MM-DD)")
        parser.add_argument("--no-persist", action="store_true",
                            help="N'écrit pas en base (calcul à blanc)")

    def handle(self, *args, **opts):
        depuis = opts["depuis"] or HistoriqueAction.objects.aggregate(d=Min("date"))["d"]
        jusqua = opts["jusqua"] or HistoriqueAction.objects.aggregate(d=Max("date"))["d"]

        self.stdout.write(self.style.NOTICE(
            f"Construction des portefeuilles factoriels : {depuis} -> {jusqua}"
        ))
        df = construire_rendements_factoriels(
            date_debut=depuis,
            date_fin=jusqua,
            persist=not opts["no_persist"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"  Rendements calcules : {df.shape[0]} dates x {df.shape[1]} facteurs"
        ))
        if not opts["no_persist"]:
            self.stdout.write(self.style.SUCCESS(
                f"  Total en BD : {RendementPortefeuilleFactoriel.objects.count()} lignes"
            ))
