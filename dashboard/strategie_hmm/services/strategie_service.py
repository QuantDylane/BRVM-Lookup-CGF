"""Service principal qui orchestre le pipeline Stratégie HMM."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
from django.db import transaction

from dashboard.models import (
    Action,
    AllocationStrategie,
    FacteurStrategie,
    ParametresHMM,
    RegimeMarche,
    RendementPortefeuilleFactoriel,
)
from dashboard.strategie_hmm.core.facteurs import calculer_facteurs_toutes_actions
from dashboard.strategie_hmm.core.hmm_engine import (
    confirmer_regime,
    entrainer_hmm,
)
from dashboard.strategie_hmm.core.optimisation import (
    OPTIMISEURS,
    metriques_attendues,
)
from dashboard.strategie_hmm.core.portefeuilles_factoriels import (
    charger_rendements_depuis_bd,
    construire_rendements_factoriels,
)
from dashboard.strategie_hmm.core.scoring import scorer_actions, selectionner_top_n


def construire_rendements_si_necessaire(force: bool = False) -> int:
    """Construit les rendements des 13 portefeuilles depuis la BD si la table
    est vide (ou si force=True). Retourne le nombre de lignes en base.
    """
    if not force and RendementPortefeuilleFactoriel.objects.exists():
        return RendementPortefeuilleFactoriel.objects.count()
    if force:
        RendementPortefeuilleFactoriel.objects.all().delete()
    construire_rendements_factoriels(persist=True)
    return RendementPortefeuilleFactoriel.objects.count()


def executer_pipeline_complet(
    strategie: str = "SHARPE_HMM",
    nb_actions_top: int = 15,
    d_confirmation: int = 5,
) -> dict:
    """Exécute le pipeline complet : HMM → optimisation → scoring → allocation.

    Sauvegarde la nouvelle ``AllocationStrategie`` (et ``RegimeMarche``,
    ``ParametresHMM``) en base. Retourne un dict de synthèse.
    """
    # 1. Charger les rendements depuis la BD (construits par
    # ``construire_portefeuilles_factoriels``).
    construire_rendements_si_necessaire()
    df = charger_rendements_depuis_bd()
    if df.empty:
        raise RuntimeError(
            "Aucun rendement de portefeuille factoriel disponible. "
            "Lancez d'abord : python manage.py construire_portefeuilles_factoriels"
        )
    # Convertir l'index en datetime pour la suite (le HMM travaille sur values)
    df.index = pd.to_datetime(df.index)
    df = df.dropna()

    # 1bis. Retirer les facteurs marqués comme exclus (étape 4 — robustesse)
    from dashboard.strategie_hmm.services.facteurs_config import get_facteurs_exclus
    exclus = {c.upper() for c in get_facteurs_exclus()}
    if exclus:
        cols_actifs = [c for c in df.columns if c.upper() not in exclus]
        if len(cols_actifs) < 2:
            raise RuntimeError(
                f"Trop de facteurs exclus — il reste {len(cols_actifs)} facteur(s) actif(s). "
                "Réactivez au moins 2 facteurs depuis l'onglet « Robustesse statistique »."
            )
        df = df[cols_actifs]

    # 2. Entraîner le HMM
    res = entrainer_hmm(df, n_regimes=2)

    # 3. Confirmation des régimes (filtre d=5)
    seq_brut = res.sequence_regimes
    seq_conf = confirmer_regime(seq_brut, d=d_confirmation)

    # 4. Sauvegarder snapshot HMM (un seul par date d'entraînement)
    date_train = df.index.max().date()
    params, _ = ParametresHMM.objects.update_or_create(
        date_entrainement=date_train,
        defaults={
            "n_observations": len(df),
            "n_regimes": 2,
            "n_facteurs": df.shape[1],
            "matrice_transition": res.matrice_transition.tolist(),
            "moyennes_regime_0": res.moyennes[0].tolist(),
            "moyennes_regime_1": res.moyennes[1].tolist(),
            "covariance_regime_0": res.covariances[0].tolist(),
            "covariance_regime_1": res.covariances[1].tolist(),
            "log_likelihood": res.log_likelihood,
            "converged": res.converged,
        },
    )

    # 5. Sauvegarder le régime courant
    regime_brut = int(seq_brut[-1])
    regime_conf = int(seq_conf[-1])
    proba = res.proba_courant()
    changement = bool(len(seq_conf) >= 2 and seq_conf[-1] != seq_conf[-2])
    regime, _ = RegimeMarche.objects.update_or_create(
        date=date_train,
        defaults={
            "regime_brut": regime_brut,
            "regime_confirme": regime_conf,
            "proba_regime_0": float(proba[0]),
            "proba_regime_1": float(proba[1]),
            "changement": changement,
            "declenche_reallocation": changement,
            "log_likelihood": res.log_likelihood,
        },
    )

    # 6. Optimiser sur le régime confirmé
    mu = res.moyennes[regime_conf]
    V = res.covariances[regime_conf]
    sigma = np.sqrt(np.diag(V))
    optim = OPTIMISEURS.get(strategie, OPTIMISEURS["SHARPE_HMM"])
    w_factor = optim(mu, V, sigma)
    metriques = metriques_attendues(w_factor, mu, V)

    poids_facteurs = {c: float(w) for c, w in zip(df.columns, w_factor)}

    # 7. Scorer les actions — facteurs calculés depuis la BD uniquement
    df_facteurs_societes = calculer_facteurs_toutes_actions(date_ref=date_train)
    # Ne garder que les actions ayant tous les facteurs critiques renseignés
    df_facteurs_societes = df_facteurs_societes.dropna(
        subset=["CAPI", "VARIANCE", "RDT_JOURNALIER"], how="any"
    )

    # 7bis. Persister les facteurs en base pour alimenter la page Stratégie HMM
    # (onglets Composition Buckets, Poids Globaux, Top Actifs, Heatmap, Long-Only).
    _persister_facteurs(df_facteurs_societes, date_train)

    scores = scorer_actions(df_facteurs_societes, poids_facteurs, methode="zscore")
    top = selectionner_top_n(scores, n=nb_actions_top)
    poids_actions = {str(t): float(p) for t, p in top["poids"].items()}

    # 8. Sauvegarder l'allocation
    alloc = AllocationStrategie.objects.create(
        date=date_train,
        strategie=strategie,
        regime=regime,
        poids_facteurs=poids_facteurs,
        poids_actions=poids_actions,
        nb_actions_top=nb_actions_top,
        methode_normalisation="zscore",
        declencheur="pipeline_complet",
        rendement_attendu=metriques["rendement_annualise"],
        volatilite_attendue=metriques["volatilite_annualisee"],
        sharpe_attendu=metriques["sharpe_annualise"],
    )

    return {
        "date": date_train,
        "regime_brut": regime_brut,
        "regime_confirme": regime_conf,
        "proba": proba.tolist(),
        "changement": changement,
        "n_observations": len(df),
        "log_likelihood": res.log_likelihood,
        "converged": res.converged,
        "strategie": strategie,
        "poids_facteurs": poids_facteurs,
        "poids_actions": poids_actions,
        "metriques": metriques,
        "allocation_id": alloc.id,
        "params_hmm_id": params.id,
        "regime_id": regime.id,
        "n_actions_matchees": len(df_facteurs_societes),
        "n_actions_non_matchees": 0,
    }


def _persister_facteurs(df_facteurs: pd.DataFrame, date_ref) -> int:
    """Persiste les valeurs des 13 facteurs (DataFrame [ticker × facteur]) dans
    ``FacteurStrategie`` pour la date donnée. Supprime au préalable les lignes
    existantes à cette date pour garantir l'idempotence du pipeline.

    Retourne le nombre de lignes créées.
    """
    if df_facteurs is None or df_facteurs.empty:
        return 0
    tickers = [str(t) for t in df_facteurs.index]
    actions_map = {a.ticker: a for a in Action.objects.filter(ticker__in=tickers)}
    objs = []
    for ticker, row in df_facteurs.iterrows():
        action = actions_map.get(str(ticker))
        if action is None:
            continue
        for code, val in row.items():
            if val is None or pd.isna(val):
                continue
            objs.append(FacteurStrategie(
                action=action, facteur=str(code), date=date_ref, valeur=float(val),
            ))
    if not objs:
        return 0
    with transaction.atomic():
        FacteurStrategie.objects.filter(date=date_ref).delete()
        FacteurStrategie.objects.bulk_create(objs, batch_size=2000)
    return len(objs)


def reconstruire_historique_regimes() -> pd.DataFrame:
    """Reconstruit l'historique complet des régimes (brut + confirmé) pour
    affichage dans la page régime. Ne persiste pas en base — utilisé pour
    visualisation. Retourne DataFrame [date, regime_brut, regime_confirme,
    proba_0, proba_1].
    """
    df = charger_rendements_depuis_bd()
    if df.empty:
        return pd.DataFrame(
            columns=["regime_brut", "regime_confirme", "proba_0", "proba_1"]
        )
    df.index = pd.to_datetime(df.index)
    df = df.dropna()
    res = entrainer_hmm(df, n_regimes=2)
    seq_conf = confirmer_regime(res.sequence_regimes, d=5)
    return pd.DataFrame({
        "date": df.index,
        "regime_brut": res.sequence_regimes,
        "regime_confirme": seq_conf,
        "proba_0": res.proba_regimes[:, 0],
        "proba_1": res.proba_regimes[:, 1],
    }).set_index("date")


# Libellés des 6 stratégies d'optimisation (cf. OPTIMISEURS).
LIBELLE_STRATEGIES = {
    "SHARPE_HMM": "Maximum Sharpe",
    "DYN_HMM": "Allocation dynamique",
    "MR_HMM": "Maximum Return",
    "RP_HMM": "Risk Parity",
    "MD_HMM": "Maximum Diversification",
    "MV_HMM": "Minimum Variance",
}


def comparer_toutes_strategies(
    params: ParametresHMM,
    regime_confirme: int,
    factor_codes: list[str],
) -> list[dict]:
    """Calcule les 6 vecteurs d'allocation factorielle α sous le régime confirmé.

    Utilise les paramètres (μ, Σ) déjà estimés et persistés dans
    ``ParametresHMM`` — aucun ré-entraînement nécessaire.

    Returns
    -------
    Liste de dicts contenant pour chaque stratégie :
        - code, libelle
        - poids_facteurs : dict {code_facteur: α_f}
        - poids_facteurs_pct : dict {code_facteur: α_f × 100}
        - rendement_annualise, volatilite_annualisee, sharpe_annualise
    """
    if regime_confirme == 0:
        mu = np.array(params.moyennes_regime_0)
        V = np.array(params.covariance_regime_0)
    else:
        mu = np.array(params.moyennes_regime_1)
        V = np.array(params.covariance_regime_1)
    sigma = np.sqrt(np.diag(V))

    results = []
    for code, optim in OPTIMISEURS.items():
        try:
            w = optim(mu, V, sigma)
            m = metriques_attendues(w, mu, V)
        except Exception as exc:  # pragma: no cover
            results.append({
                "code": code,
                "libelle": LIBELLE_STRATEGIES.get(code, code),
                "erreur": str(exc),
                "poids_facteurs": {},
                "poids_facteurs_pct": {},
                "rendement_annualise": 0.0,
                "volatilite_annualisee": 0.0,
                "sharpe_annualise": 0.0,
            })
            continue
        poids = {c: float(v) for c, v in zip(factor_codes, w.tolist())}
        results.append({
            "code": code,
            "libelle": LIBELLE_STRATEGIES.get(code, code),
            "poids_facteurs": poids,
            "poids_facteurs_pct": {c: v * 100.0 for c, v in poids.items()},
            "rendement_annualise": m["rendement_annualise"],
            "volatilite_annualisee": m["volatilite_annualisee"],
            "sharpe_annualise": m["sharpe_annualise"],
        })
    return results
