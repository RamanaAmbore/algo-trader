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

from backend.brokers.adapters.dhan import DhanBroker, _DHAN_RATE_LIMITER
from backend.brokers.errors import BrokerCapabilityError, BrokerNetworkError


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
        with pytest.raises(BrokerNetworkError, match="pagination loop"):
            broker.trade_history("2026-04-01", "2026-04-30")
        assert sdk.get_trade_history.call_count == broker._TRADE_HISTORY_MAX_PAGES

    def test_uses_history_rate_limit_bucket(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.get_trade_history.return_value = {"status": "success", "data": []}
        mock_conn.get_dhan_conn.return_value = sdk
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            broker.trade_history("2026-04-01", "2026-04-30")
        mock_throttle.assert_called_once_with("history")


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

    def test_uses_history_rate_limit_bucket(self):
        broker, mock_conn = _make_broker()
        sdk = _spec_sdk()
        sdk.ledger_report.return_value = {"status": "success", "data": []}
        mock_conn.get_dhan_conn.return_value = sdk
        with patch.object(_DHAN_RATE_LIMITER, "throttle") as mock_throttle:
            broker.ledger_entries_raw("2026-07-01", "2026-07-31")
        mock_throttle.assert_called_once_with("history")


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
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker("DH3747", broker_id="dhan")
        with patch.object(rb, "_call", return_value=[{"trade_id": "T1"}]) as mock_call:
            out = rb.trade_history("2026-04-01", "2026-04-30")
        mock_call.assert_called_once_with(
            "trade_history", from_date="2026-04-01", to_date="2026-04-30",
        )
        assert out == [{"trade_id": "T1"}]

    def test_remote_broker_ledger_entries_raw_forwards_with_kwargs(self):
        from backend.brokers.client.remote_broker import RemoteBroker
        rb = RemoteBroker("DH3747", broker_id="dhan")
        with patch.object(rb, "_call", return_value=[{"runbal": "1"}]) as mock_call:
            out = rb.ledger_entries_raw("2026-07-01", "2026-07-31")
        mock_call.assert_called_once_with(
            "ledger_entries_raw", from_date="2026-07-01", to_date="2026-07-31",
        )
        assert out == [{"runbal": "1"}]
