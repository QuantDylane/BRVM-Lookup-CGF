"""Profilage de la vue `accueil`.

Mesure le temps total, le nombre de requêtes SQL, le temps SQL cumulé,
et identifie les requêtes les plus lentes ou répétées.

Usage:
    python scripts/profile_accueil.py
"""
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Bootstrap Django
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "lookupbrvm.settings")

import django
django.setup()

from django.conf import settings
from django.db import connection, reset_queries
from django.test import RequestFactory

# Force DEBUG pour capturer les requêtes SQL
settings.DEBUG = True

from dashboard import views  # noqa: E402


def profile(n_runs: int = 2):
    rf = RequestFactory()
    for i in range(n_runs):
        reset_queries()
        request = rf.get("/")
        t0 = time.perf_counter()
        response = views.accueil(request)
        elapsed = (time.perf_counter() - t0) * 1000
        queries = connection.queries
        sql_time = sum(float(q["time"]) for q in queries) * 1000

        print(f"\n=== Run {i+1} ===")
        print(f"Status: {response.status_code}")
        print(f"Total view time: {elapsed:.0f} ms")
        print(f"SQL queries: {len(queries)}")
        print(f"SQL total time: {sql_time:.0f} ms ({sql_time/elapsed*100:.0f}% of view)")
        print(f"Python time: {elapsed - sql_time:.0f} ms")

        # Top 5 slowest queries
        slow = sorted(queries, key=lambda q: float(q["time"]), reverse=True)[:5]
        print("\nTop 5 slowest queries:")
        for q in slow:
            sql = q["sql"][:200].replace("\n", " ")
            print(f"  {float(q['time'])*1000:6.0f} ms | {sql}")

        # Most repeated query shapes
        def shape(sql: str) -> str:
            # Coarse normalization: keep first 120 chars w/o params
            s = sql.split(" WHERE ")[0]
            return s[:120]

        counter = Counter(shape(q["sql"]) for q in queries)
        repeated = [(k, v) for k, v in counter.most_common(5) if v > 1]
        if repeated:
            print("\nMost repeated query shapes:")
            for k, v in repeated:
                print(f"  x{v:3d} | {k}")


if __name__ == "__main__":
    profile(n_runs=2)
