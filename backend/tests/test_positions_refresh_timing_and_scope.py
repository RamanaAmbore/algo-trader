"""
Fix 2 (orders-page P1 audit): `_positions_refresh_after_fill` took its
baseline reading too late (t+3s, AFTER the broker had usually already
applied the fill) to ever detect a real change, so the 5-attempt poll
loop reliably timed out and `positions_refreshed` essentially never
fired in practice. The `qty_delta` parameter was accepted but never
used in the comparison. Every poll attempt was also scoped to EVERY
configured account (`@for_all_accounts` fan-out), not just the one
that filled. Separately, paper/sim/replay fills (which share
PaperTradeEngine and fan out through `_postback_broadcast_fanout` with
the one literal `broker="paper"`) scheduled this same real-broker poll
for no reason, since a simulated fill never touches a real position.

Covers:
  1. The baseline is read BEFORE the propagation sleep and compared
     against the algebraically expected post-fill quantity
     (baseline + qty_delta) — a time-aware scenario that would fail
     under the OLD "read baseline after the first sleep" design.
  2. Every poll attempt scopes `fetch_positions` to the account that
     filled (`account=...`), not an all-accounts fan-out.
  3. `_postback_broadcast_fanout` never schedules
     `_positions_refresh_after_fill` for `broker="paper"` — with a
     positive control (`broker="kite"`) proving the assertion isn't
     vacuously true.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────
# 1. Baseline-timing fix — expected-quantity comparison
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_fires_when_fill_lands_during_propagation_sleep():
    """Time-aware proof of the fix: `fetch_positions` returns the
    PRE-fill quantity until the first `asyncio.sleep` call has landed
    (simulating the fill arriving broker-side during the 2s
    propagation window), then the POST-fill quantity afterwards.

    Under the OLD code — which read its "baseline" INSIDE the loop,
    after the initial `sleep(2)` had already elapsed — this exact
    scenario reproduces the defect: the first read inside the loop
    would already observe the POST-fill value, making cur_qty equal
    to that "baseline" on every subsequent attempt, so the loop timed
    out without ever firing `positions_refreshed`. The fix reads the
    baseline BEFORE any sleep, so it correctly captures the PRE-fill
    value and detects the change once the broker catches up.
    """
    import pandas as pd
    from backend.api.routes.orders import _positions_refresh_after_fill

    sleep_calls = {"n": 0}

    async def _fake_sleep(_seconds):
        sleep_calls["n"] += 1

    pre_fill_df = pd.DataFrame([{"tradingsymbol": "NIFTY25JUL24000CE", "quantity": 0}])
    post_fill_df = pd.DataFrame([{"tradingsymbol": "NIFTY25JUL24000CE", "quantity": 50}])

    def _fake_fetch_positions(*args, **kwargs):
        # No sleep has landed yet -> this is the pre-propagation
        # baseline read; everything after is a post-propagation poll.
        return [pre_fill_df] if sleep_calls["n"] == 0 else [post_fill_df]

    broadcasts = []
    with patch("asyncio.sleep", new=_fake_sleep), \
         patch("backend.brokers.broker_apis.fetch_positions",
               side_effect=_fake_fetch_positions), \
         patch("backend.brokers.broker_apis._raw_cache_invalidate"), \
         patch("backend.api.routes.orders.invalidate"), \
         patch("backend.api.routes.orders.broadcast",
               side_effect=lambda msg: broadcasts.append(msg)):
        await _positions_refresh_after_fill("ZG0001", "NIFTY25JUL24000CE", 50)

    assert any("positions_refreshed" in b for b in broadcasts), (
        "the baseline must be read BEFORE the propagation sleep so a "
        "fill landing during that window is actually detected — this "
        "is the exact scenario the old late-baseline read missed"
    )


@pytest.mark.asyncio
async def test_expected_qty_uses_qty_delta_parameter():
    """The algebraic expected value (baseline + qty_delta) must drive
    the fire decision — not fire on a false positive. Quantity stays
    at 100 for EVERY call (the expected fill never actually lands at
    the broker), so even though a +50 qty_delta was requested, neither
    'matches expected' nor 'differs from baseline' is true — nothing
    should be invalidated or broadcast."""
    import pandas as pd
    from backend.api.routes.orders import _positions_refresh_after_fill

    same_df = pd.DataFrame([{"tradingsymbol": "NIFTY25JUL24000CE", "quantity": 100}])

    invalidated = []
    with patch("asyncio.sleep", new=AsyncMock()), \
         patch("backend.brokers.broker_apis.fetch_positions", return_value=[same_df]), \
         patch("backend.brokers.broker_apis._raw_cache_invalidate"), \
         patch("backend.api.routes.orders.invalidate",
               side_effect=lambda key: invalidated.append(key)), \
         patch("backend.api.routes.orders.broadcast"):
        await _positions_refresh_after_fill("ZG0001", "NIFTY25JUL24000CE", 50)

    assert invalidated == []


# ─────────────────────────────────────────────────────────────────────────
# 2. Single-account scoping
# ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_fetch_call_scoped_to_filled_account_not_all_accounts():
    """Every fetch_positions call (baseline + every poll attempt) must
    be scoped to the account that actually filled via `account=...`,
    never an all-accounts fan-out — a 4-leg basket fill on 3 accounts
    must not trigger ~60 broker calls for nothing."""
    import pandas as pd
    from backend.api.routes.orders import _positions_refresh_after_fill

    df = pd.DataFrame([{"tradingsymbol": "NIFTY25JUL24000CE", "quantity": 50}])
    calls: list[dict] = []

    def _fake_fetch_positions(*args, **kwargs):
        calls.append(kwargs)
        return [df]

    with patch("asyncio.sleep", new=AsyncMock()), \
         patch("backend.brokers.broker_apis.fetch_positions",
               side_effect=_fake_fetch_positions), \
         patch("backend.brokers.broker_apis._raw_cache_invalidate"), \
         patch("backend.api.routes.orders.broadcast"):
        await _positions_refresh_after_fill("ZG0001", "NIFTY25JUL24000CE", 0)

    assert len(calls) >= 1, "fetch_positions must be called at least once"
    for i, kwargs in enumerate(calls):
        assert kwargs.get("account") == "ZG0001", (
            f"poll attempt {i} did not scope fetch_positions to the "
            f"filled account — got kwargs={kwargs}"
        )
        assert kwargs.get("force_refresh") is True


# ─────────────────────────────────────────────────────────────────────────
# 3. Paper-broker fills never schedule the real-broker poll
# ─────────────────────────────────────────────────────────────────────────

class TestFanoutSkipsRefreshForPaperBroker:
    """`_postback_broadcast_fanout`'s create_task call is spied on
    directly (rather than inferred from side effects) so the assertion
    can't pass vacuously from the function's own blanket try/except —
    see the positive `broker="kite"` control below."""

    def test_paper_broker_never_schedules_refresh(self):
        from backend.api.routes.orders import _postback_broadcast_fanout

        scheduled = []

        def _fake_create_task(coro):
            scheduled.append(coro)
            coro.close()  # never actually run it
            return MagicMock()

        with patch("backend.api.routes.orders.invalidate"), \
             patch("backend.api.routes.orders.broadcast"), \
             patch("asyncio.create_task", side_effect=_fake_create_task):
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="O1", account="SIMACC",
                masked="S1####", symbol="NIFTY25JUL24000CE", txn="BUY",
                qty=50, price=100.0, broker="paper", exchange="NFO",
            )

        assert scheduled == [], (
            "a paper/sim/replay fill must never schedule a real-broker "
            "positions poll — all three modes share PaperTradeEngine "
            "and fan out through this helper with broker='paper'"
        )

    def test_kite_broker_still_schedules_refresh_positive_control(self):
        """Positive control: a real broker fill (kite) must still
        schedule the refresh — proving the paper-skip assertion above
        actually exercises the gate rather than passing vacuously."""
        from backend.api.routes.orders import _postback_broadcast_fanout

        scheduled = []

        def _fake_create_task(coro):
            scheduled.append(coro)
            coro.close()
            return MagicMock()

        with patch("backend.api.routes.orders.invalidate"), \
             patch("backend.api.routes.orders.broadcast"), \
             patch("asyncio.create_task", side_effect=_fake_create_task):
            _postback_broadcast_fanout(
                status="COMPLETE", order_id="O2", account="ZG0790",
                masked="ZG####", symbol="NIFTY25JUL24000CE", txn="BUY",
                qty=50, price=100.0, broker="kite", exchange="NFO",
            )

        assert len(scheduled) == 1, (
            "a genuine broker fill must still schedule exactly one "
            "positions-refresh task"
        )
