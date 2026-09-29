# Plan: heartbeat contrast revert, Payoff spinner repositioning, Performance-page tab colors, chain submit tooltip

## Context

Five distinct, unrelated visual bugs reported and grounded via 3 parallel research
agents (facts below, not guesses):

1. **PositionStrip heartbeat pulse now invisible against the stale-data state.**
   A prior-session fix (commit `7445bcdc`, 2026-09-26) unified `.ps-strip.ps-stale`'s
   `border-bottom-color` from a distinct orange to the SAME amber hue the heartbeat
   pulse animates through (`@keyframes ps-heartbeat-pulse`, both ~`rgba(251,191,36,…)`
   at similar alpha, `PositionStrip.svelte:826-849`). The animation still technically
   wins the CSS cascade while running (confirmed via spec: animations sit in a
   higher-priority origin than static author rules, no `!important` anywhere in the
   file) — but because the resting stale border and the pulse's peak color are now
   nearly identical, and the pulse only lasts 300ms before the class is removed and
   the static stale border reasserts, the pulse reads as invisible in practice.
   Operator: "that border us for heart beat. revert it and let the heart beat
   animate it." — revert the color choice specifically (not the token-consolidation
   rationale) so the two states are visually distinct again.

2. **CardHeader.svelte's loading spinner sits BEFORE the title and has no reserved
   width**, so toggling `loading` shifts the title text (`CardHeader.svelte:101-113`,
   `{#if loading}<svg class="ch-spin">…</svg>{/if}` then `{#if title}<span
   class="ch-title">` — spinner is a plain flex child of `.ch-left`, adding/removing
   its width + the `gap` on every toggle). This is a SHARED component used by every
   card header in the app (not just "Payoff") — operator named "Payoff" as the
   example they noticed, but the fix belongs at the component level.

3. **OptionsPayoff.svelte's refresh spinner is positioned in the top-right corner
   of the chart** (`position: absolute`, `.payoff-loading-ring-corner`), completely
   separate from the "LTP" label/value it conceptually relates to (which lives in
   the top-left `.payoff-stats` overlay, `OptionsPayoff.svelte:892-925`). Operator
   wants it moved to render inline, immediately after the LTP value, not floating
   in an unrelated corner.

4. **Public `/performance` page's tab colors are inconsistent AND have a real
   invisible-text bug**, root-caused precisely:
   - Both tab strips (`PerformancePage.svelte:1501-1511` NAV/Funds,
     `:1520-1530` Positions/Holdings) reuse the shared `AlgoTabs.svelte` — not
     bespoke markup, so this is a pure CSS-drift bug, not an intentional
     inconsistency.
   - Positions/Holdings strip's cream-theme override (`PerformancePage.svelte:
     1912-1919`) targets `button[class*="border-primary"]`/`[class*="text-muted"]`
     — classes from an OLD bespoke Tailwind-button implementation that
     `AlgoTabs.svelte` never emits (confirmed via `git log`: commit `8b70771f`
     swapped the markup but left these selectors stale). Dead code — zero effect.
   - NAV/Funds strip's override (`:1954-1962`) sets `color` for the *selected*
     state but omits it for the *hover-not-selected* state.
   - Both gaps fall through to the canonical dark-algo-page rule
     `app.css:2568`: `.algo-tab:hover { color: var(--algo-slate); }`, and
     `--algo-slate: #ffffff` (`app.css:43`, explicitly documented "primary text
     on dark surfaces"). The public page's actual background here is `#fffdf8`
     (near-white cream) — so hovering/pressing a non-active tab renders white
     text on a near-white background, i.e. genuinely invisible. This is the
     exact mechanism behind "text changes to white and not visible."

5. **Chain submit button** — re-verified: the VISIBLE label is already correctly
   "Submit" (confirmed both in source and live on dev.ramboq.com in this
   session). The `title` tooltip attribute, however, still reads "Submit all N
   basket legs" (`SymbolPanel.svelte:2930`) — left as-is in an earlier pass since
   it's not the visible label. Given the operator has now repeated this complaint
   multiple times, simplifying the tooltip to match ("Submit") removes any
   remaining ambiguity, at negligible risk.

## Fix

### 1. `frontend/src/lib/PositionStrip.svelte`

Revert `.ps-strip.ps-stale`'s `border-bottom-color` (line ~848) from
`color-mix(in srgb, var(--algo-amber) 60%, transparent)` back to the original
`rgba(251, 146, 60, 0.6)` — restores hue contrast against the heartbeat's amber
pulse so the 300ms animation is perceptible again when both states are active
together. Update the code comment to explain WHY the color-token-consolidation
was reverted here specifically (heartbeat-visibility regression), so a future
pass doesn't "fix" it back to the canonical token without knowing this
constraint. Leave `BrokerHealthBadge`'s own stale-state color untouched — that
component has no heartbeat to camouflage against, so the original
consolidation rationale still applies there.

### 2. `frontend/src/lib/CardHeader.svelte`

Move the `{#if loading}<svg class="ch-spin">…</svg>{/if}` block to render
AFTER `{#if title}<span class="ch-title">{title}</span>{/if}` (still before
`timestamp`/`left` snippet, matching the operator's "after the label" ask).
To satisfy "should not shift text after it": give `.ch-spin` a fixed-width
wrapper (e.g. reserve `width: 10px` + the `gap` amount via a permanently
present container that toggles only `visibility`/`opacity` rather than
mounting/unmounting the SVG itself) OR keep the `{#if}` but reserve the slot
with a fixed-width invisible placeholder element on the `{:else}` branch —
prefer whichever keeps the diff smallest while genuinely eliminating layout
shift (verify by computed-style/bounding-box check in the same viewport,
loading on vs off, title's `x` position must not change). This is a shared
component — verify via a quick grep that no OTHER caller relies on the
spinner being BEFORE the title (unlikely, since `loading` is a generic prop),
and spot-check one or two other CardHeader usages after the change.

### 3. `frontend/src/lib/OptionsPayoff.svelte`

Move the refresh-spinner markup (`OptionsPayoff.svelte:892-903`,
`.payoff-loading-ring-corner`) from its current absolute top-right-corner
position to render INLINE immediately after the LTP value span inside the
`.ps-row` for LTP (`OptionsPayoff.svelte:922-926`, after
`<span class="ps-v ...">{fmtSpot(spot)}</span>`). Drop the
`position: absolute` / `top`/`right`/`z-index` corner-specific CSS for this
usage (or introduce a second, non-corner class) since it now flows inline
within `.payoff-stats`. Same layout-shift concern as #2: `.payoff-stats` uses
`grid-template-columns: max-content max-content` — toggling an inline icon
after the value will resize the row's content width; reserve a fixed-width
slot the same way as #2 so the LTP row's own width doesn't visibly jump when
`refreshing` toggles. Read the full surrounding `.payoff-stats`/`.ps-row`/
`.ps-k`/`.ps-v` CSS first to fit the icon into the existing grid/flex
structure cleanly rather than fighting it.

### 4. `frontend/src/lib/PerformancePage.svelte` + possibly `app.css`

- Positions/Holdings strip (`:1912-1919`): fix the dead selectors to target
  what `AlgoTabs.svelte` actually renders — `.tabs-row :global(.algo-tab[aria-selected="true"])`
  for the active state and `.tabs-row :global(.algo-tab:hover:not([aria-selected="true"]))`
  for hover, mirroring the NAV/Funds strip's own (mostly-working) pattern
  exactly, including setting `color` explicitly in BOTH states using the same
  `--card-active-row-text`/`--card-active-border`/`--card-active-row-bg`
  cream-theme variables the NAV/Funds strip already uses.
- NAV/Funds strip (`:1954-1962`): add the missing `color` declaration to the
  hover-not-selected rule (`:1959-1962`) — use a sensible cream-theme-
  appropriate inactive/hover text color (check what token the surrounding
  cream theme uses for muted/secondary text on `#fffdf8`, reuse it rather
  than inventing a new one).
- End state: both strips render IDENTICAL active/hover color treatment
  (same variables, same values) — this directly satisfies "active tab colors
  not consistent" / "hover tab colors not consistent", and eliminates the
  white-on-near-white invisible-text bug since `color` is now always
  explicitly set to a cream-appropriate value in every state, never falling
  through to `--algo-slate`/`#ffffff`.

### 5. `frontend/src/lib/SymbolPanel.svelte`

Simplify the `title` tooltip on `.oes-common-submit` (line ~2930) from
`` `Submit all ${basketLegs.length} basket leg${basketLegs.length > 1 ? 's' : ''}` ``
to a plain `'Submit'` for the basket-mode branch, matching the already-correct
visible label exactly.

## Explicitly out of scope

- Do not touch `BrokerHealthBadge`'s own stale-color token (the Sep 26
  consolidation is still correct there).
- Do not change `AlgoTabs.svelte` itself — the shared component's own base
  styles are correct for its primary (dark algo page) consumers; only the
  PUBLIC performance page's local override CSS is being fixed.
- Do not touch any OTHER CardHeader consumer's `loading` semantics — only the
  spinner's position/layout-stability within the shared component.

## Tests (mandatory, same commit)

- New/extended Playwright spec (source-scan + live-render mix, following this
  session's established pattern) covering:
  - `PositionStrip.svelte`'s `.ps-stale` border-bottom-color is the reverted
    `rgba(251, 146, 60, 0.6)`, not the color-mix token.
  - `CardHeader.svelte`: spinner markup renders after `.ch-title` in source
    order; a live/computed-style check that toggling `loading` does not move
    `.ch-title`'s bounding-box x-position.
  - `OptionsPayoff.svelte`: spinner markup no longer has the `-corner`
    absolute-position class; renders after the LTP value span in source
    order; a live check that toggling `refreshing` doesn't shift the LTP row's
    other content.
  - `PerformancePage.svelte`: both `.tabs-row` and `.funds-nav-tabs` override
    blocks set an explicit `color` for both active and hover-not-selected
    states, using the same `--card-*` variables in both strips (byte-level
    consistency check between the two rule blocks).
  - `SymbolPanel.svelte`: `.oes-common-submit` title is plain `'Submit'` for
    basket mode, not the old "Submit all N..." template string.

## Verification

1. `npx svelte-check --output machine` — 0 errors.
2. `npx vitest run` — full suite green.
3. New/updated Playwright spec green.
4. Live check via local dev server + screenshots: PositionStrip heartbeat
   visibly pulses distinctly from its stale resting state; CardHeader spinner
   sits after title with no jump when toggled; Payoff chart LTP row's spinner
   renders after LTP value with no shift; public `/performance` page's two
   tab strips show identical, clearly-visible active/hover colors with no
   white-on-cream invisible text at any point during a click/hover.
5. Stop the local dev server when done.

## Commit message (draft)

`fix(ui): restore heartbeat/stale contrast, reposition loading spinners without text shift, fix public performance-page tab color drift`

## Done when

- Heartbeat pulse is visually distinguishable from the stale resting state.
- Both CardHeader and OptionsPayoff spinners render after their label/value,
  with zero layout shift on toggle (verified via bounding-box check, not just
  visual impression).
- Public Performance page's two tab strips render byte-identical active/hover
  color treatment; no white-on-cream text at any interaction state.
- Chain submit button tooltip matches its visible "Submit" label.
- svelte-check + vitest + new Playwright spec all green; committed to `workshop`.
