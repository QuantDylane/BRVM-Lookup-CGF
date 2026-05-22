from django.db import models


class Action(models.Model):
    """Titre coté sur la BRVM (action)"""
    ticker = models.CharField(max_length=20, unique=True, db_index=True)
    nom = models.CharField(max_length=200, blank=True, default="")
    pays = models.CharField(max_length=5, blank=True, default="")
    secteur = models.CharField(max_length=100, blank=True, default="")
    isin = models.CharField(max_length=20, blank=True, default="")
    description = models.TextField(blank=True, default="")
    nombre_actions = models.BigIntegerField(null=True, blank=True)
    flottant_pct = models.FloatField(null=True, blank=True)
    chiffre_affaires = models.FloatField(null=True, blank=True)
    resultat_net = models.FloatField(null=True, blank=True)
    bnpa = models.FloatField(null=True, blank=True)
    per = models.FloatField(null=True, blank=True)
    dividende = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["ticker"]
        verbose_name = "Action"
        verbose_name_plural = "Actions"

    def __str__(self):
        return self.ticker


class Indice(models.Model):
    """Indice boursier BRVM"""
    ticker = models.CharField(max_length=30, unique=True, db_index=True)
    nom = models.CharField(max_length=200, blank=True, default="")
    description = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["ticker"]
        verbose_name = "Indice"
        verbose_name_plural = "Indices"

    def __str__(self):
        return self.ticker


class HistoriqueAction(models.Model):
    """Données historiques OHLCV pour une action"""
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="historiques")
    date = models.DateField(db_index=True)
    ouverture = models.FloatField(null=True, blank=True)
    plus_haut = models.FloatField(null=True, blank=True)
    plus_bas = models.FloatField(null=True, blank=True)
    cloture = models.FloatField(null=True, blank=True)
    volume_titres = models.BigIntegerField(null=True, blank=True)
    volume_fcfa = models.FloatField(null=True, blank=True)
    variation_pct = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-date"]
        unique_together = ["action", "date"]
        verbose_name = "Historique Action"
        verbose_name_plural = "Historiques Actions"
        indexes = [
            models.Index(fields=["action", "date"]),
        ]

    def __str__(self):
        return f"{self.action.ticker} - {self.date}"


class HistoriqueIndice(models.Model):
    """Données historiques OHLCV pour un indice"""
    indice = models.ForeignKey(Indice, on_delete=models.CASCADE, related_name="historiques")
    date = models.DateField(db_index=True)
    ouverture = models.FloatField(null=True, blank=True)
    plus_haut = models.FloatField(null=True, blank=True)
    plus_bas = models.FloatField(null=True, blank=True)
    cloture = models.FloatField(null=True, blank=True)
    volume_titres = models.BigIntegerField(null=True, blank=True)
    volume_fcfa = models.FloatField(null=True, blank=True)
    variation_pct = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-date"]
        unique_together = ["indice", "date"]
        verbose_name = "Historique Indice"
        verbose_name_plural = "Historiques Indices"
        indexes = [
            models.Index(fields=["indice", "date"]),
        ]

    def __str__(self):
        return f"{self.indice.ticker} - {self.date}"


class News(models.Model):
    """Article d'actualité BRVM"""
    id_source = models.IntegerField(unique=True, db_index=True)
    titre = models.CharField(max_length=500)
    date_publication = models.DateTimeField(null=True, blank=True, db_index=True)
    auteur = models.CharField(max_length=200, blank=True, default="")
    categorie = models.CharField(max_length=200, blank=True, default="")
    contenu = models.TextField(blank=True, default="")
    image_url = models.URLField(max_length=500, blank=True, default="")
    url = models.URLField(max_length=500, blank=True, default="")

    class Meta:
        ordering = ["-date_publication"]
        verbose_name = "Actualité"
        verbose_name_plural = "Actualités"

    def __str__(self):
        return self.titre[:80]


class ScrapingLog(models.Model):
    """Journal des exécutions de scraping"""
    type_scraping = models.CharField(max_length=50)
    date_debut = models.DateTimeField(auto_now_add=True)
    date_fin = models.DateTimeField(null=True, blank=True)
    statut = models.CharField(max_length=20, default="en_cours")
    message = models.TextField(blank=True, default="")
    nb_elements = models.IntegerField(default=0)

    class Meta:
        ordering = ["-date_debut"]
        verbose_name = "Log de Scraping"
        verbose_name_plural = "Logs de Scraping"

    def __str__(self):
        return f"{self.type_scraping} - {self.date_debut} - {self.statut}"


class ApiConfig(models.Model):
    """Configuration des clés API (Claude, etc.) — stockée en base, modifiable depuis l'app."""
    cle = models.CharField(max_length=50, unique=True)
    valeur = models.CharField(max_length=500)
    description = models.CharField(max_length=200, blank=True, default="")
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuration API"
        verbose_name_plural = "Configurations API"

    def __str__(self):
        return f"{self.cle}"

    @classmethod
    def get(cls, cle, default=""):
        try:
            return cls.objects.get(cle=cle).valeur
        except cls.DoesNotExist:
            return default

    @classmethod
    def set(cls, cle, valeur, description=""):
        obj, _ = cls.objects.update_or_create(
            cle=cle, defaults={"valeur": valeur, "description": description}
        )
        return obj


class CommentHistory(models.Model):
    """Historique des commentaires générés pour les factsheets."""
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="commentaires")
    analyse = models.TextField(verbose_name="Commentaire d'analyse")
    recommandation = models.TextField(verbose_name="Recommandation")
    disclaimer = models.TextField(blank=True, default="")
    donnees_contexte = models.JSONField(default=dict, verbose_name="Données de contexte")
    modele_ia = models.CharField(max_length=100, default="claude-sonnet-4-20250514")
    favori = models.BooleanField(default=False)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_creation"]
        verbose_name = "Historique commentaire"
        verbose_name_plural = "Historique commentaires"

    def __str__(self):
        return f"{self.action.ticker} - {self.date_creation.strftime('%d/%m/%Y %H:%M')}"


class TradingSignal(models.Model):
    """Historique des signaux de trading générés par l'IA."""
    SIGNAL_CHOICES = [
        ('ACHAT', 'Achat'),
        ('VENTE', 'Vente'),
        ('NEUTRE', 'Neutre'),
    ]
    
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="signaux")
    date_generation = models.DateTimeField(auto_now_add=True, db_index=True)
    periode_analyse = models.CharField(max_length=10, default="1y", verbose_name="Période d'analyse")
    
    # Signal
    signal = models.CharField(max_length=10, choices=SIGNAL_CHOICES)
    confiance = models.FloatField(verbose_name="Niveau de confiance (%)")
    
    # Niveaux
    prix_entree = models.FloatField(null=True, blank=True, verbose_name="Prix d'entrée")
    stop_loss = models.FloatField(null=True, blank=True)
    take_profit = models.FloatField(null=True, blank=True)
    risk_reward = models.FloatField(null=True, blank=True, verbose_name="Ratio Risque/Rendement")
    
    # Supports et résistances (stockés en JSON)
    supports = models.JSONField(default=list, verbose_name="Niveaux de support")
    resistances = models.JSONField(default=list, verbose_name="Niveaux de résistance")
    
    # Analyse
    justification = models.TextField(verbose_name="Justification technique")
    indicateurs_utilises = models.JSONField(default=list, verbose_name="Indicateurs utilisés")
    valeurs_indicateurs = models.JSONField(default=dict, verbose_name="Valeurs des indicateurs")
    
    # Métadonnées
    modele_ia = models.CharField(max_length=50, default="sonnet")
    prix_actuel = models.FloatField(null=True, blank=True, verbose_name="Prix au moment du signal")
    favori = models.BooleanField(default=False)

    class Meta:
        ordering = ["-date_generation"]
        verbose_name = "Signal de trading"
        verbose_name_plural = "Signaux de trading"
        indexes = [
            models.Index(fields=["action", "-date_generation"]),
        ]

    def __str__(self):
        return f"{self.action.ticker} - {self.signal} ({self.confiance}%) - {self.date_generation.strftime('%d/%m/%Y %H:%M')}"


class Portefeuille(models.Model):
    """Portefeuille de simulation."""
    nom = models.CharField(max_length=200)
    montant_initial = models.FloatField(verbose_name="Montant alloué (FCFA)")
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)
    frais_courtage_pct = models.FloatField(default=1.0, verbose_name="Frais de courtage (%)")

    class Meta:
        ordering = ["-date_modification"]
        verbose_name = "Portefeuille"
        verbose_name_plural = "Portefeuilles"

    def __str__(self):
        return self.nom

    @property
    def montant_investi(self):
        """Somme des montants investis (prix_achat * quantite) pour les lignes actives."""
        total = 0
        for ligne in self.lignes.filter(active=True):
            total += ligne.prix_achat * ligne.quantite
        return total

    @property
    def liquidite(self):
        """Partie non investie du portefeuille."""
        return self.montant_initial - self.montant_investi

    @property
    def valeur_flottante(self):
        """Valeur actuelle des positions (basée sur le dernier cours)."""
        total = 0
        for ligne in self.lignes.filter(active=True):
            dernier = HistoriqueAction.objects.filter(
                action=ligne.action
            ).order_by("-date").values_list("cloture", flat=True).first()
            if dernier:
                total += dernier * ligne.quantite
        return total

    @property
    def valeur_totale(self):
        """Valeur totale = liquidité + valeur flottante."""
        return self.liquidite + self.valeur_flottante

    @property
    def pnl(self):
        """Profit/Perte = valeur_totale - montant_initial."""
        return self.valeur_totale - self.montant_initial

    @property
    def pnl_pct(self):
        """Profit/Perte en pourcentage."""
        if self.montant_initial == 0:
            return 0
        return (self.pnl / self.montant_initial) * 100


class LignePortefeuille(models.Model):
    """Ligne d'investissement dans un portefeuille."""
    portefeuille = models.ForeignKey(Portefeuille, on_delete=models.CASCADE, related_name="lignes")
    action = models.ForeignKey(Action, on_delete=models.CASCADE)
    quantite = models.IntegerField(verbose_name="Nombre de titres")
    prix_achat = models.FloatField(verbose_name="Prix d'achat unitaire (FCFA)")
    date_achat = models.DateField(verbose_name="Date d'achat")
    frais = models.FloatField(default=0, verbose_name="Frais de transaction (FCFA)")
    active = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date_creation"]
        verbose_name = "Ligne de portefeuille"
        verbose_name_plural = "Lignes de portefeuille"

    def __str__(self):
        return f"{self.portefeuille.nom} - {self.action.ticker} x{self.quantite}"

    @property
    def montant_investi(self):
        return self.prix_achat * self.quantite

    @property
    def cout_total(self):
        """Montant investi + frais."""
        return self.montant_investi + self.frais

    @property
    def cours_actuel(self):
        dernier = HistoriqueAction.objects.filter(
            action=self.action
        ).order_by("-date").values_list("cloture", flat=True).first()
        return dernier

    @property
    def valeur_actuelle(self):
        cours = self.cours_actuel
        if cours:
            return cours * self.quantite
        return self.montant_investi

    @property
    def pnl(self):
        return self.valeur_actuelle - self.cout_total

    @property
    def pnl_pct(self):
        if self.cout_total == 0:
            return 0
        return (self.pnl / self.cout_total) * 100

    @property
    def poids_pct(self):
        """Poids de cette ligne dans la valeur flottante du portefeuille."""
        val_flottante = self.portefeuille.valeur_flottante
        if val_flottante == 0:
            return 0
        return (self.valeur_actuelle / val_flottante) * 100


class IndicateurCache(models.Model):
    """Cache des indicateurs techniques calculés par l'AnalyseAgent.
    Évite de recalculer à chaque consultation de la page Analyse Actions."""
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="indicateurs_cache")
    date_calcul = models.DateTimeField(auto_now=True, db_index=True)
    indicateurs_json = models.JSONField(default=dict)

    class Meta:
        verbose_name = "Cache Indicateur"
        verbose_name_plural = "Cache Indicateurs"
        unique_together = ["action"]

    def __str__(self):
        return f"{self.action.ticker} — {self.date_calcul}"


class SignalChangement(models.Model):
    """Historique des changements de signal pour une action.
    Enregistré quand le signal passe d'un état à un autre (ex: NEUTRE→ACHAT)."""
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name="changements_signal")
    ancien_signal = models.CharField(max_length=10)
    nouveau_signal = models.CharField(max_length=10)
    ancien_confiance = models.FloatField(null=True, blank=True)
    nouveau_confiance = models.FloatField(null=True, blank=True)
    signal_precedent = models.ForeignKey(TradingSignal, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    signal_nouveau = models.ForeignKey(TradingSignal, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    justification = models.TextField(blank=True, default="")
    date = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Changement de Signal"
        verbose_name_plural = "Changements de Signaux"
        ordering = ["-date"]
        indexes = [
            models.Index(fields=["action", "-date"]),
        ]

    def __str__(self):
        return f"{self.action.ticker}: {self.ancien_signal} → {self.nouveau_signal} ({self.date})"


class FondamentauxAnnuel(models.Model):
    """Fondamentaux annuels scrapés depuis Sikafinance (matrice 5 ans).

    Une ligne = (action, exercice). Les montants CA / RN sont stockés
    tels quels (en millions de FCFA d'après Sikafinance). Les pourcentages
    de croissance et le BNPA / PER / Dividende sont en valeurs brutes.
    """
    action = models.ForeignKey(
        Action, on_delete=models.CASCADE, related_name="fondamentaux_annuels"
    )
    exercice = models.IntegerField(db_index=True, help_text="Année de l'exercice (ex: 2024)")

    chiffre_affaires = models.FloatField(null=True, blank=True, help_text="CA en millions FCFA")
    croissance_ca = models.FloatField(null=True, blank=True, help_text="Croissance CA en %")
    resultat_net = models.FloatField(null=True, blank=True, help_text="RN en millions FCFA")
    croissance_rn = models.FloatField(null=True, blank=True, help_text="Croissance RN en %")
    bnpa = models.FloatField(null=True, blank=True, help_text="Bénéfice net par action (FCFA)")
    per = models.FloatField(null=True, blank=True, help_text="Price Earning Ratio")
    dividende = models.FloatField(null=True, blank=True, help_text="Dividende par action (FCFA)")

    source = models.CharField(max_length=50, default="sikafinance")
    source_url = models.URLField(blank=True, default="")
    date_import = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("action", "exercice")]
        ordering = ["action", "-exercice"]
        verbose_name = "Fondamental Annuel (scrap)"
        verbose_name_plural = "Fondamentaux Annuels (scrap)"
        indexes = [models.Index(fields=["action", "-exercice"])]

    def __str__(self):
        return f"{self.action.ticker} — {self.exercice}"

    @property
    def rendement_dividende(self):
        """Yield brut = dividende / cours estimé via BNPA × PER, en %."""
        if self.dividende and self.bnpa and self.per:
            cours = self.bnpa * self.per
            if cours:
                return self.dividende / cours * 100
        return None

    @property
    def payout_ratio(self):
        """Taux de distribution = dividende / BNPA, en %."""
        if self.dividende and self.bnpa:
            return self.dividende / self.bnpa * 100
        return None


class ConseilSikafinance(models.Model):
    """Snapshot quotidien du conseil Sikafinance pour une action.

    Sikafinance publie une image (acheter.gif / renforcer.gif / conserver.gif /
    alleger.gif / vendre.gif) sur /analyses/conseil/{ticker}. On stocke un
    snapshot par jour pour reconstituer un historique exploitable.
    """
    CODE_CHOICES = [
        ("ACHETER", "Acheter"),
        ("RENFORCER", "Renforcer"),
        ("CONSERVER", "Conserver"),
        ("ALLEGER", "Alléger"),
        ("VENDRE", "Vendre"),
        ("INCONNU", "Inconnu"),
    ]

    action = models.ForeignKey(
        Action, on_delete=models.CASCADE, related_name="conseils_sika"
    )
    date_scrape = models.DateField(db_index=True)
    code = models.CharField(max_length=12, choices=CODE_CHOICES, default="INCONNU")
    libelle = models.CharField(max_length=20, blank=True, default="")
    texte = models.TextField(blank=True, default="")
    image_nom = models.CharField(max_length=100, blank=True, default="")
    image_url = models.URLField(max_length=500, blank=True, default="")
    source_url = models.URLField(max_length=500, blank=True, default="")
    date_import = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("action", "date_scrape")]
        ordering = ["-date_scrape"]
        verbose_name = "Conseil Sikafinance"
        verbose_name_plural = "Conseils Sikafinance"
        indexes = [models.Index(fields=["action", "-date_scrape"])]

    def __str__(self):
        return f"{self.action.ticker} — {self.date_scrape} — {self.code}"


class GarchModel(models.Model):
    """Modèle de volatilité conditionnelle estimé action par action.

    On met en compétition GARCH(1,1), GJR-GARCH(1,1,1) et EGARCH(1,1) ;
    sélection par BIC (le plus faible gagne). Stocké en upsert par action :
    une seule ligne par action, écrasée à chaque ré-entraînement mensuel.

    Référence : Hansen & Lunde (2005, JBES) — GARCH(1,1) reste difficile à
    battre out-of-sample, donc la compétition utile est entre GARCH(1,1) et
    GJR/EGARCH pour capturer l'asymétrie (effet de levier).
    """
    MODEL_CHOICES = [
        ("GARCH", "GARCH(1,1)"),
        ("GJR-GARCH", "GJR-GARCH(1,1,1)"),
        ("EGARCH", "EGARCH(1,1)"),
        ("INSUFFISANT", "Observations insuffisantes"),
        ("FAILED", "Échec d'estimation"),
    ]

    action = models.OneToOneField(
        Action, on_delete=models.CASCADE, related_name="garch_model"
    )
    model_type = models.CharField(max_length=15, choices=MODEL_CHOICES)

    # Ordres
    p = models.IntegerField(null=True, blank=True)
    q = models.IntegerField(null=True, blank=True)
    o = models.IntegerField(null=True, blank=True, help_text="Asymétrie (GJR/EGARCH)")

    # Paramètres
    omega = models.FloatField(null=True, blank=True)
    alpha = models.FloatField(null=True, blank=True)
    beta = models.FloatField(null=True, blank=True)
    gamma = models.FloatField(null=True, blank=True, help_text="Effet de levier")

    # Diagnostics
    persistence = models.FloatField(null=True, blank=True, help_text="α+β (ou équivalent)")
    aic = models.FloatField(null=True, blank=True)
    bic = models.FloatField(null=True, blank=True)
    llf = models.FloatField(null=True, blank=True, help_text="Log-likelihood")
    n_obs = models.IntegerField(null=True, blank=True)

    # Volatilité conditionnelle
    vol_actuelle_annualisee = models.FloatField(
        null=True, blank=True,
        help_text="σ_T × √252 (en %)"
    )
    vol_conditionnelle_json = models.JSONField(
        default=list, blank=True,
        help_text="Série des 252 derniers σ_t quotidiens"
    )

    fitted_date = models.DateTimeField(auto_now=True)
    erreur_message = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        verbose_name = "Modèle GARCH"
        verbose_name_plural = "Modèles GARCH"
        ordering = ["action"]

    def __str__(self):
        return f"{self.action.ticker} — {self.model_type}"


# Modèles dédiés à la Stratégie HMM (importés ici pour que Django les détecte
# sans modifier les tables existantes ci-dessus).
from .models_strategie import (  # noqa: E402,F401
    BilanActif,
    BilanPassif,
    CompteResultat,
    FluxTresorerie,
    FacteurStrategie,
    RendementPortefeuilleFactoriel,
    RegimeMarche,
    ParametresHMM,
    AllocationStrategie,
    PortefeuilleStrategie,
    PositionStrategie,
    OrdreReallocation,
)
