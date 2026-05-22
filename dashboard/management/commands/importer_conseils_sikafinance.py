"""Logging quotidien du conseil Sikafinance pour chaque action.

Appelle ``scrape_conseil`` (de ``scraper_info_societes.py``) pour chaque
``Action`` en base et persiste un snapshot daté du jour dans
``ConseilSikafinance``. L'opération est idempotente sur la clé
``(action, date_scrape)`` : relancer plusieurs fois le même jour met à jour
la ligne au lieu d'en créer une nouvelle.

Usage:
    python manage.py importer_conseils_sikafinance
    python manage.py importer_conseils_sikafinance --ticker SGBC.ci
    python manage.py importer_conseils_sikafinance --delay 0.8
"""
from __future__ import annotations

import time
from datetime import date

from django.core.management.base import BaseCommand

from dashboard.models import Action, ConseilSikafinance

from scraper_info_societes import (  # noqa: E402  (racine projet sur sys.path)
    CONSEIL_CODE_TO_LIBELLE,
    create_session,
    scrape_conseil,
)


class Command(BaseCommand):
    help = "Logue le conseil Sikafinance du jour pour chaque action en base."

    def add_arguments(self, parser):
        parser.add_argument(
            "--ticker", default=None,
            help="Limite l'import à un seul ticker (ex: SGBC.ci).",
        )
        parser.add_argument(
            "--delay", type=float, default=0.5,
            help="Délai entre requêtes (secondes, défaut 0.5).",
        )

    def handle(self, *args, **opts):
        ticker_filter = opts.get("ticker")
        delay = float(opts.get("delay") or 0.5)

        qs = Action.objects.all().order_by("ticker")
        if ticker_filter:
            qs = qs.filter(ticker=ticker_filter)

        actions = list(qs)
        if not actions:
            self.stderr.write(self.style.ERROR("Aucune action à traiter."))
            return

        session = create_session()
        today = date.today()
        nb_ok = 0
        nb_inconnu = 0
        nb_err = 0

        for i, action in enumerate(actions, 1):
            ticker = action.ticker
            try:
                info = scrape_conseil(session, ticker)
            except Exception as e:  # noqa: BLE001
                self.stderr.write(self.style.WARNING(
                    f"[{i}/{len(actions)}] {ticker} ERREUR: {e}"
                ))
                nb_err += 1
                time.sleep(delay)
                continue

            code = info.get("Conseil_Code", "INCONNU")
            libelle = info.get("Conseil_Libelle") or CONSEIL_CODE_TO_LIBELLE.get(code, "")

            ConseilSikafinance.objects.update_or_create(
                action=action,
                date_scrape=today,
                defaults={
                    "code": code,
                    "libelle": libelle,
                    "texte": info.get("Conseil_Texte", "")[:5000],
                    "image_nom": info.get("Conseil_Image_Nom", ""),
                    "image_url": info.get("Conseil_Image_URL", ""),
                    "source_url": info.get("Conseil_Source_URL", ""),
                },
            )

            if code == "INCONNU":
                nb_inconnu += 1
                self.stdout.write(self.style.WARNING(
                    f"[{i}/{len(actions)}] {ticker} : conseil non reconnu "
                    f"(image='{info.get('Conseil_Image_Nom', '')}')"
                ))
            else:
                nb_ok += 1
                self.stdout.write(
                    f"[{i}/{len(actions)}] {ticker} : {libelle} ({code})"
                )

            time.sleep(delay)

        self.stdout.write(self.style.SUCCESS(
            f"\nTerminé. OK={nb_ok}  Inconnus={nb_inconnu}  Erreurs={nb_err}"
        ))
