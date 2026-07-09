# Delegation Daemon Operational Notes (2026-05-01)

## Daemon Lifecycle Pattern

The delegation daemon is **event-driven**, not a persistent background service:

```
Task Queued → Daemon Starts → Polls for Messages → Task Complete/Idle Timeout → Daemon Stops
```

### Key Timing Parameters

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `poll_interval_sec` | 15 | How often daemon polls Matrix for new messages and checks completion marker |
| `progress_report_interval_sec` | 900 | Interval between periodic progress reports to delegator (only when idle) |

### Normal Daemon Behavior

1. **Start**: Triggered by `start_task` command creating `delegation_startup_{name}.json`
2. **Run**: Polls every `poll_interval_sec`, processes tasks, logs activity
3. **Stop**: After task finalization (`=== NO_MORE_ACTIONS ===`) or manual stop

**Example log sequence:**
```
03:29:05 - Daemon started (PID 3667868)
03:29:05 - Found startup task file
03:29:05 - Starting task for delegatee coding-agent
03:29:05 - Task started for coding-agent
03:29:05 - Worker started, check period: 300s
03:29:05 - Finalizing delegation for coding-agent
03:29:05 - Delegation finalized
03:34:05 - Worker stopped after 1 iterations
```

## Heartbeat Status Interpretation

### ⚠️ "Daemon dead" - NOT AN ERROR

When the heartbeat shows:
```
Status:
⚠️  coding-agent: Daemon dead (PID 3667868)
```

**This is NORMAL behavior after task completion.** The daemon:
- Completed its assigned work
- Exited cleanly after idle timeout
- Is NOT crashed or stuck

### When to Investigate

Only investigate if you see:
1. **ERROR entries in logs**: `grep ERROR delegator-delegatee-*.log`
2. **Daemon never started**: No log file for expected PID
3. **Task stuck mid-execution**: Daemon running > 30 min with no progress messages

## Log File Locations

All daemon activity logged to:
```
~/.openclaw/agents/main/delegator-delegatee-{pid}.log
```

**State tracking:**
```json
// delegation_daemons.json
{
  "coding-agent": {
    "pid": 3667868,
    "started_at": "2026-05-01T03:29:05+02:00",
    "room_id": "!coding-room:example.com"
  }
}
```

## Startup Task File Format

When `start_task` is called, it creates a JSON trigger file:

```json
{
  "task_file": "tasks/test-simple-task.md",
  "timestamp": "2026-05-01T03:10:08+02:00"
}
```

Daemon reads this during `__init__`, stores task path, processes asynchronously in `run()` method.

**Fix Applied (2026-05-01):** Previously called `asyncio.run()` in sync context causing error. Now stores metadata for async processing.

## Troubleshooting Commands

```bash
# Check if daemon is currently running
ps aux | grep {pid} | grep delegation_worker

# View current daemon state
cat ~/.openclaw/agents/main/delegation_daemons.json

# Follow latest daemon logs (replace PID)
tail -f ~/.openclaw/agents/main/delegator-delegatee-3667868.log

# Search all daemon logs for errors
grep ERROR ~/.openclaw/agents/main/delegator-delegatee-*.log

# Check recent daemon lifecycle events
cat ~/.openclaw/agents/main/delegator-delegatee-*.log | grep -E "(started|stopped|finalized)" | tail -20
```

## Common Misconceptions

❌ "Daemon dead means something broke" → ✅ Normal after task completion  
❌ "Daemon should run forever" → ✅ Event-driven, stops when idle  
❌ "Need to restart daemon manually" → ✅ New task auto-starts daemon  
