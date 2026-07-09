# Configuration Troubleshooting Reference

## Error: "No delegator-delegatee.yaml found"

### Symptoms
```bash
$ bin/list_delegatees
Error: No delegator-delegatee.yaml found
```

### Root Cause
When running from symlinked skill locations in Hermes profiles, `Path.home()` returns the profile-specific path (`~/.hermes/profiles/<profile>/home`) instead of the actual home directory (`~`). The config file may exist at `~/.openclaw/agents/main/delegator-delegatee.yaml` but isn't found.

### Solution
The skill's `bin/delegation_core.py` automatically detects Hermes environment by checking for `.hermes/profiles` in the home path and checks both locations:
1. Paths relative to `Path.home()` (Hermes profile paths)
2. Paths relative to `~` (actual home directory)

If adding custom config locations, maintain this dual-check pattern:

```python
home_dir = Path.home()
real_home = Path('~')
is_hermes_env = '.hermes/profiles' in str(home_dir)

config_paths = [
    home_dir / '.openclaw' / 'agents' / 'main' / 'delegator-delegatee.yaml',
]

if is_hermes_env:
    config_paths.extend([
        real_home / '.openclaw' / 'agents' / 'main' / 'delegator-delegatee.yaml',
    ])
```

## Error: YAML Scanner - "found character '@' that cannot start any token"

### Symptoms
```python
yaml.scanner.ScannerError: while scanning for the next token
found character '@' that cannot start any token
  in "~/.openclaw/agents/main/delegator-delegatee.yaml", line 4, column 12
```

### Root Cause
Matrix identifiers contain colons (`:`) which are special YAML characters. Unquoted values like `user_id: @delegator:example.com` cause parser errors because the colon is interpreted as a mapping indicator.

### Solution
Always quote Matrix identifiers and tokens containing special characters:

```yaml
# WRONG
matrix:
  user_id: @delegator:example.com
  access_token: YOUR_ACCESS_TOKEN_HERE
  room_id: "!orchestrator-room:example.com"

# CORRECT
matrix:
  user_id: "@delegator:example.com"
  access_token: "YOUR_ACCESS_TOKEN_HERE"
  room_id: "!orchestrator-room:example.com"
```

**Always quote these fields:**
- `user_id`: Contains `@` and `:` (e.g., `@user:server`)
- `access_token`: May contain special characters
- `room_id`: Contains `!` and `:` (e.g., `!roomid:server`)

## Config Search Path Order

The skill checks these locations in order (first match wins):

1. `~/.hermes/delegator-delegatee.yaml`
2. `~/.hermes/profiles/<profile>/delegator-delegatee.yaml`
3. `~/.openclaw/delegator-delegatee.yaml`
4. `~/.openclaw/agents/main/delegator-delegatee.yaml`
5. `~/.hermes/delegator-delegatee.yaml` (Hermes fallback)
6. `~/.hermes/profiles/<profile>/delegator-delegatee.yaml` (Hermes fallback)
7. `~/.openclaw/delegator-delegatee.yaml` (Hermes fallback)
8. `~/.openclaw/agents/main/delegator-delegatee.yaml` (Hermes fallback)

## Verification Commands

```bash
# Check if config file exists at expected location
ls -la ~/.openclaw/agents/main/delegator-delegatee.yaml

# Validate YAML syntax
python3 -c "import yaml; yaml.safe_load(open('~/.openclaw/agents/main/delegator-delegatee.yaml'))"

# Test delegation scripts
bin/list_delegatees

# Check Python home detection (debugging)
python3 -c "from pathlib import Path; print('HOME:', Path.home())"
```
