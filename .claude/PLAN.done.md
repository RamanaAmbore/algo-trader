# Plan: AlgoTimestamp + RefreshButton + market-open-nse ntfy — timestamp accuracy + Android click + agent fire_at_time sync

## Current state (verified)

### `AlgoTimestamp.svelte`
```js
let _lastRefresh = $state(0);                     // initialized to 0 always
$effect(() => { _lastRefresh = $lastRefreshAt; }) // bridge fires post-render
let _refreshTs = $derived(_lastRefresh ? formatDualTz(new Date(_lastRefresh)) : null);
function _toggle() { if (_refreshTs) _showRefresh = !_showRefresh; }
$effect(() => { if (!_refreshTs && _showRefresh) _showRefresh = false; });
```
Template uses `{$nowStamp}` (60 s writable store from stores.js).  
CSS: `@media (min-width: 641px) { pointer-events: none }` for desktop  
CSS: `@media (max-width: 640px)` for mobile toggle with `.ats-mobile-hide`  
Button only has `onclick` — no `ontouchend`.

### `RefreshButton.svelte`
```js
let _prevLoading = false;
$effect(() => {
  if (_prevLoading && !loading) lastRefreshAt.set(Date.now());
  _prevLoading = loading;
});
```
Stamps `lastRefreshAt` ONLY on `loading: true→false`. No stamp on `_refiring: true→false`.

---

## Bugs

### Bug 1 — Android portrait: onclick doesn't fire (ats-group in position:fixed parent)
`onclick` relies on browser click-synthesis from touch events. Android Chrome cancels this synthesis when a touch moves 1–2 px on a `position:fixed` container (`.page-header` is `position:fixed`). The CSS `:active` state still fires on `touchstart` (visual animation), but the synthesised `click` never arrives.  
**Fix:** add `ontouchend` that calls `e.preventDefault()` (cancels ghost click) then `_toggle()`.

### Bug 2 — refresh timestamp > current timestamp
`$nowStamp` ticks every 60 s from page-load time (not aligned to minute boundary). If a data refresh lands after `$nowStamp` just snapped its minute (e.g. now shows ":29" and refresh happens at ":31" in the next minute), `_refreshTs` shows ":31 IST" while `$nowStamp` still shows ":29 IST" — refresh appears to be in the future for up to ~60 s.  
**Fix:** Replace `{$nowStamp}` with a local `_nowEpoch` that ticks every 30 s AND snaps to `Date.now()` whenever a refresh arrives, guaranteeing current ≥ refresh.

### Bug 3 — initial flash: refresh timestamp absent on first render
`let _lastRefresh = $state(0)` initializes to 0. The `$effect` bridge fires only after the first render. If `lastRefreshAt` is already set (from a prior page visit in the same session), `_refreshTs` is null for one render cycle → `| HH:MM IST` blinks in.  
**Fix:** Initialize `_lastRefresh` with the current store value synchronously using `get(lastRefreshAt)`.

### Bug 4 — refire path never stamps lastRefreshAt ← CONFIRMED (09:20 IST symptom)
`postHibernationRefiring` goes `true` when the tab returns from background, causing the RefreshButton to spin (via `_refiring`). Data is actually refreshed during this cycle. But `loading` never transitions (`true→false`) during a refire — only `_refiring` does. The `_prevLoading` effect never fires → `lastRefreshAt` is never stamped → timestamp freezes at the last genuine load (e.g. 09:20 at page-open), even as the spinner animates on every tab-return.  
**Fix:** Add a parallel `_prevRefiring` effect in RefreshButton that stamps `lastRefreshAt` when `_refiring` goes `true→false`.

### Bug 5 — market-open-nse never fires at 09:15 on ntfy ← CONFIRMED
`_ae_build_agent_row` in `agent_engine.py` does NOT write `fire_at_time` when seeding the agent row. `_ae_sync_existing_builtin` also does NOT sync it. Result: the DB column is NULL. `_cycle_outside_fire_at` sees NULL → treats the agent as schedule-only (no time window) → agent fires on the very first background cycle (random time) → enters 22-hour cooldown → next fire is 22h later at that same random time. Never fires at 09:15.  
ntfy infrastructure is confirmed working (order alerts arrive). The issue is purely the NULL `fire_at_time` in the agent row.  
**Fix:**  
1. In `_ae_build_agent_row` (agent_engine.py ~line 1216): add `fire_at_time=agent_def.get("fire_at_time")`.  
2. In `_ae_sync_existing_builtin` (agent_engine.py ~line 1184): add syncing of `fire_at_time` field alongside `long_name`, `schedule`, etc.  
On next restart, `seed_agents()` will call `_ae_sync_existing_builtin` for the existing market-open-nse row and write "09:15" to the DB. No manual SQL needed.

---

## Files and changes

### `frontend/src/lib/AlgoTimestamp.svelte`

**Script:**
```js
import { get } from 'svelte/store';
import { browser } from '$app/environment';
import { lastRefreshAt, formatDualTz } from '$lib/stores';
// remove: nowStamp import

let _lastRefresh = $state(browser ? get(lastRefreshAt) : 0);  // Bug 3: sync init
let _showRefresh = $state(false);

let _nowEpoch = $state(browser ? Date.now() : 0);             // Bug 2: local clock

$effect(() => {
  const lr = $lastRefreshAt;
  _lastRefresh = lr;
  if (lr > _nowEpoch) _nowEpoch = Date.now();                 // Bug 2: snap forward
});

$effect(() => {                                                 // Bug 2: 30 s tick
  const id = setInterval(() => { _nowEpoch = Date.now(); }, 30_000);
  return () => clearInterval(id);
});

let _nowTs     = $derived(_nowEpoch ? formatDualTz(new Date(_nowEpoch)) : '');
let _refreshTs = $derived(_lastRefresh ? formatDualTz(new Date(_lastRefresh)) : null);

function _handleTap(e) { e.preventDefault(); _toggle(); }     // Bug 1: ontouchend
function _toggle() { if (_refreshTs) _showRefresh = !_showRefresh; }
$effect(() => { if (!_refreshTs && _showRefresh) _showRefresh = false; });
```

**Template:** replace `{$nowStamp}` → `{_nowTs}`, add `ontouchend={_handleTap}`:
```svelte
<button
  type="button"
  class="ats-group"
  ontouchend={_handleTap}
  onclick={_toggle}
  onkeydown={(e) => e.key === 'Enter' && _toggle()}
  style="touch-action: manipulation; user-select: none; -webkit-tap-highlight-color: transparent;">
  <span class="ats-now" class:ats-mobile-hide={_showRefresh}>{_nowTs}</span>
  {#if _refreshTs}
    <span class="ats-sep" aria-hidden="true">|</span>
    <span class="ats-refresh" class:ats-mobile-hide={!_showRefresh}>{_refreshTs}</span>
  {/if}
</button>
```

**CSS:** Replace `max-width: 640px` breakpoint with capability-based detection (touch devices, any viewport width):
```css
/* Default: desktop — show both, no interaction */
.ats-group { cursor: default; pointer-events: none; ... }

/* Touch devices: tap-to-toggle regardless of viewport width */
@media (hover: none) and (pointer: coarse) {
  .ats-group { cursor: pointer; pointer-events: auto; font-size: 0.6rem; min-height: 1.8rem; }
  .ats-sep { display: none; }
  .ats-mobile-hide { display: none; }
}
```
Remove the `min-width: 641px` block (replaced by default cursor:default/pointer-events:none on `.ats-group`).

### `frontend/src/lib/RefreshButton.svelte`

Add after the existing `_prevLoading` effect (line ~376):
```js
// Bug 4: stamp lastRefreshAt when refire completes — the _prevLoading
// effect only fires on the loading prop; _refiring never touches loading.
let _prevRefiring = false;
$effect(() => {
  if (_prevRefiring && !_refiring) lastRefreshAt.set(Date.now());
  _prevRefiring = _refiring;
});
```

### `backend/api/algo/agent_engine.py` — Bug 5 fix

**`_ae_build_agent_row`** (when creating a new agent row for the first time):
```python
# Add alongside schedule, tier, topic, etc.:
fire_at_time=agent_def.get("fire_at_time"),
```

**`_ae_sync_existing_builtin`** (when syncing an existing agent row on startup):
```python
# Add alongside the existing synced fields (long_name, schedule, tier, topic, status, events):
if agent.fire_at_time != agent_def.get("fire_at_time"):
    agent.fire_at_time = agent_def.get("fire_at_time")
    dirty = True
```

---

## Agents
- frontend: implement AlgoTimestamp.svelte + RefreshButton.svelte changes (Bugs 1–4 above)
- backend: fix `_ae_build_agent_row` + `_ae_sync_existing_builtin` in `backend/api/algo/agent_engine.py` to write/sync `fire_at_time` field (Bug 5)

## Tests
- pytest: yes (backend — verify seed_agents sets fire_at_time on new + existing rows)
- svelte-check: yes
- playwright: no

## Commit message
fix(ui,agents): timestamp Android tap + refire stamp + clock accuracy + market-open-nse fire_at_time sync

## Done when
1. Android portrait tap toggles between current time and refresh time
2. Post-hibernation refire (tab return) updates the refresh timestamp when spinner completes
3. Refresh timestamp never shows a time later than the displayed current time
4. Refresh timestamp appears immediately on page nav when lastRefreshAt is already set
5. market-open-nse agent has fire_at_time="09:15" in DB after next restart/seed
6. svelte-check 0 errors, pytest green
