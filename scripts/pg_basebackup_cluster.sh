#!/bin/bash
# container name form Docker Desktop: postgres-docker-postgres_db-1

# 1. Generate the timestamp (Format: YYYY-MM-DD_HHMMSS)
# You can adjust the format inside the quotes
TIMESTAMP=$(date +%Y-%m-%d_%H%M%S)

# 2. Construct the full filename
DIR_NAME="celex-${TIMESTAMP}.basebackup"

# 3. Execute the backup
# Note: Removed -it to ensure it runs smoothly in non-interactive shells
docker exec postgres-docker-postgres_db-1 pg_basebackup \
  -U postgres \
  -D "/var/lib/postgresql/backups/${DIR_NAME}" \
  -Ft -z -X fetch
