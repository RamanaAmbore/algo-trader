"""
NFO parent_lot_size resolution tests for the GTT template-attach layer.

P1 defect: apply_template_to_order only called get_lot_size() for MCX/NCO.
For NFO/BFO/CDS (NIFTY=75, BANKNIFTY=15, FINNIFTY=40, etc.) parent_lot_size
stayed 1, making the G1 lot-multiple guard in apply_plan_live dead code for
NFO GTT legs — sub-lot or misaligned GTT qty on NFO passed through unchecked.

Fix: exchange gate extended to ("MCX", "NCO", "NFO", "BFO", "CDS").

Five test dimensions:
  SSOT   — apply_template_to_order sets plan.parent_lot_size from get_lot_size()
  Perf   — get_lot_size called exactly once per apply_template_to_order call
  Stale  — old lot_size=1 path is dead for NFO (G1 guard now fires)
  Reuse  — same AttachResult.errors path used as MCX cache-miss
  UX     — G1 guard error message surfaces to caller when NFO qty misaligned
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.algo.template_attach import (
    AttachResult,
    TemplatePlan,
    WingSpec,
    apply_plan_live,
    resolve_template_plan,
)


# ── Shared overrides that force an ad-hoc template via build_adhoc_template
# (template_id=None + template_slug=None + has_any_override=True)

_NFO_OVERRIDES = {
    "tp_pct": 10.0,
    "sl_pct": 5.0,
    "wing_premium_pct": None,
    "wing_strike_offset": None,
}


def _make_mock_broker_nfo() -> MagicMock:
    """Return a mock broker whose translate_qty is a passthrough for NFO contracts."""
    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.place_gtt.return_value = "gtt-nfo-123"
    broker.place_order.return_value = "order-nfo-456"
    # NFO: Kite accepts raw contracts, not lots — passthrough
    broker.translate_qty.side_effect = lambda exch, qty, ls: qty
    return broker


# ── SSOT: parent_lot_size resolved from get_lot_size() for NFO ────────

@pytest.mark.asyncio
async def test_apply_template_to_order_nfo_resolves_lot_size():
    """apply_template_to_order must set plan.parent_lot_size=75 for NIFTY (NFO)."""
    from backend.api.algo.template_attach import apply_template_to_order

    with patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=75),
    ):
        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=_NFO_OVERRIDES,
            parent_account="ZG0790",
            parent_symbol="NIFTY25JULFUT",
            parent_side="BUY",
            parent_qty=75,
            parent_exchange="NFO",
            parent_fill_price=24000.0,
            apply_path="preview",
        )

    assert result is not None, "Expected AttachResult, got None"
    assert isinstance(result, AttachResult), (
        f"Expected AttachResult, got {type(result)}"
    )
    assert result.plan.parent_lot_size == 75, (
        f"Expected parent_lot_size=75 for NFO NIFTY, got {result.plan.parent_lot_size}"
    )


@pytest.mark.asyncio
async def test_apply_template_to_order_nfo_not_one():
    """Regression guard: parent_lot_size must NOT be 1 for NFO when get_lot_size returns 75."""
    from backend.api.algo.template_attach import apply_template_to_order

    with patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=75),
    ):
        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=_NFO_OVERRIDES,
            parent_account="ZG0790",
            parent_symbol="NIFTY25JULFUT",
            parent_side="BUY",
            parent_qty=75,
            parent_exchange="NFO",
            parent_fill_price=24000.0,
            apply_path="preview",
        )

    assert result is not None, "Expected AttachResult, got None"
    assert result.plan.parent_lot_size != 1, (
        "P1 regression: parent_lot_size=1 means NFO branch was NOT reached — "
        "G1 guard is dead for NFO GTT legs."
    )


# ── Perf: get_lot_size called exactly once ────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_nfo_get_lot_size_called_once():
    """get_lot_size must be called exactly once per apply_template_to_order invocation."""
    from backend.api.algo.template_attach import apply_template_to_order

    mock_get_lot_size = AsyncMock(return_value=75)
    with patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=mock_get_lot_size,
    ):
        await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=_NFO_OVERRIDES,
            parent_account="ZG0790",
            parent_symbol="NIFTY25JULFUT",
            parent_side="BUY",
            parent_qty=75,
            parent_exchange="NFO",
            parent_fill_price=24000.0,
            apply_path="preview",
        )

    assert mock_get_lot_size.call_count == 1, (
        f"Expected get_lot_size called once, got {mock_get_lot_size.call_count}"
    )


# ── Stale: G1 guard fires for NFO when lot_size is resolved ──────────

def test_g1_guard_fires_for_nfo_sub_lot_qty():
    """G1 guard must reject a GTT leg qty that is not a multiple of NFO lot_size=75."""
    _template = {
        "id": 10, "slug": "default-bull", "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 10.0, "sl_pct": 5.0,
        "wing_premium_pct": None, "wing_strike_offset": None,
        "tp_order_type": "LIMIT",
        "tp_scales_json": None,
        "sl_trail_pct": None,
    }
    plan = resolve_template_plan(
        _template, _NFO_OVERRIDES,
        parent_account="ZG0790",
        parent_symbol="NIFTY25JULFUT",
        parent_side="BUY",
        parent_qty=37,   # not a multiple of 75 — sub-lot
        parent_exchange="NFO",
        parent_fill_price=24000.0,
        parent_lot_size=75,
    )
    broker = _make_mock_broker_nfo()

    result = apply_plan_live(plan, broker)

    assert result.errors, "Expected G1 error for sub-lot NFO qty"
    assert any("G1 lot-multiple guard" in e for e in result.errors), (
        f"Error must reference G1 lot-multiple guard, got: {result.errors}"
    )
    assert not broker.place_gtt.called, (
        "place_gtt must not be called when G1 fires for NFO"
    )


def test_g1_guard_passes_for_nfo_exact_multiple():
    """G1 guard must pass for exact multiple of NFO lot_size=75."""
    _template = {
        "id": 10, "slug": "default-bull", "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 10.0, "sl_pct": 5.0,
        "wing_premium_pct": None, "wing_strike_offset": None,
        "tp_order_type": "LIMIT",
        "tp_scales_json": None,
        "sl_trail_pct": None,
    }
    plan = resolve_template_plan(
        _template, _NFO_OVERRIDES,
        parent_account="ZG0790",
        parent_symbol="NIFTY25JULFUT",
        parent_side="BUY",
        parent_qty=75,   # exactly 1 lot
        parent_exchange="NFO",
        parent_fill_price=24000.0,
        parent_lot_size=75,
    )
    broker = _make_mock_broker_nfo()

    result = apply_plan_live(plan, broker)

    assert not any("G1 lot-multiple guard" in e for e in result.errors), (
        f"G1 guard must not fire for exact multiple, got: {result.errors}"
    )
    assert broker.place_gtt.called, "place_gtt must be called for valid NFO qty"


# ── Reuse: cache-miss propagates as AttachResult error (same path as MCX) ──

@pytest.mark.asyncio
async def test_apply_template_to_order_nfo_cache_miss_refused():
    """lot_size=0 (cache miss) for NFO must return AttachResult with errors."""
    from backend.api.algo.template_attach import apply_template_to_order

    with patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=0),
    ):
        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=_NFO_OVERRIDES,
            parent_account="ZG0790",
            parent_symbol="NIFTY25JULFUT",
            parent_side="BUY",
            parent_qty=75,
            parent_exchange="NFO",
            parent_fill_price=24000.0,
            apply_path="preview",
        )

    assert isinstance(result, AttachResult), (
        f"Expected AttachResult on cache miss, got {type(result)}"
    )
    assert result.errors, (
        "Expected errors in AttachResult for NFO cache miss (lot_size=0)"
    )
    assert any("GTT-QTY-GUARD" in e for e in result.errors), (
        f"Error must reference GTT-QTY-GUARD, got: {result.errors}"
    )


# ── UX: BFO and CDS also resolve lot_size (gate completeness) ────────

@pytest.mark.asyncio
@pytest.mark.parametrize("exchange,symbol,lot_size", [
    ("BFO", "SENSEX25JULFUT", 10),
    ("CDS", "USDINR25JULFUT", 1000),
])
async def test_apply_template_to_order_resolves_lot_size_bfo_cds(
    exchange: str, symbol: str, lot_size: int
):
    """BFO and CDS exchanges must also resolve lot_size via get_lot_size()."""
    from backend.api.algo.template_attach import apply_template_to_order

    with patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=lot_size),
    ):
        result = await apply_template_to_order(
            template_id=None,
            template_slug=None,
            overrides=_NFO_OVERRIDES,
            parent_account="ZG0790",
            parent_symbol=symbol,
            parent_side="BUY",
            parent_qty=lot_size,
            parent_exchange=exchange,
            parent_fill_price=100.0,
            apply_path="preview",
        )

    assert isinstance(result, AttachResult), (
        f"Expected AttachResult for {exchange}, got {type(result)}"
    )
    assert result.plan.parent_lot_size == lot_size, (
        f"Expected parent_lot_size={lot_size} for {exchange}/{symbol}, "
        f"got {result.plan.parent_lot_size}"
    )
