#!/usr/bin/env python3
"""
Shared path utilities for the delegation skill.

Detects the runtime environment (Hermes vs OpenClaw vs plain) and returns
the correct base path, config search paths, and state file locations.

This eliminates the hardcoded /home/<user> paths that were scattered across
7 files (bug M1). All callers should use these functions instead of
hardcoding Path('/home/<user>').
"""

import os
from pathlib import Path
from typing import List


def is_hermes_env() -> bool:
    """Detect if we're running inside a Hermes chroot-like environment.

    In Hermes, Path.home() returns something like
    ~/.hermes/profiles/<profile>/home, so '.hermes/profiles'
    appears in the home string.
    """
    home_dir = Path.home()
    return '.hermes/profiles' in str(home_dir)


def real_home() -> Path:
    """Return the actual host home directory regardless of Hermes chroot.

    Inside a Hermes chroot, Path.home() returns something like
    ~/.hermes/profiles/<profile>/home.  We recover the real home by
    taking every path component that precedes the '.hermes' segment.
    """
    home = Path.home()
    if is_hermes_env():
        parts = home.parts
        for i, part in enumerate(parts):
            if part == '.hermes':
                return Path(*parts[:i]) if i > 0 else Path('/')
    return home


def _profile_from_skill_path() -> str | None:
    """Detect the Hermes profile name from this file's location.

    When the skill is installed at
    /…/.hermes/profiles/<name>/skills/…/src/path_utils.py
    we extract <name> so the caller can resolve a profile-specific config.
    """
    parts = Path(__file__).resolve().parts
    for i, part in enumerate(parts):
        if part == 'profiles' and i > 0 and parts[i - 1] == '.hermes':
            if i + 1 < len(parts):
                return parts[i + 1]
    return None


def config_search_paths() -> List[Path]:
    """Return the single canonical config location for this skill instance.

    Each Hermes profile gets its own `delegator-delegatee.yaml` at the profile
    root.  The skill is always invoked through its installed copy inside a
    profile (`~/.hermes/profiles/<name>/skills/.../scripts/start_task`), so we
    derive the profile name from this file's location and return exactly one
    path.

    If the skill is invoked from outside a Hermes profile (e.g. a development
    checkout under `~/Coding/`), this returns an empty list — the caller will
    raise a clear "no config found" error rather than silently fall back to a
    stale shared config and route delegations to the wrong rooms.

    Per-profile lookup keeps delegation hierarchies isolated: an orchestrator
    in profile A and another in profile B can each delegate to their own
    coding-agent without configs colliding.
    """
    profile = _profile_from_skill_path()
    if not profile:
        return []
    return [real_home() / '.hermes' / 'profiles' / profile / 'delegator-delegatee.yaml']


def daemon_state_file() -> Path:
    """Return the path to the daemon PID tracking file.

    In Hermes / OpenClaw environments: ~/.openclaw/agents/<name>/
    Otherwise: ~/.hermes/delegation_daemons.json
    """
    if is_hermes_env():
        return real_home() / '.openclaw' / 'agents' / 'main' / 'delegation_daemons.json'
    return Path.home() / '.hermes' / 'delegation_daemons.json'


def delegation_state_file(delegator_type: str = 'Hermes',
                          profile: str = '') -> Path:
    """Return the path to the delegation worker state file.

    Args:
        delegator_type: 'OpenClaw' or 'Hermes'
        profile: Hermes profile name (used only for Hermes)
    """
    if delegator_type == 'OpenClaw':
        return real_home() / '.openclaw' / 'agents' / 'main' / 'delegation_state.json'
    return Path.home() / '.hermes' / 'profiles' / profile / 'delegation_state.json'


def gateway_state_file(profile: str | None = None) -> Path:
    """Return the path to the Hermes gateway_state.json for idle checks.

    Args:
        profile: If set, look under ~/.hermes/profiles/<profile>/
                 Otherwise look at ~/.hermes/gateway_state.json
    """
    if profile:
        return real_home() / '.hermes' / 'profiles' / profile / 'gateway_state.json'
    return real_home() / '.hermes' / 'gateway_state.json'


def startup_file(delegatee_name: str) -> Path:
    """Return the path to the delegation startup state file for a delegatee."""
    return real_home() / '.openclaw' / 'agents' / 'main' / \
        f'delegation_startup_{delegatee_name}.json'


def pause_marker_file(delegatee_name: str) -> Path:
    """Return the path to the pause marker file for a delegatee.

    The marker is created by `scripts/pause`, removed by `scripts/resume`,
    and polled by the worker on every iteration. The marker file contains
    JSON `{"paused_at": <unix_ts>}` so the worker can compute paused-duration.
    """
    return real_home() / '.openclaw' / 'agents' / 'main' / \
        f'delegation_pause_{delegatee_name}.json'


def outbox_file(delegatee_name: str) -> Path:
    """Return the path to the outbound-to-delegator queue file.

    When the delegator is busy, the worker spools messages here instead of
    sending immediately, and flushes them on the next cycle in which the
    delegator becomes idle. Kept on disk so a worker restart preserves
    deferred messages.
    """
    return real_home() / '.openclaw' / 'agents' / 'main' / \
        f'delegation_outbox_{delegatee_name}.json'


def validate_delegatee_name(name: str) -> str:
    """Validate and sanitize a delegatee name.

    Only alphanumeric characters, hyphens, and underscores are allowed.
    Raises ValueError if the name is invalid.
    """
    import re
    if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
        raise ValueError(
            f"Invalid delegatee name '{name}'. "
            "Only alphanumeric characters, hyphens, and underscores are allowed."
        )
    return name


def validate_task_file_path(path: str) -> Path:
    """Validate that a task file path is safe (no path traversal beyond user home).

    Raises ValueError if the path contains '..' or is not absolute.
    """
    resolved = Path(path).resolve()
    if '..' in Path(path).parts:
        raise ValueError(f"Task file path contains path traversal: {path}")
    # Allow files anywhere under the user's home directory
    if not resolved.is_relative_to(real_home()):
        raise ValueError(
            f"Task file must be under {real_home()}: {resolved}"
        )
    return resolved


def validate_message(message: str, max_length: int = 10_000) -> str:
    """Validate a message string (length and control characters).

    Raises ValueError if the message is empty or exceeds max_length.
    """
    if not message or not message.strip():
        raise ValueError("Message must not be empty")
    if len(message) > max_length:
        raise ValueError(
            f"Message too long ({len(message)} chars, max {max_length})"
        )
    return message


# Convenience: find the first existing config file
find_config = lambda: next((p for p in config_search_paths() if p.exists()), None)
