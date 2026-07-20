#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# The model must be served from the local Hugging Face cache.  A missing or
# damaged cache is an operator error, not permission to fetch a cloud model at
# runtime.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"

exec /Users/geoff/.local/bin/mlx_lm.server \
  --model "${LOCAL_APPRAISAL_MODEL:-mlx-community/Qwen3-1.7B-4bit}" \
  --host "127.0.0.1" \
  --port "${LOCAL_APPRAISAL_PORT:-8188}" \
  --chat-template-args '{"enable_thinking":false}' \
  --max-tokens 256 \
  --temp 0.0 \
  --prompt-cache-size 16 \
  --log-level "${LOCAL_APPRAISAL_LOG_LEVEL:-WARNING}"
