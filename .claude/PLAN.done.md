# Plan: Label Alignment + Column Unification

## Context

Three groups of UX/consistency issues across the frontend:

**1. Payoff overlay non-standard labels (stat panel + hover tooltip + legend)**
- `DAY Δ` — Δ means the options Greek "Delta" everywhere else in the app. Using it to mean "Day P&L" is confusing. Every other screen says "Day P&L".
- `TDAY` — Bloomberg shorthand, not standard. Appears in the stat panel (line 764) AND the hover tooltip (line 1221). Should be `TODAY` (all caps, matching other stat panel labels like SPOT, CLOSE, DTE).
- `Today (BS)` in the chart legend — "(BS)" is Black-Scholes jargon. Drop it: just `Today`.
- Tooltip prose on TDAY and ADJ rows still references "TDAY" and "DAY Δ" — update to match.

**2. "Winners" is not Indian market standard**
NSE, BSE, Zerodha, Moneycontrol, Groww all use **Gainers / Losers**. "Losers" is already correct and stays unchanged.

**3. Sparkline missing right border in bucket grids**
Global border-strip rule removes all cell borders in `.mp-bucket-wrap` grids. The sparkline (5d) column has no right separator before LTP.

**4. PerformancePage Holdings missing `Invested` column**
MarketPulse positions right grid has: Symbol | 5d | LTP | Avg | Day P&L | Day % | Close | P&L | P&L % | Qty | Lots | **Invested** | Value | ...
PerformancePage `holdingsCols` has: Symbol | LTP | Avg | Day P&L | Day % | P&L | P&L % | Close | Qty | Lots | Weight % | Value | Account — missing `Invested`.

**Label naming decisions (from review):**
- `Day P&L` + `Day %` stays as the standard pair for positions (absolute + %). `Day %` is the accepted short form of "Day P&L %", consistent with the existing `P&L` / `P&L %` pair. No rename to `Chg %`.
- The stat panel uses ALL CAPS for all labels (SPOT, CLOSE, EXP, DTE). New labels follow that: `DAY P&L`, `TODAY`.
- The hover tooltip follows the same ALL CAPS as the stat panel.
- The chart legend uses Title Case (`Today`, `Expiry`, `Breakeven`) — keep that.

---

## Files to Modify

### 1. `frontend/src/lib/OptionsPayoff.svelte`

**Stat panel label changes:**
- Line 752: `<span class="ps-k">DAY Δ</span>` → `<span class="ps-k">DAY P&amp;L</span>`
- Line 764: `<span class="ps-k">TDAY</span>` → `<span class="ps-k">TODAY</span>`

**Stat panel tooltip prose (title attributes):**
- Line 751 (DAY P&L row title): already refers to "today's mark-to-market change" — fine, no change needed to prose
- Line 761-763 (TODAY row title): update `"NOT today's delta — use the DAY Δ row above for that."` → `"NOT today's intraday move — use the DAY P&L row above for that."`
- Line 778 (ADJ row title): `"Adjustment folded into TDAY"` → `"Adjustment folded into TODAY"`

**Comment at line 748:**
`"Operator can scan TDAY (lifetime) vs DAY (today's delta) at a glance."` →
`"Operator can scan TODAY (lifetime P&L at spot) vs DAY P&L (today's intraday move) at a glance."`

**Hover tooltip (chart crosshair):**
- Line 1221: `<span class="chart-tooltip-label">TDAY</span>` → `<span class="chart-tooltip-label">TODAY</span>`

**Chart legend:**
- Line 1244: `Today (BS)` → `Today`

### 2. `frontend/src/lib/MarketPulse.svelte`

**"Winners" → "Gainers" (display text only; internal `value: 'winners'` key, CSS class `.mp-bucket-label-winners`, and `cardId="pulse-winners"` unchanged):**
- Line 431: `label: 'Winners'` → `label: 'Gainers'`
- Line 3988: `label="Winners"` (CardHeader prop) → `label="Gainers"`
- Line 3994: `>Winners<` (display span) → `>Gainers<`

**Sparkline right border — add after the global border-strip rule (after line 4763):**
```css
/* Sparkline cell exception — restore right border as visual separator
   between the 5d chart column and LTP. */
:global(.mp-bucket-wrap .ag-theme-algo .ag-cell.spark-cell) {
  border-right: 1px solid var(--algo-amber-border-soft) !important;
}
```

### 3. `frontend/src/lib/PerformancePage.svelte`

**Add `Invested` to `holdingsCols` — insert after the `Lots` column (after line 527), before `mkWeightPctCol`:**
```js
{ field: 'inv_val', headerName: 'Invested', width: 88,
  valueGetter: (p) => (p.data?.average_price ?? 0) * Math.abs(p.data?.quantity ?? 0),
  valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
```

---

## Agents
- frontend: Implement all changes in OptionsPayoff.svelte, MarketPulse.svelte, PerformancePage.svelte as described. Run svelte-check after.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): label alignment — DAY P&L/TODAY in overlay+tooltip, Gainers, sparkline border; Invested in PerformancePage holdings

## Done when
- Payoff stat panel shows `DAY P&L` and `TODAY` (all caps, matching SPOT/CLOSE/DTE).
- Chart hover tooltip shows `TODAY`.
- Chart legend shows `Today` (no "(BS)").
- Tooltip prose updated — no stale "TDAY" or "DAY Δ" references in title text.
- Winners card shows `Gainers`; Losers unchanged.
- Sparkline column has a visible right border in bucket grids.
- PerformancePage Holdings has `Invested` column between Lots and Weight %.
- svelte-check 0 errors.
