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

Push → webhook (`webhook.ramboq.com/hooks/update`) → `dispatch.sh` → `deploy.sh` → restart.

**Note**: `webhook.ramboq.com` and `dev.ramboq.com` must be grey cloud (DNS only) in Cloudflare.

---

## Key File Map

### Helpers (`backend/shared/helpers/`)
- **`broker_apis.py`** — `fetch_holdings / positions / margins` + `@for_all_accounts`. Holiday calendar cached per `(exchange, date)`.
- **`connections.py`** — `Connections` singleton; 2FA, token refresh, IPv6 binding per account.
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
| Market open | `Open` | `RamboQuant Open: ` |
| Agent fire | `Agent` | `RamboQuant Agent: ` |
| Market close | `Close` | `RamboQuant Close: ` |

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

**Deploy notifications**: Telegram-only (May 2026).

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

**Sparkline cache** — `_spark_past_cache` (past closes) + `_spark_today_cache` (intraday 30m, 5min TTL) + LTP at response time. Disk-persisted to `.log/sparkline_cache.json` (throttled 5s writes, atomic).

---

## KiteTicker / SSE live-LTP pipeline

Persistent Kite WebSocket → BroadcastBus → asyncio.Queue per SSE client → EventSource (browser auto-reconnect) → `liveLtp` store → 250ms throttled Svelte effect → ag-Grid.

**TickerManager singleton** — one WebSocket per process (`--workers 1` in prod). Twisted reactor thread holds ticks; asyncio route handlers read via `get_ltp()` with brief lock (O(1) hold-time, non-reentrant).

**Failover**: `_task_ticker_watchdog` detects >60s disconnect, restarts with next eligible account, 60s rate-limit cool-off, Telegram alert >30min.

**Subscriptions**: Watchlist + holdings + positions at startup + dynamic on add. `subscribe()` idempotent.

**Steady-state cost (market hours)**: ~0 REST calls + 1 persistent WS. LTP read from `_tick_map` (zero quota); sparkline historical 1 call on cache miss (3 req/sec budget, pre-warmed).

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

**Holiday calendar**: `fetch_holidays(exchange)` cached per `(exchange, today's date)` in `_HOLIDAY_CACHE` module dict. Buster = date rollover. Empty sets also cached (avoid retry-storm on API failure).

**Multi-account broker calls**: `@for_all_accounts` iterates accounts, returns list of DataFrames. Callers use `pd.concat(..., ignore_index=True)`.

**Account masking**: `mask_account(s: str) -> str` replaces digits with `#`. `mask_column(pd.Series)` for DataFrames. Used in all alerts + summaries.

**Singleton Connections**: Thread-safe, access as `connections.Connections()`. Initialized once at startup.

---

## Things to Avoid

- Don't mock broker API calls — `@for_all_accounts` and singleton behave differently
- Don't commit `secrets.yaml` — gitignored; SSH-edit `/opt/ramboq*` on server
- Don't add branch filters to `hooks.json` — routing in `dispatch.sh`
- Don't use `2>>&1` in systemd — use `2>&1` (>> causes bash syntax errors)
- Always `chown www-data -R` after server ops: `/opt/ramboq*/.git /opt/ramboq*/.log`
- Weekends hardcoded closed — special sessions need explicit override

---

## API Architecture (Litestar + SvelteKit)

**Stack**:
- **Framework**: Litestar 2.x + msgspec.Struct (10× faster pydantic)
- **Database**: PostgreSQL 17 + SQLAlchemy 2.x async + asyncpg
- **DataFrames**: Polars (routes) / pandas (broker/alert layer)
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
- `ChartWorkspace.svelte` (570 LOC) — OHLCV (line/area/candle, 1D/1W/1M/3M/6M/1Y, SMA20/50, Vol), intraday tick overlay (toggleable), underlying-spot overlay (dashed sky-blue for derivatives), Greeks strip (Δ Γ Θ V ρ IV).
- `ChartModal.svelte` — overlay wrapper (Esc / overlay-click closes, scroll-locked).
- `/charts` page — reads URL params, syncs picks via `goto({replaceState: true})`.

**Price history** (`/api/charts/*`) — in-memory rolling per-symbol buffers + lifecycle markers from `AlgoOrder` rows. No new persistent state.

**Batch endpoint** (`GET /api/charts/batch?mode=…&symbols=a,b,c`) — coalesce N symbols into one round-trip (cap 50). Returns `{mode, charts: [...]}` in input order.

**Chart grid lines** — faint cool-blue grid (0.10 major / 0.07 vertical). PriceChart x-labels HH:MM:SS. Zoom y-range auto-fits to visible data.

---

## MarketPulse + PerformancePage

**Pulse** (symbol grid) — two side-by-side ag-Grid (left: pinned watchlists + movers; right: positions + holdings). Desktop left/right, mobile stacked. Account filter shows Positions/Holdings only when account picked.

**Bucket sort integrity**: `bucketOf` returns pinned (1), watchlist (2), positions (3), holdings (4), movers (5). `postSortRows` keeps sort scoped within bucket.

**Default-visible cluster**: Symbol · 5d · LTP · Avg · Close · Qty · Day P&L · Day % · P&L % · P&L. Deviates from canonical (Avg next to LTP + P&L % before P&L) intentionally.

**Directional encoding**:
- **Background tint**: pos-long green 10%, pos-short red 10%. row-hold-up green 10%, row-hold-down red 10%, row-hold-flat slate 8%. row-watch amber 10%, row-und violet 10%, row-pos slate 8%.
- **Day P&L mini-bar**: 2px bar at symbol cell right (4px gap from edge). Positions + Holdings only; hidden on TOTAL.
- **CE/PE**: symbol text green/red (Sensibull convention).
- **Account tint** (Positions/Holdings): 14% bg via account hash. No inset bars.
- **TOTAL row**: amber 12% bg + 2px top border + 1px bottom + bold. No direction sign.

**PerformancePage** — canonical-cluster reference. Public page (cream theme) shows real Kite data even during sim. Admin `/dashboard` = P&L Analysis + MarketPulse summary grids (Funds/Positions/Holdings, single-account scoped) + Agent activity log.

**Public-theme row bars** — left + right edges on symbol cell only (`.ag-col-sym`). Background tint extends symbol + account cells (`.ag-col-fill`).

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

**RefreshButton**: cyan-400. Swaps glyph on loading (spin → arc-spinner, not rotated arrow). Native tooltip: "Refresh — 1 of 2 broker accounts loaded | Failed: ZG#### | Last refreshed: Sun 30 May · 21:42 IST · 12:12 EDT". Badge states: grey `?` (API unreachable) · green count (all loaded) · amber count (partial) · red count (none) · nothing (no brokers).

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

## Performance tuning (multi-wave)

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

## Critical math guards

**Option qty vs lot_size** — Kite ships MCX intraday fields in lots, NSE in contracts. Double-check every multiplication. Has caused multi-lakh P&L distortion + 20× over-orders.

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

---

## Refactoring Notes

**Slice X — audit cycle summary** (Jun 2026): Multi-wave audit cycles closed defects in postback scaffolding (Dhan/Groww), role vocabulary migration (operator terms), data-layer cascades, perf optimizations (parallel preflight, set-diff bootstrap, cumsum history), palette consolidation (4 waves), UX consistency (ConfirmModal on PWA, tab-strip deduplication, auth-check pattern cleanup).

**Key patterns**: `$effect`-gated auth checks on all admin pages. Parallel `asyncio.gather` for any multi-step broker operations. Module-level caching (dicts with identity flip on refresh). Canonical components (AlgoTabs, Select, ConfirmModal, CollapseButton, RefreshButton).
