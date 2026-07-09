#!/bin/bash
# Usage: ./check_openclaw_idle.sh [agent_name] [threshold_ms]
# Default: main, 30000 (30 seconds)

AGENT_NAME="${1:-main}"
THRESHOLD_MS="${2:-30000}"

STATUS=$(openclaw status --json 2>/dev/null)

if [ -z "$STATUS" ]; then
    echo "ERROR: Could not get openclaw status"
    exit 1
fi

# 1. Check for active subagent sessions first (most important)
HAS_SUBAGENT=$(echo "$STATUS" | jq -r --arg name "$AGENT_NAME" '
  .sessions.byAgent[]? 
  | select(.agentId == $name) 
  | .recent[]? 
  | select(.key | startswith("agent:\($name):subagent:")) 
  | .key
' | head -1)

if [ -n "$HAS_SUBAGENT" ]; then
    echo "BUSY (subagent active: $HAS_SUBAGENT)"
    exit 0
fi

# 2. No subagent → check lastActiveAgeMs
LAST_ACTIVE=$(echo "$STATUS" | jq -r --arg name "$AGENT_NAME" '
  .agents.agents[]? 
  | select(.id == $name) 
  | .lastActiveAgeMs // 999999999
')

if [ "$LAST_ACTIVE" -gt "$THRESHOLD_MS" ]; then
    echo "IDLING (last active ${LAST_ACTIVE}ms ago)"
else
    echo "BUSY (last active ${LAST_ACTIVE}ms ago)"
fi
