"""test_template_attach_lock_coverage.py

Covers a 2026-10 fix: the per-row `_get_template_attach_lock` (minted in
`orders_place.py`, already used by the fill-triggered path
`_fire_template_attach_on_fill`) was never acquired by two OTHER callers
of `apply_template_to_order`:

  - `orders.py:_rco_run_template_attach` (the admin `/retry-template`
    route's attach call)
  - `actions.py:_al_apply_template` (an agent-fired place_order action's
    own template attach)

Before a separate 2026-10 fix (see test_template_attach_event_loop_offload.py),
this was accidentally safe: `apply_plan_live` ran synchronously on the
event loop, so the whole process was serialized while any attach ran —
no other coroutine (including a concurrent fill-triggered attach on the
SAME row) could interleave. Once the live branch was offloaded to a
worker thread (asyncio.to_thread), that accidental serialization went
away, opening a real (if narrow — admin-triggered, not routine) race
where a retry/reconcile attach could interleave with a concurrently-
running fill-triggered attach on the same row and place a duplicate
live GTT.

These tests prove both call sites now take the SAME per-row lock the
fill-triggered path already uses, via a real asyncio.Lock so the
assertion exercises actual mutual exclusion, not just a call count.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_rco_run_template_attach_holds_the_per_row_lock():
    from backend.api.routes import orders as orders_mod

    row = MagicMock()
    row.id = 777
    row.template_id = 11
    row.account = "ZG0790"
    row.symbol = "NIFTY25JULFUT"
    row.exchange = "NFO"
    row.transaction_type = "SELL"
    row.product = "NRML"
    row.mode = "live"
    row.fill_price = 100.0
    row.initial_price = 100.0
    row.template_overrides_json = None

    real_lock = asyncio.Lock()
    lock_states_during_call = []

    async def _fake_apply_template_to_order(**kwargs):
        # The lock must already be held by the caller at this point.
        lock_states_during_call.append(real_lock.locked())
        return None

    with patch.object(orders_mod, "_get_template_attach_lock",
                       new=AsyncMock(return_value=real_lock)), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=AsyncMock(side_effect=_fake_apply_template_to_order)), \
         patch.object(orders_mod, "_retry_effective_parent_qty",
                      new=AsyncMock(return_value=50)):
        await orders_mod._rco_run_template_attach(row)

    assert lock_states_during_call == [True], (
        "apply_template_to_order must run while the per-row lock is held"
    )
    assert not real_lock.locked(), "lock must be released after the call"


@pytest.mark.asyncio
async def test_al_apply_template_holds_the_per_row_lock():
    from backend.api.algo import actions as actions_mod

    real_lock = asyncio.Lock()
    lock_states_during_call = []

    async def _fake_apply_template_to_order(**kwargs):
        lock_states_during_call.append(real_lock.locked())
        result = MagicMock()
        result.to_dict.return_value = {"ok": True}
        return result

    agent = MagicMock()
    agent.slug = "test-agent"

    with patch("backend.api.routes.orders_place._get_template_attach_lock",
               new=AsyncMock(return_value=real_lock)), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=AsyncMock(side_effect=_fake_apply_template_to_order)):
        result = await actions_mod._al_apply_template(
            agent, 888, None, "default-bull", {}, {},
            parent_account="ZG0790", parent_symbol="NIFTY25JULFUT",
            parent_side="SELL", parent_qty=50, parent_exchange="NFO",
            parent_price=100.0, apply_path="live",
        )

    assert lock_states_during_call == [True], (
        "apply_template_to_order must run while the per-row lock is held"
    )
    assert not real_lock.locked(), "lock must be released after the call"
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_al_apply_template_still_swallows_exceptions_and_returns_none():
    """Regression guard: the lock wrap must not change the function's
    existing contract of swallowing exceptions from apply_template_to_order
    and returning None (callers rely on this, not a raised exception)."""
    from backend.api.algo import actions as actions_mod

    agent = MagicMock()
    agent.slug = "test-agent"

    with patch("backend.api.routes.orders_place._get_template_attach_lock",
               new=AsyncMock(return_value=asyncio.Lock())), \
         patch("backend.api.algo.template_attach.apply_template_to_order",
               new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await actions_mod._al_apply_template(
            agent, 999, None, "default-bull", {}, {},
            parent_account="ZG0790", parent_symbol="NIFTY25JULFUT",
            parent_side="SELL", parent_qty=50, parent_exchange="NFO",
            parent_price=100.0, apply_path="live",
        )

    assert result is None
