# 🚀 PROMPT POUR CLAUDE CODE — Page "Stratégie HMM Temps Réel" dans LOOK UP BRVM

## 🎯 CONTEXTE GÉNÉRAL DU PROJET

Tu travailles sur **LOOK UP BRVM**, une application Django d'analyse quantitative du marché BRVM (Bourse Régionale des Valeurs Mobilières de l'UEMOA). L'application est structurée par pages et utilise déjà une base de données riche contenant les données de marché, les actions, les indices et un système d'agents pour l'automatisation.

Ta mission est d'**ajouter une nouvelle page dédiée à l'implémentation en temps réel d'une stratégie d'allocation multifactorielle basée sur un modèle HMM (Hidden Markov Model)**. Cette stratégie est issue d'un mémoire de recherche dont les résultats ont démontré la supériorité du **portefeuille Sharpe HMM** (rendement annualisé de **+19,9%**, ratio IR de **1,268**, Sortino de **2,087**) sur la BRVM.

---

## 📚 RAPPEL DE LA STRATÉGIE À IMPLÉMENTER

### Principe
La stratégie repose sur :
1. **13 facteurs financiers** calculés mensuellement à partir des fondamentaux et données de marché des actions BRVM
2. **Un modèle HMM à 2 régimes cachés** (régime favorable / régime défavorable) entraîné sur les rendements journaliers de portefeuilles factoriels long-short
3. **Une optimisation Sharpe** conditionnée au régime détecté qui produit un vecteur de pondérations factorielles
4. **Une projection sur les actions réelles** via scoring multifactoriel pour obtenir les poids finaux du portefeuille

### Les 13 facteurs (par famille)

| Famille | Facteur | Tri |
|---------|---------|-----|
| **Valeur** | Book-to-Market (BtM) | Décroissant |
| **Valeur** | Earnings-to-Price (E/P) | Décroissant |
| **Valeur** | Sales-to-Price (S/P) | Décroissant |
| **Qualité** | Return on Assets (ROA) | Décroissant |
| **Qualité** | Return on Equity (ROE) | Décroissant |
| **Qualité** | Levier financier | **Croissant** |
| **Croissance** | Dividend Yield | Décroissant |
| **Volatilité** | Variance | **Croissant** |
| **Momentum** | Rendement journalier | Décroissant |
| **Momentum** | 6-Month Price Momentum | Décroissant |
| **Liquidité** | Volume de transaction | Décroissant |
| **Risque** | Bêta | **Croissant** |
| **Taille** | Capitalisation boursière | Décroissant |

### Pipeline complet (à implémenter)

```
┌─────────────────────────────────────────────────────────────────┐
│  1. CALCUL DES FACTEURS (mensuel, par action)                   │
│     Source: HistoriqueAction + Action (BD) + fichiers Excel     │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  2. CONSTRUCTION DES 13 PORTEFEUILLES FACTORIELS LONG-SHORT     │
│     • Long: top 20% des actions selon le facteur                │
│     • Short: bottom 20% des actions selon le facteur            │
│     • Pondération: ±1/n_long (équipondéré)                      │
│     → Sortie: 13 séries de rendements journaliers               │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  3. ENTRAÎNEMENT/MAJ DU MODÈLE HMM                              │
│     • GaussianHMM(n_components=2, covariance_type='full')       │
│     • Standardisation préalable (StandardScaler)                │
│     • Réentraînement journalier (expanding window)              │
│     • Confirmation des régimes (paramètre d=5 jours)            │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  4. DÉTECTION DU RÉGIME COURANT                                 │
│     • Prédiction Viterbi sur la dernière observation            │
│     • Récupération μ (vecteur des moyennes) et V (covariance)   │
│       conditionnels au régime actuel                            │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  5. OPTIMISATION SHARPE (POIDS FACTORIELS)                      │
│     max  μᵀw / √(wᵀVw)                                          │
│     s.c. wᵢ ≥ 0,  Σwᵢ = 1                                       │
│     → w_f = [w_BtM, w_EP, ..., w_Capi] (13 poids)               │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  6. SCORING DES ACTIONS RÉELLES                                 │
│     • Matrice X (actions × facteurs) normalisée (z-score)       │
│     • Score_i = Σ w_j × X_ij                                    │
│     • Poids_i = max(0, Score_i) / Σ max(0, Score_i)             │
│     • Sélection des top 10-20 actions selon concentration       │
└──────────────────────┬──────────────────────────────────────────┘
                       ▼
┌─────────────────────────────────────────────────────────────────┐
│  7. SIGNAL DE RÉALLOCATION                                      │
│     • Déclenchement uniquement si changement de régime confirmé │
│     • Comparaison avec composition courante du portefeuille     │
│     • Génération des ordres d'achat/vente                       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🗄️ STRUCTURE EXISTANTE DE LA BASE DE DONNÉES (RAPPEL)

Tu disposes déjà des modèles suivants (NE PAS MODIFIER LEUR STRUCTURE EXISTANTE, seulement les ÉTENDRE si nécessaire) :

- **Action** : `ticker`, `nom`, `pays`, `secteur`, `isin`, `nombre_actions`, `flottant_pct`, `chiffre_affaires`, `resultat_net`, `bnpa`, `per`, `dividende`
- **HistoriqueAction** : `action`, `date`, `ouverture`, `plus_haut`, `plus_bas`, `cloture`, `volume_titres`, `volume_fcfa`, `variation_pct`
- **Indice / HistoriqueIndice** : pour le BRVM Composite (benchmark)
- **Portefeuille / LignePortefeuille** : pour le suivi des positions
- **AgentConfig / AgentTask / AgentLog / AgentAlerte** : pour l'orchestration des traitements
- **TradingSignal** : pour les signaux générés
- **News, CommentHistory, IndicateurCache, SignalChangement** : autres modèles existants

---

## ⚠️ DONNÉES MANQUANTES DANS LA BD — À RÉCUPÉRER DEPUIS LES FICHIERS EXCEL

Plusieurs informations nécessaires au calcul des facteurs ne sont **PAS** présentes dans la base de données actuelle. Elles devront être :
1. **Soit** importées depuis les fichiers Excel fournis (voir ci-dessous) via une commande Django `import_donnees_strategie`
2. **Soit** ajoutées progressivement par scraping/API dans une future itération

### Fichiers Excel à utiliser (placés dans `data/strategie_hmm/`)

| Fichier | Usage |
|---------|-------|
| `Données_Modele_HMM_FSHMM.xlsx` | Données brutes + facteurs calculés (formules) |
| `Données_Modele_HMMFSHMM_copie.xlsx` | **INPUT PRINCIPAL** — Valeurs numériques des 13 facteurs par action et par date |
| `rendements_portefeuilles_corr_1.xlsx` | Rendements des 13 portefeuilles long-short (peut être régénéré) |
| `Allocation_Actifs_Mars2023.xlsx` | Exemple de sortie attendue (allocation FSDAA Risk Parity au 01/03/2023) |

### Nouveaux modèles Django à créer

Ajoute ces modèles dans `dashboard/models_strategie.py` (nouveau fichier, importé dans `models.py`) :

```python
class FacteurStrategie(models.Model):
    """Stocke la valeur d'un facteur pour une action à une date donnée."""
    NOMS_FACTEURS = [
        ('BtM', 'Book-to-Market'),
        ('EP', 'Earnings-to-Price'),
        ('SP', 'Sales-to-Price'),
        ('ROA', 'Return on Assets'),
        ('ROE', 'Return on Equity'),
        ('LEVIER', 'Levier financier'),
        ('DIV_YIELD', 'Dividend Yield'),
        ('VARIANCE', 'Variance'),
        ('RDT_JOURNALIER', 'Rendement journalier'),
        ('MOM_6M', '6-Month Momentum'),
        ('VOLUME', 'Volume de transaction'),
        ('BETA', 'Bêta'),
        ('CAPI', 'Capitalisation boursière'),
    ]
    action = models.ForeignKey(Action, on_delete=models.CASCADE, related_name='facteurs')
    facteur = models.CharField(max_length=20, choices=NOMS_FACTEURS, db_index=True)
    date = models.DateField(db_index=True)
    valeur = models.FloatField()
    date_calcul = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('action', 'facteur', 'date')]
        indexes = [models.Index(fields=['date', 'facteur'])]

class RendementPortefeuilleFactoriel(models.Model):
    """Rendement journalier d'un portefeuille long-short pour un facteur donné."""
    facteur = models.CharField(max_length=20, db_index=True)
    date = models.DateField(db_index=True)
    rendement = models.FloatField()
    
    class Meta:
        unique_together = [('facteur', 'date')]

class RegimeMarche(models.Model):
    """Régime de marché détecté par le HMM à une date donnée."""
    date = models.DateField(unique=True, db_index=True)
    regime_brut = models.IntegerField(help_text="Régime brut prédit (0 ou 1)")
    regime_confirme = models.IntegerField(help_text="Régime confirmé après filtre de stabilité")
    proba_regime_0 = models.FloatField(help_text="P(régime=0 | observation)")
    proba_regime_1 = models.FloatField(help_text="P(régime=1 | observation)")
    changement = models.BooleanField(default=False, help_text="Changement de régime confirmé")
    declenche_reallocation = models.BooleanField(default=False)
    log_likelihood = models.FloatField(null=True, blank=True)
    date_calcul = models.DateTimeField(auto_now=True)

class ParametresHMM(models.Model):
    """Stocke les paramètres du HMM à une date de réentraînement (snapshot)."""
    date_entrainement = models.DateField(unique=True, db_index=True)
    n_observations = models.IntegerField()
    matrice_transition = models.JSONField(help_text="Matrice 2x2 P(régime_t+1 | régime_t)")
    moyennes_regime_0 = models.JSONField(help_text="Vecteur μ pour régime 0 (13 facteurs)")
    moyennes_regime_1 = models.JSONField(help_text="Vecteur μ pour régime 1 (13 facteurs)")
    covariance_regime_0 = models.JSONField(help_text="Matrice 13x13 V pour régime 0")
    covariance_regime_1 = models.JSONField(help_text="Matrice 13x13 V pour régime 1")
    log_likelihood = models.FloatField()
    converged = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)

class AllocationStrategie(models.Model):
    """Allocation factorielle et actions issue de l'optimisation Sharpe HMM."""
    STRATEGIE_CHOICES = [
        ('SHARPE_HMM', 'Sharpe HMM'),
        ('DYN_HMM', 'Dynamic HMM'),
        ('MR_HMM', 'Max Return HMM'),
        ('RP_HMM', 'Risk Parity HMM'),
        ('MD_HMM', 'Max Diversification HMM'),
        ('MV_HMM', 'Min Variance HMM'),
    ]
    date = models.DateField(db_index=True)
    strategie = models.CharField(max_length=20, choices=STRATEGIE_CHOICES, default='SHARPE_HMM')
    regime = models.ForeignKey(RegimeMarche, on_delete=models.SET_NULL, null=True)
    poids_facteurs = models.JSONField(help_text="Dict {facteur: poids} (13 entrées)")
    poids_actions = models.JSONField(help_text="Dict {ticker: poids} (top N actions)")
    nb_actions_top = models.IntegerField(default=15)
    methode_normalisation = models.CharField(max_length=20, default='zscore')
    declencheur = models.CharField(max_length=50, default='regime_change')
    rendement_attendu = models.FloatField(null=True, blank=True)
    volatilite_attendue = models.FloatField(null=True, blank=True)
    sharpe_attendu = models.FloatField(null=True, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-date']

class PortefeuilleStrategie(models.Model):
    """Portefeuille effectivement constitué et suivi en temps réel."""
    nom = models.CharField(max_length=100)
    strategie = models.CharField(max_length=20, default='SHARPE_HMM')
    capital_initial = models.FloatField()
    capital_courant = models.FloatField()
    date_creation = models.DateTimeField(auto_now_add=True)
    actif = models.BooleanField(default=True)
    frais_courtage_pct = models.FloatField(default=0.01)
    derniere_allocation = models.ForeignKey(AllocationStrategie, on_delete=models.SET_NULL, null=True)

class PositionStrategie(models.Model):
    """Position courante dans le portefeuille stratégie."""
    portefeuille = models.ForeignKey(PortefeuilleStrategie, on_delete=models.CASCADE, related_name='positions')
    action = models.ForeignKey(Action, on_delete=models.PROTECT)
    quantite = models.IntegerField()
    prix_achat_moyen = models.FloatField()
    poids_cible = models.FloatField(help_text="Poids cible issu de l'allocation")
    poids_effectif = models.FloatField(help_text="Poids effectif calculé sur la valeur courante")
    date_entree = models.DateField()
    active = models.BooleanField(default=True)

class OrdreReallocation(models.Model):
    """Ordres générés lors d'une réallocation."""
    TYPE_CHOICES = [('ACHAT', 'Achat'), ('VENTE', 'Vente'), ('CONSERVER', 'Conserver')]
    STATUT_CHOICES = [('EN_ATTENTE', 'En attente'), ('EXECUTE', 'Exécuté'), ('ANNULE', 'Annulé')]
    
    portefeuille = models.ForeignKey(PortefeuilleStrategie, on_delete=models.CASCADE)
    allocation = models.ForeignKey(AllocationStrategie, on_delete=models.CASCADE)
    action = models.ForeignKey(Action, on_delete=models.PROTECT)
    type_ordre = models.CharField(max_length=10, choices=TYPE_CHOICES)
    quantite = models.IntegerField()
    prix_indicatif = models.FloatField()
    montant_estime = models.FloatField()
    statut = models.CharField(max_length=15, choices=STATUT_CHOICES, default='EN_ATTENTE')
    date_generation = models.DateTimeField(auto_now_add=True)
    date_execution = models.DateTimeField(null=True, blank=True)
```

---

## 🏗️ ARCHITECTURE DU MODULE À CRÉER

### Arborescence proposée

```
dashboard/
├── strategie_hmm/                        # Nouveau package
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── facteurs.py                   # Calcul des 13 facteurs
│   │   ├── portefeuilles_factoriels.py   # Construction long-short
│   │   ├── hmm_engine.py                 # Wrapper du modèle HMM
│   │   ├── optimisation.py               # Sharpe, MV, RP, MD, MR, Dyn
│   │   ├── scoring.py                    # Projection facteurs → actions
│   │   └── reallocation.py               # Logique de déclenchement
│   ├── importers/
│   │   ├── __init__.py
│   │   └── excel_loader.py               # Import des fichiers Excel
│   ├── services/
│   │   ├── __init__.py
│   │   ├── strategie_service.py          # Orchestration complète
│   │   └── alertes_service.py            # Notifications
│   └── tests/
│       ├── __init__.py
│       └── test_strategie.py
├── views_strategie.py                    # Nouvelle vue (à ajouter dans urls)
├── urls.py                               # Ajouter les routes
├── models_strategie.py                   # Nouveaux modèles
├── templates/dashboard/strategie/
│   ├── strategie_dashboard.html          # Page principale
│   ├── strategie_facteurs.html           # Vue des facteurs
│   ├── strategie_regime.html             # Vue du régime courant
│   ├── strategie_allocation.html         # Vue de l'allocation
│   └── strategie_historique.html         # Historique des allocations
└── management/commands/
    ├── import_donnees_strategie.py       # Commande d'import Excel
    ├── calculer_facteurs.py              # Calcul mensuel des facteurs
    ├── construire_portefeuilles_factoriels.py
    ├── entrainer_hmm.py                  # Entraînement/MAJ du HMM
    └── generer_allocation.py             # Génère l'allocation du jour
```

### Fichiers `base.py` et `hmm.py`

⚠️ **IMPORTANT** : Le projet fournit deux fichiers Python (`base.py` et `hmm.py`) qui contiennent les classes fondamentales des modèles **HMM et FSHMM** (notamment les fonctions optimisées Viterbi/Forward/Backward avec Numba). Tu dois :
1. Copier ces fichiers dans `dashboard/strategie_hmm/core/hmm_lib/`
2. Les utiliser comme dépendance interne (NE PAS les réécrire)
3. Pour la version production, utiliser **`hmmlearn.GaussianHMM`** comme implémentation principale (plus stable) avec ces fichiers en fallback pour expérimentation

---

## 📋 SPÉCIFICATIONS DÉTAILLÉES PAR MODULE

### 1️⃣ `core/facteurs.py` — Calcul des 13 facteurs

```python
class CalculateurFacteurs:
    """Calcule les 13 facteurs pour une action à une date donnée."""
    
    def __init__(self, action: Action, date_ref: date):
        self.action = action
        self.date_ref = date_ref
    
    def calculer_tous(self) -> dict:
        """Retourne {facteur_code: valeur} pour les 13 facteurs."""
    
    # Méthodes par facteur (utilisent HistoriqueAction + Action)
    def book_to_market(self) -> float: ...      # capitaux propres / capi boursière
    def earnings_to_price(self) -> float: ...   # BNPA / cours
    def sales_to_price(self) -> float: ...      # CA par action / cours
    def roa(self) -> float: ...                  # résultat_net / total_actif
    def roe(self) -> float: ...                  # résultat_net / capitaux_propres
    def levier(self) -> float: ...               # dette_totale / capitaux_propres
    def dividend_yield(self) -> float: ...       # dividende / cours
    def variance(self) -> float: ...             # var(rendements 60j)
    def rendement_journalier(self) -> float: ...
    def momentum_6m(self) -> float: ...          # (cours_t - cours_t-126) / cours_t-126
    def volume_transaction(self) -> float: ...   # moyenne 20j du volume_fcfa
    def beta(self) -> float: ...                 # cov(action, BRVM Composite) / var(BRVM)
    def capitalisation(self) -> float: ...       # nombre_actions × cours
```

**⚠️ Fallback Excel** : Pour les facteurs nécessitant des données absentes de la BD (total_actif, capitaux_propres, dette_totale, CA par action), lire dans `Données_Modele_HMM_FSHMM.xlsx` via `excel_loader.py`. Documenter clairement dans le code chaque donnée manquante de la BD.

### 2️⃣ `core/portefeuilles_factoriels.py` — Construction Long-Short

```python
class ConstructeurPortefeuillesFactoriels:
    """Construit les 13 portefeuilles long-short et calcule leurs rendements."""
    
    FACTEURS_A_MINIMISER = ['LEVIER', 'VARIANCE', 'BETA']  # tri croissant
    
    def construire_pour_mois(self, mois: int, annee: int) -> dict:
        """
        Pour le mois donné:
        1. Classe les actions selon chaque facteur (à la 1ère date du mois)
        2. Long: top 20%, Short: bottom 20% (pondération équipondérée)
        3. Calcule les rendements journaliers du portefeuille long-short
        Retourne: {facteur: pd.Series(rendements_journaliers)}
        """
    
    def construire_serie_complete(self, date_debut, date_fin) -> pd.DataFrame:
        """DataFrame avec dates en index, 13 facteurs en colonnes."""
```

### 3️⃣ `core/hmm_engine.py` — Moteur HMM

```python
from hmmlearn.hmm import GaussianHMM
from sklearn.preprocessing import StandardScaler

class MoteurHMM:
    """Wrapper du modèle GaussianHMM avec persistance et logique de réentraînement."""
    
    def __init__(self, n_regimes: int = 2, periode_confirmation: int = 5):
        self.n_regimes = n_regimes
        self.periode_confirmation = periode_confirmation  # paramètre d
        self.scaler = StandardScaler()
        self.model = None
    
    def entrainer(self, df_rendements: pd.DataFrame) -> ParametresHMM:
        """
        Entraîne GaussianHMM(n_components=2, covariance_type='full', 
                              n_iter=1000, random_state=42)
        Sauvegarde les paramètres dans ParametresHMM.
        """
    
    def reentrainer_journalier(self, date_jour: date) -> tuple[int, float]:
        """
        Expanding window: réentraîne sur toutes les données ≤ date_jour
        Retourne (regime_brut, log_likelihood)
        """
    
    def confirmer_regime(self, date_jour: date) -> int:
        """
        Applique le filtre de stabilité: un changement de régime n'est
        confirmé que s'il persiste ≥ d jours consécutifs.
        """
    
    def detecter_changement(self, date_jour: date) -> bool:
        """True si changement de régime confirmé aujourd'hui vs hier."""
    
    def parametres_regime_courant(self, date_jour: date) -> tuple[np.array, np.array]:
        """Retourne (μ, V) du régime confirmé à date_jour."""
```

### 4️⃣ `core/optimisation.py` — 6 Stratégies d'allocation

Implémenter **toutes les 6 stratégies** du mémoire avec `scipy.optimize.minimize` (SLSQP), mais **Sharpe HMM** est la stratégie par défaut (meilleurs résultats empiriques):

```python
import numpy as np
from scipy.optimize import minimize

def optimiser_sharpe(mu: np.ndarray, V: np.ndarray) -> np.ndarray:
    """max wᵀμ / √(wᵀVw)  s.c. w≥0, Σw=1"""

def optimiser_max_return(mu: np.ndarray, w_max: float = 0.8) -> np.ndarray:
    """max wᵀμ s.c. 0≤wᵢ≤0.8, Σw=1"""

def optimiser_dyn(mu: np.ndarray) -> np.ndarray:
    """Si tous μᵢ > 0: wᵢ = μᵢ/Σμ. Sinon: équipondéré."""

def optimiser_min_variance(V: np.ndarray) -> np.ndarray:
    """min wᵀVw  s.c. w≥0, Σw=1"""

def optimiser_risk_parity(V: np.ndarray) -> np.ndarray:
    """Égalise les contributions au risque."""

def optimiser_max_diversification(sigma: np.ndarray, V: np.ndarray) -> np.ndarray:
    """max (wᵀσ) / √(wᵀVw)  s.c. w≥0, Σw=1"""

def calculer_metriques_attendues(w: np.ndarray, mu: np.ndarray, V: np.ndarray) -> dict:
    """Retourne {rendement_attendu, volatilite_attendue, sharpe_attendu}."""
```

### 5️⃣ `core/scoring.py` — Projection Facteurs → Actions

```python
class ScorerActions:
    """Convertit les poids factoriels en poids d'actions réelles."""
    
    def __init__(self, methode_normalisation: str = 'zscore'):
        self.methode = methode_normalisation  # 'zscore' ou 'minmax'
    
    def scorer(self, poids_facteurs: dict, date_ref: date, 
               tickers: list[str] = None) -> pd.DataFrame:
        """
        1. Récupère les valeurs des facteurs sélectionnés pour toutes les actions
        2. Inverse le signe pour les facteurs à minimiser (Levier, Variance, Beta)
        3. Normalise par colonne (zscore ou minmax)
        4. Calcule Score_i = Σ w_j × X_ij
        5. Convertit en poids: max(0, Score_i) / Σ max(0, Score_i)
        Retourne: DataFrame [ticker, score, poids, rang]
        """
    
    def selectionner_top_n(self, scores: pd.DataFrame, n: int = 15) -> pd.DataFrame:
        """Garde les N actions au score le plus élevé, renormalise les poids."""
```

### 6️⃣ `core/reallocation.py` — Logique de Déclenchement

```python
class GestionnaireReallocation:
    """Décide quand réallouer et génère les ordres."""
    
    def doit_reallouer(self, date_jour: date, portefeuille: PortefeuilleStrategie) -> tuple[bool, str]:
        """
        Retourne (bool, raison).
        Conditions:
        - Changement de régime confirmé sur les 22 jours précédents, OU
        - Drift > seuil sur poids effectifs vs poids cibles (ex: 5%), OU
        - Rééquilibrage périodique (1er jour ouvré du mois), OU
        - Aucune allocation existante (1er run)
        """
    
    def generer_ordres(self, portefeuille: PortefeuilleStrategie, 
                       nouvelle_allocation: AllocationStrategie) -> list[OrdreReallocation]:
        """
        Compare positions actuelles vs poids cibles.
        Génère ACHAT, VENTE ou CONSERVER pour chaque action.
        Prend en compte:
        - Capital disponible
        - Frais de courtage (frais_courtage_pct)
        - Quantités entières
        - Liquidité minimale (skip actions à volume < seuil)
        """
```

### 7️⃣ `services/strategie_service.py` — Orchestration

```python
class StrategieHMMService:
    """Service principal qui orchestre l'ensemble du pipeline."""
    
    def executer_pipeline_quotidien(self, date_jour: date = None) -> dict:
        """
        Pipeline complet à exécuter chaque jour ouvré:
        1. Vérifier que les données du jour sont disponibles
        2. Calculer/mettre à jour les facteurs
        3. Mettre à jour les rendements des portefeuilles factoriels
        4. Réentraîner le HMM
        5. Détecter et confirmer le régime
        6. Si changement confirmé: optimiser Sharpe + scorer actions
        7. Si nécessaire: générer ordres de réallocation
        8. Émettre alertes
        Retourne un rapport détaillé.
        """
    
    def initialiser_strategie(self, date_debut, date_fin) -> dict:
        """
        Initialisation complète (à exécuter une fois):
        - Import des données Excel
        - Calcul historique des facteurs
        - Construction historique des portefeuilles factoriels
        - Entraînement initial du HMM
        - Génération de la première allocation
        """
    
    def simuler_backtest(self, date_debut, date_fin, capital_initial: float) -> dict:
        """Backtest de la stratégie sur la période donnée."""
```

---

## 🎨 SPÉCIFICATIONS DE LA PAGE WEB

### Route principale
```
/strategie-hmm/                     → strategie_dashboard
/strategie-hmm/facteurs/            → strategie_facteurs
/strategie-hmm/regime/              → strategie_regime
/strategie-hmm/allocation/          → strategie_allocation_courante
/strategie-hmm/allocation/<id>/     → strategie_allocation_detail
/strategie-hmm/historique/          → strategie_historique
/strategie-hmm/backtest/            → strategie_backtest
/strategie-hmm/portefeuille/<id>/   → strategie_portefeuille_suivi
```

### Page `strategie_dashboard.html` — Vue d'ensemble

Une page synthétique avec **5 sections principales** :

#### Section 1 — Header "État du Marché"
```
┌─────────────────────────────────────────────────────────────────┐
│ 🎯 STRATÉGIE SHARPE HMM — BRVM                                  │
│                                                                  │
│ Régime actuel: [REGIME 0 - Favorable]  Confiance: 87%           │
│ Détecté le: 12/05/2026                                          │
│ Dernier changement: 03/04/2026 (Régime 1 → Régime 0)            │
│ Stabilité: ✓ Confirmé depuis 8 jours                            │
└─────────────────────────────────────────────────────────────────┘
```

**Indicateurs visuels** :
- Badge coloré pour le régime (vert=favorable, rouge=défavorable)
- Jauge de probabilité du régime
- Mini-graphique de l'évolution du régime sur 6 mois (step chart)

#### Section 2 — Allocation Factorielle Courante (graphique camembert + tableau)
```
Allocation des facteurs (Sharpe HMM):
┌─────────────────────────────────────────────────────────────────┐
│  Facteur          Poids    Famille      Régime               │
│  6M Momentum     22.3%    Momentum     0 (Favorable)         │
│  ROA             18.7%    Qualité      0 (Favorable)         │
│  Book-to-Market  15.2%    Valeur       0 (Favorable)         │
│  Dividend Yield  12.5%    Croissance   0 (Favorable)         │
│  ...                                                          │
└─────────────────────────────────────────────────────────────────┘

[Graphique camembert/treemap interactif Chart.js ou Plotly]
```

#### Section 3 — Portefeuille d'Actions Cible (top 15)
```
┌────────────────────────────────────────────────────────────────┐
│ Ticker  Nom          Secteur  Score  Poids  Capital   Qté    │
│ SNTS    SONATEL     Telecom   2.34   12.4%  1.24M F   125    │
│ SOGC    SGB Côte d.. Banque    1.98   10.5%  1.05M F    48    │
│ ETIT    ECOBANK     Banque    1.82    9.7%  0.97M F    87    │
│ ...                                                            │
└────────────────────────────────────────────────────────────────┘
```

Avec :
- Boutons d'action : "Voir détails", "Générer ordres", "Exporter Excel"
- Comparaison vs portefeuille actuel (deltas)

#### Section 4 — KPIs et Performance
- **Rendement attendu** (à partir de μᵀw)
- **Volatilité attendue** (à partir de √(wᵀVw))
- **Sharpe attendu**
- **Date de prochaine réallocation prévue**
- **Nombre de jours en régime courant**

#### Section 5 — Alertes et Signal de Réallocation
- Si un changement de régime confirmé est récent : **bandeau d'alerte** invitant à exécuter la réallocation
- Liste des ordres en attente
- Bouton "Lancer la réallocation maintenant"

### Page `strategie_regime.html` — Détail du Régime

- **Graphique temporel** (Plotly) : évolution du régime sur la période complète (steps-post)
- **Graphique des probabilités** : P(régime=0|t) et P(régime=1|t) au cours du temps
- **Tableau de transition** : matrice 2x2 des probabilités de transition
- **Statistiques par régime** : durée moyenne, fréquence, dates de changement
- **Comparaison régime vs BRVM Composite** : superposition graphique

### Page `strategie_facteurs.html` — Vue des Facteurs

- **Heatmap** : facteurs × dates avec coloration selon les rendements
- **13 mini-graphiques** : évolution de chaque facteur (rendement du portefeuille long-short)
- **Matrice de corrélation** entre facteurs (heatmap)
- **Statistiques descriptives** : moyenne, écart-type, skewness, kurtosis par facteur
- **Top/Bottom 5 actions** par facteur à la date courante

### Page `strategie_historique.html` — Historique des Allocations

- **Tableau paginé** des allocations passées
- **Graphique** du nombre de réallocations par mois
- **Graphique de turnover** : % de positions modifiées à chaque réallocation
- **Performance cumulée** vs benchmark (BRVM Composite équipondéré)

### Page `strategie_backtest.html` — Outil de Backtest

Interface permettant de :
- Choisir une **période** (date début, date fin)
- Choisir un **capital initial**
- Choisir une **stratégie d'optimisation** parmi les 6 (Sharpe HMM par défaut)
- Lancer le backtest et visualiser :
  - Courbe de capital cumulé vs benchmark
  - Drawdown chart
  - Tableau des indicateurs (rendement annualisé, volatilité, Sharpe, Sortino, IR, max DD)
  - Liste des trades simulés

---

## 🔌 ENDPOINTS API (Django REST ou vues JSON)

```
GET  /api/strategie/regime-courant/            → régime + probabilités
GET  /api/strategie/allocation-courante/       → poids facteurs + actions
GET  /api/strategie/historique-regimes/        → série temporelle des régimes
GET  /api/strategie/historique-allocations/    → liste des allocations passées
GET  /api/strategie/facteurs/<ticker>/         → facteurs courants pour une action
GET  /api/strategie/performance/?from=&to=     → métriques de performance
POST /api/strategie/declencher-reallocation/   → lance une réallocation manuelle
POST /api/strategie/backtest/                  → exécute un backtest (payload: dates, capital, strategie)
POST /api/strategie/portefeuilles/             → crée un nouveau portefeuille stratégie
GET  /api/strategie/portefeuilles/<id>/ordres/ → liste des ordres en attente
```

---

## 🤖 INTÉGRATION AVEC LE SYSTÈME D'AGENTS EXISTANT

Crée 3 nouveaux types d'agents dans `AgentConfig.AGENT_TYPES` :

| Type | Cron par défaut | Description |
|------|-----------------|-------------|
| `STRATEGIE_FACTEURS` | `0 18 * * 1-5` | Recalcule les facteurs en fin de journée de bourse |
| `STRATEGIE_HMM` | `30 18 * * 1-5` | Réentraîne le HMM + détecte le régime |
| `STRATEGIE_ALLOCATION` | `45 18 * * 1-5` | Génère une nouvelle allocation si nécessaire |

Chaque agent doit :
- Logger ses actions dans `AgentLog`
- Créer des `AgentAlerte` en cas de changement de régime, dérive, erreur
- Stocker ses résultats dans `AgentTask.resultat_json`
- Respecter les dépendances (`AgentDependency`) : ALLOCATION dépend de HMM dépend de FACTEURS

---

## 📦 DÉPENDANCES À AJOUTER

```txt
# requirements.txt — à ajouter
hmmlearn==0.3.2
scipy>=1.10
scikit-learn>=1.3
numpy>=1.24
pandas>=2.0
plotly>=5.18
numba>=0.58
openpyxl>=3.1
```

---

## ✅ COMMANDES MANAGEMENT À CRÉER

```bash
# Initialisation complète (une seule fois)
python manage.py import_donnees_strategie --fichier "data/strategie_hmm/Données_Modele_HMM_FSHMM.xlsx"
python manage.py calculer_facteurs --depuis 2017-01-01 --jusqua 2026-05-14
python manage.py construire_portefeuilles_factoriels --depuis 2017-01-01
python manage.py entrainer_hmm --initial --jusqua 2024-12-31

# Pipeline quotidien (orchestré par les agents)
python manage.py calculer_facteurs --jour aujourd_hui
python manage.py construire_portefeuilles_factoriels --jour aujourd_hui
python manage.py entrainer_hmm --reentrainement-journalier
python manage.py generer_allocation --strategie SHARPE_HMM --auto

# Outils
python manage.py backtest_strategie --depuis 2023-01-01 --jusqua 2025-12-31 --capital 100000000
```

---

## 🧪 TESTS ATTENDUS

Couvre au minimum :
- `test_calcul_facteurs.py` : valide chaque formule sur des données connues
- `test_construction_portefeuilles.py` : vérifie le tri ASC/DESC selon le facteur
- `test_hmm_engine.py` : vérifie la convergence + la confirmation de régime sur d=5
- `test_optimisation.py` : valide les 6 fonctions d'optimisation (contraintes respectées)
- `test_scoring.py` : valide la normalisation z-score et la conversion en poids
- `test_reallocation.py` : valide la logique de déclenchement et la génération d'ordres
- `test_integration_end_to_end.py` : pipeline complet sur un mini-dataset

---

## 🎨 STYLE ET COHÉRENCE UI

- **Respecter le style existant** de l'application LOOK UP BRVM (mêmes couleurs, composants, navigation)
- Utiliser les **mêmes librairies** déjà présentes (Chart.js, DataTables, Tailwind/Bootstrap selon ce qui est utilisé)
- Pour les graphiques complexes (régimes, heatmaps, backtest), utiliser **Plotly.js**
- Ajouter une **entrée "Stratégie HMM"** dans le menu principal de l'application
- Toutes les pages doivent être **responsive** et fonctionner sur mobile

---

## ⚠️ POINTS DE VIGILANCE CRITIQUES

1. **Performance** : Les calculs HMM peuvent être lourds. Utilise :
   - Cache Redis/Django pour les résultats récents
   - Tâches asynchrones (Celery) si déjà en place dans le projet
   - Numba (via `base.py`) pour les boucles critiques

2. **Reproductibilité** : Fixe `random_state=42` partout dans les HMM pour des résultats reproductibles

3. **Robustesse** :
   - Gérer les valeurs manquantes (`NaN`) dans les facteurs (interpolation linéaire pour les courtes lacunes, exclusion pour les longues)
   - Gérer les actions peu liquides (filtre minimum volume)
   - Logger toutes les erreurs dans `AgentLog`

4. **Sécurité** :
   - Toutes les vues d'écriture doivent être protégées par `@login_required` (et idéalement permissions admin)
   - Les opérations de réallocation manuelle doivent être confirmées (modal)

5. **Évolutivité** : Le code doit être prêt à supporter :
   - Plus de 2 régimes (n_regimes paramétrable)
   - L'ajout du modèle FSHMM en complément (même si HMM est meilleur sur BRVM selon le mémoire)
   - L'ajout de nouveaux facteurs

6. **Documentation** :
   - Chaque module doit avoir un docstring expliquant son rôle
   - Référence le mémoire (sections clés) en commentaires
   - Crée un `README_STRATEGIE_HMM.md` à la racine du projet expliquant l'usage

---

## 📊 EXEMPLE DE LIVRABLE ATTENDU (sortie de l'allocation au 1er mars 2023)

Pour valider ton implémentation, à partir du fichier `Allocation_Actifs_Mars2023.xlsx` fourni, le système doit pouvoir produire l'allocation suivante :

**Poids factoriels (Risk Parity, mars 2023)** :
- Beta: 16.05% | Book-to-Market: 13.11% | 6M Momentum: 9.7%
- ROA: 9.6% | Rdt Journalier: 7.52% | Levier: 6.51%
- ROE: 6.3% | Dividend Yield: 5.82% | S/P: 5.74%
- Volume: 5.68% | Variance: 5.44% | Capi: 4.53% | E/P: 4.01%

**Top 5 actions** : SICC (6.09%), ... (compléter via lecture du fichier)

⚠️ Tu dois pouvoir reproduire cette allocation **exactement** lorsque la stratégie est paramétrée sur Risk Parity à la date du 01/03/2023. Sinon, il y a un bug dans le calcul des facteurs ou la projection.

---

## 🚦 ORDRE D'IMPLÉMENTATION RECOMMANDÉ

1. **Phase 1 — Fondations** (1-2 jours)
   - Créer `models_strategie.py` et appliquer les migrations
   - Créer la structure de dossiers `dashboard/strategie_hmm/`
   - Copier `base.py` et `hmm.py` fournis dans `core/hmm_lib/`
   - Créer la commande `import_donnees_strategie` pour charger les Excel

2. **Phase 2 — Calculs offline** (2-3 jours)
   - Implémenter `core/facteurs.py` (avec fallback Excel)
   - Implémenter `core/portefeuilles_factoriels.py`
   - Tester sur l'historique complet → comparer avec `rendements_portefeuilles_corr_1.xlsx`

3. **Phase 3 — Cœur HMM** (2-3 jours)
   - Implémenter `core/hmm_engine.py`
   - Implémenter `core/optimisation.py` (les 6 stratégies)
   - Implémenter `core/scoring.py`
   - Tests unitaires

4. **Phase 4 — Orchestration** (1-2 jours)
   - Implémenter `services/strategie_service.py`
   - Créer les commandes management
   - Ajouter les agents (AgentConfig avec types STRATEGIE_*)

5. **Phase 5 — Frontend** (3-4 jours)
   - Vues Django + templates
   - Endpoints API
   - Intégration des graphiques Plotly
   - Tests d'intégration

6. **Phase 6 — Validation** (1 jour)
   - Validation contre `Allocation_Actifs_Mars2023.xlsx`
   - Backtest sur 2017-2025 → comparer avec les résultats du mémoire (Sharpe HMM = +19.9%)
   - Documentation finale

---

## 🎓 RÉFÉRENCE THÉORIQUE

Cette stratégie est issue du mémoire :
**"Allocation multifactorielle conditionnée aux régimes de marché : Application des modèles HMM et FSHMM sur la BRVM" (CGF Gestion, 2025)**

Conclusions clés à respecter :
- ✅ Le **Sharpe HMM est la meilleure stratégie** empiriquement (rendement annualisé +19.9%, IR 1.268, Sortino 2.087)
- ✅ Dyn HMM est aussi performant (+10.1%, IR 0.8)
- ⚠️ Max Return HMM est à éviter (-25.2% sur la période testée)
- ⚠️ Le FSHMM est moins performant que le HMM sur la BRVM (à implémenter en option mais pas en défaut)
- ✅ Le réentraînement quotidien avec confirmation à d=5 jours est essentiel pour éviter le sur-trading

---

## 🎯 CRITÈRES DE SUCCÈS

✅ La page `/strategie-hmm/` est accessible et affiche le régime courant
✅ Le système calcule les 13 facteurs automatiquement chaque jour ouvré
✅ Le HMM est réentraîné chaque jour et persiste ses paramètres
✅ L'allocation Sharpe HMM est régénérée à chaque changement de régime confirmé
✅ Le top 15 d'actions BRVM est affiché avec poids cibles, quantités et capital
✅ Les ordres de réallocation peuvent être générés (sans exécution réelle pour l'instant)
✅ Un backtest reproduit approximativement les résultats du mémoire (+/- 2% sur le rendement annualisé)
✅ La validation sur mars 2023 reproduit l'allocation Risk Parity du fichier Excel fourni
✅ Tous les tests unitaires passent (couverture > 80% du code stratégie)
✅ La documentation est complète et un nouveau développeur peut comprendre le système

---

**Bon courage Claude Code ! Cette page sera le centre névralgique de la stratégie quantitative de LOOK UP BRVM. La rigueur dans l'implémentation des formules et la robustesse du pipeline sont essentielles. N'hésite pas à demander des clarifications si une partie du protocole HMM n'est pas claire — la documentation complète est dans les PDFs `Memoire_finale.pdf` et `Protocole_de_mise_en_œuvre_des_modèles_HMM_et_FSHMM.pdf`.**
