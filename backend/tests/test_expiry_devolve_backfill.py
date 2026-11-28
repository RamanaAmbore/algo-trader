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
from datetime import date, datetime, time as dtime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

        async def _fake_upsert(rows):
            upserted.extend(rows)
            return len(rows)

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

        upserted: list = []

        async def _fake_upsert(rows):
            upserted.extend(rows)
            return len(rows)

        broker_zg = _fake_broker("ZG0790")
        broker_zj = _fake_broker("ZJ6294")

        with patch.object(
            ds, "_edb_query_mcx_positions_rows", AsyncMock(return_value=candidate_rows),
        ), patch.object(ds, "_edb_existing_symbols_for_account", _fake_existing), \
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
        with patch.object(exchange_clock, "_effective_gate_rows", return_value=[_mcx_row()]), \
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
        with patch.object(exchange_clock, "_effective_gate_rows", return_value=[_mcx_row()]), \
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
        with patch.object(exchange_clock, "_effective_gate_rows", return_value=[]), \
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
        with patch.object(exchange_clock, "_effective_gate_rows", return_value=[_mcx_row()]), \
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
        with patch.object(exchange_clock, "_effective_gate_rows", return_value=[_mcx_row()]), \
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
        with patch.object(exchange_clock, "_effective_gate_rows", return_value=[_mcx_row()]), \
             patch(
                 "backend.api.algo.daily_snapshot.run_expiry_devolve_backfill",
                 AsyncMock(side_effect=RuntimeError("boom")),
             ), patch("backend.api.background._edb_marker_date", AsyncMock(return_value=None)), \
             patch("backend.api.background._edb_mark_done", AsyncMock()) as mock_mark_done:
            await _ds_tick_expiry_devolve_backfill(now, today, done_ref)
        assert done_ref[0] == fire_date
        mock_mark_done.assert_awaited_once_with(fire_date)


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
