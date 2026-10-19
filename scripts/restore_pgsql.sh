#!/usr/bin/env bash
#
# Restore a pg_dump custom-format backup (see dump_pgsql.sh) into a *new*
# database in the pgvector Docker container.
#
# The target database name is derived from the backup file name with the
# ".backup" suffix stripped, e.g.
#   ~/a/postgres-docker/backups/celex-20260717.backup
#       -> database "celex-20260717"
# A target name can be given explicitly as the second argument.
#
# Usage:
#   scripts/restore_pgsql.sh BACKUP_FILE [TARGET_DB]
#
# Environment overrides (same names as dump_pgsql.sh):
#   PG_CONTAINER  container running the server   (postgres-docker-postgres_db-1)
#   PG_USER       role used for create/restore   (job)
#   DROP_EXISTING set to 1 to drop TARGET_DB if it already exists
#   JOBS          parallel pg_restore workers    (4)
#
# The dump lives on the host but the backups directory is bind-mounted into the
# *pgadmin* container only, so the archive is streamed into the container over
# stdin rather than referenced by a container-side path. Streaming a -Fc archive
# on stdin rules out parallel restore, so the file is first copied into the
# container's writable layer when JOBS > 1 and removed afterwards.
#
# Restoring inside the container also sidesteps the host's /usr/bin/pg_restore
# wrapper, which resolves to the default local cluster's version (16) and
# rejects a v18 archive with "unsupported version (1.16) in file header".

set -euo pipefail

CONTAINER="${PG_CONTAINER:-postgres-docker-postgres_db-1}"
DB_USER="${PG_USER:-job}"
DROP_EXISTING="${DROP_EXISTING:-0}"
JOBS="${JOBS:-4}"

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 BACKUP_FILE [TARGET_DB]" >&2
    exit 2
fi

backup_file="$1"

if [[ ! -r "${backup_file}" ]]; then
    echo "Backup file '${backup_file}' does not exist or is not readable." >&2
    exit 1
fi

# Strip the directory and the trailing ".backup" suffix to get the database name.
db_name="${2:-$(basename "${backup_file}" .backup)}"

if [[ -z "${db_name}" || ${#db_name} -gt 63 ]]; then
    echo "Derived database name '${db_name}' is empty or longer than 63 bytes." >&2
    exit 1
fi

if ! docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null | grep -q true; then
    echo "Container '${CONTAINER}' is not running." >&2
    exit 1
fi

db_exists() {
    docker exec "${CONTAINER}" psql -U "${DB_USER}" -d postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '${db_name//\'/\'\'}'" | grep -q 1
}

if db_exists; then
    if [[ "${DROP_EXISTING}" == "1" ]]; then
        echo "Dropping existing database '${db_name}'..."
        docker exec "${CONTAINER}" dropdb -U "${DB_USER}" --force "${db_name}"
    else
        echo "Database '${db_name}' already exists. Re-run with DROP_EXISTING=1 to replace it." >&2
        exit 1
    fi
fi

echo "Creating database '${db_name}' in container '${CONTAINER}' as user '${DB_USER}'..."
docker exec "${CONTAINER}" createdb -U "${DB_USER}" "${db_name}"

# A half-restored database is worse than none: drop it if pg_restore fails hard.
drop_on_failure() {
    echo "Restore failed. Dropping partially restored database '${db_name}'." >&2
    docker exec "${CONTAINER}" dropdb -U "${DB_USER}" --force --if-exists "${db_name}" || true
}

echo "Restoring '${backup_file}' ($(du -h "${backup_file}" | cut -f1)) into '${db_name}'..."

if [[ "${JOBS}" -gt 1 ]]; then
    container_file="/tmp/$(basename "${backup_file}")"
    # shellcheck disable=SC2064  # container_file must expand now, not at trap time.
    trap "docker exec '${CONTAINER}' rm -f '${container_file}' || true" EXIT
    docker exec -i "${CONTAINER}" sh -c "cat > '${container_file}'" < "${backup_file}"

    if ! docker exec "${CONTAINER}" pg_restore -U "${DB_USER}" -d "${db_name}" \
            --no-owner --no-privileges -j "${JOBS}" -v "${container_file}"; then
        drop_on_failure
        exit 1
    fi
else
    if ! docker exec -i "${CONTAINER}" pg_restore -U "${DB_USER}" -d "${db_name}" \
            --no-owner --no-privileges -v < "${backup_file}"; then
        drop_on_failure
        exit 1
    fi
fi

echo "Restore complete: database '${db_name}'."
echo "Connect with: psql -h localhost -p 5433 -U ${DB_USER} -d ${db_name}"
