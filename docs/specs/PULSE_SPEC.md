# Pulse Page Specification

Single source of truth for the `/pulse` page behavior across all market states, user states,
and data sources. Code, tests, and documentation must stay in sync with this file.

**Version**: 1.1 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/MarketPulse.svelte` · `frontend/src/lib/data/marketDataStores.svelte.js` · `backend/api/routes/quote.py` · `backend/api/routes/watchlist.py` · `backend/api/helpers/snapshot_gate.py` · `backend/api/algo/daily_snapshot.py`

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
11. [Known Defects](#11-known-defects)

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
- Closed: from `daily_book` snapshot → `'snapshot'` with `as_of` timestamp
- Day P&L: always via `baseDayPnlForPosition(p)` — NEVER read `day_change_val` directly
- Do NOT use `positions.close_price` (stale overnight); use `daily_book.ltp`

### 4.5 Card Controls — CSV Export Button

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

## 11. Known Defects

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
