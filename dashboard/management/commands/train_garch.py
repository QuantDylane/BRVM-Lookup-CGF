"""Entraînement mensuel des modèles GARCH par action BRVM.

Pour chaque ``Action``, récupère la série de cours de clôture, calcule les
rendements log en %, et met en compétition GARCH(1,1), GJR-GARCH(1,1,1) et
EGARCH(1,1) via le package `arch` (Kevin Sheppard). Sélection par BIC.

Convention : les rendements sont passés en % (100 × log_return) pour que
l'optimiseur du package `arch` converge proprement — c'est le default
recommandé par l'auteur.

Usage:
    python manage.py train_garch
    python manage.py train_garch --ticker SGBC.ci
    python manage.py train_garch --min-obs 500
"""
from __future__ import annotations

import math
import warnings

import numpy as np
from django.core.management.base import BaseCommand

from dashboard.models import Action, GarchModel, HistoriqueAction

# Le package arch émet beaucoup de FutureWarning / ConvergenceWarning ;
# on les ignore proprement.
warnings.filterwarnings("ignore")

TRADING_DAYS_PER_YEAR = 252
VOL_SERIES_KEEP = 252  # nb de points conservés dans le JSON pour mini-chart


def _log_returns_pct(prices: list[float]) -> np.ndarray:
    """Rendements log en %, NaN/inf filtrés."""
    arr = np.asarray(prices, dtype=float)
    arr = arr[np.isfinite(arr) & (arr > 0)]
    if arr.size < 2:
        return np.array([])
    r = 100.0 * np.diff(np.log(arr))
    return r[np.isfinite(r)]


def _fit_one(returns: np.ndarray, model_type: str):
    """Estime un modèle. Retourne (result, params_dict) ou (None, None) si échec.

    params_dict contient : omega, alpha, beta, gamma (gamma=None si pas applicable),
                            persistence, aic, bic, llf, n_obs.
    """
    from arch import arch_model

    kwargs = dict(mean="Zero", p=1, q=1, dist="normal", rescale=False)
    if model_type == "GARCH":
        kwargs.update(vol="GARCH", o=0)
    elif model_type == "GJR-GARCH":
        kwargs.update(vol="GARCH", o=1)
    elif model_type == "EGARCH":
        kwargs.update(vol="EGARCH", o=1)
    else:
        return None, None

    try:
        model = arch_model(returns, **kwargs)
        res = model.fit(disp="off", show_warning=False)
    except Exception:
        return None, None

    if not np.isfinite(res.loglikelihood):
        return None, None

    p = res.params
    omega = float(p.get("omega", np.nan))
    alpha = float(p.get("alpha[1]", np.nan))
    beta = float(p.get("beta[1]", np.nan))
    gamma = float(p.get("gamma[1]", np.nan)) if "gamma[1]" in p.index else None

    # Persistance : α+β pour GARCH ; α+β+γ/2 pour GJR (sous résidu normal)
    if model_type == "GARCH":
        persistence = alpha + beta
    elif model_type == "GJR-GARCH":
        persistence = alpha + beta + (gamma or 0.0) / 2.0
    else:  # EGARCH — persistance = β (coef sur ln σ²_{t-1})
        persistence = beta

    params = {
        "omega": omega if np.isfinite(omega) else None,
        "alpha": alpha if np.isfinite(alpha) else None,
        "beta": beta if np.isfinite(beta) else None,
        "gamma": gamma if (gamma is not None and np.isfinite(gamma)) else None,
        "persistence": persistence if np.isfinite(persistence) else None,
        "aic": float(res.aic),
        "bic": float(res.bic),
        "llf": float(res.loglikelihood),
        "n_obs": int(res.nobs),
    }
    return res, params


class Command(BaseCommand):
    help = "Entraîne un modèle GARCH par action (sélection BIC)."

    def add_arguments(self, parser):
        parser.add_argument("--ticker", default=None,
                            help="Limite à un seul ticker (ex: SGBC.ci).")
        parser.add_argument("--min-obs", type=int, default=500,
                            help="Nb minimum d'observations (rendements). Défaut 500.")

    def handle(self, *args, **opts):
        ticker_filter = opts.get("ticker")
        min_obs = int(opts.get("min_obs") or 500)

        qs = Action.objects.all().order_by("ticker")
        if ticker_filter:
            qs = qs.filter(ticker=ticker_filter)
        actions = list(qs)
        if not actions:
            self.stderr.write(self.style.ERROR("Aucune action à traiter."))
            return

        nb_ok = 0
        nb_insuf = 0
        nb_failed = 0

        for i, action in enumerate(actions, 1):
            ticker = action.ticker
            prices = list(
                HistoriqueAction.objects
                .filter(action=action, cloture__isnull=False)
                .order_by("date")
                .values_list("cloture", flat=True)
            )
            returns = _log_returns_pct(prices)
            n = int(returns.size)

            if n < min_obs:
                GarchModel.objects.update_or_create(
                    action=action,
                    defaults={
                        "model_type": "INSUFFISANT",
                        "n_obs": n,
                        "erreur_message": f"{n} obs < min {min_obs}",
                        "p": None, "q": None, "o": None,
                        "omega": None, "alpha": None, "beta": None, "gamma": None,
                        "persistence": None, "aic": None, "bic": None, "llf": None,
                        "vol_actuelle_annualisee": None,
                        "vol_conditionnelle_json": [],
                    },
                )
                self.stdout.write(self.style.WARNING(
                    f"[{i}/{len(actions)}] {ticker}: INSUFFISANT ({n} obs)"
                ))
                nb_insuf += 1
                continue

            # Compétition entre les 3 modèles
            candidates = []
            for mtype in ("GARCH", "GJR-GARCH", "EGARCH"):
                res, params = _fit_one(returns, mtype)
                if res is not None and params and params.get("bic") is not None:
                    candidates.append((params["bic"], mtype, res, params))

            if not candidates:
                GarchModel.objects.update_or_create(
                    action=action,
                    defaults={
                        "model_type": "FAILED",
                        "n_obs": n,
                        "erreur_message": "Aucun modèle n'a convergé",
                        "p": None, "q": None, "o": None,
                        "omega": None, "alpha": None, "beta": None, "gamma": None,
                        "persistence": None, "aic": None, "bic": None, "llf": None,
                        "vol_actuelle_annualisee": None,
                        "vol_conditionnelle_json": [],
                    },
                )
                self.stderr.write(self.style.ERROR(
                    f"[{i}/{len(actions)}] {ticker}: FAILED (aucune convergence)"
                ))
                nb_failed += 1
                continue

            # Le meilleur BIC gagne (BIC le plus bas)
            candidates.sort(key=lambda x: x[0])
            best_bic, best_type, best_res, best_params = candidates[0]

            # Volatilité conditionnelle quotidienne (en % puisque returns en %)
            cond_vol_pct = np.asarray(best_res.conditional_volatility, dtype=float)
            # On la ramène en fraction (÷100) puis on annualise
            sigma_T = float(cond_vol_pct[-1]) / 100.0 if cond_vol_pct.size else None
            vol_annuelle = (
                sigma_T * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0
                if sigma_T is not None and np.isfinite(sigma_T) else None
            )
            # Série conservée : derniers VOL_SERIES_KEEP points, annualisés en %
            tail = cond_vol_pct[-VOL_SERIES_KEEP:] if cond_vol_pct.size else np.array([])
            vol_serie = [
                float(v / 100.0 * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)
                for v in tail if np.isfinite(v)
            ]

            o_val = 1 if best_type in ("GJR-GARCH", "EGARCH") else 0

            GarchModel.objects.update_or_create(
                action=action,
                defaults={
                    "model_type": best_type,
                    "p": 1, "q": 1, "o": o_val,
                    "omega": best_params["omega"],
                    "alpha": best_params["alpha"],
                    "beta": best_params["beta"],
                    "gamma": best_params["gamma"],
                    "persistence": best_params["persistence"],
                    "aic": best_params["aic"],
                    "bic": best_params["bic"],
                    "llf": best_params["llf"],
                    "n_obs": best_params["n_obs"],
                    "vol_actuelle_annualisee": vol_annuelle,
                    "vol_conditionnelle_json": vol_serie,
                    "erreur_message": "",
                },
            )

            vol_str = f"{vol_annuelle:.1f}%" if vol_annuelle is not None else "—"
            self.stdout.write(
                f"[{i}/{len(actions)}] {ticker}: {best_type} "
                f"(BIC={best_bic:.1f}, vol_an={vol_str}, n={n})"
            )
            nb_ok += 1

        self.stdout.write(self.style.SUCCESS(
            f"\nTerminé. OK={nb_ok}  Insuffisants={nb_insuf}  Échecs={nb_failed}"
        ))
