"""Tests for the three new `/api/admin/history/*` endpoints added
alongside the existing `/orders` / `/trades` / `/funds` / `/realized-pnl`
surfaces (`backend/api/routes/history.py`):

    GET /api/admin/history/tradebook    — raw Kite Console tradebook
    GET /api/admin/history/ledger       — raw Kite Console funds ledger
    GET /api/admin/history/pnl-summary  — win/loss aggregate over the
                                           realized-pnl ground truth

Plus the `broker` filter param added to `/realized-pnl` in the same
change.

Fixture shapes (ORM row attrs, raw_row JSON contents) are copied
verbatim from a read-only query against the real prod
`kite_console_pnl_sync` table, not invented — see the plan this test
file implements for the exact `psql` output.

DB-backed controller tests follow this project's own established
pattern for testing an ORM query against `KiteConsolePnlSync` (a
Postgres-only JSONB column) without a real Postgres instance: a
hand-built sqlite Core `Table` with a generic `JSON` column standing
in for `raw_row`, created on an in-memory `aiosqlite` engine, with
`backend.api.database.async_session` monkeypatched to a real
`async_sessionmaker` bound to it (same mechanism as
`test_kite_console_pnl_sync.py`'s `recon_engine`/
`recon_session_factory` fixtures). `select(KiteConsolePnlSync)` reads
from this table by name/column match, not by object identity, so the
real ORM class and route handlers run unmodified against it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import (
    JSON, Column, Date, DateTime, Integer, MetaData, Numeric, String, Table,
    UniqueConstraint, and_, text as sqltext,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.models import KiteConsolePnlSync
from backend.api.routes import history
from backend.api.routes.history import (
    HistoryController,
    LedgerListResponse,
    PnlSummaryResponse,
    TradebookListResponse,
    _hist_broker_param_matches_all,
    _hist_build_console_conditions,
    _hist_console_account_condition,
    _hist_console_broker_condition,
    _hist_console_broker_value,
    _hist_ledger_row,
    _hist_pnl_group_matches_filters,
    _hist_pnl_summary_rows,
    _hist_tradebook_row,
    _ledger_segment_from_cost_center,
    _safe_float,
)


# ===========================================================================
# Pure helpers — no DB
# ===========================================================================

class TestSafeFloat:
    def test_parses_numeric_string(self):
        assert _safe_float("1416190.500000") == 1416190.5

    def test_none_is_none(self):
        assert _safe_float(None) is None

    def test_empty_string_is_none(self):
        assert _safe_float("") is None

    def test_unparseable_is_none_not_zero(self):
        assert _safe_float("not-a-number") is None

    def test_real_float_passthrough(self):
        assert _safe_float(42.5) == 42.5


class TestLedgerSegmentFromCostCenter:
    """Mapping confirmed against the real, live prod cost_center values:
    'NSE-EQ - Z', 'NSE-F&O - Z', 'MCX-F&O - Z' (identical suffix across
    both live accounts)."""

    def test_nse_eq_maps_to_equity(self):
        assert _ledger_segment_from_cost_center("NSE-EQ - Z") == "equity"

    def test_nse_fo_maps_to_derivatives(self):
        assert _ledger_segment_from_cost_center("NSE-F&O - Z") == "derivatives"

    def test_mcx_fo_maps_to_commodities(self):
        assert _ledger_segment_from_cost_center("MCX-F&O - Z") == "commodities"

    def test_bse_variants_mapped_too(self):
        assert _ledger_segment_from_cost_center("BSE-EQ - Z") == "equity"
        assert _ledger_segment_from_cost_center("BSE-F&O - Z") == "derivatives"

    def test_unrecognised_cost_center_is_none(self):
        assert _ledger_segment_from_cost_center("SOMETHING-NEW - Z") is None

    def test_missing_cost_center_is_none(self):
        assert _ledger_segment_from_cost_center(None) is None
        assert _ledger_segment_from_cost_center("") is None

    def test_case_insensitive(self):
        assert _ledger_segment_from_cost_center("mcx-f&o - z") == "commodities"


class TestBrokerParamMatchesAll:
    def test_none_matches_all(self):
        assert _hist_broker_param_matches_all(None) is True

    def test_kite_matches_all(self):
        assert _hist_broker_param_matches_all("kite") is True
        assert _hist_broker_param_matches_all("KITE") is True
        assert _hist_broker_param_matches_all(" kite ") is True

    def test_dhan_does_not_match(self):
        assert _hist_broker_param_matches_all("dhan") is False


class TestConsoleAccountCondition:
    def test_no_account_returns_none(self):
        assert _hist_console_account_condition(KiteConsolePnlSync.account, None) is None

    def test_raw_account_resolves_both_forms(self):
        from backend.shared.helpers.utils import register_accounts, mask_account
        register_accounts(["ZG0790"])
        masked = mask_account("ZG0790")
        cond = _hist_console_account_condition(KiteConsolePnlSync.account, "ZG0790")
        compiled = str(cond.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
        ))
        assert "ZG0790" in compiled
        assert masked in compiled


class TestConsoleBrokerCondition:
    """Exercises both the degraded (column-absent) path and the
    real-column path. The real `KiteConsolePnlSync` model gained a
    `broker` column (2026-11-27, concurrent change) BEFORE this test
    file was written — the degraded path is simulated explicitly here
    (monkeypatching `history.KiteConsolePnlSync` to a stand-in with no
    `broker` attribute) so it stays covered regardless of the real
    model's current shape."""

    def test_no_broker_returns_none(self):
        assert _hist_console_broker_condition(None) is None

    def test_kite_filters_on_real_column_when_present(self):
        """Once the `broker` column exists, 'kite' is filtered for
        real like any other value — not specially no-op'd (every row
        currently has broker='kite' anyway, so this changes nothing
        observable today, but is the more precise behaviour)."""
        cond = _hist_console_broker_condition("kite")
        assert cond is not None
        compiled = str(cond.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
        ))
        assert "kite_console_pnl_sync.broker" in compiled
        assert "kite" in compiled

    def test_other_broker_filters_on_real_column_when_present(self):
        cond = _hist_console_broker_condition("dhan")
        assert cond is not None
        compiled = str(cond.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
        ))
        assert "kite_console_pnl_sync.broker" in compiled
        assert "dhan" in compiled

    def test_degraded_kite_is_noop_when_column_absent(self, monkeypatch):
        class _NoBrokerColumn:
            pass

        monkeypatch.setattr(history, "KiteConsolePnlSync", _NoBrokerColumn)
        assert _hist_console_broker_condition("kite") is None
        assert _hist_console_broker_condition(None) is None

    def test_degraded_other_broker_matches_nothing_when_column_absent(self, monkeypatch):
        class _NoBrokerColumn:
            pass

        monkeypatch.setattr(history, "KiteConsolePnlSync", _NoBrokerColumn)
        cond = _hist_console_broker_condition("dhan")
        assert cond is not None
        # sqlalchemy.false() compiles to a literal falsy clause.
        compiled = str(cond.compile(dialect=postgresql.dialect()))
        assert "false" in compiled.lower() or "0 = 1" in compiled.lower() or compiled.strip() in ("false", "0")


class TestConsoleBrokerValue:
    def test_real_value_returned(self):
        assert _hist_console_broker_value(SimpleNamespace(broker="dhan")) == "dhan"

    def test_none_degrades_to_kite(self):
        assert _hist_console_broker_value(SimpleNamespace(broker=None)) == "kite"

    def test_missing_attribute_degrades_to_kite(self):
        assert _hist_console_broker_value(SimpleNamespace()) == "kite"


class TestBuildConsoleConditions:
    def _compile(self, conditions):
        return str(and_(*conditions).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
        ))

    def test_report_type_always_present(self):
        conditions = _hist_build_console_conditions(
            "tradebook", from_date=None, to_date=None, account=None, broker=None,
        )
        compiled = self._compile(conditions)
        assert "report_type" in compiled
        assert "tradebook" in compiled

    def test_date_range_applied_when_given(self):
        conditions = _hist_build_console_conditions(
            "ledger",
            from_date=date(2026, 4, 1), to_date=date(2026, 4, 30),
            account=None, broker=None,
        )
        compiled = self._compile(conditions)
        assert "trade_date" in compiled
        assert "2026-04-01" in compiled
        assert "2026-04-30" in compiled

    def test_no_date_condition_when_both_omitted(self):
        """Full-history surface — no default 30/90-day window, unlike
        /orders, /trades, /funds."""
        conditions = _hist_build_console_conditions(
            "tradebook", from_date=None, to_date=None, account=None, broker=None,
        )
        # Only the report_type condition — no trade_date clause at all.
        assert len(conditions) == 1

    def test_account_and_broker_both_applied(self):
        conditions = _hist_build_console_conditions(
            "tradebook", from_date=None, to_date=None,
            account="Z1####", broker="dhan",
        )
        compiled = self._compile(conditions)
        assert "Z1####" in compiled
        assert "dhan" in compiled


# ===========================================================================
# _hist_tradebook_row / _hist_ledger_row — row builders, real prod shapes
# ===========================================================================

def _fake_tradebook_orm_row(**overrides):
    base = dict(
        account="ZJ####", broker="kite", segment="commodities",
        symbol="CRUDEOIL26APR10500CE", exchange="MCX",
        instrument_type="option", symbol_root="CRUDEOIL",
        expiry=date(2026, 4, 24), trade_date=date(2026, 4, 2),
        trade_id="150707579", order_id="609215344302031",
        buy_qty=100.0, sell_qty=None, buy_value=104240.0, sell_value=None,
        raw_row={
            "isin": "", "price": "1042.400000", "series": "", "auction": "false",
            "segment": "COM", "exchange": "MCX", "order_id": "609215344302031",
            "quantity": "100.000000", "trade_id": "150707579",
            "trade_date": "2026-04-02", "trade_type": "buy",
            "expiry_date": "2026-04-16", "symbol": "CRUDEOIL26APR10500CE",
            "order_execution_time": "2026-04-02T18:43:32",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestHistTradebookRow:
    def test_typed_and_console_expiry_both_surfaced_and_differ(self):
        """Live-confirmed divergence: typed `expiry` (last-Thursday
        approximation) is 2026-04-24; the raw Console `expiry_date` is
        2026-04-16 — a real MCX option expiring before the future."""
        row = _hist_tradebook_row(_fake_tradebook_orm_row())
        assert row.expiry == "2026-04-24"
        assert row.console_expiry == "2026-04-16"
        assert row.expiry != row.console_expiry

    def test_raw_row_fields_surfaced(self):
        row = _hist_tradebook_row(_fake_tradebook_orm_row())
        assert row.price == 1042.4
        assert row.trade_type == "buy"
        assert row.order_execution_time == "2026-04-02T18:43:32"

    def test_typed_columns_surfaced(self):
        row = _hist_tradebook_row(_fake_tradebook_orm_row())
        assert row.account == "ZJ####"
        assert row.broker == "kite"
        assert row.symbol == "CRUDEOIL26APR10500CE"
        assert row.exchange == "MCX"
        assert row.instrument_type == "option"
        assert row.symbol_root == "CRUDEOIL"
        assert row.trade_id == "150707579"
        assert row.order_id == "609215344302031"
        assert row.buy_qty == 100.0
        assert row.sell_qty is None
        assert row.buy_value == 104240.0
        assert row.sell_value is None

    def test_broker_degrades_to_kite_when_absent(self):
        row_raw = _fake_tradebook_orm_row()
        del row_raw.broker
        row = _hist_tradebook_row(row_raw)
        assert row.broker == "kite"

    def test_missing_console_expiry_is_none(self):
        raw = dict(_fake_tradebook_orm_row().raw_row)
        raw.pop("expiry_date")
        row = _hist_tradebook_row(_fake_tradebook_orm_row(raw_row=raw))
        assert row.console_expiry is None

    def test_equity_row_has_no_expiry_at_all(self):
        """Equity trades carry no expiry_date in raw_row and no typed
        expiry — both fields are null, not a crash."""
        row = _hist_tradebook_row(_fake_tradebook_orm_row(
            segment="equity", symbol="HCLTECH", exchange="NSE",
            instrument_type="equity", symbol_root="HCLTECH",
            expiry=None,
            raw_row={"symbol": "HCLTECH", "price": "1800.00", "trade_type": "sell"},
        ))
        assert row.expiry is None
        assert row.console_expiry is None


def _fake_ledger_orm_row(**overrides):
    base = dict(
        account="ZJ####", broker="kite",
        trade_date=date(2026, 4, 1),
        raw_row={
            "debit": "0.000000", "credit": "1416190.500000",
            "cost_center": "MCX-F&O - Z", "net_balance": "190471.655934",
            "particulars": "Span margin reversed for MCX F&O",
            "posting_date": "2026-04-01", "voucher_type": "Delivery Voucher",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestHistLedgerRow:
    def test_numeric_strings_parsed_to_float(self):
        row = _hist_ledger_row(_fake_ledger_orm_row())
        assert row.debit == 0.0
        assert row.credit == 1416190.5
        assert row.net_balance == 190471.655934

    def test_segment_derived_from_cost_center(self):
        row = _hist_ledger_row(_fake_ledger_orm_row())
        assert row.cost_center == "MCX-F&O - Z"
        assert row.segment == "commodities"

    def test_particulars_and_voucher_type_surfaced(self):
        row = _hist_ledger_row(_fake_ledger_orm_row())
        assert row.particulars == "Span margin reversed for MCX F&O"
        assert row.voucher_type == "Delivery Voucher"

    def test_posting_date_from_typed_trade_date_column(self):
        row = _hist_ledger_row(_fake_ledger_orm_row())
        assert row.posting_date == "2026-04-01"

    def test_missing_fields_are_none_not_zero(self):
        row = _hist_ledger_row(_fake_ledger_orm_row(raw_row={}))
        assert row.debit is None
        assert row.credit is None
        assert row.net_balance is None
        assert row.cost_center is None
        assert row.segment is None

    def test_unrecognised_cost_center_segment_is_none(self):
        raw = dict(_fake_ledger_orm_row().raw_row)
        raw["cost_center"] = "SOME-NEW-CENTER - Z"
        row = _hist_ledger_row(_fake_ledger_orm_row(raw_row=raw))
        assert row.segment is None


# ===========================================================================
# _hist_pnl_group_matches_filters / _hist_pnl_summary_rows — aggregation
# ===========================================================================

class TestPnlGroupMatchesFilters:
    def test_no_filters_matches_everything(self):
        assert _hist_pnl_group_matches_filters(
            "Z1####", "commodities", date(2026, 4, 24),
            account_candidates=None, segment=None, expiry_from=None, expiry_to=None,
        ) is True

    def test_account_candidates_narrows(self):
        assert _hist_pnl_group_matches_filters(
            "Z1####", "commodities", None,
            account_candidates={"Z2####"}, segment=None, expiry_from=None, expiry_to=None,
        ) is False

    def test_segment_mismatch_excludes(self):
        assert _hist_pnl_group_matches_filters(
            "Z1####", "commodities", None,
            account_candidates=None, segment="equity", expiry_from=None, expiry_to=None,
        ) is False

    def test_expiry_range_excludes_null_expiry(self):
        assert _hist_pnl_group_matches_filters(
            "Z1####", "commodities", None,
            account_candidates=None, segment=None,
            expiry_from=date(2026, 1, 1), expiry_to=date(2026, 12, 31),
        ) is False


def _two_month_backfill_overlap_result() -> dict:
    """Simulates the exact scenario `get_accumulated_pnl()`'s own
    docstring warns about and its ROW_NUMBER()-partition SQL already
    prevents: a backfill pull and a later ongoing sync both touching
    the same calendar month for the same contract. By the time this
    dict reaches `_hist_pnl_summary_rows()`, that dedup has ALREADY
    happened (it's done inside `get_accumulated_pnl()`'s own SQL) — so
    this fixture models the CORRECTLY-deduped, already-summed output:
    one group per (account, segment, itype, root, expiry), not two."""
    return {
        "groups": {
            # GOLDM future, one contract-cycle, correctly summed across
            # whatever months of pulls covered it (not two separate rows).
            ("Z1####", "commodities", "future", "GOLDM", date(2026, 12, 31)): 50000.0,
            # A second, different expiry cycle for the SAME root+segment —
            # genuinely a different outcome, must be counted separately.
            ("Z1####", "commodities", "future", "GOLDM", date(2026, 11, 30)): -20000.0,
            # Different account, same root+segment — still folds into the
            # same (symbol_root, segment) bucket (account is collapsed).
            ("Z2####", "commodities", "future", "GOLDM", date(2026, 10, 31)): 0.0,
            # Different root, derivatives segment.
            ("Z1####", "derivatives", "option", "NIFTY", date(2026, 11, 27)): -5000.0,
            # Equity, no expiry.
            ("Z2####", "equity", "equity", "RELIANCE", None): 2500.0,
        },
        "by_account_root_expiry": {}, "overall_total": 27500.0,
    }


class TestHistPnlSummaryRows:
    def test_buckets_by_symbol_root_and_segment_only(self):
        rows = _hist_pnl_summary_rows(_two_month_backfill_overlap_result())
        by_key = {(r.symbol_root, r.segment): r for r in rows}
        assert set(by_key) == {
            ("GOLDM", "commodities"), ("NIFTY", "derivatives"), ("RELIANCE", "equity"),
        }

    def test_goldm_bucket_collapses_account_and_sums_both_cycles(self):
        """Two different expiry cycles + two different accounts, same
        root+segment — must sum to 50000 - 20000 + 0 = 30000 across 3
        contract-cycles, NOT double-count or drop either."""
        rows = _hist_pnl_summary_rows(_two_month_backfill_overlap_result())
        goldm = next(r for r in rows if r.symbol_root == "GOLDM")
        assert goldm.trade_count == 3
        assert goldm.total_pnl == 30000.0
        assert goldm.win_count == 1
        assert goldm.loss_count == 1
        assert goldm.flat_count == 1

    def test_win_rate_and_averages(self):
        rows = _hist_pnl_summary_rows(_two_month_backfill_overlap_result())
        goldm = next(r for r in rows if r.symbol_root == "GOLDM")
        assert goldm.win_rate == pytest.approx(1 / 3)
        assert goldm.avg_win == 50000.0
        assert goldm.avg_loss == -20000.0

    def test_no_wins_or_losses_yields_none_averages_not_zero(self):
        result = {
            "groups": {
                ("Z1####", "commodities", "future", "FLAT", None): 0.0,
            },
            "by_account_root_expiry": {}, "overall_total": 0.0,
        }
        rows = _hist_pnl_summary_rows(result)
        row = rows[0]
        assert row.trade_count == 1
        assert row.win_count == 0
        assert row.loss_count == 0
        assert row.flat_count == 1
        assert row.avg_win is None
        assert row.avg_loss is None
        assert row.win_rate == 0.0

    def test_empty_groups_returns_empty_list(self):
        assert _hist_pnl_summary_rows({"groups": {}}) == []
        assert _hist_pnl_summary_rows({}) == []

    def test_sorted_by_total_pnl_descending(self):
        rows = _hist_pnl_summary_rows(_two_month_backfill_overlap_result())
        pnls = [r.total_pnl for r in rows]
        assert pnls == sorted(pnls, reverse=True)
        assert rows[0].symbol_root == "GOLDM"  # 30000, highest

    def test_account_filter_narrows_bucket_membership(self):
        rows = _hist_pnl_summary_rows(
            _two_month_backfill_overlap_result(), account="Z1####",
        )
        goldm = next(r for r in rows if r.symbol_root == "GOLDM")
        # Only the two Z1#### cycles now (50000 - 20000), Z2####'s flat
        # 0.0 cycle is excluded.
        assert goldm.trade_count == 2
        assert goldm.total_pnl == 30000.0

    def test_segment_filter(self):
        rows = _hist_pnl_summary_rows(
            _two_month_backfill_overlap_result(), segment="equity",
        )
        assert [r.symbol_root for r in rows] == ["RELIANCE"]

    def test_expiry_range_excludes_null_expiry_bucket(self):
        rows = _hist_pnl_summary_rows(
            _two_month_backfill_overlap_result(),
            expiry_from=date(2026, 1, 1), expiry_to=date(2026, 12, 31),
        )
        roots = {r.symbol_root for r in rows}
        assert "RELIANCE" not in roots
        assert "GOLDM" in roots
        assert "NIFTY" in roots


# ===========================================================================
# Controller — /realized-pnl broker extension, /pnl-summary
# ===========================================================================

class TestListRealizedPnlBrokerExtension:
    @pytest.mark.asyncio
    async def test_kite_broker_behaves_as_before(self):
        canned = {
            "groups": {
                ("Z1####", "commodities", "future", "GOLDM", date(2026, 12, 31)): 120000.0,
            },
            "by_account_root_expiry": {}, "overall_total": 120000.0,
        }
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl",
                   AsyncMock(return_value=canned)):
            result = await HistoryController.list_realized_pnl.fn(None, broker="kite")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_non_kite_broker_short_circuits_to_empty_without_calling_through(self):
        mock_fn = AsyncMock(return_value={"groups": {}, "by_account_root_expiry": {}, "overall_total": 0.0})
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl", mock_fn):
            result = await HistoryController.list_realized_pnl.fn(None, broker="dhan")
        assert result == []
        mock_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_omitted_broker_behaves_as_before(self):
        canned = {
            "groups": {
                ("Z1####", "commodities", "future", "GOLDM", date(2026, 12, 31)): 120000.0,
            },
            "by_account_root_expiry": {}, "overall_total": 120000.0,
        }
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl",
                   AsyncMock(return_value=canned)):
            result = await HistoryController.list_realized_pnl.fn(None)
        assert len(result) == 1

    def test_guarded_by_view_audit_cap(self):
        handler = HistoryController.list_realized_pnl
        assert handler.guards[0].__name__ == "cap_guard__view_audit"


class TestPnlSummaryEndpoint:
    def test_guarded_by_view_audit_cap(self):
        handler = HistoryController.pnl_summary
        assert handler.guards[0].__name__ == "cap_guard__view_audit"

    @pytest.mark.asyncio
    async def test_returns_aggregated_rows(self):
        with patch(
            "scripts.kite_console_pnl_sync.get_accumulated_pnl",
            AsyncMock(return_value=_two_month_backfill_overlap_result()),
        ):
            result = await HistoryController.pnl_summary.fn(None)
        assert isinstance(result, PnlSummaryResponse)
        assert result.total == 3
        assert result.rows[0].symbol_root == "GOLDM"

    @pytest.mark.asyncio
    async def test_non_kite_broker_short_circuits_without_calling_through(self):
        mock_fn = AsyncMock(return_value={"groups": {}, "by_account_root_expiry": {}, "overall_total": 0.0})
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl", mock_fn):
            result = await HistoryController.pnl_summary.fn(None, broker="dhan")
        assert result == PnlSummaryResponse(rows=[], total=0)
        mock_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_symbol_root_forwarded_to_underlying_function(self):
        mock_fn = AsyncMock(return_value={"groups": {}, "by_account_root_expiry": {}, "overall_total": 0.0})
        with patch("scripts.kite_console_pnl_sync.get_accumulated_pnl", mock_fn):
            await HistoryController.pnl_summary.fn(None, symbol_root="GOLDM")
        mock_fn.assert_awaited_once_with(symbol_root="GOLDM")

    @pytest.mark.asyncio
    async def test_expiry_range_applied(self):
        with patch(
            "scripts.kite_console_pnl_sync.get_accumulated_pnl",
            AsyncMock(return_value=_two_month_backfill_overlap_result()),
        ):
            result = await HistoryController.pnl_summary.fn(
                None, expiry_from="2026-01-01", expiry_to="2026-12-31",
            )
        roots = {r.symbol_root for r in result.rows}
        assert "RELIANCE" not in roots


# ===========================================================================
# Controller — /tradebook, /ledger — real DB-backed, in-memory sqlite
# ===========================================================================

@pytest_asyncio.fixture
async def console_engine():
    meta = MetaData()
    Table(
        "kite_console_pnl_sync", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("broker", String(16), nullable=False, server_default="kite"),
        Column("account", String(16), nullable=False),
        Column("segment", String(16), nullable=False),
        Column("report_type", String(16), nullable=False),
        Column("dedup_key", String(256), nullable=False),
        Column("symbol", String(64), nullable=True),
        Column("exchange", String(16), nullable=True),
        Column("instrument_type", String(16), nullable=True),
        Column("symbol_root", String(64), nullable=True),
        Column("expiry", Date, nullable=True),
        Column("trade_date", Date, nullable=True),
        Column("trade_id", String(64), nullable=True),
        Column("order_id", String(64), nullable=True),
        Column("pull_range_start", Date, nullable=True),
        Column("pull_range_end", Date, nullable=True),
        Column("buy_qty", Numeric, nullable=True),
        Column("sell_qty", Numeric, nullable=True),
        Column("buy_value", Numeric, nullable=True),
        Column("sell_value", Numeric, nullable=True),
        Column("realized_pnl", Numeric, nullable=True),
        Column("raw_row", JSON, nullable=False, default=dict),
        Column("synced_at", DateTime(timezone=True)),
        Column("updated_at", DateTime(timezone=True)),
        UniqueConstraint("account", "report_type", "dedup_key",
                          name="uq_kite_console_pnl_sync_account_type_key"),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def console_session_factory(console_engine, monkeypatch):
    """`history.py` does `from backend.api.database import async_session`
    at module scope, binding its OWN name — patching the attribute on
    the original `backend.api.database` module has no effect on the
    already-bound name in `history`'s namespace, so this patches
    `history.async_session` directly."""
    factory = async_sessionmaker(console_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(history, "async_session", factory)
    return factory


_TRADEBOOK_INSERT_SQL = sqltext("""
    INSERT INTO kite_console_pnl_sync
        (broker, account, segment, report_type, dedup_key, symbol, exchange,
         instrument_type, symbol_root, expiry, trade_date, trade_id, order_id,
         buy_qty, sell_qty, buy_value, sell_value, raw_row, updated_at)
    VALUES
        (:broker, :account, :segment, :report_type, :dedup_key, :symbol, :exchange,
         :instrument_type, :symbol_root, :expiry, :trade_date, :trade_id, :order_id,
         :buy_qty, :sell_qty, :buy_value, :sell_value, :raw_row, :updated_at)
""")

_LEDGER_INSERT_SQL = sqltext("""
    INSERT INTO kite_console_pnl_sync
        (broker, account, segment, report_type, dedup_key, trade_date, raw_row, updated_at)
    VALUES
        (:broker, :account, :segment, :report_type, :dedup_key, :trade_date, :raw_row, :updated_at)
""")


async def _insert_tradebook_row(session, *, broker="kite", account="ZJ####",
                                 symbol="CRUDEOIL26APR10500CE", exchange="MCX",
                                 instrument_type="option", symbol_root="CRUDEOIL",
                                 expiry=date(2026, 4, 24), trade_date=date(2026, 4, 2),
                                 trade_id="150707579", order_id="609215344302031",
                                 buy_qty=100.0, sell_qty=None, buy_value=104240.0,
                                 sell_value=None, segment="commodities",
                                 dedup_key=None, raw_row=None):
    import json as _json
    await session.execute(_TRADEBOOK_INSERT_SQL, {
        "broker": broker, "account": account, "segment": segment,
        "report_type": "tradebook", "dedup_key": dedup_key or trade_id or "k",
        "symbol": symbol, "exchange": exchange, "instrument_type": instrument_type,
        "symbol_root": symbol_root, "expiry": expiry, "trade_date": trade_date,
        "trade_id": trade_id, "order_id": order_id,
        "buy_qty": buy_qty, "sell_qty": sell_qty,
        "buy_value": buy_value, "sell_value": sell_value,
        "raw_row": _json.dumps(raw_row or {
            "price": "1042.400000", "trade_type": "buy",
            "expiry_date": "2026-04-16", "order_execution_time": "2026-04-02T18:43:32",
        }),
        "updated_at": datetime.now(timezone.utc),
    })


async def _insert_ledger_row(session, *, broker="kite", account="ZJ####",
                              trade_date=date(2026, 4, 1), dedup_key="l1",
                              raw_row=None):
    import json as _json
    await session.execute(_LEDGER_INSERT_SQL, {
        "broker": broker, "account": account, "segment": "all",
        "report_type": "ledger", "dedup_key": dedup_key, "trade_date": trade_date,
        "raw_row": _json.dumps(raw_row or {
            "debit": "0.000000", "credit": "1416190.500000",
            "cost_center": "MCX-F&O - Z", "net_balance": "190471.655934",
            "particulars": "Span margin reversed for MCX F&O",
            "posting_date": "2026-04-01", "voucher_type": "Delivery Voucher",
        }),
        "updated_at": datetime.now(timezone.utc),
    })


class TestListTradebookEndpoint:
    def test_guarded_by_view_audit_cap(self):
        handler = HistoryController.list_tradebook
        assert handler.guards[0].__name__ == "cap_guard__view_audit"

    @pytest.mark.asyncio
    async def test_returns_rows_newest_first(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, trade_date=date(2026, 4, 2), trade_id="t1", dedup_key="t1")
            await _insert_tradebook_row(s, trade_date=date(2026, 4, 7), trade_id="t2", dedup_key="t2")
            await s.commit()

        result = await HistoryController.list_tradebook.fn(None)
        assert isinstance(result, TradebookListResponse)
        assert result.total == 2
        assert [r.trade_id for r in result.rows] == ["t2", "t1"]

    @pytest.mark.asyncio
    async def test_no_default_date_window_returns_everything(self, console_session_factory):
        """Unlike /trades and /orders, omitting from_date/to_date here
        must NOT silently default to a recent window."""
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, trade_date=date(2026, 3, 1), trade_id="old", dedup_key="old")
            await s.commit()
        result = await HistoryController.list_tradebook.fn(None)
        assert result.total == 1
        assert result.rows[0].trade_id == "old"

    @pytest.mark.asyncio
    async def test_date_range_filter(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, trade_date=date(2026, 4, 2), trade_id="in", dedup_key="in")
            await _insert_tradebook_row(s, trade_date=date(2026, 8, 1), trade_id="out", dedup_key="out")
            await s.commit()
        result = await HistoryController.list_tradebook.fn(
            None, from_date="2026-04-01", to_date="2026-04-30",
        )
        assert [r.trade_id for r in result.rows] == ["in"]

    @pytest.mark.asyncio
    async def test_segment_instrument_type_symbol_root_exchange_filters(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_tradebook_row(
                s, trade_id="opt", dedup_key="opt", segment="commodities",
                instrument_type="option", symbol_root="CRUDEOIL", exchange="MCX",
            )
            await _insert_tradebook_row(
                s, trade_id="eq", dedup_key="eq", segment="equity",
                instrument_type="equity", symbol_root="HCLTECH", exchange="NSE",
                symbol="HCLTECH",
            )
            await s.commit()
        result = await HistoryController.list_tradebook.fn(None, segment="equity")
        assert [r.trade_id for r in result.rows] == ["eq"]
        result2 = await HistoryController.list_tradebook.fn(None, symbol_root="CRUDEOIL")
        assert [r.trade_id for r in result2.rows] == ["opt"]
        result3 = await HistoryController.list_tradebook.fn(None, exchange="MCX")
        assert [r.trade_id for r in result3.rows] == ["opt"]

    @pytest.mark.asyncio
    async def test_account_filter_matches_masked_or_raw(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, account="Z1####", trade_id="a", dedup_key="a")
            await _insert_tradebook_row(s, account="Z2####", trade_id="b", dedup_key="b")
            await s.commit()
        result = await HistoryController.list_tradebook.fn(None, account="Z1####")
        assert [r.trade_id for r in result.rows] == ["a"]

    @pytest.mark.asyncio
    async def test_broker_filter_real_column(self, console_session_factory):
        """With the real `broker` column present, the filter is exact
        for every value, including 'kite' — the no-op-for-'kite'
        degraded behaviour only applies when the column is ABSENT (see
        `_hist_console_broker_condition`'s own docstring/tests)."""
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, broker="kite", trade_id="k", dedup_key="k")
            await _insert_tradebook_row(s, broker="dhan", trade_id="d", dedup_key="d")
            await s.commit()
        result_kite = await HistoryController.list_tradebook.fn(None, broker="kite")
        assert [r.trade_id for r in result_kite.rows] == ["k"]
        result_dhan = await HistoryController.list_tradebook.fn(None, broker="dhan")
        assert [r.trade_id for r in result_dhan.rows] == ["d"]
        assert result_dhan.rows[0].broker == "dhan"
        result_all = await HistoryController.list_tradebook.fn(None)
        assert {r.trade_id for r in result_all.rows} == {"k", "d"}

    @pytest.mark.asyncio
    async def test_pagination_limit_offset(self, console_session_factory):
        async with console_session_factory() as s:
            for i in range(5):
                await _insert_tradebook_row(
                    s, trade_id=f"t{i}", dedup_key=f"t{i}",
                    trade_date=date(2026, 4, 1 + i),
                )
            await s.commit()
        result = await HistoryController.list_tradebook.fn(None, limit=2, offset=1)
        assert result.total == 5
        assert result.limit == 2
        assert result.offset == 1
        assert len(result.rows) == 2

    @pytest.mark.asyncio
    async def test_limit_clamped_to_500(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, trade_id="x", dedup_key="x")
            await s.commit()
        result = await HistoryController.list_tradebook.fn(None, limit=99999)
        assert result.limit == 500

    @pytest.mark.asyncio
    async def test_only_tradebook_report_type_returned(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, trade_id="tb", dedup_key="tb")
            await _insert_ledger_row(s, dedup_key="lg")
            await s.commit()
        result = await HistoryController.list_tradebook.fn(None)
        assert result.total == 1
        assert result.rows[0].trade_id == "tb"


class TestListLedgerEndpoint:
    def test_guarded_by_view_audit_cap(self):
        handler = HistoryController.list_ledger
        assert handler.guards[0].__name__ == "cap_guard__view_audit"

    @pytest.mark.asyncio
    async def test_returns_rows_newest_first(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_ledger_row(s, trade_date=date(2026, 4, 1), dedup_key="l1")
            await _insert_ledger_row(s, trade_date=date(2026, 4, 2), dedup_key="l2")
            await s.commit()
        result = await HistoryController.list_ledger.fn(None)
        assert isinstance(result, LedgerListResponse)
        assert result.total == 2
        assert result.rows[0].posting_date == "2026-04-02"

    @pytest.mark.asyncio
    async def test_no_default_date_window(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_ledger_row(s, trade_date=date(2026, 1, 1), dedup_key="old")
            await s.commit()
        result = await HistoryController.list_ledger.fn(None)
        assert result.total == 1

    @pytest.mark.asyncio
    async def test_segment_filter_derived_from_cost_center(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_ledger_row(
                s, dedup_key="mcx", raw_row={
                    "debit": "100", "credit": "0", "cost_center": "MCX-F&O - Z",
                    "net_balance": "900", "particulars": "x", "posting_date": "2026-04-01",
                    "voucher_type": "Delivery Voucher",
                },
            )
            await _insert_ledger_row(
                s, dedup_key="eq", raw_row={
                    "debit": "0", "credit": "50", "cost_center": "NSE-EQ - Z",
                    "net_balance": "950", "particulars": "y", "posting_date": "2026-04-01",
                    "voucher_type": "Delivery Voucher",
                },
            )
            await s.commit()
        result = await HistoryController.list_ledger.fn(None, segment="equity")
        assert result.total == 1
        assert result.rows[0].particulars == "y"

    @pytest.mark.asyncio
    async def test_account_and_broker_filters(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_ledger_row(s, account="Z1####", broker="kite", dedup_key="a")
            await _insert_ledger_row(s, account="Z1####", broker="dhan", dedup_key="b")
            await s.commit()
        result = await HistoryController.list_ledger.fn(None, broker="dhan")
        assert result.total == 1
        assert result.rows[0].broker == "dhan"

    @pytest.mark.asyncio
    async def test_pagination_applied_after_segment_filter(self, console_session_factory):
        async with console_session_factory() as s:
            for i in range(3):
                await _insert_ledger_row(
                    s, dedup_key=f"mcx{i}", trade_date=date(2026, 4, 1 + i),
                    raw_row={
                        "debit": "0", "credit": "0", "cost_center": "MCX-F&O - Z",
                        "net_balance": "0", "particulars": f"mcx{i}",
                        "posting_date": "2026-04-01", "voucher_type": "x",
                    },
                )
            await _insert_ledger_row(
                s, dedup_key="eq0", trade_date=date(2026, 4, 5),
                raw_row={
                    "debit": "0", "credit": "0", "cost_center": "NSE-EQ - Z",
                    "net_balance": "0", "particulars": "eq0",
                    "posting_date": "2026-04-05", "voucher_type": "x",
                },
            )
            await s.commit()
        result = await HistoryController.list_ledger.fn(
            None, segment="commodities", limit=2, offset=0,
        )
        assert result.total == 3  # 3 MCX rows match the segment filter
        assert len(result.rows) == 2  # but only 2 returned on this page

    @pytest.mark.asyncio
    async def test_only_ledger_report_type_returned(self, console_session_factory):
        async with console_session_factory() as s:
            await _insert_tradebook_row(s, trade_id="tb", dedup_key="tb")
            await _insert_ledger_row(s, dedup_key="lg")
            await s.commit()
        result = await HistoryController.list_ledger.fn(None)
        assert result.total == 1
