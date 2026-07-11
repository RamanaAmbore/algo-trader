# RamboQuant — Migration Plan: Litestar + SvelteKit

This document tracks the phased migration to a proper frontend/backend split.
The goal is zero downtime — each phase is independently deployable.

---

## Target Stack

| Layer | Technology | Reason |
|---|---|---|
| **Backend API** | Litestar 2.x (ASGI) | Native async, fast DI, WebSocket, OpenAPI |
| **ASGI server** | Uvicorn | Standard, battle-tested |
| **Background jobs** | Litestar async tasks (primary) + ARQ/Redis (optional) | In-process async; ARQ available for multi-process setups |
| **Serialisation** | msgspec.Struct | ~10× faster than pydantic; native Litestar support |
| **DataFrame** | Polars (API routes) + pandas (broker/alert layer) | Polars for aggregation speed; pandas where broker APIs require it |
| **Database** | PostgreSQL 17 via SQLAlchemy 2.x + asyncpg | Two databases: `ramboq` (prod) / `ramboq_dev` (dev), selected by `deploy_branch` |
| **Frontend** | SvelteKit | Reactive, small bundle, no virtual DOM overhead |
| **Styling** | TailwindCSS | Responsive utility-first CSS |
| **Data grid** | AG Grid Community | High-performance financial tables |
| **Caching** | In-process TTL cache + stale-while-revalidate (frontend) | No external cache dependency |
| **Real-time push** | WebSockets (Litestar → SvelteKit) | Live data without polling |
| **Auth** | JWT (HS256) + PBKDF2-SHA256 passwords | Users in SQLAlchemy DB |

---

## Phase 1 — API Layer ✅

**Goal:** Litestar API validates API contract and broker connectivity.

```
backend/
  api/
    app.py              — Litestar app, CORS config, OpenAPI (Scalar UI at /schema)
    schemas.py          — Pydantic v2 response models
    routes/
      holdings.py       — GET /api/holdings/
      positions.py      — GET /api/positions/
      funds.py          — GET /api/funds/
      market.py         — GET /api/market/
      ws.py             — WS  /ws/performance  (placeholder)
  workers/
    refresh_worker.py   — ARQ worker; cron-style background refresh
  requirements-api.txt  — litestar, uvicorn, redis, arq, httpx
  run_api.sh            — starts Litestar on port 8000 (cd to repo root)
  run_worker.sh         — starts ARQ worker (requires Redis)
```

---

## Phase 2 — Redis pub/sub + WebSocket push ✅

**Goal:** ARQ worker publishes refreshed data to Redis. Litestar WebSocket handler
fans out to connected clients. SvelteKit frontend subscribes via WebSocket.

```
backend/api/
  app.py              — Redis pub/sub listener background task
  routes/
    ws.py             — WS /ws/performance — fan-out via per-connection asyncio.Queue
    auth.py           — POST /api/auth/login, POST /api/auth/logout (JWT, stub)

backend/workers/
  refresh_worker.py   — Publishes JSON to Redis 'performance:update' after each refresh

frontend/             — SvelteKit app
  src/
    lib/
      api.js          — REST helpers
      ws.js           — createPerformanceSocket() — auto-reconnect WebSocket client
    routes/
      +layout.svelte  — Nav bar, footer, responsive shell
      performance/    — AG Grid tables + live WS updates
      market/         — AI market report page
```

---

## Phase 3 — Full SvelteKit frontend ✅

**Goal:** SvelteKit as the primary UI with all pages ported.

Pages: About, Market, Performance, FAQ, Insights, Contact, Sign In (with Register tab), Portfolio.

---

## Phase 4 — Performance + Auth + Background (current) ✅

**Goal:** Replace pydantic with msgspec, add polars for fast aggregation, move background
refresh into Litestar async tasks, add SQLAlchemy user management, AG Grid polish.

### What changed

**Backend:**

```
backend/api/
  schemas.py          — msgspec.Struct (was pydantic BaseModel) — ~10× faster serialisation
  database.py         — NEW: SQLAlchemy async engine + session factory + init_db()
  models.py           — NEW: User ORM model (id, username, password_hash, role, display_name,
                         contribution, account, is_active, created_at, updated_at)
  background.py       — NEW: Litestar-integrated async background scheduler
                         Three tasks: _task_market, _task_performance, _task_close
                         Blocking broker calls in ThreadPoolExecutor
                         Calls broadcast() + invalidate_all() directly (no Redis needed)
  cache.py            — In-process TTL cache (unchanged)
  auth_guard.py       — jwt_guard + admin_guard (unchanged)
  app.py              — on_startup: init_db() + bg_startup(); removed Redis listener
  database.py         — PostgreSQL via asyncpg; ramboq (prod) / ramboq_dev (dev)
  models.py           — User ORM with full partner fields (30 columns)
  routes/
    holdings.py       — polars for aggregation (broker still returns pandas)
    positions.py      — polars for aggregation
    funds.py          — polars for aggregation
    auth.py           — Login (blocks unapproved), register (pending approval), me, logout
    admin.py          — Create user, approve/reject, update all partner fields, logs, exec
    market.py         — TTL-cached Gemini response; refreshed_at in IST|EST format
    config.py         — PostResponse with refreshed_at timestamp
    orders.py         — (unchanged)
    ws.py             — (unchanged)
```

**Frontend:**

```
frontend/src/
  app.css             — AG Grid: flat (no shadow), mermaid-teal header (#d0e0e0), #315062 text,
                         sans-serif font, btn-primary matches AG Grid header colour
  routes/
    +layout.svelte    — Admin sees "Users" nav link; Console/Orders temporarily hidden
    performance/      — Per-account individual grids + combined "All" grid; smooth transaction updates
                         URL synced: ?tab=holdings|positions|funds; white card wrapper
    market/           — Timestamp left-aligned with IST|EST format; white card wrapper
    post/             — Timestamp left-aligned; white card wrapper
    faq/              — White card wrapper; accordion + mermaid diagrams
    signin/           — Register collects name, email, phone, PAN; shows "pending approval" notice
    admin/            — User management: create user, approve/reject, edit all partner fields
                         (personal, address, investment, bank, nominee, notes)
```

**Dependencies (`requirements-api.txt`):**

```
litestar[standard]~=2.12
uvicorn[standard]~=0.34
redis~=5.2              # optional — for ARQ worker mode
arq~=0.26               # optional — for ARQ worker mode
polars>=1.0             # fast DataFrame aggregation in API routes
httpx~=0.28
PyJWT~=2.10
SQLAlchemy[asyncio]~=2.0
asyncpg~=0.30           # PostgreSQL async driver
```

### How to run

```bash
# On server — Litestar API (includes background tasks + DB init)
bash backend/run_api.sh

# SvelteKit dev server
cd frontend && npm install && npm run dev

# Optional: ARQ worker (alternative to Litestar background tasks)
docker run -d -p 6379:6379 redis:alpine
bash backend/run_worker.sh
```

### API endpoints (Phase 4)

| Method | Path | Auth | Response |
|---|---|---|---|
| POST | `/api/auth/login` | — | JWT token + user info (403 if unapproved) |
| POST | `/api/auth/register` | — | JWT token (partner, pending approval) |
| GET | `/api/auth/me` | JWT | User profile |
| POST | `/api/auth/logout` | — | Stateless ack |
| GET | `/api/holdings/` | — | Holdings rows + summary + refreshed_at |
| GET | `/api/positions/` | — | Positions rows + summary + refreshed_at |
| GET | `/api/funds/` | — | Funds rows + refreshed_at |
| GET | `/api/market/` | — | Market report + refreshed_at |
| GET | `/api/config/post` | — | Insights content + refreshed_at |
| GET | `/api/config/about` | — | About content + refreshed_at |
| POST | `/api/admin/users` | admin | Create user (pre-approved) |
| PUT | `/api/admin/users/{username}/approve` | admin | Approve pending user |
| PUT | `/api/admin/users/{username}/reject` | admin | Reject (deactivate) user |
| PUT | `/api/admin/users/{username}` | admin | Update all partner fields |
| GET | `/api/admin/users` | admin | All users with full partner details |
| DELETE | `/api/admin/users/{username}` | admin | Deactivate user |
| GET | `/api/admin/logs` | admin | Log file tail |
| POST | `/api/admin/exec` | admin | Shell command output |
| WS | `/ws/performance` | — | Live push — `{event, refreshed_at}` |

### Database

PostgreSQL 17 on server. Two databases:
- `ramboq` — production (deploy_branch == 'main')
- `ramboq_dev` — development (any other branch)

Credentials in `secrets.yaml`: `db_user`, `db_password`, `db_host`, `db_port`.

Users table (30 columns): `id`, `account_id` (auto-generated `rambo-XXXXXX`), `username`, `password_hash`, `role`, `display_name`, `email`, `phone`, `pan`, `aadhaar_last4`, `date_of_birth`, `kyc_verified`, address fields, `contribution`, `contribution_date`, `share_pct`, bank fields, nominee fields, `is_approved`, `is_active`, `join_date`, `notes`, timestamps.

---

## What stays unchanged

- `backend/shared/helpers/` — broker_apis, connections, decorators, alert_utils, genai_api (all reused by API)
- `backend/config/` — all config and secrets files
- `webhook/` — deploy scripts, notify_deploy.py
- `backend/workers/refresh_worker.py` — kept as optional ARQ-based alternative

---

## Background processing modes

Two modes available — Litestar async (default) or ARQ worker:

| Mode | How to run | Redis needed? | Process count |
|---|---|---|---|
| **Litestar async** (default) | `bash backend/run_api.sh` | No | 1 (API + background) |
| **ARQ worker** | `bash backend/run_api.sh` + `bash backend/run_worker.sh` | Yes | 2 (API + worker) |

Litestar async is simpler (single process, no Redis). ARQ mode is useful for
multi-server setups where the worker runs on a different machine.

---

## Branch strategy

- `new` — active migration work
- `dev` / `main` — production branches; never rebased or force-pushed
- Migration work merged to `dev` → `main` when each phase is production-ready
