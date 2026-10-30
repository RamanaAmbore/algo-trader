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
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="1 lot")):
        text = await _v2_expiry_position_breakdown()
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
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)):
        text = await _v2_expiry_position_breakdown()
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
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="1 lot")):
        text = await _v2_expiry_position_breakdown()
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
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="50 (contracts)")):
        text = await _v2_expiry_position_breakdown()
    assert text is not None
    assert "NSE/NFO:" in text
    assert "no action (OTM): NIFTY" in text
    assert "netted" not in text.lower(), "NFO must never use MCX's netting wording"
    assert text.index("NIFTY") < text.index("no action (OTM)")


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
         patch("backend.api.algo.agent_engine._v2_expiry_qty_display", new=AsyncMock(return_value="1 lot")):
        text = await _v2_expiry_position_breakdown()
    assert text is not None
    assert text.count("GOLD") == 2, \
        "two different accounts holding the same root must render as two separate lines, never pooled"
    assert "SELL" in text and "BUY" in text


@pytest.mark.asyncio
async def test_scan_failure_renders_explicit_degraded_note_never_blank():
    broken_engine = MagicMock()
    broken_engine.scan_positions.side_effect = RuntimeError("broker fetch failed")
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=broken_engine):
        text = await _v2_expiry_position_breakdown()
    assert text is not None
    assert "unavailable" in text.lower()


@pytest.mark.asyncio
async def test_no_expiring_positions_returns_none():
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine([])):
        text = await _v2_expiry_position_breakdown()
    assert text is None


@pytest.mark.asyncio
async def test_lot_size_resolution_failure_falls_back_to_raw_contracts():
    from backend.api.algo import agent_engine as ae_mod
    positions = [
        _pos(account="ZG0790", tradingsymbol="GOLD26OCT75000CE", underlying="GOLD",
             exchange="MCX", quantity=200, residual_qty=200, needs_close=True,
             close_reason="MCX unhedged ITM"),
    ]
    with patch("backend.api.algo.expiry.ExpiryEngine", return_value=_mock_engine(positions)), \
         patch("backend.brokers.adapters.kite.get_lot_size", new=AsyncMock(return_value=0)):
        text = await ae_mod._v2_expiry_position_breakdown()
    assert text is not None
    assert "200 (contracts)" in text, \
        f"lot_size=0 (unknown sentinel) must fall back to raw contracts with an explicit label, got: {text}"
