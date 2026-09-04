# OPTIMIZE_SPEC — Portfolio Optimization Overlay

**Status:** Design spec (not yet implemented)  
**Surface:** Derivatives page + MCP Lab tool  
**Authored:** 2026-09-04, from live CrudeOil Sep analysis (14-leg, F≈₹8,570)

---

## 1. Problem Statement

The derivatives page shows current positions with Greeks but provides no guidance on
_which trades to make_ to improve the portfolio's risk/return/margin profile. Operators
must manually assess 10–20 leg books, compute net delta, identify naked ratios, and
estimate margin impact — all without tooling.

**Goal:** A single `⚡ Optimize` button that:
1. Computes per-leg Greeks from live prices (implied F from put-call parity)
2. Identifies inefficient legs (high margin, low remaining theta, naked ratio risk)
3. Suggests a minimal set of trades to improve the selected objective
4. Renders the post-optimization scenario as a dashed overlay on the existing payoff chart
5. Lists suggested trades in the Legs grid as a distinct `.cand-opt` row type
6. Exposes the same analysis via MCP for Lab-page natural-language queries

---

## 2. Optimization Objectives

Three objectives selectable at call time (default: `margin`):

| Objective | What it does |
|---|---|
| `margin` | Score each leg by margin/remaining-TV ratio. Flag legs where SPAN margin > ₹2L and remaining time value < 50% of peak. Suggest roll/close to free margin while preserving P&L. |
| `delta_neutral` | Compute net portfolio Δ. Return minimum-cost hedge to bring |Δ| < 50 — either futures short/long or buying puts/calls at nearest liquid strike. |
| `max_theta` | Rank legs by Θ/margin ratio. Suggest replacing low-ratio legs (deep ITM options, wide spreads at max loss) with higher-theta, similar-risk positions. |

---

## 3. Worked Example — CrudeOil Sep 2026 (2026-09-04)

**Inputs (14 legs, implied F≈₹8,570 from put-call parity):**

| Leg | Lots | Avg | LTP | Total P&L | Sub-book |
|---|---|---|---|---|---|
| 7700CE | −4 | 413 | 930 | −2,06,570 | Bear call spread A |
| 7800CE | +4 | 422 | 819 | +1,58,920 | Bear call spread A |
| 7900PE | −1 | 214 | 70 | +27,370 | Short put wing |
| 8000CE | −4 | 283 | 651 | −1,47,240 | Bear call spread B |
| 8000PE | −4 | 406 | 84 | +1,28,700 | Short put wing |
| 8100CE | +4 | 535 | 578 | +17,100 | Bear call spread B |
| 8200CE | −7 | 284 | 499 | −1,50,650 | **Ratio spread (3 naked lots)** |
| 8200PE | −6 | 328 | 126 | +1,21,170 | Short put wing |
| 8300PE | −1 | 201 | 160 | +4,100 | Short put wing |
| 8400CE | +4 | 282 | 367 | +33,940 | Ratio spread hedge |
| 8500CE | −1 | 339 | 313 | +2,550 | Short call wing |
| 8500PE | −1 | 353 | 240 | +11,300 | Short put wing |
| 8600PE | −3 | 343 | 294 | +14,670 | Short put wing (near-ATM) |
| 8700PE | −1 | 416 | 350 | +6,580 | Short put wing (ITM) |

**Implied F from put-call parity:**
- 8200CE(499.4) − 8200PE(126.1) + 8200 = ₹8,573
- 8000CE(651.9) − 8000PE(84.8) + 8000 = ₹8,567
- **Consensus: F ≈ ₹8,570** (sparkline showing 8,149 was stale)

**Net Greeks (IV≈38%, T≈16 days, F=8570):**

| Leg | Δ/unit | Net Δ |
|---|---|---|
| 7700CE (−4 lots) | 0.92 | −368 |
| 7800CE (+4 lots) | 0.89 | +356 |
| 8000CE (−4 lots) | 0.82 | −328 |
| 8100CE (+4 lots) | 0.77 | +308 |
| 8200CE (−7 lots) | 0.72 | −504 |
| 8400CE (+4 lots) | 0.61 | +244 |
| 8500CE (−1 lot) | 0.56 | −56 |
| 7900→8700 PE shorts | varies | +538 |
| **Net portfolio Δ** | | **≈ +190** |

**Recommended trades (margin objective):**

| Priority | Trade | Cost | Δ impact | Margin freed | Reason |
|---|---|---|---|---|---|
| 1 — URGENT | BUY 3× 8200CE @ ₹499 | −₹1,49,820 | +216 | ~₹4–5L | Covers 3 naked lots above 8400. Each ₹1 rally = ₹300 loss currently. |
| 2 | ROLL 8600PE×3 → 8200PE | −₹50,430 | −66 | ~₹3–4L | 8600PE near-ATM (30pts OTM), consumes max SPAN. 8200PE is far OTM. |
| 3 | CLOSE 7700/7800 spread net | −₹44,200 | −12 | ~₹5–6L | Both deep ITM, near max loss. Pay ~10pts over intrinsic to free 4-lot ITM short margin. |
| 4 (optional) | ROLL 8700PE → 8300PE | −₹18,980 | −14 | ~₹1L | Frees some ITM put margin. |

**Post-trade net Δ:** +190 + 216 − 66 − 12 ≈ +328 (offset with 2× short futures = −200 Δ → net ≈ +128).

---

## 4. Backend API Contract

### `POST /api/portfolio/optimize`

**Request:**
```json
{
  "underlying": "CRUDEOIL",
  "expiry": "2026-09-19",
  "objective": "margin",
  "positions": null
}
```

`positions` is optional. When null, fetches from `daily_book` (kind='positions', today,
symbol ILIKE underlying%).

**Response:**
```json
{
  "trades": [
    {
      "action": "buy | sell | close | roll",
      "symbol": "CRUDEOIL26SEP8200CE",
      "lots": 3,
      "cost_estimate": 149820,
      "delta_impact": 216.0,
      "margin_impact": -450000,
      "reason": "Covers 3 naked lots above 8400 — eliminates unbounded upside risk"
    }
  ],
  "optimized_legs": [
    {"symbol": "...", "qty": -400, "avg_cost": 284.19, "ltp": 499.4, "iv": 0.38, "expiry": "2026-09-19"}
  ],
  "summary": {
    "delta_before": 190.0,
    "delta_after": 328.0,
    "margin_freed_estimate": 1500000,
    "total_cost": 263250,
    "objective": "margin"
  }
}
```

`optimized_legs` is the full modified leg list suitable for passing directly to
`POST /options/strategy-analytics` to generate the optimized payoff curve.

**Implementation file:** `backend/api/routes/portfolio_optimize.py`

---

## 5. Greek Computation Module

**New file:** `backend/api/algo/options_greeks.py`

### `black_scholes_greeks(F, K, T, sigma, opt_type) → dict`

Standard Black-Scholes for futures options (no carry/dividend):
- `d1 = (ln(F/K) + 0.5σ²T) / (σ√T)`
- `d2 = d1 − σ√T`
- Call delta = N(d1); Put delta = N(d1) − 1
- Gamma = N'(d1) / (F·σ·√T)
- Theta = (−F·N'(d1)·σ / (2·√T)) / 365
- Vega = F·N'(d1)·√T / 100
- Returns `{price, delta, gamma, theta, vega}`

### `implied_futures_from_parity(chain) → float`

Input: list of `{strike, call_ltp, put_ltp}` for available strikes.  
Method: F = K + (C − P) per strike; average across 3–5 nearest-ATM pairs.  
Used when the futures LTP is unavailable or stale (sparkline vs tick timing gap).

### `implied_vol_from_straddle(straddle_price, F, T) → float`

Approximation: σ ≈ straddle_price / (0.8 · F · √T).  
Uses the nearest ATM straddle (smallest |strike − F|).  
Fallback if Newton-Raphson per-option IV is too slow (use for portfolio-level IV).

### `per_option_iv(market_price, F, K, T, opt_type) → float`

Newton-Raphson root-find on `black_scholes_price(σ) = market_price`.  
Max 20 iterations, tolerance 0.001. Falls back to straddle IV if no convergence.  
Run for each leg when time allows; use portfolio IV for fast path.

---

## 6. MCP Tools

**File:** `backend/mcp/kite_server.py` (additions)

### `get_portfolio_greeks(underlying, expiry)`

Fetches live positions from `daily_book`, reads LTPs from mmap tick buffer, derives
implied F via put-call parity, computes BS Greeks for each leg, returns:
```json
{
  "implied_futures": 8570.0,
  "implied_vol": 0.38,
  "legs": [
    {"symbol": "CRUDEOIL26SEP8200CE", "lots": -7, "delta": -504, "gamma": ..., "theta": ..., "vega": ...}
  ],
  "portfolio": {"delta": 190, "gamma": ..., "theta": ..., "vega": ...}
}
```

### `optimize_portfolio(underlying, expiry, objective="margin")`

Calls the same logic as `POST /api/portfolio/optimize`. Returns the trade list + summary
with one-line `reason` per trade for natural-language rendering in Lab chat.

**Lab usage example:**
```
User: "Optimize my CRUDEOIL Sep positions for margin"
MCP → optimize_portfolio("CRUDEOIL", "2026-09-19", "margin")
Response: "3 trades suggested. Total cost ₹2.63L, margin freed ~₹12–15L.
  1. BUY 3× 8200CE — covers 3 naked lots above 8400 (unbounded risk)
  2. ROLL 8600PE×3 → 8200PE — 8600PE near-ATM, consumes max SPAN
  3. CLOSE 7700/7800 spread — near max loss, freeing ₹5-6L for ₹44K"
```

---

## 7. Derivatives Page UI Changes

### 7.1 `⚡ Optimize` button

**Location:** Payoff card header, right of EV/Greeks chips (~line 4337 in +page.svelte)  
**Style:** Compact amber-outline button matching card header density  
**States:** Idle / Loading (spinner) / Active (OPT overlay visible)  
**On click:**
1. `POST /api/portfolio/optimize` with `{underlying: selectedUnderlying, expiry: strategy.expiry, objective: 'margin'}`
2. Store result in `_optimizationResult`
3. Call `fetchStrategyAnalytics(result.optimized_legs)` → store in `_optimizedStrategy`
4. Set `showOptInPayoff = true` → payoff overlay appears

```css
.opt-btn {
  font-size: 0.7rem;
  padding: 0.2rem 0.5rem;
  border: 1px solid theme(colors.amber.500);
  color: theme(colors.amber.400);
  border-radius: 0.25rem;
  background: transparent;
  cursor: pointer;
}
.opt-btn:hover { background: theme(colors.amber.500 / 15%); }
.opt-btn.active { background: theme(colors.amber.500 / 20%); }
```

### 7.2 `OptionsPayoff.svelte` — new props

```javascript
optimizedPayoff: Array<{spot, today_value, expiry_value}> | null = null,
showOptInPayoff: boolean = false,
onToggleOpt: (() => void) | null = null,
```

**Rendering when `optimizedPayoff` set and `showOptInPayoff=true`:**
- `optimized_today` curve: dashed lime `#a3e635`, 1.5px, opacity 0.85
- `optimized_expiry` curve: dotted teal `#2dd4bf`, 1.25px, opacity 0.75
- Optimized breakeven verticals: lime dashed `#a3e635`
- **OPT** toggle button added to legend (after DRAFT), same style as HOLD/DRAFT
- Stat overlay additions:
  - `OPT Δ` — shows `summary.delta_after` from optimization result
  - `OPT EXP` — shows optimized expiry P&L at current spot

**Color rationale:** Lime/teal contrasts clearly with amber (today) and sky (expiry).
Not confused with intermediate time-slice curves (which use HSL slerp amber→sky).

### 7.3 Legs grid — `.cand-opt` trade rows

When `_optimizationResult.trades` is populated, render optimization trades AFTER the
existing leg rows (same 18-column subgrid for alignment):

```
┌─ existing legs ────────────────────────────────────────────────────┐
│  ☑  ●  7700CE        930   -4  ...  Δ -368                         │
│  ☑  ●  8200CE        499   -7  ...  Δ -504                         │
│  [TOTAL current]          ...  Δ  +190                             │
├─ optimization trades ──────────────────────────────────────────────┤
│  ▌ BUY  8200CE  ×3   499   +3  cost -₹1.5L   Δ +216   [reason]   │
│  ▌ ROLL 8600PE×3→8200PE   cost -₹0.5L   Δ  -66   [reason]        │
│  ▌ CLOSE 7700/7800  ×4    cost -₹0.44L  Δ  -12   [reason]        │
│  [TOTAL optimized]        Δ  +328   cost -₹2.44L                  │
└────────────────────────────────────────────────────────────────────┘
```

**Visual treatment:**
```css
.cand-opt {
  border-left: 3px solid theme(colors.lime.500);
  background: theme(colors.lime.500 / 4%);
}
.cand-row-total-opt {
  background: theme(colors.slate.700 / 60%);
  border-top: 1px solid theme(colors.lime.500 / 40%);
  font-weight: 600;
}
```

**Action badge** (in St column, replacing GTT/Paired/Orphan indicator):
- `BUY` → green chip
- `SELL` / `CLOSE` → red chip
- `ROLL` → amber chip

**Column mapping:**
- Symbol: target symbol (e.g., "8200CE [BUY]")
- LTP: current LTP of the option
- Lots: proposed quantity (signed: + = buy, − = sell)
- P&L column: cost_estimate (green = credit received, red = debit paid)
- Δ column: delta_impact
- Θ column: theta improvement estimate
- Reason: rendered as tooltip on row hover (not a column — space constraint)

### 7.4 Exp-Close grid — optimization band badges

When `_optimizationResult` is set, each **band header pill** gains a secondary indicator
showing how many legs change band after optimization:

```
▌ ITM ON EXPIRY  [4 legs → 1 leg after OPT]
▌ NETTED         [unchanged]
▌ OUT OF THE MONEY  [+2 legs after OPT]
```

**Derivation:** For each trade in `_optimizationResult.trades`:
- `close` action → leg leaves its current band
- `roll` action → leg moves from one band to another (new strike)
- Band re-classification uses intrinsic value of new strike at current spot

Implementation: compute `optBandDeltas` in the `displayedCandidates` derived block;
pass into the band-header injection logic in CandidateLegRow or inline in +page.svelte.

---

## 8. Data Flow

```
[daily_book positions]  ←── DB query (kind='positions', today)
         ↓
[LTPs from mmap]        ←── /dev/shm/ramboq_ticks (existing)
         ↓
[implied F]             ←── put-call parity (options_greeks.py)
[implied IV]            ←── ATM straddle approximation
         ↓
[BS Greeks per leg]     ←── black_scholes_greeks() × 14 legs
         ↓
[optimization]          ←── objective-specific scoring + rule-based trade generation
         ↓
[trade list]            ─── returned in API response
[optimized_legs]        ─── returned in API response
         ↓
[fetchStrategyAnalytics(optimized_legs)]   ←── existing API client (api.js:948)
         ↓
[_optimizedStrategy.payoff]  → OptionsPayoff optimizedPayoff prop
[_optimizationResult.trades] → .cand-opt rows in Legs grid
```

---

## 9. State additions in `+page.svelte`

```javascript
// Optimization state (add near existing let legs, let legsTab)
let _optimizationResult = $state(null);   // POST /api/portfolio/optimize response
let _optimizedStrategy = $state(null);    // fetchStrategyAnalytics for optimized legs
let _optimizing = $state(false);
let showOptInPayoff = $state(false);

// Handler
async function _runOptimize() {
  _optimizing = true;
  try {
    const res = await api.post('/api/portfolio/optimize', {
      underlying: selectedUnderlying,
      expiry: strategy?.expiry,
      objective: 'margin'
    });
    _optimizationResult = res;
    _optimizedStrategy = await fetchStrategyAnalytics(res.optimized_legs, { spot: liveSpot ?? null });
    showOptInPayoff = true;
  } finally {
    _optimizing = false;
  }
}
```

---

## 10. Implementation Files

| File | Change type | Description |
|---|---|---|
| `backend/api/algo/options_greeks.py` | New | BS pricing, implied F from parity, IV from straddle |
| `backend/api/routes/portfolio_optimize.py` | New | `POST /api/portfolio/optimize` endpoint |
| `backend/mcp/kite_server.py` | Additive | `get_portfolio_greeks` + `optimize_portfolio` MCP tools |
| `frontend/src/lib/OptionsPayoff.svelte` | Additive | `optimizedPayoff`, `showOptInPayoff`, `onToggleOpt` props + OPT curve rendering |
| `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` | Additive | Optimize button, `_runOptimize`, `_optimizationResult` state, `.cand-opt` rows, OPT TOTAL row |
| `backend/tests/broker/test_options_greeks.py` | New | Known BS values, parity, straddle IV |
| `backend/tests/test_portfolio_optimize.py` | New | Margin objective on synthetic CrudeOil positions |

---

## 11. Latency Budget

| Step | Time | Notes |
|---|---|---|
| DB read (daily_book) | ~10ms | Simple SELECT |
| mmap LTP reads | ~1ms | Already subscribed instruments |
| Implied F (parity) | ~1ms | Arithmetic |
| BS Greeks ×14 legs | ~5ms | scipy.stats.norm per leg |
| Margin objective scoring | ~5ms | Rule-based, no LP needed for first version |
| `fetchStrategyAnalytics` (optimized) | ~200ms | Existing endpoint |
| **Total** | **~220ms** | Sub-quarter-second end-to-end |

If per-option Newton-Raphson IV is added: +50ms. Still well within interactive budget.

---

## 12. Future Extensions

- **Objective dropdown** on the button: `margin / Δ neutral / max Θ`
- **Scenario slider**: drag the spot price to see how optimized vs current P&L diverge
- **Time-decay animation**: intermediate curves (time_slices already supported in API, currently disabled) showing the optimized book decaying over DTE
- **Lot-size constraints**: cap suggested lots per trade (e.g., max 4 lots per trade)
- **Multi-expiry optimization**: when positions span two expiries, suggest calendar rolls
- **SPAN margin lookup**: call Kite `/margins/orders` for accurate SPAN instead of 4–5% notional estimate
- **Save scenario**: persist an optimization result as a named draft set (reloadable across sessions)
- **MCP natural-language feedback loop**: operator says "make it cheaper" → MCP re-runs with tighter cost constraint
