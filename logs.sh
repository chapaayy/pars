#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export DB_PASSWORD="$(cat secrets/db_password)"
docker compose logs --tail=100 app
