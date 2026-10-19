#!/bin/bash
# backup script for restoring PostgreSQL entire cluster of databases
#
#  docker rm -f pg_test_restore
#   ~/a/celex-facts/scripts/pg_basebackup_cluster_restore.sh 
cd ~/a/postgres-docker
mkdir ~/a/postgres-docker/test_restore


tar -xzf ~/a/postgres-docker/pg-data/backups/celex-2026-03-27_203750.basebackup/base.tar.gz -C ~/a/postgres-docker/test_restore


docker rm -f pg_test_restore

docker run -d --name pg_test_restore \
  -p 5433:5432 \
  -v ~/a/postgres-docker/test_restore:/var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=postgres \
  -e PGDATA=/var/lib/postgresql/data \
  pgvector/pgvector:pg18-trixie \
  postgres -c shared_preload_libraries=pg_stat_statements -c pg_stat_statements.track=all