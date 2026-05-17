"""Analyse descriptive & tests de robustesse des 13 séries factorielles.

Vérifie les hypothèses statistiques requises pour l'estimation d'un HMM gaussien :
- Statistiques descriptives
- Test de stationnarité ADF (Augmented Dickey-Fuller)
- Matrice de corrélation
- VIF (Variance Inflation Factor)
- Test de normalité Jarque-Bera

Référence : mémoire COULIBALY E. (2024-2025), chapitre III.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np

from ...models import RendementPortefeuilleFactoriel


# Seuils utilisés pour les verdicts
SEUIL_ADF_PVALUE = 0.05      # rejet H0 = racine unitaire à 5 %
SEUIL_VIF = 10.0             # multicolinéarité critique au-delà
SEUIL_JB_PVALUE = 0.05       # rejet H0 = normalité à 5 %
SEUIL_CORR_FORTE = 0.70      # corrélation considérée comme forte


def _charger_matrice_rendements(facteurs_ordre: list[str]) -> tuple[list, dict[str, np.ndarray]]:
    """Reconstruit la matrice T x K des rendements factoriels journaliers.

    Retourne (dates_communes, {facteur: np.array}) où chaque série est alignée
    sur l'intersection des dates disponibles pour TOUS les facteurs présents.
    """
    qs = list(
        RendementPortefeuilleFactoriel.objects.order_by("date").values(
            "facteur", "date", "rendement"
        )
    )
    if not qs:
        return [], {}

    by_factor: dict[str, dict] = defaultdict(dict)
    for r in qs:
        by_factor[r["facteur"]][r["date"]] = r["rendement"]

    facteurs_dispo = [f for f in facteurs_ordre if f in by_factor]
    if not facteurs_dispo:
        return [], {}

    # Intersection stricte des dates (séries propres pour tests statistiques)
    dates_communes = set.intersection(*(set(by_factor[f].keys()) for f in facteurs_dispo))
    dates_communes = sorted(dates_communes)
    if not dates_communes:
        return [], {}

    series: dict[str, np.ndarray] = {}
    for f in facteurs_dispo:
        vals = [by_factor[f][d] for d in dates_communes]
        series[f] = np.asarray([float(v) if v is not None else np.nan for v in vals])

    return dates_communes, series


def _stats_descriptives(series: dict[str, np.ndarray]) -> list[dict]:
    """Moyenne, écart-type, skewness, kurtosis (excès), min/max par facteur."""
    from scipy import stats as sst
    out = []
    for code, x in series.items():
        x = x[~np.isnan(x)]
        if x.size < 3:
            continue
        out.append({
            "code": code,
            "n": int(x.size),
            "mean": float(np.mean(x)),
            "std": float(np.std(x, ddof=1)),
            "skew": float(sst.skew(x)),
            "kurt": float(sst.kurtosis(x)),  # excès (Fisher)
            "min": float(np.min(x)),
            "max": float(np.max(x)),
        })
    return out


def _test_adf(series: dict[str, np.ndarray]) -> list[dict]:
    """Test Augmented Dickey-Fuller (H0 = racine unitaire / non stationnaire)."""
    try:
        from statsmodels.tsa.stattools import adfuller
    except ImportError:
        return []
    out = []
    for code, x in series.items():
        x = x[~np.isnan(x)]
        if x.size < 20:
            continue
        try:
            stat, pvalue, _usedlag, _nobs, crit, _ic = adfuller(x, autolag="AIC")
            out.append({
                "code": code,
                "stat": float(stat),
                "pvalue": float(pvalue),
                "crit_5": float(crit.get("5%", np.nan)),
                "stationnaire": pvalue < SEUIL_ADF_PVALUE,
            })
        except Exception:
            out.append({
                "code": code, "stat": None, "pvalue": None,
                "crit_5": None, "stationnaire": None,
            })
    return out


def _matrice_correlation(series: dict[str, np.ndarray]) -> dict:
    """Matrice de corrélation de Pearson entre les facteurs."""
    codes = list(series.keys())
    mat = np.column_stack([series[c] for c in codes])
    mask = ~np.isnan(mat).any(axis=1)
    mat = mat[mask]
    if mat.shape[0] < 2:
        return {"codes": codes, "matrix": [], "redondances": []}
    corr = np.corrcoef(mat, rowvar=False)
    # Liste les paires fortement corrélées (|r| > seuil)
    redondances = []
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            r = float(corr[i, j])
            if abs(r) >= SEUIL_CORR_FORTE:
                redondances.append({"f1": codes[i], "f2": codes[j], "r": r})
    redondances.sort(key=lambda d: -abs(d["r"]))
    return {
        "codes": codes,
        "matrix": [[round(float(corr[i, j]), 3) for j in range(len(codes))]
                    for i in range(len(codes))],
        "redondances": redondances,
    }


def _calcul_vif(series: dict[str, np.ndarray]) -> list[dict]:
    """Variance Inflation Factor — seuil critique : 10."""
    codes = list(series.keys())
    mat = np.column_stack([series[c] for c in codes])
    mask = ~np.isnan(mat).any(axis=1)
    mat = mat[mask]
    n, k = mat.shape
    if n < k + 2 or k < 2:
        return []

    out = []
    try:
        from statsmodels.stats.outliers_influence import variance_inflation_factor
        # Ajouter une constante (statsmodels exige un intercept pour un VIF correct)
        X = np.column_stack([np.ones(n), mat])
        for idx, code in enumerate(codes):
            try:
                vif = float(variance_inflation_factor(X, idx + 1))
            except Exception:
                vif = float("nan")
            out.append({
                "code": code,
                "vif": vif,
                "critique": (not np.isnan(vif)) and vif >= SEUIL_VIF,
            })
    except ImportError:
        # Fallback : VIF_i = 1 / (1 - R_i^2) via régression OLS manuelle
        for idx, code in enumerate(codes):
            y = mat[:, idx]
            X = np.delete(mat, idx, axis=1)
            X = np.column_stack([np.ones(n), X])
            try:
                beta, *_ = np.linalg.lstsq(X, y, rcond=None)
                y_hat = X @ beta
                ss_res = float(np.sum((y - y_hat) ** 2))
                ss_tot = float(np.sum((y - y.mean()) ** 2))
                r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
                vif = 1.0 / (1.0 - r2) if r2 < 1.0 else float("inf")
            except Exception:
                vif = float("nan")
            out.append({
                "code": code,
                "vif": vif,
                "critique": (not np.isnan(vif)) and vif >= SEUIL_VIF,
            })
    return out


def _test_jarque_bera(series: dict[str, np.ndarray]) -> list[dict]:
    """Test de normalité Jarque-Bera (H0 = distribution normale)."""
    from scipy import stats as sst
    out = []
    for code, x in series.items():
        x = x[~np.isnan(x)]
        if x.size < 8:
            continue
        try:
            stat, pvalue = sst.jarque_bera(x)
            out.append({
                "code": code,
                "stat": float(stat),
                "pvalue": float(pvalue),
                "normal": pvalue >= SEUIL_JB_PVALUE,
            })
        except Exception:
            out.append({"code": code, "stat": None, "pvalue": None, "normal": None})
    return out


def calculer_diagnostics(facteurs_ordre: list[str]) -> dict[str, Any]:
    """Point d'entrée : calcule tous les diagnostics statistiques.

    Retourne un dict prêt à être injecté dans le contexte du template.
    """
    dates, series = _charger_matrice_rendements(facteurs_ordre)
    if not series:
        return {
            "disponible": False,
            "n_obs": 0,
            "n_facteurs": 0,
            "date_min": None,
            "date_max": None,
            "descriptive": [],
            "adf": [],
            "correlation": {"codes": [], "matrix": [], "redondances": []},
            "vif": [],
            "jarque_bera": [],
            "verdict": {},
        }

    descriptive = _stats_descriptives(series)
    adf = _test_adf(series)
    correlation = _matrice_correlation(series)
    vif = _calcul_vif(series)
    jb = _test_jarque_bera(series)

    # Verdict global
    nb_non_stationnaires = sum(1 for r in adf if r.get("stationnaire") is False)
    nb_vif_critiques = sum(1 for r in vif if r.get("critique"))
    nb_non_normaux = sum(1 for r in jb if r.get("normal") is False)
    verdict = {
        "nb_facteurs": len(series),
        "nb_non_stationnaires": nb_non_stationnaires,
        "nb_vif_critiques": nb_vif_critiques,
        "nb_non_normaux": nb_non_normaux,
        "nb_redondances": len(correlation.get("redondances", [])),
        "ok_stationnarite": nb_non_stationnaires == 0,
        "ok_multicolinearite": nb_vif_critiques == 0,
    }

    return {
        "disponible": True,
        "n_obs": len(dates),
        "n_facteurs": len(series),
        "date_min": dates[0].isoformat() if dates else None,
        "date_max": dates[-1].isoformat() if dates else None,
        "descriptive": descriptive,
        "adf": adf,
        "correlation": correlation,
        "vif": vif,
        "jarque_bera": jb,
        "verdict": verdict,
        "seuils": {
            "adf_pvalue": SEUIL_ADF_PVALUE,
            "vif": SEUIL_VIF,
            "jb_pvalue": SEUIL_JB_PVALUE,
            "corr_forte": SEUIL_CORR_FORTE,
        },
    }
