# Daemon Management Pattern

## Architecture

Each delegatee has its own background daemon process that:
1. Polls the delegatee's Matrix room for messages
2. Detects `=== NO_MORE_ACTIONS ===` completion marker
3. Relays progress to delegator (only when delegator is IDLE)
4. Handles cleanup on task completion or stop

## State Files

### Daemon Tracking
- **Location:** `~/.hermes/delegation_daemons.json`
- **Purpose:** Tracks running daemon PIDs per delegatee
- **Format:**
```json
{
  "coding-agent": {
    "pid": 12345,
    "started_at": "2026-04-30T20:49:15+00:00",
    "room_id": "!abc123:example.com"
  }
}
```

### Delegation State
- **Hermes:** `~/.hermes/profiles/<profile>/delegation_state.json`
- **OpenClaw:** `~/.openclaw/delegation_state.json`
- **Purpose:** Persists active delegation state across restarts

## Zombie Process Detection

Daemons are checked for liveness using:
```python
try:
    os.kill(pid, 0)  # Check if process exists
    return True
except OSError:
    return False
```

**Cleanup triggers:**
- On `start_task` for any delegatee (scans all tracked daemons)
- When daemon PID doesn't respond to signal 0
- Stale entries removed from state file automatically

## Isolation Guarantee

Each delegatee's daemon is independent:
- Separate process group (`start_new_session=True`)
- Killing one daemon doesn't affect others
- Each tracks only its own delegatee's room/messages

## Manual Cleanup (Emergency)

If automation fails, manually clean:
```bash
# Check state file
cat ~/.hermes/delegation_daemons.json

# Kill specific PID
kill -TERM 12345

# Remove from state file (edit JSON or delete file)
rm ~/.hermes/delegation_daemons.json
```

## Daemon Lifecycle

```
start_task → Start daemon if not running → Polling loop active
                                    ↓
                    check_progress() every 300s
                                    ↓
              Completion detected OR stop command received
                                    ↓
                          Stop polling → Kill process → Clean state
```
