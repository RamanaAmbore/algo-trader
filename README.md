# RamboQuant Analytics

A production web application for **RamboQuant Analytics LLP** at [ramboq.com](https://ramboq.com). The app provides portfolio performance tracking, AI market updates, partner management, investment information, and real-time notifications.

> **New here?** Two reading orders depending on what you're looking for:
> - **[USER_GUIDE.md](docs/guides/USER_GUIDE.md)** — concepts in plain English. What an agent is, what simulation does, how the chase engine works, what Greeks mean. Start here if you've never used a rules-based trading platform.
> - **[ADMIN_GUIDE.md](docs/guides/ADMIN_GUIDE.md)** — operations reference. Exact button labels, JSON shapes, API endpoints, config keys. Reach for this when you need step-by-step instructions on a specific task.

## 🏁 Headline feature — Simulator-backed Agent framework

RamboQuant ships a production risk / alerting / auto-action engine (**Agents**) paired with a **Market Simulator** that exercises the same engine against fabricated prices. The two together form the core of the admin platform:

- **Declarative agents.** Every loss / risk / auto-close rule is a row in the `agents` table: a condition tree of `metric / scope / operator / value` leaves combined via `all / any / not`, plus notify channels (Telegram / email / log / ws) and actions (place_order, close_position, chase_close_positions, cancel_all_orders, …). Inline editor + live graphical tree preview at [/agents](https://dev.ramboq.com/agents).
- **Auto-synthesized sim scenarios.** Press **Run in Simulator** on any agent row — the simulator builds a targeted scenario directly from that agent's condition tree (no hand-written YAML) and dry-fires it. Works for every supported metric (`pnl`, `pnl_pct`, `pnl_rate_abs`, `pnl_rate_pct`, `cash`, `avail_margin`). Holdings-metric agents (`day_pct`, `day_rate_*`) are deliberately skipped — those validate only against live data.
- **Per-symbol price-driven simulator with spread-aware chase engine.** Positions-only by design. Ships 6 book-wide stress scenarios (`generic-crash` / `-euphoria`, `extreme-crash` / `-euphoria`, `wild-swings`, `random-walk`) + 7 market-state presets (`pre_open`, `at_open`, `mid_session`, `pre_close`, `at_close`, `post_close`, `expiry_day`) so time-aware agents fire realistically. Every position derives `bid` / `ask` from a tunable spread; the chase engine re-quotes open orders each tick and marks them `FILLED` when the market crosses or `UNFILLED` after `simulator.chase_max_attempts`. Filled positions are removed from the book so the operator watches it shrink live. [`ADMIN_GUIDE.md`](docs/guides/ADMIN_GUIDE.md) walks the whole flow.
- **Same agent engine in sim and live.** No code branches in the hot path — the engine reads a `sim_mode` flag off context and tags every downstream artefact (`[SIM]` in logs, `SIM` pill in UI, `AlgoOrder.mode='sim'`, `agent_events.sim_mode=TRUE`, `SIMULATOR` in Telegram / email subjects + red banner). Real and simulated fires can't be confused.
- **Paper-trade order log.** Sim actions record an `AlgoOrder` row with `initial_price = side-aware limit (SELL→bid, BUY→ask)`, then chase progress bumps `attempts` + rewrites `detail` live until the order flips to `FILLED` / `UNFILLED`. The LogPanel's Order tab colours each row by terminal state (FILLED green / UNFILLED red / OPEN amber) with a `chase: #N` chip — operators see the chase in action.
- **DB-backed Settings.** Alert thresholds, cadences, baseline gates, notification toggles, etc. live in a `settings` table and can be tuned at runtime from [/admin/settings](https://dev.ramboq.com/admin/settings) without a deploy. Seeder auto-prunes retired keys across deploys.
- **Five execution modes, branch-gated.** SIM (fabricated quotes + paper engine, driver-fed scenarios), PAPER (real quotes + paper engine fed by `LiveQuoteSource`, with `basket_margin` validation per order — the prod default), LIVE (real broker placement), SHADOW (capture `kite.place_order` payload + validate via `basket_margin` without executing), REPLAY (historical OHLCV through the paper engine for backtesting). Mode is selected via the navbar pill / dropdown; `execution.paper_trading_mode` and `execution.shadow_mode` settings persist the operator's choice. The chase / fill / modify / unfilled lifecycle is a single `PaperTradeEngine` shared across SIM and PAPER — only the quote source differs. Alerts are auto-tagged `[PAPER]` or nothing in Telegram + email subjects so the operator knows at a glance whether a fired agent caused a real broker order.

## Architecture

**Litestar API + SvelteKit frontend.**

| Layer | Technology |
|---|---|
| Backend API | Litestar 2.x (ASGI) + msgspec |
| ASGI server | Uvicorn |
| Frontend | SvelteKit + TailwindCSS + AG Grid |
| Database | PostgreSQL 17 via SQLAlchemy 2.x + asyncpg |
| Background | Litestar async tasks |
| Agent engine | DB-backed grammar catalog (`grammar_tokens`) + tree evaluator |
| DataFrame | Polars (API aggregation) + pandas (broker layer) |
| Auth | JWT HS256 + PBKDF2-SHA256 passwords |
| Broker | Zerodha Kite API |
| AI | Gemini 2.5 Flash (Google GenAI) |
| Alerts | Telegram Bot API + SMTP email |
| Deploy | GitHub Webhook + systemd + nginx |

## Features

- **Portfolio tracking**: Holdings, positions, funds — per-account and combined views via AG Grid
- **AI market reports**: Gemini 2.5 Flash with Google Search grounding, cached daily
- **Real-time updates**: WebSocket push from background tasks to connected browsers
- **Agent framework**: Every loss/risk rule is a declarative Agent row — grammar tree of `metric / scope / op / value` leaves combined by `all / any / not`. Inline editor with a live graphical tree preview on the `/agents` page; token catalog is editable live at `/admin/tokens`.
- **Market Simulator**: Per-symbol price-driven simulator (`/admin/simulator`) that replays scripted scenarios against a scripted book or your live Kite positions. Drives the same agent engine as production — alerts, Telegram/email dispatch, paper-traded orders — all tagged `SIMULATOR` so real vs simulated fires can't be confused.
- **Expiry auto-close**: Adaptive limit-order chase engine closes ITM option positions before expiry.
- **Partner management**: Registration (pending admin approval), KYC, contribution tracking, profit share
- **Admin dashboard**: User management (create, approve/reject, edit all partner fields), log viewer, token catalog CRUD
- **Notifications**: Telegram + email for market open/close summaries and every agent alert
- **Segment-aware**: Equity (NSE/NFO 09:15–15:30) and Commodity (MCX 09:00–23:30) handled independently
- **Multi-environment**: prod (ramboq.com), dev (dev.ramboq.com)

---

## Database

PostgreSQL 17 on the server. Two databases:

| Database | Branch | Purpose |
|---|---|---|
| `ramboq` | `main` | Production |
| `ramboq_dev` | any other | Development |

Selected automatically by `deploy_branch` in `backend_config.yaml`.

**Credentials** in `secrets.yaml`:
```yaml
db_user: rambo_admin
db_password: <password>
db_host: localhost     # optional, default localhost
db_port: 5432          # optional, default 5432
```

**Users table** (30 columns):
- Identity: `id`, `account_id` (auto-generated `rambo-XXXXXX`), `username`, `password_hash`, `role` (admin/partner), `display_name`, `email`, `phone`
- KYC: `pan`, `aadhaar_last4`, `date_of_birth`, `kyc_verified`
- Address: `address_line1/2`, `city`, `state`, `pincode`
- Investment: `contribution`, `contribution_date`, `share_pct`
- Bank: `bank_name`, `bank_account`, `bank_ifsc`
- Nominee: `nominee_name`, `nominee_relation`, `nominee_phone`
- Status: `is_approved`, `is_active`, `join_date`, `notes`, `created_at`, `updated_at`

**Server setup** (one-time):
```sql
-- As postgres superuser
CREATE DATABASE ramboq OWNER rambo_admin;
CREATE DATABASE ramboq_dev OWNER rambo_admin;
```

---

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/auth/login` | — | JWT token (403 if unapproved partner) |
| POST | `/api/auth/register` | — | Register partner (pending admin approval) |
| GET | `/api/auth/me` | JWT | User profile |
| GET | `/api/holdings/` | — | Holdings rows + summary |
| GET | `/api/positions/` | — | Positions rows + summary |
| GET | `/api/funds/` | — | Funds per account |
| GET | `/api/market/` | — | AI market report |
| GET | `/api/config/post` | — | Insights content |
| GET | `/api/config/about` | — | About content |
| POST | `/api/admin/users` | admin | Create user (pre-approved) |
| PUT | `/api/admin/users/{username}/approve` | admin | Approve pending user |
| PUT | `/api/admin/users/{username}/reject` | admin | Reject user |
| PUT | `/api/admin/users/{username}` | admin | Update partner fields |
| GET | `/api/admin/users` | admin | List all users |
| DELETE | `/api/admin/users/{username}` | admin | Deactivate user |
| GET | `/api/admin/logs` | admin | Tail log file |
| POST | `/api/admin/exec` | admin | Run shell command |
| GET | `/api/agents/` | admin | List all agents |
| POST | `/api/agents/` | admin | Create custom agent |
| GET | `/api/agents/{slug}` | admin | Single agent detail |
| PUT | `/api/agents/{slug}` | admin | Update agent (name, conditions, events, actions, scope, schedule, cooldown) |
| PUT | `/api/agents/{slug}/activate` / `/deactivate` | admin | Toggle agent on/off |
| DELETE | `/api/agents/{slug}` | admin | Delete custom agent (system rows rejected) |
| POST | `/api/agents/validate-condition` | admin | Dry-check a condition tree against the grammar registry |
| GET | `/api/agents/{slug}/events` | admin | Per-agent alert history |
| GET | `/api/agents/events/recent` | admin | Most recent alerts across all agents |
| GET | `/api/admin/grammar/tokens` | admin | List grammar catalog (metrics / scopes / operators / channels / templates / actions) |
| POST / PATCH / DELETE | `/api/admin/grammar/tokens[/{id}]` | admin | CRUD custom tokens (system tokens are toggle-only) |
| POST | `/api/admin/grammar/reload` | admin | Hot-rebuild the grammar registry after edits |
| WS | `/ws/performance` | — | Live push for portfolio |
| WS | `/ws/algo` | — | Live push for agent alerts + state transitions |

All responses include `refreshed_at` in IST|EST dual-timezone format.

---

## Project Structure

```
backend/
  api/
    app.py              — Litestar app; on_startup: init_db + background tasks
    database.py         — PostgreSQL via asyncpg; DB selected by deploy_branch
    models.py           — User ORM (30 columns: personal, KYC, address, investment, bank, nominee)
    schemas.py          — msgspec.Struct response models
    background.py       — Async scheduler: market warm, performance refresh, close summaries
    cache.py            — In-process TTL cache with per-key locking
    auth_guard.py       — jwt_guard + admin_guard
    routes/
      auth.py           — Login, register, me, logout
      admin.py          — Create/approve/reject/update users, logs, exec
      holdings.py       — Polars aggregation
      positions.py      — Polars aggregation
      funds.py          — Polars aggregation
      market.py         — Gemini with 1h TTL cache
      config.py         — Post/about content
      orders.py         — Order CRUD (protected)
      ws.py             — WebSocket fan-out
    algo/
      agent_engine.py    — `run_cycle` evaluation loop, BUILTIN_AGENTS seed (14 loss-* rules)
      agent_evaluator.py — Pure tree walker for `all/any/not` + metric/scope/op/value leaves
      grammar.py         — SYSTEM_TOKENS + resolver functions
      grammar_registry.py — In-process dispatch table; rebuilt from DB on `/grammar/reload`
      events.py          — Channel dispatcher (Telegram / email / WebSocket / log) + `EvalResult`
      actions.py         — Action handler stubs (place_order, chase_close_positions, …)
      expiry.py          — Expiry-day ITM auto-close engine
      chase.py           — Adaptive limit-order chase primitives
    routes/
      agents.py          — Agent CRUD + /validate-condition
      grammar.py         — Grammar token CRUD + /reload
      algo.py            — /ws/algo fan-out
  shared/helpers/       — broker_apis, connections, decorators, alert_utils, genai_api, summarise, ...
  scripts/              — One-off admin / maintenance scripts
  config/               — backend_config.yaml, frontend_config.yaml, constants.yaml, secrets.yaml
  requirements.txt      — Core dependencies
  requirements-api.txt  — API-specific dependencies
  run_api.sh            — API entrypoint (cd to repo root, then uvicorn)
  pyproject.toml        — ramboq-backend package

frontend/
  src/
    app.css           — AG Grid theme, buttons, form fields
    lib/
      api.js          — REST + admin helpers with JWT auth
      stores.js       — authStore + dataCache (stale-while-revalidate)
      ws.js           — Auto-reconnect WebSocket
    routes/
      +layout.svelte  — Responsive nav, role-based links
      performance/    — AG Grid: per-account + combined grids, URL ?tab= sync
      market/         — Markdown renderer + timestamp
      post/           — Insights with timestamp
      faq/            — Accordion + Mermaid diagrams
      about/          — Static content
      contact/        — Contact form
      signin/         — Sign In / Register (name, email, phone, PAN)
      admin/            — User management (create, approve, edit all fields)
      admin/tokens/     — Agent Tokens page (three-tab condition/notify/action CRUD, Reload Registry)
      admin/simulator/  — Market simulator control plane (scenarios + live-book seed + tick stream)
      agents/           — Agents page — grouped rows, click to expand, edit inline with live tree preview
      portfolio/        — Partner contribution info

webhook/              — deploy.sh, dispatch.sh, service files, hooks.json
```

---

## Running Locally

```bash
# Install dependencies
pip install -r backend/requirements.txt -r backend/requirements-api.txt

# Start API (requires PostgreSQL — use server or local PostgreSQL)
bash backend/run_api.sh

# Start frontend dev server
cd frontend && npm install && npm run dev
# open http://localhost:5173
```

**Dependencies (`requirements-api.txt`):**
```
litestar[standard]~=2.12
uvicorn[standard]~=0.34
polars>=1.0
httpx~=0.28
PyJWT~=2.10
SQLAlchemy[asyncio]~=2.0
asyncpg~=0.30
```

---

## Deployment

Two environments, auto-deployed on `git push` via GitHub webhook:

| Environment | Branch | Server path | Port | Domain |
|---|---|---|---|---|
| Production | `main` | `/opt/ramboq` | 8502 | ramboq.com |
| Development | non-main | `/opt/ramboq_dev` | 8503 | dev.ramboq.com |

**Flow**: `git push` → GitHub webhook → nginx → webhook listener (port 9001) → `dispatch.sh` → `deploy.sh` → git pull + pip install + systemctl restart

Both branches (`main`, `dev`) are permanent and kept in sync.

---

## Configuration

### `secrets.yaml` (gitignored — hand-place on server)
```yaml
# SMTP
smtp_server: smtp.hostinger.com
smtp_port: 587
smtp_user_name: RamboQuant Team
smtp_user: <email>
smtp_pass: <password>

# Broker
kite_accounts:
  <user_id>:
    password: ...
    totp_token: ...
    api_key: ...
    api_secret: ...
kite_login_url: https://kite.zerodha.com/api/login
kite_twofa_url: https://kite.zerodha.com/api/twofa

# Database
db_user: rambo_admin
db_password: <password>

# Auth
cookie_secret: <random-string>

# AI
gemini_api_key: <key>

# Notifications
telegram_bot_token: <token>
telegram_chat_id: <group-id>
alert_emails:
  - <email>
```

### `backend_config.yaml` (tracked — server-tuned keys preserved across deploys)
Top-level keys include: `deploy_branch`, `cap_in_dev` (dict of per-capability toggles: `genai`, `telegram`, `mail`, `notify_on_deploy`, `market_feed`), `performance_refresh_interval`, `open_summary_offset_minutes`, `close_summary_offset_minutes`, `market_segments`, `genai_thinking_budget`.

Agent-engine knobs (all `alert_*` keys survive deploys via a `startswith("alert_")` preserve rule):
- `alert_rate_window_min` — minutes of history used to compute ΔP&L/Δmin
- `alert_baseline_offset_min` — rate-based agents stay silent for this many minutes after session start
- `alert_cooldown_minutes` — minimum time between re-fires of the same agent
- `alert_suppress_delta_abs` / `alert_suppress_delta_pct` — re-fire also requires the loss to have deepened by this much

Per-rule loss thresholds live on each agent row's condition tree (edit via the `/algo` page), not in this file.

---

## Background Processing

All scheduled work runs inside the API process as asyncio tasks (Litestar `on_startup`). There is no separate worker to start.

Tasks:
- **Market cache warm** — 08:30 IST daily; pre-fetches the Gemini report
- **Performance refresh** — every 5 min during market hours; fans out to WebSocket + caches
- **Open / close summaries** — per segment, 15 min after the segment transitions
- **Agent engine `run_cycle`** — every performance tick; evaluates each active agent's condition tree
- **Expiry auto-close** — 09:20 IST daily; scans for ITM options approaching expiry

---

## Auth Flow

- **Self-registration**: Partner registers (name, email, phone, PAN, password) → account pending → admin approves → partner can sign in
- **Admin-created**: Admin creates user via Users page → sets password, contribution, share % → user is pre-approved → admin shares password securely
- **Auto-generated account ID**: Each user gets a unique `rambo-XXXXXX` identifier
- **Roles**: `admin` (full access + Users page), `partner` (portfolio view)

---

## Agent Framework

Ramboq's risk + automation engine is built around four words:

| Word | Meaning |
|---|---|
| **Agent** | A declarative rule row (`agents` table) — `condition + notify + actions + scope + schedule + cooldown`. |
| **Alert** | The runtime event an agent emits when its condition fires (logged to `agent_events`). |
| **Notify** | A delivery channel (`telegram / email / websocket / log`). |
| **Action** | A side-effect the alert invokes (`place_order`, `chase_close_positions`, `deactivate_agent`, …). |

### Condition grammar

Conditions are JSON trees. Leaves reference the grammar catalog:

```
leaf       ::=  { "metric": <metric-token>,
                  "scope":  <scope-token>,
                  "op":     <op-token>,
                  "value":  <literal> }

condition  ::=  leaf
             |  { "all": [condition, ...] }   AND
             |  { "any": [condition, ...] }   OR
             |  { "not": condition }          NOT
```

Example — *"any account's positions pnl is ≤ −2% of that account's used margin"*:
```json
{ "metric": "pnl_pct", "scope": "positions.any_acct", "op": "<=", "value": -2.0 }
```

### Token catalog (`grammar_tokens`)

Every metric, scope, operator, channel, template, and action type is a row in `grammar_tokens`. Adding a new capability is **one DB row + one Python function** at the row's `resolver` dotted path — no engine or schema changes. CRUD via `/admin/tokens` (UI label "Tokens") or `POST /api/admin/grammar/tokens` (backend keeps the compiler-theory "grammar" namespace because the data model IS a grammar), then hit **Reload Registry** to pick it up hot.

System tokens (`is_system=True`) ship with code and are seeded on every boot — operators can toggle `is_active` but not delete them. Custom tokens support full CRUD.

### Seeded loss agents

14 loss/risk agents are seeded active in `BUILTIN_AGENTS`:
- **Static floors** — per-account + total variants for holdings %, positions %, positions ₹
- **Rate of change** — same scopes for ΔP&L/Δmin (₹ and %/min rules)
- **Funds** — `cash < 0`, `avail_margin < 0`

Each agent is editable from the `/algo` page: click the row to expand, click Edit to morph it into an inline form with a live graphical tree preview. Save or Cancel reverts the row to the normal expanded view.

### Notify + Action

The `events` field lists delivery channels; default seed is `[telegram, email, log]`. The `actions` field is a list of action invocations; stubs are in place for every action type, real broker wiring is activated per-handler as each is promoted out of stub mode.

---

## Alerts and Notifications

| Event | Telegram | Email |
|---|---|---|
| Market open | `Open — Equity/Commodity` | `RamboQuant Open:` |
| Agent fire | `Agent` | `RamboQuant Agent:` |
| Market close | `Close` | `RamboQuant Close:` |
| Deploy | `Deploy OK` | `RamboQuant Deploy OK:` |

Timestamps in dual format: `Mon, March 30, 2026, 09:30 AM IST | Mon, March 30, 2026, 10:00 PM EDT`

---

## Security

- `secrets.yaml` is gitignored — never committed
- JWT tokens expire after 8 hours
- Passwords hashed with PBKDF2-SHA256 (260k iterations)
- Partner accounts require admin approval before login
- Admin endpoints protected by `admin_guard`
- PostgreSQL credentials in secrets.yaml only
- Webhook validated with HMAC-SHA256 signature
