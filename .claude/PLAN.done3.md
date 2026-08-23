# Plan: Market data architecture — prev_close, daily window, WebSocket, cadence, animation threshold

## Task
Five coordinated fixes from the market data architecture redesign session:

1. **prev_close fix** — `_override_stale_close_from_snapshot` (positions.py) and `_override_stale_close_for_holdings` (holdings.py) both query `COALESCE(daily_book.previous_close, daily_book.ltp)` as ref_close. Since `previous_close` is populated from Kite's stale BHAV-copy API, COALESCE returns the stale value and the epsilon check passes → no patching → wrong day P&L. Fix: change both queries to use `daily_book.ltp` directly.

2. **Daily operating window** — KiteConnect should start at 08:00 IST (aligned with token refresh, so prev_close is set from daily_book.ltp before any live tick arrives), unsubscribe non-MCX symbols at 16:15 IST, and disconnect at 00:30 IST (after MCX settlement at 00:15). Currently KiteConnect starts at service startup with no scheduled stop.

3. **WebSocket subscribe on order fill** — when a new option is traded from the chain tab, it is not in positions/holdings/movers/watchlist. It gets WebSocket coverage only on the next book poll (up to 5 min gap). Fix: in the Kite postback handler, on `status == "COMPLETE"`, extract `instrument_token` from the payload and call `get_ticker().subscribe([token])` immediately.

4. **Backend cadence alignment** — `_task_close` fetches positions + holdings + margins independently from the broker at the same 5-min cadence as `_task_performance`. This doubles broker API calls. Fix: pass the already-fetched `(df_holdings, df_positions, df_margins)` from `_task_performance` into the close-summary check. Additionally, the Kite postback handler should call `kick_performance()` on COMPLETE status so the book poller fires immediately after a fill (currently only the sim driver calls kick_performance).

5. **Animation threshold alignment** — `ui.ltp_flash_pct` (default 0.1%) is read reactively only by MarketPulse's tickBus gate. All other `createTickFlash` instances use hardcoded defaults: PositionStrip (`threshold: 0`), NavCard (`threshold: 0`), PerformancePage (`threshold: 0.001`), dashboard `_dashFlash` (`threshold: 0.001`). Fix: add a `setPctThreshold(v)` method to `createTickFlash`, and subscribe each instance to `ltpFlashPct` so the operator-configured threshold applies everywhere.

## Agents

- backend:
  (a) positions.py `_override_stale_close_from_snapshot`: in the SQL query, change `COALESCE(daily_book.previous_close, daily_book.ltp) AS ref_close` → `daily_book.ltp AS ref_close`. Update docstring.
  (b) holdings.py `_override_stale_close_for_holdings`: same change.
  (c) background.py: `_task_close` is dead code — defined at line 1091 but NOT in the supervised startup list. Close summaries are currently not being sent. Fix: extract close-summary logic from `_task_close` into `_perf_run_close_check(df_h, df_p, df_m, now, today, seg_state, ...)`. Call it at the end of each `_task_performance` iteration after `_perf_fetch_all_broker_data()` returns. Delete `_task_close` function. Update module-level docstring (remove `_task_close` from item 3). This restores close summaries without any additional broker API call.
  (d) orders_postback.py: on `status == "COMPLETE"` in the Kite handler, call `kick_performance()` (same signal the sim driver uses) to trigger immediate book refresh. Import from `backend.api.background`.

- broker:
  (a) orders_postback.py (Kite): extract `instrument_token = body.get("instrument_token")` alongside existing fields at line ~809. On `status == "COMPLETE"`, call `get_ticker().subscribe([int(instrument_token)])` if token is non-null. `subscribe()` is idempotent.
  (b) orders.py `order_postback_dhan`: after `_process_broker_postback`, check `_broker_is_fill_status("dhan", str(body.get("orderStatus") or ""))`. On fill: (1) call `kick_performance()`; (2) resolve token: `from backend.api.persistence.instruments_store import get_or_fetch_instruments; tok_map = await get_or_fetch_instruments(kite_exchange); tok = tok_map.get((kite_symbol.upper(), kite_exchange))` — then `get_ticker().subscribe([tok])` if tok is non-null. Wrap both in try/except (non-fatal — next book poll is backstop).
  (c) orders.py `order_postback_groww`: same pattern — on COMPLETE fill, kick_performance() + `get_or_fetch_instruments(exchange)` lookup + subscribe. Groww postback support is uncertain; best-effort only.
  (d) kite_ticker.py: `stop()` already exists (line 556) but does NOT reset `_started`. Add `restart(api_key, access_token, account)` method: call `stop()`, reset `self._started = False`, `self._subscribed = set()`, `self._pending = set()`, `self._connected = False`, then call `self.start(api_key, access_token, account)`. Add `async def unsubscribe_non_mcx(self)`: fetch MCX token set via `await get_or_fetch_instruments("MCX")` from `instruments_store`, compute `drop = self._subscribed - set(mcx_map.values())`, call `self.unsubscribe(list(drop))`.
  (e) background.py `_task_daily_snapshot`: add two triggers — at 16:15 IST call `await get_ticker().unsubscribe_non_mcx()`; at 00:30 IST call `get_ticker().stop()`. At 08:00 IST (existing token-refresh trigger), after token refresh succeeds, call `get_ticker().restart(api_key, new_access_token, account)` — this reconnects with fresh credentials. No separate prev_close write needed: override functions already read `daily_book.ltp` at query time; starting the ticker before the first book poll is sufficient.

- frontend:
  (a) tickFlash.svelte.js `createTickFlash`: change closure `pctThreshold` to `let _pctThreshold = $state(pctThreshold)`. Add `setPctThreshold(v) { _pctThreshold = v; }` to the returned object. Update `update()` to read `_pctThreshold`.
  (b) PositionStrip.svelte line 259: after creating `flash`, add: `import { ltpFlashPct } from '$lib/stores'` and in `onMount`/`$effect`: `const _unsub = ltpFlashPct.subscribe(v => flash.setPctThreshold(v)); return _unsub;`
  (c) NavCard.svelte line 105: same pattern for `flash`.
  (d) PerformancePage.svelte line 335: same pattern for `_perfFlash`.
  (e) dashboard/+page.svelte line 74: same pattern for `_dashFlash`. For `_wlLtpFlash` (line 1316, created reactively), add a subscribe that calls `_wlLtpFlash?.setPctThreshold(v)` when `ltpFlashPct` changes.
  (f) derivatives/+page.svelte line 1051: `flash` uses `get(ltpFlashPct)` at creation (static). Add reactive subscribe → `flash.setPctThreshold(v)`.

- doc: skip

- backend-test:
  (a) prev_close fix: mock daily_book rows with `previous_close=100.0, ltp=102.0`. Verify both override functions use 102.0 as ref_close, not 100.0. Epsilon boundary: |daily_book.ltp − close_price| > 0.005 triggers patch; ≤ 0.005 does not (abs tolerance 0.005 on boundary check).
  (b) postback subscribe: mock `get_ticker()`, send COMPLETE postback with `instrument_token=12345`. Assert `ticker.subscribe([12345])` called and `kick_performance()` called.
  (c) close-summary restore: verify `_perf_run_close_check` fires from `_task_performance` when `now >= close_trigger` for a segment. Mock `now` past the NSE close trigger (15:45 IST + offset); assert summarise_holdings + summarise_positions called. Verify `_task_close` no longer exists in `background.py` (grep for the function definition).
  (d) createTickFlash setPctThreshold: verify updates below threshold are suppressed after `setPctThreshold(0.5)`; verify updates above threshold still flash.
  (e) Holdings unrealized P&L match (broker vs RamboQuant): mock holdings DataFrame with avg_price=500.0, qty=10, ltp=520.0; broker pnl=200.0. Assert RamboQuant `cur_val − inv_val` matches broker pnl within `pytest.approx(rel=1e-4, abs=0.01)` (0.01% relative or ₹0.01 absolute, whichever is larger). Same pattern for a multi-row DataFrame — assert per-row and total.
  (f) Positions unrealized P&L match (broker vs RamboQuant): mock overnight positions with known avg_price=1200.0, qty=5, ltp=1250.0; broker pnl=250.0. Assert RamboQuant computed pnl within same tolerance (rel=1e-4, abs=0.01). Include one short position (qty<0) to verify sign is correct.
  (g) Day P&L convergence (close_price == daily_book.ltp): mock holdings with close_price=100.0, daily_book.ltp=100.0, ltp=105.0, qty=10. Both broker day_change and RamboQuant day P&L should equal 50.0. Assert within pytest.approx(rel=1e-4, abs=0.01).
  (h) Day P&L divergence — BHAV window: mock close_price=100.0 (stale broker BHAV) and daily_book.ltp=102.0 (correct settlement snapshot), ltp=107.0, qty=10. Assert RamboQuant day P&L = (107−102)×10 = 50.0, NOT (107−100)×10 = 70.0. Broker day_change=70.0 intentionally diverges — do NOT assert equality against broker.
  (i) Day P&L direction consistency: three scenarios — gain (daily_book.ltp=100, ltp=105), loss (daily_book.ltp=100, ltp=95), flat (daily_book.ltp=ltp=100). Assert sign of RamboQuant day P&L is positive, negative, zero respectively. No magnitude tolerance — sign only.
  (j) Holdings internal consistency: mock multi-row holdings (5 rows, mixed qty/avg/ltp). Assert sum(ltp×qty) == sum(cur_val) within abs=0.01 and sum(avg_price×qty) == sum(inv_val) within abs=0.01. Catches floating-point accumulation errors across row aggregation.
  (k) Dhan postback TRADED → subscribe + kick: mock `get_ticker()`, `kick_performance()`, and instrument token lookup. Send Dhan TRADED postback with `orderStatus=TRADED`, `dhanClientId`, `tradingSymbol`, `exchangeSegment`. Assert `kick_performance()` called and `ticker.subscribe([resolved_token])` called. Also verify: CANCELLED Dhan postback does NOT trigger subscribe or kick.
  (l) Groww postback COMPLETE → subscribe + kick: same pattern — mock ticker + kick + lookup, send COMPLETE Groww postback, assert both called. Verify REJECTED status does not trigger either.

- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(market-data): daily_book.ltp as prev_close, 08:00-00:30 daily window, postback subscribe+kick all brokers, _task_close close-summary restore, animation threshold alignment

## Done when
- `_override_stale_close_from_snapshot` (positions.py) and `_override_stale_close_for_holdings` (holdings.py) both query `daily_book.ltp` directly — no COALESCE
- `_task_close` deleted; close-summary logic moved to `_perf_run_close_check` called from `_task_performance` — no separate broker fetch
- Kite postback on COMPLETE: `get_ticker().subscribe([instrument_token])` + `kick_performance()` both called
- Dhan postback on TRADED: `kick_performance()` + `get_or_fetch_instruments` token lookup + `subscribe()` called
- Groww postback on COMPLETE: same pattern (best-effort)
- `kite_ticker.py` has `restart()` + `unsubscribe_non_mcx()` methods; `_task_daily_snapshot` triggers `unsubscribe_non_mcx` at 16:15, `stop()` at 00:30, `restart()` at 08:00 after token refresh
- All `createTickFlash` instances expose `setPctThreshold()` and are subscribed to `ltpFlashPct`
- All pytest tests pass; broker coverage ≥ 80%; api coverage ≥ 45%; svelte-check 0 errors
- Holdings + positions unrealized P&L matches broker values within rel=1e-4, abs=0.01 per row and in aggregate
- Day P&L convergence test passes when close_price == daily_book.ltp; divergence test confirms RamboQuant uses daily_book.ltp not broker close_price
- Day P&L direction consistency verified for gain/loss/flat scenarios
- Internal consistency: sum(ltp×qty) == sum(cur_val) within abs=0.01 across multi-row holdings
