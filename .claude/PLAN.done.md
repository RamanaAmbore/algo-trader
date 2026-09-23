# Plan: fix(derivatives): snapshot spot SSOT — always use batchQuote poll per row

## Task
In the byund-grid snapshot loop, the `_useAnchor` gate made the selected underlying's row
read `liveSpot` (which chains through `liveSnap()` SSE tiers — stale when SSE subscription
lags). All other rows correctly used `_undLiveLtp[g.underlying]` (batchQuote 5s poll).
Result: selecting GOLD showed a stale price (152716 from SSE), switching to GOLDM revealed
the fresh price (152888 from poll) because the GOLD row then fell back to `_undLiveLtp`.
Fix: drop `_useAnchor` entirely from snapshot rows — always use `_undLiveLtp[g.underlying]`
(SSOT: batchQuote poll) with `_snapLtp` as cold-start fallback; `_close` always from `_q.prev_close`.

## Agents
- frontend: skip (change already applied)
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes (0 errors confirmed)
- playwright: no

## Commit message
fix(derivatives): snapshot spot SSOT — use batchQuote poll per row, drop liveSnap anchor path

## Done when
- Selecting GOLD shows the same live price as selecting GOLDM for the GOLD row
- All snapshot rows refresh independently from their own batchQuote entry
- svelte-check 0 errors
