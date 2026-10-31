"""
Phase 0 (2026-10-31) — two contained sim-engine bug fixes, per
`.claude/PLAN.md`:

1. `simulator.block_during_market_hours`'s seeded default flipped
   True -> False. SimDriver's own agent-evaluation context
   (`market_state` in `sim/driver.py`) already fully ignores real
   wall-clock market status, so hard-blocking a sim START while a real
   exchange is open was backwards for a backtesting/scenario tool.
   The gate mechanism itself is untouched — an operator who wants it
   can still turn it back on via /admin/settings.

2. `SimDriver._internal_stop()` used to log the count of still-open
   paper orders at stop time but never wrote a terminal status to
   them — the owning `PaperTradeEngine` is discarded with those rows
   left dangling at `status='OPEN', mode='sim'` forever. Fixed via
   `_sd_finalize_stale_open_orders()`, which reuses
   `PaperTradeEngine.cancel_paper_order(..., source="sim_stop")` — the
   exact mechanism the operator Kill button and the MCP cancel tool
   already use.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time as dtime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")

# A fixture time where MCX genuinely reads OPEN under the real gate logic
# (plain weekday, inside the 09:00-23:30 commodity session, no holiday).
_MARKET_OPEN_NOW = datetime(2026, 10, 19, 18, 0, tzinfo=IST)  # Monday


def _fake_holidays(exchange: str) -> set:
    return set()


def _fake_special_sessions(exchange: str) -> list:
    return []


# ---------------------------------------------------------------------------
# Fix 1 — simulator.block_during_market_hours default flipped True -> False
# ---------------------------------------------------------------------------

def test_seed_default_is_false():
    """The SEEDS tuple — the actual seeded default, not just a read-site
    fallback — must ship False. Existing/fresh installs both read this
    through `_upsert_seeds` / `_flip_sim_block_default_once`."""
    from backend.shared.helpers.settings import SEEDS

    row = next(s for s in SEEDS if s[1] == "simulator.block_during_market_hours")
    assert row[2] == "bool"
    assert row[3] is False, (
        "simulator.block_during_market_hours must seed False by default "
        "(2026-10-31) — blocking sim starts during real market hours is "
        "backwards for a tool whose own engine already ignores real "
        "wall-clock market status"
    )


def test_read_site_fallbacks_no_longer_default_true():
    """Both get_bool call sites in routes/simulator.py must pass False as
    their own in-code fallback, matching the new seeded default — not just
    the seed tuple changing while a stale read-site literal silently keeps
    the old behaviour whenever the DB/YAML lookup misses."""
    import inspect
    from backend.api.routes import simulator as sim

    src = inspect.getsource(sim)
    assert 'get_bool("simulator.block_during_market_hours", True)' not in src
    assert src.count('get_bool("simulator.block_during_market_hours", False)') == 2


def test_sim_start_not_blocked_by_default_during_real_market_hours():
    """Starting a sim while a real segment is open must NOT raise 409 by
    default — no explicit DB/YAML override present, so the call falls
    through to the (now False) in-code fallback. This is the actual
    behaviour change, not just a settings-table fixture."""
    from backend.api.routes import simulator as sim
    from backend.shared.helpers import settings
    from backend.api import background as bg

    # Simulate "no seeded row yet" / "not explicitly overridden" — the
    # in-process cache has no entry for this key, and there is no YAML
    # fallback for it either (confirmed: not present in backend_config.yaml).
    # patch.dict snapshots + restores the whole _CACHE dict so popping this
    # one key here can't leak into any other test that runs in the same
    # process.
    with patch.dict(settings._CACHE, {}, clear=False):
        settings._CACHE.pop("simulator.block_during_market_hours", None)

        with patch.object(bg, "_get_segments", return_value=[
                {"name": "MCX", "hours_start": dtime(9, 0), "hours_end": dtime(23, 30),
                 "holiday_exchange": "MCX"},
             ]), \
             patch("backend.brokers.broker_apis.fetch_holidays", side_effect=_fake_holidays), \
             patch("backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_special_sessions), \
             patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_MARKET_OPEN_NOW):
            # Precondition: the market really does read open at this fixture,
            # otherwise this test would pass for the wrong reason (segment
            # closed, not "gate defaults off").
            assert sim._markets_currently_open() is True
            # The real assertion: no HTTPException(409) raised by default.
            sim._market_hours_block_check()  # must not raise


def test_sim_start_can_still_be_blocked_when_operator_opts_in():
    """The gate mechanism itself is NOT removed — an operator who
    explicitly flips the setting back on must still get the 409."""
    from backend.api.routes import simulator as sim
    from backend.api import background as bg
    from litestar.exceptions import HTTPException

    with patch("backend.shared.helpers.settings.get_bool", return_value=True), \
         patch.object(bg, "_get_segments", return_value=[
             {"name": "MCX", "hours_start": dtime(9, 0), "hours_end": dtime(23, 30),
              "holiday_exchange": "MCX"},
         ]), \
         patch("backend.brokers.broker_apis.fetch_holidays", side_effect=_fake_holidays), \
         patch("backend.brokers.broker_apis.fetch_special_sessions", side_effect=_fake_special_sessions), \
         patch("backend.shared.helpers.date_time_utils.timestamp_indian", return_value=_MARKET_OPEN_NOW):
        with pytest.raises(HTTPException) as exc_info:
            sim._market_hours_block_check()
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# Fix 1b — the one-shot DB migration that flips an ALREADY-SEEDED row's
# `value`, since `_upsert_seeds` never touches `value` on an existing row.
# ---------------------------------------------------------------------------

class _FakeSettingRow:
    def __init__(self, value: str):
        self.value = value


def test_flip_sim_block_default_once_flips_existing_true_value():
    from backend.shared.helpers import settings

    session = MagicMock()
    row = _FakeSettingRow("true")
    existing_by_key = {"simulator.block_during_market_hours": row}

    flipped = settings._flip_sim_block_default_once(session, existing_by_key)

    assert flipped is True
    assert row.value == "false", (
        "an existing install's untouched auto-seeded row (still at the "
        "old default 'true') must be flipped by this one-shot migration"
    )
    session.add.assert_called_once()
    added_row = session.add.call_args.args[0]
    assert added_row.key == settings._SIM_BLOCK_DEFAULT_FLIP_MARKER
    assert added_row.category == "task_markers"


def test_flip_sim_block_default_once_is_noop_when_marker_already_present():
    from backend.shared.helpers import settings

    session = MagicMock()
    row = _FakeSettingRow("true")
    existing_by_key = {
        "simulator.block_during_market_hours": row,
        settings._SIM_BLOCK_DEFAULT_FLIP_MARKER: MagicMock(),
    }

    flipped = settings._flip_sim_block_default_once(session, existing_by_key)

    assert flipped is False
    assert row.value == "true", "already-flipped-once database must never re-flip"
    session.add.assert_not_called()


def test_flip_sim_block_default_once_leaves_non_true_value_untouched():
    """An operator who already set it to 'false' (or any non-'true'
    value) keeps that exact value — only a bare 'true' gets flipped."""
    from backend.shared.helpers import settings

    session = MagicMock()
    row = _FakeSettingRow("false")
    existing_by_key = {"simulator.block_during_market_hours": row}

    flipped = settings._flip_sim_block_default_once(session, existing_by_key)

    assert flipped is True  # marker row still gets written exactly once
    assert row.value == "false"


def test_flip_sim_block_default_once_handles_missing_row_gracefully():
    """A database with no simulator.block_during_market_hours row at all
    (fresh install, inserted by _upsert_seeds in the SAME seed_settings()
    call, so not yet in `existing_by_key`) must not raise."""
    from backend.shared.helpers import settings

    session = MagicMock()
    existing_by_key: dict = {}

    flipped = settings._flip_sim_block_default_once(session, existing_by_key)

    assert flipped is True
    session.add.assert_called_once()


# ---------------------------------------------------------------------------
# Fix 2 — SimDriver._internal_stop() must finalize stale OPEN paper orders
# ---------------------------------------------------------------------------

def _make_stale_sim_order(order_id: int = 4242) -> dict:
    return {
        "algo_order_id": order_id,
        "account": "ZG0790",
        "symbol": "NIFTY25JULFUT",
        "side": "BUY",
        "qty": 50,
        "limit_price": 100.0,
        "exchange": "NFO",
        "status": "OPEN",
        "attempts": 1,
    }


def _patch_db_session(row: MagicMock):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row))
    )
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=mock_session)


@pytest.fixture
def sim_driver_clean():
    """SimDriver() always returns the process-wide singleton (`__new__`
    reuses `cls._instance`; `__init__` no-ops once `_initialized` is set)
    — there is no "fresh instance" for direct construction, matching
    `test_sim_recording.py`'s own documented caveat. Save/restore the
    handful of fields this test touches so no state leaks into other
    tests that also reach the singleton."""
    from backend.api.algo.sim.driver import SimDriver

    d = SimDriver()
    saved = {
        "active": d.active,
        "_run_active": d._run_active,
        "_scheduler_task": d._scheduler_task,
        "_task": d._task,
        "_recording_active": d._recording_active,
        "iteration_end_reason": d.iteration_end_reason,
        "open_orders": list(d._paper._open_orders),
    }
    try:
        yield d
    finally:
        d.active = saved["active"]
        d._run_active = saved["_run_active"]
        d._scheduler_task = saved["_scheduler_task"]
        d._task = saved["_task"]
        d._recording_active = saved["_recording_active"]
        d.iteration_end_reason = saved["iteration_end_reason"]
        d._paper._open_orders = saved["open_orders"]


@pytest.mark.asyncio
async def test_stop_finalizes_stale_open_sim_order_to_cancelled(sim_driver_clean):
    """The core regression: a sim order left OPEN when stop() runs must
    get a real terminal status, not be abandoned forever. Pre-fix, this
    assertion fails — row.status stays 'OPEN' because _internal_stop
    never called cancel_paper_order on it."""
    d = sim_driver_clean
    d.active = True
    d._run_active = False
    d._scheduler_task = None
    d._recording_active = False

    order = _make_stale_sim_order(4242)
    d._paper._open_orders = [dict(order)]

    row = MagicMock()
    row.id = order["algo_order_id"]
    row.status = "OPEN"
    row.attempts = 0
    row.detail = ""

    with (
        patch("backend.api.database.async_session", _patch_db_session(row)),
        patch("backend.api.algo.order_events.write_event", AsyncMock()),
        patch("backend.api.routes.orders._postback_broadcast_fanout", MagicMock()),
        patch("backend.shared.helpers.utils.mask_account", return_value="ZG####"),
    ):
        d.stop()
        # cancel_paper_order's DB write is fire-and-forget via
        # asyncio.create_task — drain the engine's own pending-update
        # bookkeeping set (same mechanism paper.py itself tracks) before
        # asserting on the row.
        pending = list(d._paper._pending_updates)
        if pending:
            await asyncio.gather(*pending)

    assert row.status == "CANCELLED", (
        "a sim order still OPEN when the sim session stops must not be "
        "abandoned forever at status='OPEN'"
    )
    assert "sim session" in row.detail.lower()


@pytest.mark.asyncio
async def test_stop_is_a_noop_when_no_open_orders(sim_driver_clean):
    """No open orders at stop time — no DB calls, no crash."""
    d = sim_driver_clean
    d.active = True
    d._run_active = False
    d._scheduler_task = None
    d._recording_active = False
    d._paper._open_orders = []

    with patch("backend.api.database.async_session") as mock_async_session:
        d.stop()
        await asyncio.sleep(0.01)
        mock_async_session.assert_not_called()


@pytest.mark.asyncio
async def test_stop_finalizes_multiple_stale_open_orders(sim_driver_clean):
    """More than one abandoned order at stop time — every one of them
    gets finalized, not just the first."""
    d = sim_driver_clean
    d.active = True
    d._run_active = False
    d._scheduler_task = None
    d._recording_active = False

    orders = [_make_stale_sim_order(100), _make_stale_sim_order(101)]
    d._paper._open_orders = [dict(o) for o in orders]

    rows = {}
    for o in orders:
        r = MagicMock()
        r.id = o["algo_order_id"]
        r.status = "OPEN"
        r.attempts = 0
        r.detail = ""
        rows[o["algo_order_id"]] = r

    async def _fake_safe_update_cancel(order_dict, *, source="operator"):
        rows[order_dict["algo_order_id"]].status = "CANCELLED"

    with patch.object(d._paper, "_safe_update_algo_order_cancel", _fake_safe_update_cancel):
        d.stop()
        pending = list(d._paper._pending_updates)
        if pending:
            await asyncio.gather(*pending)

    assert all(r.status == "CANCELLED" for r in rows.values())


def test_sim_stop_finalize_reuses_cancel_paper_order_mechanism():
    """Source-level guard — `_sd_finalize_stale_open_orders` must call
    the EXISTING `cancel_paper_order` mechanism (same path the operator
    Kill button and the MCP cancel tool use), not a new independent
    AlgoOrder-status-transition implementation."""
    import inspect
    from backend.api.algo.sim.driver import SimDriver

    src = inspect.getsource(SimDriver._sd_finalize_stale_open_orders)
    assert "cancel_paper_order" in src
    assert 'source="sim_stop"' in src


def test_internal_stop_calls_finalize_before_recording_count():
    """Source-level guard — _internal_stop must capture the open-order
    count via the finalize helper's return value, not a second
    open_order_details() call made AFTER the cancel loop (which would
    always read back 0)."""
    import inspect
    from backend.api.algo.sim.driver import SimDriver

    src = inspect.getsource(SimDriver._internal_stop)
    assert "_sd_finalize_stale_open_orders" in src
    assert "stale_open_orders" in src
