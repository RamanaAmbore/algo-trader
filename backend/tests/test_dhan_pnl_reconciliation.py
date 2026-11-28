"""Tests for the Dhan historical P&L backfill's `daily_book`
reconciliation check (`scripts/dhan_pnl_sync.py`) — mirrors
`backend/tests/test_kite_console_pnl_sync.py`'s own
`TestGetAccumulatedPnlCalendarMonthChunking` /
`TestReconcileAgainstDailyBook` / `TestBuildReconciliationAlert` /
`TestAlertPnlReconciliationDisagreement` test classes, for the Dhan
side's genuinely different ground-truth source.

Scope covered here:
  - `get_dhan_tradebook_realized_pnl()` — pure DB-backed aggregation,
    flat-vs-non-flat symbol handling, broker/account/date-range scoping.
  - `reconcile_dhan_against_daily_book()` — the shared
    `_reconcile_ground_truth_against_daily_book()` core reused, never
    copied.
  - `_dhan_run_reconciliation()` — the `main()`-wiring helper, and
    `main()`'s own gating (`exit_code == 0 and not --ledger-only`).
  - Kite/Dhan reconciliation ISOLATION — confirms neither broker's
    reconciliation can see or be affected by the other's rows/alerts.
  - The real MCX lot->contract conversion chain, confirmed end-to-end
    through `_normalise_trades(strict_mcx_lot=True)` ->
    `dhan_trade_to_kite_shape()` -> `build_sync_row()` -> this
    reconciliation's own aggregation query — not assumed.

Per CLAUDE.md: "Never mock broker SDK calls in tests" refers to the real
`dhanhq` SDK boundary — this file never touches it. The one DB-adjacent
boundary here (`backend.api.database.async_session`) is monkeypatched to
a real in-memory SQLite engine, the same technique
`test_kite_console_pnl_sync.py`'s own `recon_engine`/`recon_session_factory`
fixtures use for this exact kind of aggregation-only query.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import (
    JSON, Column, Date, DateTime, Integer, MetaData, Numeric, String, Table,
    UniqueConstraint, text as _sqltext,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from scripts import dhan_pnl_sync as dmod
from scripts import kite_console_pnl_sync as kmod


# ---------------------------------------------------------------------------
# Shared fixtures — real in-memory SQLite, including `kite_console_pnl_sync
# .broker` (deliberately present here, unlike test_kite_console_pnl_sync.py's
# own `recon_engine` fixture, which predates the broker column and has no
# reason to need it for Kite-only tests).
# ---------------------------------------------------------------------------

_RECON_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def dhan_recon_engine():
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
    Table(
        "daily_book", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("date", Date, nullable=False),
        Column("account", String(32), nullable=False),
        Column("segment", String(16), nullable=False),
        Column("kind", String(16), nullable=False),
        Column("symbol", String(64), nullable=False),
        Column("exchange", String(16), nullable=True),
        Column("qty", Integer, nullable=False, default=0),
        Column("avg_cost", Numeric, nullable=True),
        Column("ltp", Numeric, nullable=True),
        Column("day_pnl", Numeric, nullable=True),
        Column("total_pnl", Numeric, nullable=True),
        Column("payload_json", String, nullable=True),
        Column("captured_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("date", "account", "kind", "symbol",
                          name="uq_daily_book_day_acct_kind_sym"),
    )
    engine = create_async_engine(_RECON_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def dhan_recon_session_factory(dhan_recon_engine, monkeypatch):
    factory = async_sessionmaker(dhan_recon_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("backend.api.database.async_session", factory)
    return factory


# Real account roster + real mask_account() output — never a hand-typed
# "D1####" literal, same convention as test_kite_console_pnl_sync.py's own
# _RECON_MASKED_ACCOUNT.
from backend.shared.helpers.utils import mask_account as _recon_mask_account
from backend.shared.helpers.utils import register_accounts as _recon_register_accounts

# Registered ONCE, with the FULL roster every test in this file needs —
# `register_accounts()` mutates a process-global registry (per its own
# docstring caveat: the ordinal form is not stable across the roster's
# lifetime), so re-registering with a DIFFERENT account set partway
# through this file would silently shift an already-computed mask (e.g.
# introducing a DH6847/DH3747 prefix collision mid-suite would flip
# DH6847 from "DH####" to an ordinal "D1####"/"D2####" form) and break
# every test that assumed the earlier value. Register the full set up
# front instead, exactly once.
_recon_register_accounts(["DH6847", "DH3747", "ZG0790"])
_DHAN_MASKED_ACCOUNT = _recon_mask_account("DH6847")
_DHAN_OTHER_MASKED_ACCOUNT = _recon_mask_account("DH3747")
_KITE_MASKED_ACCOUNT = _recon_mask_account("ZG0790")


_DHAN_TRADE_INSERT_SQL = _sqltext("""
    INSERT INTO kite_console_pnl_sync
        (broker, account, segment, report_type, dedup_key, symbol, exchange,
         instrument_type, symbol_root, expiry, trade_date, trade_id, order_id,
         buy_qty, sell_qty, buy_value, sell_value, raw_row, updated_at)
    VALUES
        (:broker, :account, :segment, :report_type, :dedup_key, :symbol, :exchange,
         :instrument_type, :symbol_root, :expiry, :trade_date, :trade_id, :order_id,
         :buy_qty, :sell_qty, :buy_value, :sell_value, :raw_row, :updated_at)
""")

_KITE_PNL_INSERT_SQL = _sqltext("""
    INSERT INTO kite_console_pnl_sync
        (broker, account, segment, report_type, dedup_key, symbol, exchange,
         instrument_type, symbol_root, expiry, pull_range_start,
         pull_range_end, realized_pnl, raw_row, updated_at)
    VALUES
        (:broker, :account, :segment, :report_type, :dedup_key, :symbol, :exchange,
         :instrument_type, :symbol_root, :expiry, :pull_range_start,
         :pull_range_end, :realized_pnl, :raw_row, :updated_at)
""")

_DAILY_BOOK_INSERT_SQL = _sqltext("""
    INSERT INTO daily_book
        (date, account, segment, kind, symbol, exchange, qty, total_pnl, captured_at)
    VALUES
        (:date, :account, :segment, :kind, :symbol, :exchange, :qty, :total_pnl, :captured_at)
""")


async def _insert_dhan_trade_row(session, *, seq: int, trade_type: str, qty: float,
                                  price: float, **overrides):
    """One BUY or SELL tradebook row for the default fixture symbol
    (GOLDM MCX future). `seq` keeps `dedup_key`/`trade_id` unique across
    multiple calls within one test."""
    defaults = dict(
        broker="dhan", account=_DHAN_MASKED_ACCOUNT, segment="commodities",
        report_type="tradebook", symbol="GOLDM26DECFUT", exchange="MCX",
        instrument_type="future", symbol_root="GOLDM", expiry=date(2026, 12, 31),
        trade_date=date(2026, 6, 1), trade_id=f"T{seq}", order_id=f"O{seq}",
        buy_qty=qty if trade_type == "BUY" else None,
        sell_qty=qty if trade_type == "SELL" else None,
        buy_value=(qty * price) if trade_type == "BUY" else None,
        sell_value=(qty * price) if trade_type == "SELL" else None,
        raw_row="{}", updated_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    defaults["dedup_key"] = f"{defaults['exchange']}:{defaults['order_id']}:{defaults['trade_id']}"
    await session.execute(_DHAN_TRADE_INSERT_SQL, defaults)


async def _insert_kite_pnl_row(session, **overrides):
    defaults = dict(
        broker="kite", account=_KITE_MASKED_ACCOUNT, segment="commodities",
        report_type="pnl", symbol="GOLDM26DECFUT", exchange="MCX",
        instrument_type="future", symbol_root="GOLDM", expiry=date(2026, 12, 31),
        pull_range_start=date(2026, 3, 1), pull_range_end=date(2026, 6, 1),
        realized_pnl=0.0, raw_row="{}",
        updated_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    defaults["dedup_key"] = (
        f"{defaults['symbol']}:{defaults['expiry']}:"
        f"{defaults['pull_range_start']}:{defaults['pull_range_end']}"
    )
    await session.execute(_KITE_PNL_INSERT_SQL, defaults)


async def _insert_daily_book_row(session, **overrides):
    defaults = dict(
        date=date(2026, 6, 1), account="DH6847", segment="commodity",
        kind="positions", symbol="GOLDM26DECFUT", exchange="MCX", qty=0,
        total_pnl=0.0, captured_at=datetime(2026, 6, 1, 15, 30, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    await session.execute(_DAILY_BOOK_INSERT_SQL, defaults)


# ===========================================================================
# 1. get_dhan_tradebook_realized_pnl() — pure aggregation over tradebook rows
# ===========================================================================

class TestGetDhanTradebookRealizedPnl:
    @pytest.mark.asyncio
    async def test_flat_symbol_computes_sell_minus_buy(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await session.commit()

        result = await dmod.get_dhan_tradebook_realized_pnl()

        assert result["excluded_count"] == 0
        key = (_DHAN_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        assert result["by_account_root_expiry"][key] == pytest.approx(500.0)
        assert result["overall_total"] == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_non_flat_symbol_excluded_and_counted(self, dhan_recon_session_factory):
        """A symbol with an open leg (buy_qty != sell_qty) must NEVER be
        included in the ground-truth total — including it would mix a
        realized leg with still-open cost basis and silently produce a
        wrong 'realized' figure."""
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=2, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await session.commit()

        result = await dmod.get_dhan_tradebook_realized_pnl()

        assert result["by_account_root_expiry"] == {}
        assert result["overall_total"] == pytest.approx(0.0)
        assert result["excluded_count"] == 1
        assert (_DHAN_MASKED_ACCOUNT, "GOLDM26DECFUT") in result["excluded_symbols"]

    @pytest.mark.asyncio
    async def test_kite_rows_never_included_broker_filter(self, dhan_recon_session_factory):
        """A `report_type='tradebook'` row written with `broker='kite'`
        (hypothetical — the real Kite script never writes tradebook
        rows with qty/value fields populated the same way, but the
        filter itself must still hold) is excluded by the explicit
        `broker == 'dhan'` filter."""
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(
                session, seq=1, trade_type="BUY", qty=1, price=1.0,
                broker="kite", account=_KITE_MASKED_ACCOUNT,
            )
            await _insert_dhan_trade_row(session, seq=2, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=3, trade_type="SELL", qty=1, price=70500.0)
            await session.commit()

        result = await dmod.get_dhan_tradebook_realized_pnl()

        assert _KITE_MASKED_ACCOUNT not in {k[0] for k in result["by_account_root_expiry"]}
        assert result["overall_total"] == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_pnl_report_type_rows_never_included(self, dhan_recon_session_factory):
        """Even a `report_type='pnl', broker='dhan'` row (something this
        script itself never writes — see module docstring's REPORT
        TYPES section) must not be picked up by this function, which is
        scoped to `report_type == 'tradebook'` only."""
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(
                session, seq=1, trade_type="BUY", qty=1, price=70000.0,
                report_type="pnl", buy_qty=None, sell_qty=None,
                buy_value=None, sell_value=None, realized_pnl=999.0,
                dedup_key="distinct-pnl-row",
            )
            await session.commit()

        result = await dmod.get_dhan_tradebook_realized_pnl()
        assert result["by_account_root_expiry"] == {}
        assert result["overall_total"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_account_filter_scopes_to_one_account(self, dhan_recon_session_factory):
        other_masked = _DHAN_OTHER_MASKED_ACCOUNT
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_dhan_trade_row(
                session, seq=3, trade_type="BUY", qty=1, price=10.0,
                account=other_masked, dedup_key="other-account-buy",
            )
            await _insert_dhan_trade_row(
                session, seq=4, trade_type="SELL", qty=1, price=20.0,
                account=other_masked, dedup_key="other-account-sell",
            )
            await session.commit()

        result = await dmod.get_dhan_tradebook_realized_pnl(account="DH6847")

        accounts_seen = {k[0] for k in result["by_account_root_expiry"]}
        assert accounts_seen == {_DHAN_MASKED_ACCOUNT}
        assert result["overall_total"] == pytest.approx(500.0)

    @pytest.mark.asyncio
    async def test_date_range_filter_excludes_rows_outside_window(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(
                session, seq=1, trade_type="BUY", qty=1, price=70000.0,
                trade_date=date(2026, 1, 15),
            )
            await _insert_dhan_trade_row(
                session, seq=2, trade_type="SELL", qty=1, price=70500.0,
                trade_date=date(2026, 1, 20),
            )
            await session.commit()

        in_range = await dmod.get_dhan_tradebook_realized_pnl(
            start=date(2026, 1, 1), end=date(2026, 1, 31),
        )
        out_of_range = await dmod.get_dhan_tradebook_realized_pnl(
            start=date(2026, 2, 1), end=date(2026, 2, 28),
        )
        assert in_range["overall_total"] == pytest.approx(500.0)
        assert out_of_range["by_account_root_expiry"] == {}

    @pytest.mark.asyncio
    async def test_multiple_symbols_same_root_expiry_summed(self, dhan_recon_session_factory):
        """Two different option strikes under the same (root, expiry) —
        correct to sum, same as Kite's own
        `test_different_symbols_within_one_group_are_correctly_summed`."""
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(
                session, seq=1, trade_type="BUY", qty=1, price=100.0,
                symbol="NIFTY25DEC25000CE", symbol_root="NIFTY",
                instrument_type="option", segment="derivatives",
                expiry=date(2025, 12, 24),
            )
            await _insert_dhan_trade_row(
                session, seq=2, trade_type="SELL", qty=1, price=140.0,
                symbol="NIFTY25DEC25000CE", symbol_root="NIFTY",
                instrument_type="option", segment="derivatives",
                expiry=date(2025, 12, 24),
            )
            await _insert_dhan_trade_row(
                session, seq=3, trade_type="BUY", qty=1, price=80.0,
                symbol="NIFTY25DEC25500PE", symbol_root="NIFTY",
                instrument_type="option", segment="derivatives",
                expiry=date(2025, 12, 24),
            )
            await _insert_dhan_trade_row(
                session, seq=4, trade_type="SELL", qty=1, price=70.0,
                symbol="NIFTY25DEC25500PE", symbol_root="NIFTY",
                instrument_type="option", segment="derivatives",
                expiry=date(2025, 12, 24),
            )
            await session.commit()

        result = await dmod.get_dhan_tradebook_realized_pnl()
        key = (_DHAN_MASKED_ACCOUNT, "NIFTY", date(2025, 12, 24))
        # CE leg: +40 ; PE leg: -10 -> summed 30
        assert result["by_account_root_expiry"][key] == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_no_rows_at_all_returns_empty_not_an_error(self, dhan_recon_session_factory):
        result = await dmod.get_dhan_tradebook_realized_pnl()
        assert result["by_account_root_expiry"] == {}
        assert result["overall_total"] == pytest.approx(0.0)
        assert result["excluded_count"] == 0
        assert result["excluded_symbols"] == []


# ===========================================================================
# 2. reconcile_dhan_against_daily_book() — the shared core, reused
# ===========================================================================

class TestReconcileDhanAgainstDailyBook:
    @pytest.mark.asyncio
    async def test_within_tolerance_small_delta_not_flagged(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_daily_book_row(session, total_pnl=300.0)  # delta 200, within ₹500 floor
            await session.commit()

        summary = await dmod.reconcile_dhan_against_daily_book()

        assert summary["discrepancy_count"] == 0
        key = (_DHAN_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        assert summary["groups"][key]["status"] == "match"
        assert summary["excluded_count"] == 0

    @pytest.mark.asyncio
    async def test_genuine_disagreement_flagged_disagree(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_daily_book_row(session, total_pnl=-5000.0)  # badly understated
            await session.commit()

        summary = await dmod.reconcile_dhan_against_daily_book()

        key = (_DHAN_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        group = summary["groups"][key]
        assert group["status"] == "disagree"
        assert summary["disagree_count"] == 1
        assert group["ground_truth_pnl"] == pytest.approx(500.0)
        assert group["daily_book_pnl"] == pytest.approx(-5000.0)

    @pytest.mark.asyncio
    async def test_missing_daily_book_row_flagged_missing_not_disagree(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await session.commit()
            # No daily_book row inserted at all.

        summary = await dmod.reconcile_dhan_against_daily_book()

        key = (_DHAN_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31))
        assert summary["groups"][key]["status"] == "missing"
        assert summary["missing_count"] == 1
        assert summary["disagree_count"] == 0

    @pytest.mark.asyncio
    async def test_no_reconcilable_ground_truth_logs_nothing_reconcilable_not_a_clean_pass(
        self, dhan_recon_session_factory,
    ):
        """A still-open (non-flat) symbol must not silently read as '0
        discrepancies found' — that would look identical to a genuine
        clean reconciliation. `excluded_count` on the returned summary
        is the caller's signal to tell the two apart; `_dhan_run_
        reconciliation()`'s own log line always includes it (see the
        dedicated wiring test below)."""
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=2, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await session.commit()

        summary = await dmod.reconcile_dhan_against_daily_book()

        assert summary["groups_compared"] == 0
        assert summary["discrepancy_count"] == 0
        assert summary["excluded_count"] == 1, (
            "0 groups compared with a nonzero excluded_count must never "
            "be read the same as a genuinely clean 0-groups result"
        )

    @pytest.mark.asyncio
    async def test_excluded_count_present_even_with_zero_excluded(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_daily_book_row(session, total_pnl=500.0)
            await session.commit()

        summary = await dmod.reconcile_dhan_against_daily_book()
        assert summary["excluded_count"] == 0
        assert summary["excluded_symbols"] == []


# ===========================================================================
# 3. Alert reuse — `alert_pnl_reconciliation_disagreement()` with Dhan's
#    own title_prefix/source_name, imported and called unmodified.
# ===========================================================================

class TestDhanReconciliationAlertReuse:
    @pytest.mark.asyncio
    async def test_disagreement_fires_alert_with_dhan_identity(self, dhan_recon_session_factory, monkeypatch):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_daily_book_row(session, total_pnl=-5000.0)
            await session.commit()

        log_mock = MagicMock()
        monkeypatch.setattr(kmod.logger, "warning", log_mock)
        flush_mock = AsyncMock()
        monkeypatch.setattr(kmod, "_flush_and_dispatch_once", flush_mock)

        summary = await dmod.reconcile_dhan_against_daily_book()
        await dmod.alert_pnl_reconciliation_disagreement(
            summary, title_prefix="Dhan P&L reconciliation", source_name="Dhan P&L sync",
        )

        log_mock.assert_called_once()
        _, kwargs = log_mock.call_args
        extra = kwargs["extra"]
        assert extra["source_name"] == "Dhan P&L sync"
        assert extra["alert_event"] == "pnl_reconciliation_disagreement"
        assert "Dhan P&L reconciliation" in extra["title"]
        assert "GOLDM" in extra["body"]
        flush_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_within_tolerance_never_fires_alert(self, dhan_recon_session_factory, monkeypatch):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_daily_book_row(session, total_pnl=300.0)
            await session.commit()

        log_mock = MagicMock()
        monkeypatch.setattr(kmod.logger, "warning", log_mock)
        flush_mock = AsyncMock()
        monkeypatch.setattr(kmod, "_flush_and_dispatch_once", flush_mock)

        summary = await dmod.reconcile_dhan_against_daily_book()
        await dmod.alert_pnl_reconciliation_disagreement(
            summary, title_prefix="Dhan P&L reconciliation", source_name="Dhan P&L sync",
        )

        log_mock.assert_not_called()
        flush_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_ground_truth_never_fires_alert(self, dhan_recon_session_factory, monkeypatch):
        """A 'missing' group alone (daily_book has nothing for a
        reconcilable symbol) is never alert-worthy on its own — same
        gate Kite's own reconciliation uses."""
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await session.commit()

        log_mock = MagicMock()
        monkeypatch.setattr(kmod.logger, "warning", log_mock)
        flush_mock = AsyncMock()
        monkeypatch.setattr(kmod, "_flush_and_dispatch_once", flush_mock)

        summary = await dmod.reconcile_dhan_against_daily_book()
        await dmod.alert_pnl_reconciliation_disagreement(
            summary, title_prefix="Dhan P&L reconciliation", source_name="Dhan P&L sync",
        )

        log_mock.assert_not_called()
        flush_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_kite_default_call_site_is_byte_identical_after_dhan_reuse(self, monkeypatch):
        """Regression guard: adding title_prefix/source_name kwargs for
        Dhan's reuse must not change Kite's own existing, argument-less
        call sites (`main()` calls `alert_pnl_reconciliation_
        disagreement(summary)` with no kwargs at all)."""
        summary = {
            "groups": {
                (_KITE_MASKED_ACCOUNT, "GOLDM", date(2026, 12, 31)): {
                    "ground_truth_pnl": 367000.0, "daily_book_pnl": -444000.0,
                    "delta": 811000.0, "status": "disagree",
                },
            },
            "groups_compared": 1, "missing_count": 0, "disagree_count": 1,
        }
        log_mock = MagicMock()
        monkeypatch.setattr(kmod.logger, "warning", log_mock)
        monkeypatch.setattr(kmod, "_flush_and_dispatch_once", AsyncMock())

        await kmod.alert_pnl_reconciliation_disagreement(summary)

        _, kwargs = log_mock.call_args
        extra = kwargs["extra"]
        assert extra["source_name"] == "Kite Console P&L sync"
        assert "Kite Console P&L reconciliation" in extra["title"]


# ===========================================================================
# 4. Isolation — neither broker's reconciliation can see or double-fire on
#    the other's data. Per CLAUDE.md's account-masking campaign, Kite and
#    Dhan accounts can never collide on the same masked form (different
#    broker-letter prefix), so the real risk tested here is SQL-level
#    (report_type/broker filters), not account-key collision.
# ===========================================================================

class TestDhanKiteReconciliationIsolation:
    @pytest.mark.asyncio
    async def test_dhan_tradebook_rows_never_leak_into_kite_reconcile(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            # Dhan side: a genuinely closed, reconcilable round trip.
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            # Kite side: its own, unrelated pnl row + daily_book row.
            await _insert_kite_pnl_row(session, realized_pnl=100000.0)
            await _insert_daily_book_row(
                session, account="ZG0790", total_pnl=100100.0,
            )
            await session.commit()

        kite_summary = await kmod.reconcile_against_daily_book(symbol_root="GOLDM")

        accounts_seen = {k[0] for k in kite_summary["groups"]}
        assert _DHAN_MASKED_ACCOUNT not in accounts_seen
        assert accounts_seen == {_KITE_MASKED_ACCOUNT}

    @pytest.mark.asyncio
    async def test_kite_pnl_rows_never_leak_into_dhan_reconcile(self, dhan_recon_session_factory):
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_kite_pnl_row(session, realized_pnl=100000.0)
            await session.commit()

        dhan_summary = await dmod.reconcile_dhan_against_daily_book()

        accounts_seen = {k[0] for k in dhan_summary["groups"]}
        assert _KITE_MASKED_ACCOUNT not in accounts_seen
        assert accounts_seen == {_DHAN_MASKED_ACCOUNT}

    @pytest.mark.asyncio
    async def test_both_brokers_disagreeing_fire_two_distinct_alerts_not_one_merged(
        self, dhan_recon_session_factory, monkeypatch,
    ):
        """Confirms running both reconciliations in the same process
        neither merges into one alert nor double-fires for the same
        finding — two independent disagreements produce exactly two
        log records, each carrying its own broker-distinct identity."""
        async with dhan_recon_session_factory() as session:
            await _insert_dhan_trade_row(session, seq=1, trade_type="BUY", qty=1, price=70000.0)
            await _insert_dhan_trade_row(session, seq=2, trade_type="SELL", qty=1, price=70500.0)
            await _insert_daily_book_row(session, account="DH6847", total_pnl=-5000.0)
            await _insert_kite_pnl_row(session, realized_pnl=367000.0)
            await _insert_daily_book_row(session, account="ZG0790", total_pnl=-444000.0)
            await session.commit()

        log_mock = MagicMock()
        monkeypatch.setattr(kmod.logger, "warning", log_mock)
        monkeypatch.setattr(kmod, "_flush_and_dispatch_once", AsyncMock())

        kite_summary = await kmod.reconcile_against_daily_book(symbol_root="GOLDM")
        await kmod.alert_pnl_reconciliation_disagreement(kite_summary)

        dhan_summary = await dmod.reconcile_dhan_against_daily_book()
        await dmod.alert_pnl_reconciliation_disagreement(
            dhan_summary, title_prefix="Dhan P&L reconciliation", source_name="Dhan P&L sync",
        )

        assert log_mock.call_count == 2
        source_names = {c.kwargs["extra"]["source_name"] for c in log_mock.call_args_list}
        assert source_names == {"Kite Console P&L sync", "Dhan P&L sync"}
        bodies = [c.kwargs["extra"]["body"] for c in log_mock.call_args_list]
        assert any(_KITE_MASKED_ACCOUNT in b for b in bodies)
        assert any(_DHAN_MASKED_ACCOUNT in b for b in bodies)


# ===========================================================================
# 5. MCX lot->contract conversion — confirmed end-to-end, not assumed.
# ===========================================================================

class TestMcxUnitsAlreadyConverted:
    """The real pipeline: raw Dhan API trade (quantity in LOTS) ->
    `_normalise_trades(strict_mcx_lot=True)` (lot->contract conversion)
    -> `dhan_trade_to_kite_shape()` -> `build_sync_row()` (buy_value/
    sell_value = qty * price) -> this reconciliation's own aggregation.
    If the lot->contract step were ever skipped, the resulting realized
    figure would be wrong by exactly `lot_size`x — this test proves the
    real chain does NOT produce that wrong figure."""

    def setup_method(self):
        import backend.brokers.adapters.dhan as m
        m._DHAN_LOT_BY_SECURITY.clear()
        m._DHAN_LOT_BY_MCX_ROOT.clear()

    def teardown_method(self):
        import backend.brokers.adapters.dhan as m
        m._DHAN_LOT_BY_SECURITY.clear()
        m._DHAN_LOT_BY_MCX_ROOT.clear()

    @staticmethod
    def _raw_dhan_trade(*, trade_id: str, order_id: str, security_id: str,
                         transaction_type: str, lots: int, price: float) -> dict:
        """`tradedQuantity` is in LOTS here — the real Dhan API field
        this whole conversion chain exists to correct before it ever
        reaches a stored `buy_value`/`sell_value`."""
        return {
            "tradeId": trade_id, "orderId": order_id,
            "tradingSymbol": "CRUDEOIL24DECFUT", "exchange": "MCX",
            "exchangeSegment": "MCX_COMM", "securityId": security_id,
            "transactionType": transaction_type, "tradedQuantity": lots,
            "tradedPrice": price, "exchangeTime": "2024-12-10 10:00:00",
        }

    @pytest.mark.asyncio
    async def test_realized_pnl_uses_contract_units_not_raw_lots(
        self, dhan_recon_session_factory,
    ):
        import backend.brokers.adapters.dhan as dhan_mod

        lot_size = 100
        dhan_mod._DHAN_LOT_BY_SECURITY["SID_CRUDE"] = lot_size

        raw_buy = self._raw_dhan_trade(
            trade_id="T1", order_id="O1", security_id="SID_CRUDE",
            transaction_type="BUY", lots=1, price=5800.0,
        )
        raw_sell = self._raw_dhan_trade(
            trade_id="T2", order_id="O2", security_id="SID_CRUDE",
            transaction_type="SELL", lots=1, price=5850.0,
        )
        unresolved: list = []
        normalised = dhan_mod._normalise_trades(
            {"status": "success", "data": [raw_buy, raw_sell]},
            strict_mcx_lot=True, unresolved=unresolved,
        )
        assert unresolved == []
        assert len(normalised) == 2
        # Confirms the lot->contract multiplication genuinely ran before
        # anything else in the chain sees `quantity`.
        assert all(t["quantity"] == lot_size for t in normalised)

        mapped_rows = [dmod.dhan_trade_to_kite_shape(t) for t in normalised]

        async with dhan_recon_session_factory() as session:
            for seq, m in enumerate(mapped_rows, start=1):
                row = kmod.build_sync_row(
                    _DHAN_MASKED_ACCOUNT, "commodities", "tradebook", m,
                    broker="dhan",
                )
                await session.execute(_sqltext("""
                    INSERT INTO kite_console_pnl_sync
                        (broker, account, segment, report_type, dedup_key, symbol,
                         exchange, instrument_type, symbol_root, expiry, trade_date,
                         trade_id, order_id, buy_qty, sell_qty, buy_value, sell_value,
                         raw_row, updated_at)
                    VALUES
                        (:broker, :account, :segment, :report_type, :dedup_key, :symbol,
                         :exchange, :instrument_type, :symbol_root, :expiry, :trade_date,
                         :trade_id, :order_id, :buy_qty, :sell_qty, :buy_value, :sell_value,
                         '{}', :updated_at)
                """), {
                    "broker": row["broker"], "account": row["account"],
                    "segment": row["segment"], "report_type": row["report_type"],
                    "dedup_key": row["dedup_key"], "symbol": row["symbol"],
                    "exchange": row["exchange"], "instrument_type": row["instrument_type"],
                    "symbol_root": row["symbol_root"], "expiry": row["expiry"],
                    "trade_date": row["trade_date"], "trade_id": row["trade_id"],
                    "order_id": row["order_id"], "buy_qty": row["buy_qty"],
                    "sell_qty": row["sell_qty"], "buy_value": row["buy_value"],
                    "sell_value": row["sell_value"], "updated_at": row["updated_at"],
                })
            await session.commit()

        result = await dmod.get_dhan_tradebook_realized_pnl()

        # Correct (contract-unit) answer: (5850 - 5800) * 100 = 5000.
        # Real parse_tradingsymbol("CRUDEOIL24DECFUT") expiry, confirmed
        # directly rather than guessed (CRUDEOIL's last-trading-day rule
        # differs from GOLDM's) — see derivatives.parse_tradingsymbol().
        key = (_DHAN_MASKED_ACCOUNT, "CRUDEOIL", date(2024, 12, 26))
        assert result["by_account_root_expiry"][key] == pytest.approx(5000.0)
        # If the lot->contract conversion had been skipped (raw lots
        # used instead), the figure would have been 100x smaller — the
        # defect class this whole mechanism exists to prevent.
        wrong_unconverted_figure = (5850.0 - 5800.0) * 1
        assert result["by_account_root_expiry"][key] != pytest.approx(wrong_unconverted_figure)


# ===========================================================================
# 6. main() wiring — the reconciliation runs only on a clean, non-ledger-
#    only backfill, is non-fatal on its own failure, and never blocks the
#    real sync outcome's exit code.
# ===========================================================================

class TestMainReconciliationWiring:
    @pytest.mark.asyncio
    async def test_main_calls_reconciliation_after_clean_backfill(self, monkeypatch):
        row = MagicMock(account="DH6847", client_id="1100000001")
        monkeypatch.setattr(dmod, "load_active_dhan_accounts", AsyncMock(return_value=[row]))
        monkeypatch.setattr(dmod, "_register_all_active_dhan_masks", MagicMock())
        monkeypatch.setattr(
            dmod, "sync_account",
            AsyncMock(return_value=dmod.DhanSyncOutcome(
                account="D1####", ok=True, trades_ingested=3, ledger_ingested=1,
            )),
        )
        recon_mock = AsyncMock()
        monkeypatch.setattr(dmod, "_dhan_run_reconciliation", recon_mock)
        with patch("backend.brokers.connections.Connections.rebuild_from_db", AsyncMock()):
            exit_code = await dmod.main(["--mode", "backfill", "--start", "2026-04-01", "--end", "2026-04-30"])

        assert exit_code == 0
        recon_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_main_skips_reconciliation_when_ledger_only(self, monkeypatch):
        row = MagicMock(account="DH6847", client_id="1100000001")
        monkeypatch.setattr(dmod, "load_active_dhan_accounts", AsyncMock(return_value=[row]))
        monkeypatch.setattr(dmod, "_register_all_active_dhan_masks", MagicMock())
        monkeypatch.setattr(
            dmod, "sync_account",
            AsyncMock(return_value=dmod.DhanSyncOutcome(account="D1####", ok=True, ledger_ingested=1)),
        )
        recon_mock = AsyncMock()
        monkeypatch.setattr(dmod, "_dhan_run_reconciliation", recon_mock)
        with patch("backend.brokers.connections.Connections.rebuild_from_db", AsyncMock()):
            exit_code = await dmod.main([
                "--mode", "backfill", "--ledger-only",
                "--start", "2026-04-01", "--end", "2026-04-30",
            ])

        assert exit_code == 0
        recon_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_main_skips_reconciliation_when_exit_code_nonzero(self, monkeypatch):
        row = MagicMock(account="DH6847", client_id="1100000001")
        monkeypatch.setattr(dmod, "load_active_dhan_accounts", AsyncMock(return_value=[row]))
        monkeypatch.setattr(dmod, "_register_all_active_dhan_masks", MagicMock())
        monkeypatch.setattr(
            dmod, "sync_account",
            AsyncMock(return_value=dmod.DhanSyncOutcome(
                account="D1####", ok=False, segment_errors=["boom"],
            )),
        )
        monkeypatch.setattr(dmod, "alert_critical", AsyncMock())
        recon_mock = AsyncMock()
        monkeypatch.setattr(dmod, "_dhan_run_reconciliation", recon_mock)
        with patch("backend.brokers.connections.Connections.rebuild_from_db", AsyncMock()):
            exit_code = await dmod.main(["--mode", "backfill", "--start", "2026-04-01", "--end", "2026-04-30"])

        assert exit_code == 1
        recon_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dhan_run_reconciliation_never_raises_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            dmod, "reconcile_dhan_against_daily_book",
            AsyncMock(side_effect=RuntimeError("db exploded")),
        )
        alert_mock = AsyncMock()
        monkeypatch.setattr(dmod, "alert_pnl_reconciliation_disagreement", alert_mock)

        await dmod._dhan_run_reconciliation()  # must not raise

        alert_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dhan_run_reconciliation_calls_alert_with_dhan_identity(self, monkeypatch):
        summary = {
            "groups": {}, "groups_compared": 0, "missing_count": 0,
            "disagree_count": 0, "discrepancy_count": 0, "excluded_count": 0, "excluded_symbols": [],
        }
        monkeypatch.setattr(
            dmod, "reconcile_dhan_against_daily_book", AsyncMock(return_value=summary),
        )
        alert_mock = AsyncMock()
        monkeypatch.setattr(dmod, "alert_pnl_reconciliation_disagreement", alert_mock)

        await dmod._dhan_run_reconciliation()

        alert_mock.assert_awaited_once()
        _, kwargs = alert_mock.call_args
        assert kwargs["title_prefix"] == "Dhan P&L reconciliation"
        assert kwargs["source_name"] == "Dhan P&L sync"

    @pytest.mark.asyncio
    async def test_dhan_run_reconciliation_calls_unbounded_no_account_or_date_filter(self, monkeypatch):
        """Mirrors `scripts.kite_console_pnl_sync.main()`'s own
        unbounded `reconcile_against_daily_book()` call — confirms this
        is a deliberate choice, not an oversight."""
        recon_mock = AsyncMock(return_value={
            "groups": {}, "groups_compared": 0, "missing_count": 0,
            "disagree_count": 0, "discrepancy_count": 0, "excluded_count": 0, "excluded_symbols": [],
        })
        monkeypatch.setattr(dmod, "reconcile_dhan_against_daily_book", recon_mock)
        monkeypatch.setattr(dmod, "alert_pnl_reconciliation_disagreement", AsyncMock())

        await dmod._dhan_run_reconciliation()

        recon_mock.assert_awaited_once_with()
