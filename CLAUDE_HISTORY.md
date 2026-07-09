# RamboQuant — Historical Notes

Archival content extracted from CLAUDE.md. Load this file only when investigating
"why a thing was done a particular way" or tracing a past refactor. Not autoloaded.

CLAUDE.md has been compressed (Jul 2026) to focus on active architecture references. 
This file retains detailed implementation notes, comprehensive subsection prose, and 
multi-wave refactoring walkthroughs. Cross-link via CLAUDE.md when needing deep-dive context.

---

## Index

- Persistence pipeline maturity + chart self-heal + NavStrip rework (Jun 2026)
- Closed-hours route gate + broker auth health badge (Jun 2026)
- Broker isolation (slices 1–4)
- Frontend perf budgets + audit (Jul 2026)
- Visibility-aware polling (Option A, Jun 2026)
- Code metrics tracking (Phase 1, Jun 2026)
- Performance tuning (multi-wave)
- Data-layer hardening
- Refactoring notes

---

## Persistence pipeline maturity + chart self-heal + NavStrip rework (Jun 2026)

**Sprint H scope**: Three orthogonal improvements shipped together.

**Slice 1 — Persistence self-heal layer**
Instruments snapshot string-date bug (`4e09d6fa`) poisoned every batch upsert,
causing intraday_bars table to be empty. Fixed via `_parse_date` helper +
per-kind isolation in `_upsert` (one bad row no longer poisons the batch).
Added `db_only` mode: per-store flag skips Tier 3 (broker) during closed hours.
Frontend sparkline refresh uses `db_only=True` when all segments closed
(low-priority 5-min poll).

Chart self-heal (`/api/options/historical`): detects under-coverage (<70% of
requested days present in DB) and auto-fetches from broker when ≥1 broker
available. Response carries `partial: bool` so frontend can hint "partial data".
Cool-off aware; skips broker during rate-limit window. Logs one healing per
symbol per 60s to avoid spam. Coverage threshold tunable via `/admin/settings`.

Coverage backfill module (`backend/api/persistence/backfill.py`):
`backfill_ohlcv_daily(symbols, target_days=365)` force-fetches 365-day window
for symbols with <70% coverage; skips broker in cooloff. `backfill_intraday_today(symbols, interval="30minute")` force-fetches today's bars; defers when markets closed. Startup hook `_task_warm_backfill` (60 s delay, once per process)
fires both for the 300-symbol universe. On-demand: `POST /api/admin/persistence/backfill?kind=daily|intraday|both` (admin-guarded). CLI:
`scripts/persistence_mode.py off|soft|hard|status` + `scripts/backfill_ohlcv.py --daily --intraday` for immediate prod fix.

**Slice 2 — NavStrip pill rework**
P pill (P&L): `<today>/<lifetime>` (was `<lifetime>/<today>`). M pill
(Margin): `<avail>/<total>` (was `<avail>/<util%>`). C pill (Cash):
`<avail>/<total>` (was `<total>/<avail>`). H pill (Holdings):
`<today>/<value>/<lifetime>` (was three separate pills H∆/HD∆/H standalone).
Snapshot SSOT replaces the localStorage `strip.frozen` cache — during closed
hours `dispPositionsToday` = `_livePositionsToday` = Σ snapshot.day_pnl. Cache
write retained for in-session reload. Latest-batch CTE in `positions.py` +
`holdings.py` snapshot readers anchors on `MAX(captured_at) per account` — no
more stale months-old rows for closed positions.

**Slice 3 — UX consolidation**
Broker AUTH badge folded into the 5/5 broker-chip. Single navbar entry point.
`BrokerHealthBadge.svelte` is modal-only, `bind:open` driven, polls every 30s
ONLY while modal is open (avoids 120 req/hr background load). "as of
<timestamp>" hint removed from Winners/Losers headers. "PREV" → "CLOSE" label
on OptionsPayoff overlay for terminology consistency. Legs grid Day P&L for
expired options now displays Exp P&L (intrinsic-based settlement value) instead
of standard `(LTP − prev_close) × qty`. New helpers `_isLegExpired(c)` +
`_dayPnlForLeg(c, spot)`. Applies to settled options on expiry day so the bulk
realization is visible in Day P&L instead of silently sloshing from "unrealized
lifetime" to "realized lifetime."

RefreshButton behaviour during closed hours: click → toast "Showing close
snapshot — markets reopen at HH:MM IST" + parent's load() runs (snapshot path =
fast DB read, no broker round-trip). Spin animates throughout (reverted earlier
closed-hours suppression — operator wants visible feedback for the real async
work).

---

## Closed-hours route gate + broker auth health badge (Jun 2026)

**Sprint F+ slice**: Two architectural pieces shipped together.

**Slice 1 — `closed_hours_or_broker` canonical gate**
Positions.py and holdings.py each had a hand-rolled `_is_all_markets_closed()` async
helper that was duplicated between files. Extracted into `backend/api/helpers/snapshot_gate.py`
as `closed_hours_or_broker(exchange, snapshot_fn, broker_fn, *, fallback_to_snapshot_on_broker_error)`.
Primary invariant: broker_fn is NEVER called during closed hours. Source tags
(`'live'` / `'snapshot'` / `'snapshot-fallback'`) let callers log and surface "as of"
timestamps. Inner `_any_segment_open()` predicate is what unit tests patch.

Routes NOT migrated (intentional exemptions):
- `quote.py` — has a per-exchange `_all_exchanges_closed` helper with different granularity (correct as-is)
- `options.py` — the closed guard at line ~2120 is an intraday sub-call guard, not a route-level gate
- `funds.py`, `market.py`, `simulator.py`, `watchlist.py` — different semantics

Test files updated: `test_snapshot_gate.py` (14 new tests, all passing), `test_closed_hours_snapshot_routes.py` (patches updated from route-level to `snapshot_gate._any_segment_open`), `test_day_change_closed_hours.py` (static asserts + functional patches updated).

**Slice 2 — Broker auth health badge**
New endpoint `GET /api/admin/broker-health` (admin-guarded, in `health.py`). Per-account
health state derived from `_FETCH_HEALTH` / `fetch_health_snapshot()` plumbing already
in broker_apis.py. State logic: green = last_good within 5 min and no recent fail;
amber = healthy but stale OR never tried; red = last_fail > last_ok. Sorted red → amber → green.

Frontend `BrokerHealthBadge.svelte` — polls 30s via `visibleInterval`, worst-state drives
badge colour, click opens per-account modal. Mounted in desktop + mobile navbar sections
behind `role === 'admin' || role === 'designated'` guard. The existing broker-count
chip (connStatus 5/5) is a separate signal and was intentionally left unchanged.

**Why two separate helpers in the navbar**: Count chip = "how many accounts loaded?";
health badge = "did the last actual API call succeed?". Different operational questions.

---

## Broker isolation (slices 1–4)

**Architecture**: Broker code isolated in `backend/brokers/` with separate
systemd service (`ramboq_conn.service`) that owns all sessions (Kite WebSocket,
Dhan/Groww tokens). Main API opts in via `RAMBOQ_USE_CONN_SERVICE=1` env flag.

**Four slices**:

1. **File reorganization** — `backend/shared/brokers/` → `backend/brokers/adapters/`;
   `backend/shared/helpers/{connections,broker_apis,kite_ticker}.py` → `backend/brokers/`;
   `backend/conn_service/` → `backend/brokers/service/`; `backend/conn_client/` →
   `backend/brokers/client/`.

2. **Separate UDS service** — `/etc/systemd/system/ramboq_conn.service` (Litestar
   app on `/tmp/ramboq_conn.sock`) owns broker lifecycle. Restart independent of
   ramboq_api. Implements `/health`, `/rebuild`, `/ticker/status`, `/ticker/subscribe`,
   `/postback` endpoints. HMAC-protected for postback route.

3. **Main API changes**: When `RAMBOQ_USE_CONN_SERVICE=1`:
   - `Connections.rebuild_from_db()` skips local logins; returns RemoteBroker stubs.
   - `registry.get_broker(account)` proxies calls over UDS via `broker_client`.
   - `broker_apis.fetch_holdings/positions/margins` → `@for_all_accounts` still works;
     internally routes through RemoteBroker proxies.
   - `get_ticker()` returns `MmapTickReader` instead of `TickerManager` (reads
     `/dev/shm/ramboq_ticks` at byte-read latency).
   - Postback HMAC verification stays in conn_service (api_secret never leaves).

4. **Shared-memory ticks** — KiteTicker writes to `/dev/shm/ramboq_ticks`
   (fixed 4096 slots, version-word atomic for lock-free reads). Background poller
   (50ms) tails version word, publishes deltas to local BroadcastBus. SSE clients
   unaffected; frontend subscriptions still work.

**Dev setup**: `ramboq_dev_api` uses same `/tmp/ramboq_conn.sock` as prod.
No parallel Dhan logins (avoids single-IP-token-per-app limits). Set
`RAMBOQ_USE_CONN_SERVICE=1` on both services via drop-in config files
(`webhook/ramboq_api.service.d-conn.conf` and `webhook/ramboq_dev_api.service.d-conn.conf`).

**Deploy integration**: `deploy.sh` sets `CONN_TOUCHED=true` only when files under
`backend/brokers/` or `webhook/ramboq_conn.service` change. Otherwise conn_service
stays warm across API restarts. Frontend-only pushes touch neither service.

---

## Frontend perf budgets + audit (Jul 2026)

Comprehensive frontend perf audit closed three systemic regression classes
flagged by repeated operator complaints ("dropdown lag", "refresh button
stuck"). Patches at `frontend/src/lib/ws.js`,
`frontend/src/lib/RefreshButton.svelte`,
`frontend/src/lib/CollapseButton.svelte`,
`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`. Guards in
`frontend/e2e/main_thread_perf.spec.js`.

**Subscription leak hygiene** — RefreshButton + CollapseButton used to
`.subscribe()` Svelte stores at module-top-level with no unsub pair.
With 1-3 RefreshButtons + dozens of CollapseButtons per algo page and
route transitions never destroying them, the listener lists grew
unbounded. Every conn-status poll (15 s) + every `lastRefreshAt.set`
fan-out paid for N dead consumers. Bind subscribes inside onMount + tear
them down in onDestroy. **Audit rule**: any `.subscribe()` call outside
a singleton module MUST have a paired unsub in the component's
onDestroy.

**Singleton WebSocket pool** — `createPerformanceSocket` / `createAlgoSocket`
previously opened a fresh `new WebSocket()` per call. Algo pages
routinely had 3-5 parallel `/ws/performance` connections, each with its
own 25 s heartbeat ping. Replaced with a ref-counted singleton subscriber
pool — one socket per endpoint, fan-out to all callers, auto-close when
the last subscriber unsubs. **Operator-facing contract preserved**: each
caller still gets an `unsub` function from the same factory.

**Dropdown click-to-feedback** — Select pick on `/admin/derivatives`
fired `goto({replaceState:true})` synchronously inside the `$effect`
watching `selectedUnderlying`. goto() walks the route tree and fires
nav lifecycle hooks even on a same-route replace, costing 200-700 ms.
Now debounced to 150 ms so a flurry of picks queues a single goto.

**RefreshButton click defer** — Click handler queues the parent
`onClick` via `queueMicrotask`. The button's disabled / spinner state
paints BEFORE the parent's onClick body begins synchronous bookkeeping
(Promise.allSettled wiring, legsKey signature compute, etc.). Operator:
"refresh button getting stuck still is an issue" — the stall was the
gap between click and next paint when the parent's onClick body did
tens of ms of work before its first await.

**Perf budgets** (assertions in main_thread_perf.spec.js):

| Dimension | Budget | Guard |
|---|---|---|
| Max long-task during interaction | <100 ms | RAIL |
| Click-to-feedback latency | <350 ms | Per-page |
| JS heap growth idle | <5 MB/min | Per-page leak check |
| `/ws/performance` connections per tab | ≤2 | WS singleton |
| Dropdown pick → panel-close | <400 ms | Derivatives Select |
| Dropdown pick max long-task | <150 ms | Derivatives Select |
| Cross-page nav heap growth (5-page lap) | <8 MB | Subscription-leak guard |
| Cross-page nav new-WS-opens (2nd lap) | ≤2 | WS pool reuse |

---

## Visibility-aware polling (Option A, Jun 2026)

**Design (operator-approved)**: ALL pollers + visual updates stop when
`document.visibilityState === 'hidden'`. WebSocket stays open (ref-counted
pool — closes only when last subscriber leaves) so `position_filled` /
`book_changed` events land. Telegram + email cover fills / agent events
during background periods. On tab return, every poller fires ONCE
immediately (within one event-loop tick) before resuming its normal cadence.

**Implementation** — `visibleInterval(fn, ms, mode = 'pause')` in `frontend/src/lib/stores.js`:
- `mode: 'pause'` (default) — clears the interval on hidden, restarts + fires `fn()` immediately on visible.
- `mode: 'throttle:<ms>'` — reduces cadence while hidden (future use, not active under Option A).
- `marketAwareInterval(fn, ms)` delegates to `visibleInterval` for the same behaviour inside the market-hours gate.

**Pollers converted to visibleInterval** (raw `setInterval` eliminated):
- `stores.js` — `nowStamp` 60 s clock + Intl format-cache 60 s purge
- `UnifiedLog.svelte` — 3 s data poll
- `LogPanel.svelte` — all tab pollers (agents / orders / system / conn / sim)
- `RefreshButton.svelte` — 30 s market-state tick (NSE/MCX session boundaries)
- `PositionStrip.svelte` — 30 s market-boundary watcher
- `PriceChart.svelte` — configurable price-history poll (was fully ungated)
- `SymbolPanel.svelte` — 3 s orders poll in bottom panel
- `market/+page.svelte` — 30 min market-summary + 10 min news polls

**Animations** — `createFreshnessShimmer.notify()` already guards `document.visibilityState === 'hidden'`. `createTickFlash` timers are 350 ms one-shots only triggered by pollers; pausing pollers prevents any new flash calls while hidden.

**WebSocket** — `ws.js` ref-counted pool closes the socket when the last subscriber leaves (page unmount). Tab background with page still mounted keeps the WS alive. Reconnects within 200 ms on tab return via the pool's existing backoff logic.

**Test guard** — `frontend/e2e/main_thread_perf.spec.js` `'visibility hibernation'` describe:
- Phase hidden 30 s → assert ZERO `/api/positions` + news requests.
- Phase visible → assert at least one immediate refire within 250 ms.
- Runs on both chromium-desktop + chromium-mobile.

---

## Code metrics tracking (Phase 1, Jun 2026)

Per-release codebase-health snapshot stored in `code_metrics_snapshots`
table. Eight metrics captured by `scripts/capture_metrics.py`:

| Metric | Tool | Backend / Frontend |
|---|---|---|
| Cyclomatic complexity | `radon cc -j` (Python) + ESLint `complexity` (JS) | Both |
| Lines of code | `radon raw -j` (Python) + recursive `wc -l` (frontend) | Both |
| Duplicated code | `jscpd` (frontend) | Frontend |
| Stale code | `vulture` (Python) + ESLint `no-unused-vars` (JS) | Both |
| Coverage | `pytest --cov` (backend, --with-coverage flag only) | Backend |
| Per-page latency | reads `/tmp/ramboq_perf.json` from main_thread_perf.spec.js | Frontend |
| Bug count | git-log heuristic since previous tag (`fix:|fix(|bug:|URGENT|P0`) | Both |
| Decoupling | DEFERRED to Phase 2 (needs import-graph build) | — |

**Capture invocation** — manual:
```
sudo -u www-data /opt/ramboq/venv/bin/python scripts/capture_metrics.py \
    --release-tag manual-2026-06-30
```

Auto from deploy: `webhook/deploy.sh` calls capture after a successful
non-frontend-only deploy, using `git describe` on main / `dev-<sha>`
on dev branches. `--force` overwrites a same-tag row in place (keeps
the trend chart clean across re-deploys).

**Route** — read-only at `/api/admin/code-metrics/*`
([backend/api/routes/metrics.py](backend/api/routes/metrics.py)):
- `GET /` — newest-first list, omits raw_payload for size
- `GET /trends?metric=<col>&limit=N` — chart-friendly time series
- `GET /{release_tag}` — single snapshot with raw_payload forensics

`_TREND_COLUMNS` allowlist gates the metric name → defends against
`getattr` injection. Writes are out-of-band only (script INSERTs;
route layer never constructs `CodeMetricsSnapshot(...)`).

**Admin page** — `/admin/metrics` shows 10 trend tiles (small SVG
line charts, cyan-400) + full snapshot table with per-row drill-in
modal exposing raw_payload + per_page_latency_ms JSON.

**Tools** — `radon` + `vulture` pinned in `requirements-api.txt`;
`jscpd` invoked via `npx --yes`; ESLint already in `frontend/`
devDependencies; pytest-cov via `--with-coverage` flag (off by
default; slow run).

---

## Performance tuning (multi-wave)

Multi-wave optimization series across backend + frontend:

- **Memoized time formatters** — `formatDualTz` + `clientTimestamp` cache per-minute-key. 2000 calls/3s → 5-10 per page load.
- **Store write guards** — `executionMode` ignores writes when value unchanged (no chain re-fires on tick burst).
- **Viewport-paused polling** — `ChartWorkspace`, `OptionChainTab`, LogPanel lazy-timers via `visibleInterval` (pause on `document.hidden`). 50-70% background reduction.
- **Lazy log tabs** — System + Sim Ticks don't fire until tab activated.
- **OrderTicket/OrderDepth lifecycle** — `suspended` / `paused` props pause preflight + quote poll.
- **WebSocket debounce** — `loadOrders()` 250ms debounce on postback burst.
- **`mask_account(s: str) -> str`** — pure regex (was pandas allocation per call). 16 callsites replaced.
- **Parallel fetches** — preflight 4 awaits → `asyncio.gather`. 300ms (was 800-1200ms). `ensure_all_bootstrapped` set-diff (was N sequential queries). `compute_slice_history` O(D × E) → O(D + E) with running-pointer cumsum. `list_funds` filtered + MIN(date) probe via `asyncio.gather`.
- **Indexes** — `algo_orders (basket_tag, account, symbol)`, `agent_events (sim_mode, agent_id, timestamp DESC)`, `hedge_proxies/investor_events` optimized.

**Result**: `/admin/execution` pages ~20-40 req/min (was 100+).

---

## Data-layer hardening

Comprehensive cascade + index audit (Jun 2026):

**Cascades fixed**:
- `agent_events.agent_id` → `ON DELETE CASCADE` (was NO ACTION, blocked deletes)
- `algo_orders.agent_id` → `ON DELETE SET NULL` (agent deletes were blocked)
- `monthly_statements.user_id` → `CASCADE` (regenerable)
- `investor_events.user_id` → `RESTRICT` (operator must redeem first)
- `investor_tokens.user_id` → `CASCADE` (ephemeral)
- `auth_tokens.user_id` → `CASCADE` (verify/reset)

**FK indexes added** — `algo_events.algo_order_id`, `sim_iterations.parent_run_id` (self-ref).

**Indexes added** — `(kind, date)` on `daily_book`, `(account)` on `algo_orders`, `(token)` on `grammar_tokens`, etc.

**Predicate fixes** — `uq_watchlist_global_pinned` was `((1)) WHERE is_global` (allowed only 1 row total) → `(name) WHERE is_global` (multiple named globals OK).

**Defaults added** — `AlgoOrder.engine` / `.exchange` `server_default=` (raw INSERTs don't NULL them).

---

## Refactoring notes

**Slice X — audit cycle summary** (Jun 2026): Multi-wave audit cycles closed
defects in postback scaffolding (Dhan/Groww), role vocabulary migration (operator
terms), data-layer cascades, perf optimizations (parallel preflight, set-diff
bootstrap, cumsum history), palette consolidation (4 waves), UX consistency
(ConfirmModal on PWA, tab-strip deduplication, auth-check pattern cleanup).

**Key patterns**: `$effect`-gated auth checks on all admin pages. Parallel
`asyncio.gather` for any multi-step broker operations. Module-level caching
(dicts with identity flip on refresh). Canonical components (AlgoTabs, Select,
ConfirmModal, CollapseButton, RefreshButton).

---

## Implementation Deep-Dives (moved from CLAUDE.md 2026-07-08)

### Unified animation model + per-exchange close-snapshot lifecycle (Jul 2026)

One branch matrix, three surfaces (positions, holdings, movers) all dispatch through 
the same resolver.

**Schema triad** — `PositionRow` / `HoldingRow` / `MoverRow` all carry:
- `price_source` ∈ `{"live","snapshot_settled","snapshot_unsettled"}`
- `current_price: float` — unified-model alias for `last_price`
- `is_animating: bool` — cell-animation gate (False on closed-exchange rows)

Legacy field `ltp_source` renamed → `price_source`. Values split by whether broker's 
`close_price` has published (post-45m settle window):
- `snapshot_settled` — Kite has weighted-avg-last-30-min close_price
- `snapshot_unsettled` — first-cut close snapshot without close

**Resolver** (`backend/api/helpers/price_resolver.py`) — pure, O(1):

```python
resolve_current_price(exchange_open, live_ltp, snapshot_close,
                     snapshot_last_ltp, settled) → (price, source, animating)
```

Branch matrix:
- open                                → (live_ltp,          "live",              True)
- closed + settled + close available  → (snapshot_close,    "snapshot_settled",  False)
- closed + no settled close           → (snapshot_last_ltp, "snapshot_unsettled",False)
- closed + no snapshot at all         → (None,              "snapshot_unsettled",False)

**Route overlays** (positions.py + holdings.py + watchlist.py) — build the 
closed-exchange snapshot LTP map ONCE per response via 
`snapshot_gate.latest_snapshot_ltp_map(kind)` (same latest-batch CTE 
the `_positions_snapshot` reader uses — SSOT). Per-row: call the 
resolver, apply the triad. Holdings-specific: cur_val recompute when 
snapshot LTP wins.

**Unified movers** (`backend/api/routes/watchlist.py:get_movers`) — one 
live-path body handles NSE-only / MCX-only / both-open scenarios. Same 
resolver dispatch. `_get_movers_mcx_live`, `_session_movers_mcx`, 
`_mcx_fut_map_cache` **deleted**. Session-sticky collapsed to one dict 
keyed by symbol. Snapshot persistence filtered to NSE-exchange rows only 
(via `nse_rows = [r for r in rows if r.exchange == "NSE"]`) so the NSE 
15:29 close snapshot isn't overwritten by evening MCX data. Cache key 
scoped by `len(key_to_meta)` — busts on NSE-only → NSE+MCX transitions.

**Exchange gate map** (`snapshot_gate._EXCHANGE_TO_GATE`):
NSE/BSE/NFO/BFO/CDS → NSE (09:15-15:30 IST), MCX → MCX (09:00-23:30 IST).

**Frontend cell gate** — `pulseColumns.js:mkLtpCol` reads the triad. The 
tick-flash cellClass is gated by `is_animating`; snapshot rows never 
flash regardless of price change. SNAP chip variants:
- `.ltp-snap-chip` — settled (standard amber)
- `.ltp-snap-chip--unsettled` — pre-settle (broker close_price pending)

`MarketPulse.svelte` unified-row merge propagates the triad: ANY leg 
tagged non-live tags the merged row; ANY leg with `is_animating=False` 
freezes the merged row.

**`?skip_ltp=1`** — RefreshButton passes this during both-markets-closed 
clicks so cash/margins/holdings-metadata refresh from the broker while 
LTPs stay frozen at the snapshot value.

**Snapshot lifecycle** — `<exch>:close` writes first-cut daily_book row 
with live LTP; `<exch>:close_settled` UPSERTs 15 min later with broker's 
weighted-avg-last-30-min close_price. Both events share the same 
`_snapshot_close` handler → the second call is idempotent overwrite. 
The handler also fires `snapshot_sparkline()` on each event so the 
frontend cell renderer has a durable close-bar series to draw when 
`is_animating === false`. Mid-session daily_book rows carry `ltp=None` 
so the row-overlay skips them (`ltp IS NOT NULL AND ltp > 0` gate in 
`latest_snapshot_ltp_map`).

**Sparkline cache** — `_spark_past_cache` (past closes) + `_spark_today_cache` 
(intraday 30m, 5min TTL) + LTP at response time. Disk-persisted to 
`.log/sparkline_cache.json` (throttled 5s writes, atomic).

**Warm symbol universe** — watchlist + holdings + positions + mover pairs 
(NIFTY MIDCAP 100 / NIFTY SMLCAP 100 / F&O largecap / indices), capped 300. 
Operator book always added first; movers drop if truncated.

### Snapshot payload extras

`daily_book.payload_json` now embeds a `snapshot_extras` block per 
positions/holdings row: `open`, `high`, `low`, `close_settled`, `prev_close`, 
`volume`, `oi`, `day_change_val`, `day_change_pct`, `ltp`, `settled`. 
Extracted by `_extract_snapshot_extras` (single helper shared by holdings + 
positions row builders in `daily_snapshot.py`). Legacy top-level Kite fields 
preserved for readers that pre-date the extras block.

### Chart Workspace indicator formulas + signal rules

**Indicator sub-panels** (below price panel, same SVG):
- `RSI 14` — Wilder-smoothed RSI with 30/70 reference lines
- `MACD 12/26/9` — histogram (green/red bars) + MACD line (amber) + 
  signal (red-dashed). Requires ≥27 bars; signal needs ≥36.

**Indicator module** — `frontend/src/lib/chart/indicators.js` — pure 
stateless functions (`sma`, `ema`, `vwap`, `bollinger`, `rsi`, `macd`) 
+ signal-detection helpers. No DOM/Svelte imports. Throw `RangeError` 
for invalid periods.

**Buy/sell signal markers** (TradingView-style) — for each active overlay 
the signal-detection function returns `[{i, type: 'buy'|'sell'}]` events. 
ChartWorkspace renders green-up triangle below bar low for buys, red-down 
triangle above bar high for sells, plus a 9px monospace tag. Same-bar 
markers stack vertically (16px offset). Density throttle: per-indicator 
cap of 12 events on dense ranges (≥180 bars).

**Signal rules**:
- **EMA cross** (needs both EMA 20 + EMA 50) — fast crosses above/below slow (golden / death cross)
- **VWAP** — close crosses above/below cumulative VWAP
- **Bollinger** — close pierces lower (buy) / upper (sell) band; throttled to first bar of contiguous run
- **RSI 14** — crosses 30 from below (buy) / 70 from above (sell)
- **MACD 12/26/9** — line crosses signal line

### LTP-flash cascade implementation

Global CSS classes `ltp-flash-up` / `ltp-flash-down` deliver 350ms 
green/red background pulse. Two tiers:

1. **LTP cell** — raw `last_price` cell always carries 
   `ltp-flash-up`/`ltp-flash-down` when SSE tick changes direction.
2. **Derived cells (cascade)** — on pages where one LTP source maps 
   unambiguously to a position row (MarketPulse, PerformancePage), 
   SAME direction class pushed to all derived cells (Day P&L, P&L, 
   Day %, P&L %, Exp P&L). SOURCE-based: LTP tick direction drives 
   cascade regardless of derived cell sign. In PerformancePage via 
   `pnlClsFlash(field)` which checks `_perfFlash.classOf(\`${k}:last_price\`)` 
   first; if set, returns `ltp-flash-up`/`ltp-flash-down` (overrides 
   per-field tf-up/tf-down). In MarketPulse, `_ltpFlashUp` / `_ltpFlashDown` 
   are `$state(Set<string>)` populated from SSE tick diffs; `cellClass` 
   callbacks emit cascade classes when set contains row symbol.

**Derivatives exemption** — `/admin/derivatives` by-underlying rollup rows 
use per-field poll-diff flash (`flash.update(\`${root}:day_w\`, ...)` etc.) 
rather than LTP-source cascade. Rationale: each rollup row aggregates N 
legs across multiple instruments — no single LTP event unambiguously dominates. 
Applying cascade would require arbitrary tie-break + mislead operator. 
Underlying Spot / Day % cells DO use `flash.update(\`${root}:ltp\`, ...)` 
independently. Intentional deviation documented in source comment.

### Write-queue + event-queue deep-dives

**Write queues** — `write_queue.py`:
- `disk_queue` (5K max) → batched JSON to `.log/sparkline_cache.json` (5s throttle)
- `db_queue` (10K max) → batched SQL upserts per kind
- Coalesce: last-write-wins on duplicate keys, 500-row batches or 500ms timeout
- On queue full: warn + drop, next read re-fetches from broker

**Event queues** — `backend/api/persistence/event_queue.py`:
Generic `EventQueue` class for high-frequency append-only writers. Uses 
SQLAlchemy bulk `executemany` INSERT (not `add_all`) for true batch efficiency. 
Re-queues on transient DB failure.

All four started at app startup in `app.py` (`_start_event_queues`) and 
stopped gracefully at shutdown (`_stop_event_queues` flushes remaining rows).

### Activity surface CSS multi-column layout

**Multi-column layout** — CSS `column-count: 2` at ≥900px container width 
(NewsList-style magazine flow). Single column below 900px. All four mounts 
(modal / dashboard card / orders card / page) apply same responsive pattern. 
NewsList accepts `columns={n}` (default 1) and `showSource={bool}` (default 
true); activity-surface News tab passes `columns={2}, showSource={false}` 
so per-row source pill collapses and title runs full row width.

### Investor portal day-delta footnotes

**Day delta** — slice(today) − slice(prior) via same event set. Subscriptions 
between snapshots inflate both slice + cost_basis (no P&L double-count).

**Auto-bootstrap** — for each eligible LP (is_active=True, share_pct > 0) 
without events, inserts synthetic bootstrap event at contribution_date 
(or created_at fallback).

### Performance measurement full JSON schema

**Baseline schema** (single JSON per snapshot):

```json
{
  "captured_at": "2026-07-03T03:57:26Z",
  "commit":      "5b841d2f",
  "frontend": {
    "pages": {
      "/pulse": {
        "file":             "frontend/src/lib/MarketPulse.svelte",
        "loc":              6465,
        "effect_count":     53,
        "state_count":      109,
        "derived_count":    69,
        "subscribe_count":  4,
        "cyclomatic_est":   1651,
        "cyclomatic_hotspots": [
          {"fn_name": "addSpotFromPicker", "cc": 75, "line": 332}
        ],
        "runtime": {
          "lcp_ms":         840,
          "load_ms":        1240,
          "heap_mb":        44.2,
          "long_task_ms":   120,
          "tbt_ms":         42,
          "ws_connections": 2,
          "refresh_click_ms": 260
        }
      }
    },
    "bundle_size_kb": 1234
  },
  "backend": {
    "routes": {
      "GET /api/positions": {
        "file":           "...",
        "loc":            890,
        "async_fn_count": 12,
        "cyclomatic_avg": 10.8,
        "cyclomatic_max": 49,
        "cyclomatic_hotspots": [
          {"fn_name": "PositionsController.get_positions", "cc": 49, "line": 709}
        ]
      }
    }
  }
}
```

**Cyclomatic thresholds**: green < 10, yellow 10–20, red > 20. Applied at 
report time; JSON stores raw scores. Backend uses `radon cc -a -j`; frontend 
uses regex heuristic on extracted `<script>` block.

**Runtime lane** — driven by `scripts/perf_capture_run.sh`. Fail-fast preflight requires:
- `PLAYWRIGHT_USER` + `PLAYWRIGHT_PASS` (real dev.ramboq.com credentials)
- `dev.ramboq.com` reachable (curl HEAD probe)

### Keyboard shortcut store-signal wiring

**Store signals** (in `frontend/src/lib/stores.js`):
- `orderTicketModal` / `openOrderTicketModal()` / `closeOrderTicketModal()`
- `chartModalTrigger` / `openChartModalTrigger()` / `closeChartModalTrigger()`

`PageHeaderActions.svelte` subscribes in `onMount`, fires modal open, 
then immediately resets store.

**Refresh wiring** (`RefreshButton.svelte`):
Each instance listens `window.addEventListener('refresh-page', …)` in 
`onMount`, cleaned up in `onDestroy`. Falls back to `goto(invalidateAll)` 
when no `.rf-btn` present on page.
