"""Service de calcul d'indicateurs techniques inspirés de TA-Lib.

Sept catégories supportées :
- Overlap Studies      : SMA, EMA, WMA, BBANDS, KAMA
- Momentum Indicators  : RSI, MACD, STOCH, CCI, ROC, MOM, ADX, WILLR
- Volume Indicators    : OBV, MFI, AD, ADOSC
- Volatility           : ATR, NATR, STDDEV
- Price Transform      : TYPPRICE, MEDPRICE, WCLPRICE, AVGPRICE
- Cycle Indicators     : HT_TRENDLINE, HT_DCPERIOD, HT_TRENDMODE (approximations Hilbert)
- Pattern Recognition  : CDL_DOJI, CDL_ENGULFING, CDL_HAMMER, CDL_SHOOTING_STAR, CDL_MORNINGSTAR, CDL_EVENINGSTAR

Toutes les sorties séries sont des listes Python alignées sur la longueur des entrées,
avec ``None`` pour les positions où l'indicateur n'est pas encore calculable.
Sérialisable JSON.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

ArrayLike = Sequence[Optional[float]]


def _to_array(values: ArrayLike) -> np.ndarray:
    return np.array([v if v is not None else np.nan for v in values], dtype=float)


def _serialize(arr: np.ndarray) -> List[Optional[float]]:
    """Convert numpy array to list with None for NaN, rounded to 4 decimals."""
    out: List[Optional[float]] = []
    for v in arr.tolist():
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            out.append(None)
        else:
            out.append(round(float(v), 4))
    return out


def _last(arr: np.ndarray) -> Optional[float]:
    if arr.size == 0:
        return None
    val = arr[-1]
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return None
    return round(float(val), 4)


def _rolling_apply(arr: np.ndarray, period: int, func) -> np.ndarray:
    n = len(arr)
    out = np.full(n, np.nan)
    if n < period or period <= 0:
        return out
    for i in range(period - 1, n):
        window = arr[i - period + 1:i + 1]
        if np.isnan(window).any():
            continue
        out[i] = func(window)
    return out


# ----------------------------------------------------------------------
# Overlap Studies
# ----------------------------------------------------------------------

def sma(closes: np.ndarray, period: int) -> np.ndarray:
    return _rolling_apply(closes, period, np.mean)


def ema(closes: np.ndarray, period: int) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    # Seed with SMA on the first `period` values
    seed = np.nanmean(closes[:period])
    out[period - 1] = seed
    for i in range(period, n):
        prev = out[i - 1]
        c = closes[i]
        if np.isnan(c) or np.isnan(prev):
            out[i] = prev
        else:
            out[i] = alpha * c + (1 - alpha) * prev
    return out


def wma(closes: np.ndarray, period: int) -> np.ndarray:
    weights = np.arange(1, period + 1, dtype=float)
    weights_sum = weights.sum()

    def _wma(window):
        return float(np.dot(window, weights) / weights_sum)

    return _rolling_apply(closes, period, _wma)


def bbands(closes: np.ndarray, period: int = 20, num_std: float = 2.0):
    mid = sma(closes, period)
    std = _rolling_apply(closes, period, np.std)
    upper = mid + num_std * std
    lower = mid - num_std * std
    return mid, upper, lower


def kama(closes: np.ndarray, period: int = 10, fast: int = 2, slow: int = 30) -> np.ndarray:
    """Kaufman's Adaptive Moving Average."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)
    out[period] = closes[period]
    for i in range(period + 1, n):
        change = abs(closes[i] - closes[i - period])
        volatility = np.sum(np.abs(np.diff(closes[i - period:i + 1])))
        er = change / volatility if volatility > 0 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        prev = out[i - 1]
        if np.isnan(prev):
            prev = closes[i - 1]
        out[i] = prev + sc * (closes[i] - prev)
    return out


# ----------------------------------------------------------------------
# Momentum Indicators
# ----------------------------------------------------------------------

def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, n):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


def macd(closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = ema_fast - ema_slow
    # Signal = EMA of macd_line, ignoring leading NaNs
    valid_mask = ~np.isnan(macd_line)
    signal_line = np.full_like(macd_line, np.nan)
    if valid_mask.sum() >= signal:
        valid = macd_line[valid_mask]
        sig = ema(valid, signal)
        signal_line[valid_mask] = sig
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def stochastic(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
               period: int = 14, smooth_k: int = 3, smooth_d: int = 3):
    n = len(closes)
    k_raw = np.full(n, np.nan)
    for i in range(period - 1, n):
        h = np.nanmax(highs[i - period + 1:i + 1])
        l = np.nanmin(lows[i - period + 1:i + 1])
        if h - l == 0:
            k_raw[i] = 50.0
        else:
            k_raw[i] = 100.0 * (closes[i] - l) / (h - l)
    k = sma(k_raw, smooth_k)
    d = sma(k, smooth_d)
    return k, d


def cci(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 20) -> np.ndarray:
    tp = (highs + lows + closes) / 3.0
    sma_tp = sma(tp, period)

    def _md(window):
        return float(np.mean(np.abs(window - window.mean())))

    md = _rolling_apply(tp, period, _md)
    out = (tp - sma_tp) / (0.015 * md)
    return out


def roc(closes: np.ndarray, period: int = 10) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    for i in range(period, n):
        prev = closes[i - period]
        if prev and prev != 0:
            out[i] = (closes[i] - prev) / prev * 100.0
    return out


def mom(closes: np.ndarray, period: int = 10) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    for i in range(period, n):
        out[i] = closes[i] - closes[i - period]
    return out


def adx(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14):
    n = len(closes)
    if n < period * 2:
        nan = np.full(n, np.nan)
        return nan, nan.copy(), nan.copy()
    up_move = np.diff(highs, prepend=highs[0])
    down_move = -np.diff(lows, prepend=lows[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    # Wilder smoothing
    def _wilder(arr, p):
        out = np.full(n, np.nan)
        out[p - 1] = arr[:p].sum()
        for i in range(p, n):
            out[i] = out[i - 1] - (out[i - 1] / p) + arr[i]
        return out

    atr_smooth = _wilder(tr, period)
    plus_dm_smooth = _wilder(plus_dm, period)
    minus_dm_smooth = _wilder(minus_dm, period)

    plus_di = 100.0 * plus_dm_smooth / np.where(atr_smooth == 0, np.nan, atr_smooth)
    minus_di = 100.0 * minus_dm_smooth / np.where(atr_smooth == 0, np.nan, atr_smooth)

    dx = 100.0 * np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) == 0, np.nan, (plus_di + minus_di))

    adx_arr = np.full(n, np.nan)
    start = period * 2 - 1
    if start < n:
        first_idx = period * 2 - 1
        seed_window = dx[period:first_idx + 1]
        if not np.isnan(seed_window).all():
            adx_arr[first_idx] = np.nanmean(seed_window)
            for i in range(first_idx + 1, n):
                prev = adx_arr[i - 1]
                if np.isnan(prev) or np.isnan(dx[i]):
                    adx_arr[i] = prev
                else:
                    adx_arr[i] = (prev * (period - 1) + dx[i]) / period
    return adx_arr, plus_di, minus_di


def williams_r(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    for i in range(period - 1, n):
        h = np.nanmax(highs[i - period + 1:i + 1])
        l = np.nanmin(lows[i - period + 1:i + 1])
        if h - l == 0:
            out[i] = -50.0
        else:
            out[i] = -100.0 * (h - closes[i]) / (h - l)
    return out


# ----------------------------------------------------------------------
# Volume Indicators
# ----------------------------------------------------------------------

def obv(closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    n = len(closes)
    out = np.zeros(n)
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            out[i] = out[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            out[i] = out[i - 1] - volumes[i]
        else:
            out[i] = out[i - 1]
    return out


def mfi(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    tp = (highs + lows + closes) / 3.0
    raw_mf = tp * volumes
    pos_mf = np.zeros(n)
    neg_mf = np.zeros(n)
    for i in range(1, n):
        if tp[i] > tp[i - 1]:
            pos_mf[i] = raw_mf[i]
        elif tp[i] < tp[i - 1]:
            neg_mf[i] = raw_mf[i]
    for i in range(period, n):
        pos_sum = pos_mf[i - period + 1:i + 1].sum()
        neg_sum = neg_mf[i - period + 1:i + 1].sum()
        if neg_sum == 0:
            out[i] = 100.0
        else:
            mr = pos_sum / neg_sum
            out[i] = 100.0 - (100.0 / (1.0 + mr))
    return out


def ad(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> np.ndarray:
    """Chaikin A/D Line."""
    n = len(closes)
    out = np.zeros(n)
    for i in range(n):
        denom = highs[i] - lows[i]
        if denom == 0:
            mfm = 0.0
        else:
            mfm = ((closes[i] - lows[i]) - (highs[i] - closes[i])) / denom
        mfv = mfm * volumes[i]
        out[i] = (out[i - 1] if i > 0 else 0.0) + mfv
    return out


def adosc(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray,
          fast: int = 3, slow: int = 10) -> np.ndarray:
    """Chaikin A/D Oscillator."""
    ad_line = ad(highs, lows, closes, volumes)
    return ema(ad_line, fast) - ema(ad_line, slow)


# ----------------------------------------------------------------------
# Volatility
# ----------------------------------------------------------------------

def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
    out[period - 1] = tr[:period].mean()
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def natr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Normalized ATR (% of price)."""
    a = atr(highs, lows, closes, period)
    return 100.0 * a / np.where(closes == 0, np.nan, closes)


def stddev(closes: np.ndarray, period: int = 20) -> np.ndarray:
    return _rolling_apply(closes, period, np.std)


# ----------------------------------------------------------------------
# Price Transform
# ----------------------------------------------------------------------

def typprice(highs, lows, closes):
    return (highs + lows + closes) / 3.0


def medprice(highs, lows):
    return (highs + lows) / 2.0


def wclprice(highs, lows, closes):
    return (highs + lows + 2 * closes) / 4.0


def avgprice(opens, highs, lows, closes):
    return (opens + highs + lows + closes) / 4.0


# ----------------------------------------------------------------------
# Cycle Indicators (Hilbert Transform approximations)
# ----------------------------------------------------------------------

def ht_trendline(closes: np.ndarray, period: int = 21) -> np.ndarray:
    """Approximation of TA-Lib HT_TRENDLINE via WMA(period) over a smoothed price."""
    smoothed = sma(closes, 4)
    smoothed = np.where(np.isnan(smoothed), closes, smoothed)
    return wma(smoothed, period)


def ht_dcperiod(closes: np.ndarray) -> np.ndarray:
    """Crude dominant cycle period estimation via FFT in rolling windows."""
    n = len(closes)
    out = np.full(n, np.nan)
    window = 64
    if n < window:
        return out
    for i in range(window - 1, n):
        seg = closes[i - window + 1:i + 1]
        if np.isnan(seg).any():
            continue
        seg = seg - seg.mean()
        spectrum = np.abs(np.fft.rfft(seg))
        if spectrum.size <= 1:
            continue
        spectrum[0] = 0  # ignore DC
        idx = int(np.argmax(spectrum))
        if idx > 0:
            out[i] = window / idx
    return out


def ht_trendmode(closes: np.ndarray) -> np.ndarray:
    """1 = trending, 0 = cycling. Heuristic based on slope of EMA(20) vs ATR-like noise."""
    n = len(closes)
    out = np.full(n, np.nan)
    e = ema(closes, 20)
    if n < 25:
        return out
    for i in range(20, n):
        if np.isnan(e[i]) or np.isnan(e[i - 5]):
            continue
        slope = abs(e[i] - e[i - 5]) / 5.0
        std_5 = float(np.nanstd(closes[i - 5:i + 1]))
        if std_5 == 0:
            out[i] = 1.0
        else:
            out[i] = 1.0 if slope > std_5 * 0.5 else 0.0
    return out


def ht_sine(closes: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Approximates HT_SINE: sine + leadsine via instantaneous phase from rolling FFT."""
    n = len(closes)
    sine = np.full(n, np.nan)
    lead = np.full(n, np.nan)
    window = 32
    for i in range(window - 1, n):
        seg = closes[i - window + 1:i + 1]
        if np.isnan(seg).any():
            continue
        seg = seg - seg.mean()
        spectrum = np.fft.rfft(seg)
        spectrum[0] = 0
        if spectrum.size <= 1:
            continue
        idx = int(np.argmax(np.abs(spectrum)))
        if idx == 0:
            continue
        phase = np.angle(spectrum[idx])
        sine[i] = np.sin(phase)
        lead[i] = np.sin(phase + np.pi / 4.0)
    return sine, lead


# ----------------------------------------------------------------------
# Pattern Recognition (returns array of -100 / 0 / +100 like TA-Lib)
# ----------------------------------------------------------------------

def _body(o, c):
    return abs(c - o)


def _candle_range(h, l):
    return h - l


def cdl_doji(opens, highs, lows, closes, body_pct: float = 0.1) -> np.ndarray:
    n = len(closes)
    out = np.zeros(n)
    for i in range(n):
        rng = _candle_range(highs[i], lows[i])
        if rng == 0:
            continue
        if _body(opens[i], closes[i]) <= body_pct * rng:
            out[i] = 100
    return out


def cdl_hammer(opens, highs, lows, closes) -> np.ndarray:
    n = len(closes)
    out = np.zeros(n)
    for i in range(n):
        rng = _candle_range(highs[i], lows[i])
        if rng == 0:
            continue
        body = _body(opens[i], closes[i])
        upper = highs[i] - max(opens[i], closes[i])
        lower = min(opens[i], closes[i]) - lows[i]
        if body <= 0.3 * rng and lower >= 2 * body and upper <= 0.2 * rng:
            out[i] = 100
    return out


def cdl_shooting_star(opens, highs, lows, closes) -> np.ndarray:
    n = len(closes)
    out = np.zeros(n)
    for i in range(n):
        rng = _candle_range(highs[i], lows[i])
        if rng == 0:
            continue
        body = _body(opens[i], closes[i])
        upper = highs[i] - max(opens[i], closes[i])
        lower = min(opens[i], closes[i]) - lows[i]
        if body <= 0.3 * rng and upper >= 2 * body and lower <= 0.2 * rng:
            out[i] = -100
    return out


def cdl_engulfing(opens, highs, lows, closes) -> np.ndarray:
    n = len(closes)
    out = np.zeros(n)
    for i in range(1, n):
        po, pc = opens[i - 1], closes[i - 1]
        o, c = opens[i], closes[i]
        if pc < po and c > o and o <= pc and c >= po:
            out[i] = 100  # bullish engulfing
        elif pc > po and c < o and o >= pc and c <= po:
            out[i] = -100  # bearish engulfing
    return out


def cdl_morning_star(opens, highs, lows, closes) -> np.ndarray:
    n = len(closes)
    out = np.zeros(n)
    for i in range(2, n):
        # day1 bearish, day2 small body, day3 bullish closing above mid of day1
        d1_o, d1_c = opens[i - 2], closes[i - 2]
        d2_o, d2_c = opens[i - 1], closes[i - 1]
        d3_o, d3_c = opens[i], closes[i]
        if d1_c >= d1_o:
            continue
        d1_body = _body(d1_o, d1_c)
        d2_body = _body(d2_o, d2_c)
        if d1_body == 0:
            continue
        if d2_body > 0.3 * d1_body:
            continue
        if d3_c <= d3_o:
            continue
        mid_d1 = (d1_o + d1_c) / 2.0
        if d3_c > mid_d1:
            out[i] = 100
    return out


def cdl_evening_star(opens, highs, lows, closes) -> np.ndarray:
    n = len(closes)
    out = np.zeros(n)
    for i in range(2, n):
        d1_o, d1_c = opens[i - 2], closes[i - 2]
        d2_o, d2_c = opens[i - 1], closes[i - 1]
        d3_o, d3_c = opens[i], closes[i]
        if d1_c <= d1_o:
            continue
        d1_body = _body(d1_o, d1_c)
        d2_body = _body(d2_o, d2_c)
        if d1_body == 0:
            continue
        if d2_body > 0.3 * d1_body:
            continue
        if d3_c >= d3_o:
            continue
        mid_d1 = (d1_o + d1_c) / 2.0
        if d3_c < mid_d1:
            out[i] = -100
    return out


PATTERNS = {
    "CDL_DOJI": lambda o, h, l, c: cdl_doji(o, h, l, c),
    "CDL_HAMMER": lambda o, h, l, c: cdl_hammer(o, h, l, c),
    "CDL_SHOOTING_STAR": lambda o, h, l, c: cdl_shooting_star(o, h, l, c),
    "CDL_ENGULFING": lambda o, h, l, c: cdl_engulfing(o, h, l, c),
    "CDL_MORNING_STAR": lambda o, h, l, c: cdl_morning_star(o, h, l, c),
    "CDL_EVENING_STAR": lambda o, h, l, c: cdl_evening_star(o, h, l, c),
}


# ----------------------------------------------------------------------
# Top-level dispatcher
# ----------------------------------------------------------------------

def compute_indicators(opens: ArrayLike, highs: ArrayLike, lows: ArrayLike,
                       closes: ArrayLike, volumes: ArrayLike,
                       config: Dict) -> Tuple[Dict, Dict]:
    """Compute all indicators selected in ``config``.

    Returns:
        (series_dict, current_values_dict)
    """
    o = _to_array(opens)
    h = _to_array(highs)
    l = _to_array(lows)
    c = _to_array(closes)
    v = _to_array(volumes)

    series: Dict[str, List] = {}
    current: Dict[str, Optional[float]] = {}

    # ---- Overlap ----
    if config.get("SMA", {}).get("enabled"):
        for p in config["SMA"].get("periods", [20, 50]):
            if not isinstance(p, int) or p <= 0:
                continue
            arr = sma(c, p)
            series[f"sma_{p}"] = _serialize(arr)
            current[f"sma_{p}"] = _last(arr)

    if config.get("EMA", {}).get("enabled"):
        for p in config["EMA"].get("periods", [12, 26]):
            if not isinstance(p, int) or p <= 0:
                continue
            arr = ema(c, p)
            series[f"ema_{p}"] = _serialize(arr)
            current[f"ema_{p}"] = _last(arr)

    if config.get("WMA", {}).get("enabled"):
        for p in config["WMA"].get("periods", [20]):
            if not isinstance(p, int) or p <= 0:
                continue
            arr = wma(c, p)
            series[f"wma_{p}"] = _serialize(arr)
            current[f"wma_{p}"] = _last(arr)

    if config.get("BBANDS", {}).get("enabled"):
        period = int(config["BBANDS"].get("period", 20))
        std_n = float(config["BBANDS"].get("std", 2.0))
        mid, up, lo = bbands(c, period, std_n)
        series["bollinger_middle"] = _serialize(mid)
        series["bollinger_upper"] = _serialize(up)
        series["bollinger_lower"] = _serialize(lo)
        current["bollinger_middle"] = _last(mid)
        current["bollinger_upper"] = _last(up)
        current["bollinger_lower"] = _last(lo)

    if config.get("KAMA", {}).get("enabled"):
        period = int(config["KAMA"].get("period", 10))
        arr = kama(c, period)
        series[f"kama_{period}"] = _serialize(arr)
        current[f"kama_{period}"] = _last(arr)

    # ---- Momentum ----
    if config.get("RSI", {}).get("enabled"):
        period = int(config["RSI"].get("period", 14))
        arr = rsi(c, period)
        series["rsi"] = _serialize(arr)
        current["rsi"] = _last(arr)

    if config.get("MACD", {}).get("enabled"):
        fast = int(config["MACD"].get("fast", 12))
        slow = int(config["MACD"].get("slow", 26))
        sig = int(config["MACD"].get("signal", 9))
        m, s, hgr = macd(c, fast, slow, sig)
        series["macd"] = _serialize(m)
        series["macd_signal"] = _serialize(s)
        series["macd_histogram"] = _serialize(hgr)
        current["macd"] = _last(m)
        current["macd_signal"] = _last(s)
        current["macd_histogram"] = _last(hgr)

    if config.get("STOCH", {}).get("enabled"):
        period = int(config["STOCH"].get("period", 14))
        sk = int(config["STOCH"].get("smooth_k", 3))
        sd = int(config["STOCH"].get("smooth_d", 3))
        k, d = stochastic(h, l, c, period, sk, sd)
        series["stoch_k"] = _serialize(k)
        series["stoch_d"] = _serialize(d)
        current["stoch_k"] = _last(k)
        current["stoch_d"] = _last(d)

    if config.get("CCI", {}).get("enabled"):
        period = int(config["CCI"].get("period", 20))
        arr = cci(h, l, c, period)
        series["cci"] = _serialize(arr)
        current["cci"] = _last(arr)

    if config.get("ROC", {}).get("enabled"):
        period = int(config["ROC"].get("period", 10))
        arr = roc(c, period)
        series["roc"] = _serialize(arr)
        current["roc"] = _last(arr)

    if config.get("MOM", {}).get("enabled"):
        period = int(config["MOM"].get("period", 10))
        arr = mom(c, period)
        series["mom"] = _serialize(arr)
        current["mom"] = _last(arr)

    if config.get("ADX", {}).get("enabled"):
        period = int(config["ADX"].get("period", 14))
        a, p_di, m_di = adx(h, l, c, period)
        series["adx"] = _serialize(a)
        series["plus_di"] = _serialize(p_di)
        series["minus_di"] = _serialize(m_di)
        current["adx"] = _last(a)
        current["plus_di"] = _last(p_di)
        current["minus_di"] = _last(m_di)

    if config.get("WILLR", {}).get("enabled"):
        period = int(config["WILLR"].get("period", 14))
        arr = williams_r(h, l, c, period)
        series["williams_r"] = _serialize(arr)
        current["williams_r"] = _last(arr)

    # ---- Volume ----
    if config.get("OBV", {}).get("enabled"):
        arr = obv(c, v)
        series["obv"] = _serialize(arr)
        current["obv"] = _last(arr)

    if config.get("MFI", {}).get("enabled"):
        period = int(config["MFI"].get("period", 14))
        arr = mfi(h, l, c, v, period)
        series["mfi"] = _serialize(arr)
        current["mfi"] = _last(arr)

    if config.get("AD", {}).get("enabled"):
        arr = ad(h, l, c, v)
        series["ad"] = _serialize(arr)
        current["ad"] = _last(arr)

    if config.get("ADOSC", {}).get("enabled"):
        fast = int(config["ADOSC"].get("fast", 3))
        slow = int(config["ADOSC"].get("slow", 10))
        arr = adosc(h, l, c, v, fast, slow)
        series["adosc"] = _serialize(arr)
        current["adosc"] = _last(arr)

    # ---- Volatility ----
    if config.get("ATR", {}).get("enabled"):
        period = int(config["ATR"].get("period", 14))
        arr = atr(h, l, c, period)
        series["atr"] = _serialize(arr)
        current["atr"] = _last(arr)

    if config.get("NATR", {}).get("enabled"):
        period = int(config["NATR"].get("period", 14))
        arr = natr(h, l, c, period)
        series["natr"] = _serialize(arr)
        current["natr"] = _last(arr)

    if config.get("STDDEV", {}).get("enabled"):
        period = int(config["STDDEV"].get("period", 20))
        arr = stddev(c, period)
        series["stddev"] = _serialize(arr)
        current["stddev"] = _last(arr)

    # ---- Price Transform ----
    if config.get("TYPPRICE", {}).get("enabled"):
        arr = typprice(h, l, c)
        series["typprice"] = _serialize(arr)
        current["typprice"] = _last(arr)
    if config.get("MEDPRICE", {}).get("enabled"):
        arr = medprice(h, l)
        series["medprice"] = _serialize(arr)
        current["medprice"] = _last(arr)
    if config.get("WCLPRICE", {}).get("enabled"):
        arr = wclprice(h, l, c)
        series["wclprice"] = _serialize(arr)
        current["wclprice"] = _last(arr)
    if config.get("AVGPRICE", {}).get("enabled"):
        arr = avgprice(o, h, l, c)
        series["avgprice"] = _serialize(arr)
        current["avgprice"] = _last(arr)

    # ---- Cycle ----
    if config.get("HT_TRENDLINE", {}).get("enabled"):
        arr = ht_trendline(c)
        series["ht_trendline"] = _serialize(arr)
        current["ht_trendline"] = _last(arr)
    if config.get("HT_DCPERIOD", {}).get("enabled"):
        arr = ht_dcperiod(c)
        series["ht_dcperiod"] = _serialize(arr)
        current["ht_dcperiod"] = _last(arr)
    if config.get("HT_TRENDMODE", {}).get("enabled"):
        arr = ht_trendmode(c)
        series["ht_trendmode"] = _serialize(arr)
        current["ht_trendmode"] = _last(arr)
    if config.get("HT_SINE", {}).get("enabled"):
        s_arr, lead = ht_sine(c)
        series["ht_sine"] = _serialize(s_arr)
        series["ht_leadsine"] = _serialize(lead)
        current["ht_sine"] = _last(s_arr)
        current["ht_leadsine"] = _last(lead)

    # ---- Pattern Recognition ----
    patterns_detected: List[Dict] = []
    for name, fn in PATTERNS.items():
        if config.get(name, {}).get("enabled"):
            arr = fn(o, h, l, c)
            series[name.lower()] = _serialize(arr)
            current[name.lower()] = _last(arr)
            # Collect last 30 occurrences
            for i in range(len(arr)):
                if arr[i] != 0:
                    patterns_detected.append({
                        "index": i,
                        "pattern": name,
                        "signal": int(arr[i]),
                    })

    return series, current, patterns_detected
