# Plan: fix(pulse): restore + button in Pinned/Watchlist card header

## Context

The Pinned/Watchlist card header had a "+" button that opened the AddToPulseModal
(add symbol, create new watchlist, rename/delete). A prior commit replaced it with a
pencil-edits-list SVG icon ("manage list" affordance) — but the operator can no longer
find the add entry point because the "+" was the expected trigger.

Fix: revert the button content from the SVG pencil to the "+" character. Same
`openSearch` call, same modal, same `/` shortcut — only the button glyph changes.

## Agents

- frontend: In `frontend/src/lib/MarketPulse.svelte`, lines 4128–4141: replace the
  SVG children of `.mp-add-btn` with the plain text `+`. The button element itself,
  the `onclick={openSearch}`, the `title`, and the `aria-label` stay. Remove the SVG
  block (lines 4132–4141) and put `+` as the text content. Also update the comment
  block at lines 4120–4126 to reflect the revert.
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
fix(pulse): restore + button in Pinned/Watchlist header (replace pencil SVG)

## Done when
- The Pinned/Watchlist card header shows "+" instead of the pencil SVG
- Clicking "+" opens AddToPulseModal (unchanged)
- svelte-check 0 errors
