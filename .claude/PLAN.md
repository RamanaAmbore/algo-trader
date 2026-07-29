# Plan: Fix closed/expired positions showing in derivatives legs and generating false alerts

## Context
Two bugs causing closed IDFCFIRST positions to appear in the derivatives legs grid and trigger false "Exp close" alerts:

**Bug 1** — `pageLoad.js:308`: When an F&O instrument expires, Kite removes it from the instrument master. `getInstrument(sym)` returns `null`, so `_expiry` is `undefined` and the guard `if (_expiry && _expiry < todayIST()) continue` never fires. The expired/closed position stays in the legs.

**Bug 2** — `derivativesMath.js:77`: `if (qty === 0 && !expFilter.length) continue` — when an expiry filter is active, qty=0 (closed/expired) positions pass through `annotateOptionCandidates` into `computeExpiryBands`, incorrectly flagging them as 'close' band → drives the amber "Exp close" badge count.

## Task
Fix both bugs so that:
- Expired instruments (not in master) never appear in legs
- Closed (qty=0) positions never appear in expiry-close analysis or alerts
- Closed equity holdings (qty=0) are also filtered from legs

## Agents
- frontend: Fix `frontend/src/lib/derivatives/pageLoad.js` and `frontend/src/lib/data/derivativesMath.js` as described below.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Changes

### `frontend/src/lib/derivatives/pageLoad.js` — F&O positions loop (lines 307-310)

Replace:
```javascript
// Skip contracts where the expiry date has already passed.
const _expiry = getInstrument(sym)?.x;
if (_expiry && _expiry < todayIST()) continue;
```
With:
```javascript
// Skip instruments no longer in master (expired and removed by Kite).
const _inst = getInstrument(sym);
if (!_inst) continue;
// Skip contracts where the expiry date has already passed.
if (_inst.x && _inst.x < todayIST()) continue;
```

Also skip qty=0 equity holdings (lines 314-319) and proxy hedges (lines 322-329):
- Add `if (!Number(h.qty || 0)) continue;` before the `real.push` in both loops

### `frontend/src/lib/derivatives/pageLoad.js` — drafts loop (lines 339-341)

Replace:
```javascript
const _dExpiry = getInstrument(sym)?.x;
if (_dExpiry && _dExpiry < todayIST()) continue;
```
With:
```javascript
const _dInst = getInstrument(sym);
if (!_dInst) continue;
if (_dInst.x && _dInst.x < todayIST()) continue;
```

### `frontend/src/lib/data/derivativesMath.js` — annotateOptionCandidates (line 77)

Change:
```javascript
if (qty === 0 && !expFilter.length) continue;
```
To:
```javascript
if (qty === 0) continue;
```

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

Frontend agent must add/update Vitest unit tests in `frontend/src/lib/__tests__/` covering:
1. `buildCandidatePositions` skips F&O positions when instrument not in master (returns null)
2. `buildCandidatePositions` skips equity holdings with qty=0
3. `annotateOptionCandidates` skips qty=0 positions even when expFilter is non-empty

## Commit message
fix(derivatives): skip expired instruments (not in master) and closed legs from expiry alerts

## Done when
- No closed/expired positions appear in the legs grid
- `expiryCloseTotal` badge is 0 for closed positions even with an expiry filter active
- svelte-check: 0 errors
