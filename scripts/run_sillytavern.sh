#!/usr/bin/env bash
set -euo pipefail
export PATH="/Users/geoff/homebrew/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
CERTIFI_CA="../../.venv/lib/python3.13/site-packages/certifi/cacert.pem"

cd "$(dirname "$0")/../external/SillyTavern"
if [ -f "$CERTIFI_CA" ]; then
  export NODE_EXTRA_CA_CERTS="$CERTIFI_CA"
fi
if [ -f ../../.env ]; then
  set -a
  # shellcheck disable=SC1091
  source ../../.env
  set +a
fi
exec npm run start
