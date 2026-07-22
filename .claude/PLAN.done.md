# Plan: LogPanel collapse fix + card button consistency + audit P1/P2 fixes

## Context

Four issue clusters identified via collapse bug report, 6-day audit, and card button consistency audit:

1. **LogPanel collapse broken**: `.dash-activity.is-collapsed > .card-body` still has `flex: 1 1 auto; min-height: 8rem` — when `isCollapsed` fires, `.lp-body-wrap[hidden]` removes content but the `.card-body` container keeps its flex sizing, leaving a blank space instead of shrinking to header-only height.

2. **Card button inconsistency (user request)**: Three non-conforming surfaces vs canonical cyan 1.4rem button pattern:
   - `ChartModal.cm-close` — red palette (`c-short`), monospace font; should match refresh button (cyan)
   - `OrderTicket.ot-close` — 1.55rem×1.55rem (too large), generic white border, red hover; should be 1.4rem cyan
   - `LogPanel lp-card-btns-legacy` — slate palette, 0.2rem gap; dead code path (all 9 mounts pass `label=` so legacy is never reached)

3. **Audit P1 — `derivatives/pageLoad.js:216`**: `open_dcv = Number(p.day_change_val || 0) - closed_day_pnl` reads `day_change_val` directly. When `overnight_quantity===0 && pnl!==0` (new intraday position), Kite returns `day_change_val=0` and real value is in `pnl`. This makes the split-row's day P&L wrong by the full position value. Must use `baseDayPnlForPosition(p)` (SSOT in `nav.js`).

4. **Audit P2 — `LogPanel.svelte:839`**: `_applyDateFilter` compares `o.created_at.slice(0,10)` against IST today. `created_at` on algo/paper/sim orders is stored as UTC (`datetime.now(timezone.utc)` in models). Between 00:00–05:30 IST the UTC slice gives yesterday's date, causing that night's orders to be incorrectly filtered out. Fix: for `created_at`, offset by +5.5h before slicing; use `order_timestamp` (already IST) where available as the primary.

## Agents
- backend: skip
- frontend: All four changes below.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Frontend agent brief

### Change A — LogPanel collapse: fix `.dash-activity.is-collapsed > .card-body` height
File: `frontend/src/routes/(algo)/dashboard/+page.svelte`

Find the `.dash-activity > .card-body` CSS block (around line 2865). Add a collapsed override immediately after:

```css
/* BEFORE — only this block exists */
.dash-activity > .card-body {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 8rem;
  max-height: 33vh;
}

/* AFTER — add the override block */
.dash-activity > .card-body {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 8rem;
  max-height: 33vh;
}
.dash-activity.is-collapsed > .card-body {
  flex: 0 0 auto;
  min-height: 0;
  max-height: none;
}
```

### Change B — Card button consistency
Three sub-changes across three files. All buttons must conform to canonical: `1.4rem × 1.4rem`, `border-radius: 3px`, cyan palette (`var(--algo-cyan-bg)` / `var(--algo-cyan-border)` / `var(--c-info)`), cyan hover.

**B1 — ChartModal.cm-close** (`frontend/src/lib/ChartModal.svelte`):

Find `.cm-close` CSS block. Replace red styling with cyan to match `.cm-refresh-btn`:
```css
/* BEFORE */
.cm-close {
  width: 1.4rem;
  height: 1.4rem;
  ...
  border: 1px solid rgba(248, 113, 113, 0.35);
  border-radius: 3px;
  color: var(--c-short);
  font-size: var(--fs-xl);
  ...
  font-family: monospace;
  ...
}
.cm-close:hover {
  background: rgba(248, 113, 113, 0.15);
}

/* AFTER */
.cm-close {
  width: 1.4rem;
  height: 1.4rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--algo-cyan-bg, rgba(34,211,238,0.08));
  border: 1px solid var(--algo-cyan-border, rgba(34,211,238,0.30));
  border-radius: 3px;
  color: var(--c-info, #22d3ee);
  font-size: var(--fs-xl);
  line-height: 1;
  padding: 0;
  cursor: pointer;
  transition: background 0.08s, border-color 0.08s;
  pointer-events: auto;
  position: relative;
  z-index: 2;
  flex-shrink: 0;
}
.cm-close:hover {
  background: rgba(34,211,238,0.14);
  border-color: rgba(34,211,238,0.65);
}
```
Remove `font-family: monospace` — it causes glyph rendering inconsistency.

**B2 — OrderTicket.ot-close** (`frontend/src/lib/order/OrderTicket.svelte`):

Find `.ot-close` CSS block. Fix size from 1.55rem to 1.4rem, change border/color to cyan, change hover to cyan:
```css
/* BEFORE */
.ot-close {
  width: 1.55rem;
  height: 1.55rem;
  background: transparent;
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 3px;
  color: var(--algo-slate);
  font-size: var(--fs-lg);
  ...
}
.ot-close:hover {
  border-color: var(--c-short);
  color: var(--c-short);
}

/* AFTER */
.ot-close {
  width: 1.4rem;
  height: 1.4rem;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--algo-cyan-bg, rgba(34,211,238,0.08));
  border: 1px solid var(--algo-cyan-border, rgba(34,211,238,0.30));
  border-radius: 3px;
  color: var(--c-info, #22d3ee);
  font-size: var(--fs-xl);
  line-height: 1;
  padding: 0;
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.08s, border-color 0.08s;
}
.ot-close:hover {
  background: rgba(34,211,238,0.14);
  border-color: rgba(34,211,238,0.65);
}
```

**B3 — LogPanel lp-card-btns-legacy removal** (`frontend/src/lib/LogPanel.svelte`):

The legacy path is unreachable: all 9 `ActivityLogSurface` / `LogPanel` mounts in the codebase pass `label="Log"`, which triggers the modern CardHeader path. The `{:else}` branch (`lp-card-btns-legacy`) never fires.

Remove from the template: the entire `{:else}` block inside `{#if label}...{:else}...{/if}` header conditional — this includes the `<header class="lp-header lp-header-legacy">` element and everything inside it.

Remove from CSS: all rules for `.lp-card-btns-legacy`, `.lp-card-btn`, `.lp-card-btn:hover`, `.lp-header-legacy`, and any CSS blocks exclusively used by the legacy path. Look for the comment "DEPRECATED: lp-card-btns-legacy".

**Verify** after removal: if `{#if label}` block is the only branch remaining, simplify to just the modern header block (remove the `{#if label}` conditional wrapper entirely, keeping only its inner content).

### Change C — P1 fix: `derivatives/pageLoad.js` open_dcv uses wrong day P&L base
File: `frontend/src/lib/derivatives/pageLoad.js`

Around line 216, find:
```javascript
const open_dcv = Number(p.day_change_val || 0) - closed_day_pnl;
```

Change to use `baseDayPnlForPosition` (SSOT from nav.js):
```javascript
const open_dcv = baseDayPnlForPosition(p) - closed_day_pnl;
```

Also add the import at the top of the file if not already present:
```javascript
import { baseDayPnlForPosition } from '$lib/data/nav.js';
```

Check whether `baseDayPnlForPosition` is already imported — if so, just change the usage.

### Change D — P2 fix: LogPanel date filter UTC vs IST bug
File: `frontend/src/lib/LogPanel.svelte`

Find `_applyDateFilter` function (around line 839). The current code:
```javascript
function _applyDateFilter(rows) {
  const istNow = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
  const today = istNow.toISOString().slice(0, 10);
  return rows.filter(o => {
    const ts = (o.created_at || o.order_timestamp || '').slice(0, 10);
    const term = _TERMINAL_STATUSES.has((o.status || '').toUpperCase());
    return !(ts && ts !== today && term);
  });
}
```

The bug: `o.created_at` is stored as UTC by the backend; `.slice(0,10)` gives wrong date between 00:00–05:30 IST.

Fix: prefer `order_timestamp` (already IST) for the date comparison; only fall back to `created_at` with UTC→IST offset applied:
```javascript
function _applyDateFilter(rows) {
  const istNow = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
  const today = istNow.toISOString().slice(0, 10);
  return rows.filter(o => {
    // order_timestamp is already IST; created_at is UTC — offset before slicing
    let ts = '';
    if (o.order_timestamp) {
      ts = String(o.order_timestamp).slice(0, 10);
    } else if (o.created_at) {
      ts = new Date(new Date(o.created_at).getTime() + 5.5 * 60 * 60 * 1000)
             .toISOString().slice(0, 10);
    }
    const term = _TERMINAL_STATUSES.has((o.status || '').toUpperCase());
    return !(ts && ts !== today && term);
  });
}
```

### After all changes, run svelte-check:
```bash
cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -10
```

Fix any errors before reporting done.

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): collapse height, cyan close buttons, legacy path removal, day-pnl SSOT, UTC date filter

## Done when
- Dashboard LogPanel collapses to header-only height (no blank space below header)
- ChartModal × button is cyan (matches refresh button beside it)
- OrderTicket × button is 1.4rem cyan (matches refresh button beside it)
- LogPanel legacy `lp-card-btns-legacy` path removed from template + CSS
- `derivatives/pageLoad.js` `open_dcv` uses `baseDayPnlForPosition(p)` not raw `day_change_val`
- LogPanel date filter correctly handles UTC `created_at` timestamps
- svelte-check: 0 errors
