"""Tests for the standalone broker-GTT listing + cancel endpoints.

Covers backend/api/routes/orders_gtt.py:
  - _fetch_gtts combines per-account GTTs, tolerating one account's
    broker call raising without blanking the rest (staleness-freeze
    convention — a partial failure degrades gracefully).
  - _normalize_gtt_row reconciles Kite's raw-SDK nested `condition`
    shape with Dhan/Groww's pre-flattened "Kite GTT shape".
  - _filter_gtts_by_accounts respects the ?accounts= query param.
  - _cancel_gtt happy path + the per-broker-missing-method (501) and
    unknown-account (404) error paths.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.routes.orders_gtt import (
    _cancel_gtt,
    _fetch_gtts,
    _filter_gtts_by_accounts,
    _normalize_gtt_row,
    _parse_accounts_filter,
)
from backend.api.schemas import GttRow


# ── _normalize_gtt_row ───────────────────────────────────────────────────────

class TestNormalizeGttRow:
    def test_kite_raw_nested_condition_shape(self):
        """Kite's `get_gtts()` passes through the raw SDK shape: id at the
        top level, everything else nested under `condition`."""
        raw = {
            "id": 555,
            "status": "active",
            "type": "single",
            "condition": {
                "tradingsymbol": "INFY",
                "exchange": "NSE",
                "trigger_values": [1600.0],
                "last_price": 1550.0,
            },
            "orders": [{"transaction_type": "SELL", "quantity": 10}],
            "created_at": "2026-09-30 10:00:00",
        }
        row = _normalize_gtt_row(raw, account="ZG0790", broker_id="zerodha_kite")
        assert row.gtt_id == "555"
        assert row.account == "ZG0790"
        assert row.broker_id == "zerodha_kite"
        assert row.status == "active"
        assert row.trigger_type == "single"
        assert row.tradingsymbol == "INFY"
        assert row.exchange == "NSE"
        assert row.trigger_values == [1600.0]
        assert row.last_price == 1550.0
        assert row.orders == [{"transaction_type": "SELL", "quantity": 10}]
        assert row.created_at == "2026-09-30 10:00:00"

    def test_dhan_groww_flattened_shape(self):
        """Dhan/Groww adapters pre-flatten to top-level `gtt_id` +
        tradingsymbol/exchange/trigger_values/last_price keys."""
        raw = {
            "gtt_id": "FO123",
            "status": "active",
            "trigger_type": "single",
            "tradingsymbol": "CRUDEOIL26OCTFUT",
            "exchange": "MCX",
            "trigger_values": [6200.0],
            "last_price": 6100.0,
            "orders": [{"transaction_type": "BUY", "quantity": 1}],
            "created_at": "2026-09-30T10:00:00",
            "_raw": {},
        }
        row = _normalize_gtt_row(raw, account="DHAN-1", broker_id="dhan")
        assert row.gtt_id == "FO123"
        assert row.account == "DHAN-1"
        assert row.broker_id == "dhan"
        assert row.tradingsymbol == "CRUDEOIL26OCTFUT"
        assert row.exchange == "MCX"
        assert row.trigger_values == [6200.0]
        assert row.last_price == 6100.0

    def test_missing_fields_default_safely(self):
        """A malformed/sparse row must not raise — missing keys default
        to empty/zero, never None propagating into the struct."""
        row = _normalize_gtt_row({}, account="ACC", broker_id="x")
        assert row.gtt_id == ""
        assert row.tradingsymbol == ""
        assert row.exchange == ""
        assert row.trigger_values == []
        assert row.last_price == 0.0
        assert row.orders == []
        assert row.created_at == ""


# ── _fetch_gtts ──────────────────────────────────────────────────────────────

class TestFetchGtts:
    def _broker(self, account: str, broker_id: str, rows=None, raises: Exception | None = None):
        b = MagicMock()
        b.account = account
        b.broker_id = broker_id
        if raises is not None:
            b.get_gtts.side_effect = raises
        else:
            b.get_gtts.return_value = rows or []
        return b

    def test_combines_multiple_accounts(self):
        b1 = self._broker("ACC-1", "zerodha_kite", rows=[
            {"id": 1, "condition": {"tradingsymbol": "INFY", "exchange": "NSE",
                                     "trigger_values": [100.0], "last_price": 99.0},
             "status": "active", "type": "single", "orders": [], "created_at": ""},
        ])
        b2 = self._broker("ACC-2", "dhan", rows=[
            {"gtt_id": "D1", "tradingsymbol": "TCS", "exchange": "NSE",
             "trigger_values": [200.0], "last_price": 195.0,
             "status": "active", "trigger_type": "single", "orders": [], "created_at": ""},
        ])

        with patch("backend.brokers.registry.all_brokers", return_value=[b1, b2]):
            rows = _fetch_gtts()

        accounts = {r.account for r in rows}
        assert accounts == {"ACC-1", "ACC-2"}
        assert len(rows) == 2

    def test_one_account_failure_does_not_blank_response(self):
        """Staleness-freeze convention: one broker's exception must not
        collapse the combined response — the other account's rows still
        come through."""
        good = self._broker("ACC-GOOD", "zerodha_kite", rows=[
            {"id": 9, "tradingsymbol": "RELIANCE", "exchange": "NSE",
             "trigger_values": [2500.0], "last_price": 2400.0,
             "status": "active", "type": "single", "orders": [], "created_at": ""},
        ])
        broken = self._broker("ACC-BROKEN", "dhan", raises=RuntimeError("network error"))

        with patch("backend.brokers.registry.all_brokers", return_value=[broken, good]):
            rows = _fetch_gtts()

        assert len(rows) == 1
        assert rows[0].account == "ACC-GOOD"

    def test_no_brokers_returns_empty(self):
        with patch("backend.brokers.registry.all_brokers", return_value=[]):
            rows = _fetch_gtts()
        assert rows == []

    def test_non_dict_rows_are_skipped(self):
        b = self._broker("ACC-X", "groww", rows=["not-a-dict", None, 42])
        with patch("backend.brokers.registry.all_brokers", return_value=[b]):
            rows = _fetch_gtts()
        assert rows == []


# ── _parse_accounts_filter / _filter_gtts_by_accounts ───────────────────────

class TestFilterGttsByAccounts:
    def _rows(self) -> list[GttRow]:
        return [
            GttRow(gtt_id="1", account="ACC-1", broker_id="zerodha_kite",
                   status="active", trigger_type="single", tradingsymbol="INFY",
                   exchange="NSE", trigger_values=[100.0], last_price=99.0,
                   orders=[], created_at=""),
            GttRow(gtt_id="2", account="ACC-2", broker_id="dhan",
                   status="active", trigger_type="single", tradingsymbol="TCS",
                   exchange="NSE", trigger_values=[200.0], last_price=195.0,
                   orders=[], created_at=""),
        ]

    def test_no_filter_returns_all(self):
        rows = self._rows()
        assert _filter_gtts_by_accounts(rows, None) == rows
        assert _filter_gtts_by_accounts(rows, "") == rows

    def test_filter_scopes_to_requested_accounts(self):
        rows = self._rows()
        filtered = _filter_gtts_by_accounts(rows, "ACC-1")
        assert len(filtered) == 1
        assert filtered[0].account == "ACC-1"

    def test_filter_comma_separated_multiple_accounts(self):
        rows = self._rows()
        filtered = _filter_gtts_by_accounts(rows, "ACC-1, ACC-2")
        assert {r.account for r in filtered} == {"ACC-1", "ACC-2"}

    def test_filter_unknown_account_returns_empty(self):
        rows = self._rows()
        filtered = _filter_gtts_by_accounts(rows, "ACC-NOPE")
        assert filtered == []

    def test_parse_accounts_filter_empty(self):
        assert _parse_accounts_filter(None) == []
        assert _parse_accounts_filter("") == []
        assert _parse_accounts_filter("  ,  ") == []

    def test_parse_accounts_filter_trims_entries(self):
        assert _parse_accounts_filter("A, B ,C") == ["A", "B", "C"]


# ── _cancel_gtt ──────────────────────────────────────────────────────────────

class TestCancelGtt:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        broker = MagicMock()
        broker.broker_id = "zerodha_kite"
        broker.cancel_gtt.return_value = "555"

        with patch("backend.brokers.registry.get_broker", return_value=broker):
            result = await _cancel_gtt("ACC-1", "555", None)

        assert result == "555"
        broker.cancel_gtt.assert_called_once_with("555", exchange=None)

    @pytest.mark.asyncio
    async def test_passes_exchange_through(self):
        broker = MagicMock()
        broker.broker_id = "groww"
        broker.cancel_gtt.return_value = "G1"

        with patch("backend.brokers.registry.get_broker", return_value=broker):
            result = await _cancel_gtt("ACC-G", "G1", "MCX")

        assert result == "G1"
        broker.cancel_gtt.assert_called_once_with("G1", exchange="MCX")

    @pytest.mark.asyncio
    async def test_unknown_account_raises_404(self):
        with patch("backend.brokers.registry.get_broker", side_effect=KeyError("ACC-NOPE")):
            with pytest.raises(HTTPException) as exc_info:
                await _cancel_gtt("ACC-NOPE", "1", None)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_broker_missing_cancel_gtt_raises_501(self):
        """A broker adapter that hasn't implemented cancel_gtt (base.py's
        default NotImplementedError) must surface as a clear 501, not a
        crash."""
        broker = MagicMock()
        broker.broker_id = "some_future_broker"
        broker.cancel_gtt.side_effect = NotImplementedError(
            "some_future_broker adapter has not implemented cancel_gtt"
        )

        with patch("backend.brokers.registry.get_broker", return_value=broker):
            with pytest.raises(HTTPException) as exc_info:
                await _cancel_gtt("ACC-FUTURE", "1", None)

        assert exc_info.value.status_code == 501
        assert "some_future_broker" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_broker_value_error_raises_400(self):
        """Groww's cancel_gtt raises ValueError when `exchange` is missing —
        must map to a 400, not a 500."""
        broker = MagicMock()
        broker.broker_id = "groww"
        broker.cancel_gtt.side_effect = ValueError("Groww cancel_gtt requires `exchange` kwarg")

        with patch("backend.brokers.registry.get_broker", return_value=broker):
            with pytest.raises(HTTPException) as exc_info:
                await _cancel_gtt("ACC-G", "1", None)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_broker_generic_exception_raises_400(self):
        broker = MagicMock()
        broker.broker_id = "zerodha_kite"
        broker.cancel_gtt.side_effect = RuntimeError("broker rejected cancel")

        with patch("backend.brokers.registry.get_broker", return_value=broker):
            with pytest.raises(HTTPException) as exc_info:
                await _cancel_gtt("ACC-1", "1", None)

        assert exc_info.value.status_code == 400
