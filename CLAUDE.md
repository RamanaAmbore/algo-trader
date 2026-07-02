# CLAUDE.md — RamboQuant Project Reference

For Claude Code. Durable architecture + file map to avoid re-exploring from scratch.

---

## Multi-agent coordination (read first)

Main coordinator + specialized subagents in `~/.claude/agents/`:

| Agent | Use | Model |
|---|---|---|
| `backend` | Litestar / SQLAlchemy work | sonnet |
| `frontend` | SvelteKit / Svelte 5 / ag-Grid | sonnet |
| `backend-test` | pytest + async tests | haiku |
| `frontend-test` | Playwright e2e | haiku |
| `audit` | Read-only defect review (no writes) | sonnet |
| `doc` | CLAUDE.md / USER_GUIDE.md / runbooks | haiku |

**Parallel by default** — independent sub-tasks fire together. Sequence only when one output feeds another or when audit finds defects requiring fixes.

---

## Project Overview

**RamboQuant** — production web app at ramboq.com. Portfolio tracking, Gemini AI market updates, multi-broker trading.

- **Stack**: Litestar API + SvelteKit frontend
- **Deployment**: Single codebase, prod (`main`) + dev (branches)
- **Database**: PostgreSQL 17 (async SQLAlchemy 2.x); `ramboq` (prod) / `ramboq_dev` (dev) by `deploy_branch`
- **Broker**: Zerodha Kite (primary); Dhan + Groww adapters
- **Auth**: JWT HS256 (24h), PBKDF2-SHA256 passwords

**Current capabilities (2026-06)**:
- Multi-execution ladder (sim → paper → shadow → live, replay)
- Declarative agent grammar (9 built-in)
- Derivatives analytics (multi-leg payoff, σ, EV, R:R)
- Proxy hedges (β regression)
- Multi-broker (Kite / Dhan / Groww), IPv6 binding, basket orders
- MCP server + Lab page (chat-driven research)

---

## Deployment

| Env | Branch | Path | Port | Domain |
|---|---|---|---|---|
| Prod | `main` | `/opt/ramboq` | 8502 | ramboq.com |
| Dev | other | `/opt/ramboq_dev` | 8503 | dev.ramboq.com |
| Conn service | both | `/opt/ramboq` (shared data) | UDS | `/tmp/ramboq_conn.sock` |

Push → webhook (`webhook.ramboq.com/hooks/update`) → `dispatch.sh` → `deploy.sh`
→ restart ramboq_api + ramboq_dev_api. Conn service restarts only if broker-layer
files changed (via `deploy.sh` `CONN_TOUCHED` flag).

**Note**: `webhook.ramboq.com` and `dev.ramboq.com` must be grey cloud (DNS only)
in Cloudflare. Conn service runs under `ramboq_conn.service` (separate systemd
unit, independent restart cadence).

---

## Key File Map

### Broker isolation (`backend/brokers/`)
Centralized broker layer — all Kite/Dhan/Groww logic lives here. Includes
adapters, connection management, ticker streaming, and credential encryption.

- **`adapters/{kite,dhan,groww}.py`** — broker-specific implementations
  (profile, quote, place_order, positions, holdings, margins, etc).
- **`connections.py`** — `Connections` singleton; 2FA, token refresh, IPv6
  binding, DB-backed credential encryption (Fernet). On `RAMBOQ_USE_CONN_SERVICE`
  mode, short-circuits local logins and returns `RemoteBroker` stubs instead.
- **`broker_apis.py`** — `fetch_holdings / positions / margins` + `@for_all_accounts`.
  Holiday calendar cached per `(exchange, date)`. When `RAMBOQ_USE_CONN_SERVICE=1`,
  proxies calls to conn_service via `broker_client`.
- **`kite_ticker.py`** — `TickerManager` singleton (one Kite WebSocket per
  process). On `RAMBOQ_USE_CONN_SERVICE=1`, local API reads from shared-memory
  mmap buffer (`/dev/shm/ramboq_ticks`) via `MmapTickReader` instead.
- **`service/`** — standalone Litestar app on UDS `/tmp/ramboq_conn.sock`.
  Owns all broker sessions (Kite WebSocket, token lifecycle for all three
  brokers). Restart independent of `ramboq_api`.
- **`client/`** — sync/async clients for service-to-API RPC over UDS + HTTP.
  Used by main API when `RAMBOQ_USE_CONN_SERVICE=1`.

### Helpers (`backend/shared/helpers/`)
- **`decorators.py`** — `@for_all_accounts`, `@retry_kite_conn()`, `@track_it()`, `@lock_it_for_update`
- **`utils.py`** — YAML loaders, validators, `get_nearest_time()`, `mask_column()`, `mask_account()`.
- **`genai_api.py`** — Gemini 2.5 Flash (gated `cap_in_dev.genai`).
- **`date_time_utils.py`** — `is_market_open()`, `timestamp_display()` (dual-TZ format).
- **`ramboq_logger.py`** — Rotating file handlers (5MB × 5).
- **`alert_utils.py`** — Telegram + email dispatch (prefixes: Open / Agent / Close).

### Webhook / Deploy (`webhook/`)
- **`deploy.sh`** — Git update, config merge, `deploy_branch` set, notify.
- **`notify_deploy.py`** — Telegram deploy notification.
- **`hooks.json`** / **`dispatch.sh`** — HMAC routing. Copy to server manually after edits.

### Config (`backend/config/`)
| File | Tracked | Purpose |
|---|---|---|
| `backend_config.yaml` | Yes | Retry, `cap_in_dev` dict, alert thresholds, market segments |
| `frontend_config.yaml` | Yes | Page content, Gemini prompts |
| `constants.yaml` | Yes | Country codes |
| `secrets.yaml` | No | API keys, cookie_secret, Telegram token |
| `grammars/orders.yaml` | Yes | Order-entry grammar (frontend symlink) |

**Deploy preserves**: every `alert_*` key, entire `cap_in_dev` dict. `deploy_branch` always fresh.

### Production capabilities (`cap_in_dev`)
Nested dict of flags. On **main** (prod) all on; on **dev/branches** each toggles independently.

```yaml
cap_in_dev:
  genai:            True   # Gemini market update
  telegram:         True   # Telegram alerts
  mail:             True   # Email (SMTP)
  notify_on_deploy: True   # Deploy ping
  market_feed:      True   # News RSS
```

Gate via `is_enabled('<cap>')` in `utils.py`. On main always True; on dev reads the dict.

---

## Alert and Notification System

**Message types**:

| Event | Telegram | Email |
|---|---|---|
| Market open | `Open` | (Telegram only) |
| Agent fire | `Agent` | `RamboQuant Agent: ` |
| Market close | `Close` | (Telegram only) |

**Vocabulary**: Agent (rule) → Alert (event) → Notify (delivery) → Action (side-effect).

**Timestamp**: `timestamp_display()` produces `Mon 30 Mar 09:30 IST | Mon 30 Mar 10:00 EDT`.

**Recipients** (`secrets.yaml`):
- `alert_emails` — operator alerts (loss / agent / summary)
- `market_emails` — public contact form

**Global gates** (`backend_config.yaml`):
- `alert_cooldown_minutes` (30) — min time between refires; suppression gate requires |Δpnl| ≥ `alert_suppress_delta_abs` or |Δpct| ≥ `alert_suppress_delta_pct`.
- `alert_baseline_offset_min` (15) — rate agents silent after session start.
- `alert_rate_window_min` (10) — minutes of P&L for rate calculation.
- Market hours gate: skip `schedule: market_hours` agents outside segment-open hours.

**Deploy notifications**: Telegram-only (May 2026). Conn-service restart status
also monitored (independent cadence; only logs if file changes detected).

---

## Market Segments

| Segment | Exchanges | Hours (IST) | Holidays |
|---|---|---|---|
| Equity | NSE, BSE, NFO, CDS | 09:15–15:30 | `kite.holidays("NSE")` |
| Commodity | MCX | 09:00–23:30 | `kite.holidays("MCX")` |

Open/close summaries sent at `open_summary_offset_minutes` / `close_summary_offset_minutes` after segment open/close. Weekends hardcoded closed (special Muhurat needs explicit override).

---

## Background Tasks (`backend/api/background.py`)

| Action | Timing |
|---|---|
| Market cache warm | Startup + daily 08:30 IST |
| Performance refresh | Every 5 min during market hours |
| Open/close summaries | Per segment, once daily |
| Loss alert check | Per performance fetch |
| Agent `run_cycle()` | Per performance fetch (skips market_hours outside hours) |
| Sparkline warm | Startup, 00:30 IST, segment opens |
| Ticker watchdog | Every 30s |
| Trail stop / OCO pair-watcher | Every 30s |
| Hedge proxy regression | Daily 02:30 IST (age > `hedge_proxies.regression_max_age_days`) |
| Market lifecycle poller | Every 30s (per-exchange open/close transitions) |
| Funds-off-hours refresh | Every 30 min while no segment open |

## Market lifecycle events

Singleton `MarketLifecycle` in `backend/api/algo/market_lifecycle.py` is
polled every 30s by `_task_market_lifecycle`. Detects per-exchange
transitions for `nse` / `mcx` / `cds` and dispatches handlers registered
via `market_lifecycle.register(event, callback)`.

| Event | Fires |
|---|---|
| `<exch>:open` | At session open (calendar-aware, holiday-gated via `fetch_holidays`) |
| `<exch>:close` | At session close |
| `<exch>:close_settled` | 45 min AFTER `<exch>:close` — operator-tunable via `market_lifecycle.settled_offset_min`. Captures broker's adjusted close_price (Kite weighted-avg-last-30-min) which lands late. |

**Default handlers** (`backend/api/algo/market_lifecycle_handlers.py`):
- `nse:close` + `nse:close_settled` → `snapshot_daily_book()` + NAV snapshot
- `mcx:close` + `mcx:close_settled` → `snapshot_daily_book()`
- `cds:close` + `cds:close_settled` → `snapshot_daily_book()`

Snapshot is idempotent via UPSERT on `(date, account, kind, symbol)`,
so `close_settled` overwrites the initial rows with the adjusted broker
values. Audit rows persisted to `market_lifecycle_events` (indexed on
fired_at + (exchange, event_type, fired_at)).

**Frontend gating** — `marketOpenInterval(fn, ms, 'NSE'|'MCX'|null)` in
`stores.js` is the per-exchange equivalent of `marketAwareInterval`.
`startMarketGatedQuoteStream()` in `quoteStream.js` pauses the SSE LTP
stream when neither NSE nor MCX is open and resumes it on the next
open transition; broker fetches for positions / cash / holdings stay
on their own poll path.

**Closed-hours refresh UX** — `RefreshButton.svelte` no longer blocks
refresh during closed hours. Click fires the parent's onClick AND
surfaces a toast "Showing close snapshot — markets reopen at <time>"
(3s auto-dismiss).

**Sparkline cache** — `_spark_past_cache` (past closes) + `_spark_today_cache` (intraday 30m, 5min TTL) + LTP at response time. Disk-persisted to `.log/sparkline_cache.json` (throttled 5s writes, atomic).

**Warm symbol universe** — watchlist + holdings + positions + mover pairs (NIFTY MIDCAP 100 / NIFTY SMLCAP 100 / F&O largecap / indices), capped 300. Operator book always added first; movers drop if truncated.

---

## KiteTicker / SSE live-LTP pipeline

**Before conn-service**: Persistent Kite WebSocket → BroadcastBus → asyncio.Queue
per SSE client → EventSource (browser auto-reconnect) → `liveLtp` store → 250ms
throttled Svelte effect → ag-Grid. TickerManager ran in the main API process.

**After conn-service isolation**: WebSocket lives in conn_service. Ticks written
to shared-memory buffer (`/dev/shm/ramboq_ticks`, fixed 4096 slots, version-word
atomic). Main API reads at byte-read latency via `MmapTickReader`. Background
poller (50ms) tails version word + publishes deltas to local BroadcastBus (so
SSE + frontend subscriptions stay unchanged).

**Failover**: 30s watchdog loop in conn_service detects `started=true AND
connected=true`. On failure, retries `_try_start_ticker()` until success. Handles
chicken-and-egg at boot when `rebuild_from_db` is still minting Kite tokens.

**Subscriptions**: Watchlist + holdings + positions at startup + dynamic on add.
`subscribe()` idempotent. Managed by conn_service.

**Steady-state cost (market hours)**: ~0 REST calls + 1 persistent WS in conn_service.
Main API LTP read from mmap (zero quota, zero latency). Sparkline historical 1
call on cache miss (3 req/sec budget, pre-warmed).

**Health surface** (`GET /api/admin/health`) — `ticker.stale_count`, `ticker.max_age_seconds`,
`ticker.stale_top` (up to 20 worst-offender entries formatted `"SYMBOL@<age>s"` or
`"SYMBOL@never"`). Distinguishes "subscribed but Kite stopped emitting" from
"subscribe call never landed".

---

## Frontend persistent cache layer

In-memory Map + localStorage (key prefix `rbq.cache.`) for high-churn surfaces. Module: `frontend/src/lib/data/persistentCache.js`. TTL buckets: `day` (24h) / `hour` (1h) / `minute` (15m) / `short` (2m). Used by MarketPulse (positions, holdings, sparklines, watchQuotes, movers), PositionStrip, NavCard. Survives reload + deploy. Live LTP state intentionally NOT cached (reconnects from SSE).

**Tick-flash primitive** — `createTickFlash({threshold, durationMs})` from `frontend/src/lib/data/tickFlash.svelte.js`. Canonical 350ms directional pulse (green up / red down) on numeric cell updates. Used by PositionStrip, NavCard, /admin/derivatives by-underlying snapshot, PerformancePage, and MarketPulse.

**LTP-flash cascade** — Global CSS classes `ltp-flash-up` / `ltp-flash-down` (defined in `app.css`) deliver a 350ms green/red background pulse. Two tiers of flash:

1. **LTP cell** — the raw `last_price` cell always carries `ltp-flash-up`/`ltp-flash-down` when the SSE tick changes direction.
2. **Derived cells (cascade)** — on pages where one LTP source maps unambiguously to a position row (MarketPulse, PerformancePage), the SAME direction class is pushed to all derived cells (Day P&L, P&L, Day %, P&L %, Exp P&L). This is SOURCE-based: the LTP tick direction drives the cascade regardless of the derived cell's sign. In PerformancePage this is implemented via `pnlClsFlash(field)` which checks `_perfFlash.classOf(`${k}:last_price`)` first; if set, returns `ltp-flash-up`/`ltp-flash-down` (overrides per-field tf-up/tf-down). In MarketPulse, `_ltpFlashUp` / `_ltpFlashDown` are `$state(Set<string>)` populated from SSE tick diffs; `cellClass` callbacks emit the cascade classes when the set contains the row's symbol.

**Derivatives exemption** — `/admin/derivatives` by-underlying rollup rows use per-field poll-diff flash (`flash.update(`${root}:day_w`, ...)` etc.) rather than LTP-source cascade. Rationale: each rollup row aggregates N legs across multiple instruments — there is no single LTP event that unambiguously dominates the row. Applying cascade would require an arbitrary tie-break and would mislead the operator. The underlying Spot / Day % cells DO use `flash.update(`${root}:ltp`, ...)` independently. This is an intentional deviation documented in the source comment near line 1437 of `+page.svelte`.

**Cross-page book poller** (operator-approved final design 2026-06-28) — `startBookPollers()` in `frontend/src/lib/data/marketDataStores.svelte.js`, invoked once from `(algo)/+layout.svelte`. Runs positions / holdings / funds at the unified `pulse.tick_interval_ms` cadence (default 5 s) regardless of which route is mounted. Pages stay as consumers — they read `positionsStore.value` / `holdingsStore.value` / `fundsStore.value`; their existing on-mount `.load()` calls dedup transparently via `createDataStore`'s in-flight Promise. Hibernation gates fire via the inner `marketAwareInterval` (throttle to 30 s after `polling.idle_timeout_min` minutes hidden, immediate refire on tab return; tab visible OR hidden < threshold → full cadence). Operator's stated end-state: "every page should poll when viewport active. only when viewport is not active for 5 mins, go into hibernation." Cross-page nav is instant — the stores are already hot before the next page mounts.

---

## Broker accounts (DB-backed CRUD)

Operators manage via `/admin/brokers`, not `secrets.yaml` on server. Credentials encrypted at rest (Fernet, derived from `cookie_secret` via HKDF-SHA256).

**Loading** (`Connections.rebuild_from_db` on startup + post-CRUD):
1. Query `broker_accounts` (active rows).
2. If empty AND `secrets.yaml` has `kite_accounts`: seed DB once (encrypt + write rows).
3. Decrypt in memory, rebuild `self.conn` map.

**API** (`/api/admin/brokers/*`, admin-guarded):
- `GET` — list / read single (no secrets returned)
- `POST` — create
- `PATCH` — update (empty secret = unchanged)
- `DELETE` — remove
- `POST /test` — verify credentials via `broker.profile()`

**Status pill** (LOADED / PENDING / DISABLED) polls every 15s.

---

## Multi-Account IPv6 Source Binding (Kite + Dhan)

**Why**: Kite = one IP per app. Dhan = one token per source IP. Groww = no per-IP rule.

**Current**: Two egressing IPs. IP-sharing works — Kite + Dhan can share an IP (different session registries).

| Account | Broker | source_ip |
|---|---|---|
| ZG0790 | Kite | `69.62.78.136` (IPv4) |
| DH6847 | Dhan | `69.62.78.136` (shared) |
| ZJ6294 | Kite | `2a02:4780:12:9e1d::1` (IPv6) |
| DH3747 | Dhan | `2a02:4780:12:9e1d::1` (shared) |
| GR87DF | Groww | default |

**Implementation**: `_IPv6SourceAdapter` extends `requests.HTTPAdapter`. Mounted on broker sessions. Groww SDK monkey-patched (module-level `requests` replaced with source-bound pool proxy).

**Per-account stabilizer** — Dhan rows grouped by `source_ip`. If 2+ Dhan on same IP, only lowest-`priority` loaded; rest deferred.

**Adding account**: Choose IPv6, bind to server, set `source_ip` in broker_accounts, clear token cache files.

---

## Key Patterns

**Caching**: In-process TTL in `backend/api/cache.py` with per-key locking. Pre-warm before users hit pages.

**Raw broker-DataFrame cache** (`backend/brokers/broker_apis.py:_RAW_CACHE`, 30s TTL):
zero-arg `fetch_holdings()` / `fetch_positions()` / `fetch_margins()` memoise their
`list[pd.DataFrame]` return. Route handlers (positions/holdings/funds), `compute_firm_nav`
(algo/nav.py), investor slice, and nav_daily writer all share one broker round-trip per
TTL window. `?fresh=1` and terminal-order postbacks (Kite + Dhan/Groww shared path) call
`_raw_cache_invalidate(key)` so fills surface immediately. The route-level `get_or_fetch`
still memoises the FORMATTED `msgspec.Struct` response — both layers cooperate. Single
canonical layer eliminates the 4× broker fan-out and the NavCard-vs-/performance drift.

**Holiday calendar**: `fetch_holidays(exchange)` cached per `(exchange, today's date)` in `_HOLIDAY_CACHE` module dict. Buster = date rollover. Empty sets also cached (avoid retry-storm on API failure).

**Multi-account broker calls**: `@for_all_accounts` iterates accounts, returns list of DataFrames. Callers use `pd.concat(..., ignore_index=True)`.

**Account masking**: `mask_account(s: str) -> str` replaces digits with `#`. `mask_column(pd.Series)` for DataFrames. Used in all alerts + summaries.

**Singleton Connections**: Thread-safe, access as `connections.Connections()`.
Initialized once at startup. On `RAMBOQ_USE_CONN_SERVICE=1`: short-circuits local
broker logins; `rebuild_from_db()` queries broker_accounts DB but populates only
the registry with `RemoteBroker` stubs that proxy over UDS to conn_service.

**Closed-hours route gate** (`backend/api/helpers/snapshot_gate.py`):
`closed_hours_or_broker(exchange, snapshot_fn, broker_fn, *, fallback_to_snapshot_on_broker_error=True) -> tuple[T, str]`
is the **canonical gate** for all data routes that need a market-closed snapshot fallback.
Primary invariant: `broker_fn` is NEVER called when the market is closed.
Source tags returned: `'live'` (market open, broker succeeded), `'snapshot'` (market closed),
`'snapshot-fallback'` (market open but broker raised, fallback enabled).
Every new data route (positions, holdings, and any future live-data route) MUST use this
helper — no inline `is_market_open` checks inside route handlers. Existing routes that
are exempt (quote.py per-exchange batch, options.py intraday sub-call) have distinct
semantics documented in the source.
`_any_segment_open()` (the inner sync predicate) is what tests patch to control the
market-state path in unit tests.

**Broker auth health badge** (`frontend/src/lib/BrokerHealthBadge.svelte`):
Admin/designated-only badge in the navbar. Polls `GET /api/admin/broker-health` every 30s
via `visibleInterval`. State: `green` (last_good < 5 min ago), `amber` (stale or never tried),
`red` (last_fail > last_ok). Worst state across all accounts drives the badge colour.
Click opens a per-account modal. The existing broker-count chip (connStatus) is a separate
signal (loaded/total count) and is unchanged.

---

## Broker resilience

**Circuit breaker (Jul 2026 — P0 DH6847 rotation-loop fix)**:
Per-account, per-broker state machine in `backend/brokers/broker_apis.py` (`_record_fetch` + `_is_circuit_open`).

- **CLOSED** (normal): every fetch runs; `consecutive_fail_count` increments on each failure.
- **OPEN** (after 3 consecutive failures): `circuit_open_until = now + cool-off`. All `_fetch_*_local` functions short-circuit immediately — the SDK is never called. One `[BREAKER]` warning logged. Cool-off is exponential: 5m → 10m → 20m → 30m (cap). Returns empty DataFrame with `attrs['circuit_open'] = True`.
- **HALF-OPEN** (after cool-off expires): next probe runs. Success → CLOSED (counters reset). Failure → OPEN again at next exponential step.

State is stored as extra fields in `_FETCH_HEALTH[account]`: `consecutive_fail_count`, `circuit_open_until`, `circuit_last_opened_at`, `open_cycle_count`. No separate state store.

`/api/admin/broker-health` surfaces `circuit_state`, `consecutive_fail_count`, `circuit_open_until` per account. `BrokerHealthBadge.svelte` renders OPEN/PROBE chips + tooltip with retry time. Tests: `backend/tests/broker/test_circuit_breaker.py` (17 tests).

**Prod effect**: DH6847 hammering DH-906 every 30s stops after 3 consecutive failures (~90s of prod log noise) vs the observed ~50 failures/hour before the fix.

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

## Things to Avoid

- Don't mock broker API calls — `@for_all_accounts` and singleton behave differently
- Don't commit `secrets.yaml` — gitignored; SSH-edit `/opt/ramboq*` on server
- Don't add branch filters to `hooks.json` — routing in `dispatch.sh`
- Don't use `2>>&1` in systemd — use `2>&1` (>> causes bash syntax errors)
- Always `chown www-data -R` after server ops: `/opt/ramboq*/.git /opt/ramboq*/.log`
- Weekends hardcoded closed — special sessions need explicit override
- Don't try to run main API without conn-service when `RAMBOQ_USE_CONN_SERVICE=1`
  is set — service startup will fail with socket connection errors

---

## Persistence pipeline (cache → DB → broker)

Three-tier read hierarchy for OHLCV, instruments, holidays, intraday bars. Each
read checks Tier 1 (in-memory LRU) → Tier 2 (PostgreSQL) → Tier 3 (broker API).
Per-key asyncio.Lock deduplicates concurrent in-flight fetches. Broker writes
return immediately to caller; persistence runs off-path via two parallel worker
coroutines (`cache_worker.py`, `db_worker.py`).

**Stores** (`backend/api/persistence/`):
- `ohlcv_store` — daily bars, (sym, exch) key, range-based completeness
- `instruments_store` — per-exchange symbol→token map, daily TTL via purge_stale()
- `holidays_store` — per-(exchange, year), immutable once year closes
- `intraday_store` — 5/15/30/60-min bars, 5-min TTL on today's data
- Base: `store_base.py` — abstract three-tier flow + LRU eviction + metrics

**Completeness checks** (prevent stale-data masquerade):
- OHLCV: boundary dates present + gaps ≤4 days (handles weekends + holidays)
- Instruments: non-empty map
- Holidays: non-empty set
- Intraday: today = any bars OK (growing); historical = must span session close

**Refresh-cycle modes** (operator can flip via POST `/api/admin/persistence/mode/{off|soft|hard}`):
- `off` — normal hierarchy (default, safe)
- `soft` — Tier 1+2 bypass, fetch from broker, write-back heals both tiers
- `hard` — soft + ticker recycle (unsubscribe→reconnect→resubscribe)
Mode is runtime-only (resets to `off` on process restart).

**DB schema**:
- `ohlcv_daily(symbol, exchange, date, open, high, low, close, volume)`
- `instruments_snapshot(exchange, date, payload jsonb, row_count)`
- `holidays_snapshot(exchange, year, dates_json)`
- `intraday_bars(symbol, exchange, date, interval, bar_ts, open, high, low, close, volume)`

**Retention** (staggered nightly cron — 03:10 / 03:15 / 03:20 IST):
- `ohlcv_daily` → 5 years; `instruments_snapshot` → 7 days; `intraday_bars` → 90 days;
  `holidays_snapshot` → forever. (hard-coded; persistence-layer cache decisions)
- `algo_events` → 30 days (`retention.algo_events_days`); diagnostic agent-state journal.
- `algo_order_events` → 90 days (`retention.algo_order_events_days`); per-order chase timeline.
- `auth_tokens` → 7 days after expiry (`retention.auth_tokens_days`); one-time verify/reset tokens only.
- `mcp_audit` → 90 days (`mcp.audit_retention_days`); MCP-initiated mutations.
- `audit_log` → 365 days (`retention.audit_log_days`); forensic operator trail. NOT the SEBI record.
- `nav_daily`, `daily_book`, `investor_events`, `monthly_statements` → forever (financial records).
- All configurable keys editable live from `/admin/settings` → Retention. Set to `0` to disable.

**Write queues** — `write_queue.py`:
- `disk_queue` (5K max) → batched JSON to `.log/sparkline_cache.json` (5s throttle)
- `db_queue` (10K max) → batched SQL upserts per kind
- Coalesce: last-write-wins on duplicate keys, 500-row batches or 500ms timeout.
- On queue full: warn + drop, next read re-fetches from broker.

**Metrics** (`GET /api/admin/health`):
- Per-store: `tier1_hits`, `tier2_hits`, `tier3_fetches`, `tier3_errors`, `hit_rate`.
- Write workers: `disk_queue.depth`, `db_queue.depth`, `last_flush_epoch`, `worker_alive`.

**Invalidation** (selective cache wipe):
- `POST /api/admin/persistence/invalidate?store=ohlcv_daily&symbol=NIFTY50`
- Drops in-memory entry + deletes matching DB rows.
- Supports symbol-only (all exchanges), exchange-only (all symbols), or full wipe.

**Bypass patterns in code**:
- `get_or_fetch_daily(..., bypass_cache=True)` — defect-recovery escape hatch
- `runtime_state.is_bypass_on()` checks on every read; soft/hard modes set this True

**db_only mode** — per-store flag skips Tier 3 (broker) during closed hours.
Each store (`ohlcv_store`, `intraday_store`) checks `_any_segment_open()` at
read time; when all markets closed, passes `db_only=True` to completeness checks
(which skip validation requiring live market data). Frontend sparkline refresh
uses `db_only=True` when all segments closed (low-priority 5-min poll).

**Chart self-heal** — `/api/options/historical?symbol=…&days=30` detects under-coverage
(<70% of requested days present in DB) and auto-fetches from broker when ≥1 broker
available. Response carries `partial: bool` (True if recovery incomplete) so frontend
can hint "partial data". Cool-off aware: skips broker during rate-limit window.
Logs one healing per symbol per 60s to avoid spam. Coverage threshold tunable via
`/admin/settings` → Persistence → `chart_self_heal_coverage_threshold` (default 0.70).

**Coverage backfill** (`backend/api/persistence/backfill.py`):
- `backfill_ohlcv_daily(symbols, target_days=365)` — force-fetch 365-day window
  for symbols with <70% coverage; skips broker in cooloff.
- `backfill_intraday_today(symbols, interval="30minute")` — force-fetch today's bars;
  defers when markets closed.
- Startup hook `_task_warm_backfill` (60 s delay, once per process) fires both
  for the 300-symbol universe.
- On-demand: `POST /api/admin/persistence/backfill?kind=daily|intraday|both`
  (admin-guarded).
- CLI: `scripts/persistence_mode.py off|soft|hard|status` (reads operator login)
  and `scripts/backfill_ohlcv.py --daily --intraday` for immediate prod fix.

**Coverage backfill** (`backend/api/persistence/backfill.py`):
- `backfill_ohlcv_daily(symbols, target_days=365)` — force-fetch 365-day window for symbols with < 70% coverage; skips broker in cooloff.
- `backfill_intraday_today(symbols, interval="30minute")` — force-fetch today's bars; defers when markets closed.
- Startup hook `_task_warm_backfill` (60 s delay, once per process) fires both for the 300-symbol universe.
- On-demand: `POST /api/admin/persistence/backfill?kind=daily|intraday|both` (admin-guarded).
- CLI: `scripts/backfill_ohlcv.py --daily --intraday` for operator immediate prod fix.

---

## API Architecture (Litestar + SvelteKit)

**Stack**:
- **Framework**: Litestar 2.x + msgspec.Struct (10× faster pydantic)
- **Database**: PostgreSQL 17 + SQLAlchemy 2.x async + asyncpg
- **DataFrames**: Polars (routes: positions.py, holdings.py, funds.py, nav.py) / pandas (broker SDK boundary: broker_apis.py, background.py, summarise.py, sim/driver.py, agent_evaluator.py). Conversion at boundary: `pl.from_pandas(df)` in the route layer. Dead pandas imports audited and removed from orders.py, logs.py, actions.py (Jun 2026).
- **Background**: asyncio tasks (market, performance, close, expiry)
- **Auth**: JWT HS256 (24h TTL), PBKDF2-SHA256 passwords
- **SEO**: OG/Twitter cards, JSON-LD, sitemap.xml, robots.txt

**Tables**: `users` (32 cols) · `algo_orders` · `algo_events` · auto-created on startup.

**Core routes** (`backend/api/`):
- `app.py` — Litestar app + startup (init_db + tasks)
- `database.py` — PostgreSQL + `init_db()`
- `models.py` — User, AlgoOrder, AlgoEvent
- `routes/algo.py` — Agents API + WebSocket
- `routes/orders.py` — Ticket, basket, postback
- `algo/agent_engine.py` — Declarative agent runner + `run_cycle()`
- `algo/chase.py` — Adaptive limit-order chase
- `algo/simulator.py` / `sim/driver.py` — Market sim + scenario engine

---

## Execution Modes (1-5)

Five modes form a **confidence ladder** (sim → paper → shadow → live) + parallel **replay** (historical backtest).

| Mode | Quote | Engine | Branch | Use |
|---|---|---|---|---|
| 1-Simulator | Fabricated | PaperTradeEngine (sim quotes) | Both | Stress-test agents |
| 2-Paper | Live | PaperTradeEngine (live quotes, 5s tick) | Both | End-to-end validation |
| 3-Live | Live | Real broker | Prod only | Real orders |
| 4-Replay | Historical OHLCV | PaperTradeEngine | Both | Backtesting |
| 5-Shadow | Live | Log payload + validate (no execute) | Prod only | Pre-live check |

**Branch gate**: Non-main forces paper regardless of DB flags. Main uses DB flags.

**Mode resolution** (`_resolve_mode`): sim > replay > branch check > shadow > paper_trading_mode > agent.trade_mode.

**Navbar pill**: Exclusive entry point. SIM/REPLAY green, PAPER sky-blue, LIVE red, SHADOW orange. Dropdown commits settings (PAPER/SHADOW) or navigates (SIM/REPLAY) or shows confirm (LIVE).

**Each mode has own page** under `/admin/execution?mode=<slug>`.

---

## Agents Framework

**Four words**:
- **Agent** = rule row (condition + notify + actions + metadata). Seeded from `BUILTIN_AGENTS` in `agent_engine.py`. Extensible via `/automation`.
- **Alert** = runtime event when condition fires. Persisted to `agent_events` with `sim_mode` flag.
- **Notify** = delivery channel (telegram / email / websocket / log).
- **Action** = side-effect (order placement, modify, cancel, close, …).

**Loss agents** — prefix `loss-*`. Four consolidated alerting agents + one fund-negative, all active by default. Rules editable live from `/automation`. Global gates in `backend_config.yaml`: cooldown, baseline offset, rate window.

**Tokens (condition / notify / action)** — `grammar_tokens` table. System tokens seeded on boot, custom via `/admin/tokens`. Each row: grammar_kind, token_kind, token, value_type, resolver (dotted path to function), params_schema, enum_values.

**Condition tree** (v2 grammar):
```
condition  ::= leaf | {all: [...]}, {any: [...]}, {not: ...}
leaf       ::= {metric, scope, op, value}
```

**Metrics**: point-in-time (pnl, pnl_pct, day_pct), rate (pnl_rate_abs, pnl_rate_pct), rolling-window aggregates (mean, max_drawdown, stdev, range), expiry-aware (is_itm, is_ntm, days_until_expiry).

**GrammarRegistry** singleton — in-memory dispatch table. Reloaded on `/api/admin/grammar/reload`.

---

## Simulator

Market simulator feeds fabricated positions into same agent engine as real pipeline — no code branches in hot path. Flag = sim_mode on context dict; downstream tags: `[SIM]` in logs, SIMULATOR in user-facing surfaces.

**Positions-only** by design. Holdings aren't sim'd (intraday risk in F&O). Agents checking holdings metrics validate against live data only.

**Move primitives** (scenario ticks): pct, abs, random_walk, target_pnl, set_margin. Scope glob: `section.account.tradingsymbol` with `*` / `**` wildcards.

**Shipped scenarios**: generic-crash, generic-euphoria, extreme variants, random-walk, 21 specialized (26 total). `synthesize_for_agent()` builds inline scenario from agent condition (nearest-to-fire leaf).

**Chase engine** (spread-aware) — each tick checks if bid/ask crosses limit, fills if matched, otherwise re-quotes. Capped at `simulator.chase_max_attempts` (default 5); after cap flips to UNFILLED.

**Paper-trade action expansion**: `close_position` / `place_order` write one `AlgoOrder`; `chase_close_positions` scope-level write per matching position.

**Seeding modes**: `scripted` (scenario initial blocks), `live` (broker fetch), `live+scenario` (broker + script overlay).

**Sim/real boundary** — `/api/simulator/status` returns `enabled` + `active` + `tick_index` + position/order snapshots + price history by symbol. Pages poll 4s, show sticky SIMULATOR banner.

---

## Derivatives Analytics

Options research: underlying-driven re-pricing, Black-Scholes, implied-vol calibrator, greeks (per-share + position-scaled), strategy multi-leg analysis.

**Symbol parser** — `parse_tradingsymbol()` returns `{kind, underlying, strike, opt_type, expiry}` or None.

**Re-pricing** — `reprice_row(row, spot, sigma)` for derivatives. Futures track spot 1:1; options via BS with cached σ.

**Payoff range** — σ-driven via `span_pct = span_sigmas × σ × √T_years` (default span_sigmas=2.5, clamped [2%, 50%]). Operator can override.

**Endpoints** (`admin-guarded`):
- `GET /api/options/analytics?mode=live|sim|hypothetical&symbol=…` — Greeks, pricing, risk, payoff curve
- `POST /api/options/strategy-analytics` (legs list) — multi-leg aggregate + R:R ratio
- `GET /api/options/historical?symbol=…&days=30` — OHLCV bars with multi-account fallback (skip rate-limited accounts)

**LTP fallback chain**: override → sim positions → live broker → close price → depth midpoint → avg_cost → Black-Scholes-at-default-IV.

**Expected value** — trapezoidal integration of expiry payoff against risk-neutral lognormal. R:R = max_profit / |max_loss| (None for unbounded).

**UI** — `OptionAnalyticsResponse` gains `ev`, `ev_pct`, `rr_ratio`. Payoff chart hand-rolled SVG (no chart lib); overlays underlying spot (dashed sky-blue) for derivatives; zoom + pan + reset toolbar.

**3-band expiry view** — ITM ON EXPIRY (amber, action needed) / NETTED (slate) / OUT OF THE MONEY (muted). Greedy theta-priority pairing for MCX. Per-pair numbered chip (5-color rotation).

---

## Proxy hedges

DB-backed cross-reference between holdings (GOLDBEES, NIFTYBEES, …) and option roots they hedge (GOLD, NIFTY, …).

**Schema**: `hedge_proxies` table — proxy_symbol, target_root, is_active, note, beta (nullable = 1.0 default), correlation, regression_at.

**Math (per render, no stored factor)**:
```
effective_qty = β × market_value / target_spot
target_lots = effective_qty / target_lot_size
effective_cost = investment_value / effective_qty
Δ_extra = effective_qty
```

**β regression** (stage 3) — 60-day daily-returns: β = Cov(p,t) / Var(t), R² = corr². Operator-triggered via `POST /api/admin/hedge-proxies/{id}/compute`. Needs ≥15 overlapping bars.

**Auto-recompute** (`_task_hedge_proxy_regression`, daily 02:30 IST) — per active row where `regression_at` > `regression_max_age_days`.

**UI** — PROXY chip on eq legs (magenta label + lot count + β); Hedge proxies settings card with Compute button.

---

## Chart Workspace

Unified chart for any symbol kind (underlying / future / option / equity). Reads `?symbol=…&mode=…` URL params.

**Components**:
- `ChartWorkspace.svelte` — OHLCV (line/area/candle, 1D/1W/1M/3M/6M/1Y), intraday tick overlay (toggleable), underlying-spot overlay (dashed sky-blue for derivatives), Greeks strip (Δ Γ Θ V ρ IV).
- `ChartModal.svelte` — overlay wrapper (Esc / overlay-click closes, scroll-locked).
- `/charts` page — reads URL params, syncs picks via `goto({replaceState: true})`.

**Indicator overlays** (price panel, toggled via MultiSelect "Overlays" button):
- `SMA 20` / `SMA 50` — simple moving averages (sky-blue / violet)
- `EMA 20` / `EMA 50` — exponential moving averages (green `#4ade80` / orange `#fb923c`), Wilder k=2/(n+1) seed from SMA
- `VWAP` — cumulative volume-weighted average price (solid cyan `#7dd3fc`); returns null for zero-volume bars (indices have no VWAP)
- `BB` — Bollinger Bands ±2σ, 20-period, population σ (TradingView standard), rendered as three lines + fill ribbon

**Indicator sub-panels** (below price panel, same SVG):
- `RSI 14` — Wilder-smoothed RSI with 30/70 reference lines (amber line, `RSI_H=48` user-units)
- `MACD 12/26/9` — histogram (green/red bars) + MACD line (amber) + signal (red-dashed), (`MACD_H=56` user-units). Requires ≥27 bars; signal needs ≥36.

**Indicator module** — `frontend/src/lib/chart/indicators.js` — pure stateless functions (`sma`, `ema`, `vwap`, `bollinger`, `rsi`, `macd`) + signal-detection helpers (`emaSignals`, `vwapSignals`, `bollingerSignals`, `rsiSignals`, `macdSignals`). No DOM/Svelte imports. All compute-only; throw `RangeError` for invalid periods.

**Buy/sell signal markers** (TradingView-style triangles on price panel) — for each active overlay the signal-detection function returns `[{i, type: 'buy'|'sell'}]` events; ChartWorkspace renders green-up triangle (`#4ade80`) below the bar low for buys, red-down triangle (`#f87171`) above the bar high for sells, plus a 9px monospace indicator tag (`EMA↑`, `RSI↓`, `MACD↑`, `BB↓`, `VWAP↑`). Same-bar markers stack vertically (16px offset). Density throttle: per-indicator cap of 12 events on dense ranges (≥180 bars). Signal rules per peer platforms:
- **EMA cross** (needs both EMA 20 + EMA 50) — fast crosses above/below slow (golden / death cross)
- **VWAP** — close crosses above/below cumulative VWAP
- **Bollinger** — close pierces lower (buy) / upper (sell) band; throttled to first bar of contiguous run
- **RSI 14** — crosses 30 from below (buy) / 70 from above (sell)
- **MACD 12/26/9** — line crosses signal line

**Signals toggle** — toolbar chip (only visible when ≥1 indicator selected), default ON, persisted to `localStorage` key `rbq.cache.chart-signals.v1`.

**Overlay persistence** — `localStorage` key `rbq.cache.chart-overlays.v1` (JSON array of overlay keys). Hydrated in `onMount` (not `$state()` init, which would run on the server during SSR). Save guard: `_overlaysHydrated` flag prevents the persist `$effect` from overwriting stored prefs during the brief `[]`-to-hydrated window.

**CSS class selectors** — every overlay `<path>` carries `class="overlay-{type}"` (`overlay-sma`, `overlay-ema`, `overlay-vwap`, `overlay-bb`, `overlay-rsi`, `overlay-macd`). Signal markers carry `class="signal-marker signal-{type}"` (`signal-buy` / `signal-sell`) for stable Playwright locators.

**_bandH** — `$derived((_showRsi ? RSI_H : 0) + (_showMacd ? MACD_H : 0))` reserves SVG space at the bottom for sub-panels. `_innerH = chartH - CPAD_T - CPAD_B - _bandH`.

**Unit tests** — `frontend/scripts/indicators.test.js` (52 tests, `node --test`) covering both indicator math and the five signal-detection helpers. Five dimensions: SSOT (hand-calculated reference values + crossover fixtures), Perf (sync-only), Stale (no duplication in ChartWorkspace), Reuse (same import), UX (edge cases: empty, N=0, constant series, null-tolerant).

**E2E spec** — `frontend/e2e/chart_overlays.spec.js` covers indicator paths; `frontend/e2e/chart_signals.spec.js` covers buy/sell markers (chromium-desktop + mobile-portrait, 1Y NIFTY/RELIANCE fixtures). Uses `STOCK_URL` (RELIANCE) for VWAP/MACD tests — NIFTY 50 is an index with zero volume.

**Price history** (`/api/charts/*`) — in-memory rolling per-symbol buffers + lifecycle markers from `AlgoOrder` rows. No new persistent state.

**Batch endpoint** (`GET /api/charts/batch?mode=…&symbols=a,b,c`) — coalesce N symbols into one round-trip (cap 50). Returns `{mode, charts: [...]}` in input order.

**Chart grid lines** — faint cool-blue grid (0.10 major / 0.07 vertical). PriceChart x-labels HH:MM:SS. Zoom y-range auto-fits to visible data.

---

## MarketPulse + PerformancePage

**Pulse** (symbol grid) — two side-by-side ag-Grid (left: pinned watchlists + movers; right: positions + holdings). Desktop left/right, mobile stacked. Account filter shows Positions/Holdings only when account picked.

**Bucket sort integrity**: `bucketOf` returns pinned (1), watchlist (2), positions (3), holdings (4), movers (5). `postSortRows` keeps sort scoped within bucket.

**Default-visible cluster**: Symbol · 5d · LTP · Avg · Day % · Close · Qty · Day P&L · P&L % · P&L. Deviates from canonical (Avg next to LTP + Day % before Close) intentionally.

**Directional encoding**:
- **Background tint**: pos-long green 10%, pos-short red 10%. row-hold-up green 10%, row-hold-down red 10%, row-hold-flat slate 8%. row-watch amber 10%, row-und violet 10%, row-pos slate 8%.
- **Day P&L mini-bar**: 2px bar at symbol cell right (4px gap from edge). Positions + Holdings only; hidden on TOTAL.
- **CE/PE**: symbol text green/red (Sensibull convention).
- **Account tint** (Positions/Holdings): 14% bg via account hash. No inset bars.
- **TOTAL row**: amber 12% bg + 2px top border + 1px bottom + bold. No direction sign.

**PerformancePage** — canonical-cluster reference. Public page (cream theme) shows real Kite data even during sim. Admin `/dashboard` = P&L Analysis + MarketPulse summary grids (Funds/Positions/Holdings, single-account scoped) + Agent activity log.

**Dashboard layout (`dash-row1-split`)** — chart card LEFT, tabbed NAV / Capital / Equity sidebar RIGHT (Jun 2026 shuffle). NAV is the default tab on the sidebar; renders `NavBreakdown.svelte` which shares the `cash_sod + option_premium + Σ position.unrealised + Σ holdings.cur_val` arithmetic with `PerformancePage` `navByAcct` and `backend/api/algo/nav.py:compute_firm_nav` (v4 formula). Both surfaces source positions / holdings / funds from the module-level `marketDataStores` singletons so the dashboard NAV tab and the `/performance` NAV grid can't drift. Grid cells stretch vertically via flex so the sidebar height responds to whichever chart tab is active.

**NavStrip pill cluster** — PerformancePage + MarketPulse header shows SSOT snapshot
data (frozen during closed hours until next market open). Pill layout: P / M / C / H
(slash-joined trios where applicable):
- **P** (P&L) — `today / lifetime / expiry` (intraday delta / cumulative since inception / F&O expiry profit at current spot)
- **M** (Margin) — `available / total` (used margin as fraction of sanctioned total)
- **C** (Cash) — `available / total` (same framing as Margin)
- **H** (Holdings) — `today / value / lifetime` (intraday delta / current market value / cumulative cost).

**P expiry value** — computed client-side in `PositionStrip.svelte` from `positionsStore` rows. Identical math to `_byUnderlyingExp` rollup in `/admin/derivatives` (parity with the TOTAL row when no account/strategy filter applied). Futures + options only (exchange in NFO/MCX/CDS/BFO); equity excluded. Math per leg:
- Futures: `(live_ltp − avg) × qty` (qty in contracts; multiplied by `broker_apis.py` before positionsStore)
- Options: `(intrinsic(underlying_spot, strike, opt_type) − avg) × qty` where `intrinsic = max(spot−strike,0)` (CE) or `max(strike−spot,0)` (PE). `underlying_spot` resolved via `resolveUnderlying(inst.u, findNearestFuture)?.tradingsymbol` → `symbolStore.get(tradingsymbol)?.ltp` (e.g. `"NIFTY"` → `"NIFTY 50"`). Spots pre-fetched by `_loadUnderlyingSpots()` via `batchQuote` + `publishPulseQuotes` on each 30s poll. If underlying spot unavailable, leg contributes 0 (no phantom intrinsic). Renders amber (#fbbf24, `.ps-exp` class). Gated by `_throttledTick` (4 Hz) like other live deriveds.

Snapshot SSOT replaces localStorage `strip.frozen` cache during closed hours;
in-session reload restored via disk cache. Latest-batch CTE in positions/holdings
snapshot readers anchors on `MAX(captured_at) per account` — no stale months-old rows.

**Public-theme row bars** — left + right edges on symbol cell only (`.ag-col-sym`). Background tint extends symbol + account cells (`.ag-col-fill`).

---

## Activity surface architecture

Unified log viewer with multiple mount points (modal, card, page), all sharing
two reusable components + filter state:

**Components**:
- `ActivityLogSurface.svelte` — wraps LogPanel with canonical config
  (`multiColumn=true, hideInlineAccountFilter=true`). Bindable: `accountFilter`,
  `availableAccounts`, `levelFilter`. Consumers pass their own filter state.
- `ActivityHeaderFilters.svelte` — bundles ActivityAccountSelect + log-level
  dropdown (All/Error/Warning/Info, default 'All'). Consumers embed in card header
  or page header.
- `LogPanel.svelte` — refined with per-tab level parsing + multi-column layout
  at ≥900px container width.

**Four mount points**:
- **ActivityLogModal** — full-screen modal (e.g., from navbar Log icon). Shows
  all tabs (System / Conn / Agents / Orders / Terminal) with independent filters.
- **Activity card** (`/admin/execution`, `/orders`) — inline card with single
  tab (Orders), filters live in card state.
- **Dashboard activity card** (`/dashboard`) — replaces the legacy MARKET NEWS
  strip (Jun 2026). `defaultTab='news'` so the dashboard still lands on the
  market headlines flow, but a click switches to Orders / Agents / Terminal /
  Conn / System / Ticks for the wider operator paper trail without leaving
  the page.
- **`/activity` page** — bookmarkable route (part of navbar `build` dropdown).
  Defaults to Orders tab. Filters persist across tab switches via single shared
  thread.

**Filter persistence** — filters live in parent component state; LogPanel receives
as bindable props per-tab. Same thread (component instance) keeps filters in sync
when switching tabs without resetting selections.

**Multi-column layout** — CSS `column-count: 2` at ≥900px container width
(NewsList-style magazine flow). Single column below 900px. All four mounts
(modal / dashboard card / orders card / page) apply the same responsive
pattern. NewsList accepts `columns={n}` (default 1) and `showSource={bool}`
(default true); the activity-surface News tab passes `columns={2},
showSource={false}` so the per-row source pill collapses and the title
runs the full row width.

**Log-level parsing**:
- System/Conn lines: extract `[LEVEL]` token from message text (case-insensitive)
- Agent rows: map `event_type` → level (e.g., agent-fired = info, agent-error = error)
- Order rows: unchanged (no level token, all info by default)

**Endpoint** — `/api/admin/logs/conn` tails `/opt/ramboq/.log/conn_log_file`.
Path resolver prefers absolute `/opt/ramboq` over CWD-relative so dev API
(running from `/opt/ramboq_dev`) still accesses shared prod conn log.

---

## Canonical card-header rule

Every algo page card follows ONE structure:

```
[Title]  [Tabs?]  [AccountMultiSelect?]  [Chips?]  → spacer →  [Trio]
```

**Default mode** (not fullscreen): Collapse · Fullscreen. Cyan-400 palette (`#22d3ee` rest / `#67e8f9` hover, bg α 0.14, border α 0.55). 1.4rem buttons, 0.3rem gap.

**Fullscreen mode**: Collapse hidden · Default shown · Fullscreen self-hides · optional RefreshButton. Same cyan palette. DefaultSizeButton glyph = Windows restore icon.

**Width**: 100%; box-sizing: border-box. Collapse never shrinks horizontally.

**Page-header** (every algo page): `<div class="page-header">` wrapping `<span class="algo-title-group">` (title) · `<span class="algo-ts">` (now) · `<span class="ml-auto">` (spacer) · `<span class="page-header-actions">` (RefreshButton + page chips + PageHeaderActions).

**RefreshButton**: cyan-400. Swaps glyph on loading (spin → arc-spinner, not rotated arrow). Native tooltip: "Refresh — 1 of 2 broker accounts loaded | Failed: ZG#### | Last refreshed: Sun 30 May · 21:42 IST · 12:12 EDT". Badge states: grey `?` (API unreachable) · green count (all loaded) · amber count (partial) · red count (none) · nothing (no brokers). Clicking during market-closed shows informational popup ("Both NSE and MCX are currently closed").

**PageHeaderActions**: Order (amber gradient), Chart (cyan gradient), Log (violet gradient). Order/Chart hidden when not contextually applicable. Chart without symbol navigates to `/charts` workspace.

---

## Algo navbar + Agent workspace tabs

**Navbar groups** (in `(algo)/+layout.svelte`): `{monitor, analyze, explore, build, config}`. Monitor/Explore inline; Build/Config dropdown. Mobile drawer sections.

**Resequenced** — frequency-based: Tour · Pulse · Dashboard · Orders · Derivatives · Charts · Automation · Strategies · NAV. Orders before analysis. Strategies/NAV weekly cadence moved to end.

**Workspace tabs** (`AutomationTabs.svelte`) — Agents · Order Templates · Agent Templates · Activity · Tokens · Lab. Canonical strip on every agent-adjacent page. 308-redirects from old `/agents/*` URLs for bookmarks.

---

## Order placement + multi-account basket

**OrderTicket.svelte** — unified modal. DRAFT/PAPER/LIVE routes. Account required. Qty validated (must be lot multiple). Pre-fills from calling page context. Success feedback inline (green `✓`) before auto-close.

**Basket orders** — `POST /api/orders/basket` groups legs by account, `asyncio.gather` per-account place. Shared `basket_tag=ramboq-basket-<uuid>`.

**Target profit** — `AlgoOrder` gained `target_pct, target_abs, parent_order_id, basket_tag`. On parent fill, auto-attach TP flip-side order (idempotent via `parent_order_id IS NULL`). Default `algo.default_target_pct` (0.30).

**Preflight parallelized** — 4 sequential awaits (`profile`, `instruments`, `basket_order_margins`, `margins`) → `asyncio.gather`. Wall-time ~300ms (was 800-1200ms).

**`_TICK_INDEX` dict** — O(1) tick lookup (was O(N) linear scan). Module-level dict from instruments cache; identity flip triggers rebuild.

**PAPER skips preflight** — `PaperTradeEngine.register_open_order` already validates via basket_margin; no duplicate work.

---

## Post-fill handling + postback scaffold

**Postback fan-out** (`order_postback`, all brokers):
1. `invalidate("orders")` always
2. On COMPLETE/CANCELLED/REJECTED/EXPIRED: `invalidate("positions", "holdings")` + broadcast `book_changed` event
3. On COMPLETE: broadcast `position_filled` (qty delta for optimistic patch)
4. Audit log entry tagged `order.fill / order.cancel / order.reject / order.expired`
5. Optional template-attach on FILL (idempotent via `attached_gtts_json IS NULL` guard)

**BroadcastBus pattern** — backend invalidation + broadcast → frontend WS subscriber → `bookChanged` store (monotonic counter) → `$effect` debounced refetch (200ms).

**Surfaces wired**: `/admin/derivatives`, `/dashboard`, `/pulse`, `/orders`, `/performance`.

**Dhan/Groww postback scaffolds** — `POST /api/orders/{dhan,groww}_postback` routes with shared `_process_broker_postback` helper. Status normalization via `_DHAN_STATUS_TO_KITE` / `_GROWW_STATUS_TO_KITE` tables.

---

## MCP server + Lab page

`/admin/research` — thread persistence + audit + token mint UI. Operator chats in Claude Code; backend serves 17 read-only tools + 2 persist tools + 6 gated write tools = 25 total.

**MCP server** ([backend/mcp/kite_server.py](backend/mcp/kite_server.py)) — FastMCP subprocess. Tools: positions, holdings, quote, ohlcv, news, chain, macro, agents, threads, audit, dry_run, server_info, place/cancel/modify orders, activate/deactivate/update agents, save thread/draft.

**Confirm-token gate** (60s TTL, single-use, purpose-hash bound) — mint token for `place | cancel | modify | activate | deactivate | update`. Purpose hash prevents bait-and-switch.

**McpAudit table** — tool, user_id, args_redacted, result_status, result_summary, request_id. Daily cleanup (default 90-day retention).

---

## Investor portal + NAV

LP-facing token-gated `/investor/<token>` surface. Token IS the credential. Operator mints per-user via `/admin` Portal button (90-day default, cap 10y). Tracks last_visit_at + visit_count.

**Active check**: `revoked_at IS NULL AND expires_at > now()`.

**Units model** (slice 7N) — All surfaces route through `investor_units.py`:
```
units_held = Σ units_delta for events ≤ t
total_units = Σ units_held across LPs
nav_per_unit = firm_nav / total_units
slice = units_held × nav_per_unit
cost_basis = Σ amount (sub/bootstrap) − Σ amount (redemption)
pnl = slice − cost_basis
```

**Auto-bootstrap** — for each eligible LP (is_active=True, share_pct > 0) without events, inserts synthetic bootstrap event at contribution_date (or created_at fallback).

**Day delta** — slice(today) − slice(prior) via same event set. Subscriptions between snapshots inflate both slice + cost_basis (no P&L double-count).

**Three endpoints**:
- `GET /api/nav/me` — current slice + day delta
- `GET /api/nav/me/history?days=180` — scaled NAV curve
- `GET /api/investor/{token}/slice` — same math (no auth required, token in URL)

**NAV v4 formula** — firm NAV computed as:
```
firm_nav = cash_sod + option_premium + Σ position.unrealised + Σ holdings.cur_val
```

Changed post-Sprint E: `option_premium` replaces full `used_margin` term to eliminate
double-counting. `option_premium` = sum of long-option premiums only (operator-verified
spec); `used_margin` includes futures SPAN already captured in position.unrealised.
Implemented in three sites: `backend/api/algo/nav.py:compute_firm_nav`, 
`frontend/src/lib/PerformancePage.svelte:navByAcct`, `scripts/nav_breakdown.py`.

**NavCard ↔ grid sync** — `/api/auth/firm-nav` and `/api/auth/me/nav` endpoints now
delegate NAV computation to `backend/api/algo/nav.py:compute_firm_nav`, ensuring
NavCard headline matches `/performance` TOTAL row exactly. Day-PnL and cum-PnL still
use intraday_equity deque path (unchanged).

---

## Audit log + History

**AuditLog** schema — id, actor_user_id / username / role (snapshotted), action, **category** (nullable), method, path, target_type, target_id, status_code, summary, request_id, client_ip, created_at.

**Two write paths**:
1. **AuditMiddleware** — every HTTP request (skips non-mutating + suppress prefixes). Captures actor from JWT, status from response, body summary (first 1KB).
2. **`write_audit_event()`** — non-HTTP (broker postback, agent action, system tasks). Used by order.fill / order.cancel / agent.action / system.statement / system.nav.

**Category routing** (`_PATH_CATEGORY_RULES`) — path prefix matches category (order.place / order.fill / order.modify / order.cancel / user / config / config.broker / config.grammar / config.fragment / config.hedge / system.statement / system.nav / agent / strategy / http).

**Failed mutations gate** (`alert.log_failed_mutations`, default False) — when ON, 4xx/5xx rows also logged (defect tracking).

**`/admin/history`** (cap `view_audit`) — three tabs over `algo_orders` + `daily_book`:
- **Orders** (30 days, 50/page, cap 500) — status histogram, mode filter
- **Trades** (`daily_book[kind='trades']`, 30 days) — summary.total_notional = Σ qty × avg_cost
- **Funds** (`daily_book[kind='funds']`, 90 days unpaged) — per account/segment/day; cash_delta computed server-side (diff prior); Dhan adapter `funds_ledger` implemented

**Per-row audit drill** — `algo_orders.request_id` (nullable) captures postback; `/admin/audit?request_id=<uuid>` pre-filters.

---

## Settings (DB-backed tunables)

`/admin/settings` exposes parameters changing faster than deploy cycle. Reader chain: **DB cache → YAML fallback → in-code default**.

**Schema** (`Setting` table) — category, key, value_type, value, default_value, description, schema, units.

**Seeded buckets**:
- `alerts.*` — cooldown_minutes, rate_window_min, baseline_offset_min, suppress_delta_abs/_pct
- `performance.*` — refresh_interval, open_summary_offset_min, close_summary_offset_min
- `simulator.*` — positions_every_n_ticks, auto_stop_minutes, default_rate_ms
- `notifications.*` — telegram_enabled, email_enabled, notify_on_deploy
- `logging.*` — file_log_level, console_log_level, error_log_level
- `hedge_proxies.*` — regression_enabled, regression_window_days, regression_max_age_days

**Seeder** — insert missing rows; refresh all columns (code changes land); **preserve `value`** (operator edits); auto-prune retired keys.

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

## Critical math guards

**Option qty vs lot_size** — Kite ships MCX intraday fields in lots, NSE in contracts. Double-check every multiplication. Has caused multi-lakh P&L distortion + 20× over-orders.

**GTT layer also enforces translate_qty** — `apply_plan_live` in `template_attach.py` must call `broker.translate_qty(exchange, raw_qty, lot_size)` for EVERY GTT leg AND for the wing order before calling `broker.place_gtt` / `broker.place_order`. `place_gtt` in `kite.py` does NOT auto-translate and has no adapter guard unless the qty is > 50 lots (ceiling). Incident (2026-07-02, audit aad6e8cb): 1-lot MCX CRUDEOIL position (qty=100 contracts) sent `quantity=100` to GTT → Kite read it as 100 lots. Fix: `parent_lot_size` baked into `TemplatePlan` at resolve-time via `await get_lot_size()` in the async `apply_template_to_order`; `apply_plan_live` calls `broker.translate_qty` per leg then the adapter ceiling in `place_gtt` provides last-line defense. Never pass raw contract qty from `parent_qty` directly to `place_gtt`.

**G1 fires on ALL close paths** — `run_preflight(account, {..., "intent": "close"})` is called before `chase_order` in `_action_live_close_position` AND per-position in `_action_live_chase_close_positions`. G1 (LOT_MULTIPLE) fires even for closes; G2 (FAT_FINGER_5_LOT_CAP) is bypassed via `intent="close"`. `_arm_take_profit` live path has an inline G1 guard (no `run_preflight` call — intentional; G2 skipped there too). A blocked close writes a REJECTED AlgoOrder row and sends an alert; the chase_close loop uses `continue` not `raise` so other positions proceed. The 50-lot adapter ceiling in `kite.py:place_order` has NO intent bypass — 51-lot closes are hard-blocked unconditionally at the adapter.

**Kite close_price stale overnight** — positions.close_price + quote.ohlc.close lag prior-session EOD between MCX close and next open. Use `daily_book.ltp` instead.

**Day P&L formula** — Decomposed intraday (not naive `(LTP−close)×qty`). Positions: `overnight_qty × (LTP − prev_close) + day_buy/sell legs`. Holdings: `broker.pnl − (close − cost) × opening_qty`. MCX guard: apply lot_size to intraday qty too.

---

## Common Tasks — Where to Make Changes

| Task | Files |
|---|---|
| Add new page | SvelteKit route + nav entry in `+layout.svelte` |
| Change page content | `backend/config/frontend_config.yaml` |
| Change Gemini prompt | `backend/config/frontend_config.yaml` — `genai_system_msg`, `genai_user_msg`, temp, tokens, model |
| Change retry behaviour | `backend/config/backend_config.yaml` — `retry_count`, `conn_reset_hours` |
| Change log verbosity | `backend/config/backend_config.yaml` — file/error/console levels |
| Add broker account | `backend/config/secrets.yaml` — `kite_accounts` |
| Change deploy routing | `webhook/dispatch.sh` → copy to `/etc/webhook/dispatch.sh` |
| Change tab title / SEO | `frontend/src/app.html` + per-route `<svelte:head>` |
| Change footer | `backend/config/frontend_config.yaml` — footer_name, footer_text2, footer_mobile_text3 |
| Change loss threshold | `/agents` page → edit `loss-*` agent condition tree `value` |
| Change alert recipients | `backend/config/secrets.yaml` on server — `alert_emails`, `telegram_chat_id` |
| Deploy notification | `backend/config/backend_config.yaml` on server — `notify_on_startup` |
| Market hours | `backend/config/backend_config.yaml` — `market_segments` |
| Summary timing | `backend/config/backend_config.yaml` — `open/close_summary_offset_minutes` |
| Order-entry grammar | `backend/config/grammars/orders.yaml` (frontend symlink) |
| Toggle agent default status | `backend/api/algo/agent_engine.py` — `status=` in `BUILTIN_AGENTS` |
| Add MCP tool | `backend/mcp/kite_server.py` @app.tool() + update `/admin/research` TOOLS const |
| Tune MCP audit | `/admin/settings` → `mcp.audit_retention_days` (default 90) |
| Update macro data | `backend/config/backend_config.yaml` `macros:` block (preserved on deploy) |
| Day P&L formula | `backend/api/algo/pnl_math.py` — `decomposed_intraday_pnl` + `naive_day_pnl`. Both polars (broker_apis) and pandas (positions route) call it. |
| NAV breakdown (frontend) | `frontend/src/lib/data/nav.js` — `navByAccount` + `navTotalRow`. PerformancePage + NavBreakdown consume. Backend equivalent: `backend/api/algo/nav.py:compute_firm_nav`. |
| LTP-override scaffold | `backend/api/helpers/ltp_patch.py` — `apply_ltp_patch(df, policy)` shared by positions + holdings routes. Each route passes its own `policy(current, tick_ltp) -> Decision`. |
| Mask account in text | `backend/shared/helpers/utils.py:mask_account_in_text` — JSON-string scrubber for non-admin viewers. Routes every match through canonical `mask_account()`. |
| Postback fan-out | `backend/api/routes/orders.py:_postback_broadcast_fanout` — cache-invalidate + WS-broadcast trio (orders/positions/holdings + order_update/position_filled/book_changed). Kite (inline) and Dhan/Groww (via `_process_broker_postback`) both delegate. |
| Percentage formatters | `frontend/src/lib/format.js` — `fmtPctScaled(v, dp, signed)` for already-percent inputs; `fmtPctFraction(v, dp, signed)` for fractional inputs (e.g. 0.05). |
| Chart self-heal threshold | `/admin/settings` → Persistence → `chart_self_heal_coverage_threshold` (default 0.70). Auto-fetch from broker if <70% of requested days present in DB. |
| Backfill admin endpoint | `POST /api/admin/persistence/backfill?kind=daily\|intraday\|both` (admin-guarded). Starts async coverage repair for 300-symbol universe. |
| Backfill CLI (immediate) | `scripts/persistence_mode.py off\|soft\|hard\|status` (reads operator login) + `scripts/backfill_ohlcv.py --daily --intraday` for prod defect-recovery. |

---

## History

For completed-slice notes, sprint diaries, multi-wave refactor anecdotes, and the
"why a rule was established" history, see [CLAUDE_HISTORY.md](CLAUDE_HISTORY.md).
