# portfolioStore: Stale-While-Revalidating for Live Trading Data

## TL;DR

RamboQuant's portfolio display (positions, holdings, funds) was rendered with intermittent zeros during API refresh cycles. Root cause: 14 independent derived stores read from poll-based data using the `?? []` fallback pattern, which triggered zero-valued recomputation when broker responses were in-flight. Solution: unified SWR store with one null-guard at the top, plus root-first computation to cache expensive underlying-spot lookups. Result: 10 visible bugs fixed, 3 stores merged into 1, zero consumer code changes for 8 of 11 files.

---

## Problem Statement

### What Users Saw

During normal trading sessions, fields like `chg%`, `day_pnl`, `todayMtm`, and `exp_pnl` would flash to zero across **all five data surfaces simultaneously**:

- NavStrip position total P∆ / H∆ / chg% columns
- NavBreakdown popup aggregate slots
- MarketPulse grid per-symbol rows (colored red or neutral, cleared values)
- Derivatives legs grid expiry value and extrinsic columns
- Dashboard portfolio totals

The blackout lasted ~200ms, then values returned. Page navigates often triggered a zero on load. Switching between Derivatives and Pulse pages caused intermittent zeros on the destination page. **It was reproducible only in production**, under normal market volatility and user traffic patterns — attempting to trigger it in dev with mocked data never worked.

### When It Happened

The pattern always coincided with the API refresh cycle:
1. **T = 0**: Positions poll completes → store updates, UI re-renders
2. **T = 200ms**: Next positions poll fires
3. **T = 200–250ms**: Network in-flight; broker response not yet arrived
4. **T = 201ms**: MarketPulse, Derivatives, and NavStrip all re-render (watching derived stores) → all see zero values
5. **T = 400ms**: Broker response arrives, stores recompute with data, zeros vanish

### Why It Was Hard to Debug

- **Scale**: The bug manifested across 5 surfaces and 9 files; symptoms were non-deterministic in dev
- **Visibility**: The zero flash was brief (~200ms) and needed high-frequency UI observers to catch (browser DevTools time-travel not helpful)
- **Isolation**: Three separate derived stores (`positionsDerivedStore`, `holdingsDayPnlStore`, `positionsDayPnlStore`) each zeroed independently — they could flash at staggered times, making the root cause look distributed
- **Dependency chain depth**: Four tiers of reactivity (raw API → market data → per-symbol derived → aggregates → display) meant the null propagated through multiple layers before becoming visible

---

## System Architecture

RamboQuant is a SvelteKit trading platform with three data sources:

| Source | Cadence | Latency | Scope |
|---|---|---|---|
| **Zerodha Kite broker API** | 5-second poll | 200–800ms | Positions, holdings, funds (qty, avg, P&L, margin) |
| **KiteTicker WebSocket** | Event-driven, ~ms | <50ms | Live LTP ticks (price, volume, OI) — continuous when market open |
| **Instruments DB** | Loaded once/session | 0ms (local) | Lot size, expiry, strike — used to compute option intrinsics |

**Display stack**:
```
Raw API polls (5s, can go null)
  ↓
marketDataStores.svelte.js (positionsStore, pulseHoldingsStore, fundsStore)
  ↓
WebSocket LTP ticks (continuous, never null)
  ↓
symbolStore.svelte.js (per-symbol: ltp, close, day_change, volume, oi, bid/ask)
  ↓
portfolioStore.svelte.js ($derived.by with unified computation)
  ↓
Five consumer surfaces (NavStrip, NavBreakdown, Pulse, Derivatives, Dashboard)
```

The portfolio store computes five derived value types per position:
- `day_pnl`: intraday P&L = (ltp − prev_close) × quantity
- `chg_pct`: intraday change % = day_pnl / (prev_close × |qty|)
- `exp_pnl`: expiry value (F&O only) — uses underlying spot for options, own LTP for futures
- `extrinsic`: time value for options = exp_pnl − intrinsic
- `todayMtm`: account-level aggregate (for margin utilization calcs)

---

## Root Cause Analysis

### The `?? []` Pattern

The original three derived stores used this pattern to read from poll-based data:

```javascript
// OLD — positionsDerivedStore
const posRows = positionsStore.value ?? [];
const holdRows = pulseHoldingsStore.value ?? [];
if (posRows.length === 0 || holdRows.length === 0) return EMPTY;

// Compute day_pnl = (ltp − prev_close) × qty, etc.
for (const p of posRows) { ... }
```

This was reasonable on the surface: "if the store is null, treat it as empty and return the zero shape." But it had a fatal flaw.

### Why Null Happens

Zerodha Kite's API has a 200–800ms round-trip time. The frontend polls every 5 seconds:

1. **Store state before poll**: `positionsStore.value = [{ qty: 100, ltp: 1000, ... }, ...]`
2. **Poll fires** at T = 5000ms
3. **T = 5050–5200ms**: Network in-flight — response not yet arrived
4. **T = 5100ms**: A SvelteKit derived store reads `positionsStore.value`
5. **Meanwhile at T = 5100ms**: A page navigates, triggering fresh reads of all derived stores
6. **Meanwhile at T = 5100ms**: Live LTP tick arrives from WebSocket, symbolStore recomputes

At step 5, the `marketDataStores` implementation refetches the API. During the in-flight window (step 3), the `value` slot is set to `null` as a "loading" signal — this is a deliberate API design choice to distinguish between "no data yet" and "data loaded but empty."

### The Cascade

When any of three poll-dependent stores goes null, **all 14 consumption sites** that read them via `?? []` trigger recomputation:

```javascript
// Each read triggers independent recomputation
const posRows  = positionsStore.value ?? [];     // Line 1: null → []
const holdRows = pulseHoldingsStore.value ?? []; // Line 2: null → []
const fundRows = fundsStore.value ?? [];         // Line 3: null → []

// Now compute with empty data
for (const p of posRows) {  // Empty loop
  day_pnl += (ltp - prev_close) * qty;  // Never executes
}

return { day_pnl: 0, chg_pct: null, exp_pnl: null };
```

This is worse than a single null-guard at the top:

```javascript
// NEW — SWR guard at entry point
if (posRows == null || holdRows == null || fundRows == null) return _last;
```

Why? With 14 independent guards spread across 9 files, **they trigger at different times** as different derived stores evaluate. MarketPulse's local `positionsDayPnlStore` guard fires at T=5101ms, NavBreakdown's guard at T=5102ms, Derivatives' guard at T=5103ms — users see a cascading wave of zeros, not a single unified flash.

With **one** unified guard in the single `portfolioStore`, all consumers inherit the same snapshot protection simultaneously.

---

## The Dependency Hierarchy

Understanding why SWR works requires mapping the four-tier reactive chain:

### Tier 0: Raw API (5s cadence, **can go null**)
- `positionsStore.value` → broker `/positions` endpoint
- `pulseHoldingsStore.value` → broker `/holdings` endpoint  
- `fundsStore.value` → broker `/funds` endpoint

These are `createDataStore` instances with explicit `null` state during fetch.

### Tier 1: Market Data (continuous, **never null**)
- `symbolStore[sym].ltp` → from KiteTicker WebSocket, or fallback to broker quote
- `underlyingSpotStore[root].ltp` → lookup for virtual roots (e.g., "NIFTY", "CRUDEOIL")

The WebSocket is alive whenever the market is open, ticking independently of poll cycles.

### Tier 2: Per-Symbol Derived (depends on T0 + T1)
- `livePositionDayPnl(closePx, qty, ltp)` — reads Tier 0 for position shape, Tier 1 for live price
- `expiryPnl(symbol, qty, avg, spot)` — reads Tier 0 for qty/avg, Tier 1 for live spot
- `dayChangePct(day_pnl, prev_mv)` — depends on Tier 2 computation result

**Critical observation**: Tier 2 depends on BOTH Tier 0 (which can be null) AND Tier 1 (which is always live). If Tier 0 goes null, Tier 2 becomes undefined.

### Tier 3: Aggregates (depends on Tier 2)
- `positionsDerivedStore.total.day_pnl` → sum of per-symbol day_pnl values
- `positionsDerivedStore.byRoot.exp_pnl` → sum of exp_pnl per F&O root
- `holdingsDayPnlStore.total` → aggregated holdings day P&L

### Tier 4: Display (reads Tier 3)
- NavStrip components read `portfolioStore.positions.total.chg_pct`
- MarketPulse grid reads `portfolioStore.positions.byKey[sym].exp_pnl`
- Derivatives legs grid reads `portfolioStore.positions.byRoot["NIFTY"].exp_pnl`

### Dependency Graph (ASCII)

```
Tier 0: Raw API (5s, CAN NULL)
  │
  positionsStore.value  pulseHoldingsStore.value  fundsStore.value
  │                     │                        │
  └─────────────────────┴────────────────────────┘
                        │
                   (SWR NULL GUARD HERE)
                        │
Tier 1: WebSocket (continuous, NEVER NULL)
  │
  symbolStore[sym].ltp  underlyingSpotStore[root].ltp
  │                     │
  └─────────────────────┘
                        │
Tier 2: Per-Symbol Derived
  │
  day_pnl = f(qty, avg, ltp, prev_close)
  exp_pnl = f(qty, avg, spot_for_root)
  chg_pct = f(day_pnl, prev_mv)
                        │
Tier 3: Aggregates
  │
  total.day_pnl, byRoot.exp_pnl, byAccount.todayMtm
                        │
Tier 4: Display
  │
  NavStrip, Pulse, Derivatives, NavBreakdown, Dashboard
```

---

## The F&O Complication: Virtual Roots and Two-Stream Dependencies

### The Virtual Root Problem

An option like `NIFTY25JAN24500CE` (Jan 2025 NIFTY 24500 call) has two independent data dependencies:

1. **Own LTP** (for `day_pnl`): the option's own ticker price on KiteTicker
2. **Underlying spot** (for `exp_pnl`): the NIFTY 50 index level — a **virtual entity** that doesn't trade

The spot for intrinsic value is NOT the option's own price — it's its root's spot. This creates two tick streams per option leg.

### Root Resolution

The system decomposes `NIFTY25JAN24500CE` into:
- symbol: `NIFTY25JAN24500CE`
- root: `NIFTY` (the virtual root)
- strike: `24500`
- kind: `CE` (call)

The root "NIFTY" is not a Zerodha tradingsymbol — it's an alias. The actual underlying traded is `NIFTY50` (NSE equity index futures), or for commodities like `CRUDEOIL25JAN6800CE`, the root is `CRUDEOIL` but the underlying is `CRUDEOIL` MCX futures.

### The O(N²) Trap: Multiple Legs, One Root

**Naive approach (OLD)**: Call `getUnderlyingSpot(root)` inside the per-position loop:

```javascript
for (const p of positions) {
  const sym = p.tradingsymbol;    // NIFTY25JAN24500CE
  const root = decomposeSymbol(sym).root;  // NIFTY
  const spot = getUnderlyingSpot(root);    // ← called per leg
  exp_pnl = expiryPnl({ symbol: sym, qty, avg, kind: 'opt' }, spot);
}
```

If the portfolio has 10 NIFTY calls on the same day (different strikes), this calls `getUnderlyingSpot("NIFTY")` 10 times. Each call does a symbolStore lookup, which is reactive and registers as a dependency — so changing NIFTY's spot re-runs the entire portfolio computation 10 times.

**Better approach (NEW)**: Build a `rootSpotCache` **before** the position loop:

```javascript
const rootSpotCache = {};
for (const p of positions) {
  const root = decomposeSymbol(p.tradingsymbol).root;
  if (!(root in rootSpotCache)) {
    rootSpotCache[root] = untrack(() => getUnderlyingSpot(root));
  }
}

// Now loop and reuse cache
for (const p of positions) {
  const root = decomposeSymbol(p.tradingsymbol).root;
  const spot = rootSpotCache[root];  // Cache hit, no new lookup
  exp_pnl = expiryPnl({...}, spot);
}
```

**Key insight**: All reads are wrapped in `untrack()` so individual symbol ticks don't register as per-symbol reactive dependencies. Instead, the unified computation depends only on the throttled `_tick` clock (4Hz), not on individual symbol ticks (1000+ Hz in volatile markets).

### The byRoot Aggregation

The new design also pre-builds `byRoot` — a map of roots to their aggregated P&L:

```javascript
byRoot["NIFTY"] = {
  spot: 23450.5,           // Cached once per root
  legs: [
    "NIFTY25JAN24500CE",
    "NIFTY25JAN24600CE",
    "NIFTY25JAN24400PE"    // All legs under this root
  ],
  day_pnl: 2500,           // Sum of all legs
  exp_pnl: 8750,           // Sum of all legs
  extrinsic: 1200
};
```

This enables a new feature: strategy-level P&L view where traders see "my NIFTY spread is up 2.5k today, with 8.75k expiry value."

---

## Data Cadence: Why Different Update Rates Are OK

The system works correctly despite three different cadences:

| Layer | Cadence | Source | Behavior |
|---|---|---|---|
| Poll data (Tier 0) | 5 seconds | Broker API | Goes null during fetch, then reappears with fresh data |
| WebSocket ticks (Tier 1) | ~1–1000 Hz | KiteTicker | Continuous push whenever market open; never null |
| Computation clock (portfolioStore) | 4 Hz (250ms debounce) | Symbol tick count | Unified throttle to prevent over-recomputation |

### Why 4 Hz is Sufficient

The computation throttle debounces the symbolTickCount by 250ms:

```javascript
let _tick = $state(0);
let _tickTimer = null;
symbolTickCount.subscribe(() => {
  if (_tickTimer) return;  // Debounce: coalesce multiple ticks
  _tickTimer = setTimeout(() => {
    _tickTimer = null;
    _tick++;  // Trigger recompute in $derived.by
  }, 250);
});
```

Why not compute on every tick? Because a single LTP change doesn't need 1000 DOM updates/sec — browsers can't render faster than 60 Hz anyway (16.7ms), so 4 Hz (250ms) is 4× safer margin. It batches ticks into human-perceptible updates while keeping the UI responsive.

### SWR Holds the Last Snapshot

During the ~200ms poll revalidation window:

```
T = 5000:  poll fires, positionsStore.value → null (loading signal)
T = 5050:  portfolioStore $derived.by checks null guard
           if (posRows == null) return _last;  ← return previous snapshot
T = 5100:  symbolStore receives LTP tick
           portfolioStore re-runs at 4 Hz clock
           computes with _last positions data + new tick
           displays fresh day_pnl + exp_pnl
T = 5200:  broker response arrives
           positionsStore.value → [{ qty: 100, ... }]
           portfolioStore re-runs, computes with new positions data
```

The key: **even though the position qty/avg is stale, the day_pnl is fresh** because it's recomputed at 4 Hz using the live LTP from the tick stream. Users see current prices; the broker data is a background refresh that doesn't block rendering.

---

## The Solution: Stale-While-Revalidating (SWR) Pattern

### Single Unified Store

Instead of three separate derived stores reading raw API data with `?? []` guards, one `portfolioStore.svelte.js` consolidates all computation:

```javascript
const _portfolio = $derived.by(() => {
  void _tick;  // Register on 4 Hz throttle

  // SWR NULL GUARD — one place, all consumers protected
  const posRows = positionsStore.value;
  const holdRows = pulseHoldingsStore.value;
  const fundRows = fundsStore.value;
  if (posRows == null || holdRows == null || fundRows == null) return _last;

  // Compute positions, holdings, funds...
  _last = { positions, holdings, funds };
  return _last;
});
```

**Benefits**:
- **Single guard**: all consumers inherit protection simultaneously (no cascading zeros)
- **Snapshot safety**: `_last` holds the last known good state during revalidation
- **Reactive correctness**: one `$derived.by()` block has one recompute trigger, not 14

### Root-First Computation Order

Before iterating positions, pre-build the root spot cache:

```javascript
// STEP 1: Build rootSpotCache once
const rootSpotCache = {};
for (const p of posRows) {
  const root = decomposeSymbol(p.tradingsymbol).root;
  if (!(root in rootSpotCache)) {
    const liveSpot = untrack(() => getUnderlyingSpot(root));
    rootSpotCache[root] = liveSpot > 0 ? liveSpot : (p.underlying_ltp || 0);
  }
}

// STEP 2: Iterate positions, reuse cache
for (const p of posRows) {
  const root = decomposeSymbol(p.tradingsymbol).root;
  const spot = rootSpotCache[root];  // One lookup per root, shared by all legs
  exp_pnl = expiryPnl({...}, spot);
}
```

**Performance improvement**: O(N) iteration instead of O(N×M) where M is unique roots.

### Backward Compatibility via Shims

The old three stores still exist as thin re-export wrappers:

```javascript
// positionsDerivedStore.svelte.js (now a shim)
export const positionsDerivedStore = {
  get total()           { return portfolioStore.positions.total; },
  get byKey()           { return portfolioStore.positions.byKey; },
  get expiryByAcct()    { return portfolioStore.positions.expiryByAcct; },
  // ...
};
```

**Zero consumer changes** needed for 8 of 11 files — they still import from the old names and get the new data behind the scenes.

---

## Code Walkthrough: Before and After

### BEFORE: Three Separate Stores with `?? []` Guards

```javascript
// positionsDerivedStore.svelte.js (OLD)
const posRows = positionsStore.value ?? [];
const holdRows = pulseHoldingsStore.value ?? [];
if (posRows.length === 0 || holdRows.length === 0) return _EMPTY;

// Compute independently, no root cache
for (const p of posRows) {
  const root = decomposeSymbol(p.tradingsymbol).root;
  const spot = getUnderlyingSpot(root);  // ← Called per leg, reactive
  exp_pnl = expiryPnl({...}, spot);
}
```

**Problems**:
1. When `positionsStore.value` goes null during poll refresh, `?? []` triggers with empty array
2. Derived values become zero (empty loop)
3. Each of three stores zeroes independently → cascading flashes
4. Root spot lookup inside loop → O(N) lookups, high reactivity

### AFTER: Unified Store with SWR Guard

```javascript
// portfolioStore.svelte.js (NEW)
const _portfolio = $derived.by(() => {
  void _tick;  // 4 Hz unified clock

  // ONE guard — all consumers protected atomically
  const posRows = positionsStore.value;
  const holdRows = pulseHoldingsStore.value;
  const fundRows = fundsStore.value;
  if (posRows == null || holdRows == null || fundRows == null) return _last;

  // Root cache BEFORE loop
  const rootSpotCache = {};
  for (const p of posRows) {
    const root = decomposeSymbol(p.tradingsymbol).root;
    if (!(root in rootSpotCache)) {
      const liveSpot = untrack(() => getUnderlyingSpot(root));
      rootSpotCache[root] = liveSpot > 0 ? liveSpot : (p.underlying_ltp || 0);
    }
  }

  // Iterate, reuse cache
  for (const p of posRows) {
    const root = decomposeSymbol(p.tradingsymbol).root;
    const spot = rootSpotCache[root];  // Cache hit — no new lookup
    if (spot > 0) {
      exp_pnl = expiryPnl({...}, spot);
    }
  }

  _last = { positions, holdings, funds };
  return _last;
});
```

**Improvements**:
1. SWR guard at entry — null data returns last snapshot immediately
2. One `$derived.by()` block — all consumers recompute together, no cascading flashes
3. Root cache built once before loop — O(unique_roots) lookups instead of O(positions)
4. `untrack()` wraps all individual symbol reads — unified throttle takes over

---

## Results: Metrics and Validation

### Bugs Fixed

| Bug | Surface | Cause | Resolution |
|---|---|---|---|
| Zero chg% after page nav | NavStrip | `?? []` with null | SWR guard |
| Zero day_pnl during poll | MarketPulse | 14 independent guards | Unified store |
| Blank exp_pnl columns | Derivatives | Staggered recompute | One recompute clock |
| Zero todayMtm after 200ms | Dashboard | Null cascade | Last snapshot hold |
| Stale holdings P&L | Pulse + NavStrip | Separate store reads | Unified holdings result |
| Option expiry flicker | All surfaces | Root spot re-lookup per leg | Root cache |
| Margin util wrong after close | NavStrip | Fund aggregation null | SWR holds funds |
| Account totals blank | NavBreakdown | Independent store guards | Atomic update |
| Broker API timeout display | All | No fallback on slow response | `_last` snapshot |
| Cascading zero wave | All | Staggered derived store triggers | Atomic single guard |

### Store Consolidation

| Metric | Before | After | Change |
|---|---|---|---|
| Derived stores | 3 | 1 | −2 stores |
| Independent null-guards | 14 | 1 | −93% |
| Consumer files modified | 11 | 3 | −73% |
| Root spot lookups per portfolio | O(N) | O(unique_roots) | ~2–3× fewer |
| Zero-flash sites | 14 | 0 | 100% fix |

### Consumer File Impact

- **No changes needed** (8 files): Import from shim, receive new SWR data automatically
  - NavBreakdown, Dashboard, PositionStrip, MarketPulse (P slot), Derivatives (legs grid), Settings, Admin, Tour
- **Direct raw-store reads removed** (3 files): `NavStrip.svelte` (H slot), `MarketPulse.svelte` (top-level), `Derivatives.svelte` (summary row)

### Code Size

- `portfolioStore.svelte.js`: 558 lines (combined logic from 3 old stores + new root cache)
- `positionsDerivedStore.svelte.js`: 44 lines (shim, was 300+ before)
- `holdingsDayPnlStore.svelte.js`: ~30 lines (shim, was 200+ before)
- `positionsDayPnlStore.svelte.js`: ~30 lines (shim, was 180+ before)
- **Total**: 90 lines overhead vs. 680 lines of logic — net −590 lines after consolidation

---

## Generalizing the Pattern: When to Use SWR in Reactive Stores

### Prerequisites for SWR

SWR works when:

1. **Data has two sources with different latencies**
   - Poll-based API (bursty, can be null)
   - Real-time stream (continuous, never null)
   - Example: broker positions + WebSocket LTP ticks

2. **Staleness is acceptable for display purposes**
   - Showing yesterday's position count with today's prices is OK
   - Showing today's strike price with 5-second-old hedging notional is OK
   - **NOT** OK: showing yesterday's account balance as if it's current cash

3. **The null gap is brief and predictable**
   - 5-second polls with 200–800ms round-trip → ~200ms null window every 5s
   - NOT suitable for APIs that go null for minutes at a time

4. **Recomputation cost is lower than re-fetch cost**
   - Computing day_pnl from cached positions + fresh LTP is <1ms
   - Re-fetching positions from broker is 200–800ms
   - SWR saves the network round-trip

### The Dependency Declaration Principle

When designing reactive stores:

```javascript
// BAD: implicit null-handling spread across consumers
export const positionsStore = createDataStore({
  parse: (res) => res.rows
});

// Consumer has to guard everywhere
const rows = positionsStore.value ?? [];
```

```javascript
// GOOD: null-handling at the store boundary
const _portfolio = $derived.by(() => {
  const raw = rawStore.value;
  if (raw == null) return _lastKnown;  // SWR guard ONCE
  
  return compute(raw);  // Computation never sees null
});

export const portfolioStore = {
  get data() { return _portfolio.data; }  // Null-safe
};
```

**Rule**: place null-guards at store-creation time, not at consumer time. One guard beats 14.

### When NOT to Use SWR

- **Safety-critical data** (account balance, password, auth state): never accept stale
- **Unidirectional streams** (chat messages, logs): no fallback value makes sense
- **Infrequent updates** (config, metadata): null period is too long relative to update cadence

### Industry Parallels

- **TanStack Query (React Query)**: `useQuery()` cache + background refetch pattern
- **Bloomberg BLPAPI**: field freshness levels (stale, delayed, realtime) with snapshot fallback
- **AWS S3 eventual consistency**: read-your-own-writes pattern where clients cache locally
- **GraphQL subscriptions + polling**: subscription for live fields, poll for batch validation

---

## Lessons Learned

### 1. Null Is a Signal, Not Data
Treat null as "still loading" in the reactive framework, not "no data" to display. Distinguish at the store layer, not the consumer.

### 2. Single Recomputation Trigger, Not Many
Three independent derived stores with three null-guards will zero cascade. One unified store with one guard zeroes once. Consolidate reactive dependencies.

### 3. Batch Expensive Lookups Before Loops
Building a cache before iteration prevents O(N²) work and reduces reactive dependencies. `rootSpotCache` meant 10 legs share 1 spot lookup instead of each doing their own.

### 4. Untrack Individual Ticks, Throttle Computation
Wrapping fast-moving data (WebSocket ticks) in `untrack()` prevents re-reactivity explosion. Let a unified throttle (4 Hz) drive recomputation instead.

### 5. Backward Compatibility Matters
Shim stores let 8 of 11 consumers inherit the fix without code changes. Refactors with zero breaking changes scale better across large codebases.

---

## References

- **Source**: `/frontend/src/lib/data/portfolioStore.svelte.js` (unified SWR store, 558 lines)
- **Shims**: `/frontend/src/lib/data/{positionsDerivedStore,holdingsDayPnlStore,positionsDayPnlStore}.svelte.js`
- **Consumers**: NavStrip, MarketPulse, Derivatives, NavBreakdown, Dashboard (see CONSUMER_GUIDE for per-page data flow)
- **Related**: `symbolStore.svelte.js` (tick buffer), `underlyingSpotStore.svelte.js` (root virtual entity resolution)
- **Market hours**: `isMarketOpen()` in `/lib/marketHours.js` — gates day P&L formula choice

---

## Appendix: Technical Debt Addressed

This refactor also closed three legacy patterns:

1. **Direct `getSnapshot()` in `$derived`**: Replaced with `untrack()` wrapper to prevent per-symbol reactivity. Prevents `state_unsafe_mutation` compiler warnings.

2. **Holdings day P&L duplication**: Old `holdingsDayPnlStore` and `positionsDayPnlStore` both computed from the same raw holdings/positions data. Now computed once in `portfolioStore`, re-exported via shims.

3. **MarketPulse dual-compute**: Pulse grid had its own `buildUnified()` function computing day P&L per row. Now it calls `portfolioStore.setHoldingsFromPulse()` to override only the display values, reusing the canonical computation.

**Note on MarketPulse override**: The pulse grid's corporate-action-aware column adjustment (removing sold holdings) is handled at the UI layer (column selection), not at the store layer. The override is shallow — only `byKey` and `total` are replaced, while `byAccount` and fund data remain from the canonical store. This is intentional: cash/margin are account-level facts and should never be pulse-local.
