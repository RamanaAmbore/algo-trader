"""
test_expiry_devolve_backfill.py — tests for the delayed expiry-devolvement
daily_book backfill (2026-11-28 live incident).

Confirmed live gap: the main EOD close-snapshot (`_positions_rows` via
`snapshot_daily_book`) does ONE point-in-time broker fetch when it fires.
On 2026-11-27, two accounts (ZG0790, ZJ6294) each held an ITM MCX
GOLD/GOLDM option through its own expiry — the option physically devolved
into a short future at the broker, but Kite's own backend had not finished
processing the devolvement by the time the EOD snapshot ran, so that
day's daily_book batch for both accounts contains ZERO rows for the new
future symbol.

`daily_snapshot.run_expiry_devolve_backfill()` is a delayed (~30 min after
MCX close) re-check that:
  1. identifies, from already-persisted daily_book rows (zero broker
     calls), which accounts held an MCX option expiring today;
  2. for only those accounts, re-fetches LIVE positions and backfills any
     daily_book row genuinely missing for a symbol the broker now reports
     holding.

`background.py:_ds_tick_expiry_devolve_backfill` is the scheduling-only
wrapper (mirrors `_ds_tick_mcx_stop`'s own MCX-close-relative threshold +
post-midnight-rollover handling), gated by a one-shot-per-day DB marker
(`_edb_marker_date`/`_edb_mark_done`, same restart-safety pattern as
`_pcf_marker_date`/`_msr_marker_date`).
"""

from __future__ import annotations

import types
from datetime import date, datetime, time as dtime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from zoneinfo import ZoneInfo

# pytest-asyncio runs in auto mode for this project (see pyproject.toml) —
# async test functions are detected automatically, so no module-level
# `pytestmark = pytest.mark.asyncio` is needed (and would wrongly tag the
# plain sync tests in TestEdbMcxExpiringAccounts below).

IST = ZoneInfo("Asia/Kolkata")

# Real incident date/symbols — 2026-11-27 is GOLDM/GOLD's November MCX
# option expiry (confirmed via parse_tradingsymbol('GOLDM26NOV68000CE')
# == date(2026, 11, 27)).
_TARGET_DATE = date(2026, 11, 27)
_ZG_OPTION = "GOLDM26NOV68000CE"
_ZG_DEVOLVED_FUTURE = "GOLDM26DECFUT"
_ZJ_OPTION = "GOLD26NOV72500PE"
_ZJ_DEVOLVED_FUTURE = "GOLD26DECFUT"


# ---------------------------------------------------------------------------
# _edb_mcx_expiring_accounts — pure classification function
# ---------------------------------------------------------------------------


class TestEdbMcxExpiringAccounts:
    def test_option_expiring_today_is_a_candidate(self):
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        rows = [("ZG0790", _ZG_OPTION, -50)]
        out = _edb_mcx_expiring_accounts(rows, _TARGET_DATE)
        assert out == {"ZG0790": [_ZG_OPTION]}

    def test_option_expiring_a_different_day_is_excluded(self):
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        # GOLDM26DECFUT expiry is 2026-12-24 — not today.
        rows = [("ZG0790", "GOLDM26DEC70000CE", -50)]
        out = _edb_mcx_expiring_accounts(rows, _TARGET_DATE)
        assert out == {}

    def test_flat_qty_row_is_excluded(self):
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        rows = [("ZG0790", _ZG_OPTION, 0)]
        out = _edb_mcx_expiring_accounts(rows, _TARGET_DATE)
        assert out == {}

    def test_future_row_is_excluded(self):
        """A future symbol is never itself a 'candidate option expiring
        today' — only the CE/PE option side is relevant for devolvement
        detection."""
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        rows = [("ZG0790", "GOLDM26NOVFUT", -1)]
        out = _edb_mcx_expiring_accounts(rows, _TARGET_DATE)
        assert out == {}

    def test_multiple_accounts_aggregated(self):
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        rows = [
            ("ZG0790", _ZG_OPTION, -50),
            ("ZJ6294", _ZJ_OPTION, 25),
        ]
        out = _edb_mcx_expiring_accounts(rows, _TARGET_DATE)
        assert out == {"ZG0790": [_ZG_OPTION], "ZJ6294": [_ZJ_OPTION]}

    def test_multiple_symbols_same_account(self):
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        rows = [
            ("ZG0790", _ZG_OPTION, -50),
            ("ZG0790", "GOLDM26NOV69000PE", 10),
        ]
        out = _edb_mcx_expiring_accounts(rows, _TARGET_DATE)
        assert out["ZG0790"] == [_ZG_OPTION, "GOLDM26NOV69000PE"]

    def test_empty_rows_returns_empty(self):
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        assert _edb_mcx_expiring_accounts([], _TARGET_DATE) == {}

    def test_unparseable_symbol_excluded(self):
        from backend.api.algo.daily_snapshot import _edb_mcx_expiring_accounts

        rows = [("ZG0790", "SOMEJUNK", -1)]
        out = _edb_mcx_expiring_accounts(rows, _TARGET_DATE)
        assert out == {}


# ---------------------------------------------------------------------------
# run_expiry_devolve_backfill — end to end (broker/DB boundaries mocked,
# matching the established daily_snapshot.py test convention — e.g.
# TestSnapshotDailyBookAccountsScoping — of patching `all_brokers`,
# `_fetch_account_data`, `_upsert_rows` rather than hitting a real
# Postgres-only `_UPSERT_SQL` upsert on SQLite).
# ---------------------------------------------------------------------------


def _fake_broker(account: str) -> MagicMock:
    b = MagicMock()
    b.account = account
    return b


class TestRunExpiryDevolveBackfillNoCandidates:
    """Test case 4 (perf requirement): no MCX option expiring today at
    all → the task must skip entirely, with NO broker call made."""

    async def test_no_mcx_expiring_rows_skips_with_zero_broker_calls(self):
        from backend.api.algo import daily_snapshot as ds

        with patch.object(ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=[])), \
             patch("backend.brokers.registry.all_brokers") as mock_all_brokers, \
             patch.object(ds, "_fetch_account_data") as mock_fetch, \
             patch.object(ds, "_upsert_rows", AsyncMock(return_value=0)) as mock_upsert:
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        mock_all_brokers.assert_not_called()
        mock_fetch.assert_not_called()
        mock_upsert.assert_not_called()
        assert result["accounts_checked"] == []
        assert result["accounts_backfilled"] == []
        assert result["symbols_backfilled"] == {}
        assert result["target_date"] == _TARGET_DATE

    async def test_rows_present_but_none_parse_as_expiring_today_also_skips(self):
        """A candidate-account query that returns MCX rows which don't
        parse to an option expiring target_date (e.g. a future, or an
        option expiring a different day) must also make zero broker
        calls — the broker-loop guard is driven by the CLASSIFIED
        candidate set, not the raw row count."""
        from backend.api.algo import daily_snapshot as ds

        with patch.object(
            ds, "_edb_query_mcx_positions_rows",
            AsyncMock(return_value=[("ZG0790", "GOLDM26NOVFUT", -1)]),
        ), patch("backend.brokers.registry.all_brokers") as mock_all_brokers:
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        mock_all_brokers.assert_not_called()
        assert result["accounts_checked"] == []


class TestRunExpiryDevolveBackfillGapFound:
    """Test case 1 (the real incident scenario): an ITM MCX leg expired
    today, broker now shows a devolved future with no daily_book row —
    confirm the backfill writes it correctly."""

    async def test_devolved_future_is_backfilled(self):
        from backend.api.algo import daily_snapshot as ds

        candidate_rows = [("ZG0790", _ZG_OPTION, -50)]
        existing_symbols = {_ZG_OPTION}  # the option itself was already captured

        raw_positions = [
            {
                "tradingsymbol": _ZG_OPTION, "exchange": "MCX",
                "quantity": 0, "average_price": 0.0, "last_price": 0.0,
                "pnl": -1200.0, "close_price": 68500.0,
            },
            {
                # The newly-devolved future — NOT in existing_symbols.
                "tradingsymbol": _ZG_DEVOLVED_FUTURE, "exchange": "MCX",
                "quantity": -1, "average_price": 68000.0, "last_price": 68200.0,
                "pnl": -200.0, "close_price": 68000.0, "overnight_quantity": 0,
            },
        ]

        upserted: list = []
        upsert_calls: list = []
        # The already-persisted main EOD batch's own captured_at for this
        # account — the backfilled row MUST reuse this exact stamp, never
        # a fresh `now()` (see `_upsert_rows`'s own docstring: a later
        # stamp would make the backfilled row a later, exclusive "latest
        # batch" that masks every other correctly-captured row).
        existing_eod_captured_at = datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc)

        async def _fake_upsert(rows, *, captured_at=None):
            upserted.extend(rows)
            upsert_calls.append(captured_at)
            return len(rows)

        broker = _fake_broker("ZG0790")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(
            ds, "_edb_existing_symbols_for_account", AsyncMock(return_value=existing_symbols),
        ), patch.object(
            ds, "_edb_existing_captured_at_for_account",
            AsyncMock(return_value=existing_eod_captured_at),
        ), patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch.object(
                 ds, "_fetch_account_data",
                 return_value={"holdings": [], "positions": raw_positions, "trades": [], "funds": []},
             ), \
             patch.object(ds, "_upsert_rows", _fake_upsert), \
             patch.object(ds, "_snap_broadcast_book_changed") as mock_broadcast:
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        assert result["accounts_checked"] == ["ZG0790"]
        assert result["accounts_backfilled"] == ["ZG0790"]
        assert result["symbols_backfilled"] == {"ZG0790": [_ZG_DEVOLVED_FUTURE]}

        # Exactly one row upserted — the devolved future, never the
        # already-captured option.
        assert len(upserted) == 1
        row = upserted[0]
        assert row["symbol"] == _ZG_DEVOLVED_FUTURE
        assert row["account"] == "ZG0790"
        assert row["kind"] == "positions"
        assert row["date"] == _TARGET_DATE
        assert row["qty"] == -1

        # The defect-1 regression check: the backfilled row's captured_at
        # was threaded through as the account's own existing EOD batch
        # stamp, not left to default to a fresh `now()`.
        assert upsert_calls == [existing_eod_captured_at]

        mock_broadcast.assert_called_once_with(["ZG0790"])

    async def test_two_accounts_each_get_their_own_devolved_future(self):
        """Mirrors the real incident exactly — ZG0790 and ZJ6294 each had
        their own ITM MCX option devolve independently; each account's
        backfill must be scoped to its own data, never cross-contaminate."""
        from backend.api.algo import daily_snapshot as ds

        candidate_rows = [
            ("ZG0790", _ZG_OPTION, -50),
            ("ZJ6294", _ZJ_OPTION, 25),
        ]

        def _raw_for(account):
            if account == "ZG0790":
                return {
                    "holdings": [], "trades": [], "funds": [],
                    "positions": [
                        {"tradingsymbol": _ZG_OPTION, "exchange": "MCX", "quantity": 0,
                         "average_price": 0.0, "last_price": 0.0, "pnl": -500.0},
                        {"tradingsymbol": _ZG_DEVOLVED_FUTURE, "exchange": "MCX", "quantity": -1,
                         "average_price": 68000.0, "last_price": 68200.0, "pnl": -200.0,
                         "close_price": 68000.0},
                    ],
                }
            return {
                "holdings": [], "trades": [], "funds": [],
                "positions": [
                    {"tradingsymbol": _ZJ_OPTION, "exchange": "MCX", "quantity": 0,
                     "average_price": 0.0, "last_price": 0.0, "pnl": 300.0},
                    {"tradingsymbol": _ZJ_DEVOLVED_FUTURE, "exchange": "MCX", "quantity": 1,
                     "average_price": 72000.0, "last_price": 71900.0, "pnl": -100.0,
                     "close_price": 72000.0},
                ],
            }

        async def _fake_existing(target_date, account):
            return {_ZG_OPTION} if account == "ZG0790" else {_ZJ_OPTION}

        # Each account's own, DIFFERENT existing EOD batch captured_at —
        # proves the stamp is resolved per-account, never shared/mixed up.
        _captured_at_by_account = {
            "ZG0790": datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc),
            "ZJ6294": datetime(2026, 11, 27, 18, 16, tzinfo=timezone.utc),
        }

        async def _fake_existing_captured_at(target_date, account):
            return _captured_at_by_account[account]

        upserted: list = []
        upsert_captured_at_by_symbol: dict = {}

        async def _fake_upsert(rows, *, captured_at=None):
            upserted.extend(rows)
            for r in rows:
                upsert_captured_at_by_symbol[r["symbol"]] = captured_at
            return len(rows)

        broker_zg = _fake_broker("ZG0790")
        broker_zj = _fake_broker("ZJ6294")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(ds, "_edb_existing_symbols_for_account", _fake_existing), \
             patch.object(ds, "_edb_existing_captured_at_for_account", _fake_existing_captured_at), \
             patch("backend.brokers.registry.all_brokers", return_value=[broker_zg, broker_zj]), \
             patch.object(ds, "_fetch_account_data", side_effect=lambda broker, account, target_date: _raw_for(account)), \
             patch.object(ds, "_upsert_rows", _fake_upsert), \
             patch.object(ds, "_snap_broadcast_book_changed"):
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        assert set(result["accounts_backfilled"]) == {"ZG0790", "ZJ6294"}
        assert result["symbols_backfilled"]["ZG0790"] == [_ZG_DEVOLVED_FUTURE]
        assert result["symbols_backfilled"]["ZJ6294"] == [_ZJ_DEVOLVED_FUTURE]
        backfilled_symbols = {r["symbol"] for r in upserted}
        assert backfilled_symbols == {_ZG_DEVOLVED_FUTURE, _ZJ_DEVOLVED_FUTURE}

        # Each account's backfilled row reused ITS OWN existing EOD batch
        # captured_at — never cross-contaminated with the other account's.
        assert upsert_captured_at_by_symbol[_ZG_DEVOLVED_FUTURE] == (
            _captured_at_by_account["ZG0790"]
        )
        assert upsert_captured_at_by_symbol[_ZJ_DEVOLVED_FUTURE] == (
            _captured_at_by_account["ZJ6294"]
        )


class TestRunExpiryDevolveBackfillNoGap:
    """Test case 2: devolvement was captured correctly the first time —
    the task must find nothing to backfill and be a genuine no-op."""

    async def test_already_captured_devolved_future_is_a_noop(self):
        from backend.api.algo import daily_snapshot as ds

        candidate_rows = [("ZG0790", _ZG_OPTION, -50)]
        # Both the option AND the devolved future are already captured.
        existing_symbols = {_ZG_OPTION, _ZG_DEVOLVED_FUTURE}

        raw_positions = [
            {"tradingsymbol": _ZG_OPTION, "exchange": "MCX", "quantity": 0,
             "average_price": 0.0, "last_price": 0.0, "pnl": -1200.0},
            {"tradingsymbol": _ZG_DEVOLVED_FUTURE, "exchange": "MCX", "quantity": -1,
             "average_price": 68000.0, "last_price": 68200.0, "pnl": -200.0,
             "close_price": 68000.0},
        ]

        broker = _fake_broker("ZG0790")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(
            ds, "_edb_existing_symbols_for_account", AsyncMock(return_value=existing_symbols),
        ), patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch.object(
                 ds, "_fetch_account_data",
                 return_value={"holdings": [], "positions": raw_positions, "trades": [], "funds": []},
             ), \
             patch.object(ds, "_upsert_rows", AsyncMock(return_value=0)) as mock_upsert, \
             patch.object(ds, "_snap_broadcast_book_changed") as mock_broadcast:
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        assert result["accounts_checked"] == ["ZG0790"]
        assert result["accounts_backfilled"] == []
        assert result["symbols_backfilled"] == {}
        mock_upsert.assert_not_called()
        mock_broadcast.assert_not_called()

    async def test_account_fetch_failure_is_tolerated_not_fatal(self):
        """A per-account exception (broker outage during the re-check)
        must not raise out of the function — it is logged and skipped,
        same fail-soft convention as the main snapshot_daily_book loop."""
        from backend.api.algo import daily_snapshot as ds

        candidate_rows = [("ZG0790", _ZG_OPTION, -50)]
        broker = _fake_broker("ZG0790")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(
            ds, "_edb_existing_symbols_for_account",
            AsyncMock(side_effect=RuntimeError("DB unreachable")),
        ), patch("backend.brokers.registry.all_brokers", return_value=[broker]):
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        assert result["accounts_checked"] == ["ZG0790"]
        assert result["accounts_backfilled"] == []


# ---------------------------------------------------------------------------
# _ds_tick_expiry_devolve_backfill — scheduling wrapper
# ---------------------------------------------------------------------------


def _mcx_row(close_time=dtime(23, 30)):
    return types.SimpleNamespace(close_time=close_time)


class TestDsTickExpiryDevolveBackfill:
    async def test_no_fire_before_threshold(self):
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        now = datetime(2026, 11, 27, 23, 55, tzinfo=IST)  # close(23:30)+25min, before +30
        today = now.date()
        done_ref = [None]
        with patch.object(exchange_clock, "_effective_gate_rows_for_date", return_value=[_mcx_row()]), \
             patch(
                 "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill", AsyncMock(),
             ) as mock_run:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)
        mock_run.assert_not_awaited()
        assert done_ref[0] is None

    async def test_fires_at_threshold(self):
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        now = datetime(2026, 11, 28, 0, 0, 0, tzinfo=IST)  # MCX close(23:30)+30min = 00:00
        today = now.date()  # real caller convention — today is always now.date()
        done_ref = [None]
        with patch.object(exchange_clock, "_effective_gate_rows_for_date", return_value=[_mcx_row()]), \
             patch(
                 "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill",
                 AsyncMock(return_value={"accounts_backfilled": []}),
             ) as mock_run, \
             patch("backend.api.background._edb_marker_date", AsyncMock(return_value=None)), \
             patch("backend.api.background._edb_mark_done", AsyncMock()) as mock_mark_done:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)
        mock_run.assert_awaited_once()
        mock_mark_done.assert_awaited_once()

    async def test_no_fire_when_no_mcx_gate_row(self):
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        now = datetime(2026, 11, 28, 1, 0, tzinfo=IST)
        today = now.date()
        done_ref = [None]
        with patch.object(exchange_clock, "_effective_gate_rows_for_date", return_value=[]), \
             patch(
                 "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill", AsyncMock(),
             ) as mock_run:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)
        mock_run.assert_not_awaited()

    async def test_fires_only_once_via_in_memory_done_ref(self):
        """Test case 3: already ran today → don't re-fire. First the
        in-memory `done_ref` fast path."""
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        now = datetime(2026, 11, 28, 1, 0, tzinfo=IST)
        today = now.date()
        fire_date = date(2026, 11, 27)
        done_ref = [fire_date]  # already fired for this session date
        with patch.object(exchange_clock, "_effective_gate_rows_for_date", return_value=[_mcx_row()]), \
             patch(
                 "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill", AsyncMock(),
             ) as mock_run:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)
        mock_run.assert_not_awaited()

    async def test_fires_only_once_via_db_marker_across_a_restart(self):
        """Test case 3, process-restart variant: `done_ref` resets to
        `[None]` on a fresh process, but the DB marker still blocks a
        second run for the same trading session."""
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        now = datetime(2026, 11, 28, 1, 0, tzinfo=IST)
        today = now.date()
        fire_date = date(2026, 11, 27)
        done_ref = [None]  # fresh process — no in-memory record
        with patch.object(exchange_clock, "_effective_gate_rows_for_date", return_value=[_mcx_row()]), \
             patch(
                 "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill", AsyncMock(),
             ) as mock_run, \
             patch("backend.api.background._edb_marker_date", AsyncMock(return_value=fire_date)):
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)
        mock_run.assert_not_awaited()
        # The local sentinel is still seeded so subsequent ticks this
        # process don't re-hit the DB.
        assert done_ref[0] == fire_date

    async def test_mid_run_exception_does_not_retry_same_tick_window(self):
        """Marked done BEFORE running — a scan failure must not be
        retried on every subsequent 30s tick for the rest of the day."""
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        now = datetime(2026, 11, 28, 0, 0, tzinfo=IST)
        today = now.date()  # real caller convention — today is always now.date()
        fire_date = date(2026, 11, 27)
        done_ref = [None]
        with patch.object(exchange_clock, "_effective_gate_rows_for_date", return_value=[_mcx_row()]), \
             patch(
                 "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill",
                 AsyncMock(side_effect=RuntimeError("boom")),
             ), patch("backend.api.background._edb_marker_date", AsyncMock(return_value=None)), \
             patch("backend.api.background._edb_mark_done", AsyncMock()) as mock_mark_done:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)
        assert done_ref[0] == fire_date
        mock_mark_done.assert_awaited_once_with(fire_date)


# ---------------------------------------------------------------------------
# _ds_tick_expiry_devolve_backfill — session-day gate-row resolution
# (2026-11-28 fix for defect 2: the OLD code called
# `exchange_clock._effective_gate_rows("MCX")` — today-anchored, at
# tick-run time — BEFORE computing `session_day`. Since MCX close + 30
# minutes reliably crosses IST midnight, this tick almost always runs on
# the calendar day AFTER the session (e.g. a Friday close checked just
# after midnight on Saturday). "Today" at that moment is Saturday, whose
# Mon-Fri weekday filter returns `[]` — close_t resolves to None and the
# function returns before the already-correct `session_day` adjustment
# ever mattered, silently skipping every Friday MCX session.
#
# These tests exercise the REAL `exchange_clock._CACHE` + real
# `_effective_gate_rows_for_date`/`_effective_gate_rows` (not a mocked
# return value) — same pattern as
# `test_exchange_clock_mcx_winter_snapshot_gap.py`'s sibling midnight-
# wrap fix, which this fix follows.
# ---------------------------------------------------------------------------


class TestDsTickExpiryDevolveBackfillSessionDayResolution:
    FRIDAY = date(2026, 11, 20)
    SATURDAY = date(2026, 11, 21)

    @staticmethod
    def _default_row(close_time, weekdays=(0, 1, 2, 3, 4)):
        """A default (date=None) MCX gate row, weekday-filtered — same
        shape `_row_matches_date`/`_effective_gate_rows_for_date`
        inspect (`gate`, `date`, `weekdays`, `close_time`)."""
        return types.SimpleNamespace(
            gate="MCX", date=None, weekdays=list(weekdays), close_time=close_time,
        )

    @staticmethod
    def _override_row(for_date, close_time):
        """A date-specific override row (e.g. an MCX holiday,
        close_time=None) — overrides have absolute priority over
        default rows for that one date, per
        `_effective_gate_rows_for_date`'s own docstring."""
        return types.SimpleNamespace(
            gate="MCX", date=for_date, weekdays=None, close_time=close_time,
        )

    @pytest.fixture(autouse=True)
    def _restore_cache(self):
        """`exchange_clock._CACHE` is module-level global state — isolate
        it per test so this class can't leak into any other test's cache
        (same convention as `test_exchange_clock_mcx_winter_snapshot_gap.py`)."""
        from backend.api.helpers import exchange_clock
        original = exchange_clock._CACHE
        yield
        exchange_clock._CACHE = original

    async def test_friday_winter_close_fires_when_checked_saturday_post_midnight(self):
        """The real reported hazard: a Friday MCX close at the winter
        23:55 IST close, checked just after midnight on Saturday
        (close+30min reliably crosses midnight). The fix must resolve
        Friday's own default row via `_effective_gate_rows_for_date`
        (keyed on `session_day`=Friday), not the bare, today-anchored
        `_effective_gate_rows` (which would resolve "today"=Saturday and
        find nothing, per the next test)."""
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        assert self.FRIDAY.weekday() == 4 and self.SATURDAY.weekday() == 5
        exchange_clock._CACHE = [self._default_row(dtime(23, 55))]
        now = datetime(2026, 11, 21, 0, 25, tzinfo=IST)  # Sat 00:25 = Fri close(23:55)+30min
        today = now.date()
        done_ref = [None]

        with patch(
            "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill",
            AsyncMock(return_value={"accounts_backfilled": []}),
        ) as mock_run, \
             patch("backend.api.background._edb_marker_date", AsyncMock(return_value=None)), \
             patch("backend.api.background._edb_mark_done", AsyncMock()) as mock_mark_done:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)

        mock_run.assert_awaited_once()
        mock_mark_done.assert_awaited_once_with(self.FRIDAY)
        assert done_ref[0] == self.FRIDAY

    def test_old_today_anchored_lookup_would_have_found_nothing(self):
        """Contrast proof, not exercising the tick itself: the bare,
        today-anchored `_effective_gate_rows` (what the PRE-FIX code
        called) genuinely returns `[]` at the exact clock moment the
        test above fires at — demonstrating the old code path's gate-rows
        lookup really was the defect, not merely a cosmetic rename."""
        from backend.api.helpers import exchange_clock

        exchange_clock._CACHE = [self._default_row(dtime(23, 55))]
        saturday_post_midnight = datetime(2026, 11, 21, 0, 25, tzinfo=IST)

        with patch.object(exchange_clock, "_now_ist", return_value=saturday_post_midnight):
            today_anchored = exchange_clock._effective_gate_rows("MCX")
        assert today_anchored == []  # the bug: "today" is Saturday, no Mon-Fri row matches

        session_anchored = exchange_clock._effective_gate_rows_for_date("MCX", self.FRIDAY)
        assert len(session_anchored) == 1  # the fix: resolved directly against Friday

    async def test_unrelated_holiday_override_the_day_after_does_not_block_the_session(self):
        """A date-specific MCX holiday override row for the day AFTER
        the session (e.g. a long weekend) must never suppress resolution
        of the SESSION's own (Friday's) default row — overrides are
        matched by their own exact date, never leak across dates."""
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        exchange_clock._CACHE = [
            self._default_row(dtime(23, 30)),
            self._override_row(self.SATURDAY, None),  # Saturday itself irrelevant here
        ]
        now = datetime(2026, 11, 21, 0, 5, tzinfo=IST)  # Fri close(23:30)+30min = Sat 00:00
        today = now.date()
        done_ref = [None]

        with patch(
            "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill",
            AsyncMock(return_value={"accounts_backfilled": []}),
        ) as mock_run, \
             patch("backend.api.background._edb_marker_date", AsyncMock(return_value=None)), \
             patch("backend.api.background._edb_mark_done", AsyncMock()):
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)

        mock_run.assert_awaited_once()

    async def test_session_day_itself_an_mcx_holiday_override_does_not_fire(self):
        """When the SESSION's own date carries a holiday override
        (close_time=None — e.g. a date-specific Muhurat/holiday row),
        the resolved close_t is None and the tick must not fire at all,
        regardless of how far past midnight `now` is."""
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        exchange_clock._CACHE = [
            self._default_row(dtime(23, 30)),
            self._override_row(self.FRIDAY, None),  # Friday ITSELF is an MCX holiday
        ]
        now = datetime(2026, 11, 21, 0, 30, tzinfo=IST)  # well past any threshold
        today = now.date()
        done_ref = [None]

        with patch(
            "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill", AsyncMock(),
        ) as mock_run:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)

        mock_run.assert_not_awaited()

    async def test_normal_weekday_pre_midnight_resolves_rows_but_still_waits(self):
        """Non-regression for the `is_post_midnight=False` branch
        (`session_day == today`, no wrap in play yet): the reordering in
        this fix must not change the common pre-midnight case — rows
        still resolve correctly against `today` via the REAL
        `_effective_gate_rows_for_date`, and the tick still correctly
        waits (close+30min for a 23:30 close always lands the NEXT
        calendar day, so it can never fire before midnight)."""
        from backend.api.background import _ds_tick_expiry_devolve_backfill
        from backend.api.helpers import exchange_clock

        tuesday = date(2026, 11, 24)
        assert tuesday.weekday() == 1
        exchange_clock._CACHE = [self._default_row(dtime(23, 30))]
        now = datetime(2026, 11, 24, 23, 31, tzinfo=IST)  # same day, well past 23:30 (not +30 yet)
        today = now.date()
        done_ref = [None]

        with patch(
            "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill", AsyncMock(),
        ) as mock_run:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)

        # 23:31 is before close(23:30)+30min=00:00 the next day — must not fire yet.
        mock_run.assert_not_awaited()
        assert done_ref[0] is None


# ---------------------------------------------------------------------------
# _edb_marker_date / _edb_mark_done — the real settings-table round trip
# (same convention as `test_background_helpers.py`'s `_FakeMarkerStore` /
# `_patch_marker_store` for `_pcf_marker_date` / `_msr_marker_date`).
# ---------------------------------------------------------------------------


class _FakeMarkerStore:
    """In-memory stand-in for the `settings` table's get_string/set_string,
    scoped to a single test so no real DB is needed."""

    def __init__(self):
        self.values: dict = {}

    def get_string(self, key, default=""):
        return self.values.get(key, default)

    async def set_string(self, key, value, **kw):
        old = self.values.get(key)
        self.values[key] = value
        return old


def _patch_marker_store(store: "_FakeMarkerStore"):
    return (
        patch("backend.shared.helpers.settings.get_string", store.get_string),
        patch("backend.shared.helpers.settings.set_string", store.set_string),
    )


class TestEdbMarkerRoundTrip:
    async def test_marker_round_trip(self):
        from backend.api.background import _edb_marker_date, _edb_mark_done

        store = _FakeMarkerStore()
        p1, p2 = _patch_marker_store(store)
        with p1, p2:
            assert await _edb_marker_date() is None
            await _edb_mark_done(date(2026, 11, 27))
            assert await _edb_marker_date() == date(2026, 11, 27)

    async def test_marker_date_malformed_value_returns_none(self):
        """A corrupted/unparseable stored value must not raise — it is
        treated the same as 'no marker yet', failing toward re-running
        the backfill rather than crashing the tick."""
        from backend.api.background import _edb_marker_date

        store = _FakeMarkerStore()
        store.values["task_markers.expiry_devolve_backfill_date"] = "not-a-date"
        p1, p2 = _patch_marker_store(store)
        with p1, p2:
            assert await _edb_marker_date() is None


# ---------------------------------------------------------------------------
# _edb_query_mcx_positions_rows / _edb_existing_symbols_for_account — the
# real DB-read bodies (same `async_session` mocking convention as
# `test_daily_snapshot_orphan.py`'s `_make_session_mock`).
# ---------------------------------------------------------------------------


def _make_session_mock(all_return):
    result = MagicMock()
    result.all.return_value = all_return
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def _make_scalar_session_mock(scalar_return):
    """Same convention as `_make_session_mock` above, but for a query
    whose result is read via `.scalar()` (a `select(func.max(...))`)
    rather than `.all()`."""
    result = MagicMock()
    result.scalar.return_value = scalar_return
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


class TestEdbDbReadFunctions:
    async def test_query_mcx_positions_rows_real_body(self):
        from backend.api.algo import daily_snapshot as ds

        mock_session = _make_session_mock([
            ("ZG0790", _ZG_OPTION, -50),
            ("ZJ6294", _ZJ_OPTION, 25),
        ])
        with patch.object(ds, "async_session", return_value=mock_session):
            rows = await ds._edb_query_mcx_positions_rows(_TARGET_DATE)
        assert rows == [("ZG0790", _ZG_OPTION, -50), ("ZJ6294", _ZJ_OPTION, 25)]

    async def test_query_mcx_positions_rows_coerces_null_qty_to_zero(self):
        """A NULL `qty` column (should not happen in practice, but the
        model allows it) must coerce to 0, never raise."""
        from backend.api.algo import daily_snapshot as ds

        mock_session = _make_session_mock([("ZG0790", _ZG_OPTION, None)])
        with patch.object(ds, "async_session", return_value=mock_session):
            rows = await ds._edb_query_mcx_positions_rows(_TARGET_DATE)
        assert rows == [("ZG0790", _ZG_OPTION, 0)]

    async def test_existing_symbols_for_account_real_body(self):
        from backend.api.algo import daily_snapshot as ds

        mock_session = _make_session_mock([(_ZG_OPTION,), (_ZG_DEVOLVED_FUTURE,)])
        with patch.object(ds, "async_session", return_value=mock_session):
            existing = await ds._edb_existing_symbols_for_account(_TARGET_DATE, "ZG0790")
        assert existing == {_ZG_OPTION, _ZG_DEVOLVED_FUTURE}

    async def test_existing_symbols_for_account_empty(self):
        from backend.api.algo import daily_snapshot as ds

        mock_session = _make_session_mock([])
        with patch.object(ds, "async_session", return_value=mock_session):
            existing = await ds._edb_existing_symbols_for_account(_TARGET_DATE, "ZG0790")
        assert existing == set()

    async def test_existing_captured_at_for_account_real_body(self):
        """The real SELECT func.max(captured_at) body — defect-1 fix."""
        from backend.api.algo import daily_snapshot as ds

        stamp = datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc)
        mock_session = _make_scalar_session_mock(stamp)
        with patch.object(ds, "async_session", return_value=mock_session):
            result = await ds._edb_existing_captured_at_for_account(_TARGET_DATE, "ZG0790")
        assert result == stamp

    async def test_existing_captured_at_for_account_none_when_no_row(self):
        """No existing row for (target_date, account, kind='positions')
        → `None`, never a fabricated/defaulted value — the caller must
        treat this as 'do not write'."""
        from backend.api.algo import daily_snapshot as ds

        mock_session = _make_scalar_session_mock(None)
        with patch.object(ds, "async_session", return_value=mock_session):
            result = await ds._edb_existing_captured_at_for_account(_TARGET_DATE, "ZG0790")
        assert result is None


# ---------------------------------------------------------------------------
# _edb_existing_captured_at_for_account — Round 3 audit fix: the helper's
# own SQL filter must agree with `positions.py`'s `latest_batch` CTE /
# `_BASELINE_PNL_CTE_SQL` (`ltp IS NOT NULL AND ltp > 0`). The tests above
# mock the session entirely, so they never exercise the real SQL WHERE
# clause — these use a REAL (sqlite, async) session instead, same
# StaticPool-backed in-memory-DB convention as
# `test_snapshot_market_open.py:db_session` / `test_daily_snapshot.py:db_session`.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _edb_real_session_factory():
    """A real async sqlite session factory sharing ONE in-memory DB
    (StaticPool) across every session it opens — required because the
    function under test opens its OWN session via `async_session()`
    independently of whatever session the test used to seed rows."""
    from sqlalchemy import Column, Date, DateTime, Integer, MetaData, Numeric, String, Table, Text, UniqueConstraint
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    meta = MetaData()
    Table(
        "daily_book", meta,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("date", Date, nullable=False),
        Column("account", String(32), nullable=False),
        Column("segment", String(16), nullable=False),
        Column("kind", String(16), nullable=False),
        Column("symbol", String(64), nullable=False),
        Column("exchange", String(8), nullable=True),
        Column("qty", Integer, nullable=False, default=0),
        Column("lots", Integer, nullable=False, default=1),
        Column("lot_size", Integer, nullable=False, default=1),
        Column("avg_cost", Numeric, nullable=True),
        Column("ltp", Numeric, nullable=True),
        Column("day_pnl", Numeric, nullable=True),
        Column("total_pnl", Numeric, nullable=True),
        Column("payload_json", Text, nullable=True),
        Column("captured_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint("date", "account", "kind", "symbol",
                         name="uq_daily_book_day_acct_kind_sym"),
    )

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _edb_seed_positions_row(session_factory, *, account, symbol, ltp, captured_at,
                                   target_date=_TARGET_DATE):
    """Insert one `kind='positions'` daily_book row via raw SQL, bound
    against the same real `DailyBook.date` (a `date` object, matching the
    ORM column's `Date` type) that `_edb_existing_captured_at_for_account`
    itself filters on."""
    from sqlalchemy import text as _sql_text

    async with session_factory() as session:
        await session.execute(
            _sql_text(
                """
                INSERT INTO daily_book
                    (date, account, segment, kind, symbol, exchange, qty,
                     lots, lot_size, avg_cost, ltp, day_pnl, total_pnl,
                     payload_json, captured_at)
                VALUES
                    (:date, :account, 'commodity', 'positions', :symbol,
                     'MCX', 1, 1, 1, 70000.0, :ltp, 0, 0, '{}', :captured_at)
                """
            ),
            {
                "date": target_date,
                "account": account,
                "symbol": symbol,
                "ltp": ltp,
                "captured_at": captured_at,
            },
        )
        await session.commit()


class TestEdbExistingCapturedAtRealSqlNullLtpFiltered:
    """Round 3 audit finding: without the `ltp IS NOT NULL AND ltp > 0`
    filter, a newer NULL-ltp daily_book batch (e.g. a mid-session EOD
    write that correctly nulled ltp/day_pnl because the market was still
    open — the 2026-11-26 `market_lifecycle.py` DST incident) would win
    `MAX(captured_at)` over an older VALID (ltp>0) batch, promoting the
    null batch to 'latest' in `positions.py`'s own eyes."""

    async def test_newer_null_ltp_batch_does_not_win_over_older_valid_batch(
        self, _edb_real_session_factory,
    ):
        from backend.api.algo import daily_snapshot as ds

        older_valid_at = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
        newer_null_at = datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc)

        await _edb_seed_positions_row(
            _edb_real_session_factory, account="ZG0790", symbol=_ZG_OPTION,
            ltp=70000.0, captured_at=older_valid_at,
        )
        await _edb_seed_positions_row(
            _edb_real_session_factory, account="ZG0790", symbol=_ZG_DEVOLVED_FUTURE,
            ltp=None, captured_at=newer_null_at,
        )

        with patch.object(ds, "async_session", _edb_real_session_factory):
            result = await ds._edb_existing_captured_at_for_account(_TARGET_DATE, "ZG0790")

        assert result is not None
        # sqlite round-trips DateTime(timezone=True) as naive — compare
        # on naive wall-clock value.
        assert result.replace(tzinfo=None) == older_valid_at.replace(tzinfo=None)

    async def test_newer_zero_ltp_batch_also_does_not_win(self, _edb_real_session_factory):
        """`ltp == 0` must be excluded too — the filter is `> 0`, not
        merely `isnot(None)`."""
        from backend.api.algo import daily_snapshot as ds

        older_valid_at = datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc)
        newer_zero_at = datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc)

        await _edb_seed_positions_row(
            _edb_real_session_factory, account="ZG0790", symbol=_ZG_OPTION,
            ltp=70000.0, captured_at=older_valid_at,
        )
        await _edb_seed_positions_row(
            _edb_real_session_factory, account="ZG0790", symbol=_ZG_DEVOLVED_FUTURE,
            ltp=0.0, captured_at=newer_zero_at,
        )

        with patch.object(ds, "async_session", _edb_real_session_factory):
            result = await ds._edb_existing_captured_at_for_account(_TARGET_DATE, "ZG0790")

        assert result is not None
        assert result.replace(tzinfo=None) == older_valid_at.replace(tzinfo=None)

    async def test_all_rows_null_ltp_returns_none_not_a_fabricated_stamp(
        self, _edb_real_session_factory,
    ):
        """When every positions row for this account+date is null/zero
        ltp, there is no valid batch to join — the helper must return
        `None` (caller already treats this as 'do not write' and logs a
        visible WARNING), never fabricate a stamp from the null batch."""
        from backend.api.algo import daily_snapshot as ds

        await _edb_seed_positions_row(
            _edb_real_session_factory, account="ZG0790", symbol=_ZG_DEVOLVED_FUTURE,
            ltp=None, captured_at=datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc),
        )

        with patch.object(ds, "async_session", _edb_real_session_factory):
            result = await ds._edb_existing_captured_at_for_account(_TARGET_DATE, "ZG0790")

        assert result is None

    async def test_real_valid_batch_still_resolves_when_it_is_the_only_row(
        self, _edb_real_session_factory,
    ):
        """Non-regression: a single, ordinary valid batch (the common
        case — no null-out row at all) must still resolve correctly
        through the real SQL path once the filter is applied."""
        from backend.api.algo import daily_snapshot as ds

        valid_at = datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
        await _edb_seed_positions_row(
            _edb_real_session_factory, account="ZG0790", symbol=_ZG_OPTION,
            ltp=70000.0, captured_at=valid_at,
        )

        with patch.object(ds, "async_session", _edb_real_session_factory):
            result = await ds._edb_existing_captured_at_for_account(_TARGET_DATE, "ZG0790")

        assert result is not None
        assert result.replace(tzinfo=None) == valid_at.replace(tzinfo=None)


class TestRunExpiryDevolveBackfillUpsertFailure:
    """The `_upsert_rows` write itself failing for one account must be
    tolerated (logged, skipped) — never fatal to the whole backfill pass,
    and never counted as a successful backfill for that account."""

    async def test_upsert_failure_is_tolerated_account_not_marked_backfilled(self):
        from backend.api.algo import daily_snapshot as ds

        candidate_rows = [("ZG0790", _ZG_OPTION, -50)]
        raw_positions = [
            {"tradingsymbol": _ZG_OPTION, "exchange": "MCX", "quantity": 0,
             "average_price": 0.0, "last_price": 0.0, "pnl": -1200.0},
            {"tradingsymbol": _ZG_DEVOLVED_FUTURE, "exchange": "MCX", "quantity": -1,
             "average_price": 68000.0, "last_price": 68200.0, "pnl": -200.0,
             "close_price": 68000.0},
        ]
        broker = _fake_broker("ZG0790")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(
            ds, "_edb_existing_symbols_for_account", AsyncMock(return_value={_ZG_OPTION}),
        ), patch.object(
            # Resolve a real captured_at so the exercised failure is
            # genuinely the `_upsert_rows` write itself, not an
            # incidental fall-through from `captured_at is None`.
            ds, "_edb_existing_captured_at_for_account",
            AsyncMock(return_value=datetime(2026, 11, 27, 18, 15, tzinfo=timezone.utc)),
        ), patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch.object(
                 ds, "_fetch_account_data",
                 return_value={"holdings": [], "positions": raw_positions, "trades": [], "funds": []},
             ), \
             patch.object(ds, "_upsert_rows", AsyncMock(side_effect=RuntimeError("DB write failed"))), \
             patch.object(ds, "_snap_broadcast_book_changed") as mock_broadcast:
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        assert result["accounts_checked"] == ["ZG0790"]
        assert result["accounts_backfilled"] == []
        assert result["symbols_backfilled"] == {}
        mock_broadcast.assert_not_called()

    async def test_missing_existing_captured_at_skips_write_instead_of_defaulting_to_now(self):
        """When no existing EOD batch captured_at can be resolved for the
        account (should not normally happen — see
        `_edb_existing_captured_at_for_account`'s own docstring — but
        must fail SAFE), the write is skipped entirely rather than
        falling back to `_upsert_rows`'s own `now()` default, which
        would risk corrupting the account's latest-batch anchor."""
        from backend.api.algo import daily_snapshot as ds

        candidate_rows = [("ZG0790", _ZG_OPTION, -50)]
        raw_positions = [
            {"tradingsymbol": _ZG_OPTION, "exchange": "MCX", "quantity": 0,
             "average_price": 0.0, "last_price": 0.0, "pnl": -1200.0},
            {"tradingsymbol": _ZG_DEVOLVED_FUTURE, "exchange": "MCX", "quantity": -1,
             "average_price": 68000.0, "last_price": 68200.0, "pnl": -200.0,
             "close_price": 68000.0},
        ]
        broker = _fake_broker("ZG0790")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(
            ds, "_edb_existing_symbols_for_account", AsyncMock(return_value={_ZG_OPTION}),
        ), patch.object(
            ds, "_edb_existing_captured_at_for_account", AsyncMock(return_value=None),
        ), patch("backend.brokers.registry.all_brokers", return_value=[broker]), \
             patch.object(
                 ds, "_fetch_account_data",
                 return_value={"holdings": [], "positions": raw_positions, "trades": [], "funds": []},
             ), \
             patch.object(ds, "_upsert_rows", AsyncMock()) as mock_upsert, \
             patch.object(ds, "_snap_broadcast_book_changed") as mock_broadcast:
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        mock_upsert.assert_not_called()
        assert result["accounts_backfilled"] == []
        mock_broadcast.assert_not_called()


class TestRunExpiryDevolveBackfillTargetDateDefault:
    """`target_date=None` (the real caller's own default, mirrors
    `snapshot_daily_book()`'s own session-boundary convention) resolves
    via `expiry_freeze.session_date_of(now_ist)`, not a bare `now.date()`
    — this task fires well after MCX close, which can cross IST
    midnight."""

    async def test_default_target_date_resolves_via_session_date_of(self):
        from backend.api.algo import daily_snapshot as ds

        resolved_date = date(2026, 11, 27)
        with patch.object(ds, "timestamp_indian", return_value=datetime(2026, 11, 28, 0, 30, tzinfo=IST)), \
             patch("backend.api.algo.expiry_freeze.session_date_of", return_value=resolved_date) as mock_sdo, \
             patch.object(ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=[])), \
             patch("backend.brokers.registry.all_brokers") as mock_all_brokers:
            result = await ds.run_expiry_devolve_backfill(target_date=None)

        mock_sdo.assert_called_once()
        assert result["target_date"] == resolved_date
        mock_all_brokers.assert_not_called()


class TestRunExpiryDevolveBackfillNonCandidateBrokerSkipped:
    """A loaded broker whose account is NOT in the candidate set must be
    skipped entirely — never counted in `accounts_checked`, never
    fetched."""

    async def test_non_candidate_account_is_skipped(self):
        from backend.api.algo import daily_snapshot as ds

        candidate_rows = [("ZG0790", _ZG_OPTION, -50)]
        broker_candidate = _fake_broker("ZG0790")
        broker_other = _fake_broker("SOMEOTHER")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(
            ds, "_edb_existing_symbols_for_account", AsyncMock(return_value={_ZG_OPTION, _ZG_DEVOLVED_FUTURE}),
        ), patch("backend.brokers.registry.all_brokers", return_value=[broker_other, broker_candidate]), \
             patch.object(
                 ds, "_fetch_account_data",
                 return_value={"holdings": [], "positions": [], "trades": [], "funds": []},
             ) as mock_fetch:
            result = await ds.run_expiry_devolve_backfill(target_date=_TARGET_DATE)

        assert result["accounts_checked"] == ["ZG0790"]
        # _fetch_account_data must only ever be called for the candidate
        # account, never for "SOMEOTHER".
        mock_fetch.assert_called_once()
        assert mock_fetch.call_args[0][1] == "ZG0790"
