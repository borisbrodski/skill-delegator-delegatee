#!/usr/bin/env python3
"""
Agent Delegation Worker - Background process for tracking delegatee progress.

This worker subscribes to Matrix messages in the delegatee room, detects the
=== NO_MORE_ACTIONS === marker, and relays progress updates to the delegator.

Usage: python delegation_worker.py <delegatee-name>
"""

import asyncio
import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import quote, urlencode
import yaml
try:
    from .matrix_client import MatrixClient
    from . import path_utils
except ImportError:
    import os, sys
    _src = os.path.dirname(os.path.abspath(__file__))
    if _src not in sys.path:
        sys.path.insert(0, _src)
    from matrix_client import MatrixClient, _retry_async
    import path_utils


# Matrix CS spec hard limit is 65,536 bytes per event body.
# We keep a safe margin for the event envelope fields (type, sender, room_id, …).
MATRIX_MAX_MESSAGE_BYTES = 62_000


def setup_logging(config_dir: Path, pid: int):
    """Setup logging to file in config directory."""
    log_file = config_dir / f"delegator-delegatee-{pid}.log"
    
    # Configure file logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger('delegation_worker')




# Note: logger will be set up after config is loaded with PID-based filename


class DelegationState:
    """Tracks the state of a single delegation (one per delegatee)."""
    
    def __init__(self, delegatee_name: str, delegatee_room_id: str, delegator_room_id: str):
        self.delegatee_name = delegatee_name  # Use name instead of ID
        self.delegatee_room_id = delegatee_room_id
        self.delegator_room_id = delegator_room_id
        self.task_description: Optional[str] = None
        # Batch token for Matrix /messages pagination (NOT event_id)
        self.last_batch_token: Optional[str] = None
        self.polling_active: bool = False
        self.created_at: datetime = datetime.now()
        self.messages_collected: list = []
        # Configurable message truncation (in bytes) - default 1KB
        self.max_update_bytes = 1024
        # Track event IDs we've already processed to avoid duplicates
        self.processed_event_ids: set = set()
        # Map event_id -> index in messages_collected (for detecting edits)
        self.event_id_to_index: Dict[str, int] = {}
        # Map event_id -> last known body content (for reliable edit detection)
        self.event_id_to_body: Dict[str, str] = {}
        # Track the user ID that sent the task (to distinguish from delegatee responses)
        self.task_sender_user_id: Optional[str] = None
        # Timestamp when current task started - used to filter out old messages
        self.task_start_timestamp: Optional[float] = None
        # Timestamp for periodic progress reports — initialized to task start time
        # so messages collected during startup aren't missed by the first report.
        # Will be overwritten when task_start_timestamp is set from Matrix event.
        self.last_progress_report_time: float = 0.0
        # Persistent flag: True when completion detected but handoff not yet confirmed delivered
        self.completion_handoff_pending: bool = False
        # Track event IDs that were skipped as OLD (before task_start_timestamp)
        # so the all_already_processed check can still advance the batch token.
        self.seen_old_event_ids: set = set()
        # Once True, stop advancing the batch token — we've scrolled past all old messages
        # and should hold position to detect new messages arriving after task_start_timestamp.
        self.history_catchup_done: bool = False
        # Auto-recovery: track last time we saw substantive output from the delegatee.
        # If silence exceeds idle_ping_threshold_sec, the worker auto-pings.
        # Initialized to task start time when task starts.
        self.last_substantive_msg_time: float = 0.0
        # How many auto-pings we've sent in the current silence window.
        self.idle_ping_count: int = 0
        # Last time we sent an auto-ping (debounce).
        self.last_auto_ping_time: float = 0.0
        # Pause / resume support — suppress all outbound reports while paused.
        self.paused: bool = False
        self.paused_at: float = 0.0
        # Accumulated paused duration; subtracted from elapsed checks on resume.
        self.paused_duration_total: float = 0.0
        # Has fall-out-of-loop alert already been sent? Reset on substantive activity.
        self.fall_out_of_loop_alerted: bool = False



class DelegationWorker:
    """Main worker class managing a single delegatee."""
    
    def __init__(self, config_path: str, delegatee_name: str, logger=None):
        self.config_path = Path(config_path)
        self.delegatee_name = delegatee_name
        self.config = self._load_config()
        self.client = MatrixClient(self.config)
        self.state_file = self._get_state_file_path()
        self.state: Optional[DelegationState] = None
        self.running = False
        
        # Polling intervals from config
        timeouts = self.config.get('timeouts', {})
        self.poll_interval_sec = timeouts.get('poll_interval_sec', 15)
        self.progress_report_interval_sec = timeouts.get('progress_report_interval_sec', 900)
        
        # Circuit breaker: track consecutive errors to alert delegator
        self.consecutive_errors = 0
        self.max_consecutive_errors = 10  # ~2.5 min at 15s interval before alerting
        
        # Use provided logger or create one
        self.logger = logger if logger else logging.getLogger('delegation_worker')
        
        # Check for startup task file (passed by delegator)
        self._load_startup_task()
        
    def _load_config(self) -> dict:
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Config not found: {self.config_path}")
            
        with open(self.config_path, 'r') as f:
            return yaml.safe_load(f)
    
    def _get_state_file_path(self) -> Path:
        """Get path to delegation state file."""
        delegator_type = self.config.get('delegator', {}).get('type', 'Hermes')
        profile = self.config.get('delegator', {}).get('profile', '')
        return path_utils.delegation_state_file(delegator_type, profile)
    
    def _load_state(self) -> dict:
        """Load persisted state from file with shared lock."""
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
        """Persist state to file with exclusive lock."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.state_file, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    
    def is_delegator_idle(self) -> bool:
        """Check if delegator agent is currently idle.
        
        Uses a single path based on delegator type:
          - OpenClaw: runs check_openclaw_idle.sh script only
          - Hermes: checks gateway_state.json at profile path (or main if no profile set),
                    with fallback to main path if profile path doesn't exist
        """
        delegator_type = self.config.get('delegator', {}).get('type', 'Hermes')
        
        if delegator_type == 'OpenClaw':
            script_path = Path(__file__).parent.parent / 'tools' / 'check_openclaw_idle.sh'
            if os.path.exists(script_path):
                import subprocess
                result = subprocess.run([str(script_path)], capture_output=True, text=True, timeout=5)
                return "IDLING" in result.stdout.upper()
            return True
        
        # Hermes — use ONE path based on profile config
        profile = self.config.get('delegator', {}).get('profile')
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

    def is_delegatee_idle(self) -> bool:
        """Check if the **delegatee** agent is currently idle.

        Same mechanism as `is_delegator_idle` but reads the delegatee's
        profile gateway_state.json. Returns True only if active_agents == 0
        (i.e. the delegatee daemon is not currently running an agent turn).
        For OpenClaw delegatees, runs check_openclaw_idle.sh.

        Default-True if state can't be read — we never want a missing file
        to keep us silent forever; the time-based fall-out window still
        wins.
        """
        # Find the delegatee config block for this worker
        delegatee_cfg = {}
        for d in self.config.get('delegatees', []) or []:
            if d.get('name') == self.delegatee_name:
                delegatee_cfg = d
                break
        delegatee_type = delegatee_cfg.get('type', 'Hermes')

        if delegatee_type == 'OpenClaw':
            script_path = Path(__file__).parent.parent / 'tools' / 'check_openclaw_idle.sh'
            if os.path.exists(script_path):
                import subprocess
                try:
                    result = subprocess.run(
                        [str(script_path), self.delegatee_name],
                        capture_output=True, text=True, timeout=5,
                    )
                    return "IDLING" in result.stdout.upper()
                except Exception:
                    return True
            return True

        # Hermes — read the delegatee's profile gateway_state.json
        profile = delegatee_cfg.get('profile')
        state_path = path_utils.gateway_state_file(profile)
        try:
            with open(state_path, 'r') as f:
                state = json.load(f)
                return state.get('active_agents', 0) == 0
        except (FileNotFoundError, json.JSONDecodeError, KeyError, OSError):
            return True  # Default-idle when state unreadable

    # ------------------------------------------------------------------
    # Pause / resume support
    # ------------------------------------------------------------------
    def _check_pause_marker(self) -> tuple[bool, float]:
        """Return (paused_now, marker_timestamp). paused_now=True iff the
        pause marker file exists on disk. marker_timestamp is the wall-clock
        time at which the pause was requested (from the marker JSON), or
        0.0 if no marker exists or it could not be parsed.
        """
        marker = path_utils.pause_marker_file(self.delegatee_name)
        if not marker.exists():
            return False, 0.0
        try:
            with open(marker, 'r') as f:
                data = json.load(f)
            return True, float(data.get('paused_at', 0.0))
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            # Treat malformed marker as "paused now" with unknown start; we'd
            # rather under-report than spam alerts while the marker is broken.
            return True, time.time()

    def _sync_pause_state(self):
        """Reconcile the worker's in-memory pause state with the marker file.

        - If a marker appeared since last cycle → transition into paused:
          set self.state.paused = True, remember paused_at.
        - If a marker disappeared since last cycle → transition out of paused:
          add elapsed (now - paused_at) to paused_duration_total, clear flag.
        - Otherwise: no-op.

        Called at the top of every poll iteration.
        """
        if not self.state:
            return
        marker_present, marker_ts = self._check_pause_marker()
        if marker_present and not self.state.paused:
            # Entering pause window.
            self.state.paused = True
            self.state.paused_at = marker_ts or time.time()
            self.logger.warning(
                f"[PAUSE] Worker entering paused state for {self.delegatee_name} "
                f"(marker ts={self.state.paused_at:.1f}). All outbound reports, "
                f"auto-pings, and out-of-loop alerts are suppressed until resume."
            )
        elif not marker_present and self.state.paused:
            # Leaving pause window — accumulate elapsed.
            now = time.time()
            delta = max(0.0, now - (self.state.paused_at or now))
            self.state.paused_duration_total += delta
            self.state.paused = False
            self.state.paused_at = 0.0
            self.logger.warning(
                f"[RESUME] Worker resumed for {self.delegatee_name} after "
                f"{delta:.1f}s pause (cumulative paused {self.state.paused_duration_total:.1f}s). "
                f"Timers will subtract paused-duration to keep thresholds honest."
            )

    def _adjusted_silence(self, now: Optional[float] = None) -> float:
        """Return silence-duration (seconds since last substantive activity)
        with paused-duration subtracted out. Use this in place of
        `now - last_substantive_msg_time` for any threshold check.
        """
        if not self.state:
            return 0.0
        if now is None:
            now = time.time()
        last = self.state.last_substantive_msg_time or now
        raw = now - last
        return max(0.0, raw - self.state.paused_duration_total)

    # ------------------------------------------------------------------
    # Outbound-to-delegator outbox with backpressure
    # ------------------------------------------------------------------
    def _load_outbox(self) -> list:
        """Load the on-disk outbox of messages deferred while delegator was busy."""
        path = path_utils.outbox_file(self.delegatee_name)
        if not path.exists():
            return []
        try:
            with open(path, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save_outbox(self, items: list):
        """Persist the outbox atomically with an exclusive lock."""
        path = path_utils.outbox_file(self.delegatee_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(items, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    async def _send_to_delegator(self, message: str, *, force: bool = False,
                                  kind: str = 'progress') -> bool:
        """Deliver an outbound message to the delegator with idle-backpressure.

        If the delegator is idle right now, sends immediately and also drains
        any previously queued messages in FIFO order. If the delegator is busy
        AND `force` is False, the message is appended to a persistent outbox
        and will be retried on subsequent poll cycles.

        Set ``force=True`` for messages that MUST go through even if the
        delegator is mid-action (fatal "delegatee wedged" alerts, completion
        handoffs that have been pending too long, etc.). Callers should think
        twice — gratuitous force defeats the whole point of backpressure.

        ``kind`` is a short tag carried in the outbox for diagnostics
        (e.g. "progress", "handoff", "wedged", "fall_out_of_loop", "health").
        """
        if not self.state:
            return False
        delegator_idle = self.is_delegator_idle()
        outbox = self._load_outbox()

        if delegator_idle:
            # Send the message first, then drain any older queued items.
            try:
                await self.client.send_message(
                    self.state.delegator_room_id, message,
                )
                self.logger.info(
                    f"[outbox] Delivered {kind!r} message to delegator "
                    f"({len(message)} chars)"
                )
            except Exception as exc:
                self.logger.error(
                    f"[outbox] send_to_delegator failed (kind={kind}): {exc}",
                    exc_info=True,
                )
                # Spool to outbox so we retry next cycle.
                outbox.append({
                    'kind': kind, 'message': message,
                    'enqueued_at': time.time(), 'attempts': 1,
                })
                self._save_outbox(outbox)
                return False

            # Drain queued items now that delegator is idle.
            still_queued = []
            for item in outbox:
                try:
                    await self.client.send_message(
                        self.state.delegator_room_id, item['message'],
                    )
                    self.logger.info(
                        f"[outbox] Drained queued {item.get('kind', '?')!r} "
                        f"message (was queued {time.time() - item.get('enqueued_at', 0):.0f}s)"
                    )
                except Exception as exc:
                    item['attempts'] = item.get('attempts', 0) + 1
                    still_queued.append(item)
                    self.logger.warning(
                        f"[outbox] Drain failed for {item.get('kind', '?')!r}: {exc}"
                    )
            self._save_outbox(still_queued)
            return True

        # Delegator busy.
        if force:
            try:
                await self.client.send_message(
                    self.state.delegator_room_id, message,
                )
                self.logger.warning(
                    f"[outbox] FORCED delivery of {kind!r} message despite "
                    f"busy delegator ({len(message)} chars)"
                )
                return True
            except Exception as exc:
                self.logger.error(
                    f"[outbox] Forced send failed (kind={kind}): {exc}",
                    exc_info=True,
                )
                return False

        # Defer.
        outbox.append({
            'kind': kind, 'message': message,
            'enqueued_at': time.time(), 'attempts': 0,
        })
        self._save_outbox(outbox)
        self.logger.info(
            f"[outbox] Delegator busy — queued {kind!r} message "
            f"({len(message)} chars, queue depth={len(outbox)})"
        )
        return False

    def _load_startup_task(self):
        """Load startup task from file if provided by delegator. Stores path for async loading."""
        startup_file = path_utils.startup_file(self.delegatee_name)
        if not startup_file.exists():
            return
        
        try:
            with open(startup_file, 'r') as f:
                startup_data = json.load(f)
            
            task_file = startup_data.get('task_file')
            if task_file:
                self.logger.info(f"Found startup task file: {startup_file}")
                # Store for async loading in run() method
                self._pending_startup_task = task_file
                self._pending_startup_file = startup_file
        except Exception as e:
            self.logger.error(
                f"Failed to load startup task metadata: {e}", exc_info=True
            )

    def get_delegatee_config(self) -> Optional[dict]:
        """Get configuration for this delegatee."""
        for d in self.config.get('delegatees', []):
            if d['name'] == self.delegatee_name:
                return d
        return None
    
    async def start_task(self, task_file_path: str) -> bool:
        """
        Start a new delegation task for this delegatee.
        
        Only one task per delegatee - any existing task is stopped first.
        The task file path is sent to delegatee, which should read the file itself.
        """
        self.logger.info(f"Starting task for delegatee {self.delegatee_name} from file: {task_file_path}")
        
        # Get delegatee config
        delegatee = self.get_delegatee_config()
        if not delegatee:
            self.logger.error(f"Delegatee '{self.delegatee_name}' not found in configuration")
            return False
        
        delegator_room = self.config['delegator']['matrix']['room_id']

        # Remember if there was an active task before we replace state
        had_active_task = bool(self.state and getattr(self.state, 'polling_active', False))

        # Create/update state (single state per delegatee)
        self.state = DelegationState(
            self.delegatee_name,
            delegatee['matrix']['room_id'],
            delegator_room
        )
        self.state.task_description = task_file_path
        
        # CRITICAL: Record a temporary start time now; will be overwritten with the actual
        # Matrix server timestamp after we fetch the event (avoids clock skew issues)
        import time
        self.state.task_start_timestamp = time.time()
        self.logger.info(f"Task started at timestamp {self.state.task_start_timestamp} (temp, will use Matrix ts)")
        
        # Reset message tracking - old event_ids must not be reprocessed
        self.state.messages_collected = []
        self.state.processed_event_ids = set()
        self.state.event_id_to_index = {}
        self.state.event_id_to_body = {}
        self.state.seen_old_event_ids = set()  # Reset seen-old tracking for new task
        self.state.history_catchup_done = False  # Will be set once we scroll past all old messages
        self.state.completion_handoff_pending = False  # Reset for new task
        # Auto-recovery: start the silence timer at task start.
        self.state.last_substantive_msg_time = self.state.task_start_timestamp
        self.state.idle_ping_count = 0
        self.state.last_auto_ping_time = 0.0
        
        # Don't set last_batch_token here - it's updated from API response in check_progress()
        
        # Convert to absolute path if relative for the message
        task_path = Path(task_file_path)
        if not task_path.is_absolute():
            task_path = Path.cwd() / task_path

        # Compose and send task message
        # If there's an existing active task, prepend stop instruction
        if had_active_task:
            preamble = "Stop previous task.\n\n"
        else:
            preamble = ""

        completion_marker = "\n\nWhen no more tool calls are issued, call delegator attention by adding `=== NO_MORE_ACTIONS ===` (exact wording!)."

        # Try to inline the file content so the delegatee doesn't need to read it.
        # Fall back to the file path if the content is too large for a Matrix message.
        task_body = ""
        try:
            raw = task_path.read_text(encoding="utf-8", errors="replace")
            candidate = f"{preamble}New task:\n\n{raw}{completion_marker}"
            if len(candidate.encode("utf-8")) <= MATRIX_MAX_MESSAGE_BYTES:
                task_body = candidate
        except Exception as read_err:
            self.logger.warning(
                f"Could not read task file for inlining: {read_err}",
                exc_info=True,
            )

        if not task_body:
            task_body = f"{preamble}New task file: {task_path}\n\nRead the task description from this file.{completion_marker}"

        task_message = task_body
        
        # Send task message and capture event_id
        send_result = await self.client.send_message(self.state.delegatee_room_id, task_message)
        task_event_id = send_result.get('event_id')
        
          # Fetch the actual sender of this message (may differ from configured user_id due to bot relay)
        if task_event_id:
            try:
                import aiohttp
                encoded_room = self.client._encode_room_id(self.state.delegatee_room_id)
                url = f"{self.client.url}/_matrix/client/r0/rooms/{encoded_room}/event/{task_event_id}"
                headers = {"Authorization": f"Bearer {self.client.access_token}"}
                
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            event_data = await resp.json()
                            actual_sender = event_data.get('sender')
                            self.state.task_sender_user_id = actual_sender
                            self.logger.info(f"Task message {task_event_id} sent by: {actual_sender}")
                            
                            # CRITICAL: Use the Matrix server timestamp as task start cutoff.
                            # This avoids clock skew between local time.time() and Matrix origin_server_ts.
                            matrix_ts_ms = event_data.get('origin_server_ts', 0)
                            self.state.task_start_timestamp = matrix_ts_ms / 1000.0
                            # Sync periodic report timer to task start so early messages aren't missed
                            self.state.last_progress_report_time = self.state.task_start_timestamp
                            self.logger.info(
                                f"Task start timestamp set to Matrix server time: "
                                f"{self.state.task_start_timestamp} (from event {task_event_id})"
                            )
            except Exception as e:
                self.logger.warning(
                    f"Could not determine task sender: {e}", exc_info=True
                )
                # Fallback: sync periodic report timer to the temporary start timestamp
                self.state.last_progress_report_time = self.state.task_start_timestamp or 0.0
        
        # Activate polling
        self.state.polling_active = True
        
        self.logger.info(f"Task started for {self.delegatee_name}")
        return True
    
    async def check_progress(self) -> Optional[str]:
        """
        Check progress and relay to delegator if needed.
        
        Returns:
            Summary message for delegator if there are new messages, None otherwise
        """
        if not self.state:
            return None
        
        # If a previous completion handoff failed, retry it before fetching new messages
        if self.state.completion_handoff_pending:
            self.logger.info("Retrying pending completion handoff")
            recent = self.state.messages_collected[-5:] if self.state.messages_collected else []
            summary = self._compose_progress_summary(recent, True)
            try:
                await self._finalize_delegation(summary)
            except Exception as e:
                self.logger.error(
                    f"Completion handoff retry raised exception (will retry next cycle): {e}",
                    exc_info=True
                )
            return None
            
        # Diagnostic: log poll cycle state
        import time
        poll_ts = time.time()
        self.logger.info(
            f"[POLL] Cycle start (t={poll_ts:.0f}), batch_token={self.state.last_batch_token[:30] if self.state.last_batch_token else 'None'}, "
            f"processed={len(self.state.processed_event_ids)}, seen_old={len(self.state.seen_old_event_ids)}, "
            f"collected={len(self.state.messages_collected)}"
        )
        
        # Fetch new messages using batch token (NOT event_id)
        from_token = self.state.last_batch_token
        chunk, end_token = await self.client.get_messages(
            self.state.delegatee_room_id,
            from_token=from_token,
            limit=50
        )
        
        messages = chunk if chunk else []
        if not messages:
            return None
        
        # Build set of senders to filter out (CRITICAL: must include the configured delegator)
        configured_sender = self.config['matrix']['user_id']  # the configured delegator
        task_sender = self.state.task_sender_user_id or configured_sender
        
        # CRITICAL: Always filter out delegator (the configured delegator) to prevent false completion detection
        filtered_senders = {configured_sender}
        if task_sender and task_sender != configured_sender:
            filtered_senders.add(task_sender)
        
        self.logger.debug(f"Filtering messages from senders: {filtered_senders}")
            
        # Process messages - skip messages from filtered senders and already-processed messages
        new_messages = []
        
        # CRITICAL: Snapshot which events were already tracked BEFORE this fetch.
        # Used to determine if we've caught up with history (all events in batch were known before).
        pre_fetch_seen_ids = self.state.processed_event_ids | self.state.seen_old_event_ids
        completion_detected = False
        
        for msg in messages:
            event_type = msg.get('type', '')
            event_id = msg.get('event_id', '')
            sender = msg.get('sender', '')

            # Skip redacted events (Hermes edits messages, causing originals to be redacted)
            if event_type == 'm.room.redacted':
                self.logger.debug(f"Skipping redacted event: {event_id}")
                self.state.processed_event_ids.add(event_id)  # Track to avoid re-fetching
                continue

            # Skip non-text events (membership changes, room config, etc.)
            if event_type not in ('m.room.message', 'm.text'):
                self.logger.debug(f"Skipping non-text event type {event_type}: {event_id}")
                self.state.processed_event_ids.add(event_id)  # Track to avoid re-fetching
                continue

            content = msg.get('content', {})
            body = content.get('body', '')

            # Skip messages with empty body — nothing meaningful to process
            if not body or not body.strip():
                self.logger.debug(f"Skipping empty-body message: {event_id}")
                self.state.processed_event_ids.add(event_id)
                continue

            # Skip if we've already processed this message
            if event_id in self.state.processed_event_ids:
                # CHECK FOR EDITS: Hermes may edit messages (same event_id, new content)
                # Use event_id_to_body for reliable comparison instead of reading from list
                last_known = self.state.event_id_to_body.get(event_id, '')

                if body and body != last_known:
                    # Message was edited - update with latest version in place
                    if event_id in self.state.event_id_to_index:
                        idx = self.state.event_id_to_index[event_id]
                        old_body = self.state.messages_collected[idx]['message']
                        self.state.messages_collected[idx]['message'] = body
                        self.state.messages_collected[idx]['timestamp'] = datetime.now().isoformat()
                        self.state.event_id_to_body[event_id] = body
                        self.logger.info(f"Message EDITED (event {event_id}): updated content for {sender}")

                        # Check completion marker in edited content
                        if '=== NO_MORE_ACTIONS ===' in body:
                            completion_detected = True
                            self.logger.warning(f"Completion marker detected in EDITED message from {sender}")
                    continue  # Don't add to new_messages - we updated in place

                self.logger.debug(f"Skipping already-processed message (no change): {event_id}")
                continue

            # CRITICAL: Skip messages from filtered senders (delegator/bot users)
            # This prevents false completion detection from the configured delegator messages
            if sender in filtered_senders:
                self.state.processed_event_ids.add(event_id)
                self.logger.debug(f"Skipping message from filtered sender {sender}: {event_id}")
                continue

            # CRITICAL: Skip messages sent BEFORE this task started
            # This prevents old messages from previous runs being reprocessed
            # Add a 2-second buffer for clock skew between local time.time() and Matrix server timestamps
            origin_server_ts = msg.get('origin_server_ts', 0) / 1000.0  # Convert to seconds
            if hasattr(self.state, 'task_start_timestamp') and self.state.task_start_timestamp:
                task_cutoff = self.state.task_start_timestamp - 2.0  # 2-second buffer
                if origin_server_ts < task_cutoff:
                    # Track in seen_old_event_ids so the all_already_processed check can advance
                    # the batch token past batches of purely old messages (Bug fix: prevents infinite
                    # re-fetching of the same stale batch every poll cycle).
                    self.state.seen_old_event_ids.add(event_id)
                    # Also track body for edit detection — Hermes may edit old messages.
                    self.state.event_id_to_body[event_id] = body
                    self.logger.debug(
                        f"Skipping OLD message from {sender} (ts={origin_server_ts:.1f} < cutoff={task_cutoff:.1f}): {event_id}"
                    )
                    continue

            # Mark as processed before we do anything else
            self.state.processed_event_ids.add(event_id)

            # Track body content for edit detection and record index mapping
            self.state.event_id_to_body[event_id] = body
            idx = len(self.state.messages_collected)
            self.state.event_id_to_index[event_id] = idx

            # Log all messages we process (for debugging)
            self.logger.info(f"Processing message from {sender}: {body[:100]}...")

            if '=== NO_MORE_ACTIONS ===' in body:
                completion_detected = True
                self.logger.warning(f"Completion marker detected in message from {sender}")

            new_messages.append({
                'timestamp': datetime.now().isoformat(),
                'sender': sender,
                'message': body
            })

            # Auto-recovery bookkeeping: any sign of life from the delegatee
            # (tool calls, text, completion markers) resets the silence timer.
            # Hermes "stuck" status lines (retry/throbber/empty-response) do
            # NOT count as activity — they're evidence of being wedged. See
            # _counts_as_activity for the full filter.
            if self._counts_as_activity(body):
                self.state.last_substantive_msg_time = time.time()
                if self.state.idle_ping_count > 0:
                    self.logger.info(
                        f"Activity from {sender} after {self.state.idle_ping_count} pings — "
                        f"resetting idle counter."
                    )
                    self.state.idle_ping_count = 0
                # New activity also clears the one-shot "fall out of loop"
                # alert so the next silence window can re-arm the alert.
                if self.state.fall_out_of_loop_alerted:
                    self.logger.info(
                        f"Activity from {sender} cleared fall-out-of-loop alert."
                    )
                    self.state.fall_out_of_loop_alerted = False
        
        # Summarize this poll cycle's results
        total_fetched = len(messages)
        old_skipped = len([m for m in messages if m.get('event_id', '') in self.state.seen_old_event_ids])
        self.logger.info(
            f"[POLL] Fetched {total_fetched} msgs: new={len(new_messages)}, "
            f"old_skipped={old_skipped}, completion_detected={completion_detected}"
        )
        
        # Only update batch token if we actually processed at least one message.
        # If all messages were filtered out (too old, duplicate, or from filtered sender),
        # keeping the same token ensures we don't paginate past new messages on the next poll.
        # EXCEPTION: if ALL fetched events were already-processed BEFORE this batch, advance anyway —
        # staying stuck would waste API calls re-fetching the same batch forever.
        all_seen_ids = self.state.processed_event_ids | self.state.seen_old_event_ids
        all_already_processed = (
            len(messages) > 0 and
            all(msg.get('event_id', '') in all_seen_ids for msg in messages)
        )
        
        # FIX: After the first poll, we've already seen the most recent messages.
        # Don't keep scrolling backward through history — clear the batch token so
        # we always poll the latest 50 messages. New delegatee messages will appear
        # at the front of that batch. Old messages are efficiently skipped via
        # task_start_timestamp filter and seen_old_event_ids tracking.
        if not self.state.history_catchup_done:
            self.state.history_catchup_done = True
            self.state.last_batch_token = None  # Clear token → next poll fetches latest 50
            self.logger.warning(
                f"*** History catchup complete — saw {len(self.state.seen_old_event_ids)} old messages, "
                f"{len(self.state.processed_event_ids)} tracked events. "
                f"Switching to latest-50 polling mode. ***"
            )
        
        # Ensure we always fetch the 50 most recent messages.
        # Old messages are skipped via task_start_timestamp filter and event_id tracking.
        if not self.state.history_catchup_done:
            # Still scrolling through history (shouldn't happen after fix above, but keep as fallback).
            if end_token:
                self.state.last_batch_token = end_token
                self.logger.info(
                    f"Batch token updated (scrolling history): new_msgs={len(new_messages)}, "
                    f"seen_old={len(self.state.seen_old_event_ids)}, "
                    f"token={end_token[:30] if end_token else None}..."
                )
        else:
            # Already caught up — ensure token is cleared.
            if self.state.last_batch_token is not None:
                self.state.last_batch_token = None
                self.logger.info(
                    f"Batch token CLEARED (catchup done) — next poll will fetch latest 50 messages"
                )
        
        self.state.messages_collected.extend(new_messages)
        
        if not new_messages:
            return None
            
        summary = self._compose_progress_summary(new_messages, completion_detected)
        
        # If completion detected, finalize (sets completion_handoff_pending if send fails)
        if completion_detected:
            self.logger.warning(
                f"*** COMPLETION DETECTED for {self.delegatee_name} — "
                f"{len(self.state.messages_collected)} messages collected, starting handoff ***"
            )
            self.state.completion_handoff_pending = True
            summary = self._compose_progress_summary(new_messages, True)
            try:
                await self._finalize_delegation(summary)
            except Exception as e:
                self.logger.error(
                    f"Finalization raised exception (will retry next cycle): {e}", exc_info=True
                )
                # completion_handoff_pending is already True — will retry on next check_progress()
            return None
        
        # Periodic progress reports are handled by _send_periodic_progress_report() in run().
        # Don't send intermediate relays here — they conflict with the periodic timer.
        return summary
    
    # Hermes adds display-only emoji prefixes to messages it posts to a chat room
    # to signal what the agent is doing (tool calls, throbbers, retry chatter).
    # When these messages are echoed back to a *different* agent (the delegator)
    # inside a user-role message, they confuse the model — it sees what looks
    # like its own tool-call markers in user content. We strip them out of
    # progress + handoff messages so the delegator only sees substantive text.
    _HERMES_DISPLAY_PREFIXES = (
        '💻 ',     # terminal
        '✍️ ',     # write_file
        '🔧 ',     # patch / tool generic
        '📖 ',     # read_file
        '📚 ',     # skill_view / skill load
        '⏳ ',     # progress throbber
        '📋 ',     # todo
        '⚠️ ',     # warning / retry / empty-after-tool-calls
        '↻ ',     # thinking-only retry
        '💾 ',     # self-improvement
        '* 💻',
        '* ✍️',
        '* 📖',
        '* 🔧',
        '* 📚',
        '⚡ ',     # interrupt
        '[CONTEXT COMPACTION ',  # Hermes compaction handoff summary (otherwise propagates to delegator and cascades)
    )

    @classmethod
    def _is_substantive_message(cls, body: str) -> bool:
        """Filter Hermes display chatter from delegatee message lists.

        Returns True for actual content (text, completion summaries, error reports);
        False for tool-call display lines, throbbers, retry notices.

        Use ONLY for handoff/progress summaries (deciding what to forward to the
        delegator). For "is the delegatee still alive?" use ``_counts_as_activity``.
        """
        if not body or not body.strip():
            return False
        b = body.strip()
        # Strip leading edit marker '* ' that Hermes prepends to edited messages
        if b.startswith('* '):
            b = b[2:].lstrip()
        for prefix in cls._HERMES_DISPLAY_PREFIXES:
            if b.startswith(prefix):
                return False
        return True

    # Hermes-emitted "the model is stuck" status messages. Seeing only these
    # in the room means the LLM is wedged — auto-ping should NOT reset on them.
    #
    # IMPORTANT: ``⏳ Still working...`` is NOT in this list — Hermes only emits
    # it when the agent has progressed an iteration. It commonly carries a
    # counter like ``iteration 5/200`` that increments on each tick. When the
    # delegatee is using a subagent (which has no chat-room presence), this is
    # the *only* signal of progress, so we MUST count it as activity.
    _HERMES_STUCK_PREFIXES = (
        '⏳ Retrying',
        '⏳ Waiting',
        '⏳ Server loading',
        '⚠️ Empty response from model',
        '⚠️ Model produced reasoning but no visible response',
        '⚠️ Model returned empty after tool calls',
        '⚠️ Response truncated',
        '⚠️ Max retries',
        '⚠️ Gateway shutting down',
        '⚠️ The model returned no response',
        '⚠ Stream stalled',     # Hermes prints this with VS-16-stripped warning sign
        '⚠️ Stream stalled',
        '↻ Thinking-only response',
        '❌ ',
    )

    @classmethod
    def _counts_as_activity(cls, body: str) -> bool:
        """Return True if this message is evidence the delegatee is still doing
        useful work (tool calls, text, completion markers).

        Used by the auto-ping logic to reset the silence timer. Distinct from
        ``_is_substantive_message`` which is for delegator-facing summaries.
        Tool-call display lines (`💻 terminal`, `✍️ write_file`, etc.) DO count
        as activity — they prove the coder is actively running tools — but they
        are NOT forwarded to the delegator (that goes through the substantive
        filter). Hermes "stuck" status messages do NOT count as activity.
        """
        if not body or not body.strip():
            return False
        b = body.strip()
        if b.startswith('* '):
            b = b[2:].lstrip()
        for prefix in cls._HERMES_STUCK_PREFIXES:
            if b.startswith(prefix):
                return False
        return True

    def _compose_progress_summary(self, messages: list, completion_detected: bool) -> str:
        """Compose a progress summary message for the delegator."""
        # Build full summary first, then truncate total to max bytes (1KB)
        max_bytes = self.state.max_update_bytes if self.state else 1024

        # Deduplicate messages (keep latest version of each unique body), then
        # filter out Hermes display chatter so the delegator only sees real
        # content from the delegatee.
        deduped = self.deduplicate_messages(messages)
        substantive = [m for m in deduped if self._is_substantive_message(m.get('message', ''))]

        # Take only the most recent 3 substantive messages (smaller is better —
        # large echo-backs of tool history poison the delegator's next turn).
        recent_messages = substantive[-3:] if substantive else deduped[-2:]

        # Build the message list part. Scrub any literal `=== NO_MORE_ACTIONS ===`
        # tokens from message bodies — they confuse some chat templates into
        # treating the user-role payload as an end-of-turn marker.
        msg_list = "\n".join([
            f"[{m['timestamp']}] {m['message'].replace('=== NO_MORE_ACTIONS ===', '[completion-signalled]')}"
            for m in recent_messages
        ])

        # Build full summary
        if completion_detected:
            decision_block = (
                "---\n"
                "Completion signal seen (`=== NO_MORE_ACTIONS ===` or `[completion-signalled]`).\n"
                "Verify the delegatee's deliverables now: read artifacts, run `project_recall`, "
                "check acceptance criteria, post a 4-6 line summary, decide next step."
            )
        else:
            decision_block = (
                "---\n"
                "Choices:\n"
                "  - wait — reply with one short text line, no tool calls\n"
                "  - ping — send a bare `continue` prompt (use when delegatee looks idle but not stuck)\n"
                "  - correct — send `/steer` with a single sharp instruction\n"
                "  - stop — cancel the delegatee (only if clearly off-track)\n\n"
                "Decide without calling any tools. If unsure, wait."
            )
        full_summary = f"""**Delegation Update: {self.delegatee_name}**
{"COMPLETION DETECTED" if completion_detected else "In Progress"}

Recent delegatee output (Hermes display chatter filtered):

{msg_list}

{decision_block}"""

        # Truncate the entire summary to max bytes
        return self._truncate_to_bytes(full_summary, max_bytes)
    
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

    @staticmethod
    def deduplicate_messages(messages: list) -> list:
        """Deduplicate messages by body content, keeping the LATEST occurrence.

        When Hermes edits messages or the delegatee sends similar status updates,
        we want only the most recent version of each unique message body.

        Args:
            messages: List of dicts with at least 'message' key (body text).

        Returns:
            New list with duplicates removed. For identical bodies, only the last
            occurrence is kept, preserving overall chronological order.
        """
        # Build mapping: body -> latest index
        seen_bodies: Dict[str, int] = {}
        for idx, m in enumerate(messages):
            seen_bodies[m['message']] = idx

        # Collect indices to keep (the latest of each unique body)
        keep_indices = set(seen_bodies.values())

        # Return messages at kept indices, preserving original order
        return [m for i, m in enumerate(messages) if i in keep_indices]
    
    async def _finalize_delegation(self, final_summary: str):
        """Finalize a completed delegation and hand off to delegator."""
        self.logger.info(
            f"Finalizing delegation for {self.delegatee_name}, "
            f"{len(self.state.messages_collected)} messages collected"
        )
        
        # Safety net: deduplicate messages_collected by body content (keep LATEST occurrence)
        self.state.messages_collected = self.deduplicate_messages(self.state.messages_collected)
        self.logger.info(f"After dedup: {len(self.state.messages_collected)} unique messages")

        # Filter out Hermes display chatter (tool-call notification lines etc.) — those
        # are display-only artifacts of the delegatee's chat UI and confuse the
        # delegator model when it sees them in a user-role message.
        substantive = [
            m for m in self.state.messages_collected
            if self._is_substantive_message(m.get('message', ''))
        ]
        # Keep at most the last 5 substantive turns. The delegator should NOT
        # need to replay the delegatee's full conversation — a brief outcome
        # summary is enough to plan the next step.
        if substantive:
            picked = substantive[-5:]
        else:
            # Fallback: if everything was filtered, use the last few raw entries
            # so the delegator still has *some* signal.
            picked = self.state.messages_collected[-3:]

        # Strip out any occurrences of the literal completion marker from
        # message bodies before forwarding. Some chat templates (qwopus) treat
        # `=== NO_MORE_ACTIONS ===` as an end-of-turn token even inside a
        # user-role message, causing the next assistant turn to emit EOS
        # immediately. Replace with a plain phrase.
        def _scrub(s: str) -> str:
            return s.replace('=== NO_MORE_ACTIONS ===', '[completion-signalled]')

        all_messages = "\n\n".join([
            f"[{m['timestamp']}] {_scrub(m['message'])}"
            for m in picked
        ])

        handoff_message = f"""**Delegation handoff: {self.delegatee_name}**

The delegatee has finished its action loop.

Last substantive output from the delegatee (Hermes display chatter filtered out):

{all_messages}

---
Control is back with the delegator. Verify the result against the original task acceptance criteria, then either:
  - delegate the next task with start_task, or
  - issue a correction with correct (sends /steer), or
  - if the goal is met, summarize for the user.

If the delegatee's last output is itself a QUESTION (rather than a result),
answer it by starting a NEW delegation with the answer + any follow-up
instructions baked into the task spec."""

        self.logger.info(
            f"Sending handoff to delegator room {self.state.delegator_room_id}, "
            f"message length: {len(handoff_message)} chars"
        )

        # Use the outbox helper so delivery respects delegator-busy backpressure.
        # If the delegator is busy, the message gets spooled to disk and will be
        # flushed on the next poll cycle when the delegator goes idle. If it's
        # idle, the message goes through immediately. Worker stays alive via
        # completion_handoff_pending until the outbox confirms delivery.
        sent = await self._send_to_delegator(handoff_message, kind='handoff')

        if sent:
            # Only stop polling after successful delivery
            self.state.polling_active = False
            self.state.completion_handoff_pending = False  # Clear pending flag
            self.running = False
            self.logger.warning(f"*** Delegation for {self.delegatee_name} FINALIZED — worker stopping ***")
        else:
            # Either the delegator was busy (message queued) or the send threw.
            # Either way, keep worker alive — completion_handoff_pending stays
            # True and the next poll cycle will re-call this method.
            self.logger.info(
                f"Handoff for {self.delegatee_name} queued (delegator busy "
                f"or transient error); worker keeps polling and will retry."
            )
    
    async def correct(self, correction_message: str):
        """Issue a correction to the delegatee."""
        if not self.state:
            raise ValueError(f"No active delegation for {self.delegatee_name}")
            
        correction = f"/steer {correction_message}"
        await self.client.send_message(self.state.delegatee_room_id, correction)
        self.logger.info(f"Correction sent to {self.delegatee_name}: {correction_message}")
    
    async def stop(self):
        """Stop the running delegation."""
        if not self.state:
            self.logger.warning(f"No active delegation for {self.delegatee_name}")
            return
            
        # Send stop prompt
        stop_message = "Stop all tasks and wait for further instructions."
        await self.client.send_message(self.state.delegatee_room_id, stop_message)
        
        # Clean up state
        self.state.polling_active = False
        
        # Notify delegator
        notification = f"""🛑 **Delegation Stopped**

Delegation: {self.delegatee_name}

The delegation has been stopped. Delegatee has been notified."""
        
        await self.client.send_message(self.state.delegator_room_id, notification)
        
        self.state = None
        self.logger.info(f"Delegation for {self.delegatee_name} stopped")
    
    async def ping(self):
        """Send continue prompt to delegatee."""
        if not self.state:
            raise ValueError(f"No active delegation for {self.delegatee_name}")
            
        continue_message = "Continue with the task. If finished or delegator action is required, answer with summary adding `=== NO_MORE_ACTIONS ===` (exact wording!)."""
        
        await self.client.send_message(self.state.delegatee_room_id, continue_message)
        self.logger.info(f"Ping sent to {self.delegatee_name}")
    
    async def _send_periodic_progress_report(self):
        """Send periodic progress report to delegator with all messages since last report.

        CRITICAL: This is a HEARTBEAT — it MUST fire every 15 minutes regardless of
        whether there are new messages. If there are no new messages, the delegator
        needs to know the delegatee might be stuck and can choose to ping or stop.
        """
        if not self.state or not self.state.polling_active:
            self.logger.debug(f"Skipping periodic report for {self.delegatee_name}: task finalized or no state")
            return

        # Collect ALL messages in messages_collected that are newer than last report time
        new_messages = [
            m for m in self.state.messages_collected
            if datetime.fromisoformat(m['timestamp']).timestamp() >= self.state.last_progress_report_time
        ]

        self.logger.debug(
            f"Periodic report check: {len(self.state.messages_collected)} total messages, "
            f"{len(new_messages)} since last report (cutoff: {self.state.last_progress_report_time:.1f})"
        )

        # Deduplicate (already deduped in check_progress, but be safe)
        deduped = self.deduplicate_messages(new_messages)

        # Compose summary — always send a heartbeat, even with zero new messages
        summary = self._compose_periodic_summary(deduped)

        delivered = await self._send_to_delegator(summary, kind='progress')
        if delivered:
            # Only update timer when we actually delivered the message.
            self.state.last_progress_report_time = time.time()
            self.logger.info(
                f"Periodic progress report delivered for {self.delegatee_name}: "
                f"{len(deduped)} messages"
            )
        else:
            self.logger.info(
                f"Delegator busy — periodic report for {self.delegatee_name} "
                f"queued ({len(deduped)} messages will retry next cycle)"
            )

    def _compose_periodic_summary(self, messages: list) -> str:
        """Compose periodic progress summary (different from check_progress summary).

        When there are no new messages, sends a clear STUCK alert so the delegator
        knows the delegatee may need a ping or the task may need to be stopped.
        """
        elapsed = datetime.now() - self.state.created_at

        if messages:
            msg_list = "\n".join([
                f"[{m['timestamp']}] {m['message']}"
                for m in messages
            ])
            summary = f"""📊 **Periodic Progress: {self.delegatee_name}**

Elapsed: {elapsed}
Messages collected: {len(messages)}

{msg_list}

---
*Task still in progress. Delegator can ping, correct, or stop.*"""
        else:
            # No new activity — alert the delegator that the delegatee appears stuck
            summary = f"""⚠️ **NO ACTIVITY — Delegatee appears stuck: {self.delegatee_name}**

Elapsed: {elapsed}
No new messages since last report.

The delegatee has not produced any output in the last 15 minutes.
This may mean the delegatee is stuck, waiting for input, or the task is complete but `=== NO_MORE_ACTIONS ===` was not sent.

**Options:**
- **Ping** the delegatee to prompt it to continue
- **Correct** the delegatee with additional instructions
- **Stop** the delegation and handle manually
- **Wait** — some tasks take a long time"""

        return self._truncate_to_bytes(summary, self.state.max_update_bytes * 3)  # Allow more for periodic reports

    async def run(self):
        """Main polling loop with two concurrent timers:
        
        1. Completion poll (every poll_interval_sec, default 15s): Check for new messages, detect === NO_MORE_ACTIONS ===
        2. Progress report (every progress_report_interval_sec, default 900s): Collect all messages since last report, send to delegator if idle
        """
        self.running = True
        
        # Process pending startup task if any
        if hasattr(self, '_pending_startup_task') and self._pending_startup_task:
            task_file = self._pending_startup_task
            startup_file = self._pending_startup_file
            
            try:
                await self.start_task(task_file)
                # Remove the startup file after successful loading
                if startup_file.exists():
                    startup_file.unlink()
                self.logger.info(f"Initialized with startup task from delegator")
            except Exception as e:
                self.logger.error(
                    f"Failed to process startup task: {e}", exc_info=True
                )
        
        poll_interval = self.poll_interval_sec
        report_interval = self.progress_report_interval_sec
        
        self.logger.info(
            f"Worker started for {self.delegatee_name}, "
            f"poll interval: {poll_interval}s, "
            f"report interval: {report_interval}s"
        )
        
        poll_counter = 0
        report_cycles = max(report_interval // poll_interval, 1)
        iteration = 0

        # Auto-recovery / fall-out-of-loop thresholds.
        #
        # The delegatee may be doing legitimate long work via subagents, in which
        # case the only chat-room signal is Hermes's ``⏳ Still working...
        # (10 min elapsed — iteration N/M, ...)`` heartbeat that fires every
        # ~10 minutes. Those heartbeats now count as activity (see
        # ``_HERMES_STUCK_PREFIXES``), so they reset the timer naturally.
        #
        # FALL_OUT_THRESHOLD: shorter than the auto-ping (~5 min) — when this
        # fires we send one diagnostic alert to the delegator (via outbox/
        # backpressure) BEFORE the next periodic-progress tick so the
        # delegator finds out about the silence within ~5 min instead of the
        # 15-min progress-report cadence. We do NOT escalate-and-stop here;
        # this is purely a notification.
        #
        # IDLE_PING_THRESHOLD: longer (12 min) — past one Hermes heartbeat
        # tick so a single missed heartbeat alone won't trigger a false ping.
        FALL_OUT_THRESHOLD = 300.0    # 5 minutes — quick "did the delegatee die?" signal
        IDLE_PING_THRESHOLD = 720.0   # 12 minutes — past one Hermes heartbeat tick
        IDLE_PING_DEBOUNCE = 300.0    # don't ping more than once every 5 min
        IDLE_PING_MAX = 3             # up to 3 pings then escalate-and-stop

        while self.running and self.state and self.state.polling_active:
            iteration += 1
            try:
                self.logger.debug(f"Polling iteration {iteration} for {self.delegatee_name}")

                # Reconcile pause state from the on-disk marker file FIRST so
                # all subsequent checks (silence, periodic, auto-ping, fall-
                # out-of-loop) honor the current pause window. Keep going
                # through check_progress() even while paused so that if the
                # delegatee posts NO_MORE_ACTIONS while paused, we still see
                # it (it'll be queued via outbox until resume + idle).
                self._sync_pause_state()

                result = await self.check_progress()
                if result:
                    self.logger.info(f"Progress update: {result[:100]}...")

                # Skip all timer-based actions while paused.
                if self.state and self.state.polling_active and not self.state.paused:
                    # Pause-aware silence (already subtracts paused_duration_total).
                    silence = self._adjusted_silence()

                    # ----- Fall-out-of-loop recovery (5 min, state-gated) -----
                    # If the delegatee has been silent past FALL_OUT_THRESHOLD
                    # AND the delegatee's daemon reports idle (active_agents==0),
                    # the agent has actually fallen out of its tool-call loop —
                    # send the same `continue or NMA` ping to the delegatee
                    # directly (no delegator alert, no extra noise).
                    #
                    # If silence > threshold but `is_delegatee_idle()` returns
                    # False, the daemon is still running a turn (long vision /
                    # reasoning step). In that case do nothing — the model is
                    # actually working, just not posting tool calls.
                    if (silence >= FALL_OUT_THRESHOLD
                            and not self.state.completion_handoff_pending
                            and not self.state.fall_out_of_loop_alerted):
                        delegatee_idle = self.is_delegatee_idle()
                        if not delegatee_idle:
                            self.logger.debug(
                                f"[FALL-OUT] Silence={int(silence)}s but "
                                f"delegatee daemon still busy (active_agents>0) — "
                                f"skipping (model is working)."
                            )
                        else:
                            ping_text = (
                                "Continue with the task. If finished or delegator "
                                "action is required, answer with summary adding "
                                "`=== NO_MORE_ACTIONS ===` (exact wording!)."
                            )
                            try:
                                await self.client.send_message(
                                    self.state.delegatee_room_id, ping_text
                                )
                                self.state.fall_out_of_loop_alerted = True
                                self.state.idle_ping_count += 1
                                self.state.last_auto_ping_time = time.time()
                                self.logger.warning(
                                    f"[FALL-OUT] Delegatee idle (active_agents=0) "
                                    f"after {int(silence)}s silence — sent continue/"
                                    f"NMA ping directly to {self.delegatee_name}."
                                )
                            except Exception as fout_err:
                                self.logger.error(
                                    f"Fall-out ping to delegatee failed: {fout_err}",
                                    exc_info=True,
                                )

                    # ----- Auto-ping recovery (12 min, up to 3 retries) -----
                    if not self.state.completion_handoff_pending:
                        now = time.time()
                        debounce_ok = (now - (self.state.last_auto_ping_time or 0.0)) >= IDLE_PING_DEBOUNCE
                        if (silence >= IDLE_PING_THRESHOLD
                                and self.state.idle_ping_count < IDLE_PING_MAX
                                and debounce_ok):
                            ping_text = (
                                "Are you still working? You've been silent for "
                                f"~{int(silence)}s. If you're stuck, summarise where you are; "
                                "if you're done, write `=== NO_MORE_ACTIONS ===` exactly. "
                                "Otherwise emit your next tool call now."
                            )
                            try:
                                await self.client.send_message(self.state.delegatee_room_id, ping_text)
                                self.state.idle_ping_count += 1
                                self.state.last_auto_ping_time = now
                                self.logger.warning(
                                    f"[AUTO-PING {self.state.idle_ping_count}/{IDLE_PING_MAX}] "
                                    f"Sent idle ping after {int(silence)}s silence."
                                )
                            except Exception as ping_err:
                                self.logger.error(
                                    f"Auto-ping send failed: {ping_err}", exc_info=True
                                )
                        elif (silence >= IDLE_PING_THRESHOLD
                                and self.state.idle_ping_count >= IDLE_PING_MAX
                                and debounce_ok):
                            # Exhausted all auto-pings — escalate to the delegator and stop.
                            alert = (
                                f"⚠️ **Delegatee wedged: {self.delegatee_name}**\n\n"
                                f"The delegatee has produced no substantive output for "
                                f"{int(silence)}s and has not responded to {IDLE_PING_MAX} "
                                "automatic pings. The background worker is stopping this "
                                "delegation.\n\n"
                                "Decide next steps: rewrite the task into a smaller chunk and "
                                "`start_task` again, or escalate to the user."
                            )
                            # Force the wedged alert through even if delegator
                            # is busy — the worker is about to die and we
                            # can't queue past the daemon's lifetime cleanly
                            # for this terminal-state notice.
                            try:
                                await self._send_to_delegator(
                                    alert, kind='wedged', force=True,
                                )
                            except Exception as alert_err:
                                self.logger.error(
                                    f"Failed to deliver 'delegatee-wedged' alert "
                                    f"to delegator: {alert_err}",
                                    exc_info=True,
                                )
                            self.state.polling_active = False
                            self.running = False
                            self.logger.warning(
                                f"*** Delegation for {self.delegatee_name} STOPPED — "
                                f"delegatee unresponsive after {IDLE_PING_MAX} auto-pings ***"
                            )

                # Every report_interval seconds, send periodic progress report.
                # Guard: skip if check_progress() just finalized the task
                # (polling_active=False) OR if currently paused.
                if (poll_counter >= report_cycles and self.state
                        and self.state.polling_active and not self.state.paused):
                    await self._send_periodic_progress_report()
                    poll_counter = 0

            except Exception as e:
                self.consecutive_errors += 1
                self.logger.error(
                    f"Error in polling loop ({self.consecutive_errors} consecutive, "
                    f"iteration {iteration}): {e}", exc_info=True
                )

                # Circuit breaker: alert delegator after max consecutive errors.
                # Suppress while paused — errors during a held delegation are
                # expected (token revoked etc. while we wait, or network blip)
                # and we don't want to spam the delegator with worker noise
                # during a planned pause.
                if (self.consecutive_errors >= self.max_consecutive_errors and
                        self.state and hasattr(self.state, 'delegator_room_id')
                        and not self.state.paused):
                    alert = (
                        f"🚨 **Worker Health Alert: {self.delegatee_name}**\n\n"
                        f"The background worker has encountered "
                        f"{self.consecutive_errors} consecutive errors.\n"
                        f"Last error: {e}\n\n"
                        f"Please check logs or restart the delegation."
                    )
                    # Force health alert: if the worker is failing repeatedly
                    # the delegator needs to know now, not after the next
                    # idle window which may never come.
                    try:
                        await self._send_to_delegator(
                            alert, kind='health', force=True,
                        )
                    except Exception as alert_err:
                        self.logger.error(
                            f"Failed to deliver 'worker health' alert: "
                            f"{alert_err}",
                            exc_info=True,
                        )
            finally:
                # CRITICAL: Always increment poll_counter regardless of success/failure.
                # If this is skipped on exceptions, periodic reports stop triggering.
                # Don't advance the counter while paused — pause should not
                # consume the delegator's 15-min reporting budget.
                if not (self.state and self.state.paused):
                    poll_counter += 1

            # Wait for next check
            await asyncio.sleep(poll_interval)
        
        self.logger.info(f"Worker stopped for {self.delegatee_name} after {iteration} iterations")


async def main():
    """Main entry point - run worker for a specific delegatee."""
    if len(sys.argv) < 2:
        print("Usage: python delegation_worker.py <delegatee-name>", file=sys.stderr)
        sys.exit(1)
    
    delegatee_name = sys.argv[1]
    
    # Find config file
    config_path = None
    for p in path_utils.config_search_paths():
        if p.exists():
            config_path = p
            break
    
    if not config_path:
        searched = "\n".join(f"  {p}" for p in path_utils.config_search_paths())
        example = Path(__file__).parent.parent / 'config.example.yaml'
        print(
            f"delegator-delegatee.yaml not found. Report this to the user — "
            f"the config must be created by a human before this skill can be used.\n\n"
            f"Searched:\n{searched}\n\n"
            f"Template for the user: {example}",
            file=sys.stderr,
        )
        sys.exit(1)
    
    # Get config directory for log files
    config_dir = config_path.parent
    
    # Setup logging with PID-based filename
    pid = os.getpid()
    logger = setup_logging(config_dir, pid)
    
    worker = DelegationWorker(str(config_path), delegatee_name, logger)
    
    # Check if delegatee exists
    if not worker.get_delegatee_config():
        print(f"Delegatee '{delegatee_name}' not found in configuration", file=sys.stderr)
        sys.exit(1)
    
    logger.info(f"Delegation Worker initialized for {delegatee_name}")
    logger.info(f"Config loaded from: {config_path}")
    logger.info(f"Log file: {config_dir / f'delegator-delegatee-{pid}.log'}")
    
    # Run the polling loop.
    #
    # Top-level catch-all logs any exception that escapes the inner handlers
    # before the process exits.  Without this, a silently-dying daemon
    # leaves no trace in its own log — the bug that hid the 2026-05-12 T3
    # completion-handoff loss (orch never saw coder's =/= NO_MORE_ACTIONS =/=
    # because the daemon had quietly exited 1h47m earlier with no log entry).
    try:
        await worker.run()
    except KeyboardInterrupt:
        logger.info("Worker interrupted (KeyboardInterrupt)")
        await worker.stop()
    except Exception:
        logger.exception(
            "Worker died with unhandled exception — process will now exit. "
            "Check the traceback above; rerun `start_task` to recover."
        )
        try:
            await worker.stop()
        except Exception:
            logger.exception("worker.stop() also failed during cleanup")
        raise


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Clean exit on Ctrl-C / SIGINT — already logged inside main().
        sys.exit(130)
    except Exception:
        # Last-ditch logger: if `main()` couldn't set up the file logger,
        # at least the systemd journal sees the traceback.
        import logging as _logging
        _logging.basicConfig(level=_logging.ERROR)
        _logging.exception(
            "delegation_worker top-level: unhandled exception escaped "
            "asyncio.run(main()). Process is exiting."
        )
        sys.exit(1)
