# Plan: UI Polish Round 4 — Signals border, bio buttons, ag-Grid shared defaults + row shading consistency, card reuse, timestamp fix

## Context

Several UI polish items queued after Round 3 ship:
- Signals button inactive border is too faint (`--algo-cyan-border-soft` = rgba cyan at low opacity)
- Bio contact buttons in showcase lack visual cohesion with the sky-blue attribution panel
- Derivatives page footnote "* numerical max/min within ±3.0σ..." is noise — remove it
- Mobile timestamp blinks indefinitely (animation stays infinite even when no data) and tap does nothing (iOS Safari ignores click on spans without `cursor: pointer`)
- ag-Grid has duplicated formatters and divergent config across MarketPulse/dashboard/perf — extract shared defaults
- Card header patterns diverge: dashboard Capital/Equity hand-rolls a `bucket-header` instead of using CardHeader; admin/perf duplicates the card grid structure twice

## Task

1. **Signals border** — `ChartWorkspace.svelte:2523`: change `.cw-signals-btn` inactive border from `var(--algo-cyan-border-soft)` to `var(--algo-cyan-border)` so the button is visible before activation.

2. **Bio buttons** — `showcase/+page.svelte` `.show-contact-btn`: align border and text color with the sky-blue panel (`#7dd3fc` scheme). Change border from `rgba(126,151,184,0.42)` → `color-mix(in srgb, #7dd3fc 35%, transparent)`, bg from `rgba(71,100,140,0.12)` → `color-mix(in srgb, #7dd3fc 8%, transparent)`, color from `rgba(226,232,240,0.88)` → `#cbd5e1`, hover border → `color-mix(in srgb, #7dd3fc 55%, transparent)`, hover color → `#e2e8f0`.

3. **Remove derivatives footnote** — `derivatives/+page.svelte:4741-4750`: delete the entire `<div class="text-[0.5rem]...">` block (10 lines) containing "* numerical max/min within ±{span_sigmas}σ...".

4. **Math term hints — Greeks + Risk labels** — `derivatives/+page.svelte`:
   - **Greeks block (lines 4670-4689)**: Each Greek row currently has `title=""` only — doesn't fire on mobile. Replace with `<InfoHint popup text="...">` inline in the `.kv-k.kv-k-greek` span. Reuse the existing title text as the InfoHint `text` prop. Keep `kv-pair` structure unchanged. Δ text: "Delta — net directional exposure. +50 ≈ ₹50 gained per ₹1 spot rise. Includes +qty for enabled equity-holding legs."; Γ: "Gamma — rate-of-change of delta as spot moves. High Γ = position is becoming more/less directional quickly."; Θ: "Theta — daily decay in rupees. Positive when net short premium. A Θ of −5 = position loses ₹5/day from time decay alone."; 𝒱: "Vega — P&L change per 1% IV move. Positive = long volatility (benefits from IV expansion)."; ρ: "Rho — sensitivity to a 1% rate change. Mostly cosmetic for short-dated index options."
   - **Risk labels (lines 4700-4734)**: InfoHint chips already exist but are hard to discover (0.75rem chip). Add visual affordance to the label text: add CSS for `.kv-k { cursor: help; }` and `.kv-k:not(.kv-k-greek) { border-bottom: 1px dashed rgba(148,163,184,0.3); }` so the label text itself signals it's interactive. The existing InfoHint chips remain — the dashed underline makes the whole label area scannable as "has explanation". Apply in the component's `<style>` block.

5. **Timestamp mobile fix** — `app.css`: 
   - Add `.algo-ts { cursor: pointer; }` so iOS Safari fires tap events (currently only `.algo-ts-data` has it).
   - Change `algo-ts-pulse` from `infinite` to `animation-iteration-count: 3` (4.5 s of pulse then stops; indicates loading state, not a permanent blink).

6. **ag-Grid shared defaults + row shading consistency** — Two sub-items:
   - **Shared defaults**: Create `frontend/src/lib/data/gridDefaults.js` with `GRID_BASE_OPTS` (suppressMovableColumns, suppressCellFocus, animateRows, domLayout, headerHeight:28) and shared value formatters `fmtNum(dp)`, `fmtPct`, `fmtCcy` extracted from MarketPulse/dashboard duplicates. Import and use in MarketPulse.svelte, dashboard/+page.svelte, PerformancePage.svelte — replace inline duplicates. Do NOT change rowHeight (26/28/default intentional per grid), do NOT change theme tokens.
   - **Row shading consistency**: `CandidateLegRow.svelte` (Legs grid) has no alternating row shading while the Snapshot grid on the same Derivatives page has `.byund-row:nth-of-type(odd) > span { background-color: rgba(13,22,42,0.30); }`. Add the same pattern to `.cand-row` in `CandidateLegRow.svelte` — add `.cand-row:nth-of-type(odd) { background: rgba(13,22,42,0.30); }`. Also update both grids to reference `var(--ag-odd-row-background-color, rgba(13,22,42,0.30))` so they stay in sync when the theme variable changes.

7. **Card reuse** — Two targeted extractions:
   - **PerformanceCardGrid.svelte** (new component in `src/lib/`): extract the duplicated 80-line `<article class="perf-card">` block that appears twice in `admin/perf/+page.svelte` (FE grid lines ~357-450, BE grid lines ~451-553). Props: `cards`, `historyData`, `regressionMap`, `isFE`. Move `.perf-card*` scoped CSS into the new component.
   - **Dashboard Capital/Equity** (`dashboard/+page.svelte` lines ~2068-2212): replace hand-rolled `<div class="bucket-header">` with CardHeader using its `middle` slot for AlgoTabs. Wire `detectOverflow={true}` if needed.

## Agents

- frontend: Implement all 7 items above. Files to touch: `frontend/src/app.css` (timestamp cursor + animation), `frontend/src/lib/ChartWorkspace.svelte:2523` (signals border), `frontend/src/routes/(algo)/showcase/+page.svelte:507-526` (bio buttons), `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:4741-4750` (remove footnote) + Greeks InfoHints (lines 4670-4689) + Risk label CSS + Snapshot row shading to use CSS variable, `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` (add alternating row shading), `frontend/src/lib/data/gridDefaults.js` (new — GRID_BASE_OPTS + shared formatters), `frontend/src/lib/MarketPulse.svelte` (import gridDefaults), `frontend/src/routes/(algo)/dashboard/+page.svelte` (import gridDefaults + CardHeader for cap/eq bucket), `frontend/src/routes/(algo)/admin/perf/+page.svelte` (import gridDefaults + use new PerformanceCardGrid), `frontend/src/lib/PerformanceCardGrid.svelte` (new component). Scope discipline: only change what's listed. Do not alter rowHeight values (26/28/default) or ag-Grid theme class names.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add/update Playwright smoke tests for: (1) Showcase bio buttons visible with correct border color, (2) Derivatives page does NOT contain "numerical max/min" text, (3) Signals button has visible border in inactive state, (4) Legs rows alternate background color (check computed style on even/odd .cand-row), (5) Greeks InfoHint buttons are present on the derivatives page (aria-label or role=button within .kv-k-greek). Existing specs in `frontend/tests/` — add assertions to relevant spec files.

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

```
feat(ui): Polish Round 4 — signals border, bio buttons, math hints, ag-Grid consistency + shared defaults, card reuse, timestamp fix

- Signals button: inactive border raised from border-soft → border (visible at rest)
- Bio contact buttons: border/bg/color aligned to sky-blue attribution scheme
- Derivatives: remove numerical footnote (max/min ±σ% spot range); Greeks Δ/Γ/Θ/𝒱/ρ get
  InfoHint popup (replacing title="" which doesn't fire on mobile); Risk labels get
  dashed underline + cursor:help to signal "tap for explanation"
- Timestamp: add cursor:pointer to .algo-ts for iOS tap; pulse animation stops after 3 cycles
- ag-Grid row shading: add alternating row bg to Legs grid (CandidateLegRow.svelte);
  unify Snapshot + Legs to use --ag-odd-row-background-color CSS variable
- ag-Grid: extract GRID_BASE_OPTS + shared formatters into gridDefaults.js; import in
  MarketPulse, dashboard, PerformancePage — eliminate inline duplicates
- Card reuse: extract PerformanceCardGrid.svelte (dedup 80-line block in admin/perf);
  migrate dashboard Capital/Equity bucket-header → CardHeader middle slot
```

## Done when

- Signals button border visible in inactive state (visible cyan ring at rest)
- Bio contact buttons use sky-blue border that reads against the attribution panel bg
- Derivatives page has no "* numerical max/min within" text
- Greeks (Δ, Γ, Θ, 𝒱, ρ) each have an InfoHint popup button (not just title attribute)
- Risk section labels (R:R, POP, EV, etc.) show dashed underline + cursor:help to signal they carry explanation
- Mobile: `.algo-ts` has `cursor: pointer`; pulse animation stops after ~4.5s (3 iterations)
- `gridDefaults.js` exists; no duplicate `fmtNum`/`fmtPct`/`fmtCcy` inline in the 3 grid pages
- Legs grid (`.cand-row`) has alternating row shading matching Snapshot grid visually
- Both Snapshot (`.byund-row`) and Legs (`.cand-row`) use `var(--ag-odd-row-background-color, rgba(13,22,42,0.30))` for odd rows
- `PerformanceCardGrid.svelte` exists; `admin/perf/+page.svelte` renders both grids via it
- Dashboard Capital/Equity card uses CardHeader (no `bucket-header` div)
- svelte-check: 0 errors
- Playwright: smoke tests pass for bio buttons, derivatives footnote removed, signals border
