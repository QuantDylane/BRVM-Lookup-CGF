"""Migration des données de db.sqlite3 → Postgres (Supabase).

Stratégie : on parcourt chaque modèle Django dans l'ordre des dépendances FK,
on lit depuis la connexion `sqlite_legacy` et on bulk-insert dans `default`
(Postgres). Insertions par batch de BATCH_SIZE pour limiter la mémoire et
respecter les limites Supabase pooler.

Prérequis :
- db.sqlite3 présent à la racine
- DATABASE_URL défini dans .env (pointant vers Supabase)
- Migrations Django déjà appliquées sur Supabase (`python manage.py migrate`)

Usage :
    python scripts/migrate_sqlite_to_postgres.py
    python scripts/migrate_sqlite_to_postgres.py --reset   # vide les tables PG d'abord
    python scripts/migrate_sqlite_to_postgres.py --only dashboard.HistoriqueAction
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Bootstrap Django
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lookupbrvm.settings")

import django
django.setup()

from django.apps import apps
from django.db import connections, transaction


BATCH_SIZE = 2000

# Ordre de migration : modèles parents (sans FK) avant enfants.
# On liste explicitement pour contrôler les dépendances.
MIGRATION_ORDER = [
    # Indépendants
    "dashboard.Action",
    "dashboard.Indice",
    "dashboard.ApiConfig",
    "dashboard.Portefeuille",
    "dashboard.RegimeMarche",
    "dashboard.ParametresHMM",
    # Dépendent de Action
    "dashboard.HistoriqueAction",
    "dashboard.CommentHistory",
    "dashboard.TradingSignal",
    "dashboard.IndicateurCache",
    "dashboard.FondamentauxAnnuel",
    "dashboard.ConseilSikafinance",
    "dashboard.GarchModel",
    "dashboard.SignalHistorique",
    "dashboard.BilanActif",
    "dashboard.BilanPassif",
    "dashboard.CompteResultat",
    "dashboard.FluxTresorerie",
    "dashboard.FacteurStrategie",
    # Dépendent d'Indice
    "dashboard.HistoriqueIndice",
    # Dépendent de Portefeuille + Action
    "dashboard.LignePortefeuille",
    # Dépendent de TradingSignal
    "dashboard.SignalChangement",
    # Stratégie HMM (rendements factoriels et allocations)
    "dashboard.RendementPortefeuilleFactoriel",
    "dashboard.AllocationStrategie",
    # Logs (volume modeste, en dernier)
    "dashboard.ScrapingLog",
    "dashboard.News",
]


def _reset_target_tables(model_labels):
    """Vide les tables Postgres dans l'ordre inverse pour respecter les FK."""
    print("\n=== Reset des tables Postgres ===")
    for label in reversed(model_labels):
        try:
            model = apps.get_model(*label.split("."))
        except LookupError:
            continue
        with transaction.atomic(using="default"):
            deleted, _ = model.objects.using("default").all().delete()
        print(f"  {label:50s} {deleted:>10d} supprimées")


def _migrate_one(label: str) -> tuple[int, float]:
    """Migre un modèle. Retourne (nb_lignes_copiées, durée_s)."""
    model = apps.get_model(*label.split("."))
    t0 = time.time()

    # Compte source
    source_qs = model.objects.using("sqlite_legacy").all()
    n_total = source_qs.count()
    if n_total == 0:
        return 0, 0.0

    # Pour préserver les PK (cohérence des FK), on bulk_create en gardant les PK
    fields = [f.name for f in model._meta.concrete_fields]

    inserted = 0
    # Itération par tranches via Iterator pour éviter de tout charger en mémoire
    batch: list = []
    for obj in source_qs.iterator(chunk_size=BATCH_SIZE):
        # Détache l'objet de sa connexion source
        obj._state.db = None
        obj._state.adding = False
        batch.append(obj)
        if len(batch) >= BATCH_SIZE:
            model.objects.using("default").bulk_create(
                batch, batch_size=BATCH_SIZE, ignore_conflicts=True,
            )
            inserted += len(batch)
            batch = []
            print(f"    ...{inserted}/{n_total}", flush=True)
    if batch:
        model.objects.using("default").bulk_create(
            batch, batch_size=BATCH_SIZE, ignore_conflicts=True,
        )
        inserted += len(batch)

    # Resync de la sequence Postgres pour les PK auto (sinon les futurs
    # INSERT échoueront avec "duplicate key violates unique constraint").
    with connections["default"].cursor() as cur:
        table = model._meta.db_table
        pk_col = model._meta.pk.column
        cur.execute(f"""
            SELECT pg_get_serial_sequence('{table}', '{pk_col}')
        """)
        seq_name = cur.fetchone()[0]
        if seq_name:
            cur.execute(f"""
                SELECT setval('{seq_name}', COALESCE((SELECT MAX({pk_col}) FROM {table}), 1))
            """)

    return inserted, time.time() - t0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Vider les tables Postgres avant d'insérer.")
    parser.add_argument("--only", default=None,
                        help="Limiter à un seul modèle (ex: dashboard.HistoriqueAction).")
    args = parser.parse_args()

    # Vérif que les deux connexions répondent
    print("=== Vérification des connexions ===")
    with connections["sqlite_legacy"].cursor() as cur:
        cur.execute("SELECT 1")
        print(f"  sqlite_legacy: OK")
    with connections["default"].cursor() as cur:
        cur.execute("SELECT version()")
        v = cur.fetchone()[0]
        print(f"  default (Postgres): {v[:60]}")

    labels = [args.only] if args.only else MIGRATION_ORDER

    if args.reset:
        _reset_target_tables(labels)

    print(f"\n=== Migration ({len(labels)} modèles) ===")
    grand_total = 0
    grand_t0 = time.time()
    for label in labels:
        try:
            apps.get_model(*label.split("."))
        except LookupError:
            print(f"  {label:50s} SKIP (modèle introuvable)")
            continue
        n, dt = _migrate_one(label)
        grand_total += n
        status = "OK" if dt > 0 else "vide"
        print(f"  {label:50s} {n:>10d} en {dt:6.1f}s  [{status}]")

    print(f"\n=== Terminé ===")
    print(f"  Total : {grand_total} lignes en {time.time()-grand_t0:.1f}s")


if __name__ == "__main__":
    main()
