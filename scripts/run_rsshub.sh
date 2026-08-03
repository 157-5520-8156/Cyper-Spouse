#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/Users/geoff/homebrew/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

if ! colima status >/dev/null 2>&1; then
  colima start
fi

compose=(docker compose -f "$ROOT/deployments/rsshub.compose.yml")
if [ -f "$ROOT/.env" ]; then
  compose+=(--env-file "$ROOT/.env")
fi
exec "${compose[@]}" up --no-color
