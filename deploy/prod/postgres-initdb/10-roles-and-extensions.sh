#!/bin/sh
# Production: executed by the postgres image on the first start of an empty data volume, as the bootstrap
# superuser (ADR-011, ADR-024). Creates the schema owner role (used by `migrate` only) and its database, the
# runtime role of the services (no privileges of its own: `migrate` grants them after every upgrade) and
# installs the required extensions into that database only. Passwords come from Docker secrets.
# An existing installation gets the runtime role from deploy/scripts/split-db-roles.sh instead.
set -eu

: "${PLUMMY_DB_USER:?PLUMMY_DB_USER is required}"
: "${PLUMMY_DB_NAME:?PLUMMY_DB_NAME is required}"
: "${PLUMMY_DB_PASSWORD_FILE:?PLUMMY_DB_PASSWORD_FILE is required}"
: "${PLUMMY_DB_RUNTIME_USER:?PLUMMY_DB_RUNTIME_USER is required}"
: "${PLUMMY_DB_RUNTIME_PASSWORD_FILE:?PLUMMY_DB_RUNTIME_PASSWORD_FILE is required}"
PLUMMY_DB_PASSWORD=$(cat "$PLUMMY_DB_PASSWORD_FILE")
PLUMMY_DB_RUNTIME_PASSWORD=$(cat "$PLUMMY_DB_RUNTIME_PASSWORD_FILE")

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_user="$PLUMMY_DB_USER" -v app_password="$PLUMMY_DB_PASSWORD" -v app_db="$PLUMMY_DB_NAME" \
  -v runtime_user="$PLUMMY_DB_RUNTIME_USER" -v runtime_password="$PLUMMY_DB_RUNTIME_PASSWORD" <<'SQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE :"runtime_user" LOGIN PASSWORD :'runtime_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
CREATE DATABASE :"app_db" OWNER :"app_user";
REVOKE ALL ON DATABASE :"app_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"app_db" TO :"runtime_user";
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$PLUMMY_DB_NAME" -f /plummy/extensions.sql
