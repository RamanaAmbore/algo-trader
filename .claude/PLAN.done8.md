# Plan: Pulse grid fixes + derivatives page polish

## Context
Six distinct defects + two UI improvements across MarketPulse, the derivatives page, and
the backend day P&L pipeline. Each fix is independent; all target user-visible regressions
or missing behaviour.

## Implementation

---

### Fix 1 — `_compareMainRows` groupOrder (MarketPulse.svelte ~line 1746)

Replace:
```js
const ua = _mrUgKey(a), ub = _mrUgKey(b);
if (ua !== ub) return ua.localeCompare(ub);
```
With:
```js
const ua = _mrUgKey(a), ub = _mrUgKey(b);
if (ua !== ub) {
  const ra = groupOrder[ua] ?? null, rb = groupOrder[ub] ?? null;
  if (ra !== null && rb !== null) return ra - rb;
  if (ra !== null) return -1;
  if (rb !== null) return  1;
  return ua.localeCompare(ub);
}
```

### Fix 2 — `_topRowsFor` movers sort (no change needed)

**Council finding**: `_topRowsFor` sorts by `|change_pct|` descending — this is intentional.
Movers always show the biggest gainers/losers by magnitude regardless of operator group
preference. Adding `groupOrder` here would cause double-invalidation (Performance concern)
and semantically wrong behaviour (movers ordered by operator rank, not magnitude).

The ▲/▼ buttons on mover rows still call `moveGroup()` and update `groupOrder` (for use by
Fix 1 in the positions/holdings/watchlist grids). No code change needed in `_topRowsFor`.

### Note on reactivity
`_compareMainRows` is used inside `mainRows ($derived.by)`. Since `groupOrder` is `$state`,
reading it inside `_compareMainRows` (which runs inside the `$derived.by` closure) makes the
derived reactive to `groupOrder` changes automatically — the derived already depends on
`groupOrder` via line 2949 (`groupOrder; detachedSymbols;`), so no change needed there.

`_topRowsFor` is called inside `winRows/$derived` and `loseRows/$derived`. Reading
`groupOrder` inside `_topRowsFor` registers it as a dependency of those deriveds, so
clicking ▲/▼ in the movers grid will trigger a re-sort.

### Fix 3 — Pulse grid cell vertical alignment

**Root cause**: ag-Grid `.ag-cell` uses `display: inline-block; height: 100%` by default.
Only `.ag-col-sym` has `display: flex; align-items: center` (app.css line 1170). Non-sym
cells top-align their content, leaving visible dark space above/below. Meanwhile the sym
cell's background-color tint fills the full 28px (including empty space above/below
centered content), creating the "color patterns at top and bottom" effect. The legs grid
(CandidateLegRow CSS subgrid) avoids this because CSS grid cells fill their track height
naturally without a padding gap.

**Fix**: In `frontend/src/lib/MarketPulse.svelte` scoped `<style>`, add inside the
existing `.mp-bucket-wrap` scope (after the existing ag-cell border-stripping rules
at line ~5021):

```css
/* Flex-center content inside all pulse bucket cells via the inner value wrapper,
   NOT the outer .ag-cell. Targeting .ag-cell directly breaks text-overflow ellipsis
   (ag-Grid issue #3828). .ag-cell-value is the correct target for vertical centering. */
:global(.mp-bucket-wrap .ag-theme-algo .ag-cell-value) {
  display: flex !important;
  align-items: center !important;
  height: 100%;
}
```

This vertically centers cell content in all pulse grid cells without disrupting ag-Grid's
cell positioning or text-overflow ellipsis. The sym column's own flex centering
(app.css:1170, on `.ag-col-sym` which is the outer cell) remains unchanged.

---

### Fix 4 — Day P&L Case 3: intraday exit shows 0 instead of realised P&L

**File: `backend/api/routes/positions.py`**

**Root cause**: `_apply_flat_row_hygiene()` (lines ~608–641) runs AFTER
`apply_day_change_backstop()`. The hygiene function zeroes `day_change_val` for every row
where `quantity=0 AND overnight_quantity=0`, which is the exact mask for Case 3
(fully closed intraday). This overwrites the backstop's correct restoration
`dcv = pnl` for rows where `pnl != 0`.

**Fix**: Exclude rows with realised P&L from the hygiene mask. In `_apply_flat_row_hygiene`,
change the zero-out line to only apply when `pnl == 0`:

```python
# current (lines ~636-637):
if 'day_change_val' in raw.columns:
    raw.loc[_flat_mask, 'day_change_val'] = 0.0

# replace with:
if 'day_change_val' in raw.columns:
    _pnl = pd.to_numeric(raw.get('pnl', pd.Series(dtype=float)), errors='coerce').fillna(0)
    # Only zero day_change_val when pnl is also zero (break-even round-trip).
    # Preserve when abs(pnl) > 0.005 — Case 3 intraday exit with realised gain/loss.
    # Uses half-paisa threshold (0.005) consistent with _override_stale_close_from_snapshot.
    raw.loc[_flat_mask & (_pnl.abs() < 0.005), 'day_change_val'] = 0.0
```

Case 1 (new entry, oq=0, qty>0) and Case 2 (closed overnight, oq>0) are unaffected —
Case 1 rows have `qty > 0` (not in flat mask), Case 2 rows have `oq > 0` (also not in
flat mask since mask requires `oq == 0`).

---

### Fix 5 — Payoff overlay LTP/chg% not flashing on value refresh

**File: `frontend/src/lib/OptionsPayoff.svelte`**

**Root cause** (to verify during impl): `_spotFlash.update('spot', spot)` at line 423 fires
when the `spot` prop changes. `spot` comes from `liveSpot` in the parent (derived from
`liveSnap(anchor)?.ltp`), which updates on SSE ticks. If the underlying isn't actively
ticking (market closed or no KiteTicker subscription), `spot` doesn't change → no flash.

**Investigation + fix**: In impl, read lines 740–760 (overlay LTP/chg% rendering) and
lines 418–424 (flash wiring). Verify: (a) whether the flash fires on SSE tick vs only on
manual refresh path; (b) whether `_spotFlash.threshold: 0` prevents flash when price
unchanged. If tick flash IS correctly wired, check if the `_spotFlash` `classOf()` call
is actually applied to the rendered span. If the flash class isn't appearing in the DOM,
add a CSS rule for `.ltp-flash-up` / `.ltp-flash-down` scoped to the overlay.

---

### Fix 6 — Payoff chart loading state visibility

**File: `frontend/src/lib/OptionsPayoff.svelte`**

**Root cause**: The loading branch (lines ~693–714) already renders `"Resolving spot…"`
text. The UX council confirmed this is correct — a shimmer would be meaningless for a
chart whose shape isn't known at load time. The actual issue is visibility: the text may
not be styled prominently enough for the operator to notice.

**Fix**: Read the current `.payoff-empty` CSS in `OptionsPayoff.svelte`. If the text is
low-contrast (opacity or small font), boost it to `color: var(--algo-slate); font-size:
var(--fs-sm)` with a subtle pulse animation (`@keyframes pulse-opacity`) so the operator
sees active loading feedback rather than a blank area. Keep the existing `"Resolving spot…"`
copy — do NOT replace with a shimmer bar.

---

### Fix 7 — Checkbox multi-selection in legs/exp close

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

**Root cause (to verify)**: Individual row checkbox binding is correct (`enabledSymbols[enKey(c)]`).
The suspected issue is the master-checkbox `indeterminate` tri-state logic
(`allCandidatesOn` / `someCandidatesOn`, lines ~1998–2035). When a single row is toggled,
`someCandidatesOn` changes, which may re-trigger the master checkbox's `indeterminate`
attribute update and visually appear to "select" all rows.

**Fix**: In impl agent, verify whether checking a single row checkbox changes
`enabledSymbols` entries for only that row. If the visual multi-row selection is purely
the master-checkbox DOM state (indeterminate visual affecting all `<input type=checkbox>`
in the same form via shared name/group), isolate the master checkbox from the row
checkboxes (add `name="master"` vs row-level `name="leg-{i}"`). If it IS a real data
fan-out, inspect the `onchange` handler in `+page.svelte`.

---

### Fix 8 — Reduce ST→Symbol spacing in legs/exp close grid

**File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

Change line 5976 from `column-gap: 0.6rem` to `column-gap: 0.35rem` in `.cand-grid`:

```css
/* current */
column-gap: 0.6rem;

/* replace with */
column-gap: 0.35rem;
```

This tightens the gap between ALL columns (checkbox → ST → symbol → LTP…) uniformly.
The 28px ST column + smaller gap will no longer appear as a wide dead zone before the
symbol text.

---

### Fix 9 — Add bottom border between symbol cells in legs/exp close

**File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`**

Add a horizontal row-separator specifically to the `.cand-sym` cell so adjacent rows in
the legs/exp-close grid have a clear visual boundary at the symbol column:

```css
/* current .cand-sym (line 512): */
.cand-sym {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}

/* add: */
.cand-sym {
  border-bottom: 1px solid rgba(126,151,184,0.25);
}
```

Scoped to `CandidateLegRow.svelte` so it only applies in the legs/exp-close grid, not
the candidate picker or other surfaces.

---

## Tests
- Add a Vitest test verifying the groupOrder-aware sort for positions and movers rows
  (test via `sortUnifiedRows` from `pulseUnified.js` with a non-alphabetical groupOrder map)
- Add a pytest unit test for `_apply_flat_row_hygiene` in `backend/tests/test_positions_route.py`
  confirming Case 3 rows (qty=0, oq=0, pnl≠0) retain their `day_change_val` after hygiene
- svelte-check must exit 0 errors
- vitest run must pass

## Agents
- frontend: Fix 1, 2, 3, 5, 6, 7, 8, 9
- backend: Fix 4
- backend-test: pytest for Fix 4
- playwright: skip

## Commit message
fix(pulse+derivatives+pnl): groupOrder sort, cell alignment, Case 3 day P&L, payoff flash/skeleton, legs spacing + border

## Done when
- ▲/▼ buttons in pulse grid visually reorder positions/holdings/watchlist/movers grids
- All pulse grid cells are vertically centered (no visible top/bottom spacing gap)
- Intraday exit (same-day open + close) shows correct realised day P&L (not 0)
- Payoff overlay LTP/chg% flash animates on tick update
- Payoff chart shows a skeleton/spinner while payoff data is loading
- Checking a single leg row selects only that row (no multi-row visual bleed)
- ST→Symbol gap visibly tighter in legs/exp close grid
- Horizontal separator visible between symbol cells in legs/exp close rows
- pytest and vitest pass with 0 failures
