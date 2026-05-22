"""Verdict synthétique 4 axes (Tendance / Momentum / Volatilité / Volume).

Méthodologie
------------
- Chaque axe contient 2 sous-signaux convertis en score directionnel borné [-1, +1]
  via une transformation linéaire saturée (équivalent z-score clippée, plus
  robuste sur les historiques courts de la BRVM).
- Agrégation 1/N intra-axe (moyenne simple des 2 sous-signaux non nuls).
- Agrégation 1/N inter-axes (moyenne simple des 4 scores d'axes non nuls).
- Mapping final sur les 5 modalités Sikafinance : Vendre / Alléger /
  Conserver / Renforcer / Acheter.

La pondération 1/N est défendue empiriquement par DeMiguel, Garlappi & Uppal
(2009) sur la diversification ; la transformation bornée à ±1 par seuils
"métier" évite l'instabilité d'estimation des z-scores glissantes sur des
séries courtes (typique BRVM).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np


# Mapping label (seuil_min inclus, code interne, libellé Sikafinance, emoji, couleur)
LABELS: List[Tuple[float, str, str, str, str]] = [
    (0.50, "ACHETER", "Acheter", "🟢", "#10B981"),
    (0.15, "RENFORCER", "Renforcer", "🟢", "#34D399"),
    (-0.15, "CONSERVER", "Conserver", "⚪", "#9CA3AF"),
    (-0.50, "ALLEGER", "Alléger", "🟠", "#F59E0B"),
    (-1.01, "VENDRE", "Vendre", "🔴", "#EF4444"),
]


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _safe(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return f


# ---------------------------------------------------------------------------
# Sous-signaux (8) -> score directionnel [-1, +1]
# ---------------------------------------------------------------------------

def signal_sma_cross(sma20: Optional[float], sma50: Optional[float]) -> Optional[float]:
    """Tendance #1 : écart relatif SMA20/SMA50, ±1 si |écart| >= 5%."""
    s20, s50 = _safe(sma20), _safe(sma50)
    if s20 is None or s50 is None or s50 == 0:
        return None
    return _clip((s20 / s50 - 1.0) / 0.05)


def signal_adx_di(adx: Optional[float], di_plus: Optional[float],
                  di_minus: Optional[float]) -> Optional[float]:
    """Tendance #2 : direction (DI+ vs DI-) pondérée par la force (ADX).

    ADX < 20 -> 0 (pas de tendance) ; ADX >= 50 -> ±1 (tendance forte).
    """
    a, dp, dm = _safe(adx), _safe(di_plus), _safe(di_minus)
    if a is None or dp is None or dm is None:
        return None
    direction = 1.0 if dp > dm else (-1.0 if dp < dm else 0.0)
    strength = _clip((a - 20.0) / 30.0, 0.0, 1.0)
    return direction * strength


def signal_rsi(rsi_val: Optional[float]) -> Optional[float]:
    """Momentum #1 : contrarian classique, RSI=30 -> +1, RSI=70 -> -1."""
    r = _safe(rsi_val)
    if r is None:
        return None
    return _clip((50.0 - r) / 20.0)


def signal_macd_hist(macd_hist: Optional[float], atr_val: Optional[float],
                     last_close: Optional[float]) -> Optional[float]:
    """Momentum #2 : signe de l'histogramme MACD normalisé par ATR (ou par prix)."""
    h = _safe(macd_hist)
    if h is None:
        return None
    a = _safe(atr_val)
    if a is not None and a > 0:
        norm = abs(h) / (0.5 * a)
    else:
        p = _safe(last_close)
        if p is None or p == 0:
            return None
        # Fallback : 0.5% du prix sature à ±1
        norm = abs(h) / (0.005 * p)
    return (1.0 if h > 0 else -1.0) * _clip(norm, 0.0, 1.0)


def signal_bbands_pctb(last_close: Optional[float], upper: Optional[float],
                       lower: Optional[float]) -> Optional[float]:
    """Volatilité #1 : %B contrarian — prix sur bande basse -> +1, bande haute -> -1."""
    p, u, l = _safe(last_close), _safe(upper), _safe(lower)
    if p is None or u is None or l is None or u == l:
        return None
    pct_b = (p - l) / (u - l)
    return _clip(2.0 * (0.5 - pct_b))


def signal_natr_regime(natr_series: Optional[List[Optional[float]]],
                       lookback: int = 60) -> Optional[float]:
    """Volatilité #2 : NATR actuel vs médiane historique.

    Vol anormalement élevée -> signal négatif (régime incertain).
    Vol anormalement basse  -> signal positif léger (régime calme).
    """
    if not natr_series:
        return None
    arr = np.array([v for v in natr_series if v is not None], dtype=float)
    if arr.size < 10:
        return None
    current = arr[-1]
    ref = float(np.median(arr[-lookback:])) if arr.size >= lookback else float(np.median(arr))
    if ref <= 0 or not np.isfinite(current):
        return None
    deviation = current / ref - 1.0
    return -_clip(deviation / 0.5)


def signal_garch_regime(vol_series: Optional[List[Optional[float]]],
                        lookback: int = 60) -> Optional[float]:
    """Volatilité #3 (parallèle NATR) : vol GARCH actuelle vs médiane historique.

    Symétrique à ``signal_natr_regime`` mais basé sur la volatilité conditionnelle
    GARCH (action-spécifique, ré-estimée mensuellement) plutôt que sur le NATR.

    Vol anormalement élevée -> signal négatif (régime stressé) ;
    vol anormalement basse  -> signal positif léger (régime calme).
    """
    if not vol_series:
        return None
    arr = np.array([v for v in vol_series if v is not None], dtype=float)
    if arr.size < 10:
        return None
    current = arr[-1]
    ref = float(np.median(arr[-lookback:])) if arr.size >= lookback else float(np.median(arr))
    if ref <= 0 or not np.isfinite(current):
        return None
    deviation = current / ref - 1.0
    return -_clip(deviation / 0.5)


def signal_obv_slope(obv_series: Optional[List[Optional[float]]],
                     window: int = 10) -> Optional[float]:
    """Volume #1 : pente de l'OBV sur ``window`` jours, normalisée."""
    if not obv_series:
        return None
    arr = np.array([v for v in obv_series if v is not None], dtype=float)
    if arr.size < window + 5:
        return None
    seg = arr[-window:]
    x = np.arange(window, dtype=float)
    slope = float(np.polyfit(x, seg, 1)[0])
    ref = float(np.mean(np.abs(arr[-window * 3:])))
    if ref <= 0 or not np.isfinite(slope):
        return None
    # Pente de 5% du niveau moyen par jour sature à ±1
    return _clip((slope / ref) / 0.05)


def signal_mfi(mfi_val: Optional[float]) -> Optional[float]:
    """Volume #2 : MFI contrarian, symétrique au RSI sur le volume monétaire."""
    m = _safe(mfi_val)
    if m is None:
        return None
    return _clip((50.0 - m) / 20.0)


# ---------------------------------------------------------------------------
# Agrégation
# ---------------------------------------------------------------------------

def _aggregate(signals: List[Optional[float]]) -> Tuple[Optional[float], int]:
    valid = [s for s in signals if s is not None]
    if not valid:
        return None, 0
    return float(np.mean(valid)), len(valid)


def _label_from_score(score: float) -> Tuple[str, str, str, str]:
    for threshold, code, libelle, emoji, color in LABELS:
        if score >= threshold:
            return code, libelle, emoji, color
    last = LABELS[-1]
    return last[1], last[2], last[3], last[4]


def _round(x: Optional[float], n: int = 3) -> Optional[float]:
    return round(x, n) if x is not None else None


def compute_verdict(*,
                    sma20: Optional[float] = None,
                    sma50: Optional[float] = None,
                    adx: Optional[float] = None,
                    di_plus: Optional[float] = None,
                    di_minus: Optional[float] = None,
                    rsi_val: Optional[float] = None,
                    macd_hist: Optional[float] = None,
                    atr_val: Optional[float] = None,
                    last_close: Optional[float] = None,
                    bb_upper: Optional[float] = None,
                    bb_lower: Optional[float] = None,
                    natr_series: Optional[List[Optional[float]]] = None,
                    garch_vol_series: Optional[List[Optional[float]]] = None,
                    obv_series: Optional[List[Optional[float]]] = None,
                    mfi_val: Optional[float] = None) -> Dict:
    """Calcule le verdict synthétique 4 axes / 8 sous-signaux.

    Retourne un dict prêt pour le template avec :
      - score      : score global [-1, +1] (None si aucun axe calculable)
      - score_pct  : version 0..100 pour barre de jauge
      - label/emoji/color/code : modalité Sikafinance
      - axes       : détail par axe (score, n_valides, sous-signaux)

    Champs de compatibilité descendante (ancien verdict 4 votes) :
      - votes : nombre d'axes valides (1..4)
      - norm  : alias de ``score``
    """
    # Axe Tendance
    s_tend_1 = signal_sma_cross(sma20, sma50)
    s_tend_2 = signal_adx_di(adx, di_plus, di_minus)
    score_tendance, n_tend = _aggregate([s_tend_1, s_tend_2])

    # Axe Momentum
    s_mom_1 = signal_rsi(rsi_val)
    s_mom_2 = signal_macd_hist(macd_hist, atr_val, last_close)
    score_momentum, n_mom = _aggregate([s_mom_1, s_mom_2])

    # Axe Volatilité (3 sous-signaux : %B, NATR, GARCH en parallèle)
    s_vol_1 = signal_bbands_pctb(last_close, bb_upper, bb_lower)
    s_vol_2 = signal_natr_regime(natr_series)
    s_vol_3 = signal_garch_regime(garch_vol_series)
    score_volat, n_vol = _aggregate([s_vol_1, s_vol_2, s_vol_3])

    # Axe Volume
    s_volu_1 = signal_obv_slope(obv_series)
    s_volu_2 = signal_mfi(mfi_val)
    score_volume, n_volu = _aggregate([s_volu_1, s_volu_2])

    axes_scores = [score_tendance, score_momentum, score_volat, score_volume]
    score_global, n_axes = _aggregate(axes_scores)

    axes_detail = {
        "tendance": {
            "score": _round(score_tendance), "n": n_tend,
            "details": {
                "sma_cross": _round(s_tend_1),
                "adx_di": _round(s_tend_2),
            },
        },
        "momentum": {
            "score": _round(score_momentum), "n": n_mom,
            "details": {
                "rsi": _round(s_mom_1),
                "macd_hist": _round(s_mom_2),
            },
        },
        "volatilite": {
            "score": _round(score_volat), "n": n_vol,
            "details": {
                "bb_pctb": _round(s_vol_1),
                "natr_regime": _round(s_vol_2),
                "garch_regime": _round(s_vol_3),
            },
        },
        "volume": {
            "score": _round(score_volume), "n": n_volu,
            "details": {
                "obv_slope": _round(s_volu_1),
                "mfi": _round(s_volu_2),
            },
        },
    }

    if score_global is None:
        return {
            "label": "Indisponible",
            "code": "NA",
            "emoji": "—",
            "color": "#6B7280",
            "score": None,
            "score_pct": None,
            "n_axes_valides": 0,
            "axes": axes_detail,
            # compat ancien template
            "votes": 0,
            "norm": None,
        }

    code, libelle, emoji, color = _label_from_score(score_global)

    return {
        "label": libelle,
        "code": code,
        "emoji": emoji,
        "color": color,
        "score": _round(score_global),
        "score_pct": round((score_global + 1.0) * 50.0, 1),
        "n_axes_valides": n_axes,
        "axes": axes_detail,
        # compat ancien template
        "votes": n_axes,
        "norm": _round(score_global),
    }
