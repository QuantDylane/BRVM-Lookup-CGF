"""Moteur HMM (Gaussien) — wrapper léger autour de hmmlearn.GaussianHMM.

Entraîne sur les rendements journaliers des 13 portefeuilles factoriels et
expose les paramètres conditionnels (μ, V) du régime courant.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler


@dataclass
class ResultatHMM:
    sequence_regimes: np.ndarray  # entiers, taille T
    proba_regimes: np.ndarray  # T × n_regimes
    moyennes: np.ndarray  # n_regimes × n_facteurs (dans l'espace original)
    covariances: np.ndarray  # n_regimes × n_facteurs × n_facteurs
    matrice_transition: np.ndarray  # n_regimes × n_regimes
    log_likelihood: float
    converged: bool
    scaler: StandardScaler

    def regime_courant(self) -> int:
        return int(self.sequence_regimes[-1])

    def proba_courant(self) -> np.ndarray:
        return self.proba_regimes[-1]


def entrainer_hmm(
    df_rendements: pd.DataFrame,
    n_regimes: int = 2,
    random_state: int = 42,
    n_iter: int = 1000,
) -> ResultatHMM:
    """Entraîne un GaussianHMM sur le DataFrame des rendements (T × n_facteurs)."""
    if df_rendements.isna().any().any():
        df_rendements = df_rendements.dropna()
    X = df_rendements.values
    scaler = StandardScaler()
    X_std = scaler.fit_transform(X)

    model = GaussianHMM(
        n_components=n_regimes,
        covariance_type="full",
        n_iter=n_iter,
        random_state=random_state,
        tol=1e-4,
    )
    model.fit(X_std)

    seq = model.predict(X_std)
    probas = model.predict_proba(X_std)
    ll = model.score(X_std)

    # Re-mapper μ et V vers l'espace original (rendements bruts)
    means_orig = scaler.inverse_transform(model.means_)
    # cov_orig = D · cov_std · D où D = diag(scale_)
    D = np.diag(scaler.scale_)
    covs_orig = np.array([D @ c @ D for c in model.covars_])

    # Convention : régime 0 = "favorable" (μᵀ1 > 0), régime 1 = "défavorable"
    sums = means_orig.sum(axis=1)
    if sums[0] < sums[1]:
        # swap
        seq = 1 - seq
        probas = probas[:, ::-1]
        means_orig = means_orig[::-1]
        covs_orig = covs_orig[::-1]
        T = model.transmat_[::-1, ::-1]
    else:
        T = model.transmat_

    return ResultatHMM(
        sequence_regimes=seq,
        proba_regimes=probas,
        moyennes=means_orig,
        covariances=covs_orig,
        matrice_transition=T,
        log_likelihood=float(ll),
        converged=bool(model.monitor_.converged),
        scaler=scaler,
    )


def confirmer_regime(seq: np.ndarray, d: int = 5) -> np.ndarray:
    """Retourne la séquence des régimes confirmés : un changement n'est validé
    que s'il persiste pendant au moins ``d`` jours consécutifs.
    """
    n = len(seq)
    if n == 0:
        return seq
    confirme = seq.copy()
    courant = int(seq[0])
    confirme[0] = courant
    streak_alt = 0
    alt = courant
    for i in range(1, n):
        if seq[i] == courant:
            streak_alt = 0
            confirme[i] = courant
        else:
            if seq[i] == alt:
                streak_alt += 1
            else:
                alt = int(seq[i])
                streak_alt = 1
            if streak_alt >= d:
                courant = alt
                confirme[i] = courant
                streak_alt = 0
            else:
                confirme[i] = courant
    return confirme
