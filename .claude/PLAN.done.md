# Plan: 4 follow-up UI fixes — mobile header height, timestamp toggle, conn borders, NavBreakdown slot-specific

## Context
After the 5-bug fix ship (commit `fce75119` remediation), 4 regressions/gaps remain:
1. Mobile page-header is now 2.5rem tall (over-corrected from the old 1.8rem); user wants smaller.
2. Timestamp toggle on mobile is still awkward — the delegation handler excludes the `.algo-ts` zone so clicking the timestamp fires the button's own onclick AND the delegated handler doesn't fire; user says "it's a simple change."
3. Conn tab rows have no bottom border — other log tab grids (event/order rows) have `border-bottom: 1px solid rgba(126,151,184,0.10)`; conn rows are visually inconsistent.
4. NavBreakdown popup shows the same 4-column NAV table for every slot (P/M/C/H); user wants slot-specific data whose TOTAL matches the NavStrip pill value for that slot.

## Agents
- frontend: All 4 fixes (layout.svelte + AlgoTimestamp.svelte + LogPanel.svelte + NavBreakdown.svelte + PositionStrip.svelte).
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Update/add e2e spec covering the 4 fixed surfaces.

## Fixes

### Fix 1 · layout.svelte — Revert mobile page-header to 1.8rem
**File**: `frontend/src/routes/(algo)/+layout.svelte`

Mobile `@media (max-width: 640px)` block currently (line ~2138):
```css
:global(.page-header) {
  padding: 0.1rem 0.4rem;
  min-height: 2.5rem;   /* ← revert to 1.8rem */
}
.algo-content { padding-top: calc(3rem + 2.5rem); }
:global(.algo-viewport:has(.ps-strip)) .algo-content {
  padding-top: calc(3rem + 1.5rem + 2.5rem);
}
```

Change to:
```css
:global(.page-header) {
  padding: 0.1rem 0.4rem;
  min-height: 1.8rem;   /* restored; no overflow-x:hidden so no clipping */
}
.algo-content { padding-top: calc(3rem + 1.8rem); }
:global(.algo-viewport:has(.ps-strip)) .algo-content {
  padding-top: calc(3rem + 1.5rem + 1.8rem);
}
```

Note: `overflow-x: hidden` was already removed in the previous fix — do NOT re-add it. Without it, the 1.8rem header won't clip the button touch target.

---

### Fix 2 · layout.svelte + AlgoTimestamp.svelte — Remove page-header delegated click zone
**Files**:
- `frontend/src/routes/(algo)/+layout.svelte` (delegated handler, line ~937)
- `frontend/src/lib/AlgoTimestamp.svelte` (toggle-ts listener, lines ~22-24)

**What exists**: Commit `fce75119` added an `onclick` on `.algo-viewport` that fires a `toggle-ts` custom event whenever the user taps empty space inside `.page-header`. AlgoTimestamp listens for this via `window.addEventListener('toggle-ts', ...)`. User does not want the header area to be a click zone — only the timestamp button itself should toggle.

**Fix**:

**layout.svelte** — remove the entire `onclick` from `.algo-viewport` and the associated svelte-ignore comment:
```svelte
<!-- Before: -->
<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
<div class="algo-viewport card-theme-dark"
  onclick={(e) => {
    const t = /** @type {HTMLElement} */ (e.target);
    if (t.closest('.page-header') && !t.closest('.page-header-actions') && !t.closest('.algo-ts')) {
      window.dispatchEvent(new CustomEvent('toggle-ts'));
    }
  }}>

<!-- After: -->
<div class="algo-viewport card-theme-dark">
```

**AlgoTimestamp.svelte** — remove the `toggle-ts` event listener (no longer fired by anything) and the `onMount`/`onDestroy` pair that wire it up. Keep `onclick={_toggle}` on the button as the sole toggle path:
```svelte
// Remove:
function _onToggleTs() { _toggle(); }
onMount(() => { window.addEventListener('toggle-ts', _onToggleTs); });
onDestroy(() => { window.removeEventListener('toggle-ts', _onToggleTs); });
```
Remove `onMount`/`onDestroy` imports if no longer used by anything else in the file.

Also update `.ats-group` mobile `min-height` from `2.5rem` back to `1.8rem` to match the restored header height:
```css
@media (max-width: 640px) {
  .ats-group {
    font-size: 0.6rem;
    min-height: 1.8rem;   /* was 2.5rem — matches restored header */
    align-items: center;
  }
}
```

---

### Fix 3 · LogPanel.svelte — Conn tab row borders
**File**: `frontend/src/lib/LogPanel.svelte`

Add `border-bottom` to `.lp-conn-row` to match other tab grid row borders:
```css
.lp-conn-row {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.2rem 0.5rem;
  white-space: nowrap;
  border-bottom: 1px solid rgba(126,151,184,0.10);  /* ← ADD */
}
.lp-conn-row:last-child {
  border-bottom: none;   /* ← ADD — no double-border at list end */
}
```

---

### Fix 4 · NavBreakdown.svelte — Slot-specific per-account breakdown
**File**: `frontend/src/lib/NavBreakdown.svelte`

Replace the single 5-column NAV table with a slot-aware table. When `activeSlot` changes, the columns and per-account computations switch. TOTAL rows must match the corresponding NavStrip pill values.

**Slot → columns → data source**:

| Slot | Col 1 | Col 2 | Source |
|------|-------|-------|--------|
| P | Day P&L | Lifetime P&L | `positionsStore`: Σ `baseDayPnlForPosition(p)` per acct; Σ `p.pnl` per acct |
| M | Avail Margin | Total Margin | `fundsStore`: `f.avail_margin`; `f.used_margin + f.avail_margin` per acct |
| C | Live Cash | Total Cash | `fundsStore`: `f.live_cash ?? f.cash`; add per-acct long-option premium from `positionsStore` |
| H | Today MTM | Value | Lifetime | `holdingsStore`: Σ `h.day_change_val`; Σ `h.cur_val`; Σ `h.pnl` per acct |

**TOTAL row match targets** (from PositionStrip):
- P Day: `dispPositionsToday` (live-tick version; `baseDayPnlForPosition` total is the static approximation — acceptable)
- P Lifetime: `_livePositionsPnl` = Σ `p.pnl`
- M Avail: `marginAvail` = Σ `f.avail_margin`
- M Total: `marginTotal` = Σ `(f.used_margin + f.avail_margin)`
- C Live: `liveCashTotal` = Σ `(f.live_cash ?? f.cash)`
- C Total: `cashTotal` = `liveCashTotal + longOptionsCashPaid`
- H Today: `dispHoldingsToday` ≈ Σ `h.day_change_val` (live tick delta excluded — close enough)
- H Value: `_liveHoldingsValue` = Σ `h.cur_val`
- H Lifetime: `_liveHoldingsTotal` = Σ `h.pnl`

**Implementation approach**:
- Keep the existing `_funds`, `_positions`, `_holdings` store bindings and `_allAccounts`/`_scopedAccounts` derivation.
- Add `$derived.by()` blocks for each slot's per-account data:
  - `_pByAcct`: group positions by account → compute day PnL via `baseDayPnlForPosition` + Σ pnl
  - `_mByAcct`: group funds by account → avail + total margin
  - `_cByAcct`: funds live_cash per account + per-account long-option premium from positions
  - `_hByAcct`: group holdings by account → Σ day_change_val, Σ cur_val, Σ pnl
- Import `baseDayPnlForPosition` from `$lib/data/nav`.
- Template: `{#if activeSlot === 'P'}` … `{:else if activeSlot === 'M'}` … etc. Each branch renders its own `<table>` with appropriate headers + data rows + TOTAL row.
- Preserve existing loading/error/timeout/empty state machine (check `_allLoaded`, `_anyError`, `_inFlight` as before — but only require the stores that the active slot uses).
- Keep existing `.nav-bd-*` CSS; add `.nav-slot-label` for a small slot-name indicator above the table.
- Update the caption to describe the current slot's formula.
- `downloadCsv()` exports the currently visible slot's data.

---

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes — add/update spec covering:
  - Mobile viewport: page-header `min-height` ≈ 1.8rem (≤ 30px)
  - Mobile: tapping anywhere in header area (including on timestamp text) toggles refresh timestamp
  - Conn tab: row has `border-bottom` visible (CSS check)
  - NavStrip P slot click → NavBreakdown shows "Day P&L" + "Lifetime P&L" columns
  - NavStrip M slot click → NavBreakdown shows "Avail Margin" + "Total Margin" columns
  - NavStrip C slot click → NavBreakdown shows "Live Cash" + "Total Cash" columns
  - NavStrip H slot click → NavBreakdown shows "Today MTM" + "Value" + "Lifetime" columns

## Done when
1. Mobile page-header `min-height` is 1.8rem; `algo-content` padding-top adjusted in lockstep
2. Tapping anywhere on the mobile page-header (including on the timestamp text itself) toggles the refresh timestamp; no double-fire
3. Conn tab rows have `border-bottom` matching other log tab grids
4. Each NavStrip slot (P/M/C/H) opens NavBreakdown with slot-specific columns; TOTAL row matches NavStrip pill values
5. `svelte-check` clean, Playwright green
