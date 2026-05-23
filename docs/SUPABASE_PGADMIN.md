# Connexion pgAdmin → Supabase (LOOK UP BRVM)

La base est hébergée chez Supabase Postgres 17. Tu peux t'y connecter avec
n'importe quel client Postgres : pgAdmin, DBeaver, TablePlus, `psql`...

## 1. Credentials

Récupère-les depuis le dashboard Supabase :
- https://supabase.com/dashboard → projet **lookup-brvm**
- **Project Settings → Database**

| Champ | Direct (pgAdmin, dumps) | Pooler (Django, CI) |
|---|---|---|
| Host | `db.xgsqgzwazdwinkomdmox.supabase.co` | `aws-1-eu-central-1.pooler.supabase.com` |
| Port | `5432` | `6543` (Transaction mode) |
| Database | `postgres` | `postgres` |
| User | `postgres` | `postgres.xgsqgzwazdwinkomdmox` |
| Password | ton mot de passe DB | idem |

## 2. Setup pgAdmin

1. **Object Explorer** → clic droit sur "Servers" → **Register → Server...**
2. Onglet **General** :
   - Name : `Supabase — LOOK UP BRVM` (libre)
3. Onglet **Connection** :
   - Host name/address : `db.xgsqgzwazdwinkomdmox.supabase.co`
   - Port : `5432`
   - Maintenance database : `postgres`
   - Username : `postgres`
   - Password : ton mot de passe DB (coche "Save password")
4. Onglet **SSL** :
   - SSL mode : `Require` (Supabase impose TLS)
5. **Save**

Une fois connecté, tu vois :
- `postgres` (database)
  - `Schemas/`
    - `brvm` ← **nos tables Django sont ici**
    - `public` ← schema Postgres par défaut, peut contenir d'autres tables
    - `auth`, `storage`, `realtime`, ... ← schemas Supabase, ne pas toucher

## 3. Requêtes utiles

```sql
-- Toutes les tables Django
SELECT tablename FROM pg_tables WHERE schemaname = 'brvm' ORDER BY tablename;

-- Compteurs ligne par table
SELECT 'dashboard_action'         AS t, COUNT(*) FROM brvm.dashboard_action
UNION ALL SELECT 'dashboard_historiqueaction',   COUNT(*) FROM brvm.dashboard_historiqueaction
UNION ALL SELECT 'dashboard_signalhistorique',   COUNT(*) FROM brvm.dashboard_signalhistorique
UNION ALL SELECT 'dashboard_conseilsikafinance', COUNT(*) FROM brvm.dashboard_conseilsikafinance
UNION ALL SELECT 'dashboard_garchmodel',         COUNT(*) FROM brvm.dashboard_garchmodel
ORDER BY 1;

-- Taille de chaque table
SELECT relname AS table,
       pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables
WHERE schemaname = 'brvm'
ORDER BY pg_total_relation_size(relid) DESC;
```

## 4. Quotas Free tier

- **DB size** : 500 Mo (largement suffisant ; on est à ~120 Mo actuellement)
- **Connexions directes (5432)** : 60 simultanées max → réservé à pgAdmin et dumps
- **Connexions pooler (6543)** : 200 simultanées → utilisé par Django / CI
- **Backups** : 7 jours rétention automatique (rolling)
- **Pause auto** : projet mis en pause après 7 jours sans activité (réveil instantané)

## 5. Direct vs Pooler — pourquoi deux modes ?

- **Direct (5432)** : connexion Postgres standard. Idéal pour outils admin
  (pgAdmin, `pg_dump`, migrations interactives). Tu vois toutes les fonctions
  Postgres natives (PREPARE, LISTEN, etc.).
- **Pooler/Transaction mode (6543)** : passe par PgBouncer en mode transaction.
  Mutualise les connexions, supporte beaucoup plus de clients simultanés, mais
  certaines features Postgres ne marchent pas (LISTEN/NOTIFY, prepared
  statements persistants). Django/psycopg gèrent ça tout seul.

Règle : **pgAdmin → Direct**, **app/CI → Pooler**.
