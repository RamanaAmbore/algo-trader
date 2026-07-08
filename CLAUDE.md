# CLAUDE.md — RamboQuant Project Reference

For Claude Code. Three-layer architecture reference + guardrails. Sprint diaries + 
completed-slice history live in [CLAUDE_HISTORY.md](CLAUDE_HISTORY.md).

---

## Contents

**Orientation** — [Multi-agent coordination](#multi-agent-coordination-read-first) · 
[Project Overview](#project-overview) · [Deployment](#deployment)

**Layer 1: Broker Connection** — [File map](#layer-1-broker-connection-backendbrokers) · 
[KiteTicker / SSE pipeline](#kiteticker-sse-live-ltp-pipeline) · [Broker accounts (CRUD)](#broker-accounts-db-backed-crud) · 
[IPv6 source binding](#multi-account-ipv6-source-binding-kite-dhan) · [Broker resilience](#broker-resilience)

**Layer 2: Backend API** — [File map](#layer-2-backend-api-backendapi) · 
[Alert and Notification System](#alert-and-notification-system) · [Market Segments](#market-segments) · 
[Background Tasks](#background-tasks) · [Market lifecycle events](#market-lifecycle-events) · 
[Persistence pipeline](#persistence-pipeline-cache-db-broker) · [Execution Modes](#execution-modes-1-5) · 
[Agents Framework](#agents-framework) · [Symbol resolution + virtual roots](#symbol-resolution-virtual-roots-mcx-cds) · 
[Investor portal + NAV](#investor-portal-nav) · [Audit log + History](#audit-log-history) · [Settings](#settings-db-backed-tunables)

**Layer 3: Frontend** — [Persistent cache layer](#frontend-persistent-cache-layer) · 
[NavStrip pill cluster](#navstrip-pill-cluster) · [MarketPulse + PerformancePage](#marketpulse-performancepage) · 
[Activity surface](#activity-surface-architecture) · [Chart Workspace](#chart-workspace) · 
[Keyboard shortcuts](#keyboard-shortcuts) · [Order placement + basket](#order-placement-multi-account-basket)

**Cross-cutting** — [Key Patterns](#key-patterns) · [Derivatives Analytics](#derivatives-analytics) · 
[Proxy hedges](#proxy-hedges) · [MCP server + Lab](#mcp-server-lab-page) · [Performance measurement](#performance-measurement) · 
[Things to Avoid](#things-to-avoid) · [Critical math guards](#critical-math-guards) · 
[Common Tasks](#common-tasks-where-to-make-changes)

---

## Multi-agent coordination (read first)

Specialized subagents in `~/.claude/agents/` dispatched in parallel by default:

| Agent | Layer | Use | Model |
|---|---|---|---|
| `broker` | Layer 1 | `backend/brokers/` — connections, ticker, service, adapters, resilience | sonnet |
| `backend` | Layer 2 | `backend/api/` — routes, models, background, persistence, algo engine | sonnet |
| `frontend` | Layer 3 | `frontend/` — SvelteKit, Svelte 5, ag-Grid | sonnet |
| `backend-test` | Layer 1+2 | pytest + pytest-asyncio — broker + API tests | haiku |
| `playwright` | Layer 3 | Playwright e2e — browser flows, mobile viewport | haiku |
| `audit` | All | Read-only defect review — no writes | sonnet |
| `doc` | All | CLAUDE.md / USER_GUIDE.md / ADMIN_GUIDE.md | haiku |

**Parallel by default** — independent sub-tasks fire together. Sequence only when 
one output feeds another or when audit finds defects.

---

## Project Overview

**RamboQuant** — production web app at ramboq.com. Portfolio tracking, Gemini AI 
market updates, multi-broker trading.

- **Stack**: Litestar API + SvelteKit frontend
- **Deployment**: Single codebase, prod (`main`) + dev (branches)
- **Database**: PostgreSQL 17 (async SQLAlchemy 2.x); `ramboq` (prod) / `ramboq_dev` (dev)
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
| Conn service | both | `/opt/ramboq` (shared) | UDS | `/tmp/ramboq_conn.sock` |

Push → webhook → `dispatch.sh` → `deploy.sh` → restart ramboq_api + ramboq_dev_api. 
Conn service restarts only if broker-layer files changed (via `CONN_TOUCHED` flag).

---

## Layer 1: Broker Connection (`backend/brokers/`)

Centralized broker layer. All Kite/Dhan/Groww logic lives here: adapters, 
connection management, ticker streaming, credential encryption.

### File map

- **`adapters/{kite,dhan,groww}.py`** — broker-specific implementations
- **`connections.py`** — Connections singleton, 2FA, token refresh, IPv6 binding, 
  DB-backed Fernet encryption. On `RAMBOQ_USE_CONN_SERVICE=1`, returns RemoteBroker stubs.
- **`broker_apis.py`** — `fetch_holdings / positions / margins` + `@for_all_accounts`. 
  Holiday calendar cached per (exchange, date). Proxies to conn_service when enabled.
- **`kite_ticker.py`** — TickerManager singleton (one WebSocket per process). On 
  `RAMBOQ_USE_CONN_SERVICE=1`, local API reads `/dev/shm/ramboq_ticks` via MmapTickReader.
- **`service/`** — standalone Litestar app on UDS. Owns all broker sessions.
- **`client/`** — sync/async clients for UDS + HTTP RPC to conn_service.

### KiteTicker / SSE live-LTP pipeline

**Architecture**: WebSocket in conn_service. Ticks written to `/dev/shm/ramboq_ticks` 
(4096 slots, atomic version-word). Main API reads at byte-read latency via MmapTickReader. 
Background poller (50ms) publishes deltas to local BroadcastBus.

**Failover**: 30s watchdog in conn_service. On failure, retries `_try_start_ticker()`.

**Universe registration invariant** — `MmapTickReader._sym_to_token` MUST be populated 
before first mmap tick arrives. Implementation: `_task_sparkline_warm._register_universe_with_ticker` 
runs at startup + segment-open boundaries (00:30 IST, 09:00 MCX, 09:15 NSE).

**Health surface** (`GET /api/admin/health`) — `ticker.stale_count`, `ticker.max_age_seconds`, 
`ticker.stale_top`.

---

## Broker accounts (DB-backed CRUD)

Operators manage via `/admin/brokers`. Credentials encrypted at rest (Fernet, 
derived from `cookie_secret` via HKDF-SHA256).

**Loading** (`Connections.rebuild_from_db` on startup + post-CRUD):
1. Query `broker_accounts` (active rows).
2. If empty AND `secrets.yaml` has `kite_accounts`: seed DB once.
3. Decrypt in memory, rebuild `self.conn` map.

**API** (`/api/admin/brokers/*`, admin-guarded):
- `GET` — list / read single (no secrets)
- `POST` / `PATCH` / `DELETE` — create / update / remove
- `POST /test` — verify via `broker.profile()`

**Canonical display order** — `broker_accounts.display_order` (INTEGER, default 500) 
controls sort across all UI surfaces (BrokerHealthBadge, AccountMultiSelect, 
PerformancePage, MarketPulse, OrderTicket, derivatives). Seeded on first startup.

**Backend sort helper** (`backend/brokers/broker_apis.py`): `sort_accounts()` reads 
`get_account_order_map()` (60s TTL cache), sorts by `(display_order ASC, account_id ASC)`.

**Frontend** (`frontend/src/lib/data/accountSort.js`): `accountDisplayOrder` singleton. 
All UI surfaces call `sortAccountsBy(rawList, $orderMap)`. Do NOT sort inline by `localeCompare`.

---

## Multi-Account IPv6 Source Binding (Kite + Dhan)

**Why**: Kite = one IP per app. Dhan = one token per source IP. Groww = no per-IP rule.

**Current setup**:

| Account | Broker | source_ip |
|---|---|---|
| ZG0790 | Kite | `69.62.78.136` (IPv4) |
| DH6847 | Dhan | `69.62.78.136` (shared) |
| ZJ6294 | Kite | `2a02:4780:12:9e1d::1` (IPv6) |
| DH3747 | Dhan | `2a02:4780:12:9e1d::1` (shared) |
| GR87DF | Groww | default |

**Implementation**: `_IPv6SourceAdapter` extends `requests.HTTPAdapter`. Groww SDK 
monkey-patched at module-level.

**Per-account stabilizer** — Dhan rows grouped by `source_ip`. If 2+ Dhan on same IP, 
only lowest-`priority` loaded.

---

## Broker resilience

**Circuit breaker** (Jul 2026, P0 DH6847 fix):
Per-account opt-in state machine. `circuit_breaker_enabled` column on `broker_accounts` 
(default FALSE). Currently DH6847 only.

- **CLOSED** (normal) — every fetch runs.
- **OPEN** (after 3 consecutive failures) — short-circuit; return empty DataFrame. 
  Cool-off exponential: 5m → 10m → 20m → 30m (cap).
- **HALF-OPEN** (after cool-off) — next probe runs. Success → CLOSED. Failure → OPEN.

State stored in `_FETCH_HEALTH[account]`: `consecutive_fail_count`, `circuit_open_until`, 
`circuit_last_opened_at`, `open_cycle_count`.

**Per-account Dhan poll priority**:
Column `poll_priority VARCHAR(8)` controls cadence for Dhan (Kite + Groww unaffected).
- `'hot'` (default) — 30s
- `'warm'` — 120s
- `'cold'` — 600s

**Auto-downgrade**: When ≥5 breaker-OPEN events within 15-min window, auto-set to 'cold'. 
Operator restores via `POST /api/admin/brokers/{id}/restore-priority`.

`/api/admin/broker-health` surfaces `circuit_state`, `consecutive_fail_count`, 
`circuit_open_until`, `circuit_breaker_enabled` per account.

---

## Layer 2: Backend API (`backend/api/`)

### File map

**Stack**: Litestar 2.x + msgspec.Struct (10× faster pydantic) · PostgreSQL 17 + 
SQLAlchemy 2.x async + asyncpg · Polars (routes: positions, holdings, funds, nav) / 
pandas (broker SDK boundary) · asyncio background tasks · JWT HS256 (24h) · 
PBKDF2-SHA256 passwords

**Core routes** (`backend/api/`):
- `app.py` — Litestar app + startup (init_db + tasks)
- `routes/algo.py` — Agents API + WebSocket
- `routes/orders.py` — OrdersController (<1500 LOC after split)
- `routes/orders_place.py` — Ticket handler, guards, template-attach (~1270 LOC)
- `routes/orders_postback.py` — Kite postback, Dhan/Groww shared (~475 LOC)
- `routes/orders_basket.py` — Basket dispatch, margin (~530 LOC)
- `algo/agent_engine.py` — Declarative agent runner + `run_cycle()`
- `algo/simulator.py` / `sim/driver.py` — Market sim + scenario engine

---

## Alert and Notification System

**Vocabulary**: Agent (rule) → Alert (event) → Notify (delivery) → Action (side-effect).

**Message types**:

| Event | Telegram | Email |
|---|---|---|
| Market open | `Open` | (Telegram only) |
| Agent fire | `Agent` | `RamboQuant Agent: ` |
| Market close | `Close` | (Telegram only) |

**Timestamp**: `timestamp_display()` produces `Mon 30 Mar 09:30 IST | Mon 30 Mar 10:00 EDT`.

**Global gates** (`backend_config.yaml`):
- `alert_cooldown_minutes` (30) — min time between refires
- `alert_baseline_offset_min` (15) — silent after session start
- `alert_rate_window_min` (10) — P&L window for rate calculation
- Market hours gate: skip `schedule: market_hours` agents outside segment-open hours

---

## Market Segments

| Segment | Exchanges | Hours (IST) |
|---|---|---|
| Equity | NSE, BSE, NFO, CDS | 09:15–15:30 |
| Commodity | MCX | 09:00–23:30 |

Open/close summaries at `open_summary_offset_minutes` / `close_summary_offset_minutes`. 
Weekends hardcoded closed; use `market_special_sessions` table for exceptions (e.g. Muhurat).

**Special-session overrides** (`market_special_sessions` table) — highest-precedence 
rule. Row `(exchange, date, start_time, end_time)` says: on that date, exchange open 
ONLY during `[start_time, end_time)` IST. Beats holiday check, regular windows, weekend rule.

**Winners/Losers movers gate**:
- NSE open → NSE equity universe
- NSE closed + MCX open → Live MCX commodity movers (CRUDEOIL, GOLD, SILVER, …)
- Both closed → NSE DB snapshot fallback

---

## Background Tasks

| Action | Timing |
|---|---|
| Market cache warm | Startup + daily 08:30 IST |
| Performance refresh | Every 5 min during market hours |
| Open/close summaries | Per segment, once daily |
| Loss alert check | Per performance fetch |
| Agent `run_cycle()` | Per performance fetch |
| Sparkline warm | Startup, 00:30 IST, segment opens |
| Ticker watchdog | Every 30s |
| Trail stop / OCO pair-watcher | Every 30s |
| Hedge proxy regression | Daily 02:30 IST |
| Market lifecycle poller | Every 30s |
| Funds-off-hours refresh | Every 30 min when no segment open |

**Ticker subscription DB backstop**: `_task_sparkline_warm` + `_task_performance` 
union `daily_book` DB query (past 7 days positions/holdings) into Kite subscription 
universe. Survives conn_service restart, circuit-breaker open, broker unhealthy at boot.

---

## Market lifecycle events

Singleton `MarketLifecycle` polled every 30s by `_task_market_lifecycle`.

| Event | Fires |
|---|---|
| `<exch>:open` | At session open (calendar-aware) |
| `<exch>:close` | At session close |
| `<exch>:close_settled` | 15 min after close (operator-tunable `settled_offset_min`). Captures broker's adjusted close_price. Before firing, re-probes `is_market_open`; if exchange reopened (evening MCX-style), skips to avoid capturing mid-session as "settled". |

**Default handlers** (`backend/api/algo/market_lifecycle_handlers.py`):
- `nse:close` + `nse:close_settled` → `snapshot_daily_book(settled=…)` + `snapshot_sparkline(settled=…)` + NAV snapshot
- `mcx:close` + `mcx:close_settled` → same as NSE
- `cds:close` + `cds:close_settled` → same as NSE

Snapshot idempotent via UPSERT on `(date, account, kind, symbol)`. The `settled` flag 
lands in `daily_book.payload_json.snapshot_extras.settled` so downstream readers 
distinguish initial close from settled follow-up.

---

## Persistence pipeline (cache → DB → broker)

Three-tier read hierarchy: Tier 1 (in-memory LRU) → Tier 2 (PostgreSQL) → Tier 3 (broker).
Per-key asyncio.Lock deduplicates concurrent fetches. Broker writes return immediately; 
persistence runs off-path via worker coroutines.

**Stores** (`backend/api/persistence/`):
- `ohlcv_store` — daily bars, (sym, exch) key
- `instruments_store` — per-exchange symbol→token map, daily TTL
- `holidays_store` — per-(exchange, year), immutable once year closes
- `intraday_store` — 5/15/30/60-min bars, 5-min TTL on today

**Completeness checks**:
- OHLCV: boundary dates present + gaps ≤4 days (weekends + holidays)
- Instruments: non-empty map
- Holidays: non-empty set
- Intraday: today = any bars OK; historical = must span session close

**Refresh-cycle modes** (operator can flip via `POST /api/admin/persistence/mode/{off|soft|hard}`):
- `off` — normal hierarchy (default)
- `soft` — Tier 1+2 bypass, fetch from broker, write-back heals both
- `hard` — soft + ticker recycle

**Retention** (staggered nightly cron):
- `ohlcv_daily` → 5 years
- `instruments_snapshot` → 7 days
- `intraday_bars` → 90 days
- `holidays_snapshot` → forever
- `algo_events` → 30 days (`retention.algo_events_days`)
- `audit_log` → 365 days (`retention.audit_log_days`)
- `nav_daily`, `daily_book`, `investor_events`, `monthly_statements` → forever (financial records)
- All configurable from `/admin/settings` → Retention

**Event queues** (`backend/api/persistence/event_queue.py`):
Generic `EventQueue` class for high-frequency append-only writers. Uses SQLAlchemy 
bulk `executemany` INSERT. Re-queues on transient failure.

| Queue | Table | Location |
|---|---|---|
| `algo_event_queue` | `algo_events` | `routes/algo.py` |
| `agent_event_queue` | `agent_events` | `algo/events.py` |
| `order_event_queue` | `algo_order_events` | `algo/order_events.py` |
| `mcp_audit_queue` | `mcp_audit` | `routes/research.py` |

---

## Execution Modes (1-5)

Five modes form a confidence ladder (sim → paper → shadow → live) + parallel replay.

| Mode | Quote | Engine | Branch | Use |
|---|---|---|---|---|
| 1-Simulator | Fabricated | PaperTradeEngine (sim quotes) | Both | Stress-test agents |
| 2-Paper | Live | PaperTradeEngine (live quotes, 5s) | Both | End-to-end validation |
| 3-Live | Live | Real broker | Prod only | Real orders |
| 4-Replay | Historical OHLCV | PaperTradeEngine | Both | Backtesting |
| 5-Shadow | Live | Log payload (no execute) | Prod only | Pre-live check |

**Branch gate**: Non-main forces paper regardless of DB flags. Main uses DB flags.

**Mode resolution** (`_resolve_mode`): sim > replay > branch check > shadow > paper_trading_mode > agent.trade_mode.

**Navbar pill**: SIM/REPLAY green, PAPER sky-blue, LIVE red, SHADOW orange.

---

## Agents Framework

**Four words**:
- **Agent** = rule row (condition + notify + actions). Seeded from `BUILTIN_AGENTS`. 
  Extensible via `/automation`.
- **Alert** = runtime event when condition fires. Persisted to `agent_events`.
- **Notify** = delivery channel (telegram / email / websocket / log).
- **Action** = side-effect (order placement, modify, cancel, close).

**Loss agents** — prefix `loss-*`. Four consolidated alerting agents + one fund-negative, 
all active by default. Rules editable live from `/automation`.

**Tokens** — `grammar_tokens` table. System tokens seeded on boot, custom via `/admin/tokens`.

**Condition tree** (v2 grammar):
```
condition  ::= leaf | {all: [...]}, {any: [...]}, {not: ...}
leaf       ::= {metric, scope, op, value}
```

**Metrics**: point-in-time (pnl, pnl_pct, day_pct), rate (pnl_rate_abs, pnl_rate_pct), 
rolling-window (mean, max_drawdown, stdev, range), expiry-aware (is_itm, is_ntm, days_until_expiry).

**GrammarRegistry** singleton — in-memory dispatch. Reloaded on `/api/admin/grammar/reload`.

---

## Symbol resolution + virtual roots (MCX / CDS)

MCX commodity futures and CDS currency futures never expose raw contract names in UI.

| Virtual | Maps to |
|---|---|
| `CRUDEOIL` | front-month (current expiry) |
| `CRUDEOIL_NEXT` | back-month (next expiry) |
| `CRUDEOIL26AUGFUT` | far-month — passes through raw |

**Canonical module**: `backend/api/algo/symbol_resolver.py` — SSOT:
- `list_active_futures(root, exchange, limit)` — filters `inst.x > today_iso (IST)`
- `resolve_symbol(virtual, exchange)` — forward resolver
- `root_of(contract, exchange)` — reverse resolver

**Supported roots**: MCX: CRUDEOIL, CRUDEOILM, NATURALGAS, NATGASMINI, GOLD, GOLDM, 
GOLDGUINEA, GOLDPETAL, SILVER, SILVERM, SILVERMIC, COPPER, ZINC, LEAD, ALUMINIUM, 
NICKEL, MENTHAOIL, COTTON, CPO. CDS: USDINR, EURINR, GBPINR, JPYINR.

**API endpoints**:
- `GET /api/symbols/resolve?symbol=CRUDEOIL&exchange=MCX`
- `GET /api/symbols/root_of?contract=CRUDEOIL26JUNFUT&exchange=MCX`

**Frontend** (`frontend/src/lib/data/rootOf.js`):
- `seedRootMapFromInstruments(items)` — called post-instruments-cache-load
- `rootOf(contract, exchange)` — sync, no fetch
- `rootOfLabel(contract, exchange)` — human label
- `resolveVirtual(virtual, exchange)` — forward direction sync

**Rollover**: `inst.x > today_iso (IST)` rule — settling contracts excluded on expiry day.

---

## Investor portal + NAV

LP-facing token-gated `/investor/<token>` surface. Token IS the credential. 
Operator mints per-user via `/admin` Portal button (90-day default, cap 10y).

**Units model**:
```
units_held = Σ units_delta for events ≤ t
total_units = Σ units_held across LPs
nav_per_unit = firm_nav / total_units
slice = units_held × nav_per_unit
cost_basis = Σ amount (sub/bootstrap) − Σ amount (redemption)
pnl = slice − cost_basis
```

**NAV v4 formula** — firm NAV computed as:
```
firm_nav = cash_sod + option_premium + Σ position.unrealised + Σ holdings.cur_val
```

`option_premium` replaces full `used_margin` to eliminate double-counting. 
Implemented in: `backend/api/algo/nav.py:compute_firm_nav`, 
`frontend/src/lib/PerformancePage.svelte:navByAcct`, `scripts/nav_breakdown.py`.

**Three endpoints**:
- `GET /api/nav/me` — current slice + day delta
- `GET /api/nav/me/history?days=180` — scaled NAV curve
- `GET /api/investor/{token}/slice` — same math (no auth, token in URL)

---

## Audit log + History

**AuditLog** schema — id, actor_user_id / username / role (snapshotted), action, 
**category** (nullable), method, path, target_type, target_id, status_code, summary, 
request_id, client_ip, created_at.

**Two write paths**:
1. **AuditMiddleware** — every HTTP request (skips non-mutating)
2. **`write_audit_event()`** — non-HTTP (broker postback, agent action, system tasks)

**Category routing** — path prefix matches category (order.place / order.fill / 
order.modify / order.cancel / user / config / config.broker / config.grammar / 
config.fragment / config.hedge / system.statement / system.nav / agent / strategy / http).

**`/admin/history`** (cap `view_audit`) — three tabs:
- **Orders** (30 days, 50/page) — status histogram, mode filter
- **Trades** (`daily_book[kind='trades']`, 30 days) — summary.total_notional
- **Funds** (`daily_book[kind='funds']`, 90 days) — per account/segment/day

---

## Settings (DB-backed tunables)

`/admin/settings` exposes parameters. Reader chain: DB cache → YAML fallback → in-code default.

**Seeded buckets**:
- `alerts.*` — cooldown_minutes, rate_window_min, baseline_offset_min, suppress_delta_abs/_pct
- `performance.*` — refresh_interval, open/close_summary_offset_min
- `simulator.*` — positions_every_n_ticks, auto_stop_minutes, default_rate_ms
- `notifications.*` — telegram_enabled, email_enabled, notify_on_deploy
- `logging.*` — file/console/error log levels
- `hedge_proxies.*` — regression_enabled, regression_window_days, regression_max_age_days

---

## Layer 3: Frontend (`frontend/`)

### Persistent cache layer

In-memory Map + localStorage (key prefix `rbq.cache.`) for high-churn surfaces. 
Module: `frontend/src/lib/data/persistentCache.js`. TTL buckets: `day` (24h) / `hour` (1h) / 
`minute` (15m) / `short` (2m).

Used by MarketPulse (positions, holdings, sparklines, watchQuotes, movers), 
PositionStrip, NavCard. Survives reload + deploy. Live LTP state NOT cached.

**Tick-flash primitive** — `createTickFlash({threshold, durationMs})` from 
`frontend/src/lib/data/tickFlash.svelte.js`. Canonical 350ms directional pulse 
(green up / red down) on numeric cell updates.

---

## NavStrip pill cluster

PerformancePage + MarketPulse header shows SSOT snapshot (frozen during closed 
hours until next market open).

Pill layout: P / M / C / H (slash-joined trios):
- **P** (P&L) — `today / lifetime / expiry` (intraday delta / cumulative / F&O expiry profit)
- **M** (Margin) — `available / total` (used margin fraction of sanctioned total)
- **C** (Cash) — `available / total` (same framing as Margin)
- **H** (Holdings) — `today / value / lifetime` (intraday delta / current market value / cumulative cost)

**P expiry value** — computed client-side in `PositionStrip.svelte`. Identical math 
to `/admin/derivatives` TOTAL row. Futures + options only (exchange in NFO/MCX/CDS/BFO).

Snapshot SSOT replaces localStorage during closed hours; in-session reload via disk cache.

---

## MarketPulse + PerformancePage

**Pulse** (symbol grid) — two side-by-side ag-Grid (left: pinned watchlists + movers; 
right: positions + holdings). Desktop left/right, mobile stacked.

**Bucket sort**: `bucketOf` returns pinned (1), watchlist (2), positions (3), holdings (4), 
movers (5). `postSortRows` scopes sort within bucket.

**Default-visible cluster**: Symbol · 5d · LTP · Avg · Day % · Close · Qty · Day P&L · P&L % · P&L.

**Directional encoding**:
- **Background tint**: pos-long green 10%, pos-short red 10%. Holdings: up green 10%, 
  down red 10%, flat slate 8%. Watchlist amber 10%, underlying violet 10%, position slate 8%.
- **Day P&L mini-bar**: 2px bar at symbol cell right (4px gap). Positions + Holdings only.
- **CE/PE**: symbol text green/red (Sensibull convention).
- **TOTAL row**: amber 12% bg + borders + bold.

**PerformancePage** — canonical-cluster reference. Public page (cream theme) shows 
real Kite data even during sim. Admin `/dashboard` = P&L Analysis + MarketPulse summary 
grids (Funds/Positions/Holdings) + Agent activity log.

**Dashboard layout** — chart card LEFT, tabbed NAV / Capital / Equity sidebar RIGHT. 
NAV is default tab. Renders `NavBreakdown.svelte` which shares NAV arithmetic with 
PerformancePage + backend `compute_firm_nav`.

---

## Activity surface architecture

Unified log viewer with multiple mount points (modal, card, page), sharing 
components + filter state.

**Components**:
- `ActivityLogSurface.svelte` — wraps LogPanel (multiColumn, hideInlineAccountFilter)
- `ActivityHeaderFilters.svelte` — bundles ActivityAccountSelect + log-level dropdown
- `LogPanel.svelte` — per-tab level parsing + multi-column at ≥900px width

**Four mount points**:
- **ActivityLogModal** — full-screen (from navbar Log icon). All tabs independent.
- **Activity card** (`/admin/execution`, `/orders`) — single tab (Orders).
- **Dashboard activity card** (`/dashboard`) — replaces legacy NEWS strip.
- **`/activity` page** — bookmarkable route. Defaults to Orders tab.

**Log-level parsing**:
- System/Conn: extract `[LEVEL]` token from message
- Agent rows: map `event_type` → level
- Order rows: no level token, all info

---

## Chart Workspace

Unified chart for any symbol kind (underlying / future / option / equity).

**Components**:
- `ChartWorkspace.svelte` — OHLCV (line/area/candle, 1D/1W/1M/3M/6M/1Y), intraday tick 
  overlay (toggleable), underlying-spot overlay (dashed sky-blue), Greeks strip
- `ChartModal.svelte` — overlay wrapper
- `/charts` page — reads URL params, syncs via `goto({replaceState: true})`

**Indicator overlays** (price panel, toggled via MultiSelect):
- `SMA 20` / `SMA 50` — simple moving averages (sky-blue / violet)
- `EMA 20` / `EMA 50` — exponential (green / orange), Wilder k=2/(n+1)
- `VWAP` — cumulative volume-weighted average price (cyan); null for zero-volume
- `BB` — Bollinger Bands ±2σ, 20-period, population σ

**Indicator sub-panels** (below price, same SVG):
- `RSI 14` — Wilder-smoothed, 30/70 reference lines
- `MACD 12/26/9` — histogram + line + signal (dashed)

**Signals toggle** — toolbar chip (default ON). Persisted to `localStorage`.

**Overlay persistence** — `localStorage` key `rbq.cache.chart-overlays.v1` (JSON array).

---

## Keyboard shortcuts

All handling in ONE place: `(algo)/+layout.svelte` `_onGlobalKeydown`. 
Discovery via `?` (cheatsheet modal).

**Rules**:
- Pause when `document.activeElement` is `INPUT / TEXTAREA / SELECT / contenteditable`
- `Esc` defocuses input (no navigate)
- `Cmd+K / Ctrl+K` fires even while typing (command-palette exception)
- `?` case-sensitive (requires Shift); all letter keys case-insensitive

**Navigation** (Bloomberg two-key `g`+letter, 800ms window):

| Keys | Destination |
|---|---|
| `g p` | /pulse |
| `g d` | /dashboard |
| `g o` | /orders |
| `g e` | /admin/derivatives |
| `g c` | /charts |
| `g v` | /performance |
| `g a` | /automation |
| `g h` | /admin/history |
| `g m` | /pulse#movers |

**Actions**:

| Key | Effect |
|---|---|
| `t` | Open order ticket |
| `h` | Open activity/log modal |
| `k` | Open chart modal |
| `/` | Focus symbol search |
| `r` | Dispatch `refresh-page` event |
| `?` | Toggle cheatsheet |
| `Esc` | Close cheatsheet |

**Grid contextual** (when ag-Grid cell has focus):

| Key | Effect |
|---|---|
| `j` | Down arrow |
| `k` | Up arrow |
| `Enter` | Context menu |
| `f` | Fullscreen toggle |
| `c` | Collapse toggle |

---

## Order placement + multi-account basket

**OrderTicket.svelte** — unified modal. DRAFT/PAPER/LIVE routes. Account required. 
Qty validated (must be lot multiple). Pre-fills from context. Success feedback inline.

**Basket orders** — `POST /api/orders/basket` groups by account, `asyncio.gather` 
per-account place. Shared `basket_tag=ramboq-basket-<uuid>`.

**Target profit** — `AlgoOrder` has `target_pct, target_abs, parent_order_id, basket_tag`. 
On parent fill, auto-attach TP flip-side order (idempotent via `parent_order_id IS NULL`). 
Default `algo.default_target_pct` (0.30).

**Preflight parallelized** — 4 sequential awaits → `asyncio.gather`. Wall-time ~300ms (was 800-1200ms).

**`_TICK_INDEX` dict** — O(1) tick lookup (was O(N)). Module-level from instruments cache.

---

## Key Patterns

**Market-data broker resolution** — `get_market_data_broker()` in 
`backend/brokers/registry.py` is the SINGLE resolution path for quote / ltp / instruments / 
historical_data calls. `contextvars.ContextVar` (`_MDB_CTX`) caches `PriceBroker` for 
request lifetime so every callsite picks same broker.

- `reset_market_data_broker_ctx()` — called in Litestar `before_request` hook
- Selection order: (1) `connections.price_account` operator pin, (2) `broker_accounts.priority` 
  ASC, (3) insertion order
- Telemetry: first call per request logs `[MARKET-DATA-BROKER]`. Failover logs 
  `[MARKET-DATA-FALLBACK]`.
- Background pollers have own asyncio context → resolve fresh on every call (correct — 
  should see healthiest broker)
- `get_sparkline_broker()` and `get_historical_brokers()` intentionally NOT wired (spread 
  3 req/sec Kite budget)
- `@for_all_accounts` untouched — fans out per-account by design

Wired callsites (15 files): quote.py, watchlist.py, options.py, strategies.py, positions.py, 
orders_place.py, hedge_proxies.py, admin.py, instruments.py, background.py, lot_ledger.py, 
paper.py, template_attach.py, replay/driver.py, ohlcv_store.py, broker_apis.py.

**Raw broker-DataFrame cache** (`backend/brokers/broker_apis.py:_RAW_CACHE`, 30s TTL):
`fetch_holdings()` / `fetch_positions()` / `fetch_margins()` memoise their list[pd.DataFrame] 
return. Route handlers, `compute_firm_nav`, investor slice, nav_daily writer all share 
one broker round-trip per TTL window. `?fresh=1` and postbacks call `_raw_cache_invalidate(key)`.

**Holiday calendar** — four-tier read in `fetch_holidays(exchange)`:
1. `holidays_store._MEM_CACHE` (in-process LRU, year-scoped)
2. Module-level `_HOLIDAY_CACHE` (daily TTL, sync fallback)
3. **`market_holidays` PostgreSQL table** (durable). Populated by daily 04:00 IST 
   `_task_holiday_refresh`. Retry every 30 min until 08:00 IST hard stop.
4. NSE public API (`nseindia.com/api/holiday-master?type=trading`) — cold-boot fallback only

Empty sets cached. Buster = date rollover for Tiers 1+2; PK-idempotent UPSERT keeps Tier 3 accurate.

**Market segments — multi-session shape** — every `market_segments.<seg>` block carries 
`sessions: list[{start, end}]` + `evening_open_on_holidays: bool` flag. Legacy 
`hours_start`/`hours_end` still parsed. `is_market_open()` positional signature unchanged; 
new keyword-only `sessions=[…]` + `evening_open_on_holidays=<bool>` override when passed.

**Multi-account broker calls**: `@for_all_accounts` iterates accounts, returns 
list of DataFrames. Callers use `pd.concat(..., ignore_index=True)`.

**Account masking**: `mask_account(s: str) -> str` replaces digits with `#`. 
`mask_column(pd.Series)` for DataFrames. Used in all alerts + summaries.

**Singleton Connections**: Thread-safe. Initialized once at startup. On 
`RAMBOQ_USE_CONN_SERVICE=1`: populates registry with `RemoteBroker` stubs.

**Closed-hours route gate** (`backend/api/helpers/snapshot_gate.py`):
`closed_hours_or_broker(exchange, snapshot_fn, broker_fn, *, fallback_to_snapshot_on_broker_error=True)`
is the CANONICAL gate for data routes needing market-closed snapshot fallback.
Primary invariant: `broker_fn` NEVER called when market closed.
Source tags returned: `'live'` / `'snapshot'` / `'snapshot-fallback'`.
Every new data route MUST use this helper. `_any_segment_open()` is what tests patch.

**Broker auth health badge** (`frontend/src/lib/BrokerHealthBadge.svelte`):
Admin/designated-only navbar badge. Polls `GET /api/admin/broker-health` every 30s via 
`visibleInterval`. State: `green` (last_good < 5 min ago), `amber` (stale), `red` 
(last_fail > last_ok). Worst state drives colour. Click opens per-account modal.

---

## Cross-cutting

### Derivatives Analytics

Options research: underlying-driven re-pricing, Black-Scholes, implied-vol calibrator, 
greeks (per-share + position-scaled), strategy multi-leg analysis.

**Symbol parser** — `parse_tradingsymbol()` returns `{kind, underlying, strike, opt_type, expiry}` or None.

**Re-pricing** — `reprice_row(row, spot, sigma)`. Futures track spot 1:1; options via BS.

**Payoff range** — σ-driven via `span_pct = span_sigmas × σ × √T_years` (default 
span_sigmas=2.5, clamped [2%, 50%]).

**Endpoints** (admin-guarded):
- `GET /api/options/analytics?mode=live|sim|hypothetical&symbol=…` — Greeks, pricing, payoff
- `POST /api/options/strategy-analytics` (legs list) — multi-leg aggregate + R:R
- `GET /api/options/historical?symbol=…&days=30` — OHLCV with multi-account fallback

**LTP fallback chain**: override → sim positions → live broker → close price → 
depth midpoint → avg_cost → Black-Scholes-at-default-IV.

**Expected value** — trapezoidal integration of expiry payoff against risk-neutral 
lognormal. R:R = max_profit / |max_loss|.

---

### Proxy hedges

DB-backed cross-reference between holdings (GOLDBEES, NIFTYBEES, …) and option roots 
they hedge (GOLD, NIFTY, …).

**Schema**: `hedge_proxies` table — proxy_symbol, target_root, is_active, note, beta 
(nullable = 1.0 default), correlation, regression_at.

**Math**:
```
effective_qty = β × market_value / target_spot
target_lots = effective_qty / target_lot_size
effective_cost = investment_value / effective_qty
Δ_extra = effective_qty
```

**β regression** — 60-day daily-returns: β = Cov(p,t) / Var(t), R² = corr². 
Operator-triggered via `POST /api/admin/hedge-proxies/{id}/compute`. Needs ≥15 bars.

**Auto-recompute** (`_task_hedge_proxy_regression`, daily 02:30 IST) — per active row 
where `regression_at` > `regression_max_age_days`.

**UI** — PROXY chip on eq legs (magenta label + lot count + β).

---

### MCP server + Lab page

`/admin/research` — thread persistence + audit + token mint. Operator chats; 
backend serves 17 read-only + 2 persist + 6 gated write tools = 25 total.

**MCP server** (`backend/mcp/kite_server.py`) — FastMCP subprocess. Tools: positions, 
holdings, quote, ohlcv, news, chain, macro, agents, threads, audit, dry_run, server_info, 
place/cancel/modify orders, activate/deactivate/update agents, save thread/draft.

**Confirm-token gate** (60s TTL, single-use, purpose-hash bound) — mint token for 
`place | cancel | modify | activate | deactivate | update`.

**McpAudit table** — tool, user_id, args_redacted, result_status, result_summary, 
request_id. Daily cleanup (default 90-day retention).

---

### Performance measurement

Four-tool scaffold for iterative perf work. Every result JSON in `.log/`.

**Tools**:

| Tool | Kind | Output |
|---|---|---|
| `scripts/perf_baseline.py` | Static grep | `.log/perf_baseline_<utc>.json` |
| `frontend/e2e/perf_capture.spec.js` | Playwright | `.log/perf_capture_<utc>.json` |
| `scripts/perf_capture_run.sh` | Wrapper | stdout + capture JSON |
| `backend/api/middleware/perf_stats.py` | ASGI (opt-in) | `.log/perf_stats.json` |
| `scripts/perf_diff.py` | Diff reader | stdout + `.log/perf_diff_*.txt` |

**Canonical workflow**:
```sh
./venv/bin/python scripts/perf_baseline.py --no-build
# ... ship changes ...
./venv/bin/python scripts/perf_baseline.py --no-build
./venv/bin/python scripts/perf_diff.py
```

**Full baseline** (static + cyclomatic + runtime):
```sh
export PLAYWRIGHT_USER=ambore
export PLAYWRIGHT_PASS='...'
./venv/bin/python scripts/perf_baseline.py --no-build --with-cyclomatic --with-runtime
```

---

## Things to Avoid

- Don't mock broker API calls — `@for_all_accounts` and singleton behave differently
- Don't commit `secrets.yaml` — gitignored; SSH-edit `/opt/ramboq*` on server
- Don't add branch filters to `hooks.json` — routing in `dispatch.sh`
- Don't use `2>>&1` in systemd — use `2>&1` (>> causes bash syntax errors)
- Always `chown www-data -R` after server ops: `/opt/ramboq*/.git /opt/ramboq*/.log`
- Weekends hardcoded closed — use `market_special_sessions` table for exceptions
- Don't try to run main API without conn-service when `RAMBOQ_USE_CONN_SERVICE=1` — 
  service startup will fail with socket errors

---

## Critical math guards

**Option qty vs lot_size** — Kite ships MCX intraday fields in lots, NSE in contracts. 
Double-check every multiplication. Has caused multi-lakh P&L distortion + 20× over-orders.

**GTT layer also enforces translate_qty** — `apply_plan_live` in `template_attach.py` 
must call `broker.translate_qty(exchange, raw_qty, lot_size)` for EVERY GTT leg AND 
wing order before calling `broker.place_gtt` / `broker.place_order`. `place_gtt` in 
`kite.py` does NOT auto-translate. Incident (2026-07-02): 1-lot MCX CRUDEOIL (qty=100 
contracts) sent `quantity=100` to GTT → Kite read as 100 lots. Fix: `parent_lot_size` 
baked into `TemplatePlan` at resolve-time; `apply_plan_live` calls `broker.translate_qty` 
per leg; adapter ceiling in `place_gtt` provides last-line defense.

**G1 fires on ALL close paths** — `run_preflight(account, {..., "intent": "close"})` 
called before `chase_order` in `_action_live_close_position` AND per-position in 
`_action_live_chase_close_positions`. G1 (LOT_MULTIPLE) fires even for closes; G2 
(FAT_FINGER_5_LOT_CAP) bypassed via `intent="close"`. `_arm_take_profit` live path has 
inline G1 guard (no `run_preflight` — G2 skipped). Blocked close writes REJECTED 
AlgoOrder + alert; chase loop uses `continue` so other positions proceed. 50-lot adapter 
ceiling in `kite.py:place_order` has NO intent bypass — 51-lot closes hard-blocked.

**G1 also fires in `apply_plan_live` (GTT template layer)** — synchronous G1 check at 
top of `apply_plan_live` verifies every GTT leg qty + wing qty against `plan.parent_lot_size` 
before any broker call. Returns `AttachResult.errors` immediately on failure. Sits upstream 
of `broker.translate_qty` + adapter ceiling. `plan.parent_lot_size` always resolved (never 0) 
by `apply_template_to_order` via `await get_lot_size()`.

**Kite close_price stale overnight** — positions.close_price + quote.ohlc.close lag 
prior-session EOD between MCX close + next open. Use `daily_book.ltp` instead.

**Day P&L formula** — Decomposed intraday (not naive `(LTP−close)×qty`). Positions: 
`overnight_qty × (LTP − prev_close) + day_buy/sell legs`. Holdings: `broker.pnl − (close − cost) × opening_qty`. 
MCX guard: apply lot_size to intraday qty too.

**Frontend Day P&L SSOT** — `baseDayPnlForPosition(p)` in `frontend/src/lib/data/nav.js` 
is canonical new-position override: when `overnight_quantity=0 && pnl≠0`, Kite returns 
`day_change_val=0` and real value is in `pnl`. All 6 frontend Day P&L surfaces (NavStrip P 
slot 1, MarketPulse position card, MarketPulse TOTAL row, Snapshot rows, Legs grid, 
Payoff overlay) MUST call this helper — never read `day_change_val` directly.

---

## Common Tasks — Where to Make Changes

| Task | Files |
|---|---|
| Add new page | SvelteKit route + nav entry in `+layout.svelte` |
| Change page content | `backend/config/frontend_config.yaml` |
| Change Gemini prompt | `backend/config/frontend_config.yaml` |
| Change retry behaviour | `backend/config/backend_config.yaml` |
| Change log verbosity | `backend/config/backend_config.yaml` |
| Add broker account | `backend/config/secrets.yaml` |
| Change deploy routing | `webhook/dispatch.sh` |
| Change tab title / SEO | `frontend/src/app.html` + per-route `<svelte:head>` |
| Change footer | `backend/config/frontend_config.yaml` |
| Change loss threshold | `/agents` page → edit `loss-*` agent condition |
| Change alert recipients | `backend/config/secrets.yaml` on server |
| Deploy notification | `backend/config/backend_config.yaml` on server |
| Market hours | `backend/config/backend_config.yaml` |
| Summary timing | `backend/config/backend_config.yaml` |
| Order-entry grammar | `backend/config/grammars/orders.yaml` |
| Toggle agent default status | `backend/api/algo/agent_engine.py` |
| Add MCP tool | `backend/mcp/kite_server.py` @app.tool() |
| Tune MCP audit | `/admin/settings` |
| Update macro data | `backend/config/backend_config.yaml` |
| Day P&L formula | `backend/api/algo/pnl_math.py` + `frontend/src/lib/data/nav.js` |
| NAV breakdown | `frontend/src/lib/data/nav.js` + `backend/api/algo/nav.py:compute_firm_nav` |
| LTP-override scaffold | `backend/api/helpers/ltp_patch.py` |
| Mask account in text | `backend/shared/helpers/utils.py:mask_account_in_text` |
| Postback fan-out | `backend/api/routes/orders.py:_postback_broadcast_fanout` |
| Ticket placement | `backend/api/routes/orders_place.py:ticket_order_handler` |
| Basket order | `backend/api/routes/orders_basket.py` |
| Percentage formatters | `frontend/src/lib/format.js` |
| Chart self-heal threshold | `/admin/settings` |
| Backfill admin endpoint | `POST /api/admin/persistence/backfill` |
| Backfill CLI | `scripts/persistence_mode.py` + `scripts/backfill_ohlcv.py` |
| Perf dashboard | `frontend/src/routes/(algo)/admin/perf/+page.svelte` |
| Virtual root display | `backend/api/algo/symbol_resolver.py` + `frontend/src/lib/data/rootOf.js` |
| MCX lot-size overrides | `backend/api/routes/instruments.py` |
