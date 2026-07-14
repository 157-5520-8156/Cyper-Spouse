#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${QQ_TURN_OBSERVATION_PATH:=data/private/qq-turns.jsonl}"
export QQ_TURN_OBSERVATION_PATH
: "${QQ_MESSAGE_BATCH_SECONDS:=0.8}"
export QQ_MESSAGE_BATCH_SECONDS

exec .venv/bin/python -m companion_daemon.napcat_cli --adapter napcat --host 127.0.0.1 --port 8787
