# Plan: InfoHint tooltip consistency — full NavStrip panel style everywhere

## Context

Operator flagged that InfoHint tooltips render inconsistently across the app: NavStrip's
P/H/M/C pills use a richer "panel" presentation (gradient background, `accentColor`-tinted
left border, uppercase titled header — `InfoHint.svelte:320-346`) while ~75 other tooltip
call sites across 13 files use a plain flat-slate popup with no title. Operator confirmed
via screenshot (NavStrip "H" panel: amber section headers, bold white body, rounded
gradient card) that this exact look, triggered on both hover and click/press, is the
desired universal format — not a lighter title-only compromise.

A 5-agent council (architect/ux/risk/devil/perf) reviewed the approach before this was
finalized. Architect/perf/risk approved the mechanism: `panel`/`accentColor`/`title` props
already exist and already power `PositionStrip.svelte`'s pills — this is a call-site
styling migration, not new component code, with zero measurable perf cost (popout is
`{#if visible}`-gated, outside any hot tick-render path) and no data/P&L path touched.
UX and devil's-advocate flagged that `InfoHint.svelte`'s own code comment (lines 278-283)
documents the panel look was previously reverted from being app-wide for "shouting louder
than the helper text it was framing," and reintroducing it into dense multi-chip rows
(Greeks, payoff stat strip) risks the same regression at higher density. Operator saw this
trade-off and reaffirmed intent with the screenshot, so proceeding with the full migration —
care is taken on viewport/overlap behavior in the two densest rows during verification
(see Tests section).

## Approach

Add `panel` + `title={<site's own existing visible stat/field label, not new copy>}` to
every non-NavStrip `<InfoHint>` call site. Omit `accentColor` everywhere except NavStrip's
4 pills — it falls back to the component's shared default (`var(--algo-amber)`), avoiding
an arbitrary per-metric color scheme that only makes sense for 4 adjacent, differentiated
pills. `popup` mode is already hover-or-click reactive
(`InfoHint.svelte:103`: `visible = popup ? (open || hovered) : open`), so no new trigger
logic is needed anywhere that's already `popup` mode.

Call sites to migrate (same one-line edit pattern repeats ~75 times across 13 files):
- `frontend/src/lib/PnlPanel.svelte:113`
- `frontend/src/lib/ChartWorkspace.svelte:2377-2405` (6 Greek tooltips)
- `frontend/src/lib/OptionsPayoff.svelte:906-1008` (8 stat-row tooltips: LTP/Chg%/P.Close/
  Day/Adj/DTE/IV)
- `frontend/src/lib/order/OrderTicket.svelte:3062`
- `frontend/src/lib/execution/SimulatorPanel.svelte:1132,1137`
- `frontend/src/routes/(algo)/strategies/[id]/+page.svelte:279-333` (8 metric tooltips)
- `frontend/src/routes/(algo)/admin/research/+page.svelte:480`
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:5507-5579` (`hideButton`+
  `anchor` Greek+EV header chips, 6 — keep the anchor/click-outside wiring untouched, add
  `panel`+`title` only) and `:6158-6225` (Strategy Summary kv rows — Δ/Γ/Θ/𝒱/ρ, R:R,
  Risk-of-ruin, Breakevens, POP, EV, EV/cost, aggregate-greeks note — 14)
- `frontend/src/routes/(algo)/automation/+page.svelte:774-986` (7 form-field hints)
- `frontend/src/routes/(algo)/automation/templates/+page.svelte:305-539` (4)
- `frontend/src/routes/(algo)/admin/metrics/+page.svelte` and `admin/perf/+page.svelte`
  (17 `content={METRIC_META...}` sites) — add `panel`+`title={<metric label>}`, leave the
  structured What/Ideal/Impact/Fix `<dl>` body unchanged; `panel` is chrome-only CSS on the
  wrapping `.info-popout`, independent of body content shape
- `frontend/src/routes/(algo)/admin/settings/+page.svelte:448` — the one site NOT already
  in `popup` mode (today it's an inline-expand-below-chip). Convert to `popup panel` so it
  gains the same floating hover/press popup mechanism as every other site, per the
  operator's explicit "all other tooltips hovering or pressing should show similar popup."

## Known gotcha (risk council finding)

`frontend/src/lib/__tests__/optionsPayoffStatInfoHint.test.js:65-69` regex-asserts no
unescaped `P&L` inside any `<InfoHint ...>` tag. Any new `title=` text containing "P&L"
must be written as `P&amp;L` to avoid a false-positive test failure (safe at runtime
either way — `title` is plain-text interpolated, not `{@html}`).

## Agents

- frontend: Migrate all ~75 non-NavStrip InfoHint call sites listed above to add `panel` +
  `title` (reuse each site's existing visible label text as the title; escape any literal
  "P&L" as "P&amp;L" per the gotcha above). Convert `admin/settings/+page.svelte`'s single
  inline-mode InfoHint to `popup panel`. Do not change `content=` grid bodies, `text=` HTML
  bodies, or any `hideButton`/`anchor` wiring — this is a styling-prop addition only, no
  logic changes.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: After the frontend change, verify/extend
  `derivatives_greek_header_chip_infohint.spec.js`, `options_payoff_stat_infohints.spec.js`,
  and `metrics_tooltip.spec.js` to confirm the panel popout doesn't clip/overflow the
  viewport or occlude sibling stats in the two densest rows (derivatives Strategy Summary,
  OptionsPayoff stat strip) at both desktop and mobile widths — this directly checks the
  risk the UX/devil council flagged.

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes — `derivatives_greek_header_chip_infohint.spec.js`,
  `options_payoff_stat_infohints.spec.js`, `metrics_tooltip.spec.js`
- vitest: `optionsPayoffStatInfoHint.test.js`, `InfoHint.sourceAudit.test.js`,
  `strategyDetailMetricsInfoHint.test.js`

## Commit message

fix(ui): standardize InfoHint tooltips on NavStrip's panel presentation

## Done when

Every InfoHint tooltip in the app (except the 4 NavStrip pills, already canonical) opens on
hover or click as a titled gradient panel matching NavStrip's P/H/M/C look; no panel
overflows/clips the viewport or occludes adjacent stats in the two densest rows; the named
vitest specs pass with the P&L-escaping gotcha handled; svelte-check and the named
Playwright specs pass.
