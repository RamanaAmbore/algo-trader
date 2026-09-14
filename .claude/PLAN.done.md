# Plan: SSOT for underlying spot prices — NavStrip / Snapshot / Pulse alignment

## Context

NavStrip Exp P&L has diverged from derivatives snapshot Exp P&L across multiple fixes because there is no single source of truth for underlying spot prices. Today's state:

- **Derivatives snapshot** (correct, per operator): falls back to `_underlyingQuotes[root]?.ltp` (batchQuote REST, refreshed every 30s) when `p.underlying_ltp = 0`. Correct price → correct intrinsic → correct Exp P&L.
- **NavStrip** (wrong): falls back to `symbolStore` (live KiteTicker) when `p.underlying_ltp = 0`. KiteTicker only has MCX option LTPs subscribed (the positions the user holds), NOT the underlying futures. So `getSnapshot('CRUDEOIL')` = null → row-scan returns 0 or option premium (not futures price) → wrong spot → wrong Exp P&L.

The `underlying_ltp` backend fix (962c020e) stamps the value on snapshot rows, but:
1. If `broker.quote()` fails for any reason (session hiccup, wrong key), `underlying_ltp = 0` → fallback divergence reappears.
2. Priorities 2–5 in NavStrip's `_resolveOptionSpot` are structurally wrong for MCX.

**Root cause**: three independent spot sources (backend `underlying_ltp`, page-local batchQuote, symbolStore) that return different values. Every patch fixes one code path but leaves the structural divergence.

**Fix**: Create a single `underlyingSpotStore.svelte.js` that both NavStrip and derivatives page import. One batchQuote poll, one cache, one result for every surface.

The three critical metrics (Day P&L, P&L, Exp P&L) must all derive from the same `positionsStore` base. Exp P&L additionally needs spot — that spot must come from a single frontend store, not three different fallback chains.

## Task

### 1. Create `frontend/src/lib/data/underlyingSpotStore.svelte.js`

New module. Responsibilities:
- Watches `positionsStore.value` reactively → extracts unique F&O underlying roots (e.g., `'CRUDEOIL'`, `'NIFTY'`)
- Polls `GET /api/instruments/batchQuote?symbols=<root1>,<root2>,...` every 30s (same API the derivatives page already uses)
- Exports:
  - `underlyingQuotes`: `$state({})` — map of `{ ROOT: { ltp, day_pct, prev_close } }` (same shape as derivatives page's current `_underlyingQuotes`)
  - `getUnderlyingSpot(root): number` — returns `underlyingQuotes[root]?.ltp ?? 0`
  - `loadUnderlyingSpots()`: triggers an immediate refresh (for call-sites that currently call `loadUnderlyingQuotes()`)

Pattern: follow the shape of `positionsDayPnlStore.svelte.js` for the store structure and polling pattern.

### 2. `frontend/src/lib/PositionStrip.svelte` — fix `_resolveOptionSpot`

Current priorities 1–5:
1. `p.underlying_ltp` (backend-stamped) ← keep
2–4. symbolStore (wrong for MCX futures when not subscribed) ← REMOVE
5. row-scan (option last_price, not spot price) ← REMOVE

New priorities:
1. `p.underlying_ltp` — backend SSOT
2. `getUnderlyingSpot(root)` from `underlyingSpotStore` — same batchQuote as derivatives page
3. Return 0 if both fail (show 0 rather than wrong value)

Import: `import { getUnderlyingSpot } from '$lib/data/underlyingSpotStore.svelte.js'`

### 3. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — use shared store

- Import `underlyingQuotes, loadUnderlyingSpots` from `underlyingSpotStore.svelte.js`
- Remove local `let _underlyingQuotes = $state({})` declaration
- Remove local `loadUnderlyingQuotes()` function definition
- Replace every `_underlyingQuotes` reference with the imported `underlyingQuotes`
- Replace every `loadUnderlyingQuotes()` call with `loadUnderlyingSpots()`
- `_rootSpot(root)` already uses `_underlyingQuotes[root]?.ltp` — no formula change, just the variable name

The derivatives snapshot Exp P&L formula and `_perRootReduce` logic stay unchanged.

### 4. Tests

**Vitest** (`frontend/src/lib/__tests__/data/underlyingSpotStore.test.js`):
- Mock batchQuote API; verify `getUnderlyingSpot('CRUDEOIL')` returns the mocked ltp
- Verify store refreshes roots from positionsStore (reactive extraction)
- Verify 0 returned for unknown root (no crash)

**Vitest** (`frontend/src/lib/__tests__/data/expiryPnl.test.js` or PositionStrip-level):
- `_resolveOptionSpot`: when `underlying_ltp = 0`, priority 2 uses `getUnderlyingSpot` not symbolStore
- When `getUnderlyingSpot('CRUDEOIL') = 5788`, Exp P&L is computed correctly

## Agents
- frontend: Implement underlyingSpotStore.svelte.js + patch PositionStrip.svelte (_resolveOptionSpot priorities) + patch derivatives/+page.svelte (use shared store). Files: `frontend/src/lib/data/underlyingSpotStore.svelte.js` (new), `frontend/src/lib/PositionStrip.svelte`, `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- backend: skip
- backend-test: skip
- doc: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no
- vitest: yes (frontend unit tests)

## Commit message
fix(navstrip): single underlyingSpotStore SSOT — share batchQuote between NavStrip and derivatives snapshot; remove symbolStore fallback from _resolveOptionSpot

## Done when
- `underlyingSpotStore.svelte.js` exists, polls batchQuote, exports `getUnderlyingSpot`
- NavStrip `_resolveOptionSpot` uses `getUnderlyingSpot` as priority 2 (no symbolStore/row-scan fallback)
- Derivatives page imports from shared store (no more local `_underlyingQuotes`)
- NavStrip Exp P&L matches derivatives snapshot Exp P&L for CRUDEOIL options during closed-hours AND during MCX open
- svelte-check 0 errors, vitest green
