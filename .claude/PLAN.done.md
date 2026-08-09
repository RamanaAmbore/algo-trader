# Plan: docs: DATA_LIFECYCLE.md — tick, positions, holdings, pulse refresh lifecycle

## Context

Operators and developers need a single reference that answers "what am I seeing and why?"
for every data surface (tick LTP, positions P&L, holdings, NavStrip, MarketPulse) at any
point in the market day — including MCX evening session, NSE-only hours, and closed hours.
This doc does not exist today; ORDER_LIFECYCLE.md covers orders but nothing covers data.

## Task

Create `docs/audits/DATA_LIFECYCLE.md` covering the full lifecycle of:
1. **Tick data** — KiteTicker → mmap → SSE → frontend LTP display
2. **Positions** — live fetch vs snapshot path (equity + MCX, open + closed hours)
3. **Holdings** — live fetch vs snapshot path
4. **NAV / NavStrip / MarketPulse** — computation, caching, polling intervals

Structure (10 sections):
- §1  Overview diagram (ASCII, shows all 4 data surfaces + their refresh cadences)
- §2  Tick data lifecycle (KiteTicker → mmap write → mmap read → SSE stream → frontend)
- §3  Positions lifecycle — live path (live hours: equity NSE 09:15–15:30, MCX 09:00–23:30)
- §4  Positions lifecycle — snapshot path (closed hours → daily_book → route response)
- §5  Holdings lifecycle — live path + snapshot path + prev_ltp day-pnl resolution
- §6  NAV computation + refresh (compute_firm_nav formula, background task schedule)
- §7  NavStrip 4 slots (P/H/F/O: data sources, formulas, MCX vs equity filter rules)
- §8  MarketPulse + quote refresh (batch_quote live vs LKG closed-hours, polling intervals)
- §9  MCX vs Equity differences (quantities in lots vs contracts, session windows, day-pnl)
- §10 Master timing table (all events ordered by IST clock from midnight to midnight)
- §11 Cache layer reference (what's cached, TTL, eviction trigger)

## Agents

- doc: Create `docs/audits/DATA_LIFECYCLE.md` using all details below.

  **§1 — Overview**
  ASCII table showing 4 surfaces (Tick / Positions / Holdings / NAV+Pulse) × 3 states
  (NSE+MCX live / NSE closed MCX live / all closed). One row per surface, one column per
  market state, cell = data source + refresh cadence.

  **§2 — Tick Data Lifecycle**
  Path: Kite WS → KiteTicker._on_ticks (kite_ticker.py:995) → zero-LTP guard → _tick_map +
  _tick_age → TickBufferWriter.upsert → /dev/shm/ramboq_ticks (40-byte slot per token:
  token, last_price, prev_close, avg_price, last_ts_ns) → MmapTickReader._poll_loop (50ms)
  → local BroadcastBus → SSE /api/quotes/stream (tick event: {tok, sym, ltp, ts}).
  Subscription: subscribe_with_sym(token, sym) → mmap → conn_service UDS route
  /internal/ticker/subscribe. Sparkline warm fires at startup + 09:00/09:15/00:30 IST to
  register universe tokens. Off-market: WebSocket stays open; no ticks land; LKG caches serve
  quote/batch. Reconnect: max 50 tries, exponential back-off 1→2→4→...→30s.

  **§3 — Positions Live Path**
  Entry: GET /api/positions → closed_hours_or_broker() gate (snapshot_gate.py:220).
  When open: fetch_positions() per account via UDS → Kite REST → DataFrame with
  quantity, average_price, last_price, close_price, pnl, day_change, overnight_quantity,
  multiplier → backfill_market_data() for zero-LTP rows (Dhan/Groww adapters) →
  _override_stale_ltp_from_ticker() (patch LTP from KiteTicker tick_map) →
  _patch_raw_positions(): _override_stale_close_from_snapshot() (prior-session close from
  daily_book) + apply_day_change_backstop() (Case 1: oq=0 new position; Case 3: qty=0 flat).
  → Polars vectorised enrichment → _enrich_position_greeks() (IV/delta/theta for options) →
  _overlay_snapshot_for_closed_exchanges() (per-exchange: closed exchange rows patched with
  snapshot LTP from latest_snapshot_ltp_map()) → build_summary_from_rows() → response.
  Anti-flicker stale-live cache (120s TTL): on broker failure returns last cached live response.

  **§4 — Positions Snapshot Path**
  When closed: _positions_snapshot() queries daily_book.
  SQL structure: latest_batch CTE (MAX captured_at per account, kind='positions') +
  prev_batch CTE (DISTINCT ON account/symbol, most-recent prior-day row within 7 days,
  before today 00:00 IST) → JOIN to get prev_ltp + prev_settlement_pnl.
  Columns returned: account, symbol, exchange, qty (LOTS for MCX), avg_cost, ltp,
  day_pnl (NULL mid-session), total_pnl, payload_json (full broker row + snapshot_extras),
  captured_at, previous_close (frozen), prev_ltp, prev_settlement_pnl.
  Row reconstruction (build_row_from_snapshot_raw, positions_helpers.py:298):
  effective_qty = qty × multiplier; close_price = prev_ltp ?? previous_close ?? ltp;
  day_pnl = (ltp - close_price) × effective_qty (or snapshot_extras.day_change_val if
  day_pnl col is NULL). Tags: price_source="snapshot_settled", last_price_stale=True,
  as_of=captured_at. Flat-row hygiene: qty=0 rows zeroed day_change_val.

  **§5 — Holdings Lifecycle**
  Live path: GET /api/holdings → closed_hours_or_broker() → fetch_holdings() → DataFrame
  with tradingsymbol, quantity, average_price, last_price, pnl, day_change, cur_val.
  Snapshot path: _holdings_snapshot() → _HOLDINGS_SNAPSHOT_SQL (holdings.py:40) same
  latest_batch + prev_batch CTE pattern (kind='holdings', 7-day lookback).
  _build_holding_row_from_snapshot() (holdings.py:100): day_change_val priority:
  (1) (ltp - prev_ltp) × qty if prev_ltp > 0; (2) (ltp - previous_close) × qty if > 0;
  (3) stored day_pnl. cur_val = ltp × qty when snapshot LTP used.
  NAV uses _fetch_holdings_from_snapshot() (nav.py:316) for closed NSE hours — not live
  broker — to avoid stale/zero holdings in NAV when NSE closed.

  **§6 — NAV Computation + Refresh**
  compute_firm_nav() (nav.py:357): NAV = cash_total + positions_mtm + holdings_mtm.
  cash_total = Σ(margin.opening_balance + margin.option_premium) per account.
  positions_mtm = Σ(position.pnl where qty≠0). holdings_mtm = Σ(holding.cur_val).
  Holdings source: live when NSE open, daily_book snapshot when NSE closed.
  Persisted: _task_nav_compute() fires at 16:00 IST daily → write_nav_snapshot() → upserts
  nav_daily row (idempotent by as_of_date). NAV route /api/nav/latest: DB read (no live
  compute). /api/nav/compute (POST): operator-triggered → live compute + upsert.

  **§7 — NavStrip 4 Slots**
  PositionStrip.svelte + nav.js. Poll: marketAwareInterval 30s (pauses when tab hidden).
  P slot (Positions): day P&L = Σ baseDayPnlForPosition(p) over ALL positions (NSE+MCX+F&O).
    baseDayPnlForPosition logic: if oq≠0 and pnl≠0 and close>0: pnl - oq×(close-avg_price).
    Case 1 guard: oq=0 and pnl≠0 → return pnl (new intraday position, Kite sets day_change=0).
    Case 4 guard: close≤0 → return 0 (stale close window, typically 23:30–09:00 IST for MCX).
    Short fix (1769cffc): guard oq≠0 (not oq>0) so short overnight positions also hit fast-path.
  P-pill (filtered): F&O only, FO_EXCHANGES={NFO, MCX, BFO} — excludes NSE/BSE CNC/MIS to
    avoid double-counting with H-pill. positionsPnlFiltered() → nav.js (exported named).
  H slot (Holdings): Σ holding.day_change_val (broker day-change) + live SSE delta patches.
  Lifetime P slot: Σ position.pnl (no SSE delta — broker snapshot). Lifetime H: Σ holding.pnl.
  Off-market: NavStrip polls positions/holdings which serve snapshots → P/H frozen at last
  settlement values. P-pill reads last stored day_change_val.

  **§8 — MarketPulse + Quote Refresh**
  MarketPulse.svelte: poll every 5s (pulse.tick_interval_ms setting, default 5000ms).
  batchQuote(keys) → POST /api/quote/batch. Open market: broker.quote() → fresh OHLC/LTP →
  LKG recorded → stale=false rows. SSE ticks patch LTP in real-time between polls.
  Closed market: _serve_closed_hours_batch() → _maybe_warm_closed_hours_quotes() one-shot
  broker.quote() per day per key-set (deduped by sig + date; 60s cool-off on failure) →
  get_last_good_ltp() (1h TTL) + get_last_good_quote() (24h TTL) → stale=true rows with
  as_of timestamp. Frontend displays em-dash / "as of HH:MM" hint.
  Quote SSE (/api/quotes/stream): snapshot on connect (all active ticks), then per-tick
  events {tok, sym, ltp, ts}. Heartbeat every 30s when no ticks.

  **§9 — MCX vs Equity Differences**
  Table: quantity units / session window / snapshot timing / day P&L formula /
  NavStrip slot / underlying resolver.
  MCX qty: broker ships overnight_qty, day_buy_qty, day_sell_qty in LOTS; prices per-unit.
  Multiplier: stored in payload_json; at snapshot read effective_qty = qty × multiplier.
  Day P&L formula with multiplier (daily_snapshot.py:481):
    full decomposed: oq×m×(ltp−cls) + (bq×m×ltp−bv) + (sv−sq×m×ltp)
    simple fallback: (ltp−cls) × qty × multiplier
  Session: MCX 09:00–23:30 IST; NSE 09:15–15:30 IST.
  Snapshot timing: NSE settlement snapshot at 16:15 IST; MCX at 00:15 IST (+1 calendar day).
  MCX mid-session guard: snapshot writes ltp/day_pnl = NULL when MCX open, to prevent
  mid-evening data poisoning if service restarts.
  MCX underlying (Pulse): CRUDEOIL26JUN10500CE → resolveUnderlyingForOption() → same-month
  future CRUDEOIL26JUNFUT (not spot price).
  MCX stale-close window (23:30–09:00 IST): close_price lags; baseDayPnlForPosition
  Case 4 returns 0 when close≤0 to avoid ±₹5L distortion for short MCX overnight positions.

  **§10 — Master Timing Table**
  Table ordered by IST time (midnight to next midnight), listing: time, event, component,
  what changes for the operator. Include: 00:15 MCX snapshot, 00:30 sparkline warm,
  09:00 MCX open + warm, 09:15 NSE open + snapshot frozen, 15:30 NSE close → snapshot path,
  16:00 NAV snapshot write, 16:15 NSE settlement snapshot, 23:30 MCX close → snapshot path,
  continuous: 30s NavStrip poll, 5s Pulse poll, 50ms mmap poller, per-tick SSE.

  **§11 — Cache Layer Reference**
  Table: cache name, location, TTL, what populates it, what reads it, off-market behavior.
  Rows: _tick_map (in-process, lifetime), mmap /dev/shm/ramboq_ticks (lifetime),
  _LAST_GOOD_LTP (1h), _LAST_GOOD_QUOTE (24h), snapshot LTP map (30s TTL),
  daily_book table (perpetual, idempotent upsert), stale-live cache (120s),
  nav_daily table (1 row/day), ohlcv_store / intraday_store (today-only).

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

docs: DATA_LIFECYCLE.md — tick, positions, holdings, NAV/pulse lifecycle (equity + MCX, live + closed)

## Done when

- `docs/audits/DATA_LIFECYCLE.md` exists with all 11 sections
- All timing, file:line references, MCX vs equity differences, and cache layers are documented
- No existing docs modified (standalone new file)
