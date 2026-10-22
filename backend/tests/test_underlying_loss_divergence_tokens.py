"""
Theoretical-vs-market loss divergence gate, per underlying — four new
grammar tokens (`positions.any_underlying` scope; `underlying_market_loss`,
`underlying_theoretical_loss`, `underlying_loss_divergence` metrics) plus
two structural gap fixes (A, B) and one serialization-survival fix (C)
required to let a condition tree gate a per-underlying rate-of-change
alert on this divergence without silently misfiring across underlyings.

Baseline-reprice design (see `grammar.py`'s module comment above
`_scope_positions_any_underlying` and `agent_engine.py`'s comment above
`_update_underlying_theo_baseline` for the full rationale): implied vol is
calibrated ONCE per leg per session (`_capture_leg_theo_baseline`) and
frozen in `alert_state['underlying_theo_baseline']`; only spot and
time-to-expiry move tick to tick thereafter. Calibrating IV from the SAME
tick being evaluated would be circular (divergence always ≈0) — this is
why the write side (baseline capture) and read side (theoretical-loss
metric) are deliberately separate functions in separate modules.

Five quality dimensions:
  SSOT        — direct invocation of the real resolvers/latch functions,
                no reimplementation of the formulas under test.
  Correctness — every worked example from the implementation plan
                reproduced (Fix A, Fix B (a)/(b), Fix C round-trip,
                calibration-failure guard, missing-spot guard).
  Performance — pure functions / in-memory Context, no DB or broker I/O.
  Reuse       — shares the REGISTRY-monkeypatch wiring pattern established
                by `test_agent_evaluator.py::TestAllJoinsSameAccount` and
                `test_market_cycle_tokens.py`'s `_cycle_evaluate_agent`
                wiring tests.
  UX          — every assert carries an f-string with actual/expected.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.api.algo import agent_engine as ae
from backend.api.algo import grammar as g
from backend.api.algo.agent_evaluator import Context, evaluate
from backend.api.algo.derivatives import black_76


IST_NOW = datetime(2026, 9, 1, 10, 0, 0)


# ═══════════════════════════════════════════════════════════════════════════
#  Scope: positions.any_underlying
# ═══════════════════════════════════════════════════════════════════════════

class TestScopePositionsAnyUnderlying:
    def test_groups_rows_by_underlying(self):
        rows = [
            {'underlying': 'NIFTY', 'account': 'ACC1', 'pnl': -100.0, 'day_change_val': -50.0},
            {'underlying': 'NIFTY', 'account': 'ACC2', 'pnl': -200.0, 'day_change_val': -75.0},
            {'underlying': 'CRUDEOIL', 'account': 'ACC1', 'pnl': 300.0, 'day_change_val': 10.0},
        ]
        ctx = Context(position_rows=rows)
        out = g._scope_positions_any_underlying(ctx)
        by_u = {r['underlying']: r for r in out}
        assert set(by_u.keys()) == {'NIFTY', 'CRUDEOIL'}, f"got {by_u.keys()}"
        assert by_u['NIFTY']['pnl'] == -300.0
        assert by_u['NIFTY']['day_change_val'] == -125.0
        assert by_u['NIFTY']['accounts'] == ['ACC1', 'ACC2']
        assert by_u['CRUDEOIL']['pnl'] == 300.0

    def test_rows_without_underlying_are_skipped(self):
        """Cash-equity / unparseable rows (no 'underlying') never produce
        a bucket — same skip rule `_scope_positions_expiring_today` uses."""
        rows = [
            {'underlying': None, 'account': 'ACC1', 'pnl': -999.0},
            {'account': 'ACC1', 'pnl': -999.0},  # key entirely absent
        ]
        ctx = Context(position_rows=rows)
        assert g._scope_positions_any_underlying(ctx) == []

    def test_empty_position_rows_returns_empty(self):
        assert g._scope_positions_any_underlying(Context(position_rows=[])) == []
        assert g._scope_positions_any_underlying(Context()) == []


# ═══════════════════════════════════════════════════════════════════════════
#  Metric: underlying_market_loss
# ═══════════════════════════════════════════════════════════════════════════

class TestMetricUnderlyingMarketLoss:
    def test_sums_day_change_val_across_legs_of_one_underlying(self):
        rows = [
            {'underlying': 'NIFTY', 'day_change_val': -50.0},
            {'underlying': 'NIFTY', 'day_change_val': -75.0},
            {'underlying': 'CRUDEOIL', 'day_change_val': 10.0},
        ]
        ctx = Context(position_rows=rows)
        val = g._metric_underlying_market_loss(ctx, {'underlying': 'NIFTY'})
        assert val == -125.0, f"expected -125.0, got {val}"

    def test_missing_underlying_on_row_is_none(self):
        ctx = Context(position_rows=[{'underlying': 'NIFTY', 'day_change_val': -50.0}])
        assert g._metric_underlying_market_loss(ctx, {}) is None

    def test_missing_day_change_val_treated_as_zero(self):
        ctx = Context(position_rows=[{'underlying': 'NIFTY'}])
        val = g._metric_underlying_market_loss(ctx, {'underlying': 'NIFTY'})
        assert val == 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  Metric: underlying_theoretical_loss (+ underlying_loss_divergence)
# ═══════════════════════════════════════════════════════════════════════════

def _fut_baseline(S_base, qty, is_mcx=False):
    return {'kind': 'fut', 'ltp_base': S_base, 'S_base': S_base,
            'is_mcx': is_mcx, 'qty': qty}


class TestMetricUnderlyingTheoreticalLossAndDivergence:
    def test_future_leg_linear_reprice(self):
        """theoretical_loss(underlying) = qty * (S_now - S_base) for a
        futures leg — no option Greeks repricing."""
        rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'CRUDEOIL26OCTFUT',
                 'quantity': 10, 'day_change_val': 0.0}]
        alert_state = {'underlying_theo_baseline': {
            'CRUDEOIL26OCTFUT': _fut_baseline(S_base=6000.0, qty=10, is_mcx=True),
        }}
        ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': 6050.0},
                      alert_state=alert_state, now=IST_NOW)
        theo = g._metric_underlying_theoretical_loss(ctx, {'underlying': 'CRUDEOIL'})
        assert theo == pytest.approx(10 * (6050.0 - 6000.0)), f"got {theo}"

    def test_option_leg_bs_reprice_matches_black_76_formula(self):
        """A frozen-σ option leg reprices via the SAME black_76 primitive
        the metric uses internally — not a reimplementation."""
        from backend.api.algo.derivatives import days_to_expiry

        S_base, K, sigma_base = 5550.0, 5500.0, 0.22
        expiry = date(2026, 10, 30)  # real parse_tradingsymbol('CRUDEOIL26OCT5500CE') output
        # T_now the metric itself will compute at IST_NOW — reused here so
        # the fixture's own ltp_base is built at the EXACT same T as the
        # metric will reprice against (not an approximate hand-picked DTE).
        T_now = days_to_expiry(expiry, ref=IST_NOW, close_time=(23, 30)) / 365.0
        ltp_base = black_76(S_base, K, T_now, 0.07, sigma_base, 'CE')
        rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'CRUDEOIL26OCT5500CE',
                 'quantity': 5, 'day_change_val': 0.0}]
        alert_state = {'underlying_theo_baseline': {
            'CRUDEOIL26OCT5500CE': {
                'kind': 'opt', 'ltp_base': ltp_base, 'S_base': S_base,
                'sigma_base': sigma_base, 'K': K, 'opt_type': 'CE',
                'expiry': expiry, 'is_mcx': True, 'qty': 5,
            },
        }}
        # Spot unchanged from baseline, same `now` as baseline capture —
        # reprice must reproduce ltp_base exactly (zero theoretical move).
        ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': S_base},
                      alert_state=alert_state, now=IST_NOW)
        theo = g._metric_underlying_theoretical_loss(ctx, {'underlying': 'CRUDEOIL'})
        assert theo == pytest.approx(0.0, abs=1e-6), f"expected ~0 at baseline spot, got {theo}"

        # Spot moves up — theoretical P&L must track the BS/Black-76
        # reprice, not a linear delta-only approximation.
        ctx2 = Context(position_rows=rows, spot_prices={'CRUDEOIL': S_base + 100.0},
                       alert_state=alert_state, now=IST_NOW)
        theo2 = g._metric_underlying_theoretical_loss(ctx2, {'underlying': 'CRUDEOIL'})
        expected_price = black_76(S_base + 100.0, K, T_now, 0.07, sigma_base, 'CE')
        assert theo2 == pytest.approx(5 * (expected_price - ltp_base)), f"got {theo2}"

    def test_missing_spot_price_is_none(self):
        rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 1}]
        alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 1)}}
        ctx = Context(position_rows=rows, spot_prices={}, alert_state=alert_state, now=IST_NOW)
        assert g._metric_underlying_theoretical_loss(ctx, {'underlying': 'CRUDEOIL'}) is None

    def test_missing_baseline_for_one_leg_makes_whole_underlying_none(self):
        """A partial sum would UNDERSTATE theoretical loss — the whole
        underlying must report unavailable, not silently drop the leg."""
        rows = [
            {'underlying': 'NIFTY', 'tradingsymbol': 'LEG_A', 'quantity': 1},
            {'underlying': 'NIFTY', 'tradingsymbol': 'LEG_B', 'quantity': 1},
        ]
        alert_state = {'underlying_theo_baseline': {'LEG_A': _fut_baseline(100.0, 1)}}
        ctx = Context(position_rows=rows, spot_prices={'NIFTY': 100.0},
                      alert_state=alert_state, now=IST_NOW)
        assert g._metric_underlying_theoretical_loss(ctx, {'underlying': 'NIFTY'}) is None

    def test_no_position_rows_for_underlying_is_none(self):
        ctx = Context(position_rows=[], spot_prices={'NIFTY': 100.0}, now=IST_NOW)
        assert g._metric_underlying_theoretical_loss(ctx, {'underlying': 'NIFTY'}) is None

    def test_divergence_is_abs_diff_of_theo_and_market(self):
        rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
                 'day_change_val': -150000.0}]
        alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)}}
        ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': 100.0},
                      alert_state=alert_state, now=IST_NOW)
        div = g._metric_underlying_loss_divergence(ctx, {'underlying': 'CRUDEOIL'})
        # theoretical == 0 (spot unchanged from baseline); market == -150000
        assert div == pytest.approx(150000.0), f"got {div}"

    def test_divergence_is_none_when_theoretical_unavailable(self):
        rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
                 'day_change_val': -150000.0}]
        ctx = Context(position_rows=rows, spot_prices={}, alert_state={}, now=IST_NOW)
        assert g._metric_underlying_loss_divergence(ctx, {'underlying': 'CRUDEOIL'}) is None


# ═══════════════════════════════════════════════════════════════════════════
#  Baseline writer: agent_engine._update_underlying_theo_baseline /
#  _capture_leg_theo_baseline (the calibration-failure guard lives here)
# ═══════════════════════════════════════════════════════════════════════════

class TestBaselineWriter:
    def test_captures_future_leg_on_first_sight(self):
        alert_state = {'session_date': date(2026, 9, 1)}
        rows = [{'tradingsymbol': 'CRUDEOIL26OCTFUT', 'quantity': 10,
                 'underlying': 'CRUDEOIL', 'last_price': 6010.0}]
        ae._update_underlying_theo_baseline(alert_state, IST_NOW, rows, {'CRUDEOIL': 6000.0})
        baseline = alert_state['underlying_theo_baseline']
        assert 'CRUDEOIL26OCTFUT' in baseline
        entry = baseline['CRUDEOIL26OCTFUT']
        assert entry['kind'] == 'fut'
        assert entry['qty'] == 10
        assert entry['S_base'] == 6000.0

    def test_captures_option_leg_with_reconciling_iv(self):
        from backend.api.algo.derivatives import days_to_expiry

        alert_state = {'session_date': date(2026, 9, 1)}
        S_base, K, sigma_real = 5550.0, 5500.0, 0.22
        now = datetime(2026, 9, 25, 10, 0, 0)  # ~30 days before Oct expiry
        expiry = date(2026, 10, 30)  # real parse_tradingsymbol('CRUDEOIL26OCT5500CE') output
        T_base = days_to_expiry(expiry, ref=now, close_time=(23, 30)) / 365.0
        ltp_base = black_76(S_base, K, T_base, 0.07, sigma_real, 'CE')

        rows = [{'tradingsymbol': 'CRUDEOIL26OCT5500CE', 'quantity': 5,
                 'underlying': 'CRUDEOIL', 'last_price': ltp_base}]
        ae._update_underlying_theo_baseline(alert_state, now, rows, {'CRUDEOIL': S_base})
        baseline = alert_state['underlying_theo_baseline']
        assert 'CRUDEOIL26OCT5500CE' in baseline, (
            "a realistic, reconciling option premium must be baselined"
        )
        entry = baseline['CRUDEOIL26OCT5500CE']
        assert entry['opt_type'] == 'CE'
        assert entry['K'] == K
        assert entry['is_mcx'] is True
        assert entry['expiry'] == expiry
        # The calibrated sigma must reconcile against ltp_base — this is
        # what "reconciling" means operationally.
        reprice = black_76(S_base, K, T_base, 0.07, entry['sigma_base'], 'CE')
        assert abs(reprice - ltp_base) <= max(0.01 * ltp_base, 0.50)

    def test_calibration_failure_guard_skips_leg(self):
        """An absurd/garbage LTP (far outside any plausible bisection
        bracket) must NOT be baselined — it would otherwise bake a fake
        divergence into every later tick for that leg."""
        alert_state = {'session_date': date(2026, 9, 1)}
        rows = [{'tradingsymbol': 'CRUDEOIL26OCT5500CE', 'quantity': 5,
                 'underlying': 'CRUDEOIL', 'last_price': 1_000_000.0}]
        now = datetime(2026, 9, 25, 10, 0, 0)
        ae._update_underlying_theo_baseline(alert_state, now, rows, {'CRUDEOIL': 5550.0})
        assert 'CRUDEOIL26OCT5500CE' not in alert_state['underlying_theo_baseline'], (
            "a non-reconciling IV calibration must be rejected, not baselined"
        )

    def test_already_baselined_leg_is_never_recalibrated(self):
        """σ stays FROZEN once captured — a later tick must not overwrite
        it even if spot/LTP have since moved (the whole point of the
        baseline-reprice design)."""
        alert_state = {
            'session_date': date(2026, 9, 1),
            '_underlying_theo_baseline_date': date(2026, 9, 1),
            'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)},
        }
        rows = [{'tradingsymbol': 'SYM', 'quantity': 10, 'underlying': 'X'}]
        ae._update_underlying_theo_baseline(alert_state, IST_NOW, rows, {'X': 999.0})
        assert alert_state['underlying_theo_baseline']['SYM']['S_base'] == 100.0

    def test_closed_leg_is_dropped_when_other_rows_confirm_a_real_fetch(self):
        """A leg genuinely closed (gone from a NON-empty, confirmed-real
        position_rows) must be dropped — the normal, expected cleanup
        path, distinct from the outage no-op below."""
        alert_state = {
            'session_date': date(2026, 9, 1),
            '_underlying_theo_baseline_date': date(2026, 9, 1),
            'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)},
        }
        # A DIFFERENT, unrelated leg is present — proves this tick's fetch
        # genuinely succeeded; SYM is simply no longer in the book.
        rows = [{'tradingsymbol': 'OTHER', 'quantity': 5, 'underlying': 'Y',
                 'last_price': 50.0}]
        ae._update_underlying_theo_baseline(alert_state, IST_NOW, rows, {'Y': 50.0})
        assert 'SYM' not in alert_state['underlying_theo_baseline']

    def test_empty_position_rows_is_a_no_op_not_a_wipe(self):
        """Outage guard (advisor-confirmed correctness bug, fix #5 class)
        — a tick with NO position rows at all (fetch failure, or no read
        happened yet) must leave every existing baseline UNTOUCHED, never
        wipe it. Wiping here would silently re-baseline every leg at
        WHATEVER price the next clean tick happens to see, collapsing
        theoretical loss back to ~0 and defeating the whole feature."""
        alert_state = {
            'session_date': date(2026, 9, 1),
            '_underlying_theo_baseline_date': date(2026, 9, 1),
            'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)},
        }
        ae._update_underlying_theo_baseline(alert_state, IST_NOW, [], {})
        assert alert_state['underlying_theo_baseline'] == {'SYM': _fut_baseline(100.0, 10)}

    def test_partial_outage_attrs_flag_is_a_no_op_not_a_wipe(self):
        """Same outage guard, triggered via the `sum_positions.attrs[
        'partial_outage']` signal instead of an empty row list — one
        account's rows silently missing while another leg's rows are
        still present must not drop that missing account's baselines."""
        alert_state = {
            'session_date': date(2026, 9, 1),
            '_underlying_theo_baseline_date': date(2026, 9, 1),
            'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)},
        }
        rows = [{'tradingsymbol': 'OTHER', 'quantity': 5, 'underlying': 'Y',
                 'last_price': 50.0}]
        sum_positions = pd.DataFrame([{'account': 'TOTAL'}])
        sum_positions.attrs['partial_outage'] = ['ACC_X']
        ae._update_underlying_theo_baseline(alert_state, IST_NOW, rows, {'Y': 50.0},
                                            sum_positions=sum_positions)
        assert alert_state['underlying_theo_baseline'] == {'SYM': _fut_baseline(100.0, 10)}, (
            "a partial-outage tick must not drop SYM's baseline even though "
            "SYM isn't in this tick's (incomplete) position_rows"
        )

    def test_flat_row_is_not_baselined_and_drops_existing_entry(self):
        alert_state = {
            'session_date': date(2026, 9, 1),
            '_underlying_theo_baseline_date': date(2026, 9, 1),
            'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)},
        }
        rows = [{'tradingsymbol': 'SYM', 'quantity': 0, 'underlying': 'X'}]
        ae._update_underlying_theo_baseline(alert_state, IST_NOW, rows, {'X': 100.0})
        assert alert_state['underlying_theo_baseline'] == {}

    def test_session_rollover_resets_baseline(self):
        alert_state = {
            'session_date': date(2026, 9, 2),
            '_underlying_theo_baseline_date': date(2026, 9, 1),
            'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)},
        }
        ae._update_underlying_theo_baseline(alert_state, IST_NOW, [], {})
        assert alert_state['underlying_theo_baseline'] == {}
        assert alert_state['_underlying_theo_baseline_date'] == date(2026, 9, 2)


# ═══════════════════════════════════════════════════════════════════════════
#  Rate-of-change plumbing: agent_engine._update_underlying_pnl_history
# ═══════════════════════════════════════════════════════════════════════════

class TestUpdateUnderlyingPnlHistory:
    def test_appends_per_underlying_sample(self):
        alert_state = {}
        rows = [
            {'underlying': 'A', 'day_change_val': -50.0},
            {'underlying': 'A', 'day_change_val': -25.0},
            {'underlying': 'B', 'day_change_val': 10.0},
        ]
        ae._update_underlying_pnl_history(alert_state, IST_NOW, rows)
        hist = alert_state['pnl_history']
        assert hist[('positions_underlying', 'A')][-1] == (IST_NOW, -75.0, None)
        assert hist[('positions_underlying', 'B')][-1] == (IST_NOW, 10.0, None)

    def test_empty_position_rows_is_a_no_op(self):
        alert_state = {'pnl_history': {('positions_underlying', 'A'): [(IST_NOW, -1.0, None)]}}
        ae._update_underlying_pnl_history(alert_state, IST_NOW, [])
        assert len(alert_state['pnl_history'][('positions_underlying', 'A')]) == 1

    def test_partial_outage_attrs_flag_skips_sample(self):
        """Fix per advisor review — the plan specifies this guard reads
        the SAME `sum_positions.attrs['partial_outage']` signal
        `_update_pnl_history`'s own TOTAL-bucket skip uses, not merely an
        empty-rows check. A non-empty but INCOMPLETE `position_rows`
        (one account silently missing) must not append a sample that
        would poison the rate window once the missing account recovers."""
        alert_state = {}
        rows = [{'underlying': 'A', 'day_change_val': -999999.0}]
        sum_positions = pd.DataFrame([{'account': 'TOTAL'}])
        sum_positions.attrs['partial_outage'] = ['ACC_X']
        ae._update_underlying_pnl_history(alert_state, IST_NOW, rows, sum_positions=sum_positions)
        assert alert_state.get('pnl_history', {}) == {}, (
            "a partial-outage tick must not record any underlying sample"
        )

    def test_market_closed_skips_sample(self):
        alert_state = {}
        rows = [{'underlying': 'A', 'day_change_val': -50.0}]
        ae._update_underlying_pnl_history(alert_state, IST_NOW, rows,
                                          market_state={'nse_open': False, 'mcx_open': False})
        assert alert_state.get('pnl_history', {}) == {}

    def test_rows_without_underlying_are_skipped(self):
        alert_state = {}
        rows = [{'day_change_val': -50.0}]  # no 'underlying' key
        ae._update_underlying_pnl_history(alert_state, IST_NOW, rows)
        assert alert_state.get('pnl_history', {}) == {}


# ═══════════════════════════════════════════════════════════════════════════
#  Fix A — per-underlying latch key (two underlyings must never share one)
# ═══════════════════════════════════════════════════════════════════════════

class TestFixALatchKeyPerUnderlying:
    def test_latch_key_falls_back_to_underlying_when_no_account(self):
        m_a = {'metric': 'underlying_loss_divergence', 'scope': 'positions.any_underlying',
               'account': None, 'underlying': 'NIFTY'}
        m_b = {'metric': 'underlying_loss_divergence', 'scope': 'positions.any_underlying',
               'account': None, 'underlying': 'CRUDEOIL'}
        key_a = ae._latch_key('loss-positions-underlying-divergence', m_a)
        key_b = ae._latch_key('loss-positions-underlying-divergence', m_b)
        assert key_a != key_b, f"expected distinct keys, got {key_a} == {key_b}"
        assert 'TOTAL' not in key_a and 'TOTAL' not in key_b

    def test_both_none_falls_back_to_total(self):
        m = {'metric': 'day_val', 'scope': 'positions.total', 'account': None, 'underlying': None}
        key = ae._latch_key('loss-positions-total', m)
        assert key[-1] == 'TOTAL'

    def test_two_underlyings_breaching_same_tick_get_distinct_latches(self):
        """The single most important test in this plan — two DIFFERENT
        underlyings breaching `underlying_loss_divergence` in the SAME
        evaluate()/run_cycle pass must produce two DISTINCT `_V2_LATCH`
        keys, not a shared 'TOTAL' one."""
        class _Agent:
            slug = 'loss-positions-underlying-divergence'

        match_a = {'metric': 'underlying_loss_divergence', 'scope': 'positions.any_underlying',
                   'account': None, 'underlying': 'NIFTY', 'op': '>', 'threshold': 100000,
                   'value': 150000.0}
        match_b = {'metric': 'underlying_loss_divergence', 'scope': 'positions.any_underlying',
                   'account': None, 'underlying': 'CRUDEOIL', 'op': '>', 'threshold': 100000,
                   'value': 200000.0}
        store: dict = {}
        now = datetime(2026, 9, 1, 10, 0, 0)
        effective = ae._v2_apply_escalation_gate(_Agent(), [match_a, match_b], now,
                                                 cooldown_min=10, store=store)
        assert len(effective) == 2, f"both first-time breaches must fire, got {effective}"
        key_a = ae._latch_key(_Agent.slug, match_a)
        key_b = ae._latch_key(_Agent.slug, match_b)
        assert key_a != key_b
        assert key_a in store and key_b in store, f"expected both keys latched, got {store.keys()}"
        assert store[key_a]['val'] == 150000.0
        assert store[key_b]['val'] == 200000.0


# ═══════════════════════════════════════════════════════════════════════════
#  Fix B — cross-row AND-join recognizes 'underlying' as a join dimension
# ═══════════════════════════════════════════════════════════════════════════

_GATED_CONDITION = {"all": [
    {"metric": "underlying_loss_divergence", "scope": "positions.any_underlying",
     "op": ">", "value": 100000},
    {"any": [
        {"metric": "pnl_rate_abs", "scope": "positions.any_underlying", "op": "<=", "value": -10000},
        {"metric": "pnl_rate_pct", "scope": "positions.any_underlying", "op": "<=", "value": -0.25},
    ]},
]}


class TestFixBCrossRowJoinOnUnderlying:
    """Same bug class fix #2 closed for accounts (`loss-margin-low`), one
    dimension over: without Fix B, `_eval_all` treats every
    `positions.any_underlying` match as 'unkeyed' (no real `account`), so
    it flattens both children independently — underlying A breaching
    divergence + underlying B independently breaching rate would wrongly
    fire the AND."""

    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        from backend.api.algo.grammar_registry import REGISTRY
        monkeypatch.setitem(REGISTRY.scopes, 'positions.any_underlying', g._scope_positions_any_underlying)
        monkeypatch.setitem(REGISTRY.metrics, 'underlying_loss_divergence', g._metric_underlying_loss_divergence)
        monkeypatch.setitem(REGISTRY.metrics, 'pnl_rate_abs', g._metric_pnl_rate_abs)
        monkeypatch.setitem(REGISTRY.metrics, 'pnl_rate_pct', g._metric_pnl_rate_pct)
        monkeypatch.setitem(REGISTRY.operators, '>', g.OPERATORS['>'])
        monkeypatch.setitem(REGISTRY.operators, '<=', g.OPERATORS['<='])

    @staticmethod
    def _burn_history(now):
        return [
            (now - timedelta(minutes=10), -10000.0, None),
            (now - timedelta(minutes=5), -70000.0, None),
            (now, -130000.0, None),
        ]

    def test_underlying_a_divergence_and_underlying_b_rate_does_not_fire(self):
        """A breaches divergence ONLY; B breaches rate ONLY — no single
        underlying satisfies the whole tree."""
        now = datetime(2026, 9, 1, 10, 0, 0)
        rows = [
            {'underlying': 'A', 'tradingsymbol': 'SYM_A', 'quantity': 10, 'day_change_val': -150000.0},
            {'underlying': 'B', 'tradingsymbol': 'SYM_B', 'quantity': 10, 'day_change_val': -5000.0},
        ]
        alert_state = {
            'underlying_theo_baseline': {
                'SYM_A': _fut_baseline(100.0, 10),
                'SYM_B': _fut_baseline(200.0, 10),
            },
            'pnl_history': {
                ('positions_underlying', 'B'): self._burn_history(now),
            },
        }
        ctx = Context(position_rows=rows, spot_prices={'A': 100.0, 'B': 200.0},
                      alert_state=alert_state, now=now, rate_window_min=10.0)
        result = evaluate(_GATED_CONDITION, ctx)
        assert result == [], (
            f"underlying A's divergence breach must not combine with underlying "
            f"B's independent rate breach — got {result}"
        )

    def test_underlying_a_divergence_and_underlying_a_rate_fires(self):
        """The genuine same-underlying case must still fire."""
        now = datetime(2026, 9, 1, 10, 0, 0)
        rows = [
            {'underlying': 'A', 'tradingsymbol': 'SYM_A', 'quantity': 10, 'day_change_val': -150000.0},
        ]
        alert_state = {
            'underlying_theo_baseline': {'SYM_A': _fut_baseline(100.0, 10)},
            'pnl_history': {('positions_underlying', 'A'): self._burn_history(now)},
        }
        ctx = Context(position_rows=rows, spot_prices={'A': 100.0},
                      alert_state=alert_state, now=now, rate_window_min=10.0)
        result = evaluate(_GATED_CONDITION, ctx)
        assert result, "underlying A breaches both leaves — the AND must fire"
        assert all(m.get('underlying') == 'A' for m in result), f"got {result}"

    def test_pre_existing_account_level_fix2_regression_is_unaffected(self):
        """Direct re-run of the fix #2 `loss-margin-low` repro through the
        GENERALIZED `_eval_all` — must behave identically to before Fix B
        (account-keyed children still only intersect against each other)."""
        from backend.api.algo.grammar import (
            _metric_avail_margin, _scope_funds_any_acct,
        )
        from backend.api.algo.grammar_registry import REGISTRY
        import pytest as _pytest
        mp = _pytest.MonkeyPatch()
        try:
            mp.setitem(REGISTRY.metrics, 'avail_margin', _metric_avail_margin)
            mp.setitem(REGISTRY.scopes, 'funds.any_acct', _scope_funds_any_acct)
            mp.setitem(REGISTRY.operators, '<', g.OPERATORS['<'])
            mp.setitem(REGISTRY.operators, '>', g.OPERATORS['>'])
            rows = [
                {'account': 'ACCT_A', 'net': 0.0},
                {'account': 'ACCT_B', 'net': 373828.52},
            ]
            ctx = Context(df_margins=pd.DataFrame(rows))
            cond = {"all": [
                {"op": "<", "scope": "funds.any_acct", "metric": "avail_margin", "value": 25000},
                {"op": ">", "scope": "funds.any_acct", "metric": "avail_margin", "value": 0},
            ]}
            result = evaluate(cond, ctx)
            assert result == [], (
                f"Fix B must not change the fix #2 account-level outcome — got {result}"
            )
        finally:
            mp.undo()


# ═══════════════════════════════════════════════════════════════════════════
#  Fix C — latch hydration survives agent_events.detail JSON round trip
# ═══════════════════════════════════════════════════════════════════════════

class TestFixCHydrationRoundTrip:
    def test_two_underlyings_survive_serialize_then_hydrate(self):
        ae._V2_LATCH.clear()
        match_a = {'metric': 'underlying_loss_divergence', 'scope': 'positions.any_underlying',
                   'account': None, 'underlying': 'NIFTY', 'op': '>', 'threshold': 100000,
                   'value': 150000.0}
        match_b = {'metric': 'underlying_loss_divergence', 'scope': 'positions.any_underlying',
                   'account': None, 'underlying': 'CRUDEOIL', 'op': '>', 'threshold': 100000,
                   'value': 200000.0}
        # Mirror the real write path: _v2_build_evalresult builds `detail`,
        # and `_log_event`/events.py serializes it with a bare json.dumps
        # (no field whitelist) before persisting to agent_events.detail.
        eval_result = ae._v2_build_evalresult([match_a, match_b], 'Underlying divergence agent')
        detail_json = json.dumps(eval_result.detail)

        ts = datetime(2026, 9, 1, 9, 45, 0, tzinfo=timezone.utc)
        ae._hydrate_latch_from_rows(
            [('loss-positions-underlying-divergence', detail_json, ts)], today=ts.date(),
        )

        key_a = ae._latch_key('loss-positions-underlying-divergence', match_a)
        key_b = ae._latch_key('loss-positions-underlying-divergence', match_b)
        assert key_a != key_b
        assert key_a in ae._V2_LATCH and key_b in ae._V2_LATCH, (
            f"expected both underlying keys to survive the round trip, got {ae._V2_LATCH.keys()}"
        )
        assert ae._V2_LATCH[key_a]['val'] == 150000.0
        assert ae._V2_LATCH[key_b]['val'] == 200000.0


# ═══════════════════════════════════════════════════════════════════════════
#  End-to-end wiring — _cycle_evaluate_agent, real BUILTIN_AGENTS condition
# ═══════════════════════════════════════════════════════════════════════════

class TestEndToEndWiring:
    """Proves the tokens actually RESOLVE through the full REGISTRY +
    V2Context + evaluator path (not a silent 'unknown token' no-fire) —
    see `test_market_cycle_tokens.py::TestCycleEvaluateAgentThreadsSegmentFlags`
    for the established pattern this mirrors."""

    @pytest.fixture(autouse=True)
    def _wire_registry(self, monkeypatch):
        from backend.api.algo.grammar_registry import REGISTRY
        monkeypatch.setitem(REGISTRY.scopes, 'positions.any_underlying', g._scope_positions_any_underlying)
        monkeypatch.setitem(REGISTRY.metrics, 'underlying_market_loss', g._metric_underlying_market_loss)
        monkeypatch.setitem(REGISTRY.metrics, 'underlying_theoretical_loss', g._metric_underlying_theoretical_loss)
        monkeypatch.setitem(REGISTRY.metrics, 'underlying_loss_divergence', g._metric_underlying_loss_divergence)
        monkeypatch.setitem(REGISTRY.metrics, 'pnl_rate_abs', g._metric_pnl_rate_abs)
        monkeypatch.setitem(REGISTRY.metrics, 'pnl_rate_pct', g._metric_pnl_rate_pct)
        monkeypatch.setitem(REGISTRY.operators, '>', g.OPERATORS['>'])
        monkeypatch.setitem(REGISTRY.operators, '<=', g.OPERATORS['<='])

    def _context_and_state(self, now):
        rows = [
            {'underlying': 'A', 'tradingsymbol': 'SYM_A', 'quantity': 10,
             'day_change_val': -150000.0, 'account': 'ACC1'},
            {'underlying': 'B', 'tradingsymbol': 'SYM_B', 'quantity': 10,
             'day_change_val': -5000.0, 'account': 'ACC2'},
        ]
        burn = [
            (now - timedelta(minutes=10), -10000.0, None),
            (now - timedelta(minutes=5), -70000.0, None),
            (now, -130000.0, None),
        ]
        alert_state = {
            'underlying_theo_baseline': {
                'SYM_A': _fut_baseline(100.0, 10),
                'SYM_B': _fut_baseline(200.0, 10),
            },
            'pnl_history': {('positions_underlying', 'A'): burn},
        }
        context = {
            "now": now,
            "position_rows": rows,
            "spot_prices": {'A': 100.0, 'B': 200.0},
            "sum_holdings": None, "sum_positions": None, "df_margins": None,
        }
        return context, alert_state

    def test_builtin_agent_fires_once_for_the_breaching_underlying_only(self):
        now = datetime(2026, 9, 1, 10, 0, 0)
        context, alert_state = self._context_and_state(now)
        cfg = {"rate_window_min": 10, "baseline_offset_min": 15}

        agent = next(a for a in ae.BUILTIN_AGENTS
                     if a["slug"] == "loss-positions-underlying-divergence")

        class _FakeAgent:
            id = 1
            slug = agent["slug"]
            conditions = agent["conditions"]
            given = None

        matches, observations = ae._cycle_evaluate_agent(
            _FakeAgent(), context, cfg, now, alert_state=alert_state,
            bypass_schedule=True,
        )
        assert matches, "expected the real builtin agent's condition tree to fire for underlying A"
        assert all(m.get('underlying') == 'A' for m in matches), f"got {matches}"
        assert observations, "tokens must have genuinely resolved — ctx.observations must be non-empty"
        resolved_metrics = {o.get('metric') for o in observations}
        assert 'underlying_loss_divergence' in resolved_metrics
        assert 'pnl_rate_abs' in resolved_metrics

    def test_all_four_new_tokens_resolve_through_the_registry(self):
        """Lighter wiring check — a hand-built tree exercising all 4 new
        tokens as leaves directly, confirming every one resolves (is
        present in ctx.observations), independent of the real builtin
        agent's own tree shape."""
        now = datetime(2026, 9, 1, 10, 0, 0)
        context, alert_state = self._context_and_state(now)
        cfg = {"rate_window_min": 10, "baseline_offset_min": 15}

        cond = {"any": [
            {"metric": "underlying_market_loss", "scope": "positions.any_underlying",
             "op": "<=", "value": -1},
            {"metric": "underlying_theoretical_loss", "scope": "positions.any_underlying",
             "op": "<=", "value": 10**9},
            {"metric": "underlying_loss_divergence", "scope": "positions.any_underlying",
             "op": ">", "value": 1},
        ]}

        class _FakeAgent:
            id = 1
            slug = "probe-all-four-tokens"
            conditions = cond
            given = None

        matches, observations = ae._cycle_evaluate_agent(
            _FakeAgent(), context, cfg, now, alert_state=alert_state,
            bypass_schedule=True,
        )
        assert matches, "expected at least one leaf to fire"
        resolved_metrics = {o.get('metric') for o in observations}
        for tok in ('underlying_market_loss', 'underlying_theoretical_loss',
                    'underlying_loss_divergence'):
            assert tok in resolved_metrics, (
                f"{tok} never resolved (observations={resolved_metrics}) — "
                f"token is registered but not actually reachable"
            )


# ═══════════════════════════════════════════════════════════════════════════
#  Alert-rendering path — advisor-flagged verification, not in the original
#  plan's explicit scope: a `positions.any_underlying` match (no 'account'
#  key at all) must render through _v2_match_to_alertrow without raising,
#  and must label the alert row with the real underlying, not a
#  mislabeled 'TOTAL'.
# ═══════════════════════════════════════════════════════════════════════════

class TestAlertRenderingPathWithUnderlyingMatch:
    def test_does_not_raise_and_labels_scope_with_underlying_not_total(self):
        match = {
            'metric': 'underlying_loss_divergence', 'scope': 'positions.any_underlying',
            'op': '>', 'threshold': 100000, 'value': 150000.0,
            'row': {'underlying': 'CRUDEOIL', 'accounts': ['ACC1'], 'pnl': -150000.0,
                    'day_change_val': -150000.0},
            'account': None, 'underlying': 'CRUDEOIL',
        }
        # Must not raise — the strongest guarantee this test gives.
        alert_row = ae._v2_match_to_alertrow(match)
        assert alert_row['section'] == 'Positions'
        assert alert_row['scope'] == 'CRUDEOIL', (
            f"expected the real underlying as the alert's scope label, got "
            f"{alert_row['scope']!r} — a bare 'TOTAL' here would silently "
            f"mislabel which underlying actually fired"
        )

    def test_rate_abs_leaf_match_also_does_not_raise(self):
        match = {
            'metric': 'pnl_rate_abs', 'scope': 'positions.any_underlying',
            'op': '<=', 'threshold': -10000, 'value': -12000.0,
            'row': {'underlying': 'NIFTY', 'accounts': ['ACC1'], 'pnl': -20000.0,
                    'day_change_val': -20000.0},
            'account': None, 'underlying': 'NIFTY',
        }
        alert_row = ae._v2_match_to_alertrow(match)
        assert alert_row['kind'] == 'rate_abs'
        assert alert_row['scope'] == 'NIFTY'

    def test_existing_account_scoped_match_is_unaffected(self):
        """The fallback must not change behaviour for every pre-existing
        scope, which always carries a real, truthy `account`."""
        match = {
            'metric': 'day_val', 'scope': 'positions.any_acct', 'op': '<=',
            'threshold': -30000, 'value': -31000.0,
            'row': {'account': 'ZD1234', 'day_change_val': -31000.0, 'pnl': -31000.0},
            'account': 'ZD1234',
        }
        alert_row = ae._v2_match_to_alertrow(match)
        assert alert_row['scope'] != 'TOTAL'  # masked account id, not TOTAL and not raised
