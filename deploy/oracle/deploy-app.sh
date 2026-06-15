#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo ".env not found. Copy .env.example to .env and set IGOT_SERVICE_TOKEN first."
  exit 1
fi

cd "${REPO_ROOT}"
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8080/healthz