# Plan: Fix standalone St column + holdings Lots revert + derivatives fixes

## Context

Keep the standalone `pos_state` St column as-is in positions grids. Fix five issues:
1. **St column shows no values** — backend infinite-loop bug (`longs.pop(0)` should be `longs_q.pop(0)`) + frontend defensive fallback for unenriched position rows
2. **Holdings has St column + Lots in wrong position** — filter pos_state from holdingsColDefs AND reorder Lots back to after LTP (original position for holdings)
3. **Derivatives St cell before checkbox** — should be AFTER checkbox (swap tracks in grid-template-columns + element order in CandidateLegRow)
4. **Derivatives St values empty** — same root cause as #1 (backend bug)
5. **Public PerformancePage** — hide St column (already there inline, just change `hide: false → true`)
6. **Derivatives default root** — COPPER shown instead of CRUDEOIL; fix alphabetical tier sort to position-count-descending

---

## Fix 1 — backend/api/routes/positions.py: fix longs_q pop bug

**Line 144-147** — `longs.pop(0)` should be `longs_q.pop(0)`. The mutable waterfall queue is `longs_q`, not `longs`. When a long is fully matched, failing to pop from `longs_q` leaves a zero-qty entry at the head, causing an infinite loop for any portfolio with both long and short positions:

```python
# Current (buggy):
if longs_q[0][1] == 0:
    longs.pop(0)       # ← wrong list

# Fixed:
if longs_q[0][1] == 0:
    longs_q.pop(0)     # ← correct
```

The existing orphan-marking pass (lines 158-168) is already correct — no change needed there.

---

## Fix 2 — frontend/src/lib/data/pulseColumns.js: defensive cellRenderer fallback

In the `pos_state` column `cellRenderer` (line 483-490), add a defensive fallback so any position row that isn't enriched by the backend (all three flags false/null) still shows `'○'`:

```js
cellRenderer: (p) => {
  const d = p.data;
  if (!d || d._isTotal) return '';
  if (d.has_gtt)        return 'GTT';
  if (d.pair_group_key) return d.pair_group_key;
  if (d.is_orphan)      return '○';
  if (d.qty_pos !== undefined) return '○';   // ← add this line
  return '';
},
```

Same fix in `PerformancePage.svelte` inline cellRenderer (line 683-690) — add `if (d.qty_pos !== undefined) return '○';` before the final `return ''`.

---

## Fix 3 — frontend/src/lib/MarketPulse.svelte: holdingsColDefs — filter St + reorder Lots

Original column order (from `git show 897baee0^` — before the lots-move commit):
`sym → spark → ltp → avg → day_pnl → day_pnl_pct → prev → pnl → pnl_pct → qty_net → **lots** → inv_val → cur_val → …`

Current order has Lots moved to position 4 (before LTP). For holdings, revert Lots to its original position: **after `qty_net`** (between Qty and Invested).

Update line 3617:
```js
const holdingsColDefs = (() => {
  const cols = rightColDefs.filter(c => c.colId !== 'pos_state');
  // Revert Lots to original position: immediately before inv_val (Invested)
  const lotsIdx   = cols.findIndex(c => c.colId === 'lots');
  const invValIdx = cols.findIndex(c => c.colId === 'inv_val');
  if (lotsIdx !== -1 && invValIdx !== -1 && lotsIdx !== invValIdx - 1) {
    const [lotsCol] = cols.splice(lotsIdx, 1);
    const newInvValIdx = cols.findIndex(c => c.colId === 'inv_val');
    cols.splice(newInvValIdx, 0, lotsCol);
  }
  return cols;
})();
```

---

## Fix 4 — PerformancePage.svelte: hide St column

`PerformancePage.svelte` is used **only** in the public `/performance` route (confirmed by audit). In `positionsCols` (line 671), change:
```js
hide: false,   →   hide: true,
```

No prop needed — the entire component is public-only.

---

## Fix 5 — CandidateLegRow.svelte: move cand-state-cell after checkbox

Current order in component (line 188-205): `cand-state-cell span → checkbox input → symbol`  
Required order: `checkbox input → cand-state-cell span → symbol`

Move the `<span class="cand-state-cell">` block (lines 188-198) to AFTER the `<input type="checkbox">` block (lines 199-205).

---

## Fix 6 — derivatives +page.svelte: swap grid track order + fix default root sort

**Grid track order** — swap the first two tracks in `.cand-grid` grid-template-columns (lines 5971-5973):
```css
/* Current: */
38px   /* pos state */
auto   /* checkbox */

/* Fixed: */
auto   /* checkbox */
38px   /* pos state */
```

**Default root sort** — in `underlyingOptionsForPicker` (lines 1457-1517), replace alphabetical `.sort()` in Tier 1 (line 1462) and Tier 2 (line 1469) with position-count-descending sort:

```js
// Compute position count per root (before tiers loop):
const _rootPosCount = new Map();
for (const c of candidatePositions) {
  const r = _rootSymbol(c.tradingsymbol || c.symbol || '');
  _rootPosCount.set(r, (_rootPosCount.get(r) || 0) + 1);
}

// Tier 1 sort (line 1462) — replace [..._rootsWithOptions].sort() with:
[..._rootsWithOptions].sort((a, b) =>
  (_rootPosCount.get(b) || 0) - (_rootPosCount.get(a) || 0) || a.localeCompare(b))

// Tier 2 sort (line 1469) — same pattern for _rootsWithFuturesOnly
```

---

## Files to change

| File | Change |
|---|---|
| `backend/api/routes/positions.py` | Line 145: `longs.pop(0)` → `longs_q.pop(0)` |
| `frontend/src/lib/data/pulseColumns.js` | pos_state cellRenderer: add `qty_pos !== undefined` fallback |
| `frontend/src/lib/MarketPulse.svelte` | holdingsColDefs: filter pos_state + splice Lots before inv_val (original position) |
| `frontend/src/lib/PerformancePage.svelte` | pos_state column: `hide: false` → `hide: true`; same cellRenderer fallback |
| `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` | Move cand-state-cell after checkbox |
| `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` | Swap `auto / 38px` tracks; position-count sort for tiers 1+2 |

---

## Agents
- backend: fix `longs.pop(0)` → `longs_q.pop(0)` in `backend/api/routes/positions.py:145`
- backend-test: add test for long+short portfolio (verifies waterfall terminates, P1 assigned; all-long verifies is_orphan=True for unmatched)
- frontend: all five frontend file changes above
- broker: skip
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(positions): St column values + holdings revert + derivatives checkbox order + default root sort

## Done when
- Position rows in MarketPulse all show P1/P2/○/GTT in standalone St column
- Holdings grid: no St column, Lots appears immediately before the Invested (inv_val) column
- Derivatives legs: St cell appears AFTER checkbox, shows P1/P2/○/GTT values
- Derivatives default root picks highest-position-count root (CRUDEOIL before COPPER)
- Public PerformancePage: St column hidden
- Long+short portfolio waterfall terminates correctly
- svelte-check 0 errors, pytest green
