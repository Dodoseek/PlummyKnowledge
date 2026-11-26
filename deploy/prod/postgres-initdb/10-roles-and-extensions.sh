#!/bin/sh
# Production: executed by the postgres image on the first start of an empty data volume, as the bootstrap
# superuser (ADR-011). Creates the application role (no SUPERUSER, CREATEDB or CREATEROLE) and its database and
# installs the required extensions into that database only. Passwords come from Docker secrets.
set -eu

: "${PLUMMY_DB_USER:?PLUMMY_DB_USER is required}"
: "${PLUMMY_DB_NAME:?PLUMMY_DB_NAME is required}"
: "${PLUMMY_DB_PASSWORD_FILE:?PLUMMY_DB_PASSWORD_FILE is required}"
PLUMMY_DB_PASSWORD=$(cat "$PLUMMY_DB_PASSWORD_FILE")

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v app_user="$PLUMMY_DB_USER" -v app_password="$PLUMMY_DB_PASSWORD" -v app_db="$PLUMMY_DB_NAME" <<'SQL'
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE DATABASE :"app_db" OWNER :"app_user";
REVOKE ALL ON DATABASE :"app_db" FROM PUBLIC;
SQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$PLUMMY_DB_NAME" -f /plummy/extensions.sql
