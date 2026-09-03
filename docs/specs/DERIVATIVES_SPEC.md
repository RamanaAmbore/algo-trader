# Derivatives Analytics Specification

Single source of truth for options and futures analytics on the `/admin/derivatives`
dashboard. Covers symbol parsing, Greeks calculation, payoff curves, and multi-leg
strategy aggregation.

**Version**: 1.1 — 2026-08-13  
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
7. [Underlying Picker](#7-underlying-picker)
8. [Data Loading](#8-data-loading)
9. [Edge Cases](#9-edge-cases)
10. [Test Coverage Map](#10-test-coverage-map)

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

**Risk-free rate**: `DEFAULT_RISK_FREE = 0.07` (7% p.a., calibrated to Indian
RBI repo). Canonical source: `backend/api/algo/derivatives.py:DEFAULT_RISK_FREE`.
All other files (`positions.py`, `expiry.py`) import from this constant — do not
hardcode 0.07 elsewhere.
**Default IV**: `DEFAULT_IV = 0.15` (15%, used when market data unavailable).
Canonical source: `backend/api/algo/derivatives.py:41`.

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

**Underlying switch stale-while-revalidate**: During an underlying switch, the
previous payoff curve remains visible until the new one finishes loading
(stale-while-revalidate — avoids blank stub). The new curve replaces it
atomically on arrival.

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

## 7. Underlying Picker

**Six-tier candidate source ordering** (`underlyingOptionsForPicker` derived):

The underlying picker populates from multiple data sources to surface both
operator holdings and curated watchlist symbols in a single dropdown, ranked
by relevance:

1. **Options positions** (cyan 'options' hint): Roots where the operator holds
   at least one CE or PE contract. Highest priority — active derivatives holdings
   sort first alphabetically within the tier.

2. **Futures-only positions** (default color, 'futures' hint): Roots where the
   operator holds futures but no options. Sorted alphabetically.

3. **Holdings** ('holdings' hint): Roots from cash-equity holdings (`holdings`
   store). Extracted from the bare symbol of each holding. Same account filter
   as tiers 1-2 so account selection narrows this tier too.

4. **Pinned watchlist** (hint: 'pinned'): F&O-eligible underlying roots from
   pinned watchlists (flagged as `is_pinned` or `is_global` in the DB).
   Populated by `loadDefaultWatchlist()` → `_extractFOUnderlyingRoots(pinnedSyms)`.
   Appears before regular watchlists so operator-curated symbols surface above
   ordinary lists.

5. **Regular watchlist** (hint: 'watchlist'): F&O-eligible underlying roots from
   non-pinned operator watchlists. Populated from `regularSyms` in
   `loadDefaultWatchlist()`. Appears after pinned watchlist and before popular
   fallback.

6. **Popular underlyings** (hint: 'popular'): Static hardcoded whitelist
   (`POPULAR_UNDERLYINGS`) of liquid F&O instruments. Always emitted —
   the operator sees NIFTY, BANKNIFTY, RELIANCE etc. available even when they
   have no positions or holdings. No gate on `instrumentsReady` — the list is
   immediately available on cold start.

**Deduplication**: First occurrence wins. If a root appears in multiple tiers
(e.g., held in positions AND on a pinned watchlist), only the highest-priority
tier's entry is shown.

**Account filter**: Tiers 1-5 respect `selectedAccounts` multi-select (empty =
show all). Tier 6 (popular) is never filtered by account.

**Auto-select**: When the page lands without a cached `selectedUnderlying` or
when positions load after a cold start, the first entry in the picker list
(options > futures > holdings > pinned > regular > popular) is auto-selected.
Cold-start with no book lands on `POPULAR_UNDERLYINGS[0]`.

**Watchlist extraction** (`_extractFOUnderlyingRoots`): Bare equity/index
symbols (NIFTY, RELIANCE) are checked directly via `getOptionUnderlyingLot`.
Derivative symbols (CRUDEOIL26JUNFUT) are decomposed via `decomposeSymbol`
and their root is checked. Requires instruments cache to be warm — call only
after `loadInstruments()` resolves. Returns an alphabetically-sorted array of
eligible roots.

---

## 8. Data Loading

**Positions source** (`loadPositions`):
- Primary: `positionsStore.load({ fresh })` (live broker F&O positions)
- **Fallback**: `pulsePositionsStore.value` (MarketPulse positions data)
  When `positionsStore.value` is empty or unavailable, the page uses positions
  cached in `pulsePositionsStore` so the underlying picker and legs panel
  populate from cached data while a broker retry is pending. Stale-while-error:
  always process the available data (broker snapshot or cached) rather than
  going blank.
- Equity intraday positions are excluded from the F&O analysis but captured
  in `_excludedByAccount` for TOTAL row reconciliation with NavStrip.

**Sim positions** (when simulator is active):
- Fetched via `fetchSimStatus()` alongside broker positions
- Inline LTP included so strategy endpoint can compute analytics without
  an extra broker round-trip

**Holdings** (equity positions):
- Skipped in simulator mode (sim doesn't model equity book)
- Loaded via `holdingsStore.load()` when not simulating
- Derivative holdings are filtered out and excluded; only EQ rows appear
  in the holdings layer

**Load timing**: Positions load on mount and refresh via `visibleInterval` at
shared store cadence (typically 30s). Holdings load with positions. Strategy
analytics auto-refresh when the leg set changes (driven by candidate selection).

---

## 9. Edge Cases

### Spot parameter API flexibility
`annotateOptionCandidates` in `frontend/src/lib/data/derivativesMath.js` accepts `spot` as either:
- A number: single spot value (backward-compatible)
- A function `(underlying: string) => number`: per-underlying resolver (new v2026-07)

Internally: `const resolveSpot = typeof spot === 'function' ? spot : () => spot;` then
`resolveSpot(underlying)` per row. Enables per-underlying spot resolution in full-book analysis
(Exp Close tab) where positions span NIFTY, BANKNIFTY, CRUDEOIL etc. simultaneously.

### Row separation — border-bottom convention

Candidate grid (`.cand-grid`) uses `border-bottom: 1px solid rgba(126,151,184,0.10)` on every `.cand-row`
for visual separation, **not** `row-gap` (which was causing dark horizontal stripes as the parent background
bled through the gap). This matches the existing `.byund-row` pattern in the Snapshot grid.

**Why:** `row-gap: 0.2rem` created transparent space where the parent grid background (dark navy) showed through,
producing unintended visual gaps. Switching to a `border-bottom` on the row element itself uses the row's own
background, not the parent's. Result: clean row-level separation without visual artifacts.

### Exp Close full-book spot resolution
Expiry-close analysis (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte::expiryCloseAnalysis`)
uses a `spotResolver` closure that resolves spot per-underlying:
1. SSE tick via `getSnapshot(key)?.ltp`
2. Batch quote cache `_underlyingQuotes[key]?.ltp`
3. Fallback `0`

**Previous behavior**: Single underlying's spot via `_resolveExpirySpot(selectedUnderlying, ...)`.
Early gate `if (!spot || !cps.length) return empty` blocked analysis when spot=0 or no candidates.

**Current behavior**: Spot=0 no longer blocks analysis (removed early gate). Each row's underlying
resolves independently, supporting mixed-underlying baskets. Full-book expiry close works across
all positions regardless of selectedUnderlying selection.

### TOTAL row decoration — CSS convention (unified amber stratum)

**Unified stratum rule** (covers BOTH Legs + Snapshot TOTAL rows):
```css
.cand-row.cand-row-total,
.byund-row-total > span {
  background: linear-gradient(rgba(251,191,36,0.22), rgba(251,191,36,0.22)), #1d2a44 !important;
  border-top: 2px solid rgba(251,191,36,0.70);
  border-bottom: 1px solid rgba(251,191,36,0.40);
  color: var(--c-action);
  font-weight: 700;
}
```

**Two-layer architecture** — TOTAL rows use different mechanisms to achieve the same visual:

- **`cand-row-total` (Legs grid)**: Uses `display:grid; grid-template-columns:subgrid; grid-column:1/-1`
  with `column-gap: 0.6rem`. Amber rule applied to the **container** (not cells) so it covers gap areas.
  Child `> span` elements contain only: padding, font-size, font-family, font-variant-numeric, overflow,
  text-align for `.num` (no color / background / border).

- **`byund-row-total` (Snapshot grid)**: Uses `display:contents` with zero column-gap. Amber rule applied
  to per-span children directly (each gets its own amber background, border-top/bottom, color, font-weight).

**Why different mechanisms?** — `byund-row-total` relies on `display:contents` to eliminate its DOM layer
entirely, leaving bare `span` children to inherit the parent grid. With zero column-gap, amber-on-span works.
`cand-row-total` uses subgrid for alignment, which creates a separate grid container; the column-gap means
amber must be on the container to extend across the gap areas. **Result:** Single unified CSS rule, two different
implementation paths, identical visual output.

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

## 10. Test Coverage Map

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
| 2026-08-13 | v1.1 add Underlying Picker (6 tiers: options/futures/holdings/pinned/watchlist/popular) + Data Loading (positions fallback to pulsePositionsStore) |
| 2026-07-11 | v1.0 initial spec from codebase audit |
