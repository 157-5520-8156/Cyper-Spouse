#!/bin/zsh
# Watchdog for the local MLX text-endpoint service (com.girl-agent.text-endpoint).
#
# The MLX server has crashed repeatedly with Metal out-of-memory while its
# process stayed alive, so launchd's KeepAlive never restarts it.  This job
# probes both the OpenAI-compatible control plane and inference path.  The
# generation probe first acquires the same atomic capacity lease as daemon
# callers; a real in-flight/cooling-down inference is observed, never queued
# behind or counted as a watchdog failure.

set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

HEALTH_PORT="${WORLD_V2_TEXT_ENDPOINT_PORT:-8188}"
HEALTH_MODEL="${WORLD_V2_TEXT_ENDPOINT_MODEL:-mlx-community/Qwen3-1.7B-4bit}"
MODELS_URL="http://127.0.0.1:${HEALTH_PORT}/v1/models"
INFERENCE_URL="http://127.0.0.1:${HEALTH_PORT}/v1/chat/completions"
SERVICE="gui/$(id -u)/com.girl-agent.text-endpoint"
THROTTLE_FILE="${TMPDIR:-/tmp}/girl-agent-text-endpoint-watchdog.last"
INFERENCE_FAILURE_FILE="${TMPDIR:-/tmp}/girl-agent-text-endpoint-watchdog.failures"
CAPACITY_DIR="${WORLD_V2_TEXT_ENDPOINT_CAPACITY_DIR:-${TMPDIR:-/tmp}/girl-agent-text-endpoint.capacity}"
CAPACITY_STATE="${CAPACITY_DIR}/state"
CAPACITY_PYTHON="${WORLD_V2_TEXT_ENDPOINT_PYTHON:-/usr/bin/python3}"
THROTTLE_SECONDS=300
INFERENCE_FAILURE_THRESHOLD=3
CAPACITY_ACTIVE_LEASE_SECONDS=300
CAPACITY_COOLDOWN_SECONDS="${WORLD_V2_TEXT_ENDPOINT_CAPACITY_COOLDOWN_SECONDS:-120}"
WATCHDOG_LOG_FILE="${WORLD_V2_TEXT_ENDPOINT_WATCHDOG_LOG_FILE:-$HOME/Projects/Girl-Agent/logs/text-endpoint-watchdog.log}"
HEALTH_PAYLOAD="{\"model\":\"${HEALTH_MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"只输出0\"}],\"max_completion_tokens\":1,\"temperature\":0}"
CAPACITY_TOKEN=""

log_watchdog() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') text-endpoint $1" >> \
        "$WATCHDOG_LOG_FILE"
}

read_capacity_line() {
    sed -n "${1}p" "$CAPACITY_STATE" 2>/dev/null || true
}

clear_capacity_if_owned() {
    [[ -n "$CAPACITY_TOKEN" ]] || return 0
    "$CAPACITY_PYTHON" - "$CAPACITY_DIR" "$CAPACITY_TOKEN" <<'PY' || true
import fcntl
import os
from pathlib import Path
import sys

marker = Path(sys.argv[1])
expected = sys.argv[2]
lock_path = marker.with_name(marker.name + ".lock")
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
try:
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        lines = (marker / "state").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    if len(lines) >= 2 and lines[1].strip() == expected:
        try:
            (marker / "state").unlink(missing_ok=True)
            for temporary in marker.glob(".state.*"):
                temporary.unlink(missing_ok=True)
            marker.rmdir()
        except OSError:
            pass
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
PY
}

write_capacity_state() {
    local deadline="$1"
    local state_kind="$2"
    "$CAPACITY_PYTHON" - "$CAPACITY_DIR" "$CAPACITY_TOKEN" "$deadline" "$state_kind" <<'PY'
import fcntl
import os
from pathlib import Path
import sys

marker = Path(sys.argv[1])
expected = sys.argv[2]
deadline = sys.argv[3]
state_kind = sys.argv[4]
lock_path = marker.with_name(marker.name + ".lock")
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
try:
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        lines = (marker / "state").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    if len(lines) < 2 or lines[1].strip() != expected:
        raise SystemExit(1)
    temporary = marker / f".state.watchdog.{os.getpid()}"
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(f"{deadline}\n{expected}\n{state_kind}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, marker / "state")
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
PY
}

claim_probe_capacity() {
    local now
    now=$(date +%s)
    CAPACITY_TOKEN="watchdog:$$:${now}"
    "$CAPACITY_PYTHON" - \
        "$CAPACITY_DIR" \
        "$CAPACITY_TOKEN" \
        "$now" \
        "$CAPACITY_ACTIVE_LEASE_SECONDS" <<'PY'
import fcntl
import os
from pathlib import Path
import sys

marker = Path(sys.argv[1])
token = sys.argv[2]
now = float(sys.argv[3])
lease_seconds = float(sys.argv[4])
lock_path = marker.with_name(marker.name + ".lock")
fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
try:
    os.fchmod(fd, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        marker.mkdir(mode=0o700, parents=False, exist_ok=False)
    except FileExistsError:
        try:
            lines = (marker / "state").read_text(encoding="utf-8").splitlines()
            deadline = float(lines[0]) if len(lines) >= 3 else None
        except (OSError, ValueError):
            deadline = None
        if deadline is None:
            try:
                if now - marker.stat().st_mtime <= lease_seconds:
                    raise SystemExit(1)
            except OSError:
                raise SystemExit(1)
        elif deadline > now:
            raise SystemExit(1)
        try:
            (marker / "state").unlink(missing_ok=True)
            for temporary in marker.glob(".state.*"):
                temporary.unlink(missing_ok=True)
            marker.rmdir()
            marker.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError:
            raise SystemExit(1)
    temporary = marker / f".state.watchdog.{os.getpid()}"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(f"{now + lease_seconds:.6f}\n{token}\nactive\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, marker / "state")
finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
PY
}

restart_service() {
    local reason="$1"
    local now
    local last
    now=$(date +%s)
    if [[ -f "$THROTTLE_FILE" ]]; then
        last=$(cat "$THROTTLE_FILE" 2>/dev/null || echo 0)
        if (( now - last < THROTTLE_SECONDS )); then
            return 1
        fi
    fi

    if launchctl kickstart -k "$SERVICE"; then
        echo "$now" > "$THROTTLE_FILE"
        log_watchdog "${reason}; kickstarted"
        return 0
    fi
    log_watchdog "${reason}; kickstart failed"
    return 1
}

# A failed model-list request means the process is not serving even its cheap
# control plane, so waiting for three generation probes would hide a genuinely
# wedged or dead service. The existing restart throttle still protects model
# load from a tight kill loop.
if ! curl --silent --show-error --fail --max-time 2 -o /dev/null "$MODELS_URL"; then
    restart_service "model-list probe failed" || true
    exit 0
fi

# The model-list route can remain healthy while MLX's serial generation worker
# is legitimately busy. Acquire the same cross-process capacity lease used by
# daemon callers before probing. If another owner is active or cooling down,
# do not submit another request and do not carry old failures into a future
# idle period.
if ! claim_probe_capacity; then
    capacity_owner=$(read_capacity_line 2)
    capacity_status=$(read_capacity_line 3)
    case "$capacity_owner" in
        daemon:*) rm -f "$INFERENCE_FAILURE_FILE" ;;
    esac
    log_watchdog "capacity busy owner=${capacity_owner:-unknown} status=${capacity_status:-unknown}; inference probe skipped"
    exit 0
fi

# With the exclusive lease held, a one-token completion tests the inference
# path without joining a real request's queue. Three separated failures are
# still required before a restart.
if curl --silent --show-error --fail --max-time 8 -o /dev/null \
    -H "Content-Type: application/json" \
    --data-binary "$HEALTH_PAYLOAD" \
    "$INFERENCE_URL"; then
    clear_capacity_if_owned
    rm -f "$INFERENCE_FAILURE_FILE"
    exit 0
fi

failures=0
if [[ -f "$INFERENCE_FAILURE_FILE" ]]; then
    recorded_failures=$(cat "$INFERENCE_FAILURE_FILE" 2>/dev/null || echo 0)
    case "$recorded_failures" in
        ''|*[!0-9]*) ;;
        *) failures=$recorded_failures ;;
    esac
fi
(( failures += 1 ))
echo "$failures" > "$INFERENCE_FAILURE_FILE"

if (( failures < INFERENCE_FAILURE_THRESHOLD )); then
    now=$(date +%s)
    if (( CAPACITY_COOLDOWN_SECONDS > 0 )); then
        write_capacity_state "$(( now + CAPACITY_COOLDOWN_SECONDS ))" "watchdog_cooldown"
    else
        clear_capacity_if_owned
    fi
    exit 0
fi

if restart_service "inference probe failed ${failures} consecutive times"; then
    rm -f "$INFERENCE_FAILURE_FILE"
fi
clear_capacity_if_owned
exit 0
