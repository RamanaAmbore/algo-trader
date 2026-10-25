"""Tests for grammar_tokens/metric_days_until_expiry.py —
`days_until_expiry` metric.

Moved verbatim from test_expiry_resolvers.py (Wave 1, batch 3 of the
DSL token-unification plan).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from backend.api.algo.agent_evaluator import Context
from backend.api.algo.grammar_tokens.metric_days_until_expiry import _metric_days_until_expiry


def _ctx(*, position_rows=None, spot_prices=None, now=None) -> Context:
    return Context(
        sum_positions=pd.DataFrame(),
        position_rows=position_rows or [],
        spot_prices=spot_prices or {},
        now=now or datetime.now(timezone.utc),
    )


def test_days_until_expiry_parses_monthly_option():
    # NIFTY 25-APR-22000 CE (monthly) — expiry = last Thursday of April
    # 2025 = 24th April 2025.
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(now=datetime(2025, 4, 24, 9, 0, tzinfo=timezone.utc))  # ~14:30 IST
    d = _metric_days_until_expiry(ctx, row)
    assert d is not None
    # 24 Apr 2025 expiry at 15:30 IST; ref 09:00 UTC = 14:30 IST → ~1 hour to expiry
    assert 0 < d < 0.2


def test_days_until_expiry_floors_after_expiry():
    row = {"tradingsymbol": "NIFTY25APR22000CE", "exchange": "NFO"}
    ctx = _ctx(now=datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc))
    d = _metric_days_until_expiry(ctx, row)
    assert d == 0.0


def test_days_until_expiry_none_for_equity():
    row = {"tradingsymbol": "RELIANCE", "exchange": "NSE"}
    assert _metric_days_until_expiry(_ctx(), row) is None


def test_days_until_expiry_mcx_uses_2330_close():
    # CRUDEOILM expiry — MCX trades till 23:30 so an option expiring
    # today has more time-to-expiry than an NFO option at the same
    # wall-clock time. MCX commodity monthlies expire on the last
    # FRIDAY of the contract month (per MCX rule revision), not the
    # equity 'last Thursday' convention.
    row = {"tradingsymbol": "CRUDEOILM25MAY5500CE", "exchange": "MCX"}
    # 30 May 2025 — last Friday of May 2025. At 12:00 UTC = 17:30 IST,
    # 6 hours to the 23:30 IST MCX close → days_to_expiry ≈ 0.25.
    ctx = _ctx(now=datetime(2025, 5, 30, 12, 0, tzinfo=timezone.utc))
    d = _metric_days_until_expiry(ctx, row)
    assert d is not None and 0.2 < d < 0.3
