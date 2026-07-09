# Testing Patterns for Agent Delegation

## Running Tests

```bash
cd ~/.hermes/profiles/<profile>/skills/delegator-delegatee
python tests/test_delegation.py          # Built-in runner (58+ tests)
python -m pytest tests/test_delegation.py  # Pytest runner with verbose output
```

### Registering New Tests (CRITICAL)

Tests are **NOT auto-discovered** by the built-in runner. After writing a test function, you MUST add it to the `test_functions` list at the bottom of `tests/test_delegation.py`:

```python
if __name__ == '__main__':
    test_functions = [
        # ... existing tests ...
        test_my_new_test,  # <-- Must add here!
    ]
```

If you forget this step, `python tests/test_delegation.py` will silently skip your new test. This caused a missed verification when race condition fixes were tested but not registered.

## Async Polling Loop: Post-Completion Race Condition Guard Pattern

When fixing async race conditions (e.g., periodic report sent after task finalization), use **two-layer guards**:

1. **Call-site guard** — in `run()` loop: check state before calling the method
2. **Internal guard** — inside the method itself: early return if precondition violated

Testing pattern for each layer:

```python
# Test 1: Internal guard (method-level) — verify early return
async def test_guard_internal():
    worker.state.polling_active = False  # finalized
    await worker._send_periodic_progress_report()
    mock_client.send_message.assert_not_called()  # did NOT send

# Test 2: Call-site guard + integration — simulate full loop iteration
async def test_no_extra_send_after_finalization():
    result = await worker.check_progress()  # detects completion, finalizes
    assert state.polling_active is False
    
    poll_counter = 100  # at threshold
    if poll_counter >= report_cycles and worker.state and worker.state.polling_active:
        await worker._send_periodic_progress_report()  # guarded — won't run
    
    assert mock_client.send_message.call_count == 1  # handoff only, no periodic

# Test 3: Regression — verify normal operation unaffected
async def test_guard_does_not_block_normal():
    state.polling_active = True  # still active
    await worker._send_periodic_progress_report()
    mock_client.send_message.assert_called_once()  # DID send normally
```

This three-test pattern (internal guard, integration with call-site guard, regression) covers all failure modes for defense-in-depth fixes.

## Deduplication Testing

When testing message deduplication (editing messages in Matrix), verify:
1. Latest occurrence of duplicate body is kept
2. Order preservation (FIFO within uniqueness constraint)
3. All unique bodies pass through unchanged
4. Empty and None bodies are handled gracefully

See `test_progress_summary_dedup_basic`, `test_progress_summary_dedup_latest_wins`, etc. for examples.

## Completion Marker Detection

When `check_progress` finds `=== NO_MORE_ACTIONS ===` in a message body, it calls `_finalize_delegation(summary)`:
1. Composes completion summary with all collected messages
2. Sends handoff message to delegator room
3. Sets `state.polling_active = False`

Test by sending a completion marker and asserting `polling_active is False`.
