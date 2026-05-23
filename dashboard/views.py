import json
import csv
import subprocess
import threading
from datetime import date, datetime, timedelta
from collections import defaultdict

import numpy as np
from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.db.models import Max, Min, Avg, Count, Q, F, Sum, Case, When, IntegerField
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache

from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import (
    Action, Indice, HistoriqueAction, HistoriqueIndice, News,
    ScrapingLog, ApiConfig, CommentHistory, TradingSignal,
    Portefeuille, LignePortefeuille,
    IndicateurCache, SignalChangement,
    FondamentauxAnnuel,
    ConseilSikafinance,
    SignalHistorique,
)
from .models_strategie import AllocationStrategie
from .services_verdict import compute_verdict
from .services_backtest import backtest_action, matrice_confusion_vs_sika
from .services_indicators import (
    adx as ind_adx,
    atr as ind_atr,
    natr as ind_natr,
    obv as ind_obv,
    mfi as ind_mfi,
)


# ============================================================
# Helpers
# ============================================================

def get_last_date(use_cache=True):
    """Dernière date disponible dans HistoriqueAction.

    Mise en cache 60s : c'est la clé de versioning de tout le cache accueil,
    donc on accepte un léger retard d'invalidation (max 60s après un scrape).
    """
    if use_cache:
        cached = cache.get("last_date")
        if cached is not None:
            return cached
    last = HistoriqueAction.objects.aggregate(max_date=Max("date"))["max_date"]
    if use_cache and last is not None:
        cache.set("last_date", last, 60)
    return last


def get_derniere_maj(last_date=None):
    """Date de la dernière donnée disponible (format JJ/MM/AAAA).

    Accepte un `last_date` pré-calculé pour éviter un MAX(date) redondant.
    """
    last = last_date if last_date is not None else get_last_date()
    return last.strftime("%d/%m/%Y") if last else "N/A"


def get_context_base(request, last_date=None):
    """Contexte commun à toutes les pages.

    `last_date` peut être passé par la vue appelante pour mutualiser le
    MAX(date) avec ses propres besoins (évite un aller-retour SQL).
    """
    last = last_date if last_date is not None else get_last_date()
    derniere_maj = last.strftime("%d/%m/%Y") if last else "N/A"
    alerte_maj = bool(last and (datetime.now().date() - last).days > 2)
    return {"derniere_maj": derniere_maj, "alerte_maj": alerte_maj}


def _dividendes_dates_for_ticker(ticker):
    """Charge les dividendes datés pour un ticker depuis
    data/dividendes/dividendes.json.

    Renvoie un dict ``{date: montant_par_action_fcfa}`` :
    - ``a_venir`` : date exacte de détachement (Date_Detachement ISO).
    - ``historique`` (Div_<année>) : approximation au 30 juin de l'année concernée.
    Plusieurs entrées tombant sur la même date sont additionnées.
    """
    out = {}
    if not ticker:
        return out
    path = settings.BASE_DIR / "data" / "dividendes" / "dividendes.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return out

    tk = ticker.strip().lower()

    def _match(item):
        return str(item.get("Ticker", "")).strip().lower() == tk

    for item in payload.get("a_venir", []) or []:
        if not _match(item):
            continue
        try:
            d = datetime.strptime(item.get("Date_Detachement") or "", "%Y-%m-%d").date()
            montant = float(item.get("Montant_FCFA") or 0)
        except (ValueError, TypeError):
            continue
        if montant > 0:
            out[d] = out.get(d, 0.0) + montant

    for item in payload.get("historique", []) or []:
        if not _match(item):
            continue
        for key, value in item.items():
            if not key.startswith("Div_"):
                continue
            if value in (None, "", "-"):
                continue
            try:
                annee = int(key.split("_", 1)[1])
                montant = float(str(value).replace(",", "."))
            except (ValueError, TypeError, IndexError):
                continue
            if montant > 0:
                d = date(annee, 6, 30)
                out[d] = out.get(d, 0.0) + montant
    return out


def _load_dividendes_for_ticker(ticker):
    """Charge les dividendes (à venir + historique) pour un ticker depuis
    data/dividendes/dividendes.json. Retourne (a_venir, historique).
    """
    if not ticker:
        return [], []
    path = settings.BASE_DIR / "data" / "dividendes" / "dividendes.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return [], []

    tk = ticker.strip().lower()

    def _match(item):
        return str(item.get("Ticker", "")).strip().lower() == tk

    a_venir = []
    for item in payload.get("a_venir", []) or []:
        if not _match(item):
            continue
        date_str = item.get("Date_Detachement") or ""
        # Format ISO -> JJ/MM/AAAA quand possible
        date_fmt = date_str
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            date_fmt = d.strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            pass
        a_venir.append({
            "date": date_fmt,
            "statut": item.get("Statut") or "",
            "montant": item.get("Montant_FCFA"),
            "rendement": item.get("Rendement_Pct"),
        })

    historique = []
    for item in payload.get("historique", []) or []:
        if not _match(item):
            continue
        # Reconstituer la liste annuelle [(annee, montant, rendement), ...]
        for key, value in item.items():
            if not key.startswith("Div_"):
                continue
            try:
                annee = int(key.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            if value in (None, "", "-"):
                continue
            try:
                montant = float(str(value).replace(",", "."))
            except (ValueError, TypeError):
                continue
            rend = item.get(f"Rend_{annee}_Pct")
            try:
                rend = float(str(rend).replace(",", ".")) if rend not in (None, "", "-") else None
            except (ValueError, TypeError):
                rend = None
            historique.append({"annee": annee, "montant": montant, "rendement": rend})
        historique.sort(key=lambda x: x["annee"], reverse=True)
        break  # une seule ligne par ticker

    return a_venir, historique


def compute_returns(closes):
    """Calcule les rendements à partir d'une série de cours de clôture."""
    arr = np.array(closes, dtype=float)
    if len(arr) < 2:
        return np.array([])
    return np.diff(arr) / arr[:-1]


def compute_rsi(closes, period=14):
    """Calcule le RSI."""
    arr = np.array(closes, dtype=float)
    if len(arr) < period + 1:
        return None
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_sma(closes, period):
    """Moyenne mobile simple."""
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return None
    return round(np.mean(arr[-period:]), 2)


def compute_ema(closes, period):
    """Moyenne mobile exponentielle."""
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = arr[0]
    for price in arr[1:]:
        ema = (price - ema) * multiplier + ema
    return round(ema, 2)


def compute_bollinger(closes, period=20, num_std=2):
    """Bandes de Bollinger."""
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return None, None, None
    sma = np.mean(arr[-period:])
    std = np.std(arr[-period:])
    return round(sma, 2), round(sma + num_std * std, 2), round(sma - num_std * std, 2)


def compute_macd(closes, fast=12, slow=26, signal=9):
    """MACD."""
    arr = np.array(closes, dtype=float)
    if len(arr) < slow + signal:
        return None, None, None

    def ema_calc(data, period):
        ema = [data[0]]
        mult = 2 / (period + 1)
        for p in data[1:]:
            ema.append((p - ema[-1]) * mult + ema[-1])
        return np.array(ema)

    ema_fast = ema_calc(arr, fast)
    ema_slow = ema_calc(arr, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema_calc(macd_line[slow - 1:], signal)
    histogram = macd_line[-(len(signal_line)):] - signal_line
    return round(macd_line[-1], 2), round(signal_line[-1], 2), round(histogram[-1], 2)


def compute_rsi_series(closes, period=14):
    """Série RSI vectorisée (O(N)) avec lissage de Wilder.

    Retourne une liste de longueur len(closes) où les `period` premières
    valeurs sont None.
    """
    arr = np.asarray(closes, dtype=float)
    n = len(arr)
    if n < period + 1:
        return [None] * n

    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.empty(n - 1, dtype=float)
    avg_loss = np.empty(n - 1, dtype=float)
    avg_gain[:period - 1] = np.nan
    avg_loss[:period - 1] = np.nan
    avg_gain[period - 1] = gains[:period].mean()
    avg_loss[period - 1] = losses[:period].mean()

    # Wilder smoothing
    for i in range(period, n - 1):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i]) / period

    rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.nan), where=avg_loss != 0)
    rsi = 100 - (100 / (1 + rs))
    rsi = np.where(avg_loss == 0, 100.0, rsi)

    out = [None] * n
    for i in range(period, n):
        v = rsi[i - 1]
        out[i] = round(float(v), 2) if np.isfinite(v) else None
    return out


def _ema_array(arr, period):
    """EMA vectorisée retournant un tableau de même longueur que arr."""
    arr = np.asarray(arr, dtype=float)
    n = len(arr)
    if n == 0:
        return np.empty(0)
    mult = 2.0 / (period + 1)
    out = np.empty(n, dtype=float)
    out[0] = arr[0]
    for i in range(1, n):
        out[i] = (arr[i] - out[i - 1]) * mult + out[i - 1]
    return out


def compute_macd_series(closes, fast=12, slow=26, signal=9):
    """Séries MACD vectorisées (O(N))."""
    arr = np.asarray(closes, dtype=float)
    n = len(arr)
    none_list = [None] * n
    if n < slow + signal:
        return none_list, list(none_list), list(none_list)

    ema_fast = _ema_array(arr, fast)
    ema_slow = _ema_array(arr, slow)
    macd_line = ema_fast - ema_slow
    # ligne signal calculée sur la portion valide (à partir de slow-1)
    signal_line_valid = _ema_array(macd_line[slow - 1:], signal)
    signal_line = np.full(n, np.nan)
    signal_line[slow - 1:] = signal_line_valid
    hist = macd_line - signal_line

    threshold = slow + signal - 1  # premier index "fiable"

    def _to_list(a):
        res = [None] * n
        for i in range(threshold, n):
            v = a[i]
            res[i] = round(float(v), 2) if np.isfinite(v) else None
        return res

    return _to_list(macd_line), _to_list(signal_line), _to_list(hist)


def compute_beta(stock_returns, market_returns):
    """Beta d'un titre par rapport au marché."""
    min_len = min(len(stock_returns), len(market_returns))
    if min_len < 30:
        return None
    sr = stock_returns[-min_len:]
    mr = market_returns[-min_len:]
    cov = np.cov(sr, mr)
    if cov[1, 1] == 0:
        return None
    return round(cov[0, 1] / cov[1, 1], 3)


def compute_var(returns, confidence=0.95):
    """Value at Risk historique."""
    if len(returns) < 30:
        return None
    return round(np.percentile(returns, (1 - confidence) * 100), 4)


def compute_sharpe(returns, rf=0.0):
    """Ratio de Sharpe (annualisé)."""
    if len(returns) < 30:
        return None
    mean_r = np.mean(returns)
    std_r = np.std(returns)
    if std_r == 0:
        return None
    return round((mean_r - rf) * np.sqrt(252) / std_r, 3)


# ============================================================
# Pages principales
# ============================================================

# Durée de cache pour les blocs lourds de l'accueil. Les clés sont versionnées
# par `last_date` → invalidation automatique au prochain scrape. 30 min est un
# compromis sûr (la BRVM publie ses cours une fois par jour).
_ACCUEIL_CACHE_TTL = 60 * 30


def _ck(prefix, last_date):
    """Cache key versionnée par la dernière date d'historique."""
    return f"accueil:{prefix}:{last_date.isoformat() if last_date else 'none'}"


def _accueil_performances(last_date):
    """Performances + sparklines + meta action (secteur, nombre_actions).

    Retourne (performances, actions_meta) où actions_meta[ticker] =
    {"secteur": str, "nombre_actions": int|None}.
    """
    key = _ck("perf", last_date)
    cached = cache.get(key)
    if cached is not None:
        return cached

    actions_list = list(Action.objects.all().only("id", "ticker", "secteur", "nombre_actions"))
    actions_by_id = {a.id: a for a in actions_list}

    performances = []
    if last_date and actions_list:
        cutoff = last_date - timedelta(days=60)
        recent_rows = list(
            HistoriqueAction.objects
            .filter(date__gte=cutoff)
            .order_by("action_id", "-date")
            .values("action_id", "date", "cloture", "variation_pct")
        )
        by_action = defaultdict(list)
        for r in recent_rows:
            lst = by_action[r["action_id"]]
            if len(lst) < 30:
                lst.append(r)
        for aid, rows in by_action.items():
            action = actions_by_id.get(aid)
            if not action or not rows:
                continue
            sparkline = [r["cloture"] for r in reversed(rows) if r["cloture"] is not None]
            performances.append({
                "ticker": action.ticker,
                "cloture": rows[0]["cloture"],
                "date": rows[0]["date"],
                "variation": rows[0]["variation_pct"],
                "sparkline": sparkline,
            })

    actions_meta = {
        a.ticker: {"secteur": a.secteur or "", "nombre_actions": a.nombre_actions}
        for a in actions_list
    }
    result = (performances, actions_meta)
    cache.set(key, result, _ACCUEIL_CACHE_TTL)
    return result


def _accueil_indices(last_date):
    """Récupère BRVMC (chart complet + dernier point) et CAPIBRVM (dernière clôture)."""
    key = _ck("indices", last_date)
    cached = cache.get(key)
    if cached is not None:
        return cached

    indices_map = {
        i.ticker: i for i in Indice.objects.filter(ticker__in=["BRVMC", "CAPIBRVM"])
    }
    brvm_c = brvm_c_var = capi = None
    chart_data = {"labels": [], "values": []}

    indice_brvmc = indices_map.get("BRVMC")
    if indice_brvmc:
        brvmc_points = list(
            HistoriqueIndice.objects
            .filter(indice=indice_brvmc)
            .order_by("date")
            .values("date", "cloture", "variation_pct")
        )
        if brvmc_points:
            chart_data["labels"] = [p["date"].strftime("%d/%m/%y") for p in brvmc_points]
            chart_data["values"] = [p["cloture"] for p in brvmc_points]
            last_brvmc = brvmc_points[-1]
            brvm_c = last_brvmc["cloture"]
            brvm_c_var = last_brvmc["variation_pct"]

    indice_capi = indices_map.get("CAPIBRVM")
    if indice_capi:
        capi = (
            HistoriqueIndice.objects
            .filter(indice=indice_capi)
            .order_by("-date")
            .values_list("cloture", flat=True)
            .first()
        )

    result = {"brvm_c": brvm_c, "brvm_c_var": brvm_c_var, "capi": capi, "chart_data": chart_data}
    cache.set(key, result, _ACCUEIL_CACHE_TTL)
    return result


def _accueil_breadth_volume(last_date):
    """Agrégat SQL unique : volume_total + breadth (up/down/flat) du dernier jour."""
    key = _ck("breadth", last_date)
    cached = cache.get(key)
    if cached is not None:
        return cached

    volume_total = 0
    breadth = {"up": 0, "down": 0, "flat": 0}
    if last_date:
        agg = HistoriqueAction.objects.filter(date=last_date).aggregate(
            volume_total=Sum("volume_fcfa"),
            up=Count(Case(When(variation_pct__gt=0, then=1), output_field=IntegerField())),
            down=Count(Case(When(variation_pct__lt=0, then=1), output_field=IntegerField())),
            flat=Count(Case(When(variation_pct=0, then=1), output_field=IntegerField())),
        )
        volume_total = agg["volume_total"] or 0
        breadth = {"up": agg["up"] or 0, "down": agg["down"] or 0, "flat": agg["flat"] or 0}

    result = {"volume_total": volume_total, "breadth": breadth}
    cache.set(key, result, _ACCUEIL_CACHE_TTL)
    return result


def _accueil_featured(last_date):
    """Action sous surveillance (plus gros volume) avec sparkline + RSI + YTD + 52w."""
    key = _ck("featured", last_date)
    cached = cache.get(key)
    if cached is not None:
        return cached

    featured = None
    if last_date:
        top_vol = (
            HistoriqueAction.objects.filter(date=last_date, volume_fcfa__isnull=False)
            .order_by("-volume_fcfa")
            .select_related("action")
            .first()
        )
        if top_vol and top_vol.action:
            action_f = top_vol.action
            hist = list(
                HistoriqueAction.objects.filter(action=action_f)
                .order_by("-date")
                .values("date", "cloture", "variation_pct")[:260]
            )
            closes = [h["cloture"] for h in reversed(hist) if h["cloture"] is not None]
            sparkline_f = closes[-60:] if len(closes) > 60 else closes
            rsi_f = compute_rsi(closes) if len(closes) >= 15 else None
            year_now = datetime.now().year
            year_data = [h for h in hist if h["date"].year == year_now and h["cloture"]]
            var_ytd = None
            if year_data and top_vol.cloture:
                ref = year_data[-1]["cloture"]
                if ref:
                    var_ytd = round((top_vol.cloture - ref) / ref * 100, 2)
            window = closes[-252:] if len(closes) > 252 else closes
            featured = {
                "ticker": action_f.ticker,
                "nom": action_f.nom or action_f.ticker,
                "secteur": action_f.secteur or "Non classé",
                "cloture": top_vol.cloture,
                "variation": top_vol.variation_pct,
                "volume_fcfa": top_vol.volume_fcfa,
                "rsi": rsi_f,
                "var_ytd": var_ytd,
                "high_52": max(window) if window else None,
                "low_52": min(window) if window else None,
                "sparkline": sparkline_f,
            }

    cache.set(key, featured, _ACCUEIL_CACHE_TTL)
    return featured


def _accueil_kpis():
    """3 COUNT() agrégés en une seule liste de helpers, mis en cache court."""
    key = "accueil:kpis"
    cached = cache.get(key)
    if cached is not None:
        return cached
    result = {
        "nb_actions": Action.objects.count(),
        "nb_indices": Indice.objects.count(),
        "nb_news": News.objects.count(),
    }
    # TTL plus court : les compteurs peuvent évoluer hors scrape (admin, etc.)
    cache.set(key, result, 60)
    return result


def _accueil_recent_news():
    """5 dernières news. TTL court (les news arrivent en continu)."""
    key = "accueil:recent_news"
    cached = cache.get(key)
    if cached is not None:
        return cached
    rows = list(
        News.objects.exclude(titre="")
        .order_by("-date_publication")
        .values("id", "titre", "date_publication", "categorie", "url", "image_url")[:5]
    )
    cache.set(key, rows, 60)
    return rows


def accueil(request):
    last_date = get_last_date()
    ctx = get_context_base(request, last_date=last_date)

    kpis = _accueil_kpis()
    performances, actions_meta = _accueil_performances(last_date)
    indices_block = _accueil_indices(last_date)
    bv = _accueil_breadth_volume(last_date)
    featured = _accueil_featured(last_date)
    recent_news = _accueil_recent_news()

    # Tri top/flop (rapide en Python, pas la peine de mettre en cache)
    perf_sorted = sorted(performances, key=lambda x: x["variation"] or 0, reverse=True)
    top5 = perf_sorted[:5]
    flop5 = perf_sorted[-5:][::-1] if len(perf_sorted) >= 5 else perf_sorted[::-1]

    breadth = bv["breadth"]
    breadth_total = breadth["up"] + breadth["down"] + breadth["flat"]
    breadth_ratio = {
        "up": round(breadth["up"] / breadth_total * 100, 1) if breadth_total else 0,
        "down": round(breadth["down"] / breadth_total * 100, 1) if breadth_total else 0,
        "flat": round(breadth["flat"] / breadth_total * 100, 1) if breadth_total else 0,
    }

    ticker_tape = [{
        "ticker": p["ticker"],
        "cloture": p["cloture"],
        "variation": p["variation"],
    } for p in performances]

    heatmap_items = []
    for p in performances:
        meta = actions_meta.get(p["ticker"], {})
        secteur = meta.get("secteur") or "Non classé"
        nb = meta.get("nombre_actions") or 0
        cap = (p["cloture"] or 0) * nb if (p["cloture"] and nb) else 0
        heatmap_items.append({
            "ticker": p["ticker"],
            "secteur": secteur,
            "cloture": p["cloture"],
            "variation": p["variation"],
            "cap": cap,
        })

    nb_actions = kpis["nb_actions"]
    nb_indices = kpis["nb_indices"]
    nb_news = kpis["nb_news"]
    brvm_c = indices_block["brvm_c"]
    brvm_c_var = indices_block["brvm_c_var"]
    capi = indices_block["capi"]
    chart_data = indices_block["chart_data"]
    volume_total = bv["volume_total"]

    ctx.update({
        "nb_actions": nb_actions,
        "nb_indices": nb_indices,
        "nb_news": nb_news,
        "recent_news": recent_news,
        "top5": top5,
        "flop5": flop5,
        "brvm_c": brvm_c,
        "brvm_c_var": brvm_c_var,
        "volume_total": volume_total,
        "capi": capi,
        "chart_data": json.dumps(chart_data),
        "breadth": breadth,
        "breadth_ratio": breadth_ratio,
        "ticker_tape": json.dumps(ticker_tape),
        "heatmap_data": json.dumps(heatmap_items),
        "featured": featured,
        "featured_sparkline": json.dumps(featured["sparkline"]) if featured else "[]",
    })
    return render(request, "dashboard/accueil.html", ctx)


def models_sum(field):
    """Helper pour Sum import."""
    from django.db.models import Sum
    return Sum(field)


def _compute_multi_perf_and_series(rows_by_label):
    """Calcule performances glissantes + calendaires et séries pour plusieurs entités.

    rows_by_label: liste de tuples (ticker, nom, rows) où rows est une liste de
    dicts ordonnés par date avec clés "date" et "cloture".
    Retourne (perf_list, series_list).
    """
    rolling_periods = [
        ("1j", 1), ("1s", 5), ("1m", 22), ("3m", 66),
        ("6m", 132), ("1a", 252), ("3a", 756), ("5a", 1260),
    ]
    today = datetime.now().date()
    cal_starts = {
        "wtd": today - timedelta(days=today.weekday()),
        "mtd": today.replace(day=1),
        "qtd": today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1),
        "std": today.replace(month=1 if today.month <= 6 else 7, day=1),
        "ytd": today.replace(month=1, day=1),
    }

    def _pct(latest, ref):
        if latest is None or ref in (None, 0):
            return None
        return round((latest - ref) / ref * 100, 2)

    perf_list = []
    series_list = []
    for ticker, nom, rows in rows_by_label:
        rows = [r for r in rows if r["cloture"] is not None]
        if not rows:
            continue
        dates = [r["date"] for r in rows]
        closes = [r["cloture"] for r in rows]
        latest = closes[-1]

        perf = {
            "ticker": ticker,
            "nom": nom or ticker,
            "cloture": latest,
            "variation": _pct(latest, closes[-2]) if len(closes) >= 2 else None,
        }
        for code, days in rolling_periods:
            perf[f"var_{code}"] = _pct(latest, closes[-(days + 1)]) if len(closes) > days else None
        perf["var_origine"] = _pct(latest, closes[0])
        for code, start in cal_starts.items():
            ref = None
            for d, c in zip(dates, closes):
                if d < start:
                    ref = c
                else:
                    break
            perf[f"var_{code}"] = _pct(latest, ref)

        perf_list.append(perf)
        series_list.append({
            "ticker": ticker,
            "nom": nom or ticker,
            "dates": [d.strftime("%Y-%m-%d") for d in dates],
            "closes": closes,
        })
    return perf_list, series_list


def analyse_actions(request):
    ctx = get_context_base(request)
    actions = Action.objects.all()
    selected_ticker = request.GET.get("ticker", "")

    selected_action = None
    historiques = []
    indicateurs = {}
    chart_data = {}
    date_range = {}

    # ====== Vue d'ensemble : performances multi-actions + séries pour bar / base 100 ======
    actions_rows = []
    sector_by_ticker = {}
    for a in actions:
        rows = list(
            HistoriqueAction.objects.filter(action=a)
            .order_by("date")
            .values("date", "cloture")
        )
        actions_rows.append((a.ticker, a.nom or a.ticker, rows))
        sector_by_ticker[a.ticker] = (a.secteur or "").strip() or "—"
    actions_perf, actions_series = _compute_multi_perf_and_series(actions_rows)
    for p in actions_perf:
        p["secteur"] = sector_by_ticker.get(p["ticker"], "—")
    actions_perf.sort(key=lambda p: p["ticker"])
    actions_series.sort(key=lambda p: p["ticker"])
    actions_secteurs = sorted({p["secteur"] for p in actions_perf if p["secteur"] and p["secteur"] != "—"})

    # Indices disponibles comme référence (benchmark) sur le graphique base 100
    indices_rows = []
    for ind in Indice.objects.all():
        rows = list(
            HistoriqueIndice.objects.filter(indice=ind)
            .order_by("date")
            .values("date", "cloture")
        )
        indices_rows.append((ind.ticker, ind.nom or ind.ticker, rows))
    _, indices_ref_series = _compute_multi_perf_and_series(indices_rows)

    if selected_ticker:
        try:
            selected_action = Action.objects.get(ticker=selected_ticker)
        except Action.DoesNotExist:
            pass

    if selected_action:
        historiques = list(
            HistoriqueAction.objects.filter(action=selected_action)
            .order_by("date")
            .values("date", "ouverture", "plus_haut", "plus_bas", "cloture", "volume_titres", "volume_fcfa", "variation_pct")
        )

        if historiques:
            closes = [h["cloture"] for h in historiques if h["cloture"] is not None]
            dates = [h["date"].strftime("%Y-%m-%d") for h in historiques if h["cloture"] is not None]
            dates_obj = [h["date"] for h in historiques if h["cloture"] is not None]

            # Plage de dates disponible
            if dates_obj:
                date_range = {
                    "debut": dates_obj[0].strftime("%d/%m/%Y"),
                    "fin": dates_obj[-1].strftime("%d/%m/%Y"),
                    "nb_seances": len(dates_obj),
                }

            if closes:
                # Indicateurs techniques
                indicateurs["dernier_cours"] = closes[-1]
                indicateurs["rsi_14"] = compute_rsi(closes, 14)
                indicateurs["sma_20"] = compute_sma(closes, 20)
                indicateurs["sma_50"] = compute_sma(closes, 50)
                indicateurs["ema_20"] = compute_ema(closes, 20)
                boll_mid, boll_up, boll_low = compute_bollinger(closes, 20, 2)
                indicateurs["bollinger_mid"] = boll_mid
                indicateurs["bollinger_up"] = boll_up
                indicateurs["bollinger_low"] = boll_low
                macd_line, signal_line, histogram = compute_macd(closes)
                indicateurs["macd"] = macd_line
                indicateurs["macd_signal"] = signal_line
                indicateurs["macd_hist"] = histogram
                indicateurs["plus_haut_52s"] = max(closes[-252:]) if len(closes) >= 252 else max(closes)
                indicateurs["plus_bas_52s"] = min(closes[-252:]) if len(closes) >= 252 else min(closes)

                # Performances glissantes
                perf_periods = [
                    ("1j", 1), ("1s", 5), ("1m", 22), ("3m", 66), 
                    ("6m", 132), ("1a", 252), ("3a", 756), ("5a", 1260), ("origine", None)
                ]
                for period_name, period_days in perf_periods:
                    if period_name == "origine":
                        # Performance depuis l'origine
                        if closes[0] and closes[0] != 0:
                            indicateurs["var_origine"] = round((closes[-1] - closes[0]) / closes[0] * 100, 2)
                    elif period_days and len(closes) > period_days:
                        old = closes[-(period_days + 1)]
                        if old and old != 0:
                            indicateurs[f"var_{period_name}"] = round((closes[-1] - old) / old * 100, 2)

                # Performances calendaires (WTD, MTD, QTD, STD, YTD)
                now = datetime.now()
                for cal_period in ["wtd", "mtd", "qtd", "std", "ytd"]:
                    ref_date = None
                    if cal_period == "wtd":
                        # Week to date - lundi de la semaine courante
                        ref_date = now - timedelta(days=now.weekday())
                    elif cal_period == "mtd":
                        # Month to date - 1er du mois
                        ref_date = now.replace(day=1)
                    elif cal_period == "qtd":
                        # Quarter to date - 1er du trimestre
                        quarter_month = ((now.month - 1) // 3) * 3 + 1
                        ref_date = now.replace(month=quarter_month, day=1)
                    elif cal_period == "std":
                        # Semester to date - 1er du semestre
                        sem_month = 1 if now.month <= 6 else 7
                        ref_date = now.replace(month=sem_month, day=1)
                    elif cal_period == "ytd":
                        # Year to date - 1er janvier
                        ref_date = now.replace(month=1, day=1)
                    
                    if ref_date:
                        ref_date = ref_date.date() if hasattr(ref_date, 'date') else ref_date
                        ref_data = [h for h in historiques if h["date"] >= ref_date and h["cloture"]]
                        if ref_data:
                            old = ref_data[0]["cloture"]
                            if old and old != 0:
                                indicateurs[f"var_{cal_period}"] = round((closes[-1] - old) / old * 100, 2)

                # Beta vs BRVMC
                try:
                    indice_brvmc = Indice.objects.get(ticker="BRVMC")
                    idx_closes = list(
                        HistoriqueIndice.objects.filter(indice=indice_brvmc)
                        .order_by("date")
                        .values_list("cloture", flat=True)
                    )
                    stock_returns = compute_returns(closes)
                    market_returns = compute_returns([c for c in idx_closes if c is not None])
                    indicateurs["beta"] = compute_beta(stock_returns, market_returns)
                except Indice.DoesNotExist:
                    pass

                # Chart data - toutes les données
                _hist_valid = [h for h in historiques if h["cloture"] is not None]
                chart_data = {
                    "dates": dates,
                    "closes": closes,
                    "opens": [h["ouverture"] for h in _hist_valid],
                    "highs": [h["plus_haut"] for h in _hist_valid],
                    "lows": [h["plus_bas"] for h in _hist_valid],
                    "volumes": [h["volume_titres"] for h in _hist_valid],
                }

                # ===== Indicateurs additionnels pour le verdict 4 axes =====
                _hv = _hist_valid
                highs_arr = np.array(
                    [h["plus_haut"] if h["plus_haut"] is not None else np.nan for h in _hv],
                    dtype=float,
                )
                lows_arr = np.array(
                    [h["plus_bas"] if h["plus_bas"] is not None else np.nan for h in _hv],
                    dtype=float,
                )
                closes_arr = np.array(closes, dtype=float)
                volumes_arr = np.array(
                    [(h.get("volume_titres") or 0) for h in _hv],
                    dtype=float,
                )

                # Combler les NaN H/L par le close (rare, défensif)
                if np.isnan(highs_arr).any():
                    mask = np.isnan(highs_arr)
                    highs_arr[mask] = closes_arr[mask]
                if np.isnan(lows_arr).any():
                    mask = np.isnan(lows_arr)
                    lows_arr[mask] = closes_arr[mask]

                adx_val = di_p_val = di_m_val = None
                atr_val_v = natr_last = obv_last = mfi_last = None
                natr_list: list = []
                obv_list: list = []

                if len(closes_arr) >= 30:
                    try:
                        adx_s, dip_s, dim_s = ind_adx(highs_arr, lows_arr, closes_arr, 14)
                        atr_s = ind_atr(highs_arr, lows_arr, closes_arr, 14)
                        natr_s = ind_natr(highs_arr, lows_arr, closes_arr, 14)
                        obv_s = ind_obv(closes_arr, volumes_arr)
                        mfi_s = ind_mfi(highs_arr, lows_arr, closes_arr, volumes_arr, 14)

                        def _lf(arr):
                            if arr is None or len(arr) == 0:
                                return None
                            v = arr[-1]
                            return float(v) if np.isfinite(v) else None

                        def _lstf(arr):
                            return [float(v) if np.isfinite(v) else None for v in arr]

                        adx_val = _lf(adx_s)
                        di_p_val = _lf(dip_s)
                        di_m_val = _lf(dim_s)
                        atr_val_v = _lf(atr_s)
                        natr_last = _lf(natr_s)
                        obv_last = _lf(obv_s)
                        mfi_last = _lf(mfi_s)
                        natr_list = _lstf(natr_s)
                        obv_list = _lstf(obv_s)
                    except Exception:
                        # En cas de pépin numérique, on laisse les sous-signaux à None
                        pass

                indicateurs["adx_14"] = round(adx_val, 2) if adx_val is not None else None
                indicateurs["di_plus_14"] = round(di_p_val, 2) if di_p_val is not None else None
                indicateurs["di_minus_14"] = round(di_m_val, 2) if di_m_val is not None else None
                indicateurs["atr_14"] = round(atr_val_v, 2) if atr_val_v is not None else None
                indicateurs["natr_14"] = round(natr_last, 2) if natr_last is not None else None
                indicateurs["obv"] = round(obv_last, 2) if obv_last is not None else None
                indicateurs["mfi_14"] = round(mfi_last, 2) if mfi_last is not None else None

                # ===== Verdict synthétique 4 axes (modalités Sikafinance) =====
                indicateurs["verdict"] = compute_verdict(
                    sma20=indicateurs.get("sma_20"),
                    sma50=indicateurs.get("sma_50"),
                    adx=adx_val,
                    di_plus=di_p_val,
                    di_minus=di_m_val,
                    rsi_val=indicateurs.get("rsi_14"),
                    macd_hist=indicateurs.get("macd_hist"),
                    atr_val=atr_val_v,
                    last_close=closes[-1],
                    bb_upper=indicateurs.get("bollinger_up"),
                    bb_lower=indicateurs.get("bollinger_low"),
                    natr_series=natr_list,
                    obv_series=obv_list,
                    mfi_val=mfi_last,
                )

                # Calculer séries RSI (vectorisé O(N))
                chart_data["rsi"] = compute_rsi_series(closes, 14)

                # Calculer séries MACD (vectorisé O(N))
                macd_line_series, macd_signal_series, macd_hist_series = compute_macd_series(closes)
                chart_data["macd_line"] = macd_line_series
                chart_data["macd_signal"] = macd_signal_series
                chart_data["macd_hist"] = macd_hist_series

    # Données dividendes (à venir + historique annuel) pour le ticker sélectionné
    dividendes_a_venir = []
    dividendes_historique = []
    if selected_action:
        dividendes_a_venir, dividendes_historique = _load_dividendes_for_ticker(selected_action.ticker)

    # Fondamentaux annuels (matrice 5 ans Sikafinance) + métriques dérivées
    fondamentaux_rows = []
    fondamentaux_dernier = None
    fondamentaux_kpis = {}
    if selected_action:
        fondamentaux_rows = list(
            FondamentauxAnnuel.objects
            .filter(action=selected_action)
            .order_by("-exercice")
        )
        if fondamentaux_rows:
            fondamentaux_dernier = fondamentaux_rows[0]
            d = fondamentaux_dernier
            dernier_cours = indicateurs.get("dernier_cours")

            # Yield basé sur le dernier cours observé (plus fiable que BNPA×PER)
            yield_div = None
            if d.dividende and dernier_cours:
                yield_div = d.dividende / dernier_cours * 100

            # Capi boursière = cours × nombre d'actions
            capi = None
            if dernier_cours and selected_action.nombre_actions:
                capi = dernier_cours * selected_action.nombre_actions

            # Marge nette = RN / CA en %
            marge_nette = None
            if d.resultat_net and d.chiffre_affaires:
                marge_nette = d.resultat_net / d.chiffre_affaires * 100

            # CAGR du CA et du RN sur la fenêtre disponible
            def _cagr(series):
                """series ordonnée du plus ancien au plus récent."""
                vals = [v for v in series if v is not None and v > 0]
                if len(vals) < 2:
                    return None
                n = len(vals) - 1
                return ((vals[-1] / vals[0]) ** (1.0 / n) - 1) * 100

            asc = list(reversed(fondamentaux_rows))
            cagr_ca = _cagr([r.chiffre_affaires for r in asc])
            cagr_rn = _cagr([r.resultat_net for r in asc])

            fondamentaux_kpis = {
                "exercice": d.exercice,
                "chiffre_affaires": d.chiffre_affaires,
                "croissance_ca": d.croissance_ca,
                "resultat_net": d.resultat_net,
                "croissance_rn": d.croissance_rn,
                "marge_nette": marge_nette,
                "bnpa": d.bnpa,
                "per": d.per,
                "dividende": d.dividende,
                "yield_div": yield_div,
                "payout": d.payout_ratio,
                "capi": capi,
                "cagr_ca": cagr_ca,
                "cagr_rn": cagr_rn,
                "nb_exercices": len(fondamentaux_rows),
            }

    # NB: Le backtest a été retiré de cette page.
    # On expose toujours `backtest = None` dans le contexte au cas où des
    # éléments legacy du template y feraient référence (résilient).
    backtest = None

    # ===== Matrice de confusion verdict technique vs Sikafinance =====
    # Affichée dans le sous-onglet Indicateurs techniques de la page d'analyse.
    confusion_sika = None
    if selected_action:
        try:
            confusion_sika = matrice_confusion_vs_sika(selected_action)
        except Exception:
            confusion_sika = None

    # ===== Conseil Sikafinance (dernier snapshot) + comparaison verdict technique =====
    conseil_sika = None
    sika_comparaison = None
    if selected_action:
        conseil_sika = (
            ConseilSikafinance.objects
            .filter(action=selected_action)
            .order_by("-date_scrape")
            .first()
        )
        verdict_code = (indicateurs.get("verdict") or {}).get("code")
        sika_code = conseil_sika.code if conseil_sika else None

        _BULL = {"ACHETER", "RENFORCER"}
        _BEAR = {"ALLEGER", "VENDRE"}
        _NEUTRE = {"CONSERVER"}
        _NA = {None, "NA", "INCONNU"}

        if verdict_code in _NA or sika_code in _NA:
            statut = "indisponible"
        elif verdict_code == sika_code:
            statut = "concordance_exacte"
        elif (
            (verdict_code in _BULL and sika_code in _BULL)
            or (verdict_code in _BEAR and sika_code in _BEAR)
        ):
            statut = "concordance_directionnelle"
        elif (
            (verdict_code in _BULL and sika_code in _BEAR)
            or (verdict_code in _BEAR and sika_code in _BULL)
        ):
            statut = "divergence_forte"
        else:
            statut = "divergence_legere"  # un des deux est CONSERVER

        sika_comparaison = {
            "statut": statut,
            "verdict_code": verdict_code,
            "sika_code": sika_code,
        }

    ctx.update({
        "actions": actions,
        "selected_ticker": selected_ticker,
        "selected_action": selected_action,
        "indicateurs": indicateurs,
        "conseil_sika": conseil_sika,
        "sika_comparaison": sika_comparaison,
        "backtest": backtest,
        "confusion_sika": confusion_sika,
        "backtest_pnl_json": json.dumps({
            "dates": (backtest or {}).get("pnl", {}).get("dates", []),
            "sans_frais": (backtest or {}).get("pnl", {}).get("sans_frais", []),
            "avec_frais": (backtest or {}).get("pnl", {}).get("avec_frais_100bps", []),
        }, default=str) if backtest and backtest.get("disponible") else "{}",
        "date_range": date_range,
        "chart_data": json.dumps(chart_data, default=str),
        "historiques_recent": historiques[-10:][::-1] if historiques else [],
        "dividendes_a_venir": dividendes_a_venir,
        "dividendes_historique": dividendes_historique,
        "fondamentaux_rows": fondamentaux_rows,
        "fondamentaux_kpis": fondamentaux_kpis,
        "actions_perf": actions_perf,
        "actions_secteurs": actions_secteurs,
        "actions_series_json": json.dumps(
            {"actions": actions_series, "indices": indices_ref_series},
            default=str,
        ),
    })
    return render(request, "dashboard/analyse_actions.html", ctx)


def analyse_indices(request):
    ctx = get_context_base(request)
    indices = Indice.objects.all()

    # ====== Performances multi-indices + données graphiques ======
    # Périodes glissantes (en jours de bourse approx.)
    rolling_periods = [
        ("1j", 1), ("1s", 5), ("1m", 22), ("3m", 66),
        ("6m", 132), ("1a", 252), ("3a", 756), ("5a", 1260),
    ]
    # Périodes calendaires
    today = datetime.now().date()
    cal_starts = {
        "wtd": today - timedelta(days=today.weekday()),
        "mtd": today.replace(day=1),
        "qtd": today.replace(month=((today.month - 1) // 3) * 3 + 1, day=1),
        "std": today.replace(month=1 if today.month <= 6 else 7, day=1),
        "ytd": today.replace(month=1, day=1),
    }

    def _pct(latest, ref):
        if latest is None or ref in (None, 0):
            return None
        return round((latest - ref) / ref * 100, 2)

    indices_perf = []
    indices_series = []
    for indice in indices:
        rows = list(
            HistoriqueIndice.objects.filter(indice=indice)
            .order_by("date")
            .values("date", "cloture")
        )
        rows = [r for r in rows if r["cloture"] is not None]
        if not rows:
            continue

        dates = [r["date"] for r in rows]
        closes = [r["cloture"] for r in rows]
        latest = closes[-1]

        perf = {
            "ticker": indice.ticker,
            "nom": indice.nom or indice.ticker,
            "cloture": latest,
            "variation": None,
        }
        # Variation jour J (= 1j déjà ci-dessous, mais on garde la colonne dernière variation)
        if len(closes) >= 2:
            perf["variation"] = _pct(latest, closes[-2])

        # Rolling
        for code, days in rolling_periods:
            if len(closes) > days:
                perf[f"var_{code}"] = _pct(latest, closes[-(days + 1)])
            else:
                perf[f"var_{code}"] = None
        # Origine
        perf["var_origine"] = _pct(latest, closes[0])

        # Calendaires : dernière clôture STRICTEMENT avant la date de référence
        for code, start in cal_starts.items():
            ref = None
            for d, c in zip(dates, closes):
                if d < start:
                    ref = c
                else:
                    break
            perf[f"var_{code}"] = _pct(latest, ref)

        indices_perf.append(perf)
        indices_series.append({
            "ticker": indice.ticker,
            "nom": indice.nom or indice.ticker,
            "dates": [d.strftime("%Y-%m-%d") for d in dates],
            "closes": closes,
        })

    # Tri stable : BRVMC d'abord, BRVM30 ensuite, puis ordre alpha
    def _sort_key(p):
        order = {"BRVMC": 0, "BRVM30": 1}
        return (order.get(p["ticker"], 99), p["ticker"])
    indices_perf.sort(key=_sort_key)
    indices_series.sort(key=lambda p: _sort_key({"ticker": p["ticker"]}))

    ctx.update({
        "indices": indices,
        "indices_perf": indices_perf,
        "indices_series_json": json.dumps({"indices": indices_series}, default=str),
    })
    return render(request, "dashboard/analyse_indices.html", ctx)


def analyse_nouvelles(request):
    ctx = get_context_base(request)
    page = int(request.GET.get("page", 1))
    search = request.GET.get("q", "")
    categorie = request.GET.get("categorie", "")
    per_page = 20

    news_qs = News.objects.all()
    if search:
        news_qs = news_qs.filter(Q(titre__icontains=search) | Q(contenu__icontains=search))
    if categorie:
        news_qs = news_qs.filter(categorie__icontains=categorie)

    total = news_qs.count()
    news_list = list(news_qs[(page - 1) * per_page: page * per_page])
    total_pages = (total + per_page - 1) // per_page

    # Catégories uniques
    categories = list(
        News.objects.exclude(categorie="").values_list("categorie", flat=True).distinct()[:30]
    )

    ctx.update({
        "news_list": news_list,
        "search": search,
        "categorie": categorie,
        "categories": categories,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })
    return render(request, "dashboard/analyse_nouvelles.html", ctx)


def performances(request):
    ctx = get_context_base(request)

    # Tableau de toutes les actions avec performances multi-périodes
    actions = Action.objects.all()
    perf_data = []

    for action in actions:
        hist = list(
            HistoriqueAction.objects.filter(action=action)
            .order_by("date")
            .values_list("cloture", flat=True)
        )
        hist = [c for c in hist if c is not None]

        if not hist:
            continue

        row = {
            "ticker": action.ticker,
            "pays": action.pays,
            "dernier_cours": hist[-1],
            "per": action.per,
            "dividende": action.dividende,
        }

        for period_name, period_days in [("1j", 1), ("1s", 5), ("1m", 22), ("3m", 66), ("6m", 132), ("1a", 252), ("ytd", None)]:
            if period_days and len(hist) > period_days:
                old = hist[-(period_days + 1)]
                if old and old != 0:
                    row[f"var_{period_name}"] = round((hist[-1] - old) / old * 100, 2)
                else:
                    row[f"var_{period_name}"] = None
            elif period_name == "ytd":
                # On ne peut pas facilement obtenir la date ici, on approxime
                # En utilisant les 60 derniers jours comme proxy si pas assez de données
                row["var_ytd"] = None
            else:
                row[f"var_{period_name}"] = None

        perf_data.append(row)

    ctx.update({
        "perf_data": perf_data,
    })
    return render(request, "dashboard/performances.html", ctx)


def risque_volatilite(request):
    ctx = get_context_base(request)

    # Sélection des tickers pour la matrice de corrélation
    actions = Action.objects.all()
    selected_tickers = request.GET.getlist("tickers")
    if not selected_tickers:
        # Top 10 par volume par défaut
        selected_tickers = list(actions.values_list("ticker", flat=True)[:10])

    # Calculer les métriques de risque pour chaque action
    risk_data = []
    returns_dict = {}

    for action in actions:
        hist = list(
            HistoriqueAction.objects.filter(action=action)
            .order_by("date")
            .values_list("cloture", flat=True)
        )
        hist = [c for c in hist if c is not None]
        if len(hist) < 30:
            continue

        returns = compute_returns(hist)
        returns_dict[action.ticker] = returns

        volatility = round(np.std(returns) * np.sqrt(252) * 100, 2)
        var_95 = compute_var(returns, 0.95)
        var_99 = compute_var(returns, 0.99)
        sharpe = compute_sharpe(returns)
        max_dd = compute_max_drawdown(hist)

        risk_data.append({
            "ticker": action.ticker,
            "volatilite": volatility,
            "var_95": round(var_95 * 100, 2) if var_95 else None,
            "var_99": round(var_99 * 100, 2) if var_99 else None,
            "sharpe": sharpe,
            "max_drawdown": max_dd,
        })

    # Matrice de corrélation
    correlation_matrix = []
    corr_tickers = [t for t in selected_tickers if t in returns_dict]

    if len(corr_tickers) >= 2:
        for i, t1 in enumerate(corr_tickers):
            row = []
            for j, t2 in enumerate(corr_tickers):
                r1 = returns_dict[t1]
                r2 = returns_dict[t2]
                min_len = min(len(r1), len(r2))
                if min_len > 30:
                    corr = np.corrcoef(r1[-min_len:], r2[-min_len:])[0, 1]
                    row.append(round(corr, 3))
                else:
                    row.append(None)
            correlation_matrix.append(row)

    ctx.update({
        "actions": actions,
        "risk_data": risk_data,
        "selected_tickers": selected_tickers,
        "corr_tickers": corr_tickers,
        "correlation_matrix": json.dumps(correlation_matrix),
        "corr_tickers_json": json.dumps(corr_tickers),
    })
    return render(request, "dashboard/risque_volatilite.html", ctx)


def compute_max_drawdown(prices):
    """Calcul du drawdown maximum."""
    arr = np.array(prices, dtype=float)
    if len(arr) < 2:
        return None
    peak = arr[0]
    max_dd = 0
    for price in arr[1:]:
        if price > peak:
            peak = price
        dd = (peak - price) / peak
        if dd > max_dd:
            max_dd = dd
    return round(max_dd * 100, 2)


def actualisation(request):
    from pathlib import Path
    from datetime import datetime

    ctx = get_context_base(request)

    logs = ScrapingLog.objects.all()[:20]

    data_dir = Path(settings.DATA_DIR)

    def _freshness(folder, pattern="*"):
        """Renvoie (date_iso, jours_depuis, nb_fichiers) pour le fichier le plus récent."""
        p = data_dir / folder
        if not p.exists():
            return None, None, 0
        files = list(p.glob(pattern))
        if not files:
            return None, None, 0
        latest = max(files, key=lambda f: f.stat().st_mtime)
        mtime = datetime.fromtimestamp(latest.stat().st_mtime)
        jours = (datetime.now() - mtime).days
        return mtime, jours, len(files)

    cours_date, cours_jours, cours_nb = _freshness("actions", "*_historique.csv")
    indices_date, indices_jours, indices_nb = _freshness("indices", "*.csv")
    news_date, news_jours, news_nb = _freshness("news", "*.csv")
    div_date, div_jours, div_nb = _freshness("dividendes", "*.csv")
    soc_date, soc_jours, soc_nb = _freshness("societes", "*_societe.json")

    datasets = [
        {
            "key": "cours", "label": "Cours actions",
            "icon": "bi-bar-chart-line", "color": "primary",
            "date": cours_date, "jours": cours_jours, "nb": cours_nb,
            "seuil_warn": 2, "seuil_danger": 5,
            "db_count": HistoriqueAction.objects.count(),
            "db_label": "points historiques",
        },
        {
            "key": "indices", "label": "Indices",
            "icon": "bi-activity", "color": "info",
            "date": indices_date, "jours": indices_jours, "nb": indices_nb,
            "seuil_warn": 2, "seuil_danger": 5,
            "db_count": HistoriqueIndice.objects.count(),
            "db_label": "points indices",
        },
        {
            "key": "news", "label": "Actualités",
            "icon": "bi-newspaper", "color": "warning",
            "date": news_date, "jours": news_jours, "nb": news_nb,
            "seuil_warn": 3, "seuil_danger": 7,
            "db_count": News.objects.count(),
            "db_label": "articles",
        },
        {
            "key": "dividendes", "label": "Dividendes",
            "icon": "bi-cash-coin", "color": "success",
            "date": div_date, "jours": div_jours, "nb": div_nb,
            "seuil_warn": 7, "seuil_danger": 30,
            "db_count": None, "db_label": "",
        },
        {
            "key": "societes", "label": "Infos sociétés",
            "icon": "bi-building", "color": "secondary",
            "date": soc_date, "jours": soc_jours, "nb": soc_nb,
            "seuil_warn": 14, "seuil_danger": 60,
            "db_count": Action.objects.count(),
            "db_label": "sociétés",
        },
    ]

    # Statut global (le pire de tous)
    statuts = []
    for d in datasets:
        if d["jours"] is None:
            statuts.append("vide")
        elif d["jours"] >= d["seuil_danger"]:
            statuts.append("danger")
        elif d["jours"] >= d["seuil_warn"]:
            statuts.append("warning")
        else:
            statuts.append("ok")
        d["statut"] = statuts[-1]

    if "danger" in statuts or "vide" in statuts:
        statut_global = "danger"
    elif "warning" in statuts:
        statut_global = "warning"
    else:
        statut_global = "ok"

    ctx.update({
        "logs": logs,
        "datasets": datasets,
        "statut_global": statut_global,
        "github_actions_url": "https://github.com/QuantDylane/BRVM-Lookup-CGF/actions",
    })
    return render(request, "dashboard/actualisation.html", ctx)


# ============================================================
# API Endpoints
# ============================================================

def api_action_data(request, ticker):
    """Retourne les données historiques d'une action en JSON."""
    try:
        action = Action.objects.get(ticker=ticker)
    except Action.DoesNotExist:
        return JsonResponse({"error": "Action introuvable"}, status=404)

    days = int(request.GET.get("days", 252))
    hist = list(
        HistoriqueAction.objects.filter(action=action)
        .order_by("date")
        .values("date", "ouverture", "plus_haut", "plus_bas", "cloture", "volume_titres")
    )[-days:]

    return JsonResponse({
        "ticker": ticker,
        "data": [
            {
                "date": h["date"].isoformat(),
                "open": h["ouverture"],
                "high": h["plus_haut"],
                "low": h["plus_bas"],
                "close": h["cloture"],
                "volume": h["volume_titres"],
            }
            for h in hist
        ]
    })


def api_indice_data(request, ticker):
    """Retourne les données historiques d'un indice en JSON."""
    try:
        indice = Indice.objects.get(ticker=ticker)
    except Indice.DoesNotExist:
        return JsonResponse({"error": "Indice introuvable"}, status=404)

    days = int(request.GET.get("days", 252))
    hist = list(
        HistoriqueIndice.objects.filter(indice=indice)
        .order_by("date")
        .values("date", "cloture", "variation_pct")
    )[-days:]

    return JsonResponse({
        "ticker": ticker,
        "data": [
            {"date": h["date"].isoformat(), "close": h["cloture"], "variation": h["variation_pct"]}
            for h in hist
        ]
    })


# ============================================================
# Santé du marché : Fear & Greed + Régime MM
# ============================================================

def _classify_regime(p, mm20, mm50, mm200):
    """Classifie le régime selon prix + MM20/MM50/MM200.
    Retourne (code, label, color_hex)."""
    if None in (p, mm20, mm50, mm200):
        return ("UNKNOWN", "Insuffisant", "#6b7280")

    if p > mm200:
        # BULL
        if p > mm20 > mm50 > mm200:
            return ("STRONG_BULL", "Strong Bull", "#1e40af")
        if p > mm50 > mm20 > mm200:
            return ("BULL", "Bull", "#16a34a")
        if mm20 > p > mm50 > mm200:
            return ("BULL_PULLBACK", "Bull en pullback", "#eab308")
        if mm50 > p > mm20 > mm200:
            return ("BULL_PULLBACK", "Bull en pullback", "#eab308")
        if mm20 > mm50 > p > mm200:
            return ("BULL_WEAK", "Bull faible", "#f97316")
        if mm50 > mm20 > p > mm200:
            return ("BULL_WEAK", "Bull faible", "#f97316")
        return ("BULL", "Bull (atypique)", "#16a34a")
    else:
        # BEAR
        if p < mm20 < mm50 < mm200:
            return ("STRONG_BEAR", "Strong Bear", "#1e40af")
        if p < mm50 < mm20 < mm200:
            return ("BEAR", "Bear", "#dc2626")
        if mm20 < p < mm50 < mm200:
            return ("BEAR_REBOUND", "Bear en rebond", "#eab308")
        if mm50 < p < mm20 < mm200:
            return ("BEAR_REBOUND", "Bear en rebond", "#eab308")
        if mm20 < mm50 < p < mm200:
            return ("BEAR_WEAK", "Bear faible", "#f97316")
        if mm50 < mm20 < p < mm200:
            return ("BEAR_WEAK", "Bear faible", "#f97316")
        return ("BEAR", "Bear (atypique)", "#dc2626")


def _sma(arr, n):
    """SMA classique. Retourne une liste alignée (None pour i<n-1)."""
    if len(arr) < n:
        return [None] * len(arr)
    out = [None] * (n - 1)
    s = sum(arr[:n])
    out.append(s / n)
    for i in range(n, len(arr)):
        s += arr[i] - arr[i - n]
        out.append(s / n)
    return out


@csrf_exempt
def api_market_regime(request):
    """Régime de marché par moyennes mobiles (MM20/MM50/MM200) pour un indice.

    Params: ticker (def: BRVMC), mm_short (20), mm_mid (50), mm_long (200),
            days (def: 252 = 1 an)
    Retourne : régime actuel + série historique (1 an) pour graphique avec bandes de régime.
    """
    ticker = request.GET.get("ticker", "BRVMC")
    try:
        mm_s = max(2, int(request.GET.get("mm_short", 20)))
        mm_m = max(mm_s + 1, int(request.GET.get("mm_mid", 50)))
        mm_l = max(mm_m + 1, int(request.GET.get("mm_long", 200)))
        days = max(60, int(request.GET.get("days", 252)))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Paramètres numériques invalides"}, status=400)

    try:
        indice = Indice.objects.get(ticker=ticker)
    except Indice.DoesNotExist:
        return JsonResponse({"error": f"Indice {ticker} introuvable"}, status=404)

    # On lit assez d'historique pour calculer MM_long puis tronquer à `days`
    needed = days + mm_l + 5
    hist = list(
        HistoriqueIndice.objects.filter(indice=indice)
        .order_by("date")
        .values("date", "cloture")
    )
    hist = [h for h in hist if h["cloture"] is not None]
    if len(hist) < mm_l + 5:
        return JsonResponse({"error": "Historique insuffisant"}, status=400)
    hist = hist[-needed:]

    closes = [h["cloture"] for h in hist]
    dates = [h["date"] for h in hist]
    sma20 = _sma(closes, mm_s)
    sma50 = _sma(closes, mm_m)
    sma200 = _sma(closes, mm_l)

    # Tronquer aux `days` derniers points pour l'affichage
    n = len(closes)
    start = max(0, n - days)
    out_dates = [d.strftime("%Y-%m-%d") for d in dates[start:]]
    out_close = closes[start:]
    out_s20 = sma20[start:]
    out_s50 = sma50[start:]
    out_s200 = sma200[start:]

    # Régime quotidien sur la fenêtre affichée
    regimes = []
    for i in range(len(out_close)):
        code, label, color = _classify_regime(out_close[i], out_s20[i], out_s50[i], out_s200[i])
        regimes.append({"code": code, "label": label, "color": color})

    # Régime actuel
    current_code, current_label, current_color = _classify_regime(
        closes[-1], sma20[-1], sma50[-1], sma200[-1]
    )

    return JsonResponse({
        "ticker": ticker,
        "params": {"mm_short": mm_s, "mm_mid": mm_m, "mm_long": mm_l, "days": days},
        "dates": out_dates,
        "close": out_close,
        "sma_short": out_s20,
        "sma_mid": out_s50,
        "sma_long": out_s200,
        "regimes": regimes,
        "current": {
            "code": current_code,
            "label": current_label,
            "color": current_color,
            "price": closes[-1],
            "mm_short": sma20[-1],
            "mm_mid": sma50[-1],
            "mm_long": sma200[-1],
            "date": dates[-1].strftime("%Y-%m-%d"),
        },
    })


@csrf_exempt
def api_market_regime_all(request):
    """Régime actuel pour tous les indices BRVM (résumé compact)."""
    try:
        mm_s = max(2, int(request.GET.get("mm_short", 20)))
        mm_m = max(mm_s + 1, int(request.GET.get("mm_mid", 50)))
        mm_l = max(mm_m + 1, int(request.GET.get("mm_long", 200)))
    except (ValueError, TypeError):
        return JsonResponse({"error": "Paramètres invalides"}, status=400)

    results = []
    for indice in Indice.objects.all():
        hist = list(
            HistoriqueIndice.objects.filter(indice=indice)
            .order_by("date").values("date", "cloture")
        )
        closes = [h["cloture"] for h in hist if h["cloture"] is not None]
        if len(closes) < mm_l + 1:
            results.append({
                "ticker": indice.ticker,
                "nom": indice.nom or indice.ticker,
                "code": "UNKNOWN",
                "label": "Historique insuffisant",
                "color": "#6b7280",
                "price": None,
                "mm_short": None, "mm_mid": None, "mm_long": None,
            })
            continue
        s20 = _sma(closes, mm_s)
        s50 = _sma(closes, mm_m)
        s200 = _sma(closes, mm_l)
        code, label, color = _classify_regime(closes[-1], s20[-1], s50[-1], s200[-1])
        results.append({
            "ticker": indice.ticker,
            "nom": indice.nom or indice.ticker,
            "code": code,
            "label": label,
            "color": color,
            "price": closes[-1],
            "mm_short": s20[-1],
            "mm_mid": s50[-1],
            "mm_long": s200[-1],
        })

    # Ordre: BRVMC, BRVM30, puis alpha
    order = {"BRVMC": 0, "BRVM30": 1}
    results.sort(key=lambda x: (order.get(x["ticker"], 99), x["ticker"]))
    return JsonResponse({"items": results, "params": {"mm_short": mm_s, "mm_mid": mm_m, "mm_long": mm_l}})


def _percentile_rank(series, value):
    """Renvoie le rang percentile (0-100) de `value` dans `series`."""
    if not series:
        return None
    arr = np.array([v for v in series if v is not None], dtype=float)
    if len(arr) == 0:
        return None
    return float(np.mean(arr <= value)) * 100


def _compute_fear_greed(
    w1=1.0, w2=1.0, w3=1.0, w4=1.0,
    breadth_window=20, vol_short_n=20, vol_long_n=90,
    mom_window=20, disp_lookback=252, ticker="BRVMC",
):
    """Calcule l'indicateur Fear & Greed BRVM (pur helper, pas de request).

    Retourne un dict prêt à être renvoyé en JSON ou utilisé dans le PDF.
    Renvoie None si aucune donnée n'est disponible.
    """
    last_date = HistoriqueAction.objects.aggregate(d=Max("date"))["d"]
    if not last_date:
        return None

    # ---- 2. S_breadth : % actions dont rendement_{breadth_window} > 0
    actions_qs = list(Action.objects.all().values_list("id", "ticker"))
    nb_total = 0
    nb_up = 0
    for aid, tk in actions_qs:
        rows = list(
            HistoriqueAction.objects.filter(action_id=aid)
            .order_by("-date")
            .values_list("cloture", flat=True)[: breadth_window + 1]
        )
        rows = [r for r in rows if r is not None]
        if len(rows) < breadth_window + 1:
            continue
        last = rows[0]
        ref = rows[breadth_window]
        if not ref:
            continue
        nb_total += 1
        if (last - ref) / ref > 0:
            nb_up += 1

    s_breadth = (nb_up / nb_total * 100) if nb_total > 0 else None

    # ---- 3. S_vol, S_mom : sur l'indice de référence (BRVMC)
    s_vol = None
    s_mom = None
    vol_short_val = None
    vol_long_val = None
    mom_val = None
    try:
        ind = Indice.objects.get(ticker=ticker)
        ind_closes = list(
            HistoriqueIndice.objects.filter(indice=ind)
            .order_by("date").values_list("cloture", flat=True)
        )
        ind_closes = [c for c in ind_closes if c is not None]
        if len(ind_closes) >= vol_long_n + 5:
            rets = compute_returns(ind_closes)
            # vol annualisée
            short_rets = rets[-vol_short_n:]
            long_rets = rets[-vol_long_n:]
            if len(short_rets) >= 5 and len(long_rets) >= 5:
                vol_short_val = float(np.std(short_rets, ddof=1) * np.sqrt(252))
                vol_long_val = float(np.std(long_rets, ddof=1) * np.sqrt(252))
                if vol_long_val > 0:
                    ratio = vol_short_val / vol_long_val
                    raw = 100 * (1 - ratio) * 2 + 50
                    s_vol = float(max(0, min(100, raw)))

            # Momentum : rang percentile du rendement actuel sur `mom_window`
            if len(ind_closes) >= mom_window + 1:
                # Série de rendements glissants sur `mom_window`
                rolling = []
                for i in range(mom_window, len(ind_closes)):
                    ref = ind_closes[i - mom_window]
                    if ref:
                        rolling.append((ind_closes[i] - ref) / ref)
                if rolling:
                    mom_val = rolling[-1]
                    s_mom = _percentile_rank(rolling, mom_val)
    except Indice.DoesNotExist:
        pass

    # ---- 4. S_disp : dispersion cross-sectionnelle journalière, rang percentile inversé
    # Pour chaque jour, écart-type des variations journalières des actions.
    s_disp = None
    disp_today = None
    # Charger lookback derniers jours d'historique
    cutoff = last_date - timedelta(days=int(disp_lookback * 1.6) + 30)
    all_rows = list(
        HistoriqueAction.objects.filter(date__gte=cutoff)
        .order_by("date")
        .values("action_id", "date", "variation_pct")
    )
    by_date = defaultdict(list)
    for r in all_rows:
        if r["variation_pct"] is not None:
            by_date[r["date"]].append(r["variation_pct"])

    dispersions = []
    for d in sorted(by_date.keys()):
        vals = by_date[d]
        if len(vals) >= 5:
            dispersions.append((d, float(np.std(vals, ddof=1))))

    if dispersions:
        last_disp = dispersions[-1][1]
        disp_today = last_disp
        # Rang inversé : P(disp_historique >= disp_actuelle) * 100
        # ⇨ faible dispersion → score élevé (Greed)
        hist_vals = np.array([v for _, v in dispersions[-disp_lookback:]], dtype=float)
        if len(hist_vals) >= 10:
            s_disp = float(np.mean(hist_vals >= last_disp) * 100)

    # ---- 5. Score global pondéré
    components = {
        "breadth": (s_breadth, w1),
        "vol": (s_vol, w2),
        "mom": (s_mom, w3),
        "disp": (s_disp, w4),
    }
    num = 0.0
    den = 0.0
    for v, w in components.values():
        if v is not None and w > 0:
            num += v * w
            den += w
    score = round(num / den, 1) if den > 0 else None

    # Label & couleur
    if score is None:
        label, color = "Indisponible", "#6b7280"
    elif score >= 75:
        label, color = "Greed Extrême", "#15803d"
    elif score >= 55:
        label, color = "Greed", "#22c55e"
    elif score >= 45:
        label, color = "Neutre", "#f59e0b"
    elif score >= 25:
        label, color = "Fear", "#f97316"
    else:
        label, color = "Fear Extrême", "#dc2626"

    return {
        "last_date": last_date.strftime("%Y-%m-%d"),
        "ticker": ticker,
        "score": score,
        "label": label,
        "color": color,
        "components": {
            "breadth": {
                "value": round(s_breadth, 1) if s_breadth is not None else None,
                "weight": w1,
                "details": {
                    "window": breadth_window,
                    "nb_up": nb_up,
                    "nb_total": nb_total,
                },
            },
            "vol": {
                "value": round(s_vol, 1) if s_vol is not None else None,
                "weight": w2,
                "details": {
                    "vol_short": round(vol_short_val * 100, 2) if vol_short_val is not None else None,
                    "vol_long": round(vol_long_val * 100, 2) if vol_long_val is not None else None,
                    "ratio": (round(vol_short_val / vol_long_val, 3)
                              if vol_short_val is not None and vol_long_val else None),
                    "short_n": vol_short_n,
                    "long_n": vol_long_n,
                },
            },
            "mom": {
                "value": round(s_mom, 1) if s_mom is not None else None,
                "weight": w3,
                "details": {
                    "window": mom_window,
                    "current_return_pct": round(mom_val * 100, 2) if mom_val is not None else None,
                },
            },
            "disp": {
                "value": round(s_disp, 1) if s_disp is not None else None,
                "weight": w4,
                "details": {
                    "lookback": disp_lookback,
                    "current": round(disp_today, 3) if disp_today is not None else None,
                    "nb_days": len(dispersions),
                },
            },
        },
    }


@csrf_exempt
def api_market_fear_greed(request):
    """API JSON pour l'indicateur Fear & Greed.

    Score F&G = (w1*S_breadth + w2*S_vol + w3*S_mom + w4*S_disp) / (w1+w2+w3+w4)
    """
    def fnum(name, default, cast=int, minv=1):
        try:
            v = cast(request.GET.get(name, default))
            return max(minv, v) if cast is int else v
        except (ValueError, TypeError):
            return default

    w1 = fnum("w1", 1.0, float, 0.0)
    w2 = fnum("w2", 1.0, float, 0.0)
    w3 = fnum("w3", 1.0, float, 0.0)
    w4 = fnum("w4", 1.0, float, 0.0)
    if (w1 + w2 + w3 + w4) <= 0:
        return JsonResponse({"error": "Au moins un poids doit être > 0"}, status=400)

    result = _compute_fear_greed(
        w1=w1, w2=w2, w3=w3, w4=w4,
        breadth_window=fnum("breadth_window", 20),
        vol_short_n=fnum("vol_short", 20),
        vol_long_n=fnum("vol_long", 90),
        mom_window=fnum("mom_window", 20),
        disp_lookback=fnum("disp_lookback", 252),
        ticker=request.GET.get("ticker", "BRVMC"),
    )
    if result is None:
        return JsonResponse({"error": "Aucune donnée disponible"}, status=404)
    return JsonResponse(result)


def api_market_summary(request):
    """Résumé du marché."""
    summary = {}
    for ticker in ["BRVMC", "BRVM30"]:
        try:
            indice = Indice.objects.get(ticker=ticker)
            last = HistoriqueIndice.objects.filter(indice=indice).order_by("-date").first()
            if last:
                summary[ticker] = {
                    "cloture": last.cloture,
                    "variation": last.variation_pct,
                    "date": last.date.isoformat(),
                }
        except Indice.DoesNotExist:
            pass
    return JsonResponse(summary)


def api_performers(request):
    """Top 5 et Flop 5 performers selon la période sélectionnée."""
    period = request.GET.get("period", "1d")
    
    # Mapping période vers nombre de jours de trading
    period_days = {
        "1d": 1,
        "1w": 5,
        "1m": 22,
        "3m": 66,
        "6m": 132,
        "1y": 252,
        "ytd": None,  # Calculé dynamiquement
    }
    
    actions = Action.objects.all()
    performances = []
    
    for action in actions:
        historiques = list(
            HistoriqueAction.objects.filter(action=action)
            .order_by("-date")
            .values("date", "cloture")
        )
        
        if not historiques or historiques[0]["cloture"] is None:
            continue
        
        current_close = historiques[0]["cloture"]
        current_date = historiques[0]["date"]
        
        # Déterminer le cours de référence selon la période
        days = period_days.get(period, 1)

        # Sparkline : 30 derniers cours (du plus ancien au plus récent)
        spark_src = historiques[:30]
        sparkline = [h["cloture"] for h in reversed(spark_src) if h["cloture"] is not None]

        secteur = (action.secteur or "Non classé")
        nb_act = action.nombre_actions or 0
        cap = (current_close or 0) * nb_act if (current_close and nb_act) else 0

        if period == "ytd":
            # Trouver le premier cours de l'année en cours
            current_year = datetime.now().year
            year_data = [h for h in historiques if h["date"].year == current_year and h["cloture"]]
            if year_data:
                ref_close = year_data[-1]["cloture"]  # Le plus ancien de l'année
            else:
                continue
        elif period == "1d":
            # Variation journalière - prendre la variation du dernier jour
            last_hist = HistoriqueAction.objects.filter(action=action).order_by("-date").first()
            if last_hist and last_hist.variation_pct is not None:
                performances.append({
                    "ticker": action.ticker,
                    "secteur": secteur,
                    "cloture": current_close,
                    "variation": last_hist.variation_pct,
                    "sparkline": sparkline,
                    "cap": cap,
                })
            continue
        else:
            # Trouver le cours il y a N jours
            if len(historiques) > days:
                ref_data = historiques[days]
                ref_close = ref_data["cloture"]
            else:
                # Pas assez de données, prendre le plus ancien
                ref_close = historiques[-1]["cloture"] if historiques[-1]["cloture"] else None
        
        if ref_close and ref_close != 0:
            variation = round((current_close - ref_close) / ref_close * 100, 2)
            performances.append({
                "ticker": action.ticker,
                "secteur": secteur,
                "cloture": current_close,
                "variation": variation,
                "sparkline": sparkline,
                "cap": cap,
            })
    
    # Tri pour top/flop
    performances.sort(key=lambda x: x["variation"] or 0, reverse=True)
    top5 = performances[:5]
    flop5 = sorted(performances, key=lambda x: x["variation"] or 0)[:5]

    # Largeur de marché sur la période
    breadth = {"up": 0, "down": 0, "flat": 0}
    for p in performances:
        v = p.get("variation")
        if v is None:
            continue
        if v > 0:
            breadth["up"] += 1
        elif v < 0:
            breadth["down"] += 1
        else:
            breadth["flat"] += 1

    return JsonResponse({
        "top5": top5,
        "flop5": flop5,
        "all": performances,
        "breadth": breadth,
    })


def api_correlation_matrix(request):
    """Calcule et retourne la matrice de corrélation pour les tickers sélectionnés."""
    tickers = request.GET.getlist("tickers")
    if not tickers:
        tickers = list(Action.objects.values_list("ticker", flat=True)[:10])

    returns_dict = {}
    for ticker in tickers:
        try:
            action = Action.objects.get(ticker=ticker)
            closes = list(
                HistoriqueAction.objects.filter(action=action)
                .order_by("date")
                .values_list("cloture", flat=True)
            )
            closes = [c for c in closes if c is not None]
            if len(closes) > 30:
                returns_dict[ticker] = compute_returns(closes)
        except Action.DoesNotExist:
            pass

    valid_tickers = [t for t in tickers if t in returns_dict]
    matrix = []
    for t1 in valid_tickers:
        row = []
        for t2 in valid_tickers:
            r1, r2 = returns_dict[t1], returns_dict[t2]
            min_len = min(len(r1), len(r2))
            if min_len > 30:
                corr = np.corrcoef(r1[-min_len:], r2[-min_len:])[0, 1]
                row.append(round(corr, 3))
            else:
                row.append(None)
        matrix.append(row)

    return JsonResponse({"tickers": valid_tickers, "matrix": matrix})


def api_performances_table(request):
    """Données du tableau de performances en JSON."""
    # Réutilise la logique de la vue performances
    actions = Action.objects.all()
    data = []
    for action in actions:
        hist = list(
            HistoriqueAction.objects.filter(action=action)
            .order_by("date")
            .values_list("cloture", flat=True)
        )
        hist = [c for c in hist if c is not None]
        if not hist:
            continue
        row = {"ticker": action.ticker, "dernier_cours": hist[-1]}
        for name, days in [("1j", 1), ("1s", 5), ("1m", 22), ("3m", 66), ("6m", 132), ("1a", 252)]:
            if len(hist) > days and hist[-(days + 1)] and hist[-(days + 1)] != 0:
                row[f"var_{name}"] = round((hist[-1] - hist[-(days + 1)]) / hist[-(days + 1)] * 100, 2)
            else:
                row[f"var_{name}"] = None
        data.append(row)
    return JsonResponse({"data": data})


def api_export_csv(request):
    """Export CSV des performances."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="performances_brvm.csv"'
    response.write('\ufeff')

    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Ticker", "Dernier Cours", "Var 1J", "Var 1S", "Var 1M", "Var 3M", "Var 6M", "Var 1A", "PER", "Dividende"])

    for action in Action.objects.all():
        hist = list(
            HistoriqueAction.objects.filter(action=action)
            .order_by("date")
            .values_list("cloture", flat=True)
        )
        hist = [c for c in hist if c is not None]
        if not hist:
            continue

        row = [action.ticker, hist[-1]]
        for days in [1, 5, 22, 66, 132, 252]:
            if len(hist) > days and hist[-(days + 1)] and hist[-(days + 1)] != 0:
                row.append(round((hist[-1] - hist[-(days + 1)]) / hist[-(days + 1)] * 100, 2))
            else:
                row.append("")
        row.extend([action.per or "", action.dividende or ""])
        writer.writerow(row)

    return response


def api_run_scraper(request):
    """Lance un scraper en arrière-plan via le service partagé."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)

    scraper_type = request.POST.get("type", "actions")

    from dashboard.services import run_scraping

    def _run(scraper_type):
        run_scraping(scraper_type)

    thread = threading.Thread(target=_run, args=(scraper_type,))
    thread.daemon = True
    thread.start()

    last_log = ScrapingLog.objects.filter(type_scraping=scraper_type).first()
    log_id = last_log.id if last_log else None

    return JsonResponse({"status": "started", "log_id": log_id})


def api_scraping_status(request):
    """Statut du dernier scraping."""
    log_id = request.GET.get("log_id")
    if log_id:
        try:
            log = ScrapingLog.objects.get(id=log_id)
            return JsonResponse({
                "statut": log.statut,
                "message": log.message[:500],
                "date_fin": log.date_fin.isoformat() if log.date_fin else None,
            })
        except ScrapingLog.DoesNotExist:
            return JsonResponse({"error": "Log introuvable"}, status=404)

    # Dernier log
    log = ScrapingLog.objects.first()
    if log:
        return JsonResponse({
            "statut": log.statut,
            "type": log.type_scraping,
            "date_debut": log.date_debut.isoformat(),
            "date_fin": log.date_fin.isoformat() if log.date_fin else None,
        })
    return JsonResponse({"statut": "aucun"})


def api_news_search(request):
    """Recherche dans les actualités."""
    q = request.GET.get("q", "")
    page = int(request.GET.get("page", 1))
    per_page = 20

    qs = News.objects.all()
    if q:
        qs = qs.filter(Q(titre__icontains=q) | Q(contenu__icontains=q))

    total = qs.count()
    news = list(qs[(page - 1) * per_page: page * per_page].values(
        "id_source", "titre", "date_publication", "auteur", "categorie", "url"
    ))

    return JsonResponse({
        "total": total,
        "page": page,
        "news": [
            {**n, "date_publication": n["date_publication"].isoformat() if n["date_publication"] else None}
            for n in news
        ]
    })


# ============================================================
# Export Factsheet
# ============================================================

def export_factsheet(request):
    """Page d'export factsheet PDF avec aperçu et génération de commentaires IA."""
    ctx = get_context_base(request)
    actions = Action.objects.all()
    api_key_configured = bool(ApiConfig.get("ANTHROPIC_API_KEY"))
    recent_comments = CommentHistory.objects.select_related("action")[:10]

    ctx.update({
        "actions": actions,
        "api_key_configured": api_key_configured,
        "recent_comments": recent_comments,
    })
    return render(request, "dashboard/export_factsheet.html", ctx)


def _compute_calendar_perf(closes_with_dates):
    """Calcule les performances calendaires (WTD, MTD, QTD, STD, YTD)."""
    if not closes_with_dates:
        return {}

    today = closes_with_dates[-1][0]
    last_price = closes_with_dates[-1][1]
    prices_by_date = {d: p for d, p in closes_with_dates}

    results = {}
    import calendar

    # WTD - depuis lundi de la semaine en cours
    week_start = today - timedelta(days=today.weekday())
    # MTD - depuis le 1er du mois
    month_start = today.replace(day=1)
    # QTD - depuis le début du trimestre
    quarter_month = ((today.month - 1) // 3) * 3 + 1
    quarter_start = today.replace(month=quarter_month, day=1)
    # STD - depuis le début du semestre
    semester_month = 1 if today.month <= 6 else 7
    semester_start = today.replace(month=semester_month, day=1)
    # YTD - depuis le 1er janvier
    year_start = today.replace(month=1, day=1)

    for label, start_date in [("wtd", week_start), ("mtd", month_start),
                               ("qtd", quarter_start), ("std", semester_start),
                               ("ytd", year_start)]:
        # Trouver le prix le plus proche de start_date
        ref_price = None
        for d, p in closes_with_dates:
            if d >= start_date:
                ref_price = p
                break
        if ref_price and ref_price != 0:
            results[label] = round((last_price - ref_price) / ref_price * 100, 2)

    return results


def _compute_rolling_perf(closes):
    """Calcule les performances glissantes (1w, 1m, 3m, 6m, 1y, 3y)."""
    if not closes:
        return {}
    results = {}
    last = closes[-1]
    for label, days in [("perf_1w", 5), ("perf_1m", 22), ("perf_3m", 66),
                         ("perf_6m", 132), ("perf_1y", 252), ("perf_3y", 756)]:
        if len(closes) > days and closes[-(days + 1)] and closes[-(days + 1)] != 0:
            results[label] = round((last - closes[-(days + 1)]) / closes[-(days + 1)] * 100, 2)
    return results


def _compute_beta_periods(stock_closes, index_closes):
    """Calcule beta, corrélation et R² pour différentes périodes."""
    results = {}
    for label, days in [("3m", 66), ("6m", 132), ("1y", 252), ("3y", 756), ("all", None)]:
        n = days if days else min(len(stock_closes), len(index_closes))
        if n < 30 or len(stock_closes) < n or len(index_closes) < n:
            continue
        sc = stock_closes[-n:]
        ic = index_closes[-n:]
        sr = compute_returns(sc)
        ir = compute_returns(ic)
        min_len = min(len(sr), len(ir))
        if min_len < 20:
            continue
        sr, ir = sr[-min_len:], ir[-min_len:]
        cov = np.cov(sr, ir)
        if cov[1, 1] == 0:
            continue
        beta_val = cov[0, 1] / cov[1, 1]
        corr_val = np.corrcoef(sr, ir)[0, 1]
        results[label] = {
            "beta": round(beta_val, 3),
            "correlation": round(corr_val, 3),
            "r_squared": round(corr_val ** 2, 3),
        }
    return results


@csrf_exempt
def api_factsheet_data(request):
    """Données complètes pour un factsheet (JSON)."""
    ticker = request.GET.get("ticker", "")
    if not ticker:
        return JsonResponse({"error": "Paramètre ticker requis"}, status=400)

    try:
        action = Action.objects.get(ticker=ticker)
    except Action.DoesNotExist:
        return JsonResponse({"error": f"Action {ticker} introuvable"}, status=404)

    # Historique de l'action
    hist = list(
        HistoriqueAction.objects.filter(action=action)
        .order_by("date")
        .values("date", "cloture", "volume_titres")
    )
    closes_with_dates = [(h["date"], h["cloture"]) for h in hist if h["cloture"] is not None]
    closes = [c for _, c in closes_with_dates]

    if not closes:
        return JsonResponse({"error": "Aucune donnée historique"}, status=404)

    # Indice BRVMC pour comparaison
    idx_closes = []
    idx_closes_with_dates = []
    try:
        indice_brvmc = Indice.objects.get(ticker="BRVMC")
        idx_hist = list(
            HistoriqueIndice.objects.filter(indice=indice_brvmc)
            .order_by("date")
            .values("date", "cloture")
        )
        idx_closes_with_dates = [(h["date"], h["cloture"]) for h in idx_hist if h["cloture"] is not None]
        idx_closes = [c for _, c in idx_closes_with_dates]
    except Indice.DoesNotExist:
        pass

    # Indicateurs techniques
    rsi = compute_rsi(closes, 14)
    macd_line, macd_signal, macd_hist = compute_macd(closes)
    sma20 = compute_sma(closes, 20)
    sma50 = compute_sma(closes, 50)
    boll_mid, boll_up, boll_low = compute_bollinger(closes, 20, 2)

    # Performances calendaires
    cal_perf_action = _compute_calendar_perf(closes_with_dates)
    cal_perf_index = _compute_calendar_perf(idx_closes_with_dates) if idx_closes_with_dates else {}

    # Performances glissantes
    roll_perf_action = _compute_rolling_perf(closes)
    roll_perf_index = _compute_rolling_perf(idx_closes) if idx_closes else {}

    # Beta & corrélation par période
    beta_by_period = _compute_beta_periods(closes, idx_closes) if idx_closes else {}

    # Chart data - 1 an, base 100
    chart_data = {"dates": [], "action": [], "index": []}
    n_chart = min(252, len(closes))
    if n_chart > 0:
        action_slice = closes[-n_chart:]
        dates_slice = [d.strftime("%d/%m/%y") for d, _ in closes_with_dates[-n_chart:]]
        base_a = action_slice[0] if action_slice[0] != 0 else 1
        chart_data["dates"] = dates_slice
        chart_data["action"] = [round(p / base_a * 100, 2) for p in action_slice]

        if idx_closes:
            n_idx = min(n_chart, len(idx_closes))
            idx_slice = idx_closes[-n_idx:]
            base_i = idx_slice[0] if idx_slice[0] != 0 else 1
            chart_data["index"] = [round(p / base_i * 100, 2) for p in idx_slice]

    # Volatilité annualisée
    returns = compute_returns(closes)
    volatilite = round(np.std(returns) * np.sqrt(252) * 100, 2) if len(returns) > 30 else None

    return JsonResponse({
        "metadata": {
            "ticker": action.ticker,
            "nom": action.nom or action.ticker,
            "pays": action.pays,
            "isin": action.isin,
            "description": action.description,
            "nombre_actions": action.nombre_actions,
            "flottant_pct": action.flottant_pct,
            "chiffre_affaires": action.chiffre_affaires,
            "resultat_net": action.resultat_net,
            "bnpa": action.bnpa,
            "per": action.per,
            "dividende": action.dividende,
        },
        "current_price": closes[-1],
        "last_date": closes_with_dates[-1][0].strftime("%d/%m/%Y"),
        "rsi": rsi,
        "macd": macd_line,
        "macd_signal": macd_signal,
        "macd_histogram": macd_hist,
        "sma_20": sma20,
        "sma_50": sma50,
        "bollinger": {"mid": boll_mid, "up": boll_up, "low": boll_low},
        "volatilite_ann": volatilite,
        "calendar_perf_action": cal_perf_action,
        "calendar_perf_index": cal_perf_index,
        "rolling_perf_action": roll_perf_action,
        "rolling_perf_index": roll_perf_index,
        "beta_by_period": beta_by_period,
        "chart_data": chart_data,
    })


@csrf_exempt
def api_factsheet_generate_comments(request):
    """Génère des commentaires IA via Claude pour un factsheet."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)

    # Récupérer la clé API
    api_key = data.get("api_key") or ApiConfig.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JsonResponse({
            "error": "Clé API Claude requise. Configurez-la dans les paramètres ou fournissez-la dans la requête."
        }, status=400)

    factsheet_data = data.get("factsheet_data", {})
    comment_type = data.get("comment_type", "both")

    # Construire le contexte
    meta = factsheet_data.get("metadata", factsheet_data)
    ticker = meta.get("ticker", meta.get("symbol", "N/A"))
    nom = meta.get("nom", meta.get("name", ticker))

    context_parts = [
        f"Action : {ticker} - {nom}",
        f"Secteur : {meta.get('secteur', meta.get('sector', 'N/A'))}",
        f"Pays : {meta.get('pays', 'N/A')}",
        f"Prix actuel : {factsheet_data.get('current_price', 'N/A')} FCFA",
        f"Dernière date : {factsheet_data.get('last_date', 'N/A')}",
        f"PER : {meta.get('per', 'N/A')}",
        f"Dividende : {meta.get('dividende', 'N/A')} FCFA",
        f"BNPA : {meta.get('bnpa', 'N/A')} FCFA",
    ]

    if factsheet_data.get("rsi") is not None:
        context_parts.append(f"RSI (14) : {factsheet_data['rsi']:.1f}")
    if factsheet_data.get("macd") is not None:
        context_parts.append(f"MACD : {factsheet_data['macd']:.2f}")
    if factsheet_data.get("macd_signal") is not None:
        context_parts.append(f"Signal MACD : {factsheet_data['macd_signal']:.2f}")
    if factsheet_data.get("volatilite_ann") is not None:
        context_parts.append(f"Volatilité annualisée : {factsheet_data['volatilite_ann']:.1f}%")

    cal_perf = factsheet_data.get("calendar_perf_action", {})
    if cal_perf:
        context_parts.append("\nPerformances calendaires :")
        for k, v in cal_perf.items():
            context_parts.append(f"  {k.upper()} : {v:+.2f}%")

    roll_perf = factsheet_data.get("rolling_perf_action", {})
    if roll_perf:
        context_parts.append("\nPerformances glissantes :")
        labels = {"perf_1w": "1 sem.", "perf_1m": "1 mois", "perf_3m": "3 mois",
                  "perf_6m": "6 mois", "perf_1y": "1 an", "perf_3y": "3 ans"}
        for k, v in roll_perf.items():
            context_parts.append(f"  {labels.get(k, k)} : {v:+.2f}%")

    beta_data = factsheet_data.get("beta_by_period", {})
    if beta_data:
        context_parts.append("\nBeta & Corrélation vs BRVM Composite :")
        for period, vals in beta_data.items():
            context_parts.append(
                f"  {period} : Beta={vals.get('beta', 'N/A')}, "
                f"Corrél.={vals.get('correlation', 'N/A')}"
            )

    context_text = "\n".join(context_parts)

    # Prompts selon le type
    prompts = {}
    if comment_type in ("analysis", "both"):
        prompts["analysis"] = (
            f"Tu es un analyste financier senior spécialisé sur la BRVM (Bourse Régionale des Valeurs Mobilières). "
            f"Voici les données d'un titre coté :\n\n{context_text}\n\n"
            f"Rédige une analyse technique et fondamentale concise (150-200 mots) de ce titre. "
            f"Mentionne les points clés : tendance, momentum (RSI, MACD), valorisation (PER), "
            f"performance relative au marché (beta, corrélation). "
            f"Sois factuel, professionnel, en français. Ne fais pas de recommandation d'achat/vente dans cette section."
        )

    if comment_type in ("recommendation", "both"):
        prompts["recommendation"] = (
            f"Tu es un analyste financier senior spécialisé sur la BRVM. "
            f"Voici les données d'un titre coté :\n\n{context_text}\n\n"
            f"Donne une recommandation d'investissement concise (100-150 mots). "
            f"Commence OBLIGATOIREMENT par l'une de ces trois mentions en gras : "
            f"**ACHETER**, **NEUTRE**, ou **VENDRE**. "
            f"Justifie avec les données fournies. "
            f"Mentionne les risques principaux et les catalyseurs potentiels. "
            f"Sois factuel et professionnel, en français."
        )

    # Appel à l'API Claude
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return JsonResponse({"error": "Le module 'anthropic' n'est pas installé. pip install anthropic"}, status=500)
    except Exception as e:
        return JsonResponse({"error": f"Erreur d'initialisation Anthropic : {str(e)}"}, status=500)

    result = {}
    for key, prompt in prompts.items():
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1500,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text.strip()
            if key == "analysis":
                result["analysis_comment"] = text
            else:
                result["recommendation_comment"] = text
        except Exception as e:
            result[f"{key}_error"] = str(e)

    result["disclaimer"] = (
        "Ce document est fourni à titre informatif uniquement et ne constitue pas un conseil en investissement. "
        "Les performances passées ne préjugent pas des performances futures. CGF Bourse décline toute responsabilité "
        "quant aux décisions prises sur la base de ce document."
    )

    return JsonResponse(result)


@csrf_exempt
def api_factsheet_generate_pdf(request):
    """Génère un PDF factsheet professionnel."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)

    factsheets = data.get("factsheets", [])
    if not factsheets:
        return JsonResponse({"error": "Aucun factsheet fourni"}, status=400)

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io
        import tempfile
        import zipfile
    except ImportError as e:
        return JsonResponse({"error": f"Module manquant : {e}. pip install reportlab matplotlib"}, status=500)

    def generate_single_pdf(fs):
        """Génère un PDF pour un seul factsheet."""
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer, pagesize=A4,
            leftMargin=15 * mm, rightMargin=15 * mm,
            topMargin=12 * mm, bottomMargin=12 * mm,
        )

        # Couleurs
        PRIMARY = HexColor("#4f8cff")
        DARK = HexColor("#0f1117")
        GREEN = HexColor("#00d97e")
        RED = HexColor("#e63757")
        GREY = HexColor("#6b7280")
        LIGHT_BG = HexColor("#f8fafc")
        WHITE = HexColor("#ffffff")

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "FSTitle", parent=styles["Title"],
            fontSize=16, textColor=PRIMARY, spaceAfter=2 * mm,
            fontName="Helvetica-Bold",
        )
        subtitle_style = ParagraphStyle(
            "FSSubtitle", parent=styles["Normal"],
            fontSize=9, textColor=GREY, spaceAfter=4 * mm,
        )
        section_style = ParagraphStyle(
            "FSSection", parent=styles["Heading2"],
            fontSize=11, textColor=PRIMARY, spaceBefore=5 * mm,
            spaceAfter=3 * mm, fontName="Helvetica-Bold",
            borderWidth=0, borderPadding=0,
        )
        body_style = ParagraphStyle(
            "FSBody", parent=styles["Normal"],
            fontSize=8.5, leading=12, textColor=HexColor("#1f2937"),
            alignment=TA_JUSTIFY,
        )
        small_style = ParagraphStyle(
            "FSSmall", parent=styles["Normal"],
            fontSize=7.5, textColor=GREY, leading=10,
        )
        disclaimer_style = ParagraphStyle(
            "FSDisclaimer", parent=styles["Normal"],
            fontSize=7, textColor=GREY, leading=9, alignment=TA_JUSTIFY,
            fontName="Helvetica-Oblique",
        )

        elements = []
        meta = fs.get("metadata", fs)
        ticker = meta.get("ticker", meta.get("symbol", "N/A"))
        nom = meta.get("nom", meta.get("name", ticker))

        # HEADER
        elements.append(Paragraph(f"{ticker} — {nom}", title_style))
        pays = meta.get("pays", "")
        secteur = meta.get("secteur", meta.get("sector", ""))
        date_export = datetime.now().strftime("%d/%m/%Y")
        sub_parts = []
        if pays:
            sub_parts.append(f"Pays : {pays.upper()}")
        if secteur:
            sub_parts.append(f"Secteur : {secteur}")
        sub_parts.append(f"Date d'export : {date_export}")
        elements.append(Paragraph(" | ".join(sub_parts), subtitle_style))

        # Description
        desc = meta.get("description", "")
        if desc:
            elements.append(Paragraph("Présentation", section_style))
            elements.append(Paragraph(desc[:500], body_style))
            elements.append(Spacer(1, 3 * mm))

        # MÉTRIQUES CLÉS
        elements.append(Paragraph("Métriques clés", section_style))
        price = fs.get("current_price", "-")
        price_str = f"{price:,.0f} FCFA".replace(",", " ") if isinstance(price, (int, float)) else str(price)

        def fmt(val, suffix="", dec=2):
            if val is None:
                return "-"
            try:
                return f"{float(val):,.{dec}f}{suffix}".replace(",", " ")
            except (ValueError, TypeError):
                return str(val)

        metrics_data = [
            ["Prix actuel", price_str, "PER", fmt(meta.get("per"), "", 1)],
            ["RSI (14)", fmt(fs.get("rsi"), "", 1), "Dividende", fmt(meta.get("dividende"), " FCFA")],
            ["MACD", fmt(fs.get("macd")), "BNPA", fmt(meta.get("bnpa"), " FCFA")],
            ["Volatilité ann.", fmt(fs.get("volatilite_ann"), "%", 1), "Flottant", fmt(meta.get("flottant_pct"), "%", 1)],
        ]

        col_w = [35 * mm, 40 * mm, 35 * mm, 40 * mm]
        t = Table(metrics_data, colWidths=col_w)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("TEXTCOLOR", (0, 0), (0, -1), GREY),
            ("TEXTCOLOR", (2, 0), (2, -1), GREY),
            ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
            ("FONTNAME", (3, 0), (3, -1), "Helvetica-Bold"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -2), 0.5, HexColor("#e5e7eb")),
            ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
            ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 3 * mm))

        # GRAPHIQUE DE PERFORMANCE
        chart_data = fs.get("chart_data", {})
        if chart_data.get("dates") and chart_data.get("action"):
            elements.append(Paragraph("Performance comparée (Base 100, 1 an)", section_style))

            fig, ax = plt.subplots(figsize=(7, 2.5))
            fig.patch.set_facecolor("#f8fafc")
            ax.set_facecolor("#f8fafc")

            dates = chart_data["dates"]
            ax.plot(range(len(dates)), chart_data["action"],
                    color="#4f8cff", linewidth=1.5, label=ticker)
            if chart_data.get("index"):
                idx_data = chart_data["index"]
                offset = len(dates) - len(idx_data)
                ax.plot(range(offset, offset + len(idx_data)), idx_data,
                        color="#e63757", linewidth=1.2, alpha=0.7, label="BRVM Composite")

            ax.axhline(y=100, color="#9ca3af", linewidth=0.5, linestyle="--", alpha=0.5)
            ax.legend(fontsize=7, loc="upper left")
            ax.tick_params(labelsize=6)

            # X-axis labels
            step = max(1, len(dates) // 8)
            ax.set_xticks(range(0, len(dates), step))
            ax.set_xticklabels([dates[i] for i in range(0, len(dates), step)], rotation=30)
            ax.grid(True, alpha=0.2)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            plt.tight_layout()

            img_buffer = io.BytesIO()
            fig.savefig(img_buffer, format="png", dpi=150, bbox_inches="tight")
            plt.close(fig)
            img_buffer.seek(0)

            img = Image(img_buffer, width=170 * mm, height=60 * mm)
            elements.append(img)
            elements.append(Spacer(1, 3 * mm))

        # TABLEAUX DE PERFORMANCE
        elements.append(Paragraph("Performances", section_style))

        def perf_cell(val):
            if val is None:
                return "-"
            return f"{val:+.2f}%"

        def color_perf(val):
            if val is None:
                return GREY
            return GREEN if val >= 0 else RED

        # Calendaire
        cal_a = fs.get("calendar_perf_action", {})
        cal_i = fs.get("calendar_perf_index", {})
        cal_rows = [["Période", ticker, "BRVMC"]]
        for code, label in [("wtd", "WTD"), ("mtd", "MTD"), ("qtd", "QTD"), ("std", "STD"), ("ytd", "YTD")]:
            cal_rows.append([label, perf_cell(cal_a.get(code)), perf_cell(cal_i.get(code))])

        # Glissante
        roll_a = fs.get("rolling_perf_action", {})
        roll_i = fs.get("rolling_perf_index", {})
        roll_rows = [["Période", ticker, "BRVMC"]]
        for code, label in [("perf_1w", "1 sem."), ("perf_1m", "1 mois"), ("perf_3m", "3 mois"),
                             ("perf_6m", "6 mois"), ("perf_1y", "1 an"), ("perf_3y", "3 ans")]:
            roll_rows.append([label, perf_cell(roll_a.get(code)), perf_cell(roll_i.get(code))])

        half_w = 85 * mm
        col_perf = [25 * mm, 30 * mm, 30 * mm]

        def make_perf_table(rows):
            t = Table(rows, colWidths=col_perf)
            style_cmds = [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -2), 0.3, HexColor("#e5e7eb")),
            ]
            # Colorize perf cells
            for r_idx in range(1, len(rows)):
                for c_idx in [1, 2]:
                    val_str = rows[r_idx][c_idx]
                    if val_str != "-":
                        try:
                            v = float(val_str.replace("%", "").replace("+", ""))
                            style_cmds.append(("TEXTCOLOR", (c_idx, r_idx), (c_idx, r_idx), GREEN if v >= 0 else RED))
                        except ValueError:
                            pass
            t.setStyle(TableStyle(style_cmds))
            return t

        # Layout côte à côte
        combined = Table(
            [[make_perf_table(cal_rows), "", make_perf_table(roll_rows)]],
            colWidths=[half_w, 3 * mm, half_w],
        )
        combined.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        elements.append(combined)
        elements.append(Spacer(1, 3 * mm))

        # BETA & CORRÉLATION
        beta_data = fs.get("beta_by_period", {})
        if beta_data:
            elements.append(Paragraph("Beta & Corrélation vs BRVM Composite", section_style))
            beta_rows = [["Période", "Beta", "Corrélation", "R²"]]
            for code, label in [("3m", "3 mois"), ("6m", "6 mois"), ("1y", "1 an"),
                                 ("3y", "3 ans"), ("all", "Origine")]:
                vals = beta_data.get(code, {})
                beta_rows.append([
                    label,
                    str(vals.get("beta", "-")),
                    str(vals.get("correlation", "-")),
                    str(vals.get("r_squared", "-")),
                ])
            bt = Table(beta_rows, colWidths=[30 * mm, 30 * mm, 30 * mm, 30 * mm])
            bt.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("ALIGN", (1, 0), (-1, -1), "CENTER"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("LINEBELOW", (0, 0), (-1, -2), 0.3, HexColor("#e5e7eb")),
            ]))
            elements.append(bt)
            elements.append(Spacer(1, 3 * mm))

        # COMMENTAIRES IA
        analysis = fs.get("analysis_comment", "")
        recommendation = fs.get("recommendation_comment", "")
        if analysis or recommendation:
            elements.append(Paragraph("Analyse & Recommandation", section_style))
            if analysis:
                elements.append(Paragraph("<b>Analyse :</b>", small_style))
                elements.append(Paragraph(analysis.replace("\n", "<br/>"), body_style))
                elements.append(Spacer(1, 2 * mm))
            if recommendation:
                elements.append(Paragraph("<b>Recommandation :</b>", small_style))
                elements.append(Paragraph(recommendation.replace("\n", "<br/>"), body_style))
                elements.append(Spacer(1, 3 * mm))

        # DISCLAIMER
        disclaimer = fs.get("disclaimer", "")
        if disclaimer:
            elements.append(Spacer(1, 3 * mm))
            elements.append(Paragraph(
                f"<b>Avertissement :</b> {disclaimer}", disclaimer_style
            ))

        # Footer
        elements.append(Spacer(1, 3 * mm))
        elements.append(Paragraph(
            f"LOOK UP BRVM — Généré le {date_export} | CGF Bourse",
            ParagraphStyle("Footer", parent=styles["Normal"], fontSize=7,
                           textColor=GREY, alignment=TA_CENTER)
        ))

        doc.build(elements)
        buffer.seek(0)
        return buffer

    # Génération
    if len(factsheets) == 1:
        pdf_buffer = generate_single_pdf(factsheets[0])
        ticker = factsheets[0].get("metadata", factsheets[0]).get("ticker", factsheets[0].get("symbol", "factsheet"))
        response = HttpResponse(pdf_buffer.getvalue(), content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="Factsheet_{ticker}_{datetime.now().strftime("%Y%m%d")}.pdf"'
        return response
    else:
        # Multiple → ZIP
        import zipfile
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fs in factsheets:
                pdf_buffer = generate_single_pdf(fs)
                ticker = fs.get("metadata", fs).get("ticker", fs.get("symbol", "factsheet"))
                zf.writestr(f"Factsheet_{ticker}.pdf", pdf_buffer.getvalue())
        zip_buffer.seek(0)
        response = HttpResponse(zip_buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="Factsheets_BRVM_{datetime.now().strftime("%Y%m%d")}.zip"'
        return response


# ============================================================
# Reporting Marché (1 page A4)
# ============================================================

def _get_index_snapshot(ticker):
    """Renvoie le dernier point + variation jour d'un indice."""
    try:
        ind = Indice.objects.get(ticker=ticker)
    except Indice.DoesNotExist:
        return None
    qs = list(
        HistoriqueIndice.objects.filter(indice=ind)
        .order_by("-date").values("date", "cloture", "variation_pct")[:2]
    )
    if not qs:
        return None
    last = qs[0]
    return {
        "ticker": ticker,
        "nom": ind.nom or ticker,
        "cloture": last["cloture"],
        "date": last["date"].strftime("%d/%m/%Y") if last["date"] else None,
        "variation_pct": last["variation_pct"],
    }


def _build_market_report_data():
    """Agrège les données pour le reporting marché (1 page)."""
    # Dernière date globale
    last_date = HistoriqueAction.objects.aggregate(d=Max("date"))["d"]
    if not last_date:
        return None

    # Indices phares
    indices = [_get_index_snapshot(t) for t in ["BRVMC", "BRVM30", "CAPIBRVM"]]
    indices = [i for i in indices if i]

    # Stats marché — nombre de sociétés cotées (actions avec au moins un historique)
    nb_societes = Action.objects.count()

    # Capi boursière estimée = somme(prix_actuel * nombre_actions)
    capi_totale = 0.0
    actions_with_shares = Action.objects.exclude(nombre_actions__isnull=True).exclude(nombre_actions=0)
    last_prices = {}
    last_qs = (
        HistoriqueAction.objects.filter(date=last_date)
        .values("action_id", "cloture", "volume_fcfa", "volume_titres", "variation_pct")
    )
    last_map = {h["action_id"]: h for h in last_qs}
    for a in actions_with_shares:
        h = last_map.get(a.id)
        if h and h["cloture"]:
            capi_totale += h["cloture"] * a.nombre_actions
            last_prices[a.id] = h["cloture"]

    # Volume total marché (FCFA) sur la dernière séance
    vol_total = sum((h["volume_fcfa"] or 0) for h in last_map.values())
    nb_titres_echanges = sum((h["volume_titres"] or 0) for h in last_map.values())

    # Variations du jour : top hausses / baisses / volumes
    actions_by_id = {a.id: a for a in Action.objects.all()}
    movers = []
    for aid, h in last_map.items():
        a = actions_by_id.get(aid)
        if not a:
            continue
        movers.append({
            "ticker": a.ticker,
            "nom": a.nom,
            "cloture": h["cloture"],
            "variation_pct": h["variation_pct"],
            "volume_fcfa": h["volume_fcfa"] or 0,
            "volume_titres": h["volume_titres"] or 0,
        })

    movers_with_var = [m for m in movers if m["variation_pct"] is not None]
    top_hausses = sorted(movers_with_var, key=lambda x: x["variation_pct"], reverse=True)[:5]
    top_baisses = sorted(movers_with_var, key=lambda x: x["variation_pct"])[:5]
    top_volumes = sorted(movers, key=lambda x: x["volume_fcfa"], reverse=True)[:5]

    # Part marché volume des 5 plus gros
    part_top5 = 0
    if vol_total > 0:
        part_top5 = sum(m["volume_fcfa"] for m in top_volumes) / vol_total * 100

    # ---------------- Performances sectorielles (pondérées par capi) ----------------
    # Pour chaque secteur : variation jour pondérée par capi, capi totale, nb titres,
    # volume FCFA total. Capi titre = cloture * nombre_actions.
    sector_agg = defaultdict(lambda: {
        "nb_titres": 0,
        "capi": 0.0,
        "var_x_capi": 0.0,
        "capi_with_var": 0.0,
        "volume_fcfa": 0.0,
    })
    for a in Action.objects.all():
        h = last_map.get(a.id)
        if not h or not h["cloture"]:
            continue
        sec = (a.secteur or "").strip() or "Non classé"
        capi_titre = (h["cloture"] * a.nombre_actions) if a.nombre_actions else 0
        agg = sector_agg[sec]
        agg["nb_titres"] += 1
        agg["capi"] += capi_titre
        agg["volume_fcfa"] += (h["volume_fcfa"] or 0)
        if h["variation_pct"] is not None and capi_titre > 0:
            agg["var_x_capi"] += h["variation_pct"] * capi_titre
            agg["capi_with_var"] += capi_titre

    sectors = []
    for sec, agg in sector_agg.items():
        var_pond = (agg["var_x_capi"] / agg["capi_with_var"]) if agg["capi_with_var"] > 0 else None
        part_capi = (agg["capi"] / capi_totale * 100) if capi_totale > 0 else None
        sectors.append({
            "secteur": sec,
            "nb_titres": agg["nb_titres"],
            "capi": agg["capi"],
            "part_capi_pct": round(part_capi, 2) if part_capi is not None else None,
            "var_jour_pct": round(var_pond, 2) if var_pond is not None else None,
            "volume_fcfa": agg["volume_fcfa"],
        })
    # Tri : du plus positif au plus négatif, puis non-classé en queue
    sectors.sort(key=lambda x: (
        0 if x["secteur"] != "Non classé" else 1,
        -(x["var_jour_pct"] if x["var_jour_pct"] is not None else -999),
    ))

    # ---------------- Fear & Greed (poids par défaut 1/1/1/1) ----------------
    fg = _compute_fear_greed()

    return {
        "last_date": last_date.strftime("%d/%m/%Y"),
        "indices": indices,
        "stats": {
            "nb_societes": nb_societes,
            "capi_totale": capi_totale,
            "vol_total_fcfa": vol_total,
            "nb_titres_echanges": nb_titres_echanges,
            "part_top5_vol_pct": round(part_top5, 1),
        },
        "top_hausses": top_hausses,
        "top_baisses": top_baisses,
        "top_volumes": top_volumes,
        "sectors": sectors,
        "fear_greed": fg,
    }


@csrf_exempt
def api_market_report_data(request):
    """Données agrégées pour le reporting marché (JSON)."""
    data = _build_market_report_data()
    if data is None:
        return JsonResponse({"error": "Aucune donnée disponible"}, status=404)
    return JsonResponse(data)


@csrf_exempt
def api_market_report_comment(request):
    """Génère un commentaire IA marché via Claude."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)

    api_key = body.get("api_key") or ApiConfig.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JsonResponse({"error": "Clé API Claude requise."}, status=400)

    report = body.get("report_data") or _build_market_report_data()
    if not report:
        return JsonResponse({"error": "Aucune donnée marché"}, status=404)

    # Contexte texte
    lines = [f"Date d'arrêté : {report['last_date']}"]
    lines.append("\nIndices phares :")
    for i in report["indices"]:
        var = f"{i['variation_pct']:+.2f}%" if i["variation_pct"] is not None else "n/a"
        lines.append(f"  {i['ticker']} ({i['nom']}) : {i['cloture']} ({var})")
    s = report["stats"]
    lines.append(f"\nNb sociétés cotées : {s['nb_societes']}")
    lines.append(f"Capi. boursière estimée : {s['capi_totale']:,.0f} FCFA".replace(",", " "))
    lines.append(f"Volume séance : {s['vol_total_fcfa']:,.0f} FCFA".replace(",", " "))

    fg = report.get("fear_greed") or {}
    if fg.get("score") is not None:
        lines.append(f"\nFear & Greed BRVM : {fg['score']}/100 — {fg['label']}")
        comp = fg.get("components", {})
        parts = []
        for k, lbl in [("breadth", "S_breadth"), ("vol", "S_vol"),
                       ("mom", "S_mom"), ("disp", "S_disp")]:
            v = (comp.get(k) or {}).get("value")
            if v is not None:
                parts.append(f"{lbl}={v:.0f}")
        if parts:
            lines.append("Sous-indicateurs : " + " | ".join(parts))

    sectors = report.get("sectors") or []
    if sectors:
        lines.append("\nTop secteurs (var. jour pondérée par capi) :")
        named = [s for s in sectors if s.get("var_jour_pct") is not None][:6]
        for s in named:
            lines.append(
                f"  {s['secteur']} : {s['var_jour_pct']:+.2f}% "
                f"({s['nb_titres']} titres, {s['part_capi_pct']:.1f}% capi)"
            )

    if report["top_hausses"]:
        lines.append("\nTop hausses du jour : " + ", ".join(
            f"{m['ticker']} ({m['variation_pct']:+.2f}%)" for m in report["top_hausses"][:3]
        ))
    if report["top_baisses"]:
        lines.append("Top baisses du jour : " + ", ".join(
            f"{m['ticker']} ({m['variation_pct']:+.2f}%)" for m in report["top_baisses"][:3]
        ))
    if report["top_volumes"]:
        lines.append("Top volumes : " + ", ".join(
            f"{m['ticker']}" for m in report["top_volumes"][:3]
        ))

    context_text = "\n".join(lines)

    prompt = (
        "Tu es un analyste senior de la BRVM (Bourse Régionale des Valeurs Mobilières — UEMOA). "
        f"Voici un instantané de la séance :\n\n{context_text}\n\n"
        "Rédige un commentaire de marché professionnel et concis (160-220 mots) qui :\n"
        "1) Décrit la santé du marché à travers le Fear & Greed et ses sous-indicateurs,\n"
        "2) Met en exergue la dynamique sectorielle (secteurs en tête, secteurs en repli),\n"
        "3) Pointe les mouvements remarquables (top hausses, baisses, volumes),\n"
        "4) Identifie 1-2 points de vigilance ou catalyseurs à suivre.\n"
        "Style sobre, factuel, en français. Pas de recommandation d'achat/vente individuelle."
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
    except ImportError:
        return JsonResponse({"error": "Module 'anthropic' non installé."}, status=500)
    except Exception as e:
        return JsonResponse({"error": f"Erreur Claude : {str(e)}"}, status=500)

    return JsonResponse({
        "market_comment": text,
        "disclaimer": (
            "Ce reporting est fourni à titre informatif uniquement et ne constitue pas un conseil "
            "en investissement. Les performances passées ne préjugent pas des performances futures. "
            "CGF Bourse décline toute responsabilité quant aux décisions prises sur la base de ce document."
        ),
    })


@csrf_exempt
def api_market_report_pdf(request):
    """Génère un PDF 1 page A4 — Reporting Marché BRVM."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)

    try:
        body = json.loads(request.body) if request.body else {}
    except (json.JSONDecodeError, ValueError):
        body = {}

    report = body.get("report_data") or _build_market_report_data()
    if not report:
        return JsonResponse({"error": "Aucune donnée disponible"}, status=404)

    market_comment = body.get("market_comment", "")
    disclaimer = body.get("disclaimer", (
        "Ce reporting est fourni à titre informatif uniquement et ne constitue pas un conseil "
        "en investissement. Les performances passées ne préjugent pas des performances futures. "
        "CGF Bourse décline toute responsabilité quant aux décisions prises sur la base de ce document."
    ))

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, Image
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io
    except ImportError as e:
        return JsonResponse({"error": f"Module manquant : {e}"}, status=500)

    PRIMARY = HexColor("#4f8cff")
    GREEN = HexColor("#00d97e")
    RED = HexColor("#e63757")
    GREY = HexColor("#6b7280")
    LIGHT_BG = HexColor("#f8fafc")
    WHITE = HexColor("#ffffff")
    BORDER = HexColor("#e5e7eb")

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=10 * mm, bottomMargin=10 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "MRTitle", parent=styles["Title"], fontSize=15, textColor=PRIMARY,
        spaceAfter=1 * mm, fontName="Helvetica-Bold", alignment=TA_LEFT,
    )
    subtitle_style = ParagraphStyle(
        "MRSubtitle", parent=styles["Normal"], fontSize=8, textColor=GREY,
        spaceAfter=3 * mm,
    )
    section_style = ParagraphStyle(
        "MRSection", parent=styles["Heading3"], fontSize=9, textColor=PRIMARY,
        spaceBefore=2 * mm, spaceAfter=1.5 * mm, fontName="Helvetica-Bold",
    )
    body_style = ParagraphStyle(
        "MRBody", parent=styles["Normal"], fontSize=7.5, leading=10,
        textColor=HexColor("#1f2937"), alignment=TA_JUSTIFY,
    )
    disclaimer_style = ParagraphStyle(
        "MRDisc", parent=styles["Normal"], fontSize=6, textColor=GREY,
        leading=8, alignment=TA_JUSTIFY, fontName="Helvetica-Oblique",
    )

    def fmt_n(v, dec=0, suffix=""):
        if v is None:
            return "-"
        try:
            return f"{float(v):,.{dec}f}{suffix}".replace(",", " ")
        except (ValueError, TypeError):
            return str(v)

    def fmt_pct(v):
        if v is None:
            return "-"
        try:
            return f"{float(v):+.2f}%"
        except (ValueError, TypeError):
            return str(v)

    def color_for(v):
        if v is None:
            return GREY
        try:
            return GREEN if float(v) >= 0 else RED
        except (ValueError, TypeError):
            return GREY

    elements = []

    # HEADER
    elements.append(Paragraph("REPORTING MARCHÉ BRVM", title_style))
    elements.append(Paragraph(
        f"Séance du {report['last_date']} | Date d'export : {datetime.now().strftime('%d/%m/%Y')} | CGF Bourse",
        subtitle_style,
    ))

    # ---- Bloc haut : Indices + Stats marché (2 colonnes) ----
    # Indices
    idx_rows = [["Indice", "Clôture", "Var. j"]]
    for i in report["indices"]:
        idx_rows.append([
            i["ticker"],
            fmt_n(i["cloture"], 2),
            fmt_pct(i["variation_pct"]),
        ])
    idx_table = Table(idx_rows, colWidths=[24 * mm, 28 * mm, 22 * mm])
    idx_style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
    ]
    for r in range(1, len(idx_rows)):
        try:
            v = float(idx_rows[r][2].replace("%", "").replace("+", ""))
            idx_style.append(("TEXTCOLOR", (2, r), (2, r), GREEN if v >= 0 else RED))
        except (ValueError, AttributeError):
            pass
    idx_table.setStyle(TableStyle(idx_style))

    # Stats
    s = report["stats"]
    stats_rows = [
        ["Sociétés cotées", str(s["nb_societes"])],
        ["Capi. boursière", fmt_n(s["capi_totale"], 0, " FCFA")],
        ["Volume séance", fmt_n(s["vol_total_fcfa"], 0, " FCFA")],
        ["Titres échangés", fmt_n(s["nb_titres_echanges"], 0)],
        ["Concentration Top 5 vol.", fmt_n(s["part_top5_vol_pct"], 1, "%")],
    ]
    stats_table = Table(stats_rows, colWidths=[40 * mm, 40 * mm])
    stats_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("TEXTCOLOR", (0, 0), (0, -1), GREY),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica-Bold"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
    ]))

    top_row = Table(
        [[
            [Paragraph("Indices phares", section_style), idx_table],
            "",
            [Paragraph("Statistiques marché", section_style), stats_table],
        ]],
        colWidths=[78 * mm, 4 * mm, 84 * mm],
    )
    top_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(top_row)
    elements.append(Spacer(1, 2 * mm))

    # ---- Graphique BRVMC (1 an, base 100) ----
    chart = report.get("brvmc_chart", {})
    if chart.get("dates") and chart.get("values"):
        elements.append(Paragraph("BRVM Composite — performance 1 an (base 100)", section_style))
        fig, ax = plt.subplots(figsize=(7, 1.9))
        fig.patch.set_facecolor("#f8fafc")
        ax.set_facecolor("#f8fafc")
        ax.plot(range(len(chart["values"])), chart["values"],
                color="#4f8cff", linewidth=1.4)
        ax.fill_between(range(len(chart["values"])), 100, chart["values"],
                        color="#4f8cff", alpha=0.08)
        ax.axhline(y=100, color="#9ca3af", linewidth=0.5, linestyle="--", alpha=0.6)
        ax.tick_params(labelsize=6)
        step = max(1, len(chart["dates"]) // 8)
        ax.set_xticks(range(0, len(chart["dates"]), step))
        ax.set_xticklabels([chart["dates"][i] for i in range(0, len(chart["dates"]), step)], rotation=25)
        ax.grid(True, alpha=0.2)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        plt.tight_layout()
        img_buf = io.BytesIO()
        fig.savefig(img_buf, format="png", dpi=140, bbox_inches="tight")
        plt.close(fig)
        img_buf.seek(0)
        elements.append(Image(img_buf, width=180 * mm, height=42 * mm))
        elements.append(Spacer(1, 1 * mm))

    # ---- Perf calendaire + glissante BRVMC ----
    cal = report.get("calendar_perf_brvmc", {})
    roll = report.get("rolling_perf_brvmc", {})

    cal_rows = [["Période", "BRVMC"]]
    for code, label in [("wtd", "WTD"), ("mtd", "MTD"), ("qtd", "QTD"), ("std", "STD"), ("ytd", "YTD")]:
        cal_rows.append([label, fmt_pct(cal.get(code))])
    cal_t = Table(cal_rows, colWidths=[24 * mm, 30 * mm])
    cal_style = [
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
    ]
    for r in range(1, len(cal_rows)):
        try:
            v = float(cal_rows[r][1].replace("%", "").replace("+", ""))
            cal_style.append(("TEXTCOLOR", (1, r), (1, r), GREEN if v >= 0 else RED))
        except (ValueError, AttributeError):
            pass
    cal_t.setStyle(TableStyle(cal_style))

    roll_rows = [["Période", "BRVMC"]]
    for code, label in [("perf_1w", "1 sem."), ("perf_1m", "1 mois"), ("perf_3m", "3 mois"),
                         ("perf_6m", "6 mois"), ("perf_1y", "1 an"), ("perf_3y", "3 ans")]:
        roll_rows.append([label, fmt_pct(roll.get(code))])
    roll_t = Table(roll_rows, colWidths=[24 * mm, 30 * mm])
    roll_style = list(cal_style[:9])
    for r in range(1, len(roll_rows)):
        try:
            v = float(roll_rows[r][1].replace("%", "").replace("+", ""))
            roll_style.append(("TEXTCOLOR", (1, r), (1, r), GREEN if v >= 0 else RED))
        except (ValueError, AttributeError):
            pass
    roll_t.setStyle(TableStyle(roll_style))

    # ---- Top hausses / baisses ----
    def movers_rows(movers, col):
        rows = [["Ticker", "Cours", col]]
        for m in movers:
            rows.append([
                m["ticker"],
                fmt_n(m["cloture"], 0),
                fmt_pct(m["variation_pct"]),
            ])
        while len(rows) < 6:
            rows.append(["-", "-", "-"])
        return rows

    haus_rows = movers_rows(report["top_hausses"], "Var.")
    bais_rows = movers_rows(report["top_baisses"], "Var.")

    def mover_table(rows):
        t = Table(rows, colWidths=[18 * mm, 18 * mm, 18 * mm])
        sty = [
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 6.8),
            ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("ALIGN", (1, 0), (-1, -1), "CENTER"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
        ]
        for r in range(1, len(rows)):
            try:
                v = float(rows[r][2].replace("%", "").replace("+", ""))
                sty.append(("TEXTCOLOR", (2, r), (2, r), GREEN if v >= 0 else RED))
            except (ValueError, AttributeError):
                pass
        t.setStyle(TableStyle(sty))
        return t

    # Layout : 4 mini-tables côte à côte (Cal, Glissante, Hausses, Baisses)
    perf_row = Table(
        [[
            [Paragraph("Perf. calendaire", section_style), cal_t],
            "",
            [Paragraph("Perf. glissante", section_style), roll_t],
            "",
            [Paragraph("Top 5 hausses", section_style), mover_table(haus_rows)],
            "",
            [Paragraph("Top 5 baisses", section_style), mover_table(bais_rows)],
        ]],
        colWidths=[40 * mm, 2 * mm, 40 * mm, 2 * mm, 42 * mm, 2 * mm, 42 * mm],
    )
    perf_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    elements.append(perf_row)
    elements.append(Spacer(1, 2 * mm))

    # ---- Top volumes ----
    vol_rows = [["Ticker", "Cours", "Volume (FCFA)", "Part marché"]]
    for m in report["top_volumes"]:
        part = (m["volume_fcfa"] / s["vol_total_fcfa"] * 100) if s["vol_total_fcfa"] else 0
        vol_rows.append([
            m["ticker"],
            fmt_n(m["cloture"], 0),
            fmt_n(m["volume_fcfa"], 0),
            f"{part:.1f}%",
        ])
    while len(vol_rows) < 6:
        vol_rows.append(["-", "-", "-", "-"])

    elements.append(Paragraph("Top 5 volumes échangés", section_style))
    vol_t = Table(vol_rows, colWidths=[30 * mm, 35 * mm, 60 * mm, 35 * mm])
    vol_t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, BORDER),
        ("BACKGROUND", (0, 1), (-1, -1), LIGHT_BG),
    ]))
    elements.append(vol_t)
    elements.append(Spacer(1, 2 * mm))

    # ---- Commentaire marché ----
    if market_comment:
        elements.append(Paragraph("Commentaire marché", section_style))
        elements.append(Paragraph(market_comment.replace("\n", "<br/>"), body_style))
        elements.append(Spacer(1, 1.5 * mm))

    # ---- Disclaimer ----
    elements.append(Paragraph(f"<b>Avertissement :</b> {disclaimer}", disclaimer_style))
    elements.append(Spacer(1, 1 * mm))
    elements.append(Paragraph(
        f"LOOK UP BRVM — Reporting marché généré le {datetime.now().strftime('%d/%m/%Y %H:%M')} | CGF Bourse",
        ParagraphStyle("MRFooter", parent=styles["Normal"], fontSize=6,
                       textColor=GREY, alignment=TA_CENTER),
    ))

    doc.build(elements)
    buffer.seek(0)
    response = HttpResponse(buffer.getvalue(), content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="Reporting_Marche_BRVM_{datetime.now().strftime("%Y%m%d")}.pdf"'
    )
    return response


@csrf_exempt
def api_save_config(request):
    """Sauvegarde une configuration (clé API, etc.)."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)

    cle = data.get("cle", "")
    valeur = data.get("valeur", "")
    if not cle:
        return JsonResponse({"error": "Paramètre 'cle' requis"}, status=400)

    ApiConfig.set(cle, valeur, data.get("description", ""))
    return JsonResponse({"success": True})


@csrf_exempt
def api_save_comment(request):
    """Sauvegarde un commentaire dans l'historique."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)

    ticker = data.get("ticker", "")
    if not ticker:
        return JsonResponse({"error": "Ticker requis"}, status=400)

    try:
        action = Action.objects.get(ticker=ticker)
    except Action.DoesNotExist:
        return JsonResponse({"error": f"Action {ticker} introuvable"}, status=404)

    comment = CommentHistory.objects.create(
        action=action,
        analyse=data.get("analysis_comment", ""),
        recommandation=data.get("recommendation_comment", ""),
        disclaimer=data.get("disclaimer", ""),
        donnees_contexte=data.get("performance_data", {}),
        modele_ia=data.get("model", "claude-sonnet-4-20250514"),
    )
    return JsonResponse({"success": True, "id": comment.id})


@csrf_exempt
def api_delete_comment(request, comment_id):
    """Supprime un commentaire."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    try:
        comment = CommentHistory.objects.get(id=comment_id)
        comment.delete()
        return JsonResponse({"success": True})
    except CommentHistory.DoesNotExist:
        return JsonResponse({"error": "Commentaire introuvable"}, status=404)


@csrf_exempt
def api_toggle_favorite_comment(request, comment_id):
    """Toggle le favori d'un commentaire."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    try:
        comment = CommentHistory.objects.get(id=comment_id)
        comment.favori = not comment.favori
        comment.save()
        return JsonResponse({"success": True, "favori": comment.favori})
    except CommentHistory.DoesNotExist:
        return JsonResponse({"error": "Commentaire introuvable"}, status=404)


# ============================================================
# API Indicateurs Techniques & Signaux IA
# ============================================================

def compute_rsi_series(closes, period=14):
    """Calcule la série RSI complète."""
    arr = np.array(closes, dtype=float)
    result = [None] * len(arr)
    for i in range(period, len(arr)):
        deltas = np.diff(arr[:i+1])
        gains = np.where(deltas > 0, deltas, 0)[-period:]
        losses = np.where(deltas < 0, -deltas, 0)[-period:]
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = round(100 - (100 / (1 + rs)), 2)
    return result


def compute_macd_series(closes, fast=12, slow=26, signal=9):
    """Calcule les séries MACD complètes."""
    arr = np.array(closes, dtype=float)
    
    def ema_series(data, period):
        ema = [data[0]]
        mult = 2 / (period + 1)
        for p in data[1:]:
            ema.append((p - ema[-1]) * mult + ema[-1])
        return np.array(ema)
    
    if len(arr) < slow + signal:
        return [None] * len(arr), [None] * len(arr), [None] * len(arr)
    
    ema_fast = ema_series(arr, fast)
    ema_slow = ema_series(arr, slow)
    macd_line = ema_fast - ema_slow
    
    result_macd = [None] * len(arr)
    result_signal = [None] * len(arr)
    result_hist = [None] * len(arr)
    
    signal_line = ema_series(macd_line[slow-1:], signal)
    
    for i in range(slow - 1 + signal - 1, len(arr)):
        idx = i - (slow - 1)
        if idx < len(signal_line):
            result_macd[i] = round(macd_line[i], 4)
            result_signal[i] = round(signal_line[idx], 4)
            result_hist[i] = round(macd_line[i] - signal_line[idx], 4)
    
    return result_macd, result_signal, result_hist


def compute_sma_series(closes, period):
    """Calcule la série SMA complète."""
    arr = np.array(closes, dtype=float)
    result = [None] * len(arr)
    for i in range(period - 1, len(arr)):
        result[i] = round(np.mean(arr[i-period+1:i+1]), 2)
    return result


def compute_ema_series(closes, period):
    """Calcule la série EMA complète."""
    arr = np.array(closes, dtype=float)
    if len(arr) < period:
        return [None] * len(arr)
    
    result = [None] * len(arr)
    mult = 2 / (period + 1)
    
    # Première EMA = SMA
    result[period - 1] = np.mean(arr[:period])
    
    for i in range(period, len(arr)):
        result[i] = round((arr[i] - result[i-1]) * mult + result[i-1], 2)
    
    return result


def compute_bollinger_series(closes, period=20, num_std=2):
    """Calcule les séries Bollinger complètes."""
    arr = np.array(closes, dtype=float)
    upper = [None] * len(arr)
    middle = [None] * len(arr)
    lower = [None] * len(arr)
    
    for i in range(period - 1, len(arr)):
        window = arr[i-period+1:i+1]
        sma = np.mean(window)
        std = np.std(window)
        middle[i] = round(sma, 2)
        upper[i] = round(sma + num_std * std, 2)
        lower[i] = round(sma - num_std * std, 2)
    
    return middle, upper, lower


def compute_stochastic(closes, highs, lows, period=14, smooth_k=3, smooth_d=3):
    """Calcule l'oscillateur stochastique."""
    arr_c = np.array(closes, dtype=float)
    arr_h = np.array(highs, dtype=float)
    arr_l = np.array(lows, dtype=float)
    
    k_values = [None] * len(arr_c)
    d_values = [None] * len(arr_c)
    
    for i in range(period - 1, len(arr_c)):
        high_period = np.max(arr_h[i-period+1:i+1])
        low_period = np.min(arr_l[i-period+1:i+1])
        
        if high_period != low_period:
            k_values[i] = round((arr_c[i] - low_period) / (high_period - low_period) * 100, 2)
        else:
            k_values[i] = 50.0
    
    # Smooth %K
    k_smooth = [None] * len(arr_c)
    for i in range(period - 1 + smooth_k - 1, len(arr_c)):
        vals = [v for v in k_values[i-smooth_k+1:i+1] if v is not None]
        if vals:
            k_smooth[i] = round(np.mean(vals), 2)
    
    # %D = SMA of %K
    for i in range(period - 1 + smooth_k - 1 + smooth_d - 1, len(arr_c)):
        vals = [v for v in k_smooth[i-smooth_d+1:i+1] if v is not None]
        if vals:
            d_values[i] = round(np.mean(vals), 2)
    
    return k_smooth, d_values


def compute_atr(highs, lows, closes, period=14):
    """Calcule l'Average True Range."""
    arr_h = np.array(highs, dtype=float)
    arr_l = np.array(lows, dtype=float)
    arr_c = np.array(closes, dtype=float)
    
    tr = [arr_h[0] - arr_l[0]]
    for i in range(1, len(arr_c)):
        tr1 = arr_h[i] - arr_l[i]
        tr2 = abs(arr_h[i] - arr_c[i-1])
        tr3 = abs(arr_l[i] - arr_c[i-1])
        tr.append(max(tr1, tr2, tr3))
    
    atr = [None] * len(arr_c)
    for i in range(period - 1, len(arr_c)):
        atr[i] = round(np.mean(tr[i-period+1:i+1]), 2)
    
    return atr


def compute_adx(highs, lows, closes, period=14):
    """Calcule l'ADX (Average Directional Index)."""
    arr_h = np.array(highs, dtype=float)
    arr_l = np.array(lows, dtype=float)
    arr_c = np.array(closes, dtype=float)
    
    if len(arr_c) < period * 2:
        return [None] * len(arr_c), [None] * len(arr_c), [None] * len(arr_c)
    
    # True Range
    tr = [arr_h[0] - arr_l[0]]
    plus_dm = [0]
    minus_dm = [0]
    
    for i in range(1, len(arr_c)):
        tr1 = arr_h[i] - arr_l[i]
        tr2 = abs(arr_h[i] - arr_c[i-1])
        tr3 = abs(arr_l[i] - arr_c[i-1])
        tr.append(max(tr1, tr2, tr3))
        
        up_move = arr_h[i] - arr_h[i-1]
        down_move = arr_l[i-1] - arr_l[i]
        
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
    
    # Smooth using Wilder's smoothing
    atr_smooth = [None] * len(arr_c)
    plus_di = [None] * len(arr_c)
    minus_di = [None] * len(arr_c)
    adx = [None] * len(arr_c)
    
    # First values
    if len(arr_c) >= period:
        atr_smooth[period-1] = sum(tr[:period])
        plus_dm_smooth = sum(plus_dm[:period])
        minus_dm_smooth = sum(minus_dm[:period])
        
        for i in range(period, len(arr_c)):
            atr_smooth[i] = atr_smooth[i-1] - (atr_smooth[i-1] / period) + tr[i]
            plus_dm_smooth = plus_dm_smooth - (plus_dm_smooth / period) + plus_dm[i]
            minus_dm_smooth = minus_dm_smooth - (minus_dm_smooth / period) + minus_dm[i]
            
            if atr_smooth[i] != 0:
                plus_di[i] = round(100 * plus_dm_smooth / atr_smooth[i], 2)
                minus_di[i] = round(100 * minus_dm_smooth / atr_smooth[i], 2)
        
        # Calculate DX and ADX
        dx_values = []
        for i in range(period, len(arr_c)):
            if plus_di[i] is not None and minus_di[i] is not None:
                di_sum = plus_di[i] + minus_di[i]
                if di_sum != 0:
                    dx = abs(plus_di[i] - minus_di[i]) / di_sum * 100
                    dx_values.append(dx)
                    
                    if len(dx_values) >= period:
                        adx[i] = round(np.mean(dx_values[-period:]), 2)
    
    return adx, plus_di, minus_di


def compute_williams_r(highs, lows, closes, period=14):
    """Calcule Williams %R."""
    arr_h = np.array(highs, dtype=float)
    arr_l = np.array(lows, dtype=float)
    arr_c = np.array(closes, dtype=float)
    
    result = [None] * len(arr_c)
    
    for i in range(period - 1, len(arr_c)):
        high_period = np.max(arr_h[i-period+1:i+1])
        low_period = np.min(arr_l[i-period+1:i+1])
        
        if high_period != low_period:
            result[i] = round((high_period - arr_c[i]) / (high_period - low_period) * -100, 2)
        else:
            result[i] = -50.0
    
    return result


def compute_cci(highs, lows, closes, period=20):
    """Calcule le Commodity Channel Index."""
    arr_h = np.array(highs, dtype=float)
    arr_l = np.array(lows, dtype=float)
    arr_c = np.array(closes, dtype=float)
    
    # Typical Price
    tp = (arr_h + arr_l + arr_c) / 3
    
    result = [None] * len(arr_c)
    
    for i in range(period - 1, len(arr_c)):
        tp_window = tp[i-period+1:i+1]
        tp_sma = np.mean(tp_window)
        mean_dev = np.mean(np.abs(tp_window - tp_sma))
        
        if mean_dev != 0:
            result[i] = round((tp[i] - tp_sma) / (0.015 * mean_dev), 2)
        else:
            result[i] = 0.0
    
    return result


def compute_obv(closes, volumes):
    """Calcule l'On-Balance Volume."""
    arr_c = np.array(closes, dtype=float)
    arr_v = np.array(volumes, dtype=float)
    
    obv = [arr_v[0]]
    for i in range(1, len(arr_c)):
        if arr_c[i] > arr_c[i-1]:
            obv.append(obv[-1] + arr_v[i])
        elif arr_c[i] < arr_c[i-1]:
            obv.append(obv[-1] - arr_v[i])
        else:
            obv.append(obv[-1])
    
    return [round(v, 0) for v in obv]


def compute_mfi(highs, lows, closes, volumes, period=14):
    """Calcule le Money Flow Index."""
    arr_h = np.array(highs, dtype=float)
    arr_l = np.array(lows, dtype=float)
    arr_c = np.array(closes, dtype=float)
    arr_v = np.array(volumes, dtype=float)
    
    # Typical Price
    tp = (arr_h + arr_l + arr_c) / 3
    
    # Raw Money Flow
    rmf = tp * arr_v
    
    result = [None] * len(arr_c)
    
    for i in range(period, len(arr_c)):
        pos_flow = 0
        neg_flow = 0
        
        for j in range(i - period + 1, i + 1):
            if tp[j] > tp[j-1]:
                pos_flow += rmf[j]
            elif tp[j] < tp[j-1]:
                neg_flow += rmf[j]
        
        if neg_flow != 0:
            mfr = pos_flow / neg_flow
            result[i] = round(100 - (100 / (1 + mfr)), 2)
        else:
            result[i] = 100.0
    
    return result


def detect_support_resistance(closes, highs, lows, window=20, tolerance=0.02):
    """Détecte les niveaux de support et résistance."""
    arr_c = np.array(closes, dtype=float)
    arr_h = np.array(highs, dtype=float)
    arr_l = np.array(lows, dtype=float)
    
    current_price = arr_c[-1]
    
    # Find local minima and maxima
    supports = []
    resistances = []
    
    for i in range(window, len(arr_c) - window):
        # Local minimum (support)
        if arr_l[i] == min(arr_l[i-window:i+window+1]):
            supports.append(arr_l[i])
        
        # Local maximum (resistance)
        if arr_h[i] == max(arr_h[i-window:i+window+1]):
            resistances.append(arr_h[i])
    
    # Cluster nearby levels
    def cluster_levels(levels, tolerance_pct):
        if not levels:
            return []
        levels = sorted(levels)
        clusters = []
        current_cluster = [levels[0]]
        
        for level in levels[1:]:
            if abs(level - current_cluster[-1]) / current_cluster[-1] <= tolerance_pct:
                current_cluster.append(level)
            else:
                clusters.append(round(np.mean(current_cluster), 2))
                current_cluster = [level]
        clusters.append(round(np.mean(current_cluster), 2))
        return clusters
    
    clustered_supports = cluster_levels(supports, tolerance)
    clustered_resistances = cluster_levels(resistances, tolerance)
    
    # Filter: supports below current price, resistances above
    final_supports = [s for s in clustered_supports if s < current_price][-3:]
    final_resistances = [r for r in clustered_resistances if r > current_price][:3]
    
    return final_supports, final_resistances


@csrf_exempt
def api_talib_indicators(request):
    """Calcule un ensemble configurable d'indicateurs techniques inspirés de TA-Lib.

    Categories: Overlap, Momentum, Volume, Volatility, Price Transform, Cycle, Pattern.

    Body JSON:
        {
            "ticker": "SGBC",
            "period": "1y",
            "config": {
                "SMA":  {"enabled": true, "periods": [20, 50, 200]},
                "EMA":  {"enabled": true, "periods": [12, 26]},
                "WMA":  {"enabled": false, "periods": [20]},
                "BBANDS": {"enabled": true, "period": 20, "std": 2},
                "KAMA": {"enabled": false, "period": 10},
                "RSI":  {"enabled": true, "period": 14},
                "MACD": {"enabled": true, "fast": 12, "slow": 26, "signal": 9},
                "STOCH": {"enabled": false, "period": 14, "smooth_k": 3, "smooth_d": 3},
                "CCI":  {"enabled": false, "period": 20},
                "ROC":  {"enabled": false, "period": 10},
                "MOM":  {"enabled": false, "period": 10},
                "ADX":  {"enabled": false, "period": 14},
                "WILLR":{"enabled": false, "period": 14},
                "OBV":  {"enabled": false},
                "MFI":  {"enabled": false, "period": 14},
                "AD":   {"enabled": false},
                "ADOSC":{"enabled": false, "fast": 3, "slow": 10},
                "ATR":  {"enabled": false, "period": 14},
                "NATR": {"enabled": false, "period": 14},
                "STDDEV":{"enabled": false, "period": 20},
                "TYPPRICE": {"enabled": false},
                "MEDPRICE": {"enabled": false},
                "WCLPRICE": {"enabled": false},
                "AVGPRICE": {"enabled": false},
                "HT_TRENDLINE": {"enabled": false},
                "HT_DCPERIOD":  {"enabled": false},
                "HT_TRENDMODE": {"enabled": false},
                "HT_SINE":      {"enabled": false},
                "CDL_DOJI":     {"enabled": false},
                "CDL_HAMMER":   {"enabled": false},
                "CDL_SHOOTING_STAR": {"enabled": false},
                "CDL_ENGULFING": {"enabled": false},
                "CDL_MORNING_STAR": {"enabled": false},
                "CDL_EVENING_STAR": {"enabled": false}
            }
        }
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)

    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)

    ticker = data.get("ticker", "")
    period_filter = data.get("period", "1y")
    config = data.get("config", {}) or {}

    if not ticker:
        return JsonResponse({"error": "Ticker requis"}, status=400)

    try:
        action = Action.objects.get(ticker=ticker)
    except Action.DoesNotExist:
        return JsonResponse({"error": f"Action {ticker} introuvable"}, status=404)

    historiques = list(
        HistoriqueAction.objects.filter(action=action)
        .order_by("date")
        .values("date", "ouverture", "plus_haut", "plus_bas", "cloture", "volume_titres")
    )
    if not historiques:
        return JsonResponse({"error": "Aucune donnée historique"}, status=400)

    now = datetime.now().date()
    cutoff = None
    if period_filter == "1m":
        cutoff = now - timedelta(days=30)
    elif period_filter == "3m":
        cutoff = now - timedelta(days=90)
    elif period_filter == "6m":
        cutoff = now - timedelta(days=180)
    elif period_filter == "1y":
        cutoff = now - timedelta(days=365)
    elif period_filter == "3y":
        cutoff = now - timedelta(days=365 * 3)
    elif period_filter == "5y":
        cutoff = now - timedelta(days=365 * 5)

    if cutoff:
        historiques = [h for h in historiques if h["date"] >= cutoff]

    if not historiques:
        return JsonResponse({"error": "Aucune donnée pour cette période"}, status=400)

    dates = [h["date"].strftime("%Y-%m-%d") for h in historiques]
    opens = [h["ouverture"] for h in historiques]
    highs = [h["plus_haut"] for h in historiques]
    lows = [h["plus_bas"] for h in historiques]
    closes = [h["cloture"] for h in historiques]
    volumes = [h["volume_titres"] or 0 for h in historiques]

    from .services_indicators import compute_indicators
    try:
        series, current, patterns = compute_indicators(opens, highs, lows, closes, volumes, config)
    except Exception as exc:  # pragma: no cover - defensive
        return JsonResponse({"error": f"Erreur de calcul: {exc}"}, status=500)

    # Map patterns indices to dates for easier display
    detected = []
    for p in patterns[-50:]:  # last 50 occurrences
        idx = p["index"]
        if 0 <= idx < len(dates):
            detected.append({
                "date": dates[idx],
                "pattern": p["pattern"],
                "signal": p["signal"],
                "label": "Haussier" if p["signal"] > 0 else "Baissier",
            })

    return JsonResponse({
        "ticker": ticker,
        "period": period_filter,
        "dates": dates,
        "ohlcv": {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        "indicators": series,
        "current_values": current,
        "patterns_detected": detected,
        "config": config,
    })


@csrf_exempt
def api_calculate_indicators(request):
    """Calcule les indicateurs techniques sélectionnés pour une action."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)
    
    ticker = data.get("ticker", "")
    period_filter = data.get("period", "1y")  # 1m, 3m, 6m, 1y, 3y, all
    indicators = data.get("indicators", [])  # Liste des indicateurs à calculer
    params = data.get("params", {})  # Paramètres personnalisés
    
    if not ticker:
        return JsonResponse({"error": "Ticker requis"}, status=400)
    
    try:
        action = Action.objects.get(ticker=ticker)
    except Action.DoesNotExist:
        return JsonResponse({"error": f"Action {ticker} introuvable"}, status=404)
    
    # Récupérer les données historiques
    historiques = list(
        HistoriqueAction.objects.filter(action=action)
        .order_by("date")
        .values("date", "ouverture", "plus_haut", "plus_bas", "cloture", "volume_titres")
    )
    
    if not historiques:
        return JsonResponse({"error": "Aucune donnée historique"}, status=400)
    
    # Filtrer par période
    now = datetime.now().date()
    if period_filter == "1m":
        cutoff = now - timedelta(days=30)
    elif period_filter == "3m":
        cutoff = now - timedelta(days=90)
    elif period_filter == "6m":
        cutoff = now - timedelta(days=180)
    elif period_filter == "1y":
        cutoff = now - timedelta(days=365)
    elif period_filter == "3y":
        cutoff = now - timedelta(days=365*3)
    elif period_filter == "5y":
        cutoff = now - timedelta(days=365*5)
    else:  # all
        cutoff = None
    
    if cutoff:
        historiques = [h for h in historiques if h["date"] >= cutoff]
    
    if not historiques:
        return JsonResponse({"error": "Aucune donnée pour cette période"}, status=400)
    
    # Extraire les séries
    dates = [h["date"].strftime("%Y-%m-%d") for h in historiques]
    opens = [h["ouverture"] or 0 for h in historiques]
    highs = [h["plus_haut"] or 0 for h in historiques]
    lows = [h["plus_bas"] or 0 for h in historiques]
    closes = [h["cloture"] or 0 for h in historiques]
    volumes = [h["volume_titres"] or 0 for h in historiques]
    
    result = {
        "ticker": ticker,
        "period": period_filter,
        "dates": dates,
        "ohlcv": {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        },
        "indicators": {},
        "current_values": {},
    }
    
    # Calculer chaque indicateur demandé
    for ind in indicators:
        ind_lower = ind.lower()
        
        if ind_lower == "rsi":
            period = params.get("rsi_period", 14)
            series = compute_rsi_series(closes, period)
            result["indicators"]["rsi"] = series
            result["current_values"]["rsi"] = series[-1] if series else None
            
        elif ind_lower == "macd":
            fast = params.get("macd_fast", 12)
            slow = params.get("macd_slow", 26)
            signal = params.get("macd_signal", 9)
            macd_l, macd_s, macd_h = compute_macd_series(closes, fast, slow, signal)
            result["indicators"]["macd_line"] = macd_l
            result["indicators"]["macd_signal"] = macd_s
            result["indicators"]["macd_histogram"] = macd_h
            result["current_values"]["macd"] = macd_l[-1] if macd_l else None
            result["current_values"]["macd_signal"] = macd_s[-1] if macd_s else None
            result["current_values"]["macd_histogram"] = macd_h[-1] if macd_h else None
            
        elif ind_lower == "sma":
            periods = params.get("sma_periods", [20, 50, 200])
            for p in periods:
                series = compute_sma_series(closes, p)
                result["indicators"][f"sma_{p}"] = series
                result["current_values"][f"sma_{p}"] = series[-1] if series else None
                
        elif ind_lower == "ema":
            periods = params.get("ema_periods", [12, 26, 50])
            for p in periods:
                series = compute_ema_series(closes, p)
                result["indicators"][f"ema_{p}"] = series
                result["current_values"][f"ema_{p}"] = series[-1] if series else None
                
        elif ind_lower == "bollinger":
            period = params.get("bollinger_period", 20)
            std = params.get("bollinger_std", 2)
            mid, upper, lower = compute_bollinger_series(closes, period, std)
            result["indicators"]["bollinger_middle"] = mid
            result["indicators"]["bollinger_upper"] = upper
            result["indicators"]["bollinger_lower"] = lower
            result["current_values"]["bollinger_middle"] = mid[-1] if mid else None
            result["current_values"]["bollinger_upper"] = upper[-1] if upper else None
            result["current_values"]["bollinger_lower"] = lower[-1] if lower else None
            
        elif ind_lower == "stochastic":
            period = params.get("stoch_period", 14)
            k, d = compute_stochastic(closes, highs, lows, period)
            result["indicators"]["stoch_k"] = k
            result["indicators"]["stoch_d"] = d
            result["current_values"]["stoch_k"] = k[-1] if k else None
            result["current_values"]["stoch_d"] = d[-1] if d else None
            
        elif ind_lower == "atr":
            period = params.get("atr_period", 14)
            series = compute_atr(highs, lows, closes, period)
            result["indicators"]["atr"] = series
            result["current_values"]["atr"] = series[-1] if series else None
            
        elif ind_lower == "adx":
            period = params.get("adx_period", 14)
            adx, plus_di, minus_di = compute_adx(highs, lows, closes, period)
            result["indicators"]["adx"] = adx
            result["indicators"]["plus_di"] = plus_di
            result["indicators"]["minus_di"] = minus_di
            result["current_values"]["adx"] = adx[-1] if adx else None
            result["current_values"]["plus_di"] = plus_di[-1] if plus_di else None
            result["current_values"]["minus_di"] = minus_di[-1] if minus_di else None
            
        elif ind_lower == "williams":
            period = params.get("williams_period", 14)
            series = compute_williams_r(highs, lows, closes, period)
            result["indicators"]["williams_r"] = series
            result["current_values"]["williams_r"] = series[-1] if series else None
            
        elif ind_lower == "cci":
            period = params.get("cci_period", 20)
            series = compute_cci(highs, lows, closes, period)
            result["indicators"]["cci"] = series
            result["current_values"]["cci"] = series[-1] if series else None
            
        elif ind_lower == "obv":
            series = compute_obv(closes, volumes)
            result["indicators"]["obv"] = series
            result["current_values"]["obv"] = series[-1] if series else None
            
        elif ind_lower == "mfi":
            period = params.get("mfi_period", 14)
            series = compute_mfi(highs, lows, closes, volumes, period)
            result["indicators"]["mfi"] = series
            result["current_values"]["mfi"] = series[-1] if series else None
    
    # Toujours calculer support/résistance pour le signal
    supports, resistances = detect_support_resistance(closes, highs, lows)
    result["support_resistance"] = {
        "supports": supports,
        "resistances": resistances,
    }
    
    return JsonResponse(result)


@csrf_exempt
def api_validate_claude_key(request):
    """Valide une clé API Claude et la stocke si valide."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)
    
    api_key = data.get("api_key", "").strip()
    
    if not api_key:
        return JsonResponse({"error": "Clé API requise"}, status=400)
    
    if not api_key.startswith("sk-ant-"):
        return JsonResponse({"error": "Format de clé invalide (doit commencer par sk-ant-)"}, status=400)
    
    # Tester la clé avec un appel minimal
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        
        # Appel minime pour valider
        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=10,
            messages=[{"role": "user", "content": "ok"}],
        )
        
        # Si on arrive ici, la clé est valide
        ApiConfig.set("ANTHROPIC_API_KEY", api_key, "Clé API Claude validée")
        
        return JsonResponse({
            "success": True,
            "message": "Clé API validée et enregistrée"
        })
        
    except ImportError:
        return JsonResponse({"error": "Module 'anthropic' non installé"}, status=500)
    except Exception as e:
        error_msg = str(e)
        if "invalid_api_key" in error_msg.lower() or "authentication" in error_msg.lower():
            return JsonResponse({"error": "Clé API invalide"}, status=400)
        return JsonResponse({"error": f"Erreur de validation: {error_msg}"}, status=500)


@csrf_exempt
def api_estimate_claude_cost(request):
    """Estime le coût d'un appel Claude pour la génération de signal."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)
    
    model = data.get("model", "sonnet")
    period = data.get("period", "1y")
    indicators_count = data.get("indicators_count", 5)
    
    # Estimation du nombre de tokens
    # OHLCV: ~10 tokens par jour, ~252 jours/an
    days_map = {"1m": 22, "3m": 66, "6m": 132, "1y": 252, "3y": 756, "5y": 1260, "all": 2000}
    days = days_map.get(period, 252)
    
    # Tokens estimés
    ohlcv_tokens = days * 10
    indicators_tokens = indicators_count * days * 5
    prompt_tokens = 500  # Instructions système
    
    input_tokens = ohlcv_tokens + indicators_tokens + prompt_tokens
    output_tokens = 800  # Réponse structurée
    
    # Prix par million de tokens (approximatif, avril 2024)
    pricing = {
        "haiku": {"input": 0.25, "output": 1.25},
        "sonnet": {"input": 3.0, "output": 15.0},
        "opus": {"input": 15.0, "output": 75.0},
    }
    
    model_pricing = pricing.get(model.lower(), pricing["sonnet"])
    
    cost_input = (input_tokens / 1_000_000) * model_pricing["input"]
    cost_output = (output_tokens / 1_000_000) * model_pricing["output"]
    total_cost = cost_input + cost_output
    
    return JsonResponse({
        "model": model,
        "period": period,
        "estimated_tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "total": input_tokens + output_tokens,
        },
        "estimated_cost_usd": round(total_cost, 4),
        "estimated_cost_display": f"~${total_cost:.4f}",
    })


@csrf_exempt
def api_generate_trading_signal(request):
    """Génère un signal de trading via Claude."""
    if request.method != "POST":
        return JsonResponse({"error": "POST requis"}, status=405)
    
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "JSON invalide"}, status=400)
    
    # Récupérer la clé API
    api_key = data.get("api_key") or ApiConfig.get("ANTHROPIC_API_KEY")
    if not api_key:
        return JsonResponse({
            "error": "Clé API Claude requise. Veuillez configurer votre clé API."
        }, status=400)
    
    model_choice = data.get("model", "sonnet").lower()
    model_map = {
        "haiku": "claude-3-haiku-20240307",
        "sonnet": "claude-sonnet-4-20250514",
        "opus": "claude-opus-4-20250514",
    }
    model = model_map.get(model_choice, model_map["sonnet"])
    
    ticker = data.get("ticker", "")
    ohlcv_data = data.get("ohlcv", {})
    indicators_data = data.get("indicators", {})
    current_values = data.get("current_values", {})
    support_resistance = data.get("support_resistance", {})
    period = data.get("period", "1y")
    
    if not ticker:
        return JsonResponse({"error": "Ticker requis"}, status=400)
    
    # Construire le contexte pour Claude
    context_parts = [
        f"=== ANALYSE TECHNIQUE - {ticker} ===",
        f"Période d'analyse: {period}",
        f"Nombre de séances: {len(ohlcv_data.get('close', []))}",
        "",
        "--- DONNÉES OHLCV (dernières 10 séances) ---",
    ]
    
    # Ajouter les 10 dernières séances OHLCV
    closes = ohlcv_data.get("close", [])[-10:]
    opens = ohlcv_data.get("open", [])[-10:]
    highs = ohlcv_data.get("high", [])[-10:]
    lows = ohlcv_data.get("low", [])[-10:]
    volumes = ohlcv_data.get("volume", [])[-10:]
    dates = data.get("dates", [])[-10:]
    
    for i in range(len(closes)):
        context_parts.append(
            f"{dates[i] if i < len(dates) else 'N/A'}: O={opens[i] if i < len(opens) else 'N/A'}, "
            f"H={highs[i] if i < len(highs) else 'N/A'}, L={lows[i] if i < len(lows) else 'N/A'}, "
            f"C={closes[i]}, V={volumes[i] if i < len(volumes) else 'N/A'}"
        )
    
    context_parts.append("")
    context_parts.append("--- VALEURS ACTUELLES DES INDICATEURS ---")
    
    for key, value in current_values.items():
        if value is not None:
            context_parts.append(f"{key.upper()}: {value}")
    
    context_parts.append("")
    context_parts.append("--- SUPPORTS ET RÉSISTANCES DÉTECTÉS ---")
    supports = support_resistance.get("supports", [])
    resistances = support_resistance.get("resistances", [])
    context_parts.append(f"Supports: {', '.join(map(str, supports)) if supports else 'Non détectés'}")
    context_parts.append(f"Résistances: {', '.join(map(str, resistances)) if resistances else 'Non détectées'}")
    
    context_text = "\n".join(context_parts)
    
    # Prompt système
    system_prompt = """Tu es un analyste technique senior spécialisé sur la BRVM (Bourse Régionale des Valeurs Mobilières).
Tu dois analyser les données techniques fournies et produire une recommandation de trading structurée.

IMPORTANT: Tu dois répondre UNIQUEMENT en JSON valide, sans aucun texte avant ou après.
Le JSON doit suivre exactement cette structure:

{
    "signal": "ACHAT" ou "NEUTRE" ou "VENTE",
    "confidence": nombre entre 0 et 100,
    "supports": [liste des niveaux de support identifiés],
    "resistances": [liste des niveaux de résistance identifiés],
    "justification": "Explication technique détaillée de 3-5 phrases",
    "entry": prix d'entrée recommandé ou null,
    "stop_loss": niveau de stop loss recommandé ou null,
    "take_profit": niveau de take profit recommandé ou null,
    "risk_reward": ratio risque/rendement calculé ou null
}

Règles:
- Signal ACHAT: indicateurs haussiers dominants, RSI non suracheté, MACD positif ou croisement haussier
- Signal VENTE: indicateurs baissiers dominants, RSI non survendu, MACD négatif ou croisement baissier  
- Signal NEUTRE: signaux mixtes ou manque de données
- Confidence: 0-30 faible, 31-60 moyenne, 61-100 forte
- Stop loss: généralement sous le support le plus proche pour un achat, au-dessus de la résistance pour une vente
- Take profit: basé sur les niveaux de résistance (achat) ou support (vente)"""

    user_prompt = f"Analyse ces données et génère un signal de trading:\n\n{context_text}"
    
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        
        response_text = response.content[0].text.strip()
        
        # Parser le JSON de la réponse
        try:
            # Nettoyer la réponse si elle contient du texte avant/après le JSON
            json_start = response_text.find("{")
            json_end = response_text.rfind("}") + 1
            if json_start != -1 and json_end > json_start:
                json_str = response_text[json_start:json_end]
                signal_data = json.loads(json_str)
            else:
                signal_data = json.loads(response_text)
            
            return JsonResponse({
                "success": True,
                "ticker": ticker,
                "model": model_choice,
                "signal": signal_data,
                "raw_response": response_text,
            })
            
        except json.JSONDecodeError:
            # Si le parsing échoue, retourner la réponse brute
            return JsonResponse({
                "success": True,
                "ticker": ticker,
                "model": model_choice,
                "signal": {
                    "signal": "NEUTRE",
                    "confidence": 0,
                    "justification": response_text,
                    "supports": [],
                    "resistances": [],
                    "entry": None,
                    "stop_loss": None,
                    "take_profit": None,
                },
                "raw_response": response_text,
                "parse_error": True,
            })
        
    except ImportError:
        return JsonResponse({"error": "Module 'anthropic' non installé"}, status=500)
    except Exception as e:
        return JsonResponse({"error": f"Erreur API Claude: {str(e)}"}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def api_save_trading_signal(request):
    """Sauvegarde un signal de trading généré dans la base de données"""
    try:
        data = json.loads(request.body)
        
        ticker = data.get("ticker")
        signal_data = data.get("signal", {})
        model = data.get("model", "claude")
        periode = data.get("periode", "6 mois")
        indicateurs = data.get("indicateurs", [])
        prix_actuel = data.get("prix_actuel")
        
        if not ticker:
            return JsonResponse({"error": "Ticker requis"}, status=400)
        
        # Trouver l'action correspondante
        try:
            action = Action.objects.get(ticker=ticker)
        except Action.DoesNotExist:
            return JsonResponse({"error": f"Action {ticker} non trouvée"}, status=404)
        
        # Créer le signal
        trading_signal = TradingSignal.objects.create(
            action=action,
            periode_analyse=periode,
            signal=signal_data.get("signal", "NEUTRE"),
            confiance=signal_data.get("confidence", 0),
            prix_entree=signal_data.get("entry"),
            stop_loss=signal_data.get("stop_loss"),
            take_profit=signal_data.get("take_profit"),
            risk_reward=signal_data.get("risk_reward"),
            supports=signal_data.get("supports", []),
            resistances=signal_data.get("resistances", []),
            justification=signal_data.get("justification", ""),
            indicateurs_utilises=indicateurs,
            valeurs_indicateurs=data.get("valeurs_indicateurs", {}),
            modele_ia=model,
            prix_actuel=prix_actuel,
        )
        
        return JsonResponse({
            "success": True,
            "signal_id": trading_signal.id,
            "message": "Signal sauvegardé avec succès",
        })
        
    except json.JSONDecodeError:
        return JsonResponse({"error": "JSON invalide"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@require_http_methods(["GET"])
def api_get_signal_history(request, ticker):
    """Récupère l'historique des signaux pour une action"""
    try:
        # Paramètres de pagination
        limit = int(request.GET.get("limit", 20))
        offset = int(request.GET.get("offset", 0))
        favoris_only = request.GET.get("favoris", "false").lower() == "true"
        
        # Trouver l'action
        try:
            action = Action.objects.get(ticker=ticker)
        except Action.DoesNotExist:
            return JsonResponse({"error": f"Action {ticker} non trouvée"}, status=404)
        
        # Construire la requête
        queryset = TradingSignal.objects.filter(action=action)
        
        if favoris_only:
            queryset = queryset.filter(favori=True)
        
        total_count = queryset.count()
        signals = queryset.order_by("-date_generation")[offset:offset + limit]
        
        # Formater les données
        signals_data = []
        for sig in signals:
            signals_data.append({
                "id": sig.id,
                "date_generation": sig.date_generation.strftime("%d/%m/%Y %H:%M"),
                "periode_analyse": sig.periode_analyse,
                "signal": sig.signal,
                "confiance": sig.confiance,
                "prix_entree": float(sig.prix_entree) if sig.prix_entree else None,
                "stop_loss": float(sig.stop_loss) if sig.stop_loss else None,
                "take_profit": float(sig.take_profit) if sig.take_profit else None,
                "risk_reward": float(sig.risk_reward) if sig.risk_reward else None,
                "supports": sig.supports,
                "resistances": sig.resistances,
                "justification": sig.justification,
                "indicateurs_utilises": sig.indicateurs_utilises,
                "valeurs_indicateurs": sig.valeurs_indicateurs,
                "modele_ia": sig.modele_ia,
                "prix_actuel": float(sig.prix_actuel) if sig.prix_actuel else None,
                "favori": sig.favori,
            })
        
        return JsonResponse({
            "success": True,
            "ticker": ticker,
            "total_count": total_count,
            "signals": signals_data,
        })
        
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def api_toggle_signal_favorite(request, signal_id):
    """Bascule le statut favori d'un signal"""
    try:
        signal = TradingSignal.objects.get(id=signal_id)
        signal.favori = not signal.favori
        signal.save()
        
        return JsonResponse({
            "success": True,
            "signal_id": signal_id,
            "favori": signal.favori,
        })
        
    except TradingSignal.DoesNotExist:
        return JsonResponse({"error": "Signal non trouvé"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["DELETE"])
def api_delete_signal(request, signal_id):
    """Supprime un signal de trading"""
    try:
        signal = TradingSignal.objects.get(id=signal_id)
        signal.delete()

        return JsonResponse({
            "success": True,
            "message": "Signal supprimé avec succès",
        })

    except TradingSignal.DoesNotExist:
        return JsonResponse({"error": "Signal non trouvé"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# ============================================================
# Simulation Portefeuille
# ============================================================

def simulation_portefeuille(request):
    """Page principale de simulation de portefeuille."""
    ctx = get_context_base(request)
    portefeuilles = Portefeuille.objects.all()
    actions = Action.objects.all().order_by("ticker")
    indices_dispos = list(Indice.objects.all().order_by("ticker").values("ticker", "nom"))

    # Portefeuille sélectionné
    pf_id = request.GET.get("portefeuille")
    indice_benchmark = request.GET.get("indice")  # ticker indice pour benchmark
    portefeuille = None
    positions_groupees = []
    repartition_data = None
    benchmark_data = None
    kpis = None
    totaux = {"valeur": 0, "cout": 0, "pnl": 0, "frais": 0}

    if pf_id:
        try:
            portefeuille = Portefeuille.objects.get(id=pf_id)
            lignes_qs = portefeuille.lignes.filter(active=True).select_related("action").order_by("action__ticker", "date_achat")

            # Regrouper les lignes par ticker
            groupes = defaultdict(list)
            for ligne in lignes_qs:
                groupes[ligne.action.ticker].append(ligne)

            # Calculer les agrégats par ticker
            for ticker, lignes in groupes.items():
                action = lignes[0].action
                total_quantite = sum(l.quantite for l in lignes)
                total_cout = sum(l.cout_total for l in lignes)
                total_montant_investi = sum(l.montant_investi for l in lignes)
                total_frais = sum(l.frais for l in lignes)

                # Prix de revient unitaire moyen pondéré
                pru = total_montant_investi / total_quantite if total_quantite > 0 else 0

                # Cours actuel
                cours_actuel = HistoriqueAction.objects.filter(
                    action=action
                ).order_by("-date").values_list("cloture", flat=True).first()

                valeur_actuelle = (cours_actuel or 0) * total_quantite
                pnl = valeur_actuelle - total_cout
                pnl_pct = (pnl / total_cout * 100) if total_cout > 0 else 0

                # Sous-lignes (détail par date d'achat)
                sous_lignes = []
                for l in lignes:
                    l_cours = cours_actuel or l.prix_achat
                    l_valeur = l_cours * l.quantite
                    l_pnl = l_valeur - l.cout_total
                    l_pnl_pct = (l_pnl / l.cout_total * 100) if l.cout_total > 0 else 0
                    sous_lignes.append({
                        "id": l.id,
                        "date_achat": l.date_achat.strftime("%d/%m/%Y"),
                        "date_achat_iso": l.date_achat.strftime("%Y-%m-%d"),
                        "quantite": l.quantite,
                        "prix_achat": l.prix_achat,
                        "frais": l.frais,
                        "montant_investi": l.montant_investi,
                        "cout_total": l.cout_total,
                        "valeur_actuelle": l_valeur,
                        "pnl": l_pnl,
                        "pnl_pct": l_pnl_pct,
                    })

                positions_groupees.append({
                    "ticker": ticker,
                    "nom": action.nom,
                    "quantite": total_quantite,
                    "pru": pru,
                    "cours_actuel": cours_actuel,
                    "valeur_actuelle": valeur_actuelle,
                    "cout_total": total_cout,
                    "frais_total": total_frais,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "nb_entrees": len(lignes),
                    "sous_lignes": sous_lignes,
                })

            # Calcul du poids après avoir toutes les valeurs
            valeur_flottante_totale = sum(p["valeur_actuelle"] for p in positions_groupees)
            for p in positions_groupees:
                p["poids_pct"] = (p["valeur_actuelle"] / valeur_flottante_totale * 100) if valeur_flottante_totale > 0 else 0

            # Données de répartition pour le graphique (par ticker, pas par ligne)
            if positions_groupees:
                repartition_data = {
                    "labels": [p["ticker"] for p in positions_groupees],
                    "values": [p["valeur_actuelle"] for p in positions_groupees],
                    "colors": _generate_colors(len(positions_groupees)),
                }

            # Agrégats pour la ligne TOTAL (calculés côté serveur, plus fiable que JS)
            totaux = {
                "valeur": sum(p["valeur_actuelle"] for p in positions_groupees),
                "cout": sum(p["cout_total"] for p in positions_groupees),
                "pnl": sum(p["pnl"] for p in positions_groupees),
                "frais": sum(p["frais_total"] for p in positions_groupees),
            }

            # Benchmark vs indice sélectionné (ou BRVMC par défaut)
            benchmark_data = _compute_benchmark(portefeuille, indice_ticker=indice_benchmark)

            # KPIs étendus si on a une courbe
            if benchmark_data:
                kpis = _compute_kpis(
                    benchmark_data["portefeuille"],
                    benchmark_data["brvm"],
                    benchmark_data["dates"],
                )
                if kpis:
                    kpis["ecart_vs_indice_pct"] = round(
                        kpis["rendement_total_pct"] - kpis["rendement_idx_total_pct"], 2
                    )

        except Portefeuille.DoesNotExist:
            portefeuille = None

    ctx.update({
        "portefeuilles": portefeuilles,
        "actions": actions,
        "indices_dispos": indices_dispos,
        "indice_benchmark_courant": indice_benchmark or (benchmark_data["indice_ticker"] if benchmark_data else "BRVMC"),
        "portefeuille": portefeuille,
        "positions_groupees": positions_groupees,
        "positions_json": json.dumps(positions_groupees, ensure_ascii=False, default=str),
        "repartition_data": json.dumps(repartition_data, ensure_ascii=False) if repartition_data else "null",
        "benchmark_data": json.dumps(benchmark_data, ensure_ascii=False) if benchmark_data else "null",
        "nb_positions": len(positions_groupees),
        "totaux": totaux,
        "kpis": kpis,
    })
    return render(request, "dashboard/simulation_portefeuille.html", ctx)


def _generate_colors(n):
    """Génère n couleurs distinctes pour les graphiques."""
    palette = [
        "#2563eb", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
        "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#6366f1",
        "#14b8a6", "#e11d48", "#a855f7", "#0ea5e9", "#65a30d",
    ]
    return [palette[i % len(palette)] for i in range(n)]


def _compute_kpis(serie_pf, serie_idx, dates):
    """Calcule les KPIs étendus à partir des séries base 100.

    serie_pf, serie_idx : listes de float (base 100, longueur égale à dates)
    dates : liste de strings "YYYY-MM-DD" (longueur égale aux séries)

    Retourne un dict avec rendement, volatilité, sharpe, max drawdown,
    alpha/beta vs benchmark, win rate, durée.
    """
    import math
    if not serie_pf or len(serie_pf) < 2:
        return None

    n = len(serie_pf)
    # Rendements journaliers
    rets_pf = [(serie_pf[i] / serie_pf[i-1] - 1) for i in range(1, n) if serie_pf[i-1]]
    rets_idx = [(serie_idx[i] / serie_idx[i-1] - 1) for i in range(1, n) if serie_idx[i-1]] if serie_idx else []

    if not rets_pf:
        return None

    # Rendement total
    rendement_total = serie_pf[-1] / serie_pf[0] - 1 if serie_pf[0] else 0
    rendement_idx_total = (serie_idx[-1] / serie_idx[0] - 1) if (serie_idx and serie_idx[0]) else 0

    # Annualisation (252 jours ouvrés)
    annees = max(n / 252.0, 1/252)
    try:
        rendement_annualise = (1 + rendement_total) ** (1 / annees) - 1
    except Exception:
        rendement_annualise = rendement_total / annees

    # Volatilité annualisée
    mean_pf = sum(rets_pf) / len(rets_pf)
    var_pf = sum((r - mean_pf) ** 2 for r in rets_pf) / max(len(rets_pf) - 1, 1)
    vol_journaliere = math.sqrt(var_pf)
    volatilite_annualisee = vol_journaliere * math.sqrt(252)

    # Sharpe (taux sans risque = 0 pour simplifier)
    sharpe = (mean_pf * 252) / (vol_journaliere * math.sqrt(252)) if vol_journaliere else 0

    # Max drawdown
    peak = serie_pf[0]
    max_dd = 0.0
    for v in serie_pf:
        if v > peak:
            peak = v
        if peak:
            dd = (v - peak) / peak
            if dd < max_dd:
                max_dd = dd

    # Beta et alpha (régression linéaire simple vs indice)
    beta, alpha = None, None
    if rets_idx and len(rets_idx) == len(rets_pf):
        mean_idx = sum(rets_idx) / len(rets_idx)
        cov = sum((rets_pf[i] - mean_pf) * (rets_idx[i] - mean_idx) for i in range(len(rets_pf))) / max(len(rets_pf) - 1, 1)
        var_idx = sum((r - mean_idx) ** 2 for r in rets_idx) / max(len(rets_idx) - 1, 1)
        if var_idx > 0:
            beta = cov / var_idx
            # Alpha annualisé : excès de rendement non expliqué par beta
            try:
                rdt_idx_annualise = (1 + rendement_idx_total) ** (1 / annees) - 1
            except Exception:
                rdt_idx_annualise = rendement_idx_total / annees
            alpha = rendement_annualise - beta * rdt_idx_annualise

    # Taux de jours positifs
    jours_positifs = sum(1 for r in rets_pf if r > 0)
    win_rate = (jours_positifs / len(rets_pf)) * 100 if rets_pf else 0

    return {
        "rendement_total_pct": round(rendement_total * 100, 2),
        "rendement_annualise_pct": round(rendement_annualise * 100, 2),
        "rendement_idx_total_pct": round(rendement_idx_total * 100, 2),
        "volatilite_annualisee_pct": round(volatilite_annualisee * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "beta": round(beta, 2) if beta is not None else None,
        "alpha_annualise_pct": round(alpha * 100, 2) if alpha is not None else None,
        "win_rate_pct": round(win_rate, 1),
        "nb_jours": n,
        "date_debut": dates[0] if dates else None,
        "date_fin": dates[-1] if dates else None,
    }


def _compute_benchmark(portefeuille, indice_ticker=None):
    """Compare la performance du portefeuille vs un indice depuis la date du premier achat.

    Si ``indice_ticker`` est None, choisit BRVMC en priorité, sinon le premier
    indice contenant "BRVM" dans son ticker.
    """
    premiere_ligne = portefeuille.lignes.filter(active=True).order_by("date_achat").first()
    if not premiere_ligne:
        return None

    date_debut = premiere_ligne.date_achat

    indice = None
    if indice_ticker:
        indice = Indice.objects.filter(ticker=indice_ticker).first()
    if not indice:
        indice = Indice.objects.filter(ticker="BRVMC").first()
    if not indice:
        indice = Indice.objects.filter(ticker__icontains="BRVM").first()
    if not indice:
        return None

    # Cours de l'indice depuis la date de début
    hist_idx = list(HistoriqueIndice.objects.filter(
        indice=indice, date__gte=date_debut
    ).order_by("date").values_list("date", "cloture"))

    if not hist_idx:
        return None

    base_idx = next((c for _, c in hist_idx if c), None)
    if not base_idx:
        return None

    idx_dates = [h[0].strftime("%Y-%m-%d") for h in hist_idx]
    idx_perf = [round((h[1] / base_idx) * 100, 2) if h[1] else 100 for h in hist_idx]

    # Performance portefeuille : on calcule la valeur totale à chaque date.
    # IMPORTANT : ligne.date_achat est un objet date — on doit comparer à un date,
    # pas à une string. On garde donc les dates comme objets pour la boucle.
    lignes_actives = list(portefeuille.lignes.filter(active=True).select_related("action"))

    # Pré-charger l'historique de chaque action pour éviter N×M requêtes.
    actions_uniques = {l.action_id: l.action for l in lignes_actives}
    hist_par_action = {}
    for action_id, action in actions_uniques.items():
        rows = list(HistoriqueAction.objects.filter(
            action=action, date__gte=date_debut
        ).order_by("date").values_list("date", "cloture"))
        hist_par_action[action_id] = rows

    def _cours_a_la_date(action_id, ref_date):
        """Dernier cours connu pour cette action à une date <= ref_date."""
        rows = hist_par_action.get(action_id) or []
        dernier = None
        for d, c in rows:
            if d > ref_date:
                break
            if c:
                dernier = c
        return dernier

    pf_perf = []
    montant_init = portefeuille.montant_initial or 0

    for d_obj, _ in hist_idx:
        valeur = 0.0
        montant_inv = 0.0
        for ligne in lignes_actives:
            if ligne.date_achat <= d_obj:
                cours = _cours_a_la_date(ligne.action_id, d_obj)
                if cours:
                    valeur += cours * ligne.quantite
                else:
                    valeur += ligne.prix_achat * ligne.quantite
                montant_inv += ligne.prix_achat * ligne.quantite + (ligne.frais or 0)
        liquidite = montant_init - montant_inv
        valeur_totale = liquidite + valeur
        pf_perf.append(round((valeur_totale / montant_init) * 100, 2) if montant_init else 100)

    return {
        "dates": idx_dates,
        "indice_ticker": indice.ticker,
        "indice_nom": indice.nom or indice.ticker,
        "brvm": idx_perf,  # garde la clé "brvm" pour compatibilité front
        "portefeuille": pf_perf,
    }


@csrf_exempt
@require_http_methods(["POST"])
def api_portefeuille_creer(request):
    """Créer un nouveau portefeuille."""
    try:
        data = json.loads(request.body)
        nom = data.get("nom", "").strip()
        montant = data.get("montant_initial", 0)
        frais_pct = data.get("frais_courtage_pct", 1.0)

        if not nom:
            return JsonResponse({"error": "Le nom est requis"}, status=400)
        if montant <= 0:
            return JsonResponse({"error": "Le montant doit être positif"}, status=400)

        pf = Portefeuille.objects.create(
            nom=nom,
            montant_initial=montant,
            frais_courtage_pct=frais_pct,
        )
        return JsonResponse({"success": True, "id": pf.id, "nom": pf.nom})

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def api_portefeuille_supprimer(request, pf_id):
    """Supprimer un portefeuille."""
    try:
        pf = Portefeuille.objects.get(id=pf_id)
        pf.delete()
        return JsonResponse({"success": True})
    except Portefeuille.DoesNotExist:
        return JsonResponse({"error": "Portefeuille non trouvé"}, status=404)


@csrf_exempt
@require_http_methods(["POST"])
def api_portefeuille_ajouter_ligne(request):
    """Ajouter un investissement au portefeuille."""
    try:
        data = json.loads(request.body)
        pf_id = data.get("portefeuille_id")
        ticker = data.get("ticker")
        date_achat = data.get("date_achat")
        montant = float(data.get("montant", 0))

        pf = Portefeuille.objects.get(id=pf_id)
        action = Action.objects.get(ticker=ticker)

        # Trouver le prix à la date d'achat
        hist = HistoriqueAction.objects.filter(
            action=action, date=date_achat
        ).first()
        if not hist or not hist.cloture:
            return JsonResponse({
                "error": f"Pas de données de cours pour {ticker} à la date {date_achat}"
            }, status=400)

        prix_achat = hist.cloture
        quantite = int(montant // prix_achat)
        if quantite <= 0:
            return JsonResponse({
                "error": f"Montant insuffisant pour acheter au moins 1 titre de {ticker} à {prix_achat:,.0f} FCFA"
            }, status=400)

        montant_reel = prix_achat * quantite
        frais = montant_reel * (pf.frais_courtage_pct / 100)
        cout_total = montant_reel + frais

        # Vérifier la liquidité
        if cout_total > pf.liquidite:
            return JsonResponse({
                "error": f"Liquidité insuffisante. Disponible: {pf.liquidite:,.0f} FCFA, Requis: {cout_total:,.0f} FCFA"
            }, status=400)

        ligne = LignePortefeuille.objects.create(
            portefeuille=pf,
            action=action,
            quantite=quantite,
            prix_achat=prix_achat,
            date_achat=date_achat,
            frais=frais,
        )

        return JsonResponse({
            "success": True,
            "ligne": {
                "id": ligne.id,
                "ticker": action.ticker,
                "quantite": quantite,
                "prix_achat": prix_achat,
                "montant_investi": montant_reel,
                "frais": frais,
                "cout_total": cout_total,
            }
        })

    except Portefeuille.DoesNotExist:
        return JsonResponse({"error": "Portefeuille non trouvé"}, status=404)
    except Action.DoesNotExist:
        return JsonResponse({"error": "Action non trouvée"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
@require_http_methods(["POST"])
def api_portefeuille_supprimer_ligne(request, ligne_id):
    """Supprimer (vendre) une ligne du portefeuille."""
    try:
        ligne = LignePortefeuille.objects.get(id=ligne_id)
        ligne.active = False
        ligne.save()
        return JsonResponse({"success": True})
    except LignePortefeuille.DoesNotExist:
        return JsonResponse({"error": "Ligne non trouvée"}, status=404)


def api_portefeuille_dates_disponibles(request, ticker):
    """Retourne les dates disponibles pour un ticker donné."""
    dates = list(
        HistoriqueAction.objects.filter(
            action__ticker=ticker
        ).order_by("-date").values_list("date", flat=True)[:365]
    )
    return JsonResponse({
        "dates": [d.strftime("%Y-%m-%d") for d in dates]
    })


def api_portefeuille_prix(request, ticker, date):
    """Retourne le prix de clôture pour un ticker à une date donnée."""
    hist = HistoriqueAction.objects.filter(
        action__ticker=ticker, date=date
    ).first()
    if hist and hist.cloture:
        return JsonResponse({"prix": hist.cloture})
    return JsonResponse({"prix": None})


def api_portefeuille_comparer(request):
    """Compare plusieurs portefeuilles : retourne KPIs + courbe base 100 pour chacun.

    Query params: ?ids=1,2,3 (&indice=BRVMC pour l'overlay benchmark)
    """
    ids_param = request.GET.get("ids", "")
    indice_ticker = request.GET.get("indice")
    try:
        ids = [int(x) for x in ids_param.split(",") if x.strip()]
    except ValueError:
        return JsonResponse({"error": "ids invalides"}, status=400)
    if not ids:
        return JsonResponse({"error": "Aucun portefeuille sélectionné"}, status=400)

    portefeuilles_data = []
    for pf_id in ids:
        try:
            pf = Portefeuille.objects.get(id=pf_id)
        except Portefeuille.DoesNotExist:
            continue
        bench = _compute_benchmark(pf, indice_ticker=indice_ticker)
        if not bench:
            portefeuilles_data.append({
                "id": pf.id, "nom": pf.nom, "error": "Pas de positions ou pas d'historique",
            })
            continue
        kpis = _compute_kpis(bench["portefeuille"], bench["brvm"], bench["dates"])
        portefeuilles_data.append({
            "id": pf.id,
            "nom": pf.nom,
            "dates": bench["dates"],
            "courbe": bench["portefeuille"],
            "kpis": kpis,
        })

    # Indice de référence (commun à tous, on prend celui calculé sur le premier qui a une courbe)
    indice_ref = None
    for d in portefeuilles_data:
        if "courbe" in d:
            indice_ref = {
                "dates": d["dates"],
                "courbe": [],  # rempli ci-dessous
            }
            break
    # On récupère la courbe de l'indice depuis la 1re comparaison réussie
    if indice_ref:
        # Recalcul brut via _compute_benchmark sur le 1er portefeuille valable
        for pf_id in ids:
            try:
                pf = Portefeuille.objects.get(id=pf_id)
            except Portefeuille.DoesNotExist:
                continue
            bench = _compute_benchmark(pf, indice_ticker=indice_ticker)
            if bench:
                indice_ref = {
                    "ticker": bench["indice_ticker"],
                    "nom": bench["indice_nom"],
                    "dates": bench["dates"],
                    "courbe": bench["brvm"],
                }
                break

    return JsonResponse({
        "success": True,
        "portefeuilles": portefeuilles_data,
        "indice": indice_ref,
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_portefeuille_vendre(request):
    """Vendre une position (totale ou partielle)."""
    try:
        data = json.loads(request.body)
        ligne_id = data.get("ligne_id")
        quantite_vente = int(data.get("quantite", 0))
        date_vente = data.get("date_vente")

        ligne = LignePortefeuille.objects.select_related("portefeuille", "action").get(id=ligne_id)
        pf = ligne.portefeuille

        if quantite_vente <= 0:
            return JsonResponse({"error": "La quantité doit être positive"}, status=400)
        if quantite_vente > ligne.quantite:
            return JsonResponse({"error": f"Quantité max disponible: {ligne.quantite}"}, status=400)

        # Trouver le prix de vente à la date
        hist = HistoriqueAction.objects.filter(
            action=ligne.action, date=date_vente
        ).first()
        if not hist or not hist.cloture:
            return JsonResponse({
                "error": f"Pas de cours disponible pour {ligne.action.ticker} à la date {date_vente}"
            }, status=400)

        prix_vente = hist.cloture
        montant_brut = prix_vente * quantite_vente
        frais_vente = montant_brut * (pf.frais_courtage_pct / 100)
        montant_net = montant_brut - frais_vente

        # Calcul du P&L sur cette vente
        # PRU de cette ligne spécifique
        pru = ligne.prix_achat
        cout_achat_proportionnel = pru * quantite_vente
        frais_achat_proportionnel = (ligne.frais / ligne.quantite) * quantite_vente if ligne.quantite > 0 else 0
        cout_total_achat = cout_achat_proportionnel + frais_achat_proportionnel

        pnl_realise = montant_net - cout_total_achat
        pnl_pct = (pnl_realise / cout_total_achat * 100) if cout_total_achat > 0 else 0

        # Mettre à jour la ligne
        if quantite_vente == ligne.quantite:
            # Vente totale : désactiver la ligne
            ligne.active = False
            ligne.save()
        else:
            # Vente partielle : réduire la quantité et les frais proportionnellement
            ratio_restant = (ligne.quantite - quantite_vente) / ligne.quantite
            ligne.quantite = ligne.quantite - quantite_vente
            ligne.frais = ligne.frais * ratio_restant
            ligne.save()

        return JsonResponse({
            "success": True,
            "vente": {
                "ticker": ligne.action.ticker,
                "quantite": quantite_vente,
                "prix_vente": prix_vente,
                "montant_brut": round(montant_brut, 0),
                "frais_vente": round(frais_vente, 0),
                "montant_net": round(montant_net, 0),
                "pnl_realise": round(pnl_realise, 0),
                "pnl_pct": round(pnl_pct, 2),
            }
        })

    except LignePortefeuille.DoesNotExist:
        return JsonResponse({"error": "Ligne non trouvée"}, status=404)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


# ============================================================
# Stratégies — Allocation ponctuelle & Backtest
# ============================================================

# Libellés humains des stratégies HMM
_LIBELLES_STRATEGIES = {
    "SHARPE_HMM": "Maximum Sharpe",
    "DYN_HMM": "Allocation dynamique",
    "MR_HMM": "Maximum Return",
    "RP_HMM": "Risk Parity",
    "MD_HMM": "Maximum Diversification",
    "MV_HMM": "Minimum Variance",
}


def api_strategies_list(request):
    """Retourne la liste des allocations stratégie disponibles en BD."""
    allocs = AllocationStrategie.objects.order_by("-date", "strategie")[:50]
    data = []
    for a in allocs:
        pa = a.poids_actions or {}
        data.append({
            "id": a.id,
            "date": a.date.strftime("%Y-%m-%d"),
            "strategie": a.strategie,
            "libelle": _LIBELLES_STRATEGIES.get(a.strategie, a.strategie),
            "nb_actions": len(pa),
            "rendement_attendu_pct": round((a.rendement_attendu or 0) * 100, 2),
            "volatilite_attendue_pct": round((a.volatilite_attendue or 0) * 100, 2),
            "sharpe_attendu": round(a.sharpe_attendu or 0, 2),
            "top_3": sorted(pa.items(), key=lambda x: -x[1])[:3],
        })
    return JsonResponse({"success": True, "allocations": data})


def api_strategie_detail(request, alloc_id):
    """Détail d'une allocation : poids par ticker."""
    try:
        a = AllocationStrategie.objects.get(id=alloc_id)
    except AllocationStrategie.DoesNotExist:
        return JsonResponse({"error": "Allocation non trouvée"}, status=404)
    pa = a.poids_actions or {}
    return JsonResponse({
        "success": True,
        "id": a.id,
        "date": a.date.strftime("%Y-%m-%d"),
        "strategie": a.strategie,
        "libelle": _LIBELLES_STRATEGIES.get(a.strategie, a.strategie),
        "poids_actions": [
            {"ticker": t, "poids": p, "poids_pct": round(p * 100, 2)}
            for t, p in sorted(pa.items(), key=lambda x: -x[1])
        ],
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_strategie_appliquer(request):
    """Applique une (ou plusieurs) allocation(s) à un portefeuille existant.

    Body JSON :
    {
      portefeuille_id: int,
      allocations: [{id: int, poids_global: float}, ...],  // poids_global = part du montant
      montant_total: float,                                 // FCFA à allouer
      date_achat: "YYYY-MM-DD"
    }

    Comportement : pour chaque allocation × poids_global, on calcule pour chaque
    ticker le montant = montant_total × poids_global × poids_action, puis on
    achète au cours de la date_achat (entier inférieur de titres). Plusieurs
    allocations sur le même ticker s'additionnent.
    """
    try:
        data = json.loads(request.body)
        pf_id = data.get("portefeuille_id")
        allocations = data.get("allocations", [])
        montant_total = float(data.get("montant_total", 0))
        date_achat = data.get("date_achat")

        if not pf_id or not allocations or montant_total <= 0 or not date_achat:
            return JsonResponse({"error": "Paramètres manquants"}, status=400)

        pf = Portefeuille.objects.get(id=pf_id)
        if montant_total > pf.liquidite:
            return JsonResponse({
                "error": f"Liquidité insuffisante. Disponible: {pf.liquidite:,.0f} FCFA, demandé: {montant_total:,.0f}"
            }, status=400)

        # Agréger les poids cibles {ticker: poids_final}
        poids_cibles = defaultdict(float)
        for entry in allocations:
            alloc = AllocationStrategie.objects.get(id=int(entry["id"]))
            poids_global = float(entry.get("poids_global", 1.0))
            for ticker, w in (alloc.poids_actions or {}).items():
                poids_cibles[ticker] += poids_global * float(w)

        # Acheter chaque ticker
        achats = []
        echecs = []
        cout_engage = 0.0
        for ticker, poids_final in poids_cibles.items():
            montant_alloc = montant_total * poids_final
            if montant_alloc <= 0:
                continue
            try:
                action = Action.objects.get(ticker=ticker)
            except Action.DoesNotExist:
                echecs.append({"ticker": ticker, "raison": "Action inconnue"})
                continue
            hist = HistoriqueAction.objects.filter(
                action=action, date=date_achat
            ).first()
            if not hist or not hist.cloture:
                # Fallback : dernier cours <= date_achat
                hist = HistoriqueAction.objects.filter(
                    action=action, date__lte=date_achat
                ).order_by("-date").first()
            if not hist or not hist.cloture:
                echecs.append({"ticker": ticker, "raison": "Pas de cours disponible"})
                continue
            prix = hist.cloture
            quantite = int(montant_alloc // prix)
            if quantite <= 0:
                echecs.append({"ticker": ticker, "raison": f"Montant alloué insuffisant ({montant_alloc:,.0f} < {prix:,.0f})"})
                continue
            montant_reel = prix * quantite
            frais = montant_reel * (pf.frais_courtage_pct / 100)
            cout_total = montant_reel + frais
            if cout_engage + cout_total > pf.liquidite:
                echecs.append({"ticker": ticker, "raison": "Liquidité épuisée"})
                continue
            cout_engage += cout_total
            LignePortefeuille.objects.create(
                portefeuille=pf,
                action=action,
                quantite=quantite,
                prix_achat=prix,
                date_achat=hist.date,
                frais=frais,
            )
            achats.append({
                "ticker": ticker,
                "quantite": quantite,
                "prix": prix,
                "montant": montant_reel,
                "frais": frais,
                "poids_cible": round(poids_final * 100, 2),
            })

        return JsonResponse({
            "success": True,
            "achats": achats,
            "echecs": echecs,
            "cout_engage": round(cout_engage, 0),
            "liquidite_restante": round(pf.liquidite - cout_engage, 0),
        })

    except Portefeuille.DoesNotExist:
        return JsonResponse({"error": "Portefeuille non trouvé"}, status=404)
    except AllocationStrategie.DoesNotExist:
        return JsonResponse({"error": "Allocation non trouvée"}, status=404)
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=500)


def api_strategie_backtest(request):
    """Backtest buy-and-hold d'une allocation entre 2 dates, avec intégration
    des dividendes et des frais de transaction (achat + cession finale).

    Query params:
      ?alloc_id=<int>&date_debut=YYYY-MM-DD&date_fin=YYYY-MM-DD&montant=<float>
      &indice=<ticker>            (optionnel, défaut BRVMC)
      &frais_pct=<float>          (optionnel, % aller ET retour ; défaut 1.0)
      &inclure_dividendes=0|1     (optionnel, défaut 1)

    Modèle économique :
    - Achat à date_debut : frais_pct% appliqués sur le coût brut.
    - Détention : les dividendes détachés pendant la période sont crédités
      en cash sur la date de détachement (annuels approximés au 30 juin).
    - Cession à date_fin : frais_pct% appliqués sur la valeur de liquidation,
      reflétés dans le dernier point de la courbe.

    Retourne la courbe de valeur du portefeuille (base 100) vs indice + KPIs.
    """
    try:
        alloc_id = int(request.GET.get("alloc_id"))
        date_debut = request.GET.get("date_debut")
        date_fin = request.GET.get("date_fin")
        montant = float(request.GET.get("montant", 10_000_000))
        indice_ticker = request.GET.get("indice") or "BRVMC"
        frais_pct = float(request.GET.get("frais_pct", 1.0))
        inclure_dividendes = request.GET.get("inclure_dividendes", "1") not in ("0", "false", "False", "")
    except (TypeError, ValueError):
        return JsonResponse({"error": "Paramètres invalides"}, status=400)

    if frais_pct < 0:
        frais_pct = 0.0

    try:
        alloc = AllocationStrategie.objects.get(id=alloc_id)
    except AllocationStrategie.DoesNotExist:
        return JsonResponse({"error": "Allocation non trouvée"}, status=404)

    poids_actions = alloc.poids_actions or {}
    if not poids_actions:
        return JsonResponse({"error": "Cette allocation n'a pas de poids actions"}, status=400)

    try:
        date_debut_obj = datetime.strptime(date_debut, "%Y-%m-%d").date()
        date_fin_obj = datetime.strptime(date_fin, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return JsonResponse({"error": "Dates invalides"}, status=400)

    # Étape 1 — Construire le portefeuille à date_debut (buy)
    actions_map = {a.ticker: a for a in Action.objects.filter(ticker__in=poids_actions.keys())}

    positions = []  # list of (action, quantite, prix_achat, frais_achat)
    cout_initial = 0.0
    frais_entree_total = 0.0
    achats_detail = []
    for ticker, poids in poids_actions.items():
        action = actions_map.get(ticker)
        if not action:
            continue
        hist = HistoriqueAction.objects.filter(
            action=action, date__lte=date_debut
        ).order_by("-date").first()
        if not hist or not hist.cloture:
            continue
        montant_cible = montant * poids
        # On dimensionne en intégrant les frais : qte * prix * (1 + f) <= budget
        denom = hist.cloture * (1 + frais_pct / 100)
        qte = int(montant_cible // denom) if denom > 0 else 0
        if qte <= 0:
            continue
        cout_brut = qte * hist.cloture
        frais = cout_brut * (frais_pct / 100)
        positions.append((action, qte, hist.cloture, frais))
        cout_initial += cout_brut + frais
        frais_entree_total += frais
        achats_detail.append({
            "ticker": ticker,
            "quantite": qte,
            "prix_achat": hist.cloture,
            "cout": cout_brut,
            "frais": frais,
            "poids_cible_pct": round(poids * 100, 2),
        })

    liquidite_residuelle = montant - cout_initial
    if not positions:
        return JsonResponse({"error": "Aucune position constituée (cours indisponibles)"}, status=400)

    # Étape 2 — Récupérer la grille de dates depuis l'indice de référence
    indice = Indice.objects.filter(ticker=indice_ticker).first()
    if not indice:
        indice = Indice.objects.filter(ticker="BRVMC").first()
    if not indice:
        return JsonResponse({"error": "Aucun indice de référence trouvé"}, status=400)

    hist_idx = list(HistoriqueIndice.objects.filter(
        indice=indice, date__gte=date_debut, date__lte=date_fin
    ).order_by("date").values_list("date", "cloture"))
    if not hist_idx:
        return JsonResponse({"error": "Pas d'historique indice sur la période"}, status=400)

    # Étape 3 — Précharger les historiques des actions du portefeuille
    hist_par_action = {}
    for action, _, _, _ in positions:
        rows = list(HistoriqueAction.objects.filter(
            action=action, date__gte=date_debut, date__lte=date_fin
        ).order_by("date").values_list("date", "cloture"))
        hist_par_action[action.id] = rows

    def _cours_a_la_date(action_id, ref_date):
        rows = hist_par_action.get(action_id) or []
        dernier = None
        for d, c in rows:
            if d > ref_date:
                break
            if c:
                dernier = c
        return dernier

    # Étape 4 — Précharger les dividendes par action (si activés)
    dividendes_par_action = {}
    if inclure_dividendes:
        for action, _, _, _ in positions:
            dividendes_par_action[action.id] = _dividendes_dates_for_ticker(action.ticker)

    # Étape 5 — Courbe de valeur du portefeuille avec crédit des dividendes en cash
    dates = []
    courbe_pf = []
    cash_courant = liquidite_residuelle
    dividendes_total = 0.0
    prev_d = date_debut_obj  # bornage : on crédite les divs détachés *après* l'achat
    for d_obj, _ in hist_idx:
        # Crédit des dividendes tombés entre prev_d (exclusif) et d_obj (inclusif)
        for action, qte, _, _ in positions:
            for div_date, montant in (dividendes_par_action.get(action.id) or {}).items():
                if prev_d < div_date <= d_obj:
                    credit = qte * montant
                    cash_courant += credit
                    dividendes_total += credit
        prev_d = d_obj

        valeur = cash_courant
        for action, qte, prix_achat, _ in positions:
            cours = _cours_a_la_date(action.id, d_obj) or prix_achat
            valeur += cours * qte
        dates.append(d_obj.strftime("%Y-%m-%d"))
        courbe_pf.append(round((valeur / montant) * 100, 2) if montant else 100)

    # Étape 6 — Frais de cession sur le dernier point (liquidation à date_fin)
    last_d_obj = hist_idx[-1][0]
    valeur_titres_fin = 0.0
    for action, qte, prix_achat, _ in positions:
        cours = _cours_a_la_date(action.id, last_d_obj) or prix_achat
        valeur_titres_fin += cours * qte
    frais_sortie = valeur_titres_fin * (frais_pct / 100)
    frais_total = frais_entree_total + frais_sortie

    # Reflet de la cession : on ampute le dernier point du coût de liquidation
    if courbe_pf and montant:
        courbe_pf[-1] = round(courbe_pf[-1] - (frais_sortie / montant) * 100, 2)

    base_idx = next((c for _, c in hist_idx if c), None) or 1
    courbe_idx = [round((c / base_idx) * 100, 2) if c else 100 for _, c in hist_idx]

    kpis = _compute_kpis(courbe_pf, courbe_idx, dates)
    if kpis:
        kpis["ecart_vs_indice_pct"] = round(
            kpis["rendement_total_pct"] - kpis["rendement_idx_total_pct"], 2
        )

    return JsonResponse({
        "success": True,
        "alloc": {
            "id": alloc.id,
            "date": alloc.date.strftime("%Y-%m-%d"),
            "strategie": alloc.strategie,
            "libelle": _LIBELLES_STRATEGIES.get(alloc.strategie, alloc.strategie),
        },
        "dates": dates,
        "courbe_portefeuille": courbe_pf,
        "courbe_indice": courbe_idx,
        "indice_ticker": indice.ticker,
        "kpis": kpis,
        "montant_initial": montant,
        "cout_initial": round(cout_initial, 0),
        "liquidite_residuelle": round(liquidite_residuelle, 0),
        "nb_positions": len(positions),
        "achats": achats_detail,
        "frais_pct": frais_pct,
        "frais_entree": round(frais_entree_total, 0),
        "frais_sortie": round(frais_sortie, 0),
        "frais_total": round(frais_total, 0),
        "inclure_dividendes": inclure_dividendes,
        "dividendes_total": round(dividendes_total, 0),
    })


# ============================================================
# API Indicateurs Cachés & Changements de Signaux
# ============================================================

def api_indicateurs_cache(request, ticker):
    """Retourne les indicateurs précalculés par l'agent pour une action.
    Si le cache existe, le retourne directement. Sinon, calcule à la volée."""
    try:
        action = Action.objects.get(ticker=ticker)
    except Action.DoesNotExist:
        return JsonResponse({"error": "Action introuvable"}, status=404)

    from dashboard.services import get_indicateurs_cache, calculer_indicateurs_action

    cache = get_indicateurs_cache(action)
    if cache:
        # Dernier signal pour cette action
        dernier_signal = TradingSignal.objects.filter(action=action).first()
        signal_info = None
        if dernier_signal:
            signal_info = {
                "signal": dernier_signal.signal,
                "confiance": dernier_signal.confiance,
                "prix_entree": dernier_signal.prix_entree,
                "stop_loss": dernier_signal.stop_loss,
                "take_profit": dernier_signal.take_profit,
                "risk_reward": dernier_signal.risk_reward,
                "justification": dernier_signal.justification,
                "date": dernier_signal.date_generation.isoformat(),
                "modele": dernier_signal.modele_ia,
                "supports": dernier_signal.supports,
                "resistances": dernier_signal.resistances,
            }

        return JsonResponse({
            "source": "cache",
            "ticker": ticker,
            "date_calcul": cache["date_calcul"],
            "indicateurs": cache["indicateurs"],
            "signal": signal_info,
        })

    # Pas de cache — calcul à la volée
    indics = calculer_indicateurs_action(action)
    if not indics:
        return JsonResponse({"error": "Données insuffisantes"}, status=400)

    return JsonResponse({
        "source": "calcul_live",
        "ticker": ticker,
        "date_calcul": timezone.now().isoformat(),
        "indicateurs": indics,
        "signal": None,
    })


def api_signal_changements(request, ticker):
    """Retourne l'historique des changements de **verdict 4-axes** pour une action.

    Source : SignalHistorique (modalités Sikafinance : Acheter, Renforcer,
    Conserver, Alléger, Vendre). Cohérent avec le verdict synthétique affiché
    dans le bandeau de la page. Un "changement" = date où le code diffère de
    la veille (de la séance précédente disponible).
    """
    try:
        action = Action.objects.get(ticker=ticker)
    except Action.DoesNotExist:
        return JsonResponse({"error": "Action introuvable"}, status=404)

    limit = int(request.GET.get("limit", 20))

    # On parcourt l'historique ordonné par date croissante, on détecte les
    # transitions, puis on renvoie les `limit` plus récentes.
    rows = list(
        SignalHistorique.objects
        .filter(action=action)
        .order_by("date")
        .values("date", "code", "label", "score")
    )

    transitions = []
    prev = None
    for r in rows:
        if prev is not None and r["code"] != prev["code"]:
            transitions.append({
                "ancien_signal": prev["code"],
                "ancien_label": prev["label"],
                "nouveau_signal": r["code"],
                "nouveau_label": r["label"],
                "ancien_score": prev["score"],
                "nouveau_score": r["score"],
                "date": r["date"].isoformat(),
            })
        prev = r

    # Plus récents en premier, limités
    transitions.reverse()
    data = transitions[:limit]

    # Signal actuel = dernière ligne SignalHistorique
    signal_actuel = None
    if rows:
        last = rows[-1]
        signal_actuel = {
            "signal": last["code"],
            "label": last["label"],
            "score": last["score"],
            "date": last["date"].isoformat(),
        }

    return JsonResponse({
        "ticker": ticker,
        "signal_actuel": signal_actuel,
        "changements": data,
        "total": len(data),
        "total_transitions": len(transitions),
    })
