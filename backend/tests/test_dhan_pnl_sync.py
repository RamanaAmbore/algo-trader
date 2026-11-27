"""Tests for `scripts/dhan_pnl_sync.py` — the Dhan historical trade/ledger
backfill script. Mirrors the test conventions already established in
`backend/tests/test_kite_console_pnl_sync.py` for the sibling script
(the `_FakeSessionCtx` async-context-manager stand-in, per-account
error isolation, CLI arg-parser checks).

Per CLAUDE.md: "Never mock broker SDK calls in tests — they diverge
significantly from real behaviour" refers to the real `dhanhq` SDK
boundary (covered by `test_dhan_pnl_backfill_wiring.py`, which drives
the real adapter against a `create_autospec`'d SDK). This file tests
this script's OWN pure field-mapping/orchestration logic, mocking only
this script's own collaborators (`get_broker`, `ingest_rows`,
`ingest_ledger_rows`, the DB session) — never the broker SDK itself.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Pure field mapping
# ---------------------------------------------------------------------------

class TestDhanScrubClientId:
    def test_removes_known_client_id_keys(self):
        from scripts.dhan_pnl_sync import _dhan_scrub_client_id
        out = _dhan_scrub_client_id({
            "dhanClientId": "1100000001", "tradeId": "T1", "exchange": "NSE",
        })
        assert "dhanClientId" not in out
        assert out["tradeId"] == "T1"
        assert out["exchange"] == "NSE"

    def test_removes_every_plausible_spelling(self):
        from scripts.dhan_pnl_sync import _dhan_scrub_client_id
        out = _dhan_scrub_client_id({
            "dhan_client_id": "x", "clientId": "y", "client_id": "z", "keep": "me",
        })
        assert out == {"keep": "me"}

    def test_non_dict_input_returns_empty_dict(self):
        from scripts.dhan_pnl_sync import _dhan_scrub_client_id
        assert _dhan_scrub_client_id(None) == {}
        assert _dhan_scrub_client_id("not a dict") == {}

    def test_original_dict_is_not_mutated(self):
        from scripts.dhan_pnl_sync import _dhan_scrub_client_id
        original = {"dhanClientId": "123", "keep": "me"}
        _dhan_scrub_client_id(original)
        assert "dhanClientId" in original


class TestDhanTradeDateFromTimestamp:
    def test_date_only_timestamp(self):
        from scripts.dhan_pnl_sync import _dhan_trade_date_from_timestamp
        assert _dhan_trade_date_from_timestamp("2026-04-05") == "2026-04-05"

    def test_full_timestamp_with_space(self):
        from scripts.dhan_pnl_sync import _dhan_trade_date_from_timestamp
        assert _dhan_trade_date_from_timestamp("2026-04-05 10:23:11") == "2026-04-05"

    def test_iso_timestamp_with_t(self):
        from scripts.dhan_pnl_sync import _dhan_trade_date_from_timestamp
        assert _dhan_trade_date_from_timestamp("2026-04-05T10:23:11") == "2026-04-05"

    def test_none_returns_none(self):
        from scripts.dhan_pnl_sync import _dhan_trade_date_from_timestamp
        assert _dhan_trade_date_from_timestamp(None) is None

    def test_empty_string_returns_none(self):
        from scripts.dhan_pnl_sync import _dhan_trade_date_from_timestamp
        assert _dhan_trade_date_from_timestamp("") is None

    def test_garbage_returns_none(self):
        from scripts.dhan_pnl_sync import _dhan_trade_date_from_timestamp
        assert _dhan_trade_date_from_timestamp("not-a-date") is None


class TestSegmentForExchange:
    def test_nse_bse_are_equity(self):
        from scripts.dhan_pnl_sync import _segment_for_exchange
        assert _segment_for_exchange("NSE") == "equity"
        assert _segment_for_exchange("BSE") == "equity"

    def test_nfo_bfo_cds_bcd_are_derivatives(self):
        from scripts.dhan_pnl_sync import _segment_for_exchange
        for exch in ("NFO", "BFO", "CDS", "BCD"):
            assert _segment_for_exchange(exch) == "derivatives", exch

    def test_mcx_is_commodities_not_derivatives(self):
        from scripts.dhan_pnl_sync import _segment_for_exchange
        assert _segment_for_exchange("MCX") == "commodities"

    def test_unknown_exchange_falls_back_to_equity(self):
        from scripts.dhan_pnl_sync import _segment_for_exchange
        assert _segment_for_exchange("WEIRD") == "equity"

    def test_case_insensitive(self):
        from scripts.dhan_pnl_sync import _segment_for_exchange
        assert _segment_for_exchange("mcx") == "commodities"

    def test_none_falls_back_to_equity(self):
        from scripts.dhan_pnl_sync import _segment_for_exchange
        assert _segment_for_exchange(None) == "equity"


class TestDhanTradeExchangeResolution:
    def test_prefers_exchange_segment_reverse_map_for_nfo(self):
        from scripts.dhan_pnl_sync import _dhan_trade_exchange
        t = {"exchange": "NSE", "_raw": {"exchangeSegment": "NSE_FNO"}}
        # Base "exchange" field alone can't distinguish NFO from NSE —
        # the reverse-mapped exchangeSegment must win.
        assert _dhan_trade_exchange(t) == "NFO"

    def test_mcx_comm_segment_maps_to_mcx(self):
        from scripts.dhan_pnl_sync import _dhan_trade_exchange
        t = {"exchange": "MCX", "_raw": {"exchangeSegment": "MCX_COMM"}}
        assert _dhan_trade_exchange(t) == "MCX"

    def test_falls_back_to_normalised_exchange_when_segment_absent(self):
        from scripts.dhan_pnl_sync import _dhan_trade_exchange
        t = {"exchange": "NSE", "_raw": {}}
        assert _dhan_trade_exchange(t) == "NSE"

    def test_falls_back_when_raw_missing_entirely(self):
        from scripts.dhan_pnl_sync import _dhan_trade_exchange
        t = {"exchange": "BSE"}
        assert _dhan_trade_exchange(t) == "BSE"

    def test_unmapped_segment_falls_back_to_normalised_exchange(self):
        from scripts.dhan_pnl_sync import _dhan_trade_exchange
        t = {"exchange": "NSE", "_raw": {"exchangeSegment": "SOME_FUTURE_SEGMENT"}}
        assert _dhan_trade_exchange(t) == "NSE"


class TestDhanTradeToKiteShape:
    def test_maps_every_field(self):
        from scripts.dhan_pnl_sync import dhan_trade_to_kite_shape
        t = {
            "trade_id": "T1", "order_id": "O1", "tradingsymbol": "INFY",
            "exchange": "NSE", "transaction_type": "BUY", "quantity": 10,
            "average_price": 1500.0, "exchange_timestamp": "2026-04-05 10:00:00",
            "_raw": {"exchangeSegment": "NSE_EQ", "dhanClientId": "1100000001"},
        }
        row = dhan_trade_to_kite_shape(t)
        assert row["symbol"] == "INFY"
        assert row["exchange"] == "NSE"
        assert row["trade_type"] == "BUY"
        assert row["quantity"] == 10
        assert row["price"] == 1500.0
        assert row["trade_date"] == "2026-04-05"
        assert row["trade_id"] == "T1"
        assert row["order_id"] == "O1"

    def test_dhan_raw_is_scrubbed_of_client_id(self):
        from scripts.dhan_pnl_sync import dhan_trade_to_kite_shape
        t = {
            "trade_id": "T1", "order_id": "O1", "tradingsymbol": "INFY",
            "exchange": "NSE", "transaction_type": "BUY", "quantity": 10,
            "average_price": 1500.0, "exchange_timestamp": "2026-04-05 10:00:00",
            "_raw": {"exchangeSegment": "NSE_EQ", "dhanClientId": "1100000001"},
        }
        row = dhan_trade_to_kite_shape(t)
        assert "dhanClientId" not in row["_dhan_raw"]
        assert row["_dhan_raw"]["exchangeSegment"] == "NSE_EQ"

    def test_feeds_build_sync_row_without_error(self):
        """End-to-end sanity: the mapped row must satisfy the shared
        Kite-script function's own expectations (classify_instrument,
        dedup key derivation) with no KeyError/AttributeError."""
        from scripts.dhan_pnl_sync import dhan_trade_to_kite_shape
        from scripts.kite_console_pnl_sync import build_sync_row

        t = {
            "trade_id": "T1", "order_id": "O1", "tradingsymbol": "GOLDM26APRFUT",
            "exchange": "MCX", "transaction_type": "SELL", "quantity": 1,
            "average_price": 60000.0, "exchange_timestamp": "2026-04-05 10:00:00",
            "_raw": {"exchangeSegment": "MCX_COMM"},
        }
        mapped = dhan_trade_to_kite_shape(t)
        row = build_sync_row(
            "D1####", "commodities", "tradebook", mapped,
            pull_start=date(2026, 4, 1), pull_end=date(2026, 4, 30), broker="dhan",
        )
        assert row["broker"] == "dhan"
        assert row["instrument_type"] == "future"
        assert row["symbol_root"] == "GOLDM"


class TestDhanLedgerParticulars:
    def test_prefers_narration(self):
        from scripts.dhan_pnl_sync import _dhan_ledger_particulars
        assert _dhan_ledger_particulars({"narration": "Margin block", "voucherdesc": "x"}) == "Margin block"

    def test_falls_back_to_voucherdesc(self):
        from scripts.dhan_pnl_sync import _dhan_ledger_particulars
        assert _dhan_ledger_particulars({"voucherdesc": "Margin reversal"}) == "Margin reversal"

    def test_falls_back_to_vouchernumber(self):
        from scripts.dhan_pnl_sync import _dhan_ledger_particulars
        assert _dhan_ledger_particulars({"vouchernumber": "V123"}) == "V123"

    def test_never_returns_empty_string(self):
        from scripts.dhan_pnl_sync import _dhan_ledger_particulars
        out = _dhan_ledger_particulars({"voucherdate": "2026-07-01"})
        assert out
        assert "2026-07-01" in out


class TestDhanLedgerEntryToKiteShape:
    def test_maps_every_field(self):
        from scripts.dhan_pnl_sync import dhan_ledger_entry_to_kite_shape
        e = {
            "voucherdate": "2026-07-01", "narration": "Margin block",
            "debit": "100.0", "credit": "0.0", "runbal": "50000.0",
            "dhanClientId": "1100000001",
        }
        row = dhan_ledger_entry_to_kite_shape(e)
        assert row["particulars"] == "Margin block"
        assert row["posting_date"] == "2026-07-01"
        assert row["debit"] == "100.0"
        assert row["credit"] == "0.0"
        assert row["net_balance"] == "50000.0"
        assert "dhanClientId" not in row["_dhan_raw"]

    def test_dd_slash_mm_slash_yyyy_voucherdate(self):
        from scripts.dhan_pnl_sync import dhan_ledger_entry_to_kite_shape
        e = {"voucherdate": "16/07/2026", "narration": "x", "debit": "0",
             "credit": "0", "runbal": "0"}
        row = dhan_ledger_entry_to_kite_shape(e)
        assert row["posting_date"] == "2026-07-16"

    def test_unparseable_voucherdate_yields_none_posting_date(self):
        from scripts.dhan_pnl_sync import dhan_ledger_entry_to_kite_shape
        e = {"voucherdate": "garbage", "narration": "x", "debit": "0",
             "credit": "0", "runbal": "0"}
        row = dhan_ledger_entry_to_kite_shape(e)
        assert row["posting_date"] is None

    def test_feeds_validate_ledger_rows_without_error(self):
        from scripts.dhan_pnl_sync import dhan_ledger_entry_to_kite_shape
        from scripts.kite_console_pnl_sync import validate_ledger_rows

        e = {"voucherdate": "2026-07-15", "narration": "Margin block",
             "debit": "100.0", "credit": "0.0", "runbal": "50000.0"}
        row = dhan_ledger_entry_to_kite_shape(e)
        validate_ledger_rows(
            [row], start=date(2026, 7, 1), end=date(2026, 7, 31), allow_empty=False,
        )  # must not raise


# ---------------------------------------------------------------------------
# Mask registration
# ---------------------------------------------------------------------------

class TestRegisterAllActiveDhanMasks:
    def test_registers_full_roster_not_a_filtered_subset(self):
        from scripts.dhan_pnl_sync import _register_all_active_dhan_masks

        row1 = MagicMock(account="DH3747", client_id="1100000001")
        row2 = MagicMock(account="DH6847", client_id="1100000002")
        with patch("backend.shared.helpers.utils.register_accounts") as mock_register:
            _register_all_active_dhan_masks([row1, row2])
        mock_register.assert_called_once()
        args, kwargs = mock_register.call_args
        assert set(args[0]) == {"DH3747", "DH6847"}
        assert kwargs["client_ids"] == {
            "DH3747": "1100000001", "DH6847": "1100000002",
        }

    def test_skips_accounts_with_no_client_id_in_the_alias_map(self):
        from scripts.dhan_pnl_sync import _register_all_active_dhan_masks

        row1 = MagicMock(account="DH3747", client_id=None)
        with patch("backend.shared.helpers.utils.register_accounts") as mock_register:
            _register_all_active_dhan_masks([row1])
        _, kwargs = mock_register.call_args
        assert kwargs["client_ids"] == {}

    def test_real_registry_produces_ordinal_masks_for_colliding_prefix(self):
        """End-to-end against the REAL register_accounts()/mask_account()
        — confirms this script's registration call produces the same
        D1####/D2#### shape documented in CLAUDE.md, not a mocked
        assumption about it."""
        from backend.shared.helpers.utils import mask_account
        from scripts.dhan_pnl_sync import _register_all_active_dhan_masks

        row1 = MagicMock(account="DH3747", client_id=None)
        row2 = MagicMock(account="DH6847", client_id=None)
        _register_all_active_dhan_masks([row1, row2])
        assert mask_account("DH3747") == "D1####"
        assert mask_account("DH6847") == "D2####"


# ---------------------------------------------------------------------------
# load_active_dhan_accounts
# ---------------------------------------------------------------------------

class TestLoadActiveDhanAccounts:
    @pytest.mark.asyncio
    async def test_filters_by_broker_id_dhan_and_is_active(self):
        from scripts.dhan_pnl_sync import load_active_dhan_accounts

        fake_row = MagicMock(account="DH3747")
        fake_scalars = MagicMock()
        fake_scalars.all.return_value = [fake_row]
        fake_result = MagicMock()
        fake_result.scalars.return_value = fake_scalars

        fake_session = AsyncMock()
        fake_session.execute.return_value = fake_result

        class _Ctx:
            async def __aenter__(self):
                return fake_session

            async def __aexit__(self, *a):
                return False

        with patch("backend.api.database.shared_async_session", lambda: _Ctx()):
            rows = await load_active_dhan_accounts()
        assert rows == [fake_row]
        stmt = fake_session.execute.call_args.args[0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "broker_id" in compiled
        assert "dhan" in compiled
        assert "is_active" in compiled

    @pytest.mark.asyncio
    async def test_account_filter_narrows_the_returned_rows(self):
        from scripts.dhan_pnl_sync import load_active_dhan_accounts

        r1 = MagicMock(account="DH3747")
        r2 = MagicMock(account="DH6847")
        fake_scalars = MagicMock()
        fake_scalars.all.return_value = [r1, r2]
        fake_result = MagicMock()
        fake_result.scalars.return_value = fake_scalars
        fake_session = AsyncMock()
        fake_session.execute.return_value = fake_result

        class _Ctx:
            async def __aenter__(self):
                return fake_session

            async def __aexit__(self, *a):
                return False

        with patch("backend.api.database.shared_async_session", lambda: _Ctx()):
            rows = await load_active_dhan_accounts(["DH6847"])
        assert rows == [r2]


# ---------------------------------------------------------------------------
# Per-account orchestration — _sync_account_trades / _sync_account_ledger
# ---------------------------------------------------------------------------

class _FakeSessionCtx:
    """Mirrors `test_kite_console_pnl_sync.py`'s own `_FakeSessionCtx` —
    a minimal stand-in for `async_session()`'s async-context-manager
    shape, with no real DB."""

    async def __aenter__(self):
        return MagicMock()

    async def __aexit__(self, *a):
        return False

    def __call__(self):
        return self


class TestSyncAccountTrades:
    @pytest.mark.asyncio
    async def test_buckets_rows_by_segment_and_ingests_once_per_segment(self, monkeypatch):
        from scripts.dhan_pnl_sync import _sync_account_trades

        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())

        broker = MagicMock()
        broker.trade_history.return_value = [
            {"tradingsymbol": "INFY", "exchange": "NSE", "transaction_type": "BUY",
             "quantity": 1, "average_price": 100.0,
             "exchange_timestamp": "2026-04-05 10:00:00",
             "trade_id": "T1", "order_id": "O1", "_raw": {}},
            {"tradingsymbol": "GOLDM26APRFUT", "exchange": "MCX",
             "transaction_type": "SELL", "quantity": 1, "average_price": 60000.0,
             "exchange_timestamp": "2026-04-05 10:00:00",
             "trade_id": "T2", "order_id": "O2", "_raw": {}},
        ]
        with patch("scripts.dhan_pnl_sync.ingest_rows", new=AsyncMock(return_value=1)) as mock_ingest:
            total, errors = await _sync_account_trades(
                broker, "D1####", date(2026, 4, 1), date(2026, 4, 30),
            )
        assert errors == []
        assert total == 2  # one row ingested per segment call (mocked to return 1 each)
        segments_called = {c.args[2] for c in mock_ingest.call_args_list}
        assert segments_called == {"equity", "commodities"}

    @pytest.mark.asyncio
    async def test_one_chunk_failure_does_not_abort_the_whole_account(self, monkeypatch):
        from scripts.dhan_pnl_sync import _sync_account_trades

        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())

        broker = MagicMock()
        broker.trade_history.side_effect = [
            RuntimeError("boom"),
            [],
        ]
        total, errors = await _sync_account_trades(
            broker, "D1####", date(2026, 4, 1), date(2026, 5, 31),
        )
        assert total == 0
        assert len(errors) == 1
        assert "boom" in errors[0]
        assert broker.trade_history.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_chunk_is_not_an_error(self, monkeypatch):
        from scripts.dhan_pnl_sync import _sync_account_trades

        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        broker = MagicMock()
        broker.trade_history.return_value = []
        total, errors = await _sync_account_trades(
            broker, "D1####", date(2026, 4, 1), date(2026, 4, 30),
        )
        assert total == 0
        assert errors == []


class TestSyncAccountLedger:
    @pytest.mark.asyncio
    async def test_ingests_mapped_ledger_rows(self, monkeypatch):
        from scripts.dhan_pnl_sync import _sync_account_ledger

        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        broker = MagicMock()
        broker.ledger_entries_raw.return_value = [
            {"voucherdate": "2026-07-15", "narration": "Margin block",
             "debit": "100.0", "credit": "0.0", "runbal": "50000.0"},
        ]
        with patch("scripts.dhan_pnl_sync.ingest_ledger_rows", new=AsyncMock(return_value=1)) as mock_ingest:
            total, errors = await _sync_account_ledger(
                broker, "D1####", date(2026, 7, 1), date(2026, 7, 31),
            )
        assert errors == []
        assert total == 1
        mock_ingest.assert_awaited_once()
        _, kwargs = mock_ingest.call_args
        assert kwargs["broker"] == "dhan"

    @pytest.mark.asyncio
    async def test_a_row_dated_outside_the_chunk_fails_validation_not_ingestion(self, monkeypatch):
        from scripts.dhan_pnl_sync import _sync_account_ledger

        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        broker = MagicMock()
        # 2026-08-01 is outside the requested July chunk.
        broker.ledger_entries_raw.return_value = [
            {"voucherdate": "2026-08-01", "narration": "Late voucher",
             "debit": "0", "credit": "0", "runbal": "0"},
        ]
        with patch("scripts.dhan_pnl_sync.ingest_ledger_rows", new=AsyncMock()) as mock_ingest:
            total, errors = await _sync_account_ledger(
                broker, "D1####", date(2026, 7, 1), date(2026, 7, 31),
            )
        assert total == 0
        assert len(errors) == 1
        assert "validation" in errors[0]
        mock_ingest.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fetch_failure_recorded_as_chunk_error(self, monkeypatch):
        from scripts.dhan_pnl_sync import _sync_account_ledger

        monkeypatch.setattr("backend.api.database.async_session", _FakeSessionCtx())
        broker = MagicMock()
        broker.ledger_entries_raw.side_effect = RuntimeError("api down")
        total, errors = await _sync_account_ledger(
            broker, "D1####", date(2026, 7, 1), date(2026, 7, 31),
        )
        assert total == 0
        assert "api down" in errors[0]


# ---------------------------------------------------------------------------
# sync_account
# ---------------------------------------------------------------------------

class TestSyncAccount:
    @pytest.mark.asyncio
    async def test_get_broker_failure_is_isolated_per_account(self):
        from scripts.dhan_pnl_sync import sync_account

        row = MagicMock(account="DH3747")
        with patch("backend.brokers.registry.get_broker", side_effect=KeyError("no conn")):
            outcome = await sync_account(row, start=date(2026, 4, 1), end=date(2026, 4, 30))
        assert outcome.ok is False
        assert "get_broker failed" in outcome.reason

    @pytest.mark.asyncio
    async def test_ledger_only_skips_trade_sync(self, monkeypatch):
        from scripts.dhan_pnl_sync import sync_account

        row = MagicMock(account="DH3747")
        broker = MagicMock()
        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("scripts.dhan_pnl_sync._sync_account_trades", new=AsyncMock()) as mock_trades, \
             patch("scripts.dhan_pnl_sync._sync_account_ledger", new=AsyncMock(return_value=(0, []))):
            await sync_account(
                row, start=date(2026, 4, 1), end=date(2026, 4, 30), ledger_only=True,
            )
        mock_trades.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_trades_only_skips_ledger_sync(self, monkeypatch):
        from scripts.dhan_pnl_sync import sync_account

        row = MagicMock(account="DH3747")
        broker = MagicMock()
        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("scripts.dhan_pnl_sync._sync_account_trades", new=AsyncMock(return_value=(0, []))), \
             patch("scripts.dhan_pnl_sync._sync_account_ledger", new=AsyncMock()) as mock_ledger:
            await sync_account(
                row, start=date(2026, 4, 1), end=date(2026, 4, 30), trades_only=True,
            )
        mock_ledger.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_instruments_warmup_failure_is_non_fatal(self, monkeypatch):
        from scripts.dhan_pnl_sync import sync_account

        row = MagicMock(account="DH3747")
        broker = MagicMock()
        broker.instruments.side_effect = RuntimeError("cache load failed")
        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("scripts.dhan_pnl_sync._sync_account_trades", new=AsyncMock(return_value=(0, []))), \
             patch("scripts.dhan_pnl_sync._sync_account_ledger", new=AsyncMock(return_value=(0, []))):
            outcome = await sync_account(row, start=date(2026, 4, 1), end=date(2026, 4, 30))
        assert outcome.ok is True

    @pytest.mark.asyncio
    async def test_ok_false_when_any_chunk_had_errors(self, monkeypatch):
        from scripts.dhan_pnl_sync import sync_account

        row = MagicMock(account="DH3747")
        broker = MagicMock()
        with patch("backend.brokers.registry.get_broker", return_value=broker), \
             patch("scripts.dhan_pnl_sync._sync_account_trades",
                   new=AsyncMock(return_value=(0, ["tradebook chunk error"]))), \
             patch("scripts.dhan_pnl_sync._sync_account_ledger", new=AsyncMock(return_value=(0, []))):
            outcome = await sync_account(row, start=date(2026, 4, 1), end=date(2026, 4, 30))
        assert outcome.ok is False
        assert outcome.segment_errors == ["tradebook chunk error"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestArgParser:
    def test_default_mode_is_backfill(self):
        from scripts.dhan_pnl_sync import _build_arg_parser
        args = _build_arg_parser().parse_args([])
        assert args.mode == "backfill"

    def test_account_is_repeatable(self):
        from scripts.dhan_pnl_sync import _build_arg_parser
        args = _build_arg_parser().parse_args(["--account", "DH3747", "--account", "DH6847"])
        assert args.account == ["DH3747", "DH6847"]

    def test_start_end_parse_iso_dates(self):
        from scripts.dhan_pnl_sync import _build_arg_parser
        args = _build_arg_parser().parse_args(["--start", "2026-01-01", "--end", "2026-06-30"])
        assert args.start == date(2026, 1, 1)
        assert args.end == date(2026, 6, 30)

    def test_ledger_only_and_trades_only_flags_exist(self):
        from scripts.dhan_pnl_sync import _build_arg_parser
        args = _build_arg_parser().parse_args(["--ledger-only"])
        assert args.ledger_only is True
        assert args.trades_only is False


class TestMain:
    @pytest.mark.asyncio
    async def test_mutually_exclusive_ledger_and_trades_only_rejected(self):
        from scripts.dhan_pnl_sync import main
        code = await main(["--ledger-only", "--trades-only"])
        assert code == 1

    @pytest.mark.asyncio
    async def test_dry_run_prints_plan_and_touches_nothing(self, monkeypatch, capsys):
        from scripts.dhan_pnl_sync import main

        row = MagicMock(account="DH3747", client_id="1100000001")
        with patch("scripts.dhan_pnl_sync.load_active_dhan_accounts", new=AsyncMock(return_value=[row])), \
             patch("scripts.dhan_pnl_sync._register_all_active_dhan_masks"), \
             patch("scripts.dhan_pnl_sync.sync_account", new=AsyncMock()) as mock_sync:
            code = await main(["--dry-run"])
        assert code == 0
        mock_sync.assert_not_awaited()
        out = capsys.readouterr().out
        assert "backfill" in out

    @pytest.mark.asyncio
    async def test_no_active_accounts_returns_zero(self):
        from scripts.dhan_pnl_sync import main

        with patch("scripts.dhan_pnl_sync.load_active_dhan_accounts", new=AsyncMock(return_value=[])), \
             patch("scripts.dhan_pnl_sync._register_all_active_dhan_masks"):
            code = await main([])
        assert code == 0

    @pytest.mark.asyncio
    async def test_one_account_crash_does_not_abort_the_batch_and_alerts(self):
        from scripts.dhan_pnl_sync import main

        row1 = MagicMock(account="DH3747", client_id="1100000001")
        row2 = MagicMock(account="DH6847", client_id="1100000002")
        ok_outcome = MagicMock(ok=True, account="D2####", trades_ingested=1, ledger_ingested=1)
        with patch("scripts.dhan_pnl_sync.load_active_dhan_accounts", new=AsyncMock(return_value=[row1, row2])), \
             patch("scripts.dhan_pnl_sync._register_all_active_dhan_masks"), \
             patch("backend.brokers.connections.Connections") as mock_conn_cls, \
             patch("scripts.dhan_pnl_sync.sync_account",
                   new=AsyncMock(side_effect=[RuntimeError("boom"), ok_outcome])), \
             patch("scripts.dhan_pnl_sync.alert_critical", new=AsyncMock()) as mock_alert:
            mock_conn_cls.return_value.rebuild_from_db = AsyncMock()
            code = await main([])
        assert code == 1
        mock_alert.assert_awaited_once()
        _, kwargs = mock_alert.call_args
        assert kwargs["source_name"] == "Dhan P&L sync"
        assert kwargs["alert_event"] == "dhan_pnl_sync_failed"
