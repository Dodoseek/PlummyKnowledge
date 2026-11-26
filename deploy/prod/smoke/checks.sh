#!/bin/sh
# Checks of the running production stack that the HTTP smoke test cannot see (CI job "Production stack").
# Uses the COMPOSE_* environment of the caller. Prints no secret values. Reading root-owned secret files needs
# root: run with sudo -E when init-secrets.sh ran as root.
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
  value=$(cat "$file")
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

[ "$status" -eq 0 ] && echo "production stack checks passed"
exit "$status"
