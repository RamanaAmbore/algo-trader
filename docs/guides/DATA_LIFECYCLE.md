# Data Lifecycle Audit

Complete end-to-end data refresh cycles for tick data, positions, holdings, NAV, NavStrip,
and MarketPulse covering NSE equity, MCX commodity, live market hours, and closed/off-market
hours.

---

## §1 — Overview

| Layer | NSE + MCX live<br>(09:15–15:30 NSE, 09:00–23:30 MCX) | NSE closed, MCX live<br>(15:30–23:30 IST) | All markets closed<br>(23:30–09:00 IST) |
|---|---|---|---|
| **Tick LTP** | KiteTicker WS → mmap 50ms → SSE real-time | KiteTicker WS (MCX only) → mmap → SSE | WS open, no ticks; SSE serves `_LAST_GOOD_LTP` cache (1h TTL) |
| **Positions** | Kite REST + LTP override + day_pnl backstop + live cache (120s) | Kite REST (MCX rows only) + snapshot overlay | daily_book SQL + 7-day lookback CTE, snapshot_settled tag |
| **Holdings** | Kite REST + LTP override + live cache (120s) | Kite REST (MCX rows only) | daily_book SQL + 7-day lookback CTE, snapshot_settled tag |
| **NAV + Pulse** | Live broker (positions MTM + holdings MTM + cash) | Live broker (MCX holdings + cash; NSE holdings from snapshot) | nav_daily DB row (1/day) + LKG quote cache (24h); SSE ticks inactive |

---

## §2 — Tick Data Lifecycle

### 2.1 Subscription path

Happens once at conn_service startup or on `WATCH` message:
- `subscribe_with_sym(symbols) → broker.instruments() + token_map` (broker_apis.py)
- `KiteTicker.subscribe(tokens)` → WebSocket /connect → Kite sends `order_update` + `profile`
  ack after ~1–2 ticks (kite_ticker.py:995)

### 2.2 Tick ingestion (hot path)

`KiteTicker._on_ticks` (kite_ticker.py:995):
- Receives WS message (mode=ltp, list of {token, ltp, bid, ask, bid_size, ask_size, ts, ...})
- Zero-LTP guard: `if lp ≤ 0: skip` (invalid ticks dropped)
- Update `_tick_map[tok]` ← {token, sym, ltp, ts} + `_tick_age[tok]` ← now
- Build mmap write payload {tok, ltp, prev_close, avg_price, ts_ns}
- Call `ticker_writer.upsert(payload)` for mmap write

### 2.3 mmap buffer

TickBufferWriter.upsert [backend/brokers/tick_buffer.py](backend/brokers/tick_buffer.py#L117):
- 40-byte slot struct per token: u32 token + u32 pad + f64 last_price + f64 prev_close +
  f64 avg_price + u64 last_ts_ns
- Hash-indexed table in `/dev/shm/ramboq_ticks` RAM-backed mmap (4096 slots max, 64-byte header)
- Header: u32 version + u32 slot_count + u32 max_slots + u64 last_write_ns
- **Version bumped LAST** for torn-read safety (reader sees atomic slot version update after
  data is committed)

### 2.4 API-process read

MmapTickReader._poll_loop [backend/brokers/mmap_ticker.py](backend/brokers/mmap_ticker.py#L270):
- 50ms cadence
- Check mmap header version → detect deltas (compare to local version)
- Scan active slots → collect {tok, sym, ltp, ts_ns} for changed slots
- Publish to local BroadcastBus; trigger SSE event producers

### 2.5 SSE stream

`/api/quotes/stream` [backend/api/routes/quote.py](backend/api/routes/quote.py#L1777):
- On client connect: emit `snapshot` event (all currently-held ticks, {token: {ltp, sym}})
- Per mmap delta: emit `tick` event ({tok, sym, ltp, ts})
- **30s heartbeat** when no ticks (keeps connection alive)

### 2.6 Off-market behaviour

23:30–09:00 IST (all markets closed):
- WebSocket stays open; Kite sends no ticks → `_tick_map` grows stale
- `_tick_age` values increase; health watchdog may log "stale quote" warnings
- SSE clients connected during this window still receive 30s heartbeats; LTP values are
  stale (as_of last settlement)
- batch_quote / quote endpoints switch to `_LAST_GOOD_LTP` cache (1h TTL) + LKG quote
  cache (24h TTL)

---

## §3 — Positions Lifecycle: Live Path

```
┌─────────────────────────────────────┐
│ GET /api/positions (NSE/MCX live)   │
│ closed_hours_or_broker() gate       │
│ (snapshot_gate.py:220)              │
└──────────────┬──────────────────────┘
               │
        [MARKET OPEN]
               │
   ┌───────────▼─────────────┐
   │ fetch_positions()       │
   │ per account via UDS     │
   │ → Kite REST /positions  │
   └───────────┬─────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────┐
   │ 1. backfill_market_data()                                │
   │    (batch quote for rows with last_price=0)              │
   └───────────┬──────────────────────────────────────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────┐
   │ 2. _override_stale_ltp_from_ticker()                     │
   │    (patch last_price from KiteTicker _tick_map)          │
   └───────────┬──────────────────────────────────────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────┐
   │ 3. _patch_raw_positions()                                │
   │    · _override_stale_close_from_snapshot()               │
   │      (daily_book prior-session close_price)              │
   │    · apply_day_change_backstop()                         │
   │      (Cases 1/3 guards for oq=0 and qty=0)               │
   └───────────┬──────────────────────────────────────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────┐
   │ 4. Polars vectorised selection (_ROW_COLS)               │
   └───────────┬──────────────────────────────────────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────┐
   │ 5. _enrich_position_greeks()                             │
   │    (IV/delta/theta for option rows)                      │
   └───────────┬──────────────────────────────────────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────┐
   │ 6. _overlay_snapshot_for_closed_exchanges()              │
   │    (patch LTP from latest_snapshot_ltp_map if exchange   │
   │    closed; price_source="snapshot_settled")              │
   └───────────┬──────────────────────────────────────────────┘
               │
   ┌───────────▼──────────────────────────────────────────────┐
   │ 7. build_summary_from_rows() → per-account sums          │
   │ 8. apply_scope_and_mask() → trader scope + account mask  │
   └───────────┬──────────────────────────────────────────────┘
               │
       ┌───────▼──────────┐
       │ HTTP 200 response│
       └──────────────────┘
```

**DataFrame columns from Kite REST:** account, tradingsymbol, exchange, quantity,
average_price, last_price, close_price, pnl, day_change, day_change_percentage,
overnight_quantity, day_buy_quantity, day_sell_quantity, day_buy_value, day_sell_value,
multiplier, product.

**Anti-flicker mechanism:** Stale-live cache (120s TTL) at [snapshot_gate.py:66,120](backend/api/helpers/snapshot_gate.py#L66-L120). On broker connection failure, returns last cached
live response tagged `"stale-live"` rather than switching immediately to snapshot path.

---

## §4 — Positions Lifecycle: Snapshot Path

Triggered when NSE/NFO closed (15:30–09:15 IST) or on broker outage.

`_positions_snapshot()` [backend/api/routes/positions.py](backend/api/routes/positions.py#L43) → SQL query → reconstruct rows → response

**SQL structure** [backend/api/routes/positions.py](backend/api/routes/positions.py#L74):

```sql
latest_batch CTE:
  MAX(captured_at) per account
  WHERE kind='positions' AND ltp IS NOT NULL
  — finds the most-recent successful Kite snapshot (usually 16:15 IST)

prev_batch CTE:
  DISTINCT ON (account, symbol), most-recent prior-day row
  WHERE captured_at < latest_batch.max_at
    AND captured_at >= max_at - 7 days  — covers MCX holidays + weekends
    AND captured_at < today_ist_midnight — excludes mid-session rows
  Returns: prev_ltp, prev_settlement_pnl

Main SELECT:
  JOIN daily_book to latest_batch + LEFT JOIN prev_batch
  WHERE qty≠0 OR date=today
    AND NOT (ltp=0 AND total_pnl=0 AND avg_cost>0)
```

**13-column tuple:** account, symbol, exchange, qty, avg_cost, ltp, day_pnl, total_pnl,
payload_json, captured_at, previous_close, prev_ltp, prev_settlement_pnl

**Row reconstruction** [backend/api/helpers/positions_helpers.py](backend/api/helpers/positions_helpers.py#L298) `build_row_from_snapshot_raw()`:

1. Extract snapshot_extras from payload_json via `extract_snapshot_extras()`
   [backend/api/helpers/positions_helpers.py](backend/api/helpers/positions_helpers.py#L90)
2. Extract multiplier from payload_json via `extract_snapshot_multiplier()`
   [backend/api/helpers/positions_helpers.py](backend/api/helpers/positions_helpers.py#L107):
   - 1 for NSE/NFO
   - actual lot_size for MCX/NCO
3. `effective_qty = qty × multiplier`
4. close_price resolution order: previous_close (if > 0) → prev_ltp (if > 0) → ltp
5. Computed day_pnl:
   - If previous_close available: `(ltp - previous_close) × effective_qty`
   - Else if prev_ltp available: `(ltp - prev_ltp) × effective_qty`
   - Else: use stored day_pnl

   Note: this priority was corrected in Aug 2026 — `previous_close` (COALESCE-frozen at first write, holds yesterday's settlement) is now primary. `prev_ltp` is fallback only, so multi-batch intraday captures no longer collapse `day_change_val` to ~0 after market close.
6. Day percentage: `resolve_snapshot_day_pct()` — denominator =
   `prev_close × qty` (not LTP × qty)
7. Flat-row hygiene: qty=0 rows set `day_change_val=0`
   [backend/api/routes/positions.py](backend/api/routes/positions.py#L357)

**Output tags:** price_source="snapshot_settled", last_price_stale=True, as_of=captured_at

**daily_book key columns:**

| Column | Type | Notes |
|---|---|---|
| qty | INT | LOTS for MCX positions |
| ltp | NUMERIC | NULL during mid-session writes |
| day_pnl | NUMERIC | NULL during mid-session; restored from snapshot_extras on read |
| previous_close | FLOAT | Frozen on first write (COALESCE in UPSERT); prior-session Kite settlement |
| prev_ltp | — | From prev_batch CTE; most-recent prior-batch LTP |
| prev_settlement_pnl | — | From prev_batch CTE; yesterday's total_pnl for day P&L Branch A |
| payload_json | TEXT | Raw Kite row + nested snapshot_extras (OHLC, volume, OI, day_change_val, ltp, settled) |

---

## §5 — Holdings Lifecycle

### Live path

`GET /api/holdings` → closed_hours_or_broker() gate → fetch_holdings() per account via UDS →
Kite REST /holdings → DataFrame (tradingsymbol, quantity, average_price, last_price, pnl,
day_change, cur_val) → summary → response

### Snapshot path

`_holdings_snapshot()` [backend/api/routes/holdings.py](backend/api/routes/holdings.py#L209) → `_HOLDINGS_SNAPSHOT_SQL`
[backend/api/routes/holdings.py](backend/api/routes/holdings.py#L40)

Same CTE structure as positions (latest_batch + prev_batch, 7-day lookback, kind='holdings').

`_build_holding_row_from_snapshot()` [backend/api/routes/holdings.py](backend/api/routes/holdings.py#L100) — day_change_val priority:

1. `(ltp - prev_ltp) × qty` — if prev_ltp > 0 (most accurate: actual inter-batch price move)
2. `(ltp - previous_close) × qty` — if previous_close > 0 (prior-session settlement)
3. Stored day_pnl column — fallback

cur_val = ltp × qty when snapshot LTP is used (broker cur_val not stored in daily_book).

### NAV integration

`compute_firm_nav()` [backend/api/algo/nav.py](backend/api/algo/nav.py#L357) uses
`_fetch_holdings_from_snapshot()` [backend/api/algo/nav.py](backend/api/algo/nav.py#L316)
when NSE closed — not live broker — so NAV doesn't show stale/zero holdings during closed
hours.

---

## §6 — NAV Computation and Refresh

### Formula (v4)

```
NAV = cash_total + positions_mtm + holdings_mtm

  cash_total    = Σ per account (margin.opening_balance + margin.option_premium)
  positions_mtm = Σ position.pnl  where quantity ≠ 0
  holdings_mtm  = Σ holding.cur_val
```

Source: `compute_firm_nav()` [backend/api/algo/nav.py](backend/api/algo/nav.py#L357).
Implementation: Polars-vectorised (not iterrows). Account-level try/except so single-account
outage doesn't break whole NAV.

Holdings source:
- **Live broker** when NSE open
- **daily_book snapshot** when NSE closed [backend/api/algo/nav.py](backend/api/algo/nav.py#L316-L354)

### Persistence

`_task_nav_compute()` [background/background.py](backend/api/background.py#L1208) fires at
16:00 IST daily. Calls `write_nav_snapshot()` → upserts one row into nav_daily (idempotent
by as_of_date).

### Routes

| Route | Method | Behaviour |
|---|---|---|
| `/api/nav/latest` | GET | DB read — latest nav_daily row + prior-day delta |
| `/api/nav/` | GET | History — last N days (default 90) from nav_daily |
| `/api/nav/compute` | POST | Operator-triggered live compute + upsert |
| `/api/nav/me` | GET | Per-investor slice — live compute, falls back to nav_daily on outage |

No HTTP-level cache. The nav_daily row written at 16:00 IST is the caching mechanism.

---

## §7 — NavStrip Slots

Source: `PositionStrip.svelte` + `frontend/src/lib/data/nav.js`. Poll cadence: marketAwareInterval
30s (pauses when browser tab hidden).

| Slot | Label | Data source | Formula |
|---|---|---|---|
| **P (day)** | Positions day P&L | positionsStore (all exchanges) | Σ baseDayPnlForPosition(p) |
| **P (lifetime)** | Positions total | positionsStore | Σ position.pnl (no SSE delta) |
| **H (day)** | Holdings day MTM | holdingsStore | Σ holding.day_change_val + live SSE deltas |
| **H (lifetime)** | Holdings total | holdingsStore | Σ holding.pnl (no SSE delta) |

### P-pill filtering

`positionsPnlFiltered()` [frontend/src/lib/data/nav.js](frontend/src/lib/data/nav.js)
(F&O only, FO_EXCHANGES = {NFO, MCX, BFO}). Excludes NSE/BSE CNC/MIS rows to avoid
double-counting with H-pill. The P slot TOTAL includes all exchanges.

### baseDayPnlForPosition(p)

[frontend/src/lib/data/nav.js](frontend/src/lib/data/nav.js):

- **Normal overnight:** `pnl - oq × (close - avg_price)` where oq = overnight_quantity
- **Case 1 guard:** `oq === 0 && pnl !== 0` → return `pnl` (new intraday; Kite omits
  day_change_val)
- **Case 4 guard:** `close <= 0` → return `0` (stale-close window 23:30–09:00 IST;
  prevents ±₹5L distortion in short MCX positions)
- **Short fix** [commit 1769cffc](backend/api/algo/nav.py#L108): guard condition `oq !== 0`
  (not `oq > 0`) — short overnight positions (oq < 0) now hit fast-path correctly

### Off-market behaviour

Positions and holdings routes serve daily_book snapshots → P/H slots frozen at last
settlement values; no SSE deltas (ticker sends no ticks).

---

## §8 — MarketPulse and Quote Refresh

### Polling

MarketPulse polls `batchQuote()` every 5s (pulse.tick_interval_ms setting, default 5000ms
via `/admin/settings`). SSE ticks patch LTP in real-time between polls for open-market
sessions.

### `/api/quote/batch` [backend/api/routes/quote.py](backend/api/routes/quote.py#L716)

**Market open:**
1. `broker.quote(keys)` → fresh OHLC/volume/OI/LTP
2. `record_good_ltp()` (1h TTL) + `record_good_quote()` (24h TTL) → LKG caches
3. Return BatchQuoteRow(stale=False)

**Market closed:**
1. `_maybe_warm_closed_hours_quotes()` — one-shot broker.quote() per day per key-set
   signature; deduped; 60s cool-off on failure
2. `get_last_good_ltp()` + `get_last_good_quote()` → LKG caches
3. Return BatchQuoteRow(stale=True, as_of=now)

Frontend shows em-dash / "as of HH:MM" hint when stale=True.

### SSE stream (/api/quotes/stream)

[backend/api/routes/quote.py](backend/api/routes/quote.py#L1777):

- On connect: `snapshot` event (all currently-held ticks, {token: {ltp, sym}})
- Per tick: `tick` event ({tok, sym, ltp, ts})
- 30s heartbeat when no ticks

### Other quote intervals

| Component | Interval | Mechanism |
|---|---|---|
| NavStrip (P/H) | 30s | marketAwareInterval, pauses when tab hidden |
| NAV Card | 60s | visibleInterval |
| MarketPulse | 5s | visibleInterval, SSE-assisted for LTP |
| Book poller (holdings/positions/funds) | 30s | visibleInterval |
| Connection status | 15s | visibleInterval |

---

## §9 — MCX vs Equity Differences

| Aspect | NSE / BSE / NFO (Equity + Derivatives) | MCX (Commodity) |
|---|---|---|
| **Session window** | NSE: 09:15–15:30 IST; NFO: 09:15–15:30 | 09:00–23:30 IST (evening session) |
| **Quantity unit (broker)** | Contracts (NSE) / Lots (NFO) | LOTS (overnight_qty, day_buy_qty, day_sell_qty in lots; prices per unit) |
| **Multiplier** | 1 for NSE/BSE; lot_size for NFO | lot_size (e.g., 100 barrels for CRUDEOIL) |
| **daily_book qty stored** | As-is from broker (contracts or lots) | LOTS (same as broker) |
| **Snapshot read effective_qty** | qty × multiplier [positions_helpers.py:313](backend/api/helpers/positions_helpers.py#L313) | qty × multiplier |
| **Day P&L formula** | oq×(ltp−cls) + intraday legs | oq×m×(ltp−cls) + (bq×m×ltp−bv) + (sv−sq×m×ltp) where m=multiplier |
| **NSE settlement snapshot** | 16:15 IST daily | N/A |
| **MCX settlement snapshot** | N/A | 00:15 IST (calendar +1 day, trade-date = yesterday) |
| **Mid-session guard** | ltp/day_pnl NULL if exchange open at snapshot time | ltp/day_pnl NULL if MCX open (prevents mid-evening restart poisoning) |
| **Stale-close window** | Minimal (NSE close_price current by 09:15) | 23:30–09:00 IST: close_price lags; Case 4 guard returns 0 when close≤0 |
| **NavStrip P-pill** | NSE/BSE CNC/MIS excluded (FO_EXCHANGES filter) | MCX included in P-pill (F&O exchange) |
| **Pulse underlying** | Nearest-month future for index options | Same-month future for commodity options (CRUDEOIL26JUNFUT for June options) |
| **Snapshot 7-day lookback** | Covers weekends | Covers MCX holiday calendar + MCX-only trading days when NSE is closed |

---

## §10 — Master Timing Table (IST, midnight to midnight)

| Time (IST) | Event | Component | Operator impact |
|---|---|---|---|
| 00:00 | Calendar rollover | snapshot_gate | today_ist_midnight boundary updates; prev_batch CTE window shifts |
| 00:15 | MCX settlement snapshot | `_task_daily_snapshot` [background.py:1822](backend/api/background.py#L1822) | daily_book rows for MCX symbols written; trade-date = yesterday |
| 00:30 | Sparkline warm (midnight) | `_task_sparkline_warm` [background.py:3224](backend/api/background.py#L3224) | ohlcv_store + intraday_store pre-filled; SSE universe tokens registered |
| 09:00 | MCX market open | segment gate | MCX positions/quotes go live; closed_hours_or_broker() serves broker |
| 09:00 | MCX sparkline warm | `_task_sparkline_warm` | MCX commodity universe re-registered with ticker |
| 09:15 | NSE market open | segment gate | NSE/NFO positions/holdings/quotes go live |
| 09:15 | NSE sparkline warm | `_task_sparkline_warm` | NSE equity universe re-registered; previous_close frozen in daily_book |
| ~09:16 | previous_close frozen | daily_book UPSERT COALESCE | First snapshot write; Kite close_price = yesterday's settlement is frozen |
| 15:30 | NSE/NFO market close | snapshot_gate | NSE/NFO positions/holdings route → snapshot path; MCX still live |
| 16:00 | NAV snapshot write | `_task_nav_compute` [background.py:1208](backend/api/background.py#L1208) | nav_daily row upserted; /api/nav/latest returns today's frozen NAV |
| 16:15 | NSE settlement snapshot | `_task_daily_snapshot` [background.py:1813](backend/api/background.py#L1813) | daily_book rows for NSE/NFO symbols written with settlement prices |
| 23:30 | MCX market close | snapshot_gate | MCX positions route → snapshot path; all markets closed |
| Continuous | mmap poll | MmapTickReader._poll_loop | 50ms cadence; LTP deltas published to SSE |
| Continuous | NavStrip poll | PositionStrip.svelte | 30s; positions + holdings fetched; P/H updated |
| Continuous | MarketPulse poll | MarketPulse.svelte | 5s; batchQuote for OHLC/volume; SSE patches LTP between polls |
| Continuous | NAV Card poll | NavCard.svelte | 60s; /api/nav/latest DB read |
| Per tick | SSE tick event | /api/quotes/stream | Real-time LTP push to all connected browser tabs |

---

## §11 — Cache Layer Reference

| Cache | Location | TTL | Populated by | Read by | Off-market |
|---|---|---|---|---|---|
| `_tick_map[tok]` | conn_service in-process dict | Lifetime of conn_service | KiteTicker._on_ticks | get_ltp(), SSE snapshot | Stale; LKG caches used instead |
| `_tick_age[tok]` | conn_service in-process dict | Lifetime of conn_service | KiteTicker._on_ticks | Health watchdog stale_count | Stale; no action taken |
| `/dev/shm/ramboq_ticks` | Shared RAM (mmap) | Lifetime of mmap file | TickBufferWriter.upsert | MmapTickReader._poll_loop | Stale but readable; 50ms poller stops publishing deltas |
| `_LAST_GOOD_LTP` | API process in-process dict | 1 hour | record_good_ltp() on live broker.quote() | batch_quote closed-hours path | Primary LTP source during closed hours |
| `_LAST_GOOD_QUOTE` | API process in-process dict | 24 hours | record_good_quote() on live broker.quote() | batch_quote closed-hours path | Primary OHLC/volume/OI source |
| Snapshot LTP map | In-process dict (30s TTL) | 30s | latest_snapshot_ltp_map() [snapshot_gate.py:166](backend/api/helpers/snapshot_gate.py#L166) | _overlay_snapshot_for_closed_exchanges() | Used on live path to patch closed-exchange rows |
| `daily_book` table | PostgreSQL | Perpetual (idempotent upsert) | snapshot_daily_book() at 16:15/00:15 IST | _positions_snapshot(), _holdings_snapshot(), NAV | Primary data store for closed-hours positions/holdings |
| Stale-live cache | In-process dict (120s TTL) | 120s | Successful broker fetch [snapshot_gate.py:66](backend/api/helpers/snapshot_gate.py#L66) | closed_hours_or_broker() on broker failure | Returned as "stale-live" on transient outage; prevents live↔snapshot flicker |
| `nav_daily` table | PostgreSQL | 1 row per trade-date | write_nav_snapshot() at 16:00 IST | /api/nav/latest, /api/nav/me fallback | Only source for closed-hours NAV |
| `ohlcv_store` | In-process dict | Today (IST date key) | `_task_sparkline_warm` (past daily bars) | /api/quotes/sparkline | Pre-filled at 00:30/09:00/09:15 IST; stale but readable |
| `intraday_store` | In-process dict | Today (IST date key) | `_task_sparkline_warm` (30-min intraday bars) | /api/quotes/sparkline | Pre-filled at market opens; closed hours use yesterday's bars |
| Token map | In-process dict (IST day) | Calendar day | `_get_today_token_map()` → broker.instruments() | subscribe_with_sym(), batch_quote | Stable once populated; refreshes at midnight IST rollover |
| `daily_book` sparkline rows | PostgreSQL `daily_book` (kind='sparkline') | Perpetual (idempotent upsert) | `snapshot_sparkline()` at close + close_settled | `/api/quotes/sparkline` closed-hours fallback | Primary fallback for closed-hours sparklines |
| `sparklinesStore` | Frontend in-memory (Svelte store) | 1 day | `/api/quotes/sparkline` response | MarketPulse.svelte sparkline cells | Stale-while-valid; per-symbol variation-based merge |

---

## §12 — Sparkline Lifecycle

### 12.1 Overview

Sparklines are a tri-layer persistence system:
1. **Write path** — `snapshot_sparkline()` persists 5-day close-bar series to `daily_book` (kind='sparkline') on market close and close_settled events.
2. **Store caches** — `ohlcv_store` (daily bars, 3-tier: memory→DB→broker) and `intraday_store` (30-min bars, 3-tier: memory 5-min TTL→DB→broker) pre-warmed at startup and market-open boundaries.
3. **Frontend cache** — `sparklinesStore` (1-day TTL, chunked 100 symbols/request, stale-while-valid).

### 12.2 Write Path

**Trigger** — `market_lifecycle_handlers.py:90`:
- `<exch>:close` event (NSE 15:30 IST, MCX 23:30 IST) → `snapshot_sparkline(settled=False)`
- `<exch>:close_settled` event (~15 min later) → `snapshot_sparkline(settled=True)` re-upserts with broker's adjusted settlement close

**Symbol universe** (`daily_snapshot.py:945`, cap 500):
1. All `watchlist_items` (tradingsymbol + exchange)
2. Live holdings (equity)
3. Live positions (F&O + commodity)
4. `daily_book` 7-day backstop (stale accounts)
5. Mover universe (NSE indices + F&O largecap)

Virtual MCX/CDS roots resolved to front-month contracts via `resolve_virtual_roots()`.

**Bar series** — last 10 calendar days of daily closes from `ohlcv_store` via `get_or_fetch_daily()` (`daily_snapshot.py:997`). Converted to `[{"t": "YYYY-MM-DD", "ltp": float}]` oldest-first by `_snap_bars_to_points()` (`daily_snapshot.py:926`).

**`daily_book` row** (kind='sparkline'):
```
account:      "__firm__"   (market-wide sentinel — not per-account)
ltp:          points[-1]["ltp"]
payload_json: {"points": [{"t": "YYYY-MM-DD", "ltp": float}, ...], "settled": bool, "captured_at": <iso>}
```

### 12.3 Store Caches (Backend)

**`ohlcv_store`** (`backend/api/persistence/ohlcv_store.py`):
- Tier 1: in-process memory dict — no TTL (persistent until process restart)
- Tier 2: PostgreSQL `ohlcv_daily` table — permanent
- Tier 3: broker `historical_data()` on Tier 2 miss → write-back to Tier 1 only
- Gap rule: gaps ≤ 6 calendar days treated as market closure (no re-fetch)

**`intraday_store`** (`backend/api/persistence/intraday_store.py`):
- Tier 1: in-process LRU dict — **5-min TTL for today's bars** (`_TODAY_TTL_S = 300`); historical bars have no TTL
- Tier 2: PostgreSQL `intraday_bars` table — permanent
- Tier 3: broker `historical_data()` (round-robin via `get_historical_brokers()`)
- Today's bars are partial during session; historical must span session-close hour

### 12.4 Sparkline Warm

`_task_sparkline_warm()` (`background.py:3224`) fires at: service startup · 00:30 IST · 09:00 IST (MCX open) · 09:15 IST (NSE open).

Steps:
1. Build symbol universe (5 sources above; book symbols always included, movers fill up to 300-symbol ceiling)
2. `warm_sparkline_cache(symbols, days=5)` (`quote.py:1960`) — parallel fetch of daily bars → `ohlcv_store` + 30-min intraday bars → `intraday_store`
3. Seed KiteTicker token-to-symbol map early (`_ticker_seed_early()`) so SSE ticks have valid `sym` before historical fetch completes
4. Register all universe tokens with `subscribe_with_sym()`

**300-symbol cap**: book symbols (watchlist + holdings + positions + snapshot backstop) are never truncated; movers fill remaining slots up to 300.

### 12.5 Serving Sparklines

**Route:** `POST /api/quotes/sparkline` (`quote.py:1701`)  
**Request:** `{"symbols": [{"tradingsymbol": str, "exchange": str}], "days": int}` (days 1–90, default 5)  
**Response:** `{"data": {"SYM": [close1, ..., ltpTail]}, "refreshed_at": ISO, "as_of": ISO|null}`

**Market open** (4-stage pipeline):
1. Fetch past daily closes (days-1) from `ohlcv_store` + today's 30-min bars from `intraday_store` in parallel
2. Build token map, subscribe to ticker
3. LTP from ticker tick-map (zero Kite quota); fallback to `broker.ltp()` on miss
4. Compose final series via `compose_sparkline_series()`; `stale=False`

**Market closed** (`db_only=True` when all exchanges closed):
- No broker calls; Tier 1+2 only
- Fallback to `daily_book` sparkline rows via `_fill_from_daily_book_sparkline()` (`quote.py:1693`)
- `as_of` timestamp set in response; operator sees stale-data label

### 12.6 Frontend Cache

**`sparklinesStore`** (`frontend/src/lib/data/marketDataStores.svelte.js:610`):
- TTL: 1 day
- Chunked: 100 symbols per request
- Stale-while-valid: keeps cached version on empty response (`keepStaleOnEmpty: true`)
- Per-symbol merge: prefers fresh if it has variation, else keeps cached (`_mergeSparkSeries()`)

**Consumers:** MarketPulse.svelte — positions, holdings, movers, and watchlist grids all render sparkline cells via AG-Grid.

**Fetch trigger:** `sparklinesStore.load(pairs)` (`MarketPulse.svelte:1295`) on universe change; batched on demand, not polled.

**Data shape:** Array of finite numbers oldest→newest (length ≥ 2). Dev-mode validated by `assertSparklineSeries()` (`sparklineShape.js:38`).

### 12.7 Sparkline Timing Table

| Time (IST) | Event | Component | Operator impact |
|---|---|---|---|
| 00:30 | Midnight warm | `_task_sparkline_warm` | `ohlcv_store` + `intraday_store` pre-filled; universe tokens registered |
| 09:00 | MCX open warm | `_task_sparkline_warm` | MCX commodity universe added |
| 09:15 | NSE open warm | `_task_sparkline_warm` | NSE equity universe added; sparkline cache ready before first request |
| 15:30 | NSE close | `snapshot_sparkline(settled=False)` | 5-day bar series written to `daily_book`; closed-hours sparklines served from DB |
| ~15:45 | NSE close_settled | `snapshot_sparkline(settled=True)` | Re-upsert with adjusted settlement close; `settled=true` |
| 23:30 | MCX close | `snapshot_sparkline` (both passes) | MCX commodity sparklines written |
| On demand | Frontend grid load | `sparklinesStore.load()` | 100-symbol chunks; 1-day TTL frontend cache |

---

## Changelog

| Date | Version | Change |
|---|---|---|
| 2026-08-09 | v1.4 | Holdings snapshot prev_batch CTE + MCX lot-scale day P&L — §4.4 updated with prev_batch 7-day lookback finding most-recent prior-day LTP per (account, symbol); `_build_holding_row_from_snapshot()` computes `day_change_val = (ltp - prev_ltp) × qty` when `prev_ltp > 0`, matching positions closed-hours pattern; fallback to `(ltp - previous_close) × qty` when `prev_ltp` unavailable/zero |
| 2026-08-11 | v1.5 | Positions close-price priority fix + pulseHoldingsStore in book poller — §4.4 updated: `build_row_from_snapshot_raw` now prefers `previous_close` (frozen settlement) over `prev_ltp` (recent batch LTP) for `computed_day_pnl`, fixing positions showing day_change_val ≈ 0 after market close when daily_book had multiple intraday captures. Frontend: `_tickBookPollers()` now includes `pulseHoldingsStore.load()` so NavStrip H:1 stays in sync with Pulse Holdings TOTAL during closed hours |
