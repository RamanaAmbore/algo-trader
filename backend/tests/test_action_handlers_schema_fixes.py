"""
Tests for the 2026-10 audit fixes to action handlers in
backend/api/algo/actions_live.py that didn't honor their own declared
params_schema (backend/config/grammars/agent_grammar.yaml) — all three
are variants of the same root-cause class, now reachable via the CLI's
DO/$row fan-out mechanism with real financial risk:

  1. close_position — used to hard-default side="SELL" unconditionally
     and require quantity > 0, which could DOUBLE a short position.
     Fixed to look up the real live position, derive side from its
     actual sign, derive the full-close quantity when omitted, and
     refuse (not silently oversize) a quantity exceeding the held qty.
  2. cancel_all_orders — used to ignore its own scope/side/symbol
     params, cancelling every open order on an account instead of only
     matching ones.
  3. cancel_order — used to read only the legacy `order_id` key, so
     every schema-conformant agent (which saves `broker_order_id`)
     silently did nothing; and unconditionally overwrote AlgoOrder
     status to CANCELLED even on an already-FINAL row.

The project bans mocking of BROKER API calls (network-bound). Broker
adapters here (`broker.cancel_order`, `broker.orders`) are pure
in-process stubs, matching the precedent in test_agent_close_guards.py.
`backend.api.routes.orders_place._fetch_net_position_qty` is patched
for close_position tests — it's an internal verified-intent helper
(reads the cached broker-DataFrame), not a direct broker API call.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_conns_stub(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


def _make_broker_stub(*, margin_net: float = 500_000.0) -> MagicMock:
    broker = MagicMock()
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "BSE", "CDS"]}
    broker.instruments.return_value = []
    broker.basket_order_margins.return_value = [{"initial": {"total": 10_000.0}}]
    broker.margins.return_value = {
        "equity":    {"enabled": True, "net": margin_net},
        "commodity": {"enabled": True, "net": margin_net},
    }
    broker.normalise_qty.side_effect = lambda exchange, qty, lot_size: int(qty)
    return broker


def _make_agent(slug: str = "test-agent") -> MagicMock:
    agent = MagicMock()
    agent.slug = slug
    agent.id = 1
    return agent


# ---------------------------------------------------------------------------
# 1. close_position — derives side/qty from the real position
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_position_short_no_side_derives_buy_to_cover():
    """A real SHORT position (net_qty < 0) with no explicit `side` must
    derive BUY (to cover), never the old hardcoded SELL default — which
    would have DOUBLED the short."""
    from backend.api.algo.actions import _action_live_close_position

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
        # no 'side' and no 'quantity' — both derived from the real position
    }

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=-300.0)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_close_position(agent, context, params)

    mock_chase.assert_called_once()
    assert mock_chase.call_args.kwargs["transaction_type"] == "BUY"
    assert mock_chase.call_args.kwargs["quantity"] == 300


@pytest.mark.asyncio
async def test_close_position_no_quantity_flattens_full_position():
    """Omitting `quantity` on a real LONG position must flatten the full
    held quantity, not fail or default to some other size."""
    from backend.api.algo.actions import _action_live_close_position

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
        # no 'quantity' — must flatten the full held position
    }

    mock_chase = AsyncMock()

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=500.0)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order",
               new=AsyncMock(return_value=42)), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_close_position(agent, context, params)

    mock_chase.assert_called_once()
    assert mock_chase.call_args.kwargs["transaction_type"] == "SELL"
    assert mock_chase.call_args.kwargs["quantity"] == 500


@pytest.mark.asyncio
async def test_close_position_qty_exceeding_held_is_rejected_not_oversized():
    """A requested close quantity larger than the real held position must
    be REFUSED outright — never silently oversized/clamped and sent to
    the broker. chase_order must never be called."""
    from backend.api.algo.actions import _action_live_close_position

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
        "quantity": 1000,     # only 100 actually held
        "side":     "SELL",
    }

    mock_chase = AsyncMock()
    mock_write = AsyncMock(return_value=42)

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=100.0)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order", new=mock_write), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_close_position(agent, context, params)

    mock_chase.assert_not_called()
    write_calls = mock_write.call_args_list
    assert write_calls, "expected a REJECTED AlgoOrder write for the refused close"
    statuses = [c.kwargs.get("status") for c in write_calls]
    assert "REJECTED" in statuses


@pytest.mark.asyncio
async def test_close_position_no_live_position_found_is_rejected():
    """When the position lookup finds nothing (None) and no explicit
    quantity was given, the close must be refused — never guessed."""
    from backend.api.algo.actions import _action_live_close_position

    agent  = _make_agent()
    conns  = _make_conns_stub("ZG0790")
    broker = _make_broker_stub()
    context: dict = {}
    params = {
        "account":  "ZG0790",
        "symbol":   "CRUDEOILAUG25FUT",
        "exchange": "MCX",
    }

    mock_chase = AsyncMock()
    mock_write = AsyncMock(return_value=42)

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker",     return_value=broker), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.routes.orders_place._fetch_net_position_qty",
               new=AsyncMock(return_value=None)), \
         patch("backend.api.algo.chase.chase_order",      new=mock_chase), \
         patch("backend.api.algo.actions._write_live_order", new=mock_write), \
         patch("backend.brokers.get_broker",              return_value=broker), \
         patch("backend.brokers.client.is_cutover_on",    return_value=False):

        await _action_live_close_position(agent, context, params)

    mock_chase.assert_not_called()
    statuses = [c.kwargs.get("status") for c in mock_write.call_args_list]
    assert "REJECTED" in statuses


# ---------------------------------------------------------------------------
# 2. cancel_all_orders — honors scope/symbol/side
# ---------------------------------------------------------------------------

def _make_order(order_id, status="OPEN", tradingsymbol="NIFTY25JULFUT",
                 transaction_type="BUY", variety="regular"):
    return {
        "order_id": order_id, "status": status,
        "tradingsymbol": tradingsymbol, "transaction_type": transaction_type,
        "variety": variety,
    }


def _make_cancel_broker_stub(account: str, orders: list) -> MagicMock:
    broker = MagicMock()
    broker.account = account
    broker.orders.return_value = orders
    broker.cancel_order = MagicMock()
    return broker


@pytest.mark.asyncio
async def test_cancel_all_orders_symbol_filter_only_cancels_matching():
    """symbol= set must only cancel orders on that tradingsymbol, not
    every open order on the account."""
    from backend.api.algo.actions_live import _action_live_cancel_all_orders

    agent = _make_agent()
    orders = [
        _make_order("O1", tradingsymbol="NIFTY25JULFUT"),
        _make_order("O2", tradingsymbol="BANKNIFTY25JULFUT"),
    ]
    broker = _make_cancel_broker_stub("ZG0790", orders)
    params = {"scope": "account", "account": "ZG0790", "symbol": "NIFTY25JULFUT"}

    with patch("backend.brokers.registry.all_brokers", return_value=[broker]):
        await _action_live_cancel_all_orders(agent, {}, params)

    broker.cancel_order.assert_called_once()
    assert broker.cancel_order.call_args.args[0] == "O1"


@pytest.mark.asyncio
async def test_cancel_all_orders_side_filter_only_cancels_matching():
    """side= set must only cancel orders with that transaction_type."""
    from backend.api.algo.actions_live import _action_live_cancel_all_orders

    agent = _make_agent()
    orders = [
        _make_order("O1", transaction_type="BUY"),
        _make_order("O2", transaction_type="SELL"),
    ]
    broker = _make_cancel_broker_stub("ZG0790", orders)
    params = {"scope": "account", "account": "ZG0790", "side": "SELL"}

    with patch("backend.brokers.registry.all_brokers", return_value=[broker]):
        await _action_live_cancel_all_orders(agent, {}, params)

    broker.cancel_order.assert_called_once()
    assert broker.cancel_order.call_args.args[0] == "O2"


@pytest.mark.asyncio
async def test_cancel_all_orders_symbol_and_account_does_not_touch_other_account():
    """cancel_all_orders(scope=account, account=X, symbol=Y) must not
    cancel orders on an unrelated account at all (pre-existing account
    gate), AND must not cancel non-matching-symbol orders on account X."""
    from backend.api.algo.actions_live import _action_live_cancel_all_orders

    agent = _make_agent()
    broker_x = _make_cancel_broker_stub("ZG0790", [
        _make_order("O1", tradingsymbol="NIFTY25JULFUT"),
        _make_order("O2", tradingsymbol="OTHERSYM"),
    ])
    broker_y = _make_cancel_broker_stub("ZZ9999", [
        _make_order("O3", tradingsymbol="NIFTY25JULFUT"),
    ])
    params = {"scope": "account", "account": "ZG0790", "symbol": "NIFTY25JULFUT"}

    with patch("backend.brokers.registry.all_brokers",
               return_value=[broker_x, broker_y]):
        await _action_live_cancel_all_orders(agent, {}, params)

    broker_x.cancel_order.assert_called_once()
    assert broker_x.cancel_order.call_args.args[0] == "O1"
    broker_y.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_all_orders_scope_account_without_account_raises():
    """scope=account with no account value must refuse, not silently
    fall through to a fleet-wide cancel."""
    from backend.api.algo.actions_live import _action_live_cancel_all_orders

    agent = _make_agent()
    params = {"scope": "account"}

    with pytest.raises(ValueError):
        await _action_live_cancel_all_orders(agent, {}, params)


# ---------------------------------------------------------------------------
# 3. cancel_order — broker_order_id dual-key read + final-status guard
# ---------------------------------------------------------------------------

def _mock_session(rows):
    """Build an AsyncMock session whose execute().scalars().all() returns
    `rows` — mirrors the pattern in test_actions_live_modify_reject.py."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = rows

    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    return session


class _FakeOrderRow:
    def __init__(self, id_, status):
        self.id = id_
        self.status = status
        self.detail = None


@pytest.mark.asyncio
async def test_cancel_order_broker_order_id_only_succeeds():
    """params carrying ONLY `broker_order_id` (the schema-documented key,
    no `order_id` key at all) must succeed — the schema/legacy bug this
    fixes blocked every schema-conformant agent entirely."""
    from backend.api.algo.actions_live import _action_live_cancel_order

    agent  = _make_agent()
    broker = MagicMock()
    broker.cancel_order = MagicMock()
    row = _FakeOrderRow(1, "OPEN")
    session = _mock_session([row])

    params = {"account": "ZG0790", "broker_order_id": "KITE123"}

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.database.async_session", return_value=session):
        await _action_live_cancel_order(agent, {}, params)

    broker.cancel_order.assert_called_once()
    assert broker.cancel_order.call_args.args[0] == "KITE123"
    assert row.status == "CANCELLED"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_order_legacy_order_id_still_works():
    """Legacy `order_id` key (no `broker_order_id`) must still work."""
    from backend.api.algo.actions_live import _action_live_cancel_order

    agent  = _make_agent()
    broker = MagicMock()
    broker.cancel_order = MagicMock()
    row = _FakeOrderRow(2, "OPEN")
    session = _mock_session([row])

    params = {"account": "ZG0790", "order_id": "KITE456"}

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.database.async_session", return_value=session):
        await _action_live_cancel_order(agent, {}, params)

    broker.cancel_order.assert_called_once()
    assert broker.cancel_order.call_args.args[0] == "KITE456"
    assert row.status == "CANCELLED"


@pytest.mark.asyncio
async def test_cancel_order_missing_both_keys_raises():
    from backend.api.algo.actions_live import _action_live_cancel_order

    agent = _make_agent()
    params = {"account": "ZG0790"}

    with pytest.raises(ValueError):
        await _action_live_cancel_order(agent, {}, params)


@pytest.mark.asyncio
async def test_cancel_order_already_filled_row_not_overwritten():
    """A row already in a FINAL status (FILLED) must never be overwritten
    to CANCELLED by a late/duplicate cancel confirmation."""
    from backend.api.algo.actions_live import _action_live_cancel_order

    agent  = _make_agent()
    broker = MagicMock()
    broker.cancel_order = MagicMock()
    row = _FakeOrderRow(3, "FILLED")
    session = _mock_session([row])

    params = {"account": "ZG0790", "broker_order_id": "KITE789"}

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.database.async_session", return_value=session):
        await _action_live_cancel_order(agent, {}, params)

    assert row.status == "FILLED"
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_order_already_rejected_row_not_overwritten():
    from backend.api.algo.actions_live import _action_live_cancel_order

    agent  = _make_agent()
    broker = MagicMock()
    broker.cancel_order = MagicMock()
    row = _FakeOrderRow(4, "REJECTED")
    session = _mock_session([row])

    params = {"account": "ZG0790", "broker_order_id": "KITE999"}

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.database.async_session", return_value=session):
        await _action_live_cancel_order(agent, {}, params)

    assert row.status == "REJECTED"
    session.commit.assert_not_awaited()
