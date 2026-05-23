# Migration db.sqlite3 → Supabase Postgres

Réalisée le 2026-05-22. Documentation pour comprendre et finaliser.

## Pourquoi cette migration

Le fichier `db.sqlite3` versionné dans git avait dépassé la limite GitHub
(100 MB) suite à l'accumulation des `HistoriqueAction` (~135k lignes) et
des `SignalHistorique` (~123k lignes générés par le backfill). Les pushs
échouaient avec `pre-receive hook declined`.

Solution : migration vers un Postgres hébergé chez Supabase. Le repo ne
contient plus de fichier de données ; toutes les écritures (scraping
quotidien, entraînement GARCH mensuel) vont directement dans la base
hébergée via `DATABASE_URL`.

## Ce qui a été fait

| Étape | Résultat |
|---|---|
| Projet Supabase créé | `lookup-brvm` (id `xgsqgzwazdwinkomdmox`) — Frankfurt |
| Schema isolé créé | `brvm` (séparé du `public` réservé aux extensions) |
| Settings Django adapté | bascule SQLite ↔ Postgres selon `DATABASE_URL` |
| Migrations appliquées | 33 migrations Django (dont 14 dashboard) sur Supabase |
| Données migrées | **339 142 lignes** sur 26 modèles, parité 100% vérifiée |
| `db.sqlite3` retiré du tracking git | fichier reste en local mais plus dans le repo |
| Workflows GH Actions adaptés | secret `DATABASE_URL` + plus de commit `db.sqlite3` |

## Ce qu'il te reste à faire

### 1. Ajouter le secret GitHub Actions

Sans ce secret, les workflows quotidiens échoueront.

1. https://github.com/QuantDylane/BRVM-Lookup-CGF/settings/secrets/actions
2. **"New repository secret"**
3. Name : `DATABASE_URL`
4. Value : le contenu de la ligne `DATABASE_URL=...` dans ton `.env` local
   (sans le préfixe `DATABASE_URL=`)
5. Save

### 2. (Recommandé) Reset du password Supabase

Le password que tu m'as transmis pendant la session a transité par la
conversation. Pour la sécurité long terme :

1. https://supabase.com/dashboard/project/xgsqgzwazdwinkomdmox/settings/database
2. Section "Database password" → **Reset database password**
3. Note le nouveau (visible une seule fois)
4. Mets-le à jour dans :
   - `.env` local
   - Le secret GitHub Actions
5. Si tu utilises pgAdmin : mets-le à jour aussi

### 3. Vérifier que le prochain run quotidien passe

- Soit attends 20h UTC (cron auto)
- Soit déclenche manuellement : **Actions** → "Scraping BRVM quotidien" → "Run workflow"

Si l'étape "Import dans Supabase" est verte, c'est gagné.

## Comment ça marche désormais

**En local**
- `.env` contient `DATABASE_URL=postgresql://...` → Django utilise Postgres
- Si tu supprimes `.env` ou la variable → fallback automatique sur `db.sqlite3` local
- Le fichier `db.sqlite3` reste utile : il sert pour les imports historiques
  (`scripts/migrate_sqlite_to_postgres.py` peut être rejoué)

**En CI (GitHub Actions)**
- Secret `DATABASE_URL` injecté en env → Django écrit dans Supabase
- Plus de `git commit db.sqlite3` à la fin des workflows

**Connexion pgAdmin**
- Voir [`docs/SUPABASE_PGADMIN.md`](SUPABASE_PGADMIN.md)
- Direct connection (5432) pour les outils admin, pas le pooler

## Rollback si besoin

Si tu veux temporairement repasser sur SQLite (debug local par exemple) :

```bash
# Renomme .env pour désactiver DATABASE_URL
mv .env .env.disabled

# Django retombe automatiquement sur db.sqlite3
python manage.py runserver
```

Pour réactiver Postgres : `mv .env.disabled .env`.

## Re-migration depuis SQLite (si on doit recommencer)

Si pour une raison X tu veux re-pousser tout le SQLite local vers Postgres :

```bash
# Avec .env actif (Postgres en default)
python scripts/migrate_sqlite_to_postgres.py --reset
```

Le script lit `db.sqlite3` (connexion alias `sqlite_legacy`) et le copie
dans `default` (Postgres). `--reset` vide les tables Postgres d'abord pour
éviter les doublons.
