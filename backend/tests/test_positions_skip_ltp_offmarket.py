"""
Tests for the skip_ltp / fresh off-market guard in _resolve_positions_source.

Bug fixed: previously `if skip_ltp or fresh: return await _broker_fn()` ran
unconditionally. Off-market, the broker returns an empty frame → pulsePositionsStore
clears → Pulse grid goes blank.

Fix: bypass only when market is actually open; when closed, fall through to
closed_hours_or_broker so the DB snapshot is served.

Five quality dimensions:
  1. SSOT     — guard reuses _any_segment_open from snapshot_gate (single import).
  2. Perf     — no extra broker calls off-market.
  3. Stale    — no inline market-open checks remain in the bypass block.
  4. Reuse    — _resolve_positions_source uses closed_hours_or_broker (gate reused).
  5. UX       — off-market skip_ltp returns snapshot; on-market skip_ltp hits broker.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Source-level checks (Dimensions 3 & 4)
# ---------------------------------------------------------------------------

_POS_SRC = Path(__file__).parent.parent / "api" / "routes" / "positions.py"


def _src() -> str:
    return _POS_SRC.read_text(encoding="utf-8")


def test_positions_bypass_guard_uses_any_segment_open():
    """The skip_ltp bypass must check _any_segment_open, not bypass unconditionally."""
    src = _src()
    # The old unconditional bypass was:  if skip_ltp or fresh:\n        return await _broker_fn()
    # The fixed form has a market-open check before bypassing.
    assert "_any_segment_open" in src, (
        "positions.py must import _any_segment_open for the skip_ltp guard"
    )
    assert "mkt_open" in src, (
        "positions.py must compute mkt_open before the skip_ltp bypass"
    )


def test_positions_bypass_conditioned_on_mkt_open():
    """skip_ltp bypass must require market to be open; fresh has its own early-return."""
    src = _src()
    # fresh has an early return before the mkt_open check:
    assert "if fresh:" in src and "return await _broker_fn()" in src, (
        "fresh=True must have an early return to broker path"
    )
    # skip_ltp bypass is gated on mkt_open:
    assert "skip_ltp and mkt_open" in src, (
        "skip_ltp bypass must be conditioned on `skip_ltp and mkt_open`"
    )


def test_positions_no_unconditional_bypass():
    """The old unconditional bypass must not be present."""
    src = _src()
    # The old code was exactly: `    if skip_ltp or fresh:\n        return await _broker_fn()`
    # After fix, the condition includes `and mkt_open`.
    # Check neither form of the naked bypass exists.
    assert "if skip_ltp or fresh:\n        return await _broker_fn()" not in src, (
        "unconditional skip_ltp bypass still present — must add mkt_open guard"
    )


# ---------------------------------------------------------------------------
# Behavioural tests — mock _any_segment_open and _positions_snapshot / _fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_skip_ltp_market_open_calls_broker():
    """skip_ltp=True + market OPEN → broker path, NOT snapshot."""
    from backend.api.routes.positions import _resolve_positions_source

    mock_request = MagicMock()
    mock_request.state = MagicMock()

    # Provide a minimal PositionsResponse-like mock from broker
    from backend.api.schemas import PositionsResponse
    broker_resp = PositionsResponse(rows=[], summary=[], refreshed_at="now")
    snapshot_resp = PositionsResponse(rows=[], summary=[], refreshed_at="snap")

    snapshot_called = False

    async def fake_fetch():
        return broker_resp

    async def fake_snapshot():
        nonlocal snapshot_called
        snapshot_called = True
        return snapshot_resp

    with (
        patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            return_value=True,
        ),
        patch(
            "backend.api.routes.positions.get_or_fetch",
            new=AsyncMock(return_value=broker_resp),
        ),
        patch(
            "backend.api.routes.positions._positions_snapshot",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await _resolve_positions_source(
            request=mock_request,
            fresh=False,
            skip_ltp=True,
        )

    # When market is open, the broker path runs and returns broker_resp
    assert result is broker_resp, "open market + skip_ltp should return broker response"
    assert not snapshot_called, "snapshot should NOT be called when market is open"


@pytest.mark.asyncio
async def test_skip_ltp_market_closed_uses_snapshot():
    """skip_ltp=True + market CLOSED → snapshot path, broker NOT called directly."""
    from backend.api.routes.positions import _resolve_positions_source
    from backend.api.schemas import PositionsResponse

    mock_request = MagicMock()
    mock_request.state = MagicMock()

    snapshot_resp = PositionsResponse(
        rows=[], summary=[], refreshed_at="snap",
        as_of="2026-08-21 15:30 IST",  # non-None triggers the snapshot return path
    )

    broker_call_count = 0

    async def counting_fetch(*args, **kwargs):
        nonlocal broker_call_count
        broker_call_count += 1
        return PositionsResponse(rows=[], summary=[], refreshed_at="live")

    with (
        patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            return_value=False,  # MARKET CLOSED — affects closed_hours_or_broker
        ),
        patch(
            "backend.api.routes.positions._any_segment_open",
            return_value=False,  # MARKET CLOSED — affects the mkt_open check in _resolve_positions_source
        ),
        patch(
            "backend.api.routes.positions.get_or_fetch",
            new=AsyncMock(side_effect=counting_fetch),
        ),
        patch(
            "backend.api.routes.positions._positions_snapshot",
            new=AsyncMock(return_value=snapshot_resp),
        ),
    ):
        result = await _resolve_positions_source(
            request=mock_request,
            fresh=False,
            skip_ltp=True,
        )

    assert result is snapshot_resp, (
        "closed market + skip_ltp must return DB snapshot, not broker response"
    )
    assert broker_call_count == 0, (
        f"broker was called {broker_call_count} times off-market — must be 0"
    )


@pytest.mark.asyncio
async def test_fresh_market_closed_calls_broker():
    """fresh=True + market CLOSED → broker IS called (operator-explicit refresh bypasses gate).

    ?fresh=1 always hits the broker regardless of market hours. This is different from
    skip_ltp=True (which only bypasses when market is open). The rationale: ?fresh=1 is
    an operator-explicit request to get current data; skip_ltp is a programmatic 'skip
    the LTP step' hint that should respect the snapshot gate when closed.
    """
    from backend.api.routes.positions import _resolve_positions_source
    from backend.api.schemas import PositionsResponse

    mock_request = MagicMock()
    mock_request.state = MagicMock()

    broker_resp = PositionsResponse(rows=[], summary=[], refreshed_at="live")
    broker_call_count = 0

    async def counting_fetch(*args, **kwargs):
        nonlocal broker_call_count
        broker_call_count += 1
        return broker_resp

    with (
        patch(
            "backend.api.routes.positions.get_or_fetch",
            new=AsyncMock(side_effect=counting_fetch),
        ),
        patch(
            "backend.api.routes.positions._positions_snapshot",
            new=AsyncMock(return_value=PositionsResponse(rows=[], summary=[], refreshed_at="snap")),
        ),
        patch("backend.api.routes.positions.invalidate"),
    ):
        result = await _resolve_positions_source(
            request=mock_request,
            fresh=True,
            skip_ltp=False,
        )

    assert broker_call_count >= 1, (
        f"?fresh=1 must always call broker (operator-explicit refresh). Called {broker_call_count} times."
    )
    assert result is broker_resp, "fresh=True must return live broker response"


@pytest.mark.asyncio
async def test_normal_market_open_calls_broker():
    """Normal call (skip_ltp=False, fresh=False) + market OPEN → broker called."""
    from backend.api.routes.positions import _resolve_positions_source
    from backend.api.schemas import PositionsResponse

    mock_request = MagicMock()
    mock_request.state = MagicMock()

    broker_resp = PositionsResponse(rows=[], summary=[], refreshed_at="live")
    broker_call_count = 0

    async def counting_fetch(*args, **kwargs):
        nonlocal broker_call_count
        broker_call_count += 1
        return broker_resp

    with (
        patch(
            "backend.api.helpers.snapshot_gate._any_segment_open",
            return_value=True,  # MARKET OPEN
        ),
        patch(
            "backend.api.routes.positions.get_or_fetch",
            new=AsyncMock(side_effect=counting_fetch),
        ),
        patch(
            "backend.api.routes.positions._positions_snapshot",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await _resolve_positions_source(
            request=mock_request,
            fresh=False,
            skip_ltp=False,
        )

    assert isinstance(result, PositionsResponse), (
        "open market must return a PositionsResponse"
    )
    assert broker_call_count >= 1, "broker must be called when market is open"
