# Pulse Page Specification

Single source of truth for the `/pulse` page behavior across all market states, user states,
and data sources. Code, tests, and documentation must stay in sync with this file.

**Version**: 1.2 — 2026-07-11  
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
25. [Known Defects](#25-known-defects)

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
- NavStrip P-slot is guarded against zero-flash during live→snapshot transitions
  (when `close_price === ltp`, the guard returns 0 to prevent distortion)

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
- Columns: Symbol (right-aligned account tint) · 5d · LTP · Avg · Day % · Close · P&L · P&L % · Day P&L · Qty · Lots · Account

**Holdings grid** (`gridHoldings`):
- Rows: `_majorGroup === 'holdings'` + `_includesHoldAcct(account)` filter
- Source: broker holdings + daily_book snapshot; LTP never intraday-split
- Account filter: separate MultiSelect on `holdingsAccounts`; persisted independently
- TOTAL row: sum of filtered holdings (cost basis + current value)
- Columns: Symbol (account tint) · 5d · LTP · Avg · Day % · Close · Day P&L · P&L % · P&L · Qty · Lots · Invested · Value · Account

**Right-grid column order** (via `mkRightColDefs()`):
- Symbol (168px, pinned) — account-tinted background (color per lead account)
- 5d sparkline (44px)
- LTP (77px) — SSE tick + vs-avg/vs-prev heat; snapshot-frozen when is_animating=false
- Avg (68px) — weighted average entry (directional tint: long green, short red, flat gray)
- Day P&L (78px) — today's profit/loss; tick-flash on poll cycles (300ms)
- Day % (64px) — day P&L as % of yesterday's market value (close × qty)
- Close (68px) — previous close (muted)
- P&L (78px) — lifetime profit/loss; directional + tick-flash
- P&L % (64px) — P&L as % of cost basis
- Qty (56px) — net qty; aggregated across accounts; null hidden
- Lots (52px) — qty in F&O lot units; via `lotsForRow()` helper
- Invested (78px) — cost basis (avg × held qty) for holdings
- Value (78px) — current value (LTP × held qty) for holdings
- Account (86px) — lead account + "+N" for multi-account rows; STALE@HH:MM badge on circuit-breaker rows

**Day P&L recompute** — via `livePositionDayPnl()` helper (shared with derivatives):
- When market open: `(liveLtp − closePx) × qty + realisedToday`
- When market closed: `baseDayPnlForPosition(row)` (broker day_change_val or lifetime pnl if missing)
- MCX stale-ticker rescue: when broker LTP ≈ close_price (KiteTicker lag), use SSE live tick if available

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

**Numeric header style** — `numericHdr` CSS class for right-aligned headers matching data cells

---

## 15. Row Grouping (postSortGroups)

After ag-Grid's per-column sort, the component re-arranges rows so option contracts stay grouped 
with their underlying (NIFTY + all NIFTY calls + all NIFTY puts in one contiguous block).

**Algorithm**:
1. Scan `unifiedRows` and identify every underlying (from `row.underlying` field)
2. For each underlying, collect all CE/PE rows that reference it
3. Within each group, preserve ag-Grid's sort order (already applied)
4. Rows without an underlying (cash equity, indices) remain individually sorted
5. Detached symbols (operator drag-to-separate) sort individually at end of bucket

**Preserve order of** — ag-Grid's per-column sorts (Day P&L, P&L %, volume, etc.) apply BEFORE 
regrouping; no re-sort happens inside groups. If the operator sorted by P&L descending, 
the highest-P&L option contract remains highest within its underlying block.

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
   `row.realised || row.pnl`
   - When qty is 0, the entire position was closed; realized P&L is locked
   - Value is certain, independent of current spot price
   - Example: sold 2 CE 2850 short, covered today for +100 profit
     → contributes +100 to total
   - Note: closed legs previously skipped (continue statement) — now included

3. **Equity legs** — stocks in the strategy. Linear profit via 
   `(spot − cost_basis) × qty`. Handles exited equity via `opening_qty` fallback 
   (when qty=0 but opening_qty > 0). Proxy legs included via beta-adjusted quantity 
   (e.g. 0.8× hedging qty).

**Per-leg display in Legs grid**:

A helper `_legExpPnlDisplay(leg, spot)` provides the per-cell EXP value:
- **Open leg** (qty ≠ 0): `expiryPnl(leg, spot) + (leg.realised || 0)`
- **Closed leg** (qty = 0): `leg.realised || leg.pnl` (not "—", fully realized)
- Replaces direct `expiryPnl()` calls; ensures closed legs show locked-in values

**Payoff chart sync — dual-offset behaviour**:

The payoff overlay now applies **two distinct offsets** to keep chart curves and tooltip 
stats in sync:

- **`chartPnlOffset` (= `realizedPnl` = full BS-vs-broker drift)** — applied ONLY to 
  `today_value` in the `adjustedPayoff` curve. This is the cumulative difference between 
  Black-Scholes Greeks calculations and the broker's actual position snapshots (e.g., 
  rounding on MCX lot fills, slippage on partial closes).

- **`expiryPnlOffset` (= Σ `c.realised` for enabled non-equity candidates = locked-in 
  closed-leg gains only)** — applied ONLY to `expiry_value` (expiry P&L at current spot). 
  This captures the sum of all closed-leg realised gains that are already locked in.

**Effect on tooltip EXP stat**: At the current spot price, tooltip EXP now equals 
`expiry_value_at_spot + expiryPnlOffset = _legsExpPnlTotal`. The full `chartPnlOffset` 
(which includes broker MTM noise with no meaning at settlement) is excluded from the 
settlement-time EXP calculation, preventing tooltip drift.

Before this fix, both curves shifted by the full `chartPnlOffset`, causing tooltip EXP 
to include BS-vs-broker MTM noise that has no settlement meaning.

**Backend `_expPnlByRootMap` accessor**:

For Snapshot EXP column (MarketPulse Derivatives view):
- **Open leg** (qty ≠ 0): `expiryPnl(c, spot) + (c.realised || 0)`
- **Closed leg** (qty = 0): `c.realised || c.pnl` (locked value, not null/empty)

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

## 25. Known Defects

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
