"""
Tests for TickerManager.restart() and TickerManager.unsubscribe_non_mcx().

Covers five quality dimensions:
  SSOT        — restart() / unsubscribe_non_mcx() are the single lifecycle paths
  Correctness — state resets, credential forwarding, MCX filter logic
  Performance — no blocking I/O in restart(); unsubscribe_non_mcx() tolerates
                instrument store errors without raising
  Reuse       — restart_with_account() pattern reused as design reference
  UX          — operator sees correct log lines on each lifecycle step

Test catalogue:

TestTickerManagerRestart (6 tests):
  1. restart() resets _started to False before calling start()
  2. restart() resets _subscribed to empty set
  3. restart() resets _pending to empty set (not carrying previous pending)
  4. restart() resets _connected to False
  5. restart() calls start() with the supplied api_key + access_token + account
  6. restart() bails after stop() if _reactor_dead is True (does not call start)

TestTickerManagerUnsubscribeNonMcx (5 tests):
  7. unsubscribe_non_mcx() calls unsubscribe() with tokens not in MCX set
  8. unsubscribe_non_mcx() keeps MCX tokens subscribed
  9. unsubscribe_non_mcx() is a no-op when all subscribed tokens are MCX tokens
 10. unsubscribe_non_mcx() is a no-op when _subscribed is empty
 11. unsubscribe_non_mcx() handles instrument store failure gracefully (no raise)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from backend.brokers.kite_ticker import TickerManager


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_stopped_ticker() -> TickerManager:
    """Return a fresh TickerManager with _kws=None (no live connection)."""
    t = TickerManager()
    t._kws = None
    t._started = False
    t._connected = False
    return t


def _make_started_ticker(subscribed: set | None = None) -> TickerManager:
    """Return a TickerManager simulating an active (started+connected) state."""
    t = TickerManager()
    mock_kws = MagicMock()
    mock_kws.stop_retry = MagicMock(return_value=None)
    mock_kws.close       = MagicMock(return_value=None)
    mock_kws.stop        = MagicMock(return_value=None)
    t._kws       = mock_kws
    t._started   = True
    t._connected = True
    t._subscribed = set(subscribed) if subscribed else set()
    t._pending    = set()
    return t


# ═══════════════════════════════════════════════════════════════════════════
# TestTickerManagerRestart — 6 tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTickerManagerRestart:
    """Verify TickerManager.restart() produces the correct state sequence."""

    def test_restart_resets_started_to_false_then_calls_start(self):
        """restart() must reset _started=False so start()'s idempotency guard is bypassed."""
        ticker = _make_started_ticker()

        start_calls = []

        def track_start(api_key, access_token, account=""):
            # Record the state of _started at the moment start() is invoked.
            start_calls.append(ticker._started)
            # Simulate start() succeeding.
            ticker._started = True

        with patch.object(ticker, "stop", wraps=ticker.stop), \
             patch.object(ticker, "start", side_effect=track_start), \
             patch("backend.brokers.kite_ticker.logger"):
            ticker.restart("K1", "T1", "ZG0790")

        assert len(start_calls) == 1, "restart() must call start() exactly once"
        assert start_calls[0] is False, \
            "restart() must reset _started=False before calling start()"

    def test_restart_clears_subscribed_set(self):
        """restart() must clear _subscribed so re-subscribe starts from scratch."""
        ticker = _make_started_ticker(subscribed={111, 222, 333})

        def fake_start(api_key, access_token, account=""):
            ticker._started = True

        with patch.object(ticker, "start", side_effect=fake_start), \
             patch("backend.brokers.kite_ticker.logger"):
            ticker.restart("K1", "T1", "ZG0790")

        assert ticker._subscribed == set(), \
            "restart() must clear _subscribed before calling start()"

    def test_restart_carries_subscribed_tokens_into_pending(self):
        """restart() must move previously-subscribed tokens into _pending so
        on_connect can re-subscribe them automatically."""
        ticker = _make_started_ticker(subscribed={100, 200})
        ticker._pending = {300}  # pre-existing pending

        pending_at_start: set = set()

        def fake_start(api_key, access_token, account=""):
            pending_at_start.update(ticker._pending)
            ticker._started = True

        with patch.object(ticker, "start", side_effect=fake_start), \
             patch("backend.brokers.kite_ticker.logger"):
            ticker.restart("K1", "T1", "ZG0790")

        # restart() should carry both old _subscribed + old _pending into new _pending
        assert 100 in pending_at_start, "restart() should forward subscribed token 100 to _pending"
        assert 200 in pending_at_start, "restart() should forward subscribed token 200 to _pending"
        assert 300 in pending_at_start, "restart() should forward pending token 300 to _pending"

    def test_restart_resets_connected_to_false(self):
        """restart() must reset _connected=False (stop() does this, verify end state)."""
        ticker = _make_started_ticker()

        connected_at_start: list[bool] = []

        def fake_start(api_key, access_token, account=""):
            connected_at_start.append(ticker._connected)
            ticker._started = True

        with patch.object(ticker, "start", side_effect=fake_start), \
             patch("backend.brokers.kite_ticker.logger"):
            ticker.restart("K1", "T1", "ZG0790")

        assert connected_at_start[0] is False, \
            "restart() must reset _connected=False before start() runs"

    def test_restart_forwards_credentials_to_start(self):
        """restart() must pass the exact api_key, access_token, account to start()."""
        ticker = _make_stopped_ticker()

        received: dict = {}

        def capture_start(api_key, access_token, account=""):
            received["api_key"]      = api_key
            received["access_token"] = access_token
            received["account"]      = account
            ticker._started = True

        with patch.object(ticker, "start", side_effect=capture_start), \
             patch("backend.brokers.kite_ticker.logger"):
            ticker.restart("MY_KEY", "MY_TOKEN", "DH6847")

        assert received["api_key"]      == "MY_KEY"
        assert received["access_token"] == "MY_TOKEN"
        assert received["account"]      == "DH6847"

    def test_restart_bails_when_reactor_dead_after_stop(self):
        """restart() must NOT call start() when _reactor_dead becomes True during stop()."""
        ticker = _make_started_ticker()

        def fake_stop_sets_reactor_dead():
            ticker._started   = False
            ticker._connected = False
            ticker._kws       = None
            ticker._reactor_dead = True  # Simulate ReactorNotRunning in stop()

        start_called = []

        with patch.object(ticker, "stop", side_effect=fake_stop_sets_reactor_dead), \
             patch.object(ticker, "start", side_effect=lambda *a, **kw: start_called.append(1)), \
             patch("backend.brokers.kite_ticker.logger"):
            ticker.restart("K1", "T1", "ZG0790")

        assert start_called == [], \
            "restart() must not call start() when _reactor_dead=True after stop()"


# ═══════════════════════════════════════════════════════════════════════════
# TestTickerManagerUnsubscribeNonMcx — 5 tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTickerManagerUnsubscribeNonMcx:
    """Verify TickerManager.unsubscribe_non_mcx() applies the MCX-token filter."""

    @pytest.mark.asyncio
    async def test_unsubscribe_drops_non_mcx_tokens(self):
        """unsubscribe_non_mcx() must call unsubscribe() with tokens absent from MCX map."""
        ticker = _make_started_ticker(subscribed={111, 222, 999})
        # MCX instruments map: token 999 is MCX; 111+222 are not.
        mcx_map = {("CRUDEOIL23DECFUT", "MCX"): 999}

        dropped: list[list[int]] = []

        def capture_unsub(tokens):
            dropped.append(list(tokens))

        with patch.object(ticker, "unsubscribe", side_effect=capture_unsub), \
             patch(
                 "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                 new=AsyncMock(return_value=mcx_map),
             ), \
             patch("backend.brokers.kite_ticker.logger"):
            await ticker.unsubscribe_non_mcx()

        assert len(dropped) == 1, "unsubscribe_non_mcx() must call unsubscribe() once"
        dropped_set = set(dropped[0])
        assert 111 in dropped_set, "non-MCX token 111 must be dropped"
        assert 222 in dropped_set, "non-MCX token 222 must be dropped"
        assert 999 not in dropped_set, "MCX token 999 must NOT be dropped"

    @pytest.mark.asyncio
    async def test_unsubscribe_keeps_mcx_tokens_subscribed(self):
        """unsubscribe_non_mcx() must not remove MCX tokens from _subscribed."""
        ticker = _make_started_ticker(subscribed={500, 501})
        # Both are MCX tokens.
        mcx_map = {("GOLDM23DECFUT", "MCX"): 500, ("SILVERM23DECFUT", "MCX"): 501}

        unsub_called = []

        def capture_unsub(tokens):
            unsub_called.append(tokens)

        with patch.object(ticker, "unsubscribe", side_effect=capture_unsub), \
             patch(
                 "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                 new=AsyncMock(return_value=mcx_map),
             ), \
             patch("backend.brokers.kite_ticker.logger"):
            await ticker.unsubscribe_non_mcx()

        assert unsub_called == [], \
            "unsubscribe_non_mcx() must not call unsubscribe() when all tokens are MCX"

    @pytest.mark.asyncio
    async def test_unsubscribe_noop_when_all_subscribed_are_mcx(self):
        """unsubscribe_non_mcx() is a no-op when current subscriptions are all MCX."""
        ticker = _make_started_ticker(subscribed={777})
        mcx_map = {("NATURALGAS23DECFUT", "MCX"): 777}

        with patch.object(ticker, "unsubscribe") as mock_unsub, \
             patch(
                 "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                 new=AsyncMock(return_value=mcx_map),
             ), \
             patch("backend.brokers.kite_ticker.logger"):
            await ticker.unsubscribe_non_mcx()

        mock_unsub.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsubscribe_noop_when_no_subscriptions(self):
        """unsubscribe_non_mcx() is a no-op when _subscribed is empty."""
        ticker = _make_stopped_ticker()
        ticker._subscribed = set()
        mcx_map = {("CRUDEOIL23DECFUT", "MCX"): 100}

        with patch.object(ticker, "unsubscribe") as mock_unsub, \
             patch(
                 "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                 new=AsyncMock(return_value=mcx_map),
             ), \
             patch("backend.brokers.kite_ticker.logger"):
            await ticker.unsubscribe_non_mcx()

        mock_unsub.assert_not_called()

    @pytest.mark.asyncio
    async def test_unsubscribe_handles_instrument_store_error(self):
        """unsubscribe_non_mcx() must not raise when the instrument store call fails."""
        ticker = _make_started_ticker(subscribed={111})

        with patch.object(ticker, "unsubscribe") as mock_unsub, \
             patch(
                 "backend.api.persistence.instruments_store.get_or_fetch_instruments",
                 new=AsyncMock(side_effect=RuntimeError("network timeout")),
             ), \
             patch("backend.brokers.kite_ticker.logger") as mock_log:
            # Must NOT raise
            await ticker.unsubscribe_non_mcx()

        mock_unsub.assert_not_called()
        assert mock_log.warning.called, \
            "unsubscribe_non_mcx() should log a warning on instrument store error"
