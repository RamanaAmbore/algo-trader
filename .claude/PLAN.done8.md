# Plan: Fix holdings TOTAL row Day P&L + dashboard stale reads + doc sync

## Context

Audit found two code defects plus documentation gaps from recent store changes:

**1. Critical regression (introduced by recent fix)**
`MarketPulse.svelte:3616` — `p.node?.rowPinned` guard returns `positionsDayPnlStore.total`
for ALL pinned rows. `holdingsColDefs` is built from `rightColDefs.filter(...)` AFTER the
column mutation, sharing the same object. Holdings TOTAL row shows positions total. Wrong.

**2. High-priority stale reads in dashboard**
`dashboard/+page.svelte:124-131` and `734-739` compute holdings day P&L as
`(last_price − close_price) × qty` / fallback to `day_change_val`, bypassing
`holdingsDayPnlStore` stale-close rescue and epsilon guard.

**3. Documentation gaps from recent commits**
- `positionsDayPnlStore` now exports `.byAccount` — not in any spec/guide
- Pulse positions total row Day P&L reads from store — not documented
- NavBreakdown P/M/C/H total row `totals-row` styling — not documented
- `portfolioStore.holdings` pulse-override preserves `chgPctByKey` — not documented

## Files to change

| File | Change |
|------|--------|
| `frontend/src/lib/MarketPulse.svelte` | Scope pinned-row guard to `_majorGroup === 'positions'` |
| `frontend/src/routes/(algo)/dashboard/+page.svelte` | Replace inline `(last_price−close_price)×qty` with `holdingsDayPnlStore.byKey[sym]` |
| `docs/specs/NAVSTRIP_SPEC.md` | Add per-account P-slot reads `positionsDayPnlStore.byAccount`; add `totals-row` styling note |
| `docs/specs/PULSE_SPEC.md` | Add `chgPctByKey` field doc; pulse-override preservation note; pinned-row Day P&L source |
| `docs/DESIGN_GUIDE.md` | Add `.byAccount` to positionsDayPnlStore API; add `portfolioStore.positions.byAccount` section; add holdings chgPctByKey preservation note |
| `docs/guides/USER_GUIDE.md` | Clarify NavBreakdown per-account rows use live store; note holdings Chg% fixed |

## Detailed changes

### 1. `MarketPulse.svelte` — scope pinned-row guard to positions only

**Find (lines ~3613–3620):**
```javascript
rightColDefs[_dayPnlColIdx] = {
  ..._origDayPnlCol,
  valueGetter: p => {
    if (p.node?.rowPinned) return positionsDayPnlStore.total ?? p.data?.day_pnl;
    const sym = String(p.data?.tradingsymbol || '').toUpperCase();
    return positionsDerivedStore.get(sym).day_pnl ?? p.data?.day_pnl;
  },
};
```

**Replace with:**
```javascript
rightColDefs[_dayPnlColIdx] = {
  ..._origDayPnlCol,
  valueGetter: p => {
    if (p.node?.rowPinned && p.data?._majorGroup === 'positions')
      return positionsDayPnlStore.total ?? p.data?.day_pnl;
    if (p.node?.rowPinned) return p.data?.day_pnl;
    const sym = String(p.data?.tradingsymbol || '').toUpperCase();
    return positionsDerivedStore.get(sym).day_pnl ?? p.data?.day_pnl;
  },
};
```

`_majorGroup` is set by `_totalsRowFor(rows, major, label)` — positions total row has
`_majorGroup: 'positions'`. Holdings pinned row falls back to `p.data?.day_pnl`.

### 2. `dashboard/+page.svelte` — use holdingsDayPnlStore

**Lines ~124-131** (`_todayPnl` chart overlay) and **~734-739** (`_holdingsFor()` W/L card):
Replace `(h.last_price − h.close_price) × h.quantity` / `h.day_change_val` with
`holdingsDayPnlStore.byKey[sym] ?? (Number(h.day_change_val) || 0)`.

Check whether `holdingsDayPnlStore` is already imported; if not, add:
`import { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js'`

Use `String(h.tradingsymbol || h.symbol || '').toUpperCase()` as the key.

### 3. Doc updates (dispatched as parallel doc agents)

**NAVSTRIP_SPEC.md** — NavBreakdown P-slot section:
- Add: per-account rows read `positionsDayPnlStore.byAccount[acct]` (live-LTP-aware), not `baseDayPnlForPosition` on stale broker rows
- Add: P/M/C/H grids apply `getRowClass` returning `totals-row` CSS class for `account === 'TOTAL'` rows (amber background, matches PerformancePage/derivatives legs)

**PULSE_SPEC.md** — §11.2 Holdings SSOT + §13 Day P&L column:
- Add `chgPctByKey` to the holdings store API field list
- Add note: pulse-override branch preserves `chgPctByKey` from base (commit 869e4b78)
- Update Day P&L column: pinned row uses `positionsDayPnlStore.total` (not stale broker sum)

**DESIGN_GUIDE.md** — §18 positionsDayPnlStore + portfolioStore:
- Update exports list from `{ total, byKey }` to `{ total, byKey, byAccount }`
- Add `portfolioStore.positions.byAccount` structure (per-account + TOTAL scalar)
- Add consumers: NavBreakdown P-slot, MarketPulse total row
- Add: `portfolioStore.holdings` pulse-override now preserves `chgPctByKey`

**USER_GUIDE.md** — NavBreakdown + Pulse sections:
- Clarify: NavBreakdown per-account rows use live-LTP-aware store (matches NavStrip pill)
- Add: holdings Chg% in Pulse now works reliably after filter updates

## Agents
- frontend: apply MarketPulse + dashboard code fixes
- frontend-test: update `dayPnlValueGetter.test.js` — add test that holdings pinned row returns `p.data.day_pnl`, not `positionsDayPnlStore.total`
- doc (NAVSTRIP_SPEC): update NavBreakdown sections per above
- doc (PULSE_SPEC): update §11.2 and Day P&L column sections
- doc (DESIGN_GUIDE): update §18 positionsDayPnlStore + portfolioStore.holdings
- doc (USER_GUIDE): update NavBreakdown + Pulse descriptions

Frontend + frontend-test run in parallel. All four doc agents run in parallel (separate from frontend).
After doc agents complete: regenerate DESIGN_GUIDE PDF.

## Tests
- pytest: no
- svelte-check: yes
- vitest: yes (update dayPnlValueGetter.test.js)
- playwright: no

## Commit message
fix(MarketPulse,dashboard): scope positions-total guard to positions grid; replace inline holdings day_pnl in dashboard with holdingsDayPnlStore

## Done when
- Pulse holdings TOTAL row Day P&L shows holdings total (not positions total)
- Dashboard chart overlay + W/L card show holdingsDayPnlStore values
- NAVSTRIP_SPEC, PULSE_SPEC, DESIGN_GUIDE, USER_GUIDE updated
- DESIGN_GUIDE PDF regenerated
- svelte-check 0 errors; vitest 0 failures
