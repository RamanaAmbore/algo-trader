# Plan: Day P&L zero for positions / wrong value for holdings

## Task

Three structural bugs cause wrong day P&L on positions and holdings. All have the same root cause: `_perf_fetch_all_broker_data` (the async function calling the sync threadpool workers) never applies the async close-price override, so the background task feeds NavStrip and MarketPulse with stale broker `day_change_val`. After BHAV copy (~18:00 IST), broker `close_price` = today's settlement, so `day_change_val = (settlement − settlement) × qty = 0` or wrong value (dev shows 18k, prod shows -8k, both wrong). HTTP routes apply the override correctly — only the background path is broken.

Bug 1: `_perf_fetch_all_broker_data` never calls `_override_stale_close_for_holdings` or `_override_stale_close_from_snapshot` after threadpool returns. Fix: call them in the async context of `_perf_fetch_all_broker_data`, then rebuild summaries.

Bug 2: `holdingsDayPnlStore.byAccount['TOTAL']` is hardcoded to `_store.total` (not pulse-aware). When MarketPulse pushes -8k via `setFromPulse`, `total = -8000` but `byAccount['TOTAL'] = 0` — headline ≠ breakdown.

Bug 3: Dashboard `_holdingsSummary` line 481 reads `Number(r.day_change_val) || 0` directly from broker rows, bypassing `previous_close`-based formula. Per-account breakdown = 0 while NavStrip H shows the pulse value.

## Agents

- backend: In `backend/api/background.py`, add two helper functions after `_fetch_holdings_direct` (~line 158): `def _rebuild_holdings_summary(raw: pd.DataFrame) -> pd.DataFrame` (groupby account, sum ['inv_val','cur_val','pnl','day_change_val'], call `_bg_holdings_add_pct`, append TOTAL row) and `def _rebuild_positions_summary(raw: pd.DataFrame) -> pd.DataFrame` (groupby account, sum ['pnl','day_change_val'], append TOTAL row). Then in `_perf_fetch_all_broker_data` (lines 477-501), after the holdings `wait_for` block, add: `try: from backend.api.routes.holdings import _override_stale_close_for_holdings; await _override_stale_close_for_holdings(df_holdings); sum_holdings = _rebuild_holdings_summary(df_holdings); except Exception as _oe: logger.warning(f"[PERF] holdings close-override failed: {_oe}")`. Same pattern for positions: import `_override_stale_close_from_snapshot` from `backend.api.routes.positions`, await it on `df_positions`, rebuild `sum_positions`. Remove the "accepted limitation" comment at lines 175-180. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
- frontend: (a) In `frontend/src/lib/data/holdingsDayPnlStore.svelte.js`, fix the `byAccount` getter (line 136): change `return _store.byAccount` to `if (_pulseTotal === null) return _store.byAccount; return { ..._store.byAccount, TOTAL: _pulseTotal };`. Remove JSDoc that says "not overridden by setFromPulse". (b) In `frontend/src/routes/(algo)/dashboard/+page.svelte` line 481, replace `byAcct[a].day_pnl += Number(r.day_change_val) || 0` with: `const _hClose = Number(r.previous_close) || Number(r.close_price) || 0; const _hLtp = Number(r.last_price ?? 0); const _hQty = Number(r.quantity ?? 0); const _hDcv = Number(r.day_change_val) || 0; byAcct[a].day_pnl += (_hClose > 0 && Math.abs(_hLtp - _hClose) > 0.005) ? (_hLtp - _hClose) * _hQty : _hDcv;`. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
- broker: skip
- doc: skip
- backend-test: Write pytest tests for the backend changes: (a) `_rebuild_holdings_summary` with multi-account patched df → verify TOTAL row matches sum of per-account rows and day_change_val comes from patched data. (b) `_rebuild_positions_summary` same pattern. (c) Mock `_override_stale_close_for_holdings` to mutate df in-place; verify `_perf_fetch_all_broker_data` calls it and returns rebuilt sum with patched day_change_val (not the original stale value). (d) Same for positions: mock `_override_stale_close_from_snapshot`, verify rebuilt summary. Place tests in `backend/tests/test_background_pnl_override.py`. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(pnl): apply async close override in _perf_fetch_all_broker_data, fix holdingsDayPnlStore TOTAL pulse mismatch, use previous_close formula in dashboard holdings summary

## Done when
- `_perf_fetch_all_broker_data` calls `_override_stale_close_for_holdings` and `_override_stale_close_from_snapshot` on raw DataFrames after threadpool fetch; rebuilt summaries flow to NavStrip with correct day P&L (not 0 or raw broker value)
- `holdingsDayPnlStore.byAccount['TOTAL']` equals `holdingsDayPnlStore.total` under all pulse states (pulse-active and null)
- Dashboard `_holdingsSummary.day_pnl` uses `(last_price - previous_close) * qty` when `previous_close > 0`; falls back to `day_change_val` otherwise
- All pytest tests pass; svelte-check 0 errors
