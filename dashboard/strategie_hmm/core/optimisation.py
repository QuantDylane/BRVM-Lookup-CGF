"""Optimisations d'allocation factorielle conditionnées au régime.

Implémente les 6 stratégies du mémoire. ``optimiser_sharpe`` est le défaut
(meilleurs résultats empiriques sur la BRVM, +19.9 % annualisé).
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import minimize


def _contraintes_simplex(n: int):
    return ({"type": "eq", "fun": lambda w: np.sum(w) - 1.0},)


def _bornes(n: int, w_min: float = 0.0, w_max: float = 1.0):
    return [(w_min, w_max) for _ in range(n)]


def _w0(n: int) -> np.ndarray:
    return np.ones(n) / n


def optimiser_sharpe(mu: np.ndarray, V: np.ndarray) -> np.ndarray:
    """max wᵀμ / √(wᵀVw)  s.c. w≥0, Σw=1."""
    n = len(mu)

    def neg_sharpe(w):
        denom = np.sqrt(w @ V @ w + 1e-12)
        return -(mu @ w) / denom

    res = minimize(
        neg_sharpe, _w0(n),
        method="SLSQP",
        bounds=_bornes(n),
        constraints=_contraintes_simplex(n),
        options={"maxiter": 500, "ftol": 1e-9},
    )
    w = res.x
    w = np.clip(w, 0, None)
    return w / w.sum() if w.sum() > 0 else _w0(n)


def optimiser_max_return(mu: np.ndarray, w_max: float = 0.8) -> np.ndarray:
    """max wᵀμ s.c. 0≤wᵢ≤w_max, Σw=1. Solution analytique : tout sur le top."""
    n = len(mu)
    w = np.zeros(n)
    ordre = np.argsort(-mu)
    restant = 1.0
    for i in ordre:
        part = min(w_max, restant)
        w[i] = part
        restant -= part
        if restant <= 1e-9:
            break
    return w


def optimiser_dyn(mu: np.ndarray) -> np.ndarray:
    """Si tous μᵢ > 0 : wᵢ = μᵢ / Σμ. Sinon : équipondéré."""
    n = len(mu)
    if np.all(mu > 0):
        return mu / mu.sum()
    return _w0(n)


def optimiser_min_variance(V: np.ndarray) -> np.ndarray:
    n = V.shape[0]

    def var(w):
        return w @ V @ w

    res = minimize(
        var, _w0(n),
        method="SLSQP",
        bounds=_bornes(n),
        constraints=_contraintes_simplex(n),
    )
    w = np.clip(res.x, 0, None)
    return w / w.sum() if w.sum() > 0 else _w0(n)


def optimiser_risk_parity(V: np.ndarray) -> np.ndarray:
    n = V.shape[0]

    def obj(w):
        portfolio_var = w @ V @ w
        rc = w * (V @ w)
        target = portfolio_var / n
        return float(np.sum((rc - target) ** 2))

    res = minimize(
        obj, _w0(n),
        method="SLSQP",
        bounds=[(1e-6, 1.0) for _ in range(n)],
        constraints=_contraintes_simplex(n),
    )
    w = np.clip(res.x, 0, None)
    return w / w.sum() if w.sum() > 0 else _w0(n)


def optimiser_max_diversification(sigma: np.ndarray, V: np.ndarray) -> np.ndarray:
    """max (wᵀσ) / √(wᵀVw)."""
    n = len(sigma)

    def neg_div(w):
        return -(sigma @ w) / np.sqrt(w @ V @ w + 1e-12)

    res = minimize(
        neg_div, _w0(n),
        method="SLSQP",
        bounds=_bornes(n),
        constraints=_contraintes_simplex(n),
    )
    w = np.clip(res.x, 0, None)
    return w / w.sum() if w.sum() > 0 else _w0(n)


def metriques_attendues(
    w: np.ndarray, mu: np.ndarray, V: np.ndarray
) -> dict:
    rdt = float(mu @ w)
    vol = float(np.sqrt(w @ V @ w))
    sharpe = rdt / vol if vol > 0 else 0.0
    # Annualisation (252 jours ouvrés)
    return {
        "rendement_attendu": rdt,
        "volatilite_attendue": vol,
        "sharpe_attendu": sharpe,
        "rendement_annualise": rdt * 252,
        "volatilite_annualisee": vol * np.sqrt(252),
        "sharpe_annualise": sharpe * np.sqrt(252),
    }


OPTIMISEURS = {
    "SHARPE_HMM": lambda mu, V, sigma: optimiser_sharpe(mu, V),
    "DYN_HMM": lambda mu, V, sigma: optimiser_dyn(mu),
    "MR_HMM": lambda mu, V, sigma: optimiser_max_return(mu),
    "RP_HMM": lambda mu, V, sigma: optimiser_risk_parity(V),
    "MD_HMM": lambda mu, V, sigma: optimiser_max_diversification(sigma, V),
    "MV_HMM": lambda mu, V, sigma: optimiser_min_variance(V),
}
