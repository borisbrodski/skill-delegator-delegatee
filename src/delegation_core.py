#!/usr/bin/env python3
"""
Core delegation library - loaded by bash scripts.

Provides configuration loading, daemon management, and Matrix communication.
"""

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, List, Tuple
from urllib.parse import quote
try:
    from .matrix_client import MatrixClient
    from . import path_utils
except ImportError:
    import os, sys
    _src = os.path.dirname(os.path.abspath(__file__))
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from matrix_client import MatrixClient
    import path_utils
import yaml


class DelegationConfig:
    """Manages delegation configuration."""
    
    def __init__(self):
        self.config = None
        self.config_path = None
        
    def load(self) -> dict:
        """Load configuration from YAML file."""
        if self.config is not None:
            return self.config

        for path in path_utils.config_search_paths():
            if path.exists():
                self.config_path = path
                with open(path, 'r') as f:
                    self.config = yaml.safe_load(f)
                return self.config

        paths = path_utils.config_search_paths()
        example = Path(__file__).parent.parent / 'config.example.yaml'
        if not paths:
            raise FileNotFoundError(
                "delegator-delegatee skill invoked from outside a Hermes profile.\n\n"
                "The skill must be called via its installed copy at\n"
                "  /home/<user>/.hermes/profiles/<profile>/skills/autonomous-ai-agents/delegator-delegatee/scripts/\n"
                "so the profile name can be derived and the matching\n"
                "  /home/<user>/.hermes/profiles/<profile>/delegator-delegatee.yaml\n"
                "config can be loaded.\n\n"
                f"Template for the user: {example}"
            )
        searched = "\n".join(f"  {p}" for p in paths)
        raise FileNotFoundError(
            f"delegator-delegatee.yaml not found. Report this to the user — "
            f"the config must be created by a human before this skill can be used.\n\n"
            f"Searched:\n{searched}\n\n"
            f"Template for the user: {example}"
        )
    
    def get_delegatee(self, name: str) -> Optional[dict]:
        """Get delegatee configuration by name."""
        config = self.load()
        
        for delegatee in config.get('delegatees', []):
            if delegatee['name'] == name:
                return delegatee
                
        return None
    
    def get_all_delegatees(self) -> List[dict]:
        """Get all configured delegatees."""
        config = self.load()
        return config.get('delegatees', [])


class DaemonManager:
    """Manages daemon processes per delegatee."""
    
    def __init__(self):
        self.state_file = self._get_state_file_path()
        
    def _get_state_file_path(self) -> Path:
        """Get path to daemon state file."""
        return path_utils.daemon_state_file()
    
    def _load_state(self) -> dict:
        """Load daemon state from file with shared lock."""
        try:
            with open(self.state_file, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    return json.load(f)
                except json.JSONDecodeError:
                    return {}
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except FileNotFoundError:
            return {}
    
    def _save_state(self, state: dict):
        """Save daemon state to file with exclusive lock."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    
    def get_daemon_pid(self, delegatee_name: str) -> Optional[int]:
        """Get PID of daemon for delegatee."""
        state = self._load_state()
        return state.get(delegatee_name, {}).get('pid')
    
    def is_daemon_running(self, delegatee_name: str) -> bool:
        """Check if daemon is running for delegatee."""
        pid = self.get_daemon_pid(delegatee_name)
        if pid is None:
            return False
            
        try:
            os.kill(pid, 0)  # Check if process exists
            return True
        except OSError:
            return False
    
    def cleanup_zombie_daemons(self):
        """Clean up zombie/stale daemon processes."""
        state = self._load_state()
        
        for delegatee_name, info in list(state.items()):
            pid = info.get('pid')
            if pid and not self.is_daemon_running(delegatee_name):
                # Process dead, remove from state
                del state[delegatee_name]
                print(f"Cleaned up zombie daemon for {delegatee_name}", file=sys.stderr)
        
        self._save_state(state)
    
    def start_daemon(self, delegatee_name: str, config: dict, task_file_path: str = None) -> Tuple[bool, str]:
        """Start daemon for delegatee."""
        # Check if already running
        if self.is_daemon_running(delegatee_name):
            return True, f"Daemon already running for {delegatee_name}"
        
        # Clean up zombies first
        self.cleanup_zombie_daemons()
        
        # Get delegatee config
        delegatee = None
        for d in config.get('delegatees', []):
            if d['name'] == delegatee_name:
                delegatee = d
                break
        
        if not delegatee:
            return False, f"Delegatee '{delegatee_name}' not found in configuration"
        
        # If task file provided, write startup state for worker to read
        if task_file_path:
            startup_state_file = path_utils.startup_file(delegatee_name)
            startup_state = {
                'task_file': task_file_path,
                'timestamp': subprocess.check_output(['date', '-Iseconds']).decode().strip()
            }
            startup_state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(startup_state_file, 'w') as f:
                json.dump(startup_state, f)
        
        # Start daemon process
        script_path = Path(__file__).parent / 'delegation_worker.py'
        
        try:
            # Start background process
            proc = subprocess.Popen(
                [sys.executable, str(script_path), delegatee_name],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True  # Detach from parent
            )
            
            pid = proc.pid

            # Wait briefly and verify the process survived startup
            time.sleep(2)
            rc = proc.poll()
            if rc is not None:
                try:
                    stdout, stderr = proc.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    stdout, stderr = b'', b''
                log_path = path_utils.daemon_state_file().parent / f'delegator-delegatee-{pid}.log'
                log_tail = ''
                if log_path.exists():
                    try:
                        log_tail = log_path.read_text()[-800:]
                    except Exception:
                        pass
                if not log_tail:
                    log_tail = (stderr or stdout or b'').decode('utf-8', errors='replace').strip()
                return False, (
                    f"Daemon died on startup (exit code {rc}, PID {pid}).\n"
                    f"Log: {log_path}\n"
                    f"Last output:\n{log_tail}"
                )

            # Save state only after confirming the process is alive
            state = self._load_state()
            state[delegatee_name] = {
                'pid': pid,
                'started_at': subprocess.check_output(['date', '-Iseconds']).decode().strip(),
                'room_id': delegatee.get('matrix', {}).get('room_id', '')
            }
            self._save_state(state)

            return True, f"Daemon started for {delegatee_name} (PID: {pid})"

        except Exception as e:
            return False, f"Failed to start daemon: {str(e)}"
    
    def stop_daemon(self, delegatee_name: str) -> Tuple[bool, str]:
        """Stop daemon for delegatee."""
        pid = self.get_daemon_pid(delegatee_name)
        
        if pid is None:
            return True, f"No running daemon for {delegatee_name}"
        
        try:
            # Terminate process group
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            
            # Remove from state
            state = self._load_state()
            if delegatee_name in state:
                del state[delegatee_name]
                self._save_state(state)
            
            return True, f"Daemon stopped for {delegatee_name}"
            
        except Exception as e:
            return False, f"Failed to stop daemon: {str(e)}"




def check_delegator_idle(config: dict) -> bool:
    """Check if delegator agent is currently idle.

    Uses a single path based on delegator type:
      - OpenClaw: runs check_openclaw_idle.sh script only
      - Hermes: checks gateway_state.json at profile path (or main if no profile set),
                with fallback to main path if profile path doesn't exist
    """
    delegator_type = config.get('delegator', {}).get('type', 'Hermes')

    if delegator_type == 'OpenClaw':
        script_path = Path(__file__).parent.parent / 'tools' / 'check_openclaw_idle.sh'
        if os.path.exists(script_path):
            try:
                result = subprocess.run(
                    [str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return "IDLING" in result.stdout.upper()
            except Exception:
                pass
        return True

    # Hermes — use ONE path based on profile config
    profile = config.get('delegator', {}).get('profile')
    state_path = path_utils.gateway_state_file(profile)

    try:
        with open(state_path, 'r') as f:
            state = json.load(f)
            active_agents = state.get('active_agents', 0)
            return active_agents == 0
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        # Fallback: check root gateway state if profile path doesn't exist
        fallback = path_utils.gateway_state_file()
        if fallback != state_path:
            try:
                with open(fallback, 'r') as f:
                    state = json.load(f)
                    active_agents = state.get('active_agents', 0)
                    return active_agents == 0
            except (FileNotFoundError, json.JSONDecodeError, KeyError):
                pass

    # Default to idle if no state file found
    return True


def format_task_message(task_file_path: str) -> str:
    """Format task message - only filename + protocol marker."""
    # Convert to absolute path if relative
    task_path = Path(task_file_path)
    if not task_path.is_absolute():
        task_path = Path.cwd() / task_path

    return f"""New task file: {task_path}

Read the task description from this file.

When you have no more tool calls planned, hand control back to the delegator
by ending your final message with `=== NO_MORE_ACTIONS ===` (exact wording!).

You can also use this signal to ASK A QUESTION: write the question in plain
text, then add `=== NO_MORE_ACTIONS ===` on its own line as the very last line.
The delegator will see your question in the handoff summary and answer by
starting a new delegation that follows up on it."""


def format_correction_message(message: str) -> str:
    """Format correction message for Hermes agents."""
    return f"/steer {message}"


def format_stop_message() -> str:
    """Format stop message."""
    return "Stop all tasks and wait for further instructions."


def format_continue_message() -> str:
    """Format continue/ping message."""
    return "Continue with the task. If finished or delegator action is required, answer with summary adding `=== NO_MORE_ACTIONS ===` (exact wording!)."


def format_pause_message() -> str:
    """Format pause /steer message sent to delegatee.

    The delegatee must hold the session open and remember its in-flight state
    so that a later `resume` can continue from where it stopped.
    """
    return (
        "/steer PAUSE — stop your current action immediately and wait. Do not "
        "issue further tool calls. Remember where you were (recent context, "
        "next planned step, partial output) so you can resume exactly there "
        "when I send `RESUME`. Acknowledge with one short text line and then "
        "stay quiet until RESUME arrives."
    )


def format_resume_message() -> str:
    """Format resume /steer message sent to a paused delegatee."""
    return (
        "/steer RESUME — continue from where you paused. Pick up the next "
        "planned step as if no time had passed. Same NO_MORE_ACTIONS protocol "
        "applies when you reach a question or finish."
    )


def _wait_for_delegator_idle(config: dict, timeout_sec: int = 60,
                              poll_sec: float = 1.0) -> bool:
    """Poll the delegator's idle state until it becomes idle or timeout.

    Used by send_message-to-delegator to avoid disrupting the delegator while
    it is busy in its own action loop. Returns True if delegator became idle
    within the timeout, False if it stayed busy.

    Internally re-uses the same gateway_state.json / OpenClaw-script check
    used by `check_delegator_idle()` so behaviour is consistent.
    """
    import time as _time
    deadline = _time.time() + timeout_sec
    while _time.time() < deadline:
        if check_delegator_idle(config):
            return True
        _time.sleep(poll_sec)
    return check_delegator_idle(config)


def cmd_start_task(args):
    """Start a new delegation task."""
    config_mgr = DelegationConfig()
    daemon_mgr = DaemonManager()
    
    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    logger = MatrixClient.setup_command_logging(config_mgr.config_path)
    logger.info(f"Starting task for delegatee {args.delegatee} from file: {args.task_file_path}")
    
    # Get delegatee config
    delegatee = config_mgr.get_delegatee(args.delegatee)
    if not delegatee:
        print(f"Error: Delegatee '{args.delegatee}' not found in configuration", file=sys.stderr)
        return 1
    
    # Convert to absolute path for the startup file
    task_path = Path(args.task_file_path)
    if not task_path.is_absolute():
        task_path = Path.cwd() / task_path
    
    # HU2: Optionally reset the delegatee's Hermes session before delivering the
    # new task. This is the recommended way to prevent T-number / context drift
    # between successive delegations: send a literal "/reset" message to the
    # delegatee's Matrix room, which the Hermes gateway translates into a
    # session-reset command (default reset_triggers = ["/new", "/reset"]).
    #
    # Idempotency: this only sends a Matrix message; it does not depend on the
    # delegation worker daemon being alive. If the delegatee gateway is not
    # configured or unreachable, we log a warning and continue with the task
    # (we do not abort the whole start_task).
    if getattr(args, 'reset_session', False):
        delegatee_room_id = delegatee.get('matrix', {}).get('room_id')
        if delegatee_room_id:
            logger.info(
                f"HU2 reset_session=True: sending /reset to delegatee "
                f"{args.delegatee} (room {delegatee_room_id}) before new task"
            )
            try:
                reset_client = MatrixClient(config)
                if reset_client.send_message_sync(delegatee_room_id, "/reset"):
                    logger.info(f"/reset delivered to {args.delegatee}; waiting for gateway to process")
                    # Give the delegatee gateway a few seconds to interrupt any
                    # in-flight turn, clear the session, and become ready for
                    # the next user message.
                    import time as _time
                    _time.sleep(5)
                else:
                    logger.warning(
                        f"Failed to send /reset to {args.delegatee} — continuing without session reset"
                    )
            except Exception as reset_err:
                logger.warning(
                    f"/reset to {args.delegatee} raised exception: {reset_err} "
                    f"— continuing without session reset",
                    exc_info=True,
                )
        else:
            logger.warning(
                f"reset_session=True but delegatee '{args.delegatee}' has no "
                f"matrix.room_id configured — skipping /reset (no-op)"
            )

    # Stop any existing daemon first — prevents stale messages from polluting the report.
    if daemon_mgr.is_daemon_running(args.delegatee):
        logger.info(f"Stopping existing daemon for {args.delegatee} before starting new task")
        stop_success, stop_msg = daemon_mgr.stop_daemon(args.delegatee)
        logger.info(f"Stop result: {stop_msg}")

        # Wait for process to actually die - retry check with timeout
        if stop_success:
            import time as _time
            for _ in range(10):  # max 5 seconds
                _time.sleep(0.5)
                if not daemon_mgr.is_daemon_running(args.delegatee):
                    break
            else:
                logger.warning(f"Daemon for {args.delegatee} may still be alive after stop")
    
    # Create startup task file for daemon to read on initialization
    startup_file = path_utils.startup_file(args.delegatee)
    startup_file.parent.mkdir(parents=True, exist_ok=True)
    
    startup_data = {
        'task_file': str(task_path),
        'started_at': datetime.now().isoformat()
    }
    
    with open(startup_file, 'w') as f:
        json.dump(startup_data, f, indent=2)
    
    logger.info(f"Created startup task file: {startup_file}")
    
    # Start fresh daemon (will read the startup file and send task message)
    success, msg = daemon_mgr.start_daemon(args.delegatee, config, str(task_path))
    print(msg)
    logger.info(msg)
    
    if not success:
        print(f"Error: {msg}", file=sys.stderr)
        return 1
    
    # Note: Task message will be sent by the worker when it processes the startup file
    pid = daemon_mgr.get_daemon_pid(args.delegatee)
    print(f"Task started for {args.delegatee}")
    print(f"PID={pid}")
    print(
        "STOP NOW: the task is delegated and the worker daemon is running. "
        "Acknowledge with EXACTLY this text format (substitute the actual PID number): "
        f"'Delegated. PID={pid} — waiting for delegatee report.' "
        "You MUST include the literal text 'PID=' followed by the number shown above; "
        "this proves you actually ran start_task rather than just stating intent. "
        "Then emit NO further tool calls. Wait for the next progress update from the worker."
    )
    logger.info(f"Task initialization complete - worker will send task message")
    return 0


def cmd_correct(args):
    """Send correction to delegatee."""
    config_mgr = DelegationConfig()
    
    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    logger = MatrixClient.setup_command_logging(config_mgr.config_path)
    logger.info(f"Sending correction to {args.delegatee}: {args.message}")
    
    # Get delegatee config
    delegatee = config_mgr.get_delegatee(args.delegatee)
    if not delegatee:
        print(f"Error: Delegatee '{args.delegatee}' not found in configuration", file=sys.stderr)
        return 1

    # No daemon running → task already finished. A bare correction would be lost
    # (no worker to relay replies), so re-engage via start_task with keep-session.
    daemon_mgr = DaemonManager()
    if not daemon_mgr.is_daemon_running(args.delegatee):
        logger.info(
            f"No active worker for {args.delegatee}; routing correction through "
            f"start_task --no-reset-session (the task had already finished)"
        )
        print(
            f"No active worker for {args.delegatee} (task finished) — re-engaging via "
            f"start_task --no-reset-session so the correction is not lost."
        )
        from types import SimpleNamespace
        task_dir = path_utils.startup_file(args.delegatee).parent
        task_dir.mkdir(parents=True, exist_ok=True)
        corr_task = task_dir / f"correction-as-task-{args.delegatee}.md"
        with open(corr_task, 'w') as _f:
            _f.write("# Resume the current task — correction\n\n")
            _f.write(
                "Your previous delegation ended, but the work is not accepted yet. "
                "Your session context is preserved (keep-session) — resume from where you "
                "left off and address the following correction:\n\n"
            )
            _f.write(args.message.strip() + "\n\n")
            _f.write("Verify against the acceptance criteria, then end with `=== NO_MORE_ACTIONS ===`.\n")
        start_args = SimpleNamespace(
            delegatee=args.delegatee,
            task_file_path=str(corr_task),
            reset_session=False,
        )
        return cmd_start_task(start_args)

    # Active worker present — send correction.
    client = MatrixClient(config)
    room_id = delegatee.get('matrix', {}).get('room_id')

    if not room_id:
        print("Error: No room_id configured for delegatee", file=sys.stderr)
        return 1

    correction_message = format_correction_message(args.message)
    
    if client.send_message_sync(room_id, correction_message):
        print(f"Correction sent to {args.delegatee}")
        print(
            "STOP NOW: correction delivered. Emit one short text line "
            "(e.g. 'Correction sent, waiting.') and NO further tool calls. "
            "Wait for the next progress update from the worker."
        )
        logger.info(f"Correction message sent to {args.delegatee}")
        return 0
    else:
        print("Failed to send correction message", file=sys.stderr)
        return 1


def cmd_stop(args):
    """Stop delegation for delegatee."""
    config_mgr = DelegationConfig()
    daemon_mgr = DaemonManager()
    
    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    logger = MatrixClient.setup_command_logging(config_mgr.config_path)
    logger.info(f"Stopping delegation for {args.delegatee}")
    
    # Get delegatee config
    delegatee = config_mgr.get_delegatee(args.delegatee)
    if not delegatee:
        print(f"Error: Delegatee '{args.delegatee}' not found in configuration", file=sys.stderr)
        return 1
    
    # Send stop message first
    client = MatrixClient(config)
    room_id = delegatee.get('matrix', {}).get('room_id')
    
    if room_id:
        stop_message = format_stop_message()
        client.send_message_sync(room_id, stop_message)
        logger.info(f"Stop message sent to {args.delegatee}")
    
    # Stop daemon
    success, msg = daemon_mgr.stop_daemon(args.delegatee)
    print(msg)
    logger.info(msg)

    return 0 if success else 1


def cmd_ping(args):
    """Send continue prompt to delegatee."""
    config_mgr = DelegationConfig()
    
    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    logger = MatrixClient.setup_command_logging(config_mgr.config_path)
    logger.info(f"Pinging {args.delegatee} to continue")
    
    # Get delegatee config
    delegatee = config_mgr.get_delegatee(args.delegatee)
    if not delegatee:
        print(f"Error: Delegatee '{args.delegatee}' not found in configuration", file=sys.stderr)
        return 1
    
    # Send continue message
    client = MatrixClient(config)
    room_id = delegatee.get('matrix', {}).get('room_id')
    
    if not room_id:
        print("Error: No room_id configured for delegatee", file=sys.stderr)
        return 1
    
    continue_message = format_continue_message()
    
    if client.send_message_sync(room_id, continue_message):
        print(f"Continue prompt sent to {args.delegatee}")
        print(
            "STOP NOW: continue prompt delivered. Emit one short text line "
            "(e.g. 'Pinged, waiting.') and NO further tool calls. "
            "Wait for the next progress update from the worker."
        )
        logger.info(f"Continue message sent to {args.delegatee}")
        return 0
    else:
        print("Failed to send continue message", file=sys.stderr)
        return 1


def cmd_list_delegatees(args):
    """List all configured delegatees."""
    config_mgr = DelegationConfig()
    daemon_mgr = DaemonManager()
    
    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    
    delegatees = config_mgr.get_all_delegatees()
    
    if not delegatees:
        print("No delegatees configured")
        return 0
    
    # Print header
    print(f"{'Name':<20} {'Description':<35} {'Type':<10} {'Status':<10}")
    print("-" * 75)
    
    for d in delegatees:
        name = d.get('name', 'unknown')[:19]
        description = d.get('description', 'No description')[:34]
        delegator_type = d.get('type', 'Unknown')[:9]
        
        # Check status
        is_running = daemon_mgr.is_daemon_running(name)
        status = "BUSY" if is_running else "IDLE"
        
        room_id = d.get('matrix', {}).get('room_id', '')
        
        print(f"{name:<20} {description:<35} {delegator_type:<10} {status:<10}")
    
    return 0


def cmd_send_message(args):
    """Send a message to delegator or delegatee.

    Backpressure: if the target is the *delegator* and the delegator is busy
    in its own action loop, this command waits (up to --busy-timeout seconds)
    for it to become idle before delivering. Sending to a delegatee is always
    immediate — disruption is the whole point of /steer-style messages.
    """
    config_mgr = DelegationConfig()

    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    logger = MatrixClient.setup_command_logging(config_mgr.config_path)
    logger.info(f"Sending message to {args.target}")

    # Determine target room
    if args.target == 'delegator':
        delegator_config = config.get('delegator', {})
        room_id = delegator_config.get('matrix', {}).get('room_id')
        if not room_id:
            print("Error: No room_id configured for delegator", file=sys.stderr)
            return 1
        target_name = 'delegator'

        # Backpressure: wait until delegator is idle.
        busy_timeout = getattr(args, 'busy_timeout', 60) or 60
        if not _wait_for_delegator_idle(config, timeout_sec=busy_timeout):
            # Stayed busy. Caller chooses what to do: either spool to outbox
            # for the worker daemon to flush, or fail loudly. We fail loudly
            # so callers without a worker daemon (one-shot scripts) don't
            # silently drop the message.
            print(
                f"Delegator stayed busy for {busy_timeout}s; not sending. "
                f"Retry later or use --force to deliver anyway.",
                file=sys.stderr,
            )
            logger.warning(
                f"Skipped send to delegator after {busy_timeout}s busy "
                f"(target={target_name}, force={getattr(args, 'force', False)})"
            )
            if not getattr(args, 'force', False):
                return 2
    else:
        # Target is a delegatee name
        delegatee = config_mgr.get_delegatee(args.target)
        if not delegatee:
            print(f"Error: Delegatee '{args.target}' not found in configuration", file=sys.stderr)
            return 1
        room_id = delegatee.get('matrix', {}).get('room_id')
        if not room_id:
            print("Error: No room_id configured for delegatee", file=sys.stderr)
            return 1
        target_name = args.target

    # Send message
    client = MatrixClient(config)

    if client.send_message_sync(room_id, args.message):
        print(f"Message sent to {target_name}")
        logger.info(f"Message sent successfully to {target_name}")
        return 0
    else:
        print("Failed to send message", file=sys.stderr)
        return 1


def cmd_pause(args):
    """Pause a running delegation.

    Sends a /steer PAUSE message to the delegatee asking it to hold its
    session open and remember its in-flight state. Also writes a pause marker
    file that the worker polls; while the marker exists the worker:
      * does not fire periodic-progress reports,
      * does not fire auto-ping or "wedged" alerts,
      * does not generate "fallen out of loop" alerts,
      * accumulates paused-duration so that all elapsed-time thresholds
        resume cleanly when the marker is removed.
    """
    config_mgr = DelegationConfig()

    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    logger = MatrixClient.setup_command_logging(config_mgr.config_path)
    logger.info(f"Pausing delegation for {args.delegatee}")

    delegatee = config_mgr.get_delegatee(args.delegatee)
    if not delegatee:
        print(f"Error: Delegatee '{args.delegatee}' not found in configuration", file=sys.stderr)
        return 1

    room_id = delegatee.get('matrix', {}).get('room_id')
    if not room_id:
        print("Error: No room_id configured for delegatee", file=sys.stderr)
        return 1

    # Write pause marker first so the worker sees it on its next poll cycle
    # even if the /steer message somehow fails to reach the delegatee.
    marker = path_utils.pause_marker_file(args.delegatee)
    marker.parent.mkdir(parents=True, exist_ok=True)
    with open(marker, 'w') as f:
        json.dump({'paused_at': time.time()}, f)

    client = MatrixClient(config)
    pause_message = format_pause_message()

    if client.send_message_sync(room_id, pause_message):
        print(f"Delegation paused for {args.delegatee}")
        print(
            "STOP NOW: pause delivered. Worker will hold all timers until "
            "you call `resume`. No further tool calls needed."
        )
        logger.info(f"Pause message sent to {args.delegatee}")
        return 0
    else:
        # Remove marker if we couldn't notify the delegatee — otherwise the
        # worker will starve a delegatee that thinks it's still running.
        try:
            marker.unlink()
        except FileNotFoundError:
            pass
        print("Failed to send pause message", file=sys.stderr)
        return 1


def cmd_resume(args):
    """Resume a previously paused delegation.

    Removes the pause marker, sends a /steer RESUME message to the delegatee.
    The worker, on its next poll, notices the marker is gone, adds the
    elapsed paused-duration to its accumulator, and continues all timers
    from where they were when pause was issued.
    """
    config_mgr = DelegationConfig()

    try:
        config = config_mgr.load()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    logger = MatrixClient.setup_command_logging(config_mgr.config_path)
    logger.info(f"Resuming delegation for {args.delegatee}")

    delegatee = config_mgr.get_delegatee(args.delegatee)
    if not delegatee:
        print(f"Error: Delegatee '{args.delegatee}' not found in configuration", file=sys.stderr)
        return 1

    room_id = delegatee.get('matrix', {}).get('room_id')
    if not room_id:
        print("Error: No room_id configured for delegatee", file=sys.stderr)
        return 1

    marker = path_utils.pause_marker_file(args.delegatee)
    if marker.exists():
        try:
            marker.unlink()
            logger.info(f"Removed pause marker {marker}")
        except FileNotFoundError:
            pass
    else:
        logger.warning(f"No pause marker found for {args.delegatee} — resuming anyway")

    client = MatrixClient(config)
    resume_message = format_resume_message()

    if client.send_message_sync(room_id, resume_message):
        print(f"Delegation resumed for {args.delegatee}")
        print(
            "STOP NOW: resume delivered. Worker timers continue from where "
            "they were before the pause. No further tool calls needed."
        )
        logger.info(f"Resume message sent to {args.delegatee}")
        return 0
    else:
        print("Failed to send resume message", file=sys.stderr)
        return 1


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description='Agent Delegation Core')
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # start_task command
    start_parser = subparsers.add_parser('start_task', help='Start a new delegation task')
    start_parser.add_argument('--delegatee', required=True, help='Delegatee name')
    start_parser.add_argument('--task-file-path', required=True, help='Path to task description file')
    start_parser.add_argument('--timeout', type=int, help='Optional timeout in seconds')
    # Session reset is ON BY DEFAULT. Use --no-reset-session to preserve context
    # when answering a delegatee's question or continuing an interactive exchange.
    start_parser.add_argument(
        '--reset-session', dest='reset_session', action='store_true', default=True,
        help='HU2/HU6: Send /reset to the delegatee before the task (fresh '
             'session). This is the DEFAULT.'
    )
    start_parser.add_argument(
        '--no-reset-session', dest='reset_session', action='store_false',
        help='HU6: Do NOT reset the delegatee session — preserve prior context. '
             'Use when answering a question the delegatee asked or continuing an '
             'interactive exchange.'
    )
    
    # correct command
    correct_parser = subparsers.add_parser('correct', help='Send correction to delegatee')
    correct_parser.add_argument('--delegatee', required=True, help='Delegatee name')
    correct_parser.add_argument('--message', required=True, help='Correction message')
    
    # stop command
    stop_parser = subparsers.add_parser('stop', help='Stop delegation for delegatee')
    stop_parser.add_argument('--delegatee', required=True, help='Delegatee name')
    
    # ping command
    ping_parser = subparsers.add_parser('ping', help='Send continue prompt to delegatee')
    ping_parser.add_argument('--delegatee', required=True, help='Delegatee name')
    
    # list_delegatees command
    list_parser = subparsers.add_parser('list_delegatees', help='List all configured delegatees')
    
    # send_message command
    send_parser = subparsers.add_parser('send_message', help='Send a message to delegator or delegatee')
    send_parser.add_argument('--target', required=True, help='Target: "delegator" or delegatee name')
    send_parser.add_argument('--message', required=True, help='Message text to send')
    send_parser.add_argument(
        '--busy-timeout', type=int, default=60,
        help="When target is 'delegator', max seconds to wait for it to become "
             "idle before giving up. Defaults to 60.",
    )
    send_parser.add_argument(
        '--force', action='store_true',
        help="When target is 'delegator', deliver even if the delegator is "
             "still busy after --busy-timeout. Use only for urgent / fatal "
             "alerts that the delegator must see immediately.",
    )

    # pause command
    pause_parser = subparsers.add_parser('pause', help='Pause a running delegation')
    pause_parser.add_argument('--delegatee', required=True, help='Delegatee name')

    # resume command
    resume_parser = subparsers.add_parser('resume', help='Resume a paused delegation')
    resume_parser.add_argument('--delegatee', required=True, help='Delegatee name')

    args = parser.parse_args()

    if args.command == 'start_task':
        return cmd_start_task(args)
    elif args.command == 'correct':
        return cmd_correct(args)
    elif args.command == 'stop':
        return cmd_stop(args)
    elif args.command == 'ping':
        return cmd_ping(args)
    elif args.command == 'list_delegatees':
        return cmd_list_delegatees(args)
    elif args.command == 'send_message':
        return cmd_send_message(args)
    elif args.command == 'pause':
        return cmd_pause(args)
    elif args.command == 'resume':
        return cmd_resume(args)
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())
