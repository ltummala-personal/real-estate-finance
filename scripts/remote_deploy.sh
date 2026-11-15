#!/usr/bin/env bash
# Runs on the cloud server: back up the database, pull the new image, restart, wait for health.
# Expects WEB_IMAGE and IMAGE_TAG in the environment and (optionally) a GHCR token on stdin.
set -euo pipefail

: "${WEB_IMAGE:?WEB_IMAGE is required}"
: "${IMAGE_TAG:?IMAGE_TAG is required}"
export WEB_IMAGE IMAGE_TAG
compose() { docker compose -f compose.yaml -f compose.prod.yaml "$@"; }

GHCR_TOKEN=""
read -r GHCR_TOKEN || true
if [ -n "$GHCR_TOKEN" ]; then
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:-github-actions}" --password-stdin
fi

mkdir -p backups
if compose ps --status running --services 2>/dev/null | grep -qx db; then
  stamp=$(date +%Y%m%d-%H%M%S)
  echo "Backing up database to backups/pre-deploy-$stamp.sql.gz"
  compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > "backups/pre-deploy-$stamp.sql.gz"
  ls -1t backups/pre-deploy-*.sql.gz | tail -n +15 | xargs -r rm --
fi

[ -f deployed.env ] && cp deployed.env previous.env
compose pull web
compose up -d --remove-orphans --wait --wait-timeout 300
printf 'WEB_IMAGE=%s\nIMAGE_TAG=%s\n' "$WEB_IMAGE" "$IMAGE_TAG" > deployed.env

docker logout ghcr.io >/dev/null 2>&1 || true
docker image prune -f >/dev/null
compose ps
