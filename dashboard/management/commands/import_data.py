"""
Commande Django pour importer les données CSV/JSON dans la base SQLite.
Usage: python manage.py import_data
"""
import os
import csv
import json
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings
from dashboard.models import Action, Indice, HistoriqueAction, HistoriqueIndice, News


def parse_french_float(val):
    """Convertit un nombre au format français (virgule décimale, espace séparateur) en float."""
    if not val or val.strip() == "":
        return None
    val = val.replace("\xa0", "").replace(" ", "").replace(",", ".").replace("%", "")
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def parse_french_int(val):
    """Convertit un entier au format français (espace séparateur) en int."""
    if not val or val.strip() == "":
        return None
    val = val.replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _normalize_secteur(raw):
    """Normalise un libellé de secteur BRVM.

    "BRVM - SERVICES FINANCIERS" -> "Services Financiers"
    "BRVM - INDUSTRIE"          -> "Industrie"
    """
    if not raw:
        return ""
    s = str(raw).strip()
    # Retirer un éventuel préfixe BRVM/-
    for prefix in ("BRVM -", "BRVM-", "BRVM"):
        if s.upper().startswith(prefix):
            s = s[len(prefix):].lstrip(" -")
            break
    # Capitalisation propre des mots
    return " ".join(w.capitalize() for w in s.split())


class Command(BaseCommand):
    help = "Importe les données CSV/JSON scrappées dans la base de données SQLite"

    def add_arguments(self, parser):
        parser.add_argument("--only", type=str, help="Importer uniquement: actions, indices, societes, news")
        parser.add_argument("--clear", action="store_true", help="Vider les tables avant import")

    def handle(self, *args, **options):
        data_dir = settings.DATA_DIR
        only = options.get("only")
        clear = options.get("clear", False)

        if not only or only == "societes":
            self.import_societes(data_dir, clear)
        if not only or only == "actions":
            self.import_actions(data_dir, clear)
        if not only or only == "indices":
            self.import_indices(data_dir, clear)
        if not only or only == "news":
            self.import_news(data_dir, clear)

        self.stdout.write(self.style.SUCCESS("Import terminé avec succès !"))

    def import_societes(self, data_dir, clear):
        """Importe les fiches sociétés (JSON) -> crée/met à jour les objets Action."""
        societes_dir = data_dir / "societes"
        if not societes_dir.exists():
            self.stdout.write(self.style.WARNING("Dossier societes/ introuvable, ignoré."))
            return

        if clear:
            Action.objects.all().delete()

        count = 0
        for f in sorted(societes_dir.glob("*_societe.json")):
            with open(f, "r", encoding="utf-8") as fh:
                data = json.load(fh)

            ticker = data.get("ticker", "")
            if not ticker:
                # Déduire du nom de fichier: SGBC_ci_societe.json -> SGBC.ci
                parts = f.stem.replace("_societe", "").rsplit("_", 1)
                ticker = f"{parts[0]}.{parts[1]}" if len(parts) == 2 else parts[0]

            pays = ticker.split(".")[-1] if "." in ticker else ""

            action, created = Action.objects.update_or_create(
                ticker=ticker,
                defaults={
                    "nom": data.get("Nom", "") or "",
                    "secteur": _normalize_secteur(data.get("Secteur", "") or ""),
                    "pays": pays,
                    "isin": data.get("ISIN", ""),
                    "description": data.get("La société") or data.get("La soci\u00e9t\u00e9") or data.get("Description", "") or "",
                    "nombre_actions": parse_french_int(data.get("Nombre_Actions", data.get("Nombre de titres", ""))),
                    "flottant_pct": parse_french_float((data.get("Flottant_Pct", "") or data.get("Flottant", "")).replace("%", "")),
                    "chiffre_affaires": parse_french_float(data.get("Chiffre d'affaires", "")),
                    "resultat_net": parse_french_float(data.get("Résultat net", data.get("R\xe9sultat net", ""))),
                    "bnpa": parse_french_float(data.get("BNPA", "")),
                    "per": parse_french_float(data.get("PER", "")),
                    "dividende": parse_french_float(data.get("Dividende", "")),
                },
            )
            count += 1

        self.stdout.write(f"  Sociétés importées: {count}")

    def import_actions(self, data_dir, clear):
        """Importe les historiques d'actions (CSV)."""
        actions_dir = data_dir / "actions"
        if not actions_dir.exists():
            self.stdout.write(self.style.WARNING("Dossier actions/ introuvable, ignoré."))
            return

        if clear:
            HistoriqueAction.objects.all().delete()

        total = 0
        for f in sorted(actions_dir.glob("*_historique.csv")):
            # Déduire le ticker: SGBC_ci_historique.csv -> SGBC.ci
            parts = f.stem.replace("_historique", "").rsplit("_", 1)
            ticker = f"{parts[0]}.{parts[1]}" if len(parts) == 2 else parts[0]

            action, _ = Action.objects.get_or_create(ticker=ticker)

            # Obtenir les dates existantes pour éviter les doublons
            existing_dates = set(
                HistoriqueAction.objects.filter(action=action).values_list("date", flat=True)
            )

            rows = []
            with open(f, "r", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh, delimiter=";")
                for row in reader:
                    date_str = row.get("Date", "").strip()
                    if not date_str:
                        continue
                    try:
                        date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        continue

                    if date_val in existing_dates:
                        continue

                    rows.append(HistoriqueAction(
                        action=action,
                        date=date_val,
                        ouverture=parse_french_float(row.get("Ouverture")),
                        plus_haut=parse_french_float(row.get("Plus_Haut")),
                        plus_bas=parse_french_float(row.get("Plus_Bas")),
                        cloture=parse_french_float(row.get("Cloture")),
                        volume_titres=parse_french_int(row.get("Volume_Titres")),
                        volume_fcfa=parse_french_float(row.get("Volume_FCFA")),
                        variation_pct=parse_french_float(row.get("Variation_Pct")),
                    ))

            if rows:
                HistoriqueAction.objects.bulk_create(rows, batch_size=5000)
            total += len(rows)
            self.stdout.write(f"  {ticker}: {len(rows)} lignes importées")

        self.stdout.write(f"  Total historiques actions: {total}")

    def import_indices(self, data_dir, clear):
        """Importe les historiques d'indices (CSV)."""
        indices_dir = data_dir / "indices"
        if not indices_dir.exists():
            self.stdout.write(self.style.WARNING("Dossier indices/ introuvable, ignoré."))
            return

        if clear:
            HistoriqueIndice.objects.all().delete()

        total = 0
        for f in sorted(indices_dir.glob("*_historique.csv")):
            ticker = f.stem.replace("_historique", "")

            indice, _ = Indice.objects.get_or_create(ticker=ticker)

            existing_dates = set(
                HistoriqueIndice.objects.filter(indice=indice).values_list("date", flat=True)
            )

            rows = []
            with open(f, "r", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh, delimiter=";")
                for row in reader:
                    date_str = row.get("Date", "").strip()
                    if not date_str:
                        continue
                    try:
                        date_val = datetime.strptime(date_str, "%Y-%m-%d").date()
                    except ValueError:
                        continue

                    if date_val in existing_dates:
                        continue

                    rows.append(HistoriqueIndice(
                        indice=indice,
                        date=date_val,
                        ouverture=parse_french_float(row.get("Ouverture")),
                        plus_haut=parse_french_float(row.get("Plus_Haut")),
                        plus_bas=parse_french_float(row.get("Plus_Bas")),
                        cloture=parse_french_float(row.get("Cloture")),
                        volume_titres=parse_french_int(row.get("Volume_Titres")),
                        volume_fcfa=parse_french_float(row.get("Volume_FCFA")),
                        variation_pct=parse_french_float(row.get("Variation_Pct")),
                    ))

            if rows:
                HistoriqueIndice.objects.bulk_create(rows, batch_size=5000)
            total += len(rows)
            self.stdout.write(f"  {ticker}: {len(rows)} lignes importées")

        self.stdout.write(f"  Total historiques indices: {total}")

    def import_news(self, data_dir, clear):
        """Importe les actualités (CSV)."""
        news_file = data_dir / "news" / "actualites_brvm.csv"
        if not news_file.exists():
            self.stdout.write(self.style.WARNING("Fichier actualites_brvm.csv introuvable, ignoré."))
            return

        if clear:
            News.objects.all().delete()

        existing_ids = set(News.objects.values_list("id_source", flat=True))

        rows = []
        with open(news_file, "r", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, delimiter=";")
            for row in reader:
                try:
                    id_source = int(row.get("id", 0))
                except (ValueError, TypeError):
                    continue

                if id_source in existing_ids:
                    continue

                date_pub = row.get("date_publication", "")
                date_obj = None
                if date_pub:
                    # Format: 2026-04-08T18:13:04&#x2B;02:00 ou 2026-04-08T18:13:04+02:00
                    date_pub = date_pub.replace("&#x2B;", "+")
                    try:
                        date_obj = datetime.fromisoformat(date_pub)
                    except ValueError:
                        try:
                            date_obj = datetime.strptime(date_pub[:19], "%Y-%m-%dT%H:%M:%S")
                        except ValueError:
                            pass

                rows.append(News(
                    id_source=id_source,
                    titre=row.get("titre", "")[:500],
                    date_publication=date_obj,
                    auteur=row.get("auteur", "")[:200],
                    categorie=row.get("categorie", "")[:200],
                    contenu=row.get("contenu", ""),
                    image_url=row.get("image_url", "")[:500],
                    url=row.get("url", "")[:500],
                ))

        if rows:
            News.objects.bulk_create(rows, batch_size=2000)
        self.stdout.write(f"  Actualités importées: {len(rows)}")
