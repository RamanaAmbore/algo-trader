# Plan: Fix equity option payoff parallel lines + conn grid row height

## Task

Two frontend bugs:

**Bug 1: Equity option payoff shows parallel lines (regression from cdbf39af + 539a7e69)**

Root cause chain:
1. `539a7e69` added stale-while-revalidate: old `strategy` persists during underlying switch
2. `cdbf39af` added `spot: liveSpot ?? null` to `fetchStrategyAnalytics`
3. When switching from NIFTY → RELIANCE CE: `strategy.underlying = "NIFTY"`, `strategy.spot = 25000`
4. `liveSpot` falls back to `strategy?.spot = 25000` (RELIANCE not in SSE/batchQuote on derivatives page)
5. Backend receives `spot: 25000` for RELIANCE CE (strike ~3200) → grid spans 22500–27500 → strike deep OTM → both today_value and expiry_value are flat/linear across the grid
6. With different frontend offsets (chartPnlOffset vs expiryPnlOffset) applied → **parallel lines**
7. New `strategy.spot = 25000` → `liveSpot` stays at 25000 → circular, never fixes itself

Secondary bug: `_stratLastKey = legsKey` on error (line ~3532) silently suppresses retries for equity option fetches that failed.

**Fix A — `liveSpot` underlying guard** (in `+page.svelte`, `liveSpot` derived):
Replace the unconditional final fallback `return strategy?.spot` with a conditional that only uses it when `strategy.underlying` matches `selectedUnderlying`. Otherwise return `undefined` → `undefined ?? null = null` → backend computes spot from broker (correct equity stock price).

```js
// BEFORE (buggy):
return strategy?.spot;

// AFTER:
const stratUnd = String(strategy?.underlying || '').toUpperCase();
return stratUnd && stratUnd === selectedUnderlying ? strategy?.spot : undefined;
```

**Fix B — don't memo legsKey on error** (in `+page.svelte`, catch block ~line 3532):
Remove `_stratLastKey = legsKey;` from the error path so failed fetches can retry on the next interval.

**Bug 2: Conn grid row height too small**
`.lp-conn-row` in `LogPanel.svelte` has `padding: 0.1rem 0.5rem; align-items: baseline` (0.2rem vertical).
System tab `.log-row` uses `padding: 0.28rem 0; align-items: center` (0.56rem vertical).
Fix: change `.lp-conn-row` to `padding: 0.28rem 0.5rem; align-items: center`.

## Agents

- backend: skip
- frontend: Three fixes. (1) In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, find `liveSpot` derived (around line 1725-1755). The final fallback line `return strategy?.spot` must be changed to only return `strategy?.spot` when `strategy.underlying` (uppercased) equals `selectedUnderlying`; otherwise return `undefined`. Exact code: `const stratUnd = String(strategy?.underlying || '').toUpperCase(); return stratUnd && stratUnd === selectedUnderlying ? strategy?.spot : undefined;`. (2) In the same file, find the catch block around line 3532 that sets `_stratLastKey = legsKey;` on error — remove that line so failed equity option fetches are not memoized (they should retry). (3) In `frontend/src/lib/LogPanel.svelte` find `.lp-conn-row` CSS (near line 2354) and change `padding: 0.1rem 0.5rem` → `padding: 0.28rem 0.5rem` and `align-items: baseline` → `align-items: center`.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(derivatives): equity option payoff parallel lines + conn grid row height

## Done when

- Switching from NIFTY options to RELIANCE CE (or any equity stock option) shows a correct non-linear option payoff curve, not two parallel lines
- `.lp-conn-row` rows in the conn tab have the same vertical padding/height as system tab rows
- svelte-check exits 0
