# Matrix URL Configuration Pitfalls

## Error: 404 - "Unrecognized request" (M_UNRECOGNIZED)

### Symptoms
```bash
$ bin/send_message --target delegator --message "test"
Matrix API error: 404 - {"errcode":"M_UNRECOGNIZED","error":"Unrecognized request"}
Failed to send message
```

### Root Cause
The Matrix server URL in configuration has a **trailing slash**, which creates double slashes in the API path:

**Wrong:** `https://matrix.example.com/`  
**Resulting API call:** `https://matrix.example.com//_matrix/client/r0/rooms/...` (invalid)

The trailing slash causes the URL concatenation to produce `//_matrix/` instead of `/_matrix/`.

### Solution
Strip trailing slashes from the Matrix URL in configuration. The code automatically handles this with `.rstrip('/')`, but it's best practice to configure URLs correctly:

```yaml
# WRONG - has trailing slash
matrix:
  url: https://matrix.example.com/

# CORRECT - no trailing slash  
matrix:
  url: https://matrix.example.com
```

### Verification
Test the URL format before running delegation commands:

```bash
python3 << 'EOF'
import yaml
config = yaml.safe_load(open('~/.openclaw/agents/main/delegator-delegatee.yaml'))
url = config['matrix']['url']
print(f"URL ends with slash: {url.endswith('/')}")
print(f"Normalized URL: {url.rstrip('/')}/_matrix/client/r0/version")
EOF
```

### Related Issues
- Double slashes in URLs cause 404 errors on most Matrix servers
- Some Synapse homeservers are lenient, but others (like custom ports) reject malformed paths
- Always normalize URLs before concatenation: `url.rstrip('/') + '/path'`

## API Version Compatibility

The skill uses the stable r0 API path (`/_matrix/client/r0/`). If you encounter 404 errors with a valid URL format, try these alternatives in order:

1. `/_matrix/client/r0/...` (default, most compatible)
2. `/_matrix/client/v3/...` (newer spec)
3. `/_matrix/client/unstable/...` (legacy fallback)

Most Synapse servers support r0 without issues.
