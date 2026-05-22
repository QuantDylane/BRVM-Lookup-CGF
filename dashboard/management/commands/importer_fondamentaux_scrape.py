"""Importe les fondamentaux annuels scrapés depuis Sikafinance.

Lit les fichiers JSON produits par ``scraper_fondamentaux.py``
(``data/fondamentaux/{TICKER}_fondamentaux.json``) et alimente la table
``FondamentauxAnnuel``. Met aussi à jour les champs scalaires de
``Action`` (per, bnpa, dividende, chiffre_affaires, resultat_net) avec
la valeur du dernier exercice disponible.

Usage:
    python manage.py importer_fondamentaux_scrape
    python manage.py importer_fondamentaux_scrape --dossier "C:/.../fondamentaux"
    python manage.py importer_fondamentaux_scrape --ticker BICC.ci
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand

from dashboard.models import Action, FondamentauxAnnuel

DEFAULT_DIR = Path(
    r"c:/Users/DYLANE/OneDrive - CGF BOURSE/Bureau/MES MINI-APPS/TOOLS/Scraper BRVM/data/fondamentaux"
)


def _parse_number(raw: str) -> float | None:
    """Convertit '1 268', '28,97%', '-11,08%', '23,52' en float. Retourne None si vide/-."""
    if raw is None:
        return None
    s = str(raw).strip().replace("\xa0", " ")
    if not s or s == "-":
        return None
    s = s.replace("%", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


METRIC_FIELD = {
    "Chiffre d'affaires": "chiffre_affaires",
    "Croissance CA": "croissance_ca",
    "Résultat net": "resultat_net",
    "Croissance RN": "croissance_rn",
    "BNPA": "bnpa",
    "PER": "per",
    "Dividende": "dividende",
}


def _ticker_from_filename(stem: str) -> str:
    """``BICC_ci_fondamentaux`` → ``BICC.ci``."""
    base = stem.replace("_fondamentaux", "")
    return re.sub(r"_([a-z]{2})$", r".\1", base)


class Command(BaseCommand):
    help = "Importe les JSON fondamentaux scrapés Sikafinance dans FondamentauxAnnuel."

    def add_arguments(self, parser):
        parser.add_argument("--dossier", default=str(DEFAULT_DIR),
                            help=f"Dossier des JSON (défaut: {DEFAULT_DIR})")
        parser.add_argument("--ticker", default=None,
                            help="N'importe qu'un seul ticker (ex: BICC.ci)")

    def handle(self, *args, **opts):
        dossier = Path(opts["dossier"])
        if not dossier.exists():
            self.stderr.write(self.style.ERROR(f"Dossier introuvable : {dossier}"))
            return

        ticker_filter = opts.get("ticker")
        nb_files = 0
        nb_rows = 0
        nb_skipped = 0
        nb_actions_maj = 0

        for jpath in sorted(dossier.glob("*_fondamentaux.json")):
            ticker = _ticker_from_filename(jpath.stem)
            if ticker_filter and ticker != ticker_filter:
                continue

            try:
                action = Action.objects.get(ticker=ticker)
            except Action.DoesNotExist:
                self.stdout.write(self.style.WARNING(f"  [SKIP] Action inconnue : {ticker}"))
                nb_skipped += 1
                continue

            with open(jpath, "r", encoding="utf-8") as f:
                data = json.load(f)

            annees = [int(y) for y in data.get("annees", []) if re.fullmatch(r"\d{4}", str(y))]
            metrics = data.get("metrics", {})
            source_url = data.get("source", "")

            nb_files += 1

            for annee in annees:
                payload = {"source": "sikafinance", "source_url": source_url}
                has_value = False
                for label, field in METRIC_FIELD.items():
                    raw = metrics.get(label, {}).get(str(annee))
                    val = _parse_number(raw)
                    payload[field] = val
                    if val is not None:
                        has_value = True
                if not has_value:
                    continue

                FondamentauxAnnuel.objects.update_or_create(
                    action=action, exercice=annee, defaults=payload,
                )
                nb_rows += 1

            # MAJ champs scalaires Action avec le dernier exercice qui a un PER
            if annees:
                latest = max(annees)
                latest_rec = FondamentauxAnnuel.objects.filter(
                    action=action, exercice=latest,
                ).first()
                if latest_rec:
                    changed = False
                    for src_field in ("per", "bnpa", "dividende",
                                       "chiffre_affaires", "resultat_net"):
                        v = getattr(latest_rec, src_field)
                        if v is not None and getattr(action, src_field) != v:
                            setattr(action, src_field, v)
                            changed = True
                    if changed:
                        action.save(update_fields=[
                            "per", "bnpa", "dividende",
                            "chiffre_affaires", "resultat_net",
                        ])
                        nb_actions_maj += 1

            self.stdout.write(f"  [OK] {ticker} : {len(annees)} exercices")

        self.stdout.write(self.style.SUCCESS(
            f"\nTerminé. Fichiers={nb_files}  Lignes={nb_rows}  "
            f"Actions mises à jour={nb_actions_maj}  Skippés={nb_skipped}"
        ))
