# Plan: Fix frontend hang + event loop blocking + Dhan holdings zeros

## Context

Three audits completed. Root causes confirmed with file:line evidence.

**Hang (Pulse/Derivatives page):** `MarketPulse.svelte` `tickBus.subscribe` callback fires
`refreshCells({ force: true })` on up to 4 ag-Grid instances immediately per tick, PLUS a
second round via each symbol's 300ms clearance timer. At 8-12 Hz with 20-50 subscribed
symbols, this means 32-48 forced ag-Grid redraws/sec + O(N) Set spreads for `_ltpFlashUp`/
`_ltpFlashDown` per tick. JS main thread saturated → browser hang.

**Backend blocking (P1 — order hot path):** `actions_live.py:118,178,291` calls
`send_order_failure_alert(...)` bare inside three `async def` functions. Call chain resolves
to `_alert_route → _send_telegram → requests.post(timeout=10)`. 10-second blocking HTTPS
call on the event loop, on every failed order placement.

**Backend blocking (P2 — watchdog/pollers):** `background.py:3854` calls
`_fetch_special_sessions_safe` inside `_watchdog_check_market_open` (runs every 30s). On
daily cache miss, `fetch_special_sessions → fut.result(timeout=5)` blocks event loop for
up to 5s. Lines 4578 and 4628 call `is_any_segment_open(now_ist)` bare in two background
pollers; on daily cache miss → blocking DB query (Tier 3) or NSE HTTP call (Tier 4).

**Backend blocking (P3 — health endpoint):** `health.py:354-355` calls `_git_hash()` and
`_git_subject()` (each `subprocess.run(["git", ...], timeout=5)`) directly in
`async def get_health()`. Up to 10s block per call.

**Chain tab "Fetching expiries…" hang:** `OptionChainTab.svelte` retries `fetchChainExpiries`
up to 12 times × 5s = 60s when the backend returns `expiries=[]`. The backend returns empty
when `cache.peek("instruments_chain")` is None (instruments cold). But when the event loop is
blocked by `send_order_failure_alert` (up to 10s per call, 3 call sites in `actions_live.py`),
the HTTP response for `/api/options/chain-quotes` is delayed. The 5s retry fires while the event
loop is still blocked → response delayed again → appears to hang indefinitely. Fix: the
`actions_live.py` `asyncio.to_thread` wraps (in the backend agent below) clear this. No
separate chain-tab code change needed.

**Dhan holdings zeros (root cause 1 — LKG pre-backfill):** `_record_lkg_frame("holdings",
account, df_holdings)` is called inside `_fetch_holdings_local` (broker_apis.py:1455) AFTER
`_enrich_holdings` but BEFORE `_apply_backfill_to_list`. When Dhan returns `lastTradedPrice=0`
off-market, the LKG records zero-LTP rows. On subsequent calls that hit the stale-substitute
path (breaker open or interval gate), zeros are served.

**Dhan holdings zeros (root cause 2 — ssot_fetch caches zeros):** `_apply_backfill_to_list`
catches exceptions and returns raw zero-price frames (`return frames`). ssot_fetch caches any
non-None result (ssot_fetch.py:143-145). On PriceBroker rate-limit at first post-restart poll,
zeros are cached and served to all callers for the full cache window (30+s).

## Task

Fix all five root causes in order of severity.

## Agents

- frontend: Two fixes across Pulse and Chain pages.

  **Fix A — MarketPulse.svelte (Pulse page):** Debounce `refreshCells` calls in tickBus subscriber.
  File: `frontend/src/lib/MarketPulse.svelte`.

  Around line 2224 (near the other flash timer declarations), add:
  `let _flashRefreshTimer = /** @type {ReturnType<typeof setTimeout>|null} */ (null);`

  Extract a helper `_scheduleFlashRefresh()` that coalesces calls into a 50ms debounce:
  ```js
  function _scheduleFlashRefresh() {
    if (_flashRefreshTimer) return;
    _flashRefreshTimer = setTimeout(() => {
      _flashRefreshTimer = null;
      const cols = ['ltp', 'sparkline', 'day_pnl', 'pnl'];
      if (gridPositionsReady && gridPositions && showPositions)
        try { gridPositions.refreshCells({ columns: cols, force: true }); } catch (_) {}
      if (gridHoldingsReady && gridHoldings && showHoldings)
        try { gridHoldings.refreshCells({ columns: cols, force: true }); } catch (_) {}
      if (gridWinReady && gridWin && showWinners)
        try { gridWin.refreshCells({ columns: cols, force: true }); } catch (_) {}
      if (gridLoseReady && gridLose && showLosers)
        try { gridLose.refreshCells({ columns: cols, force: true }); } catch (_) {}
    }, 50);
  }
  ```

  In the `tickBus.subscribe` callback (around line 1553-1605):
  - Keep the `$state` writes for `_ltpFlashUp`/`_ltpFlashDown` immediate (needed for CSS classes)
  - Keep the per-sym clearance `setTimeout` (unchanged — needed for 300ms flash duration)
  - Replace the immediate `refreshCells` block (lines 1571-1582) with `_scheduleFlashRefresh()`
  - Replace the clearance timer's `refreshCells` block (lines 1592-1603) with `_scheduleFlashRefresh()`

  Add cleanup in `onDestroy` (near line 2531):
  `if (_flashRefreshTimer) { clearTimeout(_flashRefreshTimer); _flashRefreshTimer = null; }`

  **Fix B — Chain/Derivatives page:** Guard `_underlyingQuotes` `$state` write against no-op ticks.
  Files: `frontend/src/lib/data/underlyingQuoteUtils.js` and
  `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`.

  Problem: `_underlyingQuotes = applyUnderlyingTickLtp(...)` fires on every SSE tick (8-12Hz).
  Even when LTP hasn't changed between ticks, a new object is created and assigned, which triggers
  the `$effect` at line 1082 (flash.update loop) AND the template re-render at line 4798
  (`{@const _q = _underlyingQuotes[g.underlying]}`). This re-renders the entire underlying-totals
  section on every tick.

  1. In `underlyingQuoteUtils.js`, add no-op guard:
     ```js
     export function applyUnderlyingTickLtp(quotes, root, ltp) {
       if (!(root in quotes)) return quotes;
       const v = Number(ltp);
       if (!Number.isFinite(v) || v <= 0) return quotes;
       if (quotes[root].ltp === v) return quotes;  // same LTP — no $state write needed
       return { ...quotes, [root]: { ...quotes[root], ltp: v } };
     }
     ```
  2. In `derivatives/+page.svelte` line 1848, guard the assignment:
     ```js
     const _next = applyUnderlyingTickLtp(_underlyingQuotes, root, snap.ltp);
     if (_next !== _underlyingQuotes) _underlyingQuotes = _next;
     ```
     This prevents Svelte 5 reactivity from firing when the reference is unchanged.

  For every file you change or create, you MUST write or update at least one test that covers
  the changed behaviour. This is mandatory — not optional.
  - Write/update a Vitest test in `frontend/src/lib/__tests__/data/underlyingQuoteUtils.test.js`
    that asserts `applyUnderlyingTickLtp` returns the SAME object reference when LTP is unchanged.
  - Write a Vitest test for MarketPulse behavior: mock `tickBus`, fire 20 rapid ticks,
    assert `refreshCells` is called ≤ once per 50ms window.

- backend: Fix three categories of event loop blocking.

  1. `backend/api/algo/actions_live.py` lines ~118, ~178, ~291: wrap `send_order_failure_alert`
     in `asyncio.to_thread`. These are inside `async def` functions (`_place_order_preflight_block`,
     `_place_order_on_failure`, `_close_position_preflight_block`). Change:
     ```python
     send_order_failure_alert(account=..., ...)
     ```
     to:
     ```python
     await asyncio.to_thread(send_order_failure_alert, account=..., ...)
     ```
     for all three call sites. The `send_order_failure_alert` import line stays inside the
     try block (it's a lazy import); just add `await asyncio.to_thread(...)` around the call.

  2. `backend/api/background.py` line 3854 — `_fetch_special_sessions_safe` inside
     `_watchdog_check_market_open` generator: pre-fetch special sessions for each segment
     exchange using `asyncio.to_thread` before the `any(is_market_open(...))` call. Change
     the function body to:
     ```python
     # pre-fetch special sessions (sync DB call — must not run on event loop)
     special_sessions: dict[str, list] = {}
     for seg in segments:
         exch = seg['holiday_exchange']
         if exch not in special_sessions:
             special_sessions[exch] = await asyncio.to_thread(
                 _fetch_special_sessions_safe, exch
             )
     return any(
         is_market_open(
             now,
             holiday_cache.get(seg['holiday_exchange'], set()),
             seg['hours_start'],
             seg['hours_end'],
             special_sessions=special_sessions.get(seg['holiday_exchange'], []),
         )
         for seg in segments
     )
     ```

  3. `backend/api/background.py` lines 4578 and 4628 — bare `is_any_segment_open(now_ist)`
     calls in `_task_funds_offhours` and `_task_closed_hours_refresh`: wrap with
     `await asyncio.to_thread(is_any_segment_open, now_ist)`.

  4. `backend/api/routes/health.py` lines 354-355 — `_git_hash()` and `_git_subject()`:
     wrap with `await asyncio.to_thread(_git_hash)` and `await asyncio.to_thread(_git_subject)`.

  For every file you change or create, you MUST write or update at least one test that covers
  the changed behaviour. This is mandatory — not optional.
  Add a pytest test in `backend/tests/` that verifies `send_order_failure_alert` is NOT
  called directly in the async functions (patch the function and assert it was called via
  `asyncio.to_thread` by checking the call pattern in `_place_order_preflight_block`).

- broker: Fix two Dhan holdings zero root causes in `backend/brokers/broker_apis.py`.

  1. Post-backfill LKG recording in `_fetch_holdings_cached` (line ~1319):
     ```python
     @ssot_fetch(mode="coalesce", key="holdings")
     def _fetch_holdings_cached() -> list[pd.DataFrame]:
         if _use_conn_service():
             from backend.brokers.client import sync as conn_sync
             result = conn_sync.fetch_holdings()
         else:
             result = _fetch_holdings_local()
         backfilled = _apply_backfill_to_list(result)
         # Upgrade LKG to post-backfill prices so stale-substitute never serves zeros.
         if backfilled and len(backfilled) > 0:
             combined = backfilled[0]
             if not combined.empty and 'account' in combined.columns:
                 for acct, df_acct in combined.groupby('account', sort=False):
                     _record_lkg_frame("holdings", str(acct), df_acct.copy())
         return backfilled
     ```
     Do the same for `_fetch_positions_cached` (line ~1330).

  2. `_apply_backfill_to_list` exception handler (line ~1737): change `return frames` to
     `raise` so ssot_fetch does NOT cache the zero-price result on backfill failure.
     Change the `except` block to:
     ```python
     except Exception as _e:
         logger.warning(f"_apply_backfill_to_list: backfill failed: {_e}")
         raise  # do not cache zero-price frames via ssot_fetch
     ```

  For every file you change or create, you MUST write or update at least one test that covers
  the changed behaviour. This is mandatory — not optional.
  - Test that `_fetch_holdings_cached` upgrades LKG after backfill: mock `_fetch_holdings_local`
    returning zero-LTP frames, mock `backfill_market_data` to patch them to non-zero,
    call `_fetch_holdings_cached`, then verify `_get_lkg_frame("holdings", acct)` returns
    the patched (non-zero) frame.
  - Test that `_apply_backfill_to_list` propagates exception: mock `backfill_market_data`
    to raise `RuntimeError`, call `_apply_backfill_to_list([some_df])`, assert it raises.

- doc: skip
- backend-test: skip (tests included in agent briefs above)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Dev verification (required before dprod)
Every change must be verified on dev.ramboq.com before promoting to prod. The playwright agent
must run targeted specs against dev.ramboq.com that confirm:

1. **Pulse page hang fix** — Open Pulse page, let SSE ticks run for 30s, assert no browser
   tab freeze (use `page.waitForTimeout` + `page.evaluate(() => document.title)` still returns
   within 500ms as a liveness check). Verify LTP cells flash correctly.

2. **Chain tab expiry hang fix** — Open order ticket → Chain tab, assert expiries load within
   10s (not 60s). Verify tab is interactive (can pick expiry, strikes render).

3. **Derivatives page hang fix** — Open derivatives page with an underlying, let SSE ticks run
   for 20s, assert Spot LTP cell updates and tab remains responsive.

4. **Dhan holdings zeros fix** — After market close, open holdings page for Dhan account,
   assert `cur_val > 0` for all rows (no zeros). Check that reloading holdings does not blank
   the prices.

5. **Backend blocking fix (smoke)** — Verify `/api/health` responds in < 2s. Verify positions
   and holdings routes respond in < 3s during off-hours.

The playwright agent targets `https://dev.ramboq.com` only. All specs must pass before the
doc agent runs and before `git merge dev→main`.

## Commit message
fix(perf): debounce Pulse refreshCells + asyncio.to_thread blocking + Dhan LKG post-backfill

## Done when
- MarketPulse.svelte tickBus subscriber fires `refreshCells` at most once per 50ms (not per tick)
- Chain page `_underlyingQuotes` `$state` write is no-op when LTP unchanged between ticks
- `send_order_failure_alert` dispatched via `asyncio.to_thread` in actions_live.py (3 sites)
- `_fetch_special_sessions_safe` and bare `is_any_segment_open` wrapped in background.py
- `_git_hash`/`_git_subject` wrapped in health.py
- `_fetch_holdings_cached` and `_fetch_positions_cached` record LKG after backfill
- `_apply_backfill_to_list` re-raises on exception (ssot_fetch never caches zeros)
- All new tests green; svelte-check 0 errors; no CC regressions
- Playwright specs pass on dev.ramboq.com for all 5 verification points above
- Only after dev verification: merge to main and push prod
