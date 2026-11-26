#!/bin/sh
# DEVELOPMENT ONLY: executed by the postgres image on first start of an empty data volume
# (/docker-entrypoint-initdb.d), as the bootstrap superuser. Plays the role of the database administrator:
#
#   1. creates the schema owner role (no SUPERUSER, no CREATEROLE) and its database (ADR-011);
#   2. creates the runtime role of the services (no privileges of its own; `migrate` grants them, ADR-024);
#   3. installs the required extensions (deploy/postgres/extensions.sql) into that database;
#   4. installs them into template1 too, so databases created later by the owner role
#      (integration tests, CREATEDB is granted in development only) already have them.
#
# Production equivalent: docs/DEPLOYMENT.md, section "PostgreSQL: роли и расширения".
set -eu

: "${PLUMMY_DB_USER:?PLUMMY_DB_USER is required}"
: "${PLUMMY_DB_PASSWORD:?PLUMMY_DB_PASSWORD is required}"
: "${PLUMMY_DB_NAME:?PLUMMY_DB_NAME is required}"
: "${PLUMMY_DB_RUNTIME_USER:?PLUMMY_DB_RUNTIME_USER is required}"
: "${PLUMMY_DB_RUNTIME_PASSWORD:?PLUMMY_DB_RUNTIME_PASSWORD is required}"

EXTENSIONS_SQL=/plummy/extensions.sql

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_user="$PLUMMY_DB_USER" -v app_password="$PLUMMY_DB_PASSWORD" -v app_db="$PLUMMY_DB_NAME" \
  -v runtime_user="$PLUMMY_DB_RUNTIME_USER" -v runtime_password="$PLUMMY_DB_RUNTIME_PASSWORD" <<'SQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEROLE NOREPLICATION NOBYPASSRLS CREATEDB;
CREATE ROLE :"runtime_user" LOGIN PASSWORD :'runtime_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
CREATE DATABASE :"app_db" OWNER :"app_user";
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$PLUMMY_DB_NAME" -f "$EXTENSIONS_SQL"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname template1 -f "$EXTENSIONS_SQL"
