#!/usr/bin/env python3
"""
Verify delegation skill configuration and daemon readiness.

Run this to diagnose common setup issues before attempting delegations.

Every path is resolved at runtime (via path_utils and this file's own
location) rather than hard-coded to a specific profile / legacy layout, so
the checks reflect the environment the skill is actually installed in and
can't report a false "not found" while the skill works.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import path_utils  # noqa: E402

# <skill_root>/src/verify_delegation_setup.py -> <skill_root>
SKILL_ROOT = Path(__file__).resolve().parent.parent


def _load_config():
    for path in path_utils.config_search_paths():
        if path.exists():
            return path
    return None


def check_config_file():
    print("\n=== Config File Check ===")
    config_path = _load_config()
    if not config_path:
        searched = path_utils.config_search_paths()
        print("✗ No config file found for this skill instance.")
        for p in searched:
            print(f"  (checked) {p}")
        if not searched:
            print("  (skill running outside a Hermes profile)")
        return False
    print(f"✓ Found: {config_path}")
    try:
        import yaml
        with open(config_path) as f:
            config = yaml.safe_load(f)
        print(f"  - Delegator: {(config.get('delegator', {}) or {}).get('type', 'unknown')}")
        delegatees = config.get('delegatees', []) or []
        print(f"  - Delegatees: {len(delegatees)}")
        for d in delegatees:
            print(f"    • {d.get('name', 'unnamed')}: "
                  f"{(d.get('matrix', {}) or {}).get('room_id', 'no room_id')}")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"✗ Parse error: {e}")
        return False


def check_state_files():
    print("\n=== State Files Check ===")
    state_path = path_utils.daemon_state_file()
    if state_path.exists():
        print(f"✓ State file exists: {state_path}")
        try:
            with open(state_path) as f:
                state = json.load(f)
            if state:
                for name, info in state.items():
                    pid = info.get('pid')
                    try:
                        os.kill(pid, 0)
                        running = True
                    except OSError:
                        running = False
                    print(f"  • {name}: PID {pid} - {'RUNNING' if running else 'ZOMBIE (dead process)'}")
            else:
                print("  - No active delegations")
        except Exception as e:  # noqa: BLE001
            print(f"✗ Read error: {e}")
    else:
        print(f"✓ State file not found (no running delegations): {state_path}")
    return True


def check_daemon_scripts():
    print("\n=== Daemon Scripts Check ===")
    # Scripts live under <skill_root>/scripts/ (not bin/); the worker is in src/.
    start_task = SKILL_ROOT / 'scripts' / 'start_task'
    if start_task.exists():
        print(f"✓ Found: {start_task}")
        if os.access(start_task, os.X_OK):
            print("  - Executable")
        else:
            print("  ⚠ Not executable (run tools/chmod_scripts.sh)")
    else:
        print(f"✗ start_task not found: {start_task}")
        return False

    worker = SKILL_ROOT / 'src' / 'delegation_worker.py'
    if worker.exists():
        print(f"✓ Worker script: {worker}")
        return True
    print(f"✗ Worker script not found: {worker}")
    return False


def check_matrix_connection():
    print("\n=== Matrix Connection Check ===")
    config_path = _load_config()
    if not config_path:
        print("⚠ Config not found, skipping Matrix check")
        return True
    try:
        import yaml
        import urllib.request
        import urllib.error
        with open(config_path) as f:
            config = yaml.safe_load(f)
        matrix = config.get('matrix', {}) or {}
        matrix_url = (matrix.get('url', '') or '').rstrip('/')
        access_token = matrix.get('access_token', '')
        user_id = matrix.get('user_id', '')
        if not matrix_url or not access_token:
            print("⚠ Matrix credentials missing in config")
            return False
        # whoami both proves reachability AND that the token maps to user_id.
        url = f"{matrix_url}/_matrix/client/v3/account/whoami"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            actual = json.loads(resp.read()).get("user_id")
        print(f"✓ Matrix server reachable: {matrix_url}")
        if user_id and actual != user_id:
            print(f"✗ access_token belongs to '{actual}', not configured user_id '{user_id}'")
            return False
        print(f"✓ access_token verified for {actual}")
        return True
    except urllib.error.HTTPError as e:
        print(f"✗ HTTP error: {e.code} - {e.read().decode()[:200]}")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"✗ Connection failed: {e}")
        return False


def check_python_environment():
    print("\n=== Python Environment Check ===")
    print(f"✓ Python: {sys.executable} ({sys.version.split()[0]})")
    missing = []
    for module in ('yaml', 'aiohttp'):
        try:
            __import__(module)
            print(f"  ✓ {module}")
        except ImportError:
            print(f"  ✗ {module} (missing)")
            missing.append(module)
    if missing:
        print(f"\n⚠ Install missing modules: pip install {' '.join(missing)}")
        return False
    return True


def main():
    print("=" * 60)
    print("Agent Delegation Skill - Configuration Verification")
    print("=" * 60)

    results = [
        ("Config File", check_config_file()),
        ("State Files", check_state_files()),
        ("Daemon Scripts", check_daemon_scripts()),
        ("Python Environment", check_python_environment()),
        ("Matrix Connection", check_matrix_connection()),
    ]

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    all_passed = True
    for name, passed in results:
        print(f"{'✓ PASS' if passed else '✗ FAIL'}: {name}")
        all_passed = all_passed and passed

    if all_passed:
        print("\n✓ All checks passed. Delegation skill is ready.")
        return 0
    print("\n⚠ Some checks failed. Review errors above and fix before using delegation.")
    return 1


if __name__ == '__main__':
    sys.exit(main())
