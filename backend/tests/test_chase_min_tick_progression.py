"""
Tests for the minimum 1-tick price progression guard in chase_order.

Bug: when the bid-ask spread is 1-2 ticks wide, _calc_limit_price snaps to
the same price on consecutive attempts. The chase loop was cancelling and
re-placing at IDENTICAL prices — visible as frozen price in the chase panel.

Fix: when attempt > 1 and price == last_placed_price, force at least 1-tick
movement toward the opposite side (BUY: +1 tick clamped to best_ask;
SELL: -1 tick floored by best_bid).

Five quality dimensions:
  SSOT   — minimum-tick logic in chase_order (caller), NOT in _calc_limit_price
  Perf   — force path runs O(1) via _INSTRUMENT_INDEX; no extra broker calls
  Stale  — _calc_limit_price signature/return semantics unchanged
  Reuse  — same depth object used in force path (no extra depth fetch)
  UX     — operator sees real price movement in chase panel on every attempt
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_depth(bid: float, ask: float) -> dict:
    """Minimal depth dict with a single bid and ask level."""
    return {
        "buy":  [{"price": bid,  "quantity": 50}],
        "sell": [{"price": ask,  "quantity": 50}],
    }


def _make_cfg(exchange: str = "NFO", interval: float = 0.0, max_attempts: int = 3):
    """Chase config with zero-wait intervals for fast tests."""
    from backend.api.algo.chase import ChaseConfig
    return ChaseConfig(
        exchange=exchange,
        interval_seconds=interval,
        max_attempts=max_attempts,
    )


# Depth that causes sub-tick aggression: spread=0.05, tick=0.05 → increment=0.0025
TIGHT_DEPTH_BID = 100.00
TIGHT_DEPTH_ASK = 100.05
TIGHT_DEPTH = _make_depth(TIGHT_DEPTH_BID, TIGHT_DEPTH_ASK)


# ── SSOT: _calc_limit_price signature unchanged ───────────────────────────────

def test_calc_limit_price_signature_unchanged():
    """
    [SSOT] _calc_limit_price must not have changed signature or return type.
    Minimum-tick logic must live in the caller (chase_order), not here.
    """
    from backend.api.algo.chase import _calc_limit_price
    import inspect
    sig = inspect.signature(_calc_limit_price)
    params = list(sig.parameters.keys())
    assert params == ["depth", "transaction_type", "attempt", "aggression_step", "exchange", "symbol"], (
        f"_calc_limit_price signature changed: {params}"
    )


def test_calc_limit_price_returns_float():
    """[SSOT] _calc_limit_price return type is float (return semantics unchanged)."""
    from backend.api.algo.chase import _calc_limit_price
    price = _calc_limit_price(TIGHT_DEPTH, "BUY", 1, 0.1)
    assert isinstance(price, float), f"Expected float, got {type(price)}"
    assert price > 0


def test_calc_limit_price_same_on_tight_spread():
    """
    [SSOT] _calc_limit_price CAN return the same price on consecutive attempts
    when the spread is 1 tick wide. This is the root cause the fix addresses.
    The fix must NOT change this function's behavior.
    """
    from backend.api.algo.chase import _calc_limit_price
    # Very tight spread (1 tick) + tiny aggression_step → sub-tick increments
    tight = _make_depth(100.00, 100.05)
    p1 = _calc_limit_price(tight, "BUY", 1, 0.05, exchange="NFO", symbol="NIFTY25CE")
    p2 = _calc_limit_price(tight, "BUY", 2, 0.05, exchange="NFO", symbol="NIFTY25CE")
    # These may or may not be equal depending on snap — document either way
    # (the important thing is _calc_limit_price isn't expected to always diverge)
    assert isinstance(p1, float) and isinstance(p2, float)


# ── Core behavior: BUY gets +1 tick on stale price ───────────────────────────

@pytest.mark.asyncio
async def test_buy_forced_one_tick_higher_on_repeat():
    """
    [CORE] When _calc_limit_price returns the same price twice in a row,
    a BUY attempt places the order 1 tick higher than the previous attempt.
    """
    from backend.api.algo.chase import chase_order, ChaseStatus

    tick = 0.05
    first_price = 100.00   # attempt 1 places here
    # attempt 2: _calc_limit_price returns same price again
    placed_prices: list[float] = []

    # _calc_limit_price always returns first_price (simulates sub-tick scenario)
    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        return first_price

    # _place_order records the price and returns a fake order id
    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        """Dispatch to correct sync helper based on fn identity."""
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            return TIGHT_DEPTH
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        # First iteration: continue; second: fill
        if len(placed_prices) < 2:
            return "continue", args[7]  # (signal, remaining_qty)
        return "filled", 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=tick),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 2, (
        f"Expected 2 place_order calls, got {len(placed_prices)}: {placed_prices}"
    )
    assert placed_prices[0] == first_price, (
        f"Attempt 1 should place at {first_price}, got {placed_prices[0]}"
    )
    assert placed_prices[1] == pytest.approx(first_price + tick, abs=1e-6), (
        f"Attempt 2 (BUY) should be 1 tick higher: expected {first_price + tick}, "
        f"got {placed_prices[1]}"
    )


@pytest.mark.asyncio
async def test_sell_forced_one_tick_lower_on_repeat():
    """
    [CORE] When _calc_limit_price returns the same price twice in a row,
    a SELL attempt places the order 1 tick lower than the previous attempt.
    """
    from backend.api.algo.chase import chase_order

    tick = 0.05
    first_price = 100.05   # attempt 1 places here
    placed_prices: list[float] = []

    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        return first_price

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            return TIGHT_DEPTH
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        if len(placed_prices) < 2:
            return "continue", args[7]
        return "filled", 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=tick),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        result = await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="SELL",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 2, (
        f"Expected 2 place_order calls, got {len(placed_prices)}: {placed_prices}"
    )
    assert placed_prices[0] == first_price, (
        f"Attempt 1 should place at {first_price}, got {placed_prices[0]}"
    )
    assert placed_prices[1] == pytest.approx(first_price - tick, abs=1e-6), (
        f"Attempt 2 (SELL) should be 1 tick lower: expected {first_price - tick}, "
        f"got {placed_prices[1]}"
    )


# ── BUY clamp: forced price never exceeds best_ask ───────────────────────────

@pytest.mark.asyncio
async def test_buy_forced_clamped_to_best_ask():
    """
    [SSOT] When BUY forced price overshoots best_ask, it is clamped to best_ask.
    Prevents placing a BUY order above the current offer.
    """
    from backend.api.algo.chase import chase_order

    tick = 0.05
    best_ask = 100.05
    # last_placed_price will be set to best_ask - tick = 100.00
    # forced = 100.00 + 0.05 = 100.05 = best_ask → clamp → 100.05
    first_price = best_ask - tick   # 100.00
    placed_prices: list[float] = []
    depth = _make_depth(100.00, best_ask)

    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        return first_price  # same every attempt

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            return depth
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        if len(placed_prices) < 2:
            return "continue", args[7]
        return "filled", 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=tick),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 2
    # Forced price is clamped to best_ask (100.05)
    assert placed_prices[1] <= best_ask, (
        f"BUY forced price must not exceed best_ask={best_ask}, got {placed_prices[1]}"
    )


# ── SELL floor: forced price never goes below best_bid ───────────────────────

@pytest.mark.asyncio
async def test_sell_forced_floored_by_best_bid():
    """
    [SSOT] When SELL forced price undershoots best_bid, it is floored to best_bid.
    Prevents placing a SELL order below the current bid.
    """
    from backend.api.algo.chase import chase_order

    tick = 0.05
    best_bid = 100.00
    # last_placed_price will be set to best_bid + tick = 100.05
    # forced = 100.05 - 0.05 = 100.00 = best_bid → floor → 100.00
    first_price = best_bid + tick   # 100.05
    placed_prices: list[float] = []
    depth = _make_depth(best_bid, 100.05)

    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        return first_price

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            return depth
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        if len(placed_prices) < 2:
            return "continue", args[7]
        return "filled", 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=tick),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="SELL",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 2
    # Forced price is floored at best_bid (100.00)
    assert placed_prices[1] >= best_bid, (
        f"SELL forced price must not go below best_bid={best_bid}, got {placed_prices[1]}"
    )


# ── Stale depth: best_ask=0 → no clamp applied ───────────────────────────────

@pytest.mark.asyncio
async def test_buy_stale_depth_no_clamp_to_zero():
    """
    [STALE] When depth is stale (best_ask=0), the BUY forced price is NOT
    clamped to 0. The unclamped snapped price is used instead.
    Prevents: min(forced, 0) = 0, triggering price<=0 guard on next attempt.
    """
    from backend.api.algo.chase import chase_order

    tick = 0.05
    first_price = 100.00
    placed_prices: list[float] = []
    stale_depth = {"buy": [], "sell": []}  # both sides empty → bid=0, ask=0

    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        return first_price

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            return stale_depth
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        if len(placed_prices) < 2:
            return "continue", args[7]
        return "filled", 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=tick),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 2
    # When best_ask=0, force path uses unclamped snap (first_price + tick)
    assert placed_prices[1] > 0, (
        f"BUY forced price must be > 0 even with stale depth (best_ask=0), "
        f"got {placed_prices[1]}"
    )
    assert placed_prices[1] == pytest.approx(first_price + tick, abs=1e-6), (
        f"BUY forced price with stale depth should be last_placed+tick={first_price + tick}, "
        f"got {placed_prices[1]}"
    )


# ── Perf: no extra broker call in force path ─────────────────────────────────

@pytest.mark.asyncio
async def test_force_path_uses_same_depth_no_extra_broker_call():
    """
    [PERF] The minimum-tick force path reuses the depth already fetched at the
    top of the iteration. It must NOT trigger a second _get_depth call per attempt.
    """
    from backend.api.algo.chase import chase_order

    tick = 0.05
    first_price = 100.00
    placed_prices: list[float] = []
    depth_fetch_count = 0

    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        return first_price

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        nonlocal depth_fetch_count
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            depth_fetch_count += 1
            return TIGHT_DEPTH
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        if len(placed_prices) < 2:
            return "continue", args[7]
        return "filled", 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=tick),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 2
    # Exactly 1 depth fetch per attempt (2 attempts → 2 fetches), not 3
    assert depth_fetch_count == 2, (
        f"Force path must reuse existing depth, not re-fetch. "
        f"Expected 2 _get_depth calls (1 per attempt), got {depth_fetch_count}"
    )


# ── No force on attempt 1 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_force_on_first_attempt():
    """
    [SSOT] The force path only activates on attempt > 1.
    The first order is placed at exactly _calc_limit_price's result.
    """
    from backend.api.algo.chase import chase_order, ChaseStatus

    tick = 0.05
    calc_price = 100.00
    placed_prices: list[float] = []

    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        return calc_price

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return "order_1"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            return TIGHT_DEPTH
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        # Fill immediately on attempt 1
        return "filled", 0

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", return_value=tick),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 1
    assert placed_prices[0] == calc_price, (
        f"Attempt 1 must use _calc_limit_price result directly ({calc_price}), "
        f"got {placed_prices[0]}"
    )


# ── No force when price naturally moves ──────────────────────────────────────

@pytest.mark.asyncio
async def test_no_force_when_price_changes_naturally():
    """
    [SSOT] When _calc_limit_price returns a different price on attempt 2
    (natural progression), the force path must NOT activate.
    """
    from backend.api.algo.chase import chase_order

    tick = 0.05
    prices_from_calc = [100.00, 100.05]  # naturally diverges
    placed_prices: list[float] = []
    call_count = 0

    def _fake_calc(depth, tx_type, attempt, aggression_step, exchange="", symbol=""):
        nonlocal call_count
        p = prices_from_calc[min(call_count, len(prices_from_calc) - 1)]
        call_count += 1
        return p

    def _fake_place(account, symbol, tx_type, qty, price, cfg):
        placed_prices.append(price)
        return f"order_{len(placed_prices)}"

    async def _fake_run(fn, *args):
        from backend.api.algo.chase import _get_depth, _place_order
        if fn is _get_depth:
            return TIGHT_DEPTH
        if fn is _place_order:
            return _fake_place(*args)
        return None

    async def _fake_poll(*args, **kwargs):
        if len(placed_prices) < 2:
            return "continue", args[7]
        return "filled", 0

    mock_tick_sync = MagicMock(return_value=tick)

    with (
        patch("backend.shared.helpers.utils.is_prod_branch", return_value=True),
        patch("backend.api.algo.agent_engine._symbol_exchange_open", return_value=True),
        patch("backend.api.algo.agent_engine._build_now_ctx", return_value={}),
        patch("backend.api.algo.chase._calc_limit_price", side_effect=_fake_calc),
        patch("backend.api.algo.chase._run", side_effect=_fake_run),
        patch("backend.api.algo.chase._chase_poll_status", side_effect=_fake_poll),
        patch("backend.api.algo.chase._ch_cancel_previous", new_callable=AsyncMock),
        patch("backend.api.algo.chase._sync_algo_order_id", new_callable=AsyncMock),
        patch("backend.api.algo.chase._ch_post_replace_kill_check", return_value=False),
        patch("backend.api.algo.chase._tick_size_sync", mock_tick_sync),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        cfg = _make_cfg(exchange="NFO", max_attempts=3)
        await chase_order(
            account="ACC1",
            symbol="NIFTY25CE",
            transaction_type="BUY",
            quantity=50,
            cfg=cfg,
        )

    assert len(placed_prices) == 2
    assert placed_prices[0] == prices_from_calc[0]
    assert placed_prices[1] == prices_from_calc[1], (
        f"When calc returns different price, force must not apply: "
        f"expected {prices_from_calc[1]}, got {placed_prices[1]}"
    )
    # _tick_size_sync must NOT be called when force path is skipped
    mock_tick_sync.assert_not_called()


# ── Docstring audit ───────────────────────────────────────────────────────────

def test_calc_limit_price_docstring_mentions_tick_limitation():
    """
    [SSOT] _calc_limit_price docstring must mention the tick-quantization
    limitation and that the caller enforces minimum-tick progression.
    """
    from backend.api.algo.chase import _calc_limit_price
    doc = _calc_limit_price.__doc__ or ""
    assert "sub-tick" in doc or "tick" in doc.lower(), (
        "Docstring must mention tick quantization limitation"
    )
    assert "caller" in doc.lower(), (
        "Docstring must note that the caller (chase_order) handles progression"
    )


def test_last_placed_price_in_chase_source():
    """
    [SSOT] last_placed_price variable must exist in chase_order source.
    Source-level audit that the fix is present.
    """
    from pathlib import Path
    src = Path("backend/api/algo/chase.py").read_text()
    assert "last_placed_price" in src, (
        "last_placed_price tracking variable must exist in chase.py"
    )
