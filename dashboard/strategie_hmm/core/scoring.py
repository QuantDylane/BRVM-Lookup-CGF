"""Projection des poids factoriels sur les actions réelles via scoring multifactoriel."""
from __future__ import annotations

import numpy as np
import pandas as pd

# Facteurs où une valeur PLUS PETITE est meilleure (tri croissant)
FACTEURS_A_INVERSER = {"LEVIER", "VARIANCE", "BETA"}


def scorer_actions(
    df_facteurs: pd.DataFrame,
    poids_facteurs: dict,
    methode: str = "zscore",
) -> pd.DataFrame:
    """Calcule un score par action et déduit les poids.

    Parameters
    ----------
    df_facteurs : DataFrame index=ticker (ou nom), colonnes=codes facteurs
    poids_facteurs : dict {code_facteur: poids}
    methode : 'zscore' ou 'minmax'

    Returns
    -------
    DataFrame avec colonnes [score, poids, rang]
    """
    df = df_facteurs.copy().astype(float)
    df = df.dropna(how="all")

    # Ne garder que les facteurs présents dans poids_facteurs ET dans df
    cols = [c for c in df.columns if c in poids_facteurs]
    df = df[cols].copy()

    # Inversion de signe pour facteurs à minimiser
    for c in cols:
        if c in FACTEURS_A_INVERSER:
            df[c] = -df[c]

    # Imputation : moyenne par colonne
    df = df.fillna(df.mean())

    # Normalisation
    if methode == "zscore":
        mu = df.mean()
        sd = df.std().replace(0, 1.0)
        normed = (df - mu) / sd
    else:  # minmax
        lo = df.min()
        hi = df.max()
        rng = (hi - lo).replace(0, 1.0)
        normed = (df - lo) / rng

    w = np.array([poids_facteurs[c] for c in cols])
    scores = normed.values @ w

    out = pd.DataFrame({"score": scores}, index=df.index)
    pos = np.maximum(scores, 0)
    total = pos.sum()
    out["poids"] = pos / total if total > 0 else 1.0 / len(pos)
    out["rang"] = (-out["score"]).rank(method="min").astype(int)
    return out.sort_values("score", ascending=False)


def selectionner_top_n(scores: pd.DataFrame, n: int = 15) -> pd.DataFrame:
    """Garde les N premières actions et renormalise les poids."""
    top = scores.head(n).copy()
    s = top["poids"].sum()
    if s > 0:
        top["poids"] = top["poids"] / s
    return top


def appliquer_contraintes_liquidite(
    scores: pd.DataFrame,
    adv: pd.Series,
    capital: float,
    lambda_participation: float = 0.15,
    w_max_concentration: float = 0.10,
) -> pd.DataFrame:
    """Clip les poids selon min(w_max_concentration, λ×ADV_i/capital) puis renormalise.

    Paramètres
    ----------
    scores              : résultat de selectionner_top_n(), colonnes score/poids/rang
    adv                 : volume moyen 20j par action en FCFA (index = ticker)
    capital             : capital de référence en FCFA
    lambda_participation: taux de participation max (Almgren-Chriss, défaut 15 %)
    w_max_concentration : plafond de concentration par action (défaut 10 %)

    Colonnes ajoutées : adv, w_max, feasible, poids_contraint
    """
    df = scores.copy()
    df["adv"] = adv.reindex(df.index).fillna(0.0)
    # w_max_adv = λ × ADV / capital, plafonné à w_max_concentration
    w_max_adv = (lambda_participation * df["adv"] / capital).clip(upper=w_max_concentration)
    # Si ADV inconnu, appliquer seulement le plafond de concentration
    df["w_max"] = w_max_adv.where(df["adv"] > 0, other=float(w_max_concentration))
    df["feasible"] = df["poids"] <= df["w_max"]
    df["poids_contraint"] = df["poids"].clip(upper=df["w_max"])
    total = df["poids_contraint"].sum()
    df["poids_contraint"] = df["poids_contraint"] / total if total > 0 else df["poids_contraint"]
    return df
