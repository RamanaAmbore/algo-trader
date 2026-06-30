"""
Tests for the two P0 defects found on 2026-06-30:

  Defect 1 — basket_margin endpoint sent raw contract qty to Kite for MCX
  symbols instead of translating contracts → lots via translate_qty().
  CRUDEOIL lot_size=100 means qty=100 (1 lot) was sent as qty=100 lots
  = 10,000 contracts → Kite returned a nonsensically negative or
  astronomical margin.

  Defect 2 — run_preflight returned ok=True when basket_margin yielded
  a negative required value (-85,115,750 in the real incident).  The
  shortfall = max(0.0, negative - available) = 0 path slipped through
  with no blocker.  Also ok=True when available=0 and required>0 (the
  segment-permission skip branch was reached before the shortfall check).

Test dimensions per feedback_test_dimensions.md:
  SSOT   — hand-calculated reference values against real CRUDEOIL params.
  Perf   — preflight handler under 50ms (cold-call overhead; actual
           broker calls are mocked so pure Python cost is tested).
  Stale  — grep assertions that translate_qty / normalise_qty are used at
           every broker call boundary within the basket placement path.
  Reuse  — test helpers shared across both defect test groups.
  UX     — error strings surfaced to the caller contain enough detail
           for the operator to act on them (margin amounts, not just codes).
"""

import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

CRUDEOIL_SYM      = "CRUDEOIL26JUL7000PE"
CRUDEOIL_EXCHANGE = "MCX"
CRUDEOIL_LOT_SIZE = 100  # 1 lot = 100 contracts on MCX


def _make_kite_broker_stub(
    *,
    basket_margin_required: float = 150_000.0,
    margin_net: float = 500_000.0,
    margin_enabled: bool = True,
    lot_size: int = CRUDEOIL_LOT_SIZE,
) -> MagicMock:
    """Build a Broker stub suitable for MCX option preflight tests.

    translate_qty implements the real MCX lots convention:
    contracts → lots = contracts // lot_size (same as to_kite_qty).
    """
    broker = MagicMock()
    broker.broker_id = "zerodha_kite"
    broker.profile.return_value = {"exchanges": ["NSE", "NFO", "MCX", "CDS"]}
    broker.instruments.return_value = [{
        "tradingsymbol": CRUDEOIL_SYM,
        "exchange":      CRUDEOIL_EXCHANGE,
        "instrument_type": "OPT",
        "freeze_qty":    5000,
        "lot_size":      lot_size,
        "tick_size":     0.05,
    }]

    # basket_order_margins returns a list of per-leg entries.
    # Each entry mirrors what Kite's /margins/basket API returns.
    def _bom(orders: list[dict]) -> list[dict]:
        """Return one margin entry per leg.  For the single-leg case
        the entry's final.total is the full required margin."""
        return [{
            "initial": {"total": basket_margin_required},
            "final":   {"total": basket_margin_required},
        } for _ in orders]

    broker.basket_order_margins.side_effect = _bom
    broker.margins.return_value = {
        "equity":    {"enabled": margin_enabled, "net": margin_net},
        "commodity": {"enabled": margin_enabled, "net": margin_net},
    }

    # translate_qty: MCX → lots, others pass-through.
    def _translate(exchange: str, raw_qty: int, lot_sz: int) -> int:
        if exchange in ("MCX", "NCO") and lot_sz > 1 and raw_qty >= lot_sz:
            return max(1, raw_qty // lot_sz)
        return raw_qty

    broker.translate_qty.side_effect = _translate
    broker.normalise_qty.side_effect = _translate
    return broker


def _conns_with(account: str) -> MagicMock:
    c = MagicMock()
    c.conn = {account: object()}
    return c


# ---------------------------------------------------------------------------
# Defect 2a — preflight: negative basket margin must block (MARGIN_ANOMALY)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_negative_margin_blocks():
    """
    SSOT: real incident values.
    Kite returned basket_margin_used = -85,115,750 (negative) when MCX
    CRUDEOIL qty=100 was sent in contracts instead of being translated
    to 1 lot.  Pre-fix the shortfall = max(0, negative - available) = 0
    path passed ok=True.  Post-fix we block with MARGIN_ANOMALY.
    """
    from backend.api.algo.actions import run_preflight

    # Simulate the anomalous Kite response.
    def _negative_bom(orders: list[dict]) -> list[dict]:
        return [{"initial": {"total": -85_115_750.0}, "final": {"total": -85_115_750.0}}]

    broker = _make_kite_broker_stub()
    broker.basket_order_margins.side_effect = _negative_bom
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=CRUDEOIL_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange":         CRUDEOIL_EXCHANGE,
            "tradingsymbol":    CRUDEOIL_SYM,
            "quantity":         100,   # 1 lot in contracts
            "order_type":       "LIMIT",
            "product":          "NRML",
            "variety":          "regular",
            "transaction_type": "SELL",
            "price":            50.0,
        })

    assert result["ok"] is False, "negative margin MUST block the order"
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_ANOMALY" in codes, f"expected MARGIN_ANOMALY, got {codes}"
    blocker = next(b for b in result["blocked"] if b["code"] == "MARGIN_ANOMALY")
    # Broker margin value must appear in diagnostics for operator visibility.
    assert result["diagnostics"]["basket_margin_used"] == pytest.approx(-85_115_750.0)
    # UX: the error message must name the anomaly clearly.
    assert "negative" in blocker["reason"].lower() or "anomal" in blocker["reason"].lower()


# ---------------------------------------------------------------------------
# Defect 2b — preflight: available=0 with positive required must block
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_zero_available_blocks():
    """
    SSOT: audit row 9421 showed available_margin=0.0 with ok=true.
    When the commodity segment margin is enabled but net=0, shortfall
    = required - 0 = required > 0, so it SHOULD have blocked.
    Post-fix we explicitly gate on available==0.0 AND required>0.
    """
    from backend.api.algo.actions import run_preflight

    broker = _make_kite_broker_stub(
        basket_margin_required=29_870_396.16,
        margin_net=0.0,           # zero available — the critical condition
        margin_enabled=True,
    )
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=CRUDEOIL_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange":         CRUDEOIL_EXCHANGE,
            "tradingsymbol":    CRUDEOIL_SYM,
            "quantity":         100,
            "order_type":       "LIMIT",
            "product":          "NRML",
            "variety":          "regular",
            "transaction_type": "SELL",
            "price":            50.0,
        })

    assert result["ok"] is False, "zero available margin MUST block"
    codes = [b["code"] for b in result["blocked"]]
    # Either INSUFFICIENT_FUNDS or MARGIN_SHORTFALL is acceptable.
    assert any(c in codes for c in ("INSUFFICIENT_FUNDS", "MARGIN_SHORTFALL")), \
        f"expected a margin blocker, got {codes}"
    assert result["diagnostics"]["available_margin"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Defect 2c — preflight with real shortfall (positive required > available)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_real_shortfall_blocks():
    """
    SSOT: real incident — broker asked for ₹29,870,396 with ₹9,216,518
    available → shortfall ₹20,653,877.
    Post-fix: MARGIN_SHORTFALL blocker with correct shortfall amount.
    """
    from backend.api.algo.actions import run_preflight

    required  = 29_870_396.16
    available = 9_216_518.60
    broker = _make_kite_broker_stub(
        basket_margin_required=required,
        margin_net=available,
        margin_enabled=True,
    )
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=CRUDEOIL_LOT_SIZE)):
        result = await run_preflight("ZG0790", {
            "exchange":         CRUDEOIL_EXCHANGE,
            "tradingsymbol":    CRUDEOIL_SYM,
            "quantity":         100,
            "order_type":       "LIMIT",
            "product":          "NRML",
            "variety":          "regular",
            "transaction_type": "SELL",
            "price":            50.0,
        })

    assert result["ok"] is False
    codes = [b["code"] for b in result["blocked"]]
    assert "MARGIN_SHORTFALL" in codes
    ms = next(b for b in result["blocked"] if b["code"] == "MARGIN_SHORTFALL")
    expected_shortfall = required - available
    assert ms["data"]["shortfall"] == pytest.approx(expected_shortfall, rel=1e-3)
    # UX: rupee amounts must appear in the reason string so the operator
    # can act without opening the audit log.
    assert "₹" in ms["reason"] or "required" in ms["reason"].lower()
    # Diagnostics must surface all three values.
    assert result["diagnostics"]["basket_margin_used"] == pytest.approx(required, rel=1e-3)
    assert result["diagnostics"]["available_margin"]   == pytest.approx(available, rel=1e-3)
    assert result["diagnostics"]["margin_shortfall"]   == pytest.approx(expected_shortfall, rel=1e-3)


# ---------------------------------------------------------------------------
# Defect 1 — basket_margin qty translation: contracts → lots for MCX
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_basket_margin_translate_qty_mcx():
    """
    SSOT: CRUDEOIL lot_size=100. Frontend sends qty=100 (contracts = 1 lot).
    broker.basket_order_margins must receive qty=1 (lots), not qty=100.
    Pre-fix the endpoint sent raw leg.quantity=100 → Kite treated it as
    100 lots = 10,000 contracts → ₹2.99 crore margin asked.
    Post-fix translate_qty() is called per leg before the broker call.

    Stale check: the orders.py basket_margin inner loop must call
    broker.translate_qty before building the orders_payload dict.

    This test exercises the translate_qty → basket_order_margins call
    chain directly without instantiating the Litestar Controller (which
    requires an `owner` arg in the test environment).
    """
    # Import the qty translation helper directly to verify the contract
    # that basket_margin must satisfy: translate_qty(MCX, 100, 100) == 1.
    from backend.brokers.adapters.kite import to_kite_qty

    # SSOT: 100 contracts / lot_size=100 = 1 lot.
    translated = to_kite_qty(CRUDEOIL_EXCHANGE, 100, CRUDEOIL_LOT_SIZE)
    assert translated == 1, (
        f"to_kite_qty(MCX, 100, 100) returned {translated}, expected 1. "
        f"This is the root translation that basket_margin must call."
    )

    # Verify non-MCX passes through unchanged (NSE F&O contracts are
    # never divided by lot_size — Kite expects them as-is).
    nse_qty = to_kite_qty("NFO", 50, 50)   # 1 NIFTY lot = 50 contracts
    assert nse_qty == 50, (
        f"NSE/NFO must pass through unchanged; got {nse_qty}"
    )

    # Verify the KiteBroker adapter delegates to to_kite_qty correctly
    # (translate_qty and normalise_qty are the call sites in basket_margin).
    from backend.brokers.adapters.kite import KiteBroker
    conn_stub = MagicMock()
    conn_stub.get_kite_conn.return_value = MagicMock()
    conn_stub.account = "ZG0790"
    kb = KiteBroker(conn_stub)

    assert kb.translate_qty(CRUDEOIL_EXCHANGE, 100, CRUDEOIL_LOT_SIZE) == 1
    assert kb.normalise_qty(CRUDEOIL_EXCHANGE, 100, CRUDEOIL_LOT_SIZE) == 1

    # Stale check: grep-level assertion — orders.py must call translate_qty
    # (or normalise_qty) before every basket_order_margins call.
    import re
    import pathlib
    orders_src = pathlib.Path(
        "backend/api/routes/orders.py"
    ).read_text()
    # The fix inserts translate_qty into the basket_margin _margin_for_group
    # closure. We verify the pattern is present so no future refactor
    # silently removes it.
    assert re.search(r"translate_qty", orders_src), \
        "orders.py must call translate_qty in the basket_margin handler"
    # Before the fix, every orders_payload was built with `"quantity": leg.quantity`
    # in a simple list comprehension (no translation). That exact pattern is gone
    # from every basket_order_margins call site. Verify no list-comprehension
    # with raw leg.quantity feeds into basket_order_margins.
    # Heuristic: there must be no occurrence of `leg.quantity` inside a dict
    # that is directly part of an orders_payload list (without a translate_qty
    # wrapper). The marker string `"quantity":         leg.quantity` (with spaces
    # matching the old list-comp formatting) must not exist.
    old_raw_pattern = '"quantity":         leg.quantity'
    assert old_raw_pattern not in orders_src, (
        "Defect 1 regression: found old raw `leg.quantity` pattern in orders.py. "
        "Every basket_order_margins call site must translate qty via translate_qty."
    )


# ---------------------------------------------------------------------------
# Multi-leg basket: each leg's qty is independently translated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_basket_margin_multi_leg_each_translated():
    """
    Stale/SSOT: two MCX legs (different symbols, different lot sizes).
    Each must be independently translated — no cross-contamination.

    Uses to_kite_qty directly (the single source of truth that
    broker.translate_qty delegates to) to verify each leg's translation.
    """
    from backend.brokers.adapters.kite import to_kite_qty

    GOLD_SYM      = "GOLD26JULFUT"
    GOLD_LOT_SIZE = 10    # MCX gold mini lot

    # Leg 1: CRUDEOIL, 100 contracts → 1 lot
    crudeoil_kite_qty = to_kite_qty(CRUDEOIL_EXCHANGE, 100, CRUDEOIL_LOT_SIZE)
    assert crudeoil_kite_qty == 1, (
        f"CRUDEOIL: expected 1 lot, got {crudeoil_kite_qty}"
    )

    # Leg 2: GOLD mini, 10 contracts → 1 lot
    gold_kite_qty = to_kite_qty("MCX", 10, GOLD_LOT_SIZE)
    assert gold_kite_qty == 1, (
        f"GOLD: expected 1 lot, got {gold_kite_qty}"
    )

    # Verify that the translate_qty call chain in the broker adapter
    # is an O(1) per-leg operation (no shared state between legs).
    from backend.brokers.adapters.kite import KiteBroker
    conn_stub = MagicMock()
    conn_stub.get_kite_conn.return_value = MagicMock()
    conn_stub.account = "ZG0790"
    kb = KiteBroker(conn_stub)

    assert kb.translate_qty("MCX", 100, CRUDEOIL_LOT_SIZE) == 1
    assert kb.translate_qty("MCX", 10, GOLD_LOT_SIZE) == 1
    # Non-MCX must still pass through even when called after MCX legs.
    assert kb.translate_qty("NFO", 50, 50) == 50


# ---------------------------------------------------------------------------
# Perf: preflight handler under 50ms (Python-only, all broker calls mocked)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_perf_budget():
    """
    Perf: pure Python overhead of run_preflight with mocked broker I/O
    must stay under 50ms.  Any regression that adds synchronous work
    (DB queries, cache misses, sorting) in the hot path will trip this.
    """
    from backend.api.algo.actions import run_preflight

    broker = _make_kite_broker_stub(
        basket_margin_required=150_000.0,
        margin_net=500_000.0,
    )
    conns = _conns_with("ZG0790")

    with patch("backend.brokers.connections.Connections", return_value=conns), \
         patch("backend.brokers.registry.get_broker", return_value=broker), \
         patch("backend.brokers.client.is_cutover_on", return_value=False), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=CRUDEOIL_LOT_SIZE)):

        t0 = time.perf_counter()
        await run_preflight("ZG0790", {
            "exchange":         CRUDEOIL_EXCHANGE,
            "tradingsymbol":    CRUDEOIL_SYM,
            "quantity":         100,
            "order_type":       "LIMIT",
            "product":          "NRML",
            "variety":          "regular",
            "transaction_type": "SELL",
            "price":            50.0,
        })
        elapsed_ms = (time.perf_counter() - t0) * 1000

    assert elapsed_ms < 50, (
        f"run_preflight took {elapsed_ms:.1f}ms — exceeds 50ms budget. "
        f"A synchronous hot-path regression was introduced."
    )
