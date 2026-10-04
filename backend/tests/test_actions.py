"""
Tests for actions.py helpers introduced by the LTP-fetch dedup refactor.

Covers:
  - _fetch_ltp: returns float when broker.ltp succeeds.
  - _fetch_ltp: returns None and emits a warning when broker.ltp raises.
  - Warning log line contains the caller-supplied context string.
  - Integration smoke: _action_place_order and _action_live_close_position
    still function end-to-end with the new helper in the call path.

The project bans mocking of broker API network semantics, but `broker.ltp`
here is a pure in-process stub — the precedent established in
test_agent_close_guards.py applies.
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ---------------------------------------------------------------------------
# _fetch_ltp — unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_ltp_returns_float_on_success():
    """broker.ltp returns a valid payload → helper unwraps last_price."""
    from backend.api.algo.actions import _fetch_ltp

    broker = MagicMock()
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": 24500.0}}

    loop = asyncio.get_running_loop()
    result = await _fetch_ltp(broker, "NFO", "NIFTY25JULFUT", loop, context="test")

    assert result == 24500.0


@pytest.mark.asyncio
async def test_fetch_ltp_returns_none_on_exception():
    """broker.ltp raises → helper returns None and calls logger.warning with context."""
    from backend.api.algo.actions import _fetch_ltp
    import backend.api.algo.actions_live as _mod

    broker = MagicMock()
    broker.ltp.side_effect = RuntimeError("session expired")

    warned: list[str] = []
    mock_logger = MagicMock()
    mock_logger.warning.side_effect = lambda msg, *a, **kw: warned.append(msg)

    loop = asyncio.get_running_loop()
    with patch.object(_mod, "logger", mock_logger):
        result = await _fetch_ltp(
            broker, "MCX", "CRUDEOILAUG25FUT", loop, context="close_position"
        )

    assert result is None
    assert warned, "Expected logger.warning to be called"
    assert any("close_position" in m and "LTP fetch failed" in m for m in warned), (
        f"Expected warning containing 'close_position' and 'LTP fetch failed'; got {warned}"
    )


@pytest.mark.asyncio
async def test_fetch_ltp_context_appears_in_warn_log():
    """Each caller context ('place_order', 'close_position') shows in warning."""
    from backend.api.algo.actions import _fetch_ltp
    import backend.api.algo.actions_live as _mod

    broker = MagicMock()
    broker.ltp.side_effect = ConnectionError("timeout")

    warned: list[str] = []
    mock_logger = MagicMock()
    mock_logger.warning.side_effect = lambda msg, *a, **kw: warned.append(msg)

    loop = asyncio.get_running_loop()
    with patch.object(_mod, "logger", mock_logger):
        await _fetch_ltp(broker, "NFO", "NIFTY25JULFUT", loop, context="place_order")

    assert any("place_order" in m for m in warned), (
        f"context='place_order' missing from warnings: {warned}"
    )


@pytest.mark.asyncio
async def test_fetch_ltp_returns_none_when_last_price_zero():
    """last_price=0 is coerced to None (falsy float guard)."""
    from backend.api.algo.actions import _fetch_ltp

    broker = MagicMock()
    broker.ltp.return_value = {"NFO:X": {"last_price": 0}}

    loop = asyncio.get_running_loop()
    result = await _fetch_ltp(broker, "NFO", "X", loop)

    assert result is None


# ---------------------------------------------------------------------------
# Integration smoke — _action_place_order
# ---------------------------------------------------------------------------

def _make_conns_stub(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


def _make_broker_stub(*, ltp_value: float = 23500.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [{"initial": {"total": 5_000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": 500_000.0},
        "commodity": {"enabled": True, "net": 500_000.0},
    }
    broker.ltp.return_value = {f"NFO:NIFTY25JULFUT": {"last_price": ltp_value}}
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


@pytest.mark.asyncio
async def test_action_place_order_ltp_fetched_via_helper():
    """
    _action_place_order with no explicit price fetches LTP via _fetch_ltp
    and passes it to chase_order. Smoke-tests the refactored call path.
    """
    from backend.api.algo.actions import _action_place_order

    broker = _make_broker_stub(ltp_value=23500.0)
    conns  = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 7
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "NIFTY25JULFUT",
        "exchange": "NFO",
        "transaction_type": "SELL",
        "quantity": 50,
        # no 'price' key — forces LTP fetch
    }

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.actions_live._place_order_set_product_template",
               new=AsyncMock()) as mock_set_pt, \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_place_order(agent, context, params)

    # chase_order called — LTP fetch succeeded and preflight didn't block.
    mock_chase.assert_called_once()
    # product/template_id follow-up write was attempted for the intent row.
    mock_set_pt.assert_called_once_with(42, "NRML", None)


# ---------------------------------------------------------------------------
# chase_aggressiveness threading (_live_chase_config wiring)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_place_order_chase_aggressiveness_high_threads_through():
    """
    params.chase_aggressiveness='high' builds a ChaseConfig matching
    _live_chase_config('high', ...)'s tier (interval_seconds=10,
    aggression_step=0.25, max_attempts=10) — not the bare ChaseConfig()
    dataclass defaults. Also asserts cfg.exchange is preserved for a
    non-NFO exchange (MCX): _live_chase_config() never sets `exchange`
    itself (only interval/step/attempts/intent/product/variety/validity),
    so _action_place_order must set cfg.exchange explicitly after the
    call — exactly like the existing manual-chase caller in
    orders_helpers.py (`cfg.exchange = exchange or "NFO"`).
    """
    from backend.api.algo.actions import _action_place_order
    from backend.api.routes.orders_helpers import _live_chase_config

    broker = _make_broker_stub(ltp_value=5800.0)
    conns  = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 7
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOIL25OCTFUT",
        "exchange": "MCX",
        "transaction_type": "SELL",
        "quantity": 100,
        "product":  "NRML",
        "price":    5800.0,  # explicit price — skips the LTP-fetch branch
        "chase_aggressiveness": "high",
    }

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.actions_live._place_order_set_product_template",
               new=AsyncMock()), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_place_order(agent, context, params)

    mock_chase.assert_called_once()
    cfg = mock_chase.call_args.kwargs["cfg"]
    expected = _live_chase_config("high", product="NRML")
    assert cfg.interval_seconds == expected.interval_seconds == 10
    assert cfg.aggression_step == expected.aggression_step == 0.25
    assert cfg.max_attempts == expected.max_attempts == 10
    assert cfg.exchange == "MCX", (
        "cfg.exchange must be the order's real exchange, not the "
        "ChaseConfig dataclass default ('NFO') that _live_chase_config "
        "leaves untouched"
    )
    assert cfg.product == "NRML"


@pytest.mark.asyncio
async def test_action_place_order_no_aggressiveness_key_matches_prior_bare_chaseconfig():
    """
    Regression guard: an agent action with NO chase_aggressiveness key
    must produce a ChaseConfig byte-identical (field-for-field via
    dataclasses.asdict) to the bare ChaseConfig(exchange=exchange,
    product=product) construction this change replaces.

    Note this means the new code's absent-key default resolves to the
    'med' tier, not 'low' — ChaseConfig's own dataclass defaults
    (interval_seconds=20, aggression_step=0.10, max_attempts=20) are
    exactly the 'med' preset in _live_chase_config, not the 'low' one
    (30 / 0.05 / 30). Defaulting the absent-key case to 'low' would
    silently slow every existing live agent's chase cadence by 50%
    with no operator action — see actions_live.py docstring.
    """
    import dataclasses
    from backend.api.algo.actions import _action_place_order
    from backend.api.algo.chase import ChaseConfig

    broker = _make_broker_stub(ltp_value=23500.0)
    conns  = _make_conns_stub("ZG0790")
    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 7
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "NIFTY25JULFUT",
        "exchange": "NFO",
        "transaction_type": "SELL",
        "quantity": 50,
        # no 'chase_aggressiveness' key — existing agents never set this
    }

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.actions_live._place_order_set_product_template",
               new=AsyncMock()), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_place_order(agent, context, params)

    mock_chase.assert_called_once()
    cfg = mock_chase.call_args.kwargs["cfg"]
    prior_bare = ChaseConfig(exchange="NFO", product="NRML")
    assert dataclasses.asdict(cfg) == dataclasses.asdict(prior_bare)


# ---------------------------------------------------------------------------
# Integration smoke — _action_live_close_position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_live_close_position_ltp_fetched_via_helper():
    """
    _action_live_close_position with no explicit price fetches LTP via
    _fetch_ltp without error, reaches chase_order.
    """
    from backend.api.algo.actions import _action_live_close_position

    broker = _make_broker_stub(ltp_value=7300.0)
    broker.ltp.return_value = {"MCX:CRUDEOILAUG25FUT": {"last_price": 7300.0}}
    conns  = _make_conns_stub("ZG0790")
    agent  = MagicMock()
    agent.slug = "test-close"
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
        "quantity": 100,
        "side":     "SELL",
        # no 'price' key — forces LTP fetch
    }

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_close_position(agent, context, params)

    # chase_order reached — LTP fetch succeeded and preflight didn't block.
    mock_chase.assert_called_once()
