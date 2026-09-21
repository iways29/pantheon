#!/usr/bin/env bash
# Local database helper for environments without Docker.
#
# The supported path on a developer machine is the Supabase CLI:
#
#     supabase start        # Postgres + Auth + Realtime, matching production
#     supabase db reset     # re-apply every migration from scratch
#
# That needs Docker. Where Docker is unavailable, this script stands up the
# same schema on a plain PostgreSQL server with pgvector so migrations and RLS
# can still be exercised. It is a testing convenience, not a production path:
# it provides no GoTrue, no Realtime and no Storage.
set -euo pipefail

DB_NAME="${DB_NAME:-pantheon_dev}"
PSQL_SUPER="${PSQL_SUPER:-sudo -n -u postgres psql}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIGRATIONS_DIR="${ROOT_DIR}/supabase/migrations"

usage() {
  cat <<'USAGE'
Usage: scripts/local_db.sh <command>

  create   Create the database and enable required extensions
  reset    Drop, recreate, and re-apply every migration from scratch
  migrate  Apply every migration in supabase/migrations to the existing database
  psql     Open an interactive shell on the database

Environment: DB_NAME (default pantheon_dev)
USAGE
}

create_db() {
  $PSQL_SUPER -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 \
    || $PSQL_SUPER -qc "CREATE DATABASE ${DB_NAME}"
  $PSQL_SUPER -d "${DB_NAME}" -qc "CREATE EXTENSION IF NOT EXISTS vector"
  $PSQL_SUPER -d "${DB_NAME}" -qc "CREATE EXTENSION IF NOT EXISTS pgcrypto"
  $PSQL_SUPER -d "${DB_NAME}" -q -v ON_ERROR_STOP=1 -f "${ROOT_DIR}/supabase/local_bootstrap.sql"
  echo "Database ${DB_NAME} ready (pgvector, pgcrypto, Supabase auth shim)."
}

migrate() {
  shopt -s nullglob
  local files=("${MIGRATIONS_DIR}"/*.sql)
  if [ ${#files[@]} -eq 0 ]; then
    echo "No migrations in ${MIGRATIONS_DIR} yet."
    return 0
  fi
  for file in "${files[@]}"; do
    echo "  applying $(basename "${file}")"
    $PSQL_SUPER -d "${DB_NAME}" -q -v ON_ERROR_STOP=1 -f "${file}"
  done
  echo "Applied ${#files[@]} migration(s)."
}

case "${1:-}" in
  create)  create_db ;;
  migrate) migrate ;;
  reset)
    $PSQL_SUPER -qc "DROP DATABASE IF EXISTS ${DB_NAME} WITH (FORCE)"
    create_db
    migrate
    ;;
  psql)    $PSQL_SUPER -d "${DB_NAME}" ;;
  *)       usage; exit 1 ;;
esac
