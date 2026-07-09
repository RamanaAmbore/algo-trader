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

WebSocket in conn_service writes ticks to `/dev/shm/ramboq_ticks` (4096 slots, atomic version-word). 
Main API reads via MmapTickReader at byte-read latency. Background poller (50ms) publishes deltas to BroadcastBus. 
30s watchdog with auto-retry on failure. Universe registration (`_register_universe_with_ticker`) runs at startup + segment opens. 
Health surface: `GET /api/admin/health` — `ticker.stale_count`, `max_age_seconds`, `stale_top`.

---

## Broker accounts (DB-backed CRUD)

Managed via `/admin/brokers`. Credentials encrypted at rest (Fernet, derived from `cookie_secret`). 
Loaded on startup via `Connections.rebuild_from_db()` — query active rows, decrypt, rebuild conn map. 
Seeds from `secrets.yaml` once if empty. API: `GET/POST/PATCH/DELETE` + `POST /test` (admin-guarded). 
Display order via `broker_accounts.display_order` INT (default 500) — used across all UI surfaces via 
`sortAccountsBy(rawList, $orderMap)` in both backend and frontend (60s TTL cache).

---

## Multi-Account IPv6 Source Binding (Kite + Dhan)

Kite enforces 1 IP per app; Dhan enforces 1 token per source IP. Groww has no per-IP rule. 
Implemented via `_IPv6SourceAdapter` (requests.HTTPAdapter) + Groww SDK monkey-patch. 
Dhan rows grouped by `source_ip` — if 2+ on same IP, only lowest-priority loads.

---

## Broker resilience

**Circuit breaker** — per-account state machine (CLOSED → OPEN after 3 failures → HALF-OPEN after cool-off). 
Short-circuits to empty DataFrame on OPEN; exponential cool-off 5m → 10m → 20m → 30m. 
Stored in `_FETCH_HEALTH[account]`. Auto-downgrade to 'cold' poll after ≥5 OPEN events in 15min window. 
Operator restore via `POST /api/admin/brokers/{id}/restore-priority`.

**Dhan poll priority** — Column `poll_priority` (hot=30s, warm=120s, cold=600s) for Dhan only. 
Health surface: `GET /api/admin/broker-health` — `circuit_state`, `consecutive_fail_count`, 
`circuit_open_until`, per account.

---

## Layer 2: Backend API (`backend/api/`)

### File map

**Stack**: Litestar 2.x + msgspec.Struct · PostgreSQL 17 + SQLAlchemy 2.x async · 
Polars (positions, holdings, funds, nav) / pandas (broker boundary) · asyncio · JWT HS256 · PBKDF2-SHA256.

**Core routes**: `app.py` (startup), `routes/algo.py` (agents + WS), `routes/orders.py` (controller), 
`routes/orders_place.py` (ticket), `routes/orders_postback.py` (postback), `routes/orders_basket.py` (basket), 
`algo/agent_engine.py` (runner), `algo/simulator.py` / `sim/driver.py` (market sim).

---

## Alert and Notification System

**Flow**: Agent (rule) → Alert (event) → Notify (delivery) → Action (side-effect). 
Message types: Market open/close (Telegram only), Agent fire (Telegram + Email: "RamboQuant Agent: "). 
Dual-timezone display via `timestamp_display()`. Global gates in `backend_config.yaml`: 
`alert_cooldown_minutes` (30), `alert_baseline_offset_min` (15), `alert_rate_window_min` (10), 
market-hours gate for `schedule: market_hours` agents.

---

## Market Segments

| Segment | Exchanges | Hours (IST) |
|---|---|---|
| Equity | NSE, BSE, NFO, CDS | 09:15–15:30 |
| Commodity | MCX | 09:00–23:30 |

Summaries at `open_summary_offset_minutes` / `close_summary_offset_minutes`. 
Weekends hardcoded closed; `market_special_sessions` table overrides (highest precedence, by date/time). 
Movers gate: NSE open → NSE equity; NSE closed + MCX open → MCX commodities; both closed → NSE snapshot.

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
union `daily_book` (past 7 days) into Kite universe — survives conn_service restart, circuit-breaker, broker outage.

**Performance refresh Day P&L**: `_fetch_positions_direct` sums `day_change_val` + `pnl`, 
applies `apply_day_change_backstop()` to rescue missing-day edge cases. NavStrip P slot 1 accurate all session.

---

## Market lifecycle events

Singleton `MarketLifecycle` polled every 30s. Events: `<exch>:open`, `<exch>:close`, 
`<exch>:close_settled` (15 min after close, operator-tunable). Default handlers fire 
`snapshot_daily_book` + `snapshot_sparkline` + NAV snapshot. Snapshots idempotent via UPSERT 
on `(date, account, kind, symbol)`. Settled flag in `daily_book.payload_json.snapshot_extras.settled` 
distinguishes initial close from settled follow-up.

---

## Persistence pipeline (cache → DB → broker)

Three-tier hierarchy: in-memory LRU → PostgreSQL → broker. Per-key asyncio.Lock deduplicates fetches.

**Stores**: `ohlcv_store` (daily bars), `instruments_store` (symbol→token map, daily TTL), 
`holidays_store` (yearly, immutable post-year), `intraday_store` (5/15/30/60-min, 5-min TTL today).

**Completeness**: OHLCV (boundary + ≤4d gaps), Instruments (non-empty), Holidays (non-empty), Intraday (today any, hist span close).

**Modes**: `off` (default), `soft` (Tier 1+2 bypass), `hard` (soft + ticker recycle). Flip via `POST /api/admin/persistence/mode/{off|soft|hard}`.

**Retention** (configurable `/admin/settings`): ohlcv_daily=5yr, instruments=7d, intraday=90d, 
holidays/nav_daily/daily_book/investor_events/monthly_statements=forever, algo_events=30d, audit_log=365d.

**Event queues**: Generic `EventQueue` (bulk `executemany` INSERT). Four active: algo_events, agent_events, algo_order_events, mcp_audit.

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

**Four terms**: Agent (rule), Alert (runtime event), Notify (delivery), Action (side-effect).

Agent row: condition + notify + actions, seeded from `BUILTIN_AGENTS`, extensible via `/automation`. 
Loss agents (prefix `loss-*`) = 5 builtin + rules editable live. Alerts persisted to `agent_events`. 
Notify channels: telegram, email, websocket, log. Delivery actions: order place / modify / cancel / close.

**Condition tree** (v2): `{all/any/not: [...]}` over leaves `{metric, scope, op, value}`. 
Metrics: point-in-time (pnl, pnl_pct, day_pct), rate (pnl_rate_abs/_pct), rolling (mean, max_drawdown, stdev, range), 
expiry-aware (is_itm, is_ntm, days_until_expiry).

Tokens via `grammar_tokens` table (system on boot, custom via `/admin/tokens`). 
GrammarRegistry singleton dispatch, reloaded on `/api/admin/grammar/reload`.

---

## Symbol resolution + virtual roots (MCX / CDS)

MCX/CDS never expose raw contract names in UI. Virtual symbols map to front-month 
(CRUDEOIL), back-month (_NEXT), or explicit contract. SSOT: `backend/api/algo/symbol_resolver.py` 
with functions: `list_active_futures`, `resolve_symbol`, `root_of`. Supported roots: MCX 
(CRUDEOIL, GOLD, SILVER, etc.), CDS (USDINR, EURINR, etc.). API: 
`GET /api/symbols/resolve?symbol=CRUDEOIL&exchange=MCX` / `root_of?contract=…`. 
Frontend (`rootOf.js`): `seedRootMapFromInstruments`, `rootOf`, `rootOfLabel`, `resolveVirtual`. 
Rollover rule: settling contracts excluded on expiry day (`inst.x > today_iso IST`).

---

## Investor portal + NAV

Token-gated `/investor/<token>` portal. Token IS credential. Operator mints via `/admin` 
(90-day default, 10y cap). Units model: slice = units_held × nav_per_unit; cost_basis = 
sub − redemption; pnl = slice − cost_basis. NAV v4 formula: `firm_nav = cash_sod + option_premium + 
Σ position.unrealised + Σ holdings.cur_val` (option_premium replaces used_margin to eliminate double-count). 
Three endpoints: `GET /api/nav/me` (slice + day delta), `/nav/me/history?days=180` (curve), 
`/api/investor/{token}/slice` (public, no auth).

---

## Audit log + History

AuditLog schema: id, actor (user_id / username / role), action, category, method, path, 
target_type, target_id, status_code, summary, request_id, client_ip, created_at. 
Two write paths: AuditMiddleware (every HTTP request, skip non-mutating) + `write_audit_event()` 
(non-HTTP: postback, agent action, tasks). Category routing by path prefix 
(order.place / order.fill / order.modify / order.cancel / user / config.* / system.* / agent / strategy / http).

`/admin/history` (cap `view_audit`): Orders tab (30d, 50/page, status histogram + mode filter), 
Trades tab (daily_book trades 30d), Funds tab (daily_book funds 90d, per account/segment/day).

---

## Settings (DB-backed tunables)

`/admin/settings` UI. Reader chain: DB cache → YAML → in-code default. 
Buckets: `alerts.*` (cooldown/rate window/baseline), `performance.*` (refresh/summary offsets), 
`simulator.*` (ticks/auto_stop/rate), `notifications.*` (telegram/email/deploy), 
`logging.*` (levels), `hedge_proxies.*` (regression params).

---

## Layer 3: Frontend (`frontend/`)

### Persistent cache layer

In-memory Map + localStorage (`rbq.cache.` prefix). Module: `persistentCache.js`. 
TTL buckets: day (24h), hour (1h), minute (15m), short (2m). Used by MarketPulse, 
PositionStrip, NavCard. Survives reload + deploy. Live LTP NOT cached.

Tick-flash: `createTickFlash()` → 350ms green/red directional pulse on cell updates.

---

## NavStrip pill cluster

Header snapshot SSOT (frozen during closed hours). Pill layout P / M / C / H (slash-joined trios):
- P (P&L): `today / lifetime / expiry` 
- M (Margin): `available / total` 
- C (Cash): `available / total` 
- H (Holdings): `today / value / lifetime`

P expiry value computed in `PositionStrip.svelte` (identical `/admin/derivatives` TOTAL math, 
F&O only). Snapshot SSOT replaces localStorage when closed; in-session reload via disk cache.

---

## MarketPulse + PerformancePage

Pulse: two side-by-side ag-Grid (left: watchlists + movers, right: positions + holdings). 
Bucket sort: pinned (1), watchlist (2), positions (3), holdings (4), movers (5). 
Default columns: Symbol · 5d · LTP · Avg · Day % · Close · Qty · Day P&L · P&L % · P&L.

Directional encoding: pos-long/short tint (green/red 10%), holdings tint (up/down/flat), 
watchlist/underlying tint, Day P&L mini-bar (2px), CE/PE text color (Sensibull), TOTAL row (amber 22% + borders).

PerformancePage: canonical cluster reference, public page (cream) shows real Kite data during sim. 
Admin `/dashboard`: P&L Analysis + MarketPulse summary + Agent log. Layout: chart LEFT, 
tabbed NAV/Capital/Equity sidebar RIGHT (NavBreakdown SSOT with backend `compute_firm_nav`).

---

## Activity surface architecture

Unified log viewer (modal, card, page) sharing components + filter state. 
Components: `ActivityLogSurface.svelte` (wraps LogPanel), `ActivityHeaderFilters.svelte` 
(select + dropdown), `LogPanel.svelte` (per-tab level parsing, multi-column ≥900px).

Mount points: ActivityLogModal (navbar Log, all tabs), Activity card (/admin/execution, /orders — Orders tab), 
Dashboard card (/dashboard — replaces legacy NEWS), /activity page (bookmarkable, Orders default).

Log-level parsing: System/Conn extract `[LEVEL]`, Agent map `event_type`, Orders no token (all info).

**Activity state store** — `activityStore.svelte.js` is the SSOT for tab selection and filter state. 
All five mount points share the same store — tab persists across modal open/close. `openActivityModal(tab?)` 
accepts an optional tab override for deep-links (e.g. BrokerHealthBadge → 'conn'); generic opens 
pass no argument and leave the persisted tab unchanged.

---

## Chart Workspace

Unified chart (underlying / future / option / equity). Components: `ChartWorkspace.svelte` 
(OHLCV + tick overlay + underlying-spot + Greeks), `ChartModal.svelte` (wrapper), 
`/charts` page (URL params sync).

Indicators (price panel): SMA 20/50, EMA 20/50 (Wilder), VWAP (cyan, null on zero-volume), 
Bollinger Bands ±2σ (20-period). Sub-panels: RSI 14 (30/70 lines), MACD 12/26/9 (histogram + line + signal).

Signals toggle (default ON) persisted to localStorage. Overlays persisted to `rbq.cache.chart-overlays.v1`.

**Chart state store** — `chartStore.svelte.js` is the single-slot Svelte 5 `$state` SSOT for chart 
UI (symbol, exchange, range, ohlcv, overlays, indicators). Both `ChartModal` and `/charts` page seed 
from and write to this store. Symbol change calls `clearData()` which wipes ohlcv/lastFetched atomically; 
overlays and indicators persist across symbol changes. 30s TTL via `isFresh()` prevents duplicate 
fetches for same symbol+range.

---

## Keyboard shortcuts

Centralized in `(algo)/+layout.svelte` `_onGlobalKeydown`. Discovery: `?` (cheatsheet).

Rules: Pause on INPUT/TEXTAREA/SELECT/contenteditable; Esc defocuses; Cmd+K/Ctrl+K bypass; 
`?` requires Shift (case-sensitive), letters case-insensitive.

Navigation (Bloomberg `g`+letter, 800ms): `g p` /pulse, `g d` /dashboard, `g o` /orders, 
`g e` /admin/derivatives, `g c` /charts, `g v` /performance, `g a` /automation, `g h` /admin/history, `g m` /pulse#movers.

Actions: `t` order ticket, `h` activity/log, `k` chart, `/` symbol search, `r` refresh-page, 
`?` cheatsheet toggle, `Esc` close.

Grid (ag-Grid cell focus): `j` down, `k` up, `Enter` context menu, `f` fullscreen, `c` collapse.

---

## Order placement + multi-account basket

OrderTicket: unified modal (DRAFT/PAPER/LIVE). Qty in LOTS for F&O (converted to contracts at 
request boundary via `contracts = lots × lot_size`), raw for equity. Frontend sends `_lots` for F&O. 
G1 (LOT_MULTIPLE) removed; G2 (5-lot cap, MCX 20-lot) checks lots directly.

Basket: `POST /api/orders/basket` groups by account, `asyncio.gather` dispatch per-account. 
Shared `basket_tag=ramboq-basket-<uuid>`. Target profit: `AlgoOrder.target_pct/target_abs/parent_order_id/basket_tag` 
auto-attach on parent fill (default 0.30). Preflight: `asyncio.gather` parallel (~300ms). 
`_TICK_INDEX` dict: O(1) lookup from instruments cache.

**OrderTicket prefill** — `orderTicketModal` in `stores.js` carries a `prefill` payload alongside `open`. 
`openOrderTicketModal(prefill?)` accepts optional fields: symbol, exchange, side, qty, lots, price, product, 
lotSize, currentQty, action, account, accounts, triggerSource. No-arg call opens a blank ticket (backward-compatible). 
`closeOrderTicketModal()` resets prefill to null. Enables deep-link order entry from other surfaces 
(chart, positions grid, alerts).

---

## Key Patterns

**Market-data broker resolution** — SSOT: `get_market_data_broker()` in `registry.py`. 
Caches via `contextvars.ContextVar` (`_MDB_CTX`) per-request. Selection order: operator pin > 
`broker_accounts.priority` ASC > insertion. Telemetry: `[MARKET-DATA-BROKER]` / `[MARKET-DATA-FALLBACK]`. 
Background pollers resolve fresh (separate asyncio context). Intentionally NOT wired: `get_sparkline_broker()`, 
`get_historical_brokers()` (budget spread). `@for_all_accounts` untouched (per-account fan-out by design).

**Raw broker-DataFrame cache** — `_RAW_CACHE` (30s TTL). `fetch_holdings/positions/margins` 
memoise returns. One broker round-trip per TTL window shared by routes, nav, investor slice. 
`?fresh=1` + postbacks call `_raw_cache_invalidate(key)`.

**Holiday calendar** — four-tier read: in-process LRU → module-level TTL → PostgreSQL 
`market_holidays` (daily 04:00 IST refresh, retry 30min until 08:00 IST) → NSE API (cold-boot). 
Empty sets cached; buster = date rollover Tiers 1+2, UPSERT Tier 3.

**Market segments** — blocks carry `sessions: list[{start, end}]` + `evening_open_on_holidays`. 
`is_market_open()` signature unchanged; keyword-only overrides when passed.

**Multi-account calls**: `@for_all_accounts` returns list[DataFrame]. Callers use `pd.concat(..., ignore_index=True)`.

**Account masking**: `mask_account(s) → str` (digits → #). Used in all alerts + summaries.

**Singleton Connections** — thread-safe startup init. On `RAMBOQ_USE_CONN_SERVICE=1` populates 
registry with RemoteBroker stubs.

**Closed-hours route gate** — `closed_hours_or_broker()` in `snapshot_gate.py` CANONICAL gate. 
Invariant: `broker_fn` NEVER called when closed. Returns source tags: `'live'` / `'snapshot'` / 
`'snapshot-fallback'`. Every new data route MUST use. Tests patch `_any_segment_open()`.

**Broker auth health badge** — `BrokerHealthBadge.svelte` (admin/designated navbar, polls 30s 
via `visibleInterval`). State: green (last_good < 5min), amber (stale), red (last_fail > last_ok). 
Worst state drives color. Click opens per-account modal.

---

## Cross-cutting

### Derivatives Analytics

Options: underlying-driven re-pricing, Black-Scholes, implied-vol calibrator, greeks, multi-leg strategy.

Symbol parser: `parse_tradingsymbol()` → `{kind, underlying, strike, opt_type, expiry}`. 
Re-pricing: futures 1:1 spot, options via BS. Payoff range σ-driven (span 2.5σ, clamp 2%-50%).

Endpoints (admin): `GET /api/options/analytics?mode=live|sim|hypothetical&symbol=…` (Greeks + payoff), 
`POST /api/options/strategy-analytics` (multi-leg aggregate + R:R), `GET /api/options/historical?symbol=…&days=30` 
(OHLCV + multi-broker fallback).

LTP chain: override → sim positions → live broker → close price → depth midpoint → avg_cost → BS-default-IV. 
Expected value: trapezoidal payoff integration risk-neutral lognormal. R:R = max_profit / |max_loss|.

---

### Proxy hedges

DB-backed cross-reference holdings (GOLDBEES, NIFTYBEES) → hedged roots (GOLD, NIFTY). 
Schema: `hedge_proxies` (proxy_symbol, target_root, is_active, note, beta, correlation, regression_at).

Math: `effective_qty = β × market_value / target_spot`; `target_lots = effective_qty / target_lot_size`.

β regression (60-day daily returns): `β = Cov(p,t) / Var(t)`, needs ≥15 bars. 
Operator-triggered via `POST /api/admin/hedge-proxies/{id}/compute`. Auto-recompute daily 02:30 IST 
when `regression_at` > `regression_max_age_days`.

UI: PROXY chip on eq legs (magenta label + lot + β).

---

### MCP server + Lab page

`/admin/research` — thread persistence + audit + token mint. Operator chats; backend 
serves 17 read-only + 2 persist + 6 gated write tools (25 total). MCP server (`kite_server.py`): 
FastMCP subprocess. Confirm-token gate (60s TTL, single-use, purpose-hash bound) for 
place/cancel/modify/activate/deactivate/update. McpAudit table daily cleanup (90-day default retention).

---

### Performance measurement

Four-tool scaffold (results in `.log/`): `perf_baseline.py` (static grep), 
`perf_capture.spec.js` (Playwright), `perf_capture_run.sh` (wrapper), `perf_stats.py` (ASGI), 
`perf_diff.py` (diff reader). Workflow: baseline → ship → baseline → diff. 
Full baseline: `--no-build --with-cyclomatic --with-runtime` (requires `PLAYWRIGHT_USER/PASS`).

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

**F&O order qty convention** — API now accepts LOTS as input for instruments with 
`lot_size > 1`. `backend/api/routes/orders_place.py:_ticket_validate_input` converts 
lots → contracts (`contracts = lots × lot_size`) at the request boundary. G2 (5-lot cap, 
MCX 20-lot cap) checks against lots directly. Frontend sends `_lots` for F&O; raw qty 
for equity. Applies to `/api/orders/ticket`, `/api/orders/basket`, and preview routes.

**Option qty vs lot_size** — Kite ships MCX intraday fields in lots, NSE in contracts. 
Double-check every multiplication. Has caused multi-lakh P&L distortion + 20× over-orders.

**GTT layer also enforces translate_qty** — `apply_plan_live` in `template_attach.py` 
must call `broker.translate_qty(exchange, raw_qty, lot_size)` for EVERY GTT leg AND 
wing order before calling `broker.place_gtt` / `broker.place_order`. `place_gtt` in 
`kite.py` does NOT auto-translate. Incident (2026-07-02): 1-lot MCX CRUDEOIL (qty=100 
contracts) sent `quantity=100` to GTT → Kite read as 100 lots. Fix: `parent_lot_size` 
baked into `TemplatePlan` at resolve-time; `apply_plan_live` calls `broker.translate_qty` 
per leg; adapter ceiling in `place_gtt` provides last-line defense.

**G1 guards on close paths** — Ticket handler: G1 (LOT_MULTIPLE) removed from 
`_ticket_enforce_lot_and_fat_finger` after lots-convention refactor — `lots × lot_size` 
is always a valid multiple by construction so the check is redundant at the ticket 
boundary. Remaining G1 defenses: (1) `_arm_take_profit` live path has an inline G1 
guard before `broker.place_order` (no `run_preflight` — G2 skipped); (2) `apply_plan_live` 
GTT layer has a synchronous G1 check at the top before any broker call. G2 
(FAT_FINGER_5_LOT_CAP) bypassed via `intent="close"`. Blocked close writes REJECTED 
AlgoOrder + alert; chase loop uses `continue` so other positions proceed. 50-lot adapter 
ceiling in `kite.py:place_order` has NO intent bypass — 51-lot closes hard-blocked.

**G1 also fires in `apply_plan_live` (GTT template layer)** — synchronous G1 check at 
top of `apply_plan_live` verifies every GTT leg qty + wing qty against `plan.parent_lot_size` 
before any broker call. Returns `AttachResult.errors` immediately on failure. Sits upstream 
of `broker.translate_qty` + adapter ceiling. `plan.parent_lot_size` always resolved (never 0) 
by `apply_template_to_order` via `await get_lot_size()`.

**Kite close_price stale overnight** — positions.close_price + quote.ohlc.close lag 
prior-session EOD between MCX close + next open. Use `daily_book.ltp` instead.

**Day P&L formula + backstop** — Decomposed intraday (not naive `(LTP−close)×qty`). 
Positions: `overnight_qty × (LTP − prev_close) + day_buy/sell legs`. Holdings: 
`broker.pnl − (close − cost) × opening_qty`. MCX guard: apply lot_size to intraday qty too. 
Backend SSOT: `backend/api/algo/pnl_math.py:apply_day_change_backstop(raw: pd.DataFrame)` 
rescues two edge cases — Case 1 (new position, `overnight_quantity=0, day_change_val=0, pnl≠0`) 
and Case 3 (flat intraday, `quantity=0, day_change_val=0, pnl≠0`) where Kite omits the day 
value. Applied in `routes/positions.py` + `background.py:_fetch_positions_direct` (now sums 
both `day_change_val` AND `pnl` before applying the backstop).

**Frontend Day P&L SSOT** — `baseDayPnlForPosition(p)` in `frontend/src/lib/data/nav.js` 
is canonical new-position override: when `overnight_quantity=0 && pnl≠0`, Kite returns 
`day_change_val=0` and real value is in `pnl`. Used by PerformancePage TOTAL row, 
derivatives `_byUnderlyingTotal` F&O loop + `bumpExcluded` equity branch, dashboard 
`_todayPnl` hero + `_positionsSummary`, NavStrip P slot 1, MarketPulse position card, 
Snapshot rows, Legs grid, Payoff overlay. Never read `day_change_val` directly.

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
| F&O order qty convention | `backend/api/routes/orders_place.py:_ticket_validate_input` + `frontend/src/lib/order/orderTicketSubmit.js` |
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
| Chart state (symbol, range, OHLCV) | `frontend/src/lib/data/chartStore.svelte.js` |
| Activity tab persistence | `frontend/src/lib/data/activityStore.svelte.js` |
| Order ticket prefill | `frontend/src/lib/stores.js` |
