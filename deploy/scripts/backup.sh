#!/bin/sh
# Backup of the production stack (ADR-023 §6, DEPLOYMENT.md §10):
#
#   sh deploy/scripts/backup.sh /srv/plummy-backups
#
# Creates <directory>/plummy-<UTC time>/ with:
#   database.dump  pg_dump (custom format) of the application database, as the schema owner role;
#   objects.tar    every stored object (documents, quarantine) through the storage port: works the same for
#                  local storage, the bundled S3 server and an external S3;
#   manifest.json  application version, Alembic revision, start time of the dump, SHA-256 of both files.
# The database goes first: every object of a document alive at the dump is in the copy; objects deleted
# afterwards are deleted again on restore (maintenance export-deletions / reapply-deletions).
#
# The key ring and the bootstrap token are never part of a backup; the script refuses to finish if such a file
# is in the backup directory. Encrypt and move the copy off the host yourself (DEPLOYMENT.md §10).
# Uses compose.prod.yaml and deploy/prod/.env unless COMPOSE_FILE / COMPOSE_ENV_FILES are set.
set -eu

ROOT=${1:?usage: backup.sh <backup directory>}
cd "$(dirname -- "$0")/../.."
: "${COMPOSE_FILE:=compose.prod.yaml}"
: "${COMPOSE_ENV_FILES:=deploy/prod/.env}"
export COMPOSE_FILE COMPOSE_ENV_FILES
DB_USER=${PLUMMY_DB_USER:-plummy}
DB_NAME=${PLUMMY_DB_NAME:-plummy}

sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

umask 077
mkdir -p "$ROOT"
DIR="$ROOT/plummy-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir "$DIR"
done=no
cleanup() { if [ "$done" != yes ]; then echo "backup failed; removing $DIR" >&2; rm -rf "$DIR"; fi; }
trap cleanup EXIT

VERSION=$(docker compose exec -T backend cat /VERSION)
REVISION=$(docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atc 'SELECT version_num FROM alembic_version')
# Taken before the dump starts: export-deletions --since this time covers every later deletion.
STARTED=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)

echo "- database (pg_dump)"
docker compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom --no-owner --no-privileges \
  > "$DIR/database.dump"
echo "- stored objects"
docker compose exec -T backend python -m plummy.cli maintenance export-objects --output - > "$DIR/objects.tar"

if find "$DIR" -name 'keyring*.json' -o -name 'setup-token' | grep -q . \
  || tar -tf "$DIR/objects.tar" | grep -Eq '(^|/)(keyring[^/]*\.json|setup-token)$'; then
  echo "refusing: a key ring or bootstrap token ended up in the backup" >&2
  exit 1
fi

cat > "$DIR/manifest.json" <<JSON
{
  "format": "plummy-backup/1",
  "version": "$VERSION",
  "alembic_revision": "$REVISION",
  "dump_started_at": "$STARTED",
  "finished_at": "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)",
  "files": {
    "database.dump": "$(sha256 "$DIR/database.dump")",
    "objects.tar": "$(sha256 "$DIR/objects.tar")"
  }
}
JSON
done=yes
echo "Backup written to $DIR (dump started at $STARTED)."
echo "The key ring (deploy/prod/secrets/keyring.json) is not in it: keep its own copy."
