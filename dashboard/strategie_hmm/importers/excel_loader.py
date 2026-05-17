"""
Chargeurs des fichiers Excel sources de la stratégie HMM.

Deux fichiers sont consommés :
- ``rendements_portefeuilles_corr (1).xlsx`` — rendements journaliers des 13
  portefeuilles long-short (entrée du HMM).
- ``Données Modele HMM-FSHMM_copie.xlsx`` — valeurs des 13 facteurs par
  société et par date (entrée du scoring).

Les données sont chargées en mémoire (DataFrames pandas) ou persistées en
base via les services dédiés.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from django.conf import settings


# Codes internes -> intitulé de la colonne / feuille dans les Excel sources.
# Les libellés Excel contiennent des caractères latin-1 mal encodés (é → �),
# d'où la normalisation par lower-casing + suppression des accents au moment
# du matching.
MAPPING_FACTEURS = [
    ("BtM", "value (i)booktomarket global"),
    ("EP", "value(ii) resultnet-cours"),
    ("SP", "value(iii) ca-cours"),
    ("LEVIER", "quality(i) levier financier"),
    ("ROE", "quality(ii) roe"),
    ("ROA", "qaulity(iii) roa"),  # faute de frappe préservée pour matching
    ("DIV_YIELD", "growth(i) divident yield"),
    ("VARIANCE", "volatilite(i) la variance"),
    ("RDT_JOURNALIER", "momentum(i) rendement journalier"),
    ("MOM_6M", "mom(ii) 6 month price momentum"),
    ("VOLUME", "liquidite(i) volume"),
    ("BETA", "risk(i) beta"),
    ("CAPI", "size(i) capitalisation boursier"),
]

# Sur la feuille de facteurs (fichier copie), les noms de feuilles diffèrent
# légèrement des noms de colonnes du fichier des rendements. Mapping spécifique :
MAPPING_SHEETS_FACTEURS = {
    "BtM": "value (i)booktomarket global",
    "EP": "value(ii) resultnet-cours",
    "SP": "value(iv) ca-cours",
    "LEVIER": "quality(i) levier financier",
    "ROE": "quality(ii) roe",
    "ROA": "qaulity(iii) roa",
    "DIV_YIELD": "growth(i) divident yield",
    "VARIANCE": "volatilite(i) la volatilite",
    "RDT_JOURNALIER": "momentum(i) rendement journalie",
    "MOM_6M": "mom(ii) 6 month price momentum",
    "VOLUME": "liquidite(i) volume",
    "BETA": "risk(i) beta",
    "CAPI": "size(i) capitalisation boursier",
}


def _data_dir() -> Path:
    return Path(settings.BASE_DIR) / "data" / "strategie_hmm"


def _normalize(s: str) -> str:
    """Normalise un libellé pour matching insensible aux accents/casse."""
    if s is None:
        return ""
    s = str(s).lower().strip()
    table = str.maketrans(
        {"é": "e", "è": "e", "ê": "e", "ë": "e", "à": "a", "â": "a",
         "î": "i", "ï": "i", "ô": "o", "ö": "o", "ù": "u", "û": "u",
         "ç": "c", "�": "e"}
    )
    return s.translate(table)


@dataclass
class RendementsPortefeuilles:
    """Wrapper sur le DataFrame des rendements des 13 portefeuilles factoriels."""
    df: pd.DataFrame  # index = date, colonnes = codes facteurs (BtM, EP, …)

    @property
    def date_min(self):
        return self.df.index.min().date()

    @property
    def date_max(self):
        return self.df.index.max().date()

    @property
    def n_obs(self) -> int:
        return len(self.df)


def charger_rendements_portefeuilles(
    fichier: Path | None = None,
) -> RendementsPortefeuilles:
    """Charge le fichier des rendements et retourne un DataFrame indexé sur Date,
    avec colonnes renommées avec les codes facteurs internes (BtM, EP, …).
    """
    fichier = fichier or (_data_dir() / "rendements_portefeuilles_corr (1).xlsx")
    raw = pd.read_excel(fichier, sheet_name=0)
    if "Date" not in raw.columns:
        raise ValueError(f"Colonne 'Date' absente de {fichier}")
    raw["Date"] = pd.to_datetime(raw["Date"])
    raw = raw.set_index("Date").sort_index()

    # Construire un mapping nom_colonne_normalise -> code interne
    norm_to_code = {_normalize(label): code for code, label in MAPPING_FACTEURS}
    new_cols = {}
    for col in raw.columns:
        norm = _normalize(col)
        if norm in norm_to_code:
            new_cols[col] = norm_to_code[norm]
    df = raw.rename(columns=new_cols)
    # Ne garder que les colonnes qui ont été matchées
    df = df[[c for c in df.columns if c in {code for code, _ in MAPPING_FACTEURS}]]
    df = df.dropna(how="all")
    return RendementsPortefeuilles(df=df)


def charger_facteurs_par_action(
    fichier: Path | None = None,
    date_ref: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Lit le fichier copie (une feuille par facteur) et retourne un DataFrame
    indexé par ticker (ou nom de société Excel) avec les 13 colonnes facteurs
    pour ``date_ref`` (par défaut, la date la plus récente disponible).
    """
    fichier = fichier or (_data_dir() / "Données Modele HMM-FSHMM_copie.xlsx")
    xls = pd.ExcelFile(fichier)
    sheets_norm = {_normalize(s): s for s in xls.sheet_names}

    frames = {}
    for code, sheet_norm_target in MAPPING_SHEETS_FACTEURS.items():
        # Trouver la feuille la plus proche
        sheet_norm = _normalize(sheet_norm_target)
        sheet_name = sheets_norm.get(sheet_norm)
        if sheet_name is None:
            # Match partiel : la feuille commence par les premiers tokens
            for k, v in sheets_norm.items():
                if k.startswith(sheet_norm[:20]):
                    sheet_name = v
                    break
        if sheet_name is None:
            continue
        df = pd.read_excel(fichier, sheet_name=sheet_name)
        if "Date" not in df.columns:
            continue
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()

        if date_ref is None:
            date_choisie = df.index.max()
        else:
            # prendre la date la plus proche ≤ date_ref
            target = pd.Timestamp(date_ref)
            valid = df.index[df.index <= target]
            date_choisie = valid.max() if len(valid) else df.index.max()

        # Ligne pour la date : index = société, valeur = facteur
        if pd.isna(date_choisie):
            continue
        frames[code] = df.loc[date_choisie]

    if not frames:
        return pd.DataFrame()

    out = pd.DataFrame(frames)  # index = société (nom long), colonnes = codes
    out.index.name = "societe"
    return out
