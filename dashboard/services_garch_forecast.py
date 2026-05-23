"""Prévisions analytiques de volatilité GARCH à partir des paramètres stockés.

Le job ``train_garch`` ajuste un modèle parmi GARCH(1,1) / GJR-GARCH(1,1,1) /
EGARCH(1,1) sur les rendements log en pourcentage (r = 100·Δlog(P)) et persiste
ω, α, β, γ ainsi que la série σ_t quotidienne (en % annualisé) dans
``GarchModel.vol_conditionnelle_json``.

Ce module propage la dynamique du modèle stocké pour produire les prévisions
σ_{t+h} aux horizons h = 1, 5, 22 (J+1, J+1S, J+1M ouvrés). Il calcule
également le percentile rolling de la prévision dans son propre historique,
qui sert de modulateur de taille dans le simulateur de portefeuille.

Convention d'unités
-------------------
- Paramètres stockés (ω, α, β, γ) : calibrés sur r en pourcentage.
- Sorties annualisées : σ_quotidien × √252, exprimées en pourcentage.
- Pour repasser à des fractions (ex: 0.18 pour 18%), diviser par 100.

Hypothèses analytiques pour h > 1
---------------------------------
Sous hypothèse de résidus normaux centrés réduits :
- GARCH(1,1)         : σ²_{t+h} = ω·(1−ψ^{h−1})/(1−ψ) + ψ^{h−1}·σ²_{t+1}, ψ=α+β.
- GJR-GARCH(1,1,1)   : idem avec ψ = α + β + γ/2 (E[1{z<0}] = 1/2).
- EGARCH(1,1)        : ln σ²_{t+h} = ω·(1−β^{h−1})/(1−β) + β^{h−1}·ln σ²_{t+1}.
  Les chocs E[|z|] = √(2/π) et E[γ·z] = 0 sont absorbés dans ω à l'horizon 1.

Ces formules sont équivalentes au ``forecast(horizon=h, method='analytic')``
du package ``arch`` lorsqu'on les utilise sans paramètre de moyenne (mean='Zero').
Elles sont préférées ici car elles ne nécessitent pas de re-fitter le modèle
pour interroger un horizon différent : on travaille directement sur les
paramètres déjà persistés.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

from dashboard.models import Action, GarchFitHistorique, GarchModel, HistoriqueAction


TRADING_DAYS_PER_YEAR = 252
DEFAULT_HORIZONS = (1, 5, 22)
PERCENTILE_LOOKBACK = 252  # 1 an glissant
EXPECT_ABS_Z_NORMAL = math.sqrt(2.0 / math.pi)  # E[|z|] sous N(0,1)


@dataclass
class GarchForecast:
    """Prévision GARCH multi-horizons pour une action.

    Toutes les volatilités sont **annualisées en %** (multipliées par √252 × 100).
    Les percentiles sont dans [0, 1].
    """
    ticker: str
    model_type: str
    horizons: tuple[int, ...]
    # σ̂ annualisé en % par horizon (None si modèle indisponible)
    vol_predite_pct: dict[int, Optional[float]]
    # Percentile rolling 252j de la vol prédite dans son propre historique
    percentile: dict[int, Optional[float]]
    # σ courant (σ_T) annualisé en %, pour référence
    vol_actuelle_pct: Optional[float]
    # Médiane historique 252j (annualisée en %)
    vol_mediane_252j_pct: Optional[float]
    # Régime qualitatif basé sur percentile à l'horizon de référence (5j par défaut)
    regime: str
    regime_horizon: int
    # Disclaimer (look-ahead léger : params calibrés ex-post)
    disponible: bool
    raison_indispo: Optional[str] = None


def _is_finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


def _last_log_return_pct(action: Action) -> Optional[float]:
    """Dernier rendement log en %, calculé sur les 2 derniers cours disponibles.

    Renvoie None si moins de 2 cours valides.
    """
    prices = list(
        HistoriqueAction.objects
        .filter(action=action, cloture__isnull=False)
        .order_by("-date")
        .values_list("cloture", flat=True)[:2]
    )
    if len(prices) < 2:
        return None
    p_curr, p_prev = float(prices[0]), float(prices[1])
    if p_prev <= 0 or p_curr <= 0:
        return None
    return 100.0 * math.log(p_curr / p_prev)


def _sigma_T_pct_quotidien(gm: GarchModel) -> Optional[float]:
    """σ_T en pourcentage quotidien (unité du modèle ajusté).

    ``vol_conditionnelle_json`` stocke des σ_t **annualisés en %** (×√252 × 100,
    cf. ``train_garch``). Pour repasser à du quotidien en %, on divise par √252.
    """
    serie = gm.vol_conditionnelle_json or []
    if not serie:
        # Fallback sur vol_actuelle_annualisee si dispo
        if _is_finite(gm.vol_actuelle_annualisee):
            return float(gm.vol_actuelle_annualisee) / math.sqrt(TRADING_DAYS_PER_YEAR)
        return None
    last = serie[-1]
    if not _is_finite(last):
        return None
    return float(last) / math.sqrt(TRADING_DAYS_PER_YEAR)


def _forecast_sigma2_pct(gm,
                         sigma_T_pct: float,
                         r_T_pct: float,
                         h: int) -> Optional[float]:
    """Prévision σ²_{T+h} en (% quotidien)² selon le type de modèle stocké.

    ``gm`` doit exposer les attributs ``model_type``, ``omega``, ``alpha``,
    ``beta`` et (optionnellement) ``gamma``. Accepte indifféremment un
    :class:`GarchModel` (état courant) ou un :class:`GarchFitHistorique`
    (état à une date donnée, pour le simulateur).

    Retourne None si le modèle est invalide ou si la propagation diverge.
    """
    if h < 1:
        return None
    if not (_is_finite(gm.omega) and _is_finite(gm.alpha) and _is_finite(gm.beta)):
        return None

    omega = float(gm.omega)
    alpha = float(gm.alpha)
    beta = float(gm.beta)
    gamma = float(gm.gamma) if _is_finite(gm.gamma) else 0.0
    s2_T = sigma_T_pct * sigma_T_pct
    r2_T = r_T_pct * r_T_pct

    if gm.model_type == "GARCH":
        s2_next = omega + alpha * r2_T + beta * s2_T
        psi = alpha + beta
    elif gm.model_type == "GJR-GARCH":
        indic_neg = 1.0 if r_T_pct < 0 else 0.0
        s2_next = omega + alpha * r2_T + gamma * indic_neg * r2_T + beta * s2_T
        psi = alpha + beta + gamma / 2.0
    elif gm.model_type == "EGARCH":
        # ln σ²_{T+1} = ω + α·(|z_T|−E|z|) + γ·z_T + β·ln σ²_T
        if sigma_T_pct <= 0:
            return None
        ln_s2_T = math.log(s2_T) if s2_T > 0 else None
        if ln_s2_T is None:
            return None
        z_T = r_T_pct / sigma_T_pct
        ln_s2_next = (omega
                      + alpha * (abs(z_T) - EXPECT_ABS_Z_NORMAL)
                      + gamma * z_T
                      + beta * ln_s2_T)
        if h == 1:
            try:
                return float(math.exp(ln_s2_next))
            except OverflowError:
                return None
        # Propagation analytique pour h>1
        if beta == 1.0:
            ln_s2_h = ln_s2_next + (h - 1) * omega
        else:
            ln_s2_h = (omega * (1.0 - beta ** (h - 1)) / (1.0 - beta)
                       + (beta ** (h - 1)) * ln_s2_next)
        try:
            return float(math.exp(ln_s2_h))
        except OverflowError:
            return None
    else:
        return None

    if not math.isfinite(s2_next) or s2_next <= 0:
        return None
    if h == 1:
        return float(s2_next)

    # Propagation GARCH / GJR sous résidu normal symétrique
    if not math.isfinite(psi):
        return None
    if abs(psi - 1.0) < 1e-9:
        s2_h = s2_next + (h - 1) * omega
    else:
        s2_h = (omega * (1.0 - psi ** (h - 1)) / (1.0 - psi)
                + (psi ** (h - 1)) * s2_next)
    if not math.isfinite(s2_h) or s2_h <= 0:
        return None
    return float(s2_h)


def _annualize_pct(sigma_quotidien_pct: Optional[float]) -> Optional[float]:
    """% quotidien -> % annualisé (×√252)."""
    if sigma_quotidien_pct is None or not math.isfinite(sigma_quotidien_pct):
        return None
    return sigma_quotidien_pct * math.sqrt(TRADING_DAYS_PER_YEAR)


def _classify_regime(percentile: Optional[float]) -> str:
    """Mapping percentile -> régime qualitatif.

    Calme   : p ≤ 25%
    Normal  : 25% < p ≤ 50%
    Tendu   : 50% < p ≤ 80%
    Stressé : p > 80%
    """
    if percentile is None:
        return "Indisponible"
    if percentile <= 0.25:
        return "Calme"
    if percentile <= 0.50:
        return "Normal"
    if percentile <= 0.80:
        return "Tendu"
    return "Stressé"


def _percentile_rolling(serie_annualisee_pct: Sequence[float],
                        valeur_pct: float,
                        lookback: int = PERCENTILE_LOOKBACK) -> Optional[float]:
    """Percentile (∈ [0,1]) de ``valeur`` dans les ``lookback`` derniers points.

    La série passée doit être en mêmes unités (% annualisé). On compare la
    valeur à la fenêtre brute (pas d'interpolation, rank simple).
    """
    arr = np.asarray([v for v in serie_annualisee_pct
                      if v is not None and math.isfinite(v)], dtype=float)
    if arr.size < 30:
        return None
    window = arr[-lookback:] if arr.size >= lookback else arr
    if window.size == 0:
        return None
    rank = float(np.sum(window <= valeur_pct))
    return rank / float(window.size)


def forecast_for_action(action: Action,
                        horizons: Sequence[int] = DEFAULT_HORIZONS,
                        regime_horizon: int = 5) -> GarchForecast:
    """Calcule les prévisions GARCH multi-horizons pour une action.

    Renvoie un :class:`GarchForecast` toujours, avec ``disponible=False`` et
    ``raison_indispo`` si le modèle n'est pas exploitable.
    """
    horizons_t = tuple(int(h) for h in horizons)
    base = GarchForecast(
        ticker=action.ticker,
        model_type="—",
        horizons=horizons_t,
        vol_predite_pct={h: None for h in horizons_t},
        percentile={h: None for h in horizons_t},
        vol_actuelle_pct=None,
        vol_mediane_252j_pct=None,
        regime="Indisponible",
        regime_horizon=int(regime_horizon),
        disponible=False,
        raison_indispo=None,
    )

    gm = GarchModel.objects.filter(action=action).first()
    if gm is None:
        base.raison_indispo = "Aucun modèle GARCH entraîné pour cette action."
        return base
    base.model_type = gm.model_type
    if gm.model_type in ("INSUFFISANT", "FAILED"):
        base.raison_indispo = f"Modèle GARCH non exploitable ({gm.model_type})."
        return base

    sigma_T_pct = _sigma_T_pct_quotidien(gm)
    if sigma_T_pct is None or sigma_T_pct <= 0:
        base.raison_indispo = "Volatilité conditionnelle σ_T indisponible."
        return base

    r_T_pct = _last_log_return_pct(action)
    if r_T_pct is None:
        base.raison_indispo = "Rendement r_T indisponible (historique trop court)."
        return base

    # Volatilité actuelle annualisée (référence d'affichage)
    base.vol_actuelle_pct = _annualize_pct(sigma_T_pct)

    # Médiane historique 252j (annualisée)
    serie_an_pct = gm.vol_conditionnelle_json or []
    if serie_an_pct:
        arr = np.asarray([v for v in serie_an_pct
                          if v is not None and math.isfinite(v)], dtype=float)
        if arr.size >= 10:
            base.vol_mediane_252j_pct = float(np.median(arr[-PERCENTILE_LOOKBACK:]))

    # Prévisions par horizon
    for h in horizons_t:
        s2_h_pct2 = _forecast_sigma2_pct(gm, sigma_T_pct, r_T_pct, h)
        if s2_h_pct2 is None or s2_h_pct2 <= 0:
            continue
        sigma_h_pct = math.sqrt(s2_h_pct2)  # σ̂ quotidien en %
        vol_h_annuelle_pct = _annualize_pct(sigma_h_pct)
        base.vol_predite_pct[h] = vol_h_annuelle_pct
        # Percentile dans la série historique annualisée
        if vol_h_annuelle_pct is not None and serie_an_pct:
            base.percentile[h] = _percentile_rolling(serie_an_pct,
                                                    vol_h_annuelle_pct,
                                                    PERCENTILE_LOOKBACK)

    # Régime basé sur l'horizon de référence (5j par défaut)
    pct_ref = base.percentile.get(int(regime_horizon))
    base.regime = _classify_regime(pct_ref)
    base.disponible = any(v is not None for v in base.vol_predite_pct.values())
    if not base.disponible:
        base.raison_indispo = "Aucune prévision n'a pu être calculée."
    return base


def forecast_vol_pct_annuelle_from_fit(fit,
                                       r_T_pct: float,
                                       horizon: int) -> Optional[float]:
    """Calcule σ̂(t+h) **annualisé en %** à partir d'un GarchFitHistorique.

    ``fit`` est un :class:`GarchFitHistorique` (ou GarchModel — duck-typed).
    On utilise ``fit.sigma_T_pct_quotidien`` comme σ_T quotidien en %.
    """
    if fit is None:
        return None
    sigma_T = getattr(fit, "sigma_T_pct_quotidien", None)
    if sigma_T is None or not math.isfinite(sigma_T) or sigma_T <= 0:
        return None
    if fit.model_type in ("INSUFFISANT", "FAILED"):
        return None
    s2_h = _forecast_sigma2_pct(fit, float(sigma_T), float(r_T_pct), int(horizon))
    if s2_h is None or s2_h <= 0:
        return None
    sigma_h_pct = math.sqrt(s2_h)
    return _annualize_pct(sigma_h_pct)


def garch_size_factor(percentile: Optional[float]) -> float:
    """Facteur de taille continu basé sur le percentile de la vol prédite.

    Convention (option β validée par l'utilisateur) :
        percentile ≤ 0.50 → 1.0 (plein engagement)
        percentile = 0.75 → 0.5
        percentile ≥ 1.00 → 0.0

    Linéaire entre 0.50 et 1.00, clippé hors plage.
    Utilisé par le simulateur de portefeuille pour moduler la TAILLE des
    achats. Les ventes sont exécutées intégralement (pas modulées).
    """
    if percentile is None or not math.isfinite(percentile):
        return 1.0  # GARCH indispo → on ne pénalise pas
    if percentile <= 0.5:
        return 1.0
    if percentile >= 1.0:
        return 0.0
    return float(max(0.0, min(1.0, 1.0 - (percentile - 0.5) * 2.0)))


def to_template_dict(fcst: GarchForecast) -> dict:
    """Sérialise un :class:`GarchForecast` pour le template Django.

    Format pensé pour la "Carte régime de risque" : on expose des chaînes
    déjà formatées en plus des valeurs brutes, pour limiter la logique côté
    template.
    """
    def _fmt_pct(v: Optional[float], digits: int = 1) -> str:
        if v is None or not math.isfinite(v):
            return "—"
        return f"{v:.{digits}f}%"

    def _fmt_perc(p: Optional[float]) -> str:
        if p is None or not math.isfinite(p):
            return "—"
        return f"{p * 100.0:.0f}e"

    regime_color = {
        "Calme":   "#10B981",
        "Normal":  "#34D399",
        "Tendu":   "#F59E0B",
        "Stressé": "#EF4444",
        "Indisponible": "#6B7280",
    }.get(fcst.regime, "#6B7280")

    horizon_labels = {1: "J+1 (1 jour)", 5: "J+5 (~1 semaine)", 22: "J+22 (~1 mois)"}
    horizons_data = []
    for h in fcst.horizons:
        v = fcst.vol_predite_pct.get(h)
        p = fcst.percentile.get(h)
        horizons_data.append({
            "h": h,
            "label": horizon_labels.get(h, f"J+{h}"),
            "vol_pct": _fmt_pct(v),
            "vol_pct_raw": v,
            "percentile_pct": _fmt_perc(p),
            "percentile_raw": p,
            "size_factor": garch_size_factor(p),
        })

    return {
        "disponible": fcst.disponible,
        "raison_indispo": fcst.raison_indispo,
        "ticker": fcst.ticker,
        "model_type": fcst.model_type,
        "vol_actuelle_pct": _fmt_pct(fcst.vol_actuelle_pct),
        "vol_actuelle_raw": fcst.vol_actuelle_pct,
        "vol_mediane_252j_pct": _fmt_pct(fcst.vol_mediane_252j_pct),
        "vol_mediane_raw": fcst.vol_mediane_252j_pct,
        "regime": fcst.regime,
        "regime_color": regime_color,
        "regime_horizon": fcst.regime_horizon,
        "horizons": horizons_data,
    }
