"""Calcul des 13 facteurs financiers à partir de la BD UNIQUEMENT.

Ne dépend d'aucun fichier Excel. Combine :
- ``Action`` (nombre_actions, snapshot des fondamentaux courants)
- ``HistoriqueAction`` (cours, volumes journaliers)
- ``HistoriqueIndice`` (BRVM Composite pour Bêta)
- ``BilanActif``, ``BilanPassif``, ``CompteResultat`` (fondamentaux annuels)

Pour les facteurs fondamentaux, on utilise le DERNIER exercice disponible
≤ ``date_ref`` (ex: au 10/06/2025, on prend l'exercice 2024).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type, timedelta

import numpy as np
from django.db.models import Q

from dashboard.models import (
    Action,
    BilanActif,
    BilanPassif,
    CompteResultat,
    HistoriqueAction,
    HistoriqueIndice,
)


# Constantes de fenêtres
FENETRE_VARIANCE = 60      # jours pour la variance des rendements
FENETRE_MOMENTUM = 126     # jours pour le 6-month momentum (~6 mois ouvrés)
FENETRE_VOLUME = 20        # jours pour la moyenne du volume
FENETRE_BETA = 252         # jours pour le bêta (~1 an ouvré)
TICKER_BENCHMARK = "BRVMC"


@dataclass
class CalculateurFacteurs:
    """Calcule les 13 facteurs pour une action à une date donnée."""

    action: Action
    date_ref: date_type

    # --------------------------- helpers internes ---------------------------

    def _cours(self, d: date_type | None = None) -> float | None:
        """Dernier cours de clôture connu ≤ d (ou date_ref)."""
        d = d or self.date_ref
        h = (
            HistoriqueAction.objects
            .filter(action=self.action, date__lte=d, cloture__isnull=False)
            .order_by("-date")
            .first()
        )
        return float(h.cloture) if h else None

    def _historique_cloture(self, n_jours: int) -> list[float]:
        """Liste des n derniers cours de clôture (du plus ancien au plus récent)."""
        qs = (
            HistoriqueAction.objects
            .filter(action=self.action, date__lte=self.date_ref, cloture__isnull=False)
            .order_by("-date")[:n_jours]
        )
        return [float(h.cloture) for h in reversed(list(qs))]

    def _exercice_courant(self) -> int:
        """Dernier exercice disponible dans CompteResultat pour cette action."""
        cr = (
            CompteResultat.objects
            .filter(action=self.action,
                    exercice__lte=self.date_ref.year,
                    resultat_net__isnull=False)
            .order_by("-exercice")
            .first()
        )
        return cr.exercice if cr else self.date_ref.year - 1

    def _bilan_actif(self):
        return (BilanActif.objects
                .filter(action=self.action, exercice__lte=self.date_ref.year)
                .order_by("-exercice").first())

    def _bilan_passif(self):
        return (BilanPassif.objects
                .filter(action=self.action, exercice__lte=self.date_ref.year)
                .order_by("-exercice").first())

    def _compte_resultat(self):
        return (CompteResultat.objects
                .filter(action=self.action, exercice__lte=self.date_ref.year)
                .order_by("-exercice").first())

    # --------------------------- 13 facteurs ---------------------------

    def capitalisation(self) -> float | None:
        """nombre_actions × cours."""
        cours = self._cours()
        if cours is None or not self.action.nombre_actions:
            return None
        return float(self.action.nombre_actions) * cours

    def book_to_market(self) -> float | None:
        """Capitaux propres / Capitalisation boursière."""
        bp = self._bilan_passif()
        capi = self.capitalisation()
        if not bp or not bp.capitaux_propres or not capi:
            return None
        return float(bp.capitaux_propres) / capi

    def earnings_to_price(self) -> float | None:
        """Résultat net par action / cours = (RN / nb_actions) / cours."""
        cr = self._compte_resultat()
        cours = self._cours()
        if not cr or not cr.resultat_net or not self.action.nombre_actions or not cours:
            return None
        bnpa = float(cr.resultat_net) / float(self.action.nombre_actions)
        return bnpa / cours

    def sales_to_price(self) -> float | None:
        """CA par action / cours."""
        cr = self._compte_resultat()
        cours = self._cours()
        if not cr or not cr.chiffre_affaires or not self.action.nombre_actions or not cours:
            return None
        cape = float(cr.chiffre_affaires) / float(self.action.nombre_actions)
        return cape / cours

    def roa(self) -> float | None:
        """Résultat net / Total actif."""
        cr = self._compte_resultat()
        ba = self._bilan_actif()
        if not cr or not cr.resultat_net or not ba or not ba.total_actif:
            return None
        return float(cr.resultat_net) / float(ba.total_actif)

    def roe(self) -> float | None:
        """Résultat net / Capitaux propres."""
        cr = self._compte_resultat()
        bp = self._bilan_passif()
        if not cr or not cr.resultat_net or not bp or not bp.capitaux_propres:
            return None
        return float(cr.resultat_net) / float(bp.capitaux_propres)

    def levier(self) -> float | None:
        """Total dettes / Capitaux propres (à minimiser)."""
        bp = self._bilan_passif()
        if not bp or not bp.capitaux_propres or not bp.total_dettes:
            return None
        return float(bp.total_dettes) / float(bp.capitaux_propres)

    def dividend_yield(self) -> float | None:
        """Dividende annuel par action / cours."""
        cr = self._compte_resultat()
        cours = self._cours()
        if not cr or cr.dividende_annuel is None or not cours:
            return None
        return float(cr.dividende_annuel) / cours

    def variance(self) -> float | None:
        """Variance des rendements journaliers sur 60 jours (à minimiser)."""
        cours = self._historique_cloture(FENETRE_VARIANCE + 1)
        if len(cours) < 10:
            return None
        arr = np.array(cours)
        rdts = np.diff(arr) / arr[:-1]
        return float(np.var(rdts))

    def rendement_journalier(self) -> float | None:
        cours = self._historique_cloture(2)
        if len(cours) < 2 or cours[-2] == 0:
            return None
        return (cours[-1] - cours[-2]) / cours[-2]

    def momentum_6m(self) -> float | None:
        cours = self._historique_cloture(FENETRE_MOMENTUM + 1)
        if len(cours) < FENETRE_MOMENTUM // 2 or cours[0] == 0:
            return None
        return (cours[-1] - cours[0]) / cours[0]

    def volume_transaction(self) -> float | None:
        """Moyenne 20j du volume_fcfa."""
        qs = (
            HistoriqueAction.objects
            .filter(action=self.action,
                    date__lte=self.date_ref,
                    volume_fcfa__isnull=False)
            .order_by("-date")[:FENETRE_VOLUME]
        )
        vols = [float(h.volume_fcfa) for h in qs]
        return float(np.mean(vols)) if vols else None

    def beta(self) -> float | None:
        """cov(action, BRVM Composite) / var(BRVM)."""
        # Récupérer les rendements alignés
        debut = self.date_ref - timedelta(days=FENETRE_BETA * 2)
        actions_qs = (
            HistoriqueAction.objects
            .filter(action=self.action, date__gte=debut, date__lte=self.date_ref,
                    cloture__isnull=False)
            .order_by("date")
            .values("date", "cloture")
        )
        indice_qs = (
            HistoriqueIndice.objects
            .filter(indice__ticker=TICKER_BENCHMARK, date__gte=debut,
                    date__lte=self.date_ref, cloture__isnull=False)
            .order_by("date")
            .values("date", "cloture")
        )
        df_a = {row["date"]: float(row["cloture"]) for row in actions_qs}
        df_i = {row["date"]: float(row["cloture"]) for row in indice_qs}
        dates_communes = sorted(set(df_a) & set(df_i))
        if len(dates_communes) < 30:
            return None
        ca = np.array([df_a[d] for d in dates_communes])
        ci = np.array([df_i[d] for d in dates_communes])
        ra = np.diff(ca) / ca[:-1]
        ri = np.diff(ci) / ci[:-1]
        var_i = float(np.var(ri))
        if var_i == 0:
            return None
        return float(np.cov(ra, ri, ddof=0)[0, 1]) / var_i

    # --------------------------- API publique ---------------------------

    def calculer_tous(self) -> dict[str, float | None]:
        return {
            "BtM": self.book_to_market(),
            "EP": self.earnings_to_price(),
            "SP": self.sales_to_price(),
            "ROA": self.roa(),
            "ROE": self.roe(),
            "LEVIER": self.levier(),
            "DIV_YIELD": self.dividend_yield(),
            "VARIANCE": self.variance(),
            "RDT_JOURNALIER": self.rendement_journalier(),
            "MOM_6M": self.momentum_6m(),
            "VOLUME": self.volume_transaction(),
            "BETA": self.beta(),
            "CAPI": self.capitalisation(),
        }


def calculer_facteurs_toutes_actions(
    date_ref: date_type, tickers: list[str] | None = None
) -> "pd.DataFrame":
    """Calcule les 13 facteurs pour toutes les actions (ou la liste donnée)
    à la date ``date_ref``. Retourne un DataFrame [ticker × facteurs].
    """
    import pandas as pd

    qs = Action.objects.all()
    if tickers:
        qs = qs.filter(ticker__in=tickers)

    rows = {}
    for action in qs:
        calc = CalculateurFacteurs(action=action, date_ref=date_ref)
        rows[action.ticker] = calc.calculer_tous()
    df = pd.DataFrame.from_dict(rows, orient="index")
    df.index.name = "ticker"
    return df
