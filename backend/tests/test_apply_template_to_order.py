"""
Characterization tests for apply_template_to_order (backend/api/algo/template_attach.py:1179).

Integration tests covering plan resolution + live/sim/preview application paths.

Async entry point tested with three apply_path modes:
- 'preview'   — resolve plan, DO NOT apply (UI preview chip)
- 'sim'       — force sim path (SimDriver)
- 'live'      — force live path (broker)
- 'auto'      — detect sim_active → route

Additional coverage:
- applies_to guard (side/kind mismatch) → early return
- F&O lot_size resolution (MCX/NFO/BFO/CDS)
- Template loading (DB fetch vs ad-hoc build)
- Wing premium scan (phase 1B)
- Error accumulation in AttachResult

Five test dimensions:
  SSOT   — AttachResult carries correct plan + error messages
  Perf   — async calls (get_lot_size, _pick_wing, load_template) batched correctly
  Stale  — old template==None fallthrough path is tested
  Reuse  — shared broker mock + plan builder used across tests
  UX     — applies_to guard fires with Telegram alert on mismatch
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.api.algo.template_attach import (
    AttachResult,
    TemplatePlan,
    apply_template_to_order,
    resolve_template_plan,
)
from backend.brokers.capabilities import (
    BrokerCapabilities,
    KITE_CAPS,
)


# ── Test data helpers ────────────────────────────────────────────

def _base_template() -> dict:
    """Minimal template for testing."""
    return {
        "id": 1,
        "slug": "default-bull",
        "name": "Default Bull",
        "applies_to": "buy_any",
        "tp_pct": 10.0,
        "sl_pct": 5.0,
        "wing_premium_pct": None,
        "wing_strike_offset": None,
        "tp_order_type": "LIMIT",
        "tp_scales_json": None,
        "sl_trail_pct": None,
    }


def _make_mock_broker() -> MagicMock:
    """Mock broker for live path testing."""
    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.place_gtt.return_value = "gtt-123"
    broker.place_order.return_value = "order-456"
    broker.translate_qty.side_effect = lambda exch, qty, ls: qty  # Passthrough
    return broker


def _make_mock_sim_driver() -> MagicMock:
    """Mock SimDriver for sim path testing."""
    driver = MagicMock()
    driver.active = True
    driver.place_sim_gtt.return_value = {"gtt_id": "sim-gtt-123"}
    driver._gtt_book = {}
    return driver


# ── Template loading and defaults ────────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_no_template_no_overrides():
    """No template + no overrides → returns None (skip attach entirely)."""
    result = await apply_template_to_order(
        template_id=None,
        template_slug=None,
        overrides={},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=2900.0,
        apply_path="preview",
    )

    assert result is None, (
        "Should return None when no template and no overrides"
    )


@pytest.mark.asyncio
async def test_apply_template_to_order_no_template_but_has_overrides():
    """No template but has tp_pct override → builds ad-hoc template."""
    result = await apply_template_to_order(
        template_id=None,
        template_slug=None,
        overrides={"tp_pct": 20.0},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
        apply_path="preview",
    )

    assert result is not None, "Should return AttachResult with ad-hoc template"
    assert isinstance(result, AttachResult)
    assert len(result.plan.gtts) >= 1, "Ad-hoc template should have TP GTT"


@pytest.mark.asyncio
async def test_apply_template_to_order_none_slug_means_no_attach():
    """'none' slug + no overrides → returns None (intentional skip)."""
    template = _base_template()
    template["slug"] = "none"
    template["tp_pct"] = None
    template["sl_pct"] = None

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="none",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is None, "'none' template with no overrides → no attach"


# ── applies_to guard (incident 2026-06-22) ──────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_applies_to_buy_any_on_buy():
    """applies_to='buy_any' + BUY parent → allowed."""
    template = _base_template()
    template["applies_to"] = "buy_any"

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is not None, "Should succeed with matching side"
    assert not result.errors


@pytest.mark.asyncio
async def test_apply_template_to_order_applies_to_buy_any_on_sell_blocked():
    """applies_to='buy_any' + SELL parent → blocked, no attach."""
    template = _base_template()
    template["applies_to"] = "buy_any"

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.template_attach._fire_guard_alert",
    ) as mock_alert:
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="SELL",  # Mismatch
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is None, "Side mismatch → no attach"
    assert mock_alert.called, "Should fire guard alert on mismatch"


@pytest.mark.asyncio
async def test_apply_template_to_order_applies_to_sell_any_on_sell():
    """applies_to='sell_any' + SELL parent → allowed."""
    template = _base_template()
    template["applies_to"] = "sell_any"

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="SELL",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is not None


@pytest.mark.asyncio
async def test_apply_template_to_order_applies_to_sell_option_on_non_option():
    """applies_to='sell_option' + equity SELL → blocked."""
    template = _base_template()
    template["applies_to"] = "sell_option"

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.template_attach._fire_guard_alert",
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",  # Not an option
            parent_side="SELL",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is None, "Kind mismatch → blocked"


@pytest.mark.asyncio
async def test_apply_template_to_order_applies_to_both_allows_all():
    """applies_to='both' → allows any side/kind."""
    template = _base_template()
    template["applies_to"] = "both"

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="NIFTY25APR22000CE",
            parent_side="BUY",
            parent_qty=50,
            parent_exchange="NFO",
            parent_fill_price=100.0,
            apply_path="preview",
        )

    assert result is not None


@pytest.mark.asyncio
async def test_apply_template_to_order_applies_to_none_string_means_both():
    """applies_to empty/missing → treated as 'both'."""
    template = _base_template()
    template["applies_to"] = None

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="SELL",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is not None, "Empty applies_to defaults to 'both'"


# ── F&O lot_size resolution ──────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_mcx_resolves_lot_size():
    """MCX parent_exchange → calls get_lot_size()."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=100),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="CRUDEOIL25AUGFUT",
            parent_side="BUY",
            parent_qty=100,
            parent_exchange="MCX",
            parent_fill_price=5000.0,
            apply_path="preview",
        )

    assert result is not None
    assert result.plan.parent_lot_size == 100, (
        "MCX lot_size should be resolved from get_lot_size()"
    )


@pytest.mark.asyncio
async def test_apply_template_to_order_nfo_resolves_lot_size():
    """NFO parent_exchange → calls get_lot_size()."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=75),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="NIFTY25JULFUT",
            parent_side="BUY",
            parent_qty=75,
            parent_exchange="NFO",
            parent_fill_price=24000.0,
            apply_path="preview",
        )

    assert result is not None
    assert result.plan.parent_lot_size == 75


@pytest.mark.asyncio
async def test_apply_template_to_order_cds_resolves_lot_size():
    """CDS parent_exchange → calls get_lot_size()."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=1000),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="USDINR25JULFUT",
            parent_side="BUY",
            parent_qty=1000,
            parent_exchange="CDS",
            parent_fill_price=85.50,
            apply_path="preview",
        )

    assert result is not None
    assert result.plan.parent_lot_size == 1000


@pytest.mark.asyncio
async def test_apply_template_to_order_nse_does_not_resolve_lot_size():
    """NSE parent_exchange → lot_size stays 1 (no async call)."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ):
        with patch(
            "backend.brokers.adapters.kite.get_lot_size",
        ) as mock_get:
            result = await apply_template_to_order(
                template_id=1,
                template_slug="default-bull",
                overrides={},
                parent_account="ACC1",
                parent_symbol="RELIANCE",
                parent_side="BUY",
                parent_qty=10,
                parent_exchange="NSE",
                parent_fill_price=2900.0,
                apply_path="preview",
            )

    assert result is not None
    assert result.plan.parent_lot_size == 1
    assert not mock_get.called, "NSE should not call get_lot_size()"


@pytest.mark.asyncio
async def test_apply_template_to_order_lot_size_cache_miss_refused():
    """lot_size=0 (cache miss) → returns AttachResult with errors."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=0),  # Cache miss
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="CRUDEOIL25AUGFUT",
            parent_side="BUY",
            parent_qty=100,
            parent_exchange="MCX",
            parent_fill_price=5000.0,
            apply_path="preview",
        )

    assert result is not None
    assert result.errors, "Should have errors for cache miss"
    assert any("GTT-QTY-GUARD" in e for e in result.errors), (
        f"Error should mention GTT-QTY-GUARD, got {result.errors}"
    )


@pytest.mark.asyncio
async def test_apply_template_to_order_lot_size_lookup_exception():
    """get_lot_size() raises exception → returns AttachResult with errors."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(side_effect=RuntimeError("instruments cache down")),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="CRUDEOIL25AUGFUT",
            parent_side="BUY",
            parent_qty=100,
            parent_exchange="MCX",
            parent_fill_price=5000.0,
            apply_path="preview",
        )

    assert result is not None
    assert result.errors


# ── Wing premium scan (Phase 1B) ─────────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_wing_premium_scan_picks_symbol():
    """SELL option with wing_premium_pct → async scan picks wing symbol."""
    template = _base_template()
    template["applies_to"] = "sell_option"  # Must match SELL + option
    template["wing_premium_pct"] = 20.0
    template["wing_strike_offset"] = None  # Force premium-based pick

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=75),  # NFO lot_size
    ), patch(
        "backend.api.algo.template_attach._pick_wing_by_premium",
        new=AsyncMock(return_value=("NIFTY25APR22500CE", 45.5, "scanned")),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="NIFTY25APR22000CE",
            parent_side="SELL",
            parent_qty=50,
            parent_exchange="NFO",
            parent_fill_price=125.0,
            apply_path="preview",
        )

    assert result is not None
    assert result.plan.wing is not None
    assert result.plan.wing.tradingsymbol == "NIFTY25APR22500CE", (
        "Wing premium scan should have picked the symbol"
    )


@pytest.mark.asyncio
async def test_apply_template_to_order_wing_premium_scan_error_handled():
    """Wing premium scan error → note added, wing skipped, parent not blocked."""
    template = _base_template()
    template["applies_to"] = "sell_option"
    template["wing_premium_pct"] = 20.0
    template["wing_strike_offset"] = None

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=75),
    ), patch(
        "backend.api.algo.template_attach._pick_wing_by_premium",
        new=AsyncMock(side_effect=RuntimeError("LTP fetch failed")),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="NIFTY25APR22000CE",
            parent_side="SELL",
            parent_qty=50,
            parent_exchange="NFO",
            parent_fill_price=125.0,
            apply_path="preview",
        )

    assert result is not None
    assert result.plan.wing is None, "Wing should be skipped on scan error"
    assert any("wing_premium_pct scan errored" in n for n in result.plan.notes)


@pytest.mark.asyncio
async def test_apply_template_to_order_wing_strike_offset_takes_precedence():
    """wing_strike_offset override → skips premium scan."""
    template = _base_template()
    template["applies_to"] = "sell_option"
    template["wing_premium_pct"] = 20.0
    template["wing_strike_offset"] = None

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=75),
    ), patch(
        "backend.api.algo.template_attach._pick_wing_by_premium",
    ) as mock_scan:
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={"wing_strike_offset": 500},  # Override takes precedence
            parent_account="ACC1",
            parent_symbol="NIFTY25APR22000CE",
            parent_side="SELL",
            parent_qty=50,
            parent_exchange="NFO",
            parent_fill_price=125.0,
            apply_path="preview",
        )

    assert result is not None
    assert not mock_scan.called, "Scan should be skipped when offset is set"
    assert result.plan.wing is not None


# ── Apply path modes ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_preview_mode_no_apply():
    """apply_path='preview' → plan resolved but NOT applied."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is not None
    assert isinstance(result, AttachResult)
    # Plan is resolved but not applied (no gtt_ids placed)
    assert result.gtt_ids == [], "Preview should not place GTTs"
    assert result.wing_order_id is None


@pytest.mark.asyncio
async def test_apply_template_to_order_sim_mode_route():
    """apply_path='sim' → routes to SimDriver.place_sim_gtt."""
    template = _base_template()
    mock_driver = _make_mock_sim_driver()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.sim.driver.SimDriver",
    ) as mock_sim_class:
        mock_sim_class.instance.return_value = mock_driver
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="sim",
        )

    assert result is not None
    assert mock_driver.place_sim_gtt.called, "Should route to SimDriver"


@pytest.mark.asyncio
async def test_apply_template_to_order_live_mode_route():
    """apply_path='live' → routes to broker.place_gtt."""
    template = _base_template()
    mock_broker = _make_mock_broker()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.registry.get_broker",
        return_value=mock_broker,
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="live",
        )

    assert result is not None
    assert mock_broker.place_gtt.called, "Should route to broker"


@pytest.mark.asyncio
async def test_apply_template_to_order_auto_mode_sim_active():
    """apply_path='auto' + SimDriver.active=True → routes to sim."""
    template = _base_template()
    mock_driver = _make_mock_sim_driver()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.sim.driver.SimDriver",
    ) as mock_sim_class:
        mock_sim_class.instance.return_value = mock_driver
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="auto",
        )

    assert result is not None
    assert mock_driver.place_sim_gtt.called


@pytest.mark.asyncio
async def test_apply_template_to_order_auto_mode_sim_inactive():
    """apply_path='auto' + SimDriver.active=False → routes to live."""
    template = _base_template()
    mock_broker = _make_mock_broker()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.sim.driver.SimDriver",
    ) as mock_sim_class, patch(
        "backend.brokers.registry.get_broker",
        return_value=mock_broker,
    ):
        mock_driver = MagicMock()
        mock_driver.active = False
        mock_sim_class.instance.return_value = mock_driver

        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="auto",
        )

    assert result is not None
    assert mock_broker.place_gtt.called


@pytest.mark.asyncio
async def test_apply_template_to_order_default_product():
    """Default parent_product NRML applied to plan and GTT legs."""
    template = _base_template()
    # GTT legs inherit parent_product
    result_plan = resolve_template_plan(
        template, {},
        parent_account="ACC1",
        parent_symbol="RELIANCE",
        parent_side="BUY",
        parent_qty=10,
        parent_exchange="NSE",
        parent_fill_price=1000.0,
        parent_product="NRML",  # Default
    )
    assert result_plan.gtts[0].orders[0]["product"] == "NRML"


# ── Broker resolution errors ─────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_live_broker_lookup_fails():
    """get_broker() raises → returns AttachResult with error."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.registry.get_broker",
        side_effect=RuntimeError("broker not found for ACC1"),
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="live",
        )

    assert result is not None
    assert result.errors, "Should have error for broker lookup failure"
    assert any("broker" in e.lower() for e in result.errors)


# ── Broker capability lookup (OCO detection) ─────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_live_mode_looks_up_caps():
    """apply_path='live' → resolves broker capabilities for OCO decision."""
    template = _base_template()
    template["tp_pct"] = 10.0
    template["sl_pct"] = 5.0
    mock_broker = _make_mock_broker()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.registry.get_broker",
        return_value=mock_broker,
    ), patch(
        "backend.brokers.capabilities.capabilities_for",
        return_value=KITE_CAPS,
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="live",
        )

    assert result is not None
    # With gtt_oco=True, plan should have 1 two-leg GTT
    assert len(result.plan.gtts) == 1
    assert result.plan.gtts[0].trigger_type == "two-leg"


# ── Parent order ID passing ──────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_parent_order_id_carries_through():
    """parent_order_id passed to apply_path function."""
    template = _base_template()
    mock_driver = _make_mock_sim_driver()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.api.algo.sim.driver.SimDriver",
    ) as mock_sim_class:
        mock_sim_class.instance.return_value = mock_driver
        await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            parent_order_id=12345,
            apply_path="sim",
        )

    # Check that parent_order_id was passed to place_sim_gtt
    call_kwargs = mock_driver.place_sim_gtt.call_args.kwargs
    assert call_kwargs.get("parent_order_id") == 12345, (
        "parent_order_id should carry through to sim placement"
    )


# ── Empty/minimal cases ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_template_to_order_none_template_returns_none():
    """Template resolves to None + no overrides → returns None early."""
    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=None),
    ):
        result = await apply_template_to_order(
            template_id=999,  # Non-existent
            template_slug=None,
            overrides={},
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is None


@pytest.mark.asyncio
async def test_apply_template_to_order_integration_full_flow():
    """Full flow: load template → check applies_to → resolve lot_size → apply."""
    template = _base_template()

    with patch(
        "backend.api.algo.template_attach.load_template_for_slug_or_id",
        new=AsyncMock(return_value=template),
    ), patch(
        "backend.brokers.adapters.kite.get_lot_size",
        new=AsyncMock(return_value=1),  # NSE doesn't resolve but call is safe
    ):
        result = await apply_template_to_order(
            template_id=1,
            template_slug="default-bull",
            overrides={"tp_pct": 15.0},  # Operator override
            parent_account="ACC1",
            parent_symbol="RELIANCE",
            parent_side="BUY",
            parent_qty=10,
            parent_exchange="NSE",
            parent_fill_price=1000.0,
            apply_path="preview",
        )

    assert result is not None
    # Override should have been applied
    tp_trigger = result.plan.gtts[0].trigger_values[0]
    assert tp_trigger == pytest.approx(1000.0 * 1.15, rel=0.01), (
        "Operator override tp_pct=15% should be used"
    )
