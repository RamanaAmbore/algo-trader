# CLAUDE.md — RamboQuant Project Reference

This file is for Claude Code. It provides project context, file map, patterns, and refactoring notes to avoid re-exploring the codebase from scratch each session.

---

## Multi-agent coordination (read first)

The main Claude instance is the **coordinator**. Specialised subagents live in `~/.claude/agents/`:

| Agent | Use for | Tools | Model |
|---|---|---|---|
| `backend` | Litestar / SQLAlchemy / msgspec / Polars work | edit | sonnet |
| `frontend` | SvelteKit / Svelte 5 / ag-Grid / SVG charts / Tailwind | edit | sonnet |
| `backend-test` | pytest + pytest-asyncio for routes, agent engine, simulator, helpers | edit | haiku |
| `frontend-test` | Playwright e2e, svelte-check, mobile-viewport regression | edit | haiku |
| `audit` | Read-only defect / security / convention review. Does NOT write code. | read | sonnet |
| `doc` | CLAUDE.md / USER_GUIDE.md / ADMIN_GUIDE.md / runbooks | edit | haiku |

**Parallel by default.** When a task decomposes into independent sub-tasks (no shared file, no dependent output), the coordinator **MUST** dispatch them in a single message with multiple `Agent` tool calls. Sequence only when one agent's output feeds another.

Common parallel patterns:
- Frontend change + backend change for the same feature → `frontend` + `backend` in parallel; then `frontend-test` + `backend-test` in parallel; then `audit` + `doc`.
- Pre-merge sweep → `audit` + `frontend-test` + `backend-test` in parallel.
- Bug investigation → multiple `audit` (or `Explore`) agents pointed at different suspect areas in parallel.

Sequence when:
- The audit finds defects that need fixing → `audit` first, then `frontend` / `backend` to apply fixes.
- A schema/route change in `backend` is required before `frontend` can wire to it.
- `doc` runs after code changes settle (so it documents the final state, not a moving target).

The coordinator owns synthesis: never delegate "decide what to do based on your findings" — read the agent reports yourself and decide the next move.

---

## Project Overview

**RamboQuant** is a production web app for RamboQuant Analytics LLP at [ramboq.com](https://ramboq.com). It provides portfolio performance tracking, market updates (via Gemini AI), user onboarding, and investment information.

- **Architecture**: Litestar API + SvelteKit frontend
- **Single codebase**, two deployment targets: prod (`main`), dev (non-main branches)
- **Database**: PostgreSQL 17 via SQLAlchemy 2.x async + asyncpg; `ramboq` (prod) / `ramboq_dev` (dev) selected by `deploy_branch`
- **Broker data** comes from Zerodha Kite API; no DB storage for market data
- **Auth**: JWT (HS256) with PBKDF2-SHA256 password hashing; users in SQLAlchemy DB; stub mode when DB is empty

### Current major capabilities (2026-06)

| Surface | Status |
|---|---|
| Multi-mode execution ladder (sim → paper → shadow → live, with replay) | ✅ shipped |
| Declarative agent grammar + 17 seeded agents | ✅ shipped |
| Derivatives analytics (multi-leg payoff, σ-driven span, EV, R:R) | ✅ shipped |
| **Proxy hedges — pair table + auto β regression** | ✅ shipped (2026-06-17), see "Proxy hedges" section below |
| Multi-broker abstraction (Kite + Dhan + Groww), IP-binding, multi-account basket orders | ✅ shipped |
| MCP server + Lab page (chat-driven research / agent authoring) | ✅ shipped |

### Next focus area: order placement to various brokers

Multi-broker order placement is the next major capability. Current state:
- Basket-order endpoint exists, dispatches per-account in parallel via asyncio.gather (Kite-only path matured)
- Dhan + Groww adapters have placeholder `place_order` methods — need full wiring to match Kite's variety/exchange/product/order_type/trigger_price coverage
- OrderTicket already routes through a unified `/api/orders/ticket` endpoint that branches by mode; the broker dispatch beneath needs the per-vendor implementations completed and tested side-by-side
- Confirmation path: post-fill webhook handling already mature for Kite postbacks; Dhan + Groww postback parsing needs equivalents
- See `backend/shared/brokers/{kite,dhan,groww}.py` for adapter shape; `backend/api/routes/orders.py` for the route layer

---

## Deployment Architecture

| Environment | Branch | Server path | Port | Domain | Runtime |
|---|---|---|---|---|---|
| Production | `main` | `/opt/ramboq` | 8502 | ramboq.com | Python venv |
| Development | any other non-main | `/opt/ramboq_dev` | 8503 | dev.ramboq.com | Python venv |

- GitHub push → webhook at `webhook.ramboq.com/hooks/update` → `dispatch.sh` → `deploy.sh <ENV> <REF>` → venv+pip → systemctl restart
- `webhook.ramboq.com` and `dev.ramboq.com` must be **grey cloud (DNS only)** in Cloudflare

### Branch Strategy
Both branches stay in sync (feature on `dev` → merge to `main` → fast-forward `dev`). Both permanent; webhook deploys each auto on push.

---

## Key File Map

### Helpers (`backend/shared/helpers/`)
- **`broker_apis.py`** — `fetch_holdings()`, `fetch_positions()`, `fetch_margins()` decorated with `@for_all_accounts` (returns list of DataFrames, one per account). 503 raised only when ALL accounts fail. `fetch_holidays(exchange)` caches per `(exchange, today's date)` to avoid hammering nseindia.com.
- **`connections.py`** — `Connections` singleton holds `KiteConnection` per account; 2FA / TOTP / token refresh. Re-authenticates every 23h (`conn_reset_hours`). Supports per-account IPv6 binding to work around Kite's one-IP-per-app rule.
- **`decorators.py`** — `@for_all_accounts`, `@retry_kite_conn()`, `@track_it()`, `@lock_it_for_update`
- **`singleton_base.py`** — Thread-safe singleton via double-checked locking
- **`utils.py`** — YAML loaders, `get_path()`, `get_nearest_time()`, validators (email, phone, password, PIN, captcha)
- **`genai_api.py`** — Gemini 2.5 Flash (gated by `cap_in_dev.genai` / `genai: False`); falls back to static content on rate-limit
- **`mail_utils.py`** — SMTP via Hostinger (gated by `cap_in_dev.mail` flag)
- **`date_time_utils.py`** — `is_market_open(now, holiday_set, market_start, market_end)` using `zoneinfo`. Weekends hardcoded closed; Muhurat overrides need explicit list.
- **`ramboq_logger.py`** — Rotating file handlers (5MB × 5), queue-based async
- **`summarise.py`** — `send_summary()` for open/close alerts
- **`alert_utils.py`** — Telegram + email dispatch (prefixes: `Open|Agent|Close`). Non-main branches tagged `[branch]` + ⚠.

### Webhook / Deployment (`webhook/`)
- **`deploy.sh`** — `deploy.sh <ENV> <REF>` (ENV=prod|dev). Git update + config merge + `deploy_branch` to `backend_config.yaml` + `notify_deploy.py` call.
- **`notify_deploy.py`** — Telegram-only deploy notification; reads config directly (no imports).
- **`initial_deploy.sh`** — One-time server setup; automates everything except secrets/certbot/DNS/webhook.
- **`hooks.json`** — HMAC-SHA256 validation + pass to `dispatch.sh`. **Copy manually to `/etc/webhook/hooks.json`** after edits.
- **`dispatch.sh`** — Routes to `deploy.sh` based on branch. **Copy manually to `/etc/webhook/dispatch.sh`** after edits.
- **`ramboq_hook.service`** — Listener port 9001; all branches routed via `dispatch.sh`.

---

## Config Files (`backend/config/`)

| File | Tracked | Purpose |
|---|---|---|
| `backend_config.yaml` | Yes | `retry_count`, `conn_reset_hours`, log levels, `cap_in_dev` dict, alert thresholds, market segments |
| `frontend_config.yaml` | Yes | Page content, nav labels, Gemini prompts, Mermaid diagrams |
| `constants.yaml` | Yes | Country codes, profile keys |
| `secrets.yaml` | No | SMTP, Kite API keys/TOTP, `cookie_secret`, Gemini key, Telegram token |
| `grammars/orders.yaml` | Yes | Order-entry grammar (tokens + parse rules) for frontend autocomplete + backend agent-builder |

### Reusable Command Grammars (`backend/config/grammars/`)
- **`orders.yaml`** — declarative token grammar for order-entry commands; loadable from Python for backend agent/admin-shell use
- **Frontend bridge:** [frontend/src/lib/command/grammars/orders.yaml](frontend/src/lib/command/grammars/orders.yaml) is a symlink into `backend/config/grammars/`; [frontend/src/lib/command/grammars/orders.js](frontend/src/lib/command/grammars/orders.js) loads it via Vite `?raw` import + `js-yaml` (added to `frontend/package.json`) and wires JS suggesters

`secrets.yaml` must be **hand-placed on the server** — never in git. `initial_deploy.sh` creates `backend_config.yaml`; subsequent deploys merge: repo config is the base (picks up new fields), only `enforce_password_standard`/`cap_in_dev`/`genai`/`telegram`/`mail`/`notify_on_startup` are overlaid from the server's saved copy. `deploy_branch` is always set fresh by the deploy script — never preserved.

### Production capabilities — `cap_in_dev` is a dict, not a scalar

`cap_in_dev` in `backend_config.yaml` is now a nested dict of per-capability flags. On **prod** (`deploy_branch == 'main'`) every capability is always on regardless of these flags. On **dev / any non-main branch** each flag independently toggles its capability.

```yaml
cap_in_dev:
  genai:            True   # GenAI market update (Gemini)
  telegram:         True   # Telegram notifications
  mail:             True   # Email notifications (SMTP)
  notify_on_deploy: True   # Deploy-OK ping on restart
  market_feed:      True   # Google News RSS feed for /api/news
```

**Gate helper**: `is_enabled('<cap>')` in `backend/shared/helpers/utils.py` returns `True` on the `main` branch unconditionally, otherwise reads `cap_in_dev.<cap>`.

**Adding a new production capability**:
1. Append `new_cap: True` under `cap_in_dev` in `backend_config.yaml`.
2. Gate usage with `is_enabled('new_cap')`.
3. No `deploy.sh` edit — the preserve loop uses a pattern match on `startswith("alert_")` for alert keys AND copies the entire `cap_in_dev` dict across deploys.

**Historical rename**: `notify_on_startup → notify_on_deploy`. No `news_in_dev` top-level flag — it's `cap_in_dev.market_feed` now.

---

## Alert and Notification System

### Message Types and Prefixes

| Event | Telegram prefix | Email subject prefix |
|---|---|---|
| Market open summary | `Open` | `RamboQuant Open: ` |
| Intra-day agent fire | `Agent` | `RamboQuant Agent: ` |
| Market close summary | `Close` | `RamboQuant Close: ` |
| Deploy notification | `Deploy OK` | `RamboQuant Deploy OK: ` |

User-facing vocabulary: **Agent** (rule) → **Alert** (runtime event) → **Notify** (delivery) → **Action** (side-effect). Subjects use "Agent" so the UI label, Telegram prefix, and email subject line all match.

### Timestamp Format
All alerts, summaries, and deploy notifications use dual-timezone format generated by `timestamp_display()` in `date_time_utils.py`:
`Mon, March 30, 2026, 09:30 AM IST | Mon, March 30, 2026, 10:00 PM EDT`
The EST side uses `%Z` so it correctly shows `EST` in winter and `EDT` in summer.

### Open/Close Summary Format
Sent per segment (Equity and Commodity independently):
- **Telegram**: `Open [branch] — Equity — <timestamp>` + `⚠ Branch: <name>` line (non-main only) + `<code>` monospace block
- **Email subject**: `RamboQuant Open: [branch]Equity — <timestamp>` (branch tag omitted on main)
- **Email body**: yellow banner for non-main + HTML `<table>` sections for Holdings, Positions, and Funds
- Holdings table: Account | Cur Val | P&L | P&L% | Day Loss | Day Loss%
- Positions table: Account | P&L
- Funds table: Account | Cash | Avail Margin | Used Margin | Collateral
- Accounts shown as masked values (ZG#### / ZJ####)

### Agent Alert Format
One row per breached threshold (abs, pct, and fund checks fire separate rows):
- Columns: Type | Account | Kind | Value | Detail | Abs Thr | Pct Thr
- Type ∈ `Holdings`, `Positions`, `Funds`
- Kind tags the rule that fired (`Static %`, `Static ₹`, `Rate ₹/min`, `Rate %/min`, `Cash < 0`, `Margin < 0`)
- Funds rows fired when `cash < 0` or `avail margin < 0` for any account (subject to cooldown)
- `—` shown for columns not applicable to that rule
- Email uses HTML `<table>` with per-kind row colour (yellow = static, red = rate, grey = funds); Telegram uses `<code>` monospace block in the narrow 2-line-per-row format
- Rows sorted `Holdings → Positions → Funds`, per-account before `TOTAL` — every agent that fires on the same tick consolidates into one message

### Intra-day loss rules — now v2 agents

Every loss / fund-negative rule ships as a `loss-*` Agent row (grammar tree
of metric/scope/op/value leaves). See `_LOSS_AGENTS` in
`backend/api/algo/agent_engine.py` for the 14 seeded rules. Default floors
(editable live from the /agents page, per agent):

| Scope (scope token) | Holdings % | Positions % | Positions ₹ |
|---|---|---|---|
| Per account (`holdings.any_acct` / `positions.any_acct`) | −3.0 % | −2.0 % | −₹30,000 |
| Total (`.total`)                                         | −5.0 % | −2.0 % | −₹50,000 |

Rate rules use the `day_rate_abs` / `day_rate_pct` / `pnl_rate_abs` /
`pnl_rate_pct` metrics; defaults are −₹2k/min (acct) and −₹4k/min (total)
for holdings, −₹3k/min (acct) and −₹6k/min (total) for positions, and
−0.15 %/min (holdings) / −0.25 %/min (positions) scope-agnostic. Two fund
agents (`loss-funds-cash-negative` / `loss-funds-margin-negative`) fire on
`cash < 0` and `avail_margin < 0`.

**Global gates** (engine-wide; `alert_*` keys in `backend_config.yaml`):

- **Market hours**: run_cycle skips `schedule: market_hours` agents outside segment-open hours.
- **Baseline offset**: `alert_baseline_offset_min` (15). Rate agents stay silent for this long after session start.
- **Rate window**: `alert_rate_window_min` (10) — minutes of P&L history used to compute ΔP&L/Δmin.
- **Cooldown**: `alert_cooldown_minutes` (30). After a fire, re-fire requires the cooldown AND (|Δpnl| ≥ `alert_suppress_delta_abs` (₹15k) OR |Δpct| ≥ `alert_suppress_delta_pct` (0.5 %)). Flat loss ⇒ silent for the rest of the session.
- **Session rollover**: in-memory suppression state wipes on date change.

`deploy.sh` preserves every `alert_*` key across deploys.

### Telegram Setup
- Bot token and group chat_id stored in `secrets.yaml` as `telegram_bot_token` and `telegram_chat_id`
- Group: **RamboQuant Alerts** (`-5227999198`)
- Bot: **@RamboQuantBot**

### Email Recipients

Three audiences, three separate lists in `secrets.yaml`:

```yaml
# secrets.yaml

# Operator alerts — loss / agent fires / open-close summaries.
# Stays small + private: just the trading-ops inbox.
alert_emails:
  - "rambo@ramboq.com"

# Public-website inbound mail — contact form submissions.
# Kept separate so marketing leads don't bleed into the alert thread.
market_emails:
  - "website.ramboquant@gmail.com"
  - "afridihajayt@gmail.com"
```

**Deploy notifications** ship Telegram-only (May 2026) — the prior
email path in `notify_deploy.py` was retired because deploy noise was
cluttering the operator inbox; the Telegram ping carries the same
information. To bring email back, restore the block from git history
and re-add a `deploy_emails` list.

**Routing helpers** (`backend/shared/helpers/alert_utils.py`):
- `get_alert_recipients()` — merges DB-derived users (admin opt-in +
  designated) with `secrets.alert_emails`. Used by every loss / agent /
  summary dispatch.
- `get_market_recipients()` — reads `secrets.market_emails`, falls
  back to `smtp_user`. Used only by the contact form
  (`backend/api/routes/contact.py`).

---

## Market Segments and Hours

Defined in `backend_config.yaml` under `market_segments`. Background thread handles each segment independently.

| Segment | Config key | Exchanges | Hours (IST) | Holiday source |
|---|---|---|---|---|
| Equity | `equity` | NSE, BSE, NFO, CDS | 09:15–15:30 | `kite.holidays("NSE")` |
| Commodity | `commodity` | MCX | 09:00–23:30 | `kite.holidays("MCX")` |

- Open summary sent `open_summary_offset_minutes` (default 15) after segment open; close summary sent `close_summary_offset_minutes` (default 15) after segment close
- Holiday calendars loaded at startup, refreshed on new year
- Weekends (Sat/Sun) are treated as closed across all paths: `is_market_open()` and `_task_close()` in `backend/api/background.py`. Special Saturday sessions (Muhurat etc.) need an explicit override
- Holdings always belong to equity segment; positions are filtered by `exchange` column

---

## Background Tasks (`backend/api/background.py`)

| Action | Timing |
|---|---|
| Market update cache warm | Immediately at app startup |
| Market update pre-fetch | Once per day at `market_refresh_time` (08:30 IST) |
| Performance data pre-fetch | Every `performance_refresh_interval` (5 min) during market hours |
| Open summary (per segment) | `open_summary_offset_minutes` (15) after segment open, once per day |
| Close summary (per segment) | `close_summary_offset_minutes` (15) after segment close, once per day |
| Loss alert check | Every performance fetch during market hours |
| Agent engine `run_cycle()` | Every performance fetch; skips `schedule: market_hours` agents when no segment is open |
| Sparkline past-close warm | Immediately at app startup; daily at 00:30 IST (midnight rollover — keeps cache hot for overnight / pre-market loads); plus once per market-segment open (NSE 09:15, MCX 09:00 IST) |

**Holiday calendar caching** — `fetch_holidays(exchange)` in `broker_apis.py` is now cached per `(exchange, today's date)` to avoid hammering nseindia.com on every `_build_context()` call (once per 5 min on the real path, once per 2 s in sim). The daily cache refreshes naturally at midnight IST and caches the empty set on API failure to avoid retry-hammering when the upstream is down.

**Sparkline cache** — three-tier split, disk-persisted:
- `_spark_past_cache` (in `backend/api/routes/quote.py`) keyed `(tradingsymbol, exchange, days, ist_date_str)` stores **past `days-1` daily closes**. The last bar of Kite's `historical_data` response is dropped only when its date matches today's IST date (intraday-running value); off-hours every settled close including yesterday is kept (`_trim_past_closes`). Populated lazily on cache miss via `historical_data` (3 req/sec budget); pre-filled by `_task_sparkline_warm` at startup + daily 00:30 IST + market-segment opens so operator's first Pulse load pays no historical fetch cost. Cache hit requires `len(closes) >= days-1`; partial entries trigger a throttled re-fetch (`_INCOMPLETE_RETRY_S = 300`) so a one-bar-short result from a holiday weekend doesn't stick for the day.
- `_spark_today_cache` (same file) keyed `(tradingsymbol, exchange, ist_date_str)` stores **today's intraday 30-minute closes** with a 5-min TTL. Lets the sparkline show today's actual path — operator sees the right edge wiggle as the session progresses, not just a single end-of-line dot. Pre-filled by the warm task at every boundary and refreshed lazily on each `/api/quote/sparkline` call when the cached entry is older than the TTL.
- Current LTP is appended at response time via the SSE ticker's `_tick_map` (zero Kite quota) with a `broker.ltp()` batched fallback (10 req/sec quota) — never stored. Final sparkline series = `past_closes + today_intraday + [current_ltp]`.
- **On-disk persistence**: `_spark_past_cache` + `_spark_today_cache` + `_spark_past_attempt` snapshots are written to `.log/sparkline_cache.json` after every warm cycle and every endpoint response that mutates the cache. Throttled to one write per 5 s, written atomically (tmp + rename) under an fcntl lock, kicked to a background thread so request handlers don't pay the fsync cost. On boot, `load_sparkline_cache_from_disk` (called from `on_startup` before any request can fire) restores entries whose `ist_date` matches today; stale-day files are skipped wholesale and the warm task rebuilds in ~30 s. Redeploy during market hours → operator's first /pulse load reads from the warm cache instead of waiting for lazy fetches.
- Symbol universe for warm: `watchlist_items` (DB) + live holdings + live positions, deduped, capped at 100.
- Warm state (symbols cached, last_warmed_at) is surfaced in `GET /api/admin/health` as `sparkline_warm`.

---

## KiteTicker / SSE live-LTP pipeline

Real-time per-symbol LTP feed via a persistent Kite WebSocket + Server-Sent Events broadcast to frontend clients. Replaces historical per-symbol polling so the operator sees live tick updates in `/pulse` and other MarketPulse surfaces without any broker-API polling overhead.

### Architecture

```
KiteTicker (Twisted reactor thread)
    │ on_ticks() callback
    ▼ _tick_map: dict[token → ltp]        (lock-guarded)
    │
    ├─ Direct read: get_ltp(token) → float | None
    │
└─ BroadcastBus.publish(payload)
    │
    ▼ asyncio.Queue per SSE client        (maxsize=1000)
    │
    ▼ GET /api/quote/stream              (Litestar ServerSentEvent)
    │
    ▼ EventSource('/api/quote/stream')    (browser auto-reconnect)
    │
    ▼ liveLtp writable store              (sym → ltp)
    │
    ▼ Svelte $effect (250ms throttle)
    │
    ▼ ag-Grid refreshCells (visible rows only)
```

### Backend pipeline

**TickerManager singleton** ([`backend/shared/helpers/kite_ticker.py`](backend/shared/helpers/kite_ticker.py)):
- One Kite WebSocket connection per process. Uvicorn runs with `--workers 1` in prod so the singleton lifetime is process-scoped.
- Account selection via `get_sparkline_broker()` — if two Kite accounts are loaded, sparkline ticker uses the account that is NOT pinned to `connections.price_account` (reserved for chart-historical calls). Operator can override via `connections.sparkline_account` setting.
- **Startup flow**: `TickerManager.start(api_key, access_token)` (idempotent) spawns Twisted reactor in a daemon thread. Callbacks fire on that reactor thread; asyncio handlers read via `get_ltp()` / `get_ltp_batch()` with a brief lock hold — no deadlock risk because the lock is non-reentrant and hold-time is O(1).
- **Deferred-start safety**: access token may not be available at `app.on_startup` (async Connections reload races startup hooks). `_task_sparkline_warm` calls `ticker.ensure_started()` ~25s after boot when credentials are hydrated; redundant `start()` calls are idempotent no-ops.

**Subscription lifecycle**:
- Pre-mass-subscribe at boot + on every `_task_sparkline_warm` cycle (watchlist symbols + holdings + positions) via `warm_sparkline_cache()`.
- Dynamic subscribe on watchlist add (`watchlist.py` POST hook calls `_resolve_token_for_sym()` → `ticker.subscribe_with_sym()`).
- Auto-subscribe in `batch_sparkline` for any symbols not yet in the tick stream; `_task_performance` background task seeds the ticker with intraday-discovered book symbols (new positions, new holdings).
- `subscribe()` is idempotent (tokens already subscribed are skipped) so re-subscribing is cheap.

**Threading model**:
- KiteTicker callbacks (`_on_connect`, `_on_ticks`, `_on_close`, `_on_error`, `_on_reconnect`) all fire on Twisted reactor thread.
- `_on_ticks` merges incoming `{instrument_token, last_price}` dicts into `_tick_map` under a lock, then publishes each tick to `BroadcastBus` outside the lock (so reactor doesn't stall while the bus iterates its queue set).
- Asyncio route handlers call `get_ltp(token)` / `get_ltp_batch(tokens)` which take the lock briefly — guaranteed no async deadlock because the lock is non-reentrant and the critical section is a dict read/write.

**Clean shutdown** ([`TickerManager.stop()`](backend/shared/helpers/kite_ticker.py#L299)):
- Sequence is critical so Kite doesn't hold a stale session when the process restarts:
  1. `stop_retry()` — kill the auto-reconnect loop FIRST so the moment we close, Twisted doesn't dial back in.
  2. `close()` — send WebSocket CLOSE frame to Kite so the server-side session ends cleanly.
  3. `kws.stop()` — halt the Twisted reactor so the daemon thread exits.
  4. 500ms grace sleep so the CLOSE frame leaves before the process exits.

### Failover — ticker watchdog

`_task_ticker_watchdog` in `backend/api/background.py` (runs every 30s) monitors KiteTicker health. If the ticker is started but disconnected for >60s:

1. Query `get_historical_brokers()` for the next eligible account (honours `historical_data_enabled` flag + 30s rate-limit cool-off).
2. Call `ticker.restart_with_account(api_key, access_token, new_account)` which:
   - Marks the failing account in a 5-minute do-not-retry cool-off so we never bounce between two simultaneously-broken accounts.
   - Preserves `_tick_map` (operator's UI shows last-known LTPs during failover).
   - Re-queues previously-subscribed tokens in `_pending` for the new connection.
   - Starts fresh WebSocket against the new account.
3. Log WARNING + broadcast `_broadcast_event("ticker_failed", {...})` to the agent engine.
4. Send Telegram alert if degradation persists >30min. Send recovery notice when ticker reconnects.

When the failing account's cool-off expires, we don't auto-failback — the new account stays primary until ITS WebSocket breaks. Operator can force re-assignment by changing `connections.sparkline_account` + restarting.

**IPv6 note**: KiteTicker uses Twisted WebSocket (not requests/urllib3), so the `_IPv6SourceAdapter` from `connections.py` doesn't apply. Each Kite account has a whitelisted IPv6 on the server. If the WebSocket fails with "Insufficient permission" in prod, we'd need to monkey-patch Twisted endpoint creation — non-trivial. Phase 1 defers that: when the socket can't connect, `get_ltp()` returns None and `batch_sparkline` falls back to `broker.ltp()` transparently. Design is safe to deploy now; Twisted IP-binding can follow if connectivity issues manifest.

### Frontend consumer

**quoteStream.js** ([`frontend/src/lib/data/quoteStream.js`](frontend/src/lib/data/quoteStream.js)):
- Exports `liveLtp` (sym → ltp) + `streamOpen` (bool) writable stores + `startQuoteStream() / stopQuoteStream()`.
- EventSource auto-reconnects natively; `streamOpen` flips false on persistent error so callers can widen polling cadence gracefully.
- SSE events: `snapshot` (sent once on connect; JSON object mapping token → `{ltp, sym}`), `tick` (per symbol per Kite tick frame; `{tok, sym, ltp, ts}`), `heartbeat` (every 30s when idle; keeps proxies from closing).

**MarketPulse integration** ([`frontend/src/lib/MarketPulse.svelte`](frontend/src/lib/MarketPulse.svelte)):
- Mounts SSE on init via `startQuoteStream()`.
- LTP cell renderer reads `liveLtp[sym] ?? row.ltp` — always shows live tick when available, falls back to last-known quote.
- Sparkline cellRenderer splices `liveLtp[sym]` as the final point of the closes array so the sparkline's last candle updates in real time.
- 250ms throttle + diff-gate on a rAF effect so steady-state burst (90+ ticks/sec) becomes ≤4 paints/sec.
- Polling fallback: `_TICK_QUOTES_SSE = 6` — when stream is healthy, `loadQuotes` runs every 6th tick (~30s); when down, every tick (5s).

**Backpressure handling**: each SSE client owns a private `asyncio.Queue(maxsize=1000)`. If the client reads slower than tick rate, `put_nowait` silently drops ticks (QueueFull is swallowed in `BroadcastBus._put_nowait`). Client reconnects via EventSource retry; it receives a fresh snapshot on reconnect and resumes from current state.

### API endpoints

| Route | Purpose |
|---|---|
| `GET /api/quote/stream` | SSE stream of LTP ticks. Returns `event: snapshot` (once), then `event: tick` (per tick), `event: heartbeat` (every 30s idle). Protected by `auth_or_demo_guard`. |
| `POST /api/quote/sparkline` | Bulk past-closes lookup; appends today's LTP from tick_map (zero Kite quota) or broker.ltp() fallback. Pre-fills `_spark_past_cache` on miss via `historical_data`. Subscribers are auto-registered with the ticker. |
| `GET /api/quote` (single) | One-off LTP + depth for a symbol. Used by order-entry command bar. |
| `POST /api/quote/batch` | Bulk quote (LTP + day change + OI). Used by MarketPulse grid for non-stream symbols. |

### Steady-state cost (market hours)

| Workload | Kite REST calls |
|---|---|
| LTP for any Pulse cell | 0 — read from in-memory tick_map via SSE |
| Sparkline for watchlist symbol | 0 during hours (all from WS). 1 historical_data on cache miss (3 req/sec budget, pre-warmed at open) |
| Watchlist add | 1 instruments() lookup per add |
| Chart historical | 1 per (symbol, range) on miss (separate from sparkline; uses `price_account`) |
| Total during trading hour | ~0 REST calls + 1 persistent WS |

---

## Broker accounts (DB-backed CRUD)

Operators add / edit / delete broker accounts via `/admin/brokers` instead of editing `secrets.yaml` on the server. Credentials live in the `broker_accounts` table; the three secret columns (`api_secret_enc`, `password_enc`, `totp_token_enc`) are Fernet-encrypted at rest.

**Encryption** ([`backend/shared/helpers/broker_creds.py`](backend/shared/helpers/broker_creds.py))
- Fernet (cryptography stdlib).
- Key derived from existing `secrets.cookie_secret` via HKDF-SHA256 with info tag `b"ramboq-broker-creds-v1"`. No new master secret to provision.
- Rotating `cookie_secret` invalidates the encrypted columns — operator has to decrypt-then-re-encrypt before rotation (we don't auto-do that; rotations are rare + explicit).
- `encrypt(plaintext) -> str`, `decrypt(ciphertext) -> str`, plus `encrypt_dict(payload, fields)` / `decrypt_dict` for the route layer.

**Loading** ([`backend/shared/helpers/connections.py::Connections.rebuild_from_db`](backend/shared/helpers/connections.py))
- `Connections.__init__` still seeds synchronously from `secrets.yaml::kite_accounts` (works during module imports before any DB session exists).
- `_rebuild_broker_connections` runs in `app.on_startup` after `init_db`. It calls `Connections().rebuild_from_db()`:
  1. Query `broker_accounts` (active rows only).
  2. If empty AND `secrets.yaml` has `kite_accounts`: SEED the DB once (encrypts each YAML cred + writes a row) → re-query.
  3. Decrypt secrets in memory, rebuild `self.conn` map.
  4. If both DB and YAML are empty, `self.conn = {}` (the broker registry will then 502 on any market-data call until an account exists).
- Every CRUD mutation on `/api/admin/brokers/*` calls the same rebuild, so credential changes are picked up without a service restart.

**API** ([`backend/api/routes/brokers.py`](backend/api/routes/brokers.py), admin-guarded)

| Route | Purpose |
|---|---|
| `GET /api/admin/brokers` | List metadata for every account (no secrets ever returned). Each row includes a `loaded` boolean — true when the account is in the live `Connections` map. |
| `GET /api/admin/brokers/{account}` | Single-account metadata. |
| `POST /api/admin/brokers` | Create (full body: `account, broker_id, api_key, api_secret, password, totp_token, source_ip?, is_active?, notes?, historical_data_enabled?`). |
| `PATCH /api/admin/brokers/{account}` | Partial update. Empty / missing secret fields → "leave unchanged" so a partial form doesn't accidentally clear a TOTP seed the operator didn't intend to rotate. Includes `historical_data_enabled` toggle. |
| `DELETE /api/admin/brokers/{account}` | Remove the row. |
| `POST /api/admin/brokers/{account}/test` | Reload Connections, then call `broker.profile()` to verify the credential pipeline. Reports the authenticated `user_name` on success or the broker error verbatim on failure. |

**UI** ([`frontend/src/routes/(algo)/admin/brokers/+page.svelte`](frontend/src/routes/(algo)/admin/brokers/+page.svelte))
- Account table — one row per broker account with code / broker / api_key / source_ip / status pill (LOADED / PENDING / DISABLED) / notes / Test button / Edit / Delete.
- Edit + Create form — same fields; Edit form's secret inputs default to blank with a `(blank = unchanged)` hint so the operator can update one credential without re-typing the rest.
- Test button hits `/test`, shows ✓ / ✗ inline next to the row with the broker's response in the tooltip.
- Polling: every 15 s so the LOADED status pill catches up after a save without a manual refresh.

**Migration path from secrets.yaml** — first deploy of this feature, the table is empty, the YAML has accounts, and the seed-from-YAML happens automatically on startup. The YAML rows stay (recovery backup). Subsequent edits go through the UI; the YAML diverges from the DB but is never overwritten (so there's a path back if the DB row gets corrupted: just clear the table and restart, the seeder runs again).

---

## Multi-Account IPv6 Source Binding (Kite + Dhan)

> **2026-06-16 — Resolved via IP-sharing across brokers.** The
> Hostinger VPS edge router only egresses traffic from the two
> documented working IPs (`2a02:4780:12:9e1d::1` IPv6 and
> `69.62.78.136` IPv4). The other addresses in the documented `/48`
> (`::2`–`::5`) bind cleanly to `eth0` but their outbound packets
> are silently dropped at the provider edge. Confirmed with `curl
> --interface 2a02:4780:12:9e1d::4 https://auth.dhan.co/` → connect
> timeout 10s, vs `::1` → 200 OK.
>
> **What works in prod today** — each broker account binds to one of
> the two egressing IPs, and IPs are SHARED across brokers (different
> brokers maintain independent per-IP session registries, so a Kite
> account and a Dhan account can sit on the same IP with zero
> interference):
>
> | Account | Broker | source_ip |
> |---|---|---|
> | ZG0790 | Kite | `69.62.78.136` |
> | DH6847 | Dhan | `69.62.78.136` (shares Kite's IPv4) |
> | ZJ6294 | Kite | `2a02:4780:12:9e1d::1` |
> | DH3747 | Dhan | `2a02:4780:12:9e1d::1` (shares Kite's IPv6) |
> | GR87DF | Groww | (default route) |
>
> Both Dhan accounts now load successfully with stable tokens — zero
> rotation events in the steady state. Operator does NOT need to
> rotate which Dhan account is active.

**Defense-in-depth at the broker registry level:**

1. **Dhan multi-account stabilizer** in
   [`Connections.rebuild_from_db`](backend/shared/helpers/connections.py)
   groups all Dhan rows by `source_ip` (treating blank as "OS
   default"). If two Dhan rows would share the same egress IP, only
   the lowest-`priority` row is loaded; the rest are deferred with
   a warning log. Operator swaps which one is active by editing
   `broker_accounts.priority` in `/admin/brokers`. With the IP-sharing
   layout above this never fires in practice — but if a future
   operator forgets to set `source_ip` on a new Dhan account, the
   stabilizer prevents the rotation cycle from starting.

2. **PriceBroker soft-failure on empty quotes** in
   [`backend/shared/brokers/registry.py`](backend/shared/brokers/registry.py)
   — `_quote_has_data` / `_ltp_has_data` predicates check that the
   returned dict has at least one symbol with a usable `last_price`
   or `ohlc.close`. When a broker returns `{}` (Dhan does this for
   MCX commodity futures it doesn't expose, even though the call
   succeeds), PriceBroker now treats the empty response as a soft
   failure and falls through to the next broker. Without this, a
   single Dhan-first preference would mask Kite's MCX coverage and
   spot resolution would land on the median-strike fallback (the
   "CRUDEOIL spot 8200" bug, 2026-06-16).

3. **Same-day-expiry rollover** in
   [`backend/api/algo/derivatives.py::lookup_future_for_option`](backend/api/algo/derivatives.py)
   — when the OPTION's own expiry is today or earlier, the resolver
   rolls past the matched-month future to the next listed contract.
   MCX commodity options settle ~5 business days before the future
   (e.g. CRUDEOIL JUN options expire Jun 16, JUN future expires Jun
   19), so the operator's broker app rolls to JUL during the late
   session — and so does the chart now.

**If you ever need MORE than 2 working IPs** (e.g. 3+ Dhan accounts
on this VPS): open a Hostinger support ticket asking them to enable
egress routing for the full `2a02:4780:12:9e1d::/48` subnet. They
currently filter source IPs at the provider edge. Once they unblock
the subnet, you can bind each Dhan account to its own `/128` from
the documented allocation below. Until then, 2 Dhan accounts is the
ceiling without a residential proxy / per-account VPS.

Two of the three broker integrations enforce some form of "one active session per source IP" rule and need dedicated IPv6 binding when more than one account is loaded on the same server. Groww does not.

| Broker | Rule | What breaks without binding |
|---|---|---|
| **Kite (Zerodha)** | One IP whitelisted per Kite app. Every account uses its own Kite app + own API key. | The second account's calls go through the wrong-whitelisted IP and Kite returns `Insufficient permission`. |
| **Dhan** | One active access token per partner app per source IP at the v2 auth backend. Every successful `generate_token` from one account invalidates the prior token of every other account on the same source IP. | 3-minute token rotation loop in `api_log_file` — accounts alternate `DH-906: Invalid Token` and "login complete" lines. Positions / holdings silently empty for the bad account each cycle. |
| **Groww** | No per-IP rule observed in prod. | n/a — but wired defensively. The SDK uses module-level `requests` calls so we monkey-patch the module namespace + use a per-thread ContextVar to route through a source-bound session pool. Login + runtime both bind. |

**Solution:** All Kite + Dhan accounts use IPv6 addresses from the server's `/48` subnet (`2a02:4780:12:9e1d::/48`). Each account binds to a unique IPv6 via the `source_ip` column on `broker_accounts`. Every account **must** have `source_ip` set — without it the OS may choose IPv4 or IPv6 unpredictably and the per-IP rules above kick in.

**Aspirational allocation** (only usable once Hostinger unblocks the
`/48` egress). The actual live allocation is the IP-sharing table at
the top of this section.

| Account | Broker | Aspirational source_ip | Whitelist required at broker? |
|---|---|---|---|
| ZG0790 | Kite | `2a02:4780:12:9e1d::2` | Yes (Kite developer console) |
| ZJ6294 | Kite | `2a02:4780:12:9e1d::3` | Yes (Kite developer console) |
| DH3747 | Dhan | `2a02:4780:12:9e1d::4` | No |
| DH6847 | Dhan | `2a02:4780:12:9e1d::5` | No |
| (future) | any | `2a02:4780:12:9e1d::N` | Kite: yes; Dhan: no |

Server IPv4 (`69.62.78.136`) **was historically** reserved for web
traffic, but with Hostinger's edge filter blocking `::2`–`::5`, we
now use it for broker traffic too (shared between ZG0790 Kite and
DH6847 Dhan).

### Adding a new account

1. Choose the next IPv6: `2a02:4780:12:9e1d::N` (next free slot).
2. Add it to the server: `sudo ip -6 addr add 2a02:4780:12:9e1d::N/48 dev eth0`
3. Make persistent: add to `/etc/netplan/50-cloud-init.yaml` under `addresses:` then `sudo netplan apply`.
4. Add the broker account row via `/admin/brokers` with `source_ip = 2a02:4780:12:9e1d::N`. The connection rebuild on save picks it up — no service restart needed.
5. **Kite only**: whitelist `2a02:4780:12:9e1d::N` in the Kite developer console for that account's app.
6. Clear the relevant token cache when changing source IP for an existing account: `rm /opt/ramboq/.log/kite_tokens.json /opt/ramboq/.log/dhan_tokens.json` (do the same on `/opt/ramboq_dev/`).
7. **Dhan only**: confirm the rotation pattern stops. After deploy + IPv6 set, the `Dhan rotation pattern detected` ERROR log in `api_log_file` should stop firing. If it doesn't, the cause isn't per-IP affinity — check the Dhan dashboard's Settings → DhanHQ Trading APIs → Token validity dropdown (should be 24 h, not 5 min) and verify the two broker_accounts rows aren't pointing at the same physical Dhan account.

### Token caching

Tokens are cached per-broker in `.log/` (gitignored, per-environment): `kite_tokens.json`, `dhan_tokens.json`, `groww_tokens.json`. On startup, cached tokens are restored without re-running the login flow. Full login only fires on cache miss or token expiry. Clear the cache file when changing `source_ip` or API credentials so the next call mints a fresh token from the new source IP.

### Implementation

`_IPv6SourceAdapter` (in `connections.py`) extends `requests.HTTPAdapter` to set `source_address` on the urllib3 pool manager. Each broker integration mounts the adapter on every `requests.Session` that talks to the broker API:

- **Kite** — adapter mounted on `KiteConnect.reqsession` (runtime API calls) and the login session.
- **Dhan** — adapter mounted on `DhanContext.dhan_http.session` (runtime API calls) and on a custom `_login_session()` factory used by `_do_login` / `_try_renew`. The login path bypasses `dhanhq.auth.DhanLogin` (which uses module-level `requests.post`/`requests.get` with no session hook) and calls `https://auth.dhan.co/app/generateAccessToken` and `https://api.dhan.co/v2/RenewToken` directly through the source-bound session.
- **Groww** — the SDK (`growwapi.groww.client`) uses module-level `requests` calls with no `session` attribute we can mount on. `_install_groww_source_binding()` replaces the `requests` reference inside the SDK's module namespace with a proxy that reads a per-thread `_GROWW_SOURCE_IP_OVERRIDE` ContextVar and routes through a pooled source-bound `requests.Session` per source IP. `GrowwBroker._retry_groww_auth` sets the ContextVar for every method call; `_mint_access_token` sets it for the login POST. Idempotent install — multiple `GrowwConnection` instances on different IPs trigger the patch once.

The adapter is applied per-account; accounts with no `source_ip` configured use the OS default route (works fine for single-account deployments).

---

## Key Patterns

### Caching
In-process TTL cache in `backend/api/cache.py` with per-key locking. `get_nearest_time()` rounds down to the nearest N-minute interval — use it as a cache key when aligning requests to fixed intervals. Background tasks pre-warm the cache before users hit the page.

**Holiday calendar** — `fetch_holidays(exchange)` in [`broker_apis.py`](backend/shared/helpers/broker_apis.py) is cached per (exchange, today's date) in a module-level dict (`_HOLIDAY_CACHE`). The agent engine's `_build_context` calls this on every `run_cycle` (every 5 min real path, every 2 s in sim), and without the cache that meant a blocking HTTP GET to nseindia.com per tick. The holiday list only changes once a year — cache buster is today's date, so rollover happens naturally at midnight. Empty sets (API failure) are also cached to avoid retry-hammering when nseindia is down.

### Multi-Account Broker Calls
`@for_all_accounts` in `decorators.py` wraps broker functions to iterate all accounts. Each call returns a **list of DataFrames** (one per account). Callers use `pd.concat(..., ignore_index=True)` to merge them.

### Account Masking
`mask_column(col)` in `utils.py` replaces all digits with `#` — `ZG0790` → `ZG####`. Applied in `fetch_holdings` and `fetch_positions` cache functions. All alert and summary messages use masked account values.

### Singleton Connections
`Connections` is a thread-safe singleton. Access it as `connections.Connections()` — never instantiate `KiteConnection` directly. The singleton is initialised once at app startup and reused across requests.

---

## Things to Avoid

- **Do not mock broker API calls** — `@for_all_accounts` and `Connections` singleton behave differently
- **Do not commit `secrets.yaml`** — gitignored; SSH-edit both `/opt/ramboq*` on server
- **Do not add branch filters to `hooks.json`** — routing handled in `dispatch.sh`
- **Do not use `2>>&1` in systemd** — use `2>&1`; `>>` causes bash syntax errors
- **Always `chown www-data` after server ops** — `sudo chown -R www-data:www-data /opt/ramboq*/.git /opt/ramboq*/.log`
- **Weekends hardcoded closed** in `is_market_open()` + `_task_close()` — special Muhurat sessions need explicit override

---

## API Architecture (Litestar + SvelteKit)

### Key Technologies
- **API framework**: Litestar 2.x with msgspec.Struct schemas (~10× faster than pydantic)
- **DataFrame**: Polars for API route aggregation; pandas in broker/alert layer
- **Database**: PostgreSQL 17 via SQLAlchemy 2.x async + asyncpg. `ramboq` (prod/main) and `ramboq_dev` (dev/non-main). Selected by `deploy_branch`.
- **Background**: Four asyncio tasks: market warm, performance refresh, close summaries, expiry auto-close
- **Auth**: JWT HS256 (24h TTL), PBKDF2-SHA256 passwords, admin approval for partners
- **Algo**: Adaptive limit-order chase engine for expiry auto-close (no market orders)
- **Holidays**: NSE official API (`nseindia.com/api/holiday-master`) for NSE/NFO/MCX/CDS
- **SEO**: OG/Twitter cards, JSON-LD, sitemap.xml, robots.txt, per-page titles

### Database
- **PostgreSQL** on server, port 5432
- Credentials in `secrets.yaml`: `db_user`, `db_password`
- `deploy_branch == 'main'` → `ramboq`; any other → `ramboq_dev`
- Tables: `users` (32 cols), `algo_orders`, `algo_events` — auto-created on startup

### API File Map
- **`backend/api/app.py`** — Litestar app; startup: init_db + background tasks; serves SvelteKit build
- **`backend/api/database.py`** — PostgreSQL via asyncpg; DB selected by deploy_branch
- **`backend/api/models.py`** — User (32 cols), AlgoOrder, AlgoEvent
- **`backend/api/background.py`** — Four tasks: market, performance (sends open summary + loss alerts directly), close (sends close summary directly), expiry check (09:20 IST daily)
- **`backend/api/algo/chase.py`** — Reusable adaptive limit-order chase engine
- **`backend/api/algo/expiry.py`** — Expiry-day auto-close: scan ITM/NTM, chase-close positions
- **`backend/api/algo/agent_engine.py`** — Declarative agent runner. `run_cycle()` enforces each agent's `schedule` field — agents with `schedule: market_hours` are skipped when no market segment is open, preventing stale NSE equity P&L alerts from firing during MCX-only hours (15:30–23:30). `seed_agents()` syncs the `schedule` field on existing DB rows and forces built-in agents to `inactive` when the YAML definition marks them so; user-customized conditions/cooldown/events/actions are preserved.
- **`backend/api/routes/algo.py`** — Agents API + WebSocket `/ws/algo` + `POST /grammar/reload`
- **`backend/api/routes/grammar.py`** — Admin token CRUD (`/api/admin/grammar/tokens*`); the UI for this lives at `/admin/tokens` (page title "Tokens")
- **`backend/api/routes/simulator.py`** — Market simulator control plane (`/api/simulator/*`); pairs with `backend/api/algo/sim/driver.py`
- **`backend/api/routes/auth.py`** — Login (24h JWT), register (pending approval), me, logout
- **`backend/api/routes/admin.py`** — Create/approve/reject/update users, logs, exec

### Built-in Agents (seeded from YAML)
Summary agents (`nse_open_summary`, `nse_close_summary`, `mcx_open_summary`, `mcx_close_summary`) are **`status: "inactive"` by default** — `_task_performance` and `_task_close` in `backend/api/background.py` already send open/close summaries directly, so enabling the agents would cause duplicate alerts. `seed_agents()` force-resets these four to inactive on every startup.

**Loss agents** (prefix `loss-`) ship as 12 pure alerting agents (holdings/positions static + rate thresholds) plus 2 fund-negative agents, all **active** by default. One additional `loss-pos-total-auto-close` agent ships **inactive** (destructive close-position action). The former `check_and_alert` loss engine is retired; toggle any agent individually from the `/agents` page.

### SvelteKit Pages (routes under `frontend/src/routes/(algo)/`)
- **`+layout.svelte`** — algo-site top nav, ordered by usage frequency: Dashboard · Agents · Orders (live monitoring) → Charts · Options (analysis surfaces) → Paper · Simulator (execution modes) → Terminal · Tokens (build / extend) → Settings · Brokers (configuration) → Users (admin). The "Investor site" cross-link is mellowed (font-weight 500, alpha 0.10 bg, alpha 0.32 border) — same amber colour as the public side's "Algo Site" pill, lower visual intensity so it reads as a context-switch affordance rather than a CTA.
  - Polls `/api/simulator/status` (4 s) and renders the sticky red **SIMULATOR** banner on every algo page while a sim is running.
  - Polls `/api/charts/paper-status` (4 s) and renders a sticky sky-blue **PAPER** banner whenever the prod paper engine has open chase orders. Both banners can stack — sim sits on top, paper underneath. Stickiness pins them just under the navbar so they never scroll out of view.
- **`performance/`** (public) and **`dashboard/`** (admin). The public page uses `PerformancePage.svelte` (two-row header with timestamp + Refresh on top, tabs + account picker below). The admin `/dashboard` is its own page composed of three sections in order: **(1) P&L Analysis** (`PnlAnalysis.svelte`), **(2) MarketPulse summary grids** (Funds + Positions Summary + Holdings Summary; no per-symbol grid), **(3) Agent activity** (`UnifiedLog.svelte` filtered to `agent_fire / agent_action_success / agent_action_error` kinds). The summary grids on `/dashboard` scope to the selected account (sibling accounts + TOTAL filtered out) and hide the Account column when only one account is in view. Performance **always** shows real Kite data; the background refresh keeps going even while the simulator is active. The algo theme (`ag-theme-algo`) is the dark navy-gradient variant. Pulse symbol-cell encoding (simplified per audit + operator feedback — direction lives in the background tint, no extra inset bars):

- **Background tint** is the single direction/identity indicator on every bucket grid:
  - `pos-long` green 10 %, `pos-short` red 10 % (matching Bloomberg / IBKR / Sensibull / Streak convention)
  - `row-hold-up` green 10 %, `row-hold-down` red 10 %, `row-hold-flat` slate 8 %
  - `row-watch` amber 10 %, `row-und` violet 10 %, `row-pos` slate 8 %
  - Per-bucket row background on Winners/Losers grids: green/red 6 %
- **Day P&L mini-bar** — a 2 px pseudo-element bar at `right: 6 px` inside the symbol cell, painted only on Positions + Holdings rows (`day-pnl-up` / `-down` / `-flat`, same green/red/slate palette). 4 px gap from the cell's right edge so it never visually merges with neighbouring colour. Hidden on TOTAL rows.
- **CE / PE** — `.sym-main.sym-ce` green, `.sym-main.sym-pe` red on the symbol TEXT. Sensibull / Streak convention.
- **Account tint** (`mp-sym-acct` on Positions/Holdings) — subtle 14 % bg using the account's hash colour. No inset bars; the trailing Account column at the row's right edge is the primary account identifier.
- **TOTAL row** (`mp-total-row`) — amber 12 % bg + 2 px amber-55 % top border + 1 px amber-25 % bottom border + bold. Aggregate is direction-agnostic; no green/red sign variants. Day-P&L mini-bar hidden. Industry analogue: Bloomberg PRTU's TOTAL line + Sensibull's gold-accent footer.

**Duplicate-class debt** — two parallel directional-cell families exist with same semantics: `cell-pos` / `cell-neg` / `cell-flat` (MarketPulse / algo theme — text colour only) and `pnl-gain` / `pnl-loss` / `pnl-zero` (PerformancePage / ramboq theme — text + background). NOT consolidated because the two themes apply different visual treatments and behaviour-equivalence isn't trivial. Document here; consolidate in a future pass if it becomes a real friction point.
- **`market/`** — AI market report with timestamp
- **`signin/`** — Sign In / Register (name, email, phone)
- **`admin/`** — User management with full partner fields
- **`admin/tokens/`** — Agent Tokens page (Condition / Notify / Action tabs), create/edit custom tokens, Reload Registry. UI label is "Tokens"; the DB table and Python class keep their legacy names (`grammar_tokens`, `GrammarRegistry`) because the data model IS a grammar in the compiler-theory sense. Route: `/admin/tokens`.
- **`admin/simulator/`** — Market simulator control plane. Scenario dropdown · Seed (Scripted / Live / Live+scenario) · Rate · Load live book · Start / Stop / Step / Run cycle / Clear sim. Shared `LogPanel` embedded at the bottom, defaulted to the Simulator tab (streams per-symbol LTP diffs in real time). See **Simulator** section below.
- **`agents/`** (formerly `/algo`) — Agents page: grouped compact rows (Loss & Risk / Summaries / Automation / Other), click-to-expand, Edit with live condition validation, per-row "Run in Simulator" button that deep-links to `/admin/execution?mode=sim&agent_id=<id>`. The Agent-events panel auto-scopes: real events when sim is idle, sim events when a sim is running.
- **`console/`** — Terminal: command textarea + output + live log (equal panels)
- **`orders/`** — Order management. Entry card has 3 tabs: Order Ticket (BUY/SELL form) · Option Chain · Command Line. Chart-icon button on entry header + every row's symbol cell opens `<ChartModal>` for that symbol.
- **`charts/`** — Unified chart workspace. Reads `?symbol=…&mode=…` URL params. Single `<ChartWorkspace>` instance with RefreshButton in page header.
- **`agents/activity/`** — Recent agent fires (and optional action-success / -error events). Same `UnifiedLog` component the dashboard renders, lifted into a dedicated route inside the agent workspace so operators asking "what fired today?" don't have to scroll past P&L analysis.

---

## Canonical card-header rule (apply by default to every card)

Every card across every algo page follows ONE structural rule. The operator gets identical mental model + muscle memory regardless of which page they're on. Update this section when the rule changes — every subsequent edit MUST conform to whatever is documented here.

### Markup layout (left-to-right inside the card's `<div class="…header">`)

```
[Title/Label]  [Tabs?]  [AccountMultiSelect?]  [Inline chips?]  → … spacer …  [Card-control trio]
```

- **Text / title left-aligned.** The label or `mp-section-label`/`page-title-chip` sits at the leftmost slot of the header. Optional sub-tabs follow immediately after (underline pattern: `mp-toptab`, `cap-eq-tab`, `legs-tab`, `lab-tab`, `exec-tab`, `mp-wl-tab`).
- **AccountMultiSelect (and similar pickers) LEFT-aligned**, right after the tabs/label. Reads as part of the card's identity strip, not part of the controls cluster. The `margin-left: auto` previously on `.bucket-header > .ams` is now `margin-left: 0` — do NOT push pickers right.
- **Card-control trio RIGHT-aligned** via the first button's `margin-left: auto`.

### Default-mode controls (card NOT in fullscreen)

Visible icons, left-to-right:

```
[Collapse]  [Fullscreen]
```

Markup order at every callsite (DefaultSizeButton stays in the middle but self-hides when not fullscreen):

```svelte
<CollapseButton bind:isCollapsed={_colXxx} cardId="…" label="…" />
<DefaultSizeButton bind:isFullscreen={_fsXxx} bind:isCollapsed={_colXxx} label="…" />
<FullscreenButton bind:isFullscreen={_fsXxx} label="…" />
```

- No RefreshButton in default mode (page-level header has one).
- No notification bells in default mode (they live in the page header).
- Cyan-400 palette: `#22d3ee` resting, `#67e8f9` hover, bg α 0.14, border α 0.55.
- 1.4rem × 1.4rem buttons with 0.3rem inter-button gap.

### Fullscreen-mode controls (card has `.fs-card-on` class)

Visible icons, right-to-left:

```
… [OrderNotif] [AgentNotif] [Refresh] [Default]
       5.2rem      3.4rem     1.7rem    base
```

Conditionally rendered at each callsite (place the RefreshButton BEFORE the rest of the trio in markup; visibility is `{#if _fsXxx}`):

```svelte
{#if _fsXxx}
  <RefreshButton onClick={pageRefresh} loading={_refreshing} label="…" />
{/if}
<CollapseButton … />  <!-- hidden via .fs-card-on .collapse-btn { display: none } -->
<DefaultSizeButton … />  <!-- self-shows via {#if isFullscreen} -->
<FullscreenButton … />  <!-- self-hides via {#if !isFullscreen} -->
```

- `PageHeaderActions` is NOT placed inside the card — it's the global page-header instance. In fullscreen mode the three buttons stay in the header (no viewport-pin needed since they open modals, not popovers).
- DefaultSizeButton glyph is the Windows "Restore Down" two-overlapping-rectangles icon (visually distinct from FullscreenButton's outward arrows).
- All four card-control icons share the same cyan-400 palette.

### Width on collapse

Every card root carries `width: 100%; box-sizing: border-box;` so collapse never shrinks the card horizontally. Icons stay in their default location of the header regardless of body state.

### Page-header rule (every algo page)

The canonical page-header is:

```svelte
<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">…</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={pageLoadFn} loading={loading} label="…" />
    [optional page-specific action chips]
    <PageHeaderActions symbol={contextSymbol} hideOrder={isOrdersPage} hideChart={isChartsPage} />
  </span>
</div>
```

The `<span class="page-header-actions">` wrapper keeps all icons together as a single `inline-flex` unit — on narrow viewports the title + timestamp can wrap to the first row but the icon cluster never splits across lines.

`PageHeaderActions` renders three vibrant gradient buttons:
- **Order** — vivid amber (`#f59e0b → #d97706`). Opens `SymbolPanel` (order ticket). Hidden on `/orders` (`hideOrder={true}`).
- **Chart** — vivid cyan (`#06b6d4 → #0891b2`). With a `symbol` prop in scope, opens `ChartModal` inline. Without one (Pulse / Dashboard / Agents / etc.) the icon navigates to the `/charts` workspace where the operator picks a symbol — was previously disabled. Hidden on `/charts` (`hideChart={true}`).
- **Log** — vivid violet (`#a855f7 → #7e22ce`). Opens `ActivityLogModal` (Order Book + Agent Log tabs). Always shown.

Pass `symbol` when the page has a contextual symbol in scope:
- `/admin/derivatives` → `symbol={selectedUnderlying}`
- `/orders` → `symbol={_entrySymbol}` (+ `hideOrder={true}`)
- `/admin/research` → `symbol={selected?.symbol ?? ''}`
- `/charts` → `hideOrder={true} hideChart={true}` (page has its own order button; chart is the page itself)
- All others → no `symbol` prop (order modal opens with empty picker; chart icon navigates to `/charts`)

- **Every page that fetches data dynamically MUST have a page-header RefreshButton** wired to its primary load function. If the page has multiple loaders, wrap them: `onClick={() => { loadA(); loadB(); }}`.
- Exceptions: `console` (command surface, no data), `admin/settings` (form-only), `showcase` (static narrative).
- The `RefreshButton` placement is between the `ml-auto` spacer and `PageHeaderActions`. Page-specific action chips (back-links, ✦ Ask AI, Create User, etc.) sit between the RefreshButton and `PageHeaderActions` in markup order.
- Connection-status badge on the RefreshButton is automatic — every mounted `<RefreshButton />` subscribes to the global `connStatus` store; no per-callsite wiring needed. The badge keeps its size/color/number unchanged during a refresh (only the icon glyph swaps). It encodes THREE distinct states so the operator can diagnose without leaving the page:

  | State | Trigger | Badge |
  |---|---|---|
  | **API unreachable** | `connStatus.backendOk === false` (poller's fetch rejected) | grey `?` |
  | **All brokers loaded** | `loaded === total` | green count |
  | **Partial brokers** | `0 < loaded < total` | amber count |
  | **No brokers loaded** | `loaded === 0 && total > 0` | red count |
  | **No broker config** | `total === 0` (demo / fresh install) | no badge |

  Polling auto-retries forever via `visibleInterval(poll, 15000)`, so a backend or broker outage clears on its own as soon as connectivity returns — the operator doesn't have to do anything. When the backend goes offline mid-session, the last known broker state stays cached so the operator can still see WHICH brokers were running before the outage.

- The RefreshButton swaps its **icon glyph** during `loading=true`: idle shows the canonical circular-arrow refresh symbol; loading shows a distinct arc-spinner glyph (NOT the same arrow rotated). Operator can tell at a glance "the icon changed shape, so a refresh is in flight" without needing a separate text indicator. Do NOT add `RefreshAge` / "Updated Xs ago" visible text chips alongside — the spec was tested and dropped because the text widened the header and competed with the notification icons.
- The RefreshButton's **native tooltip** carries a multi-line summary so the operator can diagnose any connection state without leaving the page:

  ```
  Refresh — 1 of 2 broker accounts loaded     ← line 1: action + state
  Failed: ZG####                              ← line 2: WHICH broker(s), only when loaded < total
  Last refreshed: Sun 30 May · 21:42 IST · 12:12 EDT  ← line 3: data freshness
  ```

  Line 1 morphs by state:
  - mid-refresh → `Refreshing…`
  - backend offline → `API unreachable — retrying every 15s`
  - all loaded → `Refresh — N of M broker accounts loaded`
  - no broker config → `Refresh now`

  Line 2 only renders when `failingAccounts.length > 0` AND the backend is up (the list may be stale during an outage, so we hide it to avoid misleading the operator).

  Line 3 uses `formatDualTz()` from `$lib/stores` which produces the same `Sun 30 May · 21:42 IST · 12:12 EDT` shape the page-header wall clock renders. Updated automatically by the RefreshButton on every `loading` true → false transition, plus direct `lastRefreshAt.set(Date.now())` calls from pages whose auto-pollers (`loadHero` on dashboard, `loadPulse` inside MarketPulse) don't go through the button's `loading` prop.

### Chart cards in fullscreen mode

Every chart inside a card MUST scale to fill the viewport when the parent card is `.fs-card-on`. The chart container's height is normally `var(--chart-h, …)` (pinned via a `style="--chart-h: {height}px"` on the wrapper). In fullscreen, override:

```css
:global(.fs-card-on) .<chart-element> {
  height: calc(100vh - 10rem) !important;
  min-height: 320px;
}
@media (max-width: 600px) {
  :global(.fs-card-on) .<chart-element> {
    height: calc(100vh - 8rem) !important;
  }
}
```

The `10rem` budget covers the card's 2rem inset (top+bottom = 4rem) + card header + comfortable bottom pad. Charts with stat overlays or a heavier header (e.g. OptionsPayoff has stats + legend + footnote) use `calc(100vh - 12rem)` instead.

Existing implementations to copy from:
- [`OptionsPayoff.svelte`](frontend/src/lib/OptionsPayoff.svelte) — `.payoff-svg-stack` rule, `12rem` budget
- [`PriceChart.svelte`](frontend/src/lib/PriceChart.svelte) — `.chart-svg + .chart-empty` rule, `10rem` budget
- [`dashboard/+page.svelte`](frontend/src/routes/(algo)/dashboard/+page.svelte) — `.eq-svg` rule, `10rem` budget

### Picker / control bars on pages (Account · Underlying · Expiry · trailing buttons)

Every page-level picker bar (e.g. /admin/derivatives `.opt-picker`) follows this rule:

- **All fields LEFT-aligned at natural widths.** No single field uses `flex: 1 1 0` to stretch across the page.
- **Empty space on the right.** No flex-grow consumer at the trailing edge; the right side stays empty so the leftmost field doesn't visually drift away from the next.
- **Mobile (`< 900 px`) retains all-grow.** Narrow columns wrap awkwardly with content-width pickers, so flex:1 1 0 kicks back in.
- AccountMultiSelect carries its own internal width clamps; setting a `min-width: 10rem` on the field is enough to keep pills readable without stretching.

### Source files

| What | Where |
|---|---|
| FullscreenButton (self-hides when fullscreen) | [`frontend/src/lib/FullscreenButton.svelte`](frontend/src/lib/FullscreenButton.svelte) |
| DefaultSizeButton (self-shows when fullscreen, Windows restore icon) | [`frontend/src/lib/DefaultSizeButton.svelte`](frontend/src/lib/DefaultSizeButton.svelte) |
| CollapseButton (cyan-400, persists per user via localStorage) | [`frontend/src/lib/CollapseButton.svelte`](frontend/src/lib/CollapseButton.svelte) |
| RefreshButton (cyan-400, spin animation) | [`frontend/src/lib/RefreshButton.svelte`](frontend/src/lib/RefreshButton.svelte) |
| Global fullscreen pinning, bell lift, collapse hide | [`frontend/src/app.css`](frontend/src/app.css) (search `.fs-card-on`) |
| `.algo-title-group` global helper | [`frontend/src/routes/(algo)/+layout.svelte`](frontend/src/routes/(algo)/+layout.svelte) |
| Page-header action trio (Order + Chart + Log) | [`frontend/src/lib/PageHeaderActions.svelte`](frontend/src/lib/PageHeaderActions.svelte) |
| Combined activity log modal (Order Book + Agent Log) | [`frontend/src/lib/ActivityLogModal.svelte`](frontend/src/lib/ActivityLogModal.svelte) |
| Auto-fire agent toast (KEEP — separate from the log) | [`frontend/src/lib/AgentToast.svelte`](frontend/src/lib/AgentToast.svelte) |

When adding a new card, copy the trio + AccountMultiSelect placement from MarketPulse Positions/Holdings or dashboard Capital/Equity. Do NOT invent a new variant.

---

## Algo navbar — grouped + collapsible

Items in [`(algo)/+layout.svelte`](frontend/src/routes/(algo)/+layout.svelte) carry a `group:` attribute ∈ `{monitor, analyze, modes, build, config}`. Render strategy splits by group size and operator frequency:

- **Inline (always visible)** — `monitor` (Tour / Pulse / Dashboard / Agents / Orders), `analyze` (Derivatives), `modes` (Lab). High-frequency surfaces stay one click.
- **Disclosure dropdowns** — `build` (Console / Research / Tokens) and `config` (Brokers / Settings / Users / Health) collapse behind labelled triggers with carets. Trigger stays lit (`algo-nav-btn-active`) when the operator's current page is inside that group, so "I'm in Config" reads at a glance even without opening the panel.
- **Mobile drawer** (<1024px / `lg:hidden`) — hamburger opens a section-captioned drawer (MONITOR / ANALYZE / MODES / BUILD / CONFIG headers above each group's items). Operator navigates by intent, not by scrolling a flat list.

Industry analogue: Grafana left-rail collapsed/expanded pattern, ported to a top bar.

---

## Agent workspace tabs

The four surfaces operators visit when thinking about agents — rules, fires, tokens, lab — share a [`AgentWorkspaceTabs.svelte`](frontend/src/lib/AgentWorkspaceTabs.svelte) strip at the top of each page:

| Tab | Route |
|---|---|
| Agents | `/agents` |
| Activity | `/agents/activity` |
| Tokens | `/admin/tokens` |
| Lab | `/admin/research` |

URLs are unchanged — this is visual unification only. Operator gets a single mental model ("Agent Workspace") and one-click navigation between the four surfaces without leaving the workspace. Moving the URLs physically under `/agents` is a follow-up if wanted; the tab component is already structured so it's a small delta.

Industry analogue: Splunk Detections workspace with tabs.

---

## Execution modes (mode 1 / 2 / 3 / 4 / 5)

The codebase distinguishes five execution modes, each with its own quote source and trade engine. Four of them form a **confidence ladder** — an agent graduates through these before real money moves. **Replay** is a parallel research tool (historical backtests), not a step on the ladder.

```
ladder:   Simulator → Paper → Shadow → Live
parallel: Replay (historical OHLCV backtest, any time)

dev:    Simulator + Replay (every action forced to paper)
prod:   Simulator → Paper → Shadow → Live  (+ Replay on demand)
```

Conceptually: **Simulator** validates the agent's logic against fabricated stress moves on a fresh book; **Paper** runs the chase/fill engine end-to-end against live market data without touching the broker; **Shadow** is the final pre-Live sanity check — captures the exact `kite.place_order` payload + `basket_margin` verdict without simulating any lifecycle; **Live** flips the kill-switch and the same chase engine sends real orders. **Replay** sits orthogonal — it replays historical candles through the paper engine for backtesting and is opt-in regardless of ladder position.

| Mode | Quote source | Trade engine | Where it runs | Default |
|---|---|---|---|---|
| **1 — Simulator** | `SimQuoteSource` (fabricated, scenario-driven via [`SimDriver`](backend/api/algo/sim/driver.py)) | `PaperTradeEngine` fed by sim quotes — fills against fabricated bid/ask, removes positions on fill | Both dev and prod (gated by `cap_in_dev.simulator: True` / `cap_in_prod.simulator: True`) | Available on both branches; selecting SIM from the navbar goes to `/admin/execution?mode=sim` where the driver is started against a scenario + seed + agent set |
| **2 — Paper** | `LiveQuoteSource` (broker.quote / broker.ltp via the `Broker` adapter, with bid/ask from depth or `simulator.default_spread_pct`) | `PaperTradeEngine` singleton (`get_prod_paper_engine()`), 5-second background tick. Validates each new order via Kite's `basket_margin` before marking OPEN; REJECTED rows carry Kite's exact error in `.detail`. Real positions are NOT updated; cooldown handles re-fire | Both dev and prod (dev always forces this regardless of DB flags) | On dev, every action is forced paper. On prod, gated by `execution.paper_trading_mode=True` — the persisted value when the operator clicks PAPER from the navbar |
| **3 — Live** | `LiveQuoteSource` (read paths) | Real broker via [`chase.py`](backend/api/algo/chase.py) — actual Kite `place_order` / `modify_order` / `cancel_order` | Prod only, when `execution.paper_trading_mode = False` | Enabled by setting `execution.paper_trading_mode = False` via `/admin/execution` |
| **4 — Replay** | [`HistoricalQuoteSource`](backend/api/algo/quote/historical.py) (pre-loaded Kite OHLCV candles) | `PaperTradeEngine` fed by historical candles — informational orders only | Both dev and prod | `cap_in_dev.replay: True`, `cap_in_prod.replay: True` |
| **5 — Shadow** | `LiveQuoteSource` (same as paper/live) | [`ShadowTradeEngine`](backend/api/algo/shadow.py) — logs exact Kite `place_order` kwargs + validates via `basket_margin` without executing | Prod only | `execution.shadow_mode: False` (opt-in) |

**The branch is the hard outer gate.** On non-`main` branches, every broker-hitting action is forced to paper regardless of any DB flag. On `main`, the `execution.paper_trading_mode` and `execution.shadow_mode` settings decide the effective mode.

**Mode resolution precedence** in [`_resolve_mode()`](backend/api/algo/actions.py): `sim > replay > (prod-branch check) > shadow > paper_trading_mode > agent.trade_mode`. Non-prod branches force paper before even checking shadow.

**Architectural pieces**:

- [`backend/api/algo/quote/`](backend/api/algo/quote/) — `QuoteSource` ABC + `SimQuoteSource` + `LiveQuoteSource` + `HistoricalQuoteSource`. Bid/ask supplier per open order. `on_fill` hook lets the source update its book on fill (sim drops the symbol; live/replay are no-ops).
- [`backend/api/algo/paper.py`](backend/api/algo/paper.py) — `PaperTradeEngine` owns the open-order book and the chase / fill / modify / unfilled lifecycle. Constructor takes a `QuoteSource`, a `label` ("sim" / "paper" / "replay"), and an optional event callback. Used by SimDriver (mode 1), get_prod_paper_engine() (mode 2), and ReplayDriver (mode 4).
- [`backend/api/algo/shadow.py`](backend/api/algo/shadow.py) — `ShadowTradeEngine` singleton. Captures exact Kite payload + basket_margin validation. Writes `AlgoOrder(mode='shadow')` rows. `/admin/execution?mode=live` carries the "Promote to Live" button that sets both `execution.paper_trading_mode=false` and `execution.shadow_mode=false`.
- [`backend/api/algo/replay/driver.py`](backend/api/algo/replay/driver.py) — `ReplayDriver` singleton. Fetches historical candles at start, advances one candle per tick at playback rate, feeds `HistoricalQuoteSource`, runs `run_cycle()` at each tick.
- [`actions.py::_resolve_mode`](backend/api/algo/actions.py) — single source of truth for "should this action go to sim, replay, shadow, paper, or live?". Reads `context["sim_mode"]`, `context["replay_mode"]`, the branch, `execution.shadow_mode`, and the master `execution.paper_trading_mode` flag.
- [`agent_engine._agent_execution_mode_tag`](backend/api/algo/agent_engine.py) — inspects the master `paper_trading_mode` toggle and tags the alert as `[PAPER]` (when paper_trading_mode=True) or empty (when live). The tag flows through `alert_utils._dispatch` into Telegram subjects + email subject prefixes so an operator on Telegram can tell at a glance what execution mode an alert's actions used.

### Navbar-only mode (Wave C)

Mode is set **exclusively via navbar dropdown** — no per-ticket select, no mode banners, no page-level toggles. `executionMode` store reads navbar pill state; all pages + OrderTicket + SymbolPanel derive mode from the store.

**Navbar pill**: 1.4rem · 0.65rem weight-800. SIM/REPLAY green, PAPER sky-blue, LIVE red (changed from conflated color), SHADOW orange. Halo effect on hover.

**Mode dropdown** (SIM · PAPER · LIVE · SHADOW · REPLAY on prod; SIM · PAPER · REPLAY on dev). Clicking navigates to `/admin/execution?mode=<slug>`. Dropdown items 0.62rem. SIM/REPLAY picks navigate; PAPER/SHADOW commit settings; LIVE shows confirm-modal then commits `execution.paper_trading_mode = False`.

**ActivityLogPanel gating** — `gateByMode` prop (default true) filters Order/Agent/Terminal tabs by current mode. Hides Ticks tab outside sim. Header `[MODE: PAPER]` chip on every tab.

**No backward-compat** — old per-mode pages (`/admin/options`, `/admin/paper`, `/admin/live`, `/admin/shadow`, `/admin/replay`) and `/watchlist` redirect were removed. Canonical entry point is the navbar pill.

**Each mode has its own page under `/admin/execution`:**

| Mode | Page | Scope |
|---|---|---|
| Simulator | `/admin/execution?mode=sim` | Scenario picker · custom positions · live-book seed |
| Paper | `/admin/execution?mode=paper` | Paper engine chase monitor · charts · activity log |
| Live | `/admin/execution?mode=live` | Confirm-modal gateway + settings |
| Shadow | `/admin/execution?mode=shadow` | Logging + validation surface |
| Replay | `/admin/execution?mode=replay` | Historical backtest driver |

**Execution settings** (`GET /api/admin/execution/mode`):
- `mode: str` — current effective mode (computed from sim/replay driver status + settings flags)
- `allowed_modes: list[str]` — branch-filtered valid targets
- `branch: str` — raw deploy_branch value (`main` for prod, other names for dev)

**Master settings**:

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `execution.paper_trading_mode` | bool | seeded `False` on first boot (= LIVE) | `seed_settings()` upserts `false` IF the row is missing — fresh installs land in LIVE. Existing rows are NEVER overwritten (operator's last navbar pick persists). The in-code `get_bool()` fallback (5 callsites) stays at `True` (PAPER) so a row deleted mid-run fails safe. On dev, the value is ignored — branch always forces paper. On prod: `True` → paper mode, `False` → live mode. |
| `execution.shadow_mode` | bool | `False` | Opt-in. When `True` on prod, actions log Kite payload + validate via `basket_margin` without executing. |

Toggles sync via `/api/admin/execution/mode` (POST with `{mode: string}`). The page banner at `/admin/execution` shows green ("PAPER mode") when `paper_trading_mode=True`, red ("LIVE mode") when `False`. On non-`main` branches, both settings are ignored and every action is forced to paper.

**Order-log mode pills**: every `AlgoOrder` row carries `mode ∈ {sim, paper, live, shadow}`. The LogPanel Order tab shows distinct pills:
- `SIM` — amber, fabricated data
- `PAPER` — sky-blue, real data + paper trade
- `LIVE` — emerald, real broker order
- `SHADOW` — orange, logged payload + validation, no execution

`/api/orders/algo/recent?mode=paper` filters the API to just paper rows; the UI surfaces it via the mode column on each row.

**What this means for the operator on prod**: every broker-hitting agent fire or manual order routes through the mode resolution logic. On a fresh install the seeder writes `execution.paper_trading_mode=False` (LIVE) so the navbar lands on LIVE out of the box — operator flips to PAPER from the navbar if they want every action to land as a paper `AlgoOrder` row with Kite's `basket_margin` verdict in `.detail`. REJECTED paper rows tell you "Kite would have kicked this back anyway"; OPEN rows transition to FILLED / UNFILLED via the chase loop. If the DB row is ever deleted mid-run the in-code fallback (`True`, PAPER) takes over — no broker calls until the row reappears.

**Navbar-only mode (Wave C)**: per-ticket `Mode` select removed. SymbolPanel + OrderTicket read `$executionMode` store. `availableModes` / `defaultMode` props deprecated. Mode banners (SIM/PAPER/LIVE/SHADOW/REPLAY) deleted — navbar pill is sole indicator. ActivityLogPanel gates tabs by mode; Log entry visibility follows a `gateByMode` prop. SIM/REPLAY mode picks navigate to `/admin/execution`; PAPER/SHADOW commit settings; LIVE shows confirm-modal then commits.

---

## Multi-account basket + auto profit target

**Basket orders** — [`POST /api/orders/basket`](backend/api/routes/orders.py) groups legs by account, dispatches one `kite.place_order` per account in parallel via `asyncio.gather`. Shared `basket_tag=ramboq-basket-<uuid>` per group. [`POST /api/orders/basket/margin`](backend/api/routes/orders.py) calls `kite.basket_order_margins` per account for offset-aware display.

**Target profit attachment** — `AlgoOrder` schema gained `target_pct`, `target_abs`, `parent_order_id`, `basket_tag`. Postback handler + chase loop terminal hook: when a parent order fills, auto-attach a TP order on the flip side (`BUY` parent → `SELL` TP at fill × (1 + target_pct)). Idempotent via `parent_order_id IS NULL` guard. Seeded default: `algo.default_target_pct` (0.30).

**SymbolPanel** renders per-account margin strip above basket pills when basket spans 2+ accounts. OrderTicket Target row (% / ₹ toggle) seeds from default_target_pct setting.

---

## Symbol identity — root vs contract pattern

| Surface | Shows | Rationale |
|---|---|---|
| **Watchlist / Recent symbols** | Contract (tradable instance) | Operator places order on this; ambiguous root (GOLDM) at bare symbol |
| **`/admin/derivatives` picker** | Root + inline chip (e.g. `GOLDM` label · `GOLDM26JUNFUT` chip) | Identity + prompt for rolling soon |
| **`/charts` workspace** | Bare MCX root → displays "Front-month: GOLDM26JUNFUT · expiry 19 Jun" | Spot anchor chip rolls on expiry |
| **Orders / Positions / Watchlist tables** | Contract | Real tradable unit |

**MCX commodity spot resolution** — `_resolve_spot` matches option_symbol's month token (Phase 4b) → expiry_hint date walk → front-month future. Helper `lookup_future_for_option(option_symbol)` matches by month TOKEN, not expiry date (futures + options diverge even in same month).

`OptionAnalyticsResponse.spot_anchor_contract` surfaces the resolved front-month contract. `OptionsPayoff` displays amber "rolling soon" chip when ≤3 days to expiry.

**Blocking bare roots** — `_BARE_UNDERLYINGS` in `accounts.js` prevents bare commodity/index roots from being saved as recent symbols. Watchlist + Recent picker reads only tradable instances.

---

## Agent Framework

Ramboq's risk + automation engine is built around four words:

| Word | Meaning |
|---|---|
| **Agent** | A rule row (DB: `agents`) with `condition + notify + actions + metadata`. Seeded from `BUILTIN_AGENTS` in `agent_engine.py`: 12 alerting + 2 fund-negative agents ship **active** by default; 1 auto-close agent ships **inactive**. Extensible via the `/agents` UI. |
| **Alert** | The runtime event an agent emits when its condition fires. Persisted to `agent_events` with a `sim_mode` flag so real fires can be separated from simulated ones. |
| **Notify** | A delivery channel (`telegram / email / websocket / log`). |
| **Action** | A side-effect the alert invokes (order placement, monitoring, modify, cancel, close, flag-set, …). Handlers in `actions.py`; real broker wiring lands per-action as each is promoted out of stub mode. |

### Templates vs Agents — non-overlapping responsibilities

**Agents** are market-event driven: "when the portfolio's P&L drops below ₹50k, close all positions". They live on `/agents`, fire every 5 minutes per market cycle, and carry conditions, notifications, and actions.

**Templates** are order-entry driven: "when I place a SELL order, auto-attach a TP exit at +0.5% and an SL at −1% with a 2% trailing stop". They live on `/admin/templates`, attach per-ticket as the operator enters an order, and carry exit-rule specifications (TP/SL/wings/scales/trails).

**Why both?** Agents own portfolio-level risk rules that ignore where positions came from. Templates own trade-specific exit mechanics that apply to orders from _any_ source — agents, manual entry, templates themselves. They don't overlap because agents manage the _book_, templates manage _individual positions_.

### Order Templates

Per-position exit rules (TP/SL/wing/scale/trail) attach via GTT at fill. Operator picks template from OrderTicket Default/None toggle. 

**Data model**: `order_templates` table ([backend/api/models.py](backend/api/models.py)) — `tp_type` (LIMIT|MARKET), `sl_type` (LIMIT), `sl_trail_pct`, `tp_scales_json` (NinjaTrader style), `wing_strike_offset`, `wing_premium_pct`. Seeded: `default-long-option` (TP +80% MARKET), `default-short-vol` (TP +10% LIMIT, SL -20%, wing -1 strike). One `is_default` per user.

**Execution**: Mode validation → eligibility check (LIVE+PAPER only) → wing pre-scan + underlying spot resolve → parent place → on fill: `apply_template_to_order()` populates `attached_gtts_json` (idempotent), places GTTs per exit rule.

**Exit mechanics**: TP/SL auto-side-flip (short parent → SELL TP lower). Trail: `_task_trail_stop` (every 30s) polls LTP, ratchets trigger via `broker.modify_gtt()`. Wing: opposite type same strike ATM ±N (1:1 qty). Scale: multi-trigger GTT ladder per `tp_scales_json`.

**Brokers**: Kite=full (native OCO). Dhan=Forever Order (MCX blocked). Groww=emulated OCO+pair-watcher (15s race window). Capability surface: `GET /api/admin/brokers/{account}/capabilities` + OrderTicket warning chip. Postback + chase both fire attach (idempotent via `attached_gtts_json` check).

**File map**: [models](backend/api/models.py) | [routes](backend/api/routes/orders.py) | [templates.py](backend/api/algo/templates.py) | [OrderTicket.svelte](frontend/src/lib/order/OrderTicket.svelte).

### End-to-end flow on a real tick

```
_task_performance (background.py, every 5 min during market hours)
  └─ fetch_holdings / fetch_positions / fetch_margins  ← live Kite data
     └─ summarise_holdings / summarise_positions       ← per-account + TOTAL
        └─ run_cycle(ctx)                              ← agent_engine.py
           └─ for each active agent:
              1. schedule gate  — skip market_hours agents outside session
              2. cooldown gate  — skip if last fire was within cooldown_minutes
              3. baseline gate  — skip rate-metric agents for first 15 min
              4. evaluate()     — walk condition tree (agent_evaluator.py)
              5. suppress gate  — refire only if |Δpnl| or |Δpct| is material
              6. if matches: dispatch (telegram/email/ws/log) + execute(actions)
              7. write agent_events row, update last_triggered_at / status
```

Tokens referenced in a condition (metric `pnl`, scope `positions.any_acct`,
operator `<=`, value `-30000`) are resolved lazily via `GrammarRegistry`, so
adding a new metric is one DB row plus one resolver function — no engine
change.

### Tokens (condition / notify / action) — extensible via DB

The **Tokens page** (`/admin/tokens`) is the UI over the `grammar_tokens`
table. The engine ships with a full set of system tokens, seeded on every
boot from `backend/api/algo/grammar.py`; operators can toggle those on/off
and add custom tokens via the page. Each row:

| Column | Purpose |
|---|---|
| `grammar_kind` | `condition` / `notify` / `action` |
| `token_kind` | `metric` / `scope` / `operator` (condition), `channel` / `format` / `template` (notify), `action_type` (action) |
| `token` | The identifier authors write into an agent (e.g. `pnl`, `positions.any_acct`, `<=`, `telegram`, `place_order`). |
| `value_type`, `units` | Typing so the admin UI can render + validate. |
| `resolver` | Python dotted path to the function that implements the token (metric resolver, scope selector, action handler). |
| `params_schema` | JSON schema for `action_type` params (account, symbol, side, qty, …). |
| `enum_values`, `template_body` | For enum value types and notify templates. |
| `is_system`, `is_active` | System tokens ship with code and are seeded on every boot; operators can toggle but not delete. Custom tokens have full CRUD. |

`GrammarRegistry` (in `backend/api/algo/grammar_registry.py`) is a singleton that loads the catalog into an in-memory dispatch table at startup and on `/api/admin/grammar/reload`. Adding a new capability is one DB row plus one Python function — no engine or schema change.

### Condition tree schema (v2 grammar)

```
condition  ::=  leaf
             |  { "all": [condition, ...] }      AND
             |  { "any": [condition, ...] }      OR
             |  { "not": condition }             NOT

leaf       ::=  { "metric": <metric-token>,
                  "scope":  <scope-token>,
                  "op":     <op-token>,
                  "value":  <literal> }
```

`backend/api/algo/agent_evaluator.py`:
- `evaluate(cond, ctx) -> list[match]` — tree walker; empty list means no fire.
- `validate(cond) -> list[str]` — dry-check; every referenced token must exist in the registry. Used by `POST /api/agents/validate-condition` and surfaced in the `/agents` editor's Validate button.
- `Context` — bundles `sum_holdings`, `sum_positions`, `df_margins`, `position_rows` (per-symbol position dicts for expiry scopes), `spot_prices` (`{underlying: ltp}` for ITM/NTM resolvers), `alert_state` (for rate history), `now`, `segments`, `rate_window_min`, `agent`. `position_rows` and `spot_prices` are populated by [`background.py::_task_performance`](backend/api/background.py) and the simulator driver; absent ⇒ expiry-aware metric leaves return None gracefully.

The v1 `field/operator/rules` evaluator has been retired; every agent must use the grammar tree above. `run_cycle` calls `agent_evaluator.evaluate` directly.

### Extended metric families

Beyond the original point-in-time metrics (`pnl`, `pnl_pct`, `day_pct`, etc.) and rate metrics (`pnl_rate_abs`, `pnl_rate_pct`, …), the catalog ships two extension families:

**Rolling-window aggregates** (Phase 24) — read the same `pnl_history` deque the rate metrics use; aggregate the whole slice with statistical reducers. Return `None` until the window holds ≥ 2 samples so first-tick fires never happen.

| Token | Units | Reducer |
|---|---|---|
| `mean_pnl_30m` / `_1h` | ₹ | average over window |
| `mean_day_30m` / `_1h` | ₹ | average holdings day-change over window |
| `max_drawdown_pnl_30m` / `_1h` / `_4h` | ₹ | peak-to-trough drop (always ≤ 0) |
| `max_drawdown_pnl_pct_30m` / `_1h` | % | same as ratio of P&L |
| `max_drawdown_day_1h` | ₹ | drawdown on holdings day-change |
| `stdev_pnl_30m` / `_1h` | ₹ | volatility proxy |
| `range_pnl_30m` / `_1h` | ₹ | max − min over window |

**Expiry-aware metrics + scope** (Phase 25) — parse the tradingsymbol per-call (regex + dict, no I/O) and consult `ctx.spot_prices` for moneyness.

| Token | Kind | Notes |
|---|---|---|
| `days_until_expiry` | metric | Float days; handles NSE 15:30 + MCX 23:30 close-time boundaries. None for cash equity. |
| `is_itm` | metric | 1.0 / 0.0 / None. None when spot is unknown — leaf skips. |
| `is_ntm` | metric | 1.0 when within ±1.5% of spot. Same None semantics. |
| `positions.expiring_today` | scope | Per-symbol rows where the F&O contract expires within 1.5 days. Reads `ctx.position_rows` (NOT the aggregate `sum_positions`). |
| `positions.expiring_today.nfo` | scope | Subset of `expiring_today` restricted to NFO (equity F&O). Used by the equity auto-close agent. |
| `positions.expiring_today.mcx_unhedged` | scope | Subset of `expiring_today` restricted to MCX rows whose CE/PE net qty per `(underlying, expiry)` is non-zero. Hedged pairs (net 0) are skipped — broker nets them at settlement, no operator action needed. |

Three **inactive** seeded agents use them. `expiry-day-positions-alert` is notify-only. The auto-close path is split into two segment-specific destructive agents:

| Agent slug | Fires at | Condition leaf | Action |
|---|---|---|---|
| `expiry-day-equity-itm-auto-close` | 15:00 IST (T-30min) | `positions.expiring_today.nfo` filtered by `is_itm == 1` | `expiry_auto_close` with `exchange: NFO` |
| `expiry-day-commodity-itm-auto-close` | 23:00 IST (T-30min) | `positions.expiring_today.mcx_unhedged` filtered by `is_itm == 1` | `expiry_auto_close` with `exchange: MCX` |

The `expiry_auto_close` action wraps the legacy [`ExpiryEngine`](backend/api/algo/expiry.py) so the agents inherit its battle-tested rules: NFO closes ALL ITM + NTM; MCX closes only UNHEDGED ITM + NTM (CE/PE pairs that net to zero are skipped at settlement). Both agents ship INACTIVE; activate from `/agents` to graduate off the bg task after a side-by-side validation pass.

The default `algo.expiry_start_offset_hours` setting is now `0.5h` (T-30min, matching Sensibull / Streak conventions). The seeder PRESERVES the operator's live override, so prod boxes upgraded from the old 2h default still see `value=2` — flip to 0.5h via `/admin/settings`.

### Action grammar

Action tokens (seeded): `place_order`, `modify_order`, `cancel_order`, `cancel_all_orders`, `chase_close_positions`, `expiry_auto_close`, `monitor_order`, `deactivate_agent`, `set_flag`, `emit_log`. Every token carries a typed `params_schema` with `required` / `enum` / `default` / `token_ref_ok` fields so the admin UI and the runtime agree on the shape. Handlers live in `backend/api/algo/actions.py` — currently stubs that log the invocation; real broker wiring lands as each action type is promoted out of stub mode.

### Admin endpoints

| Route | Purpose |
|---|---|
| `GET /api/admin/grammar/tokens[?grammar=<kind>]` | List catalog, optional filter |
| `GET /api/admin/grammar/tokens/{id}` | Read one |
| `POST /api/admin/grammar/tokens` | Create custom token |
| `PATCH /api/admin/grammar/tokens/{id}` | Update. System tokens restrict to `is_active` toggle. |
| `DELETE /api/admin/grammar/tokens/{id}` | Custom only; system returns 400. |
| `POST /api/admin/grammar/reload` | Hot-rebuild the registry after edits. |
| `POST /api/agents/validate-condition` | Dry-check a v2 condition tree against the live catalog. |

All gated by `admin_guard`. Every mutating endpoint calls `REGISTRY.reload()` automatically.

### Deploy automation
`deploy.sh` handles: git pull → pip install → npm build → restart API service → notify. Host-wide serialisation via single `/tmp/ramboq_deploy.lock` (was per-env) prevents concurrent prod + dev builds from race-condition npm conflicts. Prod + dev `npm run build` runs at `nice -n 19 ionice -c 3` so background builds never starve the API server's ability to respond to live requests.

### Logging
- API uses `RAMBOQ_LOG_PREFIX=api_` env var for log file naming
- 3 handlers: rotating log file (5MB × 5), rotating error file, console

### Reusable fragments — saved sub-trees referenced via `$ref`

The `agent_fragments` table holds reusable sub-trees an agent can reference inline via `{"$ref": "<name>"}`. Two kinds today; `action` is reserved for a future stage.

| Kind | Lives in | Resolved by |
|---|---|---|
| `notify` | `Agent.events` list | [`resolve_events()`](backend/api/algo/fragment_registry.py) — called from `events.dispatch()` before channel fan-out. Body is a list of `{channel, enabled}` dicts. |
| `condition` | `Agent.conditions` tree | [`evaluate()`](backend/api/algo/agent_evaluator.py) — `$ref` is a recognised node alongside `all` / `any` / `not` / leaves. Body is a condition sub-tree (leaf or composite). |

**FragmentRegistry** (`backend/api/algo/fragment_registry.py`) — singleton in-memory cache `{kind: {name: body}}`. Loaded at boot from `agent_fragments` and hot-rebuilt on every CRUD mutation (matches the `GrammarRegistry` pattern). The evaluator + dispatcher always go through the cache, never the DB on the hot path.

**Cycle detection** — `evaluate()` carries an internal `_visited: set` through recursive calls. A `$ref` to a name already in the visited set logs a warning and returns `[]` instead of recursing. `A → B → A` chains can't blow the stack.

**Missing-ref behaviour** — both `resolve_events` and `evaluate` treat unknown ref names as a warning + skip:
- Notify: other channels in the list still fire; the broken ref is logged.
- Condition: the ref node returns `[]` matches (acts as a false leaf); other branches of the tree evaluate normally.

This matches the rest of the grammar pipeline — operator typos in `/agents/fragments` don't crash the engine.

**Seeded system fragments** (force-reseeded on every boot from `SYSTEM_FRAGMENTS`):
- `notify-critical-trio` — telegram + email + log (the default for every loss / expiry agent)
- `notify-log-only` — quiet diagnostic channel
- `notify-telegram-only` — phone-only ping
- `loss-positions-acct-default` — per-account 4-threshold any-block
- `loss-positions-total-default` — book-wide 4-threshold any-block
- `near-market-close-30m` — `minutes_until_close ≤ 30` guard

Operators can't delete system fragments (only toggle `is_active`); custom fragments have full CRUD.

**UI**: `/agents/fragments` — filter chip (all / notify / condition), grouped list, expand-to-view-body, edit form for custom fragments. Lives as the fourth tab in `AgentWorkspaceTabs` (Agents · Activity · Tokens · Fragments · Lab).

**API**:

| Route | Purpose |
|---|---|
| `GET /api/admin/fragments[?kind=notify\|condition]` | List |
| `GET /api/admin/fragments/{id}` | Read one |
| `POST /api/admin/fragments` | Create custom |
| `PATCH /api/admin/fragments/{id}` | Update — system rows toggle-only |
| `DELETE /api/admin/fragments/{id}` | Custom only |
| `POST /api/admin/fragments/reload` | Hot-rebuild cache |

`validate-condition` also resolves `$ref` against the registry so the `/agents` editor's Validate button drills into the fragment body and surfaces a missing-token error deep inside a referenced sub-tree.

**Composing with fragments** — a fully-fragment-composed agent looks like:

```json
{
  "conditions": {"all": [
    {"$ref": "loss-positions-total-default"},
    {"$ref": "near-market-close-30m"}
  ]},
  "events": [{"$ref": "notify-critical-trio"}],
  "actions": [{"type": "chase_close_positions", "params": {"scope": "total"}}]
}
```

Edit `loss-positions-total-default` once → every consumer (this agent + any future ones referencing it) updates. Industry analogue: Grafana Contact Points + Notification Policies, Datadog Monitor Templates. Started with notify-only (Stage 1), added conditions (Stage 2), shipped UI (Stage 3) — same staged path Grafana took.

---

## Settings — DB-backed tunables

`/admin/settings` exposes every parameter that changes more often than a deploy cycle. Values live in the `settings` table (`category / key / value_type / value / default_value / description / schema / units`). The reader chain is **DB cache → YAML fallback → in-code default**, so migrating a knob from YAML to DB is a one-line code change and zero-downtime.

Key files:

| Path | Purpose |
|---|---|
| `backend/api/models.py::Setting` | SQLAlchemy row |
| `backend/shared/helpers/settings.py` | `SEEDS` list + `get_int/get_float/get_bool/get_string` readers + `seed_settings()` seeder |
| `backend/api/routes/settings.py` | `/api/admin/settings/*` CRUD |
| `frontend/src/routes/(algo)/admin/settings/+page.svelte` | Grouped page (Alerts · Performance · Simulator · Notifications · Logging) |

Seeded buckets + sample keys:

- **alerts** — `alerts.cooldown_minutes`, `alerts.rate_window_min`, `alerts.baseline_offset_min`, `alerts.suppress_delta_abs`, `alerts.suppress_delta_pct`
- **performance** — `performance.refresh_interval`, `performance.open_summary_offset_min`, `performance.close_summary_offset_min`
- **simulator** — `simulator.positions_every_n_ticks`, `simulator.auto_stop_minutes`, `simulator.default_rate_ms`
- **notifications** — `notifications.telegram_enabled`, `notifications.email_enabled`, `notifications.notify_on_deploy`
- **logging** — `logging.file_log_level`, `logging.console_log_level`, `logging.error_log_level`

Seeder behaviour across deploys:
- Insert missing rows (from `SEEDS`) with the shipped `default_value`.
- Refresh `category / description / schema / units / default_value / value_type` on existing rows (code changes land through).
- **Preserve `value`** (the operator's live override).
- **Auto-prune** rows whose keys are no longer in `SEEDS` — retiring a knob requires no manual DB cleanup.

Per-PATCH, the in-process cache invalidates and reloads, so edits take effect on the next agent tick / sim run without a service restart.

---

## Simulator

The simulator feeds fabricated per-symbol **positions** + margins into
the **same** agent engine the real pipeline uses, so alerts, actions, and
event-logging are all exercised end-to-end without touching the broker.
**No code branches in the hot path** — the engine only sees a `sim_mode`
flag on the context dict and tags downstream artefacts: `[SIM]` for log
lines and detail strings, `SIMULATOR` for user-facing Telegram / email
surfaces.

**Positions-only by design.** Holdings aren't simulated — intraday risk
lives in F&O positions + fund-negatives, and that's the scope. Agents
that check holdings metrics (`day_pct`, `day_rate_abs`, `day_rate_pct`)
validate against live production data only. Running **Run in Simulator**
on such an agent returns a clear 400 explaining this.

### Architecture — Model B (per-symbol price driver)

```
scenario.yaml (moves)      ┌────────────────────────┐
        │                  │    Real background     │
        ▼                  │    _task_performance   │    
   SimDriver       ←─?──→  │   (stays live, only    │ ← Kite API
  (per-symbol state)       │    run_cycle gated)    │
        │ every rate_ms    └──────────┬─────────────┘
        ▼                             │
   _apply_moves (glob)                ▼
    last_price ← move               run_cycle(ctx)
    recompute pnl                   (shared — no sim branch)
        │                             │
        ▼                             ▼
   summarise_positions          dispatch · actions
   (sum_holdings empty)         → Telegram/email/ws/log
        │                       → agent_events (sim_mode=True)
        ▼                       → algo_orders (mode='sim')
   run_cycle(ctx,
     sim_mode=True,
     only_agent_ids=...,
     bypass_schedule=True)
```

Key files:

| Path | Purpose |
|---|---|
| `backend/api/algo/sim/driver.py` | `SimDriver` singleton — per-symbol state, tick loop, move primitives, glob scope matching, live-book seeding |
| `backend/api/algo/sim/scenarios.yaml` | Scenario catalog (slug, name, mode, optional `initial` per-symbol rows, `ticks` with move primitives) |
| `backend/api/routes/simulator.py` | `/api/simulator/*` endpoints (admin-guarded) |
| `frontend/src/routes/(algo)/admin/simulator/+page.svelte` | Control plane UI |

### State shape

`_positions_rows` is a list of per-symbol dicts matching what
`broker_apis.fetch_positions` returns (`tradingsymbol`, `quantity`,
`average_price`, `last_price`, `close_price`, …). When a move changes
`last_price`, `_recompute_position_row` derives `pnl`. `dataframes()`
calls the same `summarise_positions` helper the live background task uses,
producing per-account + TOTAL aggregates in the exact shape the evaluator
expects. `sum_holdings` is always an empty frame — holdings aren't
simulated.

### Move primitives (in scenario `ticks`)

```yaml
- at: 0
  moves:
    - {type: pct,         scope: "positions.**",         value: -0.03}   # -3% LTP
    - {type: abs,         scope: "positions.ZG*.NIFTY*", value: -25}     # ₹-25/share
    - {type: random_walk, scope: "positions.**", drift: -0.001, vol: 0.005}
    - {type: target_pnl,  scope: "positions.ZG*.*",      value: -50000}  # solve ΔLTP
    - {type: set_margin,  scope: "margins.ZG####",
        fields: {avail opening_balance: -1500, net: -2500}}
```

- **`pct`** / **`abs`** — LTP × (1+v) or LTP + v.
- **`random_walk`** — `LTP ← LTP × (1 + drift + vol·N(0,1))`; seedable via scenario-level `seed:`.
- **`target_pnl`** — solves `ΔLTP × Σqty = target − currentPnl` uniformly; refuses mixed long/short.
- **`set_margin`** — price-decoupled; real Kite margin math (SPAN/ELM/product type) is never simulated.

**Scope glob** is `section.account.tradingsymbol` with `*` (single-segment) and `**` (any remaining path). Examples: `positions.**`, `positions.ZG*.*`, `positions.*.NIFTY*`, `positions.ZG####.NIFTY25APRFUT`. `holdings.*` globs are silently ignored (positions-only sim).

### Shipped scenarios

`generic-crash` (−3% over 3 ticks), `generic-euphoria` (+3%), `extreme-crash` (−19% over 3 ticks), `extreme-euphoria` (+19%), `random-walk` (seeded GBM). All work against any seeded book. The synthesizer covers per-agent tests; scenarios.yaml holds only these 5 book-wide stress tests.

### Run-in-Simulator + the synthesizer

Per-agent tests don't touch `scenarios.yaml` — they're built on demand from the agent's own condition tree by [`backend/api/algo/sim/synthesize.py`](backend/api/algo/sim/synthesize.py). The button on each `/agents` row hits `POST /api/simulator/start-for-agent/<id>`; the handler calls `synthesize_for_agent(agent)` which:

1. Walks the agent's condition tree, picks the "nearest-to-fire" leaf (`all` → tightest threshold; `any` → loosest; `not` logs a warning and targets the inner leaf).
2. Maps the leaf's metric to a canned ticks-shape:
   | metric | technique |
   |---|---|
   | `pnl` | `target_pnl` on positions scoped to match |
   | `pnl_pct` | `target_pnl` sized to cross `value% × util_margin` |
   | `pnl_rate_abs` | scheduled `target_pnl` decay over the rate window |
   | `pnl_rate_pct` | same, scaled to `value% × util_margin` |
   | `cash` | `set_margin` driving `avail opening_balance < 0` |
   | `avail_margin` | `set_margin` driving `net < 0` |
3. Holdings metrics (`day_pct`, `day_rate_abs`, `day_rate_pct`) are NOT synthesizable — the handler returns 400 with a message pointing operators at live data validation instead.
4. Returns an inline scenario dict (same shape as yaml entries). `SimDriver.start(inline_scenario=…)` accepts it without touching the yaml catalog.

Result: adding a new agent adds its own test for free. Adding a new metric is one grammar token + one synthesizer entry.

### Market-state presets

Agents that reference time (rate metrics with baseline gates, `minutes_until_close`, expiry rules) need a simulated clock — wall-clock time at 3 AM IST has every segment closed. Each scenario + each Start request can declare a `market_state` block:

```yaml
market_state: {preset: pre_close}
# — or —
market_state: {preset: expiry_day, is_expiry_day: true}
# — or explicit overrides —
market_state:
  nse_open: true
  mcx_open: false
  minutes_since_nse_open: 360
```

Seven presets shipped (see `MARKET_STATE_PRESETS` in `sim/driver.py`): `pre_open` / `at_open` / `mid_session` (default) / `pre_close` / `at_close` / `post_close` / `expiry_day`. `run_cycle` calls `_build_context(now, sim_overrides=…)` which merges overrides on top of the computed live values. Real path passes `None` and behaviour is unchanged.

### Paper-trade action expansion

When an agent fires in sim mode, `actions.execute()` routes to `_sim_paper_trade`. The writer now branches:

- **`close_position` / `place_order`** — params already specify `account + symbol`. Write ONE `AlgoOrder` with `initial_price = sim bid/ask (side-aware) for that symbol`.
- **`chase_close_positions` / `chase_close`** — scope-level actions. Look up every open position matching `scope ∈ {total, account}` in `SimDriver._positions_rows`, write ONE `AlgoOrder` per position, each with the real `account / symbol / qty` and side-appropriate price (SELL→bid, BUY→ask).
- **Non-order actions** (`emit_log`, `set_flag`, `monitor_order`, `deactivate_agent`, `cancel_*`, `send_summary`) don't get a paper row — just the `action_success` agent event that `execute()` already writes.

Each paper row carries the same print-style `detail` string in all three surfaces (logger, `AlgoOrder.detail`, tick_log `note`):

```
[SIM] loss-pos-total-auto-close → close_position: SELL 50 NIFTY25APRPE22000 @₹180.00 · acct=ZG####
```

### Chase engine (spread-aware)

Every position derives `bid` / `ask` from the simulator's `spread_pct` (default 0.10 %, tunable per Start). When an agent fires, the paper-trade writer persists the `AlgoOrder` with `status='OPEN'` and registers it with the driver's chase engine. Each subsequent tick, `_chase_open_orders`:

- **Fills** the order when the bid/ask crosses the limit (SELL fills if `bid ≥ limit`; BUY fills if `ask ≤ limit`). The position is removed from `_positions_rows`, the `AlgoOrder` row flips to `FILLED` with `fill_price` + `slippage` + `filled_at`, and the detail string is rewritten as `FILLED @₹X after N chase(s)`.
- **Modifies** the limit otherwise — re-quotes at the current opposite side, bumps `attempts` on the DB row, rewrites `detail` to `chase #N limit=₹X`. Capped at `simulator.chase_max_attempts` (default 5); after the cap the row flips to `UNFILLED`.
- **Auto-completes** the sim when `_positions_rows` is empty and no orders remain `OPEN`. A terminal `completed` entry lands in the tick log.

The status snapshot carries `positions` (per-row `{account, symbol, quantity, last_price, bid, ask, pnl}`) and `open_order_details` (per in-flight chase — `{symbol, side, qty, limit_price, attempts, status}`). The simulator page renders both as compact pill strips so operators watch the book shrink and the chase re-quote live.

### Order lifecycle status

`AlgoOrder.status` progression in sim mode: `OPEN` → (per tick) `OPEN` with `attempts++` → `FILLED` (or `UNFILLED` after cap). `AlgoOrderInfo` now exposes `attempts` + `fill_price`; the LogPanel Order tab colours each row by terminal state (FILLED green / UNFILLED red / OPEN amber) and adds a `chase: #N` chip.

### Seeding modes

| `seed_mode` | Initial state |
|---|---|
| `scripted` | Scenario's `initial.positions / margins` blocks (fails loudly if empty) |
| `live` | Fresh broker fetch via `SimDriver.seed_live()` — positions + margins snapshotted (holdings skipped) |
| `live+scenario` | Live book first, scripted `initial` rows layered on top |

Scenarios with no `initial:` (all 5 shipped ones, plus synthesized scenarios from Run-in-Simulator on holdings-agnostic agents) require `seed_mode=live` or `live+scenario`; attempting `scripted` start raises a clear error.

### Gates disabled during a sim

For a sim run, `run_cycle` is called with `bypass_schedule=True` **and** optionally `only_agent_ids=[...]`. That skips:

- the schedule gate (`market_hours` agents run even at 3 AM IST),
- the cooldown gate (repeated "Run in Simulator" clicks always fire),
- the baseline gate (rate agents fire immediately, not after 15 min),
- the suppression gate,
- and — critically — does **not** mutate the agent row (no cooldown / trigger count leak into real-market state).

The simulator owns its own `_sim_alert_state` dict, so rate history and suppression state never cross with the real pipeline.

### Interaction with the real background task

`_task_performance` keeps running while a sim is active: it fetches live Kite data, refreshes the performance cache, and sends open/close summaries. Only the live `run_cycle` call is skipped (`sim_active` short-circuit at `background.py` ~line 319) — that way `/performance` stays fresh with real data and the only thing that stops firing is the live agent engine.

### `sim_mode` = `True` effects

| Surface | Tag |
|---|---|
| Telegram message | `SIMULATOR` prefix + red "SIMULATOR RUN — fabricated market data" line |
| Email subject | `RamboQuant SIMULATOR Agent: …` |
| Email body | Red banner `🚨 SIMULATOR RUN — fabricated market data, not a real alert` |
| `agent_events.sim_mode` | `TRUE` |
| `algo_orders.mode` | `'sim'` (and `engine='sim'`) |
| Log prefix | `[SIM]` (short form — Telegram/email surfaces keep the longer `SIMULATOR` form) |
| WebSocket `agent_alert` payload | `sim_mode: true` |

### Simulator API — `/api/simulator/*`

| Route | Purpose |
|---|---|
| `GET /scenarios` | List available scenarios (slug / name / mode / has_initial / tick count) |
| `GET /status` | Driver snapshot (active, scenario, seed_mode, tick_index, counts, only_agent_ids, `positions`, `open_order_details`, `spread_pct`, `enabled`, `branch`) |
| `POST /start` | Body: `{scenario, rate_ms, seed_mode, agent_ids?, positions_every_n_ticks?, market_state_preset?, pct_overrides?, symbols?, spread_pct?}` |
| `POST /start-for-agent/{id}` | Build a scenario from one agent's condition tree and start (no `scenarios.yaml` entry required) |
| `POST /stop` | Halt |
| `POST /step` | Apply one tick (deterministic debug) |
| `POST /seed-live` | Snapshot live positions + margins into `_live_snapshot` (holdings skipped — positions-only sim) |
| `POST /run-cycle` | Immediately run the agent engine against current sim state |
| `POST /clear` | Delete every sim-mode row from `agent_events` + `algo_orders` |
| `GET /events/recent?limit=N` | Recent `sim_mode=True` agent events |
| `GET /orders/recent?limit=N` | Recent `mode='sim'` algo orders |
| `GET /ticks/recent?limit=N` | Rolling driver tick log (oldest-first) with per-symbol diffs |

Gated by `admin_guard` + the per-branch `cap_in_<branch>.simulator` flag in `backend_config.yaml` (dev default: on, prod default: on after the LIVE-default rollout). `GET /status` returns `enabled: false` when the cap is off for the active branch — the Simulator panel on `/admin/execution` reads this and disables every form button with a banner explaining the gate.

### Running the simulator

- **Default path**: pick a scripted scenario (e.g. `crash-open`) → Start.
- **Stress-test your real book**: press **Load live book** → switch Seed to **Live + scenario** → pick `generic-crash` or `random-walk` → Start.
- **Dry-fire one agent**: on `/agents`, click **Run in Simulator** on a row → arrives at `/admin/execution?mode=sim&agent_id=<id>` with the agent armed → pick a scenario → Start. The agent fires regardless of its `status`, `schedule`, cooldown, or baseline gate; no real agent state is mutated.

Auto-stops after 30 minutes so a forgotten sim can't bleed forever.

---

## Price-history charts (`/api/charts/*`)

In-memory rolling per-symbol price buffers + lifecycle markers from
`AlgoOrder` rows give the operator a chart of "what the price did and
where the chase fired" without any new persistent state.

**Capture points** (zero new schema, deque self-trims):

- [`SimDriver._price_history`](backend/api/algo/sim/driver.py): `dict[symbol, deque(maxlen=600)]`. `_capture_price_history()` runs at the end of every `_apply_next_tick`, snapshotting `(ts, ltp, bid, ask)` per row in `_positions_rows`. Wiped on every `start()`.
- [`PaperTradeEngine._price_history`](backend/api/algo/paper.py): same shape, populated in `step()` after `prefetch_for(open_now)` so the snapshot reflects the same quote the chase loop evaluated against. Wiped on `reset()`.
- Live mode currently shares the prod paper engine — both feed off `LiveQuoteSource`. A dedicated live engine can plug in here later if real-broker mode grows its own state.

**API** ([`backend/api/routes/charts.py`](backend/api/routes/charts.py), admin-guarded):

| Route | Purpose |
|---|---|
| `GET /api/charts/symbols?mode=sim\|paper\|live` | Symbols with at least one captured tick. Used by the chart panel's symbol picker / grid. |
| `GET /api/charts/price-history?mode=…&symbol=…&since=…&limit=600` | `{ticks: [...], events: [...]}` — ticks from the in-memory buffer, events derived from `algo_orders` rows for the same symbol+mode (placed at `created_at`/`initial_price`, terminal at `filled_at`/`fill_price` or fallback). |

**UI**:

- [`PriceChart.svelte`](frontend/src/lib/PriceChart.svelte) — hand-rolled SVG line + bid/ask shaded band + lifecycle markers (placed=amber / filled=emerald / unfilled=red). Polls `/api/charts/price-history` every 3 s. No chart library — keeps the bundle thin.
- `/admin/execution?mode=sim` embeds one mini chart per symbol returned by `GET /charts/symbols?mode=sim` directly under the position pills, so the operator sees the trajectory + chase markers live.
- The LogPanel **News** tab is the operator's headline feed inside the algo dark UI. Pulled out of the shared `<pre>` (where every other tab still lives) and rendered as a proper `<ul class="log-news-list">` with one `<li class="log-news-row">` per headline: `[HH:MM]  [<a> title <span> · source </span></a> flexes]`. The source label sits inside the title link as a trailing tag (subtle leading "·" separator, sky-cyan small-caps) so the row is a single clickable element instead of three loose pieces. Carries a `Refreshed at <ts>` heading line above the list, matching the public `/market` and `/performance` Market News card layout. `loadNews()` captures `refreshed_at` from the `/api/news` payload; heading hides on cold-start. The dual-zone presentational `timestamp` is parsed by a news-specific `_newsTime()` helper (extracts the first `HH:MM` run) — `_shortTime()` only knows ISO/`HH:MM:SS` and was dumping the whole 60-char dual-zone string into the time column. CSS lives in [`app.css`](frontend/src/app.css) alongside the rest of the log palette.
- The standalone **Chart** tab inside LogPanel was retired — never had a clear use case (the pages that need charts already render them inline alongside their own controls, see `/admin/execution`, `/admin/derivatives`). Removed the tab + the `chartMode` / `chartSymbols` / `chartsBySymbol` props from LogPanel's API. The pages that were feeding those props for the tab dropped the dead fetch chain.

**Cleanup**: deque `maxlen=600` is the only retention mechanism — at the default tick rates (2 s sim / 5 s paper) that's ~20 min of history per symbol. Restart loses the history; operator monitoring the chase live doesn't need cross-restart continuity. If post-mortem replay becomes valuable, swap to a `price_ticks` table here.

---

## Derivatives — underlying-driven re-pricing

Options + futures re-price coherently off underlying spot moves so a single "−3% NIFTY" tick cascades through every NIFTY contract instead of each strike moving in isolation.

**Module** ([`backend/api/algo/derivatives.py`](backend/api/algo/derivatives.py)) — pure-Python, no scipy:

- **Symbol parser** — `parse_tradingsymbol(sym)` returns `{kind: 'opt'|'fut', underlying, strike, opt_type, expiry}` for Kite F&O symbols. Handles monthly (`NIFTY25APR22000CE`) and weekly (`NIFTY2542422000CE`) options, monthly futures (`NIFTY25APRFUT`), stock options (`RELIANCE25APR2800CE`). Returns `None` for cash-equity tradingsymbols.
- **Black-Scholes** — `black_scholes(S, K, T_years, r, sigma, opt_type)` for vanilla European options. q=0 (Indian index options pay no carry). Vega/theta deliberately ignored — sim runs are minutes, not days.
- **IV calibrator** — `implied_vol(price, S, K, T, r, opt_type)` is a bisection solver over [0.0001, 5.0]. Falls back to `DEFAULT_IV = 0.15` when the bracket can't bracket. Locked once at sim seed; subsequent ticks re-price with that cached σ.
- **Re-pricer** — `reprice_row(row, *, spot, sigma)` returns the new last_price for a given derivative position. Futures track spot 1:1; options use BS with the cached σ.
- **Underlying-key resolver** — `underlying_ltp_key(name)` maps underlying names to Kite quote keys (`NIFTY` → `NSE:NIFTY 50`, `BANKNIFTY` → `NSE:NIFTY BANK`, stock fallthrough to `NSE:<NAME>`).

**Sim wiring** ([`backend/api/algo/sim/driver.py`](backend/api/algo/sim/driver.py)):

- `_underlyings: dict[str, float]` — name → current spot. Resolved at seed time via (1) `scenario.initial.underlyings` explicit override → (2) the futures position's last_price → (3) median strike across the option book as a crude ATM proxy.
- `_iv_cache: dict[str, float]` — per-option σ calibrated against the seed's last_price.
- `_underlying_history: dict[str, deque]` — parallel rolling buffer for spot ticks; surfaced via the same `/api/charts` endpoints as contract ticks.
- New move primitives in `_apply_moves` — scope `underlying.<NAME>` or `underlying.*`:
  - `underlying_pct {value: -0.03}` → spot × (1 + value)
  - `underlying_abs {value: -25}` → spot + value
  - `underlying_target {value: 22000}` → spot ← value
  After the move, every position whose underlying matches re-prices via `_reprice_derivatives_for`. Each derivative gets its own change row in the tick log so the LogPanel's Simulator tab shows the chain (spot move + N option re-prices).

**Paper/live wiring** ([`backend/api/algo/paper.py`](backend/api/algo/paper.py)) — `PaperTradeEngine._capture_price_history` parses each open order's symbol via `parse_tradingsymbol`, dedupes underlyings, then calls `broker.ltp([keys])` once with the resolved Kite keys. Underlying spots land in `_underlying_history` alongside the contract ticks. No new schema; same deque cap.

**Chart UI** ([`PriceChart.svelte`](frontend/src/lib/PriceChart.svelte)):

- `/api/charts/symbols` returns each symbol's `kind` (`underlying` / `derivative` / `other`) and `underlying` (name when kind=derivative). Sorted with underlyings first, derivatives grouped by underlying.
- Chart header shows a kind tag (sky-blue `SPOT` for underlyings, amber `F&O` for derivatives) next to the mode pill.
- For derivative charts the component fetches the underlying's history too and overlays it as a sky-blue dashed line, normalized into the option's plot area so a 22,000 NIFTY move doesn't squash the 180-rupee call line.
- A `chart-legend` chip identifies the dashed underlying line so the operator never confuses the two.

**Built-in scenarios** — [`scenarios.yaml`](backend/api/algo/sim/scenarios.yaml) ships `nifty-down-3pct` and `nifty-up-3pct` (each: three `underlying_pct` ticks of ±1% on `underlying.NIFTY`). Pair with `seed_mode: live` / `live+scenario` so the BS re-pricing runs against real strikes + premiums.

**Custom-positions seeding** — the Simulator panel on `/admin/execution?mode=sim` exposes a "Custom positions" panel (account / symbol / qty / LTP rows) that the operator fills inline. `POST /api/simulator/start` accepts `custom_positions: list[dict]`; the driver appends them to whatever scripted/live seed produced via [`_normalise_custom_positions`](backend/api/algo/sim/driver.py) (uppercases the symbol, infers exchange `NFO` for parseable F&O / `NSE` otherwise, defaults `average_price = last_price`). Custom rows are layered BEFORE `_seed_derivatives` runs, so synthetic NIFTY/BANKNIFTY/etc. options pick up underlying spots + IV calibration the same way real positions do.

**Performance — per-underlying index + cached parse**: `_seed_derivatives` walks positions once, stashes the parser result on each row as `row["_parsed"]`, and builds [`_positions_by_underlying: dict[str, list[dict]]`](backend/api/algo/sim/driver.py). All downstream consumers (`_reprice_derivatives_for`, IV calibration, futures-as-spot proxy) read from these cached structures — `_apply_underlying_move("underlying.NIFTY", …)` is now O(matched-rows) instead of O(positions). Hot-path regex calls dropped from 3-per-row-per-tick to 1-per-row-per-seed.

---

## Paper-trading workspace (`/admin/execution?mode=paper`)

> The per-mode pages (`/admin/paper`, `/admin/live`, `/admin/shadow`, `/admin/replay`, `/admin/simulator`) and the `/watchlist` redirect were removed when the consolidated `/admin/execution` page absorbed them. Every mode now lives under `/admin/execution?mode=<slug>`. The notes below describe the paper-mode panel inside that workspace.

Visual surface for the prod paper-trade engine, pairing with the simulator panel so operators can monitor mode 2 the same way they monitor sims.

**Page**: [`frontend/src/routes/(algo)/admin/paper/+page.svelte`](frontend/src/routes/(algo)/admin/paper/+page.svelte). Polls `/api/charts/paper-status` every 3 s. Layout:

- Status banner — green/sky `CHASING` (orders in flight on main), amber `IDLE` (engine enabled, no orders), grey `DEV` (engine gated on this branch).
- Open-order pills — same shape as the sim page's chase pills (side / qty / symbol / current limit / attempt count).
- Chart grid — one mini chart per symbol with captured ticks; underlyings rendered first (sky-blue `SPOT` tag), derivatives grouped by underlying with the spot overlaid as a dashed line.
- Embedded LogPanel for order / agent / system / news streams; the page's main chart grid handles all chart rendering directly.

**API**: [`/api/charts/paper-status`](backend/api/routes/charts.py) — admin-guarded. Returns `{enabled, branch, open_order_count, open_order_details, captured_symbols, captured_underlyings}`. `enabled = (deploy_branch == 'main')` — the engine still exists on dev branches but no `tick_loop` is running, so no orders register and the page banner explains the gate.

**Auto-reconcile** ([`backend/api/routes/orders.py::list_active_chases`](backend/api/routes/orders.py)) — when polling for active in-flight chase orders, the endpoint now auto-reconciles live OPEN `algo_orders` rows against the cached `/api/orders` snapshot (15s TTL). Helper `reconcile_algo_orders()` + `reconcile_single_order()` route through `get_broker(account)` (matches fix for `kill_chase` per commit 41133e16) so no stale `KiteConnection.cancel_order` no-op paths. Killed set marked via `mark_killed()` / `is_killed()` signal; chase_order's CANCELLED-status branch checks the flag and exits instead of re-placing. Dashboard chase panel + LogPanel Order grid both auto-clean after fills without requiring manual refresh.

---

## Derivatives analytics workspace (`/admin/derivatives`)

Distinct workspace from the tick-chart pages — this is options *research*, not live monitoring. For any single-leg option (live position / sim position / hypothetical typed-in symbol), it computes Greeks, payoff curve, theoretical-vs-market discrepancy, max-profit / max-loss / breakeven / probability-of-profit, plus a 30-day historical price chart.

**σ-driven payoff range**

The chart x-axis used to be a fixed ±10% around spot, which wasted space on short-DTE options (a 7-DTE option might cover 1σ at expiry in just ±5%) and squashed long-DTE ones (60-DTE, 25% IV easily wants ±25%). Both endpoints now auto-derive the range from the underlying's standard deviation at expiry:

```
span_pct = span_sigmas × σ × √T_years        (default span_sigmas = 2.5)
```

`σ` is the calibrated IV for single-leg, the qty-weighted IV proxy for strategy. Clamped to `[2 %, 50 %]`. Operator can override by passing an explicit `span_pct` in the query (single-leg) or request body (strategy).

The response now includes `span_pct` (decimal fraction actually applied) and `span_sigmas` (the σ-multiple it was derived from — 0 when the operator overrode `span_pct`). UI footnote reads "±2.5σ (7.3%)" when auto-derived, "±10.0%" when overridden.

**Math** ([`backend/api/algo/derivatives.py`](backend/api/algo/derivatives.py)):

- `greeks(S, K, T_years, r, sigma, opt_type)` — analytical Δ Γ Θ V ρ. Theta is per-day, Vega is per 1 % IV, Rho is per 1 % rate (trader-friendly units, not raw mathematical units).
- `prob_above(S, K, T, r, sigma)` — P(S_T ≥ K) under the Black-Scholes log-normal assumption. Used as the building block for POP.
- `risk_metrics(S, K, T, r, sigma, opt_type, qty, entry_price)` — single-leg max profit / max loss / breakeven / POP. Returns `inf` for unlimited-payoff legs (long calls, short puts); the API serializes those as `null` so the UI renders "∞".
- `payoff_curve(S, K, T, r, sigma, opt_type, qty, entry_price, span_pct, points)` — list of `{spot, today_value, expiry_value}` spanning ±`span_pct` around current spot. Both values are **position P&L** (signed qty, net of entry cost) so they read as money the operator would make/lose.

**LTP fallback chain (graceful degradation)**

Both endpoints now use `broker.quote()` (richer than `ltp()` — has `ohlc.close` + depth) and degrade through this chain rather than 502'ing on any failure:

1. **override** — operator-supplied `ltp` query param / leg field
2. **sim** — `_positions_rows` row's `last_price` when sim is active
3. **live** — broker's `last_price`
4. **close** — previous-day `ohlc.close` (off-hours, weekend, illiquid)
5. **depth** — midpoint of top-of-book bid/ask
6. **avg_cost** — operator's recorded entry price
7. **estimated** — Black-Scholes at `DEFAULT_IV` against the resolved spot

Spot fallback chain mirrors this: override → sim → broker quote → `fallback` (the option's strike used as a synthetic spot — payoff shape is preserved, absolute P&L is not). The endpoint never returns 502 when the operator passed an option that's parseable; it always produces a payoff curve and surfaces source provenance for the UI to render appropriate stale chips.

`ltp <= 0` is treated identically to `ltp = None` (sim pickers that copied a stale `last_price=0` would otherwise bypass the broker fetch and fail straight to `avg_cost`).

**Endpoints** ([`backend/api/routes/options.py`](backend/api/routes/options.py), admin-guarded):

| Route | Purpose |
|---|---|
| `GET /api/options/analytics?mode=live\|sim\|hypothetical&symbol=…&[account, qty, avg_cost, spot, ltp, iv, span_pct, points]` | Single-leg bundle — Greeks (per-share + position-scaled), pricing block, risk, payoff curve. One round-trip. Hypothetical mode lets the operator dry-analyse a strike before taking the trade. |
| `GET /api/options/historical?symbol=…&days=30&interval=day&exchange=NFO` | Kite OHLCV bars. Instrument-token lookup goes through the cached instruments dump. |
| `POST /api/options/strategy-analytics` (body `{legs: [{symbol, qty, avg_cost?, ltp?, iv?}], spot?, span_pct?, points?}`) | Multi-leg aggregate analytics — vertical spreads, iron condors, butterflies, strangles. Each leg's `ltp` is optional: when present (e.g. legs sourced from the simulator) it's used directly; when absent, the broker is hit once for the whole batch. v1 enforces same-underlying + same-expiry across legs (calendar / diagonal spreads not yet supported). |

**Expected value + risk:reward**

Both endpoints surface position-level expected value and R:R alongside the existing POP / max-profit / max-loss block:

- `expected_value(curve, S, T_years, sigma)` — trapezoidal integration of `expiry_value` against the risk-neutral lognormal pdf of the underlying:

  ```
  f(S_T) = (1 / (S_T σ √(2πT))) · exp(-(ln(S_T/S) − (r − σ²/2)T)² / (2σ²T))
  ```

  The curve typically spans ±2.5σ which captures ~99 % of the lognormal mass, so truncation error is sub-percent. The signed-qty payoff is already baked into the curve, so this is just one trapezoidal pass per request.

- `risk_reward_ratio(max_profit, max_loss)` = `max_profit / |max_loss|`. Returns `None` for unbounded legs (long calls, short puts) where the ratio isn't meaningful — UI renders "—".

**Response surface** — `OptionRisk` and `StrategyRisk` gain `ev` (₹), `ev_pct` (return-on-cost %, null when entry cost is 0), `rr_ratio` (null when unbounded). UI risk panel renders them as additional `kv-` rows alongside POP / breakeven / max-profit / max-loss.

**Why this matters operationally** — POP alone is misleading. A 95 %-POP credit spread that risks ₹50k to make ₹500 has positive expectancy but a single loss takes 100 winners to recover; EV captures the magnitude side and tells you whether the trade is actually worth taking. R:R does the same for the asymmetric clip-size aspect.

**Option-chain picker** — `/admin/derivatives` Strategy mode now has a third leg-input alongside "Add from book" and "+ Add row": an option-chain table that lets the operator browse strikes for any underlying / expiry and click `+ CE` / `+ PE` to drop a leg. Sourced from the existing instruments cache ([`frontend/src/lib/data/instruments.js`](frontend/src/lib/data/instruments.js); IndexedDB-backed, ~90k contracts) so no new API. Default qty = `lot_size` for the contract, side toggle (Long / Short) flips the sign. A **Futures quick-add row** sits above the strike grid — clicking a futures pill drops the contract into the basket as a Long or Short leg per the side toggle, useful for delta-hedged options structures (covered call / collar / synthetic long) and pure futures plays.

**Futures legs in strategy** — `multileg_payoff_curve` and `multileg_greeks` accept `kind: "fut"` legs. Futures are linear in spot (today's value tracks spot 1:1 over the sim window; expiry settles to spot), so they contribute pure delta (1 per share, signed by qty) and zero everywhere else. The strategy endpoint resolves a futures-leg LTP through the same broker.quote / avg_cost / spot fallback chain options use, but skips IV calibration entirely. Cost basis defaults to LTP for "what would buying this NOW look like" semantics. Smoke-tested on the canonical covered call (long fut + short ATM call): delta ≈ +29 (lot of 50 fut + short ATM call −21), positive theta, negative vega, max profit capped at strike+premium, max loss bounded by spot-floor.

**Multi-leg math** ([`backend/api/algo/derivatives.py`](backend/api/algo/derivatives.py)):

- `multileg_payoff_curve(legs, S, ...)` — sums per-leg `today_value` + `expiry_value` at each spot. Each leg keeps its own (T_years, σ).
- `multileg_greeks(legs, S, ...)` — sums signed-qty per-leg Greeks (Greeks are linear in qty).
- `find_breakevens(curve)` — linear-interpolated zero-crossings on the expiry curve. Iron-condor-shaped strategies report 2 BEs; verticals 1; fully ITM/OTM 0.
- `multileg_pop(curve, S, T, σ_proxy)` — walks the expiry curve, identifies contiguous profit segments, integrates the lognormal `prob_above` over each. Open-ended endpoints (curve runs off-screen still in the money) use the analytical limits so we don't artificially clip POP. `σ_proxy` is the qty-weighted IV across legs — defensible single number from the data we have.
- `multileg_extremes(curve)` — numerical max profit / max loss from the expiry curve. As wide as the spot range; unlimited-payoff legs (long calls, short puts) need the operator to widen `span_pct` if the realistic upside isn't covered.

**Pricing-account setting** — `connections.price_account` (string, default blank) lets the operator pin which Kite account to use for shared market-data fetches (underlying spots in `PaperTradeEngine._capture_underlyings`, instrument lookup + LTP + historical for `/admin/derivatives`). Implemented in [`backend/shared/brokers/registry.py::get_price_broker()`](backend/shared/brokers/registry.py); falls back to the first available account when the setting is blank.

**PriceBroker rate-limit cool-off** (`backend/shared/brokers/registry.py`) — `PriceBroker._try()` detects Kite's "Too many requests" error (case-insensitive substring match) and marks that broker as rate-limited for 30 s (`_RATE_LIMIT_COOLOFF_SECONDS`). Subsequent calls within the cool-off window skip the rate-limited broker immediately (no network round-trip) and fall over to the next broker in the preference chain. The cool-off map (`_RATE_LIMIT_COOLOFF`) is module-level with a `threading.Lock`; the state clears automatically when the cool-off expires. This prevents a Kite rate-limit event from generating an amplifying storm of retries against the same host.

**UI** — [`frontend/src/lib/OptionsPayoff.svelte`](frontend/src/lib/OptionsPayoff.svelte) is the payoff-chart SVG. Two curves (today amber solid, expiry sky dashed), profit/loss zone shading, vertical markers for spot (cyan) / strike (white dashed) / breakeven (amber dashed), hover crosshair with a 3-line tooltip. Hand-rolled SVG, no chart lib.

**On-chart stat overlay** — top-left HTML overlay (`pointer-events: none` so it never blocks SVG hover/zoom/pan) showing `SPOT / TDAY / EXP / DTE / σ / LEGS`. Color-coded values (sky for spot, green/red for P&L direction) and `tabular-nums` so right-aligned rupees stay column-aligned. The page header above the chart was previously two rows (title + chips, then meta row with DTE / σ-proxy / legs / TDAY / EXP) but the meta row visually overlapped the overlay box on tight viewports. Now the header is a single row carrying the title + NET DEBIT/CREDIT + MAX PROFIT + MAX LOSS chips (the at-a-glance "what can this strategy make/lose" pair sits at page-header altitude where it reads first); the overlay carries the live numerics that change tick-to-tick.

**Sigma axis labels** on the payoff chart use the algo amber accent (`#fbbf24`, font-size 11, weight 700) for whole-σ ticks and light gray-blue (`#c8d8f0`, font-size 10, weight 500) for half-σ. Earlier the whole-σ labels were `#c8d8f0` and the half-σ `#7e97b8` — both too low-contrast against the navy chart background to be glanceable. The σ symbol (not "sigma" or "σ-proxy") is the canonical label everywhere it's referenced on the chart.

**Page model (v4)** — the page is a single multi-leg payoff workspace; there's no Single-vs-Strategy mode any more. One leg renders the same chart + Greeks + risk panel as many. The picker bar is two dropdowns + a single `+` toggle:

| Control | Purpose |
|---|---|
| **Account** (MultiSelect) | Scopes which broker accounts the candidates pull from. Empty = all. |
| **Underlying** (Select) | NIFTY / BANKNIFTY / … derived from the loaded book. Sets the universe. |
| **+ / −** (toggle pill) | Opens an option-chain picker; clicks land as drafts. |

Live vs sim is **auto-detected** from `/api/simulator/status`. When a sim is active the page works off sim positions and the header carries a `SIMULATOR` badge; otherwise it works off live broker positions. Polled every 5 s.

**Drafts** replace the old "hypothetical" mode — operator-typed positions appear as editable rows above the candidates list. Drafts whose symbol matches the selected underlying surface in Candidates and feed the strategy analytics like any other leg. The `+` button opens the chain picker (browse strikes for the chosen underlying, click +CE / +PE / a futures pill to drop a leg into Drafts).

**Holdings ON/OFF toggle** — new slider-style switch in the OptionsPayoff legend (small label "Hold", filled sky-cyan when ON / outline when OFF). When OFF: eq (holding) legs hidden from Legs grid, dropped from TOTAL row, removed from chart overlay, excluded from candidatesActualPnl + candidatesDayPnl sums. Useful for equity-only payoff analysis (DIXON stock + NIFTY puts = pure derivative P&L without the stock's realized basis). Pre-fill from `localStorage` so operator's last choice persists. Backend: `_synthEquityOnlyStrategy()` builds zero-baseline payoff when holdings are toggled off; `_mergedPayoff` overlays the synthetics linearly. Perf: memoized by leg signature so chart re-render is free when holdings state unchanged.

**Candidates panel** sits immediately below the payoff chart — replaces the older Per-leg breakdown card (the same backend data was shown twice, once with checkboxes, once read-only). Rows are scrollable horizontally + vertically (`.cand-scroll` wraps the grid; max-height 22rem; rows have a 720px min-width so the layout never breaks on narrow viewports). Toggling a checkbox rebuilds `legs[]` via `$effect`, which auto-triggers the strategy analytics endpoint — no Analyze button. Each Candidates row carries a chart-icon button (via the canonical `.row-chart-btn` global pattern) that opens `<ChartModal>` for that symbol.

**Historical chart removed** — when the page collapsed to multi-leg-only, the per-symbol historical chart lost its anchor (a single picked symbol). The historical endpoint stays on the backend (`GET /api/options/historical`) for any future re-introduction; the frontend no longer calls it.

**Historical-bars endpoint is graceful** — `GET /api/options/historical?symbol=…` no longer 404s when the instrument isn't in the cached dump for the first exchange tried. When the caller passes an explicit `exchange` (e.g. `MCX` for commodity pins, `NSE` for underlying spot) the handler tries **only that exchange** — no fallback walk. When `exchange` is blank it walks NFO → BFO → NSE → BSE → MCX → CDS in sequence. Returns an empty `bars: []` (200 OK) when nothing matches, rather than bubbling a 4xx that crashes the page's chart panel. Same pattern when the broker is unreachable — empty bars instead of 502.

**Historical OHLCV in-process cache** — results are cached in `_HIST_CACHE` (module-level dict in `backend/api/routes/options.py`) keyed by `(symbol, exchange_hint, days, interval)`. TTL is 60 s for non-empty bars and 10 s for empty bars (transient rate-limit failures). This means a refresh or reconnect storm can only hit Kite once per minute per symbol. The cache uses a `threading.Lock` (not asyncio.Lock) because the hot path runs in the async frame but the broker calls are offloaded to `asyncio.to_thread`; a sync lock is sufficient and safe on CPython.

**Historical OHLCV multi-account fallback** — when a Kite account returns "Too many requests", the endpoint automatically tries the next eligible account rather than returning empty bars immediately. The mechanism:

- `BrokerAccount.historical_data_enabled` (boolean, DB default `true`) controls per-account eligibility. Set to `false` via `/admin/brokers` to reserve a low-rate-limit account for order-flow only; it will be skipped entirely for historical calls.
- `get_historical_brokers()` in `backend/shared/brokers/registry.py` returns the prioritised list of eligible, non-rate-limited accounts (same preference ordering as `get_price_broker()`: pinned price_account first, then sorted by `priority` ASC). Accounts in the existing 30-second rate-limit cool-off are excluded from the list at build time so no wasted network round-trips.
- The handler iterates the list; on "too many requests" the account is marked rate-limited via `_mark_rate_limited` and the loop continues to the next one. On any other exception the account is skipped with a WARNING log and the loop continues.
- An empty broker list (all accounts disabled or all in cool-off) returns graceful empty bars with a 10-second cache TTL so the next request re-evaluates quickly after cool-offs expire.
- Cache HIT short-circuits the entire loop — a warm cache entry is returned before `get_historical_brokers()` is even called.

**Strategy 500 traps** — two separate bugs caused 500s on the strategy endpoint and were fixed together:
1. `parse_tradingsymbol("…FUT")` returns a dict without a `strike` key. `sorted_strikes = sorted({parse_tradingsymbol(l.symbol)["strike"] for ...})` crashed with KeyError when a futures leg was in the basket. Guarded with `(p := parse_tradingsymbol(l.symbol)) and "strike" in p`.
2. An inner `from backend.api.algo.derivatives import DEFAULT_IV, black_scholes` inside an LTP-fallback branch made Python flag `DEFAULT_IV` as a function-local for the whole `_strategy_analytics_impl` scope. When that branch didn't execute, the later `sig == DEFAULT_IV` raised `UnboundLocalError`. Removed the redundant inner import (DEFAULT_IV is already imported at module level). The endpoint also now wraps in a try/except that calls `logger.exception(...)` so future 500s leave a traceback (Litestar's default 500 handler swallowed them silently).

`OptionsPayoff` accepts either scalar `strike` / `breakeven` props (single-leg) or arrays `strikes` / `breakevens` (multi-leg) — same SVG, same palette.

Polling: strategy analytics auto-refreshes whenever the leg set changes (an `$effect` on `legs`), plus a 5 s visibleInterval to keep Greeks + IV live while the operator stares at the page. Sim status polled at 5 s; positions list at 30 s.

### Derivatives 3-band expiry view

**Tab renamed** — `/admin/derivatives` `TO CLOSE` tab renamed to `ITM ON EXPIRY` reflecting operator request + risk clarity.

**3-band layout** — all open derivatives grouped into sections: `ITM ON EXPIRY` (amber pill, action required) · `NETTED` (slate pill, broker nets at settlement) · `OUT OF THE MONEY` (muted pill, monitor only). Each section has a pill-style header `[● ITM ON EXPIRY (N)]` with position count. Sections display conditionally (ITM hidden when empty, NETTED always empty for equity, etc.).

**MCX commodity netting** — greedy theta-priority pairing. Four valid pair types: long CE ↔ short CE, long PE ↔ short PE, long CE + long PE, short CE + short PE. Pairing is per-account, per-underlying, per-expiry. High-|theta| pairs bind first; low-|theta| as residual. Each pair gets a numbered chip (N1, N2, ...) shared by both legs. Alternating 5-color tints (sky/violet/teal/pink/lime) per pair for visual distinction. When a partial cancel occurs (e.g. 3 lots short vs 2 lots long → 2 paired, 1 residual), the rows split with pair ID on matched legs and "UNPAIRED" label on residual.

**Equity / NSE** — no netting (NETTED band always empty). Every ITM position → ITM ON EXPIRY band.

**Integration** — netting summary persisted in `positions.expiry_netting_pair_id` (nullable int). Operators can override via the edit row (future work: "break pairing" button per leg).

---

## Proxy hedges — held instrument hedges a different option underlying

**Major capability.** No Indian retail platform (Sensibull / Streak / Opstra) ships this; institutional tools (Bloomberg PRM / IBKR Portfolio Margin / OptionVue) charge thousands per year for their version. The implementation here is operator-grade: edit a single 4-column row in `/admin/settings → hedge_proxies`, click Compute β, and the page auto-converts your held GOLDBEES into GOLDM-lot-equivalent exposure with no operator-typed factor anywhere.

DB-backed cross-reference between holdings (GOLDBEES, SILVERBEES, NIFTYBEES, BANKBEES, individual stocks, …) and the option roots they can hedge against (GOLD, SILVER, NIFTY, BANKNIFTY, etc.). When the operator picks one of those underlyings on `/admin/derivatives`, matching proxy holdings surface as eq legs in the Legs panel with auto-derived conversion math.

### Data model

`hedge_proxies` table — pair-only schema with a regression placeholder:

| Column | Use |
|---|---|
| `proxy_symbol` | held instrument (GOLDBEES, RELIANCE, …) |
| `target_root` | option underlying the proxy hedges (GOLD, NIFTY, …) |
| `is_active` | toggle |
| `note` | free-form |
| `beta` | regression slope from Stage 3 — NULL → math uses 1.0 (ETF case) |
| `correlation` | R² from the regression (0..1) |
| `regression_at` | when β was last computed |
| `created_at` / `updated_at` | standard |

Migration shape: legacy Stage 2 schema (conversion_kind / static_factor / kind / source columns) detected at boot via `information_schema.columns` and DROP'd; init_db recreates the simplified shape; seeder re-inserts the six default pairs.

Seeded defaults on first boot:
- `GOLDBEES → GOLD`, `GOLDBEES → GOLDM`
- `SILVERBEES → SILVER`, `SILVERBEES → SILVERM`
- `NIFTYBEES → NIFTY`
- `BANKBEES → BANKNIFTY`

### Math (per render, no factor stored anywhere)

```
market_value     = raw_qty × proxy_LTP            (broker live)
effective_qty    = β × market_value / target_spot
target_lots      = effective_qty / target_lot_size
investment_value = raw_qty × avg_cost
effective_cost   = investment_value / effective_qty
payoff_add(S)    = (S − effective_cost) × effective_qty
Δ_extra          = effective_qty
```

β defaults to 1.0 when the regression hasn't run yet (ETF tracking case). Stage 3 stock-vs-index uses the regression slope. The Lots column in Legs displays `target_lots` directly so 1500 GOLDBEES reads as `0.15` GOLD lots (rather than 0 from the lotsForRow helper, which doesn't know about proxies).

### Stage 3 — β regression (operator-triggered)

[`POST /api/admin/hedge-proxies/{id}/compute`](backend/api/routes/hedge_proxies.py) runs a 60-day daily-returns regression: `β = Cov(p,t) / Var(t)`, `R² = corr²`. Symbol resolution baked into `_TARGET_HINTS`:

| Target | Exchange | Resolved as |
|---|---|---|
| NIFTY | NSE | "NIFTY 50" (index instrument) |
| BANKNIFTY | NSE | "NIFTY BANK" |
| FINNIFTY | NSE | "NIFTY FIN SERVICE" |
| GOLD/GOLDM | MCX | front-month FUT |
| SILVER/SILVERM | MCX | front-month FUT |
| (others) | NSE | direct tradingsymbol match |

Proxy symbols default to NSE (works for stock proxies + ETFs that list on NSE). Regression needs ≥15 overlapping bars; failure (resolution miss, too few bars) returns 422 with a diagnostic.

### Stage 4 — daily auto-recompute

[`_task_hedge_proxy_regression`](backend/api/background.py) fires daily at 02:30 IST. For every active row whose `regression_at` is older than `hedge_proxies.regression_max_age_days` (default 7), it runs the same regression as the manual endpoint and writes back. Failed regressions still stamp `regression_at` so a broken pair doesn't retry daily. 1s pacing per row to stay within Kite's 3 req/s historical budget.

Settings:
- `hedge_proxies.regression_enabled` (bool, True) — kill-switch
- `hedge_proxies.regression_window_days` (int, 60) — daily candles in the regression
- `hedge_proxies.regression_max_age_days` (int, 7) — skip freshness window

### UI surfaces

- **Underlying picker tier 4** — proxy holdings without a direct derivative position appear in the hedge-opportunity tier (alongside direct F&O-eligible holdings). Pick GOLD → GOLDBEES proxy leg auto-checks via the existing eq-leg auto-check effect.
- **PROXY chip** on eq rows — magenta, label carries the lot count (`PROXY 0.15×`) and β when set (`PROXY 0.15× β1.18`). Tooltip surfaces the full chain: `β=1.183 × market value ₹250000 ÷ NIFTY spot ₹25000 ≈ 11.83 NIFTY-equiv ≈ 0.24 NIFTY lots · R²=0.78`.
- **Lots cell** on proxy rows — shows `target_lots` (not the raw GOLDBEES lot count, which is 0).
- **/admin/settings → Hedge proxies** — list + add form, columns `Proxy | Target | Note | β | R² | Run | Active`, "Compute β" button per row.

### Frontend module

[`$lib/data/hedgeProxies.js`](frontend/src/lib/data/hedgeProxies.js) — API-backed in-memory cache. `loadHedgeProxies(force=true)` re-fetches after admin mutations. Three indices (`_byTarget`, `_byProxy`, `_byPair`) for O(1) lookups during render. `getProxyRow(proxy, target)` returns the row at math time so β + correlation feed the derivations.

---

## Chart workspace (`/charts`) — unified chart canvas

A consolidated, reusable chart component that renders historical OHLCV + optional intraday price history + underlying-spot overlays + options Greeks for any symbol kind (underlying, future, option, equity). Serves as the entry point for all chart interactions across the platform.

**Files:**

| Path | Purpose |
|---|---|
| [`frontend/src/lib/ChartWorkspace.svelte`](frontend/src/lib/ChartWorkspace.svelte) | Unified canvas (~570 LOC). Renders OHLCV (line / area / candle, 1D / 1W / 1M / 3M / 6M / 1Y, SMA20 / SMA50 / Vol overlays). Optional intraday tick overlay (LTP stream + lifecycle markers from `/api/charts/price-history`, toggleable pill). Underlying-spot overlay for derivatives (dashed sky-blue line, fetched in parallel with bars). Greeks strip below chart for options (Δ Γ Θ V ρ IV via `fetchStrategyAnalytics`). Props: `{symbol, exchange?, mode? = 'live'\|'sim'\|'paper', compact?, showHeader?, bump?, onSymbolChange?}`. Demo-aware — anonymous sessions skip polling to avoid 401-spam. |
| [`frontend/src/lib/ChartModal.svelte`](frontend/src/lib/ChartModal.svelte) | Thin overlay wrapper (~100 LOC). Esc / overlay-click closes; body scroll-locked. Props: `{symbol, exchange?, mode?, onClose}`. Hosts `<ChartWorkspace compact={false}>`. |
| [`frontend/src/routes/(algo)/charts/+page.svelte`](frontend/src/routes/(algo)/charts/+page.svelte) | Standalone Charts page (~185 LOC). Reads `?symbol=…&mode=…` URL params; syncs picks back via `goto({replaceState:true})`. Page header carries RefreshButton + InfoHint + notification bells. Refresh increments `bump` integer; ChartWorkspace watches it via `$effect` to trigger reload. |

**Navbar:** `Charts` entry in `monitor` group, between Orders and Agents. Demo-visible (no `adminOnly`).

**Chart-icon button — canonical pattern across surfaces:** Glyph: line-chart SVG path (14×14 in headers, 12×12 in rows). Palette: cyan-400 (`#22d3ee` resting, `#67e8f9` hover, bg α 0.12 → 0.22 on hover, border α 0.45 → 0.65). Title: `"Open chart for {symbol}"`. Used in SymbolPanel header, `/orders` entry header, `/orders` row symbol cells (via `.row-chart-btn` global), `/admin/derivatives` Candidates rows, `/performance` symbol cells (ag-Grid cellRenderer).

**Symbol-kind handling:** ChartWorkspace auto-detects symbol type (underlying via `parseUnderlying()`, derivative via `parseTrading Symbol()`, equity fallback). Renders appropriate historical interval + Greeks conditionally.

**Demo behavior:** `getContext('algoStatus')` gates polling. Anonymous demo sessions skip `/api/charts/price-history` polls to avoid 401 errors (intraday data is operationally less useful for visitors anyway).

**`bump` reload pattern:** Parent page increments `bump` on manual refresh; ChartWorkspace watches via `$effect` and re-fetches all data (bars + historical + Greeks). Zero API calls when `bump` is stable (polling via `visibleInterval` alone).

---

## Charts batch endpoint + per-chart polling

The chart panel's per-symbol polling could blow up to N+M requests every 3 s (N = visible charts, M = underlying overlays). [`GET /api/charts/batch?mode=…&symbols=a,b,c`](backend/api/routes/charts.py) coalesces it to one round-trip.

- One `IN`-clause `algo_orders` query for the whole batch (cap 50 symbols), grouped client-side by symbol.
- Returns `{mode, charts: [ChartResponse, …]}` in the order of the input symbols.
- Symbols with no captured ticks come back with empty `ticks` / `events` so clients don't have to special-case absent entries.

**Frontend distribution**: [`PriceChart.svelte`](frontend/src/lib/PriceChart.svelte) gained a `data` prop. When the parent feeds it (and `chartsBySymbol` for underlying-overlay lookup), the chart skips its own poll timer (`stopPolling()` in a `$effect` triggered when `externalData` flips on). The simulator, paper, and agents pages all poll once and distribute via `chartsBySymbol`.

Effect: a page with 10 charts goes from ~200 req/min to ~20 req/min, no behaviour change for charts shown without a parent (falls back to per-chart polling).

---

## Chart grid lines

Both SVG chart components draw a faint cool-blue grid (`rgba(200,216,240,0.10)` for major lines, `0.07` for vertical x-axis lines) at 5 evenly-spaced y-positions and 4-5 x-positions across the visible domain. PriceChart x-grid lines carry HH:MM:SS labels along the bottom axis so the operator can correlate price moves with wall-clock seconds without dragging the hover crosshair. OptionsPayoff x-grid is unlabeled (the strike + breakeven + spot markers already carry the meaningful x-coordinates).

The grid colors are deliberately low-saturation cool-blue rather than amber — this keeps the chart's amber LTP / payoff lines visually dominant. Solid amber strokes against a faint blue grid is a much more legible combination than amber on amber-tinted grid.

---

## MarketPulse main grid — column rules

The Pulse symbols grid ([`frontend/src/lib/MarketPulse.svelte`](frontend/src/lib/MarketPulse.svelte)) intentionally **diverges** from the codebase-wide cluster rule (which is `LTP → Prev → Avg → Day P&L → Day % → P&L → P&L%`). Pulse-specific default-visible cluster:

```
Symbol · 5d · LTP · Avg · Prev Close · Qty · Day P&L · Day % · P&L % · P&L
```

The deviations are deliberate:
- **Avg next to LTP** — operators scan "where am I vs entry" first, before reference prices.
- **P&L % before P&L** — normalised return reads first when scanning across symbols at different price scales (Bloomberg / IBKR MarketWatch convention).

Other surfaces (PerformancePage Holdings + Positions, the `/admin/derivatives` Candidates panel) keep the canonical cluster order. PerformancePage is the canonical-cluster reference grid.

**Hidden-by-default columns:** Open, Bid, Ask, Vol, OI, Expiry. Operator can re-show via the ag-Grid column tool panel; visibility persists via `pulse.gridColumnState.v2` localStorage. The `v` suffix bumps when the column set is reshuffled so prior persisted state is discarded cleanly.

**Account-filter rule:** Positions and Holdings are the only account-scoped sources. When the Account multiselect is empty, both are hidden from the grid entirely — only Pinned watchlists, custom Watchlists, and Movers (not account-scoped) appear. Picking an account brings Positions + Holdings back, scoped to that account.

**Desktop two-grid layout (≥1024px / Tailwind `lg:`):**
The symbols view splits into two side-by-side ag-Grid instances inside a flex row so the wide-display whitespace stops at the inter-grid gap. Mobile (`<lg`) stacks them column-wise (left on top, right below), preserving the legacy scan order.

| Side | Buckets | Pinned-top behaviour |
|---|---|---|
| **Left** (`.mp-grid-left`) | Pinned watchlists (NIFTY 50, BANKNIFTY, etc.), custom Watchlists, Movers | The Pinned strip stays pinned at the top via `pinnedTopRowData` (Markets / Default / operator-pinned). |
| **Right** (`.mp-grid-right`) | Positions, Holdings | **TOTAL Positions** + **TOTAL Holdings** rows are pinned at the top. Sort-stable — pinned rows can't be reordered into the body by column sort, so the consolidated number stays put under any sort. |

Each grid scrolls independently. Both grids share the same `columnDefs`, `defaultColDef`, `postSortRows`, and `bucketOf` so visual identity is identical and sort behaviour matches across the split.

**Bucket sort integrity:** `bucketOf` returns 5 distinct values — `pinned (1)`, `watchlist (2)`, `positions (3)`, `holdings (4)`, `movers (5)`. `postSortRows` keeps the sort scoped within each bucket, so sorting by P&L on the right grid doesn't pull pinned NIFTY rows into the Positions block (and vice versa for movers vs. watchlists on the left grid).

**Show dropdown vocabulary:**
- **Default** — operator's working watchlist; ships **empty** at signup, ready for the operator to add symbols they want to monitor (the ★ marks it as the default add-target when clicking + on any row).
- **Markets** — auto-seeded with the major Indian indices (NIFTY 50, BANKNIFTY, FINNIFTY, MIDCPNIFTY, SENSEX) and MCX commodities (GOLD, SILVER, CRUDEOIL, …). Operator sees the broad market at-a-glance.
- Both are **pinned** (`is_pinned=True`), so they feed the Pinned major. Operator-created lists (`is_pinned=False`) feed a separate Watchlist major. A symbol appearing in both a pinned list and a user list renders twice — once per major, by design.

---

## Public-theme row indicators

Long/short indicator on `.ag-theme-ramboq` performance grids:

- **bars** (left + right edges, 4px box-shadow) are scoped to the symbol cell only (`.ag-col-sym`). Earlier iterations tinted the whole row + put a single-edge bar; the bars-on-the-symbol-cell-only treatment reads as "this is THE symbol of this row" rather than "the entire row is direction-tinted".
- **background tint** extends to BOTH symbol AND account cells (the two `ag-col-fill` columns) so the symbol+account pair reads as one direction-tinted block while the rest of the row stays clean. LONG = sage-teal `rgba(91,142,149,0.14)`; SHORT = warm-terracotta `rgba(196,122,61,0.14)` — desaturated so they sit alongside the cream + champagne palette without shouting.

To make the bars symbol-only, [`PerformancePage.svelte`](frontend/src/lib/PerformancePage.svelte) tags symbol cells with an extra `ag-col-sym` class via `cellClass: 'ag-col-fill ag-col-sym'`. Account cells keep just `ag-col-fill` (background tint, no bars). The Account column hides automatically when a specific account is picked (`setColumnsVisible(['account'], false)`).

---

## InfoHint popup variant

[`InfoHint.svelte`](frontend/src/lib/InfoHint.svelte): `popup` prop makes popout absolutely-positioned (z-index 50, no sibling push). Click pins it (text-selectable); hover preview disappears on mouse-leave. Styling: flat slate-blue bg, sky-blue border, bold spans amber. Viewport-clamped width: `min(13rem, 88vw)` to `min(32rem, 92vw)`. **Always use `text` prop, not children snippet** (SSR→CSR handoff can lose content in this codebase). Used on `/admin/derivatives` for Greeks + risk metrics — one-click inline help.

---

## Chart zoom y-range auto-fit

When the operator zooms in on the x-axis, both `PriceChart` and `OptionsPayoff` now also re-derive their y-domain from only the **visible** data points. Without this, zooming into a 2-minute window during a chase would leave the y-axis spanned by an out-of-view price excursion, squashing the relevant bid/ask wiggle against the top or bottom of the chart.

Implementation: `visibleTicks` / `visiblePayoff` is `$derived` filtering the input array by current `tMin`/`tMax` (or `sMin`/`sMax`); the existing `prices` / `yDomain` derivations read from this filtered set with a fallback to the full set when the visible window is empty (defensive). Adds zero overhead when not zoomed (the filter is essentially a passthrough).

---

## Chart grid + axis labels

Both SVG charts draw a faint cool-blue grid (`rgba(200,216,240,0.10)` major / `0.07` vertical x-axis) at 5 evenly-spaced y-positions and 4–5 x-positions. Both now also label the grid lines:

- `PriceChart` x-axis carries HH:MM:SS labels along the baseline so price moves can be read off without dragging the hover crosshair.
- `OptionsPayoff` x-axis carries spot-price labels at `y = height - PAD_B + 10` (between the axis baseline and the spot/strike/breakeven marker labels at `+18`), so they don't collide with the existing markers.

---

## Public-theme row borders — palette-aligned

[`app.css`](frontend/src/app.css) `.ag-theme-ramboq .ag-row.pos-long/short` previously used saturated teal-700 (`#0f766e`) and amber-700 (`#b45309`). Lightened to muted sage-teal `rgba(91,142,149,0.85)` and warm-terracotta `rgba(196,122,61,0.85)` — same direction-tint identity (cool/warm), but desaturated so they sit alongside the cream + champagne-gold palette without shouting. Background tint dropped from `0.15` to `0.10` alpha. The per-cell `.ag-cell:first-child` rule (the one that actually paints because the theme's solid `ag-col-fill` background covers the row-level box-shadow) carries the same color.

---

## Tabbed Market Summary + News on `/performance`

`<svelte:head>` placement matters in SvelteKit pages — it must NOT come before `<script>` for Svelte 5 reactivity to bind cleanly to the script's `$state` runes. Tab-switch click handlers were silent on `/performance` because the original file had `<svelte:head>` first, then `<script>`. Reordered to script-first and the tabs work.

Layout: single card with `[Market Summary | Market News]` tabs under the position grids, only one panel visible at a time. Both feeds load on mount; flipping is a paint, not a fetch. Public palette throughout — champagne underline on the active tab, navy text.

---

## Chart zoom + pan (PriceChart, OptionsPayoff)

Both SVG chart components ([`frontend/src/lib/PriceChart.svelte`](frontend/src/lib/PriceChart.svelte), [`frontend/src/lib/OptionsPayoff.svelte`](frontend/src/lib/OptionsPayoff.svelte)) carry a wheel-zoom + drag-pan + reset toolbar implemented in pure SVG/maths — no chart library.

**State**: each chart owns a `zoom: {xMin, xMax} | null` and `pan: {startClientX, startMin, startMax} | null`. The existing `xOf()` / `tMin` / `sMin` derivations read from `zoom` when set, falling back to the auto-derived data range otherwise. All downstream paths (LTP line, payoff curves, markers, hover tooltip) re-derive automatically.

**Handlers**:
- `onWheel` — clamps to data range when zooming out fully (resets to null), refuses to zoom narrower than 1 second (PriceChart) / 2 % of full range (OptionsPayoff).
- `onPointerDown` / `onPointerMoveSvg` / `onPointerUp` — pointer-captured drag-pan; suppresses hover tooltip while dragging.
- `resetZoom` — toolbar button that snaps back to auto-range. Visible only when `isZoomed` is true.

**CSS**: `cursor: crosshair` by default, `cursor: grabbing` when `.chart-panning` / `.payoff-panning` modifier is active. `touch-action: pan-y` so vertical scroll on mobile still works.

OptionsPayoff resets back to the auto `±span_sigmas × σ × √T` range that the API supplies — so post-reset the chart is consistent with the chart footnote ("±2.5σ (7.3%)").

---

## Market Summary + Market News (public)

`/market` consolidates the AI summary and news feed into a single tabbed card with `[Market Summary | Market News]`. Only one panel is visible at a time so the page stays compact; flipping tabs is a paint, not a fetch (both feeds load on mount).

Tab styling — the tab strip sits **outside** the white card, on the page's cream background. Active tab carries a champagne **bottom** border (`#d4920c`); the row's own `border-bottom: 1px solid #e7e0cf` stitches the strip together. The active tab's bottom border merges flush with the row's via `margin-bottom: -1px`, then a small `margin-top: 0.6rem` on the panel beneath gives the two regions visual separation. Earlier iterations used a left-border indicator with a tinted background; the bottom-border + outside-the-card treatment reads more naturally as a desktop-app document-tab. Right side of the tab row carries a "Loading…" / "Refreshing…" indicator. **Inside the panel**, a "Refreshed at <ts>" line uses consistent timestamp styling across the public site. `nowrap` keeps the dual-timezone string on a single line. Route:

- [`/market`](frontend/src/routes/(public)/market/+page.svelte) — page-level `lastRefresh` timestamp at the top, then a tabbed card. The `/performance` page surfaces positions + holdings only (tabbed card removed to keep focus on the book).

The Gemini market-summary prompt was simplified iteratively: drop the `**Daily Market Report — [date]**` heading, drop the date/timestamp line, drop the H3 report-title instruction, and finally hardened to "the very first line of your output MUST be `### Market Summary`" with the no-title rule lifted into the system prompt as well as the user prompt. The "Refreshed at <ts>" line under the tabs is the canonical date-stamp.

Tab labels: `Daily Market Report` (was "Summary" → "Daily market summary" → final form) + `News feed` — the qualifier moved INTO the tab label so the operator knows what they're flipping between, even when the page title isn't visible (e.g. on `/performance` where the tab card sits below the position grids).

`timestamp_display()` (Python) and `clientTimestamp()` (JS) use single space between weekday/day/month/time → renders `Sat 25 Apr 07:03 IST | Fri 24 Apr 21:33 EDT`. Banners, refreshed_at stamps, log timestamps all inherit the format.

**Refreshed-at semantics** — the timestamp shown on each tab is the actual content-update time, not the request-handler's wall clock:

- **Market summary** — `_load_market_from_db()` formats `MarketReport.generated_at` (the persisted Postgres timestamp of when the AI wrote the row) via `format_dual_tz()`. Tying the rendered stamp to the SQL column makes "when this content was last updated" unambiguous; if a future code path ever needed to update the persisted `refreshed_at` string independently, the rendered stamp would still reflect the actual content write moment.
- **News feed** — `refreshed_at` reflects the most recent persisted headline's `published_at`, not when we last polled. The operator sees "the freshest news event we have is from <X>" rather than "we polled the upstream feed at <Y>". Falls back to `timestamp_display()` only when no headlines have been persisted yet (cold-start).

`format_dual_tz(dt)` (in `backend/shared/helpers/date_time_utils.py`) is the building block — same compact format as `timestamp_display()` but for an arbitrary datetime, with naive-datetime defensiveness (interpreted as UTC).

---

## Documentation surfaces — five roles

| File | Audience | What's in it |
|---|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | Operator new to the platform | **Concepts in plain English.** What an agent is, what simulation does, what Greeks mean, what the chase engine does. No JSON, no code paths, no API endpoints. |
| [ADMIN_GUIDE.md](ADMIN_GUIDE.md) | Day-to-day operator | **Operations reference.** Exact button labels, condition-tree JSON, API endpoints, config keys, troubleshooting tables. |
| [AGENTS_GUIDE.md](AGENTS_GUIDE.md) | Operator authoring + testing agents | **Extensive testing walkthrough.** Anatomy of an agent · the grammar · fragments · four-stage validation ladder (validate → dry-run → simulator → activate) · lifespan · troubleshooting · copy-paste patterns. |
| [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md) | Operator running sim scenarios | **Hands-on simulator workflow.** Lab page anatomy · seeding modes · move primitives · scenarios · market-state presets · Run-in-Simulator · iteration mode · custom positions · troubleshooting. |
| [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md) | Operator using Claude Code MCP | **LLM-driven agent authoring runbook.** MCP server setup · `.mcp.json` · JWT bootstrap · the 26 tools · confirm-token gate · audit trail. |
| [CLAUDE.md](CLAUDE.md) (this file) | Engineers + AI assistants | **Architecture + design notes.** Code structure, data flow, design rationale, refactoring history. |

Cross-link aggressively — every page on the platform should be findable via at least one of these (USER_GUIDE for the concept, ADMIN_GUIDE for the keystrokes, AGENTS_GUIDE / SIMULATOR_GUIDE for the deep "how to test it" walkthroughs).

---

## InfoHint pattern

Most algo admin pages used to ship a long descriptive paragraph at the top — fine for first-time onboarding but pure noise once the operator knows what the page does. [`frontend/src/lib/InfoHint.svelte`](frontend/src/lib/InfoHint.svelte) replaces those with a small amber `(i)` chip next to the page title; click to toggle an inline popover with the same gradient + amber accent the Settings row info uses. Implemented across `/admin/brokers`, `/admin/derivatives`, `/admin/execution` (all five mode panels), `/admin/settings`. ~30-40 vh saved per page; help text is one click away when needed.

`<InfoHint>` accepts a children snippet so the popover can include HTML / Svelte content. Default `label='i'`; `align='right'` available for header-bar use cases. Component is theme-aligned (no extra CSS in callers).

---

## Disclosure / collapse — two components, two semantics

Two shared visual primitives handle the "expand / collapse" affordance:

| Component | Use for | Persists? | Visual |
|---|---|---|---|
| [`CollapseButton.svelte`](frontend/src/lib/CollapseButton.svelte) | **Card-level** collapse — Dashboard panels, Options analytics cards | Yes — localStorage `ramboq.collapse.<user>.<cardId>` | Chevron SVG, top-right card-header |
| [`DisclosureChevron.svelte`](frontend/src/lib/DisclosureChevron.svelte) | **Row-level** inline disclosure — Agents rows, Fragments rows | No (ephemeral) | `▾ / ▸`, 0.65rem, `#7e97b8`, flex-shrink-0 |

Earlier the row-level triangles were hand-rolled per page with mismatched colours and font sizes (Agents used `#7e97b8 0.65rem`; Fragments used `rgba(251,191,36,0.7) 0.85rem`). One component now = one identity across the workspace.

`<DisclosureChevron open={isOpen} ariaLabel?="…" />` — drop in next to the row's clickable affordance. Accepts an optional `ariaLabel` so screen readers announce expand/collapse intent.

---

## Performance tuning (multi-wave)

**Memoized time formatters** — `formatDualTz` and `clientTimestamp` now cache results per-minute-key. Reduces Intl constructor calls from ~2000 per 3s poll to ~5-10 per page load.

**Store write guards** — `executionMode` store ignores writes when value hasn't changed, preventing downstream chain re-fires on tick bursts.

**Viewport-paused polling** — `ChartWorkspace`, `OptionChainTab`, LogPanel lazy-timers all use `visibleInterval` (pause on `document.hidden`). Reduces background API load 50-70% when operator switches tabs.

**Lazy log tabs** — `LogPanel` System + Sim Ticks pollers don't fire until tab is first activated. No background noise until needed.

**OrderTicket / OrderDepth lifecycle** — `OrderTicket` `suspended` prop, `OrderDepth` `paused` prop pause preflight + quote poll when Chain tab is in focus.

**WebSocket debounce** — `loadOrders()` 250ms debounce on WS burst prevents thrashing when broker fires 50+ postbacks in 1 second.

**Result**: /admin/execution mode pages hover steady at 20-40 reqs/min (was 100+).

---

## Consistency primitives — three new abstractions

**Bucket card sections** — canonical `.bucket-card-{entry,activity,chase,info,data}` classes in `app.css`. Replaces 6 ad-hoc container patterns. ~150 LOC deduplicated.

**CSS custom properties** — 80 new `--algo-amber-*` / `--algo-cyan-*` / `--algo-slate-*` properties. 441 raw `rgba(...)` hex literals → `var(...)` references. Single point of change for palette adjustments.

**AlgoTabs component** — new `<AlgoTabs>` absorbs 5 pre-existing tab-strip implementations (LogPanel, /orders, SymbolPanel, dashboard, research). Canonical `.algo-tab` base in `app.css` eliminating visual drift.

**`.mp-section-label` canonical** — all section headers on performance grids follow the same 0.875rem · slate-600 · normal-weight rule.

---

## Agent editor — every column is editable

[`/agents`](frontend/src/routes/(algo)/agents/+page.svelte)'s inline editor now exposes every `Agent` column that's mutable through the API. Operators can read the agent's full state from one screen without falling back to "edit JSON" workarounds. Fields covered today:

- **Identity** — `name`, `long_name` (3-part `when:… alert:… do:…` operator label), `description`
- **Routing** — `scope`, `schedule`, `cooldown_minutes`, `fire_at_time`
- **Lifespan** — `lifespan_type` (persistent / one_shot / n_fires / until_date), `lifespan_max_fires`, `lifespan_expires_at`
- **Priority / alert hierarchy** — `tier` (labelled **Priority** in the UI per PagerDuty / Opsgenie convention), `topic`, `digest_window_sec`
- **Execution** — `trade_mode` (paper / live), `debounce_minutes`
- **Filtering + quiet hours** — `tags` (CSV input, parsed to list), `blackout_windows` (JSON array of `{start, end}` in IST)
- **Trees** — `conditions` (JSON), `events` (channel checkbox grid), `actions` (JSON + quick-add pills)

Industry analogue: PagerDuty / Opsgenie / Sentry expose every alert-rule column in the editor — hiding fields creates "ghost config" the operator can't account for.

---

## Templates vs Agents — non-overlapping layers

| Layer | Trigger | Scope | When | Example |
|---|---|---|---|---|
| **OrderTemplate** | Order fills | Per-position | Sub-second | TP +80%, SL -20%, OCO |
| **Agent** | 5s poll | Book-wide | Up to 5s + cooldown | Book P&L ≤ -₹50k close all |

Templates ride at the broker (GTT / Forever Order) so they fire offline. Agents need central state across all accounts. Both cohabit: template TP on fill + agent watches the book. Legacy shim ([`_arm_take_profit`](backend/api/routes/orders.py)) wires v1 `target_pct/abs` for pre-template scripts; idempotent vs template attach. UI: `tmpl:#42 ✓` chip in [`OrderCard.svelte`](frontend/src/lib/order/OrderCard.svelte).

---

## Reusable order ticket (`<OrderTicket>`)

A single Svelte component handles every order op the platform needs (open / close / modify / repeat / cancel) across every instrument (EQ / FUT / OPT / commodities). One callsite per page; the ticket renders the right fields per instrument, owns its own validation, depth ladder, and submit lifecycle.

**Files** ([`frontend/src/lib/order/`](frontend/src/lib/order/)):
- `OrderTicket.svelte` — modal shell. **Side toggle** (two-pill: ADD/CLOSE when position open, BUY/SELL otherwise; switches based on `currentQty`), qty + lot meta, order-type pills (MARKET / LIMIT / SL / SL-M), product pills (CNC/MIS for EQ; NRML/MIS for F&O — auto-filtered by parsed `kind`), conditional limit/trigger fields, mode toggle (DRAFT / PAPER / LIVE), validation messages, Cancel / Submit footer. Esc / overlay click / `×` to dismiss. On symbol change, resets `_lots=1` + clears `_lotsTouched` (new chain pick via +CE; skipped when `currentQty` set for close flows).
- `OrderDepth.svelte` — top-5 bid/ask ladder. Polls `GET /api/quote?exchange=…&tradingsymbol=…` every 1.2 s while mounted. Falls back to em-dashes + a small "depth unavailable" hint when the broker call fails.

**Three submit paths**:

| Mode | What happens |
|---|---|
| **DRAFT** | Caller's `onSubmit` callback (no API hit). Caller appends to its local drafts array — typically the [`/admin/derivatives`](frontend/src/routes/(algo)/admin/derivatives/+page.svelte) page's `drafts[]`. |
| **PAPER** | `POST /api/orders/ticket` with `mode: "paper"`. Backend persists an `AlgoOrder` row + registers the order with the prod paper engine via `register_open_order`. The engine's 5-second tick runs the same fill / modify / unfilled lifecycle that agent fires use, driven by real bid/ask via `LiveQuoteSource`. |
| **LIVE** | Same endpoint with `mode: "live"`. Two backend gates fire before any broker call: (1) `is_prod_branch()` — non-prod returns 403; (2) `get_bool('execution.paper_trading_mode', True)` must be `False` — set via the navbar mode dropdown (LIVE entry) which targets `/admin/execution?mode=live`. Both gates pass → `kite.place_order()` tagged `ramboq-ticket`. **No confirm dialog** — the pre-submit margin/cost row above the Submit button already shows exactly what's being committed to; an extra modal just slowed the fast-trading workflow. Backend gates are the only safety net. |

**Account selector** — required for PAPER + LIVE so the operator picks which Kite handle the order routes through; never relying on the backend's silent "first available" fallback. The ticket renders a readonly account display when there's exactly one available, a `<select>` dropdown when there's more than one, and refuses to submit if `_account` is blank. Pre-filled from the calling page's account state when an obvious choice exists (e.g. the operator already filtered to one account in `/admin/derivatives`). The backend enforces the same rule: ticket route returns 400 when account is blank or unknown to it, with no silent first-account fallback.

**Validation** — before any backend call: qty must be a positive multiple of `lotSize` when known (NIFTY 50, BANKNIFTY 15, …), price ≥ 0, trigger ≥ 0, account picked. Backend additionally validates the enum fields (variety / exchange / product / order_type) up-front so Kite's cryptic "Invalid input — 400" never reaches the operator.

**Success feedback** — PAPER + LIVE submits show an inline green `✓ <MODE> order placed · #<order_id>` line inside the modal for 1.4 s before auto-closing. Earlier the modal closed silently and the operator had no idea whether the order landed on the broker.

**Backend** ([`backend/api/routes/orders.py`](backend/api/routes/orders.py)):

| Route | Purpose |
|---|---|
| `POST /api/orders/ticket` | Operator-initiated order. `{mode, side, tradingsymbol, qty, exchange, product, order_type, variety, price, trigger_price, account?}`. Routes by mode. |

**Where the ticket gets opened today**:
- `/admin/derivatives` chain `+CE` / `+PE` / futures pill clicks → ticket pre-filled (DRAFT default).

**Migration plan for other surfaces** (each is now just "add `<OrderTicket>` import + open it on the relevant click"):
- `/orders` row Edit / Cancel / Repeat
- `/agents` fire-confirm
- `/performance`, `/dashboard` row "Square off" / "Sell" / "Top up"
- `/console` `place …` command — replace text-only path with the ticket for explicit confirmation

### Kite postback HMAC validation (Wave A)

`POST /api/orders/postback` (Kite's real-time order status callback) now validates the incoming `checksum` field via HMAC-SHA256 over `order_id + order_timestamp + api_secret` before broadcasting. Mismatched signatures return 401 + WARNING-level log. Multi-account fallback: tries the claimed `user_id` account first, then iterates all loaded accounts to find a match (since Kite postbacks don't always carry a recognisable user_id).

---

## Demo mode — anonymous-on-prod guest session

Prod (`main`) algo pages open to anonymous visitors (real book, masked accounts via `mask_column()`, no broker writes). Gate helper ([`is_demo_request()`](backend/api/auth_guard.py)): sets `connection.state.is_demo = True`. Write endpoints 403 or downgrade (e.g. `POST /orders/ticket mode=live` → `paper`). Admin routes 401. No synthetic data — same code path as public `/performance`.

**Frontend** ([`(algo)/+layout.svelte`](frontend/src/routes/(algo)/+layout.svelte)): predicates gate demo on (`main` + anon). Settings/Brokers/Users nav drop. "Sign In" replaces user pill.

**Badge logic**: DEMO (purple anon), PAPER (paper orders open), SIM (sim active). Navbar breakpoint: `lg:` (1024px), not `md:`. **Branch labels**: show `prod` (raw `main`) or literal name via [`branchLabel()`](frontend/src/lib/stores.js).

---

## Frontend API layer — friendly errors, masked logs

[`frontend/src/lib/api.js`](frontend/src/lib/api.js): single `_request()` wrapper for all endpoints. Transforms: (1) friendly UI message (25-35 char, 5xx → "Server busy", anon 401 → "Sign in required"); (2) masked console log (`Z####, <key>, bearer <token>, <email>` via [`_maskForDemoLog()`](frontend/src/lib/api.js)); (3) one throw path (`error = e.message`). Method shortcuts: `_get / _post / _put / _patch / _del`. 401 only redirects if token existed (guards demo from bounce).

---

## Lab page — chat-driven research via Claude Code + MCP

`/admin/research` persists threads, audit, and token-mint UI for operator-driven LLM workflows. Chat happens in Claude Code (operator's terminal); no paid GenAI beyond Gemini 2.5 Flash for helpers. Full operator runbook: [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md).

**Pages:** Research (threads + transcript) · Drafts (inactive agents from threads) · Audit (mutation forensics, filterable) · Settings (token mint + JWT bootstrap + MCP server inventory).

**MCP server** ([backend/mcp/kite_server.py](backend/mcp/kite_server.py)): FastMCP subprocess. 16 read tools (positions, holdings, quote, ohlcv, news, chain snapshot, macro, agents, threads, audit, server_info) + 2 persist (save_research_thread, save_agent_draft) + 6 gated writes (place/cancel/modify/activate/deactivate/update_agent).

**Confirm-token gate** (60s TTL, single-use, purpose-hash bound): `place | cancel | modify | activate | deactivate | update`. Purpose hash prevents bait-and-switch between mint and redeem. In-process dict; restart clears all.

**Audit table** ([McpAudit](backend/api/models.py)): tool, user_id, args_redacted, result_status, result_summary, request_id, created_at. Daily cleanup via `_task_mcp_audit_cleanup` (default 90-day retention). Telegram ping after success (mode tag + order_id + account + clickable request_id).

**Key gotcha**: Litestar's `@get/@post` decorators replace methods with route-handler objects. Call via `.fn(ctrl, ...)` not directly (learned in Phase 12).

**File map**: MCP=[backend/mcp/kite_server.py](backend/mcp/kite_server.py), tokens=[backend/api/routes/research.py](backend/api/routes/research.py), UI=[frontend/src/routes/(algo)/admin/research/+page.svelte](frontend/src/routes/(algo)/admin/research/+page.svelte).

---

## Investor portal — token-gated public NAV URL

LP-facing read-only surface at `/investor/<token>`. Token IS the credential — no LP login, no password. Operator mints from `/admin` per-user **Portal** button, copies URL, forwards through their own channel (WhatsApp / email). Industry analog: Carta investor magic-link, SS&C/GP-Link share-class URLs.

**Schema** ([`InvestorToken`](backend/api/models.py)):
- `token` — 32-byte `secrets.token_hex` (64-char). Raw, not hashed (token IS the URL slug; possession of URL = access).
- `expires_at` — 90-day default, cap 10y.
- `revoked_at` — nullable; when set, token is dead immediately.
- `last_visit_at` + `visit_count` — operator visibility ("LP last looked 3 weeks ago").
- `note` — admin-supplied label ("WhatsApp to LP 2026-06-23").
- `created_by` — FK users.id for audit.

**Active check**: `revoked_at IS NULL AND expires_at > now()`. UI surfaces three states: ACTIVE (green) / REVOKED (red) / EXPIRED (slate).

**Endpoints** ([`backend/api/routes/investor.py`](backend/api/routes/investor.py)):

| Route | Cap | Notes |
|---|---|---|
| `GET /api/admin/users/{id}/investor-tokens` | `manage_investor_tokens` | List rows — preview only (first 8 chars + `…`), never full token |
| `POST /api/admin/users/{id}/investor-tokens` | `manage_investor_tokens` | Mint. Returns full token + portal URL ONCE (subsequent list calls hide it). Body: `{expires_in_days?, note?}`. |
| `DELETE /api/admin/users/{id}/investor-tokens/{tid}` | `manage_investor_tokens` | Revoke (idempotent). Returns 204. |
| `GET /api/investor/{token}/slice` | none — token in URL | `InvestorSliceResponse` — same math as `/api/nav/me`. |
| `GET /api/investor/{token}/history?days=180` | none — token in URL | Scaled NAV curve. |

**Cap**: `manage_investor_tokens` is admin-only — LP onboarding is a designated activity, not trader/risk/ops.

**Visit tracking**: `_resolve_token()` bumps `last_visit_at` + `visit_count` via a best-effort UPDATE (try/except rollback) so a single page load increments by 2 (slice + history). Surfaced in the admin modal table.

**Frontend separation**: `/investor/[token]/+page.svelte` is a sibling of `(public)` and `(algo)` route groups — inherits only the empty root layout, so no algo navbar / public marketing nav leaks in. Cream + champagne palette matching the marketing site (LPs aren't operators; they shouldn't land on a trading-desk-looking page). `<meta name="robots" content="noindex,nofollow">` so leaked URLs don't end up in search engines.

**Admin modal** ([`(algo)/admin/+page.svelte::openPortal`](frontend/src/routes/(algo)/admin/+page.svelte)): expires_in_days + optional note form → mint → freshly-minted URL appears in green panel with Copy-to-clipboard button → token list with per-row Active/Revoked/Expired pill + last-visit + visit-count + Revoke button. Revoke runs through `ConfirmModal.ask()`; same pattern as every other destructive admin action.

**Math (units model — slice 7N+, replaces v1 static-share)**:

```
units_held(user, t)   = Σ units_delta for events <= t
total_units(t)        = Σ units_held across every LP
nav_per_unit(t)       = firm_nav(t) / total_units(t)
slice(user, t)        = units_held × nav_per_unit
cost_basis(user, t)   = Σ amount (sub+bootstrap) − Σ amount (redemption)
pnl(user, t)          = slice − cost_basis
```

All 4 surfaces (`/api/nav/me`, `/api/nav/me/history`, `/api/investor/{token}/slice` + `/history`, `compute_statement` inside the monthly PDF) route through [`investor_units.py`](backend/api/algo/investor_units.py). The v1 static-share path is gone — DO NOT add `share_pct × firm_nav / 100` math anywhere; use `compute_slice(s, user, firm_nav)` instead.

**Auto-bootstrap** runs at the start of every units compute via `ensure_all_bootstrapped(s)`. For every eligible LP (`is_active=True AND share_pct > 0`) without events, inserts a synthetic `bootstrap` event:
- `units_delta = User.share_pct`
- `amount = User.contribution`
- `nav_per_unit = contribution / share_pct` (or `1.0` when contribution=0 — operator-residual case)
- `event_date = contribution_date → created_at.date() → today` (fallback chain)

When share_pcts sum to 100 across eligible LPs, bootstrap reproduces v1 numbers exactly on day one. When sum != 100 (operator residual implied via low share_pct), units math redistributes proportionally and slices sum to `firm_nav` by construction.

**Day-delta semantics** ([`/api/nav/me`](backend/api/routes/nav.py)): computed as `slice(today) − slice(prior)`, both via the same event set. Subscriptions between snapshots inflate `slice(today)` AND `cost_basis`, so they don't read as P&L on the LP's portal — only true market moves show up as Day Δ.

**Smoke-test invariants** (commit `322f0c22`): bootstrap matches v1 when shares sum to 100 ✓; proportional gain works ✓; mid-period subscription doesn't double-count as P&L ✓; Σ slices == firm_nav by construction ✓.

**Bootstrap correction path**: operator edits `User.contribution` / `share_pct` → deletes the bootstrap event in `/admin` → Portal → Events tab → next compute auto-rebootstraps with corrected columns.

**Security model**: URL is a long-lived API key with the LP as bearer. If leakage suspected, admin revokes + re-mints. Cloudflare in front handles rate limiting / abuse; no per-endpoint limits in code.

---

## Audit log — forensic trail with category dimension

Every mutating event lands in `audit_log` via two paths: HTTP (ASGI middleware) + non-HTTP helper (`write_audit_event`). All writes are fire-and-forget via `asyncio.create_task` so callers pay ZERO latency. Read surface `/admin/audit` is cap-gated to `view_audit` (admin / risk / ops).

**Schema** ([`AuditLog`](backend/api/models.py)) — id, actor_user_id (FK users.id, SET NULL), actor_username, actor_role (all SNAPSHOTTED at write time so later demotions don't rewrite history), action, **category** (nullable for back-compat), method, path, target_type, target_id, status_code, summary, request_id (UUID mirrored in `X-Request-ID` header), client_ip, user_agent, created_at. Indexes on actor / target / category / created_at.

**Two write paths:**

1. **AuditMiddleware** ([`audit.py`](backend/api/audit.py)) — every HTTP request. Skips non-mutating + `_SUPPRESS_PREFIXES`. Captures actor from JWT, status from wrapped `send`, body summary from first 1 KB. Path → category via `_derive_category_from_path()` (prefix-match table). Methods `PUT/PATCH` on `/api/orders/{id}` narrow to `order.modify`; `DELETE` narrows to `order.cancel`.

2. **`write_audit_event(category, action, actor_user_id=None, actor_username='system', actor_role='system', target_type=None, target_id=None, summary=None, status_code=200, request_id=None)`** — public helper for non-HTTP paths. Used by:
   - Broker postback handler (`order.fill` / `order.cancel` / `order.reject`, actor=`broker`)
   - Agent action dispatcher (`agent.action`, actor=`agent:<slug>`; both success + failure)
   - Monthly statement send (`system.statement`, actor=`system`)
   - NAV compute task (`system.nav`, actor=`system`)
   - Sim-mode actions intentionally NOT audited (isolated in sim event log).

**Failed mutations gate** — `audit.log_failed_mutations` setting (default `False`). When ON, middleware also writes 4xx/5xx rows for defect tracking. Toggle off when not actively debugging.

**Category routing table** (extend `_PATH_CATEGORY_RULES` to register a new business surface):

| Path prefix | Category |
|---|---|
| `/api/orders/ticket` | `order.place` |
| `/api/orders/postback` | `order.fill` |
| `/api/orders/basket` | `order.place` |
| `/api/orders/` | `order` (narrows to `order.modify` / `order.cancel` per method) |
| `/api/admin/users/` | `user` |
| `/api/admin/settings` | `config` |
| `/api/admin/brokers` | `config.broker` |
| `/api/admin/grammar/` | `config.grammar` |
| `/api/admin/fragments` | `config.fragment` |
| `/api/admin/hedge-proxies` | `config.hedge` |
| `/api/admin/statements` | `system.statement` |
| `/api/nav/compute` | `system.nav` |
| `/api/agents/` | `agent` |
| `/api/strategies/` | `strategy` |
| (everything else) | `http` |

**UI** — `/admin/audit` carries category filter pills (All / Orders / Agents / Users / Config / System) above the existing column filters. Each pill maps to one or more category strings via comma-separated OR. The Category column in the table tints by bucket prefix (green order, cyan agent, amber user, violet config, slate system).

**Cross-referencing**: every row's `request_id` UUID is mirrored in the response's `X-Request-ID` header. To trace a single operator action end-to-end: copy the request_id from the audit row, grep `api_log_file` for it.

**Performance contract**: middleware uses `asyncio.create_task(_write_audit(...))` — no await. Helper does the same. Failed writes log a warning and drop. Hot-path callers (the postback handler, the agent dispatcher) NEVER block on the audit insert.

**Adding a new audit category**:
1. Add a prefix tuple to `_PATH_CATEGORY_RULES` (HTTP path) OR call `write_audit_event(category='...', ...)` from a non-HTTP path.
2. (Optional) Add a pill in `CATEGORY_PILLS` in [`/admin/audit/+page.svelte`](frontend/src/routes/(algo)/admin/audit/+page.svelte) so the operator can filter to the new bucket.
3. (Optional) Add a `.audit-cat-<prefix>` CSS tint so the bucket has a distinct color.

---

## Postback fan-out — book_changed bus

Single coordinated refresh trigger across every position-derived UI surface. Backend fans invalidation + broadcasts on terminal postback; frontend singleton subscriber drives a Svelte store; every algo page subscribes via `$effect` and refetches its primary loader on increment.

**Backend chain** ([`order_postback`](backend/api/routes/orders.py)) — runs inside the postback handler's existing `_asyncio.create_task` block (zero added latency on broker ack):

```python
invalidate("orders")                       # always
if status in ("COMPLETE", "CANCELLED", "REJECTED", "EXPIRED"):
    invalidate("positions")
    invalidate("holdings")
    broadcast({"event": "book_changed", "account": masked,
               "exchange": ..., "tradingsymbol": ...,
               "reason": status, "ts": int(time()*1000)})
if status == "COMPLETE":
    broadcast({"event": "position_filled", "qty": signed_delta, ...})
```

`position_filled` carries the signed-qty delta for the per-cell optimistic-patch path on Pulse + Performance (preserved). `book_changed` is the broader coordination event that also covers CANCELLED / REJECTED where there's no qty to patch.

**Frontend bus** ([`$lib/data/bookChanged.js`](frontend/src/lib/data/bookChanged.js)):
- Singleton WS subscriber via `createPerformanceSocket`. Started from `(algo)/+layout.svelte::onMount`. Idempotent.
- Listens for `book_changed`, debounces 200ms (basket-order bursts coalesce into one refresh), increments `bookChanged` (monotonic counter store) + sets `lastBookEvent` (latest payload).
- Counter pattern (not payload comparison) — `$effect(() => { const n = $bookChanged; if (n > prev) { prev = n; load(); } })` re-runs trivially on every increment.

**Surfaces wired (every position-derived UI):**

| Page | Loader |
|---|---|
| `/admin/derivatives` | `loadPositions()` + `loadStrategy()` — Snapshot + Legs + Payoff settle together |
| `/dashboard` | `loadHero()` — positions / holdings / events |
| `/pulse` | `loadPulse()` — alongside the existing `position_filled` qty-delta patch |
| `/orders` | `_debouncedLoadOrders()` — symmetric with existing `order_update` hook |
| `/performance` | `loadAll({ fresh: true })` — alongside the existing `position_filled` patch |

**Recipe — wire a new page to the bus** ([copy-paste pattern](frontend/src/routes/(algo)/dashboard/+page.svelte)):

```svelte
import { bookChanged } from '$lib/data/bookChanged';

let _bookCounter = 0;
$effect(() => {
  const n = $bookChanged;
  if (n <= _bookCounter) return;
  _bookCounter = n;
  loadXxx();
});
```

The counter guard prevents re-entry; upstream debounce handles burst coalescing.

**Performance**: backend adds one extra JSON broadcast (~150 bytes wire) per terminal status. At 10 fills/sec that's 1.5 KB/sec total across all WS clients. Frontend debounce keeps loader calls to one per 200ms window per page.

**Troubleshooting**:
- Bus startup logs a `console.warn` if WS init fails — page falls back to its existing pollers (no UX break).
- Cloudflare orange-cloud blocks raw WS upgrades → `webhook.ramboq.com` MUST be grey cloud (DNS only). Same constraint as the existing `/ws/performance` + `/ws/algo` channels.
- Page-header Refresh button always available as manual override regardless of bus state.

---

## History — multi-day orders / trades / funds surface

`/admin/history` is the row-level book-of-record companion to `/admin/audit`'s event log. Three tabs over `algo_orders` + `daily_book`. Cap-gated by `view_audit`.

**Endpoints** ([history.py](backend/api/routes/history.py)):

| Route | Source | Default range | Pagination |
|---|---|---|---|
| `GET /api/admin/history/orders` | `algo_orders` | 30 days | 50/page, cap 500 |
| `GET /api/admin/history/trades` | `daily_book[kind='trades']` | 30 days | 50/page, cap 500 |
| `GET /api/admin/history/funds`  | `daily_book[kind='funds']`  | 90 days | unpaged |

Shared params: `from_date`, `to_date`, `accounts`, `symbols` (comma-separated). Orders adds `status` + `mode`. Funds drops `symbols`.

**Response highlights:**
- Orders: `counts` field is a SQL `GROUP BY status` histogram (no pagination cost).
- Trades: `summary.total_notional` is `Σ qty × avg_cost` across the FILTERED set via `_func.sum()`.
- Funds: `earliest_date` is `MIN(date) WHERE kind='funds'` for the "tracking started X" chip.

**Funds capture** ([_funds_rows](backend/api/algo/daily_snapshot.py)) — new addition to `_task_daily_snapshot` (15:35 IST). Per account, per segment (equity / commodity), one row per day. Re-uses `daily_book` schema to avoid a new table. Column mapping:

| `daily_book` column | Funds semantic |
|---|---|
| `qty`        | `utilised.debits` |
| `avg_cost`   | `available.cash` |
| `ltp`        | `available.opening_balance` |
| `day_pnl`    | `utilised.realised_m2m` |
| `total_pnl`  | `net` |
| `symbol`     | `'__seg__'` sentinel |
| `exchange`   | segment label uppercased |

Idempotent via existing `(date, account, kind, symbol)` unique constraint.

**Frontend** (`/admin/history`) — 3-tab strip. Shared filters: from/to date, accounts list, symbols list. Orders adds Status + Mode. Per-tab summary: Orders status histogram chips, Trades total notional chip, Funds "tracking started X" chip. BUY/SELL pills + status pills mirror audit-log palette. Pagination 50/page on Orders + Trades; Funds unpaged (low cardinality).

**Navbar:** `History` entry in Config group between Statements and Audit.

**Per-row audit drill** ✓ — `algo_orders.request_id` (nullable VARCHAR(36), indexed) captured by `POST /api/orders/ticket` from `request.scope.state.request_id`. `GET /api/admin/audit` accepts `request_id` filter; audit page reads `?request_id=…` URL param on mount + widens since-hours to 90 days. Orders tab on `/admin/history` has an **Audit ↗** column per row → opens `/admin/audit?request_id=<uuid>` pre-filtered. Legacy rows pre-Jun 2026 render em-dash.

**Cashbook Δ on Funds** ✓ — `FundsRow.cash_delta` computed server-side: `HistoryController.list_funds` walks rows O(N), groups by `(account, segment)`, sorts ASC by date, tracks `prior_cash`. UI sign-tints positive green / negative red / em-dash for first row in series.

**Funds backfill** — endpoint + Dhan adapter wired. `POST /api/admin/history/funds/backfill` accepts `{account, from_date, to_date}`. Adapter contract: `Broker.funds_ledger(from_date, to_date) -> list[dict]` returning normalised rows `[{date, segment, cash_available, opening_balance, debits, realised_m2m, net, payload}, ...]`. Endpoint runs SDK call in executor + INSERT…ON CONFLICT DO UPDATE per row into `daily_book`.

Broker matrix:
- **Kite**: no programmatic ledger — always 501.
- **Dhan** ✓: `DhanBroker.funds_ledger` ([dhan.py](backend/shared/brokers/dhan.py)). Probes `get_ledger_report` (v2) / `get_funds_ledger` / `ledger_report` (fork variants), kwarg→positional fallback. Aggregates voucher-level entries per `(voucherdate, segment)`; `_DHAN_SEGMENT_MAP` collapses Dhan exchange codes (NSE_EQ / NSE_FNO / BSE_EQ / BSE_FNO / NSE_CURRENCY → equity, MCX_COMM → commodity).
- **Groww**: adapter wiring pending — same single-file pattern.

**Adapter aggregation note** — Dhan returns voucher-level rows (one per transaction); the adapter buckets per `(voucherdate, segment)`, sums debit+credit, tracks first+last `runbal` as SOD/EOD proxies. `realised_m2m = credits − debits` reads as "net daily cash move" — includes brokerage / STT / charges, NOT pure MTM. Operator UI documents this caveat.

**Idempotency** — backfill uses the same `(date, account, kind, symbol)` unique constraint as live snapshots. Re-running with wider date range upserts existing rows with the canonical voucher-aggregated numbers (intentional preference over the single broker.margins() snapshot from the live capture).

**Remaining limit:**
- Cashbook running-balance tab (separate from the Δ column) — trade-leg attribution to daily cash moves. Would be a 4th tab walking trades + funds snapshots row-by-row.

**Adding a new history surface** (recipe):
1. Pick the source table — extend `daily_book` with a new `kind` value, or add a dedicated table.
2. Add a new endpoint method to `HistoryController` with `cap_guard("view_audit")`.
3. Add a tab + filter row + table in `/admin/history/+page.svelte`.
4. Add an `fetchHistoryXxx` wrapper in `frontend/src/lib/api.js`.
5. If you want WS-driven freshness, wire `bookChanged` into the page's load function.

---

## Order placement latency — preflight + tick cache + PAPER skip

Three perf fixes shipped Jun 2026 to close the operator-reported "order placement deteriorated" complaint. Combined ticket-path savings: **~600ms LIVE, ~1500ms PAPER**.

**1. Preflight runs in parallel** ([`run_preflight`](backend/api/algo/actions.py)):
- Pre-fix: 4 sequential `await loop.run_in_executor` calls (`broker.profile`, `broker.instruments`, `broker.basket_order_margins`, `broker.margins`) → ~800-1200ms total on Kite.
- Now: 4 helper coroutines (`_fetch_profile` / `_fetch_instruments` / `_fetch_basket_margin` / `_fetch_account_margins`) fired via `asyncio.gather`. Wall-time = `max(individual)` ≈ 300ms.
- Each helper handles its own exception → returns None / `(seg_dict, err_str)` tuple. `_fetch_basket_margin` returns the Exception object on failure so the downstream MARGIN_SHORTFALL handler can re-raise into its existing try/except — minimal change to the consumer.

**2. `_TICK_INDEX` dict for O(1) tick lookup** ([`_align_price_to_tick`](backend/api/routes/orders.py)):
- Pre-fix: linear scan through 10-50k instrument rows; ticket route called twice per order (price + trigger) ≈ 100k iterations.
- Now: module-level `_TICK_INDEX: dict[(exchange,symbol)→tick_size]`, built lazily from the instruments cache. `_TICK_INDEX_STAMP` holds the cached response object; identity flip (`resp is not _TICK_INDEX_STAMP`) triggers rebuild on cache refresh.

**3. PAPER skips route-level preflight** ([`ticket_order`](backend/api/routes/orders.py)):
- `PaperTradeEngine.register_open_order` already runs basket_margin internally (REJECTED-vs-OPEN gate, writes broker's error string to `AlgoOrder.detail`). The route-level call was duplicate work.
- PAPER branch of `ticket_order` no longer calls `run_preflight()`.
- LIVE preflight stays — only chance to block before `kite.place_order` fires.

**Don't add new broker calls to `run_preflight` without parallelizing.** Adding a fifth sequential call resurrects the slowdown. Pattern: wrap any new broker fetch in a `_fetch_xxx()` helper coroutine + add it to the `asyncio.gather` tuple + handle its result inline.

---

## Navbar audit — Sandbox + Explore group + Monitor resequence

Operator-requested cleanup Jun 2026.

**Renames:**
- Group `modes` → **`explore`** (old name was vestigial from the sim/paper/live/shadow/replay terminology; mode toggles now live in the navbar dropdown).
- Label `Lab` → **`Sandbox`** (industry-standard term across QuantConnect / Streak / Sensibull; faster recognition for first-time visitors).
- URL `/admin/execution` unchanged — bookmarks + deep links preserved.

**Monitor group resequenced** by daily-trader workflow frequency:

```
new order: Tour · Pulse · Dashboard · Orders · Derivatives · Charts · Automation · Strategies · NAV
```

Orders moved ahead of the analysis surfaces (Derivatives + Charts) since active trading is the primary entry point. Strategies + NAV (attribution / LP / fund views) move to the end — weekly cadence, not minute-by-minute.

**Wiring** ([`(algo)/+layout.svelte`](frontend/src/routes/(algo)/+layout.svelte)):
```js
const GROUP_LABELS = { monitor: 'Monitor', analyze: 'Analyze', explore: 'Explore', build: 'Build', config: 'Config' };
const INLINE_GROUPS = new Set(['monitor', 'analyze', 'explore']);
```

`INLINE_GROUPS` controls which groups render inline in desktop nav; the rest collapse to dropdown triggers. Mobile drawer shows every group with a `GROUP_LABELS` caption.

**Adding a new nav entry**: append a `{href, label, group}` row to `_algoLinksAll`. Group must be one of `monitor / analyze / explore / build / config`. Optional `adminOnly: true` hides from demo. Optional `branches: ['main' | 'dev']` restricts to one deploy branch.

---

## Refactoring Notes

**Day P&L formula** (commits b95ccd79–ba9cf39c): Decomposed intraday (not naive `(LTP−close)×qty`). Positions: `overnight_qty × (LTP − prev_close) + day_buy/sell legs`. Holdings: `broker.pnl − (close − cost) × opening_qty`. **MCX guard**: apply lot_size multiplier to intraday qty too (pre-fix: ₹61k phantom GOLDM due to unit mismatch). **Always verify qty units in P&L edits.**

Regex validators: pre-compile to module-level (currently per-call). Broker calls: concurrent via `ThreadPoolExecutor`. Async offload: paper + sim use `run_in_executor` to keep sync HTTP off event loop.

**Log files** (both envs relative `.log/`): `hook_debug.log` (deploy), `api_log_file` (5MB × 5), `api_error_file` (stderr). `notify_on_startup` differs per env (preserved on deploy).

---

## Common Tasks — Where to Make Changes

| Task | Files to edit |
|---|---|
| Add a new page | Create SvelteKit route under `frontend/src/routes/<newpage>/` and add nav entry in `+layout.svelte` |
| Change page content (text, FAQs, etc.) | `backend/config/frontend_config.yaml` |
| Change AI market report prompt | `backend/config/frontend_config.yaml` — `genai_system_msg`, `genai_user_msg`, `genai_temperature`, `genai_max_tokens`, `genai_model` |
| Change connection retry behaviour | `backend/config/backend_config.yaml` — `retry_count`, `conn_reset_hours` |
| Change log verbosity | `backend/config/backend_config.yaml` — `file_log_level`, `error_log_level`, `console_log_level` |
| Add a new broker account | `backend/config/secrets.yaml` — add entry under `kite_accounts` |
| Change deploy branch routing | `webhook/dispatch.sh` — the `if/elif/else`; copy to server after changes: `sudo cp /opt/ramboq/webhook/dispatch.sh /etc/webhook/dispatch.sh` |
| Change browser tab title or SEO meta tags | `frontend/src/app.html` and per-route `<svelte:head>` sections |
| Change footer text | `backend/config/frontend_config.yaml` — `footer_name`, `footer_text2`, `footer_mobile_text3`, `footer_desktop_text3` |
| Open the chart for any symbol from any surface | Click the cyan chart-icon button — opens `<ChartModal>` via the unified `/charts` workspace. Available on `/orders` entry + rows, `/admin/derivatives` Candidates, `/performance` symbol cells (ag-Grid cellRenderer). |
| Change a loss threshold | Edit the corresponding `loss-*` agent from the `/agents` page (its condition tree's `value` is the threshold). Engine-wide knobs stay in `backend/config/backend_config.yaml` under `alert_cooldown_minutes`, `alert_rate_window_min`, `alert_baseline_offset_min`, `alert_suppress_delta_abs/_pct`. |
| Change alert recipients | `backend/config/secrets.yaml` on server — `alert_emails`, `telegram_chat_id` |
| Enable/disable deploy notification | `backend/config/backend_config.yaml` on server — `notify_on_startup` (True=dev, False=prod) |
| Add/change market segment hours | `backend/config/backend_config.yaml` — `market_segments` block |
| Change open/close summary timing | `backend/config/backend_config.yaml` — `open_summary_offset_minutes`, `close_summary_offset_minutes` |
| Add/change order-entry command tokens | `backend/config/grammars/orders.yaml` (shared source; frontend picks it up via symlink + `?raw` import) |
| Toggle a built-in agent's default status | `backend/config/agents.yaml` — `seed_agents()` will force built-in agents to match YAML `status` on startup; user-edited conditions/cooldown/events/actions are preserved |
| Add a new MCP tool | [backend/mcp/kite_server.py](backend/mcp/kite_server.py) — add a `@app.tool()` function. Update the TOOLS const in [admin/research/+page.svelte](frontend/src/routes/(algo)/admin/research/+page.svelte) so the Settings-tab inventory stays in sync. New gated writes need a new `_purpose_hash_*` + a new `kind` in the mint endpoint. Read-only tools just need the tool function + a thin HTTP proxy to an existing route. |
| Tune MCP audit retention | `/admin/settings` → `mcp.audit_retention_days` (default 90; 0 disables) |
| Update macro snapshot data | Edit `macros:` block in `backend/config/backend_config.yaml` on server. The deploy script preserves operator edits across deploys (same pattern as `cap_in_dev`). |
