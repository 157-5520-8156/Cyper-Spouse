#!/usr/bin/env bash
set -euo pipefail

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
launchctl unload "$LAUNCH_AGENTS/com.girl-agent.proactive.plist" 2>/dev/null || true
launchctl unload "$LAUNCH_AGENTS/com.girl-agent.qq-ws.plist" 2>/dev/null || true
launchctl unload "$LAUNCH_AGENTS/com.girl-agent.sillytavern.plist" 2>/dev/null || true
rm -f "$LAUNCH_AGENTS/com.girl-agent.proactive.plist"
rm -f "$LAUNCH_AGENTS/com.girl-agent.qq-ws.plist"
rm -f "$LAUNCH_AGENTS/com.girl-agent.sillytavern.plist"
echo "Uninstalled Girl-Agent launchd services."
