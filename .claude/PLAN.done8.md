# Plan: Flash discipline — ltp/spot/chg% only; subtle NavStrip heartbeat

## Context
Rule: only `ltp`, `spot`, and `chg%` columns should produce a flash animation on any
surface. Everything else (day_pnl, pnl, cur_val, etc.) may be refreshed for valueGetter
re-evaluation but must NOT trigger a visible flash CSS animation. Audit found two
violation categories and a heartbeat that is too intense.

## Violations to fix

### 1 — PerformancePage: pnl + day_pnl get tf-up/tf-down flash (violation)
File: `frontend/src/lib/PerformancePage.svelte`

The `_perfFlash.classOf()` callback is applied to pnl and day_pnl column cellClass
definitions. This applies `tf-up`/`tf-down` (350ms background flash) to those cells on
every LTP tick. Fix: remove `_perfFlash.classOf()` from the pnl and day_pnl column
cellClass. Keep it on day_pnl_pct (chg%) only. The LTP column flash is correct — keep.

### 2 — PositionStrip: per-tick rainbow shimmer on entire strip (violation)
File: `frontend/src/lib/PositionStrip.svelte`

`cell-freshness-pulse` (rainbow-fade 1.0s) fires on every SSE tick via tickBus and
blankets the whole strip. This is not scoped to ltp/chg% values — it fires on any tick.
Fix: remove the rainbow shimmer entirely from PositionStrip. The amber heartbeat (poll
cycle) is sufficient feedback. If tick-arrival indication is desired, scope it narrowly
to the ltp display cell in the strip, not the whole strip.

### 3 — PositionStrip heartbeat: too intense, should be subtler
File: `frontend/src/lib/PositionStrip.svelte` + `frontend/src/app.css`

Current `ps-heartbeat-pulse` peaks at `rgba(251,191,36, 1.00)` + `box-shadow: 0 2px 10px
0 rgba(251,191,36, 0.55)` at the 30% mark — very visible. The heartbeat is correctly
triggered by `_dataChangedTick` (fires when broker poll returns new data, ~30s cycle).
The trigger is correct; make the animation more subtle:

```css
@keyframes ps-heartbeat-pulse {
  0%   { border-bottom-color: rgba(251, 191, 36, 0.20); }
  30%  { border-bottom-color: rgba(251, 191, 36, 0.55); }
  100% { border-bottom-color: rgba(251, 191, 36, 0.20); }
}
```
Remove the box-shadow entirely. Peak opacity drops from 1.00 → 0.55, resting from 0.30 → 0.20. No box-shadow glow.

### 4 — Derivatives snapshot grid: Day P&L flash (violation)
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (line ~4769)

The snapshot grid renders a row per underlying group. The Day P&L span has
`{flash.classOf(`${g.underlying}:day_w`)}` — this applies a flash animation to Day P&L
on every LTP tick. Only ltp and chg% cells should flash. Fix: remove
`{flash.classOf(`${g.underlying}:day_w`)}` from that span. The LTP and CHG% cells
in the same row already use `flash.classOf(...)` correctly — keep those.

### 5 — Legs + exp close symbol column: match positions symbol visual style
File: `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`

The symbol span uses `font-mono cand-sym cand-sym-acct` with a nested `sym-main` span.
PerformancePage positions symbol uses `perf-sym-cell`. The text-formatting logic
(rootOfLabel / formatSymbol) is already consistent. The visual mismatch is CSS:

- Read `frontend/src/app.css` and PerformancePage-scoped CSS to find `.perf-sym-cell`
  font-family / font-size / font-weight / color definitions.
- Align CandidateLegRow's outer symbol span and `.sym-main` to match `perf-sym-cell`
  in font treatment. If `perf-sym-cell` is not monospace, remove `font-mono` from
  CandidateLegRow's outer symbol span.
- Keep chip decorations (STOCK, PROXY, CLOSED, OPEN, ~, D) — informational only.
- Keep CE/PE option-type coloring (`sym-ce` / `sym-pe`) — derivatives context only.

This change applies to both legs and exp close tabs (both rendered by CandidateLegRow).

### 6 — Payoff overlay spot + chg%: already implemented, verify after liveSpot fix
File: `frontend/src/lib/OptionsPayoff.svelte` (lines 422-757)

Flash already exists:
- SPOT: `_spotFlash.classOf('spot')` applied to the LTP `<span>` — background flash on every spot change
- CHG%: `_tcFlashClass(dir, mag)` applied to the CHG% `<span>` when `_spotFlash` is active — text-color flash

These were invisible before because `liveSpot` was falling through to `strategy.spot` (5s stale poll), so spot barely changed and flash barely fired. After our liveSpot fix (deployed), spot now updates at SSE tick cadence → flash fires on every tick. No code change needed. Verify in browser.

If flash still doesn't appear for GOLDM: the `spot_anchor_contract` for GOLDM is not resolving to a subscribed symbol in symbolStore. That's a subscription/token gap, not a flash bug — surfaced correctly now that fallback tiers are removed.

## Surfaces that are already correct (no change needed)

- **MarketPulse grids**: ltp + day_pnl_pct flash via `_ltpFlashClass` + `tf-up/down`.
  day_pnl / pnl / cur_val are in cascade refreshCells for valueGetter re-evaluation but
  their cellClass returns only `RA` / `dirCls` — no flash animation. ✓
- **Pinned / Watchlist grids**: only ltp, sparkline, left_change_pct refresh. ✓
- **Gainers / Losers grids**: only ltp, sparkline in flash path. ✓
- **PerformancePage LTP column**: ltp-flash-up/down + tc-flash correct. ✓
- **Derivatives legs grid (LTP + chg%)**: CandidateLegRow flash already scoped to LTP/chg%. ✓
- **Derivatives exp close grid**: uses same CandidateLegRow component — row formatting
  (background, text color, layout) is already identical to legs. ✓

## Agents
- backend: skip
- frontend: Make five targeted edits:
  1. `PerformancePage.svelte`: Find every column definition that applies `_perfFlash.classOf()`
     to its cellClass. Remove it from pnl and day_pnl columns — keep only on day_pnl_pct
     (chg%). Read the file first to identify exact line numbers.
  2. `PositionStrip.svelte`: Remove the `cell-freshness-pulse` shimmer — find the tickBus
     subscription that calls `_shimmer.notify('strip')` and the binding that applies
     `cell-freshness-pulse` class to the strip element, and remove both. Verify no other
     tick-per-event animation remains on the strip element itself.
  3. `app.css`: Update `ps-heartbeat-pulse` keyframes — remove box-shadow lines, reduce
     peak amber from 1.00→0.55 and rest from 0.30→0.20.
  4. `derivatives/+page.svelte` (snapshot grid, line ~4769): Remove
     `{flash.classOf(`${g.underlying}:day_w`)}` from the Day P&L span only. LTP and CHG%
     flash calls in the same row stay untouched.
  5. `CandidateLegRow.svelte` (symbol column): Read `app.css` to find `.perf-sym-cell`
     font definitions; align the outer symbol span (and `.sym-main`) font treatment to
     match. If `perf-sym-cell` is not monospace, remove `font-mono` from the outer span.
     Preserve all chip decorations and CE/PE coloring.
  Write/update tests: add a Vitest test asserting `_perfFlash.classOf()` is NOT in the
  pnl column's cellClass callback return value (or verify by checking the flash is scoped
  to day_pnl_pct only).
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(flash): restrict flash to ltp/chg% only; remove PositionStrip rainbow shimmer; subtle heartbeat; align legs/exp-close symbol style

## Done when
- PerformancePage: only ltp and day_pnl_pct columns have flash CSS applied
- PositionStrip: no rainbow shimmer on SSE ticks; amber heartbeat remains (poll cycle only)
- Heartbeat CSS: no box-shadow; peak amber 0.55, rest 0.20
- Derivatives snapshot grid: Day P&L cell has no flash class
- CandidateLegRow symbol column: font treatment matches perf-sym-cell
- svelte-check 0 errors
