"""
Regression test for the 2026-10 Dhan `place_order` typed-error-preservation
fix (dhan.py).

`DhanBroker.place_order`'s blanket `except Exception as e: raise
BrokerNetworkError(str(e)) from e` used to re-wrap ANY exception raised
while calling `self._sdk_orders.place_order(...)` as `BrokerNetworkError`
— including exceptions that were ALREADY typed `BrokerError` subclasses
(e.g. `BrokerRateLimitError` from a DH-904 response, or `BrokerAuthError`
from a persisted auth failure), both of which can originate from inside
`_DhanSDKProxy._invoke`/`_raw_call` — i.e. from INSIDE the try block that
wraps `self._sdk_orders.place_order(...)`.

This mattered because `backend/api/algo/chase.py:_ch_is_recoverable_error()`
classifies retry-vs-abort behaviour via `isinstance()` against the typed
`BrokerError` hierarchy — a genuine `BrokerAuthError`/`BrokerInputError`
misclassified as `BrokerNetworkError` would be retried by chase instead of
aborted immediately, defeating the whole point of typed-error
classification for the Dhan adapter.

Fix: `place_order` now re-raises an already-typed `BrokerError` unchanged
(`except BrokerError: raise`) BEFORE the fallback `except Exception`
wraps genuinely untyped exceptions as `BrokerNetworkError`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.brokers.adapters.dhan import DhanBroker
from backend.brokers.errors import BrokerAuthError, BrokerNetworkError, BrokerRateLimitError


def _base_kwargs(**overrides):
    kwargs = dict(
        exchange="NSE",
        tradingsymbol="RELIANCE",
        security_id="1333",   # pre-resolved — skips _resolve_security_id lookup
        transaction_type="BUY",
        quantity=1,
        order_type="MARKET",
        product="MIS",
    )
    kwargs.update(overrides)
    return kwargs


def _make_broker(sdk_handle) -> DhanBroker:
    conn_mock = MagicMock()
    conn_mock.get_dhan_conn.return_value = sdk_handle
    return DhanBroker(conn=conn_mock)


class TestDhanPlaceOrderTypedErrorPreservation:
    def test_already_typed_broker_auth_error_propagates_unchanged(self):
        """An exception already typed as BrokerAuthError — raised from
        inside the code path place_order's try block wraps — must
        propagate out of place_order AS BrokerAuthError, not get
        re-wrapped as BrokerNetworkError."""
        sdk = MagicMock()
        sdk.place_order.side_effect = BrokerAuthError(
            "session expired", broker="dhan", code="DH-901"
        )
        broker = _make_broker(sdk)

        with pytest.raises(BrokerAuthError):
            broker.place_order(**_base_kwargs())

    def test_dh904_rate_limit_response_propagates_as_rate_limit_error(self):
        """Realistic path: the SDK proxy's own DH-904 detection (inside
        _DhanSDKProxy._invoke/_raw_call, which executes INSIDE
        place_order's try block) raises BrokerRateLimitError — this must
        also survive place_order's blanket except unchanged."""
        sdk = MagicMock()
        sdk.place_order.return_value = {
            "code": "DH-904", "remarks": "rate limit exceeded",
        }
        broker = _make_broker(sdk)

        with pytest.raises(BrokerRateLimitError):
            broker.place_order(**_base_kwargs())

    def test_untyped_exception_still_wrapped_as_network_error(self):
        """Fallback behavior must NOT regress: a genuinely untyped
        exception (not already a BrokerError subclass) is still wrapped
        as BrokerNetworkError."""
        sdk = MagicMock()
        sdk.place_order.side_effect = ConnectionResetError("connection reset by peer")
        broker = _make_broker(sdk)

        with pytest.raises(BrokerNetworkError):
            broker.place_order(**_base_kwargs())

    def test_untyped_value_error_still_wrapped_as_network_error(self):
        """Same fallback guarantee for a bare ValueError from the SDK."""
        sdk = MagicMock()
        sdk.place_order.side_effect = ValueError("unexpected SDK response shape")
        broker = _make_broker(sdk)

        with pytest.raises(BrokerNetworkError):
            broker.place_order(**_base_kwargs())
