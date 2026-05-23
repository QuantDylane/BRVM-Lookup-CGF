"""
Django settings for lookupbrvm project.
LOOK UP BRVM - Application d'analyse financière BRVM
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Charge les variables d'environnement depuis .env (non commité)
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

SECRET_KEY = "django-insecure-(p(d3un*h33^*z02%!rvd=v8t91#hx%sf(^s777gwr@f$jz9rz"

DEBUG = True

ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "lookupbrvm.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "lookupbrvm.wsgi.application"

# Base de données : Postgres (Supabase) si DATABASE_URL défini, sinon SQLite local.
# La connexion utilise le schema `brvm` (créé manuellement sur Supabase pour
# isoler nos tables Django des autres tables présentes dans `public`).
_DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

if _DATABASE_URL:
    import dj_database_url
    DATABASES = {
        "default": dj_database_url.parse(
            _DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
        )
    }
    # Forcer le search_path sur notre schema dédié.
    # On garde aussi `public` accessible pour les extensions (uuid-ossp, pgcrypto).
    DATABASES["default"].setdefault("OPTIONS", {})
    DATABASES["default"]["OPTIONS"]["options"] = "-c search_path=brvm,public"
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# Connexion SQLite secondaire toujours disponible (sous l'alias `sqlite_legacy`)
# si le fichier db.sqlite3 existe. Permet de migrer les données SQLite → Postgres
# en lançant le script `scripts/migrate_sqlite_to_postgres.py`.
_SQLITE_LEGACY_PATH = BASE_DIR / "db.sqlite3"
if _SQLITE_LEGACY_PATH.exists() and "sqlite_legacy" not in DATABASES:
    DATABASES["sqlite_legacy"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _SQLITE_LEGACY_PATH,
    }



AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "fr-fr"
TIME_ZONE = "Africa/Abidjan"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Chemin vers les données scrappées
DATA_DIR = BASE_DIR / "data"

# Cache mémoire local (par process). Les clés sont versionnées par la
# dernière date d'historique → invalidation automatique après scrape.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "lookupbrvm-default",
        "TIMEOUT": 300,  # 5 min ; les clés sont versionnées par last_date donc safe plus longtemps
    }
}
