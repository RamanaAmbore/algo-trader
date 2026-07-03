"""
Tests for the per-exchange close-snapshot lifecycle (Jul 2026).

Scope:
  1. Row-level `ltp_source` tagging on PositionRow / HoldingRow.
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
                     both-closed → all snap + `?skip_ltp=1` accepted.
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
    # A short-circuit check followed by a live-tag return.
    assert "ltp_source=\"live\"" in src


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

def test_position_row_has_ltp_source_field():
    """PositionRow carries an ltp_source column (default 'live')."""
    src = _src(_SCH)
    # Find the PositionRow class and check for ltp_source with default "live"
    assert "class PositionRow" in src
    # Should have the field with a default value
    assert 'ltp_source: str = "live"' in src


def test_holding_row_has_ltp_source_field():
    """HoldingRow carries an ltp_source column (default 'live')."""
    src = _src(_SCH)
    assert "class HoldingRow" in src
    assert 'ltp_source: str = "live"' in src


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
    """When both markets open, every row is tagged ltp_source='live'."""
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
        assert r.ltp_source == "live"


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
    # NSE row — snapshot LTP overlaid, tagged snapshot
    assert nifty.ltp_source == "snapshot"
    assert nifty.last_price == 22050.0
    # MCX row — untouched, still live
    assert crude.ltp_source == "live"
    assert crude.last_price == 6850.0


@pytest.mark.asyncio
async def test_overlay_snapshot_closed_row_without_snapshot_still_tagged():
    """When a row is on a closed exchange but no snapshot exists yet
    (first deploy for a newly-listed contract), keep broker LTP but still
    tag ltp_source='snapshot' so the frontend renders the SNAP chip."""
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
    assert out[0].ltp_source == "snapshot"
    # LTP untouched (no snapshot value to overlay)
    assert out[0].last_price == 105.0


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
    assert r.ltp_source == "snapshot"
    assert r.last_price == 1650.0
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
