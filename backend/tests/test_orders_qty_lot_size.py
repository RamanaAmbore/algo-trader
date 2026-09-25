"""
P0 regression tests — lot_size double-multiplication fix.

Background: Operator placed 1 lot of CRUDEOIL 7000 option. System sent
100 lots to Kite because get_lot_size() fell back to lot_size=1 (cache
miss) and to_kite_qty(MCX, 100, 1) computed 100 // 1 = 100 LOTS instead
of 1 lot. Kite rejected the 100-lot order on margin — but if margin had
cleared, the trade would have been a 100× oversize.

Fix: to_kite_qty raises ValueError for MCX when lot_size ≤ 1; get_lot_size
returns 0 (not 1) for MCX cache misses; orders.py /ticket MCX gate reads
the resolved lot_size and 503s when unknown; size guard caps translated
lots at _MCX_MAX_LOTS (20) for all MCX orders.

These tests assert the correct qty translation and guard behaviour WITHOUT
mocking broker API calls (per project rule).
"""

from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock

from backend.brokers.adapters.kite import to_kite_qty, from_kite_qty, get_lot_size


# ── Unit tests: to_kite_qty ───────────────────────────────────────────────


class TestToKiteQty:
    """to_kite_qty must translate contracts→lots for MCX and be a no-op
    everywhere else. The 100× CRUDEOIL bug is a regression anchor."""

    def test_mcx_crudeoil_1_lot(self):
        """1 lot CRUDEOIL = 100 contracts. Kite wants 1 lot → qty=1."""
        result = to_kite_qty("MCX", 100, 100)
        assert result == 1, (
            f"CRUDEOIL 1-lot order must send qty=1 to Kite (got {result}). "
            "100× oversize regression."
        )

    def test_mcx_crudeoil_3_lots(self):
        """3 lots CRUDEOIL = 300 contracts. Kite wants qty=3."""
        result = to_kite_qty("MCX", 300, 100)
        assert result == 3

    def test_mcx_gold_1_lot(self):
        """GOLD lot_size=100. 1 lot = 100 contracts → qty=1."""
        result = to_kite_qty("MCX", 100, 100)
        assert result == 1

    def test_mcx_naturalgas_1_lot(self):
        """NATURALGAS lot_size=1250. 1 lot = 1250 contracts → qty=1."""
        result = to_kite_qty("MCX", 1250, 1250)
        assert result == 1

    def test_mcx_silver_2_lots(self):
        """SILVER lot_size=30. 2 lots = 60 contracts → qty=2."""
        result = to_kite_qty("MCX", 60, 30)
        assert result == 2

    def test_nfo_nifty_1_lot(self):
        """NFO NIFTY lot_size=50. to_kite_qty is a no-op — Kite wants
        contracts on NSE/NFO. 1 lot = 50 contracts → send 50."""
        result = to_kite_qty("NFO", 50, 50)
        assert result == 50

    def test_nfo_banknifty_1_lot(self):
        """BANKNIFTY lot_size=15. NFO → no-op → qty=15."""
        result = to_kite_qty("NFO", 15, 15)
        assert result == 15

    def test_equity_no_lot(self):
        """Equity BHEL on NSE: qty=1, lot_size=1 (no concept) → qty=1."""
        result = to_kite_qty("NSE", 1, 1)
        assert result == 1

    def test_equity_large_qty(self):
        """NSE equity: large qty, no lot concept → passes through."""
        result = to_kite_qty("NSE", 500, 1)
        assert result == 500

    # ── Safety guard: MCX cache-miss lot_size ──────────────────────────

    def test_mcx_lot_size_1_raises(self):
        """lot_size=1 on MCX is a cache-miss sentinel. to_kite_qty MUST
        raise ValueError rather than send raw_qty as lots (the 100× bug)."""
        with pytest.raises(ValueError, match="lot_size=1"):
            to_kite_qty("MCX", 100, 1)

    def test_mcx_lot_size_0_raises(self):
        """lot_size=0 on MCX (explicit unknown sentinel). Must also raise."""
        with pytest.raises(ValueError, match="lot_size=0"):
            to_kite_qty("MCX", 100, 0)

    def test_nco_lot_size_1_raises(self):
        """NCO exchange (MCX variant) same guard applies."""
        with pytest.raises(ValueError, match="lot_size=1"):
            to_kite_qty("NCO", 100, 1)

    def test_nco_lot_size_0_raises(self):
        with pytest.raises(ValueError, match="lot_size=0"):
            to_kite_qty("NCO", 100, 0)


# ── Unit tests: from_kite_qty ─────────────────────────────────────────────


class TestFromKiteQty:
    """Reverse translation: lots→contracts (Kite reports MCX fills in lots)."""

    def test_mcx_crudeoil_reverse(self):
        """Kite reports filled_quantity=1 (lot) → our qty=100 (contracts)."""
        result = from_kite_qty("MCX", 1, 100)
        assert result == 100

    def test_nfo_nifty_passthrough(self):
        """NFO: from_kite_qty is a no-op — already in contracts."""
        result = from_kite_qty("NFO", 50, 50)
        assert result == 50


# ── Integration tests: get_lot_size cache miss sentinel ───────────────────


@pytest.mark.asyncio
async def test_get_lot_size_mcx_cache_miss_returns_0():
    """When instruments cache is cold, get_lot_size returns 0 for MCX
    (not 1). The 0 is the safe sentinel that tells callers to refuse the
    order rather than dividing by 1 and producing a 100× oversize."""
    # Simulate cold cache: get_or_fetch raises an exception.
    # Must patch at the import site inside the function (backend.api.cache).
    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(side_effect=Exception("cache cold")),
    ):
        result = await get_lot_size("MCX", "CRUDEOIL26JULJUL7000CE")
    assert result == 0, (
        f"MCX cache miss must return 0 (safe sentinel), got {result}. "
        "Returning 1 triggers the 100× oversize bug."
    )


@pytest.mark.asyncio
async def test_get_lot_size_nfo_cache_miss_returns_0():
    """D6 fix (2026-09) — a genuine NFO cache miss now returns 0 (not 1).

    Pre-fix, a non-MCX miss silently returned 1 (a false "no
    translation needed" no-op), letting a genuinely-unknown NFO/BFO/
    CDS lot_size sail straight through `to_kite_qty` as a no-op
    instead of blocking with a clean 503 — the exact failure mode MCX
    already guarded against. get_lot_size's miss sentinel is now
    uniform (0 == unknown) across every exchange; see
    `_rebuild_lot_index`'s docstring for how a CONFIRMED lot_size=1 is
    now distinguished from a genuine miss."""
    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(side_effect=Exception("cache cold")),
    ):
        result = await get_lot_size("NFO", "NIFTY26JUNFUT")
    assert result == 0, (
        f"NFO cache miss must return 0 (unknown sentinel) since D6 — "
        f"a 1 here would mean an unconfirmed lot_size is silently "
        f"treated as 'no translation needed'. Got {result}."
    )


@pytest.mark.asyncio
async def test_get_lot_size_mcx_symbol_not_in_cache_returns_0():
    """D6 fix (2026-09) — MCX symbol not found in cache index → 0
    (unchanged). NSE miss ALSO now returns 0 (was 1 pre-D6) since a
    genuine cache miss is unconfirmed on every exchange now."""
    # Reset the module-level _LOT_INDEX_STAMP so the index is rebuilt.
    import backend.brokers.adapters.kite as kite_mod
    original_stamp = kite_mod._LOT_INDEX_STAMP
    kite_mod._LOT_INDEX_STAMP = None
    kite_mod._LOT_INDEX = {}
    try:
        # Simulate a warm cache that returns a valid response but doesn't
        # contain the queried MCX symbol.
        mock_resp = type("Resp", (), {"items": []})()
        with patch(
            "backend.api.cache.get_or_fetch",
            new=AsyncMock(return_value=mock_resp),
        ):
            mcx_result = await get_lot_size("MCX", "CRUDEOIL26JULJUL7000CE")
            nse_result = await get_lot_size("NSE", "RELIANCE")
    finally:
        kite_mod._LOT_INDEX_STAMP = original_stamp

    assert mcx_result == 0
    assert nse_result == 0, (
        f"D6: a genuine NSE cache miss must also return 0 now (was 1 "
        f"pre-fix) — the miss sentinel is uniform across exchanges. "
        f"Got {nse_result}."
    )


@pytest.mark.asyncio
async def test_get_lot_size_nfo_confirmed_lot_size_one_still_returns_1():
    """D6 regression guard: a CONFIRMED lot_size=1 entry (an NFO/BFO/
    CDS symbol whose instruments row genuinely reports lot_size=1,
    fed into `_LOT_INDEX` via `_rebuild_lot_index`) must still return
    1 — proving the 'confirmed 1' vs 'genuinely absent' distinction
    actually works end-to-end, not just fall back to the new 0
    sentinel for every lookup."""
    import backend.brokers.adapters.kite as kite_mod
    from backend.brokers.adapters.kite import _rebuild_lot_index

    original_stamp = kite_mod._LOT_INDEX_STAMP
    original_index = dict(kite_mod._LOT_INDEX)
    kite_mod._LOT_INDEX_STAMP = None
    kite_mod._LOT_INDEX = {}
    try:
        class _FakeInst:
            def __init__(self, e, s, ls):
                self.e = e
                self.s = s
                self.ls = ls

        _rebuild_lot_index([_FakeInst("NFO", "MICRONIFTY26JUNFUT", 1)])
        mock_resp = type("Resp", (), {"items": []})()
        with patch(
            "backend.api.cache.get_or_fetch",
            new=AsyncMock(return_value=mock_resp),
        ):
            result = await get_lot_size("NFO", "MICRONIFTY26JUNFUT")
    finally:
        kite_mod._LOT_INDEX_STAMP = original_stamp
        kite_mod._LOT_INDEX = original_index

    assert result == 1, (
        f"a CONFIRMED lot_size=1 entry must return 1, not the 0 "
        f"'unknown' sentinel. Got {result}."
    )


@pytest.mark.asyncio
async def test_get_lot_size_mcx_raw_one_never_confirmed_still_returns_0():
    """D6 regression guard: MCX carve-out. Even when the raw
    instruments dump reports lot_size=1 for an MCX symbol (Kite's
    convention for every MCX commodity NOT in the override table —
    see `instruments.py:_MCX_LOT_OVERRIDES`), `_rebuild_lot_index`
    must NOT store it as a confirmed 1 — MCX behavior is unchanged by
    D6: still 0 on both a raw-1 response and a genuine miss."""
    import backend.brokers.adapters.kite as kite_mod
    from backend.brokers.adapters.kite import _rebuild_lot_index

    original_stamp = kite_mod._LOT_INDEX_STAMP
    original_index = dict(kite_mod._LOT_INDEX)
    kite_mod._LOT_INDEX_STAMP = None
    kite_mod._LOT_INDEX = {}
    try:
        class _FakeInst:
            def __init__(self, e, s, ls):
                self.e = e
                self.s = s
                self.ls = ls

        _rebuild_lot_index([_FakeInst("MCX", "SOMEOBSCURECOMMODITY", 1)])
        mock_resp = type("Resp", (), {"items": []})()
        with patch(
            "backend.api.cache.get_or_fetch",
            new=AsyncMock(return_value=mock_resp),
        ):
            result = await get_lot_size("MCX", "SOMEOBSCURECOMMODITY")
    finally:
        kite_mod._LOT_INDEX_STAMP = original_stamp
        kite_mod._LOT_INDEX = original_index

    assert result == 0, (
        f"MCX raw lot_size=1 must never be treated as confirmed — "
        f"expected the 0 unknown sentinel (unchanged MCX behavior). "
        f"Got {result}."
    )


# ── Integration tests: /ticket MCX gate ───────────────────────────────────


def _admin_patches():
    """Mark every request as authenticated admin so the guard doesn't
    tag test requests as demo and 403 before account validation."""
    return patch.multiple(
        "backend.api.auth_guard",
        is_authenticated_request=lambda _conn: True,
        is_admin_request=lambda _conn: True,
        jwt_guard=AsyncMock(return_value=None),
    )


@pytest.mark.asyncio
async def test_ticket_mcx_cold_cache_503(async_client, stub_connections):
    """POST /ticket for an MCX symbol when instruments cache is cold (
    get_lot_size returns 0, frontend sent no lot_size_hint) must return
    503 with a clear message instead of silently placing 100 lots.

    v2 API (2026-07-08): request `quantity` is LOTS (1 = 1 lot). The
    F&O cold-cache guard in `_ticket_validate_input` fires 503 before
    the lots→contracts multiplication, so a cold cache never reaches
    the broker with a mis-scaled qty."""
    payload = {
        "mode": "live",
        "side": "BUY",
        "tradingsymbol": "CRUDEOIL26JULJUL7000CE",
        "exchange": "MCX",
        "quantity": 1,   # v2: 1 lot
        "price": 50.0,
        "order_type": "LIMIT",
        "account": "ZG0790",
        # no lot_size_hint — simulates the bug scenario
    }

    # get_lot_size is imported inside the ticket route function as
    # `from backend.brokers.adapters.kite import get_lot_size as _gls`.
    # Patch at the original module location so the import picks it up.
    # get_lot_size, _symbol_exchange_open, and _loaded_accounts are all
    # imported inside the route function; patch them at their source modules.
    with _admin_patches(), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=0)), \
         patch("backend.api.algo.agent_engine._symbol_exchange_open",
               return_value=True), \
         patch("backend.brokers.registry._loaded_accounts",
               return_value={"ZG0790"}):
        response = await async_client.post("/api/orders/ticket", json=payload)

    assert response.status_code == 503, (
        f"Expected 503 (lot_size unavailable) but got {response.status_code}. "
        "A cold cache on MCX must refuse the order, not place it."
    )
    detail = response.json().get("detail", "")
    assert "lot_size" in detail.lower() or "cache" in detail.lower()


@pytest.mark.asyncio
async def test_ticket_mcx_size_guard_422(async_client, stub_connections):
    """POST /ticket for MCX with lots exceeding _MCX_MAX_LOTS (20)
    must return 422.

    v2 API (2026-07-08): request `quantity` is LOTS. 22 lots trips
    the 20-lot MCX safety cap directly."""
    payload = {
        "mode": "live",
        "side": "BUY",
        "tradingsymbol": "CRUDEOIL26JULJUL7000CE",
        "exchange": "MCX",
        "quantity": 22,   # v2: 22 lots — trips the 20-lot cap
        "price": 50.0,
        "order_type": "LIMIT",
        "account": "ZG0790",
        "lot_size_hint": 100,
    }

    with _admin_patches(), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=100)), \
         patch("backend.api.algo.agent_engine._symbol_exchange_open",
               return_value=True), \
         patch("backend.brokers.registry._loaded_accounts",
               return_value={"ZG0790"}):
        response = await async_client.post("/api/orders/ticket", json=payload)

    assert response.status_code == 422, (
        f"Expected 422 (size guard) but got {response.status_code}. "
        "A 22-lot MCX order must be refused by the size guard."
    )
    detail = response.json().get("detail", "")
    assert "lot" in detail.lower() or "limit" in detail.lower() or "safety" in detail.lower()


@pytest.mark.asyncio
async def test_ticket_mcx_lot_size_hint_fallback(async_client, stub_connections):
    """When backend cache is cold but frontend sent lot_size_hint=100,
    the gate uses the hint and proceeds past the 503 guard.

    v2 API (2026-07-08): request `quantity` is LOTS."""
    payload = {
        "mode": "live",
        "side": "BUY",
        "tradingsymbol": "CRUDEOIL26JULJUL7000CE",
        "exchange": "MCX",
        "quantity": 1,   # v2: 1 lot
        "price": 50.0,
        "order_type": "LIMIT",
        "account": "ZG0790",
        "lot_size_hint": 100,  # frontend knows the lot_size
    }

    with _admin_patches(), \
         patch("backend.brokers.adapters.kite.get_lot_size",
               new=AsyncMock(return_value=0)), \
         patch("backend.api.algo.agent_engine._symbol_exchange_open",
               return_value=True), \
         patch("backend.brokers.registry._loaded_accounts",
               return_value={"ZG0790"}):
        response = await async_client.post("/api/orders/ticket", json=payload)

    # The MCX lot guard uses _ls_for_translate = 0 (cache) fallback to
    # lot_size_hint=100, so it should NOT fire the "cache cold" 503.
    # Any response other than the specific "lot_size for ... is not available"
    # message from the lot guard means the hint worked.
    if response.status_code == 503:
        detail = response.json().get("detail", "")
        assert "lot_size for" not in detail, (
            "The MCX lot guard 503 fired even though lot_size_hint=100 was "
            "supplied. The hint should have let the guard pass."
        )


# ── NFO path unaffected ───────────────────────────────────────────────────


def test_nfo_qty_passthrough():
    """NFO/NSE symbols are NEVER translated. to_kite_qty is always a
    no-op. Equity BHEL, NIFTY FUT, BANKNIFTY option — all pass through."""
    assert to_kite_qty("NFO", 50, 50) == 50     # NIFTY 1 lot
    assert to_kite_qty("NFO", 15, 15) == 15     # BANKNIFTY 1 lot
    assert to_kite_qty("NSE", 1, 1) == 1        # equity
    assert to_kite_qty("BSE", 500, 1) == 500    # BSE equity
    assert to_kite_qty("BFO", 25, 25) == 25     # BFO SENSEX


# ── Boundary: sub-lot MCX qty raises (C7 fix) ─────────────────────────────
#
# C7 audit fix (2026-09): sub-lot MCX qty used to pass through unchanged,
# which Kite silently accepts AS LOTS — e.g. 50 contracts (sub-lot of a
# 100 lot_size) was sent as "50 lots" instead of being refused, a silent
# 50× oversize. to_kite_qty now raises ValueError instead of guessing.


def test_mcx_sub_lot_raises():
    """raw_qty < lot_size on MCX must raise — Kite accepts the raw number
    AS LOTS (silent oversize), it does not reject a sub-lot quantity."""
    with pytest.raises(ValueError, match="QTY-GUARD"):
        to_kite_qty("MCX", 50, 100)
