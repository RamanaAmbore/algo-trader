"""
Tests for Part A's hedge-leg strike search —
`backend.api.algo.hedge_strike.resolve_hedge_strike()` /
`fetch_hedge_quotes()`.

Mirrors the frontend's own `resolveHedgeStrike()` test coverage (built
independently, same spec — see `.claude/PLAN.md` Part A) — covers
direction (CE/PE), side-premium pricing (BUY reads ask, SELL reads
bid, with fallback), depth rejection, the ±50%-of-target tolerance
band (including the exact boundary), nearest-to-target tie-break, and
structured failure reasons for every invalid-input case.
"""
import pytest

from backend.api.algo.hedge_strike import resolve_hedge_strike, fetch_hedge_quotes


# ═══════════════════════════════════════════════════════════════════
#  resolve_hedge_strike — direction + side pricing
# ═══════════════════════════════════════════════════════════════════

class TestDirectionAndPricing:
    def test_ce_hedge_searches_strikes_above_parent(self):
        """Parent CE at 22500; hedge candidates only above it are eligible."""
        candidates = {
            22400.0: {"ce": {"bid": 100.0, "ask": 105.0}},  # below parent — ignored
            22600.0: {"ce": {"bid": 48.0, "ask": 50.0}},    # above — ratio 50% at ask=50 (BUY hedge reads bid for SELL... see below)
        }
        # Parent BUY → hedge is SELL → hedge reads bid.
        result = resolve_hedge_strike(
            parent_opt_type="CE", parent_strike=22500.0, parent_side="BUY",
            parent_premium=100.0, target_pct=50.0, candidates=candidates,
        )
        assert result["ok"] is True
        assert result["strike"] == 22600.0
        assert result["price"] == 48.0
        assert result["ratio"] == pytest.approx(48.0)

    def test_pe_hedge_searches_strikes_below_parent(self):
        """Parent PE at 22500; hedge candidates only below it are eligible."""
        candidates = {
            22600.0: {"pe": {"bid": 95.0, "ask": 100.0}},   # above parent — ignored
            22400.0: {"pe": {"bid": 23.0, "ask": 25.0}},    # below — eligible
        }
        # Parent SELL → hedge is BUY → hedge reads ask.
        result = resolve_hedge_strike(
            parent_opt_type="PE", parent_strike=22500.0, parent_side="SELL",
            parent_premium=100.0, target_pct=25.0, candidates=candidates,
        )
        assert result["ok"] is True
        assert result["strike"] == 22400.0
        assert result["price"] == 25.0

    def test_buy_parent_hedge_side_is_sell_reads_bid(self):
        candidates = {22600.0: {"ce": {"bid": 50.0, "ask": 55.0}}}
        result = resolve_hedge_strike(
            "CE", 22500.0, "BUY", 100.0, 50.0, candidates,
        )
        assert result["ok"] is True
        assert result["price"] == 50.0  # bid, not ask

    def test_sell_parent_hedge_side_is_buy_reads_ask(self):
        candidates = {22600.0: {"ce": {"bid": 48.0, "ask": 50.0}}}
        result = resolve_hedge_strike(
            "CE", 22500.0, "SELL", 100.0, 50.0, candidates,
        )
        assert result["ok"] is True
        assert result["price"] == 50.0  # ask, not bid

    def test_missing_preferred_side_falls_back_to_the_other(self):
        """BUY hedge wants ask; ask absent falls back to bid."""
        candidates = {22600.0: {"ce": {"bid": 50.0, "ask": None}}}
        result = resolve_hedge_strike(
            "CE", 22500.0, "SELL", 100.0, 50.0, candidates,
        )
        assert result["ok"] is True
        assert result["price"] == 50.0

        candidates2 = {22600.0: {"ce": {"bid": None, "ask": 50.0}}}
        result2 = resolve_hedge_strike(
            "CE", 22500.0, "BUY", 100.0, 50.0, candidates2,
        )
        assert result2["ok"] is True
        assert result2["price"] == 50.0


# ═══════════════════════════════════════════════════════════════════
#  Depth rejection
# ═══════════════════════════════════════════════════════════════════

class TestDepthRejection:
    def test_zero_price_candidate_rejected(self):
        candidates = {
            22600.0: {"ce": {"bid": 0.0, "ask": 0.0}},
            22700.0: {"ce": {"bid": 48.0, "ask": 50.0}},
        }
        result = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, 50.0, candidates)
        assert result["ok"] is True
        assert result["strike"] == 22700.0

    def test_no_depth_backed_quote_for_any_candidate(self):
        candidates = {
            22600.0: {"ce": {"bid": None, "ask": None}},
            22700.0: {"ce": {"bid": 0.0, "ask": 0.0}},
        }
        result = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, 50.0, candidates)
        assert result["ok"] is False
        assert "depth" in result["reason"]


# ═══════════════════════════════════════════════════════════════════
#  Tolerance band — exact boundary, nearest-to-target tie-break
# ═══════════════════════════════════════════════════════════════════

class TestTolerance:
    def test_exact_lower_boundary_accepted(self):
        """target=50 → low bound is exactly 25 (±50%). ratio==25 accepted."""
        candidates = {22600.0: {"ce": {"bid": 25.0, "ask": 25.0}}}
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is True
        assert result["ratio"] == pytest.approx(25.0)

    def test_exact_upper_boundary_accepted(self):
        candidates = {22600.0: {"ce": {"bid": 75.0, "ask": 75.0}}}
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is True
        assert result["ratio"] == pytest.approx(75.0)

    def test_just_outside_lower_boundary_rejected(self):
        candidates = {22600.0: {"ce": {"bid": 24.99, "ask": 24.99}}}
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is False

    def test_just_outside_upper_boundary_rejected(self):
        candidates = {22600.0: {"ce": {"bid": 75.01, "ask": 75.01}}}
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is False

    def test_nearest_to_target_wins_among_in_tolerance_candidates(self):
        candidates = {
            22600.0: {"ce": {"bid": 30.0, "ask": 30.0}},  # ratio 30, diff 20
            22700.0: {"ce": {"bid": 48.0, "ask": 48.0}},  # ratio 48, diff 2 — nearest
            22800.0: {"ce": {"bid": 60.0, "ask": 60.0}},  # ratio 60, diff 10
        }
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is True
        assert result["strike"] == 22700.0
        assert result["ratio"] == pytest.approx(48.0)

    def test_tie_broken_by_search_order_closer_strike_first(self):
        """Two candidates equidistant from target → the one checked
        first (closer to parent strike, for CE = ascending) wins."""
        candidates = {
            22600.0: {"ce": {"bid": 45.0, "ask": 45.0}},  # diff 5, checked first
            22700.0: {"ce": {"bid": 55.0, "ask": 55.0}},  # diff 5, checked second
        }
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is True
        assert result["strike"] == 22600.0

    def test_no_further_otm_strikes_quoted(self):
        candidates = {22400.0: {"ce": {"bid": 100.0, "ask": 105.0}}}  # only below parent
        result = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, 50.0, candidates)
        assert result["ok"] is False
        assert "no further-OTM" in result["reason"]

    def test_no_match_within_tolerance_never_falls_back(self):
        candidates = {22600.0: {"ce": {"bid": 5.0, "ask": 5.0}}}  # ratio 5%, way off
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is False
        assert "strike" not in result


# ═══════════════════════════════════════════════════════════════════
#  Invalid input handling — never raises
# ═══════════════════════════════════════════════════════════════════

class TestInvalidInput:
    def test_invalid_option_type(self):
        result = resolve_hedge_strike("XX", 22500.0, "BUY", 100.0, 50.0, {1: {}})
        assert result["ok"] is False
        assert "option type" in result["reason"]

    def test_invalid_side(self):
        result = resolve_hedge_strike("CE", 22500.0, "HOLD", 100.0, 50.0, {1: {}})
        assert result["ok"] is False
        assert "side" in result["reason"]

    def test_zero_or_negative_premium(self):
        result = resolve_hedge_strike("CE", 22500.0, "BUY", 0.0, 50.0, {1: {}})
        assert result["ok"] is False
        result2 = resolve_hedge_strike("CE", 22500.0, "BUY", -5.0, 50.0, {1: {}})
        assert result2["ok"] is False

    def test_zero_or_negative_target_pct(self):
        result = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, 0.0, {1: {}})
        assert result["ok"] is False
        result2 = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, -10.0, {1: {}})
        assert result2["ok"] is False

    def test_non_numeric_strike_or_premium_or_target(self):
        result = resolve_hedge_strike("CE", "not-a-number", "BUY", 100.0, 50.0, {1: {}})
        assert result["ok"] is False
        assert "numeric" in result["reason"]

    def test_empty_candidates(self):
        result = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, 50.0, {})
        assert result["ok"] is False
        assert "no quotes" in result["reason"]

    def test_non_dict_candidates(self):
        result = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, 50.0, None)
        assert result["ok"] is False

    def test_malformed_candidate_strike_keys(self):
        result = resolve_hedge_strike(
            "CE", 22500.0, "BUY", 100.0, 50.0, {"not-a-strike": {"ce": {"bid": 1, "ask": 1}}},
        )
        assert result["ok"] is False
        assert "malformed" in result["reason"]

    def test_never_raises_on_garbage_input(self):
        """Pure-function invariant — invalid input is always a
        structured failure, never an exception."""
        for bad in (object(), [], 1.0, "x"):
            result = resolve_hedge_strike("CE", 22500.0, "BUY", 100.0, 50.0, bad)
            assert result["ok"] is False


# ═══════════════════════════════════════════════════════════════════
#  fetch_hedge_quotes — reuses options.py's chain-quote helpers
# ═══════════════════════════════════════════════════════════════════

class TestFetchHedgeQuotes:
    @pytest.mark.asyncio
    async def test_cold_instruments_cache_returns_empty_dict(self):
        from unittest.mock import patch
        with patch("backend.api.cache.peek", return_value=None):
            result = await fetch_hedge_quotes("NIFTY", "2026-04-28", "ZG0790")
        assert result == {}

    @pytest.mark.asyncio
    async def test_blank_underlying_or_expiry_returns_empty_dict(self):
        result = await fetch_hedge_quotes("", "2026-04-28", "ZG0790")
        assert result == {}
        result2 = await fetch_hedge_quotes("NIFTY", "", "ZG0790")
        assert result2 == {}

    @pytest.mark.asyncio
    async def test_happy_path_reuses_options_chain_quote_helpers(self):
        """Builds the lowercase {strike: {ce:{bid,ask,sym}, pe:{...}}}
        shape resolve_hedge_strike() consumes, from the exact
        book_by_strike shape _chain_quotes_build_book() returns."""
        from unittest.mock import patch, AsyncMock, MagicMock

        fake_inst_resp = MagicMock()
        fake_book = {
            22600.0: {
                "CE": {"bid": 48.0, "ask": 50.0, "depth_available": True,
                       "sym": "NIFTY26APR22600CE", "ls": 50, "e": "NFO"},
                "PE": {"bid": 10.0, "ask": 12.0, "depth_available": True,
                       "sym": "NIFTY26APR22600PE", "ls": 50, "e": "NFO"},
            },
        }

        with patch("backend.api.cache.peek", return_value=fake_inst_resp), \
             patch("backend.api.routes.options._chain_quotes_sym_lookup",
                   new=AsyncMock(return_value=({22600.0: {}}, ["2026-04-28"]))), \
             patch("backend.api.routes.options._chain_quotes_batch_quote",
                   new=AsyncMock(return_value=({}, {}))), \
             patch("backend.api.routes.options._chain_quotes_build_book",
                   return_value=fake_book):
            result = await fetch_hedge_quotes("NIFTY", "2026-04-28", "ZG0790")

        assert result == {
            22600.0: {
                "ce": {"bid": 48.0, "ask": 50.0, "sym": "NIFTY26APR22600CE", "depth_available": True},
                "pe": {"bid": 10.0, "ask": 12.0, "sym": "NIFTY26APR22600PE", "depth_available": True},
            },
        }

    @pytest.mark.asyncio
    async def test_any_exception_degrades_to_empty_dict_never_raises(self):
        from unittest.mock import patch
        with patch("backend.api.cache.peek", side_effect=RuntimeError("boom")):
            result = await fetch_hedge_quotes("NIFTY", "2026-04-28", "ZG0790")
        assert result == {}


# ═══════════════════════════════════════════════════════════════════
#  Depth-rejection through the REAL (unmocked) options.py book builder
# ═══════════════════════════════════════════════════════════════════

class TestDepthRejectionThroughRealBookBuilder:
    @pytest.mark.asyncio
    async def test_depth_unavailable_propagates_and_is_rejected(self):
        """End-to-end through the REAL, unmocked `_chain_quotes_build_book`
        (only `_chain_quotes_sym_lookup`/`_chain_quotes_batch_quote` are
        mocked) — an LTP-only quote with no real depth array must come
        back tagged `depth_available=False`, and `resolve_hedge_strike()`
        must then reject it even though bid/ask are both positive (both
        fell back to `last_price`)."""
        from unittest.mock import patch, AsyncMock, MagicMock
        from backend.api.algo.hedge_strike import fetch_hedge_quotes, resolve_hedge_strike

        fake_inst_resp = MagicMock()
        sym_by_strike = {
            22600.0: {
                "CE": {"sym": "NIFTY26APR22600CE", "ls": 50, "e": "NFO"},
                "PE": {"sym": "", "ls": 1, "e": ""},
            },
        }
        # No "depth" key at all → _chain_quotes_bid_ask_from_q's real
        # fallback path: both bid and ask come from last_price,
        # depth_available=False.
        quote_resp = {"NFO:NIFTY26APR22600CE": {"last_price": 48.0}}
        key_meta = {"NFO:NIFTY26APR22600CE": (22600.0, "CE")}

        with patch("backend.api.cache.peek", return_value=fake_inst_resp), \
             patch("backend.api.routes.options._chain_quotes_sym_lookup",
                   new=AsyncMock(return_value=(sym_by_strike, ["2026-04-28"]))), \
             patch("backend.api.routes.options._chain_quotes_batch_quote",
                   new=AsyncMock(return_value=(quote_resp, key_meta))):
            result = await fetch_hedge_quotes("NIFTY", "2026-04-28", "ZG0790")

        assert result[22600.0]["ce"]["bid"] == 48.0
        assert result[22600.0]["ce"]["ask"] == 48.0
        assert result[22600.0]["ce"]["depth_available"] is False

        hedge_result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, result)
        assert hedge_result["ok"] is False

    def test_absent_depth_available_key_still_accepted(self):
        """A hand-built candidates dict (e.g. direct unit tests of
        resolve_hedge_strike) that omits `depth_available` entirely must
        still work — the default is True, never a silent rejection."""
        from backend.api.algo.hedge_strike import resolve_hedge_strike

        candidates = {22600.0: {"ce": {"bid": 48.0, "ask": 48.0}}}  # no depth_available key
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is True

    def test_explicit_depth_available_true_accepted(self):
        from backend.api.algo.hedge_strike import resolve_hedge_strike

        candidates = {22600.0: {"ce": {"bid": 48.0, "ask": 48.0, "depth_available": True}}}
        result = resolve_hedge_strike("CE", 22500.0, "SELL", 100.0, 50.0, candidates)
        assert result["ok"] is True


# ═══════════════════════════════════════════════════════════════════
#  candidates_match_parent_symbol — expiry-mismatch sanity check
# ═══════════════════════════════════════════════════════════════════

class TestCandidatesMatchParentSymbol:
    def test_matching_symbol_at_parent_strike(self):
        from backend.api.algo.hedge_strike import candidates_match_parent_symbol

        candidates = {22500.0: {"ce": {"sym": "NIFTY26APR22500CE"}}}
        assert candidates_match_parent_symbol(candidates, 22500.0, "CE", "NIFTY26APR22500CE") is True

    def test_mismatched_symbol_at_parent_strike(self):
        """The resolved chain has an entry at this strike, but it's a
        DIFFERENT contract — e.g. the wrong expiry's chain was fetched."""
        from backend.api.algo.hedge_strike import candidates_match_parent_symbol

        candidates = {22500.0: {"ce": {"sym": "NIFTY26MAY22500CE"}}}
        assert candidates_match_parent_symbol(candidates, 22500.0, "CE", "NIFTY26APR22500CE") is False

    def test_parent_strike_entirely_absent(self):
        from backend.api.algo.hedge_strike import candidates_match_parent_symbol

        candidates = {22600.0: {"ce": {"sym": "NIFTY26APR22600CE"}}}
        assert candidates_match_parent_symbol(candidates, 22500.0, "CE", "NIFTY26APR22500CE") is False

    def test_empty_sym_at_parent_strike(self):
        from backend.api.algo.hedge_strike import candidates_match_parent_symbol

        candidates = {22500.0: {"ce": {"sym": ""}}}
        assert candidates_match_parent_symbol(candidates, 22500.0, "CE", "NIFTY26APR22500CE") is False

    def test_malformed_input_never_raises(self):
        from backend.api.algo.hedge_strike import candidates_match_parent_symbol

        assert candidates_match_parent_symbol(None, 22500.0, "CE", "X") is False
        assert candidates_match_parent_symbol({}, "not-a-number", "CE", "X") is False
        assert candidates_match_parent_symbol({22500.0: None}, 22500.0, "CE", "X") is False


# ═══════════════════════════════════════════════════════════════════
#  resolve_parent_real_expiry — prefer the instruments-cache row over
#  parse_tradingsymbol()'s calendar approximation
# ═══════════════════════════════════════════════════════════════════

class TestResolveParentRealExpiry:
    @pytest.mark.asyncio
    async def test_cold_cache_falls_back(self):
        from unittest.mock import patch
        from backend.api.algo.hedge_strike import resolve_parent_real_expiry

        with patch("backend.api.cache.peek", return_value=None):
            result = await resolve_parent_real_expiry("NIFTY26APR22500CE", "2026-04-28")
        assert result == "2026-04-28"

    @pytest.mark.asyncio
    async def test_matching_instrument_row_wins_over_fallback(self):
        from unittest.mock import patch, MagicMock
        from backend.api.algo.hedge_strike import resolve_parent_real_expiry

        inst = MagicMock()
        inst.s = "NIFTY26APR22500CE"
        inst.x = "2026-04-24"  # holiday-shifted real expiry, differs from the approximation
        inst_resp = MagicMock()
        inst_resp.items = [inst]

        with patch("backend.api.cache.peek", return_value=inst_resp):
            result = await resolve_parent_real_expiry("NIFTY26APR22500CE", "2026-04-28")
        assert result == "2026-04-24"

    @pytest.mark.asyncio
    async def test_no_matching_row_falls_back(self):
        from unittest.mock import patch, MagicMock
        from backend.api.algo.hedge_strike import resolve_parent_real_expiry

        inst = MagicMock()
        inst.s = "NIFTY26APR22600CE"
        inst.x = "2026-04-24"
        inst_resp = MagicMock()
        inst_resp.items = [inst]

        with patch("backend.api.cache.peek", return_value=inst_resp):
            result = await resolve_parent_real_expiry("NIFTY26APR22500CE", "2026-04-28")
        assert result == "2026-04-28"

    @pytest.mark.asyncio
    async def test_exception_falls_back_never_raises(self):
        from unittest.mock import patch
        from backend.api.algo.hedge_strike import resolve_parent_real_expiry

        with patch("backend.api.cache.peek", side_effect=RuntimeError("boom")):
            result = await resolve_parent_real_expiry("NIFTY26APR22500CE", "2026-04-28")
        assert result == "2026-04-28"
