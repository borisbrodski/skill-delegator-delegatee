# Delegator-Delegatee

A production-grade skill for hierarchical agent delegation via Matrix chat rooms. Supports **Hermes** and **OpenClaw** agents with automatic progress tracking, completion detection, and handoff management.

## Overview

This skill enables a **delegator agent** to:

- Delegate tasks to one or many **delegatee agents** via Matrix chat
- Automatically track progress through background message polling
- Detect when the delegatee finishes its action loop (`=== NO_MORE_ACTIONS ===`)
- Hand control back to the delegator for review and next steps
- Issue corrections, stop tasks, pause/resume, or start new ones

### Key Design Principles

1. **One Task Per Delegatee** — only one task runs per delegatee at a time.
2. **Task Files** — long descriptions go into `.md` files, sent as structured messages.
3. **Hierarchical Delegation** — a delegator can itself be a delegatee (Manager → Orchestrator → Coder).
4. **Daemon Management** — background daemons start per delegatee; zombies are cleaned up automatically.
5. **Respect for Busy State** — progress messages are only posted when the delegator is idle.

## How It Works

```
┌─────────────┐     start_task()      ┌──────────────┐
│  Delegator  │ ───────────────────►  │ Delegatee    │
│             │                       │              │
│ Orchestrator│                       │ Coding Agent │
└─────────────┘ ◄───────────────────  └──────────────┘
       ▲               progress updates        │
       │                                       │
       │         === NO_MORE_ACTIONS ===       │
       └───────────────────────────────────────┘
                    completion signal
```

1. **Delegator** runs `start_task <name> <spec.md>` — the script stops any existing daemon, sends the task via Matrix, and launches a background worker.
2. **Worker** polls the delegatee's Matrix room every 15 seconds, collecting messages and checking for the completion marker.
3. **Progress reports** are sent to the delegator every ~15 minutes (only when the delegator is idle).
4. **Completion** is detected when `=== NO_MORE_ACTIONS ===` appears. The worker composes a full conversation summary and delivers it to the delegator.
5. **Control returns** to the delegator, which can accept the result, issue a correction, or start a new task.

## Requirements

- Python 3.8+
- Matrix homeserver access with credentials
- Dependencies: `pip install -r requirements.txt` (PyMatrix, PyYAML, psutil)

## Installation

1. **Copy to your agent's skills directory:**

   **Hermes:**
   ```bash
   cp -r delegator-delegatee ~/.hermes/profiles/<profile>/skills/
   ```

   **OpenClaw:**
   ```bash
   cp -r delegator-delegatee ~/.openclaw/agents/<agent-name>/skills/
   ```

2. **Install dependencies:**
   ```bash
   cd <skills-dir>/delegator-delegatee
   pip install -r requirements.txt
   ```

3. **Create configuration:**
   ```bash
   cp config.example.yaml ~/.hermes/delegator-delegatee.yaml
   # Edit with your Matrix credentials and delegatee definitions
   ```

4. **Make scripts executable:**
   ```bash
   chmod +x scripts/*
   ```

## Quick Start

```bash
# List available delegatees
scripts/list_delegatees

# Create a task file
cat > tasks/my-task.md << 'EOF'
# Implement Feature X

Requirements:
1. Step one
2. Step two

Deliverables:
- Working feature
- Tests passing
EOF

# Start delegation (session reset is ON by default)
scripts/start_task coding-agent tasks/my-task.md 600

# Preserve session when answering a delegatee's question
scripts/start_task coding-agent tasks/answer.md --no-reset-session

# Issue a correction (without stopping)
scripts/correct coding-agent "Focus on database first, not the UI"

# Ping a stuck delegatee
scripts/ping coding-agent

# Pause / resume (freeze outbound reports, preserve session)
scripts/pause coding-agent
scripts/resume coding-agent

# Stop delegation
scripts/stop coding-agent
```

## Commands

| Command | Description |
|---------|-------------|
| `start_task <name> <spec.md> [timeout] [--no-reset-session]` | Start a new task. Stops existing daemon, sends task, launches worker. |
| `correct <name> <message>` | Issue correction without stopping. Re-engages via `start_task` if daemon is dead. |
| `stop <name>` | Stop delegation and kill daemon. |
| `ping <name>` | Send "continue" to a delegatee that fell out of its action loop. |
| `pause <name>` | Freeze all outbound reports and alerts while preserving session. |
| `resume <name>` | Unfreeze. Paused duration is subtracted from elapsed thresholds. |
| `list_delegatees` | List configured delegatees with status (IDLE/BUSY). |

## Configuration

See `config.example.yaml` for a complete template. Key sections:

```yaml
delegator:
  name: orchestrator-agent
  type: Hermes                # 'Hermes' or 'OpenClaw'
  profile: my-profile         # Hermes profile name
  matrix:
    url: https://matrix.example.com
    user_id: @user:example.com
    access_token: YOUR_TOKEN
    room_id: "!room:example.com"

timeouts:
  poll_interval_sec: 15              # Poll every N seconds
  progress_report_interval_sec: 900  # Progress report every N seconds

delegatees:
  - name: coding-agent
    description: "Code implementation"
    type: Hermes
    profile: coder-profile
    matrix:
      room_id: "!coding-room:example.com"
```

## Message Protocols

### Start Task
```
Here is the new task from delegator agent:

<task description>

When no more tool calls are issued, call delegator attention by adding `=== NO_MORE_ACTIONS ===` (exact wording!) to summary or question.
```

### Completion Detection
The exact marker `=== NO_MORE_ACTIONS ===` (case-sensitive, no extra spaces) triggers:
1. Stop polling
2. Compose full conversation summary
3. Deliver handoff to delegator
4. Clean up daemon

## Architecture

| Component | Location | Role |
|-----------|----------|------|
| Bash scripts | `scripts/` | CLI wrappers: argument parsing, daemon lifecycle |
| Core library | `src/delegation_core.py` | Config loading, Matrix communication, command dispatch |
| Worker | `src/delegation_worker.py` | Background polling, progress tracking, completion detection |
| Matrix client | `src/matrix_client.py` | Matrix API wrapper (auth, send, poll, idle detection) |
| Path utils | `src/path_utils.py` | Cross-platform path resolution for state files |

## Security

- **DO NOT** commit `*.yaml` config files — they are excluded via `.gitignore`.
- Use environment variables or secret managers for tokens.
- Restrict config file permissions: `chmod 600 delegator-delegatee.yaml`.

## Troubleshooting

### Delegatee not responding
1. Verify room ID in config
2. Check Matrix connectivity
3. `scripts/ping <name>` to wake the delegatee

### Daemon not starting
1. Check daemon state: `~/.hermes/delegation_daemons.json`
2. Look for zombie processes
3. Verify Python dependencies and valid YAML config

### Completion not detected
1. Verify exact marker: `=== NO_MORE_ACTIONS ===` (case-sensitive)
2. Check delegatee actually sent the marker
3. Review worker logs for parsing errors
4. Restart: `scripts/stop <name>` then `scripts/start_task <name> <file>`

## Testing

```bash
# Run unit tests
pytest tests/
```

## License

TBD — placeholder. See `LICENSE` file.
