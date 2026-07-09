# delegator-delegatee Reference Documentation

This directory contains detailed reference material for the delegator-delegatee skill.

## Changelog & Updates

- **changes-2026-05-01-fixes.md** - Detailed technical documentation of v2.3.0 bug fixes (old messages, sender filtering)
- **skill-update-2026-05-01.md** - Skill library update summary with lessons learned

## Troubleshooting Guides

- **file-editing-pitfalls.md** - How to avoid and recover from execute_code line number corruption
- **message-truncation.md** - Details on UTF-8 byte truncation for message size limits
- **sender-id-mismatch-fix.md** - Handling bot relay scenarios where sender differs from configured user_id

## Operational Documentation

- **delegation-daemon-operations.md** - Daemon lifecycle, startup, and management
- **delegation-daemon-troubleshooting.md** - Common daemon issues and recovery procedures
- **delegation-debugging-patterns.md** - Debug logging strategies and patterns

## Protocol & Format

- **task-message-format.md** - Task message structure and protocol markers

## Quick Reference

| Topic | File |
|-------|------|
| Bug fixes (v2.3.0) | changes-2026-05-01-fixes.md |
| execute_code pitfalls | file-editing-pitfalls.md |
| UTF-8 truncation | message-truncation.md |
| Sender ID issues | sender-id-mismatch-fix.md |
