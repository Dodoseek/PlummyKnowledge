#!/bin/sh
# Checks of the running production stack that the HTTP smoke test cannot see (CI job "Production stack").
# Uses the COMPOSE_* environment of the caller. Prints no secret values. Secret files owned by the services'
# users (init-secrets.sh run as root) are read with `sudo -n cat`.
set -eu
cd "$(dirname -- "$0")/../../.."
SECRETS=deploy/prod/secrets
status=0

echo "- only nginx publishes ports"
published=$(docker compose ps --format '{{.Service}} {{.Ports}}' | grep -- '->' | awk '{print $1}' | sort -u)
if [ "$published" != "nginx" ]; then
  echo "FAILED: services with published ports: $(echo "$published" | tr '\n' ' ')" >&2
  status=1
fi

echo "- secret values are not in the container configuration (docker inspect)"
inspect=$(docker inspect $(docker compose ps -aq))
for file in "$SECRETS"/*; do
  if [ -r "$file" ]; then value=$(cat "$file"); else value=$(sudo -n cat "$file"); fi
  case "$(basename "$file")" in
    redis_conf) continue ;;  # its password is checked as redis_password
  esac
  if printf '%s' "$inspect" | grep -qF -- "$value"; then
    echo "FAILED: the value of $(basename "$file") is visible in docker inspect" >&2
    status=1
  fi
done

echo "- the key ring is not readable by other users"
if docker compose logs --no-color backend worker 2>&1 | grep -q "key ring is accessible to other users"; then
  echo "FAILED: the key ring file is readable by other users" >&2
  status=1
fi

echo "- application filesystems are read-only"
for service in nginx backend worker worker-ingestion scheduler; do
  if [ "$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$(docker compose ps -q "$service")")" != "true" ]; then
    echo "FAILED: $service has a writable root filesystem" >&2
    status=1
  fi
done

echo "- the ingestion worker and the scheduler have no internet access (internal networks only)"
for service in worker-ingestion scheduler postgres redis; do
  networks=$(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' \
    "$(docker compose ps -q "$service")")
  for network in $networks; do
    if [ "$(docker network inspect -f '{{.Internal}}' "$network")" != "true" ]; then
      echo "FAILED: $service is attached to the non-internal network $network" >&2
      status=1
    fi
  done
done

echo "- the services connect as the runtime role; only migrate has the schema owner's credentials (ADR-024)"
for container in $(docker compose ps -aq); do
  service=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$container")
  [ "$service" = migrate ] && continue
  if docker inspect -f '{{range .Mounts}}{{.Source}} {{end}}' "$container" | grep -q migration_database_url; then
    echo "FAILED: $service has the schema owner's database URL" >&2
    status=1
  fi
done
psql_su() { docker compose exec -T postgres psql -X -U postgres -d "${PLUMMY_DB_NAME:-plummy}" -Atc "$1"; }
runtime=${PLUMMY_DB_RUNTIME_USER:-plummy_app}
users=$(psql_su "SELECT DISTINCT usename FROM pg_stat_activity
  WHERE datname = current_database() AND application_name LIKE 'plummy-%' ORDER BY 1")
if [ "$users" != "$runtime" ]; then
  echo "FAILED: service connections use the roles: $(echo "$users" | tr '\n' ' ')" >&2
  status=1
fi
audit=$(psql_su "SELECT has_table_privilege('$runtime', 'audit_logs', 'UPDATE')
  OR has_table_privilege('$runtime', 'audit_logs', 'DELETE') OR has_table_privilege('$runtime', 'audit_logs', 'TRUNCATE')
  OR has_database_privilege('$runtime', current_database(), 'CREATE')")
if [ "$audit" != "f" ]; then
  echo "FAILED: the runtime role can change the audit log or create schemas" >&2
  status=1
fi

[ "$status" -eq 0 ] && echo "production stack checks passed"
exit "$status"
