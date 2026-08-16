# Plan: St column fallback fix + derivatives column sync + Groww Day P&L

## Context

Three issues found after the St column deploy:
1. **St column still empty everywhere** — the `qty_pos` fallback in `pulseColumns.js` (commit e6656b7e) is ineffective because `qty_pos` is never in the backend response. The correct field to check is `quantity` (always present on position rows). `CandidateLegRow.svelte` has no fallback at all.
2. **Derivatives legs/exp-close column order out of sync with pulse positions** — qty and account appear early (pos 4/5), lots/ltp/avg/day_pnl/close/pnl appear in wrong relative order vs pulse positions.
3. **Groww Day P&L = 0 on initial load** — `_normalise_positions()` in `groww.py` never computes `day_change_val`. When LTP cache is cold on startup, `last_price=0` → backend formula returns 0 → frontend shows 0 until refresh warms the cache. Adding a pre-computed `day_change_val` (mirroring Dhan's line 1876) gives a concrete fallback.

---

## Fix 1 — pulseColumns.js: fix ineffective fallback

**File:** `frontend/src/lib/data/pulseColumns.js`

**cellRenderer** (line 490) — change `qty_pos` to `quantity`:
```js
// Before:
if (d.qty_pos !== undefined) return '○';
// After:
if (d.quantity !== undefined) return '○';
```

**cellStyle** (line 481-482) — add matching fallback before final `return {}`:
```js
if (d.quantity !== undefined) return { background: 'rgba(251,191,36,0.15)', color: '#fbbf24' };
return {};
```

---

## Fix 2 — CandidateLegRow.svelte: add St fallback

**File:** `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`

Line 204 — change the render expression from:
```svelte
{c.has_gtt ? 'GTT' : c.pair_group_key ?? (c.is_orphan ? '○' : '')}
```
to:
```svelte
{c.has_gtt ? 'GTT' : c.pair_group_key ?? (c.is_orphan ? '○' : (c.quantity !== undefined ? '○' : ''))}
```

---

## Fix 3 — derivatives column reorder (matches pulse positions)

Both the **Legs tab** and **Exp Close tab** share the same `.cand-grid` CSS class and `CandidateLegRow` component — fixing the grid layout and row order once fixes both tabs.

Pulse positions order (common columns):
`St → sym → lots → ltp → avg → day_pnl → close → pnl → qty → account`

### 3a. CSS grid-template-columns in +page.svelte (lines 5982-6000)

New order:
```css
auto                                 /* checkbox */
38px                                 /* pos state */
minmax(max-content, max-content)     /* symbol */
minmax(44px, max-content)            /* lots ← moved up (was pos 6) */
minmax(62px, max-content)            /* ltp ← moved up (was pos 7) */
minmax(62px, max-content)            /* avg ← moved up (was pos 9) */
minmax(72px, max-content)            /* day pnl ← moved up (was pos 11) */
minmax(62px, max-content)            /* prev close ← (was pos 8) */
minmax(72px, max-content)            /* pnl ← (was pos 10) */
minmax(48px, max-content)            /* qty ← moved down (was pos 5) */
minmax(max-content, max-content)     /* account ← moved down (was pos 4) */
minmax(72px, max-content)            /* exp pnl */
minmax(52px, max-content)            /* iv */
minmax(56px, max-content)            /* delta */
minmax(56px, max-content)            /* gamma */
minmax(62px, max-content)            /* theta */
minmax(56px, max-content)            /* vega */
minmax(62px, max-content);           /* ev */
```

### 3b. cand-headrow labels in +page.svelte (lines 4458-4502)

New label order (after checkbox + state span):
`Symbol → Lots → LTP → Avg → Day P&L → Close → P&L → Qty → Acct → Exp P&L → IV → Δ → Γ → Θ → 𝒱 → EV`

### 3c. CandidateLegRow.svelte cell render order (lines 306-372)

New cell order after the symbol block (line 305):
1. lots span (currently lines 319-343)
2. ltp span (currently line 344-351)
3. avg/cost span (currently line 353)
4. day_pnl span (currently lines 357-360)
5. prev_close span (currently line 352)
6. pnl span (currently lines 354-356)
7. qty/displayQty span (currently lines 307-318) — account first removed to here
8. account span (currently line 307)
9. exp_pnl + Greeks (unchanged)

---

## Fix 4 — groww.py: add day_change_val to _normalise_positions

**File:** `backend/brokers/adapters/groww.py`

In `_normalise_positions()` (line 1519-1571), before the `"_raw": p` entry, add computed `day_change_val`:

```python
# Add these two local vars after the existing field extractions:
_ltp   = _gf(p, "last_price", "ltp")
_close = _gf(p, "close_price", "previous_close")
_qty   = _gi(p, "quantity")
```

Then add to the row dict (after line 1563):
```python
"day_change_val": (_ltp - _close) * _qty if _ltp > 0 and _close > 0 and _qty != 0 else 0.0,
```

Note: Groww uses `multiplier=1` (line 1553), so `qty_contracts = qty`. No lot-size adjustment needed.

---

## Files to change

| File | Change |
|---|---|
| `frontend/src/lib/data/pulseColumns.js` | Fix fallback: `qty_pos` → `quantity` in cellRenderer + add to cellStyle |
| `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` | Add `c.quantity !== undefined` fallback + reorder cells |
| `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` | Reorder grid-template-columns + headrow labels |
| `backend/brokers/adapters/groww.py` | Add `day_change_val` computation in `_normalise_positions()` |

---

## Agents
- frontend: Fix 1 (pulseColumns.js fallback), Fix 2 (CandidateLegRow fallback), Fix 3 (column reorder in +page.svelte + CandidateLegRow)
- broker: Fix 4 (groww.py day_change_val)
- backend: skip
- backend-test: add test for Groww _normalise_positions verifying day_change_val is computed when ltp+close are valid
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(positions): St fallback quantity field + derivatives column sync + Groww day_change_val

## Done when
- Pulse positions St column shows '○' for all position rows (paired, orphan, and plain)
- Derivatives legs St column shows '○' for all rows
- Derivatives exp-close St column shows '○' for all rows
- Derivatives legs column order: lots → ltp → avg → day_pnl → close → pnl → qty → account → exp_pnl → Greeks
- Groww positions show non-zero Day P&L on first page load (not just after refresh)
- svelte-check 0 errors, pytest green
