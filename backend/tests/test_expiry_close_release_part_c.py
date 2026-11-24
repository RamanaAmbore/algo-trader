"""
Part C — the expiry-close hold/auto-release mechanism (2026-11-23).

This is the "it's going live within 3 days, make sure it works with no
fail" pass. Covers every function this plan changed:

  - `chase_tiers.resolve_chase_tier()` / `orders_helpers._live_chase_
    config()` parity (the shared, pure tier table extraction).
  - `order_hold._floor_minutes_for_tier()` / `_resolve_hold_auto_
    release_at()` — both policy shapes, the floor clamp, the "never
    earlier than now" clamp, and the "no policy / no close_time" cases.
  - `expiry._mcx_devolvement_future_symbol()` / `_future_matches_
    option_devolvement()` — a parity test against the REAL async
    `derivatives.lookup_future_for_option()` (plain monthly + the MCX
    day-suffix variant), plus the cross-month rejection this fix exists
    for.
  - The symmetric NTM-exclusion fix in `_best_opt_partner`/
    `_best_fut_partner`, including the mandatory same-contract
    carve-out (without which two existing fixtures in
    `test_scope_positions_expiring_today_mcx_unhedged.py` and
    `test_expiry_logic.py` — both a genuine same-strike NTM hedge —
    would break).
  - `close_positions()`'s `agent_id`-None refusal and its
    `release_policy`-driven `auto_release_at` wiring.
  - `_exp_verify_cancel_failed_leg()` / `_exp_leg_in_flight_this_
    session()`'s CANCEL_FAILED broker-truth re-verification — all
    three outcomes, and the "HELD/OPEN still block unconditionally"
    regression.
  - `background._agent_close_auto_release_sweep_once()`'s widened
    category set.
  - `_task_expiry_check` no longer calls `engine.run()` (source-level
    guard — the scheduling loop itself has its own pre-existing test
    coverage in `test_background_helpers.py`, untouched by this plan).
  - THE MANDATORY END-TO-END TEST: the real `expiry-day-itm-auto-close`
    condition → the real `close_positions()` → a real HELD `AlgoOrder`
    row (real sqlite DB) with both `agent_id` and `auto_release_at`
    genuinely set → simulated time passage → the real (widened)
    `_agent_close_auto_release_sweep_once()` → the real
    `release_held_order()` (position/quote checks run for real; only
    the broker boundary and `chase_order()` are mocked) → an assertion
    that a chase placement was actually made with the expected tier,
    side, qty, and identity.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


@pytest.fixture(autouse=True)
def _reset_alert_dedup_sets():
    """Both the auto-release failure dedup (`background.py`) and the
    CANCEL_FAILED stale-leg dedup (`expiry.py`) are in-process module-
    level sets keyed by AlgoOrder.id. Several tests below reuse the
    same small row ids (1, 7, 9, 10) — without a reset, whichever test
    runs first would silently dedup-suppress every later test's alert,
    making later assertions order-dependent and flaky."""
    from backend.api import background as bg
    from backend.api.algo import expiry as exp
    bg._AUTO_RELEASE_FAILED_ALERTED.clear()
    exp._CANCEL_FAILED_ALERTED.clear()
    yield
    bg._AUTO_RELEASE_FAILED_ALERTED.clear()
    exp._CANCEL_FAILED_ALERTED.clear()


# ═══════════════════════════════════════════════════════════════════════
# chase_tiers.py — shared pure tier table
# ═══════════════════════════════════════════════════════════════════════

class TestChaseTiersSharedTable:
    def test_resolve_chase_tier_known_values(self):
        from backend.api.algo.chase_tiers import resolve_chase_tier, CHASE_TIERS
        for level in ("low", "med", "high"):
            resolved_level, tier = resolve_chase_tier(level)
            assert resolved_level == level
            assert tier == CHASE_TIERS[level]

    def test_resolve_chase_tier_unknown_falls_back_to_low(self):
        from backend.api.algo.chase_tiers import resolve_chase_tier, CHASE_TIERS
        for bogus in (None, "", "bogus", "URGENT"):
            level, tier = resolve_chase_tier(bogus)
            assert level == "low"
            assert tier == CHASE_TIERS["low"]

    def test_live_chase_config_byte_identical_after_extraction(self):
        """_live_chase_config must build the EXACT same ChaseConfig
        shape it did before the chase_tiers.py extraction — same
        tuples, same unknown-aggressiveness->"low" fallback, same
        `cfg.level` stamp."""
        from backend.api.routes.orders_helpers import _live_chase_config

        high = _live_chase_config("high")
        assert (high.interval_seconds, high.aggression_step, high.max_attempts) == (10, 0.25, 10)
        assert high.level == "high"

        med = _live_chase_config("med")
        assert (med.interval_seconds, med.aggression_step, med.max_attempts) == (20, 0.10, 20)
        assert med.level == "med"

        low = _live_chase_config("low")
        assert (low.interval_seconds, low.aggression_step, low.max_attempts) == (30, 0.05, 30)
        assert low.level == "low"

        bogus = _live_chase_config("some-typo")
        assert (bogus.interval_seconds, bogus.aggression_step, bogus.max_attempts) == (30, 0.05, 30)
        assert bogus.level == "low", "unrecognised aggressiveness must stamp level='low', not the raw string"

    def test_order_hold_floor_reads_the_same_table(self):
        """order_hold._floor_minutes_for_tier must derive from the SAME
        chase_tiers table _live_chase_config reads — not a second,
        independently-maintained copy of the three tuples."""
        from backend.api.algo.order_hold import _floor_minutes_for_tier
        from backend.api.algo.chase_tiers import CHASE_TIERS

        for level in ("low", "med", "high"):
            expected_seconds = CHASE_TIERS[level]["interval_seconds"] * CHASE_TIERS[level]["max_attempts"]
            assert _floor_minutes_for_tier(level) == pytest.approx(expected_seconds / 60.0 + 1.0)


# ═══════════════════════════════════════════════════════════════════════
# order_hold._resolve_hold_auto_release_at — the generic resolver
# ═══════════════════════════════════════════════════════════════════════

class TestResolveHoldAutoReleaseAt:
    def test_none_or_empty_policy_returns_none(self):
        from backend.api.algo.order_hold import _resolve_hold_auto_release_at
        now = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
        assert _resolve_hold_auto_release_at(None, now, now, now) is None
        assert _resolve_hold_auto_release_at({}, now, now, now) is None
        assert _resolve_hold_auto_release_at({"unrelated": 1}, now, now, now) is None

    def test_before_close_min_fixed_deadline(self):
        from backend.api.algo.order_hold import _resolve_hold_auto_release_at
        now = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
        close = datetime(2026, 11, 26, 18, 25, tzinfo=timezone.utc)
        r = _resolve_hold_auto_release_at({"before_close_min": 25}, now, close, now)
        assert r == datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc)

    def test_before_close_min_with_no_close_time_returns_none(self):
        """A fixed deadline needs a real clock to anchor to — never guess."""
        from backend.api.algo.order_hold import _resolve_hold_auto_release_at
        now = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
        assert _resolve_hold_auto_release_at({"before_close_min": 25}, now, None, now) is None

    def test_after_min_rolling_deadline_well_before_floor(self):
        """The operator's own instruction: 'release it 30 mins if user
        does not respond'. Close is far away, so the rolling deadline
        is used as-is, not clamped by the floor."""
        from backend.api.algo.order_hold import _resolve_hold_auto_release_at
        now = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
        held_at = now
        close = now + timedelta(hours=5)
        r = _resolve_hold_auto_release_at(
            {"after_min": 30, "floor_tier": "high"}, now, close, held_at,
            default_chase_tier="high",
        )
        assert r == held_at + timedelta(minutes=30)

    def test_after_min_rolling_deadline_clamped_by_floor_near_close(self):
        """When close is imminent, the rolling deadline must never run
        later than close_time minus the tier's own worst-case chase
        duration (plus the fixed scan-latency margin)."""
        from backend.api.algo.order_hold import _resolve_hold_auto_release_at, _floor_minutes_for_tier
        now = datetime(2026, 11, 26, 23, 25, tzinfo=timezone.utc)
        held_at = now
        close = now + timedelta(minutes=2)  # close is in 2 minutes
        r = _resolve_hold_auto_release_at(
            {"after_min": 30, "floor_tier": "high"}, now, close, held_at,
            default_chase_tier="high",
        )
        floor_min = _floor_minutes_for_tier("high")
        expected_floor_deadline = close - timedelta(minutes=floor_min)
        # The floor deadline itself is before `now` here (2 min window is
        # narrower than the ~2.67+1 min floor), so the result clamps to `now`.
        assert expected_floor_deadline < now
        assert r == now

    def test_result_never_earlier_than_now(self):
        """A policy that resolves to an already-elapsed instant means
        'release on the very next scan', never a deadline in the past."""
        from backend.api.algo.order_hold import _resolve_hold_auto_release_at
        now = datetime(2026, 11, 26, 16, 0, tzinfo=timezone.utc)
        held_at = now - timedelta(hours=2)  # staged 2h ago
        close = now + timedelta(hours=1)
        r = _resolve_hold_auto_release_at(
            {"after_min": 5}, now, close, held_at, default_chase_tier="high",
        )
        # held_at + 5min is long past `now` — clamp to now.
        assert r == now

    def test_default_chase_tier_used_when_floor_tier_omitted(self):
        from backend.api.algo.order_hold import _resolve_hold_auto_release_at, _floor_minutes_for_tier
        now = datetime(2026, 11, 26, 23, 25, tzinfo=timezone.utc)
        close = now + timedelta(minutes=10)
        r_high_default = _resolve_hold_auto_release_at(
            {"after_min": 30}, now, close, now, default_chase_tier="high",
        )
        r_low_default = _resolve_hold_auto_release_at(
            {"after_min": 30}, now, close, now, default_chase_tier="low",
        )
        # 'low' has a much bigger floor (30*30/60+1=16min) than 'high'
        # (10*10/60+1≈2.67min), so the low-tier default clamps earlier.
        assert r_low_default < r_high_default


# ═══════════════════════════════════════════════════════════════════════
# expiry.py — futures-partner contract identity (parity + rejection)
# ═══════════════════════════════════════════════════════════════════════

def _future_month(months_out: int, day: int = 18):
    from zoneinfo import ZoneInfo
    import datetime as _dt_mod
    today = _dt_mod.datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if months_out == 0:
        d = today
    else:
        total = today.month + months_out - 1
        yr = today.year + total // 12
        mo = total % 12 + 1
        d = date(yr, mo, day)
    return f"{d.year % 100:02d}", d.strftime("%b").upper(), d.isoformat()


def _fake_inst(tradingsymbol: str, underlying: str, expiry: str) -> MagicMock:
    inst = MagicMock()
    inst.s = tradingsymbol
    inst.u = underlying
    inst.e = "MCX"
    inst.t = "FUT"
    inst.x = expiry
    return inst


def _fake_instruments_resp(items: list) -> MagicMock:
    resp = MagicMock()
    resp.items = items
    return resp


class TestFuturesDevolvementParity:
    def test_pure_helper_resolves_plain_monthly_symbol(self):
        from backend.api.algo.expiry import _mcx_devolvement_future_symbol
        assert _mcx_devolvement_future_symbol("CRUDEOIL25JUN5800CE") == "CRUDEOIL25JUNFUT"

    def test_pure_helper_returns_none_for_unparseable_symbol(self):
        from backend.api.algo.expiry import _mcx_devolvement_future_symbol
        assert _mcx_devolvement_future_symbol("BOGUS") is None
        assert _mcx_devolvement_future_symbol("") is None
        assert _mcx_devolvement_future_symbol(None) is None

    def test_future_matches_option_devolvement_exact(self):
        from backend.api.algo.expiry import _future_matches_option_devolvement
        assert _future_matches_option_devolvement("CRUDEOIL25JUN5800CE", "CRUDEOIL25JUNFUT")

    def test_future_matches_option_devolvement_day_suffix(self):
        from backend.api.algo.expiry import _future_matches_option_devolvement
        assert _future_matches_option_devolvement("CRUDEOIL25JUN5800CE", "CRUDEOIL25JUN19FUT")

    def test_future_matches_option_devolvement_rejects_different_month(self):
        """The exact bug this fix closes: a future on the SAME
        underlying but a DIFFERENT contract month must never match."""
        from backend.api.algo.expiry import _future_matches_option_devolvement
        assert not _future_matches_option_devolvement("CRUDEOIL25JUN5800CE", "CRUDEOIL25JULFUT")
        assert not _future_matches_option_devolvement("CRUDEOIL25JUN5800CE", "CRUDEOIL25AUGFUT")

    def test_future_matches_option_devolvement_rejects_unparseable_option(self):
        """Fails closed — an option that doesn't parse resolves NO
        target, so it never accepts ANY future as a match."""
        from backend.api.algo.expiry import _future_matches_option_devolvement
        assert not _future_matches_option_devolvement("BOGUS", "CRUDEOIL25JUNFUT")

    @pytest.mark.asyncio
    async def test_parity_against_real_async_resolver_plain_monthly(self):
        """The pure, no-I/O mirror must agree with the REAL async
        `derivatives.lookup_future_for_option()` for a plain monthly
        MCX symbol."""
        from backend.api.algo.derivatives import lookup_future_for_option
        from backend.api.algo.expiry import _mcx_devolvement_future_symbol

        yy, mon, x = _future_month(1)
        fut_sym = f"CRUDEOIL{yy}{mon}FUT"
        opt_sym = f"CRUDEOIL{yy}{mon}5800CE"
        items = [_fake_inst(fut_sym, "CRUDEOIL", x)]

        with patch("backend.api.cache.get_or_fetch", new=AsyncMock(return_value=_fake_instruments_resp(items))):
            real = await lookup_future_for_option(opt_sym)

        pure = _mcx_devolvement_future_symbol(opt_sym)
        assert real == fut_sym
        assert pure == real, f"pure mirror ({pure!r}) disagreed with the real resolver ({real!r})"

    @pytest.mark.asyncio
    async def test_parity_against_real_async_resolver_day_suffix(self):
        """Same parity check for the MCX day-suffix variant (e.g.
        CRUDEOIL26JUL19FUT) — the Pass-2 prefix-match case in both
        `derivatives._resolve_monthly_option_future` and this module's
        own `_future_matches_option_devolvement`."""
        from backend.api.algo.derivatives import lookup_future_for_option
        from backend.api.algo.expiry import _future_matches_option_devolvement

        yy, mon, x = _future_month(2, day=19)
        day_suffix_sym = f"CRUDEOIL{yy}{mon}19FUT"
        opt_sym = f"CRUDEOIL{yy}{mon}5800CE"
        items = [_fake_inst(day_suffix_sym, "CRUDEOIL", x)]

        with patch("backend.api.cache.get_or_fetch", new=AsyncMock(return_value=_fake_instruments_resp(items))):
            real = await lookup_future_for_option(opt_sym)

        assert real == day_suffix_sym
        assert _future_matches_option_devolvement(opt_sym, real), (
            "pure helper must accept the real resolver's day-suffix match"
        )


class TestClassifyMcxGroupFuturesContractIdentity:
    def test_mismatched_month_future_is_not_a_netting_partner(self):
        """A held future on the SAME underlying but a DIFFERENT
        contract month must be excluded entirely from
        `_classify_mcx_group`'s netting pass — an unnetted CE must stay
        unnetted even though a future on the same underlying exists."""
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition, FuturePosition

        engine = ExpiryEngine()
        # Long CE, ITM, delta+ — would normally net against a short future.
        ce = OptionPosition(
            account="A1", tradingsymbol="CRUDEOIL25JUN5800CE", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=5800.0,
            expiry=date(2025, 6, 17), quantity=1, product="NRML",
            moneyness="ITM", theta=-0.5, residual_qty=1,
        )
        # Short future, correct delta-offset direction, SAME underlying,
        # SAME account — but a DIFFERENT contract month (JUL, not JUN).
        wrong_month_fut = FuturePosition(
            account="A1", tradingsymbol="CRUDEOIL25JULFUT",
            underlying="CRUDEOIL", quantity=-1, product="NRML",
        )

        engine._classify_mcx_group("A1", "CRUDEOIL", [ce], [wrong_month_fut])

        assert ce.residual_qty == 1, "a different-month future must never net away this leg"
        assert ce.needs_close is True

    def test_matching_month_future_still_nets(self):
        """Sanity check for the same scenario with the CORRECT contract
        month — the fix must not also break the legitimate case."""
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition, FuturePosition

        engine = ExpiryEngine()
        ce = OptionPosition(
            account="A1", tradingsymbol="CRUDEOIL25JUN5800CE", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=5800.0,
            expiry=date(2025, 6, 17), quantity=1, product="NRML",
            moneyness="ITM", theta=-0.5, residual_qty=1,
        )
        right_month_fut = FuturePosition(
            account="A1", tradingsymbol="CRUDEOIL25JUNFUT",
            underlying="CRUDEOIL", quantity=-1, product="NRML",
        )

        engine._classify_mcx_group("A1", "CRUDEOIL", [ce], [right_month_fut])

        assert ce.residual_qty == 0, "the correct-month future must still net this leg away"
        assert ce.needs_close is False


# ═══════════════════════════════════════════════════════════════════════
# expiry.py — symmetric NTM exclusion + same-contract carve-out
# ═══════════════════════════════════════════════════════════════════════

class TestNtmExclusionSymmetric:
    def test_ntm_as_a_with_higher_theta_finds_no_itm_partner(self):
        """NTM legs typically carry the LARGEST |theta| (closest to the
        money), so the netting loop processes them FIRST. An NTM-as-A
        must still be excluded from pairing with an ITM-as-B — the
        exact gap a B-only exclusion would leave open."""
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        engine = ExpiryEngine()
        ntm = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=75000.0,
            expiry=date(2025, 5, 29), quantity=1, product="NRML",
            moneyness="NTM", theta=-0.9, residual_qty=1,   # highest |theta|
        )
        itm = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY70000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=70000.0,
            expiry=date(2025, 5, 29), quantity=-1, product="NRML",
            moneyness="ITM", theta=-0.3, residual_qty=-1,
        )
        engine._net_commodity_group([ntm, itm], [])
        assert ntm.residual_qty == 1, "NTM-as-A must not net against an ITM-as-B"
        assert itm.residual_qty == -1, "ITM-as-B must not be consumed by an NTM-as-A"

    def test_ntm_as_b_with_lower_theta_still_excluded(self):
        """Mirror ordering — ITM processed first (higher theta), NTM is
        the candidate B. Must also be excluded.

        Correction: an earlier draft of this docstring claimed this
        direction "was already correct pre-fix". Verified false by
        loading the real pre-fix `expiry.py` (via `git show HEAD:...`
        into a throwaway module, never a `git stash`) and running this
        exact scenario against it: pre-fix nets BOTH legs to residual 0
        here too (itm=0, ntm=0, not the fixed code's itm=1/ntm=-1) —
        the pre-fix code had NO moneyness awareness in `_best_opt_
        partner` at all, in either direction. Both orderings are
        therefore genuine, confirmed-failing-pre-fix regressions this
        fix closes, not one real fix plus one pre-existing-safe case."""
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        engine = ExpiryEngine()
        itm = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY70000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=70000.0,
            expiry=date(2025, 5, 29), quantity=1, product="NRML",
            moneyness="ITM", theta=-0.9, residual_qty=1,   # highest |theta|
        )
        ntm = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=75000.0,
            expiry=date(2025, 5, 29), quantity=-1, product="NRML",
            moneyness="NTM", theta=-0.3, residual_qty=-1,
        )
        engine._net_commodity_group([itm, ntm], [])
        assert itm.residual_qty == 1
        assert ntm.residual_qty == -1

    def test_same_contract_ntm_mirror_pair_still_nets(self):
        """The mandatory carve-out: two legs at the IDENTICAL (strike,
        type) — a genuine rule-1/2 mirror — must still net to zero even
        when BOTH classify as NTM. Without this carve-out the fix would
        break a real same-strike hedge (the exact scenario in
        `test_scope_positions_expiring_today_mcx_unhedged.py`'s
        `test_mcx_unhedged_surfaces_unhedged_amid_hedged` and
        `test_expiry_logic.py`'s `test_scan_positions_and_classify_
        from_context_agree_on_genuine_hedge` fixtures — both GOLD
        75000CE ±1 at spot 76000, which is 1.33% ITM, i.e. NTM under
        the default 2% buffer)."""
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        engine = ExpiryEngine()
        long_leg = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=75000.0,
            expiry=date(2025, 5, 29), quantity=1, product="NRML",
            moneyness="NTM", theta=-0.5, residual_qty=1,
        )
        short_leg = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=75000.0,
            expiry=date(2025, 5, 29), quantity=-1, product="NRML",
            moneyness="NTM", theta=-0.5, residual_qty=-1,
        )
        engine._net_commodity_group([long_leg, short_leg], [])
        assert long_leg.residual_qty == 0
        assert short_leg.residual_qty == 0

    def test_ntm_option_never_nets_against_a_future(self):
        """`_best_fut_partner` has no same-contract carve-out possible
        (a future is never 'the same contract' as an option) — an NTM
        option must find NO futures partner at all, even with a
        perfectly valid delta-offset candidate available."""
        from backend.api.algo.expiry import _best_fut_partner, OptionPosition, FuturePosition

        ntm_long_ce = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY75000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=75000.0,
            expiry=date(2025, 5, 29), quantity=1, product="NRML",
            moneyness="NTM", theta=-0.5,
        )
        short_fut = FuturePosition(
            account="A1", tradingsymbol="GOLD25MAYFUT", underlying="GOLD",
            quantity=-1, product="NRML",
        )
        remaining_opt = {id(ntm_long_ce): 1}
        remaining_fut = {id(short_fut): -1}
        best, qty = _best_fut_partner(ntm_long_ce, [short_fut], remaining_opt, remaining_fut)
        assert best is None
        assert qty == 0

    def test_itm_option_still_nets_against_a_future(self):
        """Sanity check — the NTM exclusion must not also break a
        genuinely ITM option's futures netting."""
        from backend.api.algo.expiry import _best_fut_partner, OptionPosition, FuturePosition

        itm_long_ce = OptionPosition(
            account="A1", tradingsymbol="GOLD25MAY70000CE", exchange="MCX",
            instrument_type="CE", underlying="GOLD", strike=70000.0,
            expiry=date(2025, 5, 29), quantity=1, product="NRML",
            moneyness="ITM", theta=-0.5,
        )
        short_fut = FuturePosition(
            account="A1", tradingsymbol="GOLD25MAYFUT", underlying="GOLD",
            quantity=-1, product="NRML",
        )
        remaining_opt = {id(itm_long_ce): 1}
        remaining_fut = {id(short_fut): -1}
        best, qty = _best_fut_partner(itm_long_ce, [short_fut], remaining_opt, remaining_fut)
        assert best is short_fut
        assert qty == 1


# ═══════════════════════════════════════════════════════════════════════
# close_positions() — agent_id-None refusal + release_policy wiring
# ═══════════════════════════════════════════════════════════════════════

class TestClosePositionsReleasePolicyWiring:
    @pytest.mark.asyncio
    async def test_agent_id_none_refuses_to_stage_regardless_of_policy(self):
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        engine = ExpiryEngine()  # no agent_id
        pos = OptionPosition(
            account="ZG0790", tradingsymbol="CRUDEOIL26NOV5500CE", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=5500.0,
            expiry=date.today(), quantity=1, product="NRML", residual_qty=1,
        )

        async def _not_in_flight(account, symbol, exchange, side):
            return False

        with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
             patch("backend.api.algo.expiry.expiry_close_held", return_value=True), \
             patch("backend.api.algo.expiry.record_held_order", new=AsyncMock()) as held_mock, \
             patch("backend.api.algo.expiry.chase_order", new=AsyncMock()) as chase_mock:
            await engine.close_positions([pos], release_policy={"after_min": 30, "floor_tier": "high"})

        held_mock.assert_not_awaited()
        chase_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_policy_resolves_a_concrete_deadline(self):
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        engine = ExpiryEngine(agent_id=42)
        pos = OptionPosition(
            account="ZG0790", tradingsymbol="CRUDEOIL26NOV5500CE", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=5500.0,
            expiry=date.today(), quantity=1, product="NRML", residual_qty=1,
        )

        async def _not_in_flight(account, symbol, exchange, side):
            return False

        with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
             patch("backend.api.algo.expiry.expiry_close_held", return_value=True), \
             patch("backend.api.algo.expiry.record_held_order", new=AsyncMock(return_value=101)) as held_mock:
            await engine.close_positions([pos], release_policy={"after_min": 30, "floor_tier": "high"})

        held_mock.assert_awaited_once()
        kwargs = held_mock.await_args.kwargs
        assert kwargs["agent_id"] == 42
        assert kwargs["price_policy"] == "CHASE_HIGH"
        assert kwargs["auto_release_at"] is not None
        assert kwargs["auto_release_at"].tzinfo is not None, "auto_release_at must be timezone-aware"

    @pytest.mark.asyncio
    async def test_release_policy_given_but_unresolvable_refuses(self):
        """An explicitly-passed-but-empty/malformed policy dict must be
        treated as a caller bug and refused — NEVER silently fall back
        to the legacy no-deadline behaviour (that would reproduce the
        exact unreleasable-hold bug this whole fix exists to close)."""
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        engine = ExpiryEngine(agent_id=42)
        pos = OptionPosition(
            account="ZG0790", tradingsymbol="CRUDEOIL26NOV5500CE", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=5500.0,
            expiry=date.today(), quantity=1, product="NRML", residual_qty=1,
        )

        async def _not_in_flight(account, symbol, exchange, side):
            return False

        with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
             patch("backend.api.algo.expiry.expiry_close_held", return_value=True), \
             patch("backend.api.algo.expiry.record_held_order", new=AsyncMock()) as held_mock:
            await engine.close_positions([pos], release_policy={})

        held_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_policy_preserves_legacy_behaviour(self):
        """`release_policy=None` (the legacy `run()` call shape) must
        still stage a HELD row — with no auto_release_at, exactly as
        before this fix."""
        from backend.api.algo.expiry import ExpiryEngine, OptionPosition

        engine = ExpiryEngine(agent_id=42)
        pos = OptionPosition(
            account="ZG0790", tradingsymbol="CRUDEOIL26NOV5500CE", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=5500.0,
            expiry=date.today(), quantity=1, product="NRML", residual_qty=1,
        )

        async def _not_in_flight(account, symbol, exchange, side):
            return False

        with patch("backend.api.algo.expiry._exp_leg_in_flight_this_session", new=_not_in_flight), \
             patch("backend.api.algo.expiry.expiry_close_held", return_value=True), \
             patch("backend.api.algo.expiry.record_held_order", new=AsyncMock(return_value=101)) as held_mock:
            await engine.close_positions([pos])  # no release_policy

        held_mock.assert_awaited_once()
        kwargs = held_mock.await_args.kwargs
        assert kwargs["auto_release_at"] is None
        assert kwargs["price_policy"] == "CHASE_MED"


# ═══════════════════════════════════════════════════════════════════════
# CANCEL_FAILED broker-truth verification
# ═══════════════════════════════════════════════════════════════════════

def _make_row(status="CANCEL_FAILED", broker_order_id="BO1", row_id=1):
    row = MagicMock()
    row.id = row_id
    row.status = status
    row.broker_order_id = broker_order_id
    row.symbol = "CRUDEOIL26NOV5500CE"
    row.account = "ZG0790"
    row.exchange = "MCX"
    row.transaction_type = "SELL"
    row.quantity = 1
    return row


class TestPositionMatchesSignPlusMagnitude:
    """2026-11-23 fix (found during this plan's own advisor review, not
    in the original file list — `order_release.py`): a partially-netted
    MCX leg's HELD `quantity` is the netting RESIDUAL, not the gross
    symbol position. The OLD exact-equality `position_matches` refused
    release for ANY such leg — auto-release (Part C's own purpose) and
    a manual operator click alike. Verified: without this fix, Part C's
    own Done-when criterion ("a position staged HELD ... is released
    and chased automatically") could never be satisfied for a
    partially-netted leg, the single most common MCX expiry-day shape."""

    def test_sell_close_ok_when_net_exceeds_expected_same_sign(self):
        from backend.api.algo.order_release import position_matches
        # Residual SELL 1 lot (100 contracts) staged; broker shows the
        # FULL 2-lot (200 contract) long position still open.
        ok, reason = position_matches("SELL", 100, 200)
        assert ok is True, reason

    def test_buy_close_ok_when_net_exceeds_expected_same_sign(self):
        from backend.api.algo.order_release import position_matches
        ok, reason = position_matches("BUY", 100, -200)
        assert ok is True, reason

    def test_sell_close_refused_when_net_insufficient(self):
        from backend.api.algo.order_release import position_matches
        ok, reason = position_matches("SELL", 200, 100)
        assert ok is False
        assert "position changed" in reason

    def test_sell_close_refused_when_sign_flipped_to_short(self):
        from backend.api.algo.order_release import position_matches
        ok, reason = position_matches("SELL", 100, -100)
        assert ok is False
        assert "position changed" in reason

    def test_buy_close_refused_when_sign_flipped_to_long(self):
        from backend.api.algo.order_release import position_matches
        ok, reason = position_matches("BUY", 100, 100)
        assert ok is False

    def test_exact_match_still_ok(self):
        from backend.api.algo.order_release import position_matches
        assert position_matches("SELL", 200, 200) == (True, "ok")
        assert position_matches("BUY", 200, -200) == (True, "ok")


class TestExpVerifyCancelFailedLeg:
    @pytest.mark.asyncio
    async def test_confirmed_terminal_unblocks_and_reconciles(self):
        from backend.api.algo.expiry import _exp_verify_cancel_failed_leg

        truth = MagicMock(available=True, algo_status="CANCELLED")
        row = _make_row()
        with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                   new=AsyncMock(return_value=truth)), \
             patch("backend.api.routes.orders_helpers.apply_order_truth",
                   new=AsyncMock(return_value=True)) as apply_mock:
            still_blocking = await _exp_verify_cancel_failed_leg(row)

        assert still_blocking is False
        apply_mock.assert_awaited_once_with(row.id, truth)

    @pytest.mark.asyncio
    async def test_confirmed_still_resting_without_chase_stays_blocked_and_logs(self):
        from backend.api.algo.expiry import _exp_verify_cancel_failed_leg

        truth = MagicMock(available=True, algo_status=None)
        row = _make_row()
        with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                   new=AsyncMock(return_value=truth)), \
             patch("backend.api.algo.chase.is_chase_active", return_value=False), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as alert_mock, \
             patch("backend.api.algo.expiry.logger") as mock_logger:
            still_blocking = await _exp_verify_cancel_failed_leg(row)

        assert still_blocking is True
        assert mock_logger.error.called, "a stale resting order with no active chase must alert"
        alert_mock.assert_called_once()
        assert alert_mock.call_args.kwargs["bypass_gates"] is True

    @pytest.mark.asyncio
    async def test_confirmed_still_resting_with_active_chase_stays_blocked_no_alert(self):
        from backend.api.algo.expiry import _exp_verify_cancel_failed_leg

        truth = MagicMock(available=True, algo_status=None)
        row = _make_row()
        with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                   new=AsyncMock(return_value=truth)), \
             patch("backend.api.algo.chase.is_chase_active", return_value=True), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as alert_mock, \
             patch("backend.api.algo.expiry.logger") as mock_logger:
            still_blocking = await _exp_verify_cancel_failed_leg(row)

        assert still_blocking is True
        assert not mock_logger.error.called, "a chase genuinely working this row is not 'silently unmanaged'"
        alert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_truth_unavailable_stays_blocked_and_alerts(self):
        from backend.api.algo.expiry import _exp_verify_cancel_failed_leg

        truth = MagicMock(available=False)
        row = _make_row()
        with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                   new=AsyncMock(return_value=truth)), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as alert_mock, \
             patch("backend.api.algo.expiry.logger") as mock_logger:
            still_blocking = await _exp_verify_cancel_failed_leg(row)

        assert still_blocking is True
        assert mock_logger.error.called
        alert_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_alert_is_deduped_per_row_across_repeated_checks(self):
        """A stale CANCEL_FAILED leg is re-checked on every re-scan
        (~every 30 min) — it must page the operator ONCE, not every
        single re-check."""
        from backend.api.algo.expiry import _exp_verify_cancel_failed_leg

        truth = MagicMock(available=False)
        row = _make_row(row_id=55)
        with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                   new=AsyncMock(return_value=truth)), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as alert_mock:
            await _exp_verify_cancel_failed_leg(row)
            await _exp_verify_cancel_failed_leg(row)
            await _exp_verify_cancel_failed_leg(row)

        alert_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_lookup_exception_stays_blocked_and_alerts(self):
        from backend.api.algo.expiry import _exp_verify_cancel_failed_leg

        row = _make_row()
        with patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                   new=AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("backend.api.algo.expiry.logger") as mock_logger:
            still_blocking = await _exp_verify_cancel_failed_leg(row)

        assert still_blocking is True
        assert mock_logger.error.called


class TestExpLegInFlightCancelFailedPaths:
    @pytest.mark.asyncio
    async def test_held_row_still_unconditionally_blocks(self):
        """A plain HELD row must keep blocking unconditionally — the
        CANCEL_FAILED re-verification path must never be reached for
        it (no broker-truth lookup at all)."""
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        row = _make_row(status="HELD")
        row.created_at = datetime.now(timezone.utc)

        class _FakeScalars:
            def all(self):
                return [row]

        class _FakeResult:
            def scalars(self):
                return _FakeScalars()

        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False
        fake_session.execute = AsyncMock(return_value=_FakeResult())

        with patch("backend.api.database.async_session", return_value=fake_session), \
             patch("backend.api.routes.orders_helpers.get_broker_order_truth") as truth_mock:
            blocked = await _exp_leg_in_flight_this_session("ZG0790", "SYM", "MCX", "SELL")

        assert blocked is True
        truth_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_cancel_failed_and_confirmed_gone_unblocks(self):
        from backend.api.algo.expiry import _exp_leg_in_flight_this_session

        row = _make_row(status="CANCEL_FAILED")
        row.created_at = datetime.now(timezone.utc)

        class _FakeScalars:
            def all(self):
                return [row]

        class _FakeResult:
            def scalars(self):
                return _FakeScalars()

        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False
        fake_session.execute = AsyncMock(return_value=_FakeResult())

        truth = MagicMock(available=True, algo_status="CANCELLED")
        with patch("backend.api.database.async_session", return_value=fake_session), \
             patch("backend.api.routes.orders_helpers.get_broker_order_truth",
                   new=AsyncMock(return_value=truth)), \
             patch("backend.api.routes.orders_helpers.apply_order_truth",
                   new=AsyncMock(return_value=True)):
            blocked = await _exp_leg_in_flight_this_session("ZG0790", "SYM", "MCX", "SELL")

        assert blocked is False


# ═══════════════════════════════════════════════════════════════════════
# background._agent_close_auto_release_sweep_once — widened categories
# ═══════════════════════════════════════════════════════════════════════

class TestAgentCloseAutoReleaseSweepWidenedCategories:
    def test_both_categories_in_the_set(self):
        from backend.api.background import _AUTO_RELEASE_CATEGORIES
        assert _AUTO_RELEASE_CATEGORIES == {"agent_close", "expiry_close"}

    @pytest.mark.asyncio
    async def test_unrelated_category_is_skipped(self):
        from backend.api.background import _agent_close_auto_release_sweep_once
        import json

        row = MagicMock()
        row.id = 1
        row.status = "HELD"
        row.hold_json = json.dumps({
            "category": "agent_order",  # NOT in the auto-release set
            "auto_release_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        })
        row.agent_id = 42

        class _FakeScalars:
            def all(self):
                return [row]

        class _FakeResult:
            def scalars(self):
                return _FakeScalars()

        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False
        fake_session.execute = AsyncMock(return_value=_FakeResult())

        with patch("backend.api.database.async_session", return_value=fake_session), \
             patch("backend.api.algo.order_release.get_release_handler") as handler_getter:
            released = await _agent_close_auto_release_sweep_once()

        assert released == 0
        handler_getter.assert_not_called()

    @pytest.mark.asyncio
    async def test_expiry_close_category_is_released(self):
        from backend.api.background import _agent_close_auto_release_sweep_once
        import json

        now = datetime.now(timezone.utc)
        row = MagicMock()
        row.id = 7
        row.status = "HELD"
        row.hold_json = json.dumps({
            "category": "expiry_close",
            "auto_release_at": (now - timedelta(minutes=1)).isoformat(),
        })
        row.agent_id = 42

        agent = MagicMock()
        agent.id = 42
        agent.status = "cooldown"  # real post-fire state, not literal "active"

        class _OrderScalars:
            def all(self):
                return [row]

        class _OrderResult:
            def scalars(self):
                return _OrderScalars()

        class _AgentResult:
            def scalar_one_or_none(self):
                return agent

        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False

        call_count = {"n": 0}

        async def _execute(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _OrderResult()
            return _AgentResult()

        fake_session.execute = _execute
        release_mock = AsyncMock(return_value={"ok": True})

        with patch("backend.api.database.async_session", return_value=fake_session), \
             patch("backend.api.algo.order_release.get_release_handler",
                   return_value=release_mock) as handler_getter:
            released = await _agent_close_auto_release_sweep_once(now=now)

        assert released == 1
        handler_getter.assert_called_once_with("expiry_close")
        release_mock.assert_awaited_once_with(7, actor="auto-release")

    @pytest.mark.asyncio
    async def test_cooldown_agent_status_does_not_block_release(self):
        """Kill-switch regression: `agent.status == 'cooldown'` (the
        REAL post-fire state of a persistent agent) must NOT be
        treated as the kill switch — only an explicit 'inactive' does."""
        from backend.api.background import _agent_close_auto_release_sweep_once
        import json

        now = datetime.now(timezone.utc)
        row = MagicMock()
        row.id = 9
        row.status = "HELD"
        row.hold_json = json.dumps({
            "category": "agent_close",
            "auto_release_at": (now - timedelta(seconds=1)).isoformat(),
        })
        row.agent_id = 99

        agent = MagicMock()
        agent.id = 99
        agent.status = "cooldown"

        class _OrderResult:
            def scalars(self):
                class _S:
                    def all(self):
                        return [row]
                return _S()

        class _AgentResult:
            def scalar_one_or_none(self):
                return agent

        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False
        calls = {"n": 0}

        async def _execute(*a, **k):
            calls["n"] += 1
            return _OrderResult() if calls["n"] == 1 else _AgentResult()

        fake_session.execute = _execute
        release_mock = AsyncMock(return_value={"ok": True})

        with patch("backend.api.database.async_session", return_value=fake_session), \
             patch("backend.api.algo.order_release.get_release_handler", return_value=release_mock):
            released = await _agent_close_auto_release_sweep_once(now=now)

        assert released == 1
        release_mock.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_inactive_agent_blocks_release(self):
        from backend.api.background import _agent_close_auto_release_sweep_once
        import json

        now = datetime.now(timezone.utc)
        row = MagicMock()
        row.id = 10
        row.status = "HELD"
        row.hold_json = json.dumps({
            "category": "agent_close",
            "auto_release_at": (now - timedelta(seconds=1)).isoformat(),
        })
        row.agent_id = 100

        agent = MagicMock()
        agent.id = 100
        agent.status = "inactive"

        class _OrderResult:
            def scalars(self):
                class _S:
                    def all(self):
                        return [row]
                return _S()

        class _AgentResult:
            def scalar_one_or_none(self):
                return agent

        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False
        calls = {"n": 0}

        async def _execute(*a, **k):
            calls["n"] += 1
            return _OrderResult() if calls["n"] == 1 else _AgentResult()

        fake_session.execute = _execute

        with patch("backend.api.database.async_session", return_value=fake_session), \
             patch("backend.api.algo.order_release.get_release_handler") as handler_getter:
            released = await _agent_close_auto_release_sweep_once(now=now)

        assert released == 0
        handler_getter.assert_not_called()

    @staticmethod
    def _sweep_fixture_for_one_row(row, agent):
        """Shared fake-`async_session` factory for the tests below. One
        full sweep call makes exactly two `async with async_session()`
        blocks — the HELD-row scan, then the agent lookup. Returns a
        FACTORY function (not a single fixed session object) so it
        works correctly across MULTIPLE sweep calls in one test (the
        dedup test below calls the sweep twice) — a single shared
        session object with an internal call counter would desync once
        a second sweep's own two `async_session()` calls continue
        incrementing the same counter."""
        class _OrderResult:
            def scalars(self):
                class _S:
                    def all(self):
                        return [row]
                return _S()

        class _AgentResult:
            def scalar_one_or_none(self):
                return agent

        call_index = {"n": 0}

        def _session_factory(*a, **k):
            call_index["n"] += 1
            is_order_call = (call_index["n"] % 2 == 1)
            session = AsyncMock()
            session.__aenter__.return_value = session
            session.__aexit__.return_value = False
            result = _OrderResult() if is_order_call else _AgentResult()
            session.execute = AsyncMock(return_value=result)
            return session

        return _session_factory

    @pytest.mark.asyncio
    async def test_genuine_release_failure_is_not_counted_as_released_and_alerts_once(self):
        """2026-11-23 fix: a release handler returning `{"ok": False,
        ...}` (e.g. a stale position/price check) must NOT be counted
        as released, must NOT claim success in the log, and must page
        the operator exactly once — not be silently retried forever
        with no one told."""
        from backend.api.background import _agent_close_auto_release_sweep_once
        import json

        now = datetime.now(timezone.utc)
        row = MagicMock()
        row.id = 21
        row.status = "HELD"
        row.account = "ZG0790"
        row.symbol = "CRUDEOIL26NOV5500CE"
        row.exchange = "MCX"
        row.transaction_type = "SELL"
        row.quantity = 1
        row.agent_id = 42
        row.hold_json = json.dumps({
            "category": "expiry_close",
            "auto_release_at": (now - timedelta(seconds=1)).isoformat(),
        })

        agent = MagicMock(id=42, status="cooldown")
        session_factory = self._sweep_fixture_for_one_row(row, agent)

        failing_result = {"ok": False, "reason": "position changed: net 0, expected 1", "status": "HELD"}
        release_mock = AsyncMock(return_value=failing_result)

        with patch("backend.api.database.async_session", side_effect=session_factory), \
             patch("backend.api.algo.order_release.get_release_handler", return_value=release_mock), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as alert_mock:
            released = await _agent_close_auto_release_sweep_once(now=now)
            released_again = await _agent_close_auto_release_sweep_once(now=now)

        assert released == 0, "a failed release must never be counted as released"
        assert released_again == 0
        release_mock.assert_awaited()
        alert_mock.assert_called_once(), "exactly one alert across repeated failed sweeps (dedup)"
        assert alert_mock.call_args.kwargs["bypass_gates"] is True
        assert alert_mock.call_args.kwargs["account"] == "ZG0790"

    @pytest.mark.asyncio
    async def test_benign_already_released_race_is_not_counted_and_does_not_alert(self):
        """When the row is no longer HELD by the time the handler
        actually runs (status='OPEN' — a concurrent manual release
        already won the race), this is NOT a failure. It must not be
        counted as released by THIS sweep, and must never alert."""
        from backend.api.background import _agent_close_auto_release_sweep_once
        import json

        now = datetime.now(timezone.utc)
        row = MagicMock()
        row.id = 22
        row.status = "HELD"
        row.agent_id = 42
        row.hold_json = json.dumps({
            "category": "agent_close",
            "auto_release_at": (now - timedelta(seconds=1)).isoformat(),
        })

        agent = MagicMock(id=42, status="cooldown")
        session_factory = self._sweep_fixture_for_one_row(row, agent)

        race_result = {"ok": False, "reason": "order is OPEN, not HELD", "status": "OPEN"}
        release_mock = AsyncMock(return_value=race_result)

        with patch("backend.api.database.async_session", side_effect=session_factory), \
             patch("backend.api.algo.order_release.get_release_handler", return_value=release_mock), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as alert_mock:
            released = await _agent_close_auto_release_sweep_once(now=now)

        assert released == 0
        alert_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_genuine_success_is_counted_and_never_alerts(self):
        """Sanity check — the honest-counting fix must not also break
        the real success path."""
        from backend.api.background import _agent_close_auto_release_sweep_once
        import json

        now = datetime.now(timezone.utc)
        row = MagicMock()
        row.id = 23
        row.status = "HELD"
        row.agent_id = 42
        row.hold_json = json.dumps({
            "category": "expiry_close",
            "auto_release_at": (now - timedelta(seconds=1)).isoformat(),
        })

        agent = MagicMock(id=42, status="cooldown")
        session_factory = self._sweep_fixture_for_one_row(row, agent)

        release_mock = AsyncMock(return_value={"ok": True, "reason": "released; chasing", "status": "OPEN"})

        with patch("backend.api.database.async_session", side_effect=session_factory), \
             patch("backend.api.algo.order_release.get_release_handler", return_value=release_mock), \
             patch("backend.shared.helpers.alert_utils.send_order_failure_alert") as alert_mock:
            released = await _agent_close_auto_release_sweep_once(now=now)

        assert released == 1
        alert_mock.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# _task_expiry_check no longer calls engine.run() — source-level guard
# ═══════════════════════════════════════════════════════════════════════

class TestExpiryContextShimsCarryNow:
    """2026-11-23 confirmed-live P0 fix: `_al_expiry_context_shim`
    (`actions_live.py`) and its twin `_v2_expiry_context_shim`
    (`agent_engine.py`) both omitted `.now` entirely. Confirmed via a
    direct, unpatched scratchpad call before fixing:
    `_expiry_preclose_exchanges()` ALWAYS returned an empty set through
    either shim, so `_action_live_expiry_auto_close` never actually
    closed anything and `_v2_expiry_position_breakdown` never actually
    rendered a breakdown — in production, every time either fired.
    These tests pin the fix directly, independent of the larger E2E
    flow in `test_expiry_close_release_e2e.py`."""

    def _representative_context(self, option_symbol: str) -> dict:
        from backend.api.algo.derivatives import parse_tradingsymbol
        expiry = parse_tradingsymbol(option_symbol)["expiry"]
        now = datetime(expiry.year, expiry.month, expiry.day, 10, 0, tzinfo=timezone.utc)
        return {
            "now": now,
            "segment_flags": {"mcx_open": True, "minutes_until_mcx_close": 45, "nse_open": False},
            "position_rows": [{"tradingsymbol": option_symbol, "exchange": "MCX", "quantity": 1}],
        }

    def test_al_expiry_context_shim_carries_the_real_now(self):
        from backend.api.algo.actions_live import _al_expiry_context_shim
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _expiry_preclose_exchanges,
        )
        context = self._representative_context("CRUDEOIL26NOV5500CE")
        shim = _al_expiry_context_shim(context)
        assert shim.now is context["now"], (
            "the shim must pass the REAL context now through unchanged, "
            "never silently substitute its own fallback clock when one is given"
        )
        assert _expiry_preclose_exchanges(shim, None, 60) == {"MCX"}

    def test_v2_expiry_context_shim_carries_the_real_now(self):
        from backend.api.algo.agent_engine import _v2_expiry_context_shim
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _expiry_preclose_exchanges,
        )
        context = self._representative_context("CRUDEOIL26NOV5500CE")
        shim = _v2_expiry_context_shim(context)
        assert shim.now is context["now"]
        assert _expiry_preclose_exchanges(shim, None, 65) == {"MCX"}

    def test_al_expiry_context_shim_falls_back_when_now_absent(self):
        """A hand-built context with no `now` key (e.g. a test, or a
        caller that predates this fix) must still get a usable clock,
        never an AttributeError deep inside the scope resolver."""
        from backend.api.algo.actions_live import _al_expiry_context_shim
        context = {
            "segment_flags": {"mcx_open": True},
            "position_rows": [],
        }
        shim = _al_expiry_context_shim(context)
        assert shim.now is not None

    def test_v2_expiry_context_shim_falls_back_when_now_absent(self):
        from backend.api.algo.agent_engine import _v2_expiry_context_shim
        context = {"segment_flags": {}, "position_rows": []}
        shim = _v2_expiry_context_shim(context)
        assert shim.now is not None


class TestTaskExpiryCheckRetiresEngineRun:
    def test_source_has_no_engine_run_call(self):
        """Checks for the real CALL statement (`await engine.run()`),
        not a bare substring — this function's own docstring legitimately
        mentions "engine.run()" in prose explaining why it was removed."""
        import inspect
        from backend.api import background as bg
        src = inspect.getsource(bg._task_expiry_check)
        assert "await engine.run()" not in src, (
            "_task_expiry_check must never call engine.run() — the real "
            "close action is owned exclusively by expiry-day-itm-auto-close"
        )
        assert "_broadcast_event(\"expiry_day_detected\"" in src, (
            "the detection broadcast itself is informational-only and "
            "may stay — only the close ACTION was removed"
        )

    def test_expiry_day_detected_has_zero_consumers(self):
        """Confirms the plan's own grep claim stays true: nothing in
        the backend reads this broadcast event — only the one emit
        call site in background.py exists. Restricted to real `.py`
        source files (never `.pyc`/docstring-prose lines that merely
        MENTION the event name)."""
        import subprocess
        repo_root = __file__.rsplit("/backend/", 1)[0]
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py",
             "_broadcast_event(\"expiry_day_detected\"", "backend"],
            cwd=repo_root, capture_output=True, text=True,
        )
        lines = [l for l in result.stdout.splitlines() if l.strip()]
        assert len(lines) == 1, f"expected exactly one emit call site, got: {lines}"
        assert "backend/api/background.py" in lines[0]

        # And confirm no consumer reads it back out (WS/event-handler
        # style lookup) anywhere else in the backend or frontend.
        for base, pattern in (("backend", "*.py"), ("frontend/src", "*.js")):
            consumer_result = subprocess.run(
                ["grep", "-rln", f"--include={pattern}", "expiry_day_detected", base],
                cwd=repo_root, capture_output=True, text=True,
            )
            consumer_files = [
                f for f in consumer_result.stdout.splitlines()
                if f.strip()
                and f != "backend/api/background.py"
                and f != "backend/tests/test_expiry_close_release_part_c.py"
            ]
            assert consumer_files == [], (
                f"unexpected consumer(s) of expiry_day_detected in {base}: {consumer_files}"
            )
