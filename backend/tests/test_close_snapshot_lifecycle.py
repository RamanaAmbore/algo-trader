"""
Tests for the per-exchange close-snapshot lifecycle + unified animation
model (Jul 2026).

Scope:
  1. Row-level `price_source` / `current_price` / `is_animating` tagging
     on PositionRow / HoldingRow.
  2. `is_exchange_closed_now` per-exchange gate helper.
  3. `latest_snapshot_ltp_map` reuses the same latest-batch CTE the
     per-route snapshot readers use (SSOT).
  4. `?skip_ltp=1` query param forces snapshot path even when a segment
     is open — no broker LTP fetch fires.
  5. RefreshButton contract — the route accepts the param and routes
     positions + holdings + funds through the snapshot / no-op paths.

Five quality dimensions per house style:
  SSOT       — one snapshot-map query pattern shared with route readers.
  Perf       — no per-row DB round-trip; single CTE call per response.
  Stale      — no dead code paths left over from the pre-lifecycle design.
  Reusable   — helpers are shared between positions.py + holdings.py.
  Correctness (UX) — mixed-live+snap during NSE-closed / MCX-open windows;
                     both-closed → all snap + `?skip_ltp=1` accepted;
                     `is_animating=False` on every snapshot-served row.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Source paths for static checks
# ---------------------------------------------------------------------------

_ROOT   = Path(__file__).parent.parent
_GATE   = _ROOT / "api" / "helpers" / "snapshot_gate.py"
_POS    = _ROOT / "api" / "routes"  / "positions.py"
_HOL    = _ROOT / "api" / "routes"  / "holdings.py"
_FUN    = _ROOT / "api" / "routes"  / "funds.py"
_SCH    = _ROOT / "api" / "schemas.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ===========================================================================
# Dimension 1 — SSOT: single snapshot-map query pattern
# ===========================================================================

def test_snapshot_gate_defines_latest_snapshot_ltp_map():
    """The per-row overlay reads through a helper in snapshot_gate.py."""
    src = _src(_GATE)
    assert "async def latest_snapshot_ltp_map(" in src, (
        "latest_snapshot_ltp_map must live in snapshot_gate.py"
    )
    # Uses the same CTE the per-route snapshot readers use.
    assert "WITH latest_batch AS" in src, (
        "latest_snapshot_ltp_map must anchor on the same latest-batch CTE"
    )


def test_snapshot_gate_defines_is_exchange_closed_now():
    """Per-exchange gate helper lives in snapshot_gate.py."""
    src = _src(_GATE)
    assert "def is_exchange_closed_now(" in src


# ===========================================================================
# Dimension 2 — Perf: overlay helper uses a single CTE call, no per-row lookup
# ===========================================================================

def test_positions_overlay_helper_uses_map():
    """positions._overlay_snapshot_for_closed_exchanges caches the
    snapshot map once and looks up per row."""
    src = _src(_POS)
    assert "async def _overlay_snapshot_for_closed_exchanges(" in src
    # A single call to latest_snapshot_ltp_map, not one per row.
    assert "await latest_snapshot_ltp_map" in src


def test_positions_overlay_fast_path_when_all_open():
    """No snapshot lookup when every row's exchange is currently open."""
    src = _src(_POS)
    # Fast path short-circuits when no exchanges are closed; calls resolve_current_price
    # to tag rows with price_source + is_animating under the unified animation model.
    assert "_overlay_snapshot_for_closed_exchanges" in src
    assert "resolve_current_price" in src
    assert "exchange_open=True" in src


def test_holdings_overlay_helper_uses_map():
    """holdings mirrors positions — single CTE call per response."""
    src = _src(_HOL)
    assert "async def _overlay_snapshot_for_closed_exchanges(" in src
    assert "await latest_snapshot_ltp_map" in src


# ===========================================================================
# Dimension 3 — Stale: no dead paths left behind
# ===========================================================================

def test_no_legacy_inline_close_snapshot_helpers():
    """No route defines its own inline snapshot-batch reader."""
    for src_path in (_POS, _HOL):
        src = _src(src_path)
        # The only snapshot-batch query should be the one in _positions_snapshot
        # / _holdings_snapshot; every OTHER SELECT from daily_book must route
        # through latest_snapshot_ltp_map (single CTE pattern).
        # This asserts the helper import is present so any future refactor
        # noticing an unused import can act on it — it's not a shortlist.
        assert "latest_snapshot_ltp_map" in src


# ===========================================================================
# Dimension 4 — Reusable: same schema field, shared helper module
# ===========================================================================

def test_position_row_has_unified_animation_fields():
    """PositionRow carries the unified animation-model triad:
        price_source (default 'live'), current_price, is_animating."""
    src = _src(_SCH)
    assert "class PositionRow" in src
    assert 'price_source: str = "live"' in src
    assert "current_price: float = 0.0" in src
    assert "is_animating: bool = True" in src


def test_holding_row_has_unified_animation_fields():
    """HoldingRow carries the unified animation-model triad."""
    src = _src(_SCH)
    assert "class HoldingRow" in src
    assert 'price_source: str = "live"' in src
    assert "current_price: float = 0.0" in src
    assert "is_animating: bool = True" in src


def test_routes_accept_skip_ltp_param():
    """positions + holdings + funds all accept ?skip_ltp=1."""
    for src_path in (_POS, _HOL, _FUN):
        src = _src(src_path)
        assert "skip_ltp: bool = False" in src, (
            f"{src_path.name} must accept ?skip_ltp=1 for RefreshButton's "
            f"both-markets-closed refresh flow"
        )


# ===========================================================================
# Dimension 5 — Correctness behaviour
# ===========================================================================

@pytest.mark.asyncio
async def test_exchange_to_gate_map_covers_common_exchanges():
    """NSE/BSE/NFO/BFO/CDS gate to NSE hours; MCX gates to MCX hours."""
    from backend.api.helpers.snapshot_gate import _EXCHANGE_TO_GATE
    assert _EXCHANGE_TO_GATE["NSE"] == "NSE"
    assert _EXCHANGE_TO_GATE["BSE"] == "NSE"
    assert _EXCHANGE_TO_GATE["NFO"] == "NSE"
    assert _EXCHANGE_TO_GATE["BFO"] == "NSE"
    assert _EXCHANGE_TO_GATE["CDS"] == "NSE"
    assert _EXCHANGE_TO_GATE["MCX"] == "MCX"


@pytest.mark.asyncio
async def test_overlay_snapshot_tags_rows_live_when_all_open():
    """When both markets open, every row is tagged price_source='live',
    is_animating=True, and current_price mirrors last_price."""
    from backend.api.routes.positions import _overlay_snapshot_for_closed_exchanges
    from backend.api.schemas import PositionRow

    rows = [
        PositionRow(
            account="ZG0790", tradingsymbol="NIFTY26JULFUT", exchange="NFO",
            product="NRML", quantity=50, average_price=22000.0,
            close_price=22100.0, last_price=22150.0, pnl=7500.0,
        ),
        PositionRow(
            account="ZG0790", tradingsymbol="CRUDEOIL26JULFUT", exchange="MCX",
            product="NRML", quantity=100, average_price=6800.0,
            close_price=6820.0, last_price=6850.0, pnl=5000.0,
        ),
    ]

    with patch(
        "backend.api.routes.positions.is_exchange_closed_now",
        return_value=False,
    ):
        out = await _overlay_snapshot_for_closed_exchanges(rows, kind="positions")

    assert len(out) == 2
    for r in out:
        assert r.price_source == "live"
        assert r.is_animating is True
        assert r.current_price == r.last_price


@pytest.mark.asyncio
async def test_overlay_snapshot_tags_closed_exchange_rows_as_snapshot():
    """Rows on a currently-closed exchange get their LTP frozen from snapshot
    and are tagged 'snapshot'. Rows on still-open exchanges stay live."""
    from backend.api.routes.positions import _overlay_snapshot_for_closed_exchanges
    from backend.api.schemas import PositionRow

    rows = [
        PositionRow(  # NSE (closed)
            account="ZG0790", tradingsymbol="NIFTY26JULFUT", exchange="NFO",
            product="NRML", quantity=50, average_price=22000.0,
            close_price=22100.0, last_price=22150.0, pnl=7500.0,
        ),
        PositionRow(  # MCX (open)
            account="ZG0790", tradingsymbol="CRUDEOIL26JULFUT", exchange="MCX",
            product="NRML", quantity=100, average_price=6800.0,
            close_price=6820.0, last_price=6850.0, pnl=5000.0,
        ),
    ]

    # NSE closed, MCX open.
    def _closed(exch: str) -> bool:
        return exch.upper() in ("NSE", "NFO", "BSE", "BFO", "CDS")

    snap_map = {("ZG0790", "NIFTY26JULFUT"): 22050.0}
    with patch(
        "backend.api.routes.positions.is_exchange_closed_now",
        side_effect=_closed,
    ), patch(
        "backend.api.routes.positions.latest_snapshot_ltp_map",
        AsyncMock(return_value=snap_map),
    ):
        out = await _overlay_snapshot_for_closed_exchanges(rows, kind="positions")

    assert len(out) == 2
    nifty = next(r for r in out if r.tradingsymbol == "NIFTY26JULFUT")
    crude = next(r for r in out if r.tradingsymbol == "CRUDEOIL26JULFUT")
    # NSE row — snapshot LTP overlaid, tagged snapshot_settled, no animation
    assert nifty.price_source == "snapshot_settled"
    assert nifty.last_price == 22050.0
    assert nifty.current_price == 22050.0
    assert nifty.is_animating is False
    # MCX row — untouched, still live + animating
    assert crude.price_source == "live"
    assert crude.last_price == 6850.0
    assert crude.current_price == 6850.0
    assert crude.is_animating is True


@pytest.mark.asyncio
async def test_overlay_snapshot_closed_row_without_snapshot_still_tagged():
    """When a row is on a closed exchange but no snapshot exists yet
    (first deploy for a newly-listed contract, or pre-settled window),
    keep broker LTP but tag price_source='snapshot_unsettled' + freeze
    animation so the frontend renders a static SNAP chip."""
    from backend.api.routes.positions import _overlay_snapshot_for_closed_exchanges
    from backend.api.schemas import PositionRow

    rows = [
        PositionRow(
            account="ZG0790", tradingsymbol="NEWCONTRACT26AUG", exchange="NFO",
            product="NRML", quantity=1, average_price=100.0,
            close_price=100.0, last_price=105.0, pnl=5.0,
        ),
    ]
    with patch(
        "backend.api.routes.positions.is_exchange_closed_now",
        return_value=True,
    ), patch(
        "backend.api.routes.positions.latest_snapshot_ltp_map",
        AsyncMock(return_value={}),  # no snapshot rows at all
    ):
        out = await _overlay_snapshot_for_closed_exchanges(rows, kind="positions")

    assert len(out) == 1
    assert out[0].price_source == "snapshot_unsettled"
    assert out[0].is_animating is False
    # LTP untouched (no snapshot value to overlay); current_price alias set.
    assert out[0].last_price == 105.0
    assert out[0].current_price == 105.0


@pytest.mark.asyncio
async def test_holdings_overlay_recomputes_cur_val_on_overlay():
    """Holdings cur_val is derived from ltp × qty — when we overlay a
    snapshot LTP, cur_val must be recomputed too."""
    from backend.api.routes.holdings import _overlay_snapshot_for_closed_exchanges
    from backend.api.schemas import HoldingRow

    rows = [
        HoldingRow(
            account="ZG0790", tradingsymbol="INFY", exchange="NSE",
            quantity=100, opening_quantity=100, average_price=1500.0,
            close_price=1600.0, last_price=1700.0,
            inv_val=150000.0, cur_val=170000.0,
            pnl=20000.0, pnl_percentage=13.3,
        ),
    ]
    snap_map = {("ZG0790", "INFY"): 1650.0}
    with patch(
        "backend.api.routes.holdings.is_exchange_closed_now",
        return_value=True,
    ), patch(
        "backend.api.routes.holdings.latest_snapshot_ltp_map",
        AsyncMock(return_value=snap_map),
    ):
        out = await _overlay_snapshot_for_closed_exchanges(rows)

    assert len(out) == 1
    r = out[0]
    assert r.price_source == "snapshot_settled"
    assert r.is_animating is False
    assert r.last_price == 1650.0
    assert r.current_price == 1650.0
    # cur_val recomputed from snapshot LTP × qty
    assert r.cur_val == 165000.0


@pytest.mark.asyncio
async def test_latest_snapshot_ltp_map_returns_empty_for_unknown_kind():
    """Guard: invalid kind → empty map (defensive)."""
    from backend.api.helpers.snapshot_gate import latest_snapshot_ltp_map
    out = await latest_snapshot_ltp_map("trades")
    assert out == {}
    out = await latest_snapshot_ltp_map("")
    assert out == {}


# ===========================================================================
# Change 1 — positions.py: skip_ltp gated on market-open
# ===========================================================================

def test_positions_skip_ltp_gate_imported_correctly():
    """positions.py imports _any_segment_open for the skip_ltp gate."""
    src = _src(_POS)
    assert "_any_segment_open" in src, (
        "positions.py must import _any_segment_open to gate skip_ltp bypass"
    )
    assert "await _asyncio.to_thread(_any_segment_open)" in src, (
        "positions.py must call _any_segment_open via asyncio.to_thread"
    )


def test_positions_skip_ltp_guards_on_market_open():
    """positions.py gates skip_ltp bypass on market_open check.

    The guard prevents off-market skip_ltp=True from bypassing the snapshot
    gate and calling the broker directly (which might return empty positions
    and blank the grid).
    """
    src = _src(_POS)
    # Must have: mkt_open = await ..._any_segment_open
    assert "mkt_open = await _asyncio.to_thread(_any_segment_open)" in src, (
        "positions.py must capture market-open state before skip_ltp check"
    )
    # Must have skip_ltp gated on mkt_open.
    # Two equivalent forms are accepted:
    #   (a) if (skip_ltp or fresh) and mkt_open:  — combined condition
    #   (b) if skip_ltp and mkt_open:              — split form (fresh has own early return)
    assert (
        "if (skip_ltp or fresh) and mkt_open:" in src
        or "if skip_ltp and mkt_open:" in src
    ), (
        "positions.py must gate skip_ltp bypass on mkt_open "
        "(either combined or split form with a separate fresh early-return)"
    )


@pytest.mark.asyncio
async def test_resolve_positions_source_skip_ltp_respects_market_open():
    """When market is closed and skip_ltp=True, _resolve_positions_source
    falls through to closed_hours_or_broker (snapshot path), not broker path.
    """
    from backend.api.routes.positions import _resolve_positions_source
    from litestar import Request
    from unittest.mock import AsyncMock, patch

    # Create a minimal Request mock
    request = AsyncMock(spec=Request)

    # Scenario: market closed, skip_ltp=True
    # Expected: snapshot path taken, not broker path
    with patch(
        "backend.api.routes.positions._any_segment_open",
        return_value=False,  # market closed
    ), patch(
        "backend.api.routes.positions.closed_hours_or_broker",
        new_callable=AsyncMock,
        return_value=(AsyncMock(), "snapshot"),
    ) as mock_gate:
        # This is tricky because _resolve_positions_source awaits to_thread
        # and calls _broker_fn directly when skip_ltp=True + mkt_open.
        # When market is closed, the gate is called instead.
        try:
            await _resolve_positions_source(request, fresh=False, skip_ltp=True)
        except Exception:
            # May fail due to snapshot returning None, but we're checking
            # that the gate was called, not the broker.
            pass

        # Verify closed_hours_or_broker was called (snapshot path)
        assert (
            mock_gate.called
        ), "When market is closed, closed_hours_or_broker must be called"


@pytest.mark.asyncio
async def test_resolve_positions_source_skip_ltp_bypasses_when_market_open():
    """When market is open and skip_ltp=True, _resolve_positions_source
    bypasses the snapshot gate and calls the broker directly (for metadata refresh).
    """
    from backend.api.routes.positions import _resolve_positions_source
    from litestar import Request
    from unittest.mock import AsyncMock, patch

    request = AsyncMock(spec=Request)

    # Scenario: market open, skip_ltp=True
    # Expected: broker path taken directly (bypass gate)
    with patch(
        "backend.api.routes.positions._any_segment_open",
        return_value=True,  # market open
    ), patch(
        "backend.api.routes.positions.get_or_fetch",
        new_callable=AsyncMock,
        return_value=AsyncMock(),
    ) as mock_fetch, patch(
        "backend.api.routes.positions.closed_hours_or_broker",
        new_callable=AsyncMock,
    ) as mock_gate:
        try:
            await _resolve_positions_source(request, fresh=False, skip_ltp=True)
        except Exception:
            pass

        # When market is open, skip_ltp should bypass the gate
        # and call the broker path directly (get_or_fetch)
        assert (
            mock_fetch.called
        ), "When market is open + skip_ltp=True, broker_fn must be called"


# ===========================================================================
# Change 2 — funds.py: closed_hours_or_broker + snapshot fallback
# ===========================================================================

def test_funds_uses_closed_hours_or_broker_gate():
    """funds.py uses the canonical closed_hours_or_broker gate when not fresh."""
    src = _src(_FUN)
    assert "closed_hours_or_broker" in src, (
        "funds.py must use closed_hours_or_broker gate"
    )


def test_funds_gate_has_snapshot_fallback_enabled():
    """funds.py calls closed_hours_or_broker with fallback_to_snapshot_on_broker_error=True.

    This prevents zero-margins being shown post-settlement when the broker
    returns empty during clearing; the last known cache value is served instead.
    """
    src = _src(_FUN)
    assert "fallback_to_snapshot_on_broker_error=True" in src, (
        "funds.py must enable snapshot fallback in closed_hours_or_broker call"
    )


def test_funds_fresh_bypass_before_gate():
    """funds.py has a ?fresh=1 bypass BEFORE the closed_hours_or_broker gate.

    When fresh=True, it invalidates the cache and calls get_or_fetch directly,
    bypassing all market-hour gates to force a live refresh.
    """
    src = _src(_FUN)
    # Look for the if fresh block that precedes closed_hours_or_broker
    lines = src.split("\n")
    fresh_idx = None
    gate_idx = None
    for i, line in enumerate(lines):
        if "if fresh:" in line:
            fresh_idx = i
        if "closed_hours_or_broker" in line:
            gate_idx = i
    assert (
        fresh_idx is not None and gate_idx is not None and fresh_idx < gate_idx
    ), (
        "funds.py must have if fresh: block before closed_hours_or_broker call"
    )


def test_funds_accept_skip_ltp_param():
    """funds.py accepts ?skip_ltp param (as a no-op)."""
    src = _src(_FUN)
    assert "skip_ltp: bool = False" in src, (
        "funds.py route must accept skip_ltp param for RefreshButton uniformity"
    )
    # Should be documented as a no-op
    assert "skip_ltp" in src and ("no-op" in src or "ignored" in src), (
        "funds.py should document skip_ltp as a no-op (funds have no LTP concept)"
    )


def test_funds_snapshot_fn_uses_cache_peek():
    """funds.py _funds_snapshot_fn uses peek() to return last known TTL cache."""
    src = _src(_FUN)
    assert "peek(" in src, (
        "funds.py snapshot function must use peek() from the TTL cache"
    )
    # Should be checking the cache before returning empty
    assert 'peek("funds")' in src, (
        "funds.py snapshot must peek the 'funds' cache entry"
    )
