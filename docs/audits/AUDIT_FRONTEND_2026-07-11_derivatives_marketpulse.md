# Frontend Audit — derivatives + MarketPulse (2026-07-11)

## Files audited
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (7,110 lines)
- `frontend/src/lib/MarketPulse.svelte` (5,522 lines)

## P1 — Correctness / defect surface

| File:line | Issue | Fix |
|---|---|---|
| MarketPulse:691 | `$effect(() => { const n = $bookChanged; … })` reads legacy writable store directly inside `$effect` — can stale-cache in Svelte 5 runes. Derivatives already bridges correctly (line 3536-3537). | Bridge: `let _bookChangedVal = $state(0); bookChanged.subscribe(n => _bookChangedVal = n)` |
| derivatives:106-109 | `onDestroy` registered before `_orderToastTimers` (line 164) is declared. Legal (hoisted) but ordering hazard for future refactors. | Move onDestroy block below the Set declaration. |
| MarketPulse:3849-3868 | `handleKeydown` intercepts `j`/`k` but body is `if (false) {…}` — no-op that silently swallows keys ag-Grid needs for native row navigation. | Delete the entire `if (ev.key==='j'\|\|…)` branch. |

## P2 — Large structural improvements (~1600–1700 total lines extractable)

### derivatives/+page.svelte
| What | Lines | Estimated reduction |
|---|---|---|
| Extract `<CandidateLegRow>` (row body + proxy chip + Lots cell + P&L cells + tooltip) | 4189-4494 | ~250 lines |
| Extract `chainBasketStore.svelte.js` + `<ChainBasketBar>` — full basket state machine | 2350-2550 | ~200 lines |
| Move `_normCdf`/`_probAbove`/`_expectedValueOnCurve`/`_multilegPopOnCurve` → `derivatives/riskMath.js` (pure) | 2174-2237 | ~65 lines |
| Extract `<StrategyRiskCard>` (Greeks + Risk aside) | 4809-4901 | ~80 lines |
| Move `loadRealAccounts`/`addSymbolToWatchlist`/`addOptionToWatchlist` → `derivatives/watchlist.js` | 3131-3217 | ~90 lines |
| Move `_saveCache`/`_loadCache` → `derivatives/pageCache.js` | 3448-3490 | ~45 lines |

### MarketPulse.svelte
| What | Lines | Estimated reduction |
|---|---|---|
| Extract `<AddToPulseModal>` (search + rename + delete + typeahead state machine) | 4306-4517 | ~180 lines |
| Move `mountGrid` + `postSortGroups` + `makeBucketGrid` → `pulseGridSetup.js` | 3372-3623 | ~250 lines |
| Extract `<OptionChainPickerModal>` | 4525-4614 | ~75 lines |
| Extract `pulseOverrides.svelte.js` (groupOrder/detached persist) | 821-890 | ~70 lines |
| Dedupe `<MoverBucket direction=…>` (Winners + Losers are identical modulo label) | 4099-4166 | ~40 lines |

## P3 — Dead code to delete

### derivatives
- Line 144: `showAddPanel` — `$state(false)` never toggled; delete + its 3 `$effect` guards (chainSpot fetch 2631-2661, chain quotes poll 2706-2730, ATM scroll 2788-2812)
- Line 2331: `chainSide` — always overridden by callers, remove
- Line 2335: `chainLots` — never read anywhere
- Line 3129: `watchToast` — written but never rendered
- Lines 3719-3762: `fmtMoney`/`fmtUnbounded`/`fmtNum`/`fmtPct` — replace with `format.js` `aggCompact`, `priceFmt` directly
- Line 4359: IIFE inside `{#if (() => {…})()}` — hoist to `$derived`

### MarketPulse
- Line 199: `agoTick = $state(0)` — never assigned after init
- Line 200/2536: `refreshedAt` — written but never read
- Lines 3880-3914: `subtotals` + `hasSubtotals` — derived but never rendered (~35 lines)
- Lines 3849-3868: dead `if (false)` block inside `handleKeydown`
- Lines 3201/3249-3253: sparkline hardcoded `rgba()` — move to CSS vars (palette compliance)

## Svelte 5 migration gaps
- Both files are largely clean (no `$:`, no `writable()`)
- MarketPulse:691 bridge is the one actionable gap (P1 above)

## Total estimated reduction
- derivatives: ~800-900 lines
- MarketPulse: ~650-700 lines
- **Combined: ~1500-1600 lines**
