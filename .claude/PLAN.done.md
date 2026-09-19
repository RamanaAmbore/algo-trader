# Plan: portfolioStore — Unified Reactive Data Architecture

## Context
The frontend has 14 race-prone sites where poll-based stores (`positionsStore`, `pulseHoldingsStore`, `fundsStore`) momentarily go null during a 5s refresh cycle. Every consumer independently uses `store.value ?? []` as fallback — producing zeros for `chg%`, `day_pnl`, `todayMtm`, `exp_pnl`, and `available funds` during the null gap. Root cause: no formal dependency hierarchy, no stale-while-revalidating guard, no single computation boundary.

**Design decisions from architecture discussion:**
- Symbol is the JOIN key linking WebSocket ticks → positions → holdings → instruments
- Inputs have different natural cadences (WebSocket continuous, polls every 5s) — that's fine; the COMPUTATION cadence is unified at 4Hz via `symbolTickCount + 250ms throttle`
- Options/futures map to a virtual root → one underlying spot resolved per root (not per leg)
- Root is a first-class grouping key in the output (`byRoot`) for derivatives strategy view
- One unified `portfolioStore` with ONE SWR null-guard replaces three separate derived stores
- Poll-based stores must never produce zeros during revalidation — they hold last known snapshot

**Fixes 10 visible bugs:** chg% zero on poll, chg% zero in legs, chg% lost on navigate derivatives→pulse, todayMtm zero in H-slot, NavBreakdown blanking, MarketPulse rows disappearing, PositionStrip animation glitch, OrderTicket ₹0 funds, Dashboard cards blanking, legs day_pnl/exp_pnl zeroing.

## Task

### Step 1 — Create `portfolioStore.svelte.js` (replaces 3 stores)

**File:** `frontend/src/lib/data/portfolioStore.svelte.js`

Single `$derived.by()`, same 4Hz throttle via `symbolTickCount`. Required deps gated at top. Root-first computation order. Pure computation functions extracted for Vitest.

**Required deps (gate — null → hold `_last`):**
- `positionsStore.value` — qty, avg, prev_close, account, exchange
- `pulseHoldingsStore.value` — qty, avg, previous_close, day_change_val, account
- `fundsStore.value` — live_cash, avail_margin, used_margin, collateral, option_premium

**Enrichment deps (never null, just keep ticking):**
- `symbolStore` via `_tick` — own LTP per symbol
- `underlyingSpotStore` — spot per root (resolved once per root via `getUnderlyingSpot(root)`)

**Computation order inside `$derived.by()`:**

```
STEP 1 — Positions (root-first)
  1a. For each position row: decomposeSymbol(sym) → { root, strike, kind }
  1b. Resolve root spot ONCE per root: p.underlying_ltp || getUnderlyingSpot(root)
      (one lookup per root, shared by all legs under that root)
  1c. Per leg compute:
        own_ltp  = getSnapshot(sym)?.ltp ?? p.last_price
        day_pnl  = livePositionDayPnl({closePx, pollLtp, qty, avg, dcvRow}, own_ltp, {marketOpen})
        chg_pct  = dayChangePct(day_pnl, prev_close × |qty|)
        exp_pnl  = root_spot > 0 ? expiryPnl({symbol,qty,avg_cost,kind}, root_spot) + realised : null
        extrinsic = exp_pnl != null ? exp_pnl - (own_ltp - avg) × qty : null
  1d. Accumulate:
        byKey[sym]     = { day_pnl, chg_pct, exp_pnl, extrinsic, pnl, prev_mv }
        byRoot[root]   = { spot, legs:[], day_pnl, exp_pnl, extrinsic }  ← NEW
        byAccount[acct]= { day_pnl, exp_pnl, pnl }
        expiryByAcct   Map<acct, Σ exp_pnl>
        total          { day_pnl, exp_pnl, extrinsic }

STEP 2 — Holdings
  For each holding row:
    snap_ltp = getSnapshot(sym)?.ltp
    live_ltp = snap_ltp > 0 ? snap_ltp : h.last_price
    close_px = h.previous_close || h.close_price || h.ohlc?.close || 0
    val      = close_px <= 0            ? dcv
             : |live_ltp - close_px| > 0.005 ? (live_ltp - close_px) × qty
             : dcv
  Accumulate:
    holdings.byKey[sym]     = val
    holdings.byAccount[acct]= { day_pnl: Σval, value: Σ(live_ltp×qty), lifetime: Σh.pnl }
    holdings.total          = Σval

STEP 3 — Funds
  For each fund row (account != 'TOTAL'):
    totalMargin = used_margin + avail_margin
    utilPct     = totalMargin > 0 ? used_margin / totalMargin × 100 : 0
    totalCash   = (live_cash ?? cash) + long_option_premium
  Accumulate:
    funds.byAccount[acct] = { live_cash, avail_margin, used_margin, collateral, totalMargin, utilPct }
    funds.total           = Σ all accounts
```

**Output shape:**
```js
{
  positions: {
    total:        { day_pnl, exp_pnl, extrinsic },
    byKey:        { [sym]: { day_pnl, chg_pct, exp_pnl, extrinsic, pnl, prev_mv } },
    byAccount:    { [acct]: { day_pnl, exp_pnl, pnl } },
    byRoot:       { [root]: { spot, legs:string[], day_pnl, exp_pnl, extrinsic } },
    expiryByAcct: Map<acct, number>,
  },
  holdings: {
    total:     { day_pnl },
    byKey:     { [sym]: number },
    byAccount: { [acct]: { day_pnl, value, lifetime } },
  },
  funds: {
    total:     { live_cash, avail_margin, used_margin, totalMargin, utilPct },
    byAccount: { [acct]: { live_cash, avail_margin, used_margin, collateral, totalMargin, utilPct } },
  },
}
```

**Preserve `setFromPulse`:** MarketPulse calls `holdingsDayPnlStore.setFromPulse(byKey, total)` after buildUnified for filter-aware NavStrip H-slot. Wire as `portfolioStore.setHoldingsFromPulse(byKey, total)` — same override pattern, same `_pulseHoldingsByKey` / `_pulseHoldingsTotal` state variables. Holdings getters check pulse override first.

**Preserve `_computeDerived` export:** pure function still exported from portfolioStore (or re-exported) so existing Vitest tests pass unchanged.

---

### Step 2 — Convert 3 old stores to backward-compat shims

**`positionsDerivedStore.svelte.js`** → import portfolioStore, re-export:
```js
export const positionsDerivedStore = {
  get total()           { return portfolioStore.positions.total; },
  get expiryTotal()     { return portfolioStore.positions.total.exp_pnl; },
  get byKey()           { return portfolioStore.positions.byKey; },
  get expiryByAcct()    { return portfolioStore.positions.expiryByAcct; },
  get byRootPositions() { return portfolioStore.positions.byRoot; },
  get byRootHoldings()  { return portfolioStore.holdings.byKey; },
  setFromPulse() {},
};
export { _computeDerived } from './portfolioStore.svelte.js';
```

**`holdingsDayPnlStore.svelte.js`** → import portfolioStore, re-export:
```js
export const holdingsDayPnlStore = {
  get total()     { return portfolioStore.holdings.total.day_pnl; },
  get byKey()     { return portfolioStore.holdings.byKey; },
  get byAccount() { return portfolioStore.holdings.byAccountForStrip; },
  setFromPulse(byKey, total) { portfolioStore.setHoldingsFromPulse(byKey, total); },
};
```

**`positionsDayPnlStore.svelte.js`** → already a shim pointing to positionsDerivedStore — update to point directly to portfolioStore:
```js
export const positionsDayPnlStore = {
  get total()  { return portfolioStore.positions.total.day_pnl; },
  get byKey()  { /* same Proxy pattern as today */ },
  setFromPulse() {},
};
```

---

### Step 3 — Fix all 14 consumer race sites

All consumers currently do one of:
- `store.value ?? []` → `portfolioStore.positions.rows` (never null, always last known)
- Read `positionsDerivedStore.byKey[sym]` → reads shim → portfolioStore
- Read `holdingsDayPnlStore.byAccount[key]` → reads shim → portfolioStore

**PositionStrip.svelte** (lines 33, 40, 45, 133-135):
- Remove `let positions = $state(positionsStore.value ?? [])` pattern × 3
- P∆ slot: `portfolioStore.positions.total.day_pnl`
- H∆ slot: `portfolioStore.holdings.total.day_pnl`
- Fingerprint derived: use `portfolioStore.positions.byKey` key count (never null)

**NavCard.svelte**: same pattern as PositionStrip — read from portfolioStore slots directly

**NavBreakdown.svelte** (lines 69-87): remove 4× `$state` snapshots from raw stores:
- P-slot: reads `expiryByAcct` → shim covers this
- H-slot `todayMtm`: `portfolioStore.holdings.byAccount[key]?.day_pnl` (was holdingsDayPnlStore.byAccount)
- M-slot: `portfolioStore.funds.byAccount[key]` (avail/used margin) — replaces direct fundsStore read
- C-slot: `portfolioStore.funds.byAccount[key]` (live_cash, collateral) — replaces direct fundsStore read

**MarketPulse.svelte** (lines 171, 200-201, 575, 771, 2756-2757):
- `activeListsStore.value ?? []` → keep as-is (watchlist, not position data)
- `pulsePositionsStore.value ?? []` × 2 → wrap with last-known pattern (keep as local `_lastPosRows`)
- `pulseHoldingsStore.value ?? []` × 2 → wrap with `_lastHoldRows`
- `fundsStore.value ?? []` → `portfolioStore.funds.byAccount` for display; keep raw store for OrderTicket
- `moversStore.value ?? []` → keep as-is (movers are independent)

**pulseColumns.js** `_dayPnlPctValueGetter`: already reads `positionsDerivedStore.byKey[sym]?.chg_pct` — shim covers, no change needed here. Shim ensures this never returns from an empty byKey during revalidation.

**derivatives/+page.svelte** (lines 3530, 3568):
- `pulsePositionsStore.value ?? []` → local `_lastDervPos` pattern (same as _lastCandidatesDayPnl)
- `holdingsStore.value ?? []` → local `_lastDervHold` pattern

**CandidateLegRow.svelte**: reads from positionsDerivedStore (shim) — no change if shim is correct

**OrderTicket.svelte** (line 1315): `fundsStore.value ?? []` → `portfolioStore.funds.byAccount` for display

**dashboard/+page.svelte** (lines 178-182): `store.value ?? []` × 3 → local last-known pattern per store

---

## Agents

- **frontend-phase1**: Create `portfolioStore.svelte.js` (full implementation per Steps 1 above) + convert `positionsDerivedStore.svelte.js`, `holdingsDayPnlStore.svelte.js`, `positionsDayPnlStore.svelte.js` to shims. 4 files. Run svelte-check after to verify shims compile clean.

- **frontend-phase2** (after phase1): Update consumers — `PositionStrip.svelte`, `NavCard.svelte`, `NavBreakdown.svelte`, `MarketPulse.svelte`, `OrderTicket.svelte`, `dashboard/+page.svelte`, `derivatives/+page.svelte`, `CandidateLegRow.svelte`. 8 files. All of these remove `store.value ?? []` and read from portfolioStore or its shims.

- **backend-test** (Vitest, after phase1): Write `frontend/src/lib/__tests__/data/portfolioStore.test.js`:
  - SWR guard: `positionsStore.value = null` → `portfolioStore.positions.byKey` equals last snapshot, not `{}`
  - Root decomposition: NIFTY option → `byRoot["NIFTY"]` contains the leg
  - Spot sharing: 3 NIFTY legs → `getUnderlyingSpot` called once, not 3×
  - Holdings day_pnl: formula `(ltp − close) × qty` and dcv fallback
  - Funds aggregation: `totalMargin`, `utilPct` computed correctly
  - `_computeDerived` existing tests: still pass (pure function re-exported from portfolioStore)

- **doc**: skip — architectural refactor, no operator-visible behaviour change in docs/specs

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
refactor(frontend): portfolioStore — unified reactive data store with SWR null-guard, root-first F&O computation, and byRoot aggregation replacing positionsDerivedStore + holdingsDayPnlStore + positionsDayPnlStore

## Done when
- svelte-check 0 errors
- Vitest portfolioStore tests pass (SWR guard, byRoot, chg_pct, holdings, funds)
- All existing `_computeDerived` Vitest tests still pass
- `portfolioStore.positions.byKey[sym].chg_pct` never zero during poll refresh (verified by SWR test)
- `portfolioStore.holdings.byAccount[acct].day_pnl` never zero during poll refresh
- NavBreakdown H-slot todayMtm matches PerformancePage holdings day_pnl
- chg% stable in positions, legs, and after navigate derivatives→pulse
- No `store.value ?? []` left in any consumer (grep clean)
