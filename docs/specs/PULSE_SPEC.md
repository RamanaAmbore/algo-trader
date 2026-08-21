# Pulse Page Specification

Single source of truth for the `/pulse` page behavior across all market states, user states,
and data sources. Code, tests, and documentation must stay in sync with this file.

**Version**: 1.10 — 2026-08-20  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/MarketPulse.svelte` · `frontend/src/lib/data/marketDataStores.svelte.js` · `frontend/src/lib/data/positionsDayPnlStore.svelte.js` · `backend/api/routes/quote.py` · `backend/api/routes/watchlist.py` · `backend/api/helpers/snapshot_gate.py` · `backend/api/algo/daily_snapshot.py` · `backend/api/routes/holdings.py`

---

## Contents

1. [Page Overview](#1-page-overview)
2. [Market State Matrix](#2-market-state-matrix)
3. [User State Matrix](#3-user-state-matrix)
4. [Section Specs](#4-section-specs)
5. [Data Source Ladder — DB-First Policy](#5-data-source-ladder--db-first-policy)
6. [Snapshot Preservation Requirements](#6-snapshot-preservation-requirements)
7. [Self-Healing Refresh Cycle](#7-self-healing-refresh-cycle)
8. [Demo Mode Rules](#8-demo-mode-rules)
9. [API Contract](#9-api-contract)
10. [Test Coverage Map](#10-test-coverage-map)
11. [Pulse Unified Pipeline](#11-pulse-unified-pipeline)
12. [Grid Buckets — Left Column](#12-grid-buckets--left-column)
13. [Grid Buckets — Right Column](#13-grid-buckets--right-column)
14. [Column Definitions](#14-column-definitions)
15. [Row Grouping (postSortGroups)](#15-row-grouping-postsortrgroups)
16. [LTP Tick Flash](#16-ltp-tick-flash)
17. [Derivatives P&L Formula (EXP)](#17-derivatives-pl-formula-exp)
18. [Sparklines](#18-sparklines)
19. [Symbol Context Menu](#19-symbol-context-menu)
20. [Watchlist Management](#20-watchlist-management)
21. [Account Multi-Select](#21-account-multi-select)
22. [Persistent Cache Layer](#22-persistent-cache-layer)
23. [Closed-Hours Snapshot Behavior](#23-closed-hours-snapshot-behavior)
24. [CardControls Cluster](#24-cardcontrols-cluster)
25. [Stale-While-Revalidate Bridge for DataStore Updates](#25-stale-while-revalidate-bridge-for-datastore-updates)
26. [Column Sort Now Respects User Click (postSortGroups Guard)](#26-column-sort-now-respects-user-click-postsortgroups-guard)
27. [Public Performance Page CardControls Color Override](#27-public-performance-page-cardcontrols-color-override)
28. [Known Defects](#28-known-defects)

---

## 1. Page Overview

MarketPulse is a two-panel grid:
- **Left panel**: Watchlists (Pinned + custom) + Movers (Winners / Losers tabs)
- **Right panel**: Positions + Holdings

Every row carries: Symbol · 5d sparkline · LTP · Avg · Day% · Close · Qty · Day P&L · P&L% · P&L.

The page is **always populated** — no blank grids, no "—" placeholders. Closed hours show the last snapshot with a staleness hint. Empty is a defect.

---

## 2. Market State Matrix

| State | Condition (IST) | NSE data | MCX data |
|---|---|---|---|
| **S1 — Both open** | 09:15–15:30 weekday | Live broker | Live broker |
| **S2 — MCX only** | 15:30–23:30 weekday | Snapshot | Live broker |
| **S3 — Both closed** | 23:30–09:00 + weekends | Snapshot | Snapshot |
| **S4 — Pre-open** | 09:00–09:15 weekday | Snapshot | Live broker |

`closed_hours_or_broker()` source tags: `'live'` · `'snapshot'` · `'snapshot-fallback'` · `'stale-live'`

`_any_segment_open()` fails open (returns `True` on exception).

---

## 3. User State Matrix

| State | Condition | Watchlist | Movers | Mutations |
|---|---|---|---|---|
| **U1 — Authenticated** | JWT present | Own lists | Live or snapshot | All allowed |
| **U2 — Demo (anon)** | No JWT | id=-1 pinned (23 demo symbols) | From `movers_snapshots` | All blocked |

`isDemo = $derived(!$authStore.user)` — reactive, no bridge needed.

---

## 4. Section Specs

### 4.0 Order Entry — CardHeader Layout

The order-entry ticket (LIMIT, MARKET, SL orders) relocates chase toggle and
aggressiveness picker from order body to CardHeader middle zone.

**LIMIT / SL orders**:
- Header left: symbol (read-only)
- Header middle: CHASE toggle + aggressiveness pills (L/M/H)
- Header right: refresh + close buttons
- Body area: live LTP display (from depth WebSocket)

**MARKET orders**:
- Header left: symbol
- Header middle: empty (chase not applicable)
- Header right: refresh + close buttons
- Body: live LTP display

**Rationale**: High-frequency CHASE workflows benefit from one-click toggle placement in 
the header; L/M/H aggressiveness picker selection moves alongside for rapid execution mode 
changes. LTP display relocated to body preserves real-estate balance.

### 4.1 Watchlist

- Authenticated: own lists from DB, LTP via SSE, sparklines polled on tick cadence
- Demo: `GET /api/watchlist/-1` → 23 items from `MARKETS_DEFAULT`
- Empty pinned message: authenticated → "add a symbol via the + button"; demo → "Sign in to add symbols"

### 4.2 Movers

- Open: live broker quotes via `_movers_fetch_quotes_cached()` (30s TTL), NSE rows persisted to `movers_snapshots`
- Closed: `_movers_offhours_response()` → latest `movers_snapshots` row, `captured_at` in response
- Frontend shows "Last updated: `<time>`" in Winners/Losers header when `moversSnapshotAt` is non-null
- Demo: same endpoint as authenticated (movers are not behind a real-account call)
- Demo guard: `get_movers` must check `request.state.is_demo` and return `_movers_offhours_response()` for anonymous

### 4.3 Sparklines

See Section 5 for DB-first policy and fallback ladder.

**Composition** (`compose_sparkline_series(past, today_bars, ltp_val, market_closed)`):

| Inputs | Closed | Series | Reason |
|---|---|---|---|
| past≥1, today or ltp>0 | False | past+today+[ltp] | `live` |
| past≥1, today or ltp>0 | True | past+today+[ltp] | `snapshot` |
| past≥1, ltp=None | True | past+today | `snapshot` |
| ltp>0 only | either | [ltp, ltp] | `ltp_only_flat_pad` |
| single point | either | point+point | `single_point_pad` |
| empty | True | [] | `warm_universe_empty` |
| empty | False | [] | `historical_fetch_fail` |
| ltp≤0 | either | treated as None | — |

**Merge** (`_mergeSparkSeries(cached, fresh)`): cached-with-variation beats flat fresh. `_hasVariation` requires ≥2 differing values.

**Animation**: `is_animating` and `price_source` MUST be copied from API response into row objects (`moversStore.fetcher`, `_publishWatchQuotes`). `_propagateStaleAndSource` called in `mergeMoverRows` and `mergeWatchlistRows`. Rows on a closed exchange must NOT animate.

**Snapshot persistence**:
- `snapshot_sparkline(settled=False)` at `<exch>:close`
- `snapshot_sparkline(settled=True)` overwrites at `<exch>:close_settled` (~15 min later)
- Payload: `{"points": [{"t": "<date>", "ltp": <float>}], "settled": <bool>, "captured_at": "<iso>"}`
- Universe cap: 500 symbols (watchlist + positions + holdings + movers)

### 4.4 Positions & Holdings

- Open: live from broker via `closed_hours_or_broker()` → `'live'`
- Closed: from `daily_book` snapshot → `'snapshot'` with `as_of` timestamp; uses refined filter `AND (qty != 0 OR date = today_ist)` (commits cef00739, 5ac11f56): closed positions from today's IST date appear in snapshot (shown with 'closed' chip + opacity decoration in derivatives legs grid); prior-session closed positions excluded (date != today_ist), only carried-overnight open positions visible

**PositionRow schema (additions Aug 2026)**:
- `previous_close: float = 0.0` — frozen prior-session settlement price from `COALESCE(daily_book.previous_close, daily_book.ltp)` written by `_override_stale_close_from_snapshot()`. Frontend prefers this (official settlement, write-once) over `close_price` (Kite's mutable field) for day P&L computation via `(ltp − previous_close) × qty`. Matches holdings fix (75a335f7); prevents positions day P&L zeroing at NSE settlement
- `is_orphan: bool` — True when no open AlgoOrder (status=OPEN) matches this position's (account, tradingsymbol). Positions without a parent order are considered orphaned; shown with coral "O" badge in MarketPulse grid
- `pair_group_key: str | None` — shared root AlgoOrder ID for positions linked via parent-child relationship. When two orders are paired via `POST /api/orders/pair`, child rows share the same `pair_group_key` as the parent. Null when no AlgoOrder matches this position. Used by `postSortRows` callback to keep paired positions (parent + child) adjacent in sort order regardless of column sort direction

**Day P&L computation in snapshot mode** (`_positions_snapshot()` and `_build_holding_row_from_snapshot()`):
- Snapshot readers recompute `day_change_val` from prior-session EOD reference to ensure correct day P&L after market settlement
- **Positions** (Aug 2026 `_override_stale_close_from_snapshot` fix): `_override_stale_close_from_snapshot()` now queries `COALESCE(daily_book.previous_close, daily_book.ltp)` as the reference close price and writes `previous_close` to ALL matched position rows (matching holdings fix 75a335f7). Frontend `positionsDayPnlStore`, `pulseUnified`, and `nav.js` now prefer `p.previous_close` (frozen official settlement) over `p.close_price` (Kite's mutable field). For snapshot readers in closed-hours mode, `prev_batch` CTE filters `AND db.ltp IS NOT NULL AND db.ltp > 0 AND db.captured_at < :today_ist_midnight` within a 7-day lookback window to handle multi-day holiday gaps; `day_change_val` recompute uses `(ltp − previous_close) × qty` when `previous_close` > 0 (official settlement); falls back to `(ltp − prev_ltp) × qty` when unavailable
- **Holdings** (Aug 2026): `_HOLDINGS_SNAPSHOT_SQL` now includes a `prev_batch` CTE (same pattern as positions) that finds the most-recent prior-day LTP per (account, symbol) within a 7-day lookback. `_build_holding_row_from_snapshot()` uses `(ltp - prev_ltp) × qty` when `prev_ltp > 0`, matching the positions closed-hours pattern. Holdings day P&L during closed hours is now computed from an actual price diff, not a stale stored value. Fallback to `(ltp - previous_close) × qty` when `prev_ltp` is unavailable/zero, consistent with live open-hours formula
- Day P&L: always via `baseDayPnlForPosition(p)` — NEVER read `day_change_val` directly. Formula applies canonical fast-path (`day_change_val` guard) for ALL overnight positions (both longs and shorts). Short position guard corrected in commit 1769cffc: overnight quantity check is now `oq !== 0` (not `oq > 0`), ensuring short MCX positions receive the stale-close guard during the 23:30–09:00 IST window and avoiding catastrophic ₹5,00,000+ overstatement in day P&L.
- **Flat row hygiene fix (commit ed63b9fe)**: `_apply_flat_row_hygiene` now zeros `day_change_val` ONLY for pure intraday round-trips — rows where `(quantity == 0) AND (overnight_quantity == 0)`. Closed overnight futures (qty=0, oq>0) now retain their correct backstop `day_change_val = pnl` from `apply_day_change_backstop` (Case 3), fixing missing day P&L on closed overnight F&O positions. Previous overly-broad mask `(quantity == 0)` was undoing backstop results for all flat rows including closed overnight legs.
- Do NOT use `positions.close_price` (stale overnight); use `daily_book.ltp`
- NavStrip P-slot is guarded against zero-flash during live→snapshot transitions
  (when `close_price === ltp`, the guard returns 0 to prevent distortion)
- Orphan cleanup: after-hours snapshots run `_delete_orphan_positions()` on same-day + `_delete_prior_orphan_positions()` on prior-day to remove settled/closed positions (7-day scope)

**Derivatives Legs Grid Candidate Building** (commit 9becba9f):
- F&O positions: skipped if instrument not found in master (expired contract removed by Kite) — prevents stale expired legs in grid
- Equity holdings: skipped if qty=0 (no zero-quantity holdings in leg candidates)
- Proxy-hedge holdings: skipped if qty=0 (no zero-quantity proxy legs in grid)
- Draft positions: skipped if instrument not found in master (no fallback expiry guard for drafts without master entry)

**Broker-specific Day P&L notes**:
- **Groww**: `_normalise_positions()` in `groww.py` pre-computes `day_change_val = (ltp - close) × qty` (guarded: `ltp > 0 and close > 0 and qty != 0`). Groww API does not return `day_change_val` or `day_pnl`, so this computation ensures Day P&L displays correctly on cold-cache page loads instead of showing 0
- **Holdings live-path**: `_override_stale_close_for_holdings` now queries `COALESCE(daily_book.previous_close, daily_book.ltp)` and writes `previous_close` to all holding rows. Frontend prefers `previous_close` (frozen settlement) over `close_price` (Kite's mutable field). `apply_day_change_backstop()` now runs for holdings to handle NSE settlement case where `close_price == ltp` and stored `day_change_val` stales to 0, fixing H slot zeroing after settlement

### 4.5 Chase Card Status Columns

Active chase rows now display enhanced visual feedback for execution state.

**Symbol column**:
- Pulsing colored dot adjacent to symbol name (green for BUY, red for SELL)
- Dot animation syncs with order lifecycle (stops when chase completes or cancels)

**Age column**:
- Now visible in all layout modes (previously compact-mode only)
- Shows elapsed time since chase order entered the queue

**Countdown timer**:
- Opacity increased for improved legibility (was previously dim)
- Millisecond countdown clearly visible during active execution

**All other columns** (symbol, qty, price, status) — unchanged

### 4.5.1 Cancel Reconciliation — Derivatives Page

When an order is cancelled or rejected via WebSocket `order_update` message, the 
`order_update` handler now immediately calls `loadPositions({ fresh: true })` to refresh 
the Legs grid with the updated broker state.

**Behavior**:
- Status is `CANCELLED` or `REJECTED` → trigger immediate `fresh: true` refresh
- Previously: handler returned without refreshing, leaving stale leg quantities until 
  next poll interval (5–30 seconds)
- Now: users see leg qty update on the same tick the cancel confirmation arrives

**Impact**: Eliminates race conditions where an operator cancels a close order and 
immediately submits a new one; the Legs grid no longer shows stale qty from the cancelled 
order.

### 4.6 Card Controls — CSV Export Button

All ag-Grid cards on `/pulse` and related admin surfaces include a Download button for
immediate CSV export of the current grid view (filtered, sorted).

**Position in CardControls cluster**: Search · **Download** · Collapse · DefaultSize · Fullscreen

**Cards that include Download**:
- Watchlist + Movers sections: Pinned, Winners, Losers (all watch-type grids)
- Positions and Holdings (account grids)
- Derivatives Legs grid (options/futures legs)
- Orders grid (current/filled orders)
- Automation Templates grid (saved agent rules)
- Automation Activity grid (agent execution log)

**Cards that do NOT include Download**:
- Chart cards (no ag-Grid present)
- News / Summary cards (static content, not tabular)

**Behavior**: Clicking the Download icon immediately triggers `onDownload` callback in
`CardControls.svelte`, which calls ag-Grid's export API with the current filter/sort state.

**File naming convention**:
- Pinned/custom watchlist: `watchlist.csv`
- Winners: `winners.csv`
- Losers: `losers.csv`
- Positions: `positions.csv`
- Holdings: `holdings.csv`
- Derivatives legs: `legs.csv`
- Orders: `orders.csv`
- Automation templates: `templates.csv`
- Automation activity: `activity.csv`

**Component**: `GridDownloadButton.svelte` (reusable). Accepts `onDownload` callback prop
passed from `CardControls.svelte` to parent ag-Grid wrapper.

### 4.7 Option Chain Tab — Buy/Sell Button Layout

Strike-adjacency rule: Buy (+) and Sell (−) buttons always cluster toward the strike 
column for optimal scanning.

**Call (CE) side** (left of strike):
- Buttons cluster at the right edge of the cell (immediately adjacent to strike)
- Quote columns (bid/ask/LTP) at outer left
- Button alignment: `justify-content: flex-end`

**Put (PE) side** (right of strike):
- Buttons cluster at the left edge of the cell (immediately adjacent to strike)
- Quote columns at outer right
- Button alignment: `justify-content: flex-start`

**Rationale**: Operators scanning the chain quickly locate buy/sell actions in the same 
visual line as the strike price, eliminating eye travel to distant button zones.

**Quote fetch timeout** (`OptionChainTab.svelte`):
- `chain_quotes` API calls wrapped in `asyncio.wait_for(timeout=10.0)` to prevent hangs
- Off-market gate: when markets closed, returns empty `rows: []` with populated `expiries`
- No broker quote call made during closed hours
- Expiry fetch includes retry logic (5s × 12 attempts) if initial fetch fails
- `if (!chainExpiry) return;` guard prevents quote effect from firing when expiry unset

---

## 5. Data Source Ladder — DB-First Policy

**Requirement**: For sparklines (pinned, positions, holdings, movers), prefer persisted data over broker calls. Yesterday's data in `daily_book` or `ohlcv_daily` is valid and must be used before making a broker network call.

### Sparkline fetch ladder (`batch_sparkline`):

```
1. ohlcv_daily (DB cache, db_only=True)           ← always try first
       If non-empty → use it
       If empty ↓
2. daily_book kind='sparkline' (Tier 4 fallback)   ← use yesterday's snapshot
       If non-empty → use it (even if 1 day old)
       If empty ↓
3. ohlcv_store broker fallback (db_only=False)     ← Kite historical_data REST
       Always use get_historical_brokers()[0] (Kite, not Dhan)
       If non-empty → write-back to ohlcv_daily
       If empty ↓
4. _self_heal_empty_bars (bypass_cache, not in cooloff) ← last resort broker
       If empty → compose with ltp_only_flat_pad or historical_fetch_fail
```

**Rationale**: Broker calls are rate-limited and slow. DB data from yesterday is valid for sparkline shape — the curve from 2 days ago + today's intraday is better than a flat `[ltp, ltp]` line. Tier 4 (`daily_book kind='sparkline'`) must be tried BEFORE broker fallback.

### Quote/LTP ladder:

```
Open hours:  SSE tick → broker.ltp() → _LAST_GOOD_LTP (1h TTL)
Closed hours: daily_book.ltp → _LAST_GOOD_QUOTE (24h TTL) → [empty]
```

---

## 6. Snapshot Preservation Requirements

Close and settlement snapshots are the only source of truth during closed hours. They must be written reliably and survive across restarts.

### Write requirements:

1. **Idempotent UPSERT**: `daily_book` uses `ON CONFLICT (date, account, kind, symbol) DO UPDATE`. Running close snapshot twice produces the same row.

2. **Settled flag sequence**: `_snapshot_close` computes `settled = (event_type == "close_settled")` ONCE above both try blocks. `snapshot_daily_book(settled=settled)` and `snapshot_sparkline(settled=settled)` receive the same value.

3. **Close then settled**: Both `<exch>:close` (settled=False) and `<exch>:close_settled` (settled=True, 15 min later) fire for every exchange (NSE, MCX, CDS). The settled row OVERWRITES the initial close row via UPSERT.

4. **`DISTINCT ON` ordering**: Tier 4 query must be `DISTINCT ON (symbol) ORDER BY symbol, date DESC, settled DESC NULLS LAST` so settled rows win over unsettled same-date rows.

5. **Persistence must not be skipped**: `_snapshot_close` wraps `snapshot_daily_book` and `snapshot_sparkline` in separate try blocks that swallow exceptions — so one failing does not prevent the other. Both blocks MUST exist.

6. **Exchange isolation**: NSE close does not affect MCX snapshot and vice versa. Each exchange fires its own close event.

7. **Movers snapshot on NSE close**: `_force_movers_snapshot` fires on `nse:close` ONLY (not MCX/CDS). MCX close must NOT overwrite NSE movers.

### Read requirements:

1. During closed hours, all routes must serve daily_book snapshots — never attempt a live broker call.
2. `as_of` timestamp must be included in every snapshot response so the frontend can show staleness.
3. Anti-flicker cache (`_stale_live`) serves up to 120s when broker fails mid-session (not applicable to closed hours).

---

## 7. Self-Healing Refresh Cycle

The system must recover from any single-tier failure without operator intervention.

**Expiry-day auto-close agent** (commit cbbe0f23): `expiry-day-equity-itm-auto-close` and 
`expiry-day-commodity-itm-auto-close` agents are now **active** by default. These agents 
re-scan for newly-ITM expiring positions every 30 minutes (until 15:25 IST), enabling 
intraday ITM close automation when positions tick ITM after initial close snapshot.

### Failure modes and expected recovery:

| Failure | Recovery |
|---|---|
| ohlcv_daily DB empty (cold start) | Tier 4 daily_book → broker fallback → write-back to ohlcv_daily |
| Broker rate-limited (cooloff) | Serve from ohlcv_daily DB or daily_book snapshot; retry after cooloff |
| Broker returns [] for Dhan (wrong selector) | `get_historical_brokers()[0]` always Kite — Dhan never selected |
| daily_book snapshot missing | _self_heal_empty_bars + ltp_only_flat_pad baseline |
| close snapshot write failed (one exchange) | Next close event retries (idempotent UPSERT) |
| settlement write failed | close_settled re-fires are idempotent; manually triggerable |
| Backfill script outside main API | Write queue workers must be started + drained explicitly |
| KiteTicker stale (30s watchdog) | Auto-failover to next Kite account with 5-min cooloff |
| conn_service restart | Main API reads mmap directly; ticker auto-reconnects |

### Refresh cadence (MarketPulse timer rationalization):

MarketPulse reduced from 7 distinct cadences to 4:

| Cadence | Task | Interval |
|---|---|---|
| Book poller | Positions/Holdings refresh | 5s |
| Quotes + movers + sparklines | Market data, top % change | 30s |
| Settings audit | Capability-flag reconciliation | 60s |
| Tick-driven | SSE updates, LTP flash | Sub-second |

### Warm task schedule (self-healing checkpoints):

| Task | Schedule |
|---|---|
| `_task_sparkline_warm` | Startup, 00:30 IST, segment opens |
| `warm_sparkline_cache` | Startup, 08:30 IST |
| `backfill_ohlcv_daily` | Via `POST /api/admin/persistence/backfill` (not standalone script) |
| Ticker universe registration | Startup + segment opens + daily_book union (past 7d) |

### Backfill invariant:

`scripts/backfill_ohlcv.py` MUST call `write_queue.start()` before any fetch, then `await write_queue.drain()` before exit. Running the script standalone without starting the write queue drops all writes silently.

---

## 8. Demo Mode Rules

`isDemo = $derived(!$authStore.user)` — true for anonymous users.

**Always visible**: Pinned (23 demo symbols), Winners/Losers, sparklines, LTPs (same SSE), movers.

**Always hidden** (`{#if !isDemo}`):
- Manage (pencil) button
- "New watchlist" dropdown option  
- Add symbol section
- Per-row × (remove): `_canRemoveHere = ... && !isDemo`
- ↑↓ reorder buttons
- Rename/Delete watchlist buttons
- Context menu "Add to watchlist" and "Remove from watchlist"
- `/` keyboard shortcut

**Backend auth rules** (server-enforced, 401):
- `POST/DELETE/PATCH /api/watchlist/*` — anonymous → 401
- `get_movers` — anonymous → `_movers_offhours_response()` (NOT live broker)

`auth_or_demo_guard` works on both dev and prod branches (no `is_prod_branch()` gate).

---

## 9. API Contract

| Endpoint | Auth | Open | Closed | Demo |
|---|---|---|---|---|
| `GET /api/watchlist/` | Optional | Own lists | Own lists | id=-1 only |
| `GET /api/watchlist/-1` | None | 23 demo symbols | same | same |
| `GET /api/watchlist/movers` | Optional | Live broker | movers_snapshots | movers_snapshots |
| `GET /api/quote/batch-sparkline` | Optional | DB-first ladder | DB-first + daily_book | same |
| `GET /api/positions` | Required | Live broker | daily_book snapshot | N/A |
| `GET /api/holdings` | Required | Live broker | daily_book snapshot | N/A |

**Sparkline response shape**:
```json
{"symbol": "RELIANCE", "exchange": "NSE", "series": [100.0, ...], "reason": "live|snapshot|...", "ltp": 2850.5, "as_of": null}
```
`as_of` is null during live hours; ISO-8601 UTC string for snapshots.

---

## 10. Test Coverage Map

### Backend — covered:
- `test_sparkline_snapshot.py` — snapshot_sparkline (8 tests + 4 new settled/MCX/CDS)
- `test_compose_sparkline_series.py` — compose ladder (14 tests)
- `test_sparkline_closed_hours.py` — Tier 4 fallback (5+ tests)
- `test_batch_sparkline_boundaries.py` — cap, clamp, dual-write (6 tests)
- `test_demo_mode_api.py` — anon read/write guard (18+ tests)
- `test_per_exchange_snapshot_handlers.py` — movers lifecycle, settled sequence

### Backend — gaps:
- DB-first ladder: Tier 4 checked BEFORE broker fallback (not after)
- `batch_quote` closed-hours path end-to-end
- `_serve_closed_hours_batch` with `as_of` timestamp
- GrowwBroker all four `_retry_groww_auth` branches

### Frontend — covered:
- `sparkline.spec.js` — `_mergeSparkSeries`, `_hasVariation`, visual rendering
- `demo_watchlist_guard.spec.js` — mutation guards + positive data assertions
- `pulse_movers_snapshot_timestamp.spec.js` — "as of" timestamp

### Frontend — gaps:
- `is_animating=false` suppresses animation on closed-exchange rows (D3/D4 regression)
- Context menu guard active for anonymous users (D1 regression)
- LTP tail updates live during market open

---

## 11. Pulse Unified Pipeline

The `buildUnified()` compositor (in `pulseUnified.js`) merges positions, holdings, watchlist symbols, 
option underlyings, and movers into a single row array. Every row carries account info, market data 
(LTP, day P&L), and source badges so the same symbol never appears twice across different majors.

**Pipeline stages** (in order):
1. **Watchlist rows** — pinned + user-created lists → `major='pinned'` or `major='watchlist'`
2. **Position rows** — live + intraday-closed; multi-account aggregate; day P&L recompute
3. **Holding rows** — overnight + long-term; same account aggregation; cost basis tracking
4. **Option-underlying anchors** — NFO/MCX/CDS roots; keyed by logical underlying name for Greeks
5. **Mover rows** — top winners/losers; badges existing rows; standalone movers-major rows
6. **Index tag pass** — watched indices (NIFTY 50 → NIFTY) retag `underlying` field for sort grouping
7. **Finalize** — weighted averages, combined avg, day %, directional (position tint), account color
8. **Sort** — major bucket → group order → localeCompare → tier → strike → CE before PE

**Bucket sort order** (controls visible grouping):
- Bucket 1 (pinned): indices + commodities + USDINR — always visible, operator-curated
- Bucket 2 (watchlist): user-created lists; each is a separate tab in the UI
- Bucket 3 (positions): live intraday + overnight positions; account-scoped via multi-select
- Bucket 4 (holdings): long-term holdings; separate account-scoped filter
- Bucket 5 (movers): daily top % movers (winners + losers); gated by market segment open state

**Row shape** (unified rows dict):
```
{
  key: "RELIANCE__pos",                    # unique key = symbol + major suffix
  _majorGroup: "positions",                # pinned|watchlist|positions|holdings|movers
  _majorOrder: 2,                          # numeric sort-order
  tradingsymbol: "RELIANCE",               # uppercase NSE/NFO/MCX symbol
  exchange: "NSE",                         # NSE|BSE|NFO|MCX|CDS|…
  underlying: "RELIANCE",                  # for options; null for spot/cash
  kind: "eq",                              # spot|eq|fut|opt
  strike: null,                            # F&O option strike
  opt_type: "CE" | "PE" | null,           # option type
  expiry: "25JUL2026",                     # option/future expiry (Kite format)
  src: {w: false, h: false, p: true, u: false, m: false},  # source badges (watchlist|holdings|positions|underlying|movers)
  ltp: 2850.5,                             # latest price (SSE or polled)
  close: 2847.0,                           # previous session close
  open: 2842.0,                            # today's opening price
  high: 2865.0, low: 2840.0,              # today's range
  volume: 45_000_000,                      # intraday volume
  oi: null,                                # open interest (F&O)
  bid: 2850.25, ask: 2850.75,             # live bid/ask
  change: 3.5,                             # ltp - close
  change_pct: 0.123,                       # (ltp - close) / close × 100
  day_pct: 0.123,                          # qty-weighted day % directional
  qty_pos: 100,                            # net position quantity
  qty_hold: 50,                            # holdings quantity
  avg_pos: 2800.0,                         # position entry average
  avg_hold: 2795.0,                        # holdings entry average
  avg_combined: 2798.33,                   # weighted average (pos+hold)
  pnl: 5250.0,                             # lifetime P&L
  day_pnl: 150.0,                          # today's P&L
  accounts: Set(["ZG0790", "ZJ6294"]),    # multi-account position (aggregated)
  _acctColor: "#4f46e5",                   # display color for lead account
  is_animating: true,                      # SSE live (false = snapshot)
  price_source: "live",                    # live|snapshot_settled|snapshot_unsettled
}
```

**Market-open gate** — LTP and day P&L depend on `isMarketOpen()`:
- Open: live SSE tick + broker poll LTP used; day P&L via `livePositionDayPnl()` helper
- Closed: `daily_book` snapshot LTP + zero day P&L (no intraday MTM)

**Throttle** — `_throttledTick` 4 Hz (250ms) max; SSE ticks can fire 100/sec under load

### 11.1 Position Day P&L SSOT (`positionsDayPnlStore`)

Module-level singleton in `frontend/src/lib/data/positionsDayPnlStore.svelte.js` is the 
canonical source of truth for live position day P&L across all Pulse surfaces. It exports 
`{ total, byKey }` where:
- `total` — sum of all position day P&L (₹ value, real-time)
- `byKey` — symbol-to-day_pnl map, keyed by plain uppercase tradingsymbol (no exchange prefix)

**Update direction — Pulse is the authoritative writer**:
- During market-open hours, `buildUnified()` writes accurate position day P&L values via 
  `mergePositionRows()` (cq-computed, not broker-cached). These values flow directly into 
  `unifiedRows` grid.
- MarketPulse component aggregates position rows' `day_pnl` from `unifiedRows` (filtered by 
  `r._majorGroup === 'positions'`) in a `$effect` that runs after each `unifiedRows` update.
- Aggregation calls `positionsDayPnlStore.setFromPulse(pulseByKey, pulseTotal)` to write the 
  canonical totals back to the store.
- This **inverts the data flow**: Pulse computes → store captures, not store overrides → Pulse 
  displays.

**Consumers**:
- **PositionStrip P pill** (nav): reads `store.total` for hero nav badge
- **Dashboard hero** (if applicable): reads `store.total` for quick-scan P&L

**Rationale**: Pulse's `mergePositionRows` produces accurate cq-computed day P&L values 
during market-open hours. Writing these back to the store ensures all surfaces (nav, 
dashboard) read the same authoritative calculation. Decoupling from grid renders prevents 
stale re-renders and eliminates the regression where stale broker-cached data would override 
Pulse's accurate in-memory values.

---

## 12. Grid Buckets — Left Column

Three distinct left-panel grids (Pinned, Watchlist, Movers Winners/Losers) share the same 
column definitions but render different row subsets filtered from `unifiedRows`.

**Pinned grid** (`gridPinned`):
- Rows: `_majorGroup === 'pinned'` (operator-curated 23-symbol default for demo)
- Tab: always shown; cannot be hidden or toggled off
- Add/remove: pencil button (admin/designated only) opens inline editor
- Watchlist-item link persisted to DB for future pin-set customization

**Watchlist tabs** (dynamic):
- One tab per activated watchlist + the "Pinned" pinned-list tab
- Rows: symbols in the currently-active watchlist list (selected via tab click)
- Show/hide: controlled via unified "Show" MultiSelect (`selectedShow` array)
- Add symbol: inline SymbolSearchInput + type-picker → `addWatchlistItem()` → `activeListsStore` refresh
- Remove: row × button (left-click to remove); confirmation guard for delete-list operations
- Reorder: drag-and-drop or up/down buttons persisted to `watchlist_items.display_order`

**Movers grid** (Winners / Losers tabs):
- Rows: `_majorGroup === 'movers'` filtered by `_moverDirection === 'winners'` or `'losers'`
- Source: `/api/watchlist/movers` endpoint; 30s TTL during open hours; persisted snapshot during closed
- Headers show "Last updated: <time>" when `moversSnapshotAt` is non-null (closed-hours snapshot)
- Sticky flag: some movers kept visible across refreshes (operator-marked in backend)
- Segment-aware: NSE open → NSE equity movers; NSE closed + MCX open → MCX movers; both closed → NSE snapshot

**Movers gate** (market-segment-aware):
- State S1 (both open): NSE equity movers displayed
- State S2 (MCX only): MCX commodity movers displayed
- State S3 (both closed): NSE movers snapshot (from last S1 close)
- State S4 (pre-open): NSE snapshot (from prior day)

All left-grid columns use `mkLeftColDefs()`:
- Symbol (168px, pinned left) — MCX/CDS virtual label (CRUDEOIL not CRUDEOIL26JUNFUT)
- 5d sparkline (44px) — mini SVG price curve; blank if broker rate-limited
- LTP (77px) — live SSE + tick-flash; snapshot frozen during closed hours
- Day % (64px) — raw symbol change % (no qty weighting); directional (green/red)
- Close (68px) — previous session EOD price (muted)
- Open (68px) — today's opening price (muted)
- Volume (58px) — intraday volume; compacted format (e.g. "45.6M")
- OI (58px) — open interest for F&O; compacted

---

## 13. Grid Buckets — Right Column

Two distinct right-panel grids (Positions, Holdings) show live account positions and holdings 
with account-scoped filters and a pinned TOTAL row at bottom.

**Positions grid** (`gridPositions`):
- Rows: `_majorGroup === 'positions'` + `_includesPosAcct(account)` filter
- Live: broker + SSE delta during market hours; `daily_book` snapshot when closed
- Account filter: MultiSelect on `positionsAccounts` (empty = all); persisted to sessionStorage
- TOTAL row: pinned bottom, shows sum of filtered positions + live F&O-only expiry value
- Columns: Symbol (right-aligned account tint) · St · 5d · LTP · Avg · Day % · Close · P&L · P&L % · Day P&L · Qty · Lots · Account

**Holdings grid** (`gridHoldings`):
- Rows: `_majorGroup === 'holdings'` + `_includesHoldAcct(account)` filter
- Source: broker holdings + daily_book snapshot; LTP never intraday-split
- Account filter: separate MultiSelect on `holdingsAccounts`; persisted independently
- TOTAL row: sum of filtered holdings (cost basis + current value)
- Columns: Symbol (account tint) · 5d · LTP · Avg · Day % · Close · Day P&L · P&L % · P&L · Qty · Lots (immediately before Invested) · Invested · Value · Account
- **Note**: St (pos_state) column is filtered out of holdings grid; only visible in positions grid

**Right-grid column order** (via `mkRightColDefs()`):

Positions grid:
- Symbol (168px, pinned) — account-tinted background (color per lead account)
- **St** (30px) — pos_state indicator; displays '○' for all position rows with `quantity` defined;
  fallback rendering for unenriched rows. Not shown in holdings grid.
- 5d sparkline (44px)
- Lots (52px) — qty in F&O lot units; via `lotsForRow()` helper; null hidden
- LTP (77px) — SSE tick + vs-avg/vs-prev heat; snapshot-frozen when is_animating=false
- Avg (68px) — weighted average entry (directional tint: long green, short red, flat gray)
- Day P&L (78px) — today's profit/loss; tick-flash on poll cycles (300ms)
- Close (68px) — previous close (muted)
- P&L (78px) — lifetime profit/loss; directional + tick-flash
- P&L % (64px) — P&L as % of cost basis
- Qty (56px) — net qty; aggregated across accounts; null hidden
- Account (86px) — lead account + "+N" for multi-account rows; STALE@HH:MM badge on circuit-breaker rows
- Exp P&L (78px, F&O only) — expected P&L at expiry for open option legs

Holdings grid (St column filtered out):
- Symbol (168px, pinned) — account-tinted background
- 5d sparkline (44px)
- LTP (77px)
- Avg (68px)
- Day % (64px)
- Close (68px)
- Day P&L (78px)
- P&L % (64px)
- P&L (78px)
- Qty (56px)
- **Lots** (52px, immediately before Invested) — qty in F&O lot units; via `lotsForRow()` helper.
  Positioned immediately before Invested column for better UX flow (contract quantity → invested basis).
- Invested (78px) — cost basis (avg × held qty)
- Value (78px) — current value (LTP × held qty)
- Account (86px)

**Day P&L recompute** — via `livePositionDayPnl()` helper (shared with derivatives):
- When market open: `(liveLtp − closePx) × qty + realisedToday`
- When market closed: `baseDayPnlForPosition(row)` (broker day_change_val or lifetime pnl if missing)
- MCX stale-ticker rescue: when broker LTP ≈ close_price (KiteTicker lag), use SSE live tick if available
- **Derivatives candidates SSE delta fix (commit ed63b9fe)**: `candidatesDayPnl` in derivatives overlay now gates the live-tick delta `(liveLtp − pollLtp) × qty` to only apply when `_dayPnlForLeg` fell back to baseline (i.e., `oq=0` or no valid SSE ltp or `close=0` or `qty=0`). Previously, delta was unconditionally added, causing double-counting of intraday moves for overnight legs where `_dayPnlForLeg` already returned the full `(liveLtp − close) × qty` from SSE data.

**TOTAL row** (pinned bottom):
- Positions: sums all filtered position rows; F&O-only expiry value appended to P pill slot 3
- Holdings: sums all filtered holdings; day P&L = sum of per-row holdings day change
- Styling: amber background (22% opacity) + borders to distinguish from data rows
- P&L %-cell formula: `day_pnl / (close × qty)` per symbol, market-value-weighted for TOTAL

---

## 14. Column Definitions

Every ag-Grid column uses a factory function (e.g. `mkLtpCol`, `mkPrevCol`) that accepts 
accessor functions for reactive state. Factories are called once at grid mount; closures capture 
the accessors so cells see current $state values on every redraw (not stale bindings).

**Value formatters** (pure, no leading +, no ₹ prefix, right-aligned for numerics):
- `numFmt`: price-precision format (2 decimals); null → "—"
- `aggFmtGrid`: compact format for large numbers (45.6M, 150K); null → "—"
- `pctFmtGrid`: percentage with % suffix (12.45%); null → "—"
- `qtyFmt`: quantity format (no decimals for whole shares); null → "—"
- `fmtLots`: lot format (F&O contract units); null → "—"

**Cell classes** (CSS for color + tick-flash):
- `dirCls(value)` → `"cell-pos"` (v > 0 green), `"cell-neg"` (v < 0 red), `"cell-flat"` (v = 0 gray)
- `mkPnlCellClass()` → base + directional + LTP-cascade + poll-diff tick-flash
  - LTP-cascade takes precedence (tighter feedback loop)
  - Poll-diff flash waits for broker cycle (slower, less distracting)
- `mp-pnl-cell` — background tint (light green/red 10%)
- `ltp-flash-up`, `ltp-flash-down` — directional pulse (350ms)
- `ltp-vs-avg-up/down/flat` — heat encoding LTP vs entry average
- `ltp-vs-prev-up/down/flat` — heat encoding LTP vs previous close
- `ltp-snap` — static styling (no animation) when is_animating=false
- `ltp-snap-unsettled` — dashed border for pre-settled snapshot rows

**LTP cell resolution** (via `mkResolveCellLtp()`):
- Priority: live SSE snapshot (`snap[sym]` when > 0) > polled ltp field > null
- MCX commodity special case: check `quote_symbol` first (resolved contract key) before raw symbol
- Returns null (renders "—") when no positive price available
- Poll-time LTP has `ltp_ts=0` so SSE ticks always win despite later poll completion time

**Lot display** (F&O only):
- `lotsForRow(row)` → returns lot count when `row.kind === 'fut'` or `'opt'`; null for equity/cash
- Underlying holdings on F&O underlyings use the underlying lot size (not contract)
- `fmtLots()` formats cleanly (e.g. "2.5" for 2.5 lots) or "—" when null

**CE/PE text color** (Sensibull convention):
- Green for CE (call, bullish), Red for PE (put, bearish) when visible in symbol display
- Implemented in symbol cell renderer via `mkSymColRight` / `mkSymColLeft`

**Orphan badge** (Aug 2026):
- Coral "O" badge (class `badge-o`) shown in symbol or status column when `is_orphan=true`
- Indicates position has no matching open AlgoOrder (status=OPEN) for its (account, tradingsymbol)
- Visual cue for operators to establish a parent-child relationship via the OrderPairModal if needed

**Position state (St) column renderer** (Aug 2026):
- Displays `'○'` (hollow circle, UTF-8 U+25CB) for all position rows with `quantity !== undefined`
- Defensive fallback: renders `'○'` even if `is_orphan`, `pair_group_key`, or `has_gtt` fields are
  missing (handles unenriched/partially-loaded rows gracefully). Every position row carries a
  `quantity` field, so '○' will appear in St column for every data row in the grid
- Column is visible in positions grid only; hidden in holdings grid
- Used in derivatives Legs grid (CandidateLegRow) with checkbox rendering before St cell for proper
  accessibility and tab order

**Numeric header style** — `numericHdr` CSS class for right-aligned headers matching data cells

---

## 15. Row Grouping (postSortRows)

After ag-Grid's per-column sort, the component applies two grouping strategies:

**Option underlyings grouping**:
1. Scan `unifiedRows` and identify every underlying (from `row.underlying` field)
2. For each underlying, collect all CE/PE rows that reference it
3. Within each group, preserve ag-Grid's sort order (already applied)
4. Rows without an underlying (cash equity, indices) remain individually sorted
5. Detached symbols (operator drag-to-separate) sort individually at end of bucket

**Order-pair grouping** (Aug 2026):
- Rows with matching `pair_group_key` are kept adjacent: parent row immediately followed by all 
  child rows. This grouping applies AFTER option-underlying grouping and is independent of 
  column sort direction
- When a user sorts by any column (P&L, qty, etc.), paired rows stay visually grouped, ensuring 
  operators see related positions together
- Rationale: parent-child order relationships should remain visible on the grid regardless 
  of operator sort preferences

**Preserve order of** — ag-Grid's per-column sorts (Day P&L, P&L %, volume, etc.) apply BEFORE 
regrouping; no re-sort happens inside groups. If the operator sorted by P&L descending, 
the highest-P&L option contract remains highest within its underlying block, and paired 
child rows remain adjacent to their parent.

**Pinned-bottom rows** (TOTAL) — outside the sortable body; not affected by regrouping

---

## 16. LTP Tick Flash

Directional 350ms pulse overlay on LTP and P&L cells when prices move. Two sources drive flashes 
on different schedules:
- **LTP cascade** (sub-second): SSE tick flashes via `symbolStore` updates; tight feedback loop
- **Poll-diff** (every 5 s): broker fetch cycle detects change from prior poll; P&L columns flash

**Implementation** (via `createTickFlash.svelte.js`):
- `_ltpFlashUp` / `_ltpFlashDown`: Set of symbols with active upward/downward flash
- Reassigned atomically each tick (not mutated in-place) so Svelte reactivity fires
- Zero-guard: non-positive live values treated as "no live tick" (prevents phantom delta)
- `_mpFlash` instance: tracks P&L per-symbol-per-field flashes (key = `"SYM:fieldname"`)
- Threshold: 0.001 (epsilon) prevents false flashes on identical floats due to effect re-runs

**Cell class application** (from `mkPnlCellClass`):
```
base classes (RA + dirCls + mp-pnl-cell)
  + LTP-cascade class if symbol in _ltpFlashUp/_ltpFlashDown
  + poll-diff flash class (tf-up / tf-down) otherwise
```

**Visual effect** — app.css defines:
- `.ltp-flash-up` — green directional pulse (150ms ramp)
- `.ltp-flash-down` — red directional pulse
- `.tf-up` / `.tf-down` — subtle tick-flash (13% opacity, 300ms)

---

## 17. Derivatives P&L Formula (EXP)

The expected (EXP) P&L stat appears in two surfaces: the **Legs grid TOTAL row** (on 
`/admin/derivatives` page) and the **payoff overlay stat** (when viewing payoff chart). 
Both must stay in sync via a canonical formula.

**`_legsExpPnlTotal` contract**:

```
_legsExpPnlTotal = 
  Σ[F&O open legs](intrinsic_at_spot + realised)
  + Σ[F&O closed legs](realised or pnl)
  + Σ[equity legs](linear profit via _equityLinearLegs)
```

**Three-component breakdown**:

1. **F&O open legs** (qty ≠ 0) — remaining contracts still open at expiry. Formula:
   `expiryPnl(row, spot) + (row.realised || 0)`
   - `expiryPnl()` computes intrinsic value at current spot price
   - `row.realised` is added for partial-close positions (contracts closed earlier 
     in the same day; locked-in profit)
   - Example: long 2 CE 2850 spot 2875, 1 contract closed for +30 profit
     → `2 × (2875 − 2850) = +50` (intrinsic) + `30` (realised) = `+80`

2. **F&O closed legs** (qty = 0) — entire position exited today. Formula: 
   `row.realised || row.pnl || 0`
   - When qty is 0, the entire position was closed; realized P&L is locked
   - Value is certain, independent of current spot price
   - Example: sold 2 CE 2850 short, covered today for +100 profit
     → contributes +100 to total
   - **Expiry P&L fix (commit 90d3735f)**: Kite returns `realised=0` for options settled
     at expiry; the actual P&L is stored in `c.pnl` instead. Formula now checks 
     `realised || pnl || 0` instead of `realised || 0` alone. This ensures the Legs grid 
     TOTAL row matches the sum of individual leg rows when options expire.
   - Note: closed legs previously skipped (continue statement) — now included

3. **Equity legs** — stocks in the strategy. Linear profit via 
   `(spot − cost_basis) × qty`. Handles exited equity via `opening_qty` fallback 
   (when qty=0 but opening_qty > 0). Proxy legs included via beta-adjusted quantity 
   (e.g. 0.8× hedging qty).

**Per-leg display in Legs grid**:

A helper `_legExpPnlDisplay(leg, spot)` provides the per-cell EXP value:
- **Open leg** (qty ≠ 0): `expiryPnl(leg, spot) + (leg.realised || 0)`
- **Closed leg** (qty = 0): `leg.realised || leg.pnl || 0` (not "—", fully realized)
- Replaces direct `expiryPnl()` calls; ensures closed legs show locked-in values

**Payoff chart sync — dual-offset behaviour**:

The payoff overlay now applies **two distinct offsets** to keep chart curves and tooltip 
stats in sync:

- **`chartPnlOffset` (= `realizedPnl` = full BS-vs-broker drift)** — applied ONLY to 
  `today_value` in the `adjustedPayoff` curve. This is the cumulative difference between 
  Black-Scholes Greeks calculations and the broker's actual position snapshots (e.g., 
  rounding on MCX lot fills, slippage on partial closes).

- **`expiryPnlOffset` (= Σ closed-leg P&L)** — applied ONLY to `expiry_value` (expiry P&L 
  at current spot). For **closed legs** (qty=0), uses `c.realised || c.pnl || 0` to 
  capture locked-in settlement gains. For **open legs** (qty≠0), uses `c.realised || 0` 
  only (excludes `c.pnl` to avoid double-counting unrealised MTM). This ensures the 
  payoff chart's expiry curve shifts to reflect actual settlement P&L, matching the 
  Legs grid TOTAL row.

**Effect on tooltip EXP stat**: At the current spot price, tooltip EXP now equals 
`expiry_value_at_spot + expiryPnlOffset = _legsExpPnlTotal`. The full `chartPnlOffset` 
(which includes broker MTM noise with no meaning at settlement) is excluded from the 
settlement-time EXP calculation, preventing tooltip drift.

Before this fix, closed legs (options settled at expiry with `realised=0, pnl≠0`) were 
skipped in `expiryPnlOffset` calculation, causing the payoff chart expiry curve to diverge 
from the overlay TOTAL stat and the Legs grid TOTAL row.

**Backend `_expPnlByRootMap` accessor**:

For Snapshot EXP column (MarketPulse Derivatives view):
- **Open leg** (qty ≠ 0): `expiryPnl(c, spot) + (c.realised || 0)`
- **Closed leg** (qty = 0): `c.realised || c.pnl` (locked value, not null/empty)

### 17.1 Payoff Chart Spot Price Resolution (liveSpot)

The payoff overlay derives a canonical spot price (`liveSpot`) in a four-tier ladder
to ensure the chart displays immediately on page load, without waiting for SSE ticks
or broker polls:

**Resolution order** (first non-null value wins):
1. **SSE tick on spot-anchor contract** — live tick from WebSocket subscription
2. **SSE tick on underlying** — live tick if the underlying itself is subscribed
3. **`candidatePositions[*].underlying_ltp`** (backend-stamped, positions.py Pass 3)
   — available immediately on page load from broker settlement data; eliminates 
   "Resolving spot…" placeholder during SSE warmup
4. **`batchQuote _underlyingQuotes[underlying].ltp`** (30s poll fallback) — broker 
   quote cycle refresh
5. **`strategy.spot`** (stale server value) — last-resort static value from page load

**Rationale**: Broker-stamped `underlying_ltp` appears instantly in candidatePositions 
without waiting for SSE subscription to activate, allowing the payoff chart to render 
with a real spot estimate on first paint instead of showing a loading state.

### 17.3 Candidate Leg Row — Pending Qty Chip

The Legs grid now displays a qty chip per row that reflects both closed and pending-order 
states.

**Data source**: `openOrderQtyBySymbol` store (`src/lib/data/openOrdersStore.svelte.js`)
— symbol-to-pending-qty map, co-polled with chase orders.

**Display logic**:
- If `isClosed === true`: show `closed` chip (muted / grey text)
- Else if `pendingQty > 0`: show `{pendingQty} [open]` chip + remaining qty in a separate span
- Else: show plain qty (no special marking)

**Use case**: Operators tracking partial-close executions see which legs have open 
cancel/reduce orders queued, preventing accidental double-reduces.

### 17.2 Candidate Leg Row DOM Order and Derivatives Grid Picker Sort

**Leg row DOM order** (Aug 2026, commit e6656b7e):
- Checkbox now renders **BEFORE** the pos_state (St) cell in CandidateLegRow
- St cell position shifts after checkbox (previously St was rendered first)
- Improves keyboard tab order and accessibility in the derivatives Legs grid
- CSS grid layout updated: `.cand-grid` now defines `grid-template-columns: auto 38px ...`
  where `auto` is the checkbox and `38px` is the St cell width

**Underlying options picker sort** (Aug 2026, commit e6656b7e):
- `underlyingOptionsForPicker` Tier 1 + Tier 2 now sort by **position-count descending**
  (then alphabetical), instead of purely alphabetical
- Operators see underlyings with the most active legs first when selecting roots
- Example: NIFTY with 5 open positions appears before BANKNIFTY with 2 positions

### 17.4 Expiry-Close Analysis — "Exp close" Badge Counts

The derivatives page's expiryCloseAnalysis feature identifies ITM/OTM options approaching 
expiry and organizes them into bands (close, netted, OTM). Badge counts in the UI reflect 
the number of ITM positions requiring action.

**Candidate filtering rule** (commit 9becba9f):
- `annotateOptionCandidates()` now skips all qty=0 (closed) positions before analysis
- Applies **regardless of whether an expiry filter is selected** — prevents false amber 
  "Exp close" badge counts from stale closed legs leaking into `computeExpiryBands`
- Previously: with an expiry filter active, closed positions (qty=0) passed through to 
  band computation, inflating close/netted/OTM counts with settled-away legs
- Now: only live, open positions (qty≠0) contribute to expiry-band analysis

**Data flow**:
1. `buildCandidatePositions()` collects real/provisional/draft F&O positions
2. `annotateOptionCandidates()` filters qty=0 rows (closed positions)
3. `computeExpiryBands()` organizes remaining rows into close/netted/OTM bands
4. UI renders badge counts + band tables from filtered set

**Impact**: Operators see accurate close-action counts; stale in-session closed legs no 
longer spike the orange "Exp close" badge count after partial closes.

---

## 18. Sparklines

5-day sparkline column (present on all six grids) shows intraday price curve as a tiny SVG. 
Missing data falls back to flat line or blank.

**Renderer** — SVG with linear scale; responsive to container width (44px fixed)
- Missing data: blank cell (no visual feedback)
- Single point: flat line (LTP-only during warm-up or broker-empty case)
- Real curve: ≥2 points with variation

**Fetch schedule**:
- Startup + 00:30 IST + segment opens → `_task_sparkline_warm` backend task
- Every 60 s during market hours → `_TICK_SPARK` frontend poller (mover symbols after refresh)
- Every 5 min closed hours → `_stopClosedSparkPoll` failsafe poller
- Chunked in 100-symbol batches via `fetchSparklines(pairs, 5)` from backend

**Backend endpoint** — `GET /api/market/sparklines?symbols=...`:
- Returns `{ data: { "RELIANCE": [2800, 2805, 2820, 2825, 2850], ... }, refreshed_at: "..." }`
- Implements DB-first ladder (Section 5): ohlcv_daily → daily_book → broker historical → compose fallback
- Reason field: `'live'` (today's intraday) · `'snapshot'` (EOD close) · `'ltp_only_flat_pad'` · `'single_point_pad'` · `'historical_fetch_fail'`

**Grace-window rotation** (fix for mover churn):
- When movers rotate every 30 s, new winners/losers get sparklines from prior tick + grace-window holdover
- `_prevMoverSparkPairs` stashed before `loadMovers()` so pruning doesn't drop symbols mid-render
- One-cycle grace period lets fast-moving symbols stay visible through rotation

**Merge strategy** — `_mergeSparkSeries(cached, fresh)`:
- Fresh non-array / empty → keep cached
- Cached has variation (curve) + fresh is flat → keep cached (prefer real curve)
- Fresh shorter than cached + fresh flat → keep cached
- Otherwise → take fresh

Rationale: broker rate-limited calls return `[ltp, ltp]` flat lines; prefer cached real curve 
rather than flattening the chart on the poll after a rate-limit hit.

**Ticker subscription DB backstop** — `_task_sparkline_warm` unions `daily_book` past 7 days 
with live positions/holdings to ensure symbols survive conn_service restart or broker outage. 
No gap in sparkline rendering when the connection recovers.

---

## 19. Symbol Context Menu

Right-click on any grid row (symbol cell or data cell) opens a contextual menu anchored to the 
click coordinates. Keyboard-dismissible (Esc key) and auto-closes on click-outside.

**Available actions**:
- **Open Chart** — opens `ChartModal.svelte` with the symbol pre-populated
- **Open Ticket** — opens `SymbolPanel.svelte` (order entry) pre-filled with symbol + exchange
- **Add to Watchlist** — SymbolSearchInput popup to pick a list; deduped against positions/holdings
- **Remove from Watchlist** — removes the row's watchlist-item link (not from positions/holdings)
- **Detach from Group** — unlinks the symbol from its underlying group (sorts individually)
- **View Payoff** — derivatives-only; opens analytics overlay (Greeks + P&L curve)

**Visibility gates**:
- Remove/detach — hidden for movers (read-only, refreshed every 30 s)
- Add to Watchlist — hidden for demo users (401 backend guard)
- Symbol-specific — hidden when cell is TOTAL row or `_isTotal = true`

**Deep-link actions**:
- `openChartModal(symbol, exchange)` — chart module imported, bars pre-fetched
- `openOrderTicketModal(prefill)` — prefill carries symbol + exchange + side hints
- `openActivityModal(tab)` — activity surface opened to orders/execution log tab

---

## 20. Watchlist Management

Watchlists are operator-created collections of symbols (indices, stocks, commodities, 
options chains). Each list appears as a separate tab in the left panel. The Pinned list 
is system-managed; all others are user-editable.

**CRUD endpoints**:
- `GET /api/watchlist/` — list all lists for the authenticated user
- `POST /api/watchlist` → `{ name: string, is_default: boolean }` — create
- `PATCH /api/watchlist/{id}` → `{ name, display_order }` — rename / reorder
- `DELETE /api/watchlist/{id}` — delete list + all items
- `POST /api/watchlist/{id}/items` → `{ tradingsymbol, exchange, alias? }` — add symbol
- `DELETE /api/watchlist/{id}/items/{item_id}` — remove symbol

**Display order**:
- `lists[].display_order` INT; default 500
- `watchlist_items[].display_order` INT within each list
- Frontend sort via `sortAccountsBy(lists, $orderMap)` (same helper as broker accounts)
- Persisted to DB; survives reload

**Reorder UI** — drag-and-drop or up/down arrow buttons on rows; calls `PATCH /api/watchlist/{id}/items/{item_id}`

**Add symbol flow**:
1. Operator clicks + button or types `/` keyboard shortcut
2. SymbolSearchInput autocompletes tradingsymbol via instruments cache
3. Optional alias input (display name, persisted as `watchlist_items.alias`)
4. Typeahead picks instrument → resolves exchange; direct-add defaults EQ → NSE
5. Option picker (if underlying has chains) — expiry + strike selector before add
6. `addWatchlistItem(listId, sym, exch, alias)` → refresh `activeListsStore`
7. New row appears in target watchlist tab on grid

**Watchlist cache** — `activeListsStore` (TTL.week); survives reload + deploy. Built from 
`/api/watchlist/{id}` per-list fetch; deduped in `loadActive()` via `activeIds` Set. 
Zero-LTP items rendered with sparkline baseline.

**Real-time sync** — watchlist changes trigger `activeListsStore.load()` to repaint grid 
(either `loadActive()` or explicit `activeListsStore.set([])`). TOTAL row recalculates.

---

## 21. Account Multi-Select

Per-card independent account filters — operator can scope Positions to one account 
(e.g. ZG####, intraday-only) while Holdings shows a different account (e.g. ZJ####, 
long-term holds).

**State variables**:
- `positionsAccounts: string[]` — empty = all accounts; populated = only these accounts
- `holdingsAccounts: string[]` — independent filter for holdings grid
- Both persisted to sessionStorage per-browser

**Options population** — `availableAccounts` derived from:
- Broker accounts list (from connStatus store every 15 s)
- Symbols from current pulsePositionsStore + pulseHoldingsStore rows
- Masked for demo users (ZG#### / ZJ#### instead of real codes)
- Sorted via `accountDisplayOrder` store (60s TTL cache from `/admin/brokers` priority field)

**Filtering logic**:
- `_includesPosAcct(acct)` → returns true if `positionsAccounts.length === 0` OR `acct in positionsAccounts`
- `_includesHoldAcct(acct)` → same pattern for holdings
- Applied at buildUnified input (scopes position/holding arrays BEFORE merge)

**Funds strip scope** — UNION of both pickers (shows accounts from either card)

**Prune on change** — `availableAccounts` changes trigger pruning stale selections 
(defence-in-depth for role/mask-mode changes across sessions)

---

## 22. Persistent Cache Layer

`persistentCache.js` three-tier in-memory + localStorage + fetcher pattern. 
Survives page reload + deploy; critical for instant paint after hot deploy.

**TTL buckets**:
- `TTL.week` (7 days) — watchlists, sparklines, market data snapshots; closed-hours frozen state
- `TTL.day` (24h) — past-N-day closes, static sparkline portion; resets at IST midnight
- `TTL.hour` (1h) — watchlist OHLC reference; natural refresh cadence
- `TTL.minute` (15m) — positions, holdings, funds; cache is only for instant paint
- `TTL.short` (2m) — tighter window for live-ish data when freshness critical

**Storage layers**:
1. In-memory Map (instant ~0ms) — lost on nav; survives within-session navigation
2. localStorage JSON (1-3ms read, JSON.parse cost) — survives reload + deploy + browser close
3. Caller fetcher (source of truth) — runs in background, updates both tiers on success

**Debounce on write** — 200ms per key; rapid successive writes coalesce into one fsync 
(avoids jank on mobile Safari where localStorage writes block the event loop)

**What pulse caches**:
- `md.watchlist.{id}` — per-list items + quotes
- `md.positions` — positions row data
- `md.holdings` — holdings row data
- `md.movers` — top winners/losers list
- `md.sparklines` — per-symbol 5-day price curves
- `md.symbolStore` (large blob) — every symbol's LTP + close + volume snapshot
- `rbq.cache.pulse:groupOrder` — manual underlying-group sort overrides
- `rbq.cache.pulse:detached` — symbols pulled out of their group

**Cold-reload behavior** — grid paints instantly from cache; background fetcher updates 
all tiers while the user reads the cached data. If cache is empty, fetcher runs 
on first grid mount; latency is visible but not blocking.

---

## 23. Closed-Hours Snapshot Behavior

When the market is closed, all grid data sources switch to `daily_book` snapshots 
from the last market session. The `closed_hours_or_broker()` gate centralizes this decision.

**Source tags returned by the gate**:
- `'live'` — open hours, broker data real-time
- `'snapshot'` — closed hours, from DB daily_book
- `'snapshot-fallback'` — closed hours + broker fetch failed, using old snapshot
- `'stale-live'` — market open but broker timed out; serving anti-flicker cache (≤120s old)

**Rows carry frozen state**:
- `is_animating = false` — suppresses tick-flash on snapshot rows
- `price_source = 'snapshot_settled'` / `'snapshot_unsettled'` — visual hint via `ltp-snap` CSS
- `as_of: "<ISO-8601 UTC>"` — timestamp for operator confirmation (shown in card headers)

**Grid rendering**:
- Snapshot rows render static prices (no green/red pulse, no animation)
- Card header shows "as of HH:MM IST" when `as_of` is non-null
- TOTAL rows recalculate from filtered rows (not persisted independently)

**Off-hours position snapshot filter** (commits cef00739, 5ac11f56):
- Query uses `AND (db.qty != 0 OR db.date = :today_ist)` to include positions closed intraday
- Intraday closed positions (qty=0 + date=today IST) → shown with 'closed' chip + opacity:0.45
  in derivatives legs grid; frontend `buildCleanLegs()` filters these out from payoff POST
- Prior-session closed positions (qty=0 + date≠today IST) → excluded from snapshot
- Next trading day before market open: yesterday's closed legs absent, only carried-overnight 
  open positions visible, matching broker book state at next gate open

**Day P&L recomputation in snapshot mode**:
- `_positions_snapshot()` CTE `prev_batch` filters within a 7-day lookback window
  (`db.captured_at >= lb.max_at - INTERVAL '7 days'`) to handle holiday gaps, then resolves
  yesterday's EOD LTP via `AND db.ltp IS NOT NULL AND db.ltp > 0 AND db.captured_at < :today_ist_midnight`
  (ensures snapshot uses prior-session closed price, not today's intraday rows)
- Row mapping loop recomputes `day_change_val = (ltp − prev_ltp) × qty` from the frozen
  prior-session LTP for all rows, not relying on Kite's mutable `day_pnl` field which gets
  reset to 0 at settlement
- `_build_holding_row_from_snapshot()` similarly recomputes `day_change_val = (ltp − previous_close) × qty`
  where `previous_close` is write-once (never overwritten like the broker's `day_pnl`)
- NavStrip P-slot and Pulse page grids show correct day P&L immediately after NSE/MCX
  settlement (16:15 IST, ~23:30 IST) since snapshot readers don't depend on the
  broker's zero-flash field

**Movers during closed hours**:
- Snapshot from last NSE close (persisted to `movers_snapshots` table)
- Both winners and losers rows show; sorted by `peak_pct` rather than live `change_pct`
- "Last updated: HH:MM" header shows snapshot timestamp

**Holdings LTP behavior** — distinct from positions:
- Holdings never intraday-split (no overnight_qty / day_buy/sell decomposition)
- Closed hours use `daily_book.ltp` (same snapshot)
- NOT frozen like positions (holdings don't carry intraday P&L concept)

**StaleBanner component** — shown when grid data is stale:
- Stale condition: circuit-breaker account has `last_fail > last_good`
- Message: "Data may be stale — check connection health" with link to BrokerHealthBadge
- Auto-dismisses when fresh data arrives

---

## 24. CardControls Cluster

Unified toolbar appearing on every grid (Pinned, Positions, Holdings, Movers, etc.). 
Buttons stack horizontally or wrap on mobile.

**Button order** (left to right):
1. Refresh — manual refresh for that card only (RefreshButton spinner)
2. Search (magnifier) — opens symbol search popup + type/exchange pickers
3. Download (arrow-down) — immediate CSV export of current filtered view
4. Collapse (up-arrow) — hide card body, show header only; persisted per session
5. Default Size — reset column widths + scroll position to defaults
6. Fullscreen (expand) — maximize card; CSS `fs-card-on` class; card becomes viewport-height

**Visibility rules**:
- Refresh: only in fullscreen mode (or `refreshAlwaysVisible=true`)
- Search: always visible (also via `/` keyboard shortcut)
- Download: always visible for ag-Grid cards (hidden for chart/summary cards)
- Collapse: always visible; `_effCol*` hidden state persisted to localStorage
- Default Size: visible in fullscreen
- Fullscreen: always visible

**Fullscreen behavior**:
- CSS class `fs-card-on` on `.mp-bucket-wrap` container
- `--bucket-rows` CSS var drives grid `maxHeight` (viewport height − headers − padding)
- Card scrolls independently; underlying grids still scroll
- Esc key exits fullscreen

**Refresh timestamp accuracy** (fix commit c0ce46be):
- Derivatives page `loadPositions()` now calls `lastRefreshAt.set(Date.now())` on success
- Previously, the AlgoTimestamp showed a frozen time (last manual-click only) because 
  background position polls set a local `loading` flag while `RefreshButton` watched a 
  different variable `_refreshing`. These watches were not synchronized.
- Now the timestamp advances automatically on every successful 30-second position poll cycle,
  giving operators accurate visibility into when data was last refreshed.

**Derivatives page polling schedule** (fix commit 7ed72480):
- `loadPositions()` now uses `visibleInterval` (30s cadence, continues after market close)
  — ensures EOD broker settlement data (positions marked to settlement price, cash/margin 
  adjusted) is picked up after 23:30 IST when market closes
- `loadStrategy()` and `loadUnderlyingQuotes()` remain on `marketAwareInterval` — those are 
  analytics/LTP feeds with no value after market close, and rate-limiting broker calls 
  during closed hours is counterproductive

**Search UI** (in SearchInput wrapper):
- Symbol typeahead (instruments cache, ≥2 chars)
- Exchange picker (NSE, NFO, MCX, CDS)
- Add button fires `addToWatchlistDeduped()` (hidden for demo)
- Type picker (EQ / FU / CE / PE) shown below typeahead

**CSV export** (via ag-Grid export API):
- File naming: `watchlist.csv`, `positions.csv`, `holdings.csv`, `winners.csv`, `losers.csv`
- Content: current filtered + sorted rows (not the hidden ones)
- Column header: checkbox + all visible column names
- Format: comma-separated, UTF-8, RFC 4180 compatible

---

## 25. Stale-While-Revalidate Bridge for DataStore Updates

MarketPulse applies a stale-while-revalidate pattern to prevent background data refreshes 
from flashing empty grids. When a data store (positions, holdings, movers, funds) fetches 
fresh data, the store's `.value` may transiently become `null` during the network round-trip. 
In Svelte 5, a `$derived` reading `store.value ?? []` snaps to an empty array on that null 
flicker, triggering ag-Grid's `animateRows: true` which fades in rows — making the background 
visible as a flash.

### Pattern Implementation

**Store bridge via $effect + $state** — three data stores now use an explicit bridge:

1. **`pulsePositionsStore` / `pulseHoldingsStore`** (lines 195–204):
   ```javascript
   let positions = $state(pulsePositionsStore.value ?? []);
   let holdings  = $state(pulseHoldingsStore.value  ?? []);
   $effect(() => {
     const p = pulsePositionsStore.value;
     const h = pulseHoldingsStore.value;
     untrack(() => {
       if (p != null) positions = p;  // ← skip null, keep prior snapshot
       if (h != null) holdings  = h;
     });
   });
   ```

2. **`moversStore`** (lines 569–573):
   ```javascript
   let movers = $state(moversStore.value ?? []);
   $effect(() => {
     const v = moversStore.value;
     untrack(() => { if (v != null) movers = v; });
   });
   ```

3. **`fundsStore`** (lines 763–767):
   ```javascript
   let funds = $state(fundsStore.value ?? []);
   $effect(() => {
     const v = fundsStore.value;
     untrack(() => { if (v != null) funds = v; });
   });
   ```

### Behavior

- **Fetch completes** → store.value becomes non-null → `$effect` mirrors new data into `$state`
- **Fetch in progress** → store.value goes null → `$effect` skips the assignment; local `$state` 
  retains the prior snapshot (unchanged)
- **Grid render** → derives from local `$state` (never empty during fetch)
- **Result** → background flash eliminated; grids show prior snapshot while fresh data loads 
  in background

### Related Surfaces

Same bridge applied to **PositionStrip.svelte** and **NavBreakdown.svelte** for their 
positions/holdings/funds store reads. Any new consumer of these stores that renders a 
grid or must maintain smooth UX should apply the same pattern.

---

## 26. Column Sort Now Respects User Click (postSortGroups Guard)

The `postSortGroups` function in `pulseGridSetup.js` previously reordered rows to cluster 
options with their underlying after every ag-Grid sort operation, visually nullifying the 
operator's sort click. When sorting by Day P&L descending, the underlying-grouping logic 
would reshuffle rows back into underlying clusters, ignoring the user's intent.

### Fix

Added early-return guard at the top of `postSortGroups`: when ag-Grid detects an active 
column sort via `api.getColumnState()`, the function returns immediately and lets the 
user's sort stand. Only when NO sort is active does the underlying-grouping behaviour run 
as before.

### Behavior

- **User clicks LTP column header** → ag-Grid sets sort state → `postSortGroups` detects 
  sort state via `getColumnState()` → early return → rows remain sorted by LTP (user's intent)
- **No sort active (default view)** → `postSortGroups` regroups normally → options cluster 
  with underlying
- **Result** → operator-initiated sorts are preserved; auto-grouping only applies when the 
  operator hasn't actively sorted

### Impact

Eliminates the confusing behavior where clicking a column header visually sorted for ~100ms, 
then rows shuffled back into their underlying groups. Now sorts are stable until the 
operator clicks a different column or manually resets grouping.

---

## 27. Public Performance Page CardControls Color Override

The public performance page (`/admin/perf` or similar public-facing snapshot view) displays 
CardControls icon buttons with a champagne gold color (`#c8a84b`) instead of the default 
algo-dark cyan. This visual distinction signals to external stakeholders that the view is 
a read-only performance snapshot, not an operational control surface.

### Implementation

CSS token override in the `.pub-viewport` container:
- Targets CardControls icons (refresh, search, download, fullscreen buttons)
- Applies `color: #c8a84b` to the icon elements
- Cascades to all cards rendered within the public viewport

### Rationale

Public pages (performance dashboards shared externally, performance reports for clients) 
use a different color palette from operational pages to reinforce read-only status. The 
champagne gold matches the public report's header branding and signals "data visibility, 
no editing."

---

## 28. Known Defects

See `PULSE_SPEC.md §9 Known Defects` section (BD1–BD4 fixed in `b1d7654c`, D1–D4 fixed in `b6e52b2a`).

### Open items from 2026-07-11 audit:
- **BR1**: Movers excluded from `_sparkline_universe_symbols` — cold-restart gap for mover sparklines
- **B-R1**: `get_historical_brokers()[0]` can return rate-limited Kite (acceptable — Tier 4 handles empty)
- **Complexity**: MarketPulse.svelte, quote.py, daily_snapshot.py — hotspots identified, refactor pending

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
| 2026-07-11 | v1.1 added DB-first policy (§5), snapshot preservation (§6), self-healing cycle (§7); BD1–BD4 + D1–D4 fixed |
| 2026-07-11 | v1.2 added §11–24 comprehensive component + data-layer expansion (pulseUnified, buckets, columns, context menu, watchlist, account-select, cache, closed-hours, card controls) |
| 2026-07-13 | §17 EXP formula: documented partial-close `realised` field in open-leg formula; closed-leg (qty=0) now included; per-leg helper `_legExpPnlDisplay` for Legs grid display |
| 2026-07-14 | Bucket labels and order-modal close button restored after Svelte 4→5 snippet migration (behavioral parity) |
| 2026-07-24 | §17 closed-leg P&L fallback fix (90d3735f): `_legsExpPnlTotal` and `_legExpPnlDisplay` now use `realised \|\| pnl \|\| 0` to handle Kite's expiry settlement behavior (P&L in `pnl` field when `realised=0`); Legs TOTAL row now matches sum of individual legs |
| 2026-07-24 | §24 refresh timestamp accuracy fix (c0ce46be): `loadPositions()` on derivatives page now calls `lastRefreshAt.set(Date.now())` on success, advancing timestamp on every 30s poll cycle instead of freezing at last manual-click |
| 2026-07-24 | §17 `_expiryPnlOffset` fix (71f91aa0): closed legs (qty===0) now use `c.realised \|\| c.pnl \|\| 0` instead of `c.realised \|\| 0` alone; for options settled at expiry, Kite returns `realised=0` with P&L in `pnl` field; open legs still use `c.realised` only to avoid double-counting unrealised MTM; payoff chart expiry curve now shifts to reflect settlement P&L |
| 2026-07-24 | Derivatives page polling fix (7ed72480): `loadPositions()` switched from `marketAwareInterval` to `visibleInterval` (30s cadence, continues after market close); EOD broker settlement data now picked up after 23:30 IST; `loadStrategy` and `loadUnderlyingQuotes` remain on `marketAwareInterval` |
| 2026-07-24 | §17 equity candidate expiry P&L display fix (023583f9): `_equityLinearLegs` entries now carry a `key` property; `_eqExpPnlByKey` derived maps each equity candidate key to its beta-adjusted linear expiry P&L at live spot; `_legExpPnlDisplay` now handles `'eq'` candidates returning `_eqExpPnlByKey[enKey(c)] ?? null`; `_legsExpPnlTotal` now single-pass over all candidates calling `_legExpPnlDisplay`, guaranteeing `sum(per-leg rows) == TOTAL` for all candidate types including equity and proxy hedges |
| 2026-07-26 | §17.1 payoff chart spot price resolution (63262b94): `liveSpot` derived now includes `candidatePositions[*].underlying_ltp` (backend-stamped in positions.py Pass 3) as step 3 in resolution chain, before batchQuote fallback; eliminates "Resolving spot…" during SSE warmup |
| 2026-07-27 | v1.3 Expiry re-scan + positions cleanup (commits cbbe0f23, 21d1656a): §4.4 positions/holdings now notes off-hours snapshot includes `AND qty != 0` guard (flat/expired excluded) and runs orphan cleanup (same-day + prior-day, 7-day scope). §7 added expiry-day auto-close agent status change (now active by default) with re-scan loop (every 30 min until 15:25 IST, catches newly-ITM positions). |
| 2026-07-28 | v1.4 Snapshot intraday-closed position inclusion (commits cef00739, 5ac11f56): §4.4 positions/holdings updated — off-hours snapshot filter refined from `AND qty != 0` to `AND (qty != 0 OR date = :today_ist)`. Intraday closed positions (qty=0, date=today IST) now appear in snapshot with 'closed' chip + opacity:0.45 in derivatives legs grid; prior-session closed excluded. §23 Closed-Hours Snapshot Behavior expanded with detailed off-hours position snapshot filter explanation. Frontend `buildCleanLegs()` filters qty===0 from payoff POST (strategy analytics unchanged); legs grid shows closed legs for intraday history only. |
| 2026-07-29 | §4.4 Derivatives Legs Grid candidate filtering (commit 9becba9f): F&O positions skip missing instruments (expired, not in master), equity holdings and proxy hedges skip qty=0, draft positions skip missing instruments. §17 Expiry-close analysis: qty=0 (closed) positions now always excluded from `annotateOptionCandidates` regardless of expiry filter — prevents false amber "Exp close" badge counts from stale closed legs leaking into `computeExpiryBands`. |
| 2026-08-07 | §4.4 Short position day P&L fix (commit 1769cffc): `baseDayPnlForPosition` overnight quantity guard corrected from `oq > 0` to `oq !== 0`. Short MCX positions (oq < 0) now receive the `day_change_val` fast-path and Case 4 stale-close guard during the 23:30–09:00 IST window. Previously, shorts would return an unguarded formula result, causing catastrophic ₹5,00,000+ day-P&L overstatement when `close_price=0` (broker stale data between sessions). |
| 2026-08-08 | v1.3 Snapshot day P&L recomputation + prev_batch 7-day lookback (commit TBD): §4.4 + §23 updated — `_positions_snapshot()` CTE `prev_batch` now includes 7-day lookback window (`db.captured_at >= lb.max_at - INTERVAL '7 days'`) before the time-of-day filter to handle multi-day holiday gaps and MCX's 23:30 IST close-to-open window. Row loop recomputes `day_change_val = (ltp − prev_ltp) × qty` from frozen prior-session LTP instead of trusting broker's mutable `day_pnl` field. Holdings snapshot similarly recomputes from write-once `previous_close` instead of zeroed `day_pnl`. NavStrip P and Pulse grids now show correct day P&L immediately after settlement without stale-price errors. |
| 2026-08-09 | v1.4 Holdings snapshot prev_batch CTE + MCX lot-scale day P&L (commit TBD): §4.4 updated — `_HOLDINGS_SNAPSHOT_SQL` now includes `prev_batch` CTE (same pattern as positions) finding most-recent prior-day LTP per (account, symbol) within 7-day lookback. `_build_holding_row_from_snapshot()` computes `day_change_val = (ltp - prev_ltp) × qty` when `prev_ltp > 0`, matching positions closed-hours pattern. Holdings day P&L during closed hours now derived from price diff, not stale stored value. Fallback to `(ltp - previous_close) × qty` when `prev_ltp` unavailable/zero. Related: MCX day_pnl lot-scale fix in BROKER_SPEC.md §7.3 — `_snap_compute_day_pnl()` now scales intraday quantities by lot_size before formula evaluation, fixing brand-new MCX positions showing day_pnl off by 100× on first snapshot. |
| 2026-08-11 | v1.5 Positions close-price priority fix: §4.4 updated — `build_row_from_snapshot_raw` now prefers `previous_close` (frozen settlement) over `prev_ltp` (recent batch LTP) for `computed_day_pnl`, fixing positions showing day_change_val ≈ 0 after market close when daily_book had multiple intraday captures. Frontend: `_tickBookPollers()` now includes `pulseHoldingsStore.load()` so NavStrip H:1 stays in sync with Pulse Holdings TOTAL during closed hours. |
| 2026-08-13 | v1.5 Position day P&L store + timer rationalization: §7 updated — MarketPulse reduced from 22 active timers across 7 cadences to 17 timers across 4 cadences (5s book poller, 30s quotes/movers/sparklines, 60s settings audit, tick-driven SSE). §11.1 new subsection documents `positionsDayPnlStore.svelte.js` module-level singleton (SSOT for live day P&L); exports `{ total, byKey }` at 4Hz throttle; consumed by PositionStrip P pill, MarketPulse grid cells, and Dashboard hero. `mergePositionRows` calls `livePositionDayPnl(ctx)` with `marketOpen: true` unconditionally, preferring snap LTP from `symbolStore` over `liveQ` LTP. |
| 2026-08-14 | v1.7 Order-pair feature + orphan position tracking (commit 6f374a1a): §4.4 PositionRow added `is_orphan: bool` (True when no open AlgoOrder matches position's account/tradingsymbol) and `pair_group_key: str\|None` (shared root AlgoOrder ID for parent-child linked positions). §15 Row Grouping expanded — `postSortRows` callback now keeps paired positions (same `pair_group_key`) adjacent regardless of column sort. §14 Column Definitions added orphan badge documentation (coral "O" badge when `is_orphan=true` shown in symbol column). Frontend MarketPulse position rows show orphan badge; dangling child orders in ChaseCard show "O" chip. New `OrderPairModal.svelte` accessible from Pulse positions/legs headers; fetches recent orders, links parent + unlinked-child pickers, calls `POST /api/orders/pair` endpoint on submit. |
| 2026-08-14 | v1.6 Snapshot WHERE NULL fix + holdings day P&L store (commit 43771b98): §4.4 Positions & Holdings updated — `_positions_snapshot()` WHERE filter fixed from `AND db.ltp IS NOT NULL AND db.ltp > 0` to `AND (db.ltp IS NULL OR NOT (db.ltp = 0 ...))` to include NULL LTP rows captured during mid-session NSE passes (prevents grid blanks when broker quote fails). `_override_stale_close_from_snapshot` cutoff extended from 00:00 IST to 08:00 IST — MCX post-midnight snapshots (00:00–08:00 IST) now included in stale-close override for continuous Day P&L visibility. Added §11.2 Holdings Day P&L SSOT (`holdingsDayPnlStore.svelte.js`, Aug 2026) documenting module-level singleton exporting `{ total, byKey }` at 5s live / 30min closed cadence; consumed by PositionStrip H pill, MarketPulse grid, and Dashboard. |
| 2026-08-16 | v1.8 Position state column + derivatives grid improvements (commit e6656b7e): §13 Holdings grid updated — St (pos_state) column filtered out of holdings (visible only in positions grid); Lots column repositioned immediately before Invested. §14 added pos_state cellRenderer fallback documentation — renders '○' for any row with `qty_pos !== undefined` even if enrichment fields missing. §17.2 (renamed from 17.4) new subsection documents Candidate Leg Row DOM order: checkbox now renders BEFORE St cell for better accessibility; `underlyingOptionsForPicker` Tier 1/2 now sort by position-count descending (then alphabetical) instead of purely alphabetical. PerformancePage St column hidden via `hide: true`. |
| 2026-08-16 | v1.9 St column fallback + derivatives column order + Groww day P&L (commits 0e0a3a78, b424077d): §14 Position state renderer updated — fallback field corrected from `qty_pos !== undefined` to `quantity !== undefined` (every position row carries `quantity` so '○' appears for all data rows); CandidateLegRow updated with same fallback. §13 Derivatives column order now matches positions for common fields: `St → sym → lots → ltp → avg → day_pnl → close → pnl → qty → account → exp_pnl`. Holdings Lots column confirmed positioned before Invested. §4.4 Broker-specific notes added — Groww `_normalise_positions()` pre-computes `day_change_val = (ltp - close) × qty` (guarded: ltp > 0, close > 0, qty ≠ 0) to resolve Groww Day P&L showing 0 on cold-cache page load (Groww API omits this field). |
| 2026-08-16 | v2.0 St column heading + orphan display (commit 7b8d432c): §14 Position state (St) column renderer updated — now displays visible heading "St" (was empty `<span>`); fallback rendering for unmatched positions now unconditional (returns `'○'` for any row with `quantity !== undefined`, no pair group, no GTT); removes fragile fallback complexity. CandidateLegRow.svelte checkbox-before-St DOM order confirmed. St column always visible in positions grid, filtered out of holdings grid. §13 Confirms Holdings Lots column positioned immediately before Invested for UX flow. |
| 2026-08-18 | v2.1 Day P&L correctness fixes (commit ed63b9fe): §4.4 Flat row hygiene narrowed — `_apply_flat_row_hygiene` mask now `(quantity == 0) AND (overnight_quantity == 0)` (pure intraday round-trips only), preserving backstop `day_change_val` for closed overnight futures (qty=0, oq>0). Previous broad mask `(quantity == 0)` was undoing Case 3 `apply_day_change_backstop` results. §13 Derivatives day P&L fix — `candidatesDayPnl` SSE delta now gates to fallback-only (when `_dayPnlForLeg` returns baseline due to `oq=0` or no valid SSE ltp or `close=0` or `qty=0`), preventing double-counting of live tick moves for overnight legs where `_dayPnlForLeg` already includes full `(liveLtp − close)` from SSE data. |
| 2026-08-20 | v1.6 Holdings live-path previous_close fix (commit 75a335f7): §4.4 Holdings updated — `_override_stale_close_for_holdings` now queries `COALESCE(daily_book.previous_close, daily_book.ltp)` as the reference close price and writes `previous_close` to ALL holding rows in raw DataFrame; `HoldingRow` schema now includes `previous_close: float`. Frontend `holdingsDayPnlStore.svelte.js` and `pulseUnified.js` now prefer `h.previous_close` (frozen COALESCE from daily_book) over `h.close_price` (Kite's mutable field). Backend `apply_day_change_backstop()` now called for holdings (same as positions) to handle NSE settlement case where `close_price == ltp` and `day_change_val` stales to 0. Fixes H slot showing 0 after NSE settlement when guardian formula falls back to stale `day_change_val`. |
| 2026-08-20 | v1.7 Positions previous_close fix + funds closed-hours gate (commits 2fb8ca14, 17da604a): §4.4 Positions updated — `_override_stale_close_from_snapshot()` in positions.py now queries `COALESCE(daily_book.previous_close, daily_book.ltp)` as the reference close price and writes `previous_close` to ALL matched rows; `PositionRow` schema now includes `previous_close: float` (mirrors holdings). Frontend `positionsDayPnlStore.svelte.js`, `pulseUnified.js`, and `nav.js` now prefer `p.previous_close` (frozen COALESCE from daily_book) over `p.close_price` (Kite's mutable field), eliminating positions day P&L zeroing at NSE settlement (matches holdings fix 75a335f7). §2 / §9 Funds route: `/api/funds` and `_fetch_funds_phase()` in nav.py now gate with `closed_hours_or_broker()` pattern — when both market segments closed and cache warm, returns cached value without calling broker, eliminating stale pre-settlement margin/cash data during W3 (NSE-closed + MCX-open) and W4 (both closed) windows. Adds 5-window edge-case test coverage (W1–W5) for positions, holdings, cash, margin completeness. |
