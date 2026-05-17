"""Commande management : génère une nouvelle allocation HMM en exécutant
le pipeline complet (HMM → optimisation → scoring).

Usage:
    python manage.py generer_allocation_strategie --strategie SHARPE_HMM --top 15
"""
from django.core.management.base import BaseCommand

from dashboard.strategie_hmm.services.strategie_service import (
    executer_pipeline_complet,
    construire_rendements_si_necessaire,
)


class Command(BaseCommand):
    help = "Génère une allocation Stratégie HMM (Sharpe HMM par défaut)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--strategie",
            default="SHARPE_HMM",
            choices=["SHARPE_HMM", "DYN_HMM", "MR_HMM", "RP_HMM", "MD_HMM", "MV_HMM"],
        )
        parser.add_argument("--top", type=int, default=15)
        parser.add_argument("--d", type=int, default=5,
                            help="Période de confirmation des régimes")
        parser.add_argument("--reconstruire-rendements", action="store_true",
                            help="Reconstruit les rendements des 13 portefeuilles depuis la BD")

    def handle(self, *args, **opts):
        if opts["reconstruire_rendements"]:
            n = construire_rendements_si_necessaire(force=True)
            self.stdout.write(self.style.SUCCESS(f"{n} rendements en base"))

        res = executer_pipeline_complet(
            strategie=opts["strategie"],
            nb_actions_top=opts["top"],
            d_confirmation=opts["d"],
        )
        self.stdout.write(self.style.SUCCESS(
            f"Allocation #{res['allocation_id']} générée pour le {res['date']}"
        ))
        self.stdout.write(
            f"  Régime confirmé: {res['regime_confirme']}  "
            f"P(0)={res['proba'][0]:.3f} P(1)={res['proba'][1]:.3f}"
        )
        self.stdout.write(f"  Changement détecté: {res['changement']}")
        m = res["metriques"]
        self.stdout.write(
            f"  Rendement annualisé attendu: {m['rendement_annualise']*100:.2f}%  "
            f"Vol: {m['volatilite_annualisee']*100:.2f}%  "
            f"Sharpe: {m['sharpe_annualise']:.3f}"
        )
        self.stdout.write(self.style.NOTICE("  Top 10 actions:"))
        for tk, p in list(res["poids_actions"].items())[:10]:
            self.stdout.write(f"    {tk:10s}  {p*100:5.2f}%")
