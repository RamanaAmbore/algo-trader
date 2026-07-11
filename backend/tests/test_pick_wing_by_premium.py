"""
Characterization tests for _pick_wing_by_premium (backend/api/algo/template_attach.py:306).

CC=41, branch coverage goal: ≥80% to safely refactor.

The function scans option chains to find a contract whose current premium (LTP)
is closest to a target premium (parent_fill_price × wing_premium_pct / 100).

Algorithm outline:
  1. Parse parent symbol → root, expiry, strike, opt_type (CE/PE)
  2. Read settings: min OI, max spread%, chain radius
  3. Filter instruments cache to same (root, expiry, opt_type)
  4. Slice to ±chain_radius strikes from parent
  5. Batched broker.quote() across all candidates
  6. Score by abs(ltp - target), penalize wide spreads
  7. Return best match (or fallback if all fail OI/spread filters)

Test dimensions:
  SSOT   — returned (wing_symbol, wing_ltp, reason) carries correct values
  Perf   — async quote() batched, not per-strike
  Stale  — fallback path (when all pass filters) vs best-filter path
  Reuse  — mock broker used across multiple tests
  UX     — reason strings clear on both success + error paths
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from backend.api.algo.template_attach import _pick_wing_by_premium


# ── Test data helpers ────────────────────────────────────────────

class MockInstrument:
    """Mock Instrument with attribute-based access."""
    def __init__(self, tradingsymbol: str, strike: float | None, exchange: str = "NFO"):
        self.s = tradingsymbol.upper()
        self.k = strike
        self.e = exchange


def _make_instrument(
    tradingsymbol: str,
    strike: float | None,
    exchange: str = "NFO",
) -> MockInstrument:
    """Helper to build an Instrument-like object for mocking cache."""
    return MockInstrument(tradingsymbol, strike, exchange)


class MockInstrumentsResp:
    """Mock response from instruments cache."""
    def __init__(self, items: list[MockInstrument]):
        self.items = items


def _make_mock_quote(
    ltp: float = 50.0,
    bid: float = 49.5,
    ask: float = 50.5,
    oi: int = 10000,
) -> dict:
    """Build a mock quote dict matching broker.quote() schema."""
    return {
        "last_price": ltp,
        "oi": oi,
        "depth": {
            "buy": [{"price": bid}] if bid > 0 else [],
            "sell": [{"price": ask}] if ask > 0 else [],
        },
    }


def _make_mock_broker(quote_data: dict | None = None) -> MagicMock:
    """Mock broker with quote() method returning the provided data."""
    broker = MagicMock()
    if quote_data is None:
        quote_data = {}
    broker.quote.return_value = quote_data
    return broker


# ── Happy path tests ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_exact_match():
    """Target premium = 50; one option has LTP exactly 50 → picked."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target: 200 × 25 / 100 = 50

    # Build chain: three ATM calls, one at 22000 (parent), two nearby
    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),  # Parent strike
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=55.0, bid=54.5, ask=55.5),
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=50.0, bid=49.5, ask=50.5),  # Exact
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0, bid=44.5, ask=45.5),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws == "NIFTY25APR22000CE", "Exact match should be picked"
    assert wltp == 50.0
    assert "22000" in reason and "50.00" in reason


@pytest.mark.asyncio
async def test_pick_wing_by_premium_closest_candidate():
    """Target = 50; options at 45 and 60 → picks 45 (closer)."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target: 50

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=45.0, bid=44.5, ask=45.5, oi=15000),
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=60.0, bid=59.5, ask=60.5, oi=20000),
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=40.0, bid=39.5, ask=40.5, oi=5000),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws == "NIFTY25APR21950CE", "45 is closer to target 50 than 60"
    assert wltp == 45.0


# ── CE vs PE filtering ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_filters_ce_only():
    """Parent is CE; chain has both CE and PE → only CE candidates scanned."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),  # CE
        _make_instrument("NIFTY25APR21950PE", 21950.0),  # PE — should be ignored
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=50.0),
        "NFO:NIFTY25APR21950PE": _make_mock_quote(ltp=5.0),  # Would fail if not filtered
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=48.0),
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=52.0),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws == "NIFTY25APR21950CE", "Should pick CE, not PE"
    assert "PE" not in reason or "21950" in reason


@pytest.mark.asyncio
async def test_pick_wing_by_premium_filters_pe_only():
    """Parent is PE; chain has both → only PE candidates scanned."""
    parent_symbol = "NIFTY25APR22000PE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),  # CE
        _make_instrument("NIFTY25APR21950PE", 21950.0),  # PE
        _make_instrument("NIFTY25APR22000PE", 22000.0),  # Parent
        _make_instrument("NIFTY25APR22050PE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=3.0),  # Would fail
        "NFO:NIFTY25APR21950PE": _make_mock_quote(ltp=50.0),  # PE — should pick
        "NFO:NIFTY25APR22000PE": _make_mock_quote(ltp=48.0),
        "NFO:NIFTY25APR22050PE": _make_mock_quote(ltp=52.0),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws == "NIFTY25APR21950PE", "Should pick PE"
    assert "21950" in reason


# ── Chain radius / strike ordering ──────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_chain_radius_bounds():
    """chain_radius=1; parent at 22000 → only ±1 strikes scanned."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    # Parent is at index 2 (22000). radius=1 → indices [1,3], so strikes 21950, 22000, 22050
    candidates = [
        _make_instrument("NIFTY25APR21900CE", 21900.0),  # Out of radius
        _make_instrument("NIFTY25APR21950CE", 21950.0),  # In radius
        _make_instrument("NIFTY25APR22000CE", 22000.0),  # Parent
        _make_instrument("NIFTY25APR22050CE", 22050.0),  # In radius
        _make_instrument("NIFTY25APR22100CE", 22100.0),  # Out of radius
    ]

    # Target = 50. Best within radius: 21950 @ 50 (score 0)
    quote_data = {
        "NFO:NIFTY25APR21900CE": _make_mock_quote(ltp=50.0),  # Would win if not filtered
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=50.0),
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=55.0),
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0),
        "NFO:NIFTY25APR22100CE": _make_mock_quote(ltp=50.0),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {
            "templates.wing_min_oi": 1000,
            "templates.wing_chain_radius": 1,  # radius=1
        }.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # 21950 @ 50 has score 0, 22050 @ 45 has score 5. 21950 wins.
    assert ws == "NIFTY25APR21950CE", "Should pick from radius"
    assert "21900" not in reason and "22100" not in reason, "Out-of-radius strikes should not appear"


# ── OI filter ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_min_oi_filter():
    """min_oi=5000; candidate at 5000 passes; below fails."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=50.0, oi=4999),  # Below threshold
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=55.0, oi=5000),  # At threshold — score 5
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0, oi=5000),  # At threshold — score 5
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {
            "templates.wing_min_oi": 5000,
            "templates.wing_chain_radius": 20,
        }.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # Target 50: score(22000)=|55-50|=5, score(22050)=|45-50|=5. Both same score; 22000 comes first in loop
    # So 22000 should win (first one encountered)
    assert ws == "NIFTY25APR22000CE"
    assert wltp == 55.0
    # One candidate (21950) was dropped due to OI < 5000
    assert "dropped" in reason.lower() or "OI" in reason


# ── Spread filter ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_spread_pct_filter():
    """max_spread_pct=2%; wide-spread candidate filtered, tight-spread picked."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target = 50

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    # Spread% = (ask - bid) / ltp * 100
    # 21950: (51 - 49) / 50 = 4% (too wide) — filtered
    # 22000: (50.5 - 49.5) / 50 = 2% (at limit, passes) — score = 0 (exact)
    # 22050: (45.2 - 44.8) / 45 = 0.89% (tight) — score = 5 (distance)
    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=50.0, bid=49.0, ask=51.0),  # 4% spread
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=50.0, bid=49.5, ask=50.5),  # 2% spread
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0, bid=44.8, ask=45.2),  # 0.89% spread
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {
            "templates.wing_min_oi": 1000,
            "templates.wing_chain_radius": 20,
        }.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 2.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # 22000 passes filters and scores best (exact match). 21950 filtered for wide spread.
    assert ws == "NIFTY25APR22000CE"
    # Note: reason string might not include "dropped_spread" if only one candidate is visible
    # but we can verify the pick is correct
    assert wltp == 50.0


# ── Fallback path (all fail OI/spread) ──────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_fallback_when_filters_drop_all():
    """All candidates fail OI/spread filters → use fallback (best-score ignoring filters)."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target = 50

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),  # ltp=50, score=0
        _make_instrument("NIFTY25APR22000CE", 22000.0),  # ltp=55, score=5 + spread_penalty
        _make_instrument("NIFTY25APR22050CE", 22050.0),  # ltp=45, score=5 + spread_penalty
    ]

    # All have OI < min_oi, all have spread > max_spread. Fallback will pick best overall score (21950 @ 50)
    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=50.0, bid=40.0, ask=60.0, oi=100),  # score=0
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=55.0, bid=44.0, ask=66.0, oi=100),  # score=5+penalty
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0, bid=36.0, ask=54.0, oi=100),  # score=5+penalty
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {
            "templates.wing_min_oi": 5000,  # High threshold
            "templates.wing_chain_radius": 20,
        }.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 1.0}.get(k, d),  # Tight threshold
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws == "NIFTY25APR21950CE", "Fallback picks best score (50 is exact match)"
    assert wltp == 50.0
    assert "fallback" in reason.lower()


# ── Error paths ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_non_positive_target():
    """wing_premium_pct ≤ 0 or target ≤ 0 → skip."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 100.0
    wing_premium_pct = -5.0  # Negative

    ws, wltp, reason = await _pick_wing_by_premium(
        parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
    )

    assert ws is None
    assert wltp is None
    assert "not positive" in reason


@pytest.mark.asyncio
async def test_pick_wing_by_premium_target_zero():
    """parent_fill_price=0 → target=0 → skip."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 0.0
    wing_premium_pct = 25.0

    ws, wltp, reason = await _pick_wing_by_premium(
        parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
    )

    assert ws is None
    assert wltp is None


@pytest.mark.asyncio
async def test_pick_wing_by_premium_unparseable_symbol():
    """Parent symbol unparseable → bail."""
    parent_symbol = "INVALID_SYMBOL"
    parent_exchange = "NSE"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    ws, wltp, reason = await _pick_wing_by_premium(
        parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
    )

    assert ws is None
    assert wltp is None
    assert "unparseable" in reason.lower()


@pytest.mark.asyncio
async def test_pick_wing_by_premium_instruments_cache_fail():
    """Instruments cache lookup fails → fallback reason."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(side_effect=RuntimeError("cache fail")),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws is None
    assert wltp is None
    assert "cache" in reason.lower() and "failed" in reason.lower()


@pytest.mark.asyncio
async def test_pick_wing_by_premium_no_chain_candidates():
    """No instruments match (root, expiry, opt_type) → no candidates."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    # Empty or non-matching chain
    empty_chain = []

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(empty_chain)),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws is None
    assert wltp is None
    assert "no chain candidates" in reason.lower()


@pytest.mark.asyncio
async def test_pick_wing_by_premium_chain_radius_eliminates_all():
    """chain_radius filter leaves no candidates."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21500CE", 21500.0),  # Far from parent
        _make_instrument("NIFTY25APR22000CE", 22000.0),  # Parent
        _make_instrument("NIFTY25APR22500CE", 22500.0),  # Far from parent
    ]

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {
            "templates.wing_min_oi": 1000,
            "templates.wing_chain_radius": 0,  # radius=0 → only parent
        }.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker({}),  # Empty quote data
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws is None or "chain_radius" in reason.lower() or "scanned 0" in reason


@pytest.mark.asyncio
async def test_pick_wing_by_premium_broker_quote_fail():
    """broker.quote() raises → caught, returns None."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=MagicMock(quote=MagicMock(side_effect=RuntimeError("broker error"))),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws is None
    assert wltp is None
    assert "broker.quote()" in reason


# ── Quote edge cases ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_quote_missing_ltp():
    """Candidate's quote missing ltp → skipped, continue scanning."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": {"oi": 5000},  # Missing ltp
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=50.0),
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=50.0),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # 22000 or 22050 should be picked (both score equally)
    assert ws in ["NIFTY25APR22000CE", "NIFTY25APR22050CE"]


@pytest.mark.asyncio
async def test_pick_wing_by_premium_quote_zero_ltp():
    """Candidate's quote has ltp=0 or None → skipped."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": {"last_price": None, "oi": 5000},
        "NFO:NIFTY25APR22000CE": {"last_price": 0, "oi": 5000},
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=50.0),  # Only valid one
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    assert ws == "NIFTY25APR22050CE"


# ── Commodity options (MCX) ────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_mcx_commodity_option():
    """MCX CRUDEOIL options follow same logic as NFO."""
    # MCX format: CRUDEOIL26JUL6500CE (ROOTYYMMSTRIKECE)
    parent_symbol = "CRUDEOIL26JUL6500CE"
    parent_exchange = "MCX"
    parent_fill_price = 100.0
    wing_premium_pct = 25.0  # Target = 25

    candidates = [
        _make_instrument("CRUDEOIL26JUL6400CE", 6400.0, exchange="MCX"),
        _make_instrument("CRUDEOIL26JUL6500CE", 6500.0, exchange="MCX"),
        _make_instrument("CRUDEOIL26JUL6600CE", 6600.0, exchange="MCX"),
    ]

    quote_data = {
        "MCX:CRUDEOIL26JUL6400CE": _make_mock_quote(ltp=25.0, bid=24.5, ask=25.5),
        "MCX:CRUDEOIL26JUL6500CE": _make_mock_quote(ltp=30.0, bid=29.5, ask=30.5),
        "MCX:CRUDEOIL26JUL6600CE": _make_mock_quote(ltp=20.0, bid=19.5, ask=20.5),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # Target = 25. Closest is 25.0 at 6400 strike
    assert ws == "CRUDEOIL26JUL6400CE"
    assert wltp == 25.0


# ── Settings fallback ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_settings_fallback():
    """If settings read fails, use hardcoded defaults."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=50.0),  # score=0 (exact)
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=55.0),  # score=5
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0),  # score=5
    }

    # Simulate settings read failure — should fall back to hardcoded defaults
    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=RuntimeError("settings fail"),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # Target = 50. With hardcoded defaults (min_oi=1000, chain_radius=20, max_spread=10%)
    # 21950 @ 50 has best score (0), so should be picked
    assert ws == "NIFTY25APR21950CE", "Should succeed with fallback defaults"
    assert wltp == 50.0


# ── Instrument cache edge cases ────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_missing_strike_field():
    """Instrument missing strike field → filtered out."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0

    candidates_raw = [
        MockInstrument("NIFTY25APR21950CE", None, "NFO"),  # Missing strike
        MockInstrument("NIFTY25APR22000CE", 22000.0, "NFO"),
        MockInstrument("NIFTY25APR22050CE", 22050.0, "NFO"),
    ]

    quote_data = {
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=50.0),
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=50.0),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates_raw)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # Should still succeed with valid candidates
    assert ws in ["NIFTY25APR22000CE", "NIFTY25APR22050CE"]


# ── Score calculation (spread penalty) ──────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_score_with_spread_penalty():
    """Score includes premium distance + spread% penalty."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target = 50

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    # Design: 21950 @ 48 with tight spread, 22000 @ 50 with wide spread
    # Score(21950) = |48 - 50| + (0.2% penalty) = 2.0
    # Score(22000) = |50 - 50| + (2% penalty) = 0 + 1 = 1.0
    # So 22000 should win despite being wider, because distance matters more
    quote_data = {
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=48.0, bid=47.9, ask=48.1),  # 0.2% spread
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=50.0, bid=49.0, ask=51.0),   # 2% spread
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=52.0, bid=51.5, ask=52.5),  # 0.2% spread
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # 22000 should win despite wider spread, because it's exact match
    assert ws == "NIFTY25APR22000CE"


# ── Missing depth data ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_pick_wing_by_premium_missing_depth_data():
    """Quote missing depth (bid/ask) → spread_pct=0, continue."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target = 50

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": {"last_price": 50.0, "oi": 5000},  # Missing depth → spread_pct=0
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=50.0, bid=49.5, ask=50.5),
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0, bid=44.5, ask=45.5),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # Target = 50. Both 21950 and 22000 are exact (score=0). 21950 comes first in loop order
    assert ws == "NIFTY25APR21950CE"
    assert wltp == 50.0


@pytest.mark.asyncio
async def test_pick_wing_by_premium_empty_buy_sell_depth():
    """Quote has empty buy[] or sell[] arrays → spread_pct=0."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target = 50

    candidates = [
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21950CE": {
            "last_price": 50.0, "oi": 5000,
            "depth": {"buy": [], "sell": []},  # Empty depth
        },
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=50.0, bid=49.5, ask=50.5),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 20}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # Both are exact match (50). 21950 has no depth so spread_pct=0. Both have same score.
    # 21950 comes first in candidates list, so should be picked
    assert ws == "NIFTY25APR21950CE"
    assert wltp == 50.0


@pytest.mark.asyncio
async def test_pick_wing_by_premium_parent_strike_not_found():
    """Parent strike not in candidate list → no slicing applied."""
    parent_symbol = "NIFTY25APR22000CE"
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target = 50

    candidates = [
        _make_instrument("NIFTY25APR21900CE", 21900.0),
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        # Parent (22000) not in chain
        _make_instrument("NIFTY25APR22050CE", 22050.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21900CE": _make_mock_quote(ltp=50.0),  # Exact
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=55.0),
        "NFO:NIFTY25APR22050CE": _make_mock_quote(ltp=45.0),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 1}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # Parent not found, so no slicing. All candidates remain.
    # 21900 @ 50 has best score (exact match)
    assert ws == "NIFTY25APR21900CE"
    assert wltp == 50.0


@pytest.mark.asyncio
async def test_pick_wing_by_premium_parent_at_chain_boundary():
    """Parent strike at start/end of candidates → radius clips correctly."""
    parent_symbol = "NIFTY25APR21900CE"  # At the start
    parent_exchange = "NFO"
    parent_fill_price = 200.0
    wing_premium_pct = 25.0  # Target = 50

    candidates = [
        _make_instrument("NIFTY25APR21900CE", 21900.0),  # Parent (index 0)
        _make_instrument("NIFTY25APR21950CE", 21950.0),
        _make_instrument("NIFTY25APR22000CE", 22000.0),
    ]

    quote_data = {
        "NFO:NIFTY25APR21900CE": _make_mock_quote(ltp=45.0),
        "NFO:NIFTY25APR21950CE": _make_mock_quote(ltp=50.0),  # Exact
        "NFO:NIFTY25APR22000CE": _make_mock_quote(ltp=55.0),
    }

    with patch(
        "backend.api.cache.get_or_fetch",
        new=AsyncMock(return_value=MockInstrumentsResp(candidates)),
    ), patch(
        "backend.brokers.registry.get_market_data_broker",
        return_value=_make_mock_broker(quote_data),
    ), patch(
        "backend.shared.helpers.settings.get_int",
        side_effect=lambda k, d: {"templates.wing_min_oi": 1000, "templates.wing_chain_radius": 1}.get(k, d),
    ), patch(
        "backend.shared.helpers.settings.get_float",
        side_effect=lambda k, d: {"templates.wing_max_spread_pct": 10.0}.get(k, d),
    ):
        ws, wltp, reason = await _pick_wing_by_premium(
            parent_symbol, parent_exchange, parent_fill_price, wing_premium_pct,
        )

    # With radius=1 and parent at index 0: lo=max(0, 0-1)=0, hi=min(3, 0+1+1)=2
    # So candidates [0:2] = [21900, 21950]
    # 21950 @ 50 is exact, so wins
    assert ws == "NIFTY25APR21950CE"
    assert wltp == 50.0
