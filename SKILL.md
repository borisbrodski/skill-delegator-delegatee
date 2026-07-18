---
name: delegator-delegatee
description: Delegate tasks to one or many delegatee agents via Matrix chat rooms, automatically track progress, detect completion, and hand control back to the delegator.
version: 2.7.0
author: Boris Brodski
created: 2026-04-30
updated: 2026-06-15
category: autonomous-ai-agents
---

# Delegator-Delegatee Skill

**Delegator** — The agent using this skill. Starts tasks, receives progress updates, issues corrections, stops tasks, pauses/resumes.

**Delegatee** — The agent receiving the task. Works autonomously until it signals completion or asks a question with `=== NO_MORE_ACTIONS ===`. The delegator decides: accept completion, or dispatch a follow-up.

## Core Concepts

- **One task per delegatee** — starting a new task stops any running task for that delegatee.
- **Hierarchical** — a delegator can itself be a delegatee (Manager → Orchestrator → Coder).
- **Multiple delegatees** — one delegator can run many independent delegations in parallel.

## Backpressure & Pause

- **Outbound to delegator is gated by idle state.** Progress reports, completion handoffs, and alerts are queued on disk while the delegator is busy, flushed FIFO when idle. Health alerts bypass the queue.
- **Inbound to delegatee is immediate.** `correct`, `stop`, `ping`, `pause`, `resume` deliver instantly.
- **Pause/resume** — the worker suppresses all outbound reports while paused; accumulated pause time is subtracted from elapsed thresholds on resume.

## What You Receive

### Progress Updates (~15 min)
Deduplicated messages from the delegatee since the last report.

### Completion (immediate)
Full conversation summary when `=== NO_MORE_ACTIONS ===` is detected.

### Health Alerts
Sent on consecutive worker errors or when the delegatee is wedged.

## Commands

All commands live in `{baseDir}/scripts/`.

### start_task
```bash
{baseDir}/scripts/start_task <delegatee-name> <task-file.md> [timeout-sec] [--no-reset-session]
```
Stops any existing daemon, starts fresh, sends the task, activates background polling. Verifies daemon survived startup.

> **`<delegatee-name>` is the name from _your_ config's `delegatees:` list** — run `{baseDir}/scripts/list_delegatees` to see the exact names. Do **not** copy names from the example config (`coding-agent`, `testing-agent`) or the docs; those are illustrative and a `start_task` against them fails with "unknown delegatee", wasting a round-trip.

Session reset is **ON BY DEFAULT** — every new task sends `/reset` first for a clean context. Pass `--no-reset-session` to preserve session when answering a delegatee's question or continuing an interactive exchange.

### correct
```bash
{baseDir}/scripts/correct <delegatee-name> <correction-message>
```
Issues a correction without stopping the task. If no daemon is running (task already finished), automatically re-engages via `start_task` with keep-session.

### stop
```bash
{baseDir}/scripts/stop <delegatee-name>
```
Stops the delegation and kills the daemon.

### ping
```bash
{baseDir}/scripts/ping <delegatee-name>
```
Sends "continue" to a delegatee that fell out of its action loop.

### pause / resume
```bash
{baseDir}/scripts/pause <delegatee-name>
{baseDir}/scripts/resume <delegatee-name>
```
Freeze/unfreeze all outbound reports and timers while preserving session state.

### list_delegatees
```bash
{baseDir}/scripts/list_delegatees
```
Lists configured delegatees with status (IDLE/BUSY).

### send_message
```bash
{baseDir}/scripts/send_message --target <delegator|delegatee-name> --message "text"
```
Sends a raw message to the delegator or a delegatee without changing task/daemon state.

## Key Pitfalls

### Phantom delegation
Writing a spec file does NOT delegate. You MUST run `start_task` in the same turn. Verify the PID from the script output — never fabricate one.

### Context-compression stall
When delegatee context grows large, it stalls on "Preflight compression." Fix: wait for compression to finish. Delegatee will resume automatically.

### Correction-ignoring drift
Delegatee may ignore corrections and revert to investigation. Send a second explicit directive. If still stuck, `stop` and re-delegate.

### Post-completion drift
After signaling completion, the delegatee may start investigating unrelated topics. `stop` immediately — deliverables are on disk.

### Methodological constraint drift
Delegatee may ignore "FORBIDDEN" or "DO NOT" instructions. If `correct` doesn't work, `stop` and re-delegate with a minimal, single-objective spec.

### File must use absolute path
Always specify file paths as absolute paths in task specs. Delegatee may use relative paths from its cwd, landing files in unexpected locations.

## Best Practices

- Split tasks into small, self-contained units. Avoid multi-step tasks that require context from previous steps.
- Keep task specs narrow (single feature, ~1–2K chars).
- In rare cases: When substantive work is on disk but delivery is mechanical, complete it manually instead of re-delegating.
