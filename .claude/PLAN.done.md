# Plan: Fix NavStrip P-slot regression + 3M/6M/1Y chart threshold

## Context

Two pre-existing UI bugs surfaced after the broker-coverage deploy (commit `aecd1282`).
Neither was introduced by that commit — both trace to `8474a17e` (Jul 17) and `910740f0` (Jul 15).
No Playwright tests ran for either because the coverage plan had `playwright: no`.

**Bug 1 — P slot 1 shows 0** (`8474a17e` regression)
`baseDayPnlForPosition` Case 4 guard added `close !== ltp` as a short-circuit:
```js
if (close > 0 && close !== ltp) return pnl - oq * (close - avg);
return 0;   // <-- fires when close === ltp, wiping realized P&L
```
When `close_price` hasn't been refreshed (equals LTP from broker), realized intraday P&L
in `pnl` is silently zeroed. The fix: keep only `close <= 0 → return 0`.

**Bug 2 — 3M/6M/1Y charts never load** (`910740f0` regression)
`_SELF_HEAL_COVERAGE_THRESHOLD = 0.70` fires `_still_partial` unconditionally when
`len(store_bars) < int(0.70 × days)`. NSE equity has ≈252 trading days/year = 69% of
calendar days — always below 70%. Result: every NSE 3M/6M/1Y request is flagged partial,
triggers `_ohlcv_demand_fill`, and the frontend spins indefinitely. Fix: lower to 0.60
(gives 219/365 = 60% threshold, comfortably below 252/365 = 69%).

Per standing rule — one issue at a time, separate commits.

---

## Fix 1 — NavStrip P-slot regression

### Agents
- frontend: In `frontend/src/lib/data/nav.js` lines 108-115, replace the Case 4 block:
  ```js
  // BEFORE
  if (oq > 0 && dcv === 0) {
    const ltp = Number(p?.last_price ?? 0);
    if (close > 0 && close !== ltp) return pnl - oq * (close - avg);
    return 0;
  }
  // AFTER
  if (oq > 0 && dcv === 0) {
    if (close <= 0) return 0;
    return pnl - oq * (close - avg);
  }
  ```
  Also update `docs/specs/NAVSTRIP_SPEC.md`: correct the Case 4 description — it was
  documented in `9dfc9e8b` as "returns 0 when close===ltp"; that guard is now removed,
  Case 4 only guards `close <= 0`.
  Also update `CLAUDE.md` "Case 4 stale close guard" entry to match.
- playwright: Add spec `frontend/tests/navstrip-p-slot.spec.ts` — login, wait for
  positions strip, assert P pill slot 1 (`[data-testid="p-day"]` or equivalent locator)
  shows a non-zero value when positions exist (mock or assert `!== "₹0"`). Also assert
  no zero-flash on page load.
- backend: skip
- broker: skip
- backend-test: skip
- doc: skip (handled inline by frontend agent)

### Tests
- pytest: no
- svelte-check: yes
- playwright: yes

### Commit message
```
fix(navstrip): remove close===ltp guard from baseDayPnlForPosition Case 4

Realized intraday P&L was zeroed when broker hasn't refreshed close_price
(close === ltp). Only guard close <= 0 now — avoids spurious zero when
broker's prev-close field lags during the overnight window.

Regression introduced in 8474a17e; traced via _livePositionsToday → baseDayPnlForPosition.
```

### Done when
P pill slot 1 shows correct non-zero day P&L for positions with realized intraday activity.
svelte-check 0 errors. Playwright spec green.

---

## Fix 2 — 3M/6M/1Y chart threshold

### Agents
- backend: In `backend/api/routes/options.py` line 367, change:
  ```python
  # BEFORE
  _SELF_HEAL_COVERAGE_THRESHOLD: float = 0.70
  # AFTER
  _SELF_HEAL_COVERAGE_THRESHOLD: float = 0.60
  ```
  No other changes needed — `_historical_ohlcv_store` in `options_helpers.py` already
  reads `self_heal_threshold` passed from this constant.
- playwright: Add spec `frontend/tests/chart-ranges.spec.ts` — navigate to a chart page,
  select 3M / 6M / 1Y range buttons in sequence, assert that within 8s the chart canvas
  renders data (spinner gone, bar count > 0 via JS handle or canvas non-blank check).
  Also assert no "partial data" infinite retry visible.
- frontend: skip
- broker: skip
- backend-test: skip
- doc: skip

### Tests
- pytest: no (threshold change is config-level; no unit test needed)
- svelte-check: no
- playwright: yes

### Commit message
```
fix(charts): lower SELF_HEAL_COVERAGE_THRESHOLD 0.70→0.60 for NSE equity

NSE equity has ~252 trading days/year = 69% of calendar days, always below
the 0.70 threshold — causing 3M/6M/1Y requests to be permanently flagged
partial and trigger infinite _ohlcv_demand_fill retries. 0.60 gives 219/365
threshold, comfortably below actual trading day density.

Regression introduced in 910740f0.
```

### Done when
3M, 6M, 1Y range buttons load NSE equity chart data without persistent spinner.
Playwright spec green. MCX F&O beyond front-month is intrinsically partial — not addressed.

---

## Critical files

| File | Change |
|---|---|
| `frontend/src/lib/data/nav.js` lines 108-115 | Remove `close !== ltp` guard from Case 4 |
| `docs/specs/NAVSTRIP_SPEC.md` | Correct Case 4 description |
| `CLAUDE.md` | Correct "Case 4 stale close guard" entry |
| `backend/api/routes/options.py` line 367 | `0.70 → 0.60` |
| `frontend/tests/navstrip-p-slot.spec.ts` | New Playwright spec |
| `frontend/tests/chart-ranges.spec.ts` | New Playwright spec |

## Verification

1. Fix 1: After deploy, open positions page → NavStrip P pill → slot 1 shows non-zero day P&L for positions with intraday activity. No zero-flash on load.
2. Fix 2: Select 3M / 6M / 1Y range on any NSE equity chart → data loads within a few seconds, no persistent spinner.
3. MCX front-month futures charts: partial data for 6M/1Y is expected (intrinsic limitation, not a bug).
