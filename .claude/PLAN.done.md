# Plan: Fix day P&L for closed futures and candidatesDayPnl double-counting

## Task
Two defects confirmed by audit:

**P1 — `_apply_flat_row_hygiene` wipes backstop for closed overnight futures** (`backend/api/routes/positions.py:529-530`).
`apply_day_change_backstop` (called at line 563) sets `day_change_val = pnl` for Case 3 (qty=0, pnl≠0 = closed futures). Then `_apply_flat_row_hygiene` (line 618) zeroes `day_change_val` for ALL rows where `quantity == 0`, including closed overnight positions. The backstop is undone. Grid DAY column shows ₹0 for every closed futures leg.
Fix: narrow `_flat_mask` to pure intraday round-trips only: `quantity == 0 AND overnight_quantity == 0`.

**P2 — `candidatesDayPnl` double-counts live tick** (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte:1947-1957`).
`_dayPnlForLeg(c)` already returns `(liveLtp − close) × qty` using `getSnapshot(c.symbol)?.ltp` when oq≠0 and SSE ltp is valid. `candidatesDayPnl` then adds `delta = (liveLtp − pollLtp) × qty` with the same `liveLtp`. Total = `(2×liveLtp − close − pollLtp) × qty` — wrong. Error grows with live tick speed.
Fix: gate `delta` on whether `_dayPnlForLeg` took the fallback path (oq=0 OR no valid SSE ltp OR close=0).

## Agents
- backend: Fix `_apply_flat_row_hygiene` in `backend/api/routes/positions.py:521` — change `_flat_mask` from `quantity == 0` to `(quantity == 0) & (overnight_quantity == 0)` (pure intraday round-trips only). Add `overnight_quantity` to columns-absent guard. Update the docstring to explain the narrower mask. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional. Add a pytest test in `backend/tests/` that verifies: (a) a row with qty=0, oq>0, pnl=1000 retains day_change_val=1000 after `_apply_flat_row_hygiene`, (b) a row with qty=0, oq=0, pnl=1000 still gets zeroed.
- frontend: Fix `candidatesDayPnl` double-counting in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` around line 1941-1960. Before calling `_dayPnlForLeg`, compute `dayPnlUsedLive = oq !== 0 && legLiveLtp != null && Number(legLiveLtp) > 0 && close > 0 && qty !== 0` (mirrors the guard inside `_dayPnlForLeg`). Then gate delta: `const delta = (!dayPnlUsedLive && !_isLegExpired(c) && pollLtp > 0 && liveLtp > 0 && qty !== 0) ? (liveLtp - pollLtp) * qty : 0;`. Exact variable reads: `oq = Number(c.overnight_quantity ?? c.opening_quantity ?? 0)`, `legLiveLtp = untrack(() => getSnapshot(c.symbol)?.ltp)`, `close = Number(c.prev_close ?? 0)`. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional. Add a Vitest test verifying: when oq≠0 and liveLtp is valid, delta=0; when oq=0 or liveLtp absent, delta = (liveLtp - pollLtp) × qty.
- broker: skip
- doc: skip
- backend-test: skip (covered in backend agent brief above)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(positions): closed-futures day P&L zero bug + candidatesDayPnl double-count

_apply_flat_row_hygiene now narrows its zero mask to pure intraday round-trips
(qty=0 AND oq=0) so overnight closes retain their backstop day_change_val. 
candidatesDayPnl gates the SSE-tick delta to only apply when _dayPnlForLeg 
fell back to baseDayPnlForPosition — preventing double-counting on overnight legs.

## Done when
- Closed futures positions (qty=0, oq>0) show correct day P&L (= pnl, not ₹0)
- Pure intraday round-trips (qty=0, oq=0) still show day_change_val=0
- candidatesDayPnl stops double-counting live ticks for overnight F&O legs
- pytest green, svelte-check 0 errors, vitest 0 failures
