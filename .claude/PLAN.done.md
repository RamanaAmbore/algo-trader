# Plan: Add poll-diff LTP flash to Legs grid

## Context
The Legs grid in admin/derivatives shows each position leg with LTP, Day P&L, P&L, and Exp P&L.
Day P&L, P&L, and Exp P&L cells already flash (tf-up/tf-down) via the `createTickFlash` poll-diff engine.
The LTP cell does NOT flash — `flash.update()` for the `ltp` key was never added to the legs `$effect`,
and `flash.classOf()` was never applied to the LTP span in CandidateLegRow.svelte.

The Exp-Close (by-underlying) LTP cell is ALREADY flashing — `flash.update(\`${root}:ltp\`)` at line 1063
and `flash.classOf(\`${g.underlying}:ltp\`)` at line 4701 are both present. No change needed there.

## Task
Wire the legs LTP cell to the existing `createTickFlash` poll-diff engine so it flashes `tf-up`/`tf-down`
on each 30s poll when LTP changes. Two lines total.

## Agents
- frontend: In `+page.svelte` (around line 1112), inside the legs `$effect` block that updates flash keys `leg:${k}:day/pnl/exp`, add one more line:
  ```js
  flash.update(`leg:${k}:ltp`, c.last_price != null ? Number(c.last_price) : null);
  ```
  Verify the field name on `c` that holds the current LTP (likely `last_price` or `ltp`) by reading CandidateLegRow.svelte line 336 context.

  In `CandidateLegRow.svelte` (around line 336), the LTP `<span>` currently has `ltp-vs-avg-*` and `ltp-vs-prev-*` tint classes. Add `{flash.classOf(\`${_legFlashKey}:ltp\`)}` to its class string.

  No new CSS needed — `tf-up`/`tf-down` keyframes are already in app.css and used by all other flash cells on this page.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: Write a Playwright spec `frontend/e2e/derivatives_legs_ltp_flash.spec.js` that:
  - Navigates to dev.ramboq.com/admin/derivatives
  - Waits for at least one leg row to render
  - Verifies the LTP cell has the `tf-cell` class (or `tf-up`/`tf-down` when applicable)
  - Verifies the existing pnl/day flash cells still have their `tf-cell` class (regression guard)
  - Uses CSS stale-code check: grep CandidateLegRow.svelte to confirm `flash.classOf` appears on the LTP span

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
feat(derivatives): wire LTP cell to poll-diff flash in Legs grid — matches P&L cell behaviour

## Done when
- CandidateLegRow.svelte LTP span includes `flash.classOf(\`${_legFlashKey}:ltp\`)` in its class
- +page.svelte legs `$effect` calls `flash.update(\`leg:${k}:ltp\`, ...)` alongside the day/pnl/exp updates
- svelte-check 0 errors
- Playwright spec passes
