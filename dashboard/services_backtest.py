"""Backtest rétrospectif des verdicts 4-axes.

Calcule en RAM les métriques par code de verdict (hit rate directionnel,
rendement moyen, Sharpe) et la PnL cumulée d'une stratégie long-only sur
ACHETER+RENFORCER, à partir des ``SignalHistorique`` et ``HistoriqueAction``
d'une action.

Hypothèses :
- Hit rate **directionnel** :
    BULL (ACHETER, RENFORCER) hit si rendement forward > 0
    BEAR (ALLEGER, VENDRE)    hit si rendement forward < 0
    CONSERVER : exclu du hit rate (signal neutre)
- Rendements forward calculés sur clôture-à-clôture (close[t+h] / close[t] - 1).
- Sharpe annualisé sur l'horizon 22j, rf=0, facteur √(252/22).
- PnL cumulée long-only : si verdict ∈ BULL on est long du jour t au jour t+1
  (rebalancement quotidien à la clôture), sinon cash. Frais en bps appliqués
  sur chaque changement d'état (entrée/sortie).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import math

from dashboard.models import (
    Action, HistoriqueAction, SignalHistorique, ConseilSikafinance,
)


BULL_CODES = {"ACHETER", "RENFORCER"}
BEAR_CODES = {"ALLEGER", "VENDRE"}
NEUTRE_CODES = {"CONSERVER"}
TOUS_CODES = ["ACHETER", "RENFORCER", "CONSERVER", "ALLEGER", "VENDRE"]

HORIZONS = (1, 5, 22)
SHARPE_HORIZON = 22
TRADING_DAYS_PER_YEAR = 252


@dataclass
class MetriquesParCode:
    code: str
    label: str
    n: int = 0
    hit_rate_1j: Optional[float] = None
    hit_rate_5j: Optional[float] = None
    hit_rate_22j: Optional[float] = None
    rdt_moyen_1j: Optional[float] = None
    rdt_moyen_5j: Optional[float] = None
    rdt_moyen_22j: Optional[float] = None
    rdt_median_22j: Optional[float] = None
    sharpe_annualise: Optional[float] = None


CODE_LABELS = {
    "ACHETER": "Acheter",
    "RENFORCER": "Renforcer",
    "CONSERVER": "Conserver",
    "ALLEGER": "Alléger",
    "VENDRE": "Vendre",
}


def _hit_directionnel(code: str, rdt: float) -> Optional[bool]:
    """Renvoie True si le signal est dans le bon sens, False sinon, None si exclu."""
    if code in BULL_CODES:
        return rdt > 0
    if code in BEAR_CODES:
        return rdt < 0
    return None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    if n % 2 == 1:
        return xs[n // 2]
    return (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var) if var > 0 else 0.0


def _rate_true(bools):
    bools = [b for b in bools if b is not None]
    if not bools:
        return None
    return sum(1 for b in bools if b) / len(bools)


def _join_signals_with_closes(action: Action) -> Tuple[List[dict], Dict[str, float]]:
    """Joint SignalHistorique avec HistoriqueAction pour calculer les rendements forward.

    Retourne (rows, closes_by_date_iso) où chaque row contient :
      {date, code, score, rdt_1j, rdt_5j, rdt_22j}.
    """
    closes = list(
        HistoriqueAction.objects
        .filter(action=action, cloture__isnull=False)
        .order_by("date")
        .values_list("date", "cloture")
    )
    # Map date -> close pour lookup O(1)
    closes_map: Dict[str, float] = {d.isoformat(): float(c) for d, c in closes}
    # Liste ordonnée des dates pour navigation index-based
    dates_sorted = [d.isoformat() for d, _ in closes]
    idx_by_date = {d: i for i, d in enumerate(dates_sorted)}

    signaux = list(
        SignalHistorique.objects
        .filter(action=action)
        .order_by("date")
        .values("date", "code", "score")
    )

    rows = []
    for s in signaux:
        d_iso = s["date"].isoformat()
        i = idx_by_date.get(d_iso)
        if i is None:
            continue
        c0 = closes_map.get(d_iso)
        if c0 is None or c0 <= 0:
            continue
        row = {"date": d_iso, "code": s["code"], "score": s["score"]}
        for h in HORIZONS:
            j = i + h
            if j < len(dates_sorted):
                ch = closes_map.get(dates_sorted[j])
                row[f"rdt_{h}j"] = (ch / c0 - 1.0) if (ch and ch > 0) else None
            else:
                row[f"rdt_{h}j"] = None
        rows.append(row)
    return rows, closes_map


def metriques_par_code(rows: List[dict]) -> List[MetriquesParCode]:
    """Agrège les rows par code de verdict."""
    out: List[MetriquesParCode] = []
    for code in TOUS_CODES:
        subset = [r for r in rows if r["code"] == code]
        m = MetriquesParCode(code=code, label=CODE_LABELS[code], n=len(subset))
        if not subset:
            out.append(m)
            continue

        # Hit rates par horizon
        m.hit_rate_1j = _rate_true([_hit_directionnel(code, r["rdt_1j"])
                                    for r in subset if r["rdt_1j"] is not None])
        m.hit_rate_5j = _rate_true([_hit_directionnel(code, r["rdt_5j"])
                                    for r in subset if r["rdt_5j"] is not None])
        m.hit_rate_22j = _rate_true([_hit_directionnel(code, r["rdt_22j"])
                                     for r in subset if r["rdt_22j"] is not None])

        # Rendements moyens
        m.rdt_moyen_1j = _mean([r["rdt_1j"] for r in subset])
        m.rdt_moyen_5j = _mean([r["rdt_5j"] for r in subset])
        m.rdt_moyen_22j = _mean([r["rdt_22j"] for r in subset])
        m.rdt_median_22j = _median([r["rdt_22j"] for r in subset])

        # Sharpe annualisé sur fwd 22j
        rdts22 = [r["rdt_22j"] for r in subset if r["rdt_22j"] is not None]
        if len(rdts22) >= 5:
            mean22 = sum(rdts22) / len(rdts22)
            sd22 = _stdev(rdts22)
            if sd22 and sd22 > 0:
                annualisation = math.sqrt(TRADING_DAYS_PER_YEAR / SHARPE_HORIZON)
                m.sharpe_annualise = (mean22 / sd22) * annualisation

        out.append(m)
    return out


def pnl_cumulee_long_only(
    rows: List[dict],
    frais_bps: int = 0,
) -> Tuple[List[str], List[float], Dict]:
    """PnL cumulée d'une stratégie long-only sur ACHETER+RENFORCER.

    Long du jour t au jour t+1 si code ∈ BULL, cash sinon. Rebalancement quotidien.
    Frais en bps appliqués sur chaque changement d'état (paid as drag, pas
    bid/ask spread).

    Retourne (dates, equity_curve, summary) où equity_curve commence à 1.0.
    """
    if not rows:
        return [], [], {}
    dates = [r["date"] for r in rows]
    rdts = [r["rdt_1j"] for r in rows]
    codes = [r["code"] for r in rows]

    equity = 1.0
    curve = []
    en_position = False
    nb_trades = 0
    frais_total = 0.0
    frais_pct = frais_bps / 10000.0

    for r, code in zip(rdts, codes):
        # Décision basée sur le code du jour, appliquée au rendement vers t+1
        long_today = code in BULL_CODES
        # Frais si changement d'état
        if long_today != en_position:
            equity *= (1.0 - frais_pct)
            frais_total += frais_pct
            nb_trades += 1
            en_position = long_today
        # Rendement
        if r is not None and long_today:
            equity *= (1.0 + r)
        curve.append(equity)

    # Sortie position finale (frais)
    if en_position and frais_pct > 0:
        equity *= (1.0 - frais_pct)
        if curve:
            curve[-1] = equity
        nb_trades += 1

    summary = {
        "equity_finale": equity,
        "rdt_total_pct": (equity - 1.0) * 100.0,
        "nb_trades": nb_trades,
        "frais_total_pct": frais_total * 100.0,
        "nb_jours": len(curve),
    }
    # Sharpe global de la courbe (rdt journaliers)
    daily_rets = []
    prev = 1.0
    for v in curve:
        if prev > 0:
            daily_rets.append(v / prev - 1.0)
        prev = v
    if len(daily_rets) >= 30:
        m = sum(daily_rets) / len(daily_rets)
        sd = _stdev(daily_rets)
        if sd and sd > 0:
            summary["sharpe_annualise"] = m / sd * math.sqrt(TRADING_DAYS_PER_YEAR)
    return dates, curve, summary


def matrice_confusion_vs_sika(action: Action) -> Optional[Dict]:
    """Matrice de confusion technique vs Sikafinance (sur les dates où on a les deux).

    Comme on commence tout juste à logger Sika quotidiennement, cette matrice
    sera vide tant qu'on n'a pas accumulé plusieurs snapshots. Retourne None
    s'il y a moins de 1 paire (action, date) commune.
    """
    sika_qs = ConseilSikafinance.objects.filter(action=action).values("date_scrape", "code")
    sika_map = {s["date_scrape"]: s["code"] for s in sika_qs}
    if not sika_map:
        return None

    tech_qs = SignalHistorique.objects.filter(
        action=action, date__in=list(sika_map.keys()),
    ).values("date", "code")

    # Matrice : lignes = code technique, colonnes = code Sika
    codes_tech = TOUS_CODES + ["NA"]
    codes_sika = TOUS_CODES + ["INCONNU"]
    matrice = {ct: {cs: 0 for cs in codes_sika} for ct in codes_tech}
    n_total = 0
    n_concordance = 0
    n_div_forte = 0
    for t in tech_qs:
        ct = t["code"] if t["code"] in codes_tech else "NA"
        cs = sika_map[t["date"]] if sika_map[t["date"]] in codes_sika else "INCONNU"
        matrice[ct][cs] += 1
        n_total += 1
        if ct == cs:
            n_concordance += 1
        if (ct in BULL_CODES and cs in BEAR_CODES) or (ct in BEAR_CODES and cs in BULL_CODES):
            n_div_forte += 1

    if n_total == 0:
        return None
    return {
        "matrice": matrice,
        "codes_tech": codes_tech,
        "codes_sika": codes_sika,
        "n_total": n_total,
        "taux_concordance": n_concordance / n_total,
        "taux_divergence_forte": n_div_forte / n_total,
    }


def backtest_action(action: Action) -> Dict:
    """Point d'entrée principal : retourne un dict prêt pour le template."""
    rows, _ = _join_signals_with_closes(action)
    if not rows:
        return {
            "disponible": False,
            "raison": "Aucun SignalHistorique pour cette action. Lance backfill_signals.",
        }

    metriques = metriques_par_code(rows)
    dates_pnl, pnl_curve, pnl_summary = pnl_cumulee_long_only(rows, frais_bps=0)
    _, pnl_curve_frais, pnl_summary_frais = pnl_cumulee_long_only(rows, frais_bps=100)
    confusion = matrice_confusion_vs_sika(action)

    # Sous-échantillonnage pour le chart si très long (> ~1500 points)
    def _subsample(xs, target=1200):
        if len(xs) <= target:
            return xs
        step = math.ceil(len(xs) / target)
        return xs[::step]

    return {
        "disponible": True,
        "n_signaux": len(rows),
        "date_debut": rows[0]["date"],
        "date_fin": rows[-1]["date"],
        "metriques": metriques,
        "pnl": {
            "dates": _subsample(dates_pnl),
            "sans_frais": _subsample(pnl_curve),
            "avec_frais_100bps": _subsample(pnl_curve_frais),
            "summary_sans_frais": pnl_summary,
            "summary_avec_frais": pnl_summary_frais,
        },
        "confusion_sika": confusion,
    }
