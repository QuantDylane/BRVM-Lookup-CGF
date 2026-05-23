"""Reconstruction rétrospective des verdicts 4-axes par action.

Pour chaque action, calcule la série complète des 8 sous-signaux puis le
verdict pour chaque date à partir de J+252 (de quoi avoir un historique
minimal pour les indicateurs). Persiste dans ``SignalHistorique``.

Sans look-ahead : à la date t, seules les barres ≤ t sont utilisées.
Comme tous les indicateurs sont causaux (SMA, RSI, MACD, ADX, etc.),
calculer la série complète puis trancher à l'index t donne le même
résultat que recalculer chaque indicateur sur la fenêtre [0, t].

Usage:
    python manage.py backfill_signals
    python manage.py backfill_signals --ticker SGBC.ci
    python manage.py backfill_signals --depuis 2020-01-01
    python manage.py backfill_signals --reset  # vide la table avant
"""
from __future__ import annotations

from datetime import date, datetime

import numpy as np
from django.core.management.base import BaseCommand
from django.db import transaction

from dashboard.models import Action, HistoriqueAction, SignalHistorique
from dashboard.services_indicators import (
    sma as ind_sma,
    rsi as ind_rsi,
    macd as ind_macd,
    bbands as ind_bbands,
    adx as ind_adx,
    atr as ind_atr,
    natr as ind_natr,
    obv as ind_obv,
    mfi as ind_mfi,
)
from dashboard.services_verdict import compute_verdict

MIN_BARS_AVANT_VERDICT = 252  # ~1 an de données pour stabilité des indicateurs


def _to_np(values, dtype=float):
    return np.array([v if v is not None else np.nan for v in values], dtype=dtype)


def _val(arr, i):
    """Valeur de la série à l'index i, ou None si NaN/inf."""
    if i < 0 or i >= len(arr):
        return None
    v = arr[i]
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f


class Command(BaseCommand):
    help = "Reconstruit l'historique des verdicts 4-axes (SignalHistorique)."

    def add_arguments(self, parser):
        parser.add_argument("--ticker", default=None,
                            help="Limite à un seul ticker.")
        parser.add_argument("--depuis", default=None,
                            help="Date minimale (YYYY-MM-DD) à recalculer.")
        parser.add_argument("--reset", action="store_true",
                            help="Vide la table SignalHistorique avant.")

    def handle(self, *args, **opts):
        ticker_filter = opts.get("ticker")
        depuis_str = opts.get("depuis")
        depuis: date | None = None
        if depuis_str:
            try:
                depuis = datetime.strptime(depuis_str, "%Y-%m-%d").date()
            except ValueError:
                self.stderr.write(self.style.ERROR(
                    f"--depuis invalide : {depuis_str} (attendu YYYY-MM-DD)"
                ))
                return

        qs = Action.objects.all().order_by("ticker")
        if ticker_filter:
            qs = qs.filter(ticker=ticker_filter)
        actions = list(qs)
        if not actions:
            self.stderr.write(self.style.ERROR("Aucune action à traiter."))
            return

        if opts.get("reset"):
            n_del = SignalHistorique.objects.filter(action__in=actions).delete()
            self.stdout.write(self.style.WARNING(
                f"Reset : {n_del[0]} lignes supprimées."
            ))

        total_inserted = 0
        for i, action in enumerate(actions, 1):
            inserted = self._process_action(action, depuis)
            total_inserted += inserted
            self.stdout.write(
                f"[{i}/{len(actions)}] {action.ticker} : {inserted} signaux"
            )

        self.stdout.write(self.style.SUCCESS(
            f"\nTerminé. {total_inserted} lignes SignalHistorique au total."
        ))

    def _process_action(self, action: Action, depuis: date | None) -> int:
        bars = list(
            HistoriqueAction.objects
            .filter(action=action)
            .order_by("date")
            .values("date", "ouverture", "plus_haut", "plus_bas",
                    "cloture", "volume_titres")
        )
        if len(bars) < MIN_BARS_AVANT_VERDICT + 5:
            return 0

        dates = [b["date"] for b in bars]
        closes = _to_np([b["cloture"] for b in bars])
        highs = _to_np([b["plus_haut"] for b in bars])
        lows = _to_np([b["plus_bas"] for b in bars])
        volumes = _to_np([b["volume_titres"] for b in bars])

        # Séries indicateurs (toutes vectorisées, causales)
        sma20 = ind_sma(closes, 20)
        sma50 = ind_sma(closes, 50)
        rsi14 = ind_rsi(closes, 14)
        _, _, macd_hist = ind_macd(closes)
        bb_up, _, bb_lo = ind_bbands(closes, 20, 2.0)
        adx14, di_p, di_m = ind_adx(highs, lows, closes, 14)
        atr14 = ind_atr(highs, lows, closes, 14)
        natr14 = ind_natr(highs, lows, closes, 14)
        obv_s = ind_obv(closes, volumes)
        mfi14 = ind_mfi(highs, lows, closes, volumes, 14)

        # Pré-existant ? On filtre les dates déjà calculées pour ne pas refaire.
        existing = set(
            SignalHistorique.objects
            .filter(action=action)
            .values_list("date", flat=True)
        )

        to_create: list[SignalHistorique] = []
        start = MIN_BARS_AVANT_VERDICT
        for idx in range(start, len(dates)):
            d = dates[idx]
            if depuis and d < depuis:
                continue
            if d in existing:
                continue

            # natr_series et obv_series : on passe la fenêtre [0..idx] inclus
            natr_window = natr14[: idx + 1].tolist()
            obv_window = obv_s[: idx + 1].tolist()
            # Nettoyer NaN -> None pour les fonctions de verdict
            natr_window = [None if (v is None or (isinstance(v, float) and not np.isfinite(v))) else float(v)
                           for v in natr_window]
            obv_window = [None if (v is None or (isinstance(v, float) and not np.isfinite(v))) else float(v)
                          for v in obv_window]

            verdict = compute_verdict(
                sma20=_val(sma20, idx),
                sma50=_val(sma50, idx),
                adx=_val(adx14, idx),
                di_plus=_val(di_p, idx),
                di_minus=_val(di_m, idx),
                rsi_val=_val(rsi14, idx),
                macd_hist=_val(macd_hist, idx),
                atr_val=_val(atr14, idx),
                last_close=_val(closes, idx),
                bb_upper=_val(bb_up, idx),
                bb_lower=_val(bb_lo, idx),
                natr_series=natr_window,
                obv_series=obv_window,
                mfi_val=_val(mfi14, idx),
            )

            to_create.append(SignalHistorique(
                action=action,
                date=d,
                code=verdict.get("code", "NA"),
                label=verdict.get("label", ""),
                score=verdict.get("score"),
                n_axes_valides=verdict.get("n_axes_valides", 0),
                axes_detail_json=verdict.get("axes", {}),
            ))

        if to_create:
            with transaction.atomic():
                SignalHistorique.objects.bulk_create(
                    to_create, batch_size=500, ignore_conflicts=True,
                )

        return len(to_create)
