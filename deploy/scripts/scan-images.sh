#!/bin/sh
# Scan the images of the production stack with Trivy (SECURITY_MODEL §19, Phase 12.6).
#
#   sh deploy/scripts/scan-images.sh                 # every image of compose.prod.yaml (profile s3)
#   sh deploy/scripts/scan-images.sh plummy/web:local redis:8.6-alpine
#
# Fails on a CRITICAL or HIGH vulnerability that has a fixed version: rebuild the image (our Dockerfiles take
# the security updates of their base images) or move the tag of a third-party image. A finding that does not
# apply goes into deploy/trivy/<image name>.trivyignore with the reason and an expiry date (`exp:`), after
# which Trivy reports it again and the exception is reviewed. Without arguments the images come from
# `docker compose config`, so the environment of compose.prod.yaml must be set (COMPOSE_ENV_FILES or
# --env-file in COMPOSE_*); the images must exist locally (`docker compose build`, `docker compose pull`).
set -eu

TRIVY_IMAGE=${TRIVY_IMAGE:-aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb}
root=$(cd "$(dirname "$0")/../.." && pwd)
cache=${TRIVY_CACHE_DIR:-${TMPDIR:-/tmp}/plummy-trivy-cache}
mkdir -p "$cache"

trivy() {
  docker run --rm \
    --volume /var/run/docker.sock:/var/run/docker.sock:ro \
    --volume "$cache:/root/.cache" \
    --volume "$root/deploy/trivy:/ignore:ro" \
    "$TRIVY_IMAGE" "$@"
}

if [ "$#" -eq 0 ]; then
  set -- $(docker compose -f "$root/compose.prod.yaml" --profile s3 config --images | sort -u)
fi

trivy image --quiet --download-db-only
failed=""
for image in "$@"; do
  # pgvector/pgvector:0.8.7-pg17 → pgvector, plummy/worker-ingestion:local → worker-ingestion
  name=${image%%@*}
  name=${name%:*}
  name=${name##*/}
  ignore=/dev/null
  label=none
  if [ -f "$root/deploy/trivy/$name.trivyignore" ]; then
    ignore=/ignore/$name.trivyignore
    label=deploy/trivy/$name.trivyignore
  fi
  echo "== $image (exceptions: $label)"
  if ! trivy image --quiet --skip-db-update --scanners vuln --severity CRITICAL,HIGH --ignore-unfixed \
    --ignorefile "$ignore" --exit-code 1 --format table "$image"; then
    failed="$failed $image"
  fi
done

if [ -n "$failed" ]; then
  echo "Fixable CRITICAL/HIGH vulnerabilities in:$failed" >&2
  exit 1
fi
echo "No fixable CRITICAL/HIGH vulnerabilities in $# images."
