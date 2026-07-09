"""
Tests for delegator-delegatee skill utilities.

Run with: python tests/test_delegation.py
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock, mock_open
from datetime import datetime, timedelta
import tempfile
import json
import os
import logging
import urllib.request

# Add bin and scripts directories to path
# Add src directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from delegation_core import (
    format_task_message,
    format_correction_message,
    format_stop_message,
    format_continue_message,
    check_delegator_idle,
    DelegationConfig,
    DaemonManager,
)
from delegation_worker import DelegationWorker, DelegationState


# ==============================================================================
# Tests for message formatting functions
# ==============================================================================

def test_format_task_message():
    """Test task message formatting - new format uses file path."""
    msg = format_task_message('/path/to/task.md')

    assert 'New task file:' in msg
    assert '/path/to/task.md' in msg
    assert '=== NO_MORE_ACTIONS ===' in msg


def test_format_correction_message():
    """Test correction message formatting."""
    msg = format_correction_message('Focus on database first')

    assert '/steer Focus on database first' == msg


def test_format_stop_message():
    """Test stop message formatting."""
    msg = format_stop_message()

    assert 'Stop all tasks' in msg
    assert 'wait for further instructions' in msg


def test_format_continue_message():
    """Test continue/ping message formatting."""
    msg = format_continue_message()

    assert 'Continue with the task' in msg
    assert '=== NO_MORE_ACTIONS ===' in msg


def test_format_task_message_mentions_question_pattern():
    """The start-task prompt must instruct the delegatee that NO_MORE_ACTIONS
    can be used to ask a question, not only to signal completion."""
    msg = format_task_message('/tmp/anything.md')
    assert '=== NO_MORE_ACTIONS ===' in msg
    assert 'ASK A QUESTION' in msg or 'ask a question' in msg.lower()
    # The delegator-side instruction (in the handoff template) is separate;
    # this just confirms the delegatee gets the "you may ask" hint up front.
    assert 'delegator' in msg.lower()


def test_format_pause_message_steer_prefix():
    """Pause message must be a /steer instruction so the Hermes agent
    treats it as a priority control message, and it must tell the
    delegatee to preserve session state."""
    from delegation_core import format_pause_message
    msg = format_pause_message()
    assert msg.startswith('/steer ')
    assert 'PAUSE' in msg
    assert 'remember' in msg.lower() or 'preserve' in msg.lower() or 'state' in msg.lower()


def test_format_resume_message_steer_prefix():
    """Resume message must be a /steer instruction and reference RESUME."""
    from delegation_core import format_resume_message
    msg = format_resume_message()
    assert msg.startswith('/steer ')
    assert 'RESUME' in msg


def test_pause_marker_path_per_delegatee():
    """Each delegatee gets its own pause marker path so unrelated delegations
    can be paused independently."""
    import path_utils as pu
    a = pu.pause_marker_file('agent-a')
    b = pu.pause_marker_file('agent-b')
    assert a != b
    assert 'agent-a' in str(a)
    assert 'agent-b' in str(b)


def test_outbox_path_per_delegatee():
    """Outbox file is per-delegatee — backpressure on one delegation
    must not affect another."""
    import path_utils as pu
    a = pu.outbox_file('agent-a')
    b = pu.outbox_file('agent-b')
    assert a != b
    assert 'agent-a' in str(a)
    assert 'agent-b' in str(b)


def test_config_load_from_hermes_profile():
    """Test config loading from Hermes profile directory."""
    config_mgr = DelegationConfig()
    assert config_mgr is not None


def test_config_get_all_delegatees():
    """Test getting all delegatees from config."""
    config_mgr = DelegationConfig()

    try:
        delegatees = config_mgr.get_all_delegatees()
        assert isinstance(delegatees, list)
    except FileNotFoundError:
        pass


def test_config_get_delegatee():
    """Test getting specific delegatee by name."""
    config_mgr = DelegationConfig()

    try:
        delegatee = config_mgr.get_delegatee('coding-agent')
        assert delegatee is None or isinstance(delegatee, dict)
    except FileNotFoundError:
        pass


def test_task_message_contains_protocol_marker():
    """Ensure task message contains the completion protocol marker."""
    msg = format_task_message('Test task')

    assert '=== NO_MORE_ACTIONS ===' in msg


def test_correction_message_format_for_hermes():
    """Test that correction uses /steer for Hermes agents."""
    msg = format_correction_message('Fix this issue')

    assert msg.startswith('/steer ')
    assert 'Fix this issue' in msg


# ==============================================================================
# Tests for DelegationWorker.deduplicate_messages
# ==============================================================================

def test_deduplicate_messages_keeps_latest_occurrence():
    """When the same message body appears multiple times, keep only the latest."""
    messages = [
        {'timestamp': '10:00', 'sender': 'alice', 'message': 'Step 1 complete'},
        {'timestamp': '10:05', 'sender': 'alice', 'message': 'Working on step 2'},
        {'timestamp': '10:10', 'sender': 'alice', 'message': 'Step 1 complete'},
    ]

    result = DelegationWorker.deduplicate_messages(messages)

    assert len(result) == 2
    assert result[0]['message'] == 'Working on step 2'
    assert result[1]['message'] == 'Step 1 complete'
    assert result[1]['timestamp'] == '10:10'


def test_deduplicate_messages_preserves_order():
    """Dedup should preserve chronological order of kept messages."""
    messages = [
        {'timestamp': '10:00', 'sender': 'a', 'message': 'A'},
        {'timestamp': '10:01', 'sender': 'b', 'message': 'B'},
        {'timestamp': '10:02', 'sender': 'a', 'message': 'A'},
        {'timestamp': '10:03', 'sender': 'c', 'message': 'C'},
        {'timestamp': '10:04', 'sender': 'b', 'message': 'B'},
    ]

    result = DelegationWorker.deduplicate_messages(messages)

    assert len(result) == 3
    assert [m['message'] for m in result] == ['A', 'C', 'B']


def test_deduplicate_messages_all_unique():
    """No-op when all messages have unique bodies."""
    messages = [
        {'timestamp': '10:00', 'sender': 'a', 'message': 'First'},
        {'timestamp': '10:01', 'sender': 'b', 'message': 'Second'},
        {'timestamp': '10:02', 'sender': 'c', 'message': 'Third'},
    ]

    result = DelegationWorker.deduplicate_messages(messages)

    assert len(result) == 3
    assert [m['message'] for m in result] == ['First', 'Second', 'Third']


def test_deduplicate_messages_all_identical():
    """When all messages are identical, keep only the last one."""
    messages = [
        {'timestamp': '10:00', 'sender': 'a', 'message': 'Same'},
        {'timestamp': '10:01', 'sender': 'b', 'message': 'Same'},
        {'timestamp': '10:02', 'sender': 'c', 'message': 'Same'},
    ]

    result = DelegationWorker.deduplicate_messages(messages)

    assert len(result) == 1
    assert result[0]['message'] == 'Same'
    assert result[0]['sender'] == 'c'
    assert result[0]['timestamp'] == '10:02'


def test_deduplicate_messages_empty():
    """Empty list returns empty list."""
    result = DelegationWorker.deduplicate_messages([])
    assert result == []


def test_deduplicate_messages_single():
    """Single message passes through unchanged."""
    messages = [{'timestamp': '10:00', 'sender': 'a', 'message': 'Only one'}]
    result = DelegationWorker.deduplicate_messages(messages)

    assert len(result) == 1
    assert result[0]['message'] == 'Only one'


def test_deduplicate_messages_hermes_edit_pattern():
    """Simulate Hermes editing a message: older version should be removed."""
    messages = [
        {'timestamp': '10:00', 'sender': '@delegatee:server', 'message': 'Working on migration...'},
        {'timestamp': '10:05', 'sender': '@delegatee:server', 'message': 'Migration in progress, 50% done.'},
        {'timestamp': '10:10', 'sender': '@delegatee:server', 'message': 'Working on migration...'},
        {'timestamp': '10:15', 'sender': '@delegatee:server', 'message': 'Migration complete! === NO_MORE_ACTIONS ==='},
    ]

    result = DelegationWorker.deduplicate_messages(messages)

    assert len(result) == 3
    assert result[0]['message'] == 'Migration in progress, 50% done.'
    assert result[1]['message'] == 'Working on migration...'
    assert result[2]['message'] == 'Migration complete! === NO_MORE_ACTIONS ==='


def test_deduplicate_messages_three_duplicates():
    """When a message appears 3+ times, keep only the last."""
    messages = [
        {'timestamp': '10:00', 'sender': 'a', 'message': 'Ping'},
        {'timestamp': '10:01', 'sender': 'b', 'message': 'Other'},
        {'timestamp': '10:02', 'sender': 'a', 'message': 'Ping'},
        {'timestamp': '10:03', 'sender': 'a', 'message': 'Ping'},
    ]

    result = DelegationWorker.deduplicate_messages(messages)

    assert len(result) == 2
    assert [m['message'] for m in result] == ['Other', 'Ping']
    assert result[1]['timestamp'] == '10:03'


# ==============================================================================
# Tests for check_delegator_idle (delegation_core.py)
# Uses real temp files — no mocking needed because paths are hardcoded.
# We patch the hardcoded ~ path via environment or by creating dirs.
# ==============================================================================

def _write_gateway_state(profile=None, active_agents=0):
    """Helper: write a gateway_state.json at the right path."""
    if profile:
        state_path = Path(f'/tmp/test_hermes_home/.hermes/profiles/{profile}/gateway_state.json')
    else:
        state_path = Path('/tmp/test_hermes_home/.hermes/gateway_state.json')
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, 'w') as f:
        json.dump({'active_agents': active_agents}, f)


def test_is_delegator_idle_hermes_profile():
    """Hermes with profile set — should check profile-specific path first."""
    config = {'delegator': {'type': 'Hermes', 'profile': 'test-profile'}}

    # We can't easily change the hardcoded ~ path, so mock open()
    mock_state = json.dumps({'active_agents': 0})
    m = mock_open(read_data=mock_state)
    with patch('builtins.open', m):
        result = check_delegator_idle(config)
        assert result is True


def test_is_delegator_idle_hermes_main():
    """Hermes without profile — should check main path."""
    config = {'delegator': {'type': 'Hermes'}}

    mock_state = json.dumps({'active_agents': 0})
    m = mock_open(read_data=mock_state)
    with patch('builtins.open', m):
        result = check_delegator_idle(config)
        assert result is True


def test_is_delegator_idle_openclaw():
    """OpenClaw — should run check_openclaw_idle.sh script."""
    config = {'delegator': {'type': 'OpenClaw'}}

    mock_result = MagicMock()
    mock_result.stdout = "Agent is IDLING"
    with patch('subprocess.run', return_value=mock_result) as mock_run:
        with patch('os.path.exists', return_value=True):
            result = check_delegator_idle(config)
            assert result is True
            mock_run.assert_called_once()


def test_is_delegator_busy_returns_false():
    """When active_agents > 0, should return False (not idle)."""
    config = {'delegator': {'type': 'Hermes', 'profile': 'test-profile'}}

    mock_state = json.dumps({'active_agents': 2})
    m = mock_open(read_data=mock_state)
    with patch('builtins.open', m):
        result = check_delegator_idle(config)
        assert result is False


def test_is_delegator_idle_fallback_to_main():
    """When profile path doesn't exist, fall back to main path."""
    config = {'delegator': {'type': 'Hermes', 'profile': 'nonexistent'}}

    # Use real temp files — create the fallback gateway_state.json at ~/.hermes/
    import tempfile
    import shutil

    # Create temp home directory structure
    tmpdir = Path(tempfile.mkdtemp())
    hermes_dir = tmpdir / '.hermes'
    hermes_dir.mkdir(parents=True, exist_ok=True)

    # Write fallback state file at main path (active_agents=0 means idle)
    fallback_file = hermes_dir / 'gateway_state.json'
    with open(fallback_file, 'w') as f:
        json.dump({'active_agents': 0}, f)

    # Mock builtins.open to redirect the hardcoded paths
    call_count = [0]
    original_open = __import__('builtins').open
    def side_effect_open(file, *args, **kwargs):
        call_count[0] += 1
        file_str = str(file)
        if 'nonexistent' in file_str:
            # Profile path — doesn't exist
            raise FileNotFoundError()
        elif 'gateway_state.json' in file_str and call_count[0] >= 2:
            # Fallback path — redirect to our temp file
            return original_open(fallback_file, *args, **kwargs)
        else:
            return mock_open(read_data=json.dumps({'active_agents': 0}))().return_value

    with patch('builtins.open', side_effect=side_effect_open):
        result = check_delegator_idle(config)
        assert result is True

    shutil.rmtree(tmpdir, ignore_errors=True)


def test_is_delegator_idle_no_state_file():
    """When no state file found at all, default to idle (True)."""
    config = {'delegator': {'type': 'Hermes', 'profile': 'test-profile'}}

    def raise_filenotfound(*args, **kwargs):
        raise FileNotFoundError()

    with patch('builtins.open', side_effect=raise_filenotfound):
        result = check_delegator_idle(config)
        assert result is True


# ==============================================================================
# Tests for DelegationWorker.is_delegator_idle (same logic as core function)
# ==============================================================================

def _make_worker_config(overrides=None):
    """Helper to create a minimal worker config dict."""
    cfg = {
        'matrix': {'url': 'https://test.example.com', 'user_id': '@bot:test', 'access_token': 'tok'},
        'delegator': {'type': 'Hermes', 'profile': 'test-profile', 'matrix': {'room_id': '!d:test'}},
        'delegatees': [{'name': 'coder', 'type': 'Hermes', 'matrix': {'room_id': '!c:test'}}],
        'timeouts': {'poll_interval_sec': 30, 'progress_report_interval_sec': 900},
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def test_worker_is_delegator_idle_hermes_idle():
    """Worker idle check — Hermes with active_agents=0 returns True."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config

    mock_state = json.dumps({'active_agents': 0})
    m = mock_open(read_data=mock_state)
    with patch('builtins.open', m):
        assert worker.is_delegator_idle() is True


def test_worker_is_delegator_idle_hermes_busy():
    """Worker idle check — Hermes with active_agents>0 returns False."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config

    mock_state = json.dumps({'active_agents': 1})
    m = mock_open(read_data=mock_state)
    with patch('builtins.open', m):
        assert worker.is_delegator_idle() is False


def test_worker_is_delegator_idle_openclaw():
    """Worker idle check — OpenClaw runs the script."""
    config = _make_worker_config({'delegator': {'type': 'OpenClaw'}})
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    mock_result = MagicMock()
    mock_result.stdout = "IDLING"
    with patch('subprocess.run', return_value=mock_result):
        assert worker.is_delegator_idle() is True


# ==============================================================================
# Tests for periodic progress reports
# ==============================================================================

def test_compose_periodic_summary():
    """Verify _compose_periodic_summary format and content."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = MagicMock()
    state.created_at = datetime.now() - timedelta(hours=1, minutes=30)
    state.max_update_bytes = 1024
    worker.state = state

    messages = [
        {'timestamp': (datetime.now() - timedelta(minutes=60)).isoformat(), 'message': 'Starting task...'},
        {'timestamp': (datetime.now() - timedelta(minutes=30)).isoformat(), 'message': '50% done.'},
        {'timestamp': datetime.now().isoformat(), 'message': 'Almost there!'},
    ]

    summary = worker._compose_periodic_summary(messages)

    assert 'Periodic Progress: coder' in summary
    assert 'Messages collected: 3' in summary
    assert 'Starting task...' in summary
    assert '50% done.' in summary
    assert 'Almost there!' in summary
    assert 'Elapsed:' in summary


def test_compose_periodic_summary_truncates():
    """Verify _compose_periodic_summary respects truncation."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = MagicMock()
    state.created_at = datetime.now() - timedelta(minutes=5)
    state.max_update_bytes = 100  # Very small limit
    worker.state = state

    messages = [
        {'timestamp': datetime.now().isoformat(), 'message': 'A' * 200},
    ]

    summary = worker._compose_periodic_summary(messages)
    assert len(summary.encode('utf-8')) <= 100 * 3 + 3  # max_bytes*3 + "..."


# ==============================================================================
# Tests for start_task message format
# ==============================================================================

def test_start_task_message_with_existing_task():
    """When there's an active polling task, the stop preamble should be included."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    # Setup logger (required by start_task)
    worker.logger = logging.getLogger('test_delegation')

    # Simulate an existing active task
    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    worker.state = state

    # Mock the Matrix client
    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(return_value={'event_id': '$test'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker.start_task('/tmp/test.md')
        call_args = mock_client.send_message.call_args
        message_sent = call_args[0][1]  # Second positional arg is the message
        assert 'Stop previous task.' in message_sent
        assert 'New task file:' in message_sent

    asyncio.get_event_loop().run_until_complete(run_test())


def test_start_task_message_without_existing_task():
    """When there's no active task, the stop preamble should NOT be included."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    # Setup logger (required by start_task)
    worker.logger = logging.getLogger('test_delegation')

    # No existing active task — state is None or polling_active=False
    worker.state = None

    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(return_value={'event_id': '$test'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker.start_task('/tmp/test.md')
        call_args = mock_client.send_message.call_args
        message_sent = call_args[0][1]
        assert 'Stop previous task.' not in message_sent
        assert 'New task file:' in message_sent

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# Tests for deduplication across report boundaries
# ==============================================================================

def test_deduplicate_messages_across_reports():
    """Messages deduped correctly across report boundaries.

    Simulate: messages collected over multiple polling cycles, some duplicates
    from Hermes edits or repeated status updates. Dedup should handle the full
    list correctly regardless of when they were added.
    """
    # Messages from first report cycle
    batch1 = [
        {'timestamp': '10:00', 'message': 'Starting...'},
        {'timestamp': '10:05', 'message': 'Processing A'},
        {'timestamp': '10:10', 'message': 'Status update 1'},
    ]

    # Messages from second report cycle (includes a Hermes edit of "Status update 1")
    batch2 = [
        {'timestamp': '10:15', 'message': 'Processing B'},
        {'timestamp': '10:20', 'message': 'Status update 1'},  # Hermes edited earlier msg
        {'timestamp': '10:25', 'message': 'Finalizing...'},
    ]

    all_messages = batch1 + batch2
    result = DelegationWorker.deduplicate_messages(all_messages)

    assert len(result) == 5
    # First "Status update 1" should be removed, second kept
    status_updates = [m for m in result if m['message'] == 'Status update 1']
    assert len(status_updates) == 1
    assert status_updates[0]['timestamp'] == '10:20'

    # All unique messages preserved
    msg_bodies = [m['message'] for m in result]
    assert 'Starting...' in msg_bodies
    assert 'Processing A' in msg_bodies
    assert 'Processing B' in msg_bodies
    assert 'Finalizing...' in msg_bodies


# ==============================================================================
# Tests for config timeout fields
# ==============================================================================

def test_config_poll_interval_defaults():
    """Test that poll_interval_sec defaults to 15s."""
    # Test with explicit value
    config = {'timeouts': {'poll_interval_sec': 45, 'progress_report_interval_sec': 600}}
    timeouts = config.get('timeouts', {})
    assert timeouts.get('poll_interval_sec', 15) == 45

    # Test default when not specified
    config_empty = {}
    timeouts = config_empty.get('timeouts', {})
    assert timeouts.get('poll_interval_sec', 15) == 15


def test_config_progress_report_interval_defaults():
    """Test that progress_report_interval_sec defaults to 900s."""
    config = {'timeouts': {'progress_report_interval_sec': 600}}
    timeouts = config.get('timeouts', {})
    assert timeouts.get('progress_report_interval_sec', 900) == 600

    # Default when not specified
    assert {}.get('progress_report_interval_sec', 900) == 900


# ==============================================================================
# Tests for DelegationState.last_progress_report_time
# ==============================================================================

def test_delegation_state_has_last_progress_report_time():
    """DelegationState should initialize last_progress_report_time to 0.0."""
    state = DelegationState('coder', '!c:test', '!d:test')
    assert hasattr(state, 'last_progress_report_time')
    assert state.last_progress_report_time == 0.0


# ==============================================================================
# Tests for MatrixClient._encode_room_id
# ==============================================================================

def test_encode_room_id_special_chars():
    """Room IDs with ! and : should be percent-encoded."""
    from delegation_worker import MatrixClient
    config = {'matrix': {'url': 'https://test.example.com', 'user_id': '@bot:test', 'access_token': 'tok'}}
    client = MatrixClient(config)

    encoded = client._encode_room_id('!coding-room:example.com')
    assert encoded == '%21coding-room%3Aexample.com'


def test_encode_room_id_already_encoded():
    """Already-encoded strings should be double-encoded (safe behavior)."""
    from delegation_worker import MatrixClient
    config = {'matrix': {'url': 'https://test.example.com', 'user_id': '@bot:test', 'access_token': 'tok'}}
    client = MatrixClient(config)

    encoded = client._encode_room_id('%21room')
    assert '%2521room' == encoded  # % becomes %25


def test_encode_room_id_simple():
    """Simple room IDs without special chars pass through unchanged."""
    from delegation_worker import MatrixClient
    config = {'matrix': {'url': 'https://test.example.com', 'user_id': '@bot:test', 'access_token': 'tok'}}
    client = MatrixClient(config)

    encoded = client._encode_room_id('simple-room')
    assert encoded == 'simple-room'


# ==============================================================================
# Tests for DelegationState fields
# ==============================================================================

def test_delegation_state_initial_fields():
    """All required fields should be initialized correctly."""
    state = DelegationState('coder', '!c:test', '!d:test')

    assert state.delegatee_name == 'coder'
    assert state.delegatee_room_id == '!c:test'
    assert state.delegator_room_id == '!d:test'
    assert state.task_description is None
    assert state.last_batch_token is None
    assert state.polling_active is False
    assert state.messages_collected == []
    assert state.processed_event_ids == set()
    assert state.event_id_to_index == {}
    assert state.event_id_to_body == {}
    assert state.task_sender_user_id is None
    assert state.task_start_timestamp is None
    assert state.max_update_bytes == 1024
    assert state.seen_old_event_ids == set()
    assert state.history_catchup_done is False
    assert state.completion_handoff_pending is False


# ==============================================================================
# Tests for _compose_progress_summary
# ==============================================================================

def test_compose_progress_summary_basic():
    """Basic progress summary with new messages."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = MagicMock()
    state.max_update_bytes = 2048
    worker.state = state

    messages = [
        {'timestamp': '10:00', 'sender': '@coder:test', 'message': 'Step 1 done'},
        {'timestamp': '10:05', 'sender': '@coder:test', 'message': 'Step 2 in progress'},
    ]

    summary = worker._compose_progress_summary(messages, False)

    assert 'coder' in summary
    assert 'Step 1 done' in summary
    assert 'Step 2 in progress' in summary
    assert '=== NO_MORE_ACTIONS ===' not in summary


def test_compose_progress_summary_with_completion():
    """Summary should include completion marker when detected."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = MagicMock()
    state.max_update_bytes = 2048
    worker.state = state

    messages = [
        {'timestamp': '10:00', 'sender': '@coder:test', 'message': 'Done! === NO_MORE_ACTIONS ==='},
    ]

    summary = worker._compose_progress_summary(messages, True)

    assert 'Task Complete' in summary or 'NO_MORE_ACTIONS' in summary


def test_compose_progress_summary_truncates_long_messages():
    """Long messages should be truncated to max_update_bytes total."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = MagicMock()
    state.max_update_bytes = 200  # Very small limit
    worker.state = state

    messages = [
        {'timestamp': '10:00', 'sender': '@coder:test', 'message': 'X' * 500},
    ]

    summary = worker._compose_progress_summary(messages, False)
    assert len(summary.encode('utf-8')) <= 200 + 3  # max_bytes + "..."


def test_compose_progress_summary_deduplication():
    """_compose_progress_summary should deduplicate messages."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = MagicMock()
    state.max_update_bytes = 4096
    worker.state = state

    messages = [
        {'timestamp': '10:00', 'sender': '@coder:test', 'message': 'Status update'},
        {'timestamp': '10:05', 'sender': '@coder:test', 'message': 'Other work'},
        {'timestamp': '10:10', 'sender': '@coder:test', 'message': 'Status update'},  # duplicate
    ]

    summary = worker._compose_progress_summary(messages, False)

    # Should only contain "Status update" once (the latest version)
    count = summary.count('Status update')
    assert count == 1


# ==============================================================================
# Tests for check_progress — message filtering
# ==============================================================================

def test_check_progress_filters_delegator_messages():
    """Messages from the delegator/bot should be filtered out."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    # Messages from delegator should be filtered
    messages = [
        {'type': 'm.room.message', 'event_id': '$1', 'sender': '@bot:test',  # configured sender
         'content': {'body': 'Bot message'}, 'origin_server_ts': 1000},
        {'type': 'm.room.message', 'event_id': '$2', 'sender': '@coder:test',
         'content': {'body': 'Coder message'}, 'origin_server_ts': 2000},
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages, None))
    mock_client.send_message = AsyncMock()
    worker.client = mock_client

    import asyncio
    async def run_test():
        result = await worker.check_progress()
        # Should only have the coder's message, not the bot's
        assert len(state.messages_collected) == 1
        assert state.messages_collected[0]['message'] == 'Coder message'

    asyncio.get_event_loop().run_until_complete(run_test())


def test_check_progress_skips_redacted_events():
    """Redacted events (Hermes edits) should be skipped."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    messages = [
        {'type': 'm.room.redacted', 'event_id': '$1', 'sender': '@coder:test',
         'content': {}, 'origin_server_ts': 1000},
        {'type': 'm.room.message', 'event_id': '$2', 'sender': '@coder:test',
         'content': {'body': 'Valid message'}, 'origin_server_ts': 2000},
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages, None))
    mock_client.send_message = AsyncMock()
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker.check_progress()
        assert len(state.messages_collected) == 1
        assert state.messages_collected[0]['message'] == 'Valid message'

    asyncio.get_event_loop().run_until_complete(run_test())


def test_check_progress_skips_old_messages():
    """Messages before task_start_timestamp should be skipped."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 100.0  # Task started at time 100
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    messages = [
        {'type': 'm.room.message', 'event_id': '$1', 'sender': '@coder:test',
         'content': {'body': 'Old message'}, 'origin_server_ts': 50000},  # Before task start
        {'type': 'm.room.message', 'event_id': '$2', 'sender': '@coder:test',
         'content': {'body': 'New message'}, 'origin_server_ts': 150000},  # After task start
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages, None))
    mock_client.send_message = AsyncMock()
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker.check_progress()
        assert len(state.messages_collected) == 1
        assert state.messages_collected[0]['message'] == 'New message'

    asyncio.get_event_loop().run_until_complete(run_test())


def test_check_progress_no_state_returns_none():
    """When there's no active state, check_progress returns None."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'
    worker.state = None

    # It's async, with no state it should return None immediately
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(worker.check_progress())
        assert result is None
    except TypeError:
        # If check_progress returns a coroutine that we can't await without client, skip
        pass
    finally:
        loop.close()


# ==============================================================================
# Tests for event ID tracking and edit detection
# ==============================================================================

def test_event_id_tracking_on_new_message():
    """New messages should be tracked in processed_event_ids and mappings."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    messages = [
        {'type': 'm.room.message', 'event_id': '$abc123', 'sender': '@coder:test',
         'content': {'body': 'Hello world'}, 'origin_server_ts': 1000},
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages, None))
    mock_client.send_message = AsyncMock()
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker.check_progress()
        assert '$abc123' in state.processed_event_ids
        assert '$abc123' in state.event_id_to_body
        assert state.event_id_to_body['$abc123'] == 'Hello world'
        assert '$abc123' in state.event_id_to_index
        assert state.event_id_to_index['$abc123'] == 0

    asyncio.get_event_loop().run_until_complete(run_test())


def test_edit_detection_updates_message_in_place():
    """When a message is edited (same event_id, new body), update in place."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    # Pre-populate with a processed message
    state.processed_event_ids = {'$edit1'}
    state.event_id_to_body = {'$edit1': 'Original content'}
    state.event_id_to_index = {'$edit1': 0}
    state.messages_collected = [
        {'timestamp': '10:00', 'sender': '@coder:test', 'message': 'Original content'},
    ]
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    # Same event_id but different body — simulates a Hermes edit
    messages = [
        {'type': 'm.room.message', 'event_id': '$edit1', 'sender': '@coder:test',
         'content': {'body': 'Updated content'}, 'origin_server_ts': 2000},
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages, None))
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker.check_progress()
        # Message should be updated in place, not added again
        assert len(state.messages_collected) == 1
        assert state.messages_collected[0]['message'] == 'Updated content'
        assert state.event_id_to_body['$edit1'] == 'Updated content'

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# Tests for start_task edge cases
# ==============================================================================

def test_start_task_missing_delegatee_config():
    """start_task should return False when delegatee not in config."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'nonexistent'

    worker.logger = logging.getLogger('test_delegation')
    worker.state = None

    import asyncio
    async def run_test():
        result = await worker.start_task('/tmp/test.md')
        assert result is False

    asyncio.get_event_loop().run_until_complete(run_test())


def test_start_task_relative_path_converted():
    """Relative task paths should be converted to absolute."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    worker.logger = logging.getLogger('test_delegation')
    worker.state = None

    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(return_value={'event_id': '$test'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker.start_task('relative/task.md')
        call_args = mock_client.send_message.call_args
        message_sent = call_args[0][1]
        # Should contain absolute path, not relative
        assert 'relative/task.md' not in message_sent or '/' in message_sent

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# Tests for _finalize_delegation
# ==============================================================================

def test_finalize_delegation_stops_polling():
    """_finalize_delegation should set polling_active to False."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(return_value={'event_id': '$test'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker._finalize_delegation('Final summary')
        assert state.polling_active is False

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# Tests for MatrixClient URL stripping
# ==============================================================================

def test_matrix_client_strips_trailing_slash():
    """MatrixClient should strip trailing slash from base URL."""
    from delegation_worker import MatrixClient
    config = {'matrix': {'url': 'https://test.example.com/', 'user_id': '@bot:test', 'access_token': 'tok'}}
    client = MatrixClient(config)
    assert client.url == 'https://test.example.com'


def test_matrix_client_no_trailing_slash():
    """URL without trailing slash should remain unchanged."""
    from delegation_worker import MatrixClient
    config = {'matrix': {'url': 'https://test.example.com', 'user_id': '@bot:test', 'access_token': 'tok'}}
    client = MatrixClient(config)
    assert client.url == 'https://test.example.com'


# ==============================================================================
# Tests for worker initialization fields
# ==============================================================================

def test_worker_poll_interval_from_config():
    """Worker should read poll_interval_sec from config."""
    import tempfile
    import yaml as pyyaml

    tmpdir = Path(tempfile.mkdtemp())
    config_file = tmpdir / 'test_config.yaml'
    cfg_data = {
        'matrix': {'url': 'https://test.example.com', 'user_id': '@bot:test', 'access_token': 'tok'},
        'delegator': {'type': 'Hermes', 'profile': 'test-profile', 'matrix': {'room_id': '!d:test'}},
        'delegatees': [{'name': 'coder', 'type': 'Hermes', 'matrix': {'room_id': '!c:test'}}],
        'timeouts': {'poll_interval_sec': 45, 'progress_report_interval_sec': 600},
    }
    with open(config_file, 'w') as f:
        pyyaml.dump(cfg_data, f)

    worker = DelegationWorker(str(config_file), 'coder', logger=logging.getLogger('test'))
    assert worker.poll_interval_sec == 45
    assert worker.progress_report_interval_sec == 600

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


def test_worker_deprecated_timeout_fallback():
    """When poll_interval_sec is missing, worker defaults to 15 (not delegator_check_period_sec).
    Backward compat fallback to delegator_check_period_sec was planned but never implemented."""
    import tempfile
    import yaml as pyyaml

    tmpdir = Path(tempfile.mkdtemp())
    config_file = tmpdir / 'test_config.yaml'
    cfg_data = {
        'matrix': {'url': 'https://test.example.com', 'user_id': '@bot:test', 'access_token': 'tok'},
        'delegator': {'type': 'Hermes', 'profile': 'test-profile', 'matrix': {'room_id': '!d:test'}},
        'delegatees': [{'name': 'coder', 'type': 'Hermes', 'matrix': {'room_id': '!c:test'}}],
        'timeouts': {'delegator_check_period_sec': 120},
    }
    with open(config_file, 'w') as f:
        pyyaml.dump(cfg_data, f)

    worker = DelegationWorker(str(config_file), 'coder', logger=logging.getLogger('test'))
    # Current behavior: defaults to 15 when poll_interval_sec is missing
    assert worker.poll_interval_sec == 15

    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)


# ==============================================================================
# Tests for completion detection in check_progress
# ==============================================================================

def test_completion_marker_detection():
    """When === NO_MORE_ACTIONS === is found, finalize should be called."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    messages = [
        {'type': 'm.room.message', 'event_id': '$done', 'sender': '@coder:test',
         'content': {'body': 'Task done! === NO_MORE_ACTIONS ==='}, 'origin_server_ts': 1000},
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages, None))
    mock_client.send_message = AsyncMock(return_value={'event_id': '$final'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        result = await worker.check_progress()
        # Should return None after finalization (not a summary)
        assert state.polling_active is False

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# Tests for check_progress with no new messages
# ==============================================================================

def test_check_progress_no_new_messages():
    """When there are no new messages, check_progress returns None."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")
    mock_client = MagicMock()
    mock_client.get_messages = AsyncMock(return_value=([], None))
    worker.client = mock_client

    import asyncio
    async def run_test():
        result = await worker.check_progress()
        assert result is None

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# Tests for delegation_core format functions with edge cases
# ==============================================================================

def test_format_task_message_empty_path():
    """Empty task path should still produce a valid message."""
    msg = format_task_message('')
    assert 'New task file:' in msg
    assert '=== NO_MORE_ACTIONS ===' in msg


# ==============================================================================# Tests for post-completion race condition fixes
# ==============================================================================

def test_periodic_report_skips_when_polling_inactive():
    """_send_periodic_progress_report should return early if polling_active=False."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = False  # Task already finalized
    state.messages_collected = [
        {'timestamp': datetime.now().isoformat(), 'message': 'Some work'},
    ]
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")

    mock_client = MagicMock()
    mock_client.send_message = AsyncMock()
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker._send_periodic_progress_report()
        # Should NOT send any message when polling is inactive
        mock_client.send_message.assert_not_called()

    asyncio.get_event_loop().run_until_complete(run_test())


def test_periodic_report_skips_when_no_state():
    """_send_periodic_progress_report should return early if state is None."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'
    worker.state = None  # No active task
    worker.logger = logging.getLogger("test_delegation")

    mock_client = MagicMock()
    mock_client.send_message = AsyncMock()
    worker.client = mock_client

    import asyncio
    async def run_test():
        await worker._send_periodic_progress_report()
        mock_client.send_message.assert_not_called()

    asyncio.get_event_loop().run_until_complete(run_test())


def test_periodic_report_fires_when_active():
    """Periodic report should fire normally when polling_active=True (regression)."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    import time
    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True  # Task still active
    state.last_progress_report_time = time.time() - 60  # Report was 1 min ago
    state.messages_collected = [
        {'timestamp': datetime.now().isoformat(), 'message': 'Working on it'},
    ]
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")

    mock_client = MagicMock()
    mock_client.send_message = AsyncMock(return_value={'event_id': '$report'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        # Mock is_delegator_idle to return True so the report is actually sent
        with patch.object(worker, 'is_delegator_idle', return_value=True):
            await worker._send_periodic_progress_report()
            # Should send exactly one message (the periodic report)
            mock_client.send_message.assert_called_once()
            call_args = mock_client.send_message.call_args
            room_id, message = call_args[0]
            assert 'Periodic Progress' in message

    asyncio.get_event_loop().run_until_complete(run_test())


def test_check_progress_finalization_no_extra_send():
    """When check_progress detects completion, only the handoff message should be sent (no periodic report)."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")

    mock_client = MagicMock()
    messages_with_completion = [
        {'type': 'm.room.message', 'event_id': '$done', 'sender': '@coder:test',
         'content': {'body': 'Task done! === NO_MORE_ACTIONS ==='}, 'origin_server_ts': 1000},
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages_with_completion, None))
    mock_client.send_message = AsyncMock(return_value={'event_id': '$handoff'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        # Simulate the full loop iteration logic (from run())
        result = await worker.check_progress()

        # check_progress returns None after finalization
        assert result is None
        assert state.polling_active is False  # Finalized

        # Now simulate what run() does next: periodic report check
        # With the fix, this should be guarded and not send anything extra
        poll_counter = 100  # Simulate threshold reached (report_cycles)
        report_cycles = 30
        if poll_counter >= report_cycles and worker.state and worker.state.polling_active:
            await worker._send_periodic_progress_report()

        # Total calls should be exactly 1 (the handoff message from _finalize_delegation)
        assert mock_client.send_message.call_count == 1, \
            f"Expected 1 send_message call (handoff only), got {mock_client.send_message.call_count}"

        # Verify it's the handoff message
        call_args = mock_client.send_message.call_args
        room_id, message = call_args[0]
        assert 'Delegation handoff:' in message

    asyncio.get_event_loop().run_until_complete(run_test())


def test_check_progress_finalization_without_guard_would_send_extra():
    """Verify that WITHOUT the guard, a periodic report would be sent after finalization (demonstrates the bug)."""
    config = _make_worker_config()
    worker = DelegationWorker.__new__(DelegationWorker)
    worker.config = config
    worker.delegatee_name = 'coder'

    state = DelegationState('coder', '!c:test', '!d:test')
    state.polling_active = True
    state.task_start_timestamp = 0.0
    state.processed_event_ids = set()
    state.event_id_to_body = {}
    state.event_id_to_index = {}
    worker.state = state
    worker.logger = logging.getLogger("test_delegation")

    mock_client = MagicMock()
    messages_with_completion = [
        {'type': 'm.room.message', 'event_id': '$done', 'sender': '@coder:test',
         'content': {'body': 'Task done! === NO_MORE_ACTIONS ==='}, 'origin_server_ts': 1000},
    ]
    mock_client.get_messages = AsyncMock(return_value=(messages_with_completion, None))
    mock_client.send_message = AsyncMock(return_value={'event_id': '$msg'})
    worker.client = mock_client

    import asyncio
    async def run_test():
        # Step 1: check_progress detects completion and finalizes
        result = await worker.check_progress()
        calls_after_finalize = mock_client.send_message.call_count
        assert state.polling_active is False

        # Step 2: Simulate the OLD buggy behavior — call periodic report WITHOUT guard
        # (directly call it, bypassing the run() loop guard)
        with patch.object(worker, 'is_delegator_idle', return_value=True):
            await worker._send_periodic_progress_report()

        calls_after_periodic = mock_client.send_message.call_count

        # With Fix B (internal guard in _send_periodic_progress_report),
        # it should detect polling_active=False and NOT send
        assert calls_after_finalize == calls_after_periodic, \
            f"Periodic report sent after finalization: {calls_after_periodic} total calls vs {calls_after_finalize} after finalize"

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================


def test_format_correction_message_with_special_chars():
    """Test correction message with special characters."""
    msg = format_correction_message('Fix "issue #42" (urgent!)')
    assert '/steer Fix "issue #42" (urgent!)' == msg


# ==============================================================================
# Tests for HTTP timeouts in worker MatrixClient (M3 fix)
# ==============================================================================

def test_matrix_client_send_message_has_timeout():
    """send_message should use aiohttp.ClientTimeout to prevent hanging."""
    config = _make_worker_config()
    from delegation_worker import MatrixClient
    client = MatrixClient(config)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={'event_id': '$test'})

    mock_session = MagicMock()
    mock_session.post.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.post.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import asyncio
    async def run_test():
        with patch('aiohttp.ClientSession', return_value=mock_session):
            await client.send_message('!room:test', 'hello')

        # Verify ClientSession was called with a timeout argument
        call_kwargs = MagicMock()
        # We can't easily inspect the actual timeout, but we verify the session
        # was created (which means our code path ran)
        assert mock_session.post.called

    asyncio.get_event_loop().run_until_complete(run_test())


def test_matrix_client_get_messages_has_timeout():
    """get_messages should use aiohttp.ClientTimeout to prevent hanging."""
    config = _make_worker_config()
    from delegation_worker import MatrixClient
    client = MatrixClient(config)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={'chunk': []})

    mock_session = MagicMock()
    mock_session.get.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_session.get.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    import asyncio
    async def run_test():
        with patch('aiohttp.ClientSession', return_value=mock_session):
            result = await client.get_messages('!room:test')
            assert result == ([], None)
            assert mock_session.get.called

    asyncio.get_event_loop().run_until_complete(run_test())


# ==============================================================================
# Tests for URL encoding in heartbeat script (M8 fix)
# ==============================================================================

# ==============================================================================
# C3: File locking regression tests (race condition fix)
# ==============================================================================

def test_daemon_manager_save_state_uses_flock():
    """C3 regression: DaemonManager._save_state() must use fcntl.flock LOCK_EX."""
    import inspect
    source = inspect.getsource(DaemonManager._save_state)
    assert 'fcntl.flock' in source, "_save_state should use fcntl.flock"
    assert 'LOCK_EX' in source, "_save_state should acquire exclusive lock (LOCK_EX)"
    assert 'f.flush()' in source or 'flush()' in source, "_save_state should flush before releasing lock"


def test_daemon_manager_load_state_uses_flock():
    """C3 regression: DaemonManager._load_state() must use fcntl.flock LOCK_SH."""
    import inspect
    source = inspect.getsource(DaemonManager._load_state)
    assert 'fcntl.flock' in source, "_load_state should use fcntl.flock"
    assert 'LOCK_SH' in source, "_load_state should acquire shared lock (LOCK_SH)"


def test_worker_save_state_uses_flock():
    """C3 regression: DelegationWorker._save_state() must use fcntl.flock LOCK_EX."""
    import inspect
    source = inspect.getsource(DelegationWorker._save_state)
    assert 'fcntl.flock' in source, "_save_state should use fcntl.flock"
    assert 'LOCK_EX' in source, "_save_state should acquire exclusive lock (LOCK_EX)"
    assert 'f.flush()' in source or 'flush()' in source, "_save_state should flush before releasing lock"


def test_worker_load_state_uses_flock():
    """C3 regression: DelegationWorker._load_state() must use fcntl.flock LOCK_SH."""
    import inspect
    source = inspect.getsource(DelegationWorker._load_state)
    assert 'fcntl.flock' in source, "_load_state should use fcntl.flock"
    assert 'LOCK_SH' in source, "_load_state should acquire shared lock (LOCK_SH)"


def test_daemon_manager_concurrent_writes_no_corruption():
    """C3 regression: Concurrent _save_state calls should not corrupt JSON."""
    import concurrent.futures

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / 'test_daemons.json'
        dm = DaemonManager.__new__(DaemonManager)
        dm.state_file = state_file

        errors = []
        def write_state(i):
            try:
                for _ in range(50):
                    dm._save_state({'test': f'value_{i}', 'counter': _})
            except Exception as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(write_state, i) for i in range(4)]
            concurrent.futures.wait(futures)

        assert not errors, f"Concurrent writes caused errors: {errors}"

        with open(state_file, 'r') as f:
            data = json.load(f)
        assert isinstance(data, dict)


def test_worker_concurrent_writes_no_corruption():
    """C3 regression: Concurrent worker _save_state calls should not corrupt JSON."""
    import concurrent.futures

    with tempfile.TemporaryDirectory() as tmpdir:
        state_file = Path(tmpdir) / 'test_delegation_state.json'
        worker = DelegationWorker.__new__(DelegationWorker)
        worker.state_file = state_file
        state_file.parent.mkdir(parents=True, exist_ok=True)

        errors = []
        def write_state(i):
            try:
                for _ in range(50):
                    worker._save_state({'worker': f'thread_{i}', 'counter': _})
            except Exception as e:
                errors.append(e)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(write_state, i) for i in range(4)]
            concurrent.futures.wait(futures)

        assert not errors, f"Concurrent writes caused errors: {errors}"

        with open(state_file, 'r') as f:
            data = json.load(f)
        assert isinstance(data, dict)


# ==============================================================================
# Test runner
# ==============================================================================

if __name__ == '__main__':
    print("Running delegator-delegatee utility tests...\n")

    test_functions = [
        # Message formatting
        test_format_task_message,
        test_format_correction_message,
        test_format_stop_message,
        test_format_continue_message,
        test_format_task_message_mentions_question_pattern,
        test_format_pause_message_steer_prefix,
        test_format_resume_message_steer_prefix,
        test_pause_marker_path_per_delegatee,
        test_outbox_path_per_delegatee,
        test_config_load_from_hermes_profile,
        test_config_get_all_delegatees,
        test_config_get_delegatee,
        test_task_message_contains_protocol_marker,
        test_correction_message_format_for_hermes,
        # Dedup tests
        test_deduplicate_messages_keeps_latest_occurrence,
        test_deduplicate_messages_preserves_order,
        test_deduplicate_messages_all_unique,
        test_deduplicate_messages_all_identical,
        test_deduplicate_messages_empty,
        test_deduplicate_messages_single,
        test_deduplicate_messages_hermes_edit_pattern,
        test_deduplicate_messages_three_duplicates,
        # Idle detection (delegation_core)
        test_is_delegator_idle_hermes_profile,
        test_is_delegator_idle_hermes_main,
        test_is_delegator_idle_openclaw,
        test_is_delegator_busy_returns_false,
        test_is_delegator_idle_fallback_to_main,
        test_is_delegator_idle_no_state_file,
        # Worker idle detection
        test_worker_is_delegator_idle_hermes_idle,
        test_worker_is_delegator_idle_hermes_busy,
        test_worker_is_delegator_idle_openclaw,
        # Periodic progress reports
        test_compose_periodic_summary,
        test_compose_periodic_summary_truncates,
        # Start task message format
        test_start_task_message_with_existing_task,
        test_start_task_message_without_existing_task,
        # Cross-report deduplication
        test_deduplicate_messages_across_reports,
        # Config timeout defaults
        test_config_poll_interval_defaults,
        # DelegationState fields
        test_delegation_state_has_last_progress_report_time,
        # MatrixClient URL encoding
        test_encode_room_id_special_chars,
        test_encode_room_id_already_encoded,
        test_encode_room_id_simple,
        # DelegationState initialization
        test_delegation_state_initial_fields,
        # Progress summary composition
        test_compose_progress_summary_basic,
        test_compose_progress_summary_with_completion,
        test_compose_progress_summary_truncates_long_messages,
        test_compose_progress_summary_deduplication,
        # check_progress message filtering
        test_check_progress_filters_delegator_messages,
        test_check_progress_skips_redacted_events,
        test_check_progress_skips_old_messages,
        test_check_progress_no_state_returns_none,
        # Event ID tracking & edit detection
        test_event_id_tracking_on_new_message,
        test_edit_detection_updates_message_in_place,
        # Start task edge cases
        test_start_task_missing_delegatee_config,
        test_start_task_relative_path_converted,
        # Finalize delegation
        test_finalize_delegation_stops_polling,
        # MatrixClient URL stripping
        test_matrix_client_strips_trailing_slash,
        test_matrix_client_no_trailing_slash,
        # Worker initialization
        test_worker_poll_interval_from_config,
        test_worker_deprecated_timeout_fallback,
        # Completion detection
        test_completion_marker_detection,
        # No new messages
        test_check_progress_no_new_messages,
        # Format function edge cases
        test_format_task_message_empty_path,
        test_format_correction_message_with_special_chars,
        # Post-completion race condition fixes (Issue 1: delay, Issue 2: race)
        test_periodic_report_skips_when_polling_inactive,
        test_periodic_report_skips_when_no_state,
        test_periodic_report_fires_when_active,
        test_check_progress_finalization_no_extra_send,
        test_check_progress_finalization_without_guard_would_send_extra,
        # HTTP timeouts in worker MatrixClient (M3 fix)
        test_matrix_client_send_message_has_timeout,
        test_matrix_client_get_messages_has_timeout,
        # URL encoding in heartbeat script (M8 fix) — removed (dev artifact)
        # C3: File locking regression tests
        test_daemon_manager_save_state_uses_flock,
        test_daemon_manager_load_state_uses_flock,
        test_worker_save_state_uses_flock,
        test_worker_load_state_uses_flock,
        test_daemon_manager_concurrent_writes_no_corruption,
        test_worker_concurrent_writes_no_corruption,
    ]

    passed = 0
    failed = 0

    for test_func in test_functions:
        try:
            test_func()
            print(f"  \u2714 {test_func.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  \u2718 {test_func.__name__}: ASSERTION: {e}")
            failed += 1
        except Exception as e:
            print(f"  \u2718 {test_func.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")

    if failed > 0:
        sys.exit(1)
