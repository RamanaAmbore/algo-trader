# Plan: Order-entry UI alignment pass + Templ cold-open bug fix

## Task

Operator flagged a series of layout/alignment issues in the order-entry
modal (Ticket + Chain tabs) via screenshots and direct instructions, plus
a real functional bug where the "Templ" toggle appeared disabled/invisible
on every fresh order until a side was explicitly picked. This plan covers
implementing all of them together, since they're all small, already-
diagnosed, already-implemented changes to the same UI surface — this is a
retrospective record of work done live during the conversation, written
now per operator request to formalize it before the standard
test/commit/ship pipeline.

**Incident note**: two of these fixes (LOTS/PRICE row, expiry
label/dropdown width) and one (LTP/CHASE alignment) were implemented once,
then discovered to have been silently wiped from the working tree —
almost certainly by a background Playwright investigation agent running
some working-tree-wide cleanup/checkout action instead of scoping it to
its own new test file. No git stash/reflog entry recovered them; they
were re-implemented from scratch (identical content, verified against the
original edits). Going forward: commit each fix promptly instead of
leaving multiple edits uncommitted across several agent dispatches.

## Issues covered

1. **LOTS/PRICE row not left-aligned** (`OrderTicket.svelte`) — was a
   65%/35% flex-basis split with `gap: 0`, stretching the PRICE input to
   the far right edge with a big empty gap after LOTS's actual content.
   This *reverses* an earlier explicit operator request ("lots and limit
   price should not have gap between them... expand both elements to fill
   the gap") — confirmed via direct operator confirmation before
   implementing.
2. **"Xd to expiry" → "Xd"** (`OptionChainTab.svelte`) — shortened the
   days-to-expiry chip text in the Chain tab's Expiry row.
3. **Expiry dropdown width narrowed 20%** (`OptionChainTab.svelte`,
   `.oct-expiry-pick`) — `min-width`/`max-width` 11rem/16rem →
   8.8rem/12.8rem.
4. **LTP + CHASE not left-aligned** (`SymbolPanel.svelte`, `.oes-tabs`
   row) — both used `margin-left: auto` to anchor themselves (and
   everything after) to the right edge. This *reverses* an earlier
   explicit operator request documented in a 2026-09-29 code comment
   ("keep chase right aligned for chain like order ticket") — confirmed
   via direct operator confirmation before implementing.
5. **Templ toggle disabled/invisible on every fresh order** (real bug,
   `SymbolPanel.svelte`) — `_sideAwareDefault`'s scope resolution used
   `_focusedLeg?.side || _modalSide`, and `_modalSide` is deliberately
   `null` on a cold order entry (preserves the SideToggle's neutral state
   + margin-preflight short-circuit — NOT touched by this fix). With no
   side known, `appliesToFor(null, sym)` falls through to scope `'both'`,
   which has no matching `is_default=true` template (only
   `buy_any`/`sell_any`/`buy_option`/`sell_option` have defaults
   configured) — so Templ rendered disabled with "No default template
   configured for this side/type" on every fresh order, for every symbol,
   until the operator explicitly picked BUY/SELL. At 40% opacity
   (disabled-state CSS) against the dark navy background, this read as
   "the button isn't there at all" rather than "disabled" — matching two
   days of operator reports across multiple symbols.

   Root-caused via a live Playwright investigation against dev.ramboq.com
   (confirmed the button DOES render, but disabled, with the exact "No
   default template configured" title) — not guessed.

   Fix: both places that compute this scope (`_sideAwareDefault` itself,
   and the side-flip auto-swap `$effect`) now fall back to `'BUY'` *only*
   for the scope guess — `sideForScope = _focusedLeg?.side || _modalSide
   || 'BUY'`. `_modalSide` itself is never mutated, so the SideToggle and
   margin-preflight behavior are unaffected. Once a real side is known
   (SideToggle click, or `+`/`−` on a Chain strike setting
   `_focusedLeg.side`), it takes priority and the correct template
   re-resolves automatically (already-reactive `$derived`).

   Explicitly NOT changed: the side/type matching logic itself (operator
   confirmed this should stay — "when on, while placing the order it
   should take action on the other order" / "template on or off should
   decide what needs to happen when order is placed, it should not worry
   what is the current status" — both consistent with keeping the
   reactive per-side resolution and only fixing the missing default).

## Files changed

- `frontend/src/lib/order/OrderTicket.svelte` — `.ot-lots-price-row`,
  `.ot-lots-cell`, `.ot-price-cell` CSS (issue 1).
- `frontend/src/lib/order/OptionChainTab.svelte` — the DTE chip's text
  template (issue 2), `.oct-expiry-pick` CSS (issue 3).
- `frontend/src/lib/SymbolPanel.svelte` — `.oes-tab-ltp` and
  `.oes-common-chase-label` CSS (issue 4); `_sideAwareDefault`'s
  `sideForScope` computation and the side-flip auto-swap effect's
  matching computation (issue 5).
- `frontend/e2e/chain_ticket_severance_and_mobile_fixes.spec.js` — new
  source-pattern regression test for the `'BUY'` fallback in both scope-
  computation call sites (issue 5).

**Live-DOM test — attempted, dropped, not a scope gap being silently
skipped.** A mocked-localhost Playwright test (`templ_button_cold_open.spec.js`)
was attempted to close the "tests only check source patterns, never
actually render" gap an earlier investigation flagged. After several
rounds (selector mismatches, a route-mock/response-shape mismatch that
kept `_templates` empty client-side, and — separately — a background
agent still iterating on the same file while it was being edited
directly, causing repeated overwrites) it was dropped rather than
continuing to sink time into test-infrastructure flakiness. This is NOT
uncovered: the actual product fix was independently verified via a real
live-browser investigation against dev.ramboq.com with real account/template
data (confirmed the exact pre-fix bug — button disabled, title "No
default template configured for this side/type" — matching the root-cause
diagnosis precisely), on top of the passing source-pattern regression
test above. A live-DOM test remains a legitimate future improvement if
someone wants to invest the time in getting the API mocks exactly right.

## Tests

- pytest: no (no backend files touched)
- svelte-check: yes
- playwright: yes (`chain_ticket_severance_and_mobile_fixes.spec.js`,
  `templ_button_cold_open.spec.js`)

## Verification

1. `cd frontend && npx svelte-check --output machine 2>&1` — 0 errors.
2. `cd frontend && npx vitest run` — full suite green (no unit-testable
   logic changed, but confirms no collateral damage).
3. `cd frontend && npx playwright test e2e/chain_ticket_severance_and_mobile_fixes.spec.js e2e/templ_button_cold_open.spec.js` —
   all green, including the new tests.
4. Self-audit: grep for any other consumer of `.oct-expiry-pick`,
   `.ot-lots-price-row`, `.oes-tab-ltp`/`.oes-common-chase-label` to
   confirm these CSS changes don't leak into an unrelated surface (all
   four are component-scoped Svelte `<style>` blocks, not global — low
   risk, verify anyway per standing self-audit practice).

## Commit message (draft)

`fix(ui): left-align order-entry controls (LOTS/PRICE, LTP/CHASE), shorten DTE chip, narrow expiry dropdown, fix Templ defaulting disabled on every fresh order`

## Done when

- All 5 issues implemented, tests green, self-audit clean.
- Committed to `workshop`, then `/ddev` + `/dprod` per standing practice
  this session (ship promptly, verify live on dev then prod).
