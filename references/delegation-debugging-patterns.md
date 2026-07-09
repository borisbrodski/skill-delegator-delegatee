# Common Delegation Debugging Patterns

## Issue: Delegator Never Receives Completion Notifications

**Symptoms:**
- Delegatee posts `=== NO_MORE_ACTIONS ===` in its room
- Daemon polling detects completion (no errors in logs)
- Delegator room receives NO handoff message
- Handoff message appears to "disappear"

### Root Causes & Fixes

#### 1. Matrix URL Trailing Slash (Most Common - 2026-05-01)

**Error:** Config has trailing slash: `https://matrix.example.com/`

**Effect:** API calls become `...//_matrix/client/r0/rooms/...` → HTTP 404

**Where to Check:**
- `~/.openclaw/agents/main/delegator-delegatee.yaml` - line 3 `matrix.url`
- BOTH `delegation_core.py` AND `delegation_worker.py` MatrixClient classes

**Fix in delegation_core.py (line 228):**
```python
self.url = config['matrix']['url'].rstrip('/')
```

**Fix in delegation_worker.py (line 61):**
```python
# Strip trailing slash from URL to avoid double-slash in API paths
self.url = config['matrix']['url'].rstrip('/')
```

**Verification:**
```bash
# Test direct message sending
cd ~/.hermes/profiles/<profile>/skills/delegator-delegatee
bin/send_message --target delegator --message "Test: URL fix verification"
```

#### 2. Logger NameError in _finalize_delegation() (2026-05-01)

**Error:** Using bare `logger.info(...)` instead of `self.logger.info(...)`

**Location:** `delegation_worker.py` line 343 in `_finalize_delegation()`

**Effect:** Handoff message sends successfully, but logging call crashes with NameError. The crash happens AFTER the send, so no error is visible in logs unless you catch it.

**Fix:**
```python
# WRONG (crashes):
logger.info(f"Delegation for {self.delegatee_name} finalized")

# CORRECT:
self.logger.info(f"Delegation for {self.delegatee_name} finalized")
```

**Verification:**
Check daemon logs for NameError traceback after completion:
```bash
tail -f ~/.openclaw/agents/main/delegator-delegatee-*.log | grep -A5 "NameError"
```

## Debugging Checklist

When delegation fails silently:

1. **Check Matrix URL format** (no trailing slash)
   ```bash
   grep "matrix:" -A2 ~/.openclaw/agents/main/delegator-delegatee.yaml
   ```

2. **Test message sending directly**
   ```bash
   bin/send_message --target delegator --message "test"
   bin/send_message --target coding-agent --message "test"
   ```

3. **Check daemon is running**
   ```bash
   bin/list_delegatees  # Should show BUSY status for active delegatee
   cat ~/.openclaw/agents/main/delegation_daemons.json
   ```

4. **Review daemon logs** (look for HTTP errors, NameErrors)
   ```bash
   tail -50 ~/.openclaw/agents/main/delegator-delegatee-*.log
   ```

5. **Verify both MatrixClient classes have URL fix**
   ```bash
   grep -n "rstrip" bin/delegation_core.py scripts/delegation_worker.py
   ```

## Known Working Configuration (2026-05-01)

```yaml
# ~/.openclaw/agents/main/delegator-delegatee.yaml
matrix:
  url: https://matrix.example.com  # NO trailing slash!
  user_id: "@delegator:example.com"
  access_token: "YOUR_ACCESS_TOKEN_HERE"

delegator:
  matrix:
    room_id: "!orchestrator-room:example.com"

delegatees:
  - name: coding-agent
    matrix:
      room_id: "!coding-room:example.com"
```

## Related Files

- `bin/delegation_core.py` - Command-line scripts, has URL fix at line 228
- `scripts/delegation_worker.py` - Background daemon, needs URL fix at line 61
- `references/matrix-url-pitfalls.md` - Detailed Matrix API URL documentation
- `references/logging-configuration.md` - Logger setup patterns
