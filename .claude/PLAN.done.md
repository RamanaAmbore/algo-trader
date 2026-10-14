# Plan: Panel header + grid consistency — shared CSS variables for border/gap, grid header look, and CardHeader padding

## Task

Five related visual-consistency fixes, all confirmed via investigation (not
assumed), full rationale in `~/.claude/plans/purrfect-marinating-pixel.md` — read it
for complete context before starting:
(A) a shared gap variable fixing a "double border" stacking artifact where a grid
sits directly below a CardHeader with zero gap; (B) shared grid-header-look CSS
variables (background/font-size/weight/letter-spacing/height) standardizing every
grid-style header in the app on the Derivatives Legs/Exp-close table's
already-good-looking values; (C) removing 3 divergent local `--ch-padding`
overrides so CardHeader's height is identical everywhere, while preserving every
panel's own intentional label color; (D) moving the public site's desktop gold
hairlines closer to the content column; (E) fixing the CLI dropdown's
hover-description tooltip so it overlays instead of shifting content below it.

## Agents

- backend: skip
- frontend: Implement all 5 parts from the plan doc.
  (A) In `frontend/src/app.css`, add `--grid-header-gap: 0.4rem`. In
  `frontend/src/lib/MarketPulse.svelte`, add `margin-top: var(--grid-header-gap)`
  to the `.bucket-grid`/`.summary-grid`/`.funds-grid` wrapper rule(s) (whichever
  CSS selector actually wraps these elements directly below their CardHeader).
  In `frontend/src/lib/NavBreakdown.svelte`, add the same `margin-top` to
  `.nav-bd-wrap`. Then, now that a real gap exists, make each grid's own border
  render as a complete, clean frame: confirm `.ag-root-wrapper`'s internal border
  is zeroed consistently in both MarketPulse (already done) and NavBreakdown
  (currently missing — add it), so the visible border around each grid is exactly
  ONE line (the `.ag-theme-algo` class's own border), not zero and not multiple.
  (B) In `frontend/src/app.css`, add `--grid-header-bg: rgba(15,23,42,0.65)`,
  `--grid-header-font-size: 0.65rem`, `--grid-header-font-weight: 800`,
  `--grid-header-letter-spacing: 0.06em`, `--grid-header-height: 30px`. Update
  `.ag-theme-algo .ag-header`'s background/font-size/font-weight/letter-spacing
  declarations and `--ag-header-height` to reference these new variables instead
  of their current hardcoded literals. Then update
  `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`'s `.cand-headrow` and
  `.byund-headrow` rules to ALSO reference these same new variables instead of
  their own separately-hardcoded (currently matching) values — single source of
  truth, not a coincidental match. After this change, visually confirm every real
  ag-Grid consumer app-wide (MarketPulse, NavBreakdown, BrokerHealthBadge,
  PerformancePage's embedded-banner mode) still reads correctly at the new
  darker/bolder look — not just the panels named in the original complaint.
  (C) Remove the `--ch-padding` override in `MarketPulse.svelte`'s `.mp-bucket-wrap`
  rule, the inline `style="--ch-padding:..."` on `SymbolPanel.svelte`'s
  order-ticket-modal CardHeader wrapper, and the inline `style="--ch-padding:..."`
  on `frontend/src/routes/(algo)/orders/+page.svelte`'s outer Order-Entry section —
  all three should inherit the shared body-level default. Do NOT touch any `color`
  rule on `.mp-bucket-label`, `.oes-modal-name`, or `.oc-entry-label` — those
  colors are confirmed intentional and must survive unchanged. For
  `.oc-entry-label`'s letter-spacing (currently `0.08em`, double the shared
  `0.04em` default): either converge it to `0.04em` or state a real reason to keep
  it distinct in your handback — don't silently pick either way.
  (D) In `frontend/src/routes/(algo... wait this is public)/layout — in
  `frontend/src/routes/(public)/+layout.svelte`, reduce `--b8-margin-offset`'s
  value from `24px` to `12px` (starting point — adjust after a visual check if it
  looks wrong). This value feeds into the ellipse-vignette flat-zone math
  documented in the same comment block (B8 decoration) — re-read that comment,
  confirm the vignette still reads cleanly at the new offset, not just the
  hairline position in isolation.
  (E) In `frontend/src/lib/command/AgentCliEditor.svelte`, move the
  `.agent-cli-suggest-desc` element (currently rendered in normal document flow
  right after `.agent-cli-input-wrap` closes, lines ~546-548, appearing via
  `{#if _hoverDesc}`) so it renders INSIDE `.agent-cli-dropdown` instead (e.g. as a
  description footer row within that already-correctly-`position:absolute` panel,
  shown only while `_hoverDesc` is set) — so it overlays rather than occupying
  flow and shifting the compile-errors/JSON-preview area below it.
  For every change, start a local dev server and visually confirm the before/after
  look in a real browser — this is a visual-design change; svelte-check passing
  alone is not sufficient evidence of correctness. Run `npx svelte-check --output
  machine` (0 errors required) and `npx vitest run` (full suite, 0 regressions)
  before finishing.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Add/update coverage for: (a) no visible gap/overlap between
  CardHeader and a grid on MarketPulse's Positions panel and NavBreakdown
  post-fix, confirming a deliberate small gap now exists (bounding-box check);
  (b) the real ag-Grid header's computed background-color/font-size/font-weight on
  at least 2 real ag-Grid panels (MarketPulse, NavBreakdown) now match the new
  shared variable values, and the Derivatives hand-rolled tables render identically
  to before (same computed values, now variable-driven); (c) CardHeader padding is
  identical across MarketPulse/SymbolPanel-modal/Orders-page/Derivatives/
  Automation/Dashboard; (d) label text colors on MarketPulse buckets, SymbolPanel
  modal, and Orders page are UNCHANGED (regression guard); (e) hovering a CLI
  dropdown suggestion row does not shift the compile-errors/JSON-preview area
  below it (bounding-box of that area before/after hover, must be identical).

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message

fix(ui): standardize grid-adjacency gap, grid header look, and CardHeader padding via shared CSS variables; fix CLI dropdown layout shift; tighten public-site hairline spacing

## Done when

- No panel with a grid/table directly below its CardHeader shows a stacked/
  thickened double-border — a consistent gap exists everywhere, matching the
  Derivatives reference pattern.
- Every grid-style header in the app shares the same background/font/height,
  driven by one set of CSS variables.
- CardHeader's padding/height is identical across every panel, with each panel's
  intentional label colors preserved.
- The public site's desktop gold hairlines sit closer to the content column.
- Hovering a CLI dropdown suggestion never shifts content below it.
- svelte-check 0 errors, full Vitest suite green, new/updated Playwright specs
  green, visually confirmed in a real browser.
