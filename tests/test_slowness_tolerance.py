"""Tolerance-to-slowness tests.

The detector must target exactly ONE condition — the delegatee fell out of its
tool-call loop and is doing nothing — and must NOT mistake a slow-but-working
agent (long uncached prefill / long generation) for a wedge. Hermes surfaces
in-flight-request markers while the model is working; those must count as
activity so the silence timer never accumulates during a slow turn.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from delegation_worker import DelegationWorker  # noqa: E402

CA = DelegationWorker._counts_as_activity


# ---- in-flight prefill/generation markers MUST count as activity (working) ----
def test_waiting_for_stream_counts_as_activity():
    # the exact throbber seen during a 221s uncached prefill
    body = "⏳ Working — 20 min — iteration 34/400, waiting for stream response (221s, no chunks yet)"
    assert CA(body) is True


def test_no_chunks_yet_counts_as_activity():
    assert CA("waiting for stream response (150s, no chunks yet)") is True


def test_receiving_stream_counts_as_activity():
    assert CA("⏳ Working — receiving stream response") is True


def test_working_throbber_counts_as_activity():
    assert CA("⏳ Working — 10 min elapsed — iteration 5/200") is True


def test_edited_inflight_line_counts_as_activity():
    # Hermes prepends '* ' to edited messages — must still be recognised
    assert CA("* ⏳ Working — waiting for stream response (90s, no chunks yet)") is True


# ---- genuine wedge signals still do NOT count as activity ----
def test_stream_stalled_is_not_activity():
    assert CA("⚠️ Stream stalled — no data") is False


def test_retrying_is_not_activity():
    assert CA("⏳ Retrying (2/5)") is False


def test_empty_response_is_not_activity():
    assert CA("⚠️ Empty response from model") is False


# ---- real work still counts ----
def test_tool_call_counts_as_activity():
    assert CA("💻 terminal ``` ls ```") is True


def test_empty_body_is_not_activity():
    assert CA("") is False


# ---- fall-out thresholds are CALCULATED from the Hermes heartbeat, not magic ----
def test_thresholds_derive_from_heartbeat():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "delegation_worker.py")).read()
    # thresholds must be expressed as multiples of HERMES_HEARTBEAT_SEC
    assert "HERMES_HEARTBEAT_SEC" in src
    assert "FALL_OUT_THRESHOLD = 2.0 * HERMES_HEARTBEAT_SEC" in src
    assert "IDLE_PING_THRESHOLD = 2.0 * HERMES_HEARTBEAT_SEC" in src
    # and no longer hard-coded to the old aggressive 5-min / 12-min values
    assert "FALL_OUT_THRESHOLD = 300.0" not in src
    assert "IDLE_PING_THRESHOLD = 720.0" not in src


def test_idle_check_fails_safe_to_busy():
    # when the delegatee state is unreadable we must assume BUSY, never idle
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "delegation_worker.py")).read()
    assert "return True  # Default-idle when state unreadable" not in src
    assert "fail-safe: assume BUSY when state unreadable" in src


# ---- nudges must use /steer so they never interrupt a working (prefilling) agent ----
def test_pings_use_steer_prefix():
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "delegation_worker.py")).read()
    # both the fall-out ping and the auto-ping must be /steer-prefixed
    assert '"/steer Continue with the task.' in src
    assert '"/steer Are you still working?' in src
    # and no bare (interrupting) variants remain
    assert '"Continue with the task. If finished' not in src
    assert '"Are you still working? You' not in src
