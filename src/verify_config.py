#!/usr/bin/env python3
"""
Verify delegator-delegatee configuration.

Checks:
- Config file discovered via the SAME runtime resolver the skill uses
  (path_utils.config_search_paths) — never hard-coded profile/legacy paths,
  so this can't give a false "not found" while the skill itself works.
- YAML syntax validity
- Matrix URL format (no trailing slash)
- Required fields present
- (optional) whoami: the access_token really belongs to the configured
  user_id on the configured homeserver.

Usage:
    src/verify_config.py [--config PATH] [--no-whoami]
"""

import sys
from pathlib import Path

# Resolve imports relative to this file so `path_utils` is found regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import path_utils  # noqa: E402


def find_config():
    """Return the config path using the skill's own runtime resolver.

    path_utils.config_search_paths() derives the per-profile config location
    from this file's install path, so it matches wherever the skill is actually
    running instead of guessing at legacy/hard-coded locations.
    """
    for path in path_utils.config_search_paths():
        if path.exists():
            return path
    return None


def _whoami(matrix_url, access_token):
    """Return the user_id the homeserver associates with access_token, or None."""
    import json
    import urllib.request
    import urllib.error
    url = f"{matrix_url.rstrip('/')}/_matrix/client/v3/account/whoami"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()).get("user_id")
    except Exception as e:  # noqa: BLE001 — diagnostic tool, report and continue
        print(f"  ⚠ whoami call failed: {e}")
        return None


def validate_config(config_path, do_whoami=True):
    """Validate configuration file. Returns (config, errors, warnings)."""
    import yaml

    errors = []
    warnings = []

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f)
        print("✓ YAML syntax valid")
    except yaml.YAMLError as e:
        errors.append(f"YAML syntax error: {e}")
        return None, errors, warnings

    for section in ('matrix', 'delegator', 'delegatees'):
        if section not in config:
            errors.append(f"Missing required section: '{section}'")

    matrix = config.get('matrix', {}) or {}
    matrix_url = matrix.get('url', '')
    if matrix_url.endswith('/'):
        errors.append(f"Matrix URL has trailing slash (causes 404 errors): {matrix_url}")
        errors.append(f"  Fix: {matrix_url.rstrip('/')}")
    elif matrix_url:
        print("✓ Matrix URL format correct")

    for field in ('url', 'user_id', 'access_token'):
        if field not in matrix:
            errors.append(f"Missing Matrix field: '{field}'")

    for field in ('name', 'type', 'profile', 'matrix'):
        if field not in (config.get('delegator', {}) or {}):
            errors.append(f"Missing delegator field: '{field}'")

    delegatees = config.get('delegatees', []) or []
    if not delegatees:
        warnings.append("No delegatees configured")
    else:
        print(f"✓ Found {len(delegatees)} configured delegatee(s)")
        for d in delegatees:
            print(f"  - {d.get('name', 'unnamed')}: "
                  f"{(d.get('matrix', {}) or {}).get('room_id', 'unknown')}")

    # whoami: prove the token maps to the configured user_id (catches the common
    # "wrong/expired token" and "copied someone else's user_id" mistakes that a
    # pure static check silently passes).
    token = matrix.get('access_token', '')
    user_id = matrix.get('user_id', '')
    if do_whoami and matrix_url and token and token != 'YOUR_ACCESS_TOKEN_HERE':
        actual = _whoami(matrix_url, token)
        if actual is None:
            warnings.append("Could not verify access_token via /whoami (see message above)")
        elif user_id and actual != user_id:
            errors.append(f"access_token belongs to '{actual}', not configured user_id '{user_id}'")
        else:
            print(f"✓ access_token verified for {actual}")

    return config, errors, warnings


def main():
    import argparse

    parser = argparse.ArgumentParser(description='Verify delegator-delegatee configuration')
    parser.add_argument('--config', '-c', help='Path to config file (auto-detect if not specified)')
    parser.add_argument('--no-whoami', action='store_true', help='Skip the live token /whoami check')
    args = parser.parse_args()

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"ERROR: Config file not found: {config_path}")
            sys.exit(1)
    else:
        config_path = find_config()
        if not config_path:
            print("ERROR: No delegator-delegatee.yaml found for this skill instance.")
            print("\nResolver checked:")
            searched = path_utils.config_search_paths()
            if searched:
                for p in searched:
                    print(f"  {p}")
            else:
                print("  (skill is running outside a Hermes profile — pass --config explicitly)")
            sys.exit(1)

    print(f"\nValidating: {config_path}\n")

    config, errors, warnings = validate_config(config_path, do_whoami=not args.no_whoami)

    for w in warnings:
        print(f"⚠ WARNING: {w}")

    if errors:
        print("\n❌ Configuration has errors:")
        for e in errors:
            print(f"  ✗ {e}")
        sys.exit(1)

    if not warnings:
        print("\n✅ Configuration is valid!")
    sys.exit(0)


if __name__ == '__main__':
    main()
