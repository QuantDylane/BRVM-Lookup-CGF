"""Page Simulateur de stratégie : backtest du verdict 4-axes + filtre GARCH.

Cette page expose un simulateur de portefeuille event-driven (option B, T+2,
modulation β) avec deux onglets :
  - Onglet 1 : Simulation portefeuille (résumé + graphe 3 courbes + transactions).
  - Onglet 2 : Diagnostic des signaux (métriques par code + matrice Sika).

Le calcul est déclenché par un POST (formulaire de config) ; en GET on affiche
juste le formulaire avec ses valeurs par défaut. C'est volontaire : un
backtest peut prendre plusieurs minutes (warmup du cache GARCH) donc on évite
de lancer un calcul à chaque chargement de page.
"""
from __future__ import annotations

from datetime import date, datetime

from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .models import Action, GarchFitHistorique, GarchModel, SignalHistorique
from .services_backtest import metriques_par_code, _join_signals_with_closes, matrice_confusion_vs_sika
from .services_simulation import (
    DEFAULT_CASH,
    DEFAULT_FRAIS_PCT,
    DEFAULT_GARCH_HORIZON,
    simulate_portfolio,
    to_template_dict as sim_to_tpl,
)
from .views import get_context_base


def _parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _parse_float(s, default):
    """Tolère le séparateur français (virgule) en plus du point."""
    if s is None:
        return default
    try:
        return float(str(s).replace(",", ".").replace(" ", ""))
    except (ValueError, TypeError):
        return default


def _parse_int(s, default):
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


@require_http_methods(["GET", "POST"])
def simulateur_strategie(request):
    """Page principale du simulateur."""
    ctx = get_context_base(request)

    actions = Action.objects.all().order_by("ticker")
    selected_ticker = request.GET.get("ticker") or request.POST.get("ticker") or ""
    selected_action = (
        Action.objects.filter(ticker=selected_ticker).first()
        if selected_ticker else None
    )

    # Plage de dates disponibles (pour le picker)
    date_min = date_max = None
    if selected_action:
        from .models import HistoriqueAction
        qs = HistoriqueAction.objects.filter(
            action=selected_action, cloture__isnull=False
        ).order_by("date")
        first = qs.first()
        last = qs.last()
        if first and last:
            date_min = first.date
            date_max = last.date

    # Configuration courante (lue dans le POST si présent, sinon defaults)
    cash_initial = _parse_float(request.POST.get("cash_initial"), DEFAULT_CASH)
    frais_pct = _parse_float(request.POST.get("frais_pct"), DEFAULT_FRAIS_PCT * 100.0) / 100.0
    garch_horizon = _parse_int(request.POST.get("garch_horizon"), DEFAULT_GARCH_HORIZON)
    # NB: une checkbox HTML non cochée n'est PAS envoyée dans le POST (le name
    # n'apparaît pas). Donc en POST : présence = coché, absence = décoché.
    # En GET (premier affichage) : défaut on/on.
    if request.method == "POST":
        utiliser_garch = "utiliser_garch" in request.POST
        inclure_dividendes = "inclure_dividendes" in request.POST
    else:
        utiliser_garch = True
        inclure_dividendes = True
    d_debut = _parse_date(request.POST.get("date_debut"))
    d_fin = _parse_date(request.POST.get("date_fin"))

    # Garde-fous
    if garch_horizon not in (1, 5, 22):
        garch_horizon = DEFAULT_GARCH_HORIZON
    if cash_initial <= 0:
        cash_initial = DEFAULT_CASH

    config_courante = {
        "ticker": selected_ticker,
        "cash_initial": cash_initial,
        "frais_pct_display": frais_pct * 100.0,
        "garch_horizon": garch_horizon,
        "utiliser_garch": utiliser_garch,
        "inclure_dividendes": inclure_dividendes,
        "date_debut": d_debut.isoformat() if d_debut else "",
        "date_fin": d_fin.isoformat() if d_fin else "",
        "date_min": date_min.isoformat() if date_min else "",
        "date_max": date_max.isoformat() if date_max else "",
    }

    simulation = None
    diagnostic = None
    erreur = None

    # Calcul UNIQUEMENT si POST + action sélectionnée
    if request.method == "POST" and selected_action:
        try:
            sim = simulate_portfolio(
                selected_action,
                cash_initial=cash_initial,
                frais_pct=frais_pct,
                garch_horizon=garch_horizon,
                utiliser_garch=utiliser_garch,
                inclure_dividendes=inclure_dividendes,
                date_debut=d_debut,
                date_fin=d_fin,
            )
            simulation = sim_to_tpl(sim)

            # Diagnostic des signaux (métriques + matrice de confusion)
            rows, _ = _join_signals_with_closes(selected_action)
            # Filtrer aux dates dans la plage
            if d_debut or d_fin:
                rows = [
                    r for r in rows
                    if (not d_debut or r["date"] >= d_debut.isoformat())
                    and (not d_fin or r["date"] <= d_fin.isoformat())
                ]
            metriques = metriques_par_code(rows) if rows else []
            confusion = matrice_confusion_vs_sika(selected_action)
            diagnostic = {
                "metriques": metriques,
                "confusion_sika": confusion,
                "n_signaux": len(rows),
                "date_debut": rows[0]["date"] if rows else None,
                "date_fin": rows[-1]["date"] if rows else None,
            }
        except Exception as exc:
            erreur = f"Erreur de simulation : {exc}"
            simulation = None

    # État du cache GARCH (pour afficher un avertissement si vide)
    cache_status = None
    if selected_action:
        n_fits = GarchFitHistorique.objects.filter(action=selected_action).count()
        gm = GarchModel.objects.filter(action=selected_action).first()
        cache_status = {
            "n_fits": n_fits,
            "garch_model_present": gm is not None,
            "model_type": gm.model_type if gm else None,
        }

    ctx.update({
        "actions": actions,
        "selected_ticker": selected_ticker,
        "selected_action": selected_action,
        "config": config_courante,
        "simulation": simulation,
        "diagnostic": diagnostic,
        "cache_status": cache_status,
        "erreur": erreur,
        "post_done": request.method == "POST",
    })
    return render(request, "dashboard/simulateur.html", ctx)
