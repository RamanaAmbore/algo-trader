# Proxy Hedges + Beta Regression Specification

Proxy hedges map unlisted or thinly-traded holdings (GOLDBEES, NIFTYBEES) to their
underlying commodities or indices (GOLD, NIFTY) via cross-reference table and dynamic
beta regression. The UI displays effective hedging quantities in a PROXY chip, allowing
operators to quickly calculate how many target contracts to short for a delta-neutral
position.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/routes/hedge_proxies.py` · `backend/api/models.py` ·
`backend/api/background.py`

---

## Contents

1. [Schema and Purpose](#1-schema-and-purpose)
2. [Beta Regression Math](#2-beta-regression-math)
3. [Operator-Triggered Compute](#3-operator-triggered-compute)
4. [Auto-Recompute Schedule](#4-auto-recompute-schedule)
5. [UI Display — PROXY Chip](#5-ui-display--proxy-chip)
6. [Target Symbol Hints and Resolution](#6-target-symbol-hints-and-resolution)
7. [MCX Commodity Rollover Constraints](#7-mcx-commodity-rollover-constraints)
8. [Edge Cases and Recovery](#8-edge-cases-and-recovery)
9. [Test Coverage Map](#9-test-coverage-map)

---

## 1. Schema and Purpose

**hedge_proxies table**:

| Column | Type | Purpose |
|---|---|---|
| proxy_symbol | string | Holding symbol (GOLDBEES, NIFTYBEES) |
| target_root | string | Underlying futures root (GOLD, NIFTY) for regression |
| is_active | boolean | Include in UI and regression sweep |
| note | string | Operator annotation (e.g. "pending correlation review") |
| beta | float | Regression coefficient Cov(proxy, target) / Var(target) |
| correlation | float | Pearson r (−1 to +1) for confidence assessment |
| regression_at | timestamp | Last regression timestamp (triggers auto-recompute age check) |

Purpose: When an operator holds GOLDBEES (ETF tracking GOLD), they need to know
how many GOLD futures contracts to short for a delta-neutral hedge. Math:
`effective_qty = β × market_value / target_spot`.

Correlation (r) is stored alongside β for confidence assessment. When r < 0.7,
the regression is unreliable; operator receives a warning to review manually.

---

## 2. Beta Regression Math

**Formula**: `β = Cov(p, t) / Var(t)`

where p = proxy returns (daily % change) and t = target returns over a 60-day window.

**Requirements**:
- Minimum 15 bars (calendar days) of overlapping returns data
- Both symbols must have valid price history (no truncation, no NaN)
- Window: rolling 60-day backward from today (prefers recent correlation)
- Today's bar (EOD price not yet fixed) is excluded from the window

**Return calculation**:
```
return[i] = (close[i] - close[i-1]) / close[i-1]
```

**Regression output**:
- β (sensitivity to target)
- r (Pearson correlation coefficient, −1 to +1)
- r_squared (coefficient of determination, 0 to 1)

Stored in hedge_proxies table. Frontend displays β in the PROXY chip; correlation
and r_squared are shown in the /admin hedge panel for operator review.

---

## 3. Operator-Triggered Compute

**Endpoint**: `POST /api/admin/hedge-proxies/{id}/compute`

Synchronous HTTP endpoint. Operator clicks "Compute" button next to a hedge row.

**Flow**:
1. Load proxy and target symbols
2. Fetch 60 days of daily OHLCV for both (via broker historical API)
3. Compute returns vector for each
4. Run linear regression: β = Cov(p,t) / Var(t)
5. Calculate r and r_squared
6. Upsert hedge_proxies row with new β, correlation, regression_at timestamp
7. Return the updated row to the UI

**Guard: ≥15 bars**
If fewer than 15 overlapping bars, the regression is skipped and an error is returned:
"Insufficient history (only N bars, ≥15 required)". No update is persisted.

**Guard: β sanity**
β > 2.0 flags a warning in the UI (very high sensitivity; operator should review).
Negative β is allowed (short hedge).

**Guard: target_spot = 0**
Divide-by-zero guard when computing effective_qty. Returns error if target spot is
unavailable at compute-time (e.g. MCX closed, symbol delisted).

---

## 4. Auto-Recompute Schedule

**Schedule**: Daily 02:30 IST background task (`_task_hedge_proxies_regression`).

**Trigger**: For each active hedge_proxy row, check:
`regression_at < (today - regression_max_age_days)`

where `regression_max_age_days` is a tunable in `/admin/settings` (default 30 days).

**Flow**:
1. Iterate all is_active=true rows
2. For each row with stale regression_at, call internal `_compute_regression(proxy_id)`
3. Log the count of rows recomputed (e.g. "Recomputed 3 of 5 hedges")
4. Swallow exceptions per row (one bad symbol doesn't stop the sweep)

**Log tag**: `[HEDGE-PROXIES-AUTO-RECOMPUTE]` — appears in daily logs so operator
can grep for regression activity.

---

## 5. UI Display — PROXY Chip

When a position or holding row has an active hedge_proxy, the row displays a **PROXY chip**
in the Symbol column (or alongside the symbol, depending on layout).

**Chip format**:
- Label: magenta/purple color (action palette `--c-action` or similar)
- Text: `"PROXY"` or `"PROXY: {target_root}"`
- Alongside: β value (2-decimal precision), e.g. "β 0.87"
- Tooltip: full correlation r, r_squared, regression age

**Example**: GOLDBEES row shows chip "PROXY: GOLD β 0.92" (magenta, hoverable).

**Computation display**:
When the row computes effective hedge quantity, the formula is:
`target_lots = (β × proxy_market_value) / (target_spot × target_lot_size)`

The UI displays this both numerically ("Short 5 GOLD 100-lot") and in the hedge panel
as a "Suggested" hedge quantity (editable; operator can override).

**Visibility gates**:
- PROXY chip appears only for active (is_active=true) hedges
- Correlation warning (r < 0.7) shown as amber icon + tooltip
- "Correlation stale (30+ days)" warning shown as orange badge when regression_at is old

---

## 6. Target Symbol Hints and Resolution

Targets can be equities (NSE), indices (NSE special), or MCX commodities.
Uniform symbol lookup requires exchange and name hints.

**`_TARGET_HINTS` map** (hardcoded):

| Target | Exchange | Name Hint | Why |
|---|---|---|---|
| NIFTY | NSE | NIFTY 50 | Index name, not raw instrument |
| BANKNIFTY | NSE | NIFTY BANK | Index name |
| FINNIFTY | NSE | NIFTY FIN SERVICE | Index name |
| GOLD | MCX | GOLD | Commodity, resolves to front-month FUT |
| GOLDM | MCX | GOLDM | Mini commodity |
| SILVER | MCX | SILVER | Commodity |
| SILVERM | MCX | SILVERM | Mini commodity |

**Fallback**: Unknown targets default to NSE-equity lookup (Stage 3 stock proxies,
future extensibility). CRUDEOIL, COPPER, etc. can be added to _TARGET_HINTS as needed.

**Resolution**: `_resolve_token(broker, target_symbol, exchange_hint)` walks
`broker.instruments(exchange=exchange_hint)` and finds the first matching row
(by name or alternate name variants). Returns instrument_token or None.

---

## 7. MCX Commodity Rollover Constraints

MCX futures roll monthly. A fresh contract (day 1 post-rollover) has only 30–60 days
of bars available before the next rollover. Beta regression typically needs 60+ days
for stability.

**Design choice**: Roll-aware shorter window (option B from design doc).

**`_MCX_COMMODITY_ROOTS` set**: Lists all MCX commodities (GOLD, GOLDM, SILVER, CRUDEOIL, etc.).

**Window tightening**: For MCX targets, the auto-recompute task uses a shorter window
(default 30 days instead of 60) so regression completes even on fresh contracts. The
tradeoff: β may be less stable early in a contract's life.

**Operator note**: Correlation warning (r < 0.7) is more likely on fresh contracts.
Operator should review and possibly skip the first month of regression or extend the
compute window manually.

---

## 8. Edge Cases and Recovery

**No target data**: Target symbol has no OHLCV bars (delisted, not yet listed).
Compute returns error "Target symbol {root} has no history". No update persists.

**Proxy with only LTP**: Proxy has no OHLCV bars yet (common on first hold).
Compute falls back to current LTP for spot and uses point-to-point returns if
available. On error, falls back to "Fetch failed" status. No regression update.

**Both symbols missing bars in the same period**: Fewer than 15 overlapping bars.
Regression aborts with "Insufficient overlapping history" error.

**β very small or negative**: Allowed. Negative β means proxy and target move
inversely (rare but valid). UI shows the value as-is with no warning.

**correlation = 0** (zero covariance): Proxy and target are independent.
β is valid but r = 0 triggers correlation warning ("Poor correlation, r=0.00").

**Auto-recompute stale**: regression_at > 30 days (default). Background task
skips it (doesn't auto-recompute; operator must click Compute button or adjust
the age threshold in settings). This prevents noisy recomputes of pairs with
intentionally stale regressions.

---

## 9. Test Coverage Map

### Backend

- `test_hedge_proxy_regression_math.py` — β calculation from known return vectors (60d), r_squared
- `test_hedge_proxy_15bar_guard.py` — reject < 15 overlapping bars, return error
- `test_hedge_proxy_divide_by_zero.py` — guard target_spot = 0, no divide error
- `test_hedge_proxy_auto_recompute_schedule.py` — age-check gate, skip fresh pairs (regression_at < 30d)
- `test_hedge_proxy_mcx_rollover_window.py` — MCX commodities use 30d window not 60d
- `test_hedge_proxy_effective_qty.py` — formula (β × market_value / target_spot / target_lot_size)
- `test_hedge_proxy_target_resolution.py` — NSE index vs MCX commodity exchange hints

### Frontend

- `hedge_chip_display.spec.js` — PROXY chip renders with target, β, correlation icon
- `hedge_correlation_warning.spec.js` — r < 0.7 shows amber icon + tooltip
- `hedge_compute_button.spec.js` — POST /api/admin/hedge-proxies/{id}/compute, result persists

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec; target hints, MCX rollover constraints, auto-recompute schedule documented |
