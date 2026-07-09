#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS" "$ROOT/logs"

cp "$ROOT/launchd/com.girl-agent.sillytavern.plist" "$LAUNCH_AGENTS/"
cp "$ROOT/launchd/com.girl-agent.qq-ws.plist" "$LAUNCH_AGENTS/"
cp "$ROOT/launchd/com.girl-agent.proactive.plist" "$LAUNCH_AGENTS/"

launchctl unload "$LAUNCH_AGENTS/com.girl-agent.proactive.plist" 2>/dev/null || true
launchctl unload "$LAUNCH_AGENTS/com.girl-agent.qq-ws.plist" 2>/dev/null || true
launchctl unload "$LAUNCH_AGENTS/com.girl-agent.sillytavern.plist" 2>/dev/null || true
launchctl load "$LAUNCH_AGENTS/com.girl-agent.sillytavern.plist"
launchctl load "$LAUNCH_AGENTS/com.girl-agent.qq-ws.plist"
launchctl load "$LAUNCH_AGENTS/com.girl-agent.proactive.plist"

echo "Installed and loaded Girl-Agent launchd services."
echo "Logs:"
echo "  $ROOT/logs/sillytavern.out.log"
echo "  $ROOT/logs/qq-ws.out.log"
echo "  $ROOT/logs/proactive.out.log"
