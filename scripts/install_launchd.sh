#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${GIRL_AGENT_INSTALL_ENV_FILE:-$ROOT/.env}"
if [ -n "${GIRL_AGENT_LAUNCH_AGENTS_DIR:-}" ]; then
  LAUNCH_AGENTS="$GIRL_AGENT_LAUNCH_AGENTS_DIR"
else
  USER_HOME_DIR="$(/usr/bin/python3 -c 'from pathlib import Path; print(Path.home())')"
  LAUNCH_AGENTS="$USER_HOME_DIR/Library/LaunchAgents"
fi
DOMAIN="gui/$(id -u)"
PYTHON_BIN="${GIRL_AGENT_INSTALL_PYTHON:-$ROOT/.venv/bin/python}"
HEALTH_TIMEOUT_SECONDS="${GIRL_AGENT_INSTALL_HEALTH_TIMEOUT_SECONDS:-120}"
DAEMON_HEALTH_URL="${GIRL_AGENT_DAEMON_HEALTH_URL:-http://127.0.0.1:8765/health}"

BASE_LABELS=(
  com.girl-agent.qq-ws
  com.girl-agent.daemon
  com.girl-agent.napcat
  com.girl-agent.rsshub
)
TEXT_LABELS=(
  com.girl-agent.text-endpoint
  com.girl-agent.text-endpoint-watchdog
)
OPTIONAL_LABELS=(com.girl-agent.sillytavern)
RETIRED_LABELS=(
  com.girl-agent.proactive
  com.girl-agent.local-appraisal-watchdog
  com.girl-agent.local-appraisal
)
ALL_LABELS=(
  "${BASE_LABELS[@]}"
  "${TEXT_LABELS[@]}"
  "${OPTIONAL_LABELS[@]}"
  "${RETIRED_LABELS[@]}"
)

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
TEXT_ENDPOINT_EXECUTABLE="${WORLD_V2_TEXT_ENDPOINT_EXECUTABLE:-/Users/geoff/.local/bin/mlx_lm.server}"

case "${WORLD_V2_TEXT_ENDPOINT_ENABLED:-false}" in
  1|true|TRUE|yes|YES|on|ON) TEXT_ENDPOINT_ENABLED=1 ;;
  0|false|FALSE|no|NO|off|OFF) TEXT_ENDPOINT_ENABLED=0 ;;
  *)
    echo "Invalid WORLD_V2_TEXT_ENDPOINT_ENABLED value" >&2
    exit 2
    ;;
esac
case "${INSTALL_SILLYTAVERN:-0}" in
  1|true|TRUE|yes|YES|on|ON) SILLYTAVERN_ENABLED=1 ;;
  0|false|FALSE|no|NO|off|OFF) SILLYTAVERN_ENABLED=0 ;;
  *)
    echo "Invalid INSTALL_SILLYTAVERN value" >&2
    exit 2
    ;;
esac
case "$HEALTH_TIMEOUT_SECONDS" in
  ''|*[!0-9]*)
    echo "GIRL_AGENT_INSTALL_HEALTH_TIMEOUT_SECONDS must be an integer" >&2
    exit 2
    ;;
esac

DESIRED_LABELS=("${BASE_LABELS[@]}")
if [ "$TEXT_ENDPOINT_ENABLED" = "1" ]; then
  DESIRED_LABELS+=("${TEXT_LABELS[@]}")
fi
if [ "$SILLYTAVERN_ENABLED" = "1" ]; then
  DESIRED_LABELS+=("${OPTIONAL_LABELS[@]}")
fi

plist_path() {
  printf '%s/%s.plist\n' "$LAUNCH_AGENTS" "$1"
}

source_plist_path() {
  printf '%s/launchd/%s.plist\n' "$ROOT" "$1"
}

contains_line() {
  local expected="$1"
  local file="$2"
  grep -Fqx -- "$expected" "$file" 2>/dev/null
}

is_desired() {
  local candidate="$1"
  local label
  for label in "${DESIRED_LABELS[@]}"; do
    if [ "$candidate" = "$label" ]; then
      return 0
    fi
  done
  return 1
}

validate_settings_without_output() {
  local endpoint_port="${WORLD_V2_TEXT_ENDPOINT_PORT:-8188}"
  local endpoint_model="${WORLD_V2_TEXT_ENDPOINT_MODEL:-mlx-community/Qwen3-1.7B-4bit}"
  if ! PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" - \
      "$TEXT_ENDPOINT_ENABLED" \
      "$endpoint_port" \
      "$endpoint_model" >/dev/null 2>&1 <<'PY'
import sys
from urllib.parse import urlsplit

from companion_daemon.config import Settings

expected_enabled = sys.argv[1] == "1"
expected_port = int(sys.argv[2])
expected_model = sys.argv[3]
settings = Settings(_env_file=None)
if settings.world_v2_text_endpoint_enabled is not expected_enabled:
    raise SystemExit(2)
if settings.world_v2_text_endpoint_model != expected_model:
    raise SystemExit(2)
if expected_enabled:
    parsed = urlsplit(settings.world_v2_text_endpoint_base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port != expected_port
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(2)
PY
  then
    echo "Settings preflight failed (details suppressed to protect secrets)" >&2
    return 1
  fi
}

validate_plist_contracts_without_output() {
  if ! "$PYTHON_BIN" - "$ROOT" >/dev/null 2>&1 <<'PY'
from pathlib import Path
import plistlib
import sys

root = Path(sys.argv[1]).resolve()
contracts = {
    "com.girl-agent.qq-ws": ([str(root / "scripts/run_qq_ws.sh")], root),
    "com.girl-agent.daemon": ([str(root / "scripts/run_daemon.sh")], root),
    "com.girl-agent.napcat": ([str(root / "scripts/run_napcat_adapter.sh")], root),
    "com.girl-agent.rsshub": ([str(root / "scripts/run_rsshub.sh")], root),
    "com.girl-agent.text-endpoint": ([str(root / "scripts/run_text_endpoint.sh")], root),
    "com.girl-agent.text-endpoint-watchdog": (
        ["/bin/zsh", str(root / "scripts/text_endpoint_watchdog.sh")],
        None,
    ),
    "com.girl-agent.sillytavern": ([str(root / "scripts/run_sillytavern.sh")], root),
}
for label, (arguments, working_directory) in contracts.items():
    path = root / "launchd" / f"{label}.plist"
    with path.open("rb") as stream:
        payload = plistlib.load(stream)
    if payload.get("Label") != label:
        raise SystemExit(2)
    if payload.get("ProgramArguments") != arguments:
        raise SystemExit(2)
    actual_working_directory = payload.get("WorkingDirectory")
    if working_directory is None:
        if actual_working_directory is not None:
            raise SystemExit(2)
    elif actual_working_directory != str(working_directory):
        raise SystemExit(2)
PY
  then
    echo "LaunchAgent contract preflight failed" >&2
    return 1
  fi
}

preflight() {
  local script
  local label
  for command_name in bash zsh plutil launchctl curl; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      echo "Required deployment command is unavailable: $command_name" >&2
      return 1
    fi
  done
  if [ ! -x "$PYTHON_BIN" ]; then
    echo "Configured deployment Python is not executable" >&2
    return 1
  fi

  for script in \
    "$ROOT/scripts/install_launchd.sh" \
    "$ROOT/scripts/run_daemon.sh" \
    "$ROOT/scripts/run_qq_ws.sh" \
    "$ROOT/scripts/run_napcat_adapter.sh" \
    "$ROOT/scripts/run_rsshub.sh" \
    "$ROOT/scripts/run_text_endpoint.sh" \
    "$ROOT/scripts/run_sillytavern.sh"; do
    if [ ! -x "$script" ]; then
      echo "LaunchAgent program is not executable: $script" >&2
      return 1
    fi
    bash -n "$script"
  done
  if [ ! -r "$ROOT/scripts/text_endpoint_watchdog.sh" ]; then
    echo "Text endpoint watchdog is not readable" >&2
    return 1
  fi
  zsh -n "$ROOT/scripts/text_endpoint_watchdog.sh"

  for label in "${BASE_LABELS[@]}" "${TEXT_LABELS[@]}" "${OPTIONAL_LABELS[@]}"; do
    if [ ! -f "$(source_plist_path "$label")" ]; then
      echo "LaunchAgent source is missing: $label" >&2
      return 1
    fi
    plutil -lint "$(source_plist_path "$label")" >/dev/null
  done
  validate_plist_contracts_without_output
  validate_settings_without_output

  if [ "$TEXT_ENDPOINT_ENABLED" = "1" ] && [ ! -x "$TEXT_ENDPOINT_EXECUTABLE" ]; then
    echo "Enabled text endpoint executable is unavailable" >&2
    return 1
  fi
}

remove_exact_plist() {
  local label="$1"
  local target
  target="$(plist_path "$label")"
  if [ "$target" != "$LAUNCH_AGENTS/$label.plist" ]; then
    echo "Refusing unresolved LaunchAgent removal target" >&2
    return 1
  fi
  rm -f -- "$target"
}

atomic_install_plist() {
  local label="$1"
  local source
  local target
  local temporary
  source="$(source_plist_path "$label")"
  target="$(plist_path "$label")"
  temporary="$target.install.$$"
  cp "$source" "$temporary"
  chmod 0644 "$temporary"
  mv -f "$temporary" "$target"
}

unload_label() {
  launchctl bootout "$DOMAIN/$1" >/dev/null 2>&1 || true
}

load_label() {
  launchctl bootstrap "$DOMAIN" "$(plist_path "$1")" >/dev/null
}

label_is_loaded() {
  launchctl print "$DOMAIN/$1" >/dev/null 2>&1
}

BACKUP_DIR=""
EXISTING_FILE_MANIFEST=""
LOADED_LABEL_MANIFEST=""
SWITCH_STARTED=0

cleanup_backup() {
  local label
  if [ -z "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
    return 0
  fi
  for label in "${ALL_LABELS[@]}"; do
    rm -f -- "$BACKUP_DIR/$label.plist"
  done
  rm -f -- \
    "$BACKUP_DIR/daemon-health.json" \
    "$BACKUP_DIR/text-endpoint-models.json"
  rm -f -- "$EXISTING_FILE_MANIFEST" "$LOADED_LABEL_MANIFEST"
  rmdir "$BACKUP_DIR" 2>/dev/null || true
}

capture_current_installation() {
  local label
  local target
  BACKUP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/girl-agent-launchd-install.XXXXXX")"
  EXISTING_FILE_MANIFEST="$BACKUP_DIR/existing-files"
  LOADED_LABEL_MANIFEST="$BACKUP_DIR/loaded-labels"
  : > "$EXISTING_FILE_MANIFEST"
  : > "$LOADED_LABEL_MANIFEST"
  for label in "${ALL_LABELS[@]}"; do
    target="$(plist_path "$label")"
    if [ -f "$target" ]; then
      cp -p "$target" "$BACKUP_DIR/$label.plist"
      printf '%s\n' "$label" >> "$EXISTING_FILE_MANIFEST"
    fi
    if label_is_loaded "$label"; then
      if [ ! -f "$target" ]; then
        echo "Cannot safely replace loaded LaunchAgent without its plist: $label" >&2
        return 1
      fi
      printf '%s\n' "$label" >> "$LOADED_LABEL_MANIFEST"
    fi
  done
}

rollback_installation() {
  local label
  local target
  local rollback_failed=0
  set +e
  echo "LaunchAgent switch failed; restoring prior files and loaded state." >&2
  for label in "${ALL_LABELS[@]}"; do
    unload_label "$label"
  done
  for label in "${ALL_LABELS[@]}"; do
    target="$(plist_path "$label")"
    if contains_line "$label" "$EXISTING_FILE_MANIFEST"; then
      cp -p "$BACKUP_DIR/$label.plist" "$target" || rollback_failed=1
    else
      remove_exact_plist "$label" || rollback_failed=1
    fi
  done
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    load_label "$label" || rollback_failed=1
  done < "$LOADED_LABEL_MANIFEST"
  cleanup_backup
  if [ "$rollback_failed" -ne 0 ]; then
    echo "LaunchAgent rollback was incomplete; manual recovery is required." >&2
  fi
  return "$rollback_failed"
}

handle_error() {
  local exit_code=$?
  trap - ERR INT TERM
  if [ "$SWITCH_STARTED" = "1" ]; then
    rollback_installation || true
  else
    cleanup_backup
  fi
  if [ "$exit_code" -eq 0 ]; then
    exit_code=1
  fi
  exit "$exit_code"
}

handle_signal() {
  local exit_code="$1"
  trap - ERR INT TERM
  if [ "$SWITCH_STARTED" = "1" ]; then
    rollback_installation || true
  else
    cleanup_backup
  fi
  exit "$exit_code"
}

wait_for_daemon_health() {
  local response_file="$BACKUP_DIR/daemon-health.json"
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
  while :; do
    if curl --silent --show-error --fail --max-time 2 \
      -o "$response_file" "$DAEMON_HEALTH_URL" 2>/dev/null \
      && "$PYTHON_BIN" - "$response_file" >/dev/null 2>&1 <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
if payload.get("status") != "ok":
    raise SystemExit(1)
capture = payload.get("world_v2_capture")
if not isinstance(capture, dict) or capture.get("status") != "ready":
    raise SystemExit(1)
interior = payload.get("character_interior")
if not isinstance(interior, dict):
    raise SystemExit(1)
if (
    interior.get("status") != "ready"
    or interior.get("installed") is not True
    or interior.get("semantic_author_count") != 1
    or interior.get("legacy_interface_invocations") != 0
    or interior.get("parallel_character_author_conflicts") != 0
    or interior.get("dual_write_conflicts") != 0
    or interior.get("topology_issues") != []
):
    raise SystemExit(1)
topology = interior.get("topology_evidence")
if not isinstance(topology, dict):
    raise SystemExit(1)
if (
    topology.get("duplicate_purpose_owner_count") != 0
    or topology.get("legacy_compatibility_route_installed") is not False
    or len(topology.get("semantic_author_ids", ())) != 1
):
    raise SystemExit(1)
PY
    then
      rm -f -- "$response_file"
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "Daemon health did not become ready: $DAEMON_HEALTH_URL" >&2
      return 1
    fi
    sleep 1
  done
}

wait_for_text_endpoint() {
  local endpoint_port="${WORLD_V2_TEXT_ENDPOINT_PORT:-8188}"
  local endpoint_model="${WORLD_V2_TEXT_ENDPOINT_MODEL:-mlx-community/Qwen3-1.7B-4bit}"
  local models_url="http://127.0.0.1:${endpoint_port}/v1/models"
  local response_file="$BACKUP_DIR/text-endpoint-models.json"
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
  while :; do
    if curl --silent --show-error --fail --max-time 2 \
      -o "$response_file" "$models_url" 2>/dev/null \
      && "$PYTHON_BIN" - "$response_file" "$endpoint_model" >/dev/null 2>&1 <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
models = payload.get("data")
if not isinstance(models, list) or not any(
    isinstance(item, dict) and item.get("id") == sys.argv[2] for item in models
):
    raise SystemExit(1)
PY
    then
      rm -f -- "$response_file"
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "Text endpoint model list did not become ready: $models_url" >&2
      return 1
    fi
    sleep 1
  done
}

preflight
mkdir -p "$LAUNCH_AGENTS" "$ROOT/logs"
if ! capture_current_installation; then
  cleanup_backup
  exit 1
fi
trap handle_error ERR
trap 'handle_signal 130' INT
trap 'handle_signal 143' TERM
SWITCH_STARTED=1

# Stop only the exact Girl-Agent labels participating in this transaction.
for label in "${ALL_LABELS[@]}"; do
  unload_label "$label"
done

for label in "${DESIRED_LABELS[@]}"; do
  atomic_install_plist "$label"
done
for label in "${ALL_LABELS[@]}"; do
  if ! is_desired "$label"; then
    remove_exact_plist "$label"
  fi
done
for label in "${DESIRED_LABELS[@]}"; do
  load_label "$label"
done

for label in "${DESIRED_LABELS[@]}"; do
  if ! label_is_loaded "$label"; then
    echo "Required LaunchAgent is not loaded: $label" >&2
    false
  fi
done
for label in "${ALL_LABELS[@]}"; do
  if ! is_desired "$label" && label_is_loaded "$label"; then
    echo "Retired or disabled LaunchAgent remains loaded: $label" >&2
    false
  fi
done

wait_for_daemon_health
if [ "$TEXT_ENDPOINT_ENABLED" = "1" ]; then
  wait_for_text_endpoint
fi

SWITCH_STARTED=0
trap - ERR INT TERM
cleanup_backup

echo "Installed and verified Girl-Agent launchd services."
echo "Logs:"
echo "  $ROOT/logs/qq-ws.out.log"
echo "  $ROOT/logs/daemon.out.log"
echo "  $ROOT/logs/napcat.out.log"
echo "  $ROOT/logs/rsshub.out.log"
if [ "$TEXT_ENDPOINT_ENABLED" = "1" ]; then
  echo "  $ROOT/logs/text-endpoint.out.log"
  echo "  $ROOT/logs/text-endpoint-watchdog.log"
else
  echo "Text endpoint launchd services were disabled by WORLD_V2_TEXT_ENDPOINT_ENABLED."
fi
if [ "$SILLYTAVERN_ENABLED" = "1" ]; then
  echo "  $ROOT/logs/sillytavern.out.log"
else
  echo "SillyTavern launchd service was skipped. Use INSTALL_SILLYTAVERN=1 to install it."
fi
