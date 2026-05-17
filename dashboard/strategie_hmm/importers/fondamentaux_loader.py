"""Import des fondamentaux annuels depuis le fichier Excel raw vers la BD.

Le fichier ``Données Modele HMM FSHMM.xlsx`` contient une feuille par
poste financier, avec en colonnes les sociétés (mais identifiées soit par
nom long, soit par code court — ex: 'ABJC', 'BICC') et en lignes les
exercices (2020 → 2024).

Ce module pivote ces feuilles en lignes (action × exercice × valeur) et
peuple les 4 tables ``BilanActif``, ``BilanPassif``, ``CompteResultat``,
``FluxTresorerie``.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from django.conf import settings
from django.db import transaction

from dashboard.models import (
    Action,
    BilanActif,
    BilanPassif,
    CompteResultat,
    FluxTresorerie,
)
from .matching import matcher_societes_excel_vers_actions


# Mapping : nom de feuille Excel  →  (modèle Django, nom de champ)
MAPPING_FEUILLES = {
    # Bilan – Actif
    "Total Actif=TotalPassif": (BilanActif, "total_actif"),
    "Actif Courants": (BilanActif, "actif_courants"),
    "Tréso Active": (BilanActif, "treso_active"),
    # Bilan – Passif
    "Capitaux Propres": (BilanPassif, "capitaux_propres"),
    "Total Dettes": (BilanPassif, "total_dettes"),
    "Passif Courants": (BilanPassif, "passif_courants"),
    "Résultat non réparti (Report à)": (BilanPassif, "resultat_non_reparti"),
    # Compte de résultat
    "Chiffre d'affaires": (CompteResultat, "chiffre_affaires"),
    "Résultat d'exploitation(EBIT)": (CompteResultat, "ebit"),
    "Résultats Net": (CompteResultat, "resultat_net"),
    "Dividentes annuelles": (CompteResultat, "dividende_annuel"),
    # Flux de trésorerie
    "Flux de Trésorerie (CFO)": (FluxTresorerie, "cfo"),
    "CAPEX": (FluxTresorerie, "capex"),
    "Free Cash Flow Opérationnel": (FluxTresorerie, "fcf_operationnel"),
    "Free Cash Flow (Tréso dispo)": (FluxTresorerie, "fcf_treso_disponible"),
}


def _normalize(s: str) -> str:
    if s is None:
        return ""
    s = str(s).lower().strip()
    table = str.maketrans({
        "é": "e", "è": "e", "ê": "e", "ë": "e", "à": "a", "â": "a",
        "î": "i", "ï": "i", "ô": "o", "ö": "o", "ù": "u", "û": "u",
        "ç": "c", "�": "e",
    })
    return s.translate(table)


def _trouver_feuille(wb_sheet_names: list[str], cible: str) -> str | None:
    cible_n = _normalize(cible)
    norm_to_orig = {_normalize(s): s for s in wb_sheet_names}
    if cible_n in norm_to_orig:
        return norm_to_orig[cible_n]
    # match partiel
    for n, orig in norm_to_orig.items():
        if n.startswith(cible_n[:25]):
            return orig
    return None


def _lire_feuille_pivot(fichier: Path, sheet: str) -> pd.DataFrame | None:
    """Lit une feuille au format (Date, S1, S2, ...) avec ligne 1 = noms longs,
    ligne 2 = tickers courts (ABJC, BICC, …), lignes 3+ = (exercice, valeurs).
    Retourne un DataFrame [exercice (int), nom_societe, valeur].
    """
    raw = pd.read_excel(fichier, sheet_name=sheet, header=None)
    # Trouver la ligne d'en-tête (celle qui commence par 'Date' ou 'Datte')
    header_row = None
    for i in range(min(5, len(raw))):
        first = str(raw.iat[i, 0]).strip().lower()
        if first in {"date", "datte", "entreprises"}:
            header_row = i
            break
    if header_row is None:
        return None

    noms = raw.iloc[header_row, 1:].tolist()
    # Quand la ligne suivante contient les tickers courts, on s'en sert comme
    # alias mais on garde noms longs comme clé principale (ils matchent
    # mieux le mapping fuzzy qui sait gérer les pays).
    ticker_row = header_row + 1
    tickers = []
    if ticker_row < len(raw):
        cand = raw.iloc[ticker_row, 1:].tolist()
        # est-ce que ça ressemble à des tickers ?
        if any(isinstance(c, str) and c.isupper() and len(c) <= 8 for c in cand if c):
            tickers = cand
            data_start = ticker_row + 1
        else:
            data_start = ticker_row
    else:
        data_start = ticker_row

    long_records = []
    for r in range(data_start, len(raw)):
        annee_cell = raw.iat[r, 0]
        try:
            annee = int(annee_cell)
        except (TypeError, ValueError):
            # accepter aussi datetime
            try:
                annee = pd.Timestamp(annee_cell).year
            except Exception:
                continue
        if not (1990 < annee < 2100):
            continue
        for col_idx, nom in enumerate(noms, start=1):
            if nom is None:
                continue
            valeur = raw.iat[r, col_idx]
            if pd.isna(valeur):
                continue
            try:
                valeur = float(valeur)
            except (TypeError, ValueError):
                continue
            long_records.append({
                "exercice": annee,
                "societe": str(nom).strip(),
                "ticker_court": tickers[col_idx - 1] if col_idx - 1 < len(tickers) else None,
                "valeur": valeur,
            })
    return pd.DataFrame(long_records) if long_records else None


def _matcher_tickers(noms_societes: list[str], tickers_courts: list[str | None]) -> dict[str, str | None]:
    """Match nom société Excel → ticker BD. Tente d'abord le ticker court
    si présent dans la BD, sinon fuzzy match sur le nom.
    """
    actions_par_prefixe = {}  # 'ABJC' -> 'ABJC.ci'
    for tk in Action.objects.values_list("ticker", flat=True):
        prefixe = tk.split(".")[0].upper()
        actions_par_prefixe.setdefault(prefixe, tk)

    out: dict[str, str | None] = {}
    noms_a_matcher_fuzzy = []
    for nom, tk_court in zip(noms_societes, tickers_courts):
        if tk_court and isinstance(tk_court, str):
            cand = actions_par_prefixe.get(tk_court.upper())
            if cand:
                out[nom] = cand
                continue
        noms_a_matcher_fuzzy.append(nom)

    if noms_a_matcher_fuzzy:
        fuzzy = matcher_societes_excel_vers_actions(noms_a_matcher_fuzzy)
        out.update(fuzzy)
    return out


def importer_fondamentaux(fichier: Path | None = None, force: bool = False) -> dict:
    """Importe toutes les feuilles fondamentales depuis le fichier raw.

    Returns
    -------
    dict avec compteurs par modèle et liste de sociétés non matchées.
    """
    fichier = fichier or (
        Path(settings.BASE_DIR) / "data" / "strategie_hmm"
        / "Données Modele HMM FSHMM.xlsx"
    )
    xls = pd.ExcelFile(fichier)
    sheet_names = xls.sheet_names

    # Si force, on vide d'abord (uniquement les lignes import_excel pour ne pas
    # toucher d'éventuelles saisies manuelles)
    if force:
        with transaction.atomic():
            BilanActif.objects.filter(source="excel_import").delete()
            BilanPassif.objects.filter(source="excel_import").delete()
            CompteResultat.objects.filter(source="excel_import").delete()
            FluxTresorerie.objects.filter(source="excel_import").delete()

    # Récupérer une fois pour toutes les noms de sociétés du fichier (depuis
    # la première feuille qu'on trouve)
    societe_to_ticker: dict[str, str | None] = {}
    non_matchees: set[str] = set()

    compteurs = {"BilanActif": 0, "BilanPassif": 0,
                 "CompteResultat": 0, "FluxTresorerie": 0}
    feuilles_traitees, feuilles_manquantes = [], []

    for cible, (modele, champ) in MAPPING_FEUILLES.items():
        sheet = _trouver_feuille(sheet_names, cible)
        if sheet is None:
            feuilles_manquantes.append(cible)
            continue
        df_long = _lire_feuille_pivot(fichier, sheet)
        if df_long is None or df_long.empty:
            feuilles_manquantes.append(cible)
            continue

        # Build mapping si pas encore fait
        unique = df_long[["societe", "ticker_court"]].drop_duplicates()
        for _, row in unique.iterrows():
            if row["societe"] not in societe_to_ticker:
                pass  # batch ci-dessous
        nouveaux = [n for n in unique["societe"] if n not in societe_to_ticker]
        if nouveaux:
            tickers_court_alignes = [
                unique[unique["societe"] == n]["ticker_court"].iloc[0]
                for n in nouveaux
            ]
            societe_to_ticker.update(_matcher_tickers(nouveaux, tickers_court_alignes))

        # Update / create
        with transaction.atomic():
            for _, row in df_long.iterrows():
                ticker = societe_to_ticker.get(row["societe"])
                if not ticker:
                    non_matchees.add(row["societe"])
                    continue
                try:
                    action = Action.objects.get(ticker=ticker)
                except Action.DoesNotExist:
                    continue
                obj, created = modele.objects.update_or_create(
                    action=action, exercice=int(row["exercice"]),
                    defaults={champ: float(row["valeur"]),
                              "source": "excel_import"},
                )
                if created:
                    compteurs[modele.__name__] += 1
                else:
                    # update du champ uniquement (update_or_create a déjà mis
                    # le défaut, mais on s'assure que les champs déjà présents
                    # sur d'autres feuilles ne soient pas écrasés à NULL)
                    setattr(obj, champ, float(row["valeur"]))
                    obj.source = "excel_import"
                    obj.save(update_fields=[champ, "source", "date_import"])
        feuilles_traitees.append(sheet)

    return {
        "feuilles_traitees": feuilles_traitees,
        "feuilles_manquantes": feuilles_manquantes,
        "compteurs_crees": compteurs,
        "n_societes_matchees": sum(1 for v in societe_to_ticker.values() if v),
        "n_societes_total": len(societe_to_ticker),
        "non_matchees": sorted(non_matchees),
    }
