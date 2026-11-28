"""Tests for the Dhan historical trade/ledger backfill plumbing added
2026-11-27:

  - `DhanBroker.trade_history()` / `DhanBroker.ledger_entries_raw()`
    (backend/brokers/adapters/dhan.py)
  - conn_service RPC whitelist + `RemoteBroker` forwarders for both
    (backend/brokers/service/routes.py, backend/brokers/client/remote_broker.py)

Per CLAUDE.md's own "Things to Avoid" / test-dimension conventions: the
Dhan SDK double is built via `create_autospec(dhanhq.dhanhq, instance=True)`,
not a bare `MagicMock` — a bare mock silently accepts ANY method name or
kwargs, which is exactly how both the pre-existing `get_ledger_report`
typo and the Groww margin-signature mismatch (documented in CLAUDE.md)
went unnoticed for a long time.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, create_autospec, patch

import dhanhq

import backend.brokers.adapters.dhan as dhan_module
from backend.brokers.adapters.dhan import DhanBroker, _DHAN_RATE_LIMITER
from backend.brokers.errors import BrokerCapabilityError, BrokerNetworkError


@pytest.fixture(autouse=True)
def _no_real_instruments_fetch(monkeypatch):
    """`trade_history()` (revised 2026-11-27, same day as added) now
    calls `_ensure_dhan_instruments()` unconditionally at the top — this
    would otherwise make every test in this file attempt a real network
    fetch of Dhan's public instruments CSV. Each test that cares about
    lot-cache state populates `_DHAN_LOT_BY_SECURITY` /
    `_DHAN_LOT_BY_MCX_ROOT` directly (see test_broker_qty_normalization.py
    for the dedicated lot-resolution tests), so the warm-up call itself
    can be a pure no-op here."""
    monkeypatch.setattr(dhan_module, "_ensure_dhan_instruments", lambda: None)


def _make_broker():
    mock_conn = MagicMock()
    mock_conn.account = "DH3747"
    mock_conn.client_id = "1100000001"
    broker = DhanBroker(mock_conn)
    return broker, mock_conn


def _spec_sdk():
    return create_autospec(dhanhq.dhanhq, instance=True)


# ---------------------------------------------------------------------------
# DhanBroker.trade_history()
# ---------------------------------------------------------------------------

class TestDhanBrokerTradeHistory:
    def test_calls_sdk_with_kwargs_and_normalizes_single_page(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.side_effect = [
            {"status": "success", "data": [{
                "tradeId": "T100", "orderId": "O100",
                "tradingSymbol": "INFY", "exchange": "NSE",
                "exchangeSegment": "NSE_EQ",
                "transactionType": "BUY", "tradedQuantity": 10,
                "tradedPrice": 1500.0, "exchangeTime": "2026-04-05 10:00:00",
            }]},
            {"status": "success", "data": []},
        ]
        mock_conn.get_dhan_conn.return_value = sdk
        out = broker.trade_history("2026-04-01", "2026-04-30")
        assert len(out) == 1
        assert out[0]["trade_id"] == "T100"
        assert out[0]["tradingsymbol"] == "INFY"
        # Called once per page, with from_date/to_date/page_number kwargs.
        first_call = sdk.get_trade_history.call_args_list[0]
        assert first_call.kwargs["from_date"] == "2026-04-01"
        assert first_call.kwargs["to_date"] == "2026-04-30"
        assert first_call.kwargs["page_number"] == 0

    def test_paginates_across_multiple_non_empty_pages(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()

        def _page_for(trade_id):
            return {"status": "success", "data": [{
                "tradeId": trade_id, "orderId": "O1",
                "tradingSymbol": "TCS", "exchange": "NSE",
                "exchangeSegment": "NSE_EQ",
                "transactionType": "SELL", "tradedQuantity": 1,
                "tradedPrice": 3000.0, "exchangeTime": "2026-04-05 10:00:00",
            }]}

        sdk.get_trade_history.side_effect = [
            _page_for("T1"), _page_for("T2"), {"status": "success", "data": []},
        ]
        mock_conn.get_dhan_conn.return_value = sdk
        out = broker.trade_history("2026-04-01", "2026-04-30")
        assert [r["trade_id"] for r in out] == ["T1", "T2"]
        assert sdk.get_trade_history.call_count == 3
        pages_requested = [c.kwargs["page_number"] for c in sdk.get_trade_history.call_args_list]
        assert pages_requested == [0, 1, 2]

    def test_empty_first_page_returns_empty_list_not_an_error(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.return_value = {"status": "success", "data": []}
        mock_conn.get_dhan_conn.return_value = sdk
        assert broker.trade_history("2026-04-01", "2026-04-30") == []

    def test_status_failure_raises_not_treated_as_end_of_pages(self):
        """A genuine API failure must propagate, never be silently read
        as 'this is simply the last page' — that would truncate a
        backfill with no signal."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.return_value = {
            "status": "failure", "data": "", "remarks": "invalid date range",
        }
        mock_conn.get_dhan_conn.return_value = sdk
        with pytest.raises(RuntimeError, match="invalid date range"):
            broker.trade_history("2026-04-01", "2026-04-30")

    def test_unbounded_non_empty_pages_raise_instead_of_looping_forever(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.return_value = {
            "status": "success",
            "data": [{"tradeId": "TX", "orderId": "O1", "tradingSymbol": "TCS",
                       "exchange": "NSE", "exchangeSegment": "NSE_EQ",
                       "transactionType": "BUY", "tradedQuantity": 1,
                       "tradedPrice": 10.0, "exchangeTime": "2026-04-05 10:00:00"}],
        }
        mock_conn.get_dhan_conn.return_value = sdk
        # The "backfill" bucket (1 call/s, 2026-11-27 Fix 2) would make
        # this 200-call stress test take real wall-clock minutes if the
        # rate limiter weren't bypassed — this test's own concern is the
        # pagination cap, not rate-limiting (covered separately above).
        with patch.object(_DHAN_RATE_LIMITER, "throttle"):
            with pytest.raises(BrokerNetworkError, match="pagination loop"):
                broker.trade_history("2026-04-01", "2026-04-30")
        assert sdk.get_trade_history.call_count == broker._TRADE_HISTORY_MAX_PAGES

    def test_uses_dedicated_backfill_rate_limit_bucket_not_history(self):
        """2026-11-27 fix (Fix 2): trade_history() must use the dedicated
        'backfill' bucket, NOT the shared 'history' bucket a live ltp()
        poll also uses — sharing it let an in-progress backfill starve
        live callers of their rate budget."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.return_value = {"status": "success", "data": []}
        mock_conn.get_dhan_conn.return_value = sdk
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            broker.trade_history("2026-04-01", "2026-04-30")
        mock_throttle.assert_called_once_with("backfill")

    def test_calls_ensure_dhan_instruments_up_front(self):
        """Must warm the lot caches itself — never rely solely on the
        caller script's own best-effort (debug-level, swallowed) warm-up."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.return_value = {"status": "success", "data": []}
        mock_conn.get_dhan_conn.return_value = sdk
        with patch.object(dhan_module, "_ensure_dhan_instruments") as mock_ensure:
            broker.trade_history("2026-04-01", "2026-04-30")
        mock_ensure.assert_called_once_with()

    def test_pagination_terminates_on_raw_page_not_filtered_rows(self):
        """A page where EVERY row is an unresolved MCX trade must NOT be
        mistaken for 'end of history' — termination is decided on the
        raw unwrapped page, never the post-filter row list. Page 0 is
        entirely unresolved MCX (filters to []), page 1 is a genuine
        valid trade, page 2 is a truly empty raw page (real end)."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        unresolved_mcx_page = {
            "status": "success",
            "data": [{
                "tradeId": "T0", "orderId": "O0",
                "tradingSymbol": "SOMENEWCOMMODITY-17DEC2026-FUT",
                "exchange": "MCX", "exchangeSegment": "MCX_COMM",
                "securityId": "GHOST_SID", "transactionType": "BUY",
                "tradedQuantity": 2, "tradedPrice": 100.0,
                "exchangeTime": "2026-04-05 10:00:00",
            }],
        }
        valid_page = {
            "status": "success",
            "data": [{
                "tradeId": "T1", "orderId": "O1", "tradingSymbol": "TCS",
                "exchange": "NSE", "exchangeSegment": "NSE_EQ",
                "transactionType": "SELL", "tradedQuantity": 1,
                "tradedPrice": 3000.0, "exchangeTime": "2026-04-05 11:00:00",
            }],
        }
        empty_page = {"status": "success", "data": []}
        sdk.get_trade_history.side_effect = [unresolved_mcx_page, valid_page, empty_page]
        mock_conn.get_dhan_conn.return_value = sdk

        with patch.object(_DHAN_RATE_LIMITER, "throttle"):
            out = broker.trade_history("2026-04-01", "2026-04-30")

        # Page 1's genuine row must survive — the old bug would have
        # stopped at page 0 (whose filtered rows are []) and silently
        # dropped page 1 and beyond entirely.
        assert [r["trade_id"] for r in out] == ["T1"]
        assert sdk.get_trade_history.call_count == 3

    def test_unresolved_mcx_rows_logged_as_one_aggregated_critical(self):
        """An unresolved MCX row is never silently dropped — it is
        skipped from the returned data AND reported via one aggregated
        CRITICAL log line naming the unresolved (security_id, symbol,
        time), not one log line per trade."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.side_effect = [
            {
                "status": "success",
                "data": [{
                    "tradeId": "T0", "orderId": "O0",
                    "tradingSymbol": "SOMENEWCOMMODITY-17DEC2026-FUT",
                    "exchange": "MCX", "exchangeSegment": "MCX_COMM",
                    "securityId": "GHOST_SID", "transactionType": "BUY",
                    "tradedQuantity": 2, "tradedPrice": 100.0,
                    "exchangeTime": "2026-04-05 10:00:00",
                }],
            },
            {"status": "success", "data": []},
        ]
        mock_conn.get_dhan_conn.return_value = sdk

        with patch.object(_DHAN_RATE_LIMITER, "throttle"), \
                patch.object(dhan_module.logger, "critical") as mock_critical:
            out = broker.trade_history("2026-04-01", "2026-04-30")

        assert out == []  # the unresolved row never reaches the returned data
        mock_critical.assert_called_once()
        args = mock_critical.call_args.args
        # First format arg after the message template is the account repr.
        assert "GHOST_SID" in str(args)
        assert "SOMENEWCOMMODITY26DECFUT" in str(args)

    def test_resolved_rows_coexist_with_unresolved_in_same_page(self):
        """A page mixing one resolved MCX row and one unresolved MCX row
        must keep the resolved row and skip only the unresolved one."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        dhan_module._DHAN_LOT_BY_SECURITY["RESOLVED_SID"] = 100
        try:
            sdk.get_trade_history.side_effect = [
                {
                    "status": "success",
                    "data": [
                        {
                            "tradeId": "TR", "orderId": "OR",
                            "tradingSymbol": "CRUDEOIL-17DEC2026-FUT",
                            "exchange": "MCX", "exchangeSegment": "MCX_COMM",
                            "securityId": "RESOLVED_SID", "transactionType": "BUY",
                            "tradedQuantity": 1, "tradedPrice": 5800.0,
                            "exchangeTime": "2026-04-05 10:00:00",
                        },
                        {
                            "tradeId": "TU", "orderId": "OU",
                            "tradingSymbol": "SOMENEWCOMMODITY-17DEC2026-FUT",
                            "exchange": "MCX", "exchangeSegment": "MCX_COMM",
                            "securityId": "GHOST_SID", "transactionType": "BUY",
                            "tradedQuantity": 2, "tradedPrice": 100.0,
                            "exchangeTime": "2026-04-05 10:05:00",
                        },
                    ],
                },
                {"status": "success", "data": []},
            ]
            mock_conn.get_dhan_conn.return_value = sdk
            with patch.object(_DHAN_RATE_LIMITER, "throttle"):
                out = broker.trade_history("2026-04-01", "2026-04-30")
        finally:
            dhan_module._DHAN_LOT_BY_SECURITY.clear()

        assert [r["trade_id"] for r in out] == ["TR"]
        assert out[0]["quantity"] == 100


# ---------------------------------------------------------------------------
# DhanBroker.ledger_entries_raw()
# ---------------------------------------------------------------------------

class TestDhanBrokerLedgerEntriesRaw:
    def test_returns_unaggregated_entry_list(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.ledger_report.return_value = {
            "status": "success",
            "data": [
                {"voucherdate": "2026-07-01", "exchange": "MCX",
                 "debit": "100.0", "credit": "0.0", "runbal": "50000.0"},
                {"voucherdate": "2026-07-02", "exchange": "MCX",
                 "debit": "0.0", "credit": "200.0", "runbal": "50200.0"},
            ],
        }
        mock_conn.get_dhan_conn.return_value = sdk
        out = broker.ledger_entries_raw("2026-07-01", "2026-07-31")
        # Unaggregated — one row per raw entry, NOT grouped by (date, segment)
        # the way funds_ledger()'s _dhan_ledger_aggregate() would.
        assert len(out) == 2
        assert out[0]["runbal"] == "50000.0"
        assert out[1]["runbal"] == "50200.0"
        sdk.ledger_report.assert_called_once_with(
            from_date="2026-07-01", to_date="2026-07-31",
        )

    def test_empty_entries_returns_empty_list(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.ledger_report.return_value = {"status": "success", "data": []}
        mock_conn.get_dhan_conn.return_value = sdk
        assert broker.ledger_entries_raw("2026-07-01", "2026-07-31") == []

    def test_raises_capability_error_when_sdk_lacks_ledger_report(self):
        broker, mock_conn = _make_broker()
        sdk = MagicMock(spec=[])  # no ledger_report attribute at all
        mock_conn.get_dhan_conn.return_value = sdk
        with pytest.raises(BrokerCapabilityError, match="ledger_report"):
            broker.ledger_entries_raw("2026-07-01", "2026-07-31")

    def test_status_failure_raises_unlike_funds_ledger_which_swallows(self):
        """Contrast with `funds_ledger()`, which treats any SDK/response
        failure as benign (returns []) because a daily_book upsert can
        tolerate 'no ledger data this pull'. A historical BACKFILL must
        not silently drop a genuine failure the same way."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.ledger_report.return_value = {
            "status": "failure", "data": "", "remarks": "boom",
        }
        mock_conn.get_dhan_conn.return_value = sdk
        with pytest.raises(RuntimeError, match="boom"):
            broker.ledger_entries_raw("2026-07-01", "2026-07-31")

    def test_uses_dedicated_backfill_rate_limit_bucket_not_history(self):
        """2026-11-27 fix (Fix 2) — see the matching trade_history() test
        above for the full rationale."""
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.ledger_report.return_value = {"status": "success", "data": []}
        mock_conn.get_dhan_conn.return_value = sdk
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            broker.ledger_entries_raw("2026-07-01", "2026-07-31")
        mock_throttle.assert_called_once_with("backfill")


class TestBackfillBucketIsGenuinelyIndependentFromHistory:
    """Behaviour test, not just a name check — exhausting the new
    'backfill' bucket (1 call/s) must never cause a live 'history'
    caller (e.g. ltp()) to block, proving the two buckets are
    independent token-bucket state, not merely different string keys
    pointing at shared state."""

    def setup_method(self):
        # Reset both buckets to a known-full state so this test doesn't
        # depend on leftover token counts from a previous test in the
        # same process.
        with _DHAN_RATE_LIMITER._lock:
            for _name in ("backfill", "history"):
                _b = _DHAN_RATE_LIMITER._buckets[_name]
                _b["tokens"] = _b["capacity"]

    def test_exhausting_backfill_bucket_never_sleeps_history_bucket(self):
        import time as _time

        # Exhaust "backfill" (capacity 1) down to its single token.
        _DHAN_RATE_LIMITER.throttle("backfill")
        with patch.object(_time, "sleep") as mock_sleep:
            # "history" (capacity 3) still has 3 tokens untouched —
            # must consume one instantly, with NO sleep at all.
            _DHAN_RATE_LIMITER.throttle("history")
        mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# conn_service RPC whitelist + RemoteBroker forwarders
# ---------------------------------------------------------------------------

class TestConnServiceWiring:
    def test_trade_history_is_whitelisted(self):
        from backend.brokers.service.routes import _ALLOWED_BROKER_METHODS
        assert "trade_history" in _ALLOWED_BROKER_METHODS

    def test_ledger_entries_raw_is_whitelisted(self):
        from backend.brokers.service.routes import _ALLOWED_BROKER_METHODS
        assert "ledger_entries_raw" in _ALLOWED_BROKER_METHODS

    def test_remote_broker_trade_history_forwards_with_kwargs(self):
        from backend.brokers.client.remote_broker import RemoteBroker, _TRADE_HISTORY_TIMEOUT
        rb = RemoteBroker("DH3747", broker_id="dhan")
        with patch.object(rb, "_call", return_value=[{"trade_id": "T1"}]) as mock_call:
            out = rb.trade_history("2026-04-01", "2026-04-30")
        mock_call.assert_called_once_with(
            "trade_history", from_date="2026-04-01", to_date="2026-04-30",
            _rpc_timeout=_TRADE_HISTORY_TIMEOUT,
        )
        assert out == [{"trade_id": "T1"}]

    def test_remote_broker_trade_history_timeout_covers_worst_case_paging(self):
        """Fix 3 — the client-side timeout for THIS one RPC must exceed
        the documented worst case: `DhanBroker._TRADE_HISTORY_MAX_PAGES`
        pages, each throttled by the dedicated 'backfill' bucket. Derived
        from the real constants so a future change to either the page
        cap or the bucket's rate fails this test instead of silently
        reopening the timeout/paging mismatch."""
        from backend.brokers.client.remote_broker import _TRADE_HISTORY_TIMEOUT
        from backend.brokers.adapters.dhan import DhanBroker, _DHAN_RATE_LIMITER

        backfill_capacity, backfill_period = (
            _DHAN_RATE_LIMITER._buckets["backfill"]["capacity"],
            1.0 / _DHAN_RATE_LIMITER._buckets["backfill"]["refill_rate"],
        )
        seconds_per_call = backfill_period / backfill_capacity
        worst_case_seconds = DhanBroker._TRADE_HISTORY_MAX_PAGES * seconds_per_call

        assert _TRADE_HISTORY_TIMEOUT.read > worst_case_seconds
        # Also strictly longer than the shared 30s default — this must
        # be a deliberately widened timeout, not the ordinary one.
        from backend.brokers.client.remote_broker import _TIMEOUT
        assert _TRADE_HISTORY_TIMEOUT.read > _TIMEOUT.read

    def test_remote_broker_ledger_entries_raw_uses_default_timeout(self):
        """A single ledger_report call (no pagination) stays on the
        shared 30s default — only trade_history() needs the widened
        timeout."""
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker("DH3747", broker_id="dhan")
        with patch.object(rb, "_call", return_value=[{"runbal": "1"}]) as mock_call:
            rb.ledger_entries_raw("2026-07-01", "2026-07-31")
        mock_call.assert_called_once_with(
            "ledger_entries_raw", from_date="2026-07-01", to_date="2026-07-31",
        )
        # No `_rpc_timeout` kwarg at all — call_args has no such key.
        assert "_rpc_timeout" not in mock_call.call_args.kwargs

    def test_remote_broker_ledger_entries_raw_forwards_with_kwargs(self):
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker("DH3747", broker_id="dhan")
        with patch.object(rb, "_call", return_value=[{"runbal": "1"}]) as mock_call:
            out = rb.ledger_entries_raw("2026-07-01", "2026-07-31")
        mock_call.assert_called_once_with(
            "ledger_entries_raw", from_date="2026-07-01", to_date="2026-07-31",
        )
        assert out == [{"runbal": "1"}]
