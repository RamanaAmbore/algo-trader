# Plan: fix holdings pulse day P&L (last_price fallback) + funds cold-cache zeros

## Task
Two regressions from commit d86099fe:

**1. Holdings day P&L in Pulse shows wrong value (e.g. 2.33L)**
`mergeHoldingRows` in `pulseUnified.js` computes `liveHold` without `r.last_price` fallback:
```js
const liveHold = (_snapLtp != null && Number(_snapLtp) > 0) ? Number(_snapLtp)
               : (Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null);  // ← null when no snap/liveQ
```
But `holdingsDayPnlStore._store` always has `h.last_price` as final fallback:
```js
const liveLtp = (snapLtp != null && snapLtp > 0) ? Number(snapLtp) : Number(h?.last_price ?? 0);
```
When snap and liveQ are absent, `liveHold = null` → falls to `holdDcv` (broker's `day_change_val`)
instead of formula. `holdingsDayPnlStore` uses `last_price` → formula → different result.
`setFromPulse()` then overwrites the store with the wrong Pulse dcv value.

**2. Funds/margins showing zero after cold cache**
`_funds_snapshot_fn` returns empty `FundsResponse` (no rows) when `peek("funds")` is None.
When market is closed + cache cold (after restart / invalidate), `closed_hours_or_broker`
calls `snapshot_fn()` → empty rows → zeros displayed. Old code called broker when cold.

## Files to change
- `frontend/src/lib/data/pulseUnified.js` line 553-554: add `r.last_price` to liveHold chain
- `backend/api/routes/funds.py` lines 195-206: call broker when peek returns None

## Agents
- frontend: In `frontend/src/lib/data/pulseUnified.js` at line 553-554, change:
  ```js
  const liveHold = (_snapLtp != null && Number(_snapLtp) > 0) ? Number(_snapLtp)
                 : (Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp) : null);
  ```
  To:
  ```js
  const liveHold = (_snapLtp != null && Number(_snapLtp) > 0) ? Number(_snapLtp)
                 : (Number(liveQ?.ltp) > 0 ? Number(liveQ.ltp)
                 : (Number(r.last_price) > 0 ? Number(r.last_price) : null));
  ```
  This matches `holdingsDayPnlStore._store` line 82-84's `h.last_price` fallback exactly.

  Add 4 cross-check Vitest tests in `frontend/src/lib/__tests__/data/pulseRowsAndFlash.test.js`
  under a describe block `"mergeHoldingRows — H:1 day_pnl cross-check vs broker day_change_val"`.
  Rule: each test sets `day_change_val` to an intentionally WRONG value (e.g. 99999) so that
  if the code falls through to dcv instead of using the formula, the test fails immediately.

  Test 1 — snap LTP wins:
    snap.ltp=2850, previous_close=2800, quantity=100, day_change_val=99999
    assert day_pnl ≈ 5000 (formula: (2850-2800)*100)

  Test 2 — liveQ wins (no snap):
    snap=null, liveQ.ltp=2850, previous_close=2800, quantity=100, day_change_val=99999
    assert day_pnl ≈ 5000

  Test 3 — last_price wins (no snap, no liveQ) — THIS IS THE BUG PATH:
    snap=null, liveQ=null, r.last_price=2850, previous_close=2800, quantity=100, day_change_val=99999
    assert day_pnl ≈ 5000
    (with the bug, day_pnl = 99999 because liveHold was null → dcv used)

  Test 4 — all null → dcv is correct fallback:
    snap=null, liveQ=null, r.last_price=null, previous_close=2800, quantity=100, day_change_val=5000
    assert day_pnl = 5000 (no LTP available anywhere → dcv is correct)

- backend: In `backend/api/routes/funds.py` lines 195-206, change `_funds_snapshot_fn` from returning empty FundsResponse when cold to calling broker:
  ```python
  async def _funds_snapshot_fn() -> FundsResponse:
      cached = peek("funds")
      if cached is not None:
          return cached
      # Cache cold — no DB snapshot for funds; call broker (only data source)
      return await get_or_fetch("funds", _fetch, ttl_seconds=_TTL)
  ```
  Update `backend/tests/test_funds_snapshot.py` `test_funds_closed_cache_cold_*` test to assert cold cache + market closed → broker IS called once (not zero times).

- broker: skip
- doc: skip
- playwright: skip
- backend-test: skip (backend agent handles test update)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(holdings,funds): liveHold last_price fallback in mergeHoldingRows + funds cold-cache calls broker

## Done when
- Holdings day P&L in Pulse uses last_price fallback when no snap/liveQ → matches holdingsDayPnlStore formula
- Funds/margins show real broker values (not zero) when cache is cold after restart
- pytest green, svelte-check 0 errors, vitest 0 failures
