#!/bin/sh
# Restore of the production stack onto an empty installation (ADR-023 §6, DEPLOYMENT.md §10):
#
#   sh deploy/scripts/restore.sh <backup directory> --deletions deletions.json
#   sh deploy/scripts/restore.sh <backup directory> --no-deletions
#
# deletions.json comes from the SOURCE instance, before it is torn down:
#   docker compose ... exec -T backend python -m plummy.cli maintenance export-deletions \
#     --since <dump_started_at from manifest.json> --output - > deletions.json
# Without it (--no-deletions) whatever was deleted after the dump comes back.
#
# Needs empty volumes (docker compose down --volumes, or a new host) and the same secrets as the source,
# above all the key ring (stored API keys and OAuth tokens are unreadable without it). Steps: checksums →
# PostgreSQL and Redis → pg_restore → storage (the bundled S3 server, if enabled), migrate (upgrades to this
# version) and the API →
# objects → deletions reapplied → the remaining services.
# Uses compose.prod.yaml and deploy/prod/.env unless COMPOSE_FILE / COMPOSE_ENV_FILES are set; with the
# bundled S3 server set COMPOSE_PROFILES=s3.
set -eu

DIR=${1:?usage: restore.sh <backup directory> (--deletions FILE | --no-deletions)}
MODE=${2:?usage: restore.sh <backup directory> (--deletions FILE | --no-deletions)}
DELETIONS=""
case "$MODE" in
  --deletions) DELETIONS=${3:?--deletions needs a file}; DELETIONS=$(CDPATH= cd -- "$(dirname -- "$DELETIONS")" && pwd)/$(basename -- "$DELETIONS") ;;
  --no-deletions) ;;
  *) echo "usage: restore.sh <backup directory> (--deletions FILE | --no-deletions)" >&2; exit 2 ;;
esac
DIR=$(CDPATH= cd -- "$DIR" && pwd)
cd "$(dirname -- "$0")/../.."
: "${COMPOSE_FILE:=compose.prod.yaml}"
: "${COMPOSE_ENV_FILES:=deploy/prod/.env}"
export COMPOSE_FILE COMPOSE_ENV_FILES
DB_USER=${PLUMMY_DB_USER:-plummy}
DB_NAME=${PLUMMY_DB_NAME:-plummy}

sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
expected() { sed -n "s/.*\"$1\": \"\([0-9a-f]*\)\".*/\1/p" "$DIR/manifest.json"; }

echo "- checksums"
for file in database.dump objects.tar; do
  if [ "$(sha256 "$DIR/$file")" != "$(expected "$file")" ]; then
    echo "refusing: $file does not match manifest.json" >&2
    exit 1
  fi
done
[ -z "$DELETIONS" ] || [ -r "$DELETIONS" ] || { echo "cannot read $DELETIONS" >&2; exit 1; }

echo "- PostgreSQL and Redis"
docker compose up -d --wait postgres redis
if [ "$(docker compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atc \
  "SELECT to_regclass('public.alembic_version') IS NULL")" != "t" ]; then
  echo "refusing: the database is not empty (restore needs a new installation: docker compose down --volumes)" >&2
  exit 1
fi

echo "- database (pg_restore)"
# The extensions already exist (created by the database administrator, ADR-011); the application role may
# not comment on them, so they are left out of the restore list.
docker compose exec -T postgres sh -c 'umask 077; cat > /tmp/plummy-restore.dump' < "$DIR/database.dump"
docker compose exec -T postgres sh -c "
  set -e
  pg_restore -l /tmp/plummy-restore.dump | grep -v -E ' EXTENSION | COMMENT - EXTENSION ' > /tmp/plummy-restore.list
  pg_restore -U '$DB_USER' -d '$DB_NAME' --no-owner --no-privileges --exit-on-error --single-transaction \
    -L /tmp/plummy-restore.list /tmp/plummy-restore.dump
  rm -f /tmp/plummy-restore.dump /tmp/plummy-restore.list"

echo "- storage, migrations and the API"
docker compose up -d --wait backend

echo "- stored objects"
docker compose exec -T backend python -m plummy.cli maintenance import-objects --input - < "$DIR/objects.tar"

if [ -n "$DELETIONS" ]; then
  echo "- deletions after the dump"
  docker compose exec -T backend python -m plummy.cli maintenance reapply-deletions --input - < "$DELETIONS"
else
  echo "WARNING: --no-deletions: whatever was deleted after the dump is back." >&2
fi

echo "- remaining services"
docker compose up -d --wait
echo "Restore finished. Sign in, check search and citations; re-enter secrets if the key ring was not restored."
