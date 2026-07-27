# Plan: Payoff stale-while-revalidate regression + dashboard gap

## Context

### Issue 1 — Payoff shows stale NIFTY chart when ABFRL selected
Commit 539a7e69 introduced stale-while-revalidate: it removed the `!_strategyStale` guard
so the old chart stays visible during an underlying switch (good UX). But the guard was
removed unconditionally, so after the switch completes and the backend returns no strategy
(ABFRL has no options), `strategy` remains the stale NIFTY object forever — payoff never
clears. The `_strategyStale` flag is already defined correctly (`strategy.underlying ≠
selectedUnderlying`) but it's no longer consulted at the render site.

The fix: restore the guard only for the post-load case. While `loading=true`, keep the
old chart (stale-while-revalidate UX preserved). When `loading=false` and `_strategyStale`
is still true (load completed but new underlying produced no strategy), clear to stub.

**File:** `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:4246`

```svelte
<!-- Before (539a7e69 — removed guard entirely) -->
payoff={strategy ? _mergedPayoff : (_clientPayoffStub ?? [])}

<!-- After — stale-while-revalidate only during load window -->
payoff={strategy && (!_strategyStale || loading) ? _mergedPayoff : (_clientPayoffStub ?? [])}
```

`loading` is already reactive state on that page; `_strategyStale` (line 1839) is already
derived. No new state needed.

### Issue 2 — Symbol / expiry dropdown
Explore agent verified the `expiryChoicesForUnderlying` derived (line 1628) and the
`$effect` that clears stale selections (line 1646) are both reactive to `selectedUnderlying`
and working correctly. The apparent "dropdown not updating" is most likely the stale payoff
chart above giving the impression the underlying switch didn't register. Fixing Issue 1
should resolve the perception.

### Issue 3 — Dashboard header-to-card gap still uneven
The previous fix added `margin-top: 0.6rem` to `.dash-row1-split`, which is correct when
`.dash-open-orders` is absent (page-header → cards = 0.6rem ✓). But when open-orders IS
present, in a flex column container margins don't collapse — they add up:
- page-header → open-orders = **0rem** (no margin-top on open-orders)
- open-orders → cards = **1.2rem** (0.6 margin-bottom + 0.6 margin-top of row1-split)

Fix: give `.dash-open-orders` a `margin-top: 0.6rem` and remove its `margin-bottom: 0.6rem`
(redundant since row1-split already has margin-top). Both-absent and both-present cases
then give a uniform 0.6rem gap.

**File:** `frontend/src/routes/(algo)/dashboard/+page.svelte`

```css
/* Before */
.dash-open-orders { margin-bottom: 0.6rem; }         /* no margin-top */

/* After */
.dash-open-orders { margin-top: 0.6rem; }             /* margin-bottom removed */
```

`.dash-row1-split` keeps `margin-top: 0.6rem` (from previous fix) — no change needed there.

### Issue 4 — Only 2 connections
Explore agent found only 2 Kite accounts (ZG0790, ZJ6294) in `secrets.yaml`. No Dhan
accounts configured. "2 connections" is the correct expected count given current config.
No code change needed.

---

## Fix Plan

### Change 1 — Payoff stale guard (derivatives page)

**File:** `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (~line 4246)

Find the `payoff=` prop on `OptionsPayoff` and change:
```svelte
payoff={strategy ? _mergedPayoff : (_clientPayoffStub ?? [])}
```
to:
```svelte
payoff={strategy && (!_strategyStale || loading) ? _mergedPayoff : (_clientPayoffStub ?? [])}
```

### Change 2 — Dashboard open-orders margin

**File:** `frontend/src/routes/(algo)/dashboard/+page.svelte`

Find `.dash-open-orders` CSS rule. Replace `margin-bottom: 0.6rem` with `margin-top: 0.6rem`.

---

## Agents
- backend: skip
- frontend: Changes 1 + 2 (derivatives payoff stale guard + dashboard open-orders margin)
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives+dashboard): clear stale payoff on underlying switch; fix open-orders margin for even header gap

## Done when
- Switching to ABFRL (or any equity with no options) clears the payoff chart after the load completes — no stale NIFTY curve lingers
- Switching to BANKNIFTY/NIFTY still shows old chart during the brief loading window (stale-while-revalidate preserved)
- Dashboard header-to-card gap is visually consistent (0.6rem) whether or not the open-orders strip is visible
- svelte-check 0 errors

## Critical files
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — `payoff=` prop at ~line 4246; `_strategyStale` at ~line 1839; `loading` state
- `frontend/src/routes/(algo)/dashboard/+page.svelte` — `.dash-open-orders` CSS rule
