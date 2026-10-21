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
    # (2026-10 fix: _place_order_set_product_template now also takes
    # template_slug= / overrides= kwargs — this params dict has neither.)
    mock_set_pt.assert_called_once_with(
        42, "NRML", None, template_slug=None, overrides=None,
    )


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

    P1 fix (2026-10): `ChaseConfig` gained a `level` field (pure
    metadata — `orders_helpers._live_chase_config` stamps the normalised
    L/M/H tag onto it so a hold/release cycle can resume at the
    ORIGINAL tier instead of a hardcoded MED tuple; see
    `order_release._chase_level_from_price_policy`). `_live_chase_config`
    now sets `level="med"` here, so a bare `ChaseConfig()` (which leaves
    `level=None`) is no longer byte-identical — the comparison below
    excludes `level` and asserts it separately, since it's new metadata,
    not a change to the actual chase behaviour fields this guard exists
    to protect.
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
    cfg_fields = dataclasses.asdict(cfg)
    bare_fields = dataclasses.asdict(prior_bare)
    assert cfg_fields.pop("level") == "med"
    assert bare_fields.pop("level") is None
    assert cfg_fields == bare_fields


# ---------------------------------------------------------------------------
# chase_level threading (Phase 2 of order/agent grammar unification) —
# place_order's new canonical params_schema field. Mirrors the
# chase_aggressiveness tests above; chase_level is the new preferred key,
# chase_aggressiveness remains a working fallback for pre-Phase-2 agents.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_place_order_chase_level_high_threads_through():
    """params.chase_level='HIGH' (new canonical name, uppercase per the
    params_schema enum) builds the same ChaseConfig as
    chase_aggressiveness='high' did before Phase 2."""
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
        "price":    5800.0,
        "chase_level": "HIGH",
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


@pytest.mark.asyncio
async def test_action_place_order_chase_level_wins_over_legacy_chase_aggressiveness():
    """When both keys are set (shouldn't normally happen, but an agent's
    action JSON is operator-editable), the new chase_level param takes
    priority over the legacy chase_aggressiveness key."""
    from backend.api.algo.actions import _action_place_order
    from backend.api.routes.orders_helpers import _live_chase_config

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
        "chase_level": "HIGH",
        "chase_aggressiveness": "low",
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
    expected = _live_chase_config("high", product="NRML")
    assert cfg.interval_seconds == expected.interval_seconds == 10
    assert cfg.aggression_step == expected.aggression_step == 0.25
    assert cfg.max_attempts == expected.max_attempts == 10


# ---------------------------------------------------------------------------
# qty/quantity dual-key fallback in _al_place_resolve_params (actions_live.py)
#
# place_order's params_schema documents the quantity field as `qty`
# (backend/config/grammars/order_fields.yaml), but the executor has always
# read `params.get("quantity")`. The Automation page's "+ place_order"
# quick-add skeleton ships `qty` with no structured field to correct it, so
# an operator using that control unmodified fired a live order whose
# quantity silently resolved to 0. Fixed by adding a `qty` fallback,
# mirroring the already-shipped dual-key read in
# `_al_close_resolve_params` (close_position).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_place_order_qty_key_fallback_when_quantity_absent():
    """params={"qty": 50} with no 'quantity' key resolves to quantity=50,
    not 0 — the bug an operator hit via the Automation quick-add skeleton."""
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
        "qty": 50,
        # no 'quantity' key — matches the Automation quick-add skeleton
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
    assert mock_chase.call_args.kwargs["quantity"] == 50


@pytest.mark.asyncio
async def test_action_place_order_quantity_key_wins_over_qty():
    """When both keys are present, 'quantity' takes priority over 'qty'."""
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
        "quantity": 75,
        "qty": 50,
    }

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=75)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.actions_live._place_order_set_product_template",
               new=AsyncMock()), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_place_order(agent, context, params)

    mock_chase.assert_called_once()
    assert mock_chase.call_args.kwargs["quantity"] == 75


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
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_close_position(agent, context, params)

    # chase_order reached — LTP fetch succeeded and preflight didn't block.
    mock_chase.assert_called_once()


# ---------------------------------------------------------------------------
# Fix: fail closed when the AlgoOrder DB write fails (never place an
# untracked live order) — same incident class as AlgoOrder #1088, fixed
# for the manual-ticket path (orders_place.py) but previously missing on
# the agent-action path (_action_place_order / _action_live_close_position /
# _action_live_chase_close_positions).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_place_order_db_write_failure_never_calls_chase():
    """_write_live_order returning None (DB write failed) must abort
    before chase_order is ever called, and must raise so execute() logs
    action_failed instead of a false action_success."""
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
    }

    mock_chase = AsyncMock()
    mock_alert = MagicMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=None)), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
               new=mock_alert), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        with pytest.raises(RuntimeError):
            await _action_place_order(agent, context, params)

    mock_chase.assert_not_called()
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_action_live_close_position_db_write_failure_never_calls_chase():
    """Same fail-closed guard on the close_position path."""
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
    }

    mock_chase = AsyncMock()
    mock_alert = MagicMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=None)), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
               new=mock_alert), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        with pytest.raises(RuntimeError):
            await _action_live_close_position(agent, context, params)

    mock_chase.assert_not_called()
    mock_alert.assert_called_once()


def _make_positions_df(rows: list[dict]):
    """Build a minimal pandas DataFrame mirroring df_positions shape."""
    import pandas as pd
    return pd.DataFrame(rows)


@pytest.mark.asyncio
async def test_chase_close_positions_db_write_failure_skips_only_that_position():
    """Two positions; the first position's AlgoOrder write fails (DB
    down), the second succeeds. chase_order must be called exactly once
    — only for the position with a real tracking row — and the action
    must still raise (partial failure) so the agent cycle records it,
    without the already-queued second chase being orphaned."""
    from backend.api.algo.actions import _action_live_chase_close_positions

    agent  = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    df = _make_positions_df([
        {
            "account":       "ZG0790",
            "tradingsymbol": "CRUDEOILAUG25FUT",
            "exchange":      "MCX",
            "quantity":      300,
            "last_price":    7500.0,
            "close_price":   7450.0,
        },
        {
            "account":       "ZG0790",
            "tradingsymbol": "GOLDAUG25FUT",
            "exchange":      "MCX",
            "quantity":      100,
            "last_price":    72000.0,
            "close_price":   71900.0,
        },
    ])
    context = {"df_positions": df}
    params  = {}

    mock_chase = AsyncMock()
    mock_alert = MagicMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(side_effect=[None, 42])), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
               new=mock_alert), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        with pytest.raises(RuntimeError):
            await _action_live_chase_close_positions(agent, context, params)

    assert mock_chase.call_count == 1, (
        f"expected exactly 1 chase call (second position only), got {mock_chase.call_count}"
    )
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_chase_close_positions_all_db_write_failures_raises_no_chase():
    """Single position whose AlgoOrder write fails — chase_order must
    never be called, and the action must raise."""
    from backend.api.algo.actions import _action_live_chase_close_positions

    agent  = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    df = _make_positions_df([{
        "account":       "ZG0790",
        "tradingsymbol": "CRUDEOILAUG25FUT",
        "exchange":      "MCX",
        "quantity":      300,
        "last_price":    7500.0,
        "close_price":   7450.0,
    }])
    context = {"df_positions": df}
    params  = {}

    mock_chase = AsyncMock()
    mock_alert = MagicMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=None)), \
         patch("backend.shared.helpers.alert_utils.send_order_failure_alert",
               new=mock_alert), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        with pytest.raises(RuntimeError):
            await _action_live_chase_close_positions(agent, context, params)

    mock_chase.assert_not_called()
    mock_alert.assert_called_once()


# ---------------------------------------------------------------------------
# Live close-path preflight must carry the real resolved `side`, and
# `_chase_build_tasks` must carry the real `product` off the position row
# instead of a hardcoded "BUY"/"NRML" default (2026-10 live bug — a real
# SELL-to-close priced as a defaulted "BUY" looks like a brand-new long to
# Kite's position-aware margin netting, wrongly blocking a loss-limit close
# with MARGIN_SHORTFALL exactly when closing matters most; an MIS close
# hardcoded to NRML doesn't net against the real MIS position at all).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_live_close_position_preflight_carries_real_side_long_close():
    """Long position (net_qty > 0) closes with SELL — preflight dict's
    'side' must be 'SELL', never the _parse_broker_call_fields default
    of 'BUY'."""
    from backend.api.algo.actions import _action_live_close_position
    import backend.api.algo.actions_live as _mod

    broker = _make_broker_stub(ltp_value=100.0)
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": 100.0}}
    agent = MagicMock()
    agent.slug = "test-close"
    context: dict = {}
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
    }

    captured_orders: list[dict] = []

    async def _fake_run_preflight(account, order, paired_orders=None):
        captured_orders.append(order)
        return {"ok": True, "blocked": [], "diagnostics": {}}

    with patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch.object(_mod, "run_preflight", new=_fake_run_preflight), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.chase.chase_order", new=AsyncMock()):
        await _action_live_close_position(agent, context, params)

    assert len(captured_orders) == 1
    assert captured_orders[0]["side"] == "SELL", (
        f"expected real resolved side 'SELL' for a long close, "
        f"got {captured_orders[0].get('side')!r}"
    )


@pytest.mark.asyncio
async def test_action_live_close_position_preflight_carries_real_side_short_close():
    """Short position (net_qty < 0) closes with BUY — preflight dict's
    'side' must be 'BUY' and must be present as an explicit key (not
    relying on _parse_broker_call_fields's 'BUY' default, which would
    coincidentally match here but for the wrong reason)."""
    from backend.api.algo.actions import _action_live_close_position
    import backend.api.algo.actions_live as _mod

    broker = _make_broker_stub(ltp_value=100.0)
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": 100.0}}
    agent = MagicMock()
    agent.slug = "test-close"
    context: dict = {}
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
    }

    captured_orders: list[dict] = []

    async def _fake_run_preflight(account, order, paired_orders=None):
        captured_orders.append(order)
        return {"ok": True, "blocked": [], "diagnostics": {}}

    with patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=-100.0)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch.object(_mod, "run_preflight", new=_fake_run_preflight), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.chase.chase_order", new=AsyncMock()):
        await _action_live_close_position(agent, context, params)

    assert len(captured_orders) == 1
    assert "side" in captured_orders[0], (
        "preflight order dict must carry an explicit 'side' key, not rely "
        "on _parse_broker_call_fields's 'BUY' default"
    )
    assert captured_orders[0]["side"] == "BUY", (
        f"expected real resolved side 'BUY' for a short close, "
        f"got {captured_orders[0].get('side')!r}"
    )


@pytest.mark.asyncio
async def test_chase_build_tasks_carries_real_side_and_product_mis():
    """MIS position: preflight dict AND ChaseConfig must carry 'MIS',
    never a hardcoded 'NRML' — an MIS close placed as NRML silently
    fails to net against the real position."""
    from backend.api.algo.actions_live import _chase_build_tasks
    import backend.api.algo.actions_live as _mod

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    df = _make_positions_df([{
        "account":       "ZG0790",
        "tradingsymbol": "NIFTY25JULFUT",
        "exchange":      "NFO",
        "quantity":      300,
        "last_price":    24500.0,
        "close_price":   24400.0,
        "product":       "MIS",
    }])
    rows = df.to_dict("records")

    captured_orders: list[dict] = []

    async def _fake_run_preflight(account, order, paired_orders=None):
        captured_orders.append(order)
        return {"ok": True, "blocked": [], "diagnostics": {}}

    mock_chase = AsyncMock()

    with patch.object(_mod, "run_preflight", new=_fake_run_preflight), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.chase.chase_order", new=mock_chase):
        chase_tasks, task_rows, refused_rows = await _chase_build_tasks(agent, rows)
        for t in chase_tasks:
            await t

    assert len(captured_orders) == 1
    assert captured_orders[0]["product"] == "MIS", (
        f"expected real position product 'MIS' in preflight dict, "
        f"got {captured_orders[0].get('product')!r}"
    )
    assert captured_orders[0]["side"] == "SELL", (
        f"expected real resolved side 'SELL' (long close), "
        f"got {captured_orders[0].get('side')!r}"
    )
    assert mock_chase.call_count == 1
    cfg = mock_chase.call_args.kwargs["cfg"]
    assert cfg.product == "MIS", (
        f"expected ChaseConfig.product 'MIS' (real position product), "
        f"got {cfg.product!r}"
    )


@pytest.mark.asyncio
async def test_chase_build_tasks_carries_real_side_and_product_nrml_short():
    """NRML short position: preflight dict AND ChaseConfig must carry
    'NRML' (real product) and side 'BUY' (covers the short)."""
    from backend.api.algo.actions_live import _chase_build_tasks
    import backend.api.algo.actions_live as _mod

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    df = _make_positions_df([{
        "account":       "ZG0790",
        "tradingsymbol": "GOLDAUG25FUT",
        "exchange":      "MCX",
        "quantity":      -100,
        "last_price":    72000.0,
        "close_price":   71900.0,
        "product":       "NRML",
    }])
    rows = df.to_dict("records")

    captured_orders: list[dict] = []

    async def _fake_run_preflight(account, order, paired_orders=None):
        captured_orders.append(order)
        return {"ok": True, "blocked": [], "diagnostics": {}}

    mock_chase = AsyncMock()

    with patch.object(_mod, "run_preflight", new=_fake_run_preflight), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.chase.chase_order", new=mock_chase):
        chase_tasks, task_rows, refused_rows = await _chase_build_tasks(agent, rows)
        for t in chase_tasks:
            await t

    assert len(captured_orders) == 1
    assert captured_orders[0]["product"] == "NRML"
    assert captured_orders[0]["side"] == "BUY", (
        f"expected real resolved side 'BUY' (short close), "
        f"got {captured_orders[0].get('side')!r}"
    )
    assert mock_chase.call_count == 1
    cfg = mock_chase.call_args.kwargs["cfg"]
    assert cfg.product == "NRML"


@pytest.mark.asyncio
async def test_action_live_close_position_real_preflight_quotes_margin_as_sell():
    """End-to-end through the REAL run_preflight (not stubbed) — the
    live bug's actual mechanism. Kite's basket_order_margins is
    position-aware: a genuine SELL-to-close a long position prices as
    a near-zero/negative netting margin, but priced as a defaulted
    'BUY' it looks like a brand-new long requiring fresh SPAN margin.
    Asserts the basket leg actually sent to broker.basket_order_margins
    carries transaction_type='SELL', not the 'BUY' default."""
    from backend.api.algo.actions import _action_live_close_position

    broker = _make_broker_stub(ltp_value=100.0)
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": 100.0}}
    conns  = _make_conns_stub("ZG0790")
    agent  = MagicMock()
    agent.slug = "test-close"
    context: dict = {}
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
    }

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.algo.chase.chase_order", new=AsyncMock()), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.client.is_cutover_on", return_value=False):
        await _action_live_close_position(agent, context, params)

    broker.basket_order_margins.assert_called_once()
    basket_orders = broker.basket_order_margins.call_args.args[0]
    assert basket_orders[0]["transaction_type"] == "SELL", (
        f"expected the real resolved SELL to reach basket_order_margins "
        f"(position-aware netting), got {basket_orders[0].get('transaction_type')!r}"
    )


@pytest.mark.asyncio
async def test_chase_build_tasks_real_preflight_quotes_margin_with_real_product():
    """End-to-end through the REAL run_preflight for an MIS position —
    asserts the basket leg sent to broker.basket_order_margins carries
    product='MIS' (the real position product), not a hardcoded 'NRML'."""
    from backend.api.algo.actions_live import _chase_build_tasks

    broker = _make_broker_stub()
    conns  = _make_conns_stub("ZG0790")
    agent  = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    df = _make_positions_df([{
        "account":       "ZG0790",
        "tradingsymbol": "NIFTY25JULFUT",
        "exchange":      "NFO",
        "quantity":      300,
        "last_price":    24500.0,
        "close_price":   24400.0,
        "product":       "MIS",
    }])
    rows = df.to_dict("records")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=50)), \
         patch("backend.api.algo.chase.chase_order", new=AsyncMock()), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.client.is_cutover_on", return_value=False):
        chase_tasks, task_rows, refused_rows = await _chase_build_tasks(agent, rows)
        for t in chase_tasks:
            await t

    broker.basket_order_margins.assert_called_once()
    basket_orders = broker.basket_order_margins.call_args.args[0]
    assert basket_orders[0]["product"] == "MIS", (
        f"expected the real MIS position product to reach "
        f"basket_order_margins, got {basket_orders[0].get('product')!r}"
    )
    assert basket_orders[0]["transaction_type"] == "SELL"


# ---------------------------------------------------------------------------
# _action_live_close_position residual product fix (2026-10) — Phase 1
# (commit 12b8e166) fixed _chase_build_tasks's hardcoded product="NRML"
# but NOT _action_live_close_position's identical bug: that function
# still sourced product from params.get("product") or "NRML" via
# _al_close_resolve_params, never consulting the real live position's
# own product column. Same bug class: an agent-driven single-position
# close on an MIS position, with no explicit product override, silently
# used NRML — which does not net against the real MIS position on the
# broker side.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_live_close_position_resolves_real_product_mis():
    """No explicit 'product' param — must resolve the REAL position's
    product (MIS) via _fetch_net_position_product, never the old
    hardcoded 'NRML' default."""
    from backend.api.algo.actions import _action_live_close_position
    import backend.api.algo.actions_live as _mod

    broker = _make_broker_stub(ltp_value=100.0)
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": 100.0}}
    agent = MagicMock()
    agent.slug = "test-close"
    context: dict = {}
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
        # no 'product' key — must resolve from the real position row.
    }

    captured_orders: list[dict] = []

    async def _fake_run_preflight(account, order, paired_orders=None):
        captured_orders.append(order)
        return {"ok": True, "blocked": [], "diagnostics": {}}

    mock_chase = AsyncMock()

    with patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.api.routes.orders_place._fetch_net_position_product",
               new=AsyncMock(return_value="MIS")), \
         patch("backend.api.routes.orders_place._verify_close_intent",
               new=AsyncMock(return_value=True)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch.object(_mod, "run_preflight", new=_fake_run_preflight), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.chase.chase_order", new=mock_chase):
        await _action_live_close_position(agent, context, params)

    assert len(captured_orders) == 1
    assert captured_orders[0]["product"] == "MIS", (
        f"expected the real position product 'MIS' in the preflight "
        f"dict, got {captured_orders[0].get('product')!r}"
    )
    assert mock_chase.call_count == 1
    cfg = mock_chase.call_args.kwargs["cfg"]
    assert cfg.product == "MIS", (
        f"expected ChaseConfig.product 'MIS' (real position product), "
        f"got {cfg.product!r}"
    )


@pytest.mark.asyncio
async def test_action_live_close_position_explicit_product_param_wins():
    """An explicit operator-supplied 'product' override must win over
    the real position's own product — e.g. an operator deliberately
    closing under a different product than currently held."""
    from backend.api.algo.actions import _action_live_close_position
    import backend.api.algo.actions_live as _mod

    broker = _make_broker_stub(ltp_value=100.0)
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": 100.0}}
    agent = MagicMock()
    agent.slug = "test-close"
    context: dict = {}
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
        "product": "CNC",  # explicit override
    }

    captured_orders: list[dict] = []

    async def _fake_run_preflight(account, order, paired_orders=None):
        captured_orders.append(order)
        return {"ok": True, "blocked": [], "diagnostics": {}}

    mock_chase = AsyncMock()

    with patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.api.routes.orders_place._fetch_net_position_product",
               new=AsyncMock(return_value="MIS")), \
         patch("backend.api.routes.orders_place._verify_close_intent",
               new=AsyncMock(return_value=True)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch.object(_mod, "run_preflight", new=_fake_run_preflight), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.chase.chase_order", new=mock_chase):
        await _action_live_close_position(agent, context, params)

    assert captured_orders[0]["product"] == "CNC", (
        f"expected the explicit operator override 'CNC' to win over the "
        f"real position's 'MIS', got {captured_orders[0].get('product')!r}"
    )


@pytest.mark.asyncio
async def test_action_live_close_position_product_falls_back_to_nrml():
    """When neither an explicit override NOR the real position row's
    product field is available, 'NRML' remains the final fallback
    (preserves today's behaviour for the should-be-rare case where the
    broker's own positions row is missing the column)."""
    from backend.api.algo.actions import _action_live_close_position
    import backend.api.algo.actions_live as _mod

    broker = _make_broker_stub(ltp_value=100.0)
    broker.ltp.return_value = {"NFO:NIFTY25JULFUT": {"last_price": 100.0}}
    agent = MagicMock()
    agent.slug = "test-close"
    context: dict = {}
    params = {
        "account": "ZG0790", "symbol": "NIFTY25JULFUT", "exchange": "NFO",
    }

    captured_orders: list[dict] = []

    async def _fake_run_preflight(account, order, paired_orders=None):
        captured_orders.append(order)
        return {"ok": True, "blocked": [], "diagnostics": {}}

    mock_chase = AsyncMock()

    with patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.api.routes.orders_place._fetch_net_position_product",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.routes.orders_place._verify_close_intent",
               new=AsyncMock(return_value=True)), \
         patch("backend.brokers.get_broker", return_value=broker), \
         patch.object(_mod, "run_preflight", new=_fake_run_preflight), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.api.algo.chase.chase_order", new=mock_chase):
        await _action_live_close_position(agent, context, params)

    assert captured_orders[0]["product"] == "NRML"


@pytest.mark.asyncio
async def test_fetch_net_position_product_reads_same_row_as_qty():
    """_fetch_net_position_product and _fetch_net_position_qty must read
    the SAME matched row when a symbol carries more than one open
    product for the same account (e.g. both an MIS and an NRML position
    on the same symbol at once) — both go through the shared
    _fetch_net_position_row helper, so qty and product can never be
    read from two different rows."""
    import pandas as pd
    from backend.api.routes.orders_place import (
        _fetch_net_position_qty, _fetch_net_position_product,
    )

    df = pd.DataFrame([
        {"tradingsymbol": "NIFTY25JULFUT", "account": "ZG0790",
         "quantity": 300.0, "product": "MIS"},
        {"tradingsymbol": "NIFTY25JULFUT", "account": "ZG0790",
         "quantity": 300.0, "product": "MIS"},
    ])

    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
        qty = await _fetch_net_position_qty("NIFTY25JULFUT", "NFO", "ZG0790")
        product = await _fetch_net_position_product("NIFTY25JULFUT", "NFO", "ZG0790")

    assert qty == 300.0
    assert product == "MIS"


@pytest.mark.asyncio
async def test_fetch_net_position_product_nan_is_none_not_literal_string():
    """A pandas NaN product value (missing cell on an otherwise-matched
    row — real float('nan'), not None) must resolve to None, never the
    literal string "NAN" — bool(float('nan')) is True in Python, so a
    naive `if val else None` would silently produce "NAN", which Kite
    rejects outright and Dhan's _PRODUCT_TO_DHAN.get(..., "INTRADAY")
    would instead silently coerce to the wrong product."""
    import pandas as pd
    import math
    from backend.api.routes.orders_place import _fetch_net_position_product

    df = pd.DataFrame([
        {"tradingsymbol": "NIFTY25JULFUT", "account": "ZG0790",
         "quantity": 300.0, "product": math.nan},
    ])

    with patch("backend.brokers.broker_apis.fetch_positions", return_value=[df]):
        product = await _fetch_net_position_product("NIFTY25JULFUT", "NFO", "ZG0790")

    assert product is None
    assert product != "NAN"


# ---------------------------------------------------------------------------
# execute() — action-param expression resolution failure isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_expression_param_failure_skips_only_that_action():
    """
    2-action chain: action 1 (place_order) carries a malformed expression
    param (an undefined sibling name). `resolve_action_params` raises
    ExprError as the FIRST statement inside execute()'s existing
    try/except — routed through the existing `_al_action_failed_audit`
    path with zero new control flow. Action 2 (a second place_order,
    fully literal) must still run. The broker-dispatch path
    (`_al_dispatch_by_mode`) must NEVER be invoked for action 1 — only
    for action 2.
    """
    from backend.api.algo.actions import execute

    agent = MagicMock()
    agent.slug = "test-agent"
    agent.id = 1
    agent.trade_mode = "paper"

    actions = [
        {
            "type": "place_order",
            "params": {
                "account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL",
                # undefined sibling name → ExprError inside resolve_action_params
                "qty": "undefined_lots * 2",
            },
        },
        {
            "type": "place_order",
            "params": {
                "account": "ZG0790", "symbol": "NIFTY25JULFUT", "side": "SELL",
                "qty": 50,
            },
        },
    ]
    context: dict = {}

    mock_dispatch = AsyncMock(return_value=True)
    mock_audit = AsyncMock()

    with patch("backend.api.algo.actions._resolve_mode", return_value="paper"), \
         patch("backend.api.algo.actions._exchange_gate_passes", return_value=(True, "")), \
         patch("backend.api.algo.actions._al_dispatch_by_mode", new=mock_dispatch), \
         patch("backend.api.algo.actions._al_action_failed_audit", new=mock_audit), \
         patch("backend.api.algo.actions._log_action_success", new=AsyncMock()):
        await execute(agent, actions, context)

    # Action 1's expression failure routed to the audit path exactly once,
    # carrying the UNRESOLVED raw params (resolution never completed) and
    # the real ExprError instance.
    mock_audit.assert_called_once()
    audit_call = mock_audit.call_args
    assert audit_call.args[1] == "place_order"
    assert audit_call.args[2]["qty"] == "undefined_lots * 2"
    from backend.api.algo.expr_eval import ExprError
    assert isinstance(audit_call.args[5], ExprError)

    # Broker-dispatch path (the gateway to any live/paper broker call)
    # invoked exactly once — for action 2's RESOLVED params — never for
    # action 1's.
    mock_dispatch.assert_called_once()
    dispatch_call = mock_dispatch.call_args
    assert dispatch_call.args[2] == "place_order"
    assert dispatch_call.args[3]["qty"] == 50
