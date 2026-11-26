"""Tests for grammar_tokens/metric_underlying_loss_divergence_signed.py —
`underlying_loss_divergence_signed` metric.

2026-11-26 (operator: "if the rate of change towards exp max profit, that
is okay. if it is away, that needs to be alerted") — the signed sibling of
`underlying_loss_divergence`. The single most important case this file
pins: a underlying OUTPERFORMING its theoretical/Greeks-predicted path
(market better than theoretical) must read POSITIVE, never trigger the
agent's `<= -100000` adverse-only condition no matter how large the
outperformance — this is the whole point of the signed redesign over the
old `abs()` form.
"""

from datetime import datetime

import pytest

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_underlying_loss_divergence_signed import (
    _metric_underlying_loss_divergence_signed,
)

IST_NOW = datetime(2026, 9, 1, 10, 0, 0)


def _fut_baseline(S_base, qty, is_mcx=False, dcv_base=0.0):
    return {'kind': 'fut', 'ltp_base': S_base, 'S_base': S_base,
            'is_mcx': is_mcx, 'qty': qty, 'dcv_base': dcv_base}


def test_adverse_underperformance_is_negative():
    """Market much worse than theoretical (the agent's alert-worthy
    direction) — sign must be negative."""
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
             'day_change_val': -150000.0}]
    alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)}}
    ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': 100.0},
                  alert_state=alert_state, now=IST_NOW)
    signed = _metric_underlying_loss_divergence_signed(ctx, {'underlying': 'CRUDEOIL'})
    # theoretical == 0 (spot unchanged from baseline); market == -150000.
    assert signed == pytest.approx(-150000.0), (
        f"expected market(-150000) - theoretical(0) = -150000, got {signed}"
    )


def test_benign_outperformance_is_positive_however_large():
    """The critical sign-flip case: market BETTER than theoretical (the
    underlying is outperforming its own expected decay/Greeks path) must
    read POSITIVE — never the adverse direction, regardless of magnitude.
    This is the exact scenario the old unsigned abs() form would have
    misclassified as alert-worthy; the signed redesign must not."""
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
             'day_change_val': 150000.0}]
    alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)}}
    ctx = Context(position_rows=rows, spot_prices={'CRUDEOIL': 100.0},
                  alert_state=alert_state, now=IST_NOW)
    signed = _metric_underlying_loss_divergence_signed(ctx, {'underlying': 'CRUDEOIL'})
    assert signed == pytest.approx(150000.0) and signed > 0, (
        f"expected a large POSITIVE value (outperforming theoretical, "
        f"benign), got {signed} — this must never satisfy the agent's "
        f"<=-100000 adverse-only condition"
    )


def test_sign_differs_from_unsigned_sibling_only_in_the_outperform_case():
    """Cross-check against the unsigned sibling: for the adverse case,
    abs(signed) == unsigned; for the benign/outperform case, the signed
    metric is positive while the unsigned sibling would read the same
    large magnitude regardless of direction — proving the redesign
    actually changes which case is alert-eligible, not just the number."""
    from backend.api.algo.grammar_tokens.metric_underlying_loss_divergence import (
        _metric_underlying_loss_divergence,
    )
    rows_adverse = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
                      'day_change_val': -150000.0}]
    rows_benign = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
                     'day_change_val': 150000.0}]
    alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)}}

    ctx_adverse = Context(position_rows=rows_adverse, spot_prices={'CRUDEOIL': 100.0},
                          alert_state=alert_state, now=IST_NOW)
    ctx_benign = Context(position_rows=rows_benign, spot_prices={'CRUDEOIL': 100.0},
                         alert_state=alert_state, now=IST_NOW)

    unsigned_adverse = _metric_underlying_loss_divergence(ctx_adverse, {'underlying': 'CRUDEOIL'})
    unsigned_benign = _metric_underlying_loss_divergence(ctx_benign, {'underlying': 'CRUDEOIL'})
    signed_adverse = _metric_underlying_loss_divergence_signed(ctx_adverse, {'underlying': 'CRUDEOIL'})
    signed_benign = _metric_underlying_loss_divergence_signed(ctx_benign, {'underlying': 'CRUDEOIL'})

    assert unsigned_adverse == pytest.approx(150000.0) and unsigned_benign == pytest.approx(150000.0), (
        "the OLD unsigned metric reads the same large magnitude for both "
        "directions - this is exactly the ambiguity the signed redesign fixes"
    )
    assert signed_adverse < 0 and signed_benign > 0, (
        f"signed metric must disambiguate direction: adverse={signed_adverse}, "
        f"benign={signed_benign}"
    )


def test_none_when_theoretical_unavailable():
    rows = [{'underlying': 'CRUDEOIL', 'tradingsymbol': 'SYM', 'quantity': 10,
             'day_change_val': -150000.0}]
    ctx = Context(position_rows=rows, spot_prices={}, alert_state={}, now=IST_NOW)
    assert _metric_underlying_loss_divergence_signed(ctx, {'underlying': 'CRUDEOIL'}) is None


def test_none_when_market_unavailable():
    """The baseline still holds a captured leg (so theoretical resolves
    from the baseline + live spot alone), but `position_rows` carries no
    matching row this tick (e.g. a transient fetch gap) -> market is
    None -> signed divergence must be None, never a false 0."""
    alert_state = {'underlying_theo_baseline': {'SYM': _fut_baseline(100.0, 10)}}
    ctx = Context(position_rows=[], spot_prices={'CRUDEOIL': 100.0},
                  alert_state=alert_state, now=IST_NOW)
    assert _metric_underlying_loss_divergence_signed(ctx, {'underlying': 'CRUDEOIL'}) is None
