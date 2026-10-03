# Plan: InfoHint — hover support for anchor-trigger mode + convert dense rows to use it

## Context

Follow-up to the just-shipped InfoHint panel-consistency migration (commit `80b6e5fa`).
Operator clarified two more requirements:

1. **Hover AND press must both open every tooltip, including the `hideButton`+`anchor`
   external-trigger sites.** Investigation found this is currently BROKEN for that mode:
   `InfoHint.svelte`'s default chip has its own `onmouseenter`/`onmouseleave` that set the
   internal `hovered` state (`visible = popup ? (open || hovered) : open`), but in
   `hideButton` mode that internal button is removed (`{#if !hideButton}`) and nothing else
   sets `hovered` — so today's 6 Greek/EV header chips on `admin/derivatives/+page.svelte`
   (lines ~5502-5583) only respond to click, never hover. This is a real existing gap, not
   new scope creep.

2. **"Where there is no space, the field itself should work as a tooltip"** — operator's
   example, "Greeks above payoff chart," is `ChartWorkspace.svelte`'s `.cw-greeks-strip`
   (confirmed by reading the file: a tight flex row, `gap: 0.75rem`, `--fs-sm` font, each
   `.cw-greek-item` = label + value + a separate small InfoHint chip). The request is to
   remove the separate chip in cramped rows and make the existing label/value span itself
   the hover/click trigger — exactly the mechanism the derivatives page's 6 header chips
   already use (`hideButton` + `anchor` + `bind:open`), just currently missing hover (see
   #1) and not yet applied to the other dense rows.

Investigated CSS density of every candidate site to decide "has space" vs "no space":
- **No space (convert to hideButton+anchor)**: `ChartWorkspace.svelte` `.cw-greeks-strip`
  (6 sites — the literal example given), `OptionsPayoff.svelte` `.payoff-stats` absolute
  overlay on the chart itself (9 sites — `--fs-sm`/`0.6rem` font, `.ps-row` already has
  `cursor: help`, confirming it was already meant to read as a hover target),
  `admin/derivatives/+page.svelte` Strategy Summary `.kv-pair`/`.kv-k` rows (14 sites —
  `--fs-sm`, tight inline-flex), `admin/metrics/+page.svelte` and `admin/perf/+page.svelte`
  `.metric-label` table-header cells (20 sites combined — `white-space: nowrap` abbreviated
  column headers like "BE LOC", "cc max").
- **Has space, leave as the chip+panel+title pattern already shipped**: `PnlPanel.svelte`,
  `OrderTicket.svelte`, `SimulatorPanel.svelte`, `strategies/[id]/+page.svelte` (each metric
  is its own grid card with room), `admin/research/+page.svelte`, `admin/settings/+page.svelte`,
  `automation/+page.svelte` + `automation/templates/+page.svelte` (form fields have a label
  row with room beside it). `admin/derivatives/+page.svelte`'s existing 6 header chips stay
  `hideButton`+`anchor` as-is (just gain hover for free from the component fix).

## Approach

**Step A — Component fix (`frontend/src/lib/InfoHint.svelte`), fixes #1 for all sites at once.**
Add one more `$effect` (alongside the existing click-outside effect) that, when
`hideButton && anchor` are both set, attaches `mouseenter`/`mouseleave` listeners directly
to the `anchor` element to set/clear the existing internal `hovered` state — the same state
the default chip's own button already drives. No new props needed; `visible = popup ?
(open || hovered) : open` (line 103) already handles the rest once `hovered` is wired.
This single fix retroactively gives the existing 6 derivatives header chips hover support,
and is what every newly-converted site in Step B relies on.

**Step B — Convert the 49 "no space" sites from chip-mode to `hideButton`+`anchor`+`bind:open`.**
Pattern to replicate (already proven in `admin/derivatives/+page.svelte:588-596,5502-5537`):
```js
let _xHintOpen = $state({ key1: false, key2: false, ... });   // or a bare boolean for a single site
let _xHintAnchor = $state({});                                  // or a bare ref for a single site
```
```svelte
<span class="existing-label-or-value-span" bind:this={_xHintAnchor.key}
      role="button" tabindex="0" style="cursor:help"
      aria-expanded={_xHintOpen.key}
      onclick={() => { _xHintOpen.key = !_xHintOpen.key; }}>
  {existingLabelText}
</span>
<InfoHint popup hideButton id="..." anchor={_xHintAnchor.key} bind:open={_xHintOpen.key}
          panel title="..." text="..." />
```
Remove the now-redundant separate chip (the old `<InfoHint popup panel title=... text=.../>`
sitting inline next to the label — its `panel`/`title`/`text` values carry over unchanged
onto the new `hideButton` instance, nothing in the explanatory copy changes). The label/value
span that already exists at each site becomes the `anchor` + click target; add `cursor:help`
if the site doesn't already have it (OptionsPayoff's `.ps-row` already does).

Apply this to all 5 files from the "no space" list above. Each file gets its own
`_xHintOpen`/`_xHintAnchor` state object scoped to that component (not shared across files).

## Agents

- frontend: Implement Step A in `InfoHint.svelte` first (single file, do this before Step B
  so the dense-row conversions can rely on it working). Then implement Step B across the 5
  files: `ChartWorkspace.svelte`, `OptionsPayoff.svelte`,
  `admin/derivatives/+page.svelte` (Strategy Summary kv-rows only — do NOT touch the
  existing 6 header-chip sites, they already use this pattern and just need Step A),
  `admin/metrics/+page.svelte`, `admin/perf/+page.svelte`. Do not change any `text=`
  explanatory copy, `title=` values, or the structured `content=` object on the metrics/perf
  sites — only the trigger mechanism (chip → anchor) changes. Verify via
  `cd frontend && npx svelte-check --output machine 2>&1 | tail -30` before finishing.
- playwright: Extend the same 3 specs touched in the prior commit
  (`derivatives_greek_header_chip_infohint.spec.js`, `options_payoff_stat_infohints.spec.js`,
  `metrics_tooltip.spec.js`) plus add hover-trigger coverage for `ChartWorkspace.svelte`'s
  Greeks strip (new spec or extend an existing chart spec — check
  `frontend/e2e/` for an existing ChartWorkspace Greek-tooltip spec first). Assert BOTH
  `hover` (Playwright `.hover()`) and `click` open the popover for at least one converted
  site per file, and that the existing 6 derivatives header chips now also open on hover
  (regression check for the Step A component fix).
- backend/broker/doc/backend-test: skip (frontend-only, no API/data change).

## Tests

- pytest: no
- svelte-check: yes
- playwright: yes — the 3 named specs + new/extended ChartWorkspace Greek hover coverage
- vitest: re-run `optionsPayoffStatInfoHint.test.js`, `InfoHint.sourceAudit.test.js`,
  `strategyDetailMetricsInfoHint.test.js` (same P&L-escaping/prop-shape assertions as last
  time — title/text copy is unchanged so these should stay green, but confirm)

## Commit message

fix(ui): InfoHint anchor-trigger mode supports hover; convert dense rows to field-as-trigger

## Done when

The 6 existing derivatives header chips open on hover (not just click) — the Step A fix.
All 49 dense-row sites (ChartWorkspace Greeks strip, OptionsPayoff stat overlay, derivatives
Strategy Summary kv-rows, admin/metrics + admin/perf table headers) show the same
panel-style popup on hovering or pressing the field/label itself, with no separate chip
button taking extra space. Every other, already-correct chip-mode site is untouched.
svelte-check 0 errors; named vitest + Playwright specs pass.
