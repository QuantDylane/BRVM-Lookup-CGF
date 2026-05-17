"""Construction des 13 portefeuilles factoriels long-short à partir de la BD.

Algorithme (mensuel) :
1. Au 1er jour ouvré du mois M, pour chaque facteur F :
   - Calcule F sur toutes les actions
   - Trie : long = top 20 %, short = bottom 20 %
     (sens inversé pour LEVIER, VARIANCE, BETA — facteurs à minimiser)
   - Poids : +1/n_long pour long, -1/n_short pour short
2. Pour chaque jour ouvré de M, le rendement du portefeuille est
   ``Σ poids_i × rendement_journalier_i``.

Résultat persisté dans ``RendementPortefeuilleFactoriel``. Cette table
remplace la dépendance au fichier Excel des rendements pour entraîner le HMM.
"""
from __future__ import annotations

from datetime import date as date_type, timedelta
from typing import Iterable

import numpy as np
import pandas as pd
from django.db import transaction
from django.db.models import Max, Min

from dashboard.models import (
    Action,
    HistoriqueAction,
    RendementPortefeuilleFactoriel,
)
from .facteurs import calculer_facteurs_toutes_actions
from .scoring import FACTEURS_A_INVERSER


FACTEURS_CODES = [
    "BtM", "EP", "SP", "ROA", "ROE", "LEVIER", "DIV_YIELD",
    "VARIANCE", "RDT_JOURNALIER", "MOM_6M", "VOLUME", "BETA", "CAPI",
]
QUINTILE = 0.20  # top/bottom 20 %


def _premier_jour_ouvre(annee: int, mois: int, dates_dispo: set[date_type]) -> date_type | None:
    """Premier jour de cotation effective du mois (présent dans dates_dispo)."""
    d = date_type(annee, mois, 1)
    for _ in range(10):  # max 10 jours pour trouver une date de cotation
        if d in dates_dispo:
            return d
        d += timedelta(days=1)
        if d.month != mois:
            return None
    return None


def _construire_matrice_cours(
    date_debut: date_type, date_fin: date_type
) -> pd.DataFrame:
    """Matrice [date × ticker] des cours de clôture sur la période."""
    qs = (
        HistoriqueAction.objects
        .filter(date__gte=date_debut, date__lte=date_fin, cloture__isnull=False)
        .values("date", "action__ticker", "cloture")
    )
    df = pd.DataFrame.from_records(qs)
    if df.empty:
        return df
    return df.pivot_table(
        index="date", columns="action__ticker", values="cloture", aggfunc="last"
    ).sort_index()


def _trier_quintiles(serie: pd.Series, ascending: bool) -> tuple[list[str], list[str]]:
    """Renvoie (longs, shorts). Si ascending=True, le 'long' = bottom 20 %
    (cas des facteurs à minimiser : on veut posséder les actions au facteur faible).
    """
    s = serie.dropna()
    n = len(s)
    if n < 5:  # garde-fou
        return [], []
    n_quintile = max(1, int(round(n * QUINTILE)))
    s_sorted = s.sort_values(ascending=ascending)
    longs = list(s_sorted.head(n_quintile).index)
    shorts = list(s_sorted.tail(n_quintile).index)
    return longs, shorts


def construire_rendements_factoriels(
    date_debut: date_type | None = None,
    date_fin: date_type | None = None,
    facteurs: Iterable[str] = FACTEURS_CODES,
    persist: bool = True,
    verbose: bool = False,
) -> pd.DataFrame:
    """Construit les rendements journaliers des 13 portefeuilles long-short
    sur la période donnée et les persiste en base.

    Returns
    -------
    DataFrame indexé par date, colonnes = codes facteurs, valeurs = rendements.
    """
    # Bornes par défaut : tout l'historique disponible
    if date_debut is None:
        date_debut = HistoriqueAction.objects.aggregate(d=Min("date"))["d"]
    if date_fin is None:
        date_fin = HistoriqueAction.objects.aggregate(d=Max("date"))["d"]
    if date_debut is None or date_fin is None:
        return pd.DataFrame()

    # 1. Préchargement de la matrice de cours sur toute la période
    cours = _construire_matrice_cours(date_debut, date_fin)
    if cours.empty:
        return pd.DataFrame()
    rendements_actions = cours.pct_change(fill_method=None).fillna(0.0)
    dates_dispo = set(cours.index)

    # 2. Itération par mois
    mois_courant = date_type(date_debut.year, date_debut.month, 1)
    fin_mois = date_type(date_fin.year, date_fin.month, 1)
    # tableau résultat : date × facteur
    rendements_pf = pd.DataFrame(
        index=cours.index, columns=list(facteurs), dtype=float
    )

    while mois_courant <= fin_mois:
        m, a = mois_courant.month, mois_courant.year
        date_tri = _premier_jour_ouvre(a, m, dates_dispo)
        if date_tri is None:
            mois_courant = (mois_courant.replace(day=28) + timedelta(days=4)).replace(day=1)
            continue
        if verbose:
            print(f"  Mois {a}-{m:02d} (tri @ {date_tri})")

        # Calculer tous les facteurs à la date de tri (vectorisé Python)
        df_facteurs = calculer_facteurs_toutes_actions(date_tri)

        # Bornes du mois
        debut_m = date_tri
        # 1er jour du mois suivant
        if m == 12:
            mois_suivant = date_type(a + 1, 1, 1)
        else:
            mois_suivant = date_type(a, m + 1, 1)
        # Toutes les dates de cotation dans [debut_m, mois_suivant)
        dates_du_mois = [d for d in cours.index if debut_m <= d < mois_suivant]
        if not dates_du_mois:
            mois_courant = mois_suivant
            continue

        # Pour chaque facteur, construire les portefeuilles et calculer
        for f in facteurs:
            if f not in df_facteurs.columns:
                continue
            ascending = f in FACTEURS_A_INVERSER
            longs, shorts = _trier_quintiles(df_facteurs[f], ascending=ascending)
            if not longs or not shorts:
                continue
            n_l, n_s = len(longs), len(shorts)
            # rendements du portefeuille pour chaque jour du mois
            # tickers présents dans rendements_actions
            longs_present = [t for t in longs if t in rendements_actions.columns]
            shorts_present = [t for t in shorts if t in rendements_actions.columns]
            if not longs_present or not shorts_present:
                continue
            for d in dates_du_mois:
                if d not in rendements_actions.index:
                    continue
                row = rendements_actions.loc[d]
                rdt_long = row.reindex(longs_present).fillna(0.0).mean()
                rdt_short = row.reindex(shorts_present).fillna(0.0).mean()
                rendements_pf.loc[d, f] = float(rdt_long - rdt_short)

        mois_courant = mois_suivant

    # Filtrage : on garde uniquement les dates où au moins un facteur a un rendement
    rendements_pf = rendements_pf.dropna(how="all")

    if persist:
        objs = []
        for d, row in rendements_pf.iterrows():
            for f, v in row.items():
                if pd.isna(v):
                    continue
                d_py = d if isinstance(d, date_type) else d.date()
                objs.append(RendementPortefeuilleFactoriel(
                    facteur=f, date=d_py, rendement=float(v),
                ))
        with transaction.atomic():
            # On vide d'abord pour éviter les conflits unique_together
            RendementPortefeuilleFactoriel.objects.filter(
                date__gte=rendements_pf.index.min(),
                date__lte=rendements_pf.index.max(),
            ).delete()
            RendementPortefeuilleFactoriel.objects.bulk_create(
                objs, batch_size=2000, ignore_conflicts=True,
            )
        if verbose:
            print(f"  → {len(objs)} rendements persistés")

    return rendements_pf


def charger_rendements_depuis_bd(
    date_debut: date_type | None = None,
    date_fin: date_type | None = None,
) -> pd.DataFrame:
    """Charge les rendements depuis la table BD. Renvoie un DataFrame
    [date × facteur] (équivalent du résultat de l'Excel)."""
    qs = RendementPortefeuilleFactoriel.objects.all()
    if date_debut:
        qs = qs.filter(date__gte=date_debut)
    if date_fin:
        qs = qs.filter(date__lte=date_fin)
    df = pd.DataFrame.from_records(
        qs.values("date", "facteur", "rendement")
    )
    if df.empty:
        return df
    return df.pivot(index="date", columns="facteur", values="rendement").sort_index()
