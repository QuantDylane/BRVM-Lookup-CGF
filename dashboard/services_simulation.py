"""Simulateur de portefeuille event-driven sur les verdicts 4-axes BRVM.

Cadre simulé
------------
- Cash initial configurable en FCFA (défaut 1 000 000).
- Décisions discrètes sur changements de verdict (option B) :
    ACHETER   → achat agressif : utilise tout le cash dispo (modulé GARCH).
    RENFORCER → achat modéré : utilise 50% du cash dispo (modulé GARCH).
    CONSERVER → aucun ordre.
    ALLEGER   → vente de 50% des parts détenues (NON modulé GARCH).
    VENDRE    → vente de toutes les parts détenues (NON modulé GARCH).
- **Une transaction par changement de signal** : si J et J-1 portent le même code,
  aucun ordre. Le changement de modalité réinitialise le droit d'agir.
- GARCH modère la TAILLE des achats uniquement (option β) : facteur ∈ [0,1]
  calculé sur le percentile rolling 252j de σ̂(t+h_référence) via
  ``services_garch_forecast.garch_size_factor``.
- Settlement **T+2** BRVM : cash issu d'une vente n'est disponible qu'à
  J+2 ouvrés. Un achat débite immédiatement le cash dispo. Si insuffisant
  → ordre annulé (l'utilisateur "passe son tour"), pas de file d'attente.
- Pas de short, pas de fractional shares (BRVM exige des parts entières).
- Frais configurables au taux unitaire (achat ET vente), prélevés sur le cash.
- Dividendes encaissés en cash à la date de détachement quand l'action est
  détenue. Les dividendes historiques (annuels, sans date exacte) sont
  approximés au **30 juin** de l'année concernée, faute de date stockée.

Sorties
-------
- Courbe "Stratégie" : valeur portefeuille = cash + parts × cours, jour par jour.
- Courbe "Buy & Hold" : tout le cash investi à t=0 en parts entières,
  dividendes encaissés, jamais rebalancé.
- Courbe "Cash" : cash initial figé (référence "ne rien faire").
- Tableau de transactions chronologique avec raison du signal.
- Tableau résumé : valeur finale, P&L FCFA et %, # ordres exécutés/refusés,
  frais payés, dividendes encaissés, max drawdown, Sharpe annualisé.

Hypothèses simplificatrices (à connaître)
-----------------------------------------
- Exécution à la **clôture du jour du signal** (proxy raisonnable BRVM intraday non dispo).
- Pas de bid/ask spread (les frais 100bps englobent ce coût).
- Pas de garde, pas de fiscalité (IRVM 12% non décomptée — TODO si besoin).
- Reproductibilité : pas de RNG, fonctions déterministes.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from django.conf import settings

from dashboard.models import Action, HistoriqueAction, SignalHistorique
from dashboard.services_garch_fit import ensure_monthly_fits, fit_for_date
from dashboard.services_garch_forecast import (
    DEFAULT_HORIZONS,
    forecast_for_action,
    forecast_vol_pct_annuelle_from_fit,
    garch_size_factor,
)


# ---------------------------------------------------------------------------
# Constantes & types
# ---------------------------------------------------------------------------

DEFAULT_CASH = 1_000_000.0  # FCFA
DEFAULT_FRAIS_PCT = 0.01    # 1% achat + 1% vente (configurables)
DEFAULT_GARCH_HORIZON = 5   # j ouvrés
SETTLEMENT_DAYS = 2          # T+2 BRVM
TRADING_DAYS_PER_YEAR = 252
BULL_CODES = {"ACHETER", "RENFORCER"}
BEAR_CODES = {"ALLEGER", "VENDRE"}


@dataclass
class Transaction:
    date: str            # ISO YYYY-MM-DD
    type: str            # 'ACHAT' | 'VENTE' | 'DIVIDENDE' | 'ANNULE'
    code_signal: str     # verdict qui a déclenché (ou '—' pour dividendes)
    parts: int           # parts achetées (+) / vendues (-) / 0 pour dividende
    prix: Optional[float]
    montant_brut: float  # parts × prix (positif achat, négatif vente)
    frais: float
    cash_apres: float
    parts_apres: int
    valeur_apres: float  # cash + parts × cours
    raison: str          # explication courte


@dataclass
class Resume:
    valeur_initiale: float
    valeur_finale: float
    pnl_fcfa: float
    pnl_pct: float
    nb_ordres_executes: int
    nb_ordres_annules: int
    frais_total: float
    dividendes_total: float
    max_drawdown_pct: float
    sharpe_annualise: Optional[float]


@dataclass
class SimulationResult:
    disponible: bool
    raison_indispo: Optional[str]
    config: dict
    dates: List[str]
    valeur_strategie: List[float]
    valeur_buy_hold: List[float]
    valeur_cash: List[float]
    transactions: List[Transaction]
    resume_strategie: Optional[Resume]
    resume_buy_hold: Optional[Resume]
    resume_cash: Optional[Resume]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _max_drawdown_pct(curve: Sequence[float]) -> float:
    """Max drawdown en % (valeur positive). 0 si la courbe est monotone croissante."""
    if not curve:
        return 0.0
    peak = curve[0]
    dd = 0.0
    for v in curve:
        if v > peak:
            peak = v
        if peak > 0:
            cur_dd = (peak - v) / peak
            if cur_dd > dd:
                dd = cur_dd
    return dd * 100.0


def _sharpe_annualise(curve: Sequence[float]) -> Optional[float]:
    """Sharpe annualisé sur rendements journaliers (rf=0). None si < 30 points."""
    if len(curve) < 30:
        return None
    rets = []
    prev = curve[0]
    for v in curve[1:]:
        if prev > 0:
            rets.append(v / prev - 1.0)
        prev = v
    if len(rets) < 30:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    return (m / math.sqrt(var)) * math.sqrt(TRADING_DAYS_PER_YEAR)


def _resume(curve: Sequence[float], valeur_initiale: float,
            nb_executes: int = 0, nb_annules: int = 0,
            frais_total: float = 0.0, divs_total: float = 0.0) -> Resume:
    valeur_finale = curve[-1] if curve else valeur_initiale
    pnl = valeur_finale - valeur_initiale
    pct = (pnl / valeur_initiale * 100.0) if valeur_initiale > 0 else 0.0
    return Resume(
        valeur_initiale=valeur_initiale,
        valeur_finale=valeur_finale,
        pnl_fcfa=pnl,
        pnl_pct=pct,
        nb_ordres_executes=nb_executes,
        nb_ordres_annules=nb_annules,
        frais_total=frais_total,
        dividendes_total=divs_total,
        max_drawdown_pct=_max_drawdown_pct(curve),
        sharpe_annualise=_sharpe_annualise(curve),
    )


# ---------------------------------------------------------------------------
# Chargement données : signaux, cours, dividendes
# ---------------------------------------------------------------------------

def _load_dividendes_dates(ticker: str) -> Dict[date, float]:
    """Charge tous les dividendes datés pour un ticker.

    Pour les `a_venir` : date exacte si présente (Date_Detachement ISO).
    Pour `historique` (annuels) : approximation au 30 juin de l'année concernée.

    Renvoie un dict {date: montant_par_action_fcfa}. Si plusieurs entrées
    tombent sur la même date, on les additionne.
    """
    out: Dict[date, float] = {}
    if not ticker:
        return out
    path = Path(settings.BASE_DIR) / "data" / "dividendes" / "dividendes.json"
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, ValueError):
        return out

    tk = ticker.strip().lower()

    def _match(item):
        return str(item.get("Ticker", "")).strip().lower() == tk

    # À venir : dates exactes
    for item in payload.get("a_venir", []) or []:
        if not _match(item):
            continue
        date_str = item.get("Date_Detachement") or ""
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        try:
            montant = float(item.get("Montant_FCFA") or 0)
        except (ValueError, TypeError):
            continue
        if montant > 0:
            out[d] = out.get(d, 0.0) + montant

    # Historique : Div_<année> → 30 juin <année> (approximation)
    for item in payload.get("historique", []) or []:
        if not _match(item):
            continue
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
            if montant > 0:
                d = date(annee, 6, 30)
                out[d] = out.get(d, 0.0) + montant
    return out


def _load_signaux_par_date(action: Action) -> Dict[date, str]:
    """Charge les SignalHistorique pour une action : {date: code}."""
    rows = (
        SignalHistorique.objects
        .filter(action=action)
        .order_by("date")
        .values_list("date", "code")
    )
    return {d: c for d, c in rows}


def _load_closes_par_date(action: Action) -> Tuple[Dict[date, float], List[date]]:
    """Charge les cours de clôture : {date: close} + liste ordonnée des dates."""
    rows = list(
        HistoriqueAction.objects
        .filter(action=action, cloture__isnull=False)
        .order_by("date")
        .values_list("date", "cloture")
    )
    closes = {d: float(c) for d, c in rows if c is not None and c > 0}
    dates = [d for d, _ in rows if d in closes]
    return closes, dates


# ---------------------------------------------------------------------------
# Logique d'ordre (option B)
# ---------------------------------------------------------------------------

def _parts_a_acheter(code: str, cash_dispo: float, prix: float,
                     facteur_garch: float) -> int:
    """Nombre de parts entières à acheter selon le code et le sizing GARCH.

    ACHETER   → 100% du cash dispo × facteur_garch
    RENFORCER → 50% du cash dispo × facteur_garch
    """
    if prix <= 0 or cash_dispo <= 0 or facteur_garch <= 0:
        return 0
    if code == "ACHETER":
        budget = cash_dispo * facteur_garch
    elif code == "RENFORCER":
        budget = cash_dispo * 0.5 * facteur_garch
    else:
        return 0
    # Les frais d'achat doivent rentrer dans le budget
    # parts × prix × (1 + frais) ≤ budget → parts ≤ budget / (prix × (1 + frais))
    # On retire le facteur de frais en passant le budget en mode "net après frais"
    # via le module d'exécution principal. Ici on renvoie un nombre brut basé sur prix.
    return int(budget // prix)


def _parts_a_vendre(code: str, parts_detenues: int) -> int:
    """Nombre de parts à vendre selon le code (NON modulé GARCH).

    ALLEGER → 50% des parts (arrondi inférieur, minimum 1 si on en a au moins 1)
    VENDRE  → toutes les parts
    """
    if parts_detenues <= 0:
        return 0
    if code == "VENDRE":
        return parts_detenues
    if code == "ALLEGER":
        n = parts_detenues // 2
        return max(n, 1) if parts_detenues >= 1 else 0
    return 0


# ---------------------------------------------------------------------------
# Simulateur principal
# ---------------------------------------------------------------------------

def simulate_portfolio(
    action: Action,
    *,
    cash_initial: float = DEFAULT_CASH,
    frais_pct: float = DEFAULT_FRAIS_PCT,
    garch_horizon: int = DEFAULT_GARCH_HORIZON,
    utiliser_garch: bool = True,
    inclure_dividendes: bool = True,
    date_debut: Optional[date] = None,
    date_fin: Optional[date] = None,
) -> SimulationResult:
    """Lance la simulation event-driven sur les SignalHistorique de l'action.

    Si ``utiliser_garch=False`` : le facteur de sizing est toujours 1.0 (utile
    pour comparer "Verdict seul" vs "Verdict + GARCH" dans la UI).

    Le facteur GARCH est calculé UNE FOIS sur l'état courant du modèle stocké
    (look-ahead léger v1). Pour le backtest, on applique ce facteur statique au
    moment où un signal d'achat se déclenche — c'est l'hypothèse pragmatique
    validée avec l'utilisateur (la vol BRVM est lente, les paramètres sont
    raisonnablement stables).
    """
    config = {
        "ticker": action.ticker,
        "cash_initial": cash_initial,
        "frais_pct": frais_pct,
        "garch_horizon": garch_horizon,
        "utiliser_garch": utiliser_garch,
        "inclure_dividendes": inclure_dividendes,
        "date_debut": date_debut.isoformat() if date_debut else None,
        "date_fin": date_fin.isoformat() if date_fin else None,
    }

    closes_map, dates_close = _load_closes_par_date(action)
    signaux = _load_signaux_par_date(action)
    if not dates_close or not signaux:
        return SimulationResult(
            disponible=False,
            raison_indispo=("Aucun cours ou aucun SignalHistorique pour cette action."
                            " Lancez 'backfill_signals' au préalable."),
            config=config,
            dates=[], valeur_strategie=[], valeur_buy_hold=[], valeur_cash=[],
            transactions=[],
            resume_strategie=None, resume_buy_hold=None, resume_cash=None,
        )

    # Bornage temporel
    if date_debut:
        dates_close = [d for d in dates_close if d >= date_debut]
    if date_fin:
        dates_close = [d for d in dates_close if d <= date_fin]
    if not dates_close:
        return SimulationResult(
            disponible=False,
            raison_indispo="Plage de dates vide après filtrage.",
            config=config,
            dates=[], valeur_strategie=[], valeur_buy_hold=[], valeur_cash=[],
            transactions=[],
            resume_strategie=None, resume_buy_hold=None, resume_cash=None,
        )

    # ===================================================================
    # Facteurs GARCH DYNAMIQUES (option B : re-fit mensuel sans look-ahead)
    # ===================================================================
    # Pour chaque fin de mois, on garantit un fit GARCH ajusté UNIQUEMENT
    # sur les rendements antérieurs. La série σ̂(t+h) calculée à chaque fin
    # de mois sert d'historique pour le percentile rolling 252j.
    monthly_fits: List = []
    monthly_vol_serie: List[float] = []  # σ̂(t+h) annualisée %, ordre chrono
    monthly_eom_dates: List[date] = []   # fin de mois correspondante
    if utiliser_garch:
        try:
            # Garantit le cache (auto-fit si nécessaire) sur la plage
            ensure_monthly_fits(action, date_debut=None, date_fin=date_fin)
            # On charge l'intégralité du cache une seule fois (ordre chrono)
            from dashboard.models import GarchFitHistorique
            monthly_fits = list(
                GarchFitHistorique.objects
                .filter(action=action)
                .order_by("fin_de_periode")
            )
        except Exception:
            monthly_fits = []

    # Reconstruction efficace de la série σ̂(t+h) annualisée par fin de mois,
    # à partir des paramètres GARCH du fit et du dernier rendement log connu
    # AU MOIS DU FIT (donc strictement passé du point de vue du backtest).
    closes_all, dates_all = _load_closes_par_date(action)
    closes_dates_sorted = dates_all
    idx_by_date = {d: i for i, d in enumerate(closes_dates_sorted)}
    if utiliser_garch and monthly_fits:
        for fit in monthly_fits:
            d_eom = fit.fin_de_periode
            i = idx_by_date.get(d_eom)
            if i is None or i == 0:
                continue
            if fit.sigma_T_pct_quotidien is None:
                continue
            p_curr = closes_all.get(d_eom)
            p_prev = closes_all.get(closes_dates_sorted[i - 1])
            if not (p_curr and p_prev and p_curr > 0 and p_prev > 0):
                continue
            r_T_pct = 100.0 * math.log(p_curr / p_prev)
            vol_an_pct = forecast_vol_pct_annuelle_from_fit(fit, r_T_pct, garch_horizon)
            if vol_an_pct is not None and math.isfinite(vol_an_pct):
                monthly_vol_serie.append(float(vol_an_pct))
                monthly_eom_dates.append(d_eom)

    def _garch_factor_at(d_signal: date) -> Tuple[float, Optional[date]]:
        """Renvoie (facteur, date_fit_utilisée) à la date du signal.

        Cherche le dernier fit avec fin_de_periode ≤ d_signal, puis le
        percentile rolling 252 du σ̂ correspondant dans la série jusqu'à
        ce fit (look-ahead-safe).
        """
        if not utiliser_garch or not monthly_eom_dates:
            return 1.0, None
        # Bissection sur monthly_eom_dates pour trouver le dernier ≤ d_signal
        lo, hi = 0, len(monthly_eom_dates) - 1
        idx = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if monthly_eom_dates[mid] <= d_signal:
                idx = mid
                lo = mid + 1
            else:
                hi = mid - 1
        if idx is None:
            return 1.0, None
        vol_at = monthly_vol_serie[idx]
        # Fenêtre rolling : on prend les σ̂ historiques jusqu'à idx inclus
        # avec un lookback de ~21 mois (≈ 252 jours / 12 jours-mois ≈ 21)
        # En pratique on prend les 24 derniers fits (2 ans) comme historique.
        win_start = max(0, idx - 24)
        window = monthly_vol_serie[win_start:idx + 1]
        if len(window) < 6:
            return 1.0, monthly_eom_dates[idx]
        rank = sum(1 for v in window if v <= vol_at)
        percentile = rank / float(len(window))
        return garch_size_factor(percentile), monthly_eom_dates[idx]

    # Dividendes
    divs_dates = _load_dividendes_dates(action.ticker) if inclure_dividendes else {}

    # === État du portefeuille "Stratégie" ===
    cash = float(cash_initial)
    parts = 0
    code_precedent = None  # détection des changements de signal
    pending_settlements: List[Tuple[date, float]] = []  # [(date_dispo, montant)]
    nb_executes = 0
    nb_annules = 0
    frais_total = 0.0
    divs_total = 0.0
    transactions: List[Transaction] = []

    # === Buy & Hold : on achète au j=0 autant de parts entières que possible ===
    prix_0 = closes_map[dates_close[0]]
    bh_parts_brut = int((cash_initial / (1.0 + frais_pct)) // prix_0) if prix_0 > 0 else 0
    bh_cout = bh_parts_brut * prix_0 * (1.0 + frais_pct)
    bh_cash = cash_initial - bh_cout
    bh_parts = bh_parts_brut

    # === Cash pur : reste figé à cash_initial ===

    dates_iso: List[str] = []
    valeurs_strat: List[float] = []
    valeurs_bh: List[float] = []
    valeurs_cash: List[float] = []

    for d in dates_close:
        prix = closes_map[d]

        # 1) Settlements arrivant aujourd'hui (cash débloqué des ventes T-2)
        if pending_settlements:
            arr_today = [p for p in pending_settlements if p[0] <= d]
            for _, m in arr_today:
                cash += m
            pending_settlements = [p for p in pending_settlements if p[0] > d]

        # 2) Dividendes versés aujourd'hui (sur les parts détenues)
        if d in divs_dates and parts > 0:
            montant_div = parts * divs_dates[d]
            cash += montant_div
            divs_total += montant_div
            transactions.append(Transaction(
                date=d.isoformat(), type="DIVIDENDE", code_signal="—",
                parts=0, prix=None,
                montant_brut=montant_div, frais=0.0,
                cash_apres=cash, parts_apres=parts,
                valeur_apres=cash + parts * prix,
                raison=f"Dividende {divs_dates[d]:.2f} FCFA/part × {parts} parts",
            ))
        # Buy & Hold encaisse aussi les dividendes (même logique)
        if d in divs_dates and bh_parts > 0:
            bh_cash += bh_parts * divs_dates[d]

        # 3) Décision de trading basée sur le signal du jour
        code = signaux.get(d)
        if code and code != code_precedent and code != "CONSERVER":
            executed = False
            if code in BULL_CODES:
                # Facteur GARCH dynamique évalué à la date du signal
                facteur_garch_d, fit_date_used = _garch_factor_at(d)
                # Cash dispo = cash actuel (les settlements pendants sont exclus,
                # ils sont déjà passés en (1) si t-2 atteint).
                cash_dispo = cash
                # On veut : parts × prix × (1 + frais) ≤ cash_dispo × facteur
                # Donc parts max = budget / (prix × (1 + frais))
                if code == "ACHETER":
                    budget = cash_dispo * facteur_garch_d
                else:  # RENFORCER
                    budget = cash_dispo * 0.5 * facteur_garch_d
                if prix > 0 and budget > 0:
                    parts_ord = int(budget // (prix * (1.0 + frais_pct)))
                else:
                    parts_ord = 0
                if parts_ord > 0:
                    montant = parts_ord * prix
                    frais = montant * frais_pct
                    if cash >= montant + frais:
                        cash -= (montant + frais)
                        parts += parts_ord
                        frais_total += frais
                        nb_executes += 1
                        executed = True
                        raison = (
                            f"{code} · GARCH ×{facteur_garch_d:.2f}"
                            + (f" (fit {fit_date_used.isoformat()})"
                               if fit_date_used else "")
                            if utiliser_garch
                            else f"{code} · sans modulation GARCH"
                        )
                        transactions.append(Transaction(
                            date=d.isoformat(), type="ACHAT", code_signal=code,
                            parts=parts_ord, prix=prix,
                            montant_brut=montant, frais=frais,
                            cash_apres=cash, parts_apres=parts,
                            valeur_apres=cash + parts * prix,
                            raison=raison,
                        ))
                if not executed:
                    nb_annules += 1
                    if facteur_garch_d <= 0.0:
                        raison = "Filtre GARCH (facteur 0, régime extrême)"
                    elif facteur_garch_d < 1.0:
                        raison = (f"Cash ou taille insuffisante après modulation GARCH "
                                  f"(×{facteur_garch_d:.2f})")
                    else:
                        raison = "Cash insuffisant"
                    transactions.append(Transaction(
                        date=d.isoformat(), type="ANNULE", code_signal=code,
                        parts=0, prix=prix,
                        montant_brut=0.0, frais=0.0,
                        cash_apres=cash, parts_apres=parts,
                        valeur_apres=cash + parts * prix,
                        raison=raison,
                    ))
            elif code in BEAR_CODES:
                parts_ord = _parts_a_vendre(code, parts)
                if parts_ord > 0 and prix > 0:
                    montant = parts_ord * prix
                    frais = montant * frais_pct
                    # Cash net = montant - frais, dispo à T+2
                    net = montant - frais
                    settle_date = _ajouter_jours_ouvres(d, SETTLEMENT_DAYS, dates_close)
                    pending_settlements.append((settle_date, net))
                    parts -= parts_ord
                    frais_total += frais
                    nb_executes += 1
                    transactions.append(Transaction(
                        date=d.isoformat(), type="VENTE", code_signal=code,
                        parts=-parts_ord, prix=prix,
                        montant_brut=-montant, frais=frais,
                        cash_apres=cash, parts_apres=parts,
                        valeur_apres=cash + parts * prix,
                        raison=f"{code} · règlement T+2 ({settle_date.isoformat()})",
                    ))
                elif code in BEAR_CODES and parts == 0:
                    nb_annules += 1
                    transactions.append(Transaction(
                        date=d.isoformat(), type="ANNULE", code_signal=code,
                        parts=0, prix=prix,
                        montant_brut=0.0, frais=0.0,
                        cash_apres=cash, parts_apres=parts,
                        valeur_apres=cash + parts * prix,
                        raison="Aucune part à vendre (pas de short)",
                    ))

        code_precedent = code if code else code_precedent

        # 4) Valeurs de portefeuille en fin de journée
        val_strat = cash + parts * prix
        val_bh = bh_cash + bh_parts * prix
        val_cash = cash_initial  # figé

        dates_iso.append(d.isoformat())
        valeurs_strat.append(val_strat)
        valeurs_bh.append(val_bh)
        valeurs_cash.append(val_cash)

    # Résumés
    resume_strat = _resume(valeurs_strat, cash_initial,
                           nb_executes=nb_executes, nb_annules=nb_annules,
                           frais_total=frais_total, divs_total=divs_total)
    resume_bh = _resume(valeurs_bh, cash_initial,
                        nb_executes=1 if bh_parts > 0 else 0,
                        nb_annules=0,
                        frais_total=bh_parts * prix_0 * frais_pct if bh_parts > 0 else 0.0,
                        divs_total=sum(bh_parts * divs_dates[d]
                                       for d in divs_dates if d in dates_close) if bh_parts > 0 else 0.0)
    resume_cash_ = _resume(valeurs_cash, cash_initial)

    # Statistique sur les facteurs GARCH effectivement appliqués (utile pour audit)
    nb_fits_utilises = len(monthly_eom_dates) if utiliser_garch else 0
    return SimulationResult(
        disponible=True,
        raison_indispo=None,
        config={**config, "nb_fits_garch_caches": nb_fits_utilises},
        dates=dates_iso,
        valeur_strategie=valeurs_strat,
        valeur_buy_hold=valeurs_bh,
        valeur_cash=valeurs_cash,
        transactions=transactions,
        resume_strategie=resume_strat,
        resume_buy_hold=resume_bh,
        resume_cash=resume_cash_,
    )


def _ajouter_jours_ouvres(d: date, n: int, dates_dispo: Sequence[date]) -> date:
    """Renvoie la n-ième date ouvrée ≥ d+1 présente dans ``dates_dispo``.

    Approche : on prend la prochaine date dans ``dates_dispo`` strictement
    après ``d``, puis on avance de (n-1) crans dans la liste. Si on dépasse,
    on prend la dernière dispo (le cash sera débloqué à la fin du backtest).

    Comme ``dates_dispo`` est l'ensemble des dates de cotation de l'action,
    cela correspond bien à des **jours ouvrés BRVM** pour cette valeur (pas
    juste des jours calendaires).
    """
    if n <= 0:
        return d
    # Recherche binaire serait O(log) ; mais la liste est < ~5000 entrées et
    # on n'appelle ça que par vente. Linéaire suffit.
    idx = None
    for i, x in enumerate(dates_dispo):
        if x > d:
            idx = i
            break
    if idx is None:
        return dates_dispo[-1]
    target = idx + (n - 1)
    if target >= len(dates_dispo):
        return dates_dispo[-1]
    return dates_dispo[target]


def to_template_dict(sim: SimulationResult) -> dict:
    """Sérialise un :class:`SimulationResult` pour le template Django."""
    def _r(x: Optional[Resume]) -> Optional[dict]:
        if x is None:
            return None
        return {
            "valeur_initiale": x.valeur_initiale,
            "valeur_finale": x.valeur_finale,
            "pnl_fcfa": x.pnl_fcfa,
            "pnl_pct": x.pnl_pct,
            "nb_ordres_executes": x.nb_ordres_executes,
            "nb_ordres_annules": x.nb_ordres_annules,
            "frais_total": x.frais_total,
            "dividendes_total": x.dividendes_total,
            "max_drawdown_pct": x.max_drawdown_pct,
            "sharpe_annualise": x.sharpe_annualise,
        }

    return {
        "disponible": sim.disponible,
        "raison_indispo": sim.raison_indispo,
        "config": sim.config,
        "dates": sim.dates,
        "valeur_strategie": sim.valeur_strategie,
        "valeur_buy_hold": sim.valeur_buy_hold,
        "valeur_cash": sim.valeur_cash,
        "transactions": [t.__dict__ for t in sim.transactions],
        "resume_strategie": _r(sim.resume_strategie),
        "resume_buy_hold": _r(sim.resume_buy_hold),
        "resume_cash": _r(sim.resume_cash),
    }
