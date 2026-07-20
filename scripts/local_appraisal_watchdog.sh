#!/bin/zsh
# Watchdog for the local MLX appraisal service (com.girl-agent.local-appraisal).
#
# The MLX server has crashed repeatedly with Metal out-of-memory while its
# process stayed alive, so launchd's KeepAlive never restarts it.  This job
# probes the OpenAI-compatible endpoint with a strict timeout and kickstarts
# the service when it is unresponsive.  A marker file throttles restarts so a
# model that is merely slow to load is not kill-looped.

set -u

HEALTH_URL="http://127.0.0.1:8188/v1/models"
SERVICE="gui/$(id -u)/com.girl-agent.local-appraisal"
THROTTLE_FILE="${TMPDIR:-/tmp}/girl-agent-local-appraisal-watchdog.last"
THROTTLE_SECONDS=300

if curl -s --max-time 5 -o /dev/null "$HEALTH_URL"; then
    exit 0
fi

now=$(date +%s)
if [[ -f "$THROTTLE_FILE" ]]; then
    last=$(cat "$THROTTLE_FILE" 2>/dev/null || echo 0)
    if (( now - last < THROTTLE_SECONDS )); then
        exit 0
    fi
fi

echo "$now" > "$THROTTLE_FILE"
echo "$(date '+%Y-%m-%d %H:%M:%S') local-appraisal unresponsive; kickstarting" >> \
    "$HOME/Projects/Girl-Agent/logs/local-appraisal-watchdog.log"
launchctl kickstart -k "$SERVICE"
