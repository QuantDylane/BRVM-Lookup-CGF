"""Warmup du cache GarchFitHistorique (fits par fin de mois ouvré).

Ce job est COÛTEUX : ~1-3 fits/seconde selon convergence. Pour 216 mois ×
N actions, compter plusieurs heures (offline).

Utilisé pour préparer les simulations de portefeuille sans look-ahead bias :
chaque mois du backtest utilise les paramètres GARCH ajustés UNIQUEMENT sur
les rendements antérieurs à ce mois.

Usage :
    python manage.py warmup_garch_cache                  # toutes les actions
    python manage.py warmup_garch_cache --ticker SGBC.ci # ciblé
    python manage.py warmup_garch_cache --since 2020-01  # depuis cette date
"""
from __future__ import annotations

import time
from datetime import date, datetime

from django.core.management.base import BaseCommand

from dashboard.models import Action
from dashboard.services_garch_fit import ensure_monthly_fits


class Command(BaseCommand):
    help = "Pré-calcule les fits GARCH mensuels pour le simulateur."

    def add_arguments(self, parser):
        parser.add_argument("--ticker", default=None,
                            help="Limite à un seul ticker (ex: SGBC.ci).")
        parser.add_argument("--since", default=None,
                            help="Date début (YYYY-MM-DD ou YYYY-MM). Défaut : tout l'historique.")
        parser.add_argument("--until", default=None,
                            help="Date fin (YYYY-MM-DD ou YYYY-MM). Défaut : aujourd'hui.")

    def _parse(self, s):
        if not s:
            return None
        for fmt in ("%Y-%m-%d", "%Y-%m"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                continue
        raise ValueError(f"Date invalide : {s!r}")

    def handle(self, *args, **opts):
        ticker = opts.get("ticker")
        d_debut = self._parse(opts.get("since"))
        d_fin = self._parse(opts.get("until"))

        qs = Action.objects.all().order_by("ticker")
        if ticker:
            qs = qs.filter(ticker=ticker)
        actions = list(qs)
        if not actions:
            self.stderr.write(self.style.ERROR("Aucune action à traiter."))
            return

        t_start = time.time()
        total_fits = 0
        for i, action in enumerate(actions, 1):
            t_a = time.time()
            self.stdout.write(f"[{i}/{len(actions)}] {action.ticker} ...", ending=" ")
            self.stdout.flush()

            def _cb(j, n, d):
                # Affiche progression seulement tous les 12 fits pour rester lisible
                if j % 12 == 0 or j == n:
                    self.stdout.write(f"\r[{i}/{len(actions)}] {action.ticker} : {j}/{n} ({d})", ending=" ")
                    self.stdout.flush()

            try:
                fits = ensure_monthly_fits(action, date_debut=d_debut,
                                          date_fin=d_fin, progress_cb=_cb)
                dur = time.time() - t_a
                self.stdout.write(self.style.SUCCESS(
                    f"\r[{i}/{len(actions)}] {action.ticker} OK : {len(fits)} fits en {dur:.1f}s"
                ))
                total_fits += len(fits)
            except Exception as e:
                self.stderr.write(self.style.ERROR(
                    f"\r[{i}/{len(actions)}] {action.ticker} ÉCHEC : {e}"
                ))

        self.stdout.write(self.style.SUCCESS(
            f"\nTerminé. {total_fits} fits écrits/lus en {time.time()-t_start:.1f}s "
            f"sur {len(actions)} actions."
        ))
