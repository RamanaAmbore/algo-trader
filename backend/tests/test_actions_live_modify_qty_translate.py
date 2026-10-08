"""
Regression test for the 2026-10 fix: the live `modify_order` agent action
sent `quantity` straight to `broker.modify_order()` with a comment
claiming this was deliberate ("supply Kite qty"), with NO
`translate_qty` call and NO G1 lot-multiple preflight check.
`order_fields.yaml` documents the `qty` field as lots × lot_size
(contracts) — an agent author following that documentation on MCX/NCO
would have their contracts value read as LOTS by Kite/Dhan, causing an
N× oversize modify. Exact same trap as CLAUDE.md's "Option qty vs
lot_size" math guard.

Fix: `backend/api/algo/actions_live.py`
  - `_al_modify_fetch_order_meta` resolves (exchange, tradingsymbol) from
    the AlgoOrder row by `broker_order_id` (replaces the exchange-only
    `_al_modify_fetch_exchange`).
  - `_al_modify_resolve_qty` runs the G1 lot-multiple check uniformly
    across every F&O exchange, then calls `broker.translate_qty` —
    mirroring every other order-placing path in this codebase — and
    fails closed when exchange/symbol/lot_size can't be resolved.
  - `_action_live_modify_order` only runs this when a quantity is
    actually being modified; price/trigger-only modifies are unaffected.
  - `_al_modify_build_kwargs` now also accepts the `modify_order`
    params_schema's documented keys (`new_qty`/`new_price`/
    `new_trigger`, `broker_order_id`), checked first, falling back to
    the original internal keys (`quantity`/`price`/`trigger_price`,
    `order_id`) for already-authored agents.

Uses `create_autospec(KiteConnect, instance=True)` (same pattern as
`TestKiteBrokerModifyOrder` in test_kite_adapter.py) so the mock
enforces the REAL installed SDK's signature — a hand-rolled fake would
silently accept any kwarg and hide the exact double-translation /
missing-kwarg bug classes this project has hit before.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from kiteconnect import KiteConnect

from backend.brokers.adapters.kite import KiteBroker
from backend.api.algo.actions_live import (
    _action_live_modify_order,
    _al_modify_build_kwargs,
    _al_modify_resolve_qty,
)


def _make_kite_broker():
    mock_conn = MagicMock()
    mock_kite = create_autospec(KiteConnect, instance=True)
    mock_kite.modify_order.return_value = "order_mod_1"
    mock_conn.get_kite_conn = MagicMock(return_value=mock_kite)
    broker = KiteBroker(mock_conn)
    return broker, mock_kite


# ---------------------------------------------------------------------------
# _al_modify_build_kwargs — schema-key-first, legacy-key fallback
# ---------------------------------------------------------------------------

def test_build_kwargs_prefers_schema_documented_keys():
    kwargs = _al_modify_build_kwargs({
        "new_qty": 200, "new_price": 105.5, "new_trigger": 100.0,
    })
    assert kwargs == {"quantity": 200, "price": 105.5, "trigger_price": 100.0}


def test_build_kwargs_falls_back_to_legacy_keys_when_schema_keys_absent():
    kwargs = _al_modify_build_kwargs({
        "quantity": 50, "price": 20.0, "trigger_price": 19.5,
    })
    assert kwargs == {"quantity": 50, "price": 20.0, "trigger_price": 19.5}


def test_build_kwargs_schema_key_wins_when_both_present():
    kwargs = _al_modify_build_kwargs({"new_qty": 200, "quantity": 999})
    assert kwargs["quantity"] == 200


def test_build_kwargs_order_type_and_validity_unchanged():
    kwargs = _al_modify_build_kwargs({
        "order_type": "LIMIT", "validity": "DAY",
    })
    assert kwargs == {"order_type": "LIMIT", "validity": "DAY"}


# ---------------------------------------------------------------------------
# _al_modify_resolve_qty — G1 + translate_qty, unit level
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolve_qty_mcx_translates_contracts_to_lots():
    broker, _mock_kite = _make_kite_broker()
    with patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)):
        result = await _al_modify_resolve_qty(broker, 200, "MCX", "CRUDEOIL25OCTFUT")
    assert result == 2


@pytest.mark.asyncio
async def test_resolve_qty_nfo_non_multiple_raises_g1():
    """NFO (not MCX/NCO) — only the NEW G1 check catches this; MCX's own
    translate_qty guard would not fire for a non-MCX exchange."""
    broker, _mock_kite = _make_kite_broker()
    with patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=75)):
        with pytest.raises(ValueError, match="G1 lot-multiple"):
            await _al_modify_resolve_qty(broker, 100, "NFO", "NIFTY25JULFUT")


@pytest.mark.asyncio
async def test_resolve_qty_equity_lot_size_one_is_noop():
    broker, _mock_kite = _make_kite_broker()
    with patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=1)):
        result = await _al_modify_resolve_qty(broker, 37, "NSE", "RELIANCE")
    assert result == 37


@pytest.mark.asyncio
async def test_resolve_qty_missing_symbol_fails_closed():
    broker, _mock_kite = _make_kite_broker()
    with pytest.raises(RuntimeError, match="cannot resolve exchange/symbol"):
        await _al_modify_resolve_qty(broker, 100, "NFO", None)


@pytest.mark.asyncio
async def test_resolve_qty_lot_size_cache_miss_fails_closed():
    broker, _mock_kite = _make_kite_broker()
    with patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=0)):
        with pytest.raises(RuntimeError, match="lot_size unresolved"):
            await _al_modify_resolve_qty(broker, 100, "NFO", "UNKNOWNSYM")


# ---------------------------------------------------------------------------
# _action_live_modify_order — end to end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_modify_order_mcx_qty_translated_before_broker_call():
    """Exact scenario from the defect report: an agent modifies an MCX
    order using the documented lots×lot_size contracts convention
    (qty=200, lot_size=100 → 2 lots) via the schema key `new_qty`."""
    broker, mock_kite = _make_kite_broker()
    params = {
        "account": "ZG0790",
        "broker_order_id": "251001000000010",
        "new_qty": 200,
    }
    agent = MagicMock()
    agent.slug = "test-agent"

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.algo.actions_live._al_modify_fetch_order_meta",
               new=AsyncMock(return_value=("MCX", "CRUDEOIL25OCTFUT"))), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)):
        await _action_live_modify_order(agent, {}, params)

    mock_kite.modify_order.assert_called_once_with(
        order_id="251001000000010", variety="regular", quantity=2,
    )


@pytest.mark.asyncio
async def test_modify_order_nfo_non_multiple_qty_rejected_before_broker_call():
    """NFO, lot_size=75, quantity=100 (not a multiple) — G1 must refuse
    the modify and the broker must NEVER be called."""
    broker, mock_kite = _make_kite_broker()
    params = {
        "account": "ZG0790",
        "broker_order_id": "251001000000011",
        "new_qty": 100,
    }
    agent = MagicMock()
    agent.slug = "test-agent"

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.algo.actions_live._al_modify_fetch_order_meta",
               new=AsyncMock(return_value=("NFO", "NIFTY25JULFUT"))), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=75)), \
         patch("backend.api.algo.actions_live._al_modify_write_reject",
               new=AsyncMock()) as mock_reject:
        with pytest.raises(ValueError, match="G1 lot-multiple"):
            await _action_live_modify_order(agent, {}, params)

    mock_kite.modify_order.assert_not_called()
    mock_reject.assert_awaited_once()


@pytest.mark.asyncio
async def test_modify_order_price_only_unaffected_by_qty_guard():
    """No quantity key present at all — the new G1/translate_qty path
    must never run, and lot_size is never even looked up."""
    broker, mock_kite = _make_kite_broker()
    params = {
        "account": "ZG0790",
        "broker_order_id": "251001000000012",
        "new_price": 105.5,
    }
    agent = MagicMock()
    agent.slug = "test-agent"
    mock_get_lot_size = AsyncMock(return_value=100)

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.algo.actions_live._al_modify_fetch_order_meta",
               new=AsyncMock(return_value=("MCX", "CRUDEOIL25OCTFUT"))), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=mock_get_lot_size):
        await _action_live_modify_order(agent, {}, params)

    mock_get_lot_size.assert_not_called()
    mock_kite.modify_order.assert_called_once_with(
        order_id="251001000000012", variety="regular", price=105.5,
    )


@pytest.mark.asyncio
async def test_modify_order_resolves_order_id_from_broker_order_id_key():
    """Schema-documented `broker_order_id` key resolves the order_id
    (not just the legacy `order_id` key)."""
    broker, mock_kite = _make_kite_broker()
    params = {"account": "ZG0790", "broker_order_id": "251001000000013"}
    agent = MagicMock()
    agent.slug = "test-agent"

    with patch("backend.brokers.get_broker", return_value=broker), \
         patch("backend.api.algo.actions_live._al_modify_fetch_order_meta",
               new=AsyncMock(return_value=(None, None))):
        await _action_live_modify_order(agent, {}, params)

    mock_kite.modify_order.assert_called_once_with(
        order_id="251001000000013", variety="regular",
    )
