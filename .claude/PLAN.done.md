# Plan: Chain/order-ticket visual polish + template toggle redesign

## Context

Follow-up batch on top of this session's earlier Ticket/Chain template severance
and mobile chain-height fix (commit `00f194d8`/`a0f839d1`, already live on dev+main).
Operator gave a long list of visual/functional fixes for the Chain tab, the Order
Ticket, and the template picker, then asked for a broader color/element/functionality
audit of template + chase UI. Grounded via 3 parallel Explore agents that read every
touched file in full — findings below are facts, not guesses.

Two genuinely ambiguous, risk-bearing points were resolved via AskUserQuestion:
- **"corresponding offset button should be selected"** — confirmed there is NO
  existing offset-button/selector widget anywhere in the codebase (only the numeric
  TP%/SL%/Wing-strike override fields). Resolved: toggle ON = attach the side-aware
  Default template (reuses the exact `_sideAwareDefault` resolver already wired to
  the dropdown's "Default" option today — this already factors in side + option
  type, confirmed again by operator: "it should select default template based on
  the order type"). Toggle OFF = None.
- **"submit place both the orders"** — confirmed current architecture: TP/SL are
  broker GTT triggers, Wing places via `broker.place_order` but ONLY after the
  parent fill postback/chase confirms fill (`_fire_template_attach_on_fill` →
  `apply_plan_live`). Placing the wing at submit time (before the parent fills)
  was a real bug fixed in "Sprint A" (`orders_place.py:842-856` docstring: placing
  early risked the wing pricing off a stale/wrong reference if the parent filled
  away from its limit). Operator confirmed: **keep fire-on-fill, no change** to
  order-placement timing/architecture.

## Changes

### A. `frontend/src/lib/order/OptionChainTab.svelte` — CSS/layout fixes

1. **CE/PE header alignment** (`.chain-th-ce` line 1361, `.chain-th-pe` line 1362).
   Today text-align is `left`/`right` respectively, but the row content (quote +
   +/- buttons) is pushed the OPPOSITE way via `.chain-cell-row-ce { justify-content:
   flex-end }` / `.chain-cell-row-pe { justify-content: flex-start }` (lines
   1392-1393, both toward the Strike column — a deliberate, commented layout choice
   we are NOT changing). Fix: flip the header alignment to match the buttons —
   `.chain-th-ce { text-align: right }`, `.chain-th-pe { text-align: left }`.

2. **Font size reset to normal** (lines 1349, 1664, 1677). Base `.chain-grid`
   font-size is `var(--fs-md)` (0.65rem, `app.css:276`). Two override blocks
   (`@media (min-width:640px)` line 1664 and `@media (max-width:760px)` line 1677)
   both currently force `0.78rem`. Remove the `font-size: 0.78rem` line from both
   blocks so it falls back to the base `var(--fs-md)`. The accompanying th/quote/
   no-depth/spread-warn font-sizes in the same two blocks (0.7/0.72/0.68/0.62rem)
   were tuned as a matched set alongside 0.78rem — rescale them down proportionally
   using the existing `--fs-sm`/`--fs-xs` tokens instead of hardcoded rem, so the
   whole grid reads as one consistent "normal" scale (matching the rest of the
   algo pages) rather than a mismatched leftover set.

3. **Header lower border** (`.chain-th-ce`/`.chain-th-pe`/`.chain-th-strike`, all
   three currently `border-bottom: 1px solid rgba(255,255,255,0.05)` — 5% alpha,
   too faint to read as a separator). Bump to `rgba(255,255,255,0.18)` so the
   header row visibly separates from the strike data below it, consistent with
   this app's other "visible-but-subtle" divider convention (~0.18-0.22 alpha,
   see `activity_column_divider.spec.js` SSOT).

4. **+/- button visibility** (`.chain-btn-buy`/`.chain-btn-sell`, lines 1506-1509).
   Rest state today: `background: var(--c-long-10)` (10% alpha) / `border-color:
   var(--c-long-22)` (22% alpha) — still reads as flat/text-like per operator.
   Bump rest-state background one tier to `var(--c-long-14)`/`var(--c-short-14)`
   and make the rest-state border solid `var(--c-long)`/`var(--c-short)` (full
   color, not just on hover) so the button reads as clickable immediately.
   Hover/:active tiers shift up correspondingly (still stronger than rest).

5. **Mobile chain height** ("chain is using all the available height"). Today
   `.chain-grid-wrap` is `flex: 1 1 0` (greedy — fills all available flex space up
   to its `max-height: 16rem` mobile cap, line ~1700). Since `/orders` and the
   modal body already provide page-level scroll, this greedy fill isn't needed —
   change to `flex: 0 1 auto` on mobile (max-width:760px block) so the grid sizes
   to its own content up to the 16rem ceiling instead of always claiming the full
   ceiling regardless of row count. Verify live (Playwright + screenshot, same
   method used earlier this session) that the Templ toggle / expiry row / other
   siblings get their natural space back.

6. **Expiry row gets the template toggle** — see section B/C below; the new
   compact `<TemplateBar>` mount point is the existing "EXPIRY [dropdown]
   [expires today]" row, appended at the end.

7. **Leg chip shows template** (`.chain-leg-badge`, lines 1547-1574, markup
   960-963/984-987). Today displays only `{lots}L`; the attached-template name
   already lives in the `title` tooltip. Change displayed text to `{lots}L ·
   {shortLabel}` when `tmplAttached` is true (shortLabel = template slug/name
   truncated to ~6 chars), keeping the existing full-detail tooltip unchanged.

### B. `frontend/src/lib/TemplateBar.svelte` — toggle redesign

1. Replace the primary `<Select>` (Default/None/named, lines 166-186) with a
   compact ON/OFF toggle pill (same visual language as `SideToggle.svelte`'s
   compact variant / `ChaseAggPicker`'s panel skin, for palette consistency).
   - **ON** (`!shellUsingNone`): shows the resolved template's name (Default's
     resolved name, or a specific named template if one was picked via the expand
     panel — see below). Toggling ON (from OFF) always calls `onSelectDefault()` —
     simplest, least-surprising behavior per the clarified answer ("select default
     template based on the order type").
   - **OFF**: shows "None". Calls `onSelectNone()`.
2. Add a compact named-template picker **inside the existing expand panel**
   (`.oes-tpl-expanded`, lines 443+) — reuses the exact option-list shape the old
   `<Select>` built from `nonNoneTemplates` (lines 60-69), just relocated from the
   primary row into the expand-panel row, so picking a SPECIFIC (non-default)
   template stays possible without it being the main gesture. Existing TP%/SL%/
   Wing-strike/Wing-prem% override inputs in the expand panel are unchanged.
3. Toggle color: same amber-on/slate-off convention as the rest of this session's
   already-detuned TemplateBar palette (no new colors introduced).

### C. `frontend/src/lib/SymbolPanel.svelte` — mount point + prop threading

1. Stop rendering `<TemplateBar>` as its own full-width row below `.oes-body`
   (removes the `.oes-basket-tpl-row-shell` live branch's `<TemplateBar>` mount,
   lines ~2404-2427). Instead pass the same props (`selectedTemplate`,
   `sideAwareDefault`, `nonNoneTemplates`, `showsWing`, `shellUsingNone`, the four
   override bindables, `onSelectDefault`/`onSelectNone`/`onSelectTemplate`) down
   into `<OptionChainTab>` as new pass-through props — extending the EXACT existing
   pattern already used for `templateId`/`templateName`/`templateIsNone` (line
   2322-2324). `OptionChainTab` mounts `<TemplateBar>` itself at the end of its
   expiry row (section A.6).
2. The on-fill preview chip + cap-warning block (currently inside the same
   `.oes-basket-tpl-row-shell` div, ~lines 2428-2798) is basket-wide feedback, not
   part of the picker control — it stays at the SymbolPanel/shell level as its own
   slim strip (rendered where the old row used to sit, just above
   `.oes-common-actions`), unchanged in behavior.
3. Demo-mode note (currently its own `{#if _isDemo}` branch of the same row)
   becomes a short inline note rendered next to the toggle inside OptionChainTab
   when `_isDemo` is true — same message, relocated mount point only.
4. No change to the Ticket/Chain severance already shipped — OrderTicket still
   receives zero template props (confirmed untouched by this plan).

### D. `frontend/src/lib/order/OrderTicket.svelte` / `SideToggle.svelte` — verify only, no code change

Explore agent confirmed `SideToggle.svelte:10-14` already computes contextual
`ADD/BUY` / `CLOSE/SELL` / `CLOSE/BUY` / `ADD/SELL` labels from `currentQty`,
independent of the shared submit-button label helper — this already satisfies
"order ticket should show close/add/buy/sell on the first two buttons." No build
step here; verification step only (open the ticket from an existing position row,
confirm the SideToggle buttons show the contextual label, not plain BUY/SELL).

### E. Color/functionality audit — Chase + Template (explicit ask)

- `.oes-common-chase-label` has two DIFFERENT definitions: scoped in
  `SymbolPanel.svelte:4395-4405` (wins, `--algo-slate-muted`/`var(--c-action)`) vs.
  a global fallback in `app.css:2651-2661` (`color-mix(...) 70%`/`#fbbf24` — a
  drifted duplicate that's currently dead but would silently reactivate if the
  scoped rule were ever removed). Fix: update the `app.css` fallback to match the
  scoped values so there's no latent drift.
- `ChaseAggPicker.svelte`'s two skins (ticket = graduated sky/amber/red per
  aggressiveness tier, panel = uniform amber) are confirmed intentional per its
  own header comment — documenting as audited/correct, no change.
- TemplateBar's amber palette was already detuned earlier this session and stays
  consistent with the toggle redesign above — no further palette change beyond
  what's already specified in B.3.

## Tests

Extend `frontend/e2e/chain_ticket_severance_and_mobile_fixes.spec.js` (same
source-scan style already established in this file) with new assertions:
- `.chain-th-ce`/`.chain-th-pe` text-align matches button-side (right/left).
- No `font-size: 0.78rem` remains on `.chain-grid`.
- Header border-bottom alpha bumped to 0.18.
- `.chain-btn-buy`/`.chain-btn-sell` rest-state uses the `-14` background tier and
  a solid (non-transparent) border color, not just on hover.
- `.chain-leg-badge` markup includes the short-label suffix when `tmplAttached`.
- `TemplateBar.svelte` renders a toggle, not a `<Select>`, as its primary control;
  a named-template picker exists inside the expand panel.
- `<TemplateBar` mounts inside `OptionChainTab.svelte`, not directly in
  `SymbolPanel.svelte`'s own markup (pass-through props only).
- `app.css`'s `.oes-common-chase-label` color values match `SymbolPanel.svelte`'s
  scoped copy (audit fix).

## Verification

1. `npx svelte-check --output machine` — 0 errors (same 5 baseline warnings).
2. `npx vitest run` — full suite green.
3. New/updated Playwright spec green.
4. Live check against local dev server (`npm run dev -- --port 5173`), same
   Playwright-screenshot method used earlier this session, both mobile (390px)
   and desktop (1280px) viewports: header/button alignment, normal text size,
   visible header border, visible +/- buttons at rest, toggle at the end of the
   expiry row (with expand panel for named-template pick), chip shows template
   short-label, mobile chain grid no longer dominates all available height.
5. Stop the local dev server when done.

## Commit message (draft)

`fix(ui): chain header/button visibility + template toggle redesign, audit chase/template colors`

## Done when

- All 7 OptionChainTab CSS/layout items verified live on both viewports.
- TemplateBar is a toggle (Default-on-order-type / None) with named-template pick
  moved into the expand panel, mounted inline at the end of Chain's expiry row.
- Order-placement timing for TP/SL/Wing is UNCHANGED (fire-on-fill preserved) —
  explicitly confirmed by a passing test that the submit path still only persists
  `template_id` and does not call `apply_plan_live`/`broker.place_order` for the
  wing at submit time.
- `.oes-common-chase-label` app.css/SymbolPanel.svelte color drift fixed.
- svelte-check + vitest + new Playwright spec all green; committed to `workshop`.
