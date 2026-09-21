# Plan: Live cur_val (Value) + positions skipLtp removal

## Task
Two fixes to the MarketPulse holdings/positions grids:
1. Remove `skipLtp: true` from `pulsePositionsStore.load()` so position rows receive live LTP from the 10s book poller (same as holdings). Currently positions skip LTP in the pulse poller, meaning symbolStore for F&O symbols relies solely on SSE ticks — if a symbol hasn't ticked recently, pulse rows show stale LTP.
2. Add a live `valueGetter` to the `cur_val` ("Value") column in the holdings grid. Currently `cur_val = ltp × heldAbs` is frozen at `buildUnified` time (10s SWR cadence). The column tooltip already promises "Live LTP × held qty" — make it true by reading `_liveLtpSnap` at render time.

## Agents
- backend: skip
- frontend: In `frontend/src/lib/data/marketDataStores.svelte.js` line 726, change `pulsePositionsStore.load({ skipLtp: true })` to `pulsePositionsStore.load()`.
  In `frontend/src/lib/data/pulseColumns.js`:
    - Add `getLiveLtpSnap` to `mkRightColDefs` params (line 522 signature)
    - Add `valueGetter` to the `cur_val` column at line 620: reads `getLiveLtpSnap()[quoteSym || sym]`, multiplies by `Math.abs(p.data.qty_hold)`, falls back to `p.data.cur_val` when snap is 0/null or row is total/pinned.
  In `frontend/src/lib/MarketPulse.svelte`:
    - Pass `getLiveLtpSnap: () => _liveLtpSnap` into the `mkRightColDefs(...)` call at line 3596.
    - In `_scheduleFlashRefresh` (line 2283): add `'cur_val'` to `cols` array so holdings Value column repaints on every tick burst.
    - In the `$effect` on `_liveLtpSnap` (line 2349): add `'cur_val'` to `_cascadeCols` array so it also repaints on idle cascade refresh.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(MarketPulse): live cur_val valueGetter + remove skipLtp from pulsePositionsStore

## Done when
- `npx svelte-check` passes with 0 errors
- `cur_val` column formula: `valueGetter` returns `_liveLtpSnap[sym] × qty_hold` when snap has a value, `p.data.cur_val` otherwise
- `pulsePositionsStore.load()` called without `{ skipLtp: true }` in the book poller
- Total rows: unaffected (return `p.data.cur_val` via field fallback, updated at 10s cadence)
