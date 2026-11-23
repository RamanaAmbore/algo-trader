"""Tests for `_v2_expiry_position_breakdown()` (agent_engine.py) — the
per-exchange root/position breakdown appended to the expiry-day
pre-close alert. Covers the council-mandated wording rules:
  - MCX: unnetted (needs_close) legs first, masked account, lots, the
    real close_reason; fully-netted roots demoted/last, NEUTRAL wording
    only (never "safe"/"hedged").
  - NFO: ITM/NTM legs first; OTM legs demoted/last as "no action" —
    never "netted" (different mechanism, no real pairing).
  - A scan failure renders an explicit degraded note, never a silent
    blank (which would read as a false "all clear").
  - Multi-account rows for the same root are never pooled.
  - A lot-size resolution failure falls back to raw contracts with an
    explicit unit label.

Shares `ExpiryEngine.scan_positions()` (mocked here) with the sibling
order-placing action — this file does not re-implement any netting
logic itself, only asserts on the rendered text given a fabricated
`engine.state.positions` / `scan_positions()` return value.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.api.algo.agent_engine import _v2_expiry_position_breakdown
from backend.api.algo.expiry import OptionPosition

# `_v2_expiry_position_breakdown` re-derives, independently, which
# exchange(s) are currently within their own pre-close window (2026-10-29
# audit fix — the agent's single flat condition carries no exchange
# identity) via `_expiry_preclose_exchanges`. These tests are about the
# RENDERING logic given a fabricated ExpiryEngine result, not the window
# computation itself (covered by test_expiry_day_and_preclose_agents.py),
# so every test here patches it to report "both exchanges in window"
# unless it's specifically testing the exchange filter.
_WINDOW_PATCH_TARGET = (
    "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window."
    "_expiry_preclose_exchanges"
)


def _pos(**kw) -> OptionPosition:
    defaults = dict(
        account="ZG0790", tradingsymbol="X", exchange="MCX",
        instrument_type="CE", underlying="GOLD", strike=0.0,
        expiry=None, quantity=0, product="NRML",
    )
    defaults.update(kw)
    return OptionPosition(**defaults)


def _mock_engine(positions: list[OptionPosition], to_close: list[OptionPosition] | None = None):
    engine = MagicMock()
    engine.scan_positions.return_value = to_close if to_close is not None else [
        p for p in positions if p.needs_close
    ]
    engine.state.positions = positions
    return engine


@pytest.mark.asyncio
async def test_mcx_unnetted_root_renders_first_with_masked_account_and_reason():
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM after 4-rule netting (residual qty +1; theta=-1.200)"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}), \
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="1 lot")):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert "MCX:" in text
    assert "GOLD" in text
    assert "Z#####" in text or "ZG####" in text or "#" in text, "account must be masked"
    assert "SELL 1 lot" in text
    assert "MCX unhedged ITM" in text
    assert "safe" not in text.lower()
    assert "hedged" not in text.lower() or "unhedged" in text.lower()


@pytest.mark.asyncio
async def test_mcx_fully_netted_root_is_demoted_and_neutral_never_safe_language():
    positions = [
        _pos(account="ZG0790", tradingsymbol="CRUDEOIL26OCT6000CE", underlying="CRUDEOIL",
             exchange="MCX", quantity=1, residual_qty=0, needs_close=False),
        _pos(account="ZG0790", tradingsymbol="CRUDEOIL26OCT6000PE", underlying="CRUDEOIL",
             exchange="MCX", quantity=-1, residual_qty=0, needs_close=False),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert "netted: CRUDEOIL" in text
    for banned in ("safe", "hedged, no action needed", "no action needed"):
        assert banned not in text.lower(), f"must never claim an assurance: {banned!r} found"


@pytest.mark.asyncio
async def test_mcx_unnetted_sorts_before_netted_root():
    positions = [
        _pos(account="ZG0790", tradingsymbol="CRUDEOIL26OCT6000CE", underlying="CRUDEOIL",
             exchange="MCX", quantity=1, residual_qty=0, needs_close=False),
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}), \
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="1 lot")):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert text.index("GOLD") < text.index("netted: CRUDEOIL"), \
        "action-needed root must render before the demoted netted root"


@pytest.mark.asyncio
async def test_nfo_itm_leg_renders_first_never_says_netted():
    positions = [
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT22000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=True,
             close_reason="Equity ITM — must close before expiry"),
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT23000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=False),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}), \
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="50 (contracts)")):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert "NSE/NFO:" in text
    assert "no action (OTM): NIFTY" in text
    assert "netted" not in text.lower(), "NFO must never use MCX's netting wording"
    assert text.index("NIFTY") < text.index("no action (OTM)")


@pytest.mark.asyncio
async def test_exchange_filter_excludes_out_of_window_exchange():
    """2026-10-29 audit fix regression — when only ONE exchange is in its
    pre-close window, the other exchange's section must not render at all,
    even though `ExpiryEngine.scan_positions()` always returns both.
    This is the primary bug the fix addressed: before it, an NFO-only
    window would still render MCX's (stale, hours-old) moneyness."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
        _pos(account="ZJ6294", tradingsymbol="NIFTY26OCT22000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, needs_close=True,
             close_reason="Equity ITM — must close before expiry"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch(_WINDOW_PATCH_TARGET, return_value={"NFO"}), \
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="1 lot")):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert "NSE/NFO:" in text
    assert "NIFTY" in text
    assert "MCX:" not in text, "MCX section must not render when only NFO is in its pre-close window"
    assert "GOLD" not in text


@pytest.mark.asyncio
async def test_no_exchange_in_window_returns_none():
    """When neither exchange is currently in its own pre-close window, the
    breakdown must return None (no scan, no stale render) rather than a
    blank or misleading section."""
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    engine = _mock_engine(positions)
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=engine), \
         patch(_WINDOW_PATCH_TARGET, return_value=set()):
        text = await _v2_expiry_position_breakdown({})
    assert text is None
    engine.scan_positions.assert_not_called()


@pytest.mark.asyncio
async def test_multi_account_same_root_never_pooled():
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
        _pos(account="ZJ6294", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=-1, residual_qty=-1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}), \
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="1 lot")):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert text.count("GOLD") == 2, \
        "two different accounts holding the same root must render as two separate lines, never pooled"
    assert "SELL" in text and "BUY" in text


@pytest.mark.asyncio
async def test_scan_failure_renders_explicit_degraded_note_never_blank():
    broken_engine = MagicMock()
    broken_engine.scan_positions.side_effect = RuntimeError("broker fetch failed")
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=broken_engine), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert "unavailable" in text.lower()


@pytest.mark.asyncio
async def test_no_expiring_positions_states_nothing_to_close_explicitly():
    """2026-11-22 fix (operator: "if no positions to close, mention that
    clearly") — a scan finding nothing must say so explicitly, never
    return None (which used to make the caller fall back to the bare
    one-line reminder with no real information)."""
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine([])), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}):
        text = await _v2_expiry_position_breakdown({})
    assert text is not None
    assert "nothing to close" in text.lower()


@pytest.mark.asyncio
async def test_lot_size_resolution_failure_falls_back_to_raw_contracts():
    from backend.api.algo import agent_engine as ae_mod
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=200, residual_qty=200, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch(_WINDOW_PATCH_TARGET, return_value={"MCX", "NFO"}), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=0)):
        text = await ae_mod._v2_expiry_position_breakdown({})
    assert text is not None
    assert "200 (contracts)" in text, \
        f"lot_size=0 (unknown sentinel) must fall back to raw contracts with an explicit label, got: {text}"


# ---------------------------------------------------------------------------
# `_v2_expiry_morning_preview()` — morning-side sibling, 2026-11-22. Takes
# an explicit single `exchange` (not a re-derived set like the pre-close
# function above), so these tests confirm it renders ONLY the requested
# exchange even when both have positions, and carries the "Tentative"
# framing line the pre-close breakdown deliberately does not have.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_morning_preview_renders_only_the_requested_exchange():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26NOV75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
        _pos(account="ZJ6294", tradingsymbol="NIFTY26NOV25000CE", underlying="NIFTY",
             exchange="NFO", quantity=50, residual_qty=50, needs_close=True,
             close_reason="Equity ITM — must close before expiry"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        mcx_text = await _v2_expiry_morning_preview({}, "MCX")
        nfo_text = await _v2_expiry_morning_preview({}, "NFO")

    assert mcx_text is not None and "GOLD" in mcx_text and "NIFTY" not in mcx_text, (
        f"MCX call must render only the MCX section, got: {mcx_text}"
    )
    assert nfo_text is not None and "NIFTY" in nfo_text and "GOLD" not in nfo_text, (
        f"NFO call must render only the NFO section, got: {nfo_text}"
    )


@pytest.mark.asyncio
async def test_morning_preview_carries_tentative_framing():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26NOV75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_morning_preview({}, "MCX")

    assert text is not None
    assert text.lower().startswith("tentative"), (
        f"morning preview must lead with an explicit tentative framing so the "
        f"operator never mistakes it for a final determination, got: {text!r}"
    )
    assert "1 hour before" in text


@pytest.mark.asyncio
async def test_morning_preview_states_nothing_to_close_explicitly():
    """2026-11-22 fix (operator: "if no positions to close, mention that
    clearly") -- a scan finding nothing must say so explicitly, never
    return None."""
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine([])):
        text = await _v2_expiry_morning_preview({}, "MCX")
    assert text is not None
    assert "nothing to close" in text.lower()
    assert "MCX" in text


@pytest.mark.asyncio
async def test_morning_preview_scan_failure_renders_degraded_note():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    engine = MagicMock()
    engine.scan_positions.side_effect = RuntimeError("broker unreachable")
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=engine):
        text = await _v2_expiry_morning_preview({}, "MCX")
    assert text is not None and "unavailable" in text.lower(), (
        "a scan failure must render an explicit degraded note, never a "
        "silent blank (which would read as a false 'all clear')"
    )


@pytest.mark.asyncio
async def test_morning_preview_states_nothing_to_close_when_only_other_exchange_has_positions():
    from backend.api.algo.agent_engine import _v2_expiry_morning_preview

    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26NOV75000CE", underlying="GOLD",
             exchange="MCX", quantity=1, residual_qty=1, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_morning_preview({}, "NFO")
    assert text is not None and "nothing to close" in text.lower() and "GOLD" not in text, (
        "the NFO agent's own render call must not surface MCX's section, "
        "and must state explicitly (not return None) that its own "
        "exchange has nothing expiring"
    )
