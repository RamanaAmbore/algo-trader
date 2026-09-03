# Plan: Fix alert agent gaps — pnl_pct denominator + expiry debug logging

## Task
Three gaps identified in the alert audit (₹ thresholds kept at current values per operator):

1. **P0 code bug** — `_metric_pnl_pct` always returns `None` for intraday/MIS positions.
   `used_margin_for()` reads `util debits` from df_margins, which is 0 for intraday orders.
   `None` causes the evaluator to silently skip the leaf → `loss-positions-acct` (pnl_pct ≤ -2%)
   and `loss-positions-total` (pnl_pct ≤ -2%) never fire on intraday sessions.
   Fix: fall back to `net` margin column when `util debits = 0`. `net` = available margin,
   a non-zero proxy for capital deployed; makes pnl_pct = "P&L as % of available margin".

2. **P1 expiry logging** — `spot_prices` dict is empty on the live path, so `is_itm` returns
   `None` for every expiry-aware agent (`expiry-day-commodity-itm-auto-close`). Add a WARNING
   log when spot_prices is empty at engine evaluation time so the operator can see when expiry
   agents are silently skipped.

3. **P2 test coverage** — no tests for the `pnl_pct` denominator fallback or for
   `used_margin_for` with zero/non-zero util debits.

## Agents
- backend: Fix `_metric_pnl_pct` denominator. In `backend/api/algo/agent_evaluator.py:93-104`,
  update `used_margin_for()` to fall back to `net` column when `util debits` row value is 0:
  ```python
  util_debits = float(match.iloc[0].get('util debits', 0) or 0)
  if util_debits > 0:
      return util_debits
  net = float(match.iloc[0].get('net', 0) or 0)
  return net if net > 0 else None
  ```
  Also add a WARNING log in `backend/api/algo/agent_engine.py` (around line 1570 where Context
  is constructed) when `spot_prices` is empty so expiry agents' silent skips are visible in logs.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add tests in `backend/tests/test_agent_grammar.py` (create if absent):
  - `test_pnl_pct_falls_back_to_net_when_util_debits_zero`: df_margins with util_debits=0, net=100000, pnl=-3000 → pnl_pct = -3.0
  - `test_pnl_pct_returns_none_when_both_zero`: util_debits=0, net=0 → None
  - `test_pnl_pct_uses_util_debits_when_nonzero`: util_debits=50000, net=80000 → util_debits used as denominator
  - `test_used_margin_for_falls_back_to_net`: direct unit test of Context.used_margin_for()
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(agents): pnl_pct uses net margin fallback when util_debits=0 + spot_prices warning log

## Done when
- `_metric_pnl_pct` returns non-None for intraday positions where util_debits=0 (net margin denominator)
- `used_margin_for()` returns `net` when `util debits=0` and `net>0`; None only when both are 0
- 4 new tests pass covering the denominator fallback
- WARNING log fires (once per tick) when spot_prices is empty during engine evaluation
- Pytest green, broker cov ≥ 80%, api cov ≥ 45%
