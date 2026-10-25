"""Tests for grammar_tokens/metric_underlying_loss_divergence.py —
`underlying_loss_divergence` metric.

Includes the single most important test in the original migrated
module (per its own comment): the 2026-10-22 gap-day reference-point
fix regression test. Before that fix, `underlying_market_loss` summed
RAW day_change_val (measured from prev_close), while
`underlying_theoretical_loss` measures "change since the theoretical
baseline was captured" (which can be well after prev_close's own
08:00 IST reset). A pure gap-day move already reflected in
day_change_val by the time the theo baseline was captured would then
read as a large FALSE divergence with zero real volatility surprise.

Moved verbatim from test_underlying_loss_divergence_tokens.py's
TestMetricUnderlyingTheoreticalLossAndDivergence (divergence-only
tests) and TestMarketLossGapDayReferencePointFix (Wave 1, batch 3 of
the DSL token-unification plan).
"""

from datetime import datetime

import pytest

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_underlying_loss_divergence import (
    _metric_underlying_loss_divergence,
)
from backend.api.algo.grammar_tokens.metric_underlying_market_loss import (
    _metric_underlying_market_loss,
)
from backend.api.algo.grammar_tokens.metric_underlying_theoretical_loss import (
    _metric_underlying_theoretical_loss,
)

IST_NOW = datetime(2026, 9, 1, 10, 0, 0)


def _fut_baseline(S_base, qty, is_mcx=False, dcv_base=0.0):
    return {'kind': 'fut', 'ltp_base': S_base, 'S_base': S_base,
            'is_mcx': is_mcx, 'qty': qty, 'dcv_base': dcv_base}


def test_divergence_is_abs_diff_of_theo_and_market():
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
             'day_change_val': -150000.0}]
    alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)}}
    ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': 100.0},
                  alert_state=alert_state, now=IST_NOW)
    div = _metric_underlying_loss_divergence(ctx, {'underlying': 'CRUDEOIL'})
    # theoretical == 0 (spot unchanged from baseline); market == -150000
    assert div == pytest.approx(150000.0), f"got {div}"


def test_divergence_is_none_when_theoretical_unavailable():
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
             'day_change_val': -150000.0}]
    ctx = Context(position_rows=rows, spot_prices={}, alert_state={}, now=IST_NOW)
    assert _metric_underlying_loss_divergence(ctx, {'underlying': 'CRUDEOIL'}) is None


def test_gap_day_already_priced_in_by_baseline_time_shows_small_not_large_divergence():
    underlying = 'CRUDEOIL'
    sym = 'CRUDEOILGAPFUT'
    qty = 10
    S_base = 6000.0
    # A huge overnight gap (prev_close -> the LTP at the moment the
    # theo baseline was captured this session) has ALREADY shown up
    # as day_change_val by baseline-capture time.
    dcv_base = -90000.0
    baseline = {'kind': 'fut', 'ltp_base': S_base, 'S_base': S_base,
                'is_mcx': True, 'qty': qty, 'dcv_base': dcv_base}

    # Only a SMALL real move has happened since baseline capture:
    # spot drifts 6000 -> 5980 (-20 per unit, -200 for qty=10). The
    # broker's own raw day_change_val (always measured from
    # prev_close, never from the baseline) tracks the pre-existing
    # gap PLUS this small incremental move.
    S_now = 5980.0
    dcv_now = -90200.0  # -90000 (gap, already baked into dcv_base) + -200 (real move)

    rows = [{'underlying': underlying, 'tradingsymbol': sym, 'quantity': qty,
             'day_change_val': dcv_now}]
    alert_state = {'underlying_theo_baseline': {sym: baseline}}
    ctx = Context(position_rows=rows, spot_prices={underlying: S_now},
                  alert_state=alert_state, now=IST_NOW)

    market = _metric_underlying_market_loss(ctx, {'underlying': underlying})
    theo = _metric_underlying_theoretical_loss(ctx, {'underlying': underlying})
    divergence = _metric_underlying_loss_divergence(ctx, {'underlying': underlying})

    # The OLD (pre-fix) raw-day_change_val semantics would have
    # reported market == dcv_now == -90200.0, and divergence ==
    # 90000.0 — a huge false reading entirely attributable to the
    # overnight gap, not to any real post-baseline volatility.
    assert market == pytest.approx(-200.0), (
        f"expected the baseline-relative delta (-200.0, the REAL "
        f"post-baseline move only), got {market} — the pre-fix bug "
        f"would have reported {dcv_now} (the raw, gap-inflated value)"
    )
    assert theo == pytest.approx(qty * (S_now - S_base)), f"got {theo}"
    assert divergence == pytest.approx(0.0, abs=1e-6), (
        f"expected ~0 divergence (both sides now measure the SAME "
        f"small real post-baseline move) — got {divergence}; the "
        f"pre-fix bug would have reported a false divergence of "
        f"{abs(dcv_now - theo) if theo is not None else '?'} purely "
        f"from the overnight gap"
    )
