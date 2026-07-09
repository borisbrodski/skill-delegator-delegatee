# Sender ID Mismatch Fix (2026-05-01)

## Problem Summary

Delegation daemon finalizes immediately (< 1 second) instead of waiting for delegatee response. The completion marker `=== NO_MORE_ACTIONS ===` in the task message triggers false positive because the sender filter doesn't work correctly.

## Root Cause Analysis

### Initial Assumption (WRONG)
The delegator config specifies:
```yaml
matrix:
  user_id: "@coding-agent:example.com"
```

We assumed messages sent by `send_message()` would have `sender == "@coding-agent:example.com"`.

### Reality
All Matrix messages in the room had `sender == "@delegator:example.com"`, including:
- Task messages sent by delegator daemon
- Progress updates
- Completion handoffs

This indicates the Matrix bot/relay service sends as a different user ID than what's configured.

### Why Simple Filter Failed
```python
# This code never filtered anything!
my_user_id = self.config['matrix']['user_id']  # "@coding-agent:example.com"

for msg in messages:
    sender = msg.get('sender', '')  # Always "@delegator:example.com"
    
    if sender == my_user_id:  # "@delegator:example.com" == "@coding-agent:example.com"? FALSE!
        continue  # Never executed!
```

Result: Task message (containing "New task file... When no more tool calls are issued, call delegator attention by adding `=== NO_MORE_ACTIONS ===`") was processed as if it came from delegatee, triggering immediate completion detection.

## Solution: Dynamic Sender Detection

### Step 1: Track Actual Sender in State
```python
class DelegationState:
    def __init__(self, delegatee_name: str, delegatee_room_id: str, delegator_room_id: str):
        # ... existing fields ...
        # Track the user ID that sent the task (to distinguish from delegatee responses)
        self.task_sender_user_id: Optional[str] = None
```

### Step 2: Fetch Sender After Sending Task Message
```python
async def start_task(self, task_file_path: str) -> bool:
    # ... compose task_message ...
    
    # Send task message and capture event_id
    send_result = await self.client.send_message(self.state.delegatee_room_id, task_message)
    task_event_id = send_result.get('event_id')
    
    # Fetch the actual sender of this message (may differ from configured user_id due to bot relay)
    if task_event_id:
        import aiohttp
        url = f"{self.client.url}/_matrix/client/r0/rooms/{self.state.delegatee_room_id}/event/{task_event_id}"
        headers = {"Authorization": f"Bearer {self.client.access_token}"}
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    event_data = await resp.json()
                    actual_sender = event_data.get('sender')
                    self.state.task_sender_user_id = actual_sender
                    self.logger.info(f"Task message {task_event_id} sent by: {actual_sender}")
```

### Step 3: Filter Using Detected Sender
```python
async def check_progress(self) -> Optional[str]:
    # ... fetch messages ...
    
    my_user_id = self.config['matrix']['user_id']
    task_sender = self.state.task_sender_user_id or my_user_id  # Use detected sender, fallback to config
    
    for msg in messages:
        event_id = msg.get('event_id', '')
        sender = msg.get('sender', '')
        
        # Skip if we've already processed this message
        if event_id in self.state.processed_event_ids:
            continue
        
        # Skip messages sent by the task sender (delegator) to avoid false completion detection
        if sender == task_sender:
            self.state.processed_event_ids.add(event_id)
            self.logger.debug(f"Skipping message from task sender ({task_sender}): {event_id}")
            continue
        
        # Process delegatee messages...
```

## Debugging Approach

### 1. Check Actual Sender IDs in Logs
```bash
# Look for "Processing message from" logs to see actual senders
grep "Processing message from" ~/.openclaw/agents/main/delegator-delegatee-*.log
```

Expected output before fix:
```
INFO Processing message from @delegator:example.com: New task file: /path/to/task.md...
WARNING Completion marker detected in message from @delegator:example.com  # FALSE POSITIVE!
```

Expected output after fix:
```
INFO Task message $event_id sent by: @delegator:example.com
DEBUG Skipping message from task sender (@delegator:example.com): $event_id
# ... delegatee sends message later ...
INFO Processing message from @delegatee-user:example.com: Working on the task...
```

### 2. Verify Event Sender via Matrix API
```bash
# Manually fetch event to verify sender
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "https://matrix.example.com/_matrix/client/r0/rooms/!room_id/event/$event_id" | jq '.sender'
```

### 3. Check for Bot Relay Configuration
If all messages come from a single user (like `@delegator:example.com`), you likely have:
- A Matrix bot that relays messages
- A service account that posts on behalf of multiple users
- Webhook integration that normalizes senders

This is NORMAL - just means you must detect the actual sender dynamically.

## Files Modified

### `~/.hermes/profiles/<profile>/skills/delegator-delegatee/scripts/delegation_worker.py`

1. **DelegationState.__init__()**: Added `task_sender_user_id: Optional[str] = None`
2. **start_task()**: Added code to fetch and store actual sender after sending task message
3. **check_progress()**: Changed from `if sender == my_user_id` to `if sender == task_sender`

## Testing Verification

After applying fix, run a test delegation:

```bash
# Start a simple task
bin/start_task coding-agent tasks/test-task.md

# Watch daemon logs immediately
tail -f ~/.openclaw/agents/main/delegator-delegatee-$(jq -r '.coding-agent.pid' ~/.openclaw/agents/main/delegation_daemons.json).log
```

Expected sequence:
1. "Task started for coding-agent"
2. "Task message $event_id sent by: @delegator:example.com" (or whatever actual sender is)
3. Daemon continues polling, NOT finalizing
4. Only when delegatee sends `=== NO_MORE_ACTIONS ===` does it finalize

## Related Issues

- **Message deduplication**: Also added `processed_event_ids: set` to prevent reprocessing same messages across polling cycles
- **Separate rooms**: If delegator and delegatee use different Matrix rooms, this issue doesn't occur (messages are naturally separated by room)
- **Bot architecture**: Understanding your Matrix server's bot/relay configuration helps diagnose sender ID issues

## Prevention

When setting up new delegation configurations:

1. **Test message sending first**: Send a test message and verify its sender via Matrix API
2. **Don't assume user_id matches sender**: Always fetch actual sender from event if filtering is required
3. **Use separate rooms when possible**: Eliminates need for sender filtering entirely
4. **Add debug logging early**: Log sender IDs on first few messages to catch mismatches

## References

- Matrix API: `GET /rooms/{roomId}/event/{eventId}` - Fetch single event with full metadata including sender
- Bot relay patterns: Common in enterprise Matrix setups where a central bot posts on behalf of multiple services
