# Plan: LTP/spot/day% consistency + CSS named colors + tab-return freeze + Pulse Exp P&L

## Task

Five areas of work, all frontend:

1. **CSS named color tokens** — introduce `:root` CSS custom properties for all directional
   colors and their alpha/brightness variants. Update `cell-pos/neg/flat`, all flash keyframes,
   heat classes to reference tokens instead of hardcoded hex.

2. **LTP/spot/day % — text color + text-color flash** — these three use `cell-pos/neg/flat`
   text coloring vs `prev_close`. Flash for LTP/spot/day % must animate the TEXT COLOR (not
   background): briefly transition to a brighter shade of pos/neg then settle back. Separate
   from P&L which keeps its background flash.

3. **P&L flash stays background** — `tf-up/down` (350ms bg fade) and `ltp-flash-up/down`
   (currently bg fade) are the P&L cascade mechanism; keep those as background for P&L cells
   only. New text-flash classes handle LTP/spot/day %.

4. **Tab-return freeze** — `EventSource` silently dead after TCP idle timeout; no reconnect
   on tab return. Fix: `visibilitychange` listener in `quoteStream.js` → `restartQuoteStream()`.

5. **Pulse Exp P&L scoping** — hide Exp P&L + Extrinsic for holdings-only rows; add summed
   Exp P&L to positions pinned-bottom totals row.

---

## Consistency audit — what's wrong, what's the fix

### Text color — current state
| Surface | Current | Target |
|---|---|---|
| MarketPulse LTP | bg heat + border only, **no text color** | `cell-pos/neg/flat` vs `prev_close` |
| PerformancePage LTP | `pnl-gain/loss/zero` text | `cell-pos/neg/flat` |
| Derivatives CandidateLegRow LTP | `cell-pos/neg/flat` vs `c.prev_close` | ✓ correct |
| Derivatives Snapshot spot | `cell-pos/neg/flat` vs `strategy.spot_prev_close` | ✓ correct |
| Day % columns (all pages) | `cell-pos/neg/flat` via `dirCls()` | ✓ correct — already right |

### Flash — current state and target
| Surface | Current flash | Target |
|---|---|---|
| MarketPulse LTP | `ltp-flash-up/down` 600ms **bg** | Replace with new **text-color** flash `ltp-tc-flash-up/down` |
| CandidateLegRow LTP | local `leg-ltp-up/down` 450ms bg | Replace with `ltp-tc-flash-up/down` (text-color flash); delete scoped keyframes |
| Derivatives Snapshot spot | `tf-up/down` 350ms bg | Replace with `ltp-tc-flash-up/down` (it's an LTP source) |
| PerformancePage LTP | `ltp-flash-up/down` 600ms bg | Replace with `ltp-tc-flash-up/down` |
| Day % columns | **no flash at all** | Add `ltp-tc-flash-up/down` via flash tracking |
| All P&L columns | `tf-up/down` 350ms bg cascade | ✓ keep as-is (background flash for derived values) |

### New text-color flash classes (to be added in `app.css`)
```css
/* Text-color flash: brighter shade → normal directional color */
@keyframes ltp-tc-flash-up {
  0%   { color: var(--clr-pos-bright); }   /* #86efac — green-300 */
  100% { color: var(--clr-pos); }          /* #4ade80 — green-400 */
}
@keyframes ltp-tc-flash-down {
  0%   { color: var(--clr-neg-bright); }   /* #fca5a5 — red-300 */
  100% { color: var(--clr-neg); }          /* #f87171 — red-400 */
}
.ltp-tc-flash-up   { animation: ltp-tc-flash-up   500ms ease-out; }
.ltp-tc-flash-down { animation: ltp-tc-flash-down 500ms ease-out; }
```
These override `color` only — no background change. Applied on LTP/spot/day % cells.

### CSS color divergences to fix (via tokens)
| Class | Theme | Current | → Token |
|---|---|---|---|
| `cell-pos/neg/flat` | all | hardcoded hex | `var(--clr-pos/neg/flat)` |
| `pnl-gain/loss` | algo | hardcoded hex | `var(--clr-pos/neg)` for text |
| `ltp-flash-up/down` keyframe bg | all | `rgba(74,222,128,0.35)` | `var(--clr-pos-a35)` |
| `tf-pnl-up/down` keyframe bg | all | `rgba(74,222,128,0.13)` | `var(--clr-pos-a13)` |
| `ltp-vs-avg-up/down` bg | algo | hardcoded rgba | `var(--clr-pos-a10)` / `var(--clr-neg-a10)` |
| ramboq overrides | ramboq | hardcoded teal/amber | scoped ramboq variables |

---

## Agents

- frontend: Implement all five areas.

  ### Fix 1 — CSS named tokens (`frontend/src/app.css`)

  Add `:root` block near the top:
  ```css
  :root {
    --clr-pos:        #4ade80;
    --clr-pos-bright: #86efac;   /* green-300 for text flash */
    --clr-neg:        #f87171;
    --clr-neg-bright: #fca5a5;   /* red-300 for text flash */
    --clr-flat:       #94a3b8;
    --clr-pos-a35:    rgba(74, 222, 128, 0.35);
    --clr-neg-a35:    rgba(248, 113, 113, 0.35);
    --clr-pos-a13:    rgba(74, 222, 128, 0.13);
    --clr-neg-a13:    rgba(248, 113, 113, 0.13);
    --clr-pos-a10:    rgba(74, 222, 128, 0.10);
    --clr-neg-a10:    rgba(248, 113, 113, 0.10);
    --clr-pos-a08:    rgba(74, 222, 128, 0.08);
    --clr-neg-a08:    rgba(248, 113, 113, 0.08);
    --clr-flat-a50:   rgba(126, 151, 184, 0.50);
  }
  ```

  Add new text-color flash keyframes and classes (see above).

  Replace hardcoded values in:
  - `.cell-pos/neg/flat` → `var(--clr-pos/neg/flat)`
  - `@keyframes ltp-flash-up/down` → `var(--clr-pos-a35/neg-a35)` (bg flash kept for P&L cascade)
  - `@keyframes tf-pnl-up/down` → `var(--clr-pos-a13/neg-a13)`
  - `.ag-theme-algo .ltp-vs-avg-up/down` → `var(--clr-pos-a10/neg-a10)`
  - `.ag-theme-algo .pnl-gain/loss` → `var(--clr-pos/neg)` text
  - ramboq-theme overrides → scoped variables on `.ag-theme-ramboq`

  ### Fix 2a — MarketPulse LTP text color + text flash (`frontend/src/lib/data/pulseColumns.js`)

  In `mkLtpCol()` cellClass: add `cell-pos/neg/flat` text color based on `ltp vs prev_close`
  (same field reference already used for `ltp-vs-prev-*` border). Change the flash class
  emitted for LTP from `ltp-flash-up/down` to `ltp-tc-flash-up/down`.

  In `mkPnlCellClass()` or wherever LTP flash classes are chosen: ensure LTP cells get
  `ltp-tc-flash-up/down` and P&L cells keep `ltp-flash-up/down` (bg) or `tf-up/down` (bg).

  In day % column (`change_pct`, `day_pnl_pct`) definitions: add flash tracking. Add a
  helper `mkDayPctCellClass(getMpFlash)` that combines `dirCls(p.value)` + text flash from
  `getMpFlash().classOf(sym + ':change_pct')` / `':day_pnl_pct'`. Pass `ltp-tc-flash-up/down`
  as the up/down class names on that flash instance.

  ### Fix 2b — PerformancePage LTP (`frontend/src/lib/PerformancePage.svelte`)

  On LTP column: replace `pnl-gain/loss/zero` → `cell-pos/neg/flat`. Change flash class
  for LTP from `ltp-flash-up/down` → `ltp-tc-flash-up/down`.

  ### Fix 2c — CandidateLegRow LTP flash (`frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`)

  Delete scoped `leg-ltp-up/down` `@keyframes` from `<style>` block.
  Update flash instance to emit `ltp-tc-flash-up/down`: pass `{ upClass: 'ltp-tc-flash-up',
  downClass: 'ltp-tc-flash-down' }` to the flash instance used for the LTP cell.

  ### Fix 2d — Derivatives Snapshot spot flash (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`)

  Change the spot flash instance for the Snapshot spot cell to emit `ltp-tc-flash-up/down`
  instead of `tf-up/down`.

  ### Fix 3 — Day % flash in MarketPulse (`frontend/src/lib/MarketPulse.svelte`)

  In the per-poll update cycle: call `_mpFlash.update(sym + ':change_pct', newPct, oldPct)` for
  left grid rows, and `_mpFlash.update(sym + ':day_pnl_pct', newPct, oldPct)` for right grid rows.
  Include `change_pct` and `day_pnl_pct` in the `refreshCells` column list. Flash emits
  `ltp-tc-flash-up/down` (text-color, via the class names set on the flash instance).

  ### Fix 4 — Tab-return SSE reconnect (`frontend/src/lib/data/quoteStream.js`)

  Add `let _visHandlerInstalled = false;`. In `startQuoteStream()` after singleton guard:
  ```javascript
  if (!_visHandlerInstalled && typeof document !== 'undefined') {
    _visHandlerInstalled = true;
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) restartQuoteStream();
    });
  }
  ```

  ### Fix 5a — Holdings: hide Exp P&L + Extrinsic (`frontend/src/lib/data/pulseColumns.js`)

  In `mkExpPnlCol` and `mkExtrinsicCol` valueGetters:
  ```javascript
  if (!p.data?.qty_pos) return null;
  ```

  ### Fix 5b — Positions totals row Exp P&L (`frontend/src/lib/MarketPulse.svelte`)

  - `_blankTotalsAcc()`: add `exp_pnl: 0`
  - Accumulation loop: `if (row.qty_pos) acc.exp_pnl += positionsDerivedStore.byKey[row.tradingsymbol?.toUpperCase()]?.exp_pnl ?? 0;`
  - Totals row object: `exp_pnl: acc.exp_pnl || null`

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- vitest: yes
- playwright: no

## Commit message
fix(ui): CSS named color tokens; LTP/spot/day% text-color flash; consistent coloring all pages; tab-return SSE reconnect; Pulse Exp P&L scoping

## Done when
- `:root` defines all directional color tokens; no hardcoded hex in `cell-*`, flash keyframes, heat classes
- LTP, spot, day % flash animates TEXT COLOR (`ltp-tc-flash-up/down`), not background
- P&L cells keep background flash (`tf-up/down`)
- Day % columns flash on poll updates
- All LTP/spot text uses `cell-pos/neg/flat` vs `prev_close`
- Tab-return does not freeze LTP/spot
- Holdings rows: blank Exp P&L + Extrinsic; positions totals row: summed Exp P&L
- svelte-check 0 errors, vitest passes
