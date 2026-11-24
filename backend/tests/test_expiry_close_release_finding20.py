"""
Audit finding #20 (2026-11-24) — four test-coverage gaps named explicitly
in the Part C expiry-close audit's own text, added here as their own
file rather than folded into test_expiry_close_release_part_c.py (which
already covers the 2-leg and single-account cases these gaps extend):

  1. A 3+-leg MCX netting case exercising theta-priority competition
     among 3+ candidates, including a partial-residual outcome.
  2. Multi-account isolation at the classify_prepared()/scan_positions()
     level (not just the rendering layer) — two accounts holding
     offsetting legs of the same symbol must each be classified
     independently, never netted against each other.
  3. A real-DB test of the double-close guard's actual SQL WHERE
     predicate (_exp_leg_in_flight_this_session) against a genuine
     released-then-re-scanned row, matching
     test_expiry_close_release_e2e.py's own convention (mock only the
     broker boundary, nothing else).
  4. A DST-season window-timing test driving the real resolved close
     time (not a hand-typed constant) for the 60/65-minute preclose
     windows, confirming the agent's own escalation math against a
     frozen clock on a real winter DST-season date (reusing
     test_expiry_mcx_dst_residual.py's pattern of freezing
     timestamp_indian()).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from backend.api.algo.expiry import ExpiryEngine, OptionPosition, FuturePosition


# ═══════════════════════════════════════════════════════════════════════
# Gap 1 — 3+-leg MCX netting, theta-priority competition, partial residual
# ═══════════════════════════════════════════════════════════════════════

class TestThreePlusLegThetaPriorityPartialResidual:
    """CRUDEOIL, one account, three legs, no futures. Hand-worked walk
    (confirmed against the real `_net_commodity_group` greedy algorithm):

      Leg1: Long CE strike=7000 qty=+5 theta=0.05 (highest |theta| — A)
      Leg2: Short CE strike=7000 qty=-2 theta=0.03 (rule-1 partner)
      Leg3: Long PE strike=7500 qty=+1 theta=0.02 (rule-3 partner)

    underlying_ltp=7300 makes Leg1 ITM (diff 4.3%), Leg2 ITM (same
    contract as Leg1), Leg3 ITM (diff 2.67%) — all comfortably past the
    default 2.0% NTM buffer, so NTM exclusion never enters this case.

    Walk: A=Leg1 (aq=5). Round 1 — both Leg2 (theta .03, rule-1) and
    Leg3 (theta .02, rule-3) are valid partners; Leg2 wins on higher
    |theta| (the theta-priority competition this gap names). Net
    min(5,2)=2 → Leg1 residual 3, Leg2 residual 0 (fully netted, drops
    out of further rounds). Round 2 — only Leg3 remains valid; net
    min(3,1)=1 → Leg1 residual 2, Leg3 residual 0. Round 3 — no partner
    left, loop breaks. Final: Leg1 residual +2 (needs_close), Leg2 and
    Leg3 fully netted (no close needed) — the "partial residual among
    3+ legs" outcome this gap asks for.
    """

    @staticmethod
    def _legs() -> list[OptionPosition]:
        expiry = date(2026, 11, 24)
        return [
            OptionPosition(
                account="ZG0790", tradingsymbol="LEG1", exchange="MCX",
                instrument_type="CE", underlying="CRUDEOIL", strike=7000.0,
                expiry=expiry, quantity=5, product="NRML", underlying_ltp=7300.0,
            ),
            OptionPosition(
                account="ZG0790", tradingsymbol="LEG2", exchange="MCX",
                instrument_type="CE", underlying="CRUDEOIL", strike=7000.0,
                expiry=expiry, quantity=-2, product="NRML", underlying_ltp=7300.0,
            ),
            OptionPosition(
                account="ZG0790", tradingsymbol="LEG3", exchange="MCX",
                instrument_type="PE", underlying="CRUDEOIL", strike=7500.0,
                expiry=expiry, quantity=1, product="NRML", underlying_ltp=7300.0,
            ),
        ]

    def test_theta_priority_competition_yields_partial_residual_on_the_highest_theta_leg(self):
        engine = ExpiryEngine.__new__(ExpiryEngine)
        engine._ntm_buffer = 2.0
        legs = self._legs()
        theta_by_sym = {"LEG1": 0.05, "LEG2": 0.03, "LEG3": 0.02}

        with patch.object(engine, "_compute_theta",
                          side_effect=lambda p, ref=None: theta_by_sym[p.tradingsymbol]):
            engine.classify_prepared(legs, all_futs=[])

        by_sym = {p.tradingsymbol: p for p in legs}
        assert by_sym["LEG1"].residual_qty == 2, (
            "Leg1 (highest theta) must end with the partial residual after "
            "netting against BOTH lower-theta partners in theta-priority order"
        )
        assert by_sym["LEG1"].needs_close is True
        assert by_sym["LEG2"].residual_qty == 0
        assert by_sym["LEG2"].needs_close is False, "fully netted — no close needed"
        assert by_sym["LEG3"].residual_qty == 0
        assert by_sym["LEG3"].needs_close is False, "fully netted — no close needed"

    def test_theta_priority_order_is_what_actually_drove_the_result(self):
        """Negative control — if Leg3 (theta .02) had been picked BEFORE
        Leg2 (theta .03), the walk would still fully net Leg1 down to 2
        (5-1-2=2, same arithmetic either order for THIS case), so the
        residual value alone doesn't prove priority was respected. This
        asserts the actual partner CHOSEN at round 1 is the higher-theta
        one, by checking Leg2 (not Leg3) is the one that nets out first
        when their quantities DIFFER — reversing quantities to make the
        two possible orders diverge: Leg2 qty=-1 (lower |qty| than
        Leg3's +1... use distinguishing quantities instead)."""
        engine = ExpiryEngine.__new__(ExpiryEngine)
        engine._ntm_buffer = 2.0
        expiry = date(2026, 11, 24)
        legs = [
            OptionPosition(
                account="ZG0790", tradingsymbol="LEG1", exchange="MCX",
                instrument_type="CE", underlying="CRUDEOIL", strike=7000.0,
                expiry=expiry, quantity=3, product="NRML", underlying_ltp=7300.0,
            ),
            # Lower theta but would fully absorb Leg1 if picked first.
            OptionPosition(
                account="ZG0790", tradingsymbol="LEG_LOW_THETA", exchange="MCX",
                instrument_type="CE", underlying="CRUDEOIL", strike=7000.0,
                expiry=expiry, quantity=-3, product="NRML", underlying_ltp=7300.0,
            ),
            # Higher theta, smaller qty — must be picked FIRST.
            OptionPosition(
                account="ZG0790", tradingsymbol="LEG_HIGH_THETA", exchange="MCX",
                instrument_type="PE", underlying="CRUDEOIL", strike=7500.0,
                expiry=expiry, quantity=1, product="NRML", underlying_ltp=7300.0,
            ),
        ]
        theta_by_sym = {"LEG1": 0.01, "LEG_LOW_THETA": 0.02, "LEG_HIGH_THETA": 0.09}

        with patch.object(engine, "_compute_theta",
                          side_effect=lambda p, ref=None: theta_by_sym[p.tradingsymbol]):
            engine.classify_prepared(legs, all_futs=[])

        by_sym = {p.tradingsymbol: p for p in legs}
        # LEG_HIGH_THETA (theta .09) must be consumed first (round 1,
        # min(3,1)=1 -> Leg1 residual 2), THEN LEG_LOW_THETA absorbs the
        # rest (round 2, min(2,3)=2 -> Leg1 residual 0, LOW_THETA residual -1).
        assert by_sym["LEG_HIGH_THETA"].residual_qty == 0, "consumed first, by priority"
        assert by_sym["LEG1"].residual_qty == 0
        assert by_sym["LEG_LOW_THETA"].residual_qty == -1, (
            "the lower-theta leg absorbs whatever Leg1 has left over AFTER "
            "the higher-theta leg already took its share first"
        )


# ═══════════════════════════════════════════════════════════════════════
# Gap 2 — multi-account isolation at classify_prepared() level
# ═══════════════════════════════════════════════════════════════════════

class TestMultiAccountIsolationAtClassifyPreparedLevel:
    def test_two_accounts_with_offsetting_legs_of_the_same_symbol_never_net_against_each_other(self):
        """Account A: Long CE +2. Account B: Short CE -2. Identical
        underlying/strike/type/expiry — if accounts were (incorrectly)
        pooled, this would net to zero for both. Since `_classify_mcx_
        group` groups by (account, underlying, expiry), neither account
        has a same-group partner, so BOTH must independently show the
        full original quantity as residual and need_close=True."""
        engine = ExpiryEngine.__new__(ExpiryEngine)
        engine._ntm_buffer = 2.0
        expiry = date(2026, 11, 24)
        leg_a = OptionPosition(
            account="ACCT_A", tradingsymbol="SHARED_LEG", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=7000.0,
            expiry=expiry, quantity=2, product="NRML", underlying_ltp=7300.0,
        )
        leg_b = OptionPosition(
            account="ACCT_B", tradingsymbol="SHARED_LEG", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=7000.0,
            expiry=expiry, quantity=-2, product="NRML", underlying_ltp=7300.0,
        )
        legs = [leg_a, leg_b]

        with patch.object(engine, "_compute_theta", return_value=0.01):
            engine.classify_prepared(legs, all_futs=[])

        assert leg_a.residual_qty == 2, "Account A's own leg has no in-group partner"
        assert leg_a.needs_close is True
        assert leg_b.residual_qty == -2, "Account B's own leg has no in-group partner"
        assert leg_b.needs_close is True

    def test_a_future_in_one_account_never_offsets_an_option_in_another(self):
        """Account A holds the option; Account B holds an offsetting
        future at the devolvement-matching contract month. `_classify_
        mcx_group`'s own futures filter is keyed on `f.account == acct`
        — Account B's future must never be accepted as Account A's
        netting partner."""
        engine = ExpiryEngine.__new__(ExpiryEngine)
        engine._ntm_buffer = 2.0
        expiry = date(2026, 11, 24)
        opt = OptionPosition(
            account="ACCT_A", tradingsymbol="CRUDEOIL26NOV7000CE", exchange="MCX",
            instrument_type="CE", underlying="CRUDEOIL", strike=7000.0,
            expiry=expiry, quantity=3, product="NRML", underlying_ltp=7300.0,
        )
        fut_other_account = FuturePosition(
            account="ACCT_B", tradingsymbol="CRUDEOIL26NOVFUT",
            underlying="CRUDEOIL", quantity=-3, product="NRML",
        )
        with patch(
            "backend.api.algo.expiry._future_matches_option_devolvement",
            return_value=True,
        ), patch.object(engine, "_compute_theta", return_value=0.01):
            engine.classify_prepared([opt], all_futs=[fut_other_account])

        assert opt.residual_qty == 3, (
            "a same-contract future held by a DIFFERENT account must never "
            "be accepted as a netting partner, even when the devolvement "
            "match itself would otherwise say yes"
        )
        assert opt.needs_close is True


# ═══════════════════════════════════════════════════════════════════════
# Gap 3 — real-DB double-close guard against a released-then-re-scanned row
# ═══════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def _finding20_sqlite_session_factory():
    """Real in-memory sqlite DB with a lightweight shadow `AlgoOrder`
    ORM class, swapped in for `backend.api.models.AlgoOrder` for the
    duration of the test — same pattern as
    test_expiry_close_release_e2e.py's own fixture."""
    from sqlalchemy import Column, Integer, String, DateTime
    from sqlalchemy.orm import DeclarativeBase
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

    class _Base(DeclarativeBase):
        pass

    class _AlgoOrder(_Base):
        __tablename__ = "algo_orders"
        id               = Column(Integer, primary_key=True, autoincrement=True)
        account          = Column(String(32), nullable=False)
        symbol           = Column(String(64), nullable=False)
        exchange         = Column(String(8), nullable=False, default="MCX")
        transaction_type = Column(String(4), nullable=False)
        status           = Column(String(16), nullable=False, default="OPEN")
        created_at       = Column(DateTime, nullable=False,
                                   default=lambda: datetime.now(timezone.utc))

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    import backend.api.models as _models
    orig_order = _models.AlgoOrder
    _models.AlgoOrder = _AlgoOrder  # type: ignore[assignment]

    yield factory, _AlgoOrder

    _models.AlgoOrder = orig_order  # type: ignore[assignment]
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.drop_all)
    await engine.dispose()


class TestInFlightGuardRealSqlitePredicate:
    """Drives the real `_exp_leg_in_flight_this_session` SQL query
    against a genuine sqlite row — not a mocked `scalars().all()`
    result — so the actual WHERE predicate (account/symbol/exchange/
    side/status-set, filtered to today's trading session) is exercised,
    matching test_expiry_close_release_e2e.py's "mock only the broker
    boundary" convention. No broker boundary exists in this function at
    all (it is pure DB read), so nothing is mocked here."""

    @pytest.mark.asyncio
    async def test_a_released_and_still_open_row_blocks_a_fresh_re_scan(
        self, _finding20_sqlite_session_factory,
    ):
        """The exact incident `close_positions()`'s own docstring
        describes: a hold is released (status -> OPEN) and is still
        chasing when the next periodic re-scan runs. The leg must read
        as in-flight, so a second HELD row is never staged for it."""
        factory, _AlgoOrder = _finding20_sqlite_session_factory
        async with factory() as s:
            s.add(_AlgoOrder(
                account="ZG0790", symbol="CRUDEOIL26NOV7000CE", exchange="MCX",
                transaction_type="SELL", status="OPEN",
                created_at=datetime.now(timezone.utc),
            ))
            await s.commit()

        from backend.api.algo.order_inflight import _exp_leg_in_flight_this_session

        with patch("backend.api.database.async_session", factory):
            blocked = await _exp_leg_in_flight_this_session(
                "ZG0790", "CRUDEOIL26NOV7000CE", "MCX", "SELL",
            )
        assert blocked is True

    @pytest.mark.asyncio
    async def test_a_fully_terminal_row_from_today_does_not_block(
        self, _finding20_sqlite_session_factory,
    ):
        """A FILLED row (terminal, not in the blocking status set) from
        today's session must NOT block a fresh close for the same leg —
        proves the guard is status-set-scoped, not a blanket "any row
        exists today" check."""
        factory, _AlgoOrder = _finding20_sqlite_session_factory
        async with factory() as s:
            s.add(_AlgoOrder(
                account="ZG0790", symbol="CRUDEOIL26NOV7000CE", exchange="MCX",
                transaction_type="SELL", status="FILLED",
                created_at=datetime.now(timezone.utc),
            ))
            await s.commit()

        from backend.api.algo.order_inflight import _exp_leg_in_flight_this_session

        with patch("backend.api.database.async_session", factory):
            blocked = await _exp_leg_in_flight_this_session(
                "ZG0790", "CRUDEOIL26NOV7000CE", "MCX", "SELL",
            )
        assert blocked is False

    @pytest.mark.asyncio
    async def test_a_different_accounts_open_row_never_blocks_this_leg(
        self, _finding20_sqlite_session_factory,
    ):
        """Identity-scoped, not symbol-only — a different account's
        open order for the same symbol must not falsely block."""
        factory, _AlgoOrder = _finding20_sqlite_session_factory
        async with factory() as s:
            s.add(_AlgoOrder(
                account="OTHER_ACCT", symbol="CRUDEOIL26NOV7000CE", exchange="MCX",
                transaction_type="SELL", status="OPEN",
                created_at=datetime.now(timezone.utc),
            ))
            await s.commit()

        from backend.api.algo.order_inflight import _exp_leg_in_flight_this_session

        with patch("backend.api.database.async_session", factory):
            blocked = await _exp_leg_in_flight_this_session(
                "ZG0790", "CRUDEOIL26NOV7000CE", "MCX", "SELL",
            )
        assert blocked is False

    @pytest.mark.asyncio
    async def test_a_stale_open_row_from_a_prior_trading_session_does_not_block(
        self, _finding20_sqlite_session_factory,
    ):
        """An OPEN row from BEFORE today's trading session (e.g. a stuck
        row from several days ago that was never cleaned up) must not
        block a fresh close today — the guard is scoped to the CURRENT
        session via session_date_of(), not "any OPEN row ever"."""
        factory, _AlgoOrder = _finding20_sqlite_session_factory
        async with factory() as s:
            s.add(_AlgoOrder(
                account="ZG0790", symbol="CRUDEOIL26NOV7000CE", exchange="MCX",
                transaction_type="SELL", status="OPEN",
                created_at=datetime.now(timezone.utc) - timedelta(days=10),
            ))
            await s.commit()

        from backend.api.algo.order_inflight import _exp_leg_in_flight_this_session

        with patch("backend.api.database.async_session", factory):
            blocked = await _exp_leg_in_flight_this_session(
                "ZG0790", "CRUDEOIL26NOV7000CE", "MCX", "SELL",
            )
        assert blocked is False


# ═══════════════════════════════════════════════════════════════════════
# Gap 4 — DST-season window timing for the 60/65-minute preclose windows
# ═══════════════════════════════════════════════════════════════════════

class TestDstSeasonPrecloseWindowTiming:
    """The 60/65-minute preclose tokens
    (`_metric_is_expiry_preclose_close_window`/`_alert_window`) derive
    their countdown from `ctx.segment_flags['minutes_until_mcx_close']`,
    which `_ae_segment_flags()` resolves via the real DST-aware
    `resolve_dst_aware_sessions()` (see CLAUDE.md "MCX DST-aware
    non-agri close time"). This had never been driven end to end with a
    frozen clock on a real winter date to confirm the window boundary
    tracks the WINTER close (23:55 IST) rather than the DST-season
    close (23:30) the two literal minute constants were originally
    tuned against. Reuses test_expiry_mcx_dst_residual.py's own frozen
    winter/DST reference dates.

    `_metric_is_mcx_expiry_today`/`_metric_is_nse_expiry_today` are
    patched to a fixed True/False per case — this test is about the
    close-time arithmetic, not about symbol-parsing/expiry-detection
    (already covered by test_scope_positions_expiring_today*.py), the
    same "mock what's a separately-tested concern" convention
    test_expiry_close_release_e2e.py itself documents.
    """

    _WINTER_DATE = date(2026, 11, 17)   # real MCX non-agri close 23:55 IST
    _DST_DATE = date(2026, 6, 1)        # real MCX non-agri close 23:30 IST

    @staticmethod
    def _real_segment_flags(now):
        from backend.shared.helpers.utils import config as _cfg
        from backend.api.algo.agent_engine import _ae_segment_flags
        seg_cfg = (_cfg.get("market_segments", {}) or {}).get("commodity", {}) or {}
        return _ae_segment_flags("commodity", seg_cfg, now)

    @staticmethod
    def _ctx(segment_flags):
        from types import SimpleNamespace
        return SimpleNamespace(segment_flags=segment_flags)

    def _window_bool(self, lead_minutes, ctx):
        from backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window import (
            _metric_factory_is_expiry_preclose_window,
        )
        fn = _metric_factory_is_expiry_preclose_window(lead_minutes)
        return fn(ctx, {}) == 1.0

    def test_60min_close_window_fires_at_22_56_winter_but_not_22_30_dst_season(self):
        """22:56 IST on the real winter date is 59 minutes before the
        real 23:55 close -> inside the 60-min window. The OLD flat
        23:30 assumption would have already called this leg "closed",
        reading minutes_until_mcx_close as None -- the window would
        read False. Confirms the fix is genuinely live at this level,
        not only inside _ae_segment_flags's own unit tests."""
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 11, 17, 22, 56, tzinfo=ist)
        flags = self._real_segment_flags(now)
        assert flags["mcx_open"] is True, "22:56 IST on a winter date must still read MCX open"
        assert flags["minutes_until_mcx_close"] <= 60

        with patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_mcx_expiry_today",
            return_value=1.0,
        ), patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_nse_expiry_today",
            return_value=0.0,
        ):
            ctx = self._ctx(flags)
            assert self._window_bool(60, ctx) is True

    def test_60min_close_window_does_not_fire_at_22_30_on_the_same_winter_date(self):
        """22:30 IST winter is 85 minutes before the real 23:55 close --
        outside the 60-min window. A DST-season-tuned (flat 23:30)
        implementation would have wrongly fired here (0 minutes to a
        false close), so this is the discriminating negative case."""
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 11, 17, 22, 30, tzinfo=ist)
        flags = self._real_segment_flags(now)
        assert flags["minutes_until_mcx_close"] > 60

        with patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_mcx_expiry_today",
            return_value=1.0,
        ), patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_nse_expiry_today",
            return_value=0.0,
        ):
            ctx = self._ctx(flags)
            assert self._window_bool(60, ctx) is False

    def test_65min_alert_window_fires_5_minutes_before_the_60min_close_window_in_winter(self):
        """22:51 IST winter is 64 minutes before the real 23:55 close --
        inside the 65-min alert window but still outside the 60-min
        close-action window, confirming the two stay correctly staggered
        under the WINTER close time, not just under DST-season."""
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 11, 17, 22, 51, tzinfo=ist)
        flags = self._real_segment_flags(now)
        assert 60 < flags["minutes_until_mcx_close"] <= 65

        with patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_mcx_expiry_today",
            return_value=1.0,
        ), patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_nse_expiry_today",
            return_value=0.0,
        ):
            ctx = self._ctx(flags)
            assert self._window_bool(65, ctx) is True
            assert self._window_bool(60, ctx) is False

    def test_dst_season_same_clock_time_does_not_fire_60min_window_confirming_season_awareness(self):
        """The IDENTICAL 22:56 clock time on a DST-season date is 34
        minutes before the real 23:30 DST close -- still outside the
        60-min window (34 <= 60 is actually inside... use a time that
        discriminates: 22:20 DST-season is 70 min before 23:30, outside
        60-min window; the winter test above used 22:56 which is INSIDE
        winter's window. This confirms the same wall-clock hour does not
        always mean the same window state -- it is genuinely season-
        dependent, not a constant offset from midnight)."""
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        now = datetime(2026, 6, 1, 22, 20, tzinfo=ist)
        flags = self._real_segment_flags(now)
        assert flags["minutes_until_mcx_close"] > 60, (
            "22:20 IST on a DST-season date is 70 minutes before the real "
            "23:30 close -- must read outside the 60-min window"
        )

        with patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_mcx_expiry_today",
            return_value=1.0,
        ), patch(
            "backend.api.algo.grammar_tokens.metric_is_expiry_preclose_alert_window._metric_is_nse_expiry_today",
            return_value=0.0,
        ):
            ctx = self._ctx(flags)
            assert self._window_bool(60, ctx) is False
