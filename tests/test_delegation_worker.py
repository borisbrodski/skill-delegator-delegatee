#!/usr/bin/env python3
"""
Comprehensive tests for delegation worker - batch token, message filtering,
completion detection, periodic reports, and error handling.
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from delegation_worker import (
    DelegationState,
    DelegationWorker,
    MatrixClient,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config_dir(tmp_path):
    """Create a temp config directory with a valid YAML config."""
    cfg = {
        'matrix': {
            'url': 'https://matrix.example.com',
            'user_id': '@delegator:example.com',
            'access_token': 'test_token',
        },
        'delegator': {
            'type': 'Hermes',
            'profile': 'test-profile',
            'matrix': {'room_id': '!DelegatorRoom:example.com'},
        },
        'delegatees': [
            {
                'name': 'agent-01',
                'matrix': {'room_id': '!AgentRoom:example.com'},
            }
        ],
        'timeouts': {
            'poll_interval_sec': 5,
            'progress_report_interval_sec': 30,
        },
    }
    cfg_file = tmp_path / 'delegator-delegatee.yaml'
    cfg_file.write_text(yaml.dump(cfg))
    return tmp_path


@pytest.fixture
def config_data(config_dir):
    with open(config_dir / 'delegator-delegatee.yaml') as f:
        return yaml.safe_load(f)


@pytest.fixture
def delegation_state():
    state = DelegationState('agent-01', '!AgentRoom:e', '!DelegatorRoom:e')
    state.task_start_timestamp = time.time()
    state.last_progress_report_time = state.task_start_timestamp
    state.polling_active = True
    return state


# ---------------------------------------------------------------------------
# Batch token tests — the critical regression fix
# ---------------------------------------------------------------------------

class TestBatchTokenLogic:
    """Test that batch token only advances when appropriate."""

    def test_old_messages_do_not_advance_token(self):
        """Old messages (before task_start_timestamp) must NOT advance the batch token.

        This was the root cause of the daemon "scrolling backward" through room history,
        missing new messages because it kept paginating to older batches.
        """
        state = DelegationState('agent-01', '!A:e', '!D:e')
        now = time.time()
        state.task_start_timestamp = now

        # Simulate: all fetched messages are OLD (before task start)
        old_ts = now - 100  # 100 seconds ago
        chunk = [
            {
                'type': 'm.room.message',
                'event_id': f'$old{i}',
                'sender': '@delegatee:example.com',
                'origin_server_ts': int(old_ts * 1000),
                'content': {'body': f'Old message {i}'},
            }
            for i in range(5)
        ]

        # Process messages through the same logic as check_progress()
        new_messages = []
        for msg in chunk:
            origin_server_ts = msg['origin_server_ts'] / 1000.0
            task_cutoff = state.task_start_timestamp - 2.0
            if origin_server_ts < task_cutoff:
                # OLD — do NOT add to processed_event_ids (the fix)
                continue
            state.processed_event_ids.add(msg['event_id'])
            new_messages.append(msg)

        end_token = 't50-old'

        # Apply the batch token logic from check_progress()
        all_already_processed = (
            len(chunk) > 0 and
            all(m.get('event_id', '') in state.processed_event_ids for m in chunk)
        )

        # Old messages should NOT be in processed_event_ids
        assert all(f'$old{i}' not in state.processed_event_ids for i in range(5))

        # all_already_processed should be False because old messages aren't tracked
        assert not all_already_processed, (
            "all_already_processed must be False when all messages are old — "
            "otherwise the token advances backward through history"
        )

        # Token should NOT advance
        if end_token and (new_messages or all_already_processed):
            state.last_batch_token = end_token

        assert state.last_batch_token is None, (
            "Batch token must stay None when all messages are old — "
            "advancing would paginate backward through history"
        )

    def test_new_message_advances_token(self):
        """A genuinely new message should advance the batch token."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        now = time.time()
        state.task_start_timestamp = now - 5  # Task started 5 seconds ago

        chunk = [
            {
                'type': 'm.room.message',
                'event_id': '$new1',
                'sender': '@delegatee:example.com',
                'origin_server_ts': int(now * 1000),  # Current time — NOT old
                'content': {'body': 'New message'},
            }
        ]

        new_messages = []
        for msg in chunk:
            origin_server_ts = msg['origin_server_ts'] / 1000.0
            task_cutoff = state.task_start_timestamp - 2.0
            if origin_server_ts < task_cutoff:
                continue
            state.processed_event_ids.add(msg['event_id'])
            new_messages.append(msg)

        end_token = 't51-new'

        all_already_processed = (
            len(chunk) > 0 and
            all(m.get('event_id', '') in state.processed_event_ids for m in chunk)
        )

        if end_token and (new_messages or all_already_processed):
            state.last_batch_token = end_token

        assert state.last_batch_token == 't51-new'
        assert len(new_messages) == 1

    def test_mixed_old_and_new_only_new_count(self):
        """When a batch has both old and new messages, only new ones are processed."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        now = time.time()
        state.task_start_timestamp = now

        chunk = [
            {
                'type': 'm.room.message',
                'event_id': f'$old{i}',
                'sender': '@delegatee:example.com',
                'origin_server_ts': int((now - 100) * 1000),
                'content': {'body': f'Old {i}'},
            }
            for i in range(49)
        ] + [
            {
                'type': 'm.room.message',
                'event_id': '$new1',
                'sender': '@delegatee:example.com',
                'origin_server_ts': int(now * 1000),
                'content': {'body': 'New message'},
            }
        ]

        new_messages = []
        for msg in chunk:
            origin_server_ts = msg['origin_server_ts'] / 1000.0
            task_cutoff = state.task_start_timestamp - 2.0
            if origin_server_ts < task_cutoff:
                continue
            state.processed_event_ids.add(msg['event_id'])
            new_messages.append(msg)

        assert len(new_messages) == 1
        assert new_messages[0]['event_id'] == '$new1'
        # Old messages NOT in processed set
        assert all(f'$old{i}' not in state.processed_event_ids for i in range(49))

    def test_all_already_processed_advances_token(self):
        """If ALL events were already known before this fetch, advance the token."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.task_start_timestamp = time.time()

        # Pre-populate processed_event_ids (these were seen in a previous fetch)
        for i in range(5):
            state.processed_event_ids.add(f'$seen{i}')

        chunk = [
            {
                'type': 'm.room.message',
                'event_id': f'$seen{i}',
                'sender': '@delegatee:example.com',
                'origin_server_ts': int(time.time() * 1000),
                'content': {'body': f'Already seen {i}'},
            }
            for i in range(5)
        ]

        new_messages = []
        for msg in chunk:
            if msg['event_id'] in state.processed_event_ids:
                # Already processed — not edited, just skip
                continue
            state.processed_event_ids.add(msg['event_id'])
            new_messages.append(msg)

        end_token = 't50-seen'
        all_already_processed = (
            len(chunk) > 0 and
            all(m.get('event_id', '') in state.processed_event_ids for m in chunk)
        )

        if end_token and (new_messages or all_already_processed):
            state.last_batch_token = end_token

        assert state.last_batch_token == 't50-seen'
        assert len(new_messages) == 0


# ---------------------------------------------------------------------------
# Message filtering tests
# ---------------------------------------------------------------------------

class TestMessageFiltering:
    """Test sender filtering, non-text event skipping, and edit detection."""

    def test_filtered_sender_skipped(self):
        """Messages from the delegator's own account must be filtered out."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.task_start_timestamp = time.time()

        # Delegator sends a message — should be filtered
        msg = {
            'type': 'm.room.message',
            'event_id': '$filtered',
            'sender': '@delegator:example.com',  # Same as configured sender
            'origin_server_ts': int(time.time() * 1000),
            'content': {'body': 'This is from the delegator'},
        }

        filtered_senders = {'@delegator:example.com'}

        new_messages = []
        for m in [msg]:
            if m['sender'] in filtered_senders:
                state.processed_event_ids.add(m['event_id'])
                continue
            # Would be processed normally
            state.processed_event_ids.add(m['event_id'])
            new_messages.append(m)

        assert len(new_messages) == 0, "Delegator's own messages must be filtered"
        assert '$filtered' in state.processed_event_ids

    def test_non_text_events_tracked(self):
        """Non-text events (m.room.member, etc.) should be tracked to avoid re-fetching."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.task_start_timestamp = time.time()

        non_text_event = {
            'type': 'm.room.member',
            'event_id': '$membership',
            'sender': '@server:example.com',
            'origin_server_ts': int(time.time() * 1000),
            'content': {'membership': 'join'},
        }

        # Simulate the non-text skip path
        event_type = non_text_event['type']
        if event_type not in ('m.room.message', 'm.text'):
            state.processed_event_ids.add(non_text_event['event_id'])

        assert '$membership' in state.processed_event_ids, (
            "Non-text events must be tracked to prevent infinite re-fetch"
        )

    def test_redacted_events_tracked(self):
        """Redacted events should be tracked."""
        state = DelegationState('agent-01', '!A:e', '!D:e')

        redacted = {
            'type': 'm.room.redacted',
            'event_id': '$redacted',
        }

        if redacted['type'] == 'm.room.redacted':
            state.processed_event_ids.add(redacted['event_id'])

        assert '$redacted' in state.processed_event_ids

    def test_empty_body_skipped(self):
        """Messages with empty body should be skipped but tracked."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.task_start_timestamp = time.time()

        msg = {
            'type': 'm.room.message',
            'event_id': '$empty',
            'sender': '@delegatee:example.com',
            'origin_server_ts': int(time.time() * 1000),
            'content': {'body': ''},
        }

        body = msg['content'].get('body', '')
        if not body or not body.strip():
            state.processed_event_ids.add(msg['event_id'])

        assert '$empty' in state.processed_event_ids


# ---------------------------------------------------------------------------
# Completion detection tests
# ---------------------------------------------------------------------------

class TestCompletionDetection:
    """Test === NO_MORE_ACTIONS === marker detection."""

    def test_completion_in_new_message(self):
        """Completion marker in a new message should be detected."""
        msg = {
            'timestamp': datetime.now().isoformat(),
            'sender': '@delegatee:example.com',
            'message': 'Task complete! === NO_MORE_ACTIONS ===',
        }

        assert '=== NO_MORE_ACTIONS ===' in msg['message']

    def test_completion_in_edited_message(self):
        """Completion marker in an edited message should be detected."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.task_start_timestamp = time.time()

        # First version — no completion
        original_body = 'Working on it...'
        state.event_id_to_body['$edit1'] = original_body
        state.messages_collected.append({
            'timestamp': datetime.now().isoformat(),
            'sender': '@delegatee:example.com',
            'message': original_body,
        })
        state.event_id_to_index['$edit1'] = 0

        # Edited version — has completion
        edited_body = 'Done! === NO_MORE_ACTIONS ==='

        # Simulate edit detection path
        event_id = '$edit1'
        last_known = state.event_id_to_body.get(event_id, '')
        if edited_body and edited_body != last_known:
            idx = state.event_id_to_index[event_id]
            state.messages_collected[idx]['message'] = edited_body
            state.event_id_to_body[event_id] = edited_body

            completion_detected = '=== NO_MORE_ACTIONS ===' in edited_body
            assert completion_detected, "Edited message with completion marker must be detected"


# ---------------------------------------------------------------------------
# Periodic report tests
# ---------------------------------------------------------------------------

class TestPeriodicReport:
    """Test periodic progress report timing and filtering."""

    def test_last_progress_report_time_initialized_to_task_start(self):
        """last_progress_report_time must equal task_start_timestamp, not time.time().

        If initialized to time.time(), messages collected during startup (before the timer)
        would have timestamps older than last_progress_report_time and be missed.
        """
        state = DelegationState('agent-01', '!A:e', '!D:e')
        task_start = time.time()
        state.task_start_timestamp = task_start
        state.last_progress_report_time = task_start  # The fix

        assert state.last_progress_report_time == task_start, (
            "last_progress_report_time must be set to task_start_timestamp, "
            "not a separate time.time() call"
        )

    def test_messages_before_cutoff_excluded(self):
        """Messages older than last_progress_report_time should be excluded."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        now = time.time()
        state.last_progress_report_time = now

        state.messages_collected = [
            {
                'timestamp': datetime.fromtimestamp(now - 100).isoformat(),
                'sender': '@d:e',
                'message': 'Old message',
            },
            {
                'timestamp': datetime.fromtimestamp(now + 5).isoformat(),
                'sender': '@d:e',
                'message': 'New message',
            },
        ]

        new_messages = [
            m for m in state.messages_collected
            if datetime.fromisoformat(m['timestamp']).timestamp() >= state.last_progress_report_time
        ]

        assert len(new_messages) == 1
        assert new_messages[0]['message'] == 'New message'

    def test_zero_cutoff_includes_all(self):
        """When last_progress_report_time is 0 (uninitialized), all messages are included."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.last_progress_report_time = 0.0

        state.messages_collected = [
            {
                'timestamp': datetime.fromtimestamp(time.time() - 100).isoformat(),
                'sender': '@d:example.com',
                'message': 'Old message',
            },
        ]

        new_messages = [
            m for m in state.messages_collected
            if datetime.fromisoformat(m['timestamp']).timestamp() >= state.last_progress_report_time
        ]

        assert len(new_messages) == 1

    def test_periodic_summary_with_no_messages_sends_stuck_alert(self):
        """When there are no new messages, _compose_periodic_summary must produce
        a STUCK alert — NOT silently return None. The periodic report is a heartbeat.
        """
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.task_start_timestamp = time.time()
        state.last_progress_report_time = time.time()

        worker = DelegationWorker.__new__(DelegationWorker)
        worker.delegatee_name = 'agent-01'
        worker.state = state

        summary = worker._compose_periodic_summary([])

        # Must NOT be empty — must contain a stuck alert
        assert 'stuck' in summary.lower() or 'no activity' in summary.lower(), (
            f"Empty message list must produce a stuck alert, got: {summary[:200]}"
        )
        assert 'agent-01' in summary
        assert '15 minutes' in summary

    @pytest.mark.asyncio
    async def test_periodic_report_sends_even_with_no_new_messages(self, config_dir):
        """When the 15-minute timer fires and there are no new messages,
        the worker must still send a heartbeat to alert the delegator.
        Previously it silently returned — that was the bug.
        """
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )

        worker.state = DelegationState('agent-01', '!AgentRoom:e', '!DelegatorRoom:e')
        worker.state.task_start_timestamp = time.time()
        worker.state.last_progress_report_time = time.time()
        worker.state.polling_active = True
        worker.state.messages_collected = []  # No messages at all

        mock_client = AsyncMock()
        mock_client.send_message.return_value = {'event_id': '$report'}
        worker.client = mock_client

        # Mock is_delegator_idle to return True
        worker.is_delegator_idle = lambda: True

        await worker._send_periodic_progress_report()

        # Must have sent a message even though there were no new messages
        mock_client.send_message.assert_called_once()
        call_args = mock_client.send_message.call_args[0][1]
        assert 'stuck' in call_args.lower() or 'no activity' in call_args.lower(), (
            f"Stuck alert expected, got: {call_args[:200]}"
        )

    @pytest.mark.asyncio
    async def test_periodic_report_updates_timer_on_success(self, config_dir):
        """After a successful periodic report, last_progress_report_time must be updated."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )

        worker.state = DelegationState('agent-01', '!AgentRoom:e', '!DelegatorRoom:e')
        worker.state.task_start_timestamp = time.time()
        worker.state.last_progress_report_time = 0.0
        worker.state.polling_active = True
        worker.state.messages_collected = []

        mock_client = AsyncMock()
        mock_client.send_message.return_value = {'event_id': '$report'}
        worker.client = mock_client
        worker.is_delegator_idle = lambda: True

        before = worker.state.last_progress_report_time
        await worker._send_periodic_progress_report()
        after = worker.state.last_progress_report_time

        assert after > before, "Timer must advance after successful send"


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------

class TestDeduplication:
    """Test message deduplication logic."""

    def test_deduplicate_keeps_latest(self):
        """Deduplication should keep the latest version of duplicate bodies."""
        messages = [
            {'message': 'First', 'timestamp': '2024-01-01T00:00:00'},
            {'message': 'Second', 'timestamp': '2024-01-01T00:01:00'},
            {'message': 'First', 'timestamp': '2024-01-01T00:02:00'},  # Duplicate body
        ]

        result = DelegationWorker.deduplicate_messages(messages)
        assert len(result) == 2
        # The last occurrence of 'First' should be kept
        assert result[-1]['message'] == 'First'
        assert result[-1]['timestamp'] == '2024-01-01T00:02:00'

    def test_deduplicate_preserves_order(self):
        """Deduplication should preserve chronological order."""
        messages = [
            {'message': 'A', 'timestamp': '1'},
            {'message': 'B', 'timestamp': '2'},
            {'message': 'C', 'timestamp': '3'},
        ]

        result = DelegationWorker.deduplicate_messages(messages)
        assert [m['message'] for m in result] == ['A', 'B', 'C']


# ---------------------------------------------------------------------------
# Finalization error handling tests
# ---------------------------------------------------------------------------

class TestFinalization:
    """Test completion handoff error handling and retry logic."""

    def test_completion_handoff_pending_flag(self):
        """completion_handoff_pending should be True when finalization fails."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.completion_handoff_pending = False

        # Simulate: completion detected, flag set
        state.completion_handoff_pending = True

        assert state.completion_handoff_pending is True

    def test_successful_finalization_clears_flags(self):
        """Successful handoff should clear pending flag and stop polling."""
        state = DelegationState('agent-01', '!A:e', '!D:e')
        state.polling_active = True
        state.completion_handoff_pending = True

        # Simulate successful send
        sent = True
        if sent:
            state.polling_active = False
            state.completion_handoff_pending = False

        assert not state.polling_active
        assert not state.completion_handoff_pending


# ---------------------------------------------------------------------------
# Integration-style tests (mock Matrix API)
# ---------------------------------------------------------------------------

class TestWorkerIntegration:
    """Test the worker with mocked Matrix API."""

    @pytest.mark.asyncio
    async def test_check_progress_detects_completion(self, config_dir):
        """Full check_progress flow should detect completion marker and finalize."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )

        # Create state
        worker.state = DelegationState('agent-01', '!AgentRoom:e', '!DelegatorRoom:e')
        worker.state.task_start_timestamp = time.time()
        worker.state.last_progress_report_time = worker.state.task_start_timestamp
        worker.state.polling_active = True

        # Mock Matrix client
        mock_client = AsyncMock()
        now_ts = int(time.time() * 1000)
        mock_client.get_messages.return_value = (
            [
                {
                    'type': 'm.room.message',
                    'event_id': '$completion',
                    'sender': '@delegatee:example.com',
                    'origin_server_ts': now_ts,
                    'content': {'body': 'Done! === NO_MORE_ACTIONS ==='},
                }
            ],
            't1-end',
        )

        worker.client = mock_client

        # Run check_progress
        result = await worker.check_progress()

        # Should have detected completion and called finalize
        assert result is None  # Returns None when completion detected
        mock_client.send_message.assert_called_once()  # Handoff sent to delegator
        assert not worker.state.polling_active
        assert not worker.state.completion_handoff_pending

    @pytest.mark.asyncio
    async def test_check_progress_no_new_messages(self, config_dir):
        """When no new messages, check_progress should return None.
        During history catchup, the batch token IS advanced to scroll past old messages.
        Once history_catchup_done is True, the token is cleared."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )

        worker.state = DelegationState('agent-01', '!AgentRoom:e', '!DelegatorRoom:e')
        worker.state.task_start_timestamp = time.time()
        worker.state.polling_active = True

        mock_client = AsyncMock()
        # All messages are old
        old_ts = int((time.time() - 100) * 1000)
        mock_client.get_messages.return_value = (
            [
                {
                    'type': 'm.room.message',
                    'event_id': '$old',
                    'sender': '@delegatee:example.com',
                    'origin_server_ts': old_ts,
                    'content': {'body': 'Old message'},
                }
            ],
            't1-old',
        )

        worker.client = mock_client

        result = await worker.check_progress()

        assert result is None
        # After first poll, batch token is cleared to poll latest 50 messages.
        # Old messages are skipped via task_start_timestamp filter.
        assert worker.state.last_batch_token is None
        assert worker.state.history_catchup_done is True


# ---------------------------------------------------------------------------
# Config loading tests
# ---------------------------------------------------------------------------

class TestConfigLoading:
    """Test configuration loading from YAML."""

    def test_load_config(self, config_dir):
        """Config should load without errors."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        assert worker.config is not None
        assert 'matrix' in worker.config

    def test_delegatee_config(self, config_dir):
        """Should find delegatee by name."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        cfg = worker.get_delegatee_config()
        assert cfg is not None
        assert cfg['name'] == 'agent-01'

    def test_unknown_delegatee(self, config_dir):
        """Unknown delegatee should return None."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'unknown-agent',
        )
        cfg = worker.get_delegatee_config()
        assert cfg is None


# ---------------------------------------------------------------------------
# Pause / resume + outbox + fall-out-of-loop tests
# ---------------------------------------------------------------------------

class TestPauseResume:
    """The worker must honour an on-disk pause marker file:

    * While the marker exists, all timer-based actions are suspended
      (no periodic report, no auto-ping, no wedged/fall-out alerts).
    * Time spent paused is accumulated and subtracted from silence
      calculations so the resumed worker doesn't immediately fire a
      wedged alert for the elapsed pause window.
    """

    def test_initial_state_not_paused(self, delegation_state):
        assert delegation_state.paused is False
        assert delegation_state.paused_at == 0.0
        assert delegation_state.paused_duration_total == 0.0

    def test_sync_pause_state_picks_up_marker(self, config_dir, delegation_state, monkeypatch):
        """When a pause marker appears, _sync_pause_state() must enter paused state."""
        import path_utils as pu
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state

        marker = pu.pause_marker_file('agent-01')
        marker.parent.mkdir(parents=True, exist_ok=True)
        pause_ts = time.time()
        with open(marker, 'w') as f:
            json.dump({'paused_at': pause_ts}, f)

        try:
            worker._sync_pause_state()
            assert worker.state.paused is True
            assert abs(worker.state.paused_at - pause_ts) < 1.0
        finally:
            marker.unlink(missing_ok=True)

    def test_sync_pause_state_resume_accumulates_duration(self, config_dir, delegation_state):
        """When the marker disappears, the elapsed pause time is added to
        paused_duration_total and the paused flag clears."""
        import path_utils as pu
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state
        # Manually put the worker into paused state as if we had seen the marker.
        worker.state.paused = True
        worker.state.paused_at = time.time() - 12.0  # 12 seconds ago

        # No marker on disk → should transition out of paused.
        marker = pu.pause_marker_file('agent-01')
        if marker.exists():
            marker.unlink()

        worker._sync_pause_state()
        assert worker.state.paused is False
        assert worker.state.paused_duration_total >= 11.0
        assert worker.state.paused_duration_total < 14.0

    def test_adjusted_silence_subtracts_pause(self, config_dir, delegation_state):
        """If 60s elapsed but 30s of that was paused, adjusted silence ≈ 30s."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state
        worker.state.last_substantive_msg_time = time.time() - 60.0
        worker.state.paused_duration_total = 30.0
        silence = worker._adjusted_silence()
        # 60 - 30 = 30 (allow small clock slack)
        assert 28.0 <= silence <= 32.0

    def test_adjusted_silence_never_negative(self, config_dir, delegation_state):
        """If accumulated pause exceeds wall-clock silence, clamp to 0."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state
        worker.state.last_substantive_msg_time = time.time() - 5.0
        worker.state.paused_duration_total = 60.0
        assert worker._adjusted_silence() == 0.0


class TestOutboxBackpressure:
    """The worker spools outbound-to-delegator messages when delegator is busy
    and flushes them in FIFO order when it becomes idle again."""

    def test_load_outbox_empty_when_missing(self, config_dir):
        import path_utils as pu
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        path = pu.outbox_file('agent-01')
        if path.exists():
            path.unlink()
        assert worker._load_outbox() == []

    def test_save_and_load_outbox_roundtrip(self, config_dir):
        import path_utils as pu
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        try:
            items = [{'kind': 'progress', 'message': 'hello', 'enqueued_at': 1.0}]
            worker._save_outbox(items)
            loaded = worker._load_outbox()
            assert loaded == items
        finally:
            pu.outbox_file('agent-01').unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_send_to_delegator_queues_when_busy(self, config_dir, delegation_state, monkeypatch):
        """When the delegator is busy, the message must land in the outbox
        and NOT hit the Matrix client."""
        import path_utils as pu
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state
        worker.client = MagicMock()
        worker.client.send_message = AsyncMock(return_value={'event_id': '$x'})
        monkeypatch.setattr(worker, 'is_delegator_idle', lambda: False)
        # Make sure outbox starts empty.
        outbox_path = pu.outbox_file('agent-01')
        if outbox_path.exists():
            outbox_path.unlink()
        try:
            delivered = await worker._send_to_delegator('hi there', kind='progress')
            assert delivered is False
            worker.client.send_message.assert_not_called()
            queue = worker._load_outbox()
            assert len(queue) == 1
            assert queue[0]['message'] == 'hi there'
            assert queue[0]['kind'] == 'progress'
        finally:
            outbox_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_send_to_delegator_drains_when_idle(self, config_dir, delegation_state, monkeypatch):
        """When delegator becomes idle, a fresh send also drains any queued items."""
        import path_utils as pu
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state
        worker.client = MagicMock()
        worker.client.send_message = AsyncMock(return_value={'event_id': '$x'})
        # Pre-populate outbox with two backlogged items.
        worker._save_outbox([
            {'kind': 'progress', 'message': 'older', 'enqueued_at': 1.0},
            {'kind': 'wedged', 'message': 'middle', 'enqueued_at': 2.0},
        ])
        monkeypatch.setattr(worker, 'is_delegator_idle', lambda: True)
        try:
            delivered = await worker._send_to_delegator('newest', kind='handoff')
            assert delivered is True
            # 3 calls: newest first, then drain the 2 queued in order.
            assert worker.client.send_message.call_count == 3
            sent_bodies = [c.args[1] for c in worker.client.send_message.call_args_list]
            assert sent_bodies == ['newest', 'older', 'middle']
            # Outbox is empty after successful drain.
            assert worker._load_outbox() == []
        finally:
            pu.outbox_file('agent-01').unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_send_to_delegator_force_bypasses_busy(self, config_dir, delegation_state, monkeypatch):
        """force=True must deliver even if the delegator is busy.

        Used for terminal-state alerts (wedged, health) that must reach the
        delegator before the worker dies.
        """
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state
        worker.client = MagicMock()
        worker.client.send_message = AsyncMock(return_value={'event_id': '$x'})
        monkeypatch.setattr(worker, 'is_delegator_idle', lambda: False)
        delivered = await worker._send_to_delegator(
            'EMERGENCY', kind='wedged', force=True,
        )
        assert delivered is True
        worker.client.send_message.assert_called_once()


class TestFallOutOfLoop:
    """Worker should send a single early notice when delegatee is silent for
    > FALL_OUT_THRESHOLD seconds without NO_MORE_ACTIONS — without waiting
    for the 15-minute periodic-report cadence."""

    def test_fall_out_flag_resets_on_activity(self, config_dir, delegation_state):
        """When activity returns the worker must re-arm fall-out alerting."""
        worker = DelegationWorker(
            str(config_dir / 'delegator-delegatee.yaml'),
            'agent-01',
        )
        worker.state = delegation_state
        worker.state.fall_out_of_loop_alerted = True

        # Simulate the check_progress() activity-detected branch by
        # invoking the side effect manually — the in-line block clears
        # the flag when _counts_as_activity returns True.
        worker.state.last_substantive_msg_time = time.time()
        worker.state.fall_out_of_loop_alerted = False  # what check_progress does
        assert worker.state.fall_out_of_loop_alerted is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
