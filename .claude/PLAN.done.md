# Plan: LTP color SSOT + magnitude-based flash + unified threshold + CSS token cleanup

## Context

LTP and day % currently produce identical text color (`cell-pos/neg/flat`) but from
different data sources. Unifying to `change_pct`/`day_pnl_pct` field makes LTP color a
true SSOT.

Flash threshold: LTP uses operator-configurable `ltpFlashPct`; day % uses `0.001` hardcoded
floor. Unifying to one threshold (`ltpFlashPct`) applied to both eliminates the split.
Settings label updates from "LTP flash threshold" → "Price flash threshold (%)".

Flash intensity: currently all flash classes use a fixed starting color regardless of how
large the move was. Magnitude-based tiers make large moves stand out visually.

PerformancePage still uses background flash (`ltp-flash-up/down`) for P&L cascade; every
other surface uses text-color flash (`ltp-tc-flash-up/down`). Fix to complete parity.

## Task

Five changes, all frontend:

### Fix 1 — LTP cell color from `change_pct` field (SSOT)

**File:** `src/lib/data/pulseColumns.js`

**`_ltpCellClass()`:** Read `change_pct` (left grid) or `day_pnl_pct` (right grid) field
directly — same source as day % color. Fall back to `ltp vs close_price` when field absent.

```javascript
const pct = p.data?.change_pct ?? p.data?.day_pnl_pct ?? null;
const dir = pct != null
  ? (pct > 0 ? 'cell-pos' : pct < 0 ? 'cell-neg' : 'cell-flat')
  : (ltp > close ? 'cell-pos' : ltp < close ? 'cell-neg' : 'cell-flat');
```

Derivatives Snapshot spot (`_spotDir` in `+page.svelte`) stays as-is — no precomputed
`change_pct` field available for the underlying; `ltp vs spot_prev_close` is equivalent.

### Fix 2 — Magnitude-based flash intensity (3-tier)

**CSS tokens to add in `src/app.css` `:root`:**
```css
--algo-green-text-dim:    rgba(74, 222, 128, 0.50);   /* small move */
--algo-green-text-bright: #86efac;                     /* large move — lighter/whiter */
--algo-red-text-dim:      rgba(248, 113, 113, 0.50);
--algo-red-text-bright:   #fca5a5;
/* Background cascade tiers */
--algo-green-cascade-sm:  rgba(74, 222, 128, 0.07);
--algo-green-cascade-lg:  rgba(74, 222, 128, 0.25);
--algo-red-cascade-sm:    rgba(248, 113, 113, 0.07);
--algo-red-cascade-lg:    rgba(248, 113, 113, 0.25);
```

**New CSS classes in `src/app.css`** (text-color tiers):
```css
/* small: |Δ%| < 0.5 */
.ltp-tc-flash-up-sm   { animation: ltp-tc-flash-up-sm   500ms ease-out; }
.ltp-tc-flash-down-sm { animation: ltp-tc-flash-down-sm 500ms ease-out; }
/* default: 0.5 ≤ |Δ%| < 2 — existing ltp-tc-flash-up/down, unchanged */
/* large: |Δ%| ≥ 2 */
.ltp-tc-flash-up-lg   { animation: ltp-tc-flash-up-lg   500ms ease-out; }
.ltp-tc-flash-down-lg { animation: ltp-tc-flash-down-lg 500ms ease-out; }

/* background cascade tiers (tf-up/down analogues) */
.tf-up-sm   { animation: tf-up-sm   350ms ease-out; }
.tf-down-sm { animation: tf-down-sm 350ms ease-out; }
.tf-up-lg   { animation: tf-up-lg   350ms ease-out; }
.tf-down-lg { animation: tf-down-lg 350ms ease-out; }
```

**Keyframes**: same start→fade pattern as existing, using the dim/bright tokens.

**Bucketing helper in `src/lib/data/pulseColumns.js`:**
```javascript
// |pct| in percent (e.g. 1.5 means 1.5%)
function _flashTier(absPct) {
  return absPct >= 2 ? 'lg' : absPct < 0.5 ? 'sm' : '';
}
function _tcFlashClass(dir, absPct) {
  const t = _flashTier(absPct);
  return `ltp-tc-flash-${dir}${t ? '-' + t : ''}`;
}
function _bgFlashClass(dir, absPct) {
  const t = _flashTier(absPct);
  return `tf-${dir}${t ? '-' + t : ''}`;
}
```

Apply `_tcFlashClass` everywhere LTP and day % flash classes are chosen (replacing the
current fixed `ltp-tc-flash-up/down`). Pass `Math.abs(change_pct ?? ltpDeltaPct)` as
`absPct`. For LTP tick-bus flash where `change_pct` may not be available, compute
`absPct = Math.abs((ltp - prevLtp) / prevLtp * 100)` from tick delta.

Apply `_bgFlashClass` for P&L cascade flash rows.

### Fix 3 — Unified flash threshold (ltpFlashPct → both LTP and day %)

**File:** `src/lib/data/pulseColumns.js` and `src/lib/MarketPulse.svelte`

Day % flash currently uses `0.001` hardcoded floor. Change to read `ltpFlashPct` from
settings (already imported in MarketPulse / pulseColumns via the settings store). Gate:
```javascript
if (Math.abs(newPct - oldPct) < ltpFlashPct) return;   // replaces 0.001 check
```

**File:** `src/routes/(algo)/admin/settings/+page.svelte` (or wherever the ltpFlashPct
label is rendered)  
Change label from `"LTP flash threshold (%)"` → `"Price flash threshold (%)"` and update
tooltip/hint to note it applies to both LTP tick-bus and day % poll-diff flash.

### Fix 4 — PerformancePage P&L cascade flash (`src/lib/PerformancePage.svelte`)

Lines ~370–375: change `ltp-flash-up`/`ltp-flash-down` → tiered `_bgFlashClass` output
(import / inline the helper). Default to medium tier when P&L cascade magnitude is unknown.

```javascript
if (ltpFlashUp.has(symUpper))   return `${base} ${_bgFlashClass('up',   absPct)}`;
if (ltpFlashDown.has(symUpper)) return `${base} ${_bgFlashClass('down', absPct)}`;
```

If magnitude is not readily available in PerformancePage's LTP cascade path, use fixed
medium tier (`tf-up` / `tf-down`) as a safe fallback — still text-color consistent.

### Fix 5 — Tokenize remaining hardcoded directional hex in .svelte/.js files

**`src/lib/data/pulseColumns.js` (~line 502)** — GTT badge inline style:
- `'#4ade80'` → `'var(--algo-green)'`
- `'rgba(74,222,128,0.20)'` → `'var(--algo-green-badge)'`
  (add `--algo-green-badge: rgba(74, 222, 128, 0.20)` to `:root`)

**`src/lib/MarketPulse.svelte` (~line 4704)** — scoped style override:
- `color: #94a3b8` → `color: var(--algo-dim)`
- `rgba(148,163,184,0.08)` → `var(--algo-dim-bg)`
  (add `--algo-dim-bg: rgba(148, 163, 184, 0.08)` to `:root`)

**`src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` (~line 855)**:
- `color: #94a3b8` → `color: var(--algo-dim)`

**`src/routes/(algo)/admin/derivatives/+page.svelte` (~lines 5783–5784, 6006–6007)**:
- `#86efac` → `var(--algo-green-text-bright)` (reuses Fix 2 token)
- `#fca5a5` → `var(--algo-red-text-bright)` (reuses Fix 2 token)

## Agents

- frontend: Implement all five fixes.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- vitest: no

## Commit message
fix(ui): magnitude-based flash tiers; unified price flash threshold; LTP color from change_pct SSOT; P&L cascade flash parity; tokenize directional hex

## Done when
- LTP cell color reads `change_pct`/`day_pnl_pct` field; fallback to `ltp vs close`
- Flash intensity varies by 3 tiers (`-sm` / default / `-lg`) for both text-color (LTP/day%) and background (P&L cascade) flash
- `ltpFlashPct` threshold applied to day % poll-diff flash (replaces `0.001` hardcoded floor)
- Settings label updated to "Price flash threshold (%)"
- PerformancePage P&L cells use `tf-up/down` (background) tiered flash on LTP cascade
- No `#4ade80`, `#f87171`, `#94a3b8`, `#86efac`, `#fca5a5` hardcoded in .svelte or .js files (outside `:root` token definitions)
- svelte-check exits 0
