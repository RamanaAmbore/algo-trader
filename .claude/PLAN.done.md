# Plan: LTP/spot text color redesign + flash no-change guard + Exp P&L format + legs total label + stale cleanup

## Context
Five distinct fixes bundled into one plan. The primary change is a visual redesign of how LTP / spot / day% 
communicate price direction — industry-standard dual-signal pattern used by Bloomberg, Refinitiv, Kite.

**Dual-signal design (confirmed industry standard):**
- **Text color** = WHERE you are vs. yesterday (persistent, tiered by day% magnitude)
  - > prev_close → positive green (dim / standard / bright based on |day%|)
  - < prev_close → negative red (same tiers)
  - = prev_close → flat/muted
- **Background flash** = WHAT JUST HAPPENED on the last tick (animated, tick-direction based)
  - Tick ↑ → `tf-up` pulse (green background)
  - Tick ↓ → `tf-down` pulse (red background)
  - Same value as prev tick → NO flash (no-change guard)

**Current issues:**
1. LTP text color is an ANIMATED flash (fades) based on TICK direction — conflates the two signals
2. No background pulse on LTP/spot cells (currently on P&L cells only)
3. Same-tick value re-delivers still fire the animated text flash (threshold=0 bug)
4. MarketPulse Exp P&L / Extrinsic use `toLocaleString` not `aggCompact`
5. Legs total "TOTAL" label sits in pos-state column (38px), not symbol column
6. Stale code: dead import, `_excludedByAccount`, vestigial comment

## Agents
- backend: skip
- frontend: Implement all changes below across all surfaces
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add a brief smoke test that derivatives snapshot card LTP cells have the `ltp-day-pos`
  or `ltp-day-neg` or `ltp-day-flat` class applied (verifies the new color system is wired up).

## Change A — `frontend/src/lib/data/tickFlash.svelte.js`
### A1: No-change guard
After line 41 (`if (last == null) return;`), insert:
```javascript
    if (v === last) return;       // no-change: same value → no flash, no text animation
```

## Change B — New `ltpDayClass` helper in `frontend/src/lib/format.js`
Add at the end of the file:
```javascript
/**
 * Persistent LTP text color class based on day % change, tiered by magnitude.
 * Tiers: sm (< 0.5%), default (0.5–2%), lg (≥ 2%).
 * @param {number | null | undefined} changePct
 * @returns {string}
 */
export function ltpDayClass(changePct) {
  if (changePct == null || !isFinite(changePct) || changePct === 0) return 'ltp-day-flat';
  const a = Math.abs(changePct);
  const tier = a < 0.5 ? 'sm' : a < 2 ? '' : 'lg';
  const dir = changePct > 0 ? 'pos' : 'neg';
  return tier ? `ltp-day-${dir}-${tier}` : `ltp-day-${dir}`;
}
```

## Change C — `frontend/src/app.css`

### C1: New `--text-sub` global token (add to `:root` variable block)
Sits between `--text-faint` (#94a3b8, tertiary/disabled) and `--text` (#e2e8f0, primary).
Used for secondary labels that need better legibility than tertiary (e.g., perf page labels).
```css
  --text-sub: #c4d0e0;   /* secondary readable text — above dim, below primary */
```

### C2: New LTP persistent day-change text color classes
Add near the `.ltp-tc-flash-*` block. All tokens already exist in `:root`:

```css
/* ── LTP persistent day-change text color — tiered ─────────────────── */
/* Static (not animated) — reflects LTP vs prev_close. Direction signal. */
/* Pair with tf-up / tf-down background flash for tick-direction pulse.  */
.ltp-day-pos-sm { color: var(--algo-green-text-dim);    }   /* < 0.5%: dim green    */
.ltp-day-pos    { color: var(--algo-green);              }   /* 0.5–2%: std green    */
.ltp-day-pos-lg { color: var(--algo-green-text-bright);  }   /* ≥ 2%: bright green   */
.ltp-day-neg-sm { color: var(--algo-red-text-dim);       }   /* < 0.5%: dim red      */
.ltp-day-neg    { color: var(--algo-red);                }   /* 0.5–2%: std red      */
.ltp-day-neg-lg { color: var(--algo-red-text-bright);    }   /* ≥ 2%: bright red     */
.ltp-day-flat   { color: var(--c-muted);                 }   /* = prev_close: muted  */
```

Existing tokens used (all confirmed in `:root`):
- `--algo-green-text-dim` = rgba(74, 222, 128, 0.50)
- `--algo-green` = #4ade80
- `--algo-green-text-bright` = #86efac
- `--algo-red-text-dim` = rgba(248, 113, 113, 0.50)
- `--algo-red` = #f87171
- `--algo-red-text-bright` = #fca5a5
- `--c-muted` = var(--algo-muted)

## Change D — `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

### D1: Snapshot card spot cell (around line 4775)
Current template (simplified):
```svelte
<span class="num {_spotDir} {flash.classOf(`${g.underlying}:ltp`) === 'tf-up' ? _tcFlashClass('up', _spotDayPct) : flash.classOf(`${g.underlying}:ltp`) === 'tf-down' ? _tcFlashClass('down', _spotDayPct) : ''}">
```
Replace with:
```svelte
<span class="num {ltpDayClass(_pct)} {flash.classOf(`${g.underlying}:ltp`)}">
```
- `ltpDayClass(_pct)` uses the day% (`_pct` already computed from `(_ltp - _close) / _close * 100` or `_q?.day_pct`)
- `flash.classOf(...)` returns `tf-up` / `tf-down` / `''` for background pulse (tick direction)
- Remove `_spotDir`, `_tcFlashClass`, `_spotDayPct` from this cell if no longer used elsewhere

### D2: Snapshot card day% cell — apply same tiered color
The day% cell currently uses sign classes only. Apply `ltpDayClass(_pct)` here too since it
represents the same signal:
```svelte
<span class="num {ltpDayClass(_pct)}">{_pct != null ? `${_pct.toFixed(2)}%` : '—'}</span>
```

### D3: Payoff overlay spot/chg% display
Find the overlay's spot chip (labeled "chg%" per operator note). Apply:
- Text color: `ltpDayClass` using the overlay's `_pct` or `_changePercent`
- Background flash: `flash.classOf(...)` on the relevant key

### D4: Import `ltpDayClass` at top of the file
```javascript
import { ltpDayClass } from '$lib/format.js';
```

### D5: Legs total row — move TOTAL label to symbol column (lines 4619-4621)
Current:
```svelte
<span></span>
<span class="cand-total-label">TOTAL</span>
<span>—</span>
```
Change to:
```svelte
<span></span>
<span></span>
<span class="cand-total-label">TOTAL</span>
```

### D6: Stale cleanup
- Remove line 26: `import { positionsDayPnlStore } from '$lib/data/positionsDayPnlStore.svelte.js';`
- Remove line 62: vestigial `// applyUnderlyingTickLtp — tick patches now go through patchUnderlyingSpot` comment
- Remove `let _excludedByAccount = $state({});` (line ~3323) and its two write sites (~3510, ~3603)

## Change E — `frontend/src/lib/CandidateLegRow.svelte`
Find the LTP cell. Replace current text-color logic with:
```svelte
<span class="num {ltpDayClass(leg.change_pct ?? leg.day_change_pct)} {flashClass}">
  {priceFmt(leg.ltp)}
</span>
```
Where `flashClass` comes from the flash key for this leg. Check what field carries day% in the leg
data (`change_pct`, `day_change_pct`, or compute `(ltp - close_price) / close_price * 100`).
Import `ltpDayClass` from `$lib/format.js`.

## Change F — `frontend/src/lib/data/pulseColumns.js`

### F1: Add import
```javascript
import { aggCompact, ltpDayClass } from '$lib/format.js';
```

### F2: Update `mkExpPnlCol` valueFormatter (line ~770)
```javascript
valueFormatter: p => p.value != null ? aggCompact(p.value) : '',
```

### F3: Update `mkExtrinsicCol` valueFormatter (line ~795)
```javascript
valueFormatter: p => p.value != null ? aggCompact(p.value) : '',
```

### F4: Update `mkLtpCol` cellClass to use persistent day-change color
In `mkLtpCol`, the `cellClass` currently includes tick-direction text color classes. Change to apply
`ltpDayClass(p.data?.change_pct)` for persistent text color. Keep the background flash classes
(`tf-up` / `tf-down` from `_ltpFlashUp` / `_ltpFlashDown` sets) unchanged — they already do the
right thing. Remove any `ltp-tc-flash-*` text animation classes from `cellClass`.

## Change G — `frontend/src/routes/(algo)/admin/perf/+page.svelte` — text contrast fix

Secondary labels throughout the perf page use `--text-soft` (#94a3b8) which sits at ~4.7:1
contrast on the dark backgrounds — technically accessible but visually dim, especially in
bright environments. No shading needed — lift the secondary text color in the perf page's
scoped CSS block.

Add a scoped CSS variable override at the top of the `<style>` block in `perf/+page.svelte`:
```css
  :global(.perf-stat-label),
  :global(.perf-chart-label),
  :global(.perf-card-foot),
  :global(.perf-reg-metric),
  :global(.perf-reg-nums),
  :global(.perf-fn-page),
  :global(.perf-fn-line) {
    color: #c4d0e0;   /* lifted from --text-soft (#94a3b8) → ~7:1 contrast on dark bg */
  }
```
Or if these classes are defined in the scoped `<style>` block already, just change their
`color:` value directly from `var(--text-soft, #94a3b8)` to `#c4d0e0`.

Do NOT change `--text-soft` globally — the brighter shade is perf-page specific. Other
surfaces may rely on the original value.

**Label audit note:** All column/label names are consistent across surfaces. "Spot" vs "LTP"
is intentional (underlying index vs tradeable contract). No naming changes required.

## Files changed
- `frontend/src/lib/data/tickFlash.svelte.js`
- `frontend/src/lib/format.js` (new `ltpDayClass` export)
- `frontend/src/app.css` (new CSS classes)
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- `frontend/src/lib/CandidateLegRow.svelte`
- `frontend/src/lib/data/pulseColumns.js`
- `frontend/src/routes/(algo)/admin/perf/+page.svelte` (contrast fix)

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes (smoke — LTP cells have ltp-day-* class)
- vitest: no new tests needed (ltpDayClass is a pure function — add to format.test.js)

## Commit message
feat(ui): dual-signal LTP color — persistent day-change text + tick-direction bg flash; Exp P&L L/K format; legs TOTAL in symbol column; stale cleanup

## Done when
- LTP cells everywhere show green/red text permanently based on day% vs prev_close (tiered)
- Background pulse (tf-up/tf-down) fires on tick ↑/↓, suppressed on no-change
- Animated text-color flash (`ltp-tc-flash-*`) removed from LTP cells
- pulseColumns Exp P&L and Extrinsic show K/L/C format
- Legs TOTAL label in symbol column
- svelte-check 0 errors
- Day% cell in derivatives snapshot uses same ltpDayClass color

## Deferred
- SSOT: three diverging spot resolvers (`_resolveExpirySpot` / `_rootSpot` / `liveSpot`)
- SSOT: `hold_day` raw `day_change_val` at line 3591
