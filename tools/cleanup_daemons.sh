#!/bin/bash
# cleanup_daemons.sh - Manually clean up zombie daemon processes
# Usage: ./cleanup_daemons.sh [--dry-run]

set -e

# Detect Hermes environment and use correct state file path
HOME_DIR=$(eval echo ~)
IS_HERMES=false
if echo "$HOME_DIR" | grep -q '.hermes/profiles'; then
    IS_HERMES=true
fi

# In Hermes env, derive the real host home (strip the .hermes chroot suffix)
# Otherwise, use $HOME/.hermes/
if [ "$IS_HERMES" = true ]; then
    REAL_HOME="${HOME_DIR%%/.hermes/*}"
    STATE_FILE="$REAL_HOME/.openclaw/agents/main/delegation_daemons.json"
else
    STATE_FILE="$HOME_DIR/.hermes/delegation_daemons.json"
fi

if [ ! -f "$STATE_FILE" ]; then
    echo "No daemon state file found: $STATE_FILE"
    exit 0
fi

DRY_RUN=false
if [ "$1" == "--dry-run" ]; then
    DRY_RUN=true
    echo "=== DRY RUN MODE ==="
fi

echo "Checking daemon processes..."

# Parse JSON and check each PID
python3 << PYTHON_SCRIPT
import json
import os
import sys
from pathlib import Path

state_file = "$STATE_FILE"
dry_run = "$DRY_RUN".lower() == "true"

with open(state_file, 'r') as f:
    state = json.load(f)

cleaned = 0
for delegatee_name, info in list(state.items()):
    pid = info.get('pid')
    if not pid:
        continue
    
    try:
        os.kill(pid, 0)  # Check if process exists
        print(f"✓ {delegatee_name} (PID {pid}) - RUNNING")
    except OSError:
        print(f"✗ {delegatee_name} (PID {pid}) - ZOMBIE")
        cleaned += 1
        
        if not dry_run:
            del state[delegatee_name]

if not dry_run and cleaned > 0:
    with open(state_file, 'w') as f:
        json.dump(state, f, indent=2)
    print(f"\nCleaned {cleaned} zombie daemon(s)")
elif dry_run:
    print(f"\nWould clean {cleaned} zombie daemon(s). Run without --dry-run to apply.")
else:
    print("\nNo zombies found. All daemons healthy.")
PYTHON_SCRIPT
