# Derivatives Analytics Specification

Single source of truth for options and futures analytics on the `/admin/derivatives`
dashboard. Covers symbol parsing, Greeks calculation, payoff curves, and multi-leg
strategy aggregation.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/routes/options.py` · `backend/api/routes/options_helpers.py` · 
`backend/api/algo/derivatives.py` · `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` · 
`frontend/src/lib/OptionsPayoff.svelte` · `frontend/src/lib/LegLabel.svelte`

---

## Contents

1. [Symbol Parsing](#1-symbol-parsing)
2. [Re-Pricing and Greeks](#2-re-pricing-and-greeks)
3. [Endpoints](#3-endpoints)
4. [Payoff Curves](#4-payoff-curves)
5. [Multi-Leg Strategy](#5-multi-leg-strategy)
6. [LTP Resolution Chain](#6-ltp-resolution-chain)
7. [Edge Cases](#7-edge-cases)
8. [Test Coverage Map](#8-test-coverage-map)

---

## 1. Symbol Parsing

**Canonical parser**: `backend/api/algo/derivatives.py:parse_tradingsymbol()`.
Parses Kite-format symbols into structured metadata used by all downstream logic.

```python
{
  "kind": "CE|PE|FUT|EQ|MF",        # option type or equity/futures
  "underlying": "RELIANCE|NIFTY",   # without expiry/strike
  "strike": 2600.0,                 # None for non-options
  "opt_type": "CE",                 # call/put; None for futures/equity
  "expiry": "2026-07-24",           # ISO date; None for non-options
}
```

**Examples**:
- `RELIANCE2542428000CE` → {kind: CE, underlying: RELIANCE, strike: 2600, opt_type: CE, expiry: 2026-07-24}
- `NIFTY25APRFUT` → {kind: FUT, underlying: NIFTY, strike: None, opt_type: None, expiry: 2026-04-25}
- `CRUDEOIL21JULCE` (MCX) → {kind: CE, underlying: CRUDEOIL, strike: None (parsed separately)}

**Expiry rollover rule**: Contracts on expiry day (`inst.x > today IST`) excluded
from symbol-resolution lists. `root_of()` shifts to next-month contract.

---

## 2. Re-Pricing and Greeks

**Quote hierarchy** (per position/hypothetical):
- Live broker LTP (if available and source is live broker)
- Simulator state (if mode = SIM)
- Historical close price
- Depth midpoint (bid + ask) / 2
- Average cost (fallback)
- Black-Scholes default (when no market data available)

**Futures re-pricing**: 1:1 spot relationship. Position LTP = current spot + basis.

**Options re-pricing**: Black-Scholes with live IV calibration. Given position LTP,
implied vol computed via BFGS/Newton root-finding against BS formula. IV then used
for Greeks + payoff curves at all points on the range.

**Greeks** (Black-Scholes):
- Delta: rate of change vs underlying spot (scaled by lot_size)
- Gamma: rate of change of delta
- Theta: daily time decay (negative for long options, positive for short)
- Vega: sensitivity to 1% IV move
- Rho: sensitivity to interest-rate shift (typically small for India)

**Risk-free rate**: `DEFAULT_RISK_FREE = 0.07` (7% p.a., calibrated to Indian RBI repo).
**Default IV**: `DEFAULT_IV = 0.30` (30%, used when market data unavailable).

**Caching** (Phase 2 + Phase 4 leg-curve):
- Strategy-analytics cache: 5s TTL, keyed on (sorted_legs_tuple, spot, mode). LRU 64.
- Leg-curve cache: 5min sliding TTL, keyed on legs+shape only. LRU 64. Stores
  spot-independent curves (expiry_value per x_ratio); spot-dependent work
  (Greeks, EV, POP) recomputed each request (~25ms).

---

## 3. Endpoints

### Admin (capability-gated, typically via `/admin/derivatives`)

| Endpoint | Method | Input | Returns |
|---|---|---|---|
| `/api/options/analytics` | GET | `mode={live\|sim\|hypothetical}&symbol=…&qty=…&avg=…&ltp=…` | AnalyticsResponse (Greeks + payoff) |
| `/api/options/strategy-analytics` | POST | `{mode, legs: [{symbol, qty, side}, ...]}` | StrategyResponse (aggregate Greeks + R:R + payoff) |
| `/api/options/historical` | GET | `symbol=…&days=30&interval=day&exchange=…` | HistoricalResponse (OHLCV bars + multi-broker fallback) |

**Modes**:
- `live` — read qty/avg/LTP from real broker position
- `sim` — read from SimDriver state
- `hypothetical` — operator-supplied qty/avg; LTP fetched from broker (pre-trade analysis)

### Response shapes

**AnalyticsResponse**:
```json
{
  "symbol": "RELIANCE2542428000CE",
  "kind": "CE",
  "underlying": "RELIANCE",
  "strike": 2600.0,
  "expiry": "2026-07-24",
  "days_to_expiry": 14,
  "quantity": 100,
  "ltp": 35.50,
  "iv": 0.25,
  "greeks": {
    "delta": 0.65,
    "gamma": 0.012,
    "theta": -0.05,
    "vega": 8.5,
    "rho": 0.02
  },
  "payoff": {
    "range_pct": [-50, -40, ..., 40, 50],
    "values": [0, 50, ..., 1250, 1500],
    "max_profit": 1500.0,
    "max_loss": -3550.0,
    "breakevens": [2635.5],
    "pop": 0.72
  }
}
```

**StrategyResponse** (multi-leg):
```json
{
  "legs": [...],
  "spot": 2850.0,
  "aggregate_greeks": {
    "delta": 0.45,
    "gamma": -0.008,
    "theta": 0.10,
    "vega": -5.2,
    "rho": 0.01
  },
  "payoff": { ... },
  "max_profit": 2000.0,
  "max_loss": -1000.0,
  "rr_ratio": 2.0,
  "ev": 250.0,
  "pop": 0.68
}
```

---

## 4. Payoff Curves

**Range determination**: Normalized to underlying spot via σ-driven span.
```
range = [S × (1 − 2.5σ), S × (1 + 2.5σ)]
clamped to [S × 0.02, S × 0.50]  (2%-50% of spot)
```

**Payoff computation** (single-leg):
- For each point x in range, P&L = intrinsic(x) − cost
- Intrinsic(x) = max(0, x − strike) for calls; max(0, strike − x) for puts
- Scaled by qty and lot_size

**Expected value** (strategy-level): Trapezoidal integration of payoff curve ×
risk-neutral lognormal PDF. Assumes current IV remains constant to expiry; point
estimate does not capture gamma expansion / theta bleed.

**POP (Probability of Profit)**: Cumulative probability (normal approximation) that
underlying closes ITM. For multi-leg, aggregates terminal payoff > 0.

**R:R ratio** (Risk : Reward):
```
R:R = max_profit / |max_loss|
```
Infinity when max_loss = 0 (credit-spread corner case). NaN when max_profit and
max_loss both zero (flat payoff, rare).

---

## 5. Multi-Leg Strategy

**Input shape** (`POST /api/options/strategy-analytics`):
```json
{
  "mode": "live|sim|hypothetical",
  "legs": [
    {"symbol": "RELIANCE2542428000CE", "qty": 100, "side": "long"},
    {"symbol": "RELIANCE2542428000PE", "qty": 100, "side": "short"},
    ...
  ]
}
```

**Aggregation logic**:
1. Parse each symbol → get underlying + expiry
2. Validate all legs same underlying + expiry (multi-expiry strategies not yet supported)
3. Resolve LTPs for each leg via the resolution chain
4. Compute individual payoff curves (spot-independent, cached)
5. Sum payoffs point-by-point
6. Aggregate Greeks via linear addition
7. Compute R:R, EV, POP on the aggregate curve

**TOTAL row** (`/admin/derivatives` Legs grid): F&O-only, shows aggregate Greeks +
expiry profit. Formula identical to NavStrip P slot 3 (expiry P&L at current spot).
Not included in positions grid; only in derivatives view.

**CE/PE text color**: Sensibull convention (CE blue, PE red). Holdings + positions
grids use this for options rows.

---

## 6. LTP Resolution Chain

**LTP priority** (for both single-leg and multi-leg):

```
1. Override LTP (if operator manually set via settings / UI)
2. Sim positions (if mode = SIM)
3. Live broker quote (if mode = live, symbol subscribed)
4. Prior-session close price (from historical DB or broker)
5. Depth midpoint (bid + ask) / 2 when depth available
6. Average cost (fallback)
7. Black-Scholes default (theoretical, no market data)
```

**Market-data broker**: `get_market_data_broker()` selects which Kite account's
quotes to use (operator pin > priority ASC > insertion order). Centralized in one
place so all options analytics honor the same resolution.

**Symbols with no LTP**: Contribute 0 to the aggregate Greeks (under-estimate safer
than refusing compute). Payoff curves use relative spot changes; missing LTPs only
affect Greeks scale + expected value calibration.

---

## 7. Edge Cases

### Far-OTM options (BS instability)
- Black-Scholes can oscillate when intrinsic ≈ 0 and theta → 0
- Mitigated by clamping IV to [0.05, 2.0] and limiting Newton iterations
- Far-OTM payoff curves still render correctly (intrinsic-only calculation)

### Missing expiry date (symbol parse fail)
- `parse_tradingsymbol()` returns None for expiry if malformed
- Analytics endpoint rejects the symbol with 400 (bad request)
- Frontend symbol typeahead prevents submission of invalid symbols

### Multi-leg with mixed expiries
- Currently NOT supported (validation checks all same expiry + raises 400)
- Future roadmap: support N-expiry baskets (compute payoff grid, not curve)

### No market data available (broker offline)
- All legs fall back to average cost via LTP chain
- Greeks computed against historical IV (not calibrated to live)
- Payoff curves still render but R:R / EV marked as "stale"

### Extremely wide payoff range
- Span > 50% of spot triggers clamp to [2%, 50%] range
- Operator can see the clamp in the chart UI (axis labels show the actual spot bounds)

---

## 8. Test Coverage Map

### Backend — covered

- `test_parse_tradingsymbol.py` — round-trip symbol → dict → symbol
- `test_black_scholes.py` — BS Greeks match known values (Bloomberg, CME calibration)
- `test_payoff_curve.py` — intrinsic + theta, single-leg + multi-leg
- `test_strategy_analytics.py` — aggregate Greeks via linear addition
- `test_rr_ratio.py` — R:R computation, edge cases (0 profit, 0 loss)

### Backend — gaps

- Multi-expiry basket analytics (currently blocked validation)
- IV calibration convergence vs market IV (implied vol finder accuracy)
- EV integration accuracy (trapezoidal vs numerical ODE solver)
- Historical OHLCV multi-broker fallback (Kite → Dhan order)

### Frontend — covered

- `derivatives_page.spec.js` — Legs grid renders + totals row matches backend
- `payoff_chart.spec.js` — D3 curve rendering, range clamp visualization

### Frontend — gaps

- Greeks directional color (delta < 0 red, > 0 green)
- IV manual override input (admin-only feature, not tested)
- Strategy-analytics cache hit rate dashboard (perf telemetry missing)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
