#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# The model must be served from the local Hugging Face cache.  A missing or
# damaged cache is an operator error, not permission to fetch a cloud model at
# runtime.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
TEXT_ENDPOINT_EXECUTABLE="${WORLD_V2_TEXT_ENDPOINT_EXECUTABLE:-/Users/geoff/.local/bin/mlx_lm.server}"
if [ ! -x "$TEXT_ENDPOINT_EXECUTABLE" ]; then
  echo "Configured text endpoint executable is unavailable" >&2
  exit 2
fi

exec "$TEXT_ENDPOINT_EXECUTABLE" \
  --model "${WORLD_V2_TEXT_ENDPOINT_MODEL:-mlx-community/Qwen3-1.7B-4bit}" \
  --host "127.0.0.1" \
  --port "${WORLD_V2_TEXT_ENDPOINT_PORT:-8188}" \
  --chat-template-args '{"enable_thinking":false}' \
  --max-tokens 256 \
  --temp 0.0 \
  --decode-concurrency 1 \
  --prompt-concurrency 1 \
  --prompt-cache-size 4 \
  --prompt-cache-bytes 512M \
  --log-level "${WORLD_V2_TEXT_ENDPOINT_LOG_LEVEL:-WARNING}"
