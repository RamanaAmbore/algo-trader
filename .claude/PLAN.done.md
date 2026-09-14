# Plan: positionsDerivedStore — unified 15s P&L source across all surfaces

## Context

Multiple surfaces compute Day P&L and Exp P&L independently with diverging formulas,
different LTP sources, and different cadences. The underlyingSpotStore fix (e5b844a1)
unified the spot source. This plan unifies everything else.

**Root problems:**
- NavStrip `_expiryForPosition` had same-direction formula as the Snapshot's
  `_expPnlByRootMap` but `_byUnderlyingExp/_accumulatePosExpPnl` skipped `+ realised`
- Surfaces update at different times (4Hz vs 10s vs per-tick) → transient divergence
- No single source for exp_pnl or extrinsic — each surface computes independently

## Confirmed design decisions

| # | Decision | Resolution |
|---|---|---|
| 1 | Day P&L source | symbolStore (remove setFromPulse entirely) |
| 2 | Holdings in store | Yes — separate `byRootHoldings`; Hold toggle controls display |
| 3 | Legs TOTAL row | Strategy-scoped (`_legsExpPnlTotal` unchanged) |
| 4 | `_byUnderlyingExp` | Option A — replace both with store reads |
| 5 | Extrinsic for closed legs | Zero (qty=0 → no open optionality) |

**Formula** (SSOT: `expiryPnl.js`, `+ realised` is correct):
```
qty === 0 (fully closed today):
    exp_pnl   = Number(p.realised || p.pnl || 0)
    extrinsic = 0

qty > 0 (open, possibly partial intraday close):
    exp_pnl   = expiryPnl({ qty, avg_cost, kind, symbol }, spot) + Number(p.realised || 0)
    extrinsic = expiryPnl({ qty, avg_cost, kind, symbol }, spot) − (liveLtp − avg_cost) × qty
              = (intrinsic − liveLtp) × qty   [options only; 0 for futures/equity]

Holdings (equity cross-hedge):
    exp_pnl   = (spot − cost_basis) × qty
    extrinsic = 0
```

Realised cancels in extrinsic: `exp_pnl − current_total_pnl` = intrinsic terms only.

**Trigger** (single, pure reactive):
- `positionsStore.value` or `holdingsStore.value` change → recompute immediately
- Reads current `getSnapshot(sym)?.ltp` from symbolStore at that moment (latest WebSocket tick)
- No separate timer — the 5s book poll IS the LTP refresh cadence
- Fill propagates immediately: postback → cache invalidate → positionsStore refreshes → recompute

**Cross-hedge mapping** (Gold ETF → GOLDM root, etc.): already in `rootOf.js` — no changes needed.

## Cadence after this plan

| Path | Cadence | Consumers |
|---|---|---|
| KiteTicker WebSocket → symbolStore | push | Raw LTP display columns + positionsDerivedStore (read at each recompute) |
| Book poll → positionsStore / holdingsStore | 5s | positionsDerivedStore trigger |
| underlyingSpotStore (tickBus patch) | push | positionsDerivedStore (reads at each recompute) |
| **positionsDerivedStore** | **5s (driven by book poll) + immediate on fill** | NavStrip, Pulse, Legs, Snapshot |
| holdingsDayPnlStore | 4Hz | NavStrip H-slot, Pulse holdings rows (unchanged) |

## New file: `positionsDerivedStore.svelte.js`

File: `frontend/src/lib/data/positionsDerivedStore.svelte.js`

```javascript
import { positionsStore, holdingsStore } from '$lib/data/marketDataStores.svelte.js';
import { getSnapshot } from '$lib/data/symbolStore.svelte.js';
import { getUnderlyingSpot } from '$lib/data/underlyingSpotStore.svelte.js';
import { expiryPnl } from '$lib/data/expiryPnl.js';
import { rootOf } from '$lib/data/rootOf.js';
import { targetsForProxy, getProxyRow } from '$lib/data/hedgeProxies.js';
import { livePositionDayPnl, liveHoldingDayPnl, isMarketOpen } from '$lib/data/nav.js';
import { untrack } from 'svelte';

// No timer — purely reactive. positionsStore / holdingsStore change every 5s book poll.
// LTP is read from symbolStore (latest WebSocket tick) at each recompute.
const _store = $derived.by(() => {
  const posRows  = positionsStore.value  ?? [];  // reactive: fires on every 5s poll + fill
  const holdRows = holdingsStore.value   ?? [];
  const marketOpen = untrack(() => isMarketOpen());

  const total          = { day_pnl: 0, exp_pnl: 0, extrinsic: 0 };
  const byKey          = {};
  const byRootPos      = {};
  const byRootHoldings = {};

  // ── Positions ──────────────────────────────────────────────────
  for (const p of posRows) {
    const sym    = String(p?.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const qty    = Number(p.quantity) || 0;
    const avg    = Number(p.average_price) || 0;
    const snap   = untrack(() => getSnapshot(sym));
    const ltp    = snap?.ltp ?? Number(p.last_price) ?? 0;
    const root   = rootOf(sym);
    const spot   = Number(p.underlying_ltp) || untrack(() => getUnderlyingSpot(root));
    const kind   = /CE$/i.test(sym) ? 'CE' : /PE$/i.test(sym) ? 'PE' : 'FUT';
    const realised = Number(p.realised || 0);

    const day_pnl = livePositionDayPnl(p, ltp, { marketOpen });

    let exp_pnl, extrinsic;
    if (qty === 0) {
      exp_pnl   = Number(p.realised || p.pnl || 0);
      extrinsic = 0;
    } else {
      const ev  = expiryPnl({ qty, avg_cost: avg, kind, symbol: sym }, spot);
      exp_pnl   = ev != null ? ev + realised : null;
      extrinsic = ev != null ? ev - (ltp - avg) * qty : null;
    }

    const pnl = Number(p.pnl ?? 0);
    byKey[sym] = { day_pnl, exp_pnl, extrinsic, pnl };

    total.day_pnl += day_pnl;
    if (exp_pnl  != null) total.exp_pnl  += exp_pnl;
    if (extrinsic != null) total.extrinsic += extrinsic;

    const r = byRootPos[root] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
    r.day_pnl += day_pnl;
    r.pnl     += pnl;
    if (exp_pnl   != null) r.exp_pnl   += exp_pnl;
    if (extrinsic != null) r.extrinsic += extrinsic;
  }

  // ── Holdings (cross-hedge) ─────────────────────────────────────
  // Mirrors _accumulateHoldingExpPnl logic in +page.svelte.
  // Uses targetsForProxy + beta factor from hedgeProxies.js — DO NOT reinvent.
  for (const h of holdRows) {
    const sym  = String(h?.tradingsymbol || '').toUpperCase();
    if (!sym) continue;
    const qty  = Number(h.quantity) || 0;
    const cost = Number(h.average_price) || 0;
    const snap = untrack(() => getSnapshot(sym));
    const ltp  = snap?.ltp ?? Number(h.last_price) ?? 0;
    const day_pnl = liveHoldingDayPnl(h, ltp, { marketOpen });

    // Cross-hedge routing: targetsForProxy maps proxy → target roots (e.g. GOLDBEES → GOLD)
    const targets = untrack(() => targetsForProxy(sym));
    const credits = targets.length ? targets : [rootOf(sym)];

    for (const target of credits) {
      let exp_pnl = null;
      if (targets.length && qty > 0 && ltp > 0) {
        // Beta-adjusted effective qty: effQty = (beta × marketValue) / targetSpot
        const proxyRow  = untrack(() => getProxyRow(sym, target));
        const beta      = proxyRow?.beta ?? 1;
        const targetSpot = untrack(() => getUnderlyingSpot(target));
        if (targetSpot > 0) {
          const effQty = (beta * ltp * qty) / targetSpot;
          exp_pnl = (targetSpot - (ltp / (beta || 1))) * effQty; // directional at current spot
        }
      } else if (!targets.length && qty > 0) {
        // Direct equity: (spot − cost) × qty
        const spot = untrack(() => getUnderlyingSpot(target)) || ltp;
        exp_pnl = spot > 0 ? (spot - cost) * qty : null;
      }

      const r = byRootHoldings[target] ??= { day_pnl: 0, exp_pnl: 0, extrinsic: 0, pnl: 0 };
      r.day_pnl += day_pnl;
      r.pnl     += Number(h.pnl ?? 0);
      if (exp_pnl != null) r.exp_pnl += exp_pnl;
    }
  }

  return { total, byKey, byRootPositions: byRootPos, byRootHoldings };
});

export const positionsDerivedStore = {
  get total()           { return _store.total;           },
  get byKey()           { return _store.byKey;           },
  get byRootPositions() { return _store.byRootPositions; },
  get byRootHoldings()  { return _store.byRootHoldings;  },
};
```

## File changes

### 1. Shim `positionsDayPnlStore.svelte.js`
Replace internals with a thin re-export from `positionsDerivedStore`. Keep the
exported name and API shape so existing imports compile during migration:
- `total` → `positionsDerivedStore.total.day_pnl`
- `byKey[sym]` → `positionsDerivedStore.byKey[sym]?.day_pnl ?? 0` (number, backward compat)
- `setFromPulse()` → no-op (removed)
- `holdingsDayPnlStore` untouched (NavStrip H-slot, Pulse holdings rows)

### 2. PositionStrip.svelte — NavStrip P + E slots
File: `frontend/src/lib/PositionStrip.svelte`
- Import `positionsDerivedStore` from `$lib/data/positionsDerivedStore.svelte.js`
- P-slot (Day P&L): `positionsDerivedStore.total.day_pnl`
- E-slot (Exp P&L): `positionsDerivedStore.total.exp_pnl`
- Delete `_expiryForPosition()` function (~30 lines)
- Delete the `$effect` that accumulates per-row expiry P&L into the E-slot total

### 3. NavCard.svelte + NavBreakdown.svelte
Files: `frontend/src/lib/NavCard.svelte`, `frontend/src/lib/NavBreakdown.svelte`
- Replace `positionsDayPnlStore.total` → `positionsDerivedStore.total.day_pnl`
- Replace `positionsDayPnlStore.byKey[sym]` → `positionsDerivedStore.byKey[sym]?.day_pnl ?? 0`

### 4. MarketPulse.svelte — remove setFromPulse
File: `frontend/src/lib/MarketPulse.svelte`
- Remove the `$effect` (~15 lines) that computes `pulseByKey`/`pulseTotal` and calls
  `positionsDayPnlStore.setFromPulse(pulseByKey, pulseTotal)`
- Pulse grid positions rows `day_pnl` column: add valueGetter reading
  `positionsDerivedStore.byKey[sym]?.day_pnl` (replaces CQ-based value)
- Holdings rows: unchanged (holdingsDayPnlStore still drives H-slot)

### 5. pulseColumns.js — new Exp P&L + Extrinsic columns
File: `frontend/src/lib/data/pulseColumns.js`

Add after the existing P&L column factories:
```javascript
export function mkExpPnlCol(getDerivedByKey) { ... }     // exp_pnl, dirCls, aggFmtGrid
export function mkExtrinsicCol(getDerivedByKey) { ... }  // extrinsic, same style
```
Both return null (blank cell) for non-derivative rows. Insert after P&L column in
the positions column array. Column order: **Exp P&L → Extrinsic**.

Find column assembly location: grep `mkRightColDefs` in `pulseColumns.js` (~line 521).

### 6. Derivatives Legs table — new columns + remove chip + style closed rows
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

**New columns** (after existing P&L column, lines ~4595-4615):
```javascript
{
  headerName: 'Exp P&L', width: 90, type: 'numericColumn',
  valueGetter: p => positionsDerivedStore.byKey[sym(p)]?.exp_pnl ?? null,
  cellClass: p => dirCls(p.value),
  valueFormatter: p => p.value != null ? aggFmtGrid(p.value) : '',
},
{
  headerName: 'Extrinsic', width: 90, type: 'numericColumn',
  valueGetter: p => positionsDerivedStore.byKey[sym(p)]?.extrinsic ?? null,
  cellClass: p => dirCls(p.value),
  valueFormatter: p => p.value != null ? aggFmtGrid(p.value) : '',
},
```

**Remove open/close chip**: delete the chip/badge element on `splitClosedReopened`
sub-rows (find by searching for the chip render in the Legs row template).

**Closed row styling**: for rows where `quantity === 0` (or the "close" sub-row from
`splitClosedReopened`), apply: `opacity-50` + `text-gray-400` on qty/avg/P&L cells.
Keep the row visible and readable — dimmed, not hidden.

### 7. Derivatives Snapshot — replace derivations + add Extrinsic column
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

**Delete** these now-redundant `$derived` computations:
- `_expPnlByRootMap` (replaced by store)
- `_byUnderlyingExp` + `_accumulatePosExpPnl` + `_accumulateHoldingExpPnl` (replaced by store)

**Replace Snapshot data reads:**
```javascript
// Was: _expPnlByRootMap[root]
// Now (Hold OFF): positionsDerivedStore.byRootPositions[root]?.exp_pnl ?? 0
// Now (Hold ON):  (byRootPositions[root]?.exp_pnl ?? 0) + (byRootHoldings[root]?.exp_pnl ?? 0)

// Was: _fnoDayPnlByRoot.byRoot[root]  (keep — sourced from Pulse, F&O day P&L)
// Unchanged: day_pnl stays from existing _fnoDayPnlByRoot

// New Extrinsic column:
// Hold OFF: positionsDerivedStore.byRootPositions[root]?.extrinsic ?? 0
// Hold ON:  byRootPositions[root]?.extrinsic ?? 0  (holdings have no extrinsic)
```

**Add Extrinsic column** to Snapshot ag-Grid and HTML table after Exp P&L column.
TOTAL row: `positionsDerivedStore.total.extrinsic`.

Keep `_underlyingQuotes` (→ `underlyingSpotStore.value`) for Spot display column — live.
Keep `_legsExpPnlTotal` for Legs TOTAL row — strategy-scoped, unchanged.

### 8. Decouple pulse.tick_interval_ms from book poller
File: `frontend/src/routes/(algo)/+layout.svelte`

Remove the call to `setBookPollerInterval(pulse_tick_interval_ms)` (lines ~813-816).
Book poller cadence should only come from `polling.book_live_ms` → `setBookPollerLiveMs()`.

Before: `pulse.tick_interval_ms` drove both MarketPulse rebuild AND book poll cadence
(to keep NavStrip day P&L in sync with Pulse via setFromPulse).
After: setFromPulse removed → Pulse rebuild rate and book poll rate are independent.
Changing Pulse cadence should no longer accidentally slow `positionsDerivedStore`.

Settings ownership after this change:
- `pulse.tick_interval_ms`  → MarketPulse rebuild rate only
- `polling.book_live_ms`    → book poller → positionsStore → positionsDerivedStore
- `polling.book_closed_ms`  → same, closed-market cadence
- `polling.idle_timeout_min` → hibernation threshold

No settings are removed — all serve independent purposes.

## Agents

- frontend: Implement all 8 file changes. One agent. Read each file before editing.
  Files:
  - NEW: `frontend/src/lib/data/positionsDerivedStore.svelte.js`
  - SHIM: `frontend/src/lib/data/positionsDayPnlStore.svelte.js`
  - `frontend/src/lib/PositionStrip.svelte`
  - `frontend/src/lib/NavCard.svelte`
  - `frontend/src/lib/NavBreakdown.svelte`
  - `frontend/src/lib/MarketPulse.svelte`
  - `frontend/src/lib/data/pulseColumns.js`
  - `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

  Key imports: `positionsDerivedStore`, `expiryPnl`, `getUnderlyingSpot`, `rootOf`,
  `livePositionDayPnl`, `liveHoldingDayPnl`, `isMarketOpen`,
  `targetsForProxy`, `getProxyRow` from `$lib/data/hedgeProxies.js`.
  No timer imports needed — store is purely reactive.

  Cross-hedge constraint: `byRootHoldings` MUST use `targetsForProxy(sym)` and
  `getProxyRow(sym, targetRoot).beta` — same logic as existing `_accumulateHoldingExpPnl`
  in `+page.svelte`. Read that function before implementing the holdings loop.

  Hold toggle: `_includeHoldings` state already gates equity legs in the derivatives page
  (lines 676, 1720, 2301-2305, 2445). Store always computes `byRootHoldings`; derivatives
  page decides whether to include it in Snapshot display. OptionsPayoff and `_equityLegs`
  are already wired — no changes needed there.

  Self-audit before committing:
  1. Grep all remaining uses of `setFromPulse` — must be zero (or no-op shim only)
  2. Grep `_expiryForPosition` — must be zero (deleted)
  3. Grep `_expPnlByRootMap` and `_byUnderlyingExp` — must be zero (deleted)
  4. Confirm `byRootHoldings` is used in Snapshot when Hold is active

- backend: skip

- broker: skip

- doc: Update `docs/specs/NAVSTRIP_SPEC.md` §1 — E-slot reads
  `positionsDerivedStore.total.exp_pnl` (15s + immediate on fill). Formula: closed
  legs = realised, open = expiryPnl + realised, no independent accumulation.
  Update `docs/specs/PULSE_SPEC.md` — Exp P&L + Extrinsic columns, 15s cadence,
  Hold toggle controls cross-hedge inclusion.

- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

New Vitest file: `frontend/src/lib/__tests__/data/positionsDerivedStore.test.js`

**Test architecture**: Extract the core computation into a pure helper function
`_computeDerived(posRows, holdRows, deps)` where `deps = { getSnap, getSpot, getTargets,
getProxyRow, livePositionDayPnl, liveHoldingDayPnl, marketOpen }`. The store wraps this
with the reactive `$derived.by()` call. Tests import and call `_computeDerived` directly
— no Svelte reactivity context required.

Also test `positionsDayPnlStore` shim in a separate describe block.

---

### Group A — exp_pnl formula branches

**A1. Fully closed leg** — qty=0, realised=500, pnl=500
→ `byKey['NIFTY24OCT17500CE'].exp_pnl = 500`, `extrinsic = 0`
→ `total.exp_pnl = 500`, `total.extrinsic = 0`

**A2. Open CE in-the-money (long)** — NIFTY24OCT17500CE, qty=2, avg_cost=150,
spot=18000 (strike=17500, intrinsic=500), ltp=510, realised=0
→ `expiryPnl` returns `(18000−17500)×2 = 1000`
→ `byKey[sym].exp_pnl = 1000 + 0 = 1000`
→ `byKey[sym].extrinsic = 1000 − (510−150)×2 = 1000 − 720 = 280`

**A3. Open CE out-of-the-money** — strike=19000, spot=18000 (OTM)
→ `expiryPnl` returns `0` (intrinsic is 0 for OTM call)
→ `exp_pnl = 0 + realised`, `extrinsic = 0 − (ltp − avg)×qty` (negative, theta loss)

**A4. Open PE in-the-money (long)** — NIFTY24OCT19000PE, qty=1, avg_cost=200,
spot=18000, ltp=1050, realised=0
→ intrinsic = `max(19000−18000, 0) = 1000`
→ `expiryPnl` returns `1000`
→ `exp_pnl = 1000`, `extrinsic = 1000 − (1050−200)×1 = 150`

**A5. Open FUT (no optionality)** — NIFTY24OCTFUT, qty=1, avg=24500, spot=25000, ltp=25010
→ kind='FUT' → `expiryPnl` computes as `(spot−avg)×qty = 500`
→ `extrinsic = ev − (ltp−avg)×qty = 500 − 510 = −10` (futures: basis only, usually ~0)
→ For futures the test simply asserts no crash; extrinsic is near zero numerically

**A6. Open CE with partial realised (same-day partial close)** — qty=2, avg=150,
spot=5850 (MCX strike=5500), ltp=360, realised=300
→ intrinsic = `(5850−5500)×2 = 700`; `expiryPnl` = 700
→ `exp_pnl = 700 + 300 = 1000`
→ `extrinsic = 700 − (360−150)×2 = 700 − 420 = 280`

**A7. Short option (qty < 0)** — qty=−1, avg_cost=200 (sold CE), spot=18000, ltp=210, realised=0
→ `expiryPnl({ qty:−1, avg_cost:200, kind:'CE', symbol }, 18000)` — result is negative
→ `exp_pnl` and `extrinsic` are computed (not null), assigned to `byKey[sym]`
→ `total.exp_pnl` correctly includes negative contribution

**A8. Spot zero → null guard** — qty=2, spot=0 (underlying not loaded)
→ `expiryPnl` returns `null` for spot=0
→ `byKey[sym].exp_pnl = null`, `byKey[sym].extrinsic = null`
→ `total.exp_pnl` unchanged (null entries skipped — no NaN contamination)
→ `total.exp_pnl = 0` when all spots are zero

**A9. qty=0 always gives extrinsic=0, regardless of pnl** — closed leg with large pnl
→ Even if `p.realised = 5000`, `extrinsic = 0` (no open optionality)

**A10. realised=NaN or undefined treated as 0** — `p.realised = undefined`
→ `Number(undefined) = NaN` → guard converts to 0
→ `exp_pnl = expiryPnl(...)` (not `NaN`)

---

### Group B — byRootPositions aggregation

**B1. Two NIFTY positions → byRootPositions['NIFTY'] sums** —
pos1: NIFTY CE exp_pnl=400, extrinsic=100, day_pnl=50, pnl=300
pos2: NIFTY PE exp_pnl=200, extrinsic=80, day_pnl=30, pnl=100
→ `byRootPositions['NIFTY'] = { exp_pnl:600, extrinsic:180, day_pnl:80, pnl:400 }`

**B2. Null exp_pnl entries excluded from root sum** — one NIFTY leg has null (spot=0),
other has exp_pnl=500
→ `byRootPositions['NIFTY'].exp_pnl = 500` (not NaN, not 0+null)

**B3. Mixed closed + open under same root** —
closed leg (realised=200, exp_pnl=200) + open leg (exp_pnl=300)
→ `byRootPositions[root].exp_pnl = 500`

**B4. Multiple roots in portfolio** — NIFTY + BANKNIFTY + CRUDEOIL each get separate
`byRootPositions` keys; no cross-contamination between roots

**B5. total sums across all roots** — three roots with exp_pnl 100, 200, 300
→ `total.exp_pnl = 600`

---

### Group C — byKey per-symbol

**C1. byKey contains every tradingsymbol** — 3 positions → byKey has 3 keys

**C2. byKey[sym].day_pnl uses livePositionDayPnl result** — mock returns 75
→ `byKey[sym].day_pnl = 75`

**C3. byKey does not contain holdings** — holdings contribute to byRootHoldings only,
not to byKey (holdings have no per-symbol option math)

**C4. Symbol with missing tradingsymbol ('') skipped** — `p.tradingsymbol = ''`
→ that position not in byKey, not in byRootPositions, not in total

---

### Group D — Holdings (byRootHoldings)

**D1. Direct equity holding (no proxy)** — RELIANCE, qty=10, cost=2500, spot=2700
→ `targetsForProxy('RELIANCE') = []` → credits = ['RELIANCE']
→ `exp_pnl = (2700 − 2500) × 10 = 2000`
→ `byRootHoldings['RELIANCE'].exp_pnl = 2000`, `extrinsic = 0`

**D2. Cross-hedge with beta — GOLDBEES → GOLDM** —
GOLDBEES: qty=100, ltp=58, cost=55 (market value = 5800)
`targetsForProxy('GOLDBEES') = ['GOLDM']`, `getProxyRow('GOLDBEES','GOLDM').beta = 0.9`
`getUnderlyingSpot('GOLDM') = 70000` (per 10g)
→ `effQty = (0.9 × 58 × 100) / 70000 ≈ 0.0746`
→ `exp_pnl = (70000 − 58/0.9) × 0.0746` → assert exp_pnl is a finite number
→ `byRootHoldings['GOLDM'].exp_pnl` is set, `byRootHoldings['RELIANCE']` not affected

**D3. Holdings with targetSpot=0 → exp_pnl=null** —
cross-hedge target not yet loaded in underlyingSpotStore
→ `byRootHoldings[target].exp_pnl` remains 0 / not incremented

**D4. Holdings day_pnl flows into byRootHoldings** — liveHoldingDayPnl returns 120
→ `byRootHoldings[target].day_pnl += 120`

**D5. byRootHoldings is separate from byRootPositions** — both NIFTY options (positions)
and NIFTY equity holding: `byRootPositions['NIFTY']` and `byRootHoldings['NIFTY']`
are independent objects; no cross-assignment

**D6. Holdings qty=0 → exp_pnl=null (qty guard)** — zero-qty holding produces no
contribution to byRootHoldings

---

### Group E — positionsDayPnlStore shim

**E1. shim.total returns number** — `typeof positionsDayPnlStore.total === 'number'`
→ equals `positionsDerivedStore.total.day_pnl`

**E2. shim.byKey[sym] returns number** — accessing `.byKey['NIFTY24OCT17500CE']` returns
the day_pnl number (not an object), backward-compatible with old `store.byKey[sym]` usage

**E3. shim.setFromPulse() is callable and no-ops** — calling with any args doesn't throw,
doesn't mutate anything, returns undefined

**E4. shim.byKey[unknown] is 0 or undefined gracefully** — accessing a symbol that doesn't
exist in positionsDerivedStore.byKey doesn't throw; callers guard with `?? 0`

---

### Group F — Edge cases and guard behaviour

**F1. Empty positions + empty holdings** — result:
`total = { day_pnl:0, exp_pnl:0, extrinsic:0 }`, `byKey = {}`,
`byRootPositions = {}`, `byRootHoldings = {}`

**F2. avg_cost=0 for open position** — extrinsic = `ev − (ltp − 0) × qty = ev − ltp×qty`
→ no division by zero, finite result

**F3. ltp falls back to p.last_price when getSnapshot returns null** —
mock getSnapshot returns null → ltp = Number(p.last_price)

**F4. spot prefers p.underlying_ltp over getUnderlyingSpot** — when `p.underlying_ltp > 0`,
use it; don't call getUnderlyingSpot

**F5. Mixed portfolio — all four row types** — one portfolio containing:
  - Open CE (exp_pnl=500, extrinsic=100)
  - Closed PE (exp_pnl=realised=200, extrinsic=0)
  - Open FUT (exp_pnl=300, extrinsic~0)
  - Equity holding (byRootHoldings contribution)
→ `total.exp_pnl = 500+200+300 = 1000`
→ `total.extrinsic = 100+0+~0 = ~100`

**F6. Symbol casing** — `p.tradingsymbol = 'nifty24oct17500CE'` → uppercased to
`'NIFTY24OCT17500CE'` in byKey; kind detection works on uppercased sym

**F7. Market closed scenario** — `isMarketOpen()` returns false →
`livePositionDayPnl` is called with `{ marketOpen: false }`; exp_pnl formula unchanged
(exp_pnl uses settlement math not session math)

---

### Group G — Reactivity simulation (integration-style)

**G1. Recomputes when positionsStore.value changes** —
Initial: one position (exp_pnl=100). Update positionsStore mock to two positions.
Rerun `_computeDerived` with new rows → total reflects both positions.

**G2. Recomputes when holdingsStore.value changes** —
Initial: no holdings (byRootHoldings={}). Add one holding.
Rerun `_computeDerived` → byRootHoldings has entry.

**G3. LTP read from getSnapshot at compute time** — getSnapshot mock returns ltp=100
first call, 120 second call. Second compute reflects new ltp in day_pnl and extrinsic.

**G4. byRootPositions[root] not shared across computes** — second run produces
a fresh `byRootPos` object, not accumulated on top of first run's result.

---

### Test file structure

```javascript
// positionsDerivedStore.test.js
import { describe, it, expect, vi, beforeEach } from 'vitest';
// Import the pure computation helper exported alongside the store:
import { _computeDerived } from '$lib/data/positionsDerivedStore.svelte.js';
// Import shim for Group E:
import { positionsDayPnlStore } from '$lib/data/positionsDayPnlStore.svelte.js';

// vi.mock all reactive dependencies so _computeDerived can be called as pure fn
vi.mock('$lib/data/expiryPnl.js', () => ({ expiryPnl: vi.fn() }));
vi.mock('$lib/data/rootOf.js', () => ({ rootOf: vi.fn(sym => sym.replace(/\d.*/, '')) }));
vi.mock('$lib/data/nav.js', () => ({
  livePositionDayPnl: vi.fn(() => 0),
  liveHoldingDayPnl: vi.fn(() => 0),
  isMarketOpen: vi.fn(() => true),
}));

// Helper: build a minimal position row
function mkPos(overrides) {
  return { tradingsymbol:'NIFTY24OCT17500CE', quantity:0, average_price:0,
           last_price:0, realised:0, pnl:0, underlying_ltp:0, ...overrides };
}
// Helper: build a minimal holding row
function mkHolding(overrides) {
  return { tradingsymbol:'RELIANCE', quantity:0, average_price:0,
           last_price:0, pnl:0, ...overrides };
}
```

Agent note: if `_computeDerived` is not exposed as a named export in the final implementation,
expose it under that name (tree-shaken in prod, needed for tests). Alternatively the agent may
choose to test via the Svelte reactivity harness — whichever pattern the existing test files in
`frontend/src/lib/__tests__/data/` use, follow that pattern.

## Commit message

feat(positions): positionsDerivedStore — unified 15s source; Exp P&L + Extrinsic in Pulse/Legs/Snapshot; cross-hedge byRootHoldings; remove setFromPulse; fix Legs closed-row styling

## Done when

1. NavStrip E-slot = Derivatives Snapshot TOTAL exp_pnl exactly
2. NavStrip P-slot, Pulse day_pnl column, Snapshot day_pnl all read 15s-cadence store
3. Pulse positions table shows Exp P&L + Extrinsic columns
4. Derivatives Legs shows Exp P&L + Extrinsic columns; no open/close chip; closed rows dimmed
5. Derivatives Snapshot shows Extrinsic column; Hold toggle includes byRootHoldings
6. `setFromPulse`, `_expiryForPosition`, `_expPnlByRootMap`, `_byUnderlyingExp` all gone
7. All Vitest tests pass, svelte-check 0 errors
