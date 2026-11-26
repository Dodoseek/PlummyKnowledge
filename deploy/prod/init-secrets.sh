#!/bin/sh
# Generates the secrets of the production stack (compose.prod.yaml, ADR-023 §3) into deploy/prod/secrets/.
#
#   sh deploy/prod/init-secrets.sh
#
# Existing files are kept, so the script is safe to run again (for example after adding the S3 profile).
# Nothing is printed except file names. The directory is 0700. Compose mounts each file as it is on the host,
# and every service reads its secrets as its own unprivileged user:
#   - run as root (recommended on a server: sudo sh deploy/prod/init-secrets.sh): each file gets the user ID of
#     the service that reads it and mode 0400 (ADR-023 §3);
#   - run as an ordinary user (a workstation): mode 0444, still protected by the 0700 directory; the API and the
#     worker then warn that the key ring is readable by other users.
#
# The key ring (secrets/keyring.json) is NOT part of any backup: copy it to a separate safe place now
# (DEPLOYMENT.md §4). Losing it makes every stored API key and OAuth token unreadable.
set -eu

umask 077
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/secrets
mkdir -p "$DIR"
chmod 0700 "$DIR"
# Under sudo the directory stays the administrator's, so `docker compose` run by them can find the files.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_UID:-}" ]; then
  chown "$SUDO_UID" "$DIR"
fi

DB_USER=${PLUMMY_DB_USER:-plummy}
DB_NAME=${PLUMMY_DB_NAME:-plummy}

random() { # URL-safe random string of $1 bytes of entropy
  openssl rand "$1" | base64 | tr '+/' '-_' | tr -d '=\n'
}

# User IDs of the images in compose.prod.yaml: app (backend, workers, migrate, scheduler), postgres, redis.
APP_UID=10001
POSTGRES_UID=999
REDIS_UID=999

write() { # write <name> <owner uid> <value>: only when the file does not exist yet; owner and mode every run
  if [ -e "$DIR/$1" ]; then
    echo "kept      secrets/$1"
  else
    printf '%s' "$3" > "$DIR/$1"
    echo "created   secrets/$1"
  fi
  if [ "$(id -u)" -eq 0 ]; then
    chown "$2:$2" "$DIR/$1"
    chmod 0400 "$DIR/$1"
  else
    chmod 0444 "$DIR/$1"
  fi
}

value() { cat "$DIR/$1"; }

command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 1; }

write postgres_password $POSTGRES_UID "$(random 32)"
write db_password $POSTGRES_UID "$(random 32)"
write database_url $APP_UID "postgresql+asyncpg://$DB_USER:$(value db_password)@postgres:5432/$DB_NAME"
write redis_password 0 "$(random 32)"
write redis_url $APP_UID "redis://:$(value redis_password)@redis:6379/0"
write redis_conf $REDIS_UID "$(printf 'requirepass %s\nappendonly yes\nsave ""\nprotected-mode yes\n' "$(value redis_password)")"
write secret_key $APP_UID "$(random 48)"
write keyring.json $APP_UID "$(printf '{"primary": "k1", "keys": {"k1": "%s"}}' "$(random 32)")"
# Bundled S3 server (profile s3): the root key administers it; the application key owns only its bucket.
write s3_root_access_key 0 "root-$(random 9)"
write s3_root_secret_key 0 "$(random 32)"
write s3_access_key_id $APP_UID "plummy-$(random 9)"
write s3_secret_access_key $APP_UID "$(random 32)"

echo "Done. Back up secrets/keyring.json separately from the database backups."
