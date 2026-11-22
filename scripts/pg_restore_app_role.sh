#!/usr/bin/env bash
#
# Make a database restored by pg_restore.sh usable by the app.
#
# pg_dump archives do not contain roles (they are cluster-global), and
# pg_restore.sh restores with --no-owner --no-privileges, so after a restore
# into a fresh cluster the app role is missing and every object is owned by
# the superuser. This script, run as the superuser inside the container:
#   1. creates the app role from DATABASE_URL in config_main/.env, or resets
#      its password if it already exists;
#   2. renames SOURCE_DB to the database name in DATABASE_URL (skipped when
#      that database already exists);
#   3. makes the app role owner of the database, of the schemas in APP_SCHEMAS
#      and of their tables, views, free-standing sequences, routines, enums and
#      domains (extension members and column-owned sequences are skipped);
#   4. tests a password login as the app role over TCP.
# It is idempotent. The password is never printed or put on the host command
# line.
#
# Usage:
#   scripts/pg_restore_app_role.sh SOURCE_DB
#   e.g. scripts/pg_restore_app_role.sh celex_20261111_123841
#
# Environment overrides (same names as pg_restore.sh):
#   PG_CONTAINER  container running the server   (celex-facts-postgres_db-1)
#   PG_USER       superuser used for the changes (postgres)
#   ENV_FILE      file holding DATABASE_URL      (<repo>/config_main/.env)
#   APP_SCHEMAS   space-separated app schemas    (celex_facts rule_facts)
#
# The rename fails while another session (e.g. pgAdmin) is connected to
# SOURCE_DB; close those connections first.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER="${PG_CONTAINER:-celex-facts-postgres_db-1}"
DB_USER="${PG_USER:-postgres}"
ENV_FILE="${ENV_FILE:-${script_dir}/../config_main/.env}"
APP_SCHEMAS="${APP_SCHEMAS:-celex_facts rule_facts}"

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 SOURCE_DB" >&2
    exit 2
fi
SRC_DB="$1"

if [[ ! -r "${ENV_FILE}" ]]; then
    echo "Env file '${ENV_FILE}' does not exist or is not readable." >&2
    exit 1
fi

url=$(grep -E '^DATABASE_URL=' "${ENV_FILE}" | cut -d= -f2- | tr -d '"'"'")
# Split the SQLAlchemy URL (postgresql+psycopg://user:pw@host:port/db) with
# urllib so percent-encoded passwords are decoded correctly.
mapfile -t parts < <(python3 -c '
import sys, urllib.parse as u
p = u.urlsplit(sys.argv[1])
print(u.unquote(p.username or ""))
print(u.unquote(p.password or ""))
print(p.path.lstrip("/"))
' "${url}")
APP_ROLE="${parts[0]:-}"
PW="${parts[1]:-}"
APP_DB="${parts[2]:-}"

if [[ -z "${APP_ROLE}" || -z "${PW}" || -z "${APP_DB}" ]]; then
    echo "DATABASE_URL in '${ENV_FILE}' lacks a user, password or database name." >&2
    exit 1
fi

if ! docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null | grep -q true; then
    echo "Container '${CONTAINER}' is not running." >&2
    exit 1
fi

echo "Role '${APP_ROLE}', database '${SRC_DB}' -> '${APP_DB}', schemas: ${APP_SCHEMAS}"

export PW
docker exec -i -e PW "${CONTAINER}" sh -c \
    "psql -U '${DB_USER}' -d postgres -v ON_ERROR_STOP=1 -v pw=\"\$PW\" \
      -v role='${APP_ROLE}' -v src='${SRC_DB}' -v appdb='${APP_DB}' -v schemas='${APP_SCHEMAS}'" <<'SQL'
-- 1. role: create if missing, always (re)set the password
SELECT format('CREATE ROLE %I LOGIN', :'role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I LOGIN PASSWORD %L', :'role', :'pw') \gexec

-- 2. database name the app connects to
SELECT format('ALTER DATABASE %I RENAME TO %I', :'src', :'appdb')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'appdb') \gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'appdb', :'role') \gexec

\c :"appdb"
-- 3. ownership; the DO block cannot see psql variables, so pass them as GUCs
SELECT set_config('restore.role', :'role', false), set_config('restore.schemas', :'schemas', false) \g /dev/null
DO $$
DECLARE
  app_role text := current_setting('restore.role');
  nsps oid[];
  r record;
BEGIN
  SELECT array_agg(n.oid) INTO nsps
  FROM pg_namespace n
  WHERE n.nspname = ANY (string_to_array(current_setting('restore.schemas'), ' '));

  FOR r IN SELECT n.nspname FROM pg_namespace n WHERE n.oid = ANY (nsps) LOOP
    EXECUTE format('ALTER SCHEMA %I OWNER TO %I', r.nspname, app_role);
  END LOOP;

  -- tables, views, matviews, foreign tables, free-standing sequences
  FOR r IN
    SELECT c.oid::regclass AS obj,
           CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                          WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE'
                          ELSE 'TABLE' END AS kind
    FROM pg_class c
    WHERE c.relnamespace = ANY (nsps)
      AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                        AND d.deptype IN ('a', 'i', 'e'))  -- serial/identity seqs follow their table
  LOOP
    EXECUTE format('ALTER %s %s OWNER TO %I', r.kind, r.obj, app_role);
  END LOOP;

  -- functions / procedures / aggregates
  FOR r IN
    SELECT p.oid::regprocedure AS obj FROM pg_proc p
    WHERE p.pronamespace = ANY (nsps)
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('ALTER ROUTINE %s OWNER TO %I', r.obj, app_role);
  END LOOP;

  -- enums and domains
  FOR r IN
    SELECT t.oid::regtype AS obj FROM pg_type t
    WHERE t.typnamespace = ANY (nsps)
      AND t.typtype IN ('e', 'd')
      AND NOT EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('ALTER TYPE %s OWNER TO %I', r.obj, app_role);
  END LOOP;
END $$;

-- 4. report
SELECT current_database() AS db, pg_get_userbyid(d.datdba) AS db_owner
FROM pg_database d WHERE d.datname = current_database();
SELECT n.nspname, pg_get_userbyid(c.relowner) AS owner, count(*)
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY (string_to_array(:'schemas', ' '))
GROUP BY 1, 2 ORDER BY 1, 2;
SQL

echo "Testing password login as '${APP_ROLE}' over TCP..."
PGPASSWORD="${PW}" docker exec -e PGPASSWORD "${CONTAINER}" \
    psql -h 127.0.0.1 -U "${APP_ROLE}" -d "${APP_DB}" -tAc "SELECT 'login ok as ' || current_user"
