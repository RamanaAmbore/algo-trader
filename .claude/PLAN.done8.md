# Plan: Fix NavBreakdown P-slot Day P&L — use live-LTP-aware portfolioStore

## Context

NavBreakdown's P-slot (positions breakdown by account) computes `day_pnl` by calling
`baseDayPnlForPosition(p)` on each raw broker row and summing per account
(`NavBreakdown.svelte:219`). This reads stale `day_change_val` from the broker payload,
missing SSE live-tick rescues and MCX session-gap corrections.

MarketPulse and Derivatives grids read from `positionsDerivedStore` → `portfolioStore`,
which calls `livePositionDayPnl()` using live LTP from symbolStore — giving correct values.
The H-slot in NavBreakdown is already correct: it reads
`holdingsDayPnlStore.byAccount[acct]` (pre-computed in portfolioStore). The P-slot
needs the same treatment. NavStrip popup windows use `<NavBreakdown>` directly, so
fixing NavBreakdown fixes popups and all grids in one shot.

## Root cause
`portfolioStore.positions` has `total` and `byKey` (symbol-keyed) but **no `byAccount`**.
Holdings has `byAccount` (`portfolioStore.svelte.js:267`). We add the same to positions,
then expose it via `positionsDayPnlStore.byAccount`, and read it in NavBreakdown.

## Files to change

| File | Change |
|------|--------|
| `frontend/src/lib/data/portfolioStore.svelte.js` | Add `posByAccount` accumulation in positions loop (~line 170); expose as `positions.byAccount` |
| `frontend/src/lib/data/positionsDayPnlStore.svelte.js` | Add `get byAccount()` delegating to `portfolioStore.positions.byAccount ?? {}` |
| `frontend/src/lib/NavBreakdown.svelte` | Line 219: replace `baseDayPnlForPosition` reduce with `positionsDayPnlStore.byAccount[acct.toUpperCase()] ?? 0` |

## Detailed changes

### 1. portfolioStore.svelte.js

Before the positions aggregation loop, add:
```javascript
const posByAccount = {};
```

Inside the loop (after `posTotal.day_pnl += p._day_pnl`):
```javascript
const _acct = String(p.account || '').toUpperCase();
if (_acct) posByAccount[_acct] = (posByAccount[_acct] ?? 0) + p._day_pnl;
```

After the loop:
```javascript
posByAccount['TOTAL'] = posTotal.day_pnl;
```

Include in return value:
```javascript
return { posTotal, posByKey, posByAccount, byRoot, byRootPos, expiryByAcct };
```

Expose in the exported `positions` getter (~line 349):
```javascript
byAccount: _posAgg.posByAccount,
```

Update `_EMPTY_POSITIONS` sentinel (~line 333):
```javascript
byAccount: {},
```

### 2. positionsDayPnlStore.svelte.js

Add getter after existing getters:
```javascript
get byAccount() { return portfolioStore.positions.byAccount ?? {}; }
```

### 3. NavBreakdown.svelte

Replace line 219 only:
```javascript
// BEFORE:
const dayPnl = rows.reduce((s, p) => s + baseDayPnlForPosition(p), 0);
// AFTER:
const dayPnl = positionsDayPnlStore.byAccount[acct.toUpperCase()] ?? 0;
```

`rows` is still used on line 220 for `lifetimePnl` — do not remove the filter.
Remove the `baseDayPnlForPosition` import if unused after this change.

## Agents
- frontend: apply all three file changes above
- backend-test: skip
- frontend-test: add Vitest test — `portfolioStore.positions.byAccount` aggregates
  per-account `_day_pnl`; `positionsDayPnlStore.byAccount` delegates correctly

## Tests
- pytest: no
- svelte-check: yes
- vitest: yes (new + existing)
- playwright: no (visual, can't automate)

## Commit message
fix(NavBreakdown): P-slot day_pnl — read positionsDayPnlStore.byAccount (live-LTP-aware) instead of baseDayPnlForPosition on stale broker rows

## Done when
- `positionsDayPnlStore.byAccount[acct]` exists and equals portfolioStore computed value
- NavBreakdown P-slot per-account Day P&L matches Pulse positions values
- NavStrip popup P breakdown shows same values as the dashboard grid
- svelte-check 0 errors; vitest 0 failures
