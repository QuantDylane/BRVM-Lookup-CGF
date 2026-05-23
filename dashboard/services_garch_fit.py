"""Ajustement GARCH par fenêtres expansives mensuelles, avec cache en base.

Utilisé par le simulateur de portefeuille pour évaluer le facteur de sizing
GARCH à n'importe quelle date du passé SANS look-ahead bias.

Principe
--------
Pour une action et une ``fin_de_periode`` donnée, on cherche les paramètres
ω, α, β, γ d'un modèle GARCH/GJR/EGARCH ajusté UNIQUEMENT sur les rendements
log antérieurs à cette date. Sélection BIC, comme dans ``train_garch``.

Cache
-----
Les fits sont coûteux (~50-200ms chacun avec le package ``arch``). Pour un
backtest sur 18 ans on doit faire ~216 fits, soit ~30 secondes. On persiste
donc dans ``GarchFitHistorique`` ; le deuxième backtest sur la même action
est instantané (lecture cache).

Granularité : **fin de mois** (dernier jour de cotation de chaque mois,
inclus). Pour un mois en cours, on n'écrit rien tant que le mois n'est pas
fini (sauf si on backtest avec un date_fin antérieur à aujourd'hui).
"""
from __future__ import annotations

import math
import warnings
from datetime import date
from typing import List, Optional, Sequence, Tuple

import numpy as np

from dashboard.models import Action, GarchFitHistorique, HistoriqueAction


warnings.filterwarnings("ignore")  # idem train_garch.py

TRADING_DAYS_PER_YEAR = 252
MIN_OBS_FIT = 500  # même seuil que train_garch


def _log_returns_pct(prices: Sequence[float]) -> np.ndarray:
    """Rendements log en %, NaN/inf/zéros filtrés."""
    arr = np.asarray(prices, dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if arr.size < 2:
        return np.array([])
    r = 100.0 * np.diff(np.log(arr))
    return r[np.isfinite(r)]


def _fit_one(returns: np.ndarray, model_type: str):
    """Ajuste un modèle, renvoie (params_dict, sigma_T_pct) ou (None, None).

    sigma_T_pct = σ_T en % quotidien (dernière vol conditionnelle).
    """
    from arch import arch_model

    if model_type == "GARCH":
        kwargs = dict(vol="GARCH", p=1, q=1, o=0)
    elif model_type == "GJR-GARCH":
        kwargs = dict(vol="GARCH", p=1, q=1, o=1)
    elif model_type == "EGARCH":
        kwargs = dict(vol="EGARCH", p=1, q=1, o=1)
    else:
        return None, None

    try:
        model = arch_model(returns, mean="Zero", dist="normal", rescale=False, **kwargs)
        res = model.fit(disp="off", show_warning=False)
    except Exception:
        return None, None
    if not np.isfinite(res.loglikelihood):
        return None, None

    p = res.params
    omega = float(p.get("omega", np.nan))
    alpha = float(p.get("alpha[1]", np.nan))
    beta = float(p.get("beta[1]", np.nan))
    gamma = (float(p.get("gamma[1]", np.nan))
             if "gamma[1]" in p.index else None)
    if not (np.isfinite(omega) and np.isfinite(alpha) and np.isfinite(beta)):
        return None, None
    if gamma is not None and not np.isfinite(gamma):
        gamma = None

    sigma_T_pct = float(res.conditional_volatility[-1])
    if not np.isfinite(sigma_T_pct):
        return None, None

    params = {
        "model_type": model_type,
        "omega": omega,
        "alpha": alpha,
        "beta": beta,
        "gamma": gamma,
        "bic": float(res.bic),
        "n_obs": int(res.nobs),
    }
    return params, sigma_T_pct


def _best_fit(returns: np.ndarray):
    """Compétition GARCH/GJR/EGARCH, sélection BIC. Renvoie (params, sigma_T_pct)."""
    candidates = []
    for mtype in ("GARCH", "GJR-GARCH", "EGARCH"):
        params, sigma_T = _fit_one(returns, mtype)
        if params is not None:
            candidates.append((params["bic"], params, sigma_T))
    if not candidates:
        return None, None
    candidates.sort(key=lambda x: x[0])
    _, best_params, best_sigma_T = candidates[0]
    return best_params, best_sigma_T


def _end_of_months_in_range(dates_dispo: Sequence[date],
                            date_debut: Optional[date],
                            date_fin: Optional[date]) -> List[date]:
    """Renvoie la liste des dates de cotation qui sont la DERNIÈRE du mois.

    Sur la plage [date_debut, date_fin] ; bornes incluses si fournies.
    """
    out: List[date] = []
    if not dates_dispo:
        return out
    by_month: dict[Tuple[int, int], date] = {}
    for d in dates_dispo:
        if date_debut and d < date_debut:
            continue
        if date_fin and d > date_fin:
            continue
        key = (d.year, d.month)
        if key not in by_month or d > by_month[key]:
            by_month[key] = d
    out = sorted(by_month.values())
    return out


def _ensure_fit_at(action: Action,
                   fin_de_periode: date,
                   prices_par_date: List[Tuple[date, float]]) -> Optional[GarchFitHistorique]:
    """Lit le cache à (action, fin_de_periode). Si manquant, ajuste & écrit.

    ``prices_par_date`` doit être trié par date (la liste complète disponible
    de l'action). On filtre dynamiquement les prix ≤ fin_de_periode pour fitter.
    """
    cached = GarchFitHistorique.objects.filter(
        action=action, fin_de_periode=fin_de_periode
    ).first()
    if cached is not None:
        return cached

    prices = [p for d, p in prices_par_date if d <= fin_de_periode]
    returns = _log_returns_pct(prices)
    if returns.size < MIN_OBS_FIT:
        fit, _ = GarchFitHistorique.objects.update_or_create(
            action=action, fin_de_periode=fin_de_periode,
            defaults={
                "model_type": "INSUFFISANT",
                "omega": None, "alpha": None, "beta": None, "gamma": None,
                "sigma_T_pct_quotidien": None,
                "n_obs": int(returns.size),
            },
        )
        return fit

    params, sigma_T_pct = _best_fit(returns)
    if params is None:
        fit, _ = GarchFitHistorique.objects.update_or_create(
            action=action, fin_de_periode=fin_de_periode,
            defaults={
                "model_type": "FAILED",
                "omega": None, "alpha": None, "beta": None, "gamma": None,
                "sigma_T_pct_quotidien": None,
                "n_obs": int(returns.size),
            },
        )
        return fit

    fit, _ = GarchFitHistorique.objects.update_or_create(
        action=action, fin_de_periode=fin_de_periode,
        defaults={
            "model_type": params["model_type"],
            "omega": params["omega"],
            "alpha": params["alpha"],
            "beta": params["beta"],
            "gamma": params["gamma"],
            "sigma_T_pct_quotidien": sigma_T_pct,
            "n_obs": params["n_obs"],
        },
    )
    return fit


def ensure_monthly_fits(action: Action,
                       date_debut: Optional[date] = None,
                       date_fin: Optional[date] = None,
                       progress_cb=None) -> List[GarchFitHistorique]:
    """Garantit qu'un fit GARCH existe pour chaque fin de mois ouvré dans la plage.

    ``progress_cb(i, n, date_courante)`` est appelé à chaque fin de mois traité
    (utile pour logger la progression sur un backtest long).
    Renvoie la liste des fits couvrant la plage (cache + nouveaux).
    """
    rows = list(
        HistoriqueAction.objects
        .filter(action=action, cloture__isnull=False)
        .order_by("date")
        .values_list("date", "cloture")
    )
    prices_par_date = [(d, float(c)) for d, c in rows if c and c > 0]
    if not prices_par_date:
        return []

    eom_dates = _end_of_months_in_range(
        [d for d, _ in prices_par_date], date_debut, date_fin
    )
    # Préchargement en une seule requête de TOUS les fits déjà en cache
    # pour cette action. Évite N requêtes DB dans la boucle.
    existing = {
        f.fin_de_periode: f
        for f in GarchFitHistorique.objects.filter(action=action)
    }
    fits: List[GarchFitHistorique] = []
    n = len(eom_dates)
    for i, eom in enumerate(eom_dates, 1):
        if eom in existing:
            fits.append(existing[eom])
        else:
            fit = _ensure_fit_at(action, eom, prices_par_date)
            if fit is not None:
                fits.append(fit)
                existing[eom] = fit
        if progress_cb is not None:
            progress_cb(i, n, eom)
    return fits


def fit_for_date(action: Action,
                 d: date,
                 cached_fits: Optional[List[GarchFitHistorique]] = None) -> Optional[GarchFitHistorique]:
    """Renvoie le fit valide à la date ``d`` (le plus récent fit avec fin_de_periode ≤ d).

    Si ``cached_fits`` est fourni (trié), on cherche dedans (O(log n) via
    bissection manuelle). Sinon on requête la base.
    """
    if cached_fits:
        # Bissection : trouver le plus grand fin_de_periode ≤ d
        lo, hi = 0, len(cached_fits) - 1
        result = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if cached_fits[mid].fin_de_periode <= d:
                result = cached_fits[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return result
    return (
        GarchFitHistorique.objects
        .filter(action=action, fin_de_periode__lte=d)
        .order_by("-fin_de_periode")
        .first()
    )
