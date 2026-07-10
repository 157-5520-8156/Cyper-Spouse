#!/usr/bin/env bash
set -euo pipefail
export PATH="/Users/geoff/homebrew/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

cd "$(dirname "$0")/.."
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
exec .venv/bin/python -m uvicorn companion_daemon.app:app --host 127.0.0.1 --port 8765
