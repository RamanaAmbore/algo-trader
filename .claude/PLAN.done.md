# Plan: Extract a shared scroll-not-wrap CSS utility

## Task

Extract the byte-identical scrollbar-hiding/touch-scroll mechanics
(`overflow-x: auto; -webkit-overflow-scrolling: touch; scrollbar-width:
none;` + sibling `::-webkit-scrollbar { display: none; }`) duplicated
across 12 files into one shared `.scroll-x` utility class in
`frontend/src/app.css`. Layout-specific properties (`gap`, `align-items`,
`overflow-y`, child `flex-shrink`/`white-space` policy) legitimately vary
per consumer and stay local — this is a pure dedup of the identical part,
not a redesign, and must not change any visual/behavioral outcome on any
of the 12 surfaces. Also fixes two incidental defects found while cataloguing
the 12: `PnlAnalysis.svelte`'s `.legend-chip` can wrap/shrink internally
(missing `flex-shrink: 0; white-space: nowrap`, unlike every sibling
child in the other 11 containers), and `MarketPulse.svelte`'s
`.mp-head-tabs` embeds `<AlgoTabs>` without neutralizing the component's
own inner `.algo-tabs-strip` scroller, creating a nested double-scroll
container that `LogPanel.svelte` already avoids correctly when embedding
the same component.

## Agents

- frontend: Add `.scroll-x` to `frontend/src/app.css` and migrate all 12
  consumers (`CardHeader.svelte`, `AlgoTabs.svelte`, `PositionStrip.svelte`,
  `MarketPulse.svelte`, `LogPanel.svelte`, `PnlPanel.svelte`,
  `PnlAnalysis.svelte`, `AlgoTimestamp.svelte`, `OrderBook.svelte`,
  `(algo)/+layout.svelte` footer, `derivatives/+page.svelte`,
  `dashboard/+page.svelte`) by adding `scroll-x` to each element's
  `class=` and removing the duplicated 4 properties + the
  `::-webkit-scrollbar` sibling rule from each component's own `<style>`.
  Fix the two incidental defects (PnlAnalysis `.legend-chip` wrap,
  MarketPulse `.mp-head-tabs` nested scroller) in the same pass.
- playwright: representative regression coverage across footer, OrderBook,
  AlgoTimestamp, PnlAnalysis legend-chip, MarketPulse mp-head-tabs.

## Tests

- pytest: no (frontend-only)
- svelte-check: yes
- playwright: yes

## Commit message

refactor(css): extract shared .scroll-x utility for the scroll-not-wrap header pattern, dedupe 12 hand-rolled copies; fix PnlAnalysis legend-chip wrap and MarketPulse's nested-scroller gap

## Done when

- `.scroll-x` exists once in `app.css`; all 12 consumers reference it
  instead of duplicating the scrollbar-hiding boilerplate.
- No visual/behavioral change on any of the 12 surfaces (verified via
  existing + new Playwright coverage).
- `PnlAnalysis.svelte`'s `.legend-chip` no longer wraps internally at a
  narrow viewport.
- `MarketPulse.svelte`'s `.mp-head-tabs` has exactly one scroll container.
- svelte-check 0 errors, playwright green.

## Execution notes (filled in during /impl + /ddev + /dprod)

- Pre-existing global rule `:global(.algo-viewport *), :global(.algo-content *)
  { scrollbar-width: thin; }` in `(algo)/+layout.svelte` ties `.scroll-x`'s
  specificity (both 0-1-0) once the per-component Svelte-scoped rules
  (effectively 0-2-0) were removed. Fixed by self-compounding the selector
  to `.scroll-x.scroll-x` (0-2-0), restoring order-independent precedence.
  Found and fixed by a parallel sibling agent mid-session; independently
  confirmed correct via computed-style assertions in
  `scroll_x_utility_refactor.spec.js`.
- `scroll_x_utility_refactor.spec.js` test (d) originally navigated to
  `/dashboard` for `.mp-head-tabs`, but that selector only reliably renders
  on `/pulse` (dashboard's MarketPulse embed doesn't always include the
  movers buckets). Fixed by the same parallel pass; route corrected to
  `/pulse`.
- Two pre-existing, unrelated test failures observed and investigated:
  `derivatives_greek_header_chip_infohint.spec.js`'s two "Strategy Summary"
  tests failed because the live derivatives page's current position state
  (multi-account GOLD legs) hadn't resolved into a `strategy` object at
  test time ("No legs selected..." / `.opt-block` never renders, gated on
  `{#if strategy}`) — confirmed unrelated via diff inspection (this file's
  only change is an unrelated `opt-section-chips` class rename) and via
  every other test in the same file passing. Not fixed as part of this
  change; the test's own skip-guard only fires once `.opt-block` exists,
  not when it's absent entirely — a latent gap in that spec, left as-is.
  `order_book_filled_predicate_and_mobile_overflow.spec.js` (already
  committed on workshop, untouched by this diff) flaked only under full
  parallel-worker load in this sandbox; confirmed 100% green with
  `--workers=1` and in isolation — sandbox resource contention, not a
  regression.
- All specs required by this plan plus the above spot-checks: 0
  svelte-check errors (5 pre-existing unrelated warnings), 90/90 pass on
  the directly-scoped specs (footer_credits, algo_timestamp_market_badge,
  scroll_x_utility_refactor), 9/9 pass on order_book overflow (serial).
