#!/usr/bin/env bash
# Deploy Slipway itself to the one V1 server.
#
# This deploys the orchestrator. It is not the deploy seam -- that is
# app/deploy/, which deploys the applications Slipway builds.
#
# Usage:  deploy/deploy.sh [--migrate-only]
#
# Requires an environment already carrying the SLIPWAY_ variables (see
# deploy/compose.yaml for the full list). Nothing here reads a secrets file.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
COMPOSE=(docker compose --file "${HERE}/compose.yaml")

migrate_only=0
[[ "${1:-}" == "--migrate-only" ]] && migrate_only=1

require() {
  local missing=()
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || missing+=("$name")
  done
  if (( ${#missing[@]} )); then
    printf 'refusing to deploy, unset: %s\n' "${missing[*]}" >&2
    exit 1
  fi
}

# Fail before touching the server, not halfway through.
require SLIPWAY_DATABASE_URL \
        SLIPWAY_NOVITA_API_KEY \
        SLIPWAY_DEPLOY_SSH_HOST \
        SLIPWAY_DEPLOY_SSH_USER \
        SLIPWAY_DEPLOY_PUBLIC_HOST \
        SLIPWAY_DEPLOY_REMOTE_ROOT \
        SLIPWAY_GITHUB_TOKEN \
        SLIPWAY_GITHUB_ORG \
        POSTGRES_PASSWORD

if [[ ! -s "${ROOT}/config/models.yaml" ]] || ! grep -q 'model_id:' "${ROOT}/config/models.yaml"; then
  echo "config/models.yaml has no model ids. Run 'make models-sync' first." >&2
  echo "See docs/decisions/0004-novita-base-url.md." >&2
  exit 1
fi

echo "==> building"
"${COMPOSE[@]}" build

echo "==> database"
"${COMPOSE[@]}" up --detach --wait postgres

echo "==> migrations (forward-only)"
"${COMPOSE[@]}" run --rm --no-deps api python -m app.cli.main migrate

if (( migrate_only )); then
  echo "==> migrate-only, stopping here"
  exit 0
fi

echo "==> api and worker"
"${COMPOSE[@]}" up --detach --wait api worker

echo "==> health"
"${COMPOSE[@]}" exec -T api python -c "
import sys, urllib.request, json
with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=10) as response:
    body = json.load(response)
print(json.dumps(body))
sys.exit(0 if body.get('status') == 'ok' else 1)
"

echo "==> deployed"
"${COMPOSE[@]}" ps
