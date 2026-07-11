"""
Per-exchange snapshot handler tests.

Five dimensions:
  1. SSOT       — Handlers live in market_lifecycle_handlers.py only;
                  register_default_handlers is the single registration entry.
  2. Performance — Default handler is async fire-and-forget; snapshot helper
                  uses ON CONFLICT DO UPDATE for idempotent re-write on
                  close_settled (no select-then-insert two-step).
  3. Stale code — No legacy inline close-snapshot hooks left in routes/.
  4. Reusable   — Same handler covers nse / mcx / cds + both `close` and
                  `close_settled` events.
  5. Correctness — _snapshot_close calls snapshot_daily_book; close_settled
                   overwrites the previous row via the UPSERT path.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_HANDLERS = (
    Path(__file__).parent.parent / "api" / "algo" / "market_lifecycle_handlers.py"
)
_DAILY = (
    Path(__file__).parent.parent / "api" / "algo" / "daily_snapshot.py"
)


def _hsrc() -> str:
    return _HANDLERS.read_text(encoding="utf-8")


def _dsrc() -> str:
    return _DAILY.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dimension 1 — SSOT
# ---------------------------------------------------------------------------

def test_handlers_live_in_one_module():
    """Default snapshot handlers all live in market_lifecycle_handlers.py."""
    src = _hsrc()
    assert "async def _snapshot_close(" in src
    assert "async def _snapshot_nav(" in src


def test_single_register_entry_point():
    """register_default_handlers is the single registration entry."""
    src = _hsrc()
    assert "def register_default_handlers(" in src


# ---------------------------------------------------------------------------
# Dimension 2 — Performance: UPSERT path for idempotent overwrite
# ---------------------------------------------------------------------------

def test_daily_snapshot_uses_on_conflict_do_update():
    """daily_snapshot upsert path is a single statement (no select-then-update).

    close_settled re-fires snapshot_daily_book; this UPSERT is what lets
    the second call overwrite the first row.
    """
    src = _dsrc()
    assert "ON CONFLICT" in src
    assert "DO UPDATE SET" in src


# ---------------------------------------------------------------------------
# Dimension 3 — Stale code: no inline `close_price` snapshot hooks elsewhere
# ---------------------------------------------------------------------------

def test_no_inline_close_snapshot_in_positions_route():
    """positions.py does not declare its own snapshot writer — uses the
    lifecycle hook path through daily_snapshot."""
    src = (
        Path(__file__).parent.parent / "api" / "routes" / "positions.py"
    ).read_text(encoding="utf-8")
    # We expect _override_stale_close_from_snapshot (READ path) — that's
    # fine. There should NOT be a `_persist_positions_snapshot` or
    # similar write helper inline.
    assert "_persist_positions_snapshot" not in src
    assert "INSERT INTO daily_book" not in src


# ---------------------------------------------------------------------------
# Dimension 4 — Reusable: same handler covers all 3 exchanges + both events
# ---------------------------------------------------------------------------

def test_same_handler_used_for_close_and_settled():
    """The exact same callable handles `:close` and `:close_settled`."""
    src = _hsrc()
    m = re.search(r"def register_default_handlers\(\).*?(?=\ndef |\Z)",
                  src, re.DOTALL)
    assert m
    body = m.group(0)
    # Loop over exchanges with both events registered to _snapshot_close
    assert '"nse"' in body and '"mcx"' in body and '"cds"' in body
    assert ':close"' in body and ':close_settled"' in body


# ---------------------------------------------------------------------------
# Dimension 5 — Correctness behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_close_invokes_snapshot_daily_book():
    """_snapshot_close awaits snapshot_daily_book()."""
    from backend.api.algo import market_lifecycle_handlers as mh

    mock_snap = AsyncMock(return_value={
        "accounts": ["ZG0790"],
        "holdings_rows": 5,
        "positions_rows": 10,
        "trades_rows": 0,
        "funds_rows": 2,
    })
    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book", mock_snap):
        await mh._snapshot_close("nse", "close")
    assert mock_snap.await_count == 1


@pytest.mark.asyncio
async def test_snapshot_close_swallows_exception():
    """A broker failure does not propagate back to the dispatcher."""
    from backend.api.algo import market_lifecycle_handlers as mh

    async def _boom():
        raise RuntimeError("simulated broker outage")

    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book",
               side_effect=_boom):
        # Should not raise.
        await mh._snapshot_close("mcx", "close")


@pytest.mark.asyncio
async def test_snapshot_nav_invokes_write_nav_snapshot():
    """_snapshot_nav awaits write_nav_snapshot()."""
    from backend.api.algo import market_lifecycle_handlers as mh

    mock_nav = AsyncMock(return_value={"nav": 100000.0, "as_of": "2026-06-28"})
    with patch("backend.api.algo.nav.write_nav_snapshot", mock_nav):
        await mh._snapshot_nav("nse", "close")
    assert mock_nav.await_count == 1


@pytest.mark.asyncio
async def test_register_default_handlers_idempotent():
    """Calling register_default_handlers twice is safe."""
    from backend.api.algo import market_lifecycle_handlers as mh
    from backend.api.algo.market_lifecycle import market_lifecycle

    mh._reset_for_test()
    market_lifecycle._reset_for_test()
    mh.register_default_handlers()
    state1 = market_lifecycle.get_state()
    mh.register_default_handlers()
    state2 = market_lifecycle.get_state()
    # Handler counts identical between calls.
    assert state1["handler_counts"] == state2["handler_counts"]

    # NSE:close has at least 2 handlers (close snapshot + NAV).
    nse_close = state2["handler_counts"].get("nse:close", 0)
    assert nse_close >= 2
    # MCX:close has the snapshot handler only.
    mcx_close = state2["handler_counts"].get("mcx:close", 0)
    assert mcx_close >= 1


@pytest.mark.asyncio
async def test_close_then_settled_uses_same_handler():
    """When close fires, then settled fires, the snapshot helper is
    invoked twice — UPSERT path overwrites on the second call.

    Also asserts the `settled` kwarg is threaded through — False on the
    initial close, True on the settled follow-up. Downstream row
    builders use this to gate `snapshot_extras.close_settled`.
    """
    from backend.api.algo import market_lifecycle_handlers as mh

    calls: list[dict] = []

    async def _fake_snap(*, settled: bool = False):
        calls.append({"settled": settled})
        return {
            "accounts": ["ZG0790"], "holdings_rows": 1,
            "positions_rows": 1, "trades_rows": 0, "funds_rows": 1,
        }

    async def _fake_sparkline(*, settled: bool = False):
        return {"symbols": 0, "errors": []}

    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book",
               side_effect=_fake_snap), \
         patch("backend.api.algo.daily_snapshot.snapshot_sparkline",
               side_effect=_fake_sparkline):
        await mh._snapshot_close("nse", "close")
        await mh._snapshot_close("nse", "close_settled")
    # Two passes — first writes initial close_price (settled=False),
    # second overwrites with the broker's settled close_price (settled=True).
    assert len(calls) == 2
    assert calls[0]["settled"] is False
    assert calls[1]["settled"] is True


@pytest.mark.asyncio
async def test_model_registered_in_init_db():
    """MarketLifecycleEvent appears in init_db's model import."""
    src = (
        Path(__file__).parent.parent / "api" / "database.py"
    ).read_text(encoding="utf-8")
    assert "MarketLifecycleEvent" in src


@pytest.mark.asyncio
async def test_audit_table_has_indexes():
    """ix_lifecycle_fired_at index declared on MarketLifecycleEvent."""
    src = (
        Path(__file__).parent.parent / "api" / "models.py"
    ).read_text(encoding="utf-8")
    assert "ix_lifecycle_fired_at" in src
    assert "ix_lifecycle_exch_type_fired" in src


# =============================================================================
# Movers lifecycle tests
# =============================================================================


@pytest.mark.asyncio
async def test_nse_close_fires_force_movers_snapshot():
    """On NSE close event, _force_movers_snapshot is called to persist
    the movers snapshot to the database for off-hours access."""
    from backend.api.algo import market_lifecycle_handlers as mh

    mock_movers = AsyncMock(return_value=2)  # 2 rows written

    with patch("backend.api.routes.watchlist._force_movers_snapshot",
               mock_movers):
        await mh._snapshot_movers("nse", "close")

    # Verify the movers snapshot was called
    mock_movers.assert_awaited_once()


@pytest.mark.asyncio
async def test_mcx_close_does_not_fire_movers_snapshot():
    """MCX close event should NOT fire movers snapshot.
    Movers are NSE-only; MCX close should not overwrite NSE movers."""
    from backend.api.algo import market_lifecycle_handlers as mh

    # _snapshot_movers is only registered for nse:close, not mcx:close.
    # We verify this by checking that the handler is only in NSE registrations.
    src = _hsrc()
    body = re.search(
        r"def register_default_handlers\(\).*?(?=\ndef |\Z)",
        src,
        re.DOTALL
    ).group(0)

    # Count how many times _snapshot_movers is registered
    movers_registrations = body.count("_snapshot_movers")
    # Should only appear for nse:close, not mcx or cds
    assert movers_registrations == 1, (
        f"_snapshot_movers should be registered exactly once (for nse:close), "
        f"found {movers_registrations} registrations"
    )

    # Additional check: verify it's registered to "nse:close" specifically
    assert 'register("nse:close", _snapshot_movers)' in body, \
        "_snapshot_movers should be registered to nse:close"


@pytest.mark.asyncio
async def test_nse_close_settled_fires_movers_snapshot():
    """When close_settled event fires on NSE, movers snapshot is also
    persisted (idempotent via UPSERT)."""
    from backend.api.algo import market_lifecycle_handlers as mh

    mock_movers = AsyncMock(return_value=3)

    with patch("backend.api.routes.watchlist._force_movers_snapshot",
               mock_movers):
        await mh._snapshot_movers("nse", "close_settled")

    mock_movers.assert_awaited_once()
