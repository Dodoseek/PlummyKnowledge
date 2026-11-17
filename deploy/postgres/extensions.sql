-- PostgreSQL extensions required by PlummyKnowledge (ADR-011).
--
-- Run ONCE per database by a database administrator (a role allowed to create extensions), BEFORE the
-- application's migrations, connected to the PlummyKnowledge database:
--
--   psql "postgresql://<admin>@<host>:5432/<plummy-db>" -v ON_ERROR_STOP=1 -f deploy/postgres/extensions.sql
--
-- The application and migration roles never need superuser privileges. Re-running is safe.
-- Upgrading an extension version (ALTER EXTENSION ... UPDATE) is also a DBA task, described in release notes.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;
