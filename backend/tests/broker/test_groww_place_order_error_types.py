"""
Regression test for the 2026-10 Groww `place_order` typed-error-
preservation fix (groww.py).

`GrowwBroker.place_order` called `self.groww.place_order(...)` (the
growwapi SDK itself) with NO try/except around that call at all — unlike
Kite's `_kite_exc` wrapper and Dhan's (just-fixed) typed-error-preserving
wrapper. Any SDK-raised exception (network error, auth/session expiry,
or any SDK-level validation exception) propagated completely untyped
out of `place_order`.

This mattered because `backend/api/algo/chase.py:_ch_is_recoverable_error()`
classifies retry-vs-abort behaviour via `isinstance()` against the typed
`BrokerError` hierarchy — a genuine auth/session-expiry failure
misclassified as "unknown" is treated as recoverable (retried) by chase
instead of aborted, defeating the whole point of typed classification.

Fix (3-tier, see groww.py `place_order` / `_place_order_impl` docstrings):

  1. `_place_order_impl` (still `@_retry_groww_auth`-decorated, owns the
     actual SDK call) re-raises an already-typed `BrokerError` unchanged
     (`except BrokerError: raise`), re-raises the SDK's own known
     exception types (`GrowwAPIException` subtypes + SSL errors)
     UNCHANGED so `@_retry_groww_auth`'s own retry/backoff/re-mint logic
     still sees the real SDK type on every attempt, and wraps any
     OTHER genuinely untyped exception as `BrokerNetworkError` (the
     same documented "unknown failure, treat as transient/network-
     shaped" fallback Kite/Dhan use).
  2. The public `place_order` is now a thin outer layer: it classifies
     whatever `GrowwAPIException` subtype or SSL error finally
     survives `_place_order_impl` (including after retries in
     `@_retry_groww_auth` are exhausted) via the previously-unused
     `_groww_exc(e, status=...)` helper, reusing the existing status-
     based mapping instead of duplicating the dict-response rejection
     mapping further down `_place_order_impl`.

Five quality dimensions:
  SSOT        — reuses the existing (previously dead) `_groww_exc` helper
                for status-based classification instead of duplicating it
  Correctness — typed errors preserved; retryable SDK types still retried
                by `@_retry_groww_auth`; untyped exceptions wrapped
  Performance — no extra SDK calls added; retry cadence unchanged
  Reuse       — mirrors Kite's `_kite_exc` / Dhan's typed-preservation
                pattern already established in this codebase
  UX          — chase.py sees a real BrokerError type to decide retry vs
                abort, instead of an opaque raw SDK exception
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.brokers.adapters.groww import GrowwBroker
from backend.brokers.errors import (
    BrokerAuthError, BrokerInputError, BrokerNetworkError, BrokerRateLimitError,
)

try:
    from growwapi.groww.exceptions import (
        GrowwAPIAuthenticationException,
        GrowwAPIBadRequestException,
        GrowwAPIRateLimitException,
    )
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _SDK_AVAILABLE,
    reason="growwapi SDK not installed",
)


def _base_kwargs(**overrides):
    kwargs = dict(
        exchange="NSE",
        tradingsymbol="RELIANCE",
        transaction_type="BUY",
        quantity=1,
        order_type="MARKET",
        product="MIS",
    )
    kwargs.update(overrides)
    return kwargs


def _make_broker(sdk_handle) -> GrowwBroker:
    conn = MagicMock()
    conn.account = "GRW_test"
    conn.get_groww_conn = MagicMock(return_value=sdk_handle)
    return GrowwBroker(conn)


class TestGrowwPlaceOrderTypedErrorPreservation:
    def test_already_typed_broker_auth_error_propagates_unchanged(self):
        """An exception already typed as BrokerAuthError — raised from
        inside the code path place_order's try block wraps — must
        propagate out of place_order AS BrokerAuthError, not get
        re-wrapped as BrokerNetworkError or anything else."""
        sdk = MagicMock()
        sdk.place_order.side_effect = BrokerAuthError(
            "session expired", broker="groww"
        )
        broker = _make_broker(sdk)

        with pytest.raises(BrokerAuthError):
            broker.place_order(**_base_kwargs())

    def test_untyped_connection_error_wrapped_as_network_error(self):
        """A genuinely untyped exception (not a growwapi SDK exception,
        not already a BrokerError) must be wrapped as BrokerNetworkError
        — the fallback behaviour must not regress."""
        sdk = MagicMock()
        sdk.place_order.side_effect = ConnectionResetError("connection reset by peer")
        broker = _make_broker(sdk)

        with pytest.raises(BrokerNetworkError):
            broker.place_order(**_base_kwargs())

    def test_untyped_value_error_wrapped_as_network_error(self):
        """Same fallback guarantee for a bare ValueError raised from
        inside the SDK call itself."""
        sdk = MagicMock()
        sdk.place_order.side_effect = ValueError("unexpected SDK response shape")
        broker = _make_broker(sdk)

        with pytest.raises(BrokerNetworkError):
            broker.place_order(**_base_kwargs())

    def test_bad_request_mapped_to_broker_input_error(self):
        """GrowwAPIBadRequestException (SDK .code == '400') is a known,
        distinguishable SDK exception type — reusing `_groww_exc`'s
        existing status-based mapping, it must surface as
        BrokerInputError, not a generic/untyped failure, and must NOT be
        retried by @_retry_groww_auth (no except clause for it there)."""
        sdk = MagicMock()
        sdk.place_order.side_effect = GrowwAPIBadRequestException()
        broker = _make_broker(sdk)

        with pytest.raises(BrokerInputError):
            broker.place_order(**_base_kwargs())
        assert sdk.place_order.call_count == 1, (
            "BadRequest must not be retried by @_retry_groww_auth"
        )

    def test_auth_failure_retried_then_typed_on_exhaustion(self):
        """A persistent auth failure: @_retry_groww_auth re-mints the
        token and retries ONCE (existing, unchanged retry behaviour —
        confirmed by the SDK being called exactly twice and
        conn.refresh() being called exactly once), and the exception
        surviving that one retry is classified as BrokerAuthError by the
        outer place_order() layer instead of leaking out as a raw
        GrowwAPIAuthenticationException."""
        sdk = MagicMock()
        sdk.place_order.side_effect = GrowwAPIAuthenticationException()
        broker = _make_broker(sdk)

        with pytest.raises(BrokerAuthError):
            broker.place_order(**_base_kwargs())

        assert sdk.place_order.call_count == 2, (
            "Expected exactly one re-mint retry (existing behaviour) "
            "before the outer layer classifies the final failure"
        )
        broker._conn.refresh.assert_called_once()

    def test_auth_failure_recovers_on_retry_returns_order_id(self):
        """If the re-mint retry succeeds, place_order must return
        normally — the outer classification layer must not interfere
        with a successful retry."""
        calls = {"n": 0}

        def _side_effect(**_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise GrowwAPIAuthenticationException()
            return {"data": {"groww_order_id": "GRW-ORDER-123"}}

        sdk = MagicMock()
        sdk.place_order.side_effect = _side_effect
        broker = _make_broker(sdk)

        order_id = broker.place_order(**_base_kwargs())

        assert order_id == "GRW-ORDER-123"
        assert calls["n"] == 2
        broker._conn.refresh.assert_called_once()

    def test_rate_limit_exhausted_then_typed_as_rate_limit_error(self):
        """A persistent rate-limit failure: @_retry_groww_auth exhausts
        its 4 backoff retries (5 total attempts — existing, unchanged
        behaviour), and the final failure is classified as
        BrokerRateLimitError by the outer layer."""
        sdk = MagicMock()
        sdk.place_order.side_effect = GrowwAPIRateLimitException()
        broker = _make_broker(sdk)

        from unittest.mock import patch
        with patch("backend.brokers.adapters.groww._time") as mock_time:
            mock_time.sleep = MagicMock()
            with pytest.raises(BrokerRateLimitError):
                broker.place_order(**_base_kwargs())

        assert sdk.place_order.call_count == 5, (
            "Expected first attempt + 4 backoff retries (existing behaviour)"
        )

    def test_amo_variety_not_implemented_error_passes_through(self):
        """The pre-SDK-call AMO guard must keep raising NotImplementedError
        unconverted — the outer classification layer must not catch it,
        otherwise chase could be fooled into retrying an order type this
        adapter deliberately refuses to place."""
        sdk = MagicMock()
        broker = _make_broker(sdk)

        with pytest.raises(NotImplementedError):
            broker.place_order(**_base_kwargs(variety="amo"))
        sdk.place_order.assert_not_called()

    def test_successful_order_returns_order_id(self):
        """Baseline happy path must be unaffected by the refactor."""
        sdk = MagicMock()
        sdk.place_order.return_value = {"data": {"groww_order_id": "GRW-999"}}
        broker = _make_broker(sdk)

        order_id = broker.place_order(**_base_kwargs())
        assert order_id == "GRW-999"

    def test_existing_dict_rejection_mapping_unchanged(self):
        """The existing successful-call-but-rejection-shaped dict
        response mapping (400/422 → BrokerInputError, else →
        BrokerOrderError) must still work exactly as before — this
        fix only adds exception handling AROUND the SDK call, it does
        not touch this mapping."""
        from backend.brokers.errors import BrokerOrderError

        sdk = MagicMock()
        sdk.place_order.return_value = {
            "httpStatus": 422, "message": "invalid quantity",
        }
        broker = _make_broker(sdk)
        with pytest.raises(BrokerInputError):
            broker.place_order(**_base_kwargs())

        sdk2 = MagicMock()
        sdk2.place_order.return_value = {
            "httpStatus": 500, "message": "exchange rejected",
        }
        broker2 = _make_broker(sdk2)
        with pytest.raises(BrokerOrderError):
            broker2.place_order(**_base_kwargs())
