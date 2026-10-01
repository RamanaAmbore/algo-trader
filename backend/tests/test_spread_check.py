"""
Tests for backend/api/algo/spread_check.py — the reusable Chain-tab
pre-submission bid-ask spread gate.

Test dimensions:
  SSOT   — spread_pct formula matches template_attach._ta_wing_depth_spread
           (ltp-denominator), never raises on bad input
  Perf   — check_spreads batches one quote call for N legs (via quote_fn)
  Stale  — a missing/zero bid-ask degrades to a clear no_quote result,
           never a false "ok"
  Reuse  — quote_fn test seam avoids mocking the broker directly
  UX     — reason strings are populated on every non-ok status
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from backend.api.algo.spread_check import (
    SpreadCheckResult,
    evaluate_spread,
    check_spreads,
    resolve_max_spread_pct,
)


def _quote(ltp: float = 100.0, bid: float = 95.0, ask: float = 105.0) -> dict:
    depth = {}
    if bid:
        depth["buy"] = [{"price": bid}]
    if ask:
        depth["sell"] = [{"price": ask}]
    return {"last_price": ltp, "depth": depth}


# ── evaluate_spread — happy path ──────────────────────────────────────────

class TestEvaluateSpreadHappyPath:
    def test_tight_spread_within_threshold_is_ok(self):
        # bid=99, ask=101, ltp=100 → spread = 2/100 = 2% <= 10% threshold
        q = _quote(ltp=100.0, bid=99.0, ask=101.0)
        r = evaluate_spread(q, tradingsymbol="NIFTY24OCT25000CE", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "ok"
        assert r.ok is True
        assert r.spread_pct == pytest.approx(2.0)
        assert r.basis == "ltp"
        assert r.bid == 99.0 and r.ask == 101.0
        assert r.reason is None

    def test_wide_spread_exceeding_threshold_is_wide_not_ok(self):
        # bid=90, ask=110, ltp=100 → spread = 20/100 = 20% > 10% threshold
        q = _quote(ltp=100.0, bid=90.0, ask=110.0)
        r = evaluate_spread(q, tradingsymbol="GOLDM26OCT80000CE", exchange="MCX", max_spread_pct=10.0)
        assert r.status == "wide"
        assert r.ok is False
        assert r.spread_pct == pytest.approx(20.0)
        assert "exceeds threshold" in r.reason

    def test_spread_exactly_at_threshold_is_ok_inclusive(self):
        # spread == max_spread_pct is NOT a breach — matches
        # _wing_scan_candidates' `spread_pct > max_spread_pct` gate.
        q = _quote(ltp=100.0, bid=95.0, ask=105.0)  # spread = 10%
        r = evaluate_spread(q, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "ok"
        assert r.ok is True

    def test_falls_back_to_mid_when_ltp_zero(self):
        # ltp=0 (quote still warming up) but bid/ask present → mid basis.
        q = {"last_price": 0, "depth": {"buy": [{"price": 48.0}], "sell": [{"price": 52.0}]}}
        r = evaluate_spread(q, tradingsymbol="X", exchange="NFO", max_spread_pct=50.0)
        assert r.basis == "mid"
        # mid = 50, spread = 4/50 = 8%
        assert r.spread_pct == pytest.approx(8.0)
        assert r.status == "ok"


# ── evaluate_spread — zero / invalid bid-ask handling ─────────────────────

class TestEvaluateSpreadInvalidQuote:
    def test_zero_bid_is_no_quote_not_ok(self):
        q = _quote(ltp=100.0, bid=0.0, ask=105.0)
        r = evaluate_spread(q, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "no_quote"
        assert r.ok is False
        assert r.spread_pct is None
        assert "bid/ask unavailable" in r.reason

    def test_zero_ask_is_no_quote_not_ok(self):
        q = _quote(ltp=100.0, bid=95.0, ask=0.0)
        r = evaluate_spread(q, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "no_quote"
        assert r.ok is False

    def test_missing_depth_is_no_quote(self):
        q = {"last_price": 100.0}
        r = evaluate_spread(q, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "no_quote"
        assert r.ok is False

    def test_empty_quote_dict_is_no_quote_never_raises(self):
        r = evaluate_spread({}, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "no_quote"
        assert r.ok is False

    def test_none_quote_is_no_quote_never_raises(self):
        r = evaluate_spread(None, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "no_quote"
        assert r.ok is False

    def test_malformed_depth_entry_degrades_to_error_not_exception(self):
        # depth entries that aren't dicts must never raise up to the caller.
        q = {"last_price": 100.0, "depth": {"buy": ["not-a-dict"], "sell": [{"price": 105.0}]}}
        r = evaluate_spread(q, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        assert r.status == "error"
        assert r.ok is False
        assert r.reason is not None

    def test_to_dict_round_trips_all_fields(self):
        q = _quote()
        r = evaluate_spread(q, tradingsymbol="X", exchange="NFO", max_spread_pct=10.0)
        d = r.to_dict()
        assert d["status"] == r.status
        assert d["tradingsymbol"] == "X"
        assert d["exchange"] == "NFO"


# ── check_spreads — batched async quote fetch ─────────────────────────────

class TestCheckSpreads:
    @pytest.mark.asyncio
    async def test_empty_legs_returns_empty_list(self):
        assert await check_spreads([], 10.0) == []

    @pytest.mark.asyncio
    async def test_happy_path_parent_and_wing_legs_via_quote_fn(self):
        """quote_fn test seam — no broker mock needed."""
        legs = [
            {"tradingsymbol": "NIFTY24OCT25000CE", "exchange": "NFO", "role": "parent"},
            {"tradingsymbol": "NIFTY24OCT25500CE", "exchange": "NFO", "role": "wing"},
        ]

        def _fake_quote_fn(keys):
            assert keys == [
                "NFO:NIFTY24OCT25000CE",
                "NFO:NIFTY24OCT25500CE",
            ]
            return {
                "NFO:NIFTY24OCT25000CE": _quote(ltp=100.0, bid=99.0, ask=101.0),
                "NFO:NIFTY24OCT25500CE": _quote(ltp=50.0, bid=40.0, ask=60.0),  # wide
            }

        results = await check_spreads(legs, 10.0, quote_fn=_fake_quote_fn)
        assert len(results) == 2
        assert results[0].role == "parent" and results[0].status == "ok"
        assert results[1].role == "wing" and results[1].status == "wide"

    @pytest.mark.asyncio
    async def test_quote_fn_may_be_async(self):
        async def _async_quote_fn(keys):
            return {"NFO:X": _quote(ltp=100.0, bid=99.0, ask=101.0)}

        results = await check_spreads(
            [{"tradingsymbol": "X", "exchange": "NFO"}], 10.0, quote_fn=_async_quote_fn,
        )
        assert results[0].status == "ok"

    @pytest.mark.asyncio
    async def test_quote_fetch_raising_degrades_every_leg_to_error_not_exception(self):
        def _raising_quote_fn(keys):
            raise RuntimeError("broker down")

        legs = [
            {"tradingsymbol": "A", "exchange": "NFO", "role": "parent"},
            {"tradingsymbol": "B", "exchange": "NFO", "role": "wing"},
        ]
        results = await check_spreads(legs, 10.0, quote_fn=_raising_quote_fn)
        assert len(results) == 2
        assert all(r.status == "error" and r.ok is False for r in results)
        assert "broker down" in results[0].reason

    @pytest.mark.asyncio
    async def test_missing_key_in_quote_response_is_no_quote(self):
        """Broker returns a dict that doesn't include one of the requested keys
        (instrument unknown / delisted) — never KeyErrors."""
        def _partial_quote_fn(keys):
            return {"NFO:A": _quote()}  # "NFO:B" missing

        legs = [
            {"tradingsymbol": "A", "exchange": "NFO", "role": "parent"},
            {"tradingsymbol": "B", "exchange": "NFO", "role": "wing"},
        ]
        results = await check_spreads(legs, 10.0, quote_fn=_partial_quote_fn)
        assert results[0].status == "ok"
        assert results[1].status == "no_quote"
        assert "no quote returned" in results[1].reason

    @pytest.mark.asyncio
    async def test_default_path_uses_get_market_data_broker(self):
        """No quote_fn supplied — falls through to the real broker-registry
        resolution (mocked here the same way test_pick_wing_by_premium.py
        mocks the market-data broker for template_attach.py)."""
        mock_broker = MagicMock()
        mock_broker.quote.return_value = {"NFO:X": _quote(ltp=100.0, bid=99.0, ask=101.0)}
        with patch(
            "backend.brokers.registry.get_market_data_broker",
            return_value=mock_broker,
        ):
            results = await check_spreads([{"tradingsymbol": "X", "exchange": "NFO"}], 10.0)
        assert results[0].status == "ok"
        mock_broker.quote.assert_called_once_with(["NFO:X"])


# ── resolve_max_spread_pct — override > template > setting ────────────────

class TestResolveMaxSpreadPct:
    def test_override_wins_over_template_and_setting(self):
        template = {"wing_max_spread_pct": 7.0}
        overrides = {"wing_max_spread_pct": 3.0}
        value, source = resolve_max_spread_pct(template, overrides)
        assert value == 3.0
        assert source == "override"

    def test_template_wins_when_no_override(self):
        template = {"wing_max_spread_pct": 7.0}
        value, source = resolve_max_spread_pct(template, {})
        assert value == 7.0
        assert source == "template"

    def test_falls_back_to_global_setting_when_neither_set(self):
        with patch("backend.shared.helpers.settings.get_float", return_value=12.5):
            value, source = resolve_max_spread_pct(None, None)
        assert value == 12.5
        assert source == "setting"

    def test_template_without_the_key_falls_back_to_setting(self):
        """Older template row / system default with no wing_max_spread_pct
        column value set — must not KeyError, must fall through."""
        with patch("backend.shared.helpers.settings.get_float", return_value=10.0):
            value, source = resolve_max_spread_pct({"name": "default-bull"}, {})
        assert value == 10.0
        assert source == "setting"

    def test_non_numeric_override_falls_through_to_template(self):
        template = {"wing_max_spread_pct": 7.0}
        overrides = {"wing_max_spread_pct": "not-a-number"}
        value, source = resolve_max_spread_pct(template, overrides)
        assert value == 7.0
        assert source == "template"

    def test_none_overrides_dict_does_not_raise(self):
        with patch("backend.shared.helpers.settings.get_float", return_value=10.0):
            value, source = resolve_max_spread_pct(None, None)
        assert value == 10.0
        assert source == "setting"

    def test_settings_lookup_failure_falls_back_to_hardcoded_default(self):
        """Never raise even if the settings layer itself is unavailable."""
        with patch(
            "backend.shared.helpers.settings.get_float",
            side_effect=RuntimeError("db down"),
        ):
            value, source = resolve_max_spread_pct(None, None)
        assert value == 0.5
        assert source == "setting"
