# Plan: Track 2, Sprint A — rename the Payoff chart's ambiguous "HOLD" toggle

## Task

The Payoff chart's legend has a compact toggle literally labeled `HOLD` at
`frontend/src/lib/OptionsPayoff.svelte:1573-1586`. It controls `includeHoldings` — whether
equity holdings (a directly-held stock AND any proxy-hedge ETF leg) are included in the payoff
curve. Its own tooltip already reads "Holdings ON — equity holdings included..." / "Holdings
OFF — ...", so the tooltip is correct; only the 4-letter button label is ambiguous, especially
now that this app has multiple unrelated "hold/held/holding" concepts (an order-level
review-hold mechanism, a brand-new agent-level `require_release` hold mechanism, and this
equity-holdings toggle). Rename the button text from `HOLD` to `HOLDINGS` (matching the tooltip
exactly) — or `EQ` if the legend's rendered width genuinely doesn't fit the full word at mobile
viewport widths (check the real rendered layout before choosing, don't assume either way).

Separately: CLAUDE.md claims a literal "Held" covered-call label exists elsewhere in the app
and must not be touched without a fresh decision. A prior Explore pass this session could not
find that literal string anywhere in the current codebase. Add a one-line correction to
CLAUDE.md flagging this claim as unconfirmed in the current code — do not rename or touch
anything covered-call-related; just correct the stale claim.

## Agents

- frontend: Change the literal button text at `OptionsPayoff.svelte:1586` from `HOLD` to
  `HOLDINGS`. Do not change `includeHoldings`, `onToggleHoldings`, the tooltip text, or the CSS
  class `legend-toggle-on` — purely a label change. Check the rendered legend at a mobile
  viewport width (per this repo's standing mobile-viewport rule) before finalizing; if
  `HOLDINGS` genuinely doesn't fit, use `EQ` instead and say so in the commit message. Do not
  touch: the `admin/settings` literal "HELD"/"AUTO" button text (a different feature — the
  global expiry-close/template-exit hold switches), the MarketPulse "H"/"Holding" badge, or
  anything covered-call-related — each is its own concept, not in scope here.
- doc: add a one-line CLAUDE.md correction flagging the stale "Held" covered-call claim as
  unconfirmed in the current codebase — cosmetic, do not assert a cause (renamed vs.
  always-inaccurate) that wasn't verified. Do not regenerate the DESIGN_GUIDE PDF for this —
  it's a cosmetic P2, not an architectural change.
- backend: skip
- backend-test: skip
- playwright: update whichever existing spec (if any) asserts on the literal `HOLD` button text
  in the payoff legend, to the new label. Search `frontend/e2e/` for any spec referencing
  `OptionsPayoff`'s legend toggle text specifically (not every Bracket/Template spec — this
  toggle is unrelated to Bracket).

## Tests

- pytest: no
- svelte-check: yes (baseline sanity)
- playwright: yes, if an existing spec references the literal old label text

## Commit message

`fix(payoff): rename ambiguous "HOLD" legend toggle to "HOLDINGS" (matches its own tooltip,
disambiguates from the order-hold and agent-hold features elsewhere in the app)`

## Done when

- The Payoff legend button reads `HOLDINGS` (or `EQ`, with the reason stated), functionally
  identical to before — `includeHoldings` behavior unchanged.
- No other "hold/held/holding" label in the app was touched.
- CLAUDE.md's stale covered-call "Held" claim is corrected without asserting an unverified cause.
- svelte-check clean, any affected Playwright spec passes.
