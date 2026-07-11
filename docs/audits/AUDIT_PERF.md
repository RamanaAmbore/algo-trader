# Performance Audit — 2026-06-02

## P0 (blocking — fix before next release)

- **[backend/api/routes/watchlist.py:513-522](backend/api/routes/watchlist.py)** — N+1 `COUNT` queries in `list_watchlists`. For each of the user's watchlists, a separate `SELECT count(WatchlistItem.id)` is issued inside a sequential `for wl in wls` loop. With 5 watchlists that is 5 round-trips to Postgres on every call to `GET /api/watchlist/`. Fix: one `GROUP BY watchlist_id` query outside the loop.

- **[backend/api/routes/quote.py:485-512](backend/api/routes/quote.py)** — Unbounded `broker.instruments()` calls on every warm-path sparkline request when all symbols are already past-cached. Re-fetches instrument dumps for every exchange (`NFO`, `BFO`, `NSE`, `BSE`) via `asyncio.to_thread` even when the past cache is fully warm. At 5-minute poll cadence and up to 100 symbols, this issues 4–5 blocking `~500 kB` HTTP fetches on every `/api/quote/sparkline` call. Fix: maintain a module-level `token_map` cache keyed by `(tradingsymbol, exchange, ist_date)`.

- **[backend/api/routes/orders.py:334-344](backend/api/routes/orders.py)** — `_fetch_orders()` runs `broker.orders()` per account sequentially inside `asyncio.to_thread`. When 2+ Kite accounts are loaded, one slow round-trip blocks the next. The `ThreadPoolExecutor` concurrency pattern used in `background.py` for holdings/positions/margins is not applied here. Fix: per-account concurrency via `asyncio.gather` (each offloaded) or `ThreadPoolExecutor.map`.

## P1 (worth doing soon)

- **[backend/shared/helpers/utils.py:290-291](backend/shared/helpers/utils.py)** — `mask_column(pd.Series([account]))[0]` allocates a pandas `Series` to mask a single string. Runs 10+ times per request across orders/holdings/positions. Replace with `_mask_account(s: str) -> str` using a module-level precompiled `re.sub(r'\d', '#', s)`.

- **[frontend/src/lib/LogPanel.svelte:91-102](frontend/src/lib/LogPanel.svelte) + [frontend/src/lib/UnifiedLog.svelte:120-127](frontend/src/lib/UnifiedLog.svelte) + [frontend/src/lib/ChartWorkspace.svelte:350](frontend/src/lib/ChartWorkspace.svelte)** — Raw `setInterval` with manual `document.hidden` check instead of the project-standard `visibleInterval` from `stores.js`. The callback still fires on hidden tabs (just early-returns). `visibleInterval` stops the timer entirely. Affects every page that embeds a LogPanel (Dashboard, Orders, Simulator, Agents, Console).

- **[backend/api/routes/watchlist.py:374-383](backend/api/routes/watchlist.py)** — `_ensure_default_watchlists` runs a no-op `UPDATE WatchlistItem SET tradingsymbol='NIFTY SMLCAP 100'` + `COMMIT` on every watchlist endpoint hit (list/get/quotes/add-item). One-off migration guard for a renamed symbol; the rename has completed. Remove the UPDATE after verifying zero stale rows.

- **[frontend/src/lib/SymbolPanel.svelte:541](frontend/src/lib/SymbolPanel.svelte) + [frontend/src/lib/order/OptionChainTab.svelte:290](frontend/src/lib/order/OptionChainTab.svelte) + [frontend/src/routes/(algo)/admin/derivatives/+page.svelte:1122](frontend/src/routes/(algo)/admin/derivatives/+page.svelte)** — Three separate `setInterval(...refreshChainQuotes, 5000)` / `setInterval(...loadOrdersData, 3000)` calls without visibility gating. OptionChainTab fires 12 Kite quote calls/min per mounted instance even when the browser tab is backgrounded.

## P2 (nice-to-have)

- **[backend/api/routes/quote.py:33-61](backend/api/routes/quote.py)** — `_resolve_token_for_sym` iterates up to 5 full exchange instrument dumps in series (each ~90k rows) to find one token. Called on every watchlist `POST /{wl_id}/items`. Not cached. Should read from the 24-hour shared cache that `routes/instruments.py` already maintains.

- **[backend/api/algo/agent_engine.py:82-107](backend/api/algo/agent_engine.py)** — `_update_pnl_history` calls `df.iterrows()` over `sum_positions` and `sum_holdings` on every `run_cycle`. `iterrows()` is the slowest DataFrame iteration pattern. At 2-second sim cadence this compounds. Replace with vectorised column extraction (`zip(df['account'], df['pnl'], df.get('pnl_percentage'))`).

- **[frontend/src/lib/stores.js:318-320](frontend/src/lib/stores.js)** — `nowStamp` clock uses a bare `setInterval(…, 60_000)` at module level with no visibility gating and no teardown export. Low cost, but violates the project convention.

## Notes

- `_QUOTE_CACHE` in `watchlist.py` line 146 is an unbounded `dict[int, tuple[float, WatchlistQuotes]]`. Holds one entry per `watchlist_id` and is never evicted except by 5-second TTL check on read. Fine at current scale; bound with LRU if operator count grows.
- `BroadcastBus.publish()` acquires `_lock` to snapshot the queue set on every Kite tick frame (20–200 ticks/frame at MODE_LTP). At ≤5 SSE clients the lock hold-time is negligible.
- `_fetch_ltp` in `quote.py:82` is a sync function called directly from an async route handler at line 177 (`return _fetch_ltp(exchange, tradingsymbol)`). Makes a blocking `broker.quote()` HTTP call on the event loop without `asyncio.to_thread`. Used by OrderDepth polling at 1.2 s cadence and the command bar. Promote to P1 if this hits hot paths.
