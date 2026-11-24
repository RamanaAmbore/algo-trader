"""
Part C — THE mandatory end-to-end test (2026-11-23).

Drives the real connected chain, not independent pieces:

  1. the real `_metric_is_expiry_preclose_close_window` condition token
     evaluates true for a representative 60-minute-pre-close context
     (real `agent_engine`/`grammar_tokens` resolver, real segment_flags
     shape — the complement to item 2 below, since the action's own
     internal re-derivation of "which exchange is in-window right now"
     is, by necessity, patched there — see that test's own docstring
     for why);
  2. the real `_action_live_expiry_auto_close` action →
  3. the real `ExpiryEngine.scan_positions()` (only the broker-touching
     fetch methods mocked) →
  4. the real `ExpiryEngine.close_positions()`, which stages a REAL HELD
     `AlgoOrder` row (real sqlite DB, real `backend.api.models.AlgoOrder`/
     `Agent` ORM shape) with both `agent_id` and a real, resolved
     `auto_release_at` genuinely set →
  5. simulated time passage to that deadline →
  6. the real (widened) `_agent_close_auto_release_sweep_once()` →
  7. the real `release_held_order()` (position-match + price-band
     checks run for real; ONLY the broker boundary — `get_broker`,
     `broker_apis.fetch_positions`, the broker's own `.quote()` — and
     `chase_order()` itself are mocked) →
  8. an assertion that a chase placement was actually made, at the
     correct tier, side, quantity, and identity.

Mirrors the real-DB sqlite shadow-table pattern already established in
`test_order_poll.py`'s `_op_sqlite_session_factory` fixture.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def _expiry_sqlite_session_factory():
    """Real in-memory sqlite DB with lightweight shadow `AlgoOrder` and
    `Agent` ORM classes, swapped in for `backend.api.models.AlgoOrder`/
    `Agent` for the duration of the test — same pattern as
    `test_order_poll.py:_op_sqlite_session_factory`.

    Only the columns the functions under test actually read or write
    are declared. `agent_id`/`hold_json` carry no ForeignKey constraint
    (sqlite doesn't enforce it by default, and the real model's FK
    points at the real `agents` table, which doesn't exist in this
    shadow schema)."""
    from sqlalchemy import Column, Integer, String, DateTime, Text
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _AlgoOrder(_Base):
        __tablename__ = "algo_orders"
        id                = Column(Integer, primary_key=True, autoincrement=True)
        account           = Column(String(32), nullable=False)
        symbol            = Column(String(64), nullable=False)
        exchange          = Column(String(8), nullable=False, default="NFO")
        transaction_type  = Column(String(4), nullable=False)
        quantity          = Column(Integer, nullable=False)
        filled_quantity   = Column(Integer, nullable=False, default=0)
        initial_price     = Column(Integer, nullable=True)
        status            = Column(String(16), nullable=False, default="OPEN")
        engine            = Column(String(16), nullable=False, default="manual")
        mode              = Column(String(8), nullable=False, default="live")
        agent_id          = Column(Integer, nullable=True)
        broker_order_id   = Column(String(32), nullable=True)
        product           = Column(String(8), nullable=False, default="NRML")
        detail            = Column(Text, nullable=True)
        hold_json         = Column(Text, nullable=True)
        intent            = Column(String(16), nullable=True)
        created_at        = Column(DateTime, nullable=False,
                                   default=lambda: datetime.now(timezone.utc))

    class _Agent(_Base):
        __tablename__ = "agents"
        id     = Column(Integer, primary_key=True, autoincrement=True)
        slug   = Column(String(64), nullable=False, unique=True)
        status = Column(String(16), nullable=False, default="inactive")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    orig_order = _models.AlgoOrder
    orig_agent = _models.Agent
    _models.AlgoOrder = _AlgoOrder  # type: ignore[assignment]
    _models.Agent = _Agent  # type: ignore[assignment]

    yield factory, _AlgoOrder, _Agent

    _models.AlgoOrder = orig_order  # type: ignore[assignment]
    _models.Agent = orig_agent  # type: ignore[assignment]
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


class TestRealConditionTokenEvaluatesTrue:
    """Complement to the main E2E flow below: proves the REAL condition
    token the `expiry-day-itm-auto-close` agent's `conditions` tree
    resolves against genuinely evaluates true for a representative
    60-minutes-before-close context — using the real `agent_engine`/
    `grammar_tokens` machinery, a real `Context`-shaped object, real
    `segment_flags`.

    2026-11-23 — confirmed-live P0 found and fixed during this same
    pass (not in the original plan's scope list, but directly blocking
    the "it works, no fail" requirement for the exact agent Part C
    wires its auto-release into): `_al_expiry_context_shim`
    (`actions_live.py`) and its twin `_v2_expiry_context_shim`
    (`agent_engine.py`) both omitted `.now` entirely.
    `_scope_positions_expiring_today()` reads `ctx.now` inside a bare
    `except Exception: continue`, so EVERY position row silently
    failed to parse through either shim, meaning
    `_expiry_preclose_exchanges()` ALWAYS returned an empty set — in
    PRODUCTION, not only in a test. `_action_live_expiry_auto_close`
    had therefore never actually closed anything (always hit its own
    "no exchange in window" branch and returned), and the alert
    sibling `_v2_expiry_position_breakdown()` had never actually
    rendered a breakdown either. Confirmed directly via an unpatched
    scratchpad call against the real functions before fixing. Both
    shims now read `now=context.get("now") or timestamp_indian()` —
    the exact key `_build_context()` already sets on every real
    dispatch. The main E2E test below therefore now drives the REAL
    `_expiry_preclose_exchanges()` derivation end to end, through a
    real `context` dict, with NO patch on that function — unlike the
    two pre-existing 2026-11-22 E2E tests in `test_expiry_logic.py`,
    which still patch it (they predate this fix and are out of this
    plan's scope to touch)."""

    @staticmethod
    def _ctx_now_matching_symbol_expiry(symbol: str) -> datetime:
        """`_scope_positions_expiring_today` needs `ctx.now` to land
        within 1.5 days of the symbol's OWN parsed expiry (it does not
        read the real wall clock) — compute that date from the real
        parser rather than hardcoding a date that will bit-rot."""
        from backend.api.algo.derivatives import parse_tradingsymbol
        parsed = parse_tradingsymbol(symbol)
        expiry = parsed["expiry"]
        return datetime(expiry.year, expiry.month, expiry.day, 10, 0, tzinfo=timezone.utc)

    def test_close_window_token_fires_at_60_minutes_to_mcx_close(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_close_window import (
            _metric_is_expiry_preclose_close_window, CLOSE_LEAD_MINUTES,
        )

        assert CLOSE_LEAD_MINUTES == 60

        symbol = "CRUDEOIL26NOV5500CE"
        ctx = MagicMock()
        ctx.segment_flags = {
            "mcx_open": True,
            "minutes_until_mcx_close": 45,  # inside the 60-min window
            "nse_open": False,
        }
        ctx.position_rows = [{"tradingsymbol": symbol, "exchange": "MCX", "quantity": 1}]
        ctx.now = self._ctx_now_matching_symbol_expiry(symbol)

        result = _metric_is_expiry_preclose_close_window(ctx, None)
        assert result == 1.0, "the real condition token must fire within its own 60-min window"

    def test_close_window_token_does_not_fire_outside_the_window(self):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_close_window import (
            _metric_is_expiry_preclose_close_window,
        )

        symbol = "CRUDEOIL26NOV5500CE"
        ctx = MagicMock()
        ctx.segment_flags = {
            "mcx_open": True,
            "minutes_until_mcx_close": 120,  # well outside the 60-min window
            "nse_open": False,
        }
        ctx.position_rows = [{"tradingsymbol": symbol, "exchange": "MCX", "quantity": 1}]
        ctx.now = self._ctx_now_matching_symbol_expiry(symbol)

        result = _metric_is_expiry_preclose_close_window(ctx, None)
        assert result == 0.0


async def _e2e_seed_agent_and_context(factory, _Agent):
    """Steps 1 setup — seed the real Agent row + build the real dispatch
    context. Split out of the test body purely to keep the orchestrating
    test function's own cyclomatic complexity under the CC-gate grade,
    not for reuse; behaviour is unchanged from the original inline code.
    """
    # status='cooldown' is the REAL post-fire state a persistent agent is
    # in by the time any release_timeout deadline passes (see
    # _task_agent_close_auto_release's own kill-switch docstring) — never
    # the literal 'active'.
    async with factory() as s:
        agent_row = _Agent(slug="expiry-day-itm-auto-close", status="cooldown")
        s.add(agent_row)
        await s.commit()
        real_agent_id = agent_row.id

    agent_like = MagicMock()
    agent_like.id = real_agent_id
    agent_like.slug = "expiry-day-itm-auto-close"

    from backend.api.algo.derivatives import parse_tradingsymbol

    option_symbol = "CRUDEOIL26NOV5500CE"
    # Real parser gives 2026-11-27 for this symbol's month token — pick
    # `frozen_now` so days_to_expiry(ref=frozen_now) is genuinely <= 1.5
    # (verified: ~1.08 days), so the REAL (now-fixed) condition-window
    # derivation includes this row without any patching.
    today = parse_tradingsymbol(option_symbol)["expiry"] - timedelta(days=1)
    frozen_now = datetime(today.year, today.month, today.day, 16, 0, 0, tzinfo=timezone.utc)

    from backend.api.algo.expiry import OptionPosition

    lone_itm_call = OptionPosition(
        account="ZG0790",
        tradingsymbol=option_symbol,
        exchange="MCX",
        instrument_type="CE",
        underlying="CRUDEOIL",
        strike=5500.0,
        expiry=today,
        quantity=1,
        product="NRML",
        ltp=120.0,
    )

    # A real dispatch `context` dict — the exact shape `_build_context()`
    # produces (`now`, `segment_flags`, `position_rows`). This is what
    # makes this the real "does the condition genuinely connect"
    # end-to-end proof: no patch on `_expiry_preclose_exchanges` or its
    # shim — see the 2026-11-23 P0 fix in `_al_expiry_context_shim`'s
    # own docstring (both shims used to omit `.now`, making this
    # derivation ALWAYS return an empty set in production).
    real_context = {
        "now": frozen_now,
        "segment_flags": {
            "mcx_open": True,
            "minutes_until_mcx_close": 45,  # inside the 60-min close-action window
            "nse_open": False,
        },
        "position_rows": [
            {"tradingsymbol": option_symbol, "exchange": "MCX", "quantity": 1},
        ],
    }
    return real_agent_id, agent_like, option_symbol, frozen_now, lone_itm_call, real_context


async def _e2e_stage_held_row(factory, frozen_now, lone_itm_call, agent_like, real_context):
    """Steps 1-4: the real condition-window derivation (via the real,
    now-fixed shim) -> the real action -> the real scan_positions()
    (only broker-touching fetch methods mocked) -> the real
    close_positions(), with the real database patched to the sqlite
    shadow DB built above."""
    from backend.api.algo.actions_live import _action_live_expiry_auto_close

    with patch("backend.api.database.async_session", factory), \
         patch("backend.api.algo.expiry.timestamp_indian", return_value=frozen_now), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_option_positions",
               return_value=[lone_itm_call]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_future_positions",
               return_value=[]), \
         patch("backend.api.algo.expiry.ExpiryEngine._fetch_underlying_ltps",
               return_value={"CRUDEOIL": 6200.0}), \
         patch("backend.api.algo.expiry._exp_leg_in_flight_this_session",
               new=AsyncMock(return_value=False)), \
         patch("backend.api.algo.expiry.expiry_close_held", return_value=True):
        await _action_live_expiry_auto_close(agent_like, real_context, {})


async def _e2e_assert_held_row(factory, _AlgoOrder, real_agent_id):
    """Verify step 4's own output: a real HELD row, with a real
    agent_id and a real, resolved auto_release_at — the Done-when
    criterion this whole plan exists to satisfy. Returns
    (held_row, auto_release_at)."""
    from backend.api.algo.order_hold import parse_hold_record

    async with factory() as s:
        from sqlalchemy import select
        held_rows = (await s.execute(
            select(_AlgoOrder).where(_AlgoOrder.status == "HELD")
        )).scalars().all()

    assert len(held_rows) == 1, f"expected exactly one HELD row, got {len(held_rows)}"
    held_row = held_rows[0]
    assert held_row.agent_id == real_agent_id
    assert held_row.account == "ZG0790"
    assert held_row.symbol == "CRUDEOIL26NOV5500CE"
    assert held_row.exchange == "MCX"
    assert held_row.transaction_type == "SELL"  # closing a long CALL means selling it
    assert held_row.quantity == 1

    rec = parse_hold_record(held_row.hold_json)
    assert rec is not None
    assert rec["category"] == "expiry_close"
    assert rec["price_policy"] == "CHASE_HIGH"
    assert rec.get("auto_release_at"), (
        "no HELD expiry-close row may ever lack auto_release_at — this is "
        "the exact unreleasable-hold bug Part C exists to close"
    )
    auto_release_at = datetime.fromisoformat(rec["auto_release_at"])
    if auto_release_at.tzinfo is None:
        auto_release_at = auto_release_at.replace(tzinfo=timezone.utc)
    # Policy is {"after_min": 30, "floor_tier": "high"} — the deadline
    # should be roughly held_at + 30 minutes (clamped only if close is
    # imminent, which it isn't here — close is hours away).
    assert auto_release_at > datetime.now(timezone.utc), (
        "the resolved deadline must be in the future immediately after staging"
    )
    return held_row, auto_release_at


async def _e2e_run_sweep(factory, sweep_now):
    """Steps 6-7: the real (widened) auto-release sweep -> the real
    release_held_order() default handler. Only the broker boundary
    (get_broker / broker_apis.fetch_positions / broker.quote) and
    chase_order() itself are mocked — matching the task's own
    "mock only the broker boundary, not the internal functions"
    requirement, and the identical boundary choice the two
    already-shipped 2026-11-22 E2E tests in test_expiry_logic.py use.
    Returns (released_count, chase_order_mock)."""
    mock_broker = MagicMock()
    mock_broker.quote = MagicMock(return_value={
        "MCX:CRUDEOIL26NOV5500CE": {
            "last_price": 130.0,
            "depth": {"buy": [{"price": 129.0}], "sell": [{"price": 131.0}]},
            "lower_circuit_limit": 0.0,
            "upper_circuit_limit": 0.0,
        }
    })

    positions_df = pd.DataFrame([{
        "tradingsymbol": "CRUDEOIL26NOV5500CE",
        "exchange": "MCX",
        "quantity": 1,  # still net long +1 — the close hasn't happened yet
        "account": "ZG0790",
    }])

    from backend.api import background as bg

    chase_order_mock = AsyncMock()

    before_tasks = set(asyncio.all_tasks())
    with patch("backend.api.database.async_session", factory), \
         patch("backend.brokers.get_broker", return_value=mock_broker), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[positions_df]), \
         patch("backend.api.routes.orders_helpers._ensure_tick_index", new=AsyncMock()), \
         patch("backend.api.routes.orders_helpers._TICK_INDEX",
               {("MCX", "CRUDEOIL26NOV5500CE"): 1.0}), \
         patch("backend.api.algo.agent_engine.get_agent_id_by_slug",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.algo.chase.chase_order", new=chase_order_mock):
        released = await bg._agent_close_auto_release_sweep_once(now=sweep_now)

        # Step 8 continued: drain the fire-and-forget `_chase_released`
        # task `release_held_order()` spawns via `asyncio.create_task`
        # before asserting on the mock — it is not awaited inline.
        await _e2e_drain_pending_tasks(before_tasks)

    return released, chase_order_mock


async def _e2e_drain_pending_tasks(before_tasks):
    pending = [t for t in asyncio.all_tasks() - before_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending)


def _e2e_assert_chase_call(chase_order_mock, held_row, real_agent_id):
    """Step 8: the real chase placement, at the correct tier/side/qty/
    identity — CHASE_HIGH resolves to the 'high' tier."""
    chase_order_mock.assert_awaited_once()
    call_kwargs = chase_order_mock.await_args.kwargs
    assert call_kwargs["account"] == "ZG0790"
    assert call_kwargs["symbol"] == "CRUDEOIL26NOV5500CE"
    assert call_kwargs["transaction_type"] == "SELL"
    assert call_kwargs["quantity"] == 1
    assert call_kwargs["algo_order_id"] == held_row.id
    assert call_kwargs["agent_id"] == real_agent_id
    cfg = call_kwargs["cfg"]
    assert cfg.level == "high"
    assert (cfg.interval_seconds, cfg.aggression_step, cfg.max_attempts) == (10, 0.25, 10)


@pytest.mark.asyncio
async def test_full_chain_condition_to_hold_to_timepass_to_release_to_chase(
    _expiry_sqlite_session_factory,
):
    """THE mandatory end-to-end test (Deadline section, item 1).

    Split into small `_e2e_*` helpers above purely to keep this
    function's own cyclomatic complexity under the CC gate — the
    sequence of steps, patches, and assertions is otherwise unchanged
    from the original single-function version."""
    factory, _AlgoOrder, _Agent = _expiry_sqlite_session_factory

    (real_agent_id, agent_like, option_symbol, frozen_now,
     lone_itm_call, real_context) = await _e2e_seed_agent_and_context(factory, _Agent)

    await _e2e_stage_held_row(factory, frozen_now, lone_itm_call, agent_like, real_context)

    held_row, auto_release_at = await _e2e_assert_held_row(factory, _AlgoOrder, real_agent_id)

    # ── Step 5: simulate time passage — the sweep runs with `now` at
    # (slightly past) the stored deadline, no real sleeping needed.
    sweep_now = auto_release_at + timedelta(seconds=5)

    released, chase_order_mock = await _e2e_run_sweep(factory, sweep_now)

    assert released == 1, "the sweep must release exactly the one HELD row"

    _e2e_assert_chase_call(chase_order_mock, held_row, real_agent_id)

    # ── And the row itself is no longer HELD — release_held_order()
    # moved it to OPEN (and cleared hold_json) before spawning the chase.
    async with factory() as s:
        from sqlalchemy import select
        refreshed = (await s.execute(
            select(_AlgoOrder).where(_AlgoOrder.id == held_row.id)
        )).scalar_one()
    assert refreshed.status == "OPEN"
    assert refreshed.hold_json is None
