# RamboQuant — Historical Notes

Archival content extracted from CLAUDE.md. Load this file only when investigating
"why a thing was done a particular way" or tracing a past refactor. Not autoloaded.

---

## Index

- Closed-hours route gate + broker auth health badge (Jun 2026)
- Broker isolation (slices 1–4)
- Frontend perf budgets + audit (Jul 2026)
- Visibility-aware polling (Option A, Jun 2026)
- Code metrics tracking (Phase 1, Jun 2026)
- Performance tuning (multi-wave)
- Data-layer hardening
- Refactoring notes

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
