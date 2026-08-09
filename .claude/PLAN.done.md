# Plan: docs: add §12 Sparkline Lifecycle to DATA_LIFECYCLE.md

## Context

DATA_LIFECYCLE.md (docs/audits/DATA_LIFECYCLE.md) was just created covering tick, positions,
holdings, NAV, NavStrip, and MarketPulse refresh. Sparklines are a distinct tri-layer
persistence system (snapshot write → ohlcv_store/intraday_store → frontend cache) that was
not covered. The operator needs to know when sparklines are written, what they contain, how
they're served closed-hours, and why the frontend shows stale bars.

## Task

Append **§12 — Sparkline Lifecycle** to `docs/audits/DATA_LIFECYCLE.md`.
Also update §11 (Cache Layer Reference table) to add the 4 sparkline-specific cache rows
that were missing: ohlcv_store tiers, intraday_store, frontend sparklinesStore, daily_book
sparkline rows.

Do NOT modify any other section.

## Agents

- doc: Edit `docs/audits/DATA_LIFECYCLE.md`. Two changes:

  **Change 1 — Append §12 at end of file:**

  ```
  ## §12 — Sparkline Lifecycle

  ### 12.1 Overview

  Sparklines are a tri-layer system:
  1. **Write path** — `snapshot_sparkline()` persists 5-day close-bar series to `daily_book`
     (kind='sparkline') on market close and close_settled events.
  2. **Store caches** — `ohlcv_store` (daily bars, 3-tier: memory→DB→broker) and
     `intraday_store` (30-min bars, 3-tier: memory 5min TTL→DB→broker) pre-warmed at startup
     and market-open boundaries.
  3. **Frontend cache** — `sparklinesStore` (1-day TTL, chunked 100 symbols/request, stale-
     while-valid).

  ### 12.2 Write Path

  **Trigger** — market_lifecycle_handlers.py:90:
  - `<exch>:close` event (NSE 15:30 IST, MCX 23:30 IST) → `snapshot_sparkline(settled=False)`
  - `<exch>:close_settled` event (~15 min later) → `snapshot_sparkline(settled=True)` re-upserts
    with broker's adjusted settlement close. Both write to `daily_book` (kind='sparkline').

  **Symbol universe** (daily_snapshot.py:945, cap 500):
  1. All watchlist_items (tradingsymbol + exchange)
  2. Live holdings (equity)
  3. Live positions (F&O + commodity)
  4. daily_book 7-day backstop (stale accounts)
  5. Mover universe (NSE indices + F&O largecap)
  Virtual MCX/CDS roots resolved to front-month contracts via `resolve_virtual_roots()`.

  **Bar series** — last 10 calendar days of daily closes, fetched from `ohlcv_store` via
  `get_or_fetch_daily()` (daily_snapshot.py:997). Converted to `[{"t": "YYYY-MM-DD", "ltp": float}]`
  oldest-first by `_snap_bars_to_points()` (daily_snapshot.py:926).

  **daily_book row shape** (kind='sparkline'):
  ```
  account:      "__firm__"   (market-wide sentinel, not per-account)
  symbol:       tradingsymbol
  exchange:     exchange
  ltp:          points[-1]["ltp"]  (latest close price)
  payload_json: {"points": [{"t": "YYYY-MM-DD", "ltp": float}, ...],
                 "settled": bool, "captured_at": <iso>}
  ```

  ### 12.3 Store Caches (Backend)

  **ohlcv_store** (backend/api/persistence/ohlcv_store.py):
  - Tier 1: in-process memory dict — no TTL (persistent until process restart)
  - Tier 2: PostgreSQL `ohlcv_daily` table — permanent
  - Tier 3: broker `historical_data()` on miss → write-back to Tier 1 only
  - Gap rule: gaps ≤ 6 calendar days treated as market closure (no re-fetch)

  **intraday_store** (backend/api/persistence/intraday_store.py):
  - Tier 1: in-process LRU dict — **5-min TTL for today's bars** (`_TODAY_TTL_S = 300`);
    historical bars have no TTL
  - Tier 2: PostgreSQL `intraday_bars` table — permanent
  - Tier 3: broker historical_data (round-robin via `get_historical_brokers()`)
  - Completeness: today's bars are partial during session; historical must span session-close hour

  ### 12.4 Sparkline Warm (Background Task)

  `_task_sparkline_warm()` (background.py:3224) fires at:
  - Service startup
  - 00:30 IST (midnight pre-warm)
  - 09:00 IST (MCX open)
  - 09:15 IST (NSE open)

  What it does:
  1. Builds symbol universe (same 5 sources as §12.2; book symbols always included, movers
     fill up to 300-symbol ceiling)
  2. Calls `warm_sparkline_cache(symbols, days=5)` (quote.py:1960) — parallel fetch of daily
     bars → ohlcv_store and 30-min intraday bars → intraday_store
  3. Seeds KiteTicker token-to-symbol map early (`_ticker_seed_early()`) so SSE ticks have
     valid `sym` before historical fetch completes
  4. Registers all universe tokens with `subscribe_with_sym()` for real-time tick coverage

  **300-symbol cap logic**: book_pairs (watchlist + holdings + positions + snapshot backstop)
  are never truncated; movers fill remaining slots up to 300 ceiling.

  ### 12.5 Serving Sparklines (/api/quotes/sparkline)

  **Route:** POST /api/quotes/sparkline (quote.py:1701)
  **Request:** `{"symbols": [{"tradingsymbol": str, "exchange": str}], "days": int}` (days: 1–90, default 5)
  **Response:** `{"data": {"SYM": [close1, ..., ltpTail], ...}, "refreshed_at": ISO, "as_of": ISO|null}`

  **Market open path** (4-stage pipeline):
  1. Fetch past daily closes (days-1) from ohlcv_store + today's 30-min intraday bars from
     intraday_store (parallel via `_fetch_bars_parallel()`)
  2. Build token map, subscribe to ticker
  3. LTP from ticker tick-map (zero Kite quota); fallback to broker.ltp() on miss
  4. Compose final series via `compose_sparkline_series()`; stale=False

  **Market closed path** (db_only=True when all exchanges closed):
  - Skip broker calls entirely (Tier 1+2 only)
  - Fallback to daily_book sparkline rows via `_fill_from_daily_book_sparkline()` (quote.py:1693)
  - `as_of` timestamp set; stale labeling shown to operator

  ### 12.6 Frontend Cache

  **sparklinesStore** (frontend/src/lib/data/marketDataStores.svelte.js:610):
  - TTL: 1 day
  - Chunked: 100 symbols per request
  - Stale-while-valid: keeps cached version on empty response (`keepStaleOnEmpty: true`)
  - Per-symbol merge: prefers fresh series if it has variation, else keeps cached (`_mergeSparkSeries()`)

  **Consumers** — MarketPulse.svelte: positions, holdings, movers, watchlist grids all display
  sparkline cells via AG-Grid.

  **Fetch trigger:** `sparklinesStore.load(pairs)` (MarketPulse.svelte:1295) on universe
  change; batched, not polled.

  **Data shape:** Array of finite numbers oldest→newest (length ≥ 2). Validated by
  `assertSparklineSeries()` (sparklineShape.js:38) in dev builds.

  ### 12.7 Sparkline Timing Table

  | Time (IST) | Event | Component | Operator impact |
  |---|---|---|---|
  | 00:30 | Midnight warm | _task_sparkline_warm | ohlcv_store + intraday_store pre-filled; universe tokens registered with ticker |
  | 09:00 | MCX open warm | _task_sparkline_warm | MCX commodity universe added to warm set |
  | 09:15 | NSE open warm | _task_sparkline_warm | NSE equity universe added; sparkline cache ready before first operator request |
  | 15:30 | NSE close | snapshot_sparkline(settled=False) | 5-day bar series written to daily_book; closed-hours sparklines now served from DB |
  | ~15:45 | NSE close_settled | snapshot_sparkline(settled=True) | Re-upsert with adjusted settlement close; settled=true flag set |
  | 23:30 | MCX close | snapshot_sparkline(settled=False) then settled=True | MCX commodity sparklines written |
  | On demand | Frontend grid load | sparklinesStore.load() → /api/quotes/sparkline | 100-symbol chunks, 1-day TTL cache |
  ```

  **Change 2 — Update §11 Cache Layer Reference table:**

  Add these 4 rows to the existing table (after the `intraday_store` and `ohlcv_store` rows
  if already present, or append at end):

  | `ohlcv_store` (daily bars) | Backend in-process memory + `ohlcv_daily` DB table | Tier 1: none; Tier 2: permanent | `warm_sparkline_cache()`, broker `historical_data()` on miss | `batch_sparkline()`, `snapshot_sparkline()` | Tier 1+2 served; no broker calls |
  | `intraday_store` (30-min bars) | Backend in-process LRU + `intraday_bars` DB table | Tier 1: 5 min (today only); Tier 2: permanent | `warm_sparkline_cache()`, broker `historical_data()` on miss | `batch_sparkline()` | Tier 1+2 served; today's bars may lag by ≤ 5 min |
  | `daily_book` sparkline rows | PostgreSQL `daily_book` (kind='sparkline') | Perpetual (idempotent upsert) | `snapshot_sparkline()` at close + close_settled | `/api/quotes/sparkline` closed-hours fallback | Primary fallback source for closed-hours sparklines |
  | `sparklinesStore` | Frontend in-memory (Svelte store) | 1 day | `/api/quotes/sparkline` response | MarketPulse.svelte sparkline cells | Stale-while-valid; per-symbol variation-based merge |

- backend: skip
- frontend: skip
- broker: skip
- backend-test: skip
- playwright: skip

## Tests

- pytest: no
- svelte-check: no
- playwright: no

## Commit message

docs: add §12 sparkline lifecycle to DATA_LIFECYCLE.md

## Done when

- §12 Sparkline Lifecycle appended to docs/audits/DATA_LIFECYCLE.md (7 subsections)
- §11 cache table updated with 4 sparkline-specific rows
- No other sections modified
