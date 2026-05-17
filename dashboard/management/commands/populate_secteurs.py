"""Met à jour le secteur (et les libellés/financiers manquants) des actions
à partir des fiches JSON présentes dans data/societes/.

Usage:
    python manage.py populate_secteurs
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.conf import settings

from dashboard.models import Action
from dashboard.management.commands.import_data import (
    _normalize_secteur,
    parse_french_int,
    parse_french_float,
)


class Command(BaseCommand):
    help = "Renseigne Action.secteur (et Nom/financiers) depuis data/societes/*.json"

    def handle(self, *args, **options):
        data_dir = Path(settings.BASE_DIR) / "data" / "societes"
        if not data_dir.exists():
            self.stdout.write(self.style.ERROR(f"Dossier introuvable: {data_dir}"))
            return

        files = sorted(data_dir.glob("*_societe.json"))
        if not files:
            self.stdout.write(self.style.WARNING("Aucun fichier *_societe.json trouvé."))
            return

        updated = 0
        missing = []
        sectors = {}

        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  ! Échec lecture {f.name}: {e}"))
                continue

            ticker = data.get("ticker") or ""
            if not ticker:
                parts = f.stem.replace("_societe", "").rsplit("_", 1)
                ticker = f"{parts[0]}.{parts[1]}" if len(parts) == 2 else parts[0]

            action = Action.objects.filter(ticker=ticker).first()
            if not action:
                missing.append(ticker)
                continue

            secteur = _normalize_secteur(data.get("Secteur", ""))
            sectors[secteur] = sectors.get(secteur, 0) + 1

            # Champs de base toujours rafraîchis
            action.secteur = secteur or action.secteur
            nom = data.get("Nom") or ""
            if nom:
                action.nom = nom

            # Champs financiers : remplir uniquement si vide en base
            mapping = {
                "isin": ("ISIN", lambda v: v or ""),
                "description": ("La société", lambda v: v or ""),
                "nombre_actions": ("Nombre_Actions", parse_french_int),
                "flottant_pct": ("Flottant_Pct", lambda v: parse_french_float((v or "").replace("%", ""))),
                "chiffre_affaires": ("Chiffre d'affaires", parse_french_float),
                "resultat_net": ("Résultat net", parse_french_float),
                "bnpa": ("BNPA", parse_french_float),
                "per": ("PER", parse_french_float),
                "dividende": ("Dividende", parse_french_float),
            }
            for field, (key, conv) in mapping.items():
                current = getattr(action, field, None)
                if current in (None, "", 0):
                    val = conv(data.get(key, ""))
                    if val not in (None, ""):
                        setattr(action, field, val)

            action.save()
            updated += 1

        self.stdout.write(self.style.SUCCESS(f"\n✔ {updated} action(s) mises à jour."))
        if missing:
            self.stdout.write(self.style.WARNING(
                f"  Tickers JSON sans correspondance en base: {', '.join(missing)}"
            ))
        self.stdout.write("\nRépartition sectorielle:")
        for sec, n in sorted(sectors.items(), key=lambda x: -x[1]):
            label = sec or "(vide)"
            self.stdout.write(f"  - {label}: {n}")
