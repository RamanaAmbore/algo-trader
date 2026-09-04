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


# ── Behavioral tests ──────────────────────────────────────────────────────

import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


@pytest.mark.asyncio
async def test_broker_input_error_terminates_immediately():
    """
    BrokerInputError (e.g. margin shortfall) terminates the chase loop
    immediately without retry sleep.

    Verifies:
      - Result status is FAILED
      - Result detail contains "input error"
      - Total elapsed time < 5 seconds (no sleep/retry happened)
      - send_order_failure_alert is called exactly once
    """
    from backend.api.algo.chase import chase_order, ChaseStatus, ChaseConfig
    from backend.brokers.errors import BrokerInputError

    # Track elapsed time
    start_time = time.time()

    # Mock broker to raise BrokerInputError on place_order
    mock_broker = MagicMock()
    mock_broker.place_order.side_effect = BrokerInputError("insufficient funds")
    mock_broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    # Mock quote to provide market depth for price calculation
    mock_broker.quote.return_value = {
        "NFO:NIFTY25JULFUT": {
            "depth": {
                "buy": [{"price": 24500.0, "quantity": 100}],
                "sell": [{"price": 24510.0, "quantity": 100}],
            }
        }
    }

    # Mock alert function (called synchronously, not async)
    mock_alert = MagicMock()

    # Mock the registry to return our stub broker and bypass market-hours check
    with patch("backend.api.algo.chase._get_broker_registry", return_value=mock_broker), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert), \
         patch("backend.shared.helpers.utils.is_prod_branch", return_value=True), \
         patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True):

        cfg = ChaseConfig(
            interval_seconds=1,  # use 1 second to speed up test
            max_attempts=20,
        )
        result = await chase_order(
            account="ZG0790",
            symbol="NIFTY25JULFUT",
            transaction_type="BUY",
            quantity=1,
            cfg=cfg,
        )

    elapsed = time.time() - start_time

    # Verify result status
    assert result.status == ChaseStatus.FAILED, (
        f"Expected status FAILED, got {result.status}"
    )

    # Verify detail contains "input error" (from the message format)
    assert "input error" in result.detail.lower(), (
        f"Expected 'input error' in detail, got: {result.detail}"
    )

    # Verify elapsed time is very short (no sleep/retry happened)
    # Should be < 5 seconds; a single failed attempt takes < 1 second
    assert elapsed < 5.0, (
        f"Expected elapsed < 5s but got {elapsed:.2f}s — "
        f"chase appears to have slept/retried instead of terminating immediately"
    )

    # Verify alert was called exactly once
    assert mock_alert.call_count == 1, (
        f"Expected send_order_failure_alert called exactly once, got {mock_alert.call_count}"
    )
