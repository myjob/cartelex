#!/usr/bin/env bash
#
# Dump the "celex" PostgreSQL database from the pgvector Docker container.
#
# Values are derived from ../postgres-docker/docker-compose.yml:
#   service "postgres_db" (project "postgres-docker")
#       -> container "postgres-docker-postgres_db-1"
#   image pgvector/pgvector:pg18-trixie, host port 5433 -> 5432
#   POSTGRES_PASSWORD is set, but no POSTGRES_USER / POSTGRES_DB,
#   so the application database is "celex", owned by role "job".
#
# pg_dump runs inside the container and streams the dump to the host over
# stdout. It deliberately does NOT use pg_dump -f with a container-side path:
# the backups directory is bind-mounted into the *pgadmin* container only, so
# any in-container path the postgres service can write to lives in its
# throwaway writable layer and disappears on the next recreate. The host
# directory is world-writable (owned by pgadmin's uid 5050, mode 0777), so the
# host user running this script can write to it directly.
#
# Custom format (-Fc) is used: it is compressed and restorable with pg_restore.
#
# Restore requires a pg_restore of the server's major version (18). 
#   docker exec -i CONTAINER pg_restore -U job -d celex --clean --if-exists < FILE
# or an explicit v18 host binary:
#   /usr/lib/postgresql/18/bin/pg_restore -h localhost -p 5433 -U job -d celex \
#       --clean --if-exists FILE
#
# Original one-liner adapted from https://stackoverflow.com/a/30171064
# (Cesar Augusto Nogueira et al.), CC BY-SA 4.0.

set -euo pipefail

CONTAINER="${PG_CONTAINER:-celex-facts-postgres_db-1}"
DB_USER="${PG_USER:-job}"
DB_NAME="${PG_DATABASE:-celex}"
HOST_BACKUP_DIR="${HOST_BACKUP_DIR:-~/a/postgres-docker/backups}"

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_file="${HOST_BACKUP_DIR}/celex_${timestamp}.backup"
tmp_file="${backup_file}.partial"

if ! docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null | grep -q true; then
    echo "Container '${CONTAINER}' is not running." >&2
    exit 1
fi

if [[ ! -w "${HOST_BACKUP_DIR}" ]]; then
    echo "Backup directory '${HOST_BACKUP_DIR}' is not writable by $(id -un)." >&2
    exit 1
fi

echo "Dumping database '${DB_NAME}' from container '${CONTAINER}' as user '${DB_USER}'..."

# Write to a temp file first so a failed dump never leaves a usable-looking
# backup behind. -v goes to stderr, so it does not corrupt the stdout stream.
if docker exec "${CONTAINER}" pg_dump -U "${DB_USER}" -Fc -v "${DB_NAME}" > "${tmp_file}"; then
    mv "${tmp_file}" "${backup_file}"
    echo "Dump written to ${backup_file} ($(du -h "${backup_file}" | cut -f1))"
else
    rm -f "${tmp_file}"
    echo "Dump failed." >&2
    exit 1
fi
