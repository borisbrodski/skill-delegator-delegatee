# File Editing Pitfalls in Delegation Worker

## Problem: execute_code Corrupts Files with Line Numbers

When using `execute_code` tool to read Python files, the output includes line number prefixes like:
```
     1|#!/usr/bin/env python3
     2|"""
```

**Impact**: If you use string matching on this output for patches or replacements, the patterns will fail because they include the line numbers.

**Solution**: Use direct file I/O in Python instead of relying on execute_code's formatted output:

```python
# CORRECT - Direct file I/O
with open('/path/to/file.py', 'r') as f:
    content = f.read()

# String replacement works normally
content = content.replace(old_text, new_text)

with open('/path/to/file.py', 'w') as f:
    f.write(content)
```

## Recovery from Corruption

If a file gets corrupted with line number prefixes:

1. **Restore from git**: `git checkout scripts/delegation_worker.py`
2. **Clean corrupted lines** (if needed):
   ```python
   cleaned_lines = []
   for line in lines:
       # Skip lines matching pattern "     N|..."
       if not re.match(r'\s+\d+\|', line):
           cleaned_lines.append(line)
   ```

## Best Practices

1. **Always verify file integrity** after edits:
   ```python
   with open('file.py') as f:
       lines = f.readlines()
   print(f"File has {len(lines)} lines, first line: {lines[0][:50]}")
   ```

2. **Use git diff** to verify changes before committing:
   ```bash
   git diff scripts/delegation_worker.py
   ```

3. **Prefer skill_manage patch/edit** for skill files when possible - they handle version tracking automatically.

4. **Test imports** after Python file edits:
   ```python
   import sys
   sys.path.insert(0, '/path/to/skill')
   try:
       import delegation_worker
       print("✓ Import successful")
   except Exception as e:
       print(f"✗ Import failed: {e}")
   ```

## Related Issues

- Session 2026-05-01: File corruption during message filtering fixes
- Recovery method: Git restore + clean corrupted lines (566 lines removed)
- Final state: 629 lines, all fixes applied correctly
