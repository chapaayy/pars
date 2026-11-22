#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ -d .git ]]; then
  git pull --ff-only
fi
export DB_PASSWORD="$(cat secrets/db_password)"
docker compose up -d --build --remove-orphans
