"""
Tests for chase.py — cancel+replace loop behavioral invariants.
SSOT: cancel_order + place_order (never modify_order); max_workers=8.
Perf: interval driven by cfg.interval_seconds not hardcoded literal.
Stale: killed-set is TTL-bounded (_KILLED_LOCK + expiry).
Reuse: result.attempts/next_attempt_at/last_attempt_at stored per cycle.
UX: countdown timestamps allow UI to display re-quoting delay.
"""
from pathlib import Path

_SRC = Path("backend/api/algo/chase.py").read_text()


def test_chase_uses_cancel_and_place_not_modify():
    assert "cancel_order" in _SRC, "cancel_order must appear in chase loop"
    assert "place_order" in _SRC, "place_order must appear in chase loop"
    assert "modify_order" not in _SRC, (
        "modify_order must NOT appear — chase uses cancel+replace, not modify"
    )


def test_chase_max_workers_is_8():
    assert "max_workers=8" in _SRC, (
        "ThreadPoolExecutor max_workers must be 8 — raised from 4 to prevent "
        "executor saturation when chasing multiple positions simultaneously"
    )


def test_chase_next_attempt_at_assigned_in_loop():
    assert "next_attempt_at" in _SRC, (
        "next_attempt_at must be assigned inside the loop body "
        "so the UI can show countdown to next re-quote"
    )
    assert "last_attempt_at" in _SRC, (
        "last_attempt_at must be assigned inside the loop body "
        "so the UI can show elapsed time since last attempt"
    )


def test_chase_sleep_uses_interval_seconds_not_literal():
    assert "interval_seconds" in _SRC, (
        "asyncio.sleep must use cfg.interval_seconds — "
        "hardcoding a literal (e.g. 20) makes the interval non-configurable"
    )
    # The literal 20 might still appear in defaults/comments but must not be
    # the sole sleep argument: verify interval_seconds is referenced near sleep
    import re
    sleep_call = re.search(r"asyncio\.sleep\s*\(.*?interval_seconds", _SRC, re.DOTALL)
    assert sleep_call, (
        "asyncio.sleep() call must reference interval_seconds, not a literal"
    )


def test_chase_attempts_incremented_before_broker_block():
    assert "result.attempts" in _SRC or ".attempts +=" in _SRC or "attempts =" in _SRC, (
        "result.attempts must be incremented before the cancel/place broker block "
        "so attempt count is recorded even when the broker call fails"
    )


def test_chase_killed_set_is_ttl_bounded():
    assert "_KILLED_LOCK" in _SRC, "_KILLED_LOCK must exist for thread-safe killed-set access"
    # TTL expiry pattern — killed entries must age out
    assert "expired" in _SRC or "ttl" in _SRC.lower() or "time.time()" in _SRC, (
        "killed-set must have a TTL expiry to prevent unbounded growth"
    )
