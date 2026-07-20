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

# There is exactly one QQ outbound owner.  NapCat/OneBot deployments use the
# sibling adapter; the official WebSocket process must stay dormant instead of
# entering a launchd crash loop and competing for the owner lease.
if [ "${QQ_ADAPTER:-official}" != "official" ]; then
  echo "official QQ WebSocket disabled: QQ_ADAPTER=${QQ_ADAPTER:-}" >&2
  exit 0
fi

exec .venv/bin/companion-qq-ws --sandbox
