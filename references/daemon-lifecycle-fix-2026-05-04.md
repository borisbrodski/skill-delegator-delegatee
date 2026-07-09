# Daemon Lifecycle Fix — 2026-05-04

## Problem: Old messages in final report + slow completion detection

### Symptoms
1. After delegatee reported `=== NO_MORE_ACTIONS ===`, the skill detected it after a long delay (30+ seconds)
2. Final handoff message contained messages posted BEFORE the task was started
3. When a new task was started before previous task ended, old polling continued collecting stale messages

### Root Causes

#### Bug 1: `cmd_start_task` didn't stop existing daemon
**File:** `bin/delegation_core.py::cmd_start_task()`

When `start_task` was called while a daemon was already running for the same delegatee:
```python
# OLD — returned early, old daemon kept polling
if self.is_daemon_running(delegatee_name):
    return True, f"Daemon already running for {delegatee_name}"
```

The old daemon continued with its stale `task_start_timestamp`, so:
- Messages from the NEW task were filtered as "old" (timestamp < stale cutoff)
- Pre-task room history was collected into `messages_collected`
- The new startup file was never read (daemons only check it at initialization)

**Fix:** Stop existing daemon before creating startup file and launching fresh process.

#### Bug 2: Polling interval default was 30s, not 15s
**File:** `scripts/delegation_worker.py` line 162

```python
# OLD — 30 second default
self.poll_interval_sec = timeouts.get('poll_interval_sec', timeouts.get('delegator_check_period_sec', 30))
```

After completion was detected, the loop still hit `await asyncio.sleep(30)` before checking its while condition. Combined with only setting `polling_active=False` (not `self.running=False`), this added 15-30 seconds of unnecessary delay after `=== NO_MORE_ACTIONS ===`.

**Fix:** Default to 15s, set both flags in `_finalize_delegation()`.

#### Bug 3: No clock skew buffer for timestamp filtering
**File:** `scripts/delegation_worker.py::check_progress()`

```python
# OLD — exact comparison, no buffer
if origin_server_ts < self.state.task_start_timestamp:
    # skip old message
```

Local `time.time()` and Matrix server `origin_server_ts` (milliseconds) are not perfectly synchronized. Network latency + clock drift means messages sent in the same second as task start could be filtered incorrectly.

**Fix:** Use 2-second buffer: `task_cutoff = self.state.task_start_timestamp - 2.0`.

### Applied Fixes
| File | Change |
|------|--------|
| `bin/delegation_core.py` | `cmd_start_task()` now calls `stop_daemon()` before creating startup file |
| `scripts/delegation_worker.py:162` | Default poll interval: 30 → 15 seconds |
| `scripts/delegation_worker.py:458` | Timestamp filter: added 2s buffer for clock skew |
| `scripts/delegation_worker.py:591` | `_finalize_delegation()` sets `self.running = False` (immediate exit) |
| `scripts/delegation_worker.py:336` | Preamble text: "Stop all current tasks..." → "Stop previous task." |

### Verification
All 67 existing tests pass after changes.
