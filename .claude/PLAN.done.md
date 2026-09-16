# Plan: Simplify LTP color to 3 tiers + rename "Day %" → "Chg %" everywhere

## Context
Two bundled simplifications:
1. **Remove tier variants** — `ltpDayClass` currently emits 7 classes (pos-sm/pos/pos-lg × neg + flat). User wants 3: pos / neg / flat. Simpler, cleaner.
2. **Rename Day % → Chg %** — consistent with the payoff overlay which already uses "chg%". Applies to all column headers, labels, and CSV export headers across every surface.

## Agents
- backend: skip
- frontend: Implement all changes below
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Update pulse_column_order.spec.js and losers_sign_correctness.spec.js — replace all `'Day %'` header string checks with `'Chg %'`; update derivatives_snapshot_spot_smoke.spec.js valid-class list to remove -sm/-lg variants

## Change A — `frontend/src/lib/format.js`
Simplify `ltpDayClass` from tiered logic to 3-class:
```javascript
export function ltpDayClass(changePct) {
  if (changePct == null || !isFinite(changePct) || changePct === 0) return 'ltp-day-flat';
  return changePct > 0 ? 'ltp-day-pos' : 'ltp-day-neg';
}
```

## Change B — `frontend/src/app.css`
Remove the 4 tier-variant classes; keep only 3:
```css
.ltp-day-pos  { color: var(--algo-green) !important; }
.ltp-day-neg  { color: var(--algo-red)   !important; }
.ltp-day-flat { color: var(--c-muted)    !important; }
```
Delete: `.ltp-day-pos-sm`, `.ltp-day-pos-lg`, `.ltp-day-neg-sm`, `.ltp-day-neg-lg`

## Change C — `frontend/src/lib/__tests__/format.test.js`
Remove all test cases for `-sm` and `-lg` variants. Update boundary tests to expect `ltp-day-pos` / `ltp-day-neg` for all non-zero values regardless of magnitude.

## Change D — Rename "Day %" → "Chg %" in column definitions and labels

### D1: `frontend/src/lib/data/pulseColumns.js`
- Line 488: `headerName: 'Day %'` → `headerName: 'Chg %'`  (left grid change_pct col)
- Line 625: `headerName: 'Day %'` → `headerName: 'Chg %'`  (right grid day_pnl_pct col)
- Line 700: `headerName: 'Day %'` → `headerName: 'Chg %'`  (mkPosSummaryCols)
- Line 729: `headerName: 'Day %'` → `headerName: 'Chg %'`  (mkHoldSummaryCols)

### D2: `frontend/src/lib/PerformancePage.svelte`
- Line 549: `headerName: 'Day %'` → `headerName: 'Chg %'`
- Line 575: `headerName: 'Day %'` → `headerName: 'Chg %'`
- Line 593: `headerName: 'Day %'` → `headerName: 'Chg %'`
- Line 710: `headerName: 'Day %'` → `headerName: 'Chg %'`

### D3: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- Line 4714: `{ header: 'Day %', ...}` → `{ header: 'Chg %', ... }`  (CSV export)
- Line 4736: `<span ... title="...">Day %</span>` → `Chg %` (column header span)

### D4: `frontend/src/routes/(algo)/dashboard/+page.svelte`
- Line 1489: `headerName: 'Day %'` → `headerName: 'Chg %'`

## Change E — Playwright tests

### E1: `frontend/e2e/pulse_column_order.spec.js`
Replace ALL `'Day %'` string literals used as column header checks with `'Chg %'`.
Lines: 9, 14, 54, 76, 77, 98, 106, 116, 120, 148, 149, 168, 176, 179, 253, 263, 269, 270.
Use `replace_all: true` on the string `'Day %'` → `'Chg %'` within this file.

### E2: `frontend/e2e/losers_sign_correctness.spec.js`
Update column header references from `'Day %'` → `'Chg %'` where used to locate the column by header text.

### E3: `frontend/e2e/derivatives_snapshot_spot_smoke.spec.js`
Remove `'ltp-day-pos-sm'`, `'ltp-day-neg-sm'`, `'ltp-day-pos-lg'`, `'ltp-day-neg-lg'` from the valid-class array. Keep only `['ltp-day-pos', 'ltp-day-neg', 'ltp-day-flat']`.

## Files changed
- `frontend/src/lib/format.js`
- `frontend/src/app.css`
- `frontend/src/lib/__tests__/format.test.js`
- `frontend/src/lib/data/pulseColumns.js`
- `frontend/src/lib/PerformancePage.svelte`
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- `frontend/src/routes/(algo)/dashboard/+page.svelte`
- `frontend/e2e/pulse_column_order.spec.js`
- `frontend/e2e/losers_sign_correctness.spec.js`
- `frontend/e2e/derivatives_snapshot_spot_smoke.spec.js`

## Tests
- pytest: no
- svelte-check: yes
- playwright: no (spec-only update, no live run needed)
- vitest: yes

## Commit message
refactor(ui): simplify LTP color to pos/neg/flat; rename Day % → Chg % everywhere

## Done when
- `ltpDayClass` returns only ltp-day-pos / ltp-day-neg / ltp-day-flat
- No -sm or -lg CSS classes remain in app.css or tests
- All "Day %" column headers and labels read "Chg %" across pulse, derivatives, performance, dashboard
- svelte-check 0 errors, vitest all pass
