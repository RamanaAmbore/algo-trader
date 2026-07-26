# Plan: Reduce unnecessary polling noise + closed-hours poll slowdown + admin settings

## Context
Log panel flooded by "holdings/positions: market closed — serving daily_book snapshot"
(logger.info fires every 5 s from frontend book poller during closed hours). User wants:
(1) closed-hours book poller slowed to 30 min, live cadence in sync with ticker (5 s is
the ticker-sync proxy — coupling HTTP book polls to WS ticks would flood backend);
(2) both intervals configurable from /admin/settings (polling category), not just hardcoded;
(3) KiteTicker swap/unhealthy knobs surfaced in admin settings (ticker category).
Additional log-flood bugs: watchlist seed TOCTOU race and KiteTicker swap rapid-cycling log.
Timestamps stay accurate — `as_of` comes from server, not from fetch timestamp.

## Task

**Fix 1 — Backend: holdings + positions closed-hours log to debug**
`backend/api/routes/holdings.py:604` and `backend/api/routes/positions.py:996`:
Change `logger.info(...)` → `logger.debug(...)` for the "market closed — serving
daily_book snapshot" messages. These fire on every request (every 5 s during closed
hours = 720+/hr). Routine operational noise, not actionable events.

**Fix 2 — Backend: seed polling + ticker settings**
`backend/shared/helpers/settings.py` in the SEEDS list:

Add to `polling` category (already has `polling.idle_timeout_min`):
```python
("polling", "polling.book_live_ms",   "int", 5000,
 "Book poller cadence during market hours (ms). Drives holdings/positions/funds "
 "refresh rate. Lower = faster, more broker API calls. Syncs with ticker activity — "
 "5 s catches any fill within one poll cycle.",
 "ms", {"min": 1000, "max": 60000, "step": 500}),
("polling", "polling.book_closed_ms", "int", 1800000,
 "Book poller cadence during NSE/MCX market closure (ms). Holdings and positions "
 "are frozen during closed hours; a 30-minute refresh is sufficient and eliminates "
 "720+/hr backend hits. Auto-restores to book_live_ms on market open.",
 "ms", {"min": 60000, "max": 7200000, "step": 60000}),
```

Add new `ticker` category for the three kite_ticker knobs already read by `get_int()` in
`backend/brokers/service/app.py` (lines ~262-274) but not yet seeded (invisible in UI):
```python
("ticker", "kite_ticker.unhealthy_threshold", "int", 2,
 "Consecutive missed heartbeats before a Kite account is considered unhealthy "
 "and triggers an auto-swap to the next available account.",
 None, {"min": 1, "max": 10, "step": 1}),
("ticker", "kite_ticker.swap_cooldown_seconds", "int", 300,
 "Minimum seconds between auto-swaps of the KiteTicker account. Prevents rapid "
 "ping-pong when multiple accounts are marginal. Default 5 min.",
 "s", {"min": 30, "max": 3600, "step": 30}),
("ticker", "kite_ticker.all_down_watchdog_seconds", "int", 60,
 "Seconds the watchdog waits before declaring all accounts unhealthy and "
 "attempting a forced restart of the KiteTicker.",
 "s", {"min": 10, "max": 300, "step": 10}),
```

**Fix 3 — Frontend: admin settings CATEGORY_ORDER**
`frontend/src/routes/(algo)/admin/settings/+page.svelte` line ~63:
Add `'polling'` and `'ticker'` to `CATEGORY_ORDER` (after `'performance'`, before
`'simulator'`):
```js
const CATEGORY_ORDER = ['execution', 'orders', 'alerts', 'algo', 'performance',
                        'polling', 'ticker', 'simulator', 'notifications', 'logging', 'misc'];
```
No other change — the settings page renders all DB rows grouped by category automatically.

**Fix 4 — Frontend: adaptive book poller driven by DB settings**
`frontend/src/lib/data/marketDataStores.svelte.js`:
- Change `let _bookForegroundMs = 5_000;` to `let _bookForegroundMs = 5_000;` (keep as-is
  — it is the current active interval and `setBookPollerInterval()` already updates it).
- Add `let _bookLiveMs = 5_000;` alongside `_bookForegroundMs`. `_bookLiveMs` holds the
  live-hours baseline and is only updated by the new `setBookPollerLiveMs()` export.
- Add `let _bookClosedMs = 30 * 60 * 1_000;` (fallback default = 30 min).
- Export `setBookPollerLiveMs(ms)` — sets `_bookLiveMs` (does NOT restart the interval;
  takes effect on the next state-transition check in `_tickBookPollers`).
- Export `setBookPollerClosedMs(ms)` — sets `_bookClosedMs` (same lazy pattern).
- Add `let _holdingsSnapshotAt = $state(null);` near the holdings store definition.
- In `holdingsStore` parse function, add `_holdingsSnapshotAt = r?.as_of ?? null;` before
  `return rows`. Closed-hours response carries `as_of`; live response does not.
- At end of `_tickBookPollers()` (after `_bookPollerTick++`):
  ```js
  const _wantMs = _holdingsSnapshotAt != null ? _bookClosedMs : _bookLiveMs;
  if (_wantMs !== _bookForegroundMs) setBookPollerInterval(_wantMs);
  ```

`frontend/src/routes/(algo)/+layout.svelte` — in the existing settings-fetch IIFE
(lines ~807-824 that already reads `polling.idle_timeout_min` and `pulse.tick_interval_ms`),
add two more lookups after the `tickRow` block:
```js
const liveRow   = all.find?.(s => s?.key === 'polling.book_live_ms');
const liveV     = Number(liveRow?.value ?? liveRow?.default_value);
if (Number.isFinite(liveV) && liveV >= 1000) setBookPollerLiveMs(liveV);

const closedRow = all.find?.(s => s?.key === 'polling.book_closed_ms');
const closedV   = Number(closedRow?.value ?? closedRow?.default_value);
if (Number.isFinite(closedV) && closedV >= 60000) setBookPollerClosedMs(closedV);
```
Also import `setBookPollerLiveMs` and `setBookPollerClosedMs` alongside the existing
`setBookPollerInterval` import from `marketDataStores.svelte.js`.

**Fix 5 — Backend: watchlist seed_global_pinned concurrency guard**
`backend/api/routes/watchlist.py`:
Add `_SEED_LOCK = asyncio.Lock()` at module level. Wrap the body of `seed_global_pinned()`
in `async with _SEED_LOCK:` as the first statement inside the function (before
`async with async_session()`). Eliminates 67 wave-log entries in 350ms at startup.

**Fix 6 — Broker: KiteTicker swap log rate-limit**
`backend/brokers/kite_ticker.py` in `record_swap()` (lines ~738-741):
Add `self._last_swap_log_ts = 0.0` in `__init__`. In `record_swap()`, wrap the
`logger.info(...)` call:
```python
_now = time.time()
if len(self._swap_history) % 10 == 0 or _now - self._last_swap_log_ts > 5.0:
    self._last_swap_log_ts = _now
    logger.info(...)
```
Logs every 10th swap or at most once per 5 s — kills 128-line floods during ping-pong.

**Fix 7 — Frontend: LogPanel conn tab row height matches system tab**
`frontend/src/lib/LogPanel.svelte` — CSS section for `.lp-conn-row` (lines ~2354–2383):

Root cause: `.lp-conn-time` has `min-width: 13rem` + `white-space: normal`, which lets
the dual-tz string ("Fri 25 Jul · 09:15 IST · 23:45 EDT", ~17rem wide) wrap at the `·`
separators inside the 13rem box → each conn row expands to 2 lines. System tab rows use
`flex: 0 0 auto` with no min-width, so the time column grows naturally and never wraps.

Changes to `.lp-conn-row`:
- Remove `white-space: nowrap` and `overflow: hidden` (each column handles its own overflow)
- Change `gap: 0.25rem` → `column-gap: 0.4rem; row-gap: 0.05rem;` (match `.log-row`)
- Change `padding: 0.28rem 0.5rem` → `padding: 0.28rem 0;` (match `.log-row`)
- Add `flex-wrap: wrap;` (make wrap the default, matching `.log-row` baseline)

Changes to `.lp-conn-time`:
- Remove `min-width: 13rem`, `white-space: normal`, `overflow-wrap: break-word`
- Replace with `flex: 0 0 auto; white-space: nowrap;` — natural content width, no wrap

Add desktop breakpoint (match `.log-row` ≥1024px rule):
```css
@media (min-width: 1024px) {
  .lp-conn-row { flex-wrap: nowrap; }
}
```

Remove the old mobile-only `@media (max-width: 640px)` breakpoint for `.lp-conn-row`
(wrap is now the default; the old rule only toggled on wrap that's already the default).
Keep `.lp-conn-acct { word-break: break-all; }` at mobile breakpoint if needed.

Result: conn rows are single-line on desktop (identical height to system rows), wrap
gracefully on mobile exactly like system tab rows.

## Files to modify
- `backend/api/routes/holdings.py` line 603–605
- `backend/api/routes/positions.py` line 994–997
- `backend/shared/helpers/settings.py` — SEEDS: add polling.book_live_ms, polling.book_closed_ms, 3× kite_ticker.*
- `backend/api/routes/watchlist.py` — module-level lock + seed_global_pinned() wrapper
- `backend/brokers/kite_ticker.py` — record_swap() rate-limit + __init__
- `frontend/src/lib/LogPanel.svelte` — .lp-conn-row and .lp-conn-time CSS (lines ~2354–2383)
- `frontend/src/lib/data/marketDataStores.svelte.js` — _holdingsSnapshotAt + _bookLiveMs/_bookClosedMs + setBookPollerLiveMs/ClosedMs exports + adaptive tick
- `frontend/src/routes/(algo)/+layout.svelte` — settings fetch: read polling.book_live_ms + polling.book_closed_ms, import new setters
- `frontend/src/routes/(algo)/admin/settings/+page.svelte` — CATEGORY_ORDER: add 'polling', 'ticker'

## Agents

- backend: Four fixes. (1) `backend/api/routes/holdings.py:604`: `logger.info(` → `logger.debug(` for "market closed" message. (2) `backend/api/routes/positions.py:996`: same. (3) `backend/shared/helpers/settings.py` SEEDS: add `polling.book_live_ms` (int, 5000, ms, {min:1000, max:60000, step:500}) and `polling.book_closed_ms` (int, 1800000, ms, {min:60000, max:7200000, step:60000}) to the `polling` section (after `polling.idle_timeout_min`); add new `ticker` section with `kite_ticker.unhealthy_threshold` (int, 2, {min:1,max:10,step:1}), `kite_ticker.swap_cooldown_seconds` (int, 300, s, {min:30,max:3600,step:30}), `kite_ticker.all_down_watchdog_seconds` (int, 60, s, {min:10,max:300,step:10}). (4) `backend/api/routes/watchlist.py`: add `import asyncio` if missing; add `_SEED_LOCK = asyncio.Lock()` at module level; wrap body of `seed_global_pinned()` with `async with _SEED_LOCK:` before `async with async_session()`.
- frontend: Four files. (A) `frontend/src/lib/LogPanel.svelte` CSS section ~2354–2383: (i) on `.lp-conn-row`: remove `white-space: nowrap`, `overflow: hidden`; change `gap: 0.25rem` → `column-gap: 0.4rem; row-gap: 0.05rem;`; change `padding: 0.28rem 0.5rem` → `padding: 0.28rem 0;`; add `flex-wrap: wrap;`. (ii) on `.lp-conn-time`: remove `min-width: 13rem`, `white-space: normal`, `overflow-wrap: break-word`; add `flex: 0 0 auto; white-space: nowrap;`. (iii) add `@media (min-width: 1024px) { .lp-conn-row { flex-wrap: nowrap; } }`. (iv) remove the old `@media (max-width: 640px)` `.lp-conn-row` block (keep `.lp-conn-acct { word-break: break-all; }` inside a `@media (max-width: 640px)` block if still needed). (B) `frontend/src/lib/data/marketDataStores.svelte.js`: add `let _bookLiveMs = 5_000;` and `let _bookClosedMs = 30 * 60 * 1_000;` alongside `_bookForegroundMs`; export `setBookPollerLiveMs(ms)` (sets `_bookLiveMs`) and `setBookPollerClosedMs(ms)` (sets `_bookClosedMs`); add `let _holdingsSnapshotAt = $state(null);`; in `holdingsStore` parse add `_holdingsSnapshotAt = r?.as_of ?? null;`; at end of `_tickBookPollers()` add the wantMs/setBookPollerInterval adaptive block. (B) `frontend/src/routes/(algo)/+layout.svelte`: import `setBookPollerLiveMs, setBookPollerClosedMs` from marketDataStores; inside the existing settings-fetch IIFE, after the `tickRow` block, add lookups for `polling.book_live_ms` → `setBookPollerLiveMs()` and `polling.book_closed_ms` → `setBookPollerClosedMs()`. (C) `frontend/src/routes/(algo)/admin/settings/+page.svelte`: add `'polling'` and `'ticker'` to `CATEGORY_ORDER` (after `'performance'`, before `'simulator'`).
- broker: `backend/brokers/kite_ticker.py`: in `__init__`, add `self._last_swap_log_ts = 0.0`; in `record_swap()`, wrap the `logger.info(...)` with: `_now = time.time(); if len(self._swap_history) % 10 == 0 or _now - self._last_swap_log_ts > 5.0: self._last_swap_log_ts = _now; logger.info(...)`.
- doc: skip
- backend-test: New file `backend/tests/broker/test_watchlist_seed_lock.py`: call `seed_global_pinned()` concurrently from 5 asyncio tasks via `asyncio.gather()` and assert wave cleanup functions fire at most once total (not 5×). Mock the DB session so no real DB is needed.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(polling): closed-hours book poll 5s→30min; polling+ticker admin settings; holdings/positions log→debug; watchlist seed lock; kite swap log rate-limit

## Done when
- "holdings/positions: market closed" messages appear at DEBUG level (invisible in log panel)
- During NSE/MCX closure, book poller fires every 30 min (driven by DB setting, default 30 min)
- On market reopen, book poller returns to live cadence (driven by DB setting, default 5 s)
- `/admin/settings` shows a `polling` card with `book_live_ms` and `book_closed_ms` (alongside existing `idle_timeout_min`)
- `/admin/settings` shows a `ticker` card with `kite_ticker.unhealthy_threshold`, `kite_ticker.swap_cooldown_seconds`, `kite_ticker.all_down_watchdog_seconds`
- Watchlist cleanup wave logs appear once per wave per server start, not 17×
- KiteTicker swap log fires at most once per 5 s or every 10th swap
- LogPanel conn tab rows are the same height as system tab rows on desktop; both wrap correctly on mobile
- pytest green, svelte-check 0 errors
