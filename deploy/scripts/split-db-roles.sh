#!/bin/sh
# One-time step of the database administrator when an installation created before 0.5.0 is upgraded
# (ADR-024 §5, DEPLOYMENT.md §6.1): creates the runtime role of the services next to the schema owner.
#
#   sh deploy/prod/init-secrets.sh            # adds secrets/db_runtime_password and the new database URLs
#   sh deploy/scripts/split-db-roles.sh
#   docker compose -f compose.prod.yaml --env-file deploy/prod/.env up -d   # migrate grants the privileges
#
# Connects to the postgres container as its bootstrap superuser through the local socket. Safe to repeat: an
# existing role keeps its password (only its attributes are reset to the safe ones). The password is read from
# the secret file and sent to psql on stdin, never as an argument. For an external or managed PostgreSQL run the
# equivalent SQL of DEPLOYMENT.md §6.1 instead.
# Uses compose.prod.yaml and deploy/prod/.env unless COMPOSE_FILE / COMPOSE_ENV_FILES are set.
set -eu

cd "$(dirname -- "$0")/../.."
: "${COMPOSE_FILE:=compose.prod.yaml}"
: "${COMPOSE_ENV_FILES:=deploy/prod/.env}"
export COMPOSE_FILE COMPOSE_ENV_FILES
DB_NAME=${PLUMMY_DB_NAME:-plummy}
RUNTIME_USER=${PLUMMY_DB_RUNTIME_USER:-plummy_app}
PASSWORD_FILE=${PLUMMY_DB_RUNTIME_PASSWORD_FILE:-deploy/prod/secrets/db_runtime_password}
SUPERUSER=${POSTGRES_SUPERUSER:-postgres}

case $RUNTIME_USER in
  *[!a-z0-9_]* | [!a-z_]*) echo "PLUMMY_DB_RUNTIME_USER must be a lower-case role name" >&2; exit 1 ;;
esac
case $DB_NAME in
  *[!a-z0-9_]* | [!a-z_]*) echo "PLUMMY_DB_NAME must be a lower-case database name" >&2; exit 1 ;;
esac
[ -r "$PASSWORD_FILE" ] || { echo "$PASSWORD_FILE is missing: run deploy/prod/init-secrets.sh first" >&2; exit 1; }
PASSWORD=$(cat "$PASSWORD_FILE")
# The password also goes into a database URL; init-secrets.sh generates URL-safe values only.
case $PASSWORD in
  '' | *[!A-Za-z0-9._~-]*) echo "$PASSWORD_FILE must contain a URL-safe password" >&2; exit 1 ;;
esac

docker compose up -d --wait postgres >/dev/null
{
  printf '\\set runtime_user %s\n\\set app_db %s\n' "$RUNTIME_USER" "$DB_NAME"
  printf "\\\\set runtime_password '%s'\n" "$PASSWORD"
  cat <<'SQL'
SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_user') AS role_exists \gset
\if :role_exists
\echo runtime role :runtime_user exists: password kept
\else
CREATE ROLE :"runtime_user" LOGIN PASSWORD :'runtime_password';
\echo runtime role :runtime_user created
\endif
ALTER ROLE :"runtime_user" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
REVOKE ALL ON DATABASE :"app_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"app_db" TO :"runtime_user";
\connect :"app_db"
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
} | docker compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U "$SUPERUSER" -d postgres
echo "Done. Start the stack: migrate grants the runtime role its privileges (db grant-runtime)."
