# Agent Delegation Logging Configuration

## Log File Locations

All log files are created in the same directory as `delegator-delegatee.yaml`:

**Command logs:**
- `delegator-delegatee-skill.log` - Records all CLI commands (start_task, correct, stop, ping, send_message)

**Daemon logs:**
- `delegator-delegatee-<PID>.log` - One log file per daemon process, named by PID

## Example Log Paths

For config at `~/.openclaw/agents/main/delegator-delegatee.yaml`:
```bash
~/.openclaw/agents/main/delegator-delegatee-skill.log
~/.openclaw/agents/main/delegator-delegatee-3655675.log
```

## Log Format

Both log types use the same format:
```
2026-05-01 00:21:45 [INFO] delegation_skill: Starting task for delegatee coding-agent from file: /path/to/task.md
2026-05-01 00:21:45 [INFO] delegation_worker: Delegation Worker initialized for coding-agent
2026-05-01 00:21:45 [INFO] delegation_worker: Log file: ~/.openclaw/agents/main/delegator-delegatee-3655676.log
```

## Troubleshooting

**No log files created:**
- Verify `delegator-delegatee.yaml` exists and is readable
- Check that Python scripts have write permissions in the config directory
- Run commands with verbose output: `bash -x bin/start_task ...`

**Daemon process not found but logged as running:**
- Stale PID in state file (`~/.hermes/delegation_daemons.json`)
- Clean up: `rm ~/.hermes/delegation_daemons.json`
- Restart daemon via `bin/start_task <delegatee-name> <task-file.md>`

**Log files growing large:**
- Implement log rotation at the system level (logrotate)
- Or manually archive old logs: `mv delegator-delegatee-*.log /archive/`

## Viewing Logs

```bash
# Watch command logs in real-time
tail -f ~/.openclaw/agents/main/delegator-delegatee-skill.log

# View specific daemon log
cat ~/.openclaw/agents/main/delegator-delegatee-3655675.log

# Search for errors
grep ERROR ~/.openclaw/agents/main/delegator-delegatee-*.log
```
