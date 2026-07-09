"""
Agent Delegation Skill - Hierarchical agent delegation via Matrix.

Use bash scripts in `scripts/` for most operations:

    scripts/start_task coding-agent tasks/my-task.md 600
    scripts/correct coding-agent "Focus on database first"
    scripts/ping coding-agent
    scripts/stop coding-agent
    scripts/list_delegatees
"""

import subprocess
import sys
from pathlib import Path
from typing import List, Dict, Optional


def _get_bin_path() -> Path:
    """Get path to bin directory."""
    return Path(__file__).parent / 'bin'


def start_task(
    delegatee_name: str,
    task_file: str,
    timeout_sec: int = None,
    reset_session: bool = True,
) -> bool:
    """
    Start a new delegation task for a configured delegatee.

    Args:
        delegatee_name: Name of configured delegatee (e.g., "coding-agent")
        task_file: Path to task description file (.md)
        timeout_sec: Optional idle timeout override
        reset_session: HU2/HU6 — send a "/reset" to the delegatee's Matrix room
            before the new task so it starts with a fresh Hermes session (no
            cross-task context bloat → no mid-task compressions, no T-number
            drift). Default True (ON). Set False to PRESERVE the session — use
            when answering a question the delegatee asked or continuing an
            interactive exchange. Idempotent: safe even if no live worker exists.

    Returns:
        True if task started successfully, False otherwise

    Example:
        >>> start_task("coding-agent", "tasks/oauth2-auth.md", 600)
        True
        >>> start_task("coding-agent", "tasks/answer.md", reset_session=False)
        True
    """
    cmd = [_str(_get_bin_path() / 'start_task'), delegatee_name, task_file]
    if timeout_sec:
        cmd.append(str(timeout_sec))
    cmd.append('--reset-session' if reset_session else '--no-reset-session')

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error starting task: {result.stderr}", file=sys.stderr)
        return False
    print(result.stdout)
    return True


def correct(delegatee_name: str, message: str):
    """
    Issue a correcting message to the delegatee.
    
    Args:
        delegatee_name: Name of configured delegatee
        message: The correction text
        
    Example:
        >>> correct("coding-agent", "Focus on database migration first")
    """
    cmd = [_str(_get_bin_path() / 'correct'), delegatee_name, message]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error sending correction: {result.stderr}", file=sys.stderr)
        return False
    print(result.stdout)


def stop(delegatee_name: str):
    """
    Stop a currently running delegation for the delegatee.
    
    Args:
        delegatee_name: Name of configured delegatee
        
    Example:
        >>> stop("coding-agent")
    """
    cmd = [_str(_get_bin_path() / 'stop'), delegatee_name]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error stopping delegation: {result.stderr}", file=sys.stderr)
        return False
    print(result.stdout)


def ping(delegatee_name: str):
    """
    Send "continue" prompt to delegatee that fell out of action loop.
    
    Args:
        delegatee_name: Name of configured delegatee
        
    Example:
        >>> ping("coding-agent")
    """
    cmd = [_str(_get_bin_path() / 'ping'), delegatee_name]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error sending ping: {result.stderr}", file=sys.stderr)
        return False
    print(result.stdout)


def list_delegatees() -> List[Dict]:
    """
    List all configured delegatees with their status.
    
    Returns:
        List of dicts with 'name', 'description', 'type', and 'status' keys
        
    Example:
        >>> for d in list_delegatees():
        ...     print(f"{d['name']}: {d['status']}")
        coding-agent: BUSY
        testing-agent: IDLE
    """
    cmd = [_str(_get_bin_path() / 'list_delegatees')]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Error listing delegatees: {result.stderr}", file=sys.stderr)
        return []
    
    # Parse output (skip header lines)
    delegatees = []
    for line in result.stdout.strip().split('\n'):
        parts = line.split()
        if len(parts) >= 4 and parts[0] not in ['Name', '---']:
            delegatees.append({
                'name': parts[0],
                'description': parts[1] if len(parts) > 1 else '',
                'type': parts[2] if len(parts) > 2 else '',
                'status': parts[3] if len(parts) > 3 else ''
            })
    
    return delegatees


def _str(path: Path) -> str:
    """Convert path to string."""
    return str(path)


# Export all public functions
__all__ = [
    'start_task',
    'correct', 
    'stop',
    'ping',
    'list_delegatees',
]
