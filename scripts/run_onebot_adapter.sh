#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

exec .venv/bin/python -m companion_daemon.napcat_cli --adapter onebot --host 127.0.0.1 --port 8787
