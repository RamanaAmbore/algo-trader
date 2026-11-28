"""2026-11-28 redesign of `loss-positions-total` — pure F&O theoretical
loss, three independent leaves, replacing the old market-P&L day_val/
day_pct floors and the drawdown-from-peak-gated rate leaves outright.

Operator instruction, in full: "Instead of looking for market loss, look
for theoretical loss for alert." / "the rate of change alert should
happen only in the negative direction when the total theoretical loss
crosses the threshold." / "1. total theory loss of position, if it
crosses threshold. 2. once it crosses threshold, any rate of change that
only increases the theory loss. 3. daily theory loss threshold." —
leaf 1 ("total") is the book's overall/cumulative theoretical shortfall
right now, a LEVEL since ENTRY; leaf 3 ("daily") is specifically TODAY's
theoretical P&L move, a DELTA since the per-session frozen baseline.

Deliberate, operator-approved scope tradeoff, stated plainly: this is
PURE THEORETICAL, F&O ONLY — NO hybrid equity fallback. A pure
equity-position loss no longer fires this guard at all. See this file's
own `TestEquityOnlyBookNeverFires`.

Known, flagged limitation (NOT a defect in this redesign, a structural
property of the frozen-vol model it builds on): σ is calibrated ONCE per
leg per session and held FROZEN (`agent_engine._update_underlying_theo_
baseline`). Neither leaf 1 nor leaf 3 ever reads a leg's live option
premium again after that — only live SPOT and live time-to-expiry feed
the reprice. A pure IV-spike loss on a short-premium book, with no theta
decay and no spot move, therefore fires NOTHING here. See
`TestFrozenSigmaCannotSeeVegaMoves` for a structural (not simulated)
proof of this limitation.

Reuses the SAME frozen-vol Black-Scholes/Black-76 reprice machinery
(`grammar._theo_reprice_leg`, `_theo_price_now_*`) already shipped and
council-approved for `underlying_loss_divergence_signed` — this is NOT
the theta/vega-blind raw-intrinsic-value metric a 6-perspective council
rejected for this exact agent on 2026-11-26 (see
`~/.claude/plans/lovely-stirring-allen.md`).

Five quality dimensions:
  SSOT        — direct invocation of the real resolvers/aggregators/
                writer/migration helper, no reimplementation of the
                formulas under test.
  Correctness — every scenario the implementation plan + advisor review
                called for is reproduced: decay (theta) fires both
                leaves, equity-only never fires, leaf 2 gated on leaf 1
                crossing, leaf 2 never fires on a recovering rate, leaf
                1 vs leaf 3 genuinely differ, gapped-underlying skip
                policy, the confirmed qty=0 gap in the EXISTING
                `_underlying_theoretical_loss` (flagged, not fixed
                here), the composition-reset guard, the pct writer, the
                render-field fix, and the drawdown->theoretical
                migration.
  Performance — pure functions / in-memory Context + alert_state, no DB
                or broker I/O.
  Reuse       — shares the REGISTRY-monkeypatch wiring pattern and the
                `_fut_baseline`/`_linear_hist`-style fixture helpers
                already established by `test_underlying_loss_divergence_
                tokens.py` and `test_loss_positions_total_drawdown_
                gate.py` (duplicated here, not imported, per this
                codebase's existing convention for small shared test
                fixture helpers).
  UX          — every assert carries an f-string with actual/expected,
                and tests assert WHICH leaf matched (by metric name),
                never merely whether the agent fired at all.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from backend.api.algo import agent_engine as ae
from backend.api.algo import grammar as g
from backend.api.algo.agent_evaluator import Context, evaluate
from backend.api.algo.derivatives import black_76, days_to_expiry


IST_NOW = datetime(2026, 9, 1, 10, 0, 0)


def _agent_cond():
    agent = next(a for a in ae._LOSS_AGENTS if a["slug"] == "loss-positions-total")
    return agent["conditions"]


def _fut_baseline(S_base, qty, is_mcx=True, dcv_base=0.0):
    """Same shape `_capture_leg_theo_baseline` would have captured for a
    futures leg — `ltp_base == S_base` by construction (see
    `_underlying_theo_legs_since_baseline_and_entry`'s own docstring for
    why this makes the futures-leg since-entry formula reduce to a plain
    `qty × (S_now − avg_cost)`)."""
    return {'kind': 'fut', 'ltp_base': S_base, 'S_base': S_base,
            'is_mcx': is_mcx, 'qty': qty, 'dcv_base': dcv_base}


@pytest.fixture
def wired_registry(monkeypatch):
    """Wire the real resolvers this condition tree needs into REGISTRY,
    restored automatically by monkeypatch — same pattern as
    test_loss_positions_total_drawdown_gate.py's own fixture."""
    from backend.api.algo.grammar_registry import REGISTRY
    from backend.api.algo.grammar_tokens.metric_theoretical_total_loss import (
        _metric_theoretical_total_loss,
    )
    from backend.api.algo.grammar_tokens.metric_theoretical_daily_loss import (
        _metric_theoretical_daily_loss,
    )
    from backend.api.algo.grammar_tokens.metric_theo_total_rate_abs import (
        _metric_theo_total_rate_abs,
    )
    from backend.api.algo.grammar_tokens.metric_theo_total_rate_pct import (
        _metric_theo_total_rate_pct,
    )
    from backend.api.algo.grammar_tokens.scope_positions_total import _scope_positions_total

    monkeypatch.setitem(REGISTRY.metrics, "theoretical_total_loss", _metric_theoretical_total_loss)
    monkeypatch.setitem(REGISTRY.metrics, "theoretical_daily_loss", _metric_theoretical_daily_loss)
    monkeypatch.setitem(REGISTRY.metrics, "theo_total_rate_abs", _metric_theo_total_rate_abs)
    monkeypatch.setitem(REGISTRY.metrics, "theo_total_rate_pct", _metric_theo_total_rate_pct)
    monkeypatch.setitem(REGISTRY.scopes, "positions.total", _scope_positions_total)
    monkeypatch.setitem(REGISTRY.operators, "<=", g.OPERATORS["<="])
    monkeypatch.setitem(REGISTRY.operators, "<", g.OPERATORS["<"])
    monkeypatch.setattr(REGISTRY, "_metric_call_cache", {})
    return REGISTRY


def _ctx(position_rows, spot_prices, baseline, *, hist=None, now=IST_NOW):
    """Builds a real evaluator Context. `positions.total`'s own scope
    resolver (`_scope_positions_total`) reads `ctx.sum_positions` — a
    DataFrame with a single 'TOTAL' account row — NOT `ctx.position_rows`
    (that's a separate, per-symbol list the theoretical metrics
    themselves read). A minimal TOTAL row is supplied here so `evaluate()`
    has something to iterate; the row's own content is irrelevant to the
    theoretical metrics, which never read `row` at all."""
    alert_state = {'underlying_theo_baseline': baseline}
    if hist is not None:
        alert_state['pnl_history'] = {g.PORTFOLIO_THEO_TOTAL_RATE_KEY: hist}
    sum_positions = pd.DataFrame([{'account': 'TOTAL', 'pnl': 0.0}])
    return Context(
        position_rows=position_rows, spot_prices=spot_prices,
        sum_positions=sum_positions,
        alert_state=alert_state, now=now, rate_window_min=10.0, baseline_live=True,
    )


def _linear_hist(base_dt, start_min, end_min, start_val, end_val, start_pct, end_pct,
                 step_min=1):
    """One sample per `step_min` minutes, linearly interpolated — same
    shape `test_loss_positions_total_drawdown_gate.py`'s own
    `_linear_hist` used for the retired market-based rate leaves."""
    out = []
    span = end_min - start_min
    m = start_min
    while m <= end_min:
        frac = (m - start_min) / span if span else 0.0
        val = start_val + (end_val - start_val) * frac
        pct = start_pct + (end_pct - start_pct) * frac
        out.append((base_dt + timedelta(minutes=m), val, pct))
        m += step_min
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Decay (theta) fires both leaf 1 and leaf 3 — the real scenario this
#  redesign is built to catch, NOT a theta/vega-blind raw-intrinsic test.
# ═══════════════════════════════════════════════════════════════════════════

class TestThetaDecayFiresLeaf1AndLeaf3:
    """A long CRUDEOIL CE option sitting near-flat spot, decaying purely
    on time value (frozen σ, same spot, less time-to-expiry) — exactly
    the case a pure spot-only / intrinsic-value metric would miss, since
    intrinsic value here is unchanged (spot never moved)."""

    def test_pure_theta_decay_fires_both_leaves(self, wired_registry):
        S_base, K, sigma_real = 5550.0, 5500.0, 0.22
        expiry = date(2026, 10, 30)  # real parse_tradingsymbol('CRUDEOIL26OCT5500CE') output
        now0 = datetime(2026, 9, 25, 10, 0, 0)
        T_base = days_to_expiry(expiry, ref=now0, close_time=(23, 30)) / 365.0
        ltp_base = black_76(S_base, K, T_base, 0.07, sigma_real, 'CE')
        qty = 1000

        baseline = {'CRUDEOIL26OCT5500CE': {
            'kind': 'opt', 'ltp_base': ltp_base, 'S_base': S_base,
            'sigma_base': sigma_real, 'K': K, 'opt_type': 'CE',
            'expiry': expiry, 'is_mcx': True, 'qty': qty, 'dcv_base': 0.0,
        }}
        rows = [{'tradingsymbol': 'CRUDEOIL26OCT5500CE', 'quantity': qty,
                 'underlying': 'CRUDEOIL', 'average_price': ltp_base}]
        now1 = datetime(2026, 10, 28, 10, 0, 0)  # near expiry, SAME spot — pure decay
        ctx = _ctx(rows, {'CRUDEOIL': S_base}, baseline, now=now1)

        # Sanity: the decay is real and crosses both -100000 floors.
        total = g._portfolio_theoretical_loss_since_entry(ctx)
        daily = g._portfolio_theoretical_loss_daily(ctx)
        assert total is not None and total <= -100000, f"leaf 1 must cross, got {total}"
        assert daily is not None and daily <= -100000, f"leaf 3 must cross, got {daily}"
        assert total == pytest.approx(daily, rel=1e-9), (
            "with avg_cost == ltp_base (opened at baseline), leaf 1 and "
            f"leaf 3 coincide for this single-leg fixture: {total} vs {daily}"
        )

        matches = evaluate(_agent_cond(), ctx)
        fired = {m["metric"] for m in matches}
        assert "theoretical_total_loss" in fired, f"leaf 1 must fire, got {fired}"
        assert "theoretical_daily_loss" in fired, f"leaf 3 must fire, got {fired}"


class TestFrozenSigmaCannotSeeVegaMoves:
    """Structural proof of the flagged limitation: neither reprice
    primitive ever reads a live option premium — only live spot and live
    time-to-expiry. A real IV expansion (vega move) is therefore
    invisible to this formula BY CONSTRUCTION, not merely untested. This
    is deliberately NOT a fabricated 'fake vega pass' fixture — it
    inspects what the function actually consumes."""

    def test_reprice_primitives_take_no_live_premium_argument(self):
        import inspect
        sig = inspect.signature(g._theo_reprice_opt_leg)
        params = set(sig.parameters)
        assert params == {'b', 'S_now', 'now'}, (
            f"the option reprice primitive's only inputs are the frozen "
            f"baseline dict, live spot, and live clock — got {params}. "
            f"If a live-premium parameter appears here, this limitation "
            f"note is stale and should be revisited."
        )

    def test_reprice_depends_only_on_frozen_sigma_not_live_market(self):
        """Holding S_now/now/ltp_base/qty fixed, the ONLY way to move the
        computed reprice is to change `sigma_base` itself — i.e. a real
        intraday IV expansion that never gets written back into the
        FROZEN baseline has zero effect on either leaf's value, for
        however long the position is held."""
        b = {'qty': 1, 'K': 5500.0, 'sigma_base': 0.22, 'ltp_base': 176.44,
             'opt_type': 'CE', 'expiry': date(2026, 10, 30), 'is_mcx': True}
        now = datetime(2026, 9, 25, 10, 0, 0)
        reprice_frozen = g._theo_reprice_opt_leg(b, S_now=5550.0, now=now)
        # Simulate "the real market's IV just spiked" by editing sigma_base
        # directly — the ONLY way this formula could ever reflect it, since
        # `_update_underlying_theo_baseline` never re-calibrates an
        # already-baselined leg (σ stays frozen for the whole session).
        b_spiked = dict(b, sigma_base=0.55)
        reprice_if_resynced = g._theo_reprice_opt_leg(b_spiked, S_now=5550.0, now=now)
        assert reprice_if_resynced != pytest.approx(reprice_frozen), (
            "sanity check: sigma_base genuinely drives the reprice"
        )
        # The production baseline writer NEVER performs this resync for an
        # already-captured leg — so in real operation, `reprice_frozen` is
        # what both leaves see for the whole session, regardless of how
        # far real IV has actually moved.


# ═══════════════════════════════════════════════════════════════════════════
#  Pure equity-position loss never fires — the deliberate scope tradeoff.
# ═══════════════════════════════════════════════════════════════════════════

class TestEquityOnlyBookNeverFires:
    def test_equity_only_rows_resolve_no_f_and_o_underlying(self, wired_registry):
        rows = [
            {'tradingsymbol': 'RELIANCE', 'quantity': 100, 'underlying': None,
             'average_price': 3000.0, 'pnl': -5000000.0},
        ]
        ctx = _ctx(rows, {}, {})
        assert g._portfolio_theoretical_loss_since_entry(ctx) is None
        assert g._portfolio_theoretical_loss_daily(ctx) is None
        matches = evaluate(_agent_cond(), ctx)
        assert not matches, (
            "a pure equity book must never fire this agent, however large "
            "its real market loss — this is the operator-approved scope "
            "tradeoff, not an oversight"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Leaf 1 (since-entry) genuinely differs from leaf 3 (since-this-morning).
# ═══════════════════════════════════════════════════════════════════════════

class TestLeaf1DiffersFromLeaf3:
    def test_deep_in_red_since_entry_but_flat_today_fires_leaf1_only(self, wired_registry):
        """Opened days ago at avg_cost=6500; today's own session baseline
        is 6000 and spot has barely moved to 6005 — a real, large
        since-entry shortfall, but today's OWN move is flat/positive."""
        qty = 300
        baseline = {'CRUDEOIL26OCTFUT': _fut_baseline(6000.0, qty)}
        rows = [{'tradingsymbol': 'CRUDEOIL26OCTFUT', 'quantity': qty,
                 'underlying': 'CRUDEOIL', 'average_price': 6500.0}]
        ctx = _ctx(rows, {'CRUDEOIL': 6005.0}, baseline)

        total = g._portfolio_theoretical_loss_since_entry(ctx)
        daily = g._portfolio_theoretical_loss_daily(ctx)
        assert total == pytest.approx(300 * (6005.0 - 6500.0)), f"leaf1 got {total}"
        assert daily == pytest.approx(300 * (6005.0 - 6000.0)), f"leaf3 got {daily}"
        assert total <= -100000, f"leaf 1 must cross, got {total}"
        assert daily > 0, f"leaf 3 must stay positive/flat, got {daily}"

        matches = evaluate(_agent_cond(), ctx)
        fired = {m["metric"] for m in matches}
        assert "theoretical_total_loss" in fired, f"leaf 1 must fire, got {fired}"
        assert "theoretical_daily_loss" not in fired, (
            f"leaf 3 must NOT fire — today's own move is flat/positive, got {fired}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Leaf 2 (rate of change) — gated on leaf 1 crossing, negative direction only.
# ═══════════════════════════════════════════════════════════════════════════

def _leaf1_crossing_ctx(hist):
    """A futures leg whose leaf 1 ALREADY crosses -100000, paired with a
    caller-supplied hand-built rate history."""
    qty = 300
    baseline = {'CRUDEOIL26OCTFUT': _fut_baseline(6000.0, qty)}
    rows = [{'tradingsymbol': 'CRUDEOIL26OCTFUT', 'quantity': qty,
             'underlying': 'CRUDEOIL', 'average_price': 6500.0}]
    return _ctx(rows, {'CRUDEOIL': 6005.0}, baseline, hist=hist)


def _leaf1_not_crossing_ctx(hist):
    """Same shape, but leaf 1 stays well short of -100000."""
    qty = 300
    baseline = {'CRUDEOIL26OCTFUT': _fut_baseline(6000.0, qty)}
    rows = [{'tradingsymbol': 'CRUDEOIL26OCTFUT', 'quantity': qty,
             'underlying': 'CRUDEOIL', 'average_price': 6010.0}]
    return _ctx(rows, {'CRUDEOIL': 6005.0}, baseline, hist=hist)


class TestLeaf2GatedOnLeaf1Crossing:
    def test_leaf2_fires_when_leaf1_crossed_and_rate_declining(self, wired_registry):
        hist = _linear_hist(IST_NOW, 0, 30, -50000.0, -250000.0, -1.0, -5.0)
        ctx = _leaf1_crossing_ctx(hist)
        assert ctx.rate_abs(g.PORTFOLIO_THEO_TOTAL_RATE_KEY) <= -6000, (
            "fixture must actually cross the rate threshold"
        )
        matches = evaluate(_agent_cond(), ctx)
        fired = {m["metric"] for m in matches}
        assert "theo_total_rate_abs" in fired, f"leaf 2 must fire, got {fired}"
        assert "theoretical_total_loss" in fired, f"leaf 1 must also fire, got {fired}"

    def test_leaf2_does_not_fire_when_leaf1_has_not_crossed(self, wired_registry):
        """Same declining rate as above, but leaf 1 itself stays short of
        its own threshold — the nested all[] gate must refuse to fire,
        even though the raw rate value alone would otherwise cross."""
        hist = _linear_hist(IST_NOW, 0, 30, -50000.0, -250000.0, -1.0, -5.0)
        ctx = _leaf1_not_crossing_ctx(hist)
        assert ctx.rate_abs(g.PORTFOLIO_THEO_TOTAL_RATE_KEY) <= -6000, (
            "fixture must still cross the raw rate threshold on its own"
        )
        matches = evaluate(_agent_cond(), ctx)
        fired = {m["metric"] for m in matches}
        assert "theo_total_rate_abs" not in fired, (
            f"leaf 2 must NOT fire while leaf 1 hasn't crossed its own "
            f"threshold, got {fired}"
        )
        assert "theoretical_total_loss" not in fired
        assert "theoretical_daily_loss" not in fired
        assert not matches, f"expected a total no-fire, got {matches}"

    def test_leaf2_never_fires_on_a_recovering_positive_rate(self, wired_registry):
        """Leaf 1 crosses (so it fires on its own, as an independent
        top-level item), but the rate itself is RECOVERING (less
        negative over time) — leaf 2's nested gate must stay closed."""
        hist = _linear_hist(IST_NOW, 0, 30, -500000.0, -148500.0, -8.0, -2.0)
        ctx = _leaf1_crossing_ctx(hist)
        rate = ctx.rate_abs(g.PORTFOLIO_THEO_TOTAL_RATE_KEY)
        assert rate is not None and rate > 0, (
            f"fixture must be genuinely recovering (positive rate), got {rate}"
        )
        matches = evaluate(_agent_cond(), ctx)
        fired = {m["metric"] for m in matches}
        assert "theoretical_total_loss" in fired, (
            f"leaf 1 fires on its own, independent of leaf 2's rate, got {fired}"
        )
        assert "theo_total_rate_abs" not in fired and "theo_total_rate_pct" not in fired, (
            f"leaf 2 must never fire on a recovering (positive) rate, got {fired}"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Nested-latch sanity — mirrors the retired drawdown-gate file's own
#  TestNestedConditionLatchKeying, re-pointed at the new tree shape.
# ═══════════════════════════════════════════════════════════════════════════

class TestNestedLatchKeyingUnderNewTree:
    def test_latch_key_identifies_the_real_leaf_not_the_wrapper(self, wired_registry):
        hist = _linear_hist(IST_NOW, 0, 30, -50000.0, -250000.0, -1.0, -5.0)
        ctx = _leaf1_crossing_ctx(hist)
        matches = evaluate(_agent_cond(), ctx)
        assert matches
        keys = {ae._latch_key("loss-positions-total", m) for m in matches}
        metrics_in_keys = {k[1] for k in keys}
        assert "theo_total_rate_abs" in metrics_in_keys
        assert len(keys) == len({m["metric"] for m in matches})

    def test_escalation_gate_operates_per_leaf_under_the_extra_nesting(self, wired_registry):
        hist = _linear_hist(IST_NOW, 0, 30, -50000.0, -250000.0, -1.0, -5.0)
        ctx = _leaf1_crossing_ctx(hist)
        matches = evaluate(_agent_cond(), ctx)
        assert matches

        class _A:
            slug = "loss-positions-total-test"

        agent = _A()
        ae._V2_LATCH.clear()
        try:
            effective = ae._v2_apply_escalation_gate(agent, matches, ctx.now, cooldown_min=45)
            assert effective, "a fresh (never-latched) nested leaf must fire on first pass"
            effective_again = ae._v2_apply_escalation_gate(agent, matches, ctx.now, cooldown_min=45)
            assert not effective_again, (
                "nested leaf's latch must gate a second fire inside the same cooldown window"
            )
        finally:
            ae._V2_LATCH.clear()


# ═══════════════════════════════════════════════════════════════════════════
#  Skip-the-gapped-underlying policy — both aggregators.
# ═══════════════════════════════════════════════════════════════════════════

class TestSkipGappedUnderlyingPolicy:
    def test_since_entry_aggregator_skips_an_unbaselined_underlying(self):
        qty_a = 300
        rows = [
            {'tradingsymbol': 'CRUDEOILFUT_A', 'quantity': qty_a,
             'underlying': 'CRUDEOIL', 'average_price': 6500.0},
            # GOLD has a row but NO baseline entry at all — a genuine gap.
            {'tradingsymbol': 'GOLDFUT_B', 'quantity': 5,
             'underlying': 'GOLD', 'average_price': 60000.0},
        ]
        baseline = {'CRUDEOILFUT_A': _fut_baseline(6000.0, qty_a)}
        ctx = _ctx(rows, {'CRUDEOIL': 6005.0, 'GOLD': 60500.0}, baseline)
        total = g._portfolio_theoretical_loss_since_entry(ctx)
        expected = qty_a * (6005.0 - 6500.0)
        assert total == pytest.approx(expected), (
            f"GOLD (gapped) must be skipped, not zero-fill or fail the "
            f"whole aggregate — expected {expected}, got {total}"
        )

    def test_daily_aggregator_skips_an_unbaselined_underlying(self):
        qty_a = 300
        rows = [
            {'tradingsymbol': 'CRUDEOILFUT_A', 'quantity': qty_a,
             'underlying': 'CRUDEOIL', 'average_price': 6500.0},
            {'tradingsymbol': 'GOLDFUT_B', 'quantity': 5,
             'underlying': 'GOLD', 'average_price': 60000.0},
        ]
        baseline = {'CRUDEOILFUT_A': _fut_baseline(6000.0, qty_a)}
        ctx = _ctx(rows, {'CRUDEOIL': 6005.0, 'GOLD': 60500.0}, baseline)
        daily = g._portfolio_theoretical_loss_daily(ctx)
        expected = qty_a * (6005.0 - 6000.0)
        assert daily == pytest.approx(expected), f"expected {expected}, got {daily}"

    def test_both_aggregators_return_none_when_every_underlying_is_gapped(self):
        rows = [{'tradingsymbol': 'GOLDFUT_B', 'quantity': 5,
                 'underlying': 'GOLD', 'average_price': 60000.0}]
        ctx = _ctx(rows, {'GOLD': 60500.0}, {})
        assert g._portfolio_theoretical_loss_since_entry(ctx) is None
        assert g._portfolio_theoretical_loss_daily(ctx) is None


# ═══════════════════════════════════════════════════════════════════════════
#  Confirmed, FLAGGED (not fixed here) gap in the EXISTING
#  `_underlying_theoretical_loss` — a qty=0 row Kite keeps for a
#  closed-today leg makes the whole underlying return None.
# ═══════════════════════════════════════════════════════════════════════════

class TestUnderlyingTheoreticalLossQtyZeroLegGapConfirmed:
    def test_existing_function_goes_blind_on_a_closed_leg_in_the_same_underlying(self):
        """Reproduces the gap exactly as advisor review described it:
        one open leg (baselined) + one qty=0 leg of the SAME underlying
        (Kite keeps the row; the baseline writer correctly drops its own
        entry for a flat leg) makes the EXISTING `_underlying_
        theoretical_loss` return None for the WHOLE underlying — even
        though the open leg alone is perfectly fine. NOT fixed here —
        flagged for a separate pass on `underlying_theoretical_loss`/
        `underlying_loss_divergence_signed`."""
        qty = 300
        baseline = {'CRUDEOILFUT_OPEN': _fut_baseline(6000.0, qty)}
        rows = [
            {'tradingsymbol': 'CRUDEOILFUT_OPEN', 'quantity': qty,
             'underlying': 'CRUDEOIL', 'average_price': 6500.0},
            {'tradingsymbol': 'CRUDEOILFUT_CLOSED', 'quantity': 0,
             'underlying': 'CRUDEOIL', 'average_price': 6100.0},
        ]
        ctx = _ctx(rows, {'CRUDEOIL': 6005.0}, baseline)
        assert g._underlying_theoretical_loss(ctx, 'CRUDEOIL') is None, (
            "confirming the gap: a stray qty=0 row of the SAME underlying "
            "makes the existing function go blind even though the open "
            "leg alone has a perfectly good baseline"
        )

    def test_new_walk_is_not_affected_by_the_same_qty_zero_row(self):
        """The NEW portfolio-wide walk (`_underlying_theo_legs_since_
        baseline_and_entry`, used by both leaf 1 and leaf 3) filters to
        non-flat legs BEFORE consulting the baseline, so it does NOT
        inherit this gap."""
        qty = 300
        baseline = {'CRUDEOILFUT_OPEN': _fut_baseline(6000.0, qty)}
        rows = [
            {'tradingsymbol': 'CRUDEOILFUT_OPEN', 'quantity': qty,
             'underlying': 'CRUDEOIL', 'average_price': 6500.0},
            {'tradingsymbol': 'CRUDEOILFUT_CLOSED', 'quantity': 0,
             'underlying': 'CRUDEOIL', 'average_price': 6100.0},
        ]
        ctx = _ctx(rows, {'CRUDEOIL': 6005.0}, baseline)
        since_baseline, since_entry = g._underlying_theo_legs_since_baseline_and_entry(
            ctx, 'CRUDEOIL')
        assert since_baseline is not None and since_entry is not None, (
            "the new walk must stay available despite the stray qty=0 row"
        )
        assert since_entry == pytest.approx(qty * (6005.0 - 6500.0))


# ═══════════════════════════════════════════════════════════════════════════
#  Writer: agent_engine._update_portfolio_theo_total_history
# ═══════════════════════════════════════════════════════════════════════════

class TestPortfolioTheoTotalHistoryWriter:
    def _rows(self, qty=300, avg_cost=6500.0, sym='CRUDEOILFUT_A'):
        return [{'tradingsymbol': sym, 'quantity': qty,
                 'underlying': 'CRUDEOIL', 'average_price': avg_cost}]

    def test_appends_a_sample_with_computed_pct(self):
        qty = 300
        alert_state = {'underlying_theo_baseline': {
            'CRUDEOILFUT_A': _fut_baseline(6000.0, qty)}}
        df_margins = pd.DataFrame([{'account': 'TOTAL', 'util debits': 400000.0, 'net': 100000.0}])
        ae._update_portfolio_theo_total_history(
            alert_state, IST_NOW, self._rows(qty=qty), {'CRUDEOIL': 6005.0}, df_margins,
        )
        hist = alert_state['pnl_history'][g.PORTFOLIO_THEO_TOTAL_RATE_KEY]
        assert len(hist) == 1
        now_s, value, pct = hist[-1]
        expected_value = qty * (6005.0 - 6500.0)
        assert value == pytest.approx(expected_value)
        expected_pct = expected_value / 500000.0 * 100.0
        assert pct == pytest.approx(expected_pct), (
            f"pct must be value/margin_base*100, expected {expected_pct}, got {pct}"
        )

    def test_pct_is_none_when_margin_unavailable(self):
        qty = 300
        alert_state = {'underlying_theo_baseline': {
            'CRUDEOILFUT_A': _fut_baseline(6000.0, qty)}}
        ae._update_portfolio_theo_total_history(
            alert_state, IST_NOW, self._rows(qty=qty), {'CRUDEOIL': 6005.0}, None,
        )
        hist = alert_state['pnl_history'][g.PORTFOLIO_THEO_TOTAL_RATE_KEY]
        assert hist[-1][2] is None, "pct must be None (never 0) when margin data is unavailable"

    def test_no_f_and_o_book_records_nothing(self):
        alert_state = {}
        rows = [{'tradingsymbol': 'RELIANCE', 'quantity': 100, 'underlying': None}]
        ae._update_portfolio_theo_total_history(alert_state, IST_NOW, rows, {}, None)
        assert alert_state.get('pnl_history', {}).get(g.PORTFOLIO_THEO_TOTAL_RATE_KEY) in (None, [])

    def test_closed_market_tick_records_nothing(self):
        alert_state = {}
        qty = 300
        alert_state['underlying_theo_baseline'] = {'CRUDEOILFUT_A': _fut_baseline(6000.0, qty)}
        ae._update_portfolio_theo_total_history(
            alert_state, IST_NOW, self._rows(qty=qty), {'CRUDEOIL': 6005.0}, None,
            market_state={'nse_open': False, 'mcx_open': False},
        )
        assert alert_state.get('pnl_history', {}).get(g.PORTFOLIO_THEO_TOTAL_RATE_KEY) in (None, [])

    def test_empty_position_rows_records_nothing(self):
        alert_state = {'pnl_history': {g.PORTFOLIO_THEO_TOTAL_RATE_KEY: [(IST_NOW, -1.0, None)]}}
        ae._update_portfolio_theo_total_history(alert_state, IST_NOW, [], {}, None)
        assert len(alert_state['pnl_history'][g.PORTFOLIO_THEO_TOTAL_RATE_KEY]) == 1

    def test_composition_change_clears_the_bucket(self):
        """A leg add/close/qty-change must NOT be read by the rate window
        as a real P&L move — the bucket resets clean on the new
        composition instead of stepping across it."""
        qty = 300
        alert_state = {'underlying_theo_baseline': {
            'CRUDEOILFUT_A': _fut_baseline(6000.0, qty)}}
        t0 = IST_NOW
        t1 = IST_NOW + timedelta(minutes=1)
        ae._update_portfolio_theo_total_history(
            alert_state, t0, self._rows(qty=qty), {'CRUDEOIL': 6005.0}, None)
        ae._update_portfolio_theo_total_history(
            alert_state, t1, self._rows(qty=qty), {'CRUDEOIL': 6006.0}, None)
        assert len(alert_state['pnl_history'][g.PORTFOLIO_THEO_TOTAL_RATE_KEY]) == 2, (
            "sanity: two same-composition ticks accumulate normally"
        )

        # Composition changes: a DIFFERENT leg (new symbol) now carries
        # the whole book.
        t2 = t1 + timedelta(minutes=1)
        new_rows = self._rows(qty=qty, sym='CRUDEOILFUT_NEW_LEG')
        alert_state['underlying_theo_baseline']['CRUDEOILFUT_NEW_LEG'] = _fut_baseline(6000.0, qty)
        ae._update_portfolio_theo_total_history(
            alert_state, t2, new_rows, {'CRUDEOIL': 6007.0}, None)
        hist = alert_state['pnl_history'][g.PORTFOLIO_THEO_TOTAL_RATE_KEY]
        assert len(hist) == 1, (
            f"the bucket must be cleared and restarted on a composition "
            f"change, got {len(hist)} samples: {hist}"
        )

    def test_same_composition_different_tick_does_not_clear(self):
        """Pure price movement (same symbol/qty) must never trip the
        composition guard."""
        qty = 300
        alert_state = {'underlying_theo_baseline': {
            'CRUDEOILFUT_A': _fut_baseline(6000.0, qty)}}
        for i, spot in enumerate((6005.0, 6006.0, 6007.0)):
            ae._update_portfolio_theo_total_history(
                alert_state, IST_NOW + timedelta(minutes=i), self._rows(qty=qty),
                {'CRUDEOIL': spot}, None)
        assert len(alert_state['pnl_history'][g.PORTFOLIO_THEO_TOTAL_RATE_KEY]) == 3


# ═══════════════════════════════════════════════════════════════════════════
#  Render fix: `_v2_extract_pnl_fields` must show the theoretical value,
#  not the row's raw market `pnl`.
# ═══════════════════════════════════════════════════════════════════════════

class TestRenderShowsTheoreticalValueNotMarketPnl:
    @pytest.mark.parametrize("metric", [
        "theoretical_total_loss", "theoretical_daily_loss",
        "theo_total_rate_abs", "theo_total_rate_pct",
    ])
    def test_theoretical_metrics_render_their_own_value(self, metric):
        row = {'account': 'TOTAL', 'pnl': 999999.0}  # deliberately WRONG market pnl
        pnl, pct = ae._v2_extract_pnl_fields(row, 'Positions', metric, value=-148500.0)
        assert pnl == pytest.approx(-148500.0), (
            f"{metric} must render its own resolved value, not row['pnl'] "
            f"(999999.0) — got {pnl}"
        )

    def test_day_val_still_reads_day_change_val_unaffected(self):
        """Regression guard: the pre-existing day_val branch (used by
        OTHER agents, e.g. loss-positions-acct) must be byte-identical."""
        row = {'account': 'TOTAL', 'pnl': 1.0, 'day_change_val': -5.0}
        pnl, pct = ae._v2_extract_pnl_fields(row, 'Positions', 'day_val', value=-5.0)
        assert pnl == -5.0

    def test_other_positions_metrics_still_fall_back_to_market_pnl(self):
        """Regression guard: a non-theoretical, non-day_val Positions
        metric (e.g. pnl_rate_abs, used by other agents) is unaffected."""
        row = {'account': 'TOTAL', 'pnl': -7000.0}
        pnl, pct = ae._v2_extract_pnl_fields(row, 'Positions', 'pnl_rate_abs', value=-6500.0)
        assert pnl == -7000.0, "non-theoretical Positions metrics keep the old fallback"


# ═══════════════════════════════════════════════════════════════════════════
#  Migration: drawdown-gated market-P&L shape -> pure-theoretical shape.
# ═══════════════════════════════════════════════════════════════════════════

# The EXACT shape this agent shipped with since the 2026-11 noise-
# reduction redesign — confirmed byte-for-byte identical on both prod
# and dev via a direct DB read before this edit.
_DRAWDOWN_GATED_OLD_COND = {"any": [
    {"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -100000},
    {"metric": "day_pct", "scope": "positions.total", "op": "<=", "value": -2.0},
    {"all": [
        {"any": [
            {"metric": "day_val", "scope": "positions.total", "op": "<", "value": 0},
            {"metric": "max_drawdown_pnl(30)", "scope": "positions.total",
             "op": "<=", "value": -20000},
        ]},
        {"any": [
            {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]},
    ]},
]}


class TestDrawdownToTheoreticalMigration:
    def test_migration_table_entry_matches_the_live_pre_edit_shape(self):
        assert ae._AE_CONTENT_MATCHED_COND_MIGRATIONS["loss-positions-total"] == \
            _DRAWDOWN_GATED_OLD_COND, (
                "the migration table's old_cond must exactly match what was "
                "confirmed live on prod/dev before this redesign shipped"
            )

    def test_migration_upgrades_a_row_still_at_the_drawdown_shape(self):
        from types import SimpleNamespace

        agent_def = next(a for a in ae._LOSS_AGENTS if a["slug"] == "loss-positions-total")
        existing = SimpleNamespace(
            slug="loss-positions-total", long_name=agent_def.get("long_name"),
            schedule="market_hours", tier="critical", topic="positions_loss",
            events=agent_def.get("events", []), fire_at_time=None, last_fired=None,
            conditions=dict(_DRAWDOWN_GATED_OLD_COND), cooldown_minutes=45,
            status="inactive",
        )
        ae._ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == agent_def["conditions"], (
            "a row still at the exact drawdown-gated shape must be "
            "upgraded to the new pure-theoretical shape"
        )
        # Status must NEVER be force-changed by this migration — the
        # operator's existing (inactive) choice survives untouched.
        assert existing.status == "inactive"

    def test_migration_leaves_a_customized_row_alone(self):
        from types import SimpleNamespace

        agent_def = next(a for a in ae._LOSS_AGENTS if a["slug"] == "loss-positions-total")
        customized = {"any": [
            {"metric": "theoretical_total_loss", "scope": "positions.total",
             "op": "<=", "value": -75000},
        ]}
        existing = SimpleNamespace(
            slug="loss-positions-total", long_name=agent_def.get("long_name"),
            schedule="market_hours", tier="critical", topic="positions_loss",
            events=agent_def.get("events", []), fire_at_time=None, last_fired=None,
            conditions=dict(customized), cooldown_minutes=45, status="active",
        )
        ae._ae_sync_existing_builtin(existing, agent_def)
        assert existing.conditions == customized, "operator customization must survive"
