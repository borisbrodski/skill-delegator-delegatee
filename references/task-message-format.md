# Task Message Format (Updated 2026-05-01)

## Routing Fix (2026-05-01)

**Problem**: Task messages were being sent to the wrong room (delegator instead of delegatee).

**Fix**: Ensure task messages are sent to `delegatee_room_id`, not `delegator_room_id`:

```python
# In delegation_core.py - cmd_start_task():
room_id = delegatee.get('matrix', {}).get('room_id')  # Delegatee's room, NOT delegator's
client.send_message(room_id, task_message)
```

**Verification**: Check that `delegatee['matrix']['room_id']` is used in:
- `delegation_core.py::cmd_start_task()` - line ~411
- `delegation_worker.py::start_task()` - uses `self.state.delegatee_room_id`

## Old Format (Deprecated)

Previously, the full task content was embedded in the Matrix message:

```
Here is the new task from delegator agent:

[Full task description from file...]

When no more tool calls are issued, call delegator attention by adding `=== NO_MORE_ACTIONS ===` (exact wording!) to summary or question.
```

**Problem:** Large task files get split across multiple Matrix messages, making them difficult for the delegatee to process as a single coherent unit.

## New Format (Current)

Only the filename is sent in the message:

```
New task file: /path/to/task.md

Read the task description from this file.

When no more tool calls are issued, call delegator attention by adding `=== NO_MORE_ACTIONS ===` (exact wording!) to summary or question.
```

**Benefits:**
- Single, complete message regardless of task size
- Delegatee reads file directly from filesystem
- Cleaner message parsing
- No risk of message fragmentation

## Protocol Marker Location

The `=== NO_MORE_ACTIONS ===` marker is **part of the message sent by delegator**, NOT part of the task file. This ensures:

1. The marker is always present and correctly formatted
2. Delegatee doesn't need to remember to add it (it's in the instructions)
3. Consistent protocol across all tasks

## Implementation Notes

- `bin/start_task` passes filename via `--task-file-path` argument (not `--task-content`)
- `delegation_worker.py` sends only filename, not content
- Daemon logs record: `Starting task for delegatee <name> from file: <path>`
