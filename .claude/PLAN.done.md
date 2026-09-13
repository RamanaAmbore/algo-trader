# Plan: Snapshot SSOT — Day P&L per-row, EV total, EV color + legs alignment

## Context

Five SSOT defects in the derivatives snapshot/legs grids:

1. **Snapshot Day P&L wrong for MCX (GOLDM, CRUDEOIL)** — `_dayPnlByRootMap` uses `_dayPnlForLeg` which fires `(ltp - prev_close) * qty` when `prev_close > 0`. For MCX, Kite qty is in lots — the formula gives the wrong rupee P&L. `g.day_without` from `rollupByUnderlying → baseDayPnlForPosition` is already correct (uses `day_change_val` / `pnl - prev_settlement_pnl` — lot-size-adjusted). Fix: replace `_dayPnlByRootMap` with `g.day_without` everywhere in the snapshot.

2. **Snapshot EV total shows only active symbol's `_mergedEv`** — Total row uses `_mergedEv`. Fix: new derived `_snapshotTotalEvFull = _snapshotTotalExp - _expPnlByRootMap[selectedUnderlying] + (_mergedEv ?? _expPnlByRootMap[selectedUnderlying] ?? 0)`.

3. **Snapshot EV total color wrong** — color driven by `(_mergedEv ?? 0)`. Fix: drive from `_snapshotTotalEvFull`.

4. **Snapshot per-row EV wrong** — shows `_mergedEv` when the row's underlying is the active payoff symbol, `_expVal` otherwise. This causes the EV value to change as you switch symbol selection in the payoff. Color is always `cell-muted` for non-active rows even when `_expVal > 0`. Fix: always show `_expVal` per row, color by `_expVal` sign. `_mergedEv` belongs in total row only.

5. **Legs TOTAL row column alignment broken** — Missing a P.Close `—` span shifts P&L, Exp P&L, and all Greeks one column left. Currently: P&L value appears in P.Close column; P&L column shows `—`; Exp P&L appears in Acct column; Acct column shows `—`; Greeks are one column off; EV column empty. Fix: add P.Close span, reorder Day P&L before P&L.

6. **Legs Day P&L wrong for MCX** — `_dayPnlForLeg` used in per-leg row prop, legs TOTAL `_totalDcv`, `candidatesDayPnl` (chart annotation), and flash. Includes equity. Fix: replace all call sites with `baseDayPnlForPosition(c)`, which uses `prev_settlement_pnl` (SSOT) → `day_change_val` → fallback. Exclude `c.kind === 'eq'` from TOTAL Day P&L.

7. **Remove AccountMultiSelect from snapshot card header** — operator requested UI cleanup.

---

## File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

### 1. Remove AccountMultiSelect import (~line 35)

Remove line:
```js
import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
```

### 2. Replace all `_dayPnlForLeg` call sites with `baseDayPnlForPosition`

**Line 1068 (flash effect):**
```js
flash.update(`leg:${k}:day`, _dayPnlForLeg(c, spot ?? null));
```
→
```js
flash.update(`leg:${k}:day`, baseDayPnlForPosition(c));
```

**Line 1950 (`candidatesDayPnl`):**
```js
s += _dayPnlForLeg(c, null);
```
→
```js
s += baseDayPnlForPosition(c);
```

**Line 4651 (CandidateLegRow dayPnl prop):**
```js
dayPnl={_dayPnlForLeg(c, liveSpot ?? null)}
```
→
```js
dayPnl={baseDayPnlForPosition(c)}
```

**Line 4714 (legs TOTAL `_totalDcv`):**
```js
{@const _totalDcv = _selectedCands.reduce((s, c) => s + Number(_dayPnlForLeg(c, liveSpot) ?? 0), 0)}
```
→ (exclude equity, use baseDayPnlForPosition):
```js
{@const _totalDcv = _selectedCands.filter(c => c.kind !== 'eq').reduce((s, c) => s + baseDayPnlForPosition(c), 0)}
```

### 3. Remove dead code

After replacing all call sites, remove:
- `_dayPnlByRootMap` derived (~line 922-926): `const _dayPnlByRootMap = $derived.by(...)` block
- `_snapshotTotalDay` derived (~line 3397-3406): const + comment block
- `_dayPnlForLeg` function (~line 2008-2037): full function + JSDoc

### 4. Add `_snapshotTotalEvFull` derived (~after line 3407, after removing `_snapshotTotalDay`)

```js
const _snapshotTotalEvFull = $derived.by(() => {
  const base = _snapshotTotalExp;
  const mergedEv = _mergedEv;
  if (mergedEv == null) return base;
  const activeExp = _expPnlByRootMap[selectedUnderlying] ?? 0;
  return base - activeExp + mergedEv;
});
```

### 5. Snapshot per-row `_dayVal` (~line 4886)

```html
{@const _dayVal = _dayPnlByRootMap[g.underlying] ?? 0}
```
→
```html
{@const _dayVal = g.day_without}
```

### 6. Snapshot total row — Day P&L cell (~line 4918)

```html
<span class="num tf-cell {_snapshotTotalDay > 0 ? 'cell-pos' : _snapshotTotalDay < 0 ? 'cell-neg' : 'cell-flat'} {flash.classOf('total:day')}">{aggCompact(_snapshotTotalDay)}</span>
```
→
```html
<span class="num tf-cell {_byUnderlyingTotal.day_without > 0 ? 'cell-pos' : _byUnderlyingTotal.day_without < 0 ? 'cell-neg' : 'cell-flat'} {flash.classOf('total:day')}">{aggCompact(_byUnderlyingTotal.day_without)}</span>
```

### 7. Snapshot per-row EV cell (~line 4903-4909)

Current code shows `_mergedEv` when this row's underlying is the active selected symbol, and `_expVal` otherwise. This means the EV value changes when you switch which symbol is selected in the payoff — same underlying shows different values depending on `selectedUnderlying`. Also the color is `cell-muted` for all non-active rows even when `_expVal > 0`.

Replace the current span with:
```html
<span class="num {_expVal > 0 ? 'cell-pos' : _expVal < 0 ? 'cell-neg' : 'cell-muted'}">
  {_expVal !== 0 ? aggCompact(_expVal) : '—'}
</span>
```

`_expVal` = `_expPnlByRootMap[g.underlying] ?? 0` (already declared above this line). This makes per-row EV stable (no `selectedUnderlying` dependency) and color-coded correctly. `_mergedEv` (probabilistic backend EV) appears ONLY in the total row.

### 8. Snapshot total row — EV cell (~line 4923-4925)

```html
<span class="num {(_mergedEv ?? 0) > 0 ? 'cell-pos' : (_mergedEv ?? 0) < 0 ? 'cell-neg' : 'cell-flat'}">
  {_mergedEv != null ? aggCompact(_mergedEv) : '—'}
</span>
```
→
```html
<span class="num {_snapshotTotalEvFull > 0 ? 'cell-pos' : _snapshotTotalEvFull < 0 ? 'cell-neg' : 'cell-flat'}">
  {_snapshotTotalEvFull !== 0 ? aggCompact(_snapshotTotalEvFull) : '—'}
</span>
```

### 9. Flash for total:day (~line 1078-1082)

Change:
```js
const day = _snapshotTotalDay;
...
flash.update('total:day', day);
```
→
```js
flash.update('total:day', _byUnderlyingTotal.day_without);
```
(remove the `const day = ...` local variable)

### 10. Download handler (~line 4794)

```js
const dayVal  = _dayPnlByRootMap[g.underlying] ?? 0;
```
→
```js
const dayVal  = g.day_without;
```

### 11. Fix legs TOTAL row HTML (~lines 4716-4752)

The TOTAL row is missing the P.Close `—` span, causing all columns from Day P&L onward to shift left by 1. The current span order has `_totalPnl` (P&L) BEFORE `_totalDcv` (Day P&L), which also mismatches the header column order (Day P&L col 9, P&L col 10).

Replace the current block from `<div class="cand-row cand-row-total">` through `</div>`:

```html
<div class="cand-row cand-row-total">
  <span></span>
  <span class="cand-total-label">TOTAL</span>
  <span>—</span>
  <span class="num">—</span>
  <span class="num">—</span>
  <span class="num">—</span>
  <span class="num">—</span>
  <span class="num">—</span><!-- P.Close — was missing, caused 1-column offset -->
  <span class="num tf-cell cand-pnl {_totalDcv > 0 ? 'cell-pos' : _totalDcv < 0 ? 'cell-neg' : 'cell-flat'} {flash.classOf('total:day')}"
        title="Σ Day P&L across enabled F&O legs (excludes equity)">
    {aggCompact(_totalDcv)}
  </span>
  <span class="num tf-cell cand-pnl {_totalPnl > 0 ? 'cell-pos' : _totalPnl < 0 ? 'cell-neg' : 'cell-flat'} {flash.classOf('total:pnl')}"
        title="Σ P&L across every visible row = strip's P chip for these accounts">
    {aggCompact(_totalPnl)}
  </span>
  <span class="num">—</span>
  <!-- _legsExpPnlTotal is the script-level SSOT shared with the
       snapshot row for the selected underlying — both surfaces
       read the same derived value so they are always identical. -->
  <span class="num tf-cell cand-pnl {_legsExpPnlTotal > 0 ? 'cell-pos' : _legsExpPnlTotal < 0 ? 'cell-neg' : 'cell-flat'} {flash.classOf('total:exp')}"
        title="Σ Exp P&L across every selected leg — strategy expiry-day P&L at current spot.">
    {aggCompact(_legsExpPnlTotal)}
  </span>
  <span class="num">—</span>
  <span class="num" title="Σ Δ across every selected leg (position-scaled).">{pctFmt(_tg.delta)}</span>
  <span class="num" title="Σ Γ across every selected leg (position-scaled).">{pctFmt(_tg.gamma)}</span>
  <span class="num {_tg.theta < 0 ? 'cell-neg' : 'cell-flat'}"
        title="Σ Θ across every selected leg (position-scaled). Negative = decay eating value each day.">
    {aggCompact(_tg.theta)}
  </span>
  <span class="num" title="Σ 𝒱 across every selected leg (position-scaled).">{aggCompact(_tg.vega)}</span>
  <span class="num {(_mergedEv ?? 0) > 0 ? 'cell-pos' : (_mergedEv ?? 0) < 0 ? 'cell-neg' : 'cell-flat'}"
        title="Strategy-level EV across every selected leg.">
    {_mergedEv != null ? aggCompact(_mergedEv) : '—'}
  </span>
</div>
```

### 12. Remove AccountMultiSelect from snapshot CardHeader middle snippet (~lines 4826-4831)

Remove the `AccountMultiSelect` component from the `{#snippet middle()}` block. Keep `StrategyPicker`. The middle snippet becomes:
```html
{#snippet middle()}
  <StrategyPicker label="Strategy" />
{/snippet}
```

---

## Agents

- frontend: make all changes above in `derivatives/+page.svelte` only (no other files)

## Tests

- svelte-check: yes — 0 errors
- vitest: yes — 971 passed (no new tests needed; behaviour fix only)

## Commit message

fix(derivatives): SSOT Day P&L — baseDayPnlForPosition per-row/legs; fix EV total + legs column alignment; rm accounts filter

## Done when

- GOLDM/CRUDEOIL Day P&L in snapshot and legs matches NavStrip P1
- Snapshot total Day P&L = `_byUnderlyingTotal.day_without` = NavStrip P1
- Snapshot per-row EV always shows `_expVal` (stable, not affected by symbol selection)
- Snapshot per-row EV color: green when positive, red when negative, muted when zero
- Snapshot EV total = sum of per-row `_expVal` (with active root substituted by `_mergedEv` when available)
- EV total color-coded correctly when positive
- Legs TOTAL row: Day P&L in col 9, P&L in col 10, Exp P&L in col 12, Greeks in correct columns
- Legs TOTAL Day P&L excludes equity, uses `baseDayPnlForPosition`
- Snapshot header has no accounts dropdown
- svelte-check 0 errors, vitest 971 passed
