"""
Tests for qty guard enforcement on agent-driven close paths.

Covers:
  - _action_live_close_position: G1 blocks sub-lot MCX qty; G2 bypassed
    for legitimate large-lot close; chase_order never called on block.
  - _action_live_chase_close_positions: per-position preflight; blocked
    position skips chase but other positions in same loop still proceed.
  - _arm_take_profit: G1 blocks sub-lot TP placement on live path.

The project bans mocking of BROKER API calls (network-bound).
`chase_order` is internal, so it IS patched to isolate broker contact.
`get_lot_size` is patched to avoid the instruments-cache hit.
`run_preflight` is NOT patched — these tests verify the real guard fires.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_conns_stub(account: str) -> MagicMock:
    """Connections stub that reports `account` as loaded."""
    c = MagicMock()
    c.conn = {account: object()}
    return c


def _make_broker_stub(*, margin_net: float = 500_000.0) -> MagicMock:
    """Broker stub with enough margin to pass all preflight checks."""
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [
        {"initial": {"total": 10_000.0}}
    ]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": margin_net},
        "commodity": {"enabled": True, "net": margin_net},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _make_agent(slug: str = "test-agent") -> MagicMock:
    agent = MagicMock()
    agent.slug = slug
    agent.id   = 1
    return agent


# ---------------------------------------------------------------------------
# _action_live_close_position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_sub_lot_mcx_blocked():
    """
    Sub-lot qty=50 for CRUDEOIL (lot_size=100) must trigger G1 (LOT_MULTIPLE).
    chase_order must NOT be called.
    """
    from backend.api.algo.actions import _action_live_close_position

    agent   = _make_agent()
    conns   = _make_conns_stub("ZG0790")
    broker  = _make_broker_stub()
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
        "quantity": 50,       # sub-lot — CRUDEOIL lot_size=100
        "side":     "SELL",
    }

    mock_chase = AsyncMock()
    mock_write = AsyncMock(return_value=42)

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order", new=mock_write), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_close_position(agent, context, params)

    mock_chase.assert_not_called()
    # Must have written a REJECTED row (not OPEN).
    write_calls = mock_write.call_args_list
    assert write_calls, "expected _write_live_order call for the blocked row"
    # Positional: (agent, action_type, resolved_dict, ...) + kw status=
    statuses = [c.kwargs.get("status") for c in write_calls]
    assert "REJECTED" in statuses, (
        f"expected REJECTED in write statuses; got {statuses}"
    )


@pytest.mark.asyncio
async def test_close_position_valid_3_lot_mcx_passes():
    """
    Legit 3-lot close (qty=300, lot_size=100) — G1 passes, G2 bypassed
    by intent='close'. chase_order must be called.
    """
    from backend.api.algo.actions import _action_live_close_position

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
        "quantity": 300,      # 3 lots of 100 — valid
        "side":     "SELL",
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

    mock_chase.assert_called_once()


@pytest.mark.asyncio
async def test_close_position_6_lot_mcx_passes_g2_bypassed():
    """
    6-lot close (qty=600, lot_size=100) exceeds the 5-lot G2 cap, but
    G2 MUST be bypassed for closes. chase_order must be called.
    """
    from backend.api.algo.actions import _action_live_close_position

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
        "quantity": 600,      # 6 lots — exceeds 5-lot G2 cap; bypass must apply
        "side":     "SELL",
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

    mock_chase.assert_called_once()


# ---------------------------------------------------------------------------
# _action_live_chase_close_positions
# ---------------------------------------------------------------------------

def _make_positions_df(rows: list[dict]):
    """Build a minimal pandas DataFrame mirroring df_positions shape."""
    import pandas as pd
    return pd.DataFrame(rows)


@pytest.mark.asyncio
async def test_chase_close_positions_sub_lot_blocked():
    """
    Single sub-lot MCX position — preflight blocks; chase_order NOT called.
    """
    from backend.api.algo.actions import _action_live_chase_close_positions

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    df = _make_positions_df([{
        "account":       "ZG0790",
        "tradingsymbol": "CRUDEOILAUG25FUT",
        "exchange":      "MCX",
        "quantity":      50,   # sub-lot
        "last_price":    7500.0,
        "close_price":   7450.0,
    }])
    context = {"df_positions": df}
    params  = {}

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_chase_close_positions(agent, context, params)

    mock_chase.assert_not_called()


@pytest.mark.asyncio
async def test_chase_close_positions_valid_3_lot_passes():
    """
    3-lot MCX close (qty=300) passes G1; G2 bypassed via intent='close'.
    chase_order fired once.
    """
    from backend.api.algo.actions import _action_live_chase_close_positions

    agent  = _make_agent()
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

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_chase_close_positions(agent, context, params)

    mock_chase.assert_called_once()


@pytest.mark.asyncio
async def test_chase_close_positions_6_lot_passes_g2_bypassed():
    """
    6-lot MCX close (qty=600) — G2 cap bypassed for closes; chase fires.
    """
    from backend.api.algo.actions import _action_live_chase_close_positions

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    df = _make_positions_df([{
        "account":       "ZG0790",
        "tradingsymbol": "CRUDEOILAUG25FUT",
        "exchange":      "MCX",
        "quantity":      600,
        "last_price":    7500.0,
        "close_price":   7450.0,
    }])
    context = {"df_positions": df}
    params  = {}

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_chase_close_positions(agent, context, params)

    mock_chase.assert_called_once()


@pytest.mark.asyncio
async def test_chase_close_positions_mixed_blocked_and_valid():
    """
    Two positions — sub-lot CRUDEOIL (blocked) + valid GOLD (passes).
    Loop must NOT abort early; only the valid position reaches chase_order.
    """
    from backend.api.algo.actions import _action_live_chase_close_positions

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    df = _make_positions_df([
        {
            "account":       "ZG0790",
            "tradingsymbol": "CRUDEOILAUG25FUT",
            "exchange":      "MCX",
            "quantity":      50,    # sub-lot — must be blocked
            "last_price":    7500.0,
            "close_price":   7450.0,
        },
        {
            "account":       "ZG0790",
            "tradingsymbol": "GOLDAUG25FUT",
            "exchange":      "MCX",
            "quantity":      100,   # 1 lot of 100 — valid
            "last_price":    72000.0,
            "close_price":   71900.0,
        },
    ])
    context = {"df_positions": df}
    params  = {}

    mock_chase = AsyncMock()

    # Both CRUDEOIL and GOLD have lot_size=100.
    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_chase_close_positions(agent, context, params)

    # Exactly ONE chase task (GOLD); CRUDEOIL was blocked.
    assert mock_chase.call_count == 1, (
        f"expected 1 chase call (GOLD only), got {mock_chase.call_count}"
    )
    called_symbol = mock_chase.call_args.kwargs.get("symbol", "")
    assert "GOLD" in called_symbol, (
        f"expected chase for GOLD, got symbol={called_symbol!r}"
    )


# ---------------------------------------------------------------------------
# _arm_take_profit — G1 guard on live TP path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_arm_take_profit_sub_lot_g1_blocks():
    """
    Auto-arm TP with corrupt sub-lot qty (qty=50, lot_size=100) on MCX.
    G1 must log and return before broker.place_order is called.
    """
    from backend.api.routes.orders import _arm_take_profit

    mock_broker = MagicMock()
    mock_broker.place_order.return_value = "ORDER123"
    mock_broker.translate_qty.side_effect = (
        lambda exc, qty, ls: qty // ls if ls and ls > 1 else qty
    )

    # Simulate DB: no existing child rows, then parent row with qty=50.
    mock_parent = MagicMock()
    mock_parent.quantity = 50   # sub-lot
    mock_parent.id = 999

    _existing_result     = MagicMock()
    _existing_result.scalar_one.return_value = 0   # no existing TP child

    _parent_result       = MagicMock()
    _parent_result.scalar_one_or_none.return_value = mock_parent

    _tp_row = MagicMock()
    _tp_row.id = 888

    execute_returns = iter([_existing_result, _parent_result])

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__  = AsyncMock(return_value=False)
    mock_session.execute    = AsyncMock(side_effect=lambda *a, **kw: next(execute_returns))
    mock_session.add        = MagicMock()
    mock_session.commit     = AsyncMock()

    with patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.routes.orders._broker_for",
               return_value=mock_broker), \
         patch("backend.api.database.async_session",
               return_value=mock_session):

        await _arm_take_profit(
            parent_row_id=999,
            parent_account="ZG0790",
            parent_symbol="CRUDEOILAUG25FUT",
            parent_exchange="MCX",
            parent_side="BUY",
            fill_price=7500.0,
            target_pct=0.02,
            target_abs=None,
            parent_mode="live",
        )

    mock_broker.place_order.assert_not_called()
