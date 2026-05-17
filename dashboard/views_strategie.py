"""Vues HTTP pour la page Stratégie HMM (page unique à 8 onglets)."""
from __future__ import annotations

import json
from collections import defaultdict

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from .models import (
    Action,
    AllocationStrategie,
    FacteurStrategie,
    ParametresHMM,
    RegimeMarche,
    RendementPortefeuilleFactoriel,
)
from .strategie_hmm.core.scoring import FACTEURS_A_INVERSER
from .strategie_hmm.services.diagnostics import calculer_diagnostics
from .strategie_hmm.services.facteurs_config import (
    get_facteurs_exclus,
    set_facteur_exclu,
)
from .strategie_hmm.services.strategie_service import (
    LIBELLE_STRATEGIES,
    comparer_toutes_strategies,
    executer_pipeline_complet,
    reconstruire_historique_regimes,
)


# Ordre canonique des 13 facteurs (calqué sur l'Excel de référence)
FACTEURS_ORDRE = [
    "BtM", "EP", "SP", "LEVIER", "ROE", "ROA", "DIV_YIELD",
    "VARIANCE", "RDT_JOURNALIER", "MOM_6M", "VOLUME", "BETA", "CAPI",
]

LIBELLE_FACTEURS = {
    "BtM": "Book-to-Market",
    "EP": "E/P",
    "SP": "S/P",
    "ROA": "ROA",
    "ROE": "ROE",
    "LEVIER": "Levier",
    "DIV_YIELD": "Dividend Yield",
    "VARIANCE": "Variance",
    "RDT_JOURNALIER": "Rdt Journalier",
    "MOM_6M": "6M Momentum",
    "VOLUME": "Volume",
    "BETA": "Beta",
    "CAPI": "Capitalisation",
}

FAMILLE_FACTEURS = {
    "BtM": "Valeur", "EP": "Valeur", "SP": "Valeur",
    "ROA": "Qualité", "ROE": "Qualité", "LEVIER": "Qualité",
    "DIV_YIELD": "Croissance",
    "VARIANCE": "Volatilité",
    "RDT_JOURNALIER": "Momentum", "MOM_6M": "Momentum",
    "VOLUME": "Liquidité",
    "BETA": "Risque",
    "CAPI": "Taille",
}

QUINTILE = 0.20


def _get_context_base():
    from .models import HistoriqueAction
    from django.db.models import Max
    last = HistoriqueAction.objects.aggregate(m=Max("date"))["m"]
    return {"derniere_maj": last.strftime("%d/%m/%Y") if last else "N/A"}


def _construire_buckets(date_ref):
    """Reconstruit les 13 sous-portefeuilles long/short à partir de FacteurStrategie.

    Returns
    -------
    dict {facteur_code: {"longs": [tickers], "shorts": [tickers], "poids_intra": float}}
    """
    qs = FacteurStrategie.objects.filter(date=date_ref).select_related("action")
    par_facteur: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for fs in qs:
        par_facteur[fs.facteur].append((fs.action.ticker, fs.valeur))

    buckets: dict[str, dict] = {}
    for code, valeurs in par_facteur.items():
        ascending = code in FACTEURS_A_INVERSER
        valeurs_tri = sorted(valeurs, key=lambda x: x[1], reverse=not ascending)
        n = len(valeurs_tri)
        if n < 5:
            continue
        n_q = max(1, int(round(n * QUINTILE)))
        longs = [t for t, _ in valeurs_tri[:n_q]]
        shorts = [t for t, _ in valeurs_tri[-n_q:]]
        buckets[code] = {
            "longs": longs,
            "shorts": shorts,
            "poids_intra": 1.0 / n_q,
            "n": n_q,
        }
    return buckets


def _decomposition_actifs(buckets, poids_facteurs):
    """Construit la matrice de décomposition Actif × Facteur.

    Pour chaque ticker présent dans au moins un bucket, calcule :
      - sa contribution par facteur : ±α_f × (1/n_q) × 100  (en %)
      - son poids global signé : Σ contributions
      - le nombre de présences long / short
      - sa position L/S/— par facteur (pour la heatmap)
    """
    contributions: dict[str, dict[str, float]] = defaultdict(dict)
    positions: dict[str, dict[str, str]] = defaultdict(dict)
    nb_long: dict[str, int] = defaultdict(int)
    nb_short: dict[str, int] = defaultdict(int)

    for code, b in buckets.items():
        alpha = poids_facteurs.get(code, 0.0)
        w_intra = b["poids_intra"]
        # contribution = α × (±1/n) × 100   →   en % (équivalent format Excel)
        contrib_pos = alpha * w_intra * 100.0
        contrib_neg = -contrib_pos
        for t in b["longs"]:
            contributions[t][code] = contrib_pos
            positions[t][code] = "L"
            nb_long[t] += 1
        for t in b["shorts"]:
            contributions[t][code] = contrib_neg
            positions[t][code] = "S"
            nb_short[t] += 1

    actifs = []
    for ticker, contribs in contributions.items():
        poids_global = sum(contribs.values())
        actifs.append({
            "ticker": ticker,
            "poids_global": poids_global,
            "nb_long": nb_long[ticker],
            "nb_short": nb_short[ticker],
            "contributions": contribs,
            "positions": positions[ticker],
        })
    actifs.sort(key=lambda x: -x["poids_global"])
    return actifs


def _long_only(actifs, capital=1_000_000):
    """Filtre les poids positifs et renormalise à 100% — portefeuille investissable."""
    positifs = [a for a in actifs if a["poids_global"] > 0]
    total = sum(a["poids_global"] for a in positifs)
    if total <= 0:
        return []
    out = []
    for i, a in enumerate(positifs, start=1):
        poids_norm = a["poids_global"] / total * 100.0
        out.append({
            "rang": i,
            "ticker": a["ticker"],
            "poids_brut": a["poids_global"],
            "poids_norm": poids_norm,
            "montant": int(round(capital * poids_norm / 100.0)),
            "nb_facteurs_long": a["nb_long"],
        })
    return out


def _short_normalise(actifs, capital=1_000_000):
    """Symétrique : poids négatifs renormalisés (référence — non investissable sur BRVM)."""
    negatifs = [a for a in actifs if a["poids_global"] < 0]
    negatifs.sort(key=lambda x: x["poids_global"])  # plus négatif d'abord
    total = sum(abs(a["poids_global"]) for a in negatifs)
    if total <= 0:
        return []
    out = []
    for i, a in enumerate(negatifs, start=1):
        poids_norm = abs(a["poids_global"]) / total * 100.0
        out.append({
            "rang": i,
            "ticker": a["ticker"],
            "poids_brut": a["poids_global"],
            "poids_norm": poids_norm,
            "montant": int(round(capital * poids_norm / 100.0)),
            "nb_facteurs_short": a["nb_short"],
        })
    return out


def strategie_dashboard(request):
    """Page unique de la Stratégie HMM — 8 onglets alimentés depuis la BDD."""
    ctx = _get_context_base()

    derniere_alloc = (
        AllocationStrategie.objects.filter(strategie="SHARPE_HMM")
        .select_related("regime")
        .order_by("-date", "-date_creation")
        .first()
    )
    dernier_regime = RegimeMarche.objects.order_by("-date").first()
    derniers_params = ParametresHMM.objects.order_by("-date_entrainement").first()

    # ----- Onglet 1 : Résumé FSDAA — allocations factorielles -----
    poids_facteurs_list = []
    poids_facteurs_dict = {}
    if derniere_alloc:
        poids_facteurs_dict = derniere_alloc.poids_facteurs or {}
        for code in sorted(poids_facteurs_dict, key=lambda c: -poids_facteurs_dict[c]):
            poids_facteurs_list.append({
                "code": code,
                "libelle": LIBELLE_FACTEURS.get(code, code),
                "famille": FAMILLE_FACTEURS.get(code, ""),
                "poids": poids_facteurs_dict[code],
                "poids_pct": poids_facteurs_dict[code] * 100.0,
            })

    # ----- Onglets 2-6 : reconstruction à partir des FacteurStrategie -----
    buckets_par_facteur = []   # pour onglet 2 (composition)
    decomposition = []         # pour onglet 3 (poids globaux)
    top_long = []              # pour onglet 4
    top_short = []             # pour onglet 4
    heatmap_rows = []          # pour onglet 5
    long_only_list = []        # pour onglet 6
    short_ref_list = []        # pour onglet 6 (référence)

    if derniere_alloc:
        buckets = _construire_buckets(derniere_alloc.date)
        # Onglet 2 — composition ordonnée selon l'ordre du poids facteur
        actions_nom = {
            a.ticker: a for a in Action.objects.filter(
                ticker__in={
                    t for b in buckets.values() for t in (b["longs"] + b["shorts"])
                }
            )
        }
        for code in sorted(buckets, key=lambda c: -poids_facteurs_dict.get(c, 0)):
            b = buckets[code]
            poids_pct_intra = b["poids_intra"] * 100.0
            buckets_par_facteur.append({
                "code": code,
                "libelle": LIBELLE_FACTEURS.get(code, code),
                "famille": FAMILLE_FACTEURS.get(code, ""),
                "alpha_pct": poids_facteurs_dict.get(code, 0) * 100.0,
                "n_long": len(b["longs"]),
                "n_short": len(b["shorts"]),
                "poids_pct_intra": poids_pct_intra,
                "longs": [
                    {"ticker": t, "nom": getattr(actions_nom.get(t), "nom", "") or t,
                     "poids_pct": poids_pct_intra}
                    for t in b["longs"]
                ],
                "shorts": [
                    {"ticker": t, "nom": getattr(actions_nom.get(t), "nom", "") or t,
                     "poids_pct": -poids_pct_intra}
                    for t in b["shorts"]
                ],
            })

        # Onglet 3 — décomposition Actif × Facteur
        actifs = _decomposition_actifs(buckets, poids_facteurs_dict)
        # Ordre des facteurs pour les colonnes : par α décroissant (= ordre de l'onglet 2)
        facteurs_cols = [
            code for code in sorted(
                poids_facteurs_dict, key=lambda c: -poids_facteurs_dict[c]
            ) if code in buckets
        ]
        for a in actifs:
            cellules = []
            for code in facteurs_cols:
                v = a["contributions"].get(code)
                cellules.append({"code": code, "valeur": v})
            decomposition.append({
                "ticker": a["ticker"],
                "nom": getattr(actions_nom.get(a["ticker"]), "nom", "") or a["ticker"],
                "poids_global": a["poids_global"],
                "nb_long": a["nb_long"],
                "nb_short": a["nb_short"],
                "cellules": cellules,
            })

        # Onglet 4 — Top 10 long / Top 10 short
        positifs = [a for a in actifs if a["poids_global"] > 0]
        negatifs = sorted(
            [a for a in actifs if a["poids_global"] < 0],
            key=lambda x: x["poids_global"],
        )
        top_long = [
            {
                "rang": i + 1,
                "ticker": a["ticker"],
                "poids_global": a["poids_global"],
                "nb_long": a["nb_long"],
                "nb_short": a["nb_short"],
                "bar_width": min(100, abs(a["poids_global"]) * 12),  # 100% ≈ 8.3% de poids
            }
            for i, a in enumerate(positifs[:10])
        ]
        top_short = [
            {
                "rang": i + 1,
                "ticker": a["ticker"],
                "poids_global": a["poids_global"],
                "nb_long": a["nb_long"],
                "nb_short": a["nb_short"],
                "bar_width": min(100, abs(a["poids_global"]) * 12),
            }
            for i, a in enumerate(negatifs[:10])
        ]

        # Onglet 5 — Heatmap
        for a in actifs:
            cells = []
            for code in facteurs_cols:
                pos = a["positions"].get(code, "")
                cells.append({"code": code, "pos": pos})
            heatmap_rows.append({
                "ticker": a["ticker"],
                "poids_global": a["poids_global"],
                "cells": cells,
            })

        # Onglet 6 — Long-Only investissable + référence Short
        long_only_list = _long_only(actifs)
        short_ref_list = _short_normalise(actifs)

        # Illustration réelle pour Étape 2 — facteurs du meilleur actif long-only
        illustration_action = None
        if long_only_list:
            top_ticker = long_only_list[0]["ticker"]
            latest_fs_date = (
                FacteurStrategie.objects.filter(action__ticker=top_ticker)
                .order_by("-date").values_list("date", flat=True).first()
            )
            if latest_fs_date:
                facteurs_vals = {
                    fs.facteur: fs.valeur
                    for fs in FacteurStrategie.objects.filter(
                        action__ticker=top_ticker, date=latest_fs_date
                    )
                }
                try:
                    action_illus = Action.objects.get(ticker=top_ticker)
                except Action.DoesNotExist:
                    action_illus = None
                _META = [
                    ("BtM",  "Capitaux propres / Capitalisation boursière", "Valeur",    False, "×"),
                    ("EP",   "Résultat net par action / Cours",             "Valeur",    False, "×"),
                    ("SP",   "CA par action / Cours",                       "Valeur",    False, "×"),
                    ("DIV_YIELD", "Dividende annuel / Cours",               "Valeur",    False, "%"),
                    ("ROE",  "Résultat net / Capitaux propres",             "Qualité",   False, "%"),
                    ("ROA",  "Résultat net / Total actif",                  "Qualité",   False, "%"),
                    ("LEVIER", "Total dettes / Capitaux propres",           "Qualité",   True,  "×"),
                    ("VARIANCE", "Var(Rₜ) journalière — fenêtre 60 j",     "Risque",    True,  ""),
                    ("BETA", "Cov(Rᵢ, Rₘ) / Var(Rₘ) — fenêtre 252 j",   "Risque",    True,  ""),
                    ("CAPI", "Nombre d'actions × Cours",                    "Taille",    False, "FCFA"),
                    ("RDT_JOURNALIER", "(Pₜ − Pₜ₋₁) / Pₜ₋₁",            "Momentum",  False, "%"),
                    ("MOM_6M", "(Pₜ − Pₜ₋₁₂₆) / Pₜ₋₁₂₆",               "Momentum",  False, "%"),
                    ("VOLUME", "Moyenne 20 j du volume en FCFA",           "Liquidité", False, "FCFA"),
                ]
                illustration_action = {
                    "ticker": top_ticker,
                    "nom": getattr(action_illus, "nom", top_ticker) if action_illus else top_ticker,
                    "date": latest_fs_date,
                    "facteurs": [
                        {
                            "code": code,
                            "libelle": LIBELLE_FACTEURS.get(code, code),
                            "formule": formule,
                            "famille": famille,
                            "inverser": inverser,
                            "unite": unite,
                            "valeur": facteurs_vals.get(code),
                        }
                        for code, formule, famille, inverser, unite in _META
                    ],
                }

        # En-têtes communs onglets 3 & 5
        ctx["facteurs_cols"] = [
            {"code": c, "libelle": LIBELLE_FACTEURS.get(c, c),
             "alpha_pct": poids_facteurs_dict.get(c, 0) * 100.0}
            for c in facteurs_cols
        ]
    else:
        ctx["facteurs_cols"] = []

    # ----- Onglet 7 : Régimes -----
    # Source principale : table RegimeMarche (1 ligne par exécution du pipeline).
    # Si l'historique persisté est trop court (< 2 lignes), on reconstruit la
    # chronique complète à la volée à partir des rendements factoriels.
    regimes_qs = list(
        RegimeMarche.objects.order_by("date").values(
            "date", "regime_brut", "regime_confirme",
            "proba_regime_0", "proba_regime_1", "changement",
        )
    )
    if len(regimes_qs) < 2:
        try:
            df_hist = reconstruire_historique_regimes()
        except Exception:
            df_hist = None
        if df_hist is not None and not df_hist.empty:
            regimes_qs = [
                {
                    "date": idx.date() if hasattr(idx, "date") else idx,
                    "regime_brut": int(row["regime_brut"]),
                    "regime_confirme": int(row["regime_confirme"]),
                    "proba_regime_0": float(row["proba_0"]),
                    "proba_regime_1": float(row["proba_1"]),
                    "changement": False,
                }
                for idx, row in df_hist.iterrows()
            ]
            # Marquer les changements de régime confirmé
            for i in range(1, len(regimes_qs)):
                regimes_qs[i]["changement"] = (
                    regimes_qs[i]["regime_confirme"]
                    != regimes_qs[i - 1]["regime_confirme"]
                )
    regimes_json = json.dumps(
        [{**r, "date": r["date"].isoformat()} for r in regimes_qs],
        default=str,
    )
    nb_changements = sum(1 for r in regimes_qs if r["changement"])

    # ----- Onglet 8 : Historique des allocations -----
    allocs = list(
        AllocationStrategie.objects.select_related("regime")
        .order_by("-date", "-date_creation")[:100]
    )
    allocs_view = []
    for a in allocs:
        top_f = sorted((a.poids_facteurs or {}).items(), key=lambda x: -x[1])[:3]
        top_a = sorted((a.poids_actions or {}).items(), key=lambda x: -x[1])[:5]
        allocs_view.append({
            "obj": a,
            "top_facteurs": [{"code": c, "poids": p} for c, p in top_f],
            "top_actions": [{"ticker": t, "poids": p} for t, p in top_a],
        })

    # ----- Onglet Comparaison : 6 stratégies sous le régime confirmé -----
    comparaison_strategies = []
    comparaison_facteurs_cols = []
    if derniere_alloc and derniers_params and dernier_regime:
        factor_codes = list((derniere_alloc.poids_facteurs or {}).keys())
        if factor_codes and len(factor_codes) == derniers_params.n_facteurs:
            try:
                comparaison_strategies = comparer_toutes_strategies(
                    derniers_params,
                    int(dernier_regime.regime_confirme),
                    factor_codes,
                )
                # Colonnes ordonnées par α décroissant de la stratégie courante
                comparaison_facteurs_cols = [
                    {"code": c, "libelle": LIBELLE_FACTEURS.get(c, c)}
                    for c in sorted(
                        factor_codes,
                        key=lambda x: -(derniere_alloc.poids_facteurs or {}).get(x, 0),
                    )
                ]
                # Marquer la stratégie active (celle de derniere_alloc)
                for s in comparaison_strategies:
                    s["active"] = (s["code"] == derniere_alloc.strategie)
            except Exception:  # pragma: no cover
                comparaison_strategies = []

    # ----- Onglet Comparaison : performance historique des 13 sous-portefeuilles -----
    # Cumulative performance: cumprod(1 + r) - 1 pour chaque facteur, depuis t0.
    facteurs_perf_payload = {"dates": [], "series": {}, "date_min": None, "date_max": None}
    rdt_qs = list(
        RendementPortefeuilleFactoriel.objects.order_by("date").values(
            "facteur", "date", "rendement"
        )
    )
    if rdt_qs:
        # Regrouper par date pour un axe X commun, gérer les facteurs avec NaN
        dates_set = sorted({r["date"] for r in rdt_qs})
        by_factor: dict[str, dict] = defaultdict(dict)
        for r in rdt_qs:
            by_factor[r["facteur"]][r["date"]] = r["rendement"]

        series: dict[str, list] = {}
        for code, libelle in [(c, LIBELLE_FACTEURS.get(c, c)) for c in FACTEURS_ORDRE]:
            d = by_factor.get(code)
            if not d:
                continue
            cum = 1.0
            values = []
            for dt in dates_set:
                r = d.get(dt)
                if r is None:
                    # pas de rendement ce jour-là : pas de variation
                    values.append(round((cum - 1.0) * 100.0, 4))
                    continue
                cum *= (1.0 + float(r))
                values.append(round((cum - 1.0) * 100.0, 4))
            series[code] = {"libelle": libelle, "values": values}

        facteurs_perf_payload = {
            "dates": [d.isoformat() for d in dates_set],
            "series": series,
            "date_min": dates_set[0].isoformat(),
            "date_max": dates_set[-1].isoformat(),
        }
    facteurs_perf_json = json.dumps(facteurs_perf_payload)

    # ----- Onglet Diagnostics : analyse descriptive & tests de robustesse -----
    try:
        diagnostics = calculer_diagnostics(FACTEURS_ORDRE)
    except Exception:  # pragma: no cover
        diagnostics = {"disponible": False, "n_obs": 0, "n_facteurs": 0,
                       "descriptive": [], "adf": [], "vif": [], "jarque_bera": [],
                       "correlation": {"codes": [], "matrix": [], "redondances": []},
                       "verdict": {}}
    # Ajouter le libellé + famille + état (exclu/actif) pour l'affichage
    facteurs_exclus = get_facteurs_exclus()
    exclus_upper = {c.upper() for c in facteurs_exclus}
    for lst in (diagnostics.get("descriptive", []), diagnostics.get("adf", []),
                diagnostics.get("vif", []), diagnostics.get("jarque_bera", [])):
        for row in lst:
            row["libelle"] = LIBELLE_FACTEURS.get(row["code"], row["code"])
            row["famille"] = FAMILLE_FACTEURS.get(row["code"], "")
            row["exclu"] = row["code"].upper() in exclus_upper
    diagnostics["facteurs_exclus"] = sorted(facteurs_exclus)
    diagnostics["n_actifs"] = max(0, diagnostics.get("n_facteurs", 0) - len(facteurs_exclus))
    diagnostics_json = json.dumps({
        "correlation": diagnostics.get("correlation", {}),
    })

    ctx.update({
        # Header sticky
        "derniere_alloc": derniere_alloc,
        "dernier_regime": dernier_regime,
        "derniers_params": derniers_params,
        # Stratégies disponibles (sélecteur "Lancer le pipeline")
        "strategies_dispo": [
            {"code": c, "libelle": LIBELLE_STRATEGIES.get(c, c)}
            for c in ["SHARPE_HMM", "DYN_HMM", "MR_HMM", "RP_HMM", "MD_HMM", "MV_HMM"]
        ],
        # Onglet Comparaison
        "comparaison_strategies": comparaison_strategies,
        "comparaison_facteurs_cols": comparaison_facteurs_cols,
        "facteurs_perf_json": facteurs_perf_json,
        # Onglet 1
        "poids_facteurs_list": poids_facteurs_list,
        # Onglet 2
        "buckets_par_facteur": buckets_par_facteur,
        # Onglet 3
        "decomposition": decomposition,
        # Onglet 4
        "top_long": top_long,
        "top_short": top_short,
        # Onglet 5
        "heatmap_rows": heatmap_rows,
        # Onglet 6
        "long_only_list": long_only_list,
        "short_ref_list": short_ref_list,
        # Illustration étape 2
        "illustration_action": illustration_action,
        # Onglet 7
        "regimes_json": regimes_json,
        "nb_changements": nb_changements,
        # Onglet 8
        "allocations": allocs_view,
        # Onglet Diagnostics (étape 4)
        "diagnostics": diagnostics,
        "diagnostics_json": diagnostics_json,
        "page_active": "strategie_hmm",
    })
    return render(request, "dashboard/strategie/strategie_dashboard.html", ctx)


# Conservés comme alias rétro-compatibles → ils redirigent vers la page unique avec ancre.
def strategie_regime(request):
    from django.shortcuts import redirect
    return redirect(f"{request.build_absolute_uri('/strategie-hmm/')}#tab-regimes")


def strategie_historique(request):
    from django.shortcuts import redirect
    return redirect(f"{request.build_absolute_uri('/strategie-hmm/')}#tab-historique")


# ----------------------------------------------------------------------
# Endpoints API JSON
# ----------------------------------------------------------------------

def api_strategie_regime_courant(request):
    regime = RegimeMarche.objects.order_by("-date").first()
    if not regime:
        return JsonResponse({"error": "Aucun régime calculé"}, status=404)
    return JsonResponse({
        "date": regime.date.isoformat(),
        "regime_brut": regime.regime_brut,
        "regime_confirme": regime.regime_confirme,
        "proba_regime_0": regime.proba_regime_0,
        "proba_regime_1": regime.proba_regime_1,
        "changement": regime.changement,
        "log_likelihood": regime.log_likelihood,
    })


def api_strategie_allocation_courante(request):
    alloc = (
        AllocationStrategie.objects.filter(strategie="SHARPE_HMM")
        .order_by("-date", "-date_creation").first()
    )
    if not alloc:
        return JsonResponse({"error": "Aucune allocation"}, status=404)
    return JsonResponse({
        "date": alloc.date.isoformat(),
        "strategie": alloc.strategie,
        "poids_facteurs": alloc.poids_facteurs,
        "poids_actions": alloc.poids_actions,
        "rendement_attendu": alloc.rendement_attendu,
        "volatilite_attendue": alloc.volatilite_attendue,
        "sharpe_attendu": alloc.sharpe_attendu,
    })


def api_strategie_historique_regimes(request):
    df = reconstruire_historique_regimes()
    return JsonResponse({
        "dates": [d.date().isoformat() for d in df.index],
        "regime_brut": df["regime_brut"].astype(int).tolist(),
        "regime_confirme": df["regime_confirme"].astype(int).tolist(),
        "proba_0": df["proba_0"].tolist(),
        "proba_1": df["proba_1"].tolist(),
    })


@require_http_methods(["POST"])
def api_strategie_declencher_reallocation(request):
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = {}
    strategie = body.get("strategie", "SHARPE_HMM")
    nb_top = int(body.get("nb_actions_top", 15))
    res = executer_pipeline_complet(strategie=strategie, nb_actions_top=nb_top)
    return JsonResponse({
        "success": True,
        "allocation_id": res["allocation_id"],
        "date": res["date"].isoformat(),
        "regime_confirme": res["regime_confirme"],
        "changement": res["changement"],
        "metriques": res["metriques"],
        "n_actions_matchees": res["n_actions_matchees"],
    })


@require_http_methods(["POST"])
def api_strategie_toggle_facteur(request):
    """Active ou exclut un facteur du pipeline HMM (étape 4 — robustesse)."""
    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "JSON invalide"}, status=400)
    code_in = str(body.get("code", "")).strip()
    # Résolution case-insensitive vers la forme canonique de FACTEURS_ORDRE
    code = next((f for f in FACTEURS_ORDRE if f.upper() == code_in.upper()), None)
    if not code:
        return JsonResponse(
            {"success": False, "error": f"Code facteur inconnu : {code_in}"},
            status=400,
        )
    exclu = bool(body.get("exclu", False))
    exclus = set_facteur_exclu(code, exclu)
    n_actifs = len(FACTEURS_ORDRE) - len(exclus)
    return JsonResponse({
        "success": True,
        "code": code,
        "exclu": exclu,
        "facteurs_exclus": sorted(exclus),
        "n_actifs": n_actifs,
    })
