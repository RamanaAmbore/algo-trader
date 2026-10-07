"""
Tests for chase.py's recoverable-vs-not error classification (2026-10 fix):
operator ask — "identify if an order failure is recoverable or not; if
recoverable, retry (with a changed price)".

Covers:
  - `_ch_is_recoverable_error` — typed BrokerError classification
  - `_ch_rejection_is_recoverable` — REJECTED status_message classification
  - `_ch_poll_handle_rejected` retries a price-shaped rejection instead of
    aborting, and still aborts immediately for a non-price rejection
  - `_ch_handle_attempt_error` retries a recoverable exception (with a
    longer backoff for rate-limit) and still aborts immediately for a
    non-recoverable one (extended beyond the original BrokerInputError-only
    case to also cover BrokerCapabilityError / BrokerAuthError)
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.chase import (
    ChaseConfig,
    ChaseResult,
    ChaseStatus,
    _ch_handle_attempt_error,
    _ch_is_recoverable_error,
    _ch_poll_handle_rejected,
    _ch_rejection_is_recoverable,
)
from backend.brokers.errors import (
    BrokerAuthError,
    BrokerCapabilityError,
    BrokerInputError,
    BrokerRateLimitError,
)


def test_recoverable_error_classification():
    assert _ch_is_recoverable_error(BrokerInputError("bad qty")) is False
    assert _ch_is_recoverable_error(BrokerCapabilityError("unsupported")) is False
    assert _ch_is_recoverable_error(BrokerAuthError("token expired")) is False
    # Recoverable: rate limit, and anything untyped (conservative default).
    assert _ch_is_recoverable_error(BrokerRateLimitError("slow down")) is True
    assert _ch_is_recoverable_error(ConnectionError("reset")) is True
    assert _ch_is_recoverable_error(RuntimeError("unexpected")) is True


def test_rejection_message_classification():
    assert _ch_rejection_is_recoverable("price out of circuit range") is True
    assert _ch_rejection_is_recoverable("Tick size violation") is True
    assert _ch_rejection_is_recoverable("stale quote") is True
    assert _ch_rejection_is_recoverable("insufficient margin") is False
    assert _ch_rejection_is_recoverable("RMS:Blocked for trading") is False
    assert _ch_rejection_is_recoverable("") is False
    assert _ch_rejection_is_recoverable(None) is False


def test_margin_rejection_never_recoverable_even_with_price_wording():
    """Operator instruction (2026-10): margin errors are not recoverable,
    even if the message also happens to mention price/range."""
    assert _ch_rejection_is_recoverable(
        "margin shortfall for this price range"
    ) is False
    assert _ch_rejection_is_recoverable("RMS: price band blocked") is False


def _cfg(max_attempts=5):
    return ChaseConfig(interval_seconds=1, max_attempts=max_attempts, exchange="NFO")


def test_poll_handle_rejected_retries_price_rejection():
    result = ChaseResult()
    emit = MagicMock()
    status = {"status_message": "price not within circuit limit"}

    signal, remaining = _ch_poll_handle_rejected(
        result, status, attempt=2, remaining_qty=10,
        account="ZG0790", symbol="NIFTY25JULFUT", transaction_type="BUY",
        quantity=10, current_order_id="OID1", cfg=_cfg(max_attempts=5),
        algo_order_id=None, emit=emit,
    )

    assert signal == "rejected_continue"
    assert remaining == 10
    assert result.status != ChaseStatus.FAILED
    emit.assert_called_once()
    assert emit.call_args[0][0] == "chase_reprice"


@pytest.mark.asyncio
async def test_poll_handle_rejected_does_not_retry_past_max_attempts():
    """Even a price-shaped rejection aborts on the LAST attempt — no
    attempts remain to retry with."""
    result = ChaseResult()
    emit = MagicMock()
    status = {"status_message": "price out of range"}

    with patch("backend.api.algo.chase._emit_chase_terminal", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        signal, _ = _ch_poll_handle_rejected(
            result, status, attempt=5, remaining_qty=10,
            account="ZG0790", symbol="NIFTY25JULFUT", transaction_type="BUY",
            quantity=10, current_order_id="OID1", cfg=_cfg(max_attempts=5),
            algo_order_id=None, emit=emit,
        )

    assert signal == "rejected"
    assert result.status == ChaseStatus.FAILED


@pytest.mark.asyncio
async def test_poll_handle_rejected_aborts_non_price_reason():
    result = ChaseResult()
    emit = MagicMock()
    status = {"status_message": "insufficient margin"}
    mock_alert = MagicMock()

    with patch("backend.api.algo.chase._emit_chase_terminal", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        signal, remaining = _ch_poll_handle_rejected(
            result, status, attempt=2, remaining_qty=10,
            account="ZG0790", symbol="NIFTY25JULFUT", transaction_type="BUY",
            quantity=10, current_order_id="OID1", cfg=_cfg(max_attempts=5),
            algo_order_id=None, emit=emit,
        )

    assert signal == "rejected"
    assert remaining == 10
    assert result.status == ChaseStatus.FAILED
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_handle_attempt_error_retries_recoverable_exception():
    result = ChaseResult()
    emit = MagicMock()
    with patch("backend.api.algo.chase.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        abort, consecutive = await _ch_handle_attempt_error(
            ConnectionError("network blip"), consecutive_errors=0, attempt=2,
            symbol="NIFTY25JULFUT", account="ZG0790", transaction_type="BUY",
            quantity=10, current_order_id="OID1", cfg=_cfg(max_attempts=5),
            result=result, emit=emit, algo_order_id=None,
        )

    assert abort is None
    assert consecutive == 1
    mock_sleep.assert_awaited_once_with(1)  # cfg.interval_seconds, no rate-limit bump


@pytest.mark.asyncio
async def test_handle_attempt_error_uses_longer_backoff_for_rate_limit():
    result = ChaseResult()
    emit = MagicMock()
    with patch("backend.api.algo.chase.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        abort, consecutive = await _ch_handle_attempt_error(
            BrokerRateLimitError("slow down"), consecutive_errors=0, attempt=2,
            symbol="NIFTY25JULFUT", account="ZG0790", transaction_type="BUY",
            quantity=10, current_order_id="OID1", cfg=_cfg(max_attempts=5),
            result=result, emit=emit, algo_order_id=None,
        )

    assert abort is None
    assert consecutive == 1
    mock_sleep.assert_awaited_once_with(30)  # _CH_RATE_LIMIT_BACKOFF_SECONDS, not interval_seconds=1


@pytest.mark.asyncio
async def test_handle_attempt_error_aborts_on_auth_error():
    result = ChaseResult()
    emit = MagicMock()
    mock_alert = MagicMock()
    with patch("backend.api.algo.chase._emit_chase_terminal", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", mock_alert):
        abort, consecutive = await _ch_handle_attempt_error(
            BrokerAuthError("token expired"), consecutive_errors=0, attempt=2,
            symbol="NIFTY25JULFUT", account="ZG0790", transaction_type="BUY",
            quantity=10, current_order_id="OID1", cfg=_cfg(max_attempts=5),
            result=result, emit=emit, algo_order_id=None,
        )

    assert abort is not None
    assert abort.status == ChaseStatus.FAILED
    assert "auth_invalid" in abort.detail
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_handle_attempt_error_aborts_on_capability_error():
    result = ChaseResult()
    emit = MagicMock()
    with patch("backend.api.algo.chase._emit_chase_terminal", new_callable=AsyncMock), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert", MagicMock()):
        abort, consecutive = await _ch_handle_attempt_error(
            BrokerCapabilityError("MARKET not supported"), consecutive_errors=0, attempt=2,
            symbol="NIFTY25JULFUT", account="ZG0790", transaction_type="BUY",
            quantity=10, current_order_id="OID1", cfg=_cfg(max_attempts=5),
            result=result, emit=emit, algo_order_id=None,
        )

    assert abort is not None
    assert abort.status == ChaseStatus.FAILED
    assert "capability_unsupported" in abort.detail
