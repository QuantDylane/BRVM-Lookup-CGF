"""
Modèles dédiés à la Stratégie HMM (Hidden Markov Model) — allocation
multifactorielle conditionnée aux régimes de marché sur la BRVM.

Ces modèles ÉTENDENT le schéma existant sans toucher aux tables actuelles.
Toutes les FK pointent vers les tables existantes (Action) en lecture seule
côté stratégie (PROTECT) pour éviter toute suppression accidentelle d'action
référencée par une position.
"""
from django.db import models

from .models import Action  # FK vers la table existante — non modifiée


# ----------------------------------------------------------------------
# Fondamentaux financiers annuels — 4 tables normalisées (Actif, Passif,
# Compte de résultat, Flux de trésorerie) — granularité (action, exercice).
# Ces données sont absentes du modèle Action existant et nécessaires au
# calcul des facteurs (ROA, ROE, Levier, BtM, EP, SP, DivYield).
# ----------------------------------------------------------------------
class BilanActif(models.Model):
    """Actif au bilan annuel d'une société (clôturé au 31/12 de l'exercice)."""
    action = models.ForeignKey(
        Action, on_delete=models.CASCADE, related_name="bilans_actif"
    )
    exercice = models.IntegerField(db_index=True, help_text="Année de l'exercice (ex: 2024)")
    total_actif = models.FloatField(null=True, blank=True, help_text="Total actif = Total passif (FCFA)")
    actif_courants = models.FloatField(null=True, blank=True)
    treso_active = models.FloatField(null=True, blank=True, help_text="Trésorerie active")
    source = models.CharField(max_length=50, default="excel_import")
    date_import = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("action", "exercice")]
        ordering = ["-exercice", "action"]
        verbose_name = "Bilan – Actif"
        verbose_name_plural = "Bilans – Actif"

    def __str__(self):
        return f"{self.action.ticker} actif {self.exercice}"


class BilanPassif(models.Model):
    """Passif au bilan annuel d'une société."""
    action = models.ForeignKey(
        Action, on_delete=models.CASCADE, related_name="bilans_passif"
    )
    exercice = models.IntegerField(db_index=True)
    capitaux_propres = models.FloatField(null=True, blank=True)
    total_dettes = models.FloatField(null=True, blank=True)
    passif_courants = models.FloatField(null=True, blank=True)
    resultat_non_reparti = models.FloatField(null=True, blank=True, help_text="Report à nouveau")
    source = models.CharField(max_length=50, default="excel_import")
    date_import = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("action", "exercice")]
        ordering = ["-exercice", "action"]
        verbose_name = "Bilan – Passif"
        verbose_name_plural = "Bilans – Passif"

    def __str__(self):
        return f"{self.action.ticker} passif {self.exercice}"


class CompteResultat(models.Model):
    """Compte de résultat annuel + dividende versé au titre de l'exercice."""
    action = models.ForeignKey(
        Action, on_delete=models.CASCADE, related_name="comptes_resultat"
    )
    exercice = models.IntegerField(db_index=True)
    chiffre_affaires = models.FloatField(null=True, blank=True)
    ebit = models.FloatField(null=True, blank=True, help_text="Résultat d'exploitation")
    resultat_net = models.FloatField(null=True, blank=True)
    dividende_annuel = models.FloatField(
        null=True, blank=True,
        help_text="Dividende versé au titre de l'exercice (par action, FCFA)",
    )
    source = models.CharField(max_length=50, default="excel_import")
    date_import = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("action", "exercice")]
        ordering = ["-exercice", "action"]
        verbose_name = "Compte de Résultat"
        verbose_name_plural = "Comptes de Résultat"

    def __str__(self):
        return f"{self.action.ticker} CR {self.exercice}"


class FluxTresorerie(models.Model):
    """Tableau de flux de trésorerie annuel."""
    action = models.ForeignKey(
        Action, on_delete=models.CASCADE, related_name="flux_tresorerie"
    )
    exercice = models.IntegerField(db_index=True)
    cfo = models.FloatField(null=True, blank=True, help_text="Cash-flow opérationnel")
    capex = models.FloatField(null=True, blank=True, help_text="Capex (souvent négatif)")
    fcf_operationnel = models.FloatField(null=True, blank=True)
    fcf_treso_disponible = models.FloatField(null=True, blank=True)
    source = models.CharField(max_length=50, default="excel_import")
    date_import = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("action", "exercice")]
        ordering = ["-exercice", "action"]
        verbose_name = "Flux de Trésorerie"
        verbose_name_plural = "Flux de Trésorerie"

    def __str__(self):
        return f"{self.action.ticker} FT {self.exercice}"


# ----------------------------------------------------------------------
# Valeurs des 13 facteurs par action et par date.
# ----------------------------------------------------------------------
class FacteurStrategie(models.Model):
    NOMS_FACTEURS = [
        ("BtM", "Book-to-Market"),
        ("EP", "Earnings-to-Price"),
        ("SP", "Sales-to-Price"),
        ("ROA", "Return on Assets"),
        ("ROE", "Return on Equity"),
        ("LEVIER", "Levier financier"),
        ("DIV_YIELD", "Dividend Yield"),
        ("VARIANCE", "Variance"),
        ("RDT_JOURNALIER", "Rendement journalier"),
        ("MOM_6M", "6-Month Momentum"),
        ("VOLUME", "Volume de transaction"),
        ("BETA", "Bêta"),
        ("CAPI", "Capitalisation boursière"),
    ]

    action = models.ForeignKey(
        Action, on_delete=models.CASCADE, related_name="facteurs_strategie"
    )
    facteur = models.CharField(max_length=20, choices=NOMS_FACTEURS, db_index=True)
    date = models.DateField(db_index=True)
    valeur = models.FloatField()
    date_calcul = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("action", "facteur", "date")]
        ordering = ["-date", "facteur"]
        indexes = [models.Index(fields=["date", "facteur"])]
        verbose_name = "Facteur Stratégie"
        verbose_name_plural = "Facteurs Stratégie"

    def __str__(self):
        return f"{self.action.ticker} {self.facteur}={self.valeur:.4f} @ {self.date}"


# ----------------------------------------------------------------------
# Rendements journaliers des 13 portefeuilles factoriels long-short.
# ----------------------------------------------------------------------
class RendementPortefeuilleFactoriel(models.Model):
    facteur = models.CharField(max_length=20, db_index=True)
    date = models.DateField(db_index=True)
    rendement = models.FloatField()

    class Meta:
        unique_together = [("facteur", "date")]
        ordering = ["-date", "facteur"]
        verbose_name = "Rendement Portefeuille Factoriel"
        verbose_name_plural = "Rendements Portefeuilles Factoriels"

    def __str__(self):
        return f"{self.facteur} @ {self.date} = {self.rendement:.5f}"


# ----------------------------------------------------------------------
# Régime de marché détecté par le HMM à une date donnée.
# ----------------------------------------------------------------------
class RegimeMarche(models.Model):
    date = models.DateField(unique=True, db_index=True)
    regime_brut = models.IntegerField(help_text="Régime brut prédit (0 ou 1)")
    regime_confirme = models.IntegerField(
        help_text="Régime confirmé après filtre de stabilité (d=5 jours)"
    )
    proba_regime_0 = models.FloatField(default=0.0)
    proba_regime_1 = models.FloatField(default=0.0)
    changement = models.BooleanField(default=False)
    declenche_reallocation = models.BooleanField(default=False)
    log_likelihood = models.FloatField(null=True, blank=True)
    date_calcul = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]
        verbose_name = "Régime de Marché"
        verbose_name_plural = "Régimes de Marché"

    def __str__(self):
        return f"{self.date} → régime {self.regime_confirme}"


# ----------------------------------------------------------------------
# Snapshot des paramètres du HMM à une date d'entraînement.
# ----------------------------------------------------------------------
class ParametresHMM(models.Model):
    date_entrainement = models.DateField(unique=True, db_index=True)
    n_observations = models.IntegerField()
    n_regimes = models.IntegerField(default=2)
    n_facteurs = models.IntegerField(default=13)
    matrice_transition = models.JSONField()
    moyennes_regime_0 = models.JSONField()
    moyennes_regime_1 = models.JSONField()
    covariance_regime_0 = models.JSONField()
    covariance_regime_1 = models.JSONField()
    log_likelihood = models.FloatField()
    converged = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_entrainement"]
        verbose_name = "Paramètres HMM"
        verbose_name_plural = "Paramètres HMM"

    def __str__(self):
        return f"HMM entraîné au {self.date_entrainement}"


# ----------------------------------------------------------------------
# Allocation factorielle + actions issue de l'optimisation.
# ----------------------------------------------------------------------
class AllocationStrategie(models.Model):
    STRATEGIE_CHOICES = [
        ("SHARPE_HMM", "Sharpe HMM"),
        ("DYN_HMM", "Dynamic HMM"),
        ("MR_HMM", "Max Return HMM"),
        ("RP_HMM", "Risk Parity HMM"),
        ("MD_HMM", "Max Diversification HMM"),
        ("MV_HMM", "Min Variance HMM"),
    ]
    date = models.DateField(db_index=True)
    strategie = models.CharField(
        max_length=20, choices=STRATEGIE_CHOICES, default="SHARPE_HMM"
    )
    regime = models.ForeignKey(
        RegimeMarche, on_delete=models.SET_NULL, null=True, blank=True
    )
    poids_facteurs = models.JSONField(help_text="Dict {facteur: poids}")
    poids_actions = models.JSONField(help_text="Dict {ticker: poids}")
    nb_actions_top = models.IntegerField(default=15)
    methode_normalisation = models.CharField(max_length=20, default="zscore")
    declencheur = models.CharField(max_length=50, default="regime_change")
    rendement_attendu = models.FloatField(null=True, blank=True)
    volatilite_attendue = models.FloatField(null=True, blank=True)
    sharpe_attendu = models.FloatField(null=True, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-date_creation"]
        verbose_name = "Allocation Stratégie"
        verbose_name_plural = "Allocations Stratégie"

    def __str__(self):
        return f"{self.strategie} @ {self.date}"


# ----------------------------------------------------------------------
# Portefeuille effectivement constitué et suivi.
# ----------------------------------------------------------------------
class PortefeuilleStrategie(models.Model):
    nom = models.CharField(max_length=100)
    strategie = models.CharField(max_length=20, default="SHARPE_HMM")
    capital_initial = models.FloatField()
    capital_courant = models.FloatField()
    date_creation = models.DateTimeField(auto_now_add=True)
    actif = models.BooleanField(default=True)
    frais_courtage_pct = models.FloatField(default=0.01)
    derniere_allocation = models.ForeignKey(
        AllocationStrategie,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="portefeuilles",
    )

    class Meta:
        ordering = ["-date_creation"]
        verbose_name = "Portefeuille Stratégie"
        verbose_name_plural = "Portefeuilles Stratégie"

    def __str__(self):
        return f"{self.nom} ({self.strategie})"


class PositionStrategie(models.Model):
    portefeuille = models.ForeignKey(
        PortefeuilleStrategie, on_delete=models.CASCADE, related_name="positions"
    )
    action = models.ForeignKey(Action, on_delete=models.PROTECT)
    quantite = models.IntegerField()
    prix_achat_moyen = models.FloatField()
    poids_cible = models.FloatField()
    poids_effectif = models.FloatField(default=0.0)
    date_entree = models.DateField()
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-poids_cible"]
        verbose_name = "Position Stratégie"
        verbose_name_plural = "Positions Stratégie"

    def __str__(self):
        return f"{self.portefeuille.nom} / {self.action.ticker} × {self.quantite}"


class OrdreReallocation(models.Model):
    TYPE_CHOICES = [
        ("ACHAT", "Achat"),
        ("VENTE", "Vente"),
        ("CONSERVER", "Conserver"),
    ]
    STATUT_CHOICES = [
        ("EN_ATTENTE", "En attente"),
        ("EXECUTE", "Exécuté"),
        ("ANNULE", "Annulé"),
    ]

    portefeuille = models.ForeignKey(
        PortefeuilleStrategie, on_delete=models.CASCADE, related_name="ordres"
    )
    allocation = models.ForeignKey(AllocationStrategie, on_delete=models.CASCADE)
    action = models.ForeignKey(Action, on_delete=models.PROTECT)
    type_ordre = models.CharField(max_length=10, choices=TYPE_CHOICES)
    quantite = models.IntegerField()
    prix_indicatif = models.FloatField()
    montant_estime = models.FloatField()
    statut = models.CharField(
        max_length=15, choices=STATUT_CHOICES, default="EN_ATTENTE"
    )
    date_generation = models.DateTimeField(auto_now_add=True)
    date_execution = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date_generation"]
        verbose_name = "Ordre de Réallocation"
        verbose_name_plural = "Ordres de Réallocation"

    def __str__(self):
        return f"{self.type_ordre} {self.action.ticker} × {self.quantite}"
