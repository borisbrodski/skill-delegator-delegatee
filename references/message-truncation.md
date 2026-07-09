# Message Truncation (2026-05-01)

## Overview

Progress updates sent from delegatee to delegator are truncated to prevent excessively long Matrix messages. This is particularly important when the delegatee produces verbose output or logs.

## Configuration

The truncation limit can be configured in `delegator-delegatee.yaml`:

```yaml
# Message truncation settings (optional)
message_limits:
  max_update_bytes: 1024  # Default: 1KB per progress update
  max_final_summary_bytes: 4096  # Optional: limit for final handoff message
```

## Implementation Details

### `_truncate_to_bytes()` Method

Located in `DelegationWorker` class (`delegation_worker.py`):

```python
def _truncate_to_bytes(self, text: str, max_bytes: int) -> str:
    """Truncate text to fit within max_bytes (UTF-8 encoded)."""
    # Encode to bytes to count actual byte size
    encoded = text.encode('utf-8')
    
    if len(encoded) <= max_bytes:
        return text
    
    # Truncate and decode (may cut in middle of multi-byte char)
    truncated = encoded[:max_bytes]
    
    # Try to decode, handling potential partial UTF-8 sequence
    try:
        return truncated.decode('utf-8') + "..."
    except UnicodeDecodeError:
        # If we cut in middle of multi-byte char, back up one byte
        while len(truncated) > 0:
            try:
                return truncated.decode('utf-8') + "..."
            except UnicodeDecodeError:
                truncated = truncated[:-1]
        return "...[truncated]"
```

### Where It's Applied

1. **Progress Updates**: In `_compose_progress_summary()`, each message is truncated individually
2. **Default Limit**: 1024 bytes (1KB) per update
3. **Applied To**: `messages[-5:]` - last 5 messages in summary

## UTF-8 Safety

The truncation handles multi-byte characters correctly:
- Encodes text to UTF-8 bytes before truncating
- Detects and handles partial UTF-8 sequences at truncation point
- Backs up byte-by-byte if needed to find valid character boundary
- Appends "..." to indicate truncation occurred

## Example

**Input**: 5KB message with German umlauts (ä, ö, ü = 2 bytes each)
**Output**: ~1KB message ending with "...", properly decoded without corruption

```
[2026-05-01T10:30:00] Starting OAuth2 implementation...
[2026-05-01T10:30:05] Created login endpoint (truncated...)
```

## Future Enhancements

Potential improvements:
- Configurable per-delegatee limits
- Smart truncation at line boundaries
- Compression for very long messages
- Option to send full message on request
