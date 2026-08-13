# Plan: Audit punch-list fixes (P1 × 2, P2 × 3, P3 × 4)

## Context
Full SSOT + stale-code audit across all three layers surfaced 9 actionable items (remainder
accepted as architectural limitations or false positives per exploration). Fixes are grouped
by layer to avoid agent cross-contamination.

Not fixing (accepted):
- `background.py` async gap — documented in-code as accepted architectural limitation
- `ssot_fetch` infinite TTL — instruments refreshed daily by `_task_instruments` background task
- `holdings.py:_override_stale_ltp_from_ticker` inline formula — correct in vectorised DataFrame context
- `sim/driver.py` inline day P&L — appropriate in simulation-only context
- `funds.py` missing `closed_hours_or_broker` — no DB snapshot table for funds; gate would require new feature
- `MarketPulse.svelte` byKey comment — confirmed CORRECT by exploration (no fix needed)

## Task

### Broker layer (4 items)
**P1-A** `backend/brokers/kite_ticker.py:unsubscribe()` (lines 433–449)
- Currently prunes `_subscribed` + `_tick_age` but NOT `_tick_map` or `_sym_to_token`
- Fix: inside the `with self._lock` block, after `self._subscribed -= drop`, also do:
  `for tok in drop: self._tick_map.pop(tok, None)` and prune `_sym_to_token` by value
- Note: `_sym_to_token` is keyed by symbol string, valued by token int. Must scan values to prune.

**P1-B** `backend/brokers/client/remote_broker.py:RemoteBroker.__init__` (line 60)
- `Broker.__init__` sets `self._last_req: dict = {}` and `self._last_resp: dict = {}`
- `RemoteBroker.__init__` never calls `super().__init__()` → diagnostic dicts uninitialized
- Fix: add `super().__init__()` as first line of `RemoteBroker.__init__`

**P2-A** `backend/brokers/adapters/groww.py:~771` `@ssot_fetch` cache key
- Key is `f"groww_instruments_{self.account}"` — ignores `exchange` arg
- If caller A fetches `exchange="NSE"` then caller B fetches `exchange="BSE"`, B gets NSE data
- Fix: change key lambda to `lambda self, *a, **kw: f"groww_instruments_{self.account}_{(a[0] if a else kw.get('exchange')) or 'all'}"`

**P2-B** `backend/brokers/broker_apis.py:_FETCH_HEALTH` ghost entries (lines 244, 934+)
- `_FETCH_HEALTH: dict[str, dict]` grows indefinitely — no eviction for decommissioned accounts
- Fix: add a `_prune_fetch_health()` helper that removes entries whose account key is not in the
  current `broker_accounts` table. Call it at boot (after loading saved CB state) and once every
  24h via the existing health-check cycle. Import `broker_accounts` table inside the function to
  avoid circular imports.

### Backend API layer (2 items)
**P3-A** `backend/api/routes/positions_helpers.py:225–229` stale docstring
- Remove the "Branch A/B" frontend coupling from `prev_settlement_pnl` docstring
- Replace with backend-only semantics: "frozen yesterday total_pnl from daily_book; when set,
  day P&L = total_pnl − prev_settlement_pnl; when None, fallback applies"

**P3-B** `backend/api/routes/positions.py:21–30` stale imports
- Agent to grep each imported name from `positions_helpers` in the file body (excluding the import
  line) and remove any that have zero references. Candidates: `build_snapshot_position_row`,
  `extract_snapshot_extras`, `extract_snapshot_multiplier`.

### Frontend layer (3 items)
**P2-C** `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:_dayPnlForLeg` (~line 1981)
- Bug: `(legLiveLtp - close) * qty` fires for new intraday F&O positions where `overnight_quantity=0`
  but `close > 0` (the instrument had a yesterday close even though THIS position was opened today)
  → gives overnight drift P&L instead of intraday entry-based P&L
- Fix: guard with `oq !== 0` before the close-based formula:
  ```js
  const oq = Number(c.overnight_quantity ?? c.opening_quantity ?? 0);
  if (oq !== 0 && legLiveLtp != null && Number(legLiveLtp) > 0 && close > 0 && qty !== 0) {
    return (Number(legLiveLtp) - close) * qty;
  }
  return baseDayPnlForPosition(c);
  ```

**P3-C** `frontend/src/lib/PositionStrip.svelte:13–14` unused imports
- Remove `cachedRead`, `cachedWrite`, `cachedDelete`, `TTL` from persistentCache import
- Remove `symbolStore` from symbolStore import (keep `getSnapshot`, `symbolTickCount`)

**P3-D** `frontend/src/lib/data/pulseUnified.js:mergeHoldingRows` dead `isMarketOpen` param
- `isMarketOpen` is destructured from ctx but never called inside the function
- Fix: remove from destructuring `const { snapOf, getInst } = ctx;` (drop `isMarketOpen`)
- Also remove from the JSDoc `@param` for `mergeHoldingRows`'s ctx
- Check all callers: if any pass `isMarketOpen` in the ctx object, leave those — only fix the
  dead read inside the function

## Agents
- broker: Fix P1-A, P1-B, P2-A, P2-B in backend/brokers/. Write tests for each fix.
- backend: Fix P3-A, P3-B in backend/api/routes/. Write tests for any changed lines.
- frontend: Fix P2-C, P3-C, P3-D in frontend/. Write Vitest test for P2-C _dayPnlForLeg.
- backend-test: skip (broker agent writes its own tests; backend agent writes its own)
- playwright: skip
- doc: skip (no spec changes required for these fixes)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(audit): P1 ticker ghost-subs + RemoteBroker super(); P2 groww exchange key + CB eviction + derivatives new-position oq guard; P3 dead imports/docstrings

## Done when
- `kite_ticker.py:unsubscribe()` prunes `_tick_map` + `_sym_to_token`
- `RemoteBroker.__init__` calls `super().__init__()`
- `groww.py` ssot_fetch key includes exchange
- `_FETCH_HEALTH` prunes ghost entries at boot
- `_dayPnlForLeg` uses `oq !== 0` guard before close-based formula
- All unused imports removed from PositionStrip.svelte
- `isMarketOpen` dead param removed from mergeHoldingRows destructuring
- `prev_settlement_pnl` docstring no longer references Branch A/B
- Stale imports verified + removed from positions.py
- pytest passes, svelte-check 0 errors
