# RamboQuant — Complete Design Guide

The full developer onboarding document. Read top-to-bottom to understand the codebase end-to-end; reference specific sections for ongoing work. Flow diagrams use Mermaid. Tech-stack rationale (why/what/how/where) is interleaved with each subsystem rather than collected separately — easier to learn in context.

**Goal:** anybody who reads + understands this document should be able to modify and enhance features by making the actual code changes. Each subsystem section names the files; **Part IX** at the end is a cookbook of common change recipes with exact-diff-level guidance.

---

## Table of contents

### Part I — Foundation
1. [Architecture overview](#1-architecture-overview)
2. [Tech stack — at a glance](#2-tech-stack--at-a-glance)
3. [Core architectural principles](#3-core-architectural-principles)
4. [Concurrency model](#4-concurrency-model)

### Part II — Order lifecycle
5. [Order placement — single ticket (Ticket tab)](#5-order-placement--single-ticket-ticket-tab)
6. [Order placement — basket (Chain tab)](#6-order-placement--basket-chain-tab)
7. [Chase loop lifecycle](#7-chase-loop-lifecycle)
8. [The order/chase/template tripod](#8-the-orderchasetemplate-tripod)

### Part III — Templates + exits
9. [Template attach pipeline](#9-template-attach-pipeline)
10. [4-default template matrix](#10-4-default-template-matrix)
11. [Template override merge](#11-template-override-merge)
12. [Chase loop invariants](#12-chase-loop-invariants)
13. [Trail-stop subsystem](#13-trail-stop-subsystem)

### Part IV — Brokers
14. [Broker abstraction](#14-broker-abstraction)
15. [How to add a new broker](#15-how-to-add-a-new-broker)
16. [Broker gotchas](#16-broker-gotchas)

### Part V — Frontend
17. [Frontend modal state](#17-frontend-modal-state)
18. [Frontend state architecture](#18-frontend-state-architecture)
19. [The preview pipeline](#19-the-preview-pipeline)

### Part VI — Runtime
20. [Background task topology](#20-background-task-topology)
21. [Data refresh — PositionStrip + Dashboard](#21-data-refresh--positionstrip--dashboard)
22. [Demo mode](#22-demo-mode)

### Part VII — Operations
23. [How to add a new template field](#23-how-to-add-a-new-template-field)
24. [Testing philosophy](#24-testing-philosophy)
25. [Logging discipline](#25-logging-discipline)
26. [Deployment notes](#26-deployment-notes)
27. [Sprint history + audit fixes](#27-sprint-history--audit-fixes)

### Part VIII — Wrap-up
28. [Reading order for a new developer](#28-reading-order-for-a-new-developer)
29. [When in doubt](#29-when-in-doubt)
30. [Operator's mental model](#30-operators-mental-model)

### Part IX — Change recipes (cookbook)
31. [Recipe: add a new route](#31-recipe-add-a-new-route)
32. [Recipe: add a column to an existing table](#32-recipe-add-a-column-to-an-existing-table)
33. [Recipe: add a new background task](#33-recipe-add-a-new-background-task)
34. [Recipe: add a new agent action](#34-recipe-add-a-new-agent-action)
35. [Recipe: add a new template field (worked example)](#35-recipe-add-a-new-template-field-worked-example)
36. [Recipe: add a new broker capability flag](#36-recipe-add-a-new-broker-capability-flag)
37. [Recipe: add a new page](#37-recipe-add-a-new-page)
38. [Recipe: add a setting](#38-recipe-add-a-setting)
39. [Recipe: change an existing default template](#39-recipe-change-an-existing-default-template)
40. [Recipe: wire a new notification channel](#40-recipe-wire-a-new-notification-channel)
41. [Recipe: ship a fix to dev + main](#41-recipe-ship-a-fix-to-dev--main)
42. [Cross-cutting checklist before every commit](#42-cross-cutting-checklist-before-every-commit)

> Tech-stack rationale boxes appear inline as ⚙ **TECH: WHY · WHAT · HOW · WHERE** callouts throughout. Look for the gear glyph.

---

# Part I — Foundation

## 1. Architecture overview

```mermaid
flowchart LR
    Operator((Operator)) -->|browser| FE[SvelteKit frontend<br/>port 5173 / static build]
    FE -->|HTTPS REST| API[Litestar API<br/>port 8502 prod / 8503 dev]
    API -->|asyncpg| DB[(PostgreSQL 17<br/>ramboq / ramboq_dev)]
    API -->|broker SDK| KITE[Kite Connect<br/>+ KiteTicker WebSocket]
    API -->|broker SDK| DHAN[Dhan v2]
    API -->|broker SDK| GROWW[Groww]
    API -->|google-genai| GEMINI[Gemini 2.5 Flash<br/>market + sentiment]
    API -->|smtplib| MAIL[SMTP — Hostinger]
    API -->|requests| TG[Telegram Bot<br/>@RamboQuantBot]
    KITE -->|postback HTTP| API
```

| Layer | Tech | Key files |
|---|---|---|
| Frontend | SvelteKit + Svelte 5 runes + ag-Grid + hand-rolled SVG charts | `frontend/src/` |
| API | Litestar 2.x + msgspec.Struct schemas | `backend/api/` |
| DB | PostgreSQL 17 + SQLAlchemy 2.x async + asyncpg | `backend/api/database.py`, `models.py` |
| Brokers | Vendor SDKs behind a unified `Broker` ABC | `backend/shared/brokers/` |
| Background | asyncio tasks spawned at app startup | `backend/api/background.py` |

---

## 2. Tech stack — at a glance

The choices below were made deliberately, not by inertia. Each call-out explains the **why · what · how · where**.

### ⚙ Litestar (API framework)

- **WHY** — Faster than FastAPI for msgspec workloads (~10× JSON encode/decode on big response payloads like 2000-row positions tables). First-class async lifecycle hooks (`on_startup` / `on_shutdown`) make background-task setup natural. Built-in OpenAPI without runtime cost.
- **WHAT** — Routes are defined as methods on controller classes, each one returning a msgspec `Struct`. Decorators (`@get`, `@post`, `@put`, `@delete`) handle routing + schema; guards (`@guard` decorator) handle auth.
- **HOW** — Register controllers in `backend/api/app.py`. Use `state.is_demo` from the auth guard for runtime gating. Never call sync broker SDKs directly from a route — wrap in `asyncio.to_thread`.
- **WHERE** — `backend/api/app.py` for setup; `backend/api/routes/*.py` for routes; `backend/api/schemas.py` for msgspec types.

### ⚙ msgspec.Struct (schemas)

- **WHY** — Tightest serialization + validation on hot paths (positions snapshots, ticker fan-out, AlgoOrderInfo lists hit thousands of times per session). Pydantic was ~10× slower in benchmarks for read-heavy responses.
- **WHAT** — Plain Python classes inheriting `msgspec.Struct`. Field types enforce validation; missing fields raise `msgspec.ValidationError`. Optional fields use `field | None = None`.
- **HOW** — Define once in `backend/api/schemas.py`. Litestar autowires the encode/decode. For request bodies the route handler signature `data: MySchema` triggers validation automatically.
- **WHERE** — `backend/api/schemas.py` (all types in one file by convention so the API contract is one grep away).

### ⚙ SQLAlchemy 2.x async + asyncpg

- **WHY** — asyncpg is the fastest Postgres driver in Python. SQLAlchemy 2.x's new typing + Mapped[] columns give us static-checkable ORM without runtime overhead. Async sessions integrate with Litestar's event loop cleanly.
- **WHAT** — Declarative models in `backend/api/models.py`. Sessions via `async_session()` context manager. `expire_on_commit=False` (DB config) means ORM rows stay readable post-commit — load-bearing for several audit-fix paths (reconcile attach queue, retry_template).
- **HOW** — Use `async with async_session() as s:` inside route handlers. Avoid keeping sessions across `await` points to other I/O. For bulk writes, prefer one commit at the end of a logical group.
- **WHERE** — `backend/api/database.py` (init + session factory); `backend/api/models.py` (table defs); idempotent migrations via `init_db`'s `ALTER TABLE ... IF NOT EXISTS` statements.

### ⚙ PostgreSQL 17

- **WHY** — Reliable, well-known, free. We don't need NoSQL flexibility; the schema is stable. Postgres's JSONB columns handle the `attached_gtts_json` blob without a separate table.
- **WHAT** — Two DBs: `ramboq` (prod, served from `main` branch) and `ramboq_dev` (dev, any non-main branch). Tables auto-created at startup via `init_db`.
- **HOW** — Per-environment selection in `database.py::_db_name` based on `deploy_branch`. Connection string from `secrets.yaml::db_user / db_password`. Run `psql -d ramboq_dev` to inspect dev directly.
- **WHERE** — Server-side on the Hostinger VPS port 5432, peer authentication for the `postgres` UNIX user.

### ⚙ SvelteKit + Svelte 5 runes

- **WHY** — Smaller bundle than React, simpler mental model than Vue. Svelte 5 runes (`$state`, `$derived`, `$effect`, `$props`) give us reactivity without the runtime cost. SvelteKit's file-based routing matches our page-per-feature layout. Static-built output deploys with the API as one process.
- **WHAT** — Pages under `frontend/src/routes/` (route segments by URL). Components in `frontend/src/lib/`. Bindable props (`= $bindable()`) replace the legacy `bind:` parent-child sync.
- **HOW** — `$state` for mutable component-local state. `$derived` for pure computed values. `$effect` for side effects (timers, DOM, network). Two-way sync between parent and child via `bind:value={parentState}`.
- **WHERE** — `frontend/src/routes/` for pages; `frontend/src/lib/` for reusable components. Build via `npm run build`; deploy as static via the SvelteKit static adapter.

### ⚙ ag-Grid

- **WHY** — The performance bar for tables with >500 rows + frequent updates (Pulse symbol grid hits 1000+ ticks/sec at peak). Hand-rolled HTML tables choked at ~200 rows; ag-Grid's row virtualization makes it linear.
- **WHAT** — Grid components in `frontend/src/lib/`. Column defs declarative; cell renderers can be Svelte components or HTML strings.
- **HOW** — Always set `getRowId` for in-place updates (we use `data.id` everywhere). Use `setGridOption('rowData', newRows)` for full-refresh; row-by-row mutations via `applyTransaction({ update: [...] })`.
- **WHERE** — `frontend/src/lib/MarketPulse.svelte`, `frontend/src/lib/PerformancePage.svelte`, `frontend/src/lib/OrderCard.svelte` rendered in lists.

### ⚙ Hand-rolled SVG charts

- **WHY** — No chart-library dependency (saves ~150KB on every page load). Tighter control over interactions (zoom-pan, click-to-cycle preview chip, custom hover crosshairs). Charts integrate seamlessly with our amber/cyan/red palette.
- **WHAT** — `PriceChart.svelte`, `OptionsPayoff.svelte`, `ChartWorkspace.svelte` each draw paths + axes + grid lines directly into SVG. No D3 — pure path math.
- **HOW** — Compute scale functions per-redraw (`xOf(t)`, `yOf(price)`). Use `$derived` for `pathD` strings so re-renders only happen on data change. Mouse handlers translate clientX→data via reverse-scale.
- **WHERE** — `frontend/src/lib/PriceChart.svelte`, `frontend/src/lib/OptionsPayoff.svelte`, `frontend/src/lib/ChartWorkspace.svelte`.

### ⚙ Mermaid in markdown

- **WHY** — Renders inline in GitHub + most preview tools. No build step required. Diagrams stay in version control so they evolve with code.
- **WHAT** — Sequence diagrams for flows, state diagrams for state machines, flowcharts for topology, gantt for time-based topology.
- **HOW** — Fenced code blocks with `mermaid` language tag. Test render in GitHub before committing.
- **WHERE** — This document; `frontend/src/lib/` rarely uses inline Mermaid in component docstrings.

### ⚙ asyncio + uvicorn (single worker)

- **WHY** — Single worker eliminates Kite token-collision issues + simplifies in-process locking (see §4.1). asyncio is the only viable Python concurrency story for I/O-bound workloads of this size.
- **WHAT** — `uvicorn backend.api.app:app --workers 1` in systemd. Background tasks are coroutines spawned at `on_startup` via `asyncio.create_task`.
- **HOW** — Every long-running task wraps its body in `try/except` (see §4.3). Broker SDK calls use `asyncio.to_thread` to avoid blocking the event loop. No `time.sleep` — always `await asyncio.sleep`.
- **WHERE** — `backend/api/background.py` for tasks; `backend/api/app.py` for startup spawn list.

### ⚙ KiteTicker (Twisted WebSocket)

- **WHY** — Only way to get sub-second LTP updates from Kite without burning the 3-req/sec historical_data quota. WebSocket means we pay for one persistent connection instead of polling.
- **WHAT** — `KiteTicker` runs Twisted internally; callbacks fire on a Twisted reactor thread, not the asyncio loop. `TickerManager` bridges via a `threading.Lock` + dict.
- **HOW** — `TickerManager.start(api_key, access_token)` is idempotent. `subscribe(tokens)` adds tokens to the watch list. `get_ltp(token)` is the async read; returns None if not subscribed.
- **WHERE** — `backend/shared/helpers/kite_ticker.py` for the manager; `backend/api/routes/quote.py::sparkline_stream` for the SSE pipe.

### ⚙ Telegram (Bot API via plain requests)

- **WHY** — Reliable mobile notifications without a dedicated app. Free, well-rate-limited (30 msgs/sec into a group), works on every phone.
- **WHAT** — `_send_telegram(message)` posts to `https://api.telegram.org/bot<TOKEN>/sendMessage` with the group's `chat_id`.
- **HOW** — Gate every call with `is_enabled('telegram')` so dev branches don't spam the group. Use the platform-wide `alert_utils._dispatch` for anything that should also email.
- **WHERE** — `backend/shared/helpers/alert_utils.py` for the helper; `secrets.yaml` for bot token + chat_id.

---

## 3. Core architectural principles

### 3.1 Single source of truth at the broker boundary

The `Broker` abstract base class (`backend/shared/brokers/base.py`) is the **only** place vendor differences should leak. Every route, agent, and background task talks to a `Broker` instance via `get_broker(account)` from the registry.

⚙ **TECH** — `WHY` Vendor SDKs disagree on EVERYTHING (qty units, status strings, GTT shape). Letting that disagreement propagate past the adapter boundary creates bug surface area in every consumer. `WHAT` The ABC declares ~20 methods (place_order, modify_order, cancel_order, orders, holdings, positions, funds, ltp, quote, historical_data, place_gtt, modify_gtt, cancel_gtt, get_gtts, instruments, profile, holidays, order_status, trades, basket_order_margins). `HOW` New broker? Implement every method, translate to Kite shape in `_normalise_*` helpers, register in `_ADAPTERS`. Capability gaps go in `BrokerCapabilities` — never inline `if broker_id == "groww"`. `WHERE` `backend/shared/brokers/base.py` (ABC); `backend/shared/brokers/kite.py` / `dhan.py` / `groww.py` (implementations); `backend/shared/brokers/capabilities.py` (matrix).

### 3.2 Idempotency is the default

Every path that places a broker order or GTT can fire twice — postbacks arrive twice, chase terminals race postbacks, reconcile sweeps re-fire attaches. Four patterns make this safe:

| Pattern | Where | What it guards |
|---|---|---|
| `attached_gtts_json IS NULL` check | `_fire_template_attach_on_fill` | Double-place TP/SL/Wing at broker |
| `_TEMPLATE_ATTACH_LOCKS[parent_row_id]` | Same function | Concurrent races within the same row |
| `_KILLED_ORDER_IDS` dict with 60-min TTL | `chase.py` | Operator kills landing on stale `broker_order_id` |
| `MAX(prior, cumulative)` clamp | `_record_partial_fill` | Restart causing cumulative to be added again |

**When adding a new fill-time side-effect, ask:** can my handler fire twice for the same parent? If yes, what's the idempotency check?

### 3.3 Database is authoritative; in-memory is fast-path

The single uvicorn worker (`--workers 1` in prod — see §4.1) means in-process locks are sufficient, but the DB is still the source of truth. After a restart, every chase loop recovers via `recover_from_db` and re-derives state. Don't store anything operationally meaningful in in-process state without a DB write to back it up.

The `attached_gtts_json` column is a deliberate small-state JSON blob rather than a foreign-key normalized table:
- ✅ Atomic write per parent — no half-attached state visible to readers
- ✅ Easy to refactor the GTT spec shape (just version the JSON inside)
- ❌ Harder to JOIN against; we accept this because GTT inspection is rare

### 3.4 Async by default, sync when forced

Everything API-facing is `async def` over asyncpg. Broker SDK calls are sync — Kite/Dhan/Groww use `requests` under the hood — so we wrap them in `asyncio.to_thread(...)` to keep the event loop unblocked. The threadpool sizing is the default (32 workers); we've never seen it saturate because broker API calls are sub-second.

**Anti-pattern to avoid:** `broker.method()` directly in an `async def` route handler. Even if it returns "fast," a single 2-second hang stalls every other request on that worker.

### 3.5 Demo mode = signed-out + prod branch

Demo isn't a separate code path — it's a runtime guard at the API boundary (`backend/api/auth_guard.py`) plus a frontend flag pulled from context. The same routes serve authenticated + demo traffic; the guard masks accounts and blocks writes. This means **a feature works in demo the moment it works for read-only sessions** — there's no separate "demo enablement" step to forget.

---

## 4. Concurrency model

### 4.1 Why one uvicorn worker?

`--workers 1` in prod is **intentional** for three reasons:

- **Kite session affinity:** multiple workers would invalidate each other's Kite tokens because Kite enforces one active session per IP.
- **In-process locking is enough:** all locks are `asyncio.Lock` instances; we never need multiprocess coordination.
- **Background tasks need shared state:** the trail-stop poller's in-memory state (`_TEMPLATE_ATTACH_LOCKS`, the ticker manager's `_tick_map`) is process-scoped. Multi-worker would require Redis or similar.

If we ever scale horizontally we'd need to externalize: tokens → DB, locks → DB advisory locks or Redis, ticker state → a separate fanout service.

### 4.2 KiteTicker threading

`KiteTicker` runs Twisted internally — **all WebSocket callbacks fire on a Twisted reactor thread**, not the asyncio event loop. The `TickerManager` bridges this:
- Twisted thread writes `_tick_map[token] = ltp` under a `threading.Lock`
- Async handlers read via `get_ltp(token)` — same lock, briefly held

The lock is non-reentrant and the critical section is O(1) so no deadlock risk.

⚙ **TECH** — `WHY` Twisted reactors can't see asyncio's event loop, so we can't `await` from a tick callback. Lock-protected dict is the simplest viable bridge. `WHAT` `TickerManager._tick_map: dict[int, float]` + `_tick_lock: threading.Lock`. `HOW` Tick handler does `with self._tick_lock: self._tick_map[token] = ltp`. Async reader does the same under the lock, briefly. `WHERE` `backend/shared/helpers/kite_ticker.py`.

**If you add anything that runs on the Twisted side**, never call `asyncio.run_coroutine_threadsafe` without testing both directions of the round-trip. The reactor doesn't know about asyncio's event loop.

### 4.3 Background task lifecycle

All background tasks are spawned in `app.on_startup` via `asyncio.create_task(...)`. They run forever; cancellation only happens at app shutdown. Each task is responsible for its own error handling — an uncaught exception kills the task silently. Every task body should be:

```python
async def _task_X():
    while True:
        try:
            ...real work...
        except Exception as e:
            logger.exception(f"_task_X iteration failed: {e}")
        await asyncio.sleep(interval)
```

The `try/except` around the loop body is non-negotiable. We've burned hours debugging "why did the trail stop go silent" only to discover an unhandled `KeyError` ate the task three days earlier.

---

# Part II — Order lifecycle

## 5. Order placement — single ticket (Ticket tab)

```mermaid
sequenceDiagram
    actor OP as Operator
    participant OT as OrderTicket.svelte
    participant SP as SymbolPanel.svelte
    participant API as /api/orders/ticket
    participant DB as algo_orders
    participant BR as Broker (Kite/Dhan/Groww)
    participant CH as chase_order (background)
    participant PB as /api/orders/postback (Kite)

    OP->>OT: Fill side + qty + price; click Submit
    OT->>OT: Resolve mode from $executionMode
    OT->>API: POST /ticket (mode, side, sym, qty, price, template_id, overrides)
    API->>API: Demo guard / preflight margin
    API->>DB: INSERT AlgoOrder (status=OPEN, broker_order_id=NULL)
    API->>DB: COMMIT
    alt chase_eligible (LIMIT + price > 0)
        API->>CH: _start_live_chase (async)
        CH->>BR: broker.place_order
        CH-->>API: order_id
    else single-shot (MARKET / SL-M)
        API->>BR: broker.place_order
        BR-->>API: order_id
    end
    API->>DB: UPDATE broker_order_id = order_id
    API->>DB: COMMIT
    API-->>OT: {order_id, status, mode}
    note right of PB: Kite only — postback HMAC verified
    BR-->>PB: order state change webhook
    PB->>DB: UPDATE status + fill_price + filled_at
    PB->>PB: _fire_template_attach_on_fill (async)
```

**Key files (lines verified against current code):**
- `frontend/src/lib/order/OrderTicket.svelte` — submit handler (search `async function _submit`)
- `backend/api/routes/orders.py::ticket_order` (~line 2117 — search the `async def ticket_order` definition; line drift across the file is expected as features land)
- `backend/api/algo/chase.py::chase_order` — main loop (~line 640)
- `backend/api/routes/orders.py` — postback HMAC + state update (search `async def order_postback`)
- `backend/api/routes/orders.py::_fire_template_attach_on_fill` (~line 701)

> The above grep targets are stable across refactors; the line numbers may drift. Always grep for the function name rather than navigating by line.

**Race-window note:** the AlgoOrder row commits with `broker_order_id=NULL` first; the second commit seeds it after `place_order` returns. A fast IOC fill landing in this window is caught by the **postback fallback** which matches by `(account, symbol, side, qty, status=OPEN, mode=live, created_at >= cutoff)`.

---

## 6. Order placement — basket (Chain tab)

```mermaid
sequenceDiagram
    actor OP as Operator
    participant OCT as OptionChainTab.svelte
    participant SP as SymbolPanel.svelte
    participant API as /api/orders/basket
    participant DG as _dispatch_group (per-account)
    participant BR as Broker (per-account)
    participant DB as algo_orders

    OP->>OCT: +CE / +PE / +Fut on strike rows
    OCT->>SP: onAddLeg → basketLegs[] mutation
    OP->>SP: Click Submit on basket bar
    SP->>SP: submitBasket — group legs by account
    SP->>API: POST /basket (groups: [{account, legs[]}])
    API->>API: Resolve mode + check demo
    par per-account dispatch (parallel)
        API->>DG: dispatch group A
        DG->>BR: broker.place_order (leg 0)
        DG->>BR: broker.place_order (leg 1)
        DG->>DB: INSERT AlgoOrder per leg
        DG-->>API: leg_results[]
    and
        API->>DG: dispatch group B
        DG-->>API: leg_results[]
    end
    API-->>SP: groups: [{account, results[]}]
    SP->>SP: Compute ok / fail counts
    alt all succeeded
        SP->>SP: clear basket + green sticky banner (3s)
    else partial
        SP->>SP: keep failed legs + amber sticky banner (persistent)
    else all failed
        SP->>SP: red sticky banner (8s)
    end
```

**Key files:**
- `frontend/src/lib/order/OptionChainTab.svelte` — `placeBasket` / `onAddLeg`
- `frontend/src/lib/SymbolPanel.svelte::submitBasket` — per-account groups
- `backend/api/routes/orders.py::place_basket` route + `_dispatch_group` (~line 3176)
- `frontend/src/lib/SymbolPanel.svelte` — partial-failure sticky banner (search `_stickyResultMsg`)

⚙ **TECH — asyncio.gather for parallel per-account dispatch** — `WHY` Each broker call hits a different vendor; serializing them per-account would multiply submit latency by N. `WHAT` `_dispatch_group` is called per-account; the route wraps them in `asyncio.gather(*tasks, return_exceptions=True)`. `HOW` Each group has its own try/except so one group's failure can't poison another. Results return as `{account, results[]}`. `WHERE` `backend/api/routes/orders.py::place_basket`.

**Per-leg vs shell template:** `leg.template_id ?? _sharedTemplateId` resolves to either explicit per-leg pick or shell default. **Per-leg legs with explicit `template_id` IGNORE shell overrides** — see `SymbolPanel.svelte::submitBasket` for the isolation rule.

---

## 7. Chase loop lifecycle

```mermaid
stateDiagram-v2
    [*] --> Placing
    Placing --> Polling: place_order returns order_id
    Polling --> Placing: status=OPEN AND price moved → cancel + replace
    Polling --> Filled: status=COMPLETE
    Polling --> Partial: cumulative_filled > already_filled
    Partial --> Polling: still has residual
    Partial --> Filled: cumulative = total
    Polling --> Rejected: status=REJECTED
    Polling --> KilledMidReplace: is_killed(NEW_id) post-replace
    Polling --> Killed: operator kill detected at status check
    Polling --> Unfilled: attempts >= max_attempts
    Polling --> ErrorAbort: >= _MAX_CHASE_ERRORS consecutive
    Filled --> [*]: _emit_chase_terminal(chase_fill)
    Rejected --> [*]: _emit_chase_terminal(chase_failed)
    Killed --> [*]: _emit_chase_terminal(chase_cancelled)
    KilledMidReplace --> [*]: _emit_chase_terminal(chase_cancelled, post-replace)
    Unfilled --> [*]: _emit_chase_terminal(chase_unfilled)
    ErrorAbort --> [*]
```

**Key files:**
- `backend/api/algo/chase.py::chase_order` — main loop
- `backend/api/algo/chase.py` — partial-fill branch (search `_record_partial_fill`)
- `backend/api/algo/chase.py` — kill-race post-replace check (search `is_killed(current_order_id)` after `_sync_algo_order_id`)
- `backend/api/algo/chase.py::_emit_chase_terminal` — snapshot + downstream attach
- `backend/api/algo/chase.py::_sync_algo_order_id` — writes `broker_order_id` + `current_limit`

⚙ **TECH — sync polling vs WebSocket order updates** — `WHY` Postback delivery is unreliable for non-Kite brokers (Dhan + Groww are poll-only). Sync polling is the lowest-common-denominator that works everywhere. `WHAT` `chase_order` calls `_order_status` every 20s (configurable per chase). `HOW` Each iteration: depth quote → adjusted limit → cancel old + place new → sync ID → wait → poll status. `WHERE` `backend/api/algo/chase.py`.

**Partial-fill math (post C-1 fix):**
```
already_filled = quantity - remaining_qty
new_delta = cumulative_filled - already_filled
fire partial branch when: cumulative_filled > 0 AND new_delta > 0 AND cumulative_filled < quantity
```

---

## 8. The order/chase/template tripod

This is the most complex part of the codebase. Three subsystems with overlapping responsibilities:

### 8.1 What each one owns

| | Owns | Reads |
|---|---|---|
| **Order routing** (`orders.py`) | The broker-facing entry path. Single ticket + basket. | Settings, templates |
| **Chase loop** (`chase.py`) | The per-order placement lifecycle. Cancel + replace + status polling + partial fill accounting. | Broker, AlgoOrder, kill signal |
| **Template attach** (`template_attach.py`) | Post-fill exit-rule wiring. TP/SL/Wing/Scale/Trail GTTs at the broker. | OrderTemplate, AlgoOrder (read-only at attach time) |

### 8.2 Why three? Why not one big "manage this order" function?

History: the chase loop existed first (single ticket → place + chase to fill). Templates were bolted on later (Phase 0–3 + Sprints A–E). The current shape is intentional — each subsystem can be tested in isolation:

- Chase tests use a mock `_order_status` that returns a scripted sequence.
- Template tests build a `TemplatePlan` directly and assert the GTT spec shape.
- Routes are integration-tested with real broker mocks (`backend/tests/`).

### 8.3 The mode pivot

`mode ∈ {sim, paper, live, shadow}` decides which adapter the order actually hits. The pivot happens at submit time (`_resolve_mode` in `backend/api/algo/actions.py`) and is **persisted on the AlgoOrder row** — every downstream branch (chase terminal, postback, reconcile, template attach) reads `row.mode` to decide whether to call a real broker or the paper engine.

**Gotcha:** the chase loop runs the same code regardless of mode. Paper mode is achieved by injecting the paper engine's `place_order` adapter at the broker registry boundary. Don't add `if mode == 'live'` branches inside chase — the abstraction is the broker registry, not the chase.

---

# Part III — Templates + exits

## 9. Template attach pipeline

```mermaid
flowchart TD
    subgraph triggers [Fill triggers]
        PB[Postback handler<br/>orders.py order_postback]
        CT[Chase terminal<br/>chase.py _emit_chase_terminal]
        RC[Reconcile sweep<br/>orders.py reconcile_*]
        RT[Operator retry<br/>orders.py retry_template]
    end

    PB --> FF[_fire_template_attach_on_fill]
    CT --> FF
    RC --> FF
    RT --> APT[apply_template_to_order]

    FF -->|attached_gtts_json IS NULL guard| APT
    APT --> RP[resolve_template_plan]
    RP --> PLAN[TemplatePlan: gtts + wing]
    APT --> WS[_pick_wing_by_premium<br/>chain scan]
    PLAN --> GTT1[broker.place_gtt — TP]
    PLAN --> GTT2[broker.place_gtt — SL]
    PLAN --> GTT3[broker.place_gtt — scale-out N]
    WS --> WO[broker.place_order — wing leg]
    GTT1 --> AGG[Aggregate result.gtt_ids]
    GTT2 --> AGG
    GTT3 --> AGG
    WO --> AGG
    AGG -->|attached_gtts_json| DB[(algo_orders.attached_gtts_json)]
    AGG --> RES[AttachResult<br/>{ok, errors[], notes[]}]
```

**Key files:**
- `backend/api/algo/template_attach.py::resolve_template_plan` — override merge + scope resolution
- `backend/api/algo/template_attach.py::_pick_wing_by_premium` — OI + spread filters
- `backend/api/algo/template_attach.py::AttachResult` — return type (NOT `TemplateAttachResult` — the docs previously had this wrong)
- `backend/api/routes/orders.py::_fire_template_attach_on_fill` — idempotency guard + persistence
- `backend/api/routes/orders.py::retry_template` — manual re-fire path. Persists `attached_gtts_json` per H-7 + trail-stop scaffolding per Sc.5

**Idempotency:** `_get_template_attach_lock(parent_row_id)` + `attached_gtts_json IS NULL` check. Strong dict with 1h TTL after M-5 fix replaces the prior WeakValueDictionary.

⚙ **TECH — JSON blob vs normalized table for `attached_gtts_json`** — `WHY` Each parent has 1-5 GTTs + maybe a wing. A child table would mean a JOIN on every order grid render. The blob lets us read the whole bracket in one column-fetch. `WHAT` Stored as a JSON array of entries (`{kind: "gtt", label: "TP", id: "...", current_trigger: ..., sl_trail_pct: ...}`). `HOW` Always write atomically (single column update); read+parse on every access (cheap because rows are small). `WHERE` `backend/api/models.py::AlgoOrder.attached_gtts_json` + `_fire_template_attach_on_fill`.

---

## 10. 4-default template matrix

```mermaid
flowchart LR
    Op([Operator picks symbol + side]) --> SC{_appliesToFor}
    SC -->|BUY + ends CE/PE| BO[buy_option<br/>default-long-option<br/>TP+80% MARKET]
    SC -->|BUY + EQ/FUT| BA[buy_any<br/>default-bull<br/>TP+30% SL-20%]
    SC -->|SELL + ends CE/PE| SO[sell_option<br/>default-short-vol<br/>TP+50% + Wing 10%]
    SC -->|SELL + EQ/FUT| SA[sell_any<br/>default-bear<br/>TP+30% SL-20%]
    BO --> CHIP[Default pill name chip<br/>+ override inputs]
    BA --> CHIP
    SO --> CHIP
    SA --> CHIP
    CHIP --> PREV[On-fill preview chip<br/>₹ triggers]
```

**Key files:**
- `backend/api/algo/templates_seed.py::SYSTEM_TEMPLATES` — seeded defaults + rebalance logic
- `frontend/src/lib/order/OrderTicket.svelte::_appliesToFor`
- `frontend/src/lib/SymbolPanel.svelte::_appliesToFor` — same helper at shell level
- `frontend/src/lib/SymbolPanel.svelte::_sideAwareDefault` — with fallback to focused-leg symbol
- `frontend/src/routes/(algo)/automation/templates/+page.svelte` — coverage view (note: `/automation/templates` is the actual route; older docs reference `/admin/templates` which is stale)

---

## 11. Template override merge

Operator overrides flow through multiple layers; understanding the merge order saves hours of debugging.

```
Request:
  template_id=T1, tp_pct_override=20, sl_pct_override=None

Backend persist (orders.py::_build_overrides_json ~line 541):
  AlgoOrder.template_id          = T1
  AlgoOrder.template_overrides_json = '{"tp_pct": 20}'   # only NON-None overrides

At fill (template_attach.py::_pick):
  tp_pct = _ov.get("tp_pct") or template.get("tp_pct")
       → 20 (override wins)
  sl_pct = _ov.get("sl_pct") or template.get("sl_pct")
       → template's saved sl_pct (no override)
```

**Per-leg vs shell:** when a basket leg has `template_id` set explicitly (not inherited from `_sharedTemplateId`), the SHELL overrides DO NOT flow through. This is the audit-Sc.12 fix — pre-fix the shell's `tp_pct_override` silently contaminated a leg that the operator had retargeted to a different template.

```
submitBasket logic:
  effective_template = leg.template_id ?? shell_template
  if leg has its own template_id:
      tp_override = leg.tp_pct_override  (do NOT fall through to shell)
  else:
      tp_override = leg.tp_pct_override ?? shell.tp_pct_override
```

---

## 12. Chase loop invariants

Six things the chase loop MUST guarantee:

1. **AlgoOrder.broker_order_id always matches the LATEST broker order.** Cancel-and-replace updates this via `_sync_algo_order_id`. Without it the postback handler can't resolve a row by `broker_order_id`.

2. **AlgoOrder.current_limit reflects the latest re-quoted limit.** Added in M-6. ChaseCard renders this when present so the operator sees the live limit, not entry.

3. **AlgoOrder.filled_quantity is monotonic and never exceeds AlgoOrder.quantity.** The `MAX(prior, cumulative)` clamp in `_record_partial_fill` enforces this post C-1 fix. Template attach reads `filled_quantity` to size exit GTTs.

4. **Operator kills take effect on the very next loop iteration.** `mark_killed(broker_order_id)` is synchronous + the loop checks `is_killed(current_order_id)` (a) at status-check time AND (b) immediately after replace (C-2 fix). The dict has a 60-min TTL so a stale kill flag can't survive across days.

5. **Partial fills get persisted on every NEW delta, not just the first.** The branch fires when `cumulative > already_filled` post-C-1 fix.

6. **A chase that hits >= `_MAX_CHASE_ERRORS` consecutive exceptions aborts.** Prevents infinite re-trying against a broker that's down.

Break any of these and template attach sizes wrong, kills get ignored, or zombie chases burn rate limit.

---

## 13. Trail-stop subsystem

```mermaid
sequenceDiagram
    participant T as _task_trail_stop<br/>(every 30s)
    participant DB as algo_orders.attached_gtts_json
    participant PB as PriceBroker.ltp
    participant BR as broker.modify_gtt

    loop every templates.trail_poll_interval_seconds
        T->>DB: SELECT rows with sl_trail_pct
        T->>T: Build (sym, account, exchange) batches
        T->>PB: PriceBroker.ltp(keys) — falls over per broker
        PB-->>T: {key: {last_price}}
        T->>T: For each entry: compute new trigger<br/>long: peak × (1 - trail%)<br/>short: trough × (1 + trail%)
        alt new_trigger more favorable than current
            T->>BR: broker.modify_gtt(gtt_id, trigger=new)
            alt modify succeeds
                BR-->>T: ok
                T->>DB: UPDATE attached_gtts_json with new trigger
            else Dhan partial (ENTRY_LEG ok, TARGET_LEG fail)
                BR-->>T: raise(dhan_partial_modify=True)
                T->>DB: persist partial_modify_error
                T->>T: WARNING log + Telegram alert
                T->>T: pop sl_trail_pct (stop ratcheting)
            else NotImplementedError
                T->>T: pop sl_trail_pct (stop ratcheting)
            end
        end
    end
```

**Key files:**
- `backend/api/background.py::_task_trail_stop`
- `backend/api/background.py` — Dhan partial-modify detect + alert (M-2 fix, search `dhan_partial_modify`)
- `backend/shared/brokers/dhan.py::modify_gtt` — two-leg dispatch (Sprint C)
- `backend/shared/brokers/groww.py` — emulated OCO trail (currently `NotImplementedError`-skip)
- `backend/shared/brokers/dhan.py::ltp` — wired via instruments cache (B-2 fix)

**Asymmetric SELL guard note:** the poller's SELL ratchet check is `current_trigger > 0 AND proposed < current_trigger`. If `current_trigger=0` (entry never persisted), the guard short-circuits → trail silently dead. This is why **every persistence path that writes a trail entry MUST seed `current_trigger`** (see Sc.5a fix in `retry_template`).

---

# Part IV — Brokers

## 14. Broker abstraction

```mermaid
flowchart TD
    subgraph routes [Route layer]
        OR[orders.py routes]
        AC[actions.py agent actions]
        BG[background.py tasks]
    end

    subgraph reg [Registry]
        GB[get_broker account]
        GPB[get_price_broker]
        GHB[get_historical_brokers<br/>Kite-only filter]
        GSB[get_sparkline_broker<br/>Kite-only filter]
    end

    OR --> GB
    AC --> GB
    BG --> GB
    OR --> GPB
    AC --> GPB
    BG --> GPB

    subgraph abc [Broker ABC]
        OABC[order_status]
        PABC[place_order]
        MABC[modify_order / cancel_order]
        GABC[place_gtt / modify_gtt / cancel_gtt]
        LABC[ltp / quote / historical_data]
    end

    GB --> KITE[KiteBroker]
    GB --> DHAN[DhanBroker]
    GB --> GROWW[GrowwBroker]

    KITE -.implements.-> abc
    DHAN -.implements.-> abc
    GROWW -.implements.-> abc

    KITE --> KSDK[kiteconnect SDK]
    DHAN --> DSDK[dhanhq SDK]
    GROWW --> GSDK[growwapi SDK]
```

**Capability matrix surface:**
- `backend/shared/brokers/capabilities.py::BrokerCapabilities` — dataclass with every capability flag
- `backend/shared/brokers/registry.py::get_historical_brokers` — Kite-only filter
- `frontend/src/lib/data/brokerCapWarnings.js` — single source of truth for warning strings (H-5)
- `frontend/src/lib/order/OrderTicket.svelte::capWarningFor` — single-account
- `frontend/src/lib/SymbolPanel.svelte::aggregateCapWarnings` — cross-account (H-5)

⚙ **TECH — PriceBroker fallback chain** — `WHY` Some brokers can answer quote/ltp/historical (Kite), some can't (Dhan returns `{}` by design for `quote`). Walking the chain lets the operator's chart still render even when their primary account is throttled. `WHAT` `PriceBroker._try(method_name, *args)` iterates eligible brokers, calls method, checks predicates (`_quote_has_data` / `_ltp_has_data` / `_historical_has_data`), returns first successful response. `HOW` Add a new method by name in the predicate map. Rate-limit cool-off (`_RATE_LIMIT_COOLOFF`) excludes throttled accounts for 30s. `WHERE` `backend/shared/brokers/registry.py::PriceBroker`.

---

## 15. How to add a new broker

If you're integrating a new vendor (e.g. "Upstox"):

### Backend

1. **Implement the adapter** in `backend/shared/brokers/upstox.py`. Subclass `Broker` (ABC at `base.py`). Implement EVERY method — there's no "partial" mode. If a method genuinely doesn't apply, raise `NotImplementedError` with a clear message rather than returning empty.

2. **Translate to Kite shape.** Every method that returns operator-facing data (orders, positions, ltp, GTTs) must shape its return to match Kite's structure. Frontend renders are Kite-shape; downstream chase + template code expects Kite-shape. Build a `_normalise_*` helper per category. Mirror the patterns in `dhan.py` and `groww.py`.

3. **Status-string normalization.** Add `_UPSTOX_STATUS_TO_KITE = {...}`. Every Kite-canonical status (`COMPLETE`, `OPEN`, `CANCELLED`, `REJECTED`, `EXPIRED`) must map from one Upstox string. The B-1 audit lesson: a single missing entry silently breaks chase fill detection for an entire broker.

4. **Capabilities.** Add `UPSTOX_CAPS` in `capabilities.py` with EVERY field set explicitly. Don't rely on dataclass defaults — being explicit makes capability gaps visible at code review.

5. **Register in `registry.py`.** Add to `_ADAPTERS` map + `CAPS_BY_BROKER_ID`.

6. **Token caching.** Each broker has its own `.log/<broker>_tokens.json`. Follow the connection wrapper pattern in `connections.py`.

7. **Tests.** Add `backend/tests/test_upstox_broker.py`. Mock the vendor SDK at the boundary; assert your `_normalise_*` outputs.

### Frontend

8. **No frontend code change needed.** The `BrokerCapabilities` dataclass is the contract; the cap warning helper at `brokerCapWarnings.js` reads it generically. Operator-visible capabilities surface automatically.

---

## 16. Broker gotchas

Documented so you don't relearn them the hard way:

| Gotcha | Bit us in |
|---|---|
| **Postback arrives before broker_order_id is committed** | Race window between AlgoOrder pre-persist + seed-broker_id second commit. Fix: fallback recent-NULL-id match (C-3) |
| **Cumulative vs delta in status polls** | Every broker reports `filled_quantity` cumulatively. Pre-fix we added the cumulative value each call → inflation across restarts (C-1) |
| **Kill recorded against old broker_order_id** | Cancel-and-replace creates a new id; kill was only checked against old. Operator's kill silently ignored (C-2) |
| **WeakValueDictionary GC during await** | `_TEMPLATE_ATTACH_LOCKS` could be GC'd between mint and acquire. Fix: strong dict with TTL (M-5). 1h chosen because longest realistic live-chase window is ~30 min; 1h is 2× headroom |
| **Reconcile attach BEFORE commit** | Attach pipeline opened its own session and read pre-commit state. Fix: defer to after commit (C-4 single + bulk) |
| **Empty `_normalise_orders` status map** | Groww's "EXECUTED" passed through verbatim; chase loop never saw "COMPLETE" → no fill detection (B-1) |
| **Dhan `ltp()` returned `{}`** by design until B-2. Trail stop silently dead — no log, no Telegram, just zero ratchet | |
| **Groww `cancel_gtt` blind segment fallback** | Could cancel wrong GTT on numeric id collision. Now raises if exchange missing (M-4) |
| **Naive `datetime.now()` in DB writes** | Mix with tz-aware columns → "AT TIME ZONE" errors. Always `datetime.now(timezone.utc)` |
| **Kite's `tag` is 20-char max** | We truncate via `_truncate_tag` in `chase.py` |
| **Trail-stop persistence missing `current_trigger`** | Asymmetric SELL guard short-circuits with `0`. Every trail-write path must seed (Sc.5a) |
| **OCO double-fire 15s window** | `oco_pair_poll_seconds` default. Matches the `poll_only` GTT detection lag operator sees in the cap warning chip. Telegram alert fires on detection (H-8) |
| **60-second postback fallback window** | Long enough to cover the slowest IOC fill + DB commit race; short enough to avoid cross-pollination with new orders (C-3) |

---

# Part V — Frontend

## 17. Frontend modal state

```mermaid
flowchart TD
    SP[SymbolPanel.svelte]
    SP -->|tab=ticket| OT[OrderTicket.svelte]
    SP -->|tab=chain| OCT[OptionChainTab.svelte]
    SP --> TPL[Template row: Default/None pill]
    SP --> BB[Basket bar pills]
    SP --> CC[ChaseCard.svelte]

    OT --> OD[OrderDepth.svelte]
    OT -->|onMarginUpdate| SP
    OT -->|onPreviewPlanUpdate| SP

    subgraph shellState [Shell-level state]
        SA[_sharedAccount]
        ST[_sharedTemplateId]
        SO[_sharedTpOverride / Sl / Wing×2]
        BL[basketLegs[]]
        FK[_focusedLegKey]
    end

    SP -.binds.-> SA
    SP -.binds.-> ST
    SP -.binds.-> SO
    SP -.owns.-> BL
    SP -.owns.-> FK

    OT -.binds.-> SA
    OT -.binds.-> ST
    OT -.binds.-> SO

    OCT -.binds.-> SA
    OCT -.binds.-> ST
    OCT -.onAddLeg.-> BL

    TPL -.reads.-> ST
    BB -.iterates.-> BL
    BB -.click pill.-> FK
```

**Key files:**
- `frontend/src/lib/SymbolPanel.svelte` — shell + Template row + basket bar + chase card mount
- `frontend/src/lib/order/OrderTicket.svelte` — Ticket form + depth ladder + margin preview
- `frontend/src/lib/order/OptionChainTab.svelte` — strike grid + futures + chain quotes
- `frontend/src/lib/order/OrderDepth.svelte` — bid/ask depth (visibility-gated polling)

**Preview chip swap rule (Chain tab):**
- `basketLegs.length === 0` → Ticket-form preview
- `basketLegs.length > 0` + no focus → last-leg preview
- `_focusedLegKey != null` → that specific leg's preview, badge shows `LEG N/M ●`
- Click any basket pill → set `_focusedLegKey`
- Click chip itself → cycle to next leg
- Operator × on focused leg → key clears, falls back to last-leg

⚙ **TECH — Svelte 5 `$bindable()` props** — `WHY` Two-way sync without prop-drilling or a global store. `WHAT` Child component declares `let { templateId = $bindable(null) } = $props()`; parent writes `bind:templateId={_sharedTemplateId}`. `HOW` Mutations on either side propagate. Avoid `bind:` for derived values (use a `$derived` instead). `WHERE` `SymbolPanel.svelte` ↔ `OrderTicket.svelte` ↔ `OptionChainTab.svelte` template + account props.

---

## 18. Frontend state architecture

### 18.1 Why no global store for order state?

Svelte stores would be the obvious pattern but we don't use them for order modal state. Reasons:

- **Modals are short-lived.** The operator opens, fills, submits, closes. State outlives a single modal mount maybe 5% of the time (basket persists across tab flips).
- **Component-local state with bindable props is enough.** `bind:value` on Svelte 5 runes provides bidirectional sync without the boilerplate.
- **One modal at a time.** We don't need a global "current order context" — the modal owns its context.

The exceptions: `executionMode` (navbar drives every page), `authStore` (every page), `dataCache` (PositionStrip + dashboards share), `orderTemplatesStore` (template CRUD broadcast). These are all narrow — they don't carry order-specific state.

### 18.2 The "shell" pattern

`SymbolPanel.svelte` is a shell. It owns:
- Header (account + symbol pickers)
- Tabs (Ticket / Chain)
- Template row (Default/None pill + override inputs + preview chip)
- Basket bar (when basket has legs)
- Common action footer (margin chip + Submit)

The actual tab content (`OrderTicket.svelte`, `OptionChainTab.svelte`) is mounted as a child. State pipes through:
- **Down via props:** shell → tab (e.g. `_sharedAccount` → `account` prop)
- **Up via callbacks:** tab → shell (e.g. `onMarginUpdate`, `onPreviewPlanUpdate`)
- **Two-way via `bind:`:** for shared mutable state (e.g. `bind:templateId={_sharedTemplateId}`)

When you add a new piece of shell-visible state, decide once:
- Is it tab-specific? → Stay in the tab component.
- Should it survive tab flips? → Lift to shell.
- Does any tab need to READ it? → Pipe down via prop.
- Does any tab need to WRITE it? → `bind:` it.

---

## 19. The preview pipeline

The on-fill preview chip (`on fill → TP ₹250 / SL ₹180 / + Wing BUY ...`) is the single most useful piece of context at submit time. It's computed via two independent pipelines:

- **OrderTicket's `_previewPlan`** ← computed against the Ticket form's symbol/side/qty/price/template.
- **SymbolPanel's `_lastLegPlan`** ← computed against the last basket leg (or operator-focused leg).

The chip render switches between them based on `_activeTab === 'chain' && basketLegs.length > 0`. **Why two pipelines?** Because the inputs are different:
- Ticket: form state, not yet a "leg"
- Last-leg: a fully-formed leg with its own account + symbol + overrides

Both call the same backend endpoint (`previewTicketTemplate`) with the same payload shape. The frontend just feeds them differently.

⚙ **TECH — Backend preview endpoint vs frontend simulation** — `WHY` Operators trust ₹ values that come from the same code path that will ACTUALLY fire on fill. Computing them in the frontend would risk drift; computing them in the backend guarantees the chip reflects reality. `WHAT` `POST /api/orders/preview-ticket-template` returns `{plan: {gtts: [...], wing: {...}, notes: [...]}}`. `HOW` Frontend debounces 200ms after any override change; backend runs `resolve_template_plan` with `apply_path="preview"` so no broker calls fire. `WHERE` `backend/api/routes/orders.py::preview_ticket_template`.

---

# Part VI — Runtime

## 20. Background task topology

```mermaid
gantt
    title Background tasks (app.on_startup)
    dateFormat HH:mm
    axisFormat %H:%M
    section Market data
    Performance refresh (5min)  :perf, 09:00, 6h
    Sparkline warm (daily 00:30) :spark, 00:30, 1m
    Hedge proxy regression (daily 02:30) :hp, 02:30, 5m
    section Order lifecycle
    OCO pair watcher (15s)      :oco, 09:00, 6h
    Trail-stop poller (30s)     :trail, 09:00, 6h
    Ticker watchdog (30s)       :tw, 09:00, 6h
    section Daily ops
    Open summaries              :open, 09:15, 5m
    Close summaries             :close, 15:30, 5m
    MCP audit cleanup (03:15)   :mcp, 03:15, 1m
```

**Key files:**
- `backend/api/background.py` — all task definitions
- `backend/api/app.py::on_startup` — spawn list

**Tasks that touch operator orders:**
- `_task_performance` (5min) — fetches positions/holdings/funds; runs `agent_engine.run_cycle`
- `_task_oco_pair_watcher` (15s) — Groww emulated OCO sibling cancel
- `_task_trail_stop` (30s) — Dhan + Kite trail SL ratchet
- `_task_ticker_watchdog` (30s) — KiteTicker reconnect on disconnect

⚙ **TECH — Why poll-based + not event-based** — `WHY` Vendor postbacks are unreliable (Dhan + Groww have no inbound webhook; Kite drops 0.5-2% in our experience). Polling is the conservative floor. `WHAT` Each task runs on its own asyncio cadence; no scheduler library. `HOW` Pick interval based on operator latency tolerance: trail-stop = 30s (slow ratchet OK), OCO watcher = 15s (faster because both legs settling within window means double-fire). `WHERE` `backend/api/background.py`.

---

## 21. Data refresh — PositionStrip + Dashboard

```mermaid
sequenceDiagram
    actor OP as Operator
    participant PS as PositionStrip.svelte
    participant API as /api/positions, /api/holdings, /api/funds
    participant BR as Broker (Kite)
    participant CACHE as dataCache (in-memory)

    OP->>PS: Mount on any algo page
    PS->>CACHE: Read last-good snapshot for fast paint
    PS->>API: fetchPositions + fetchHoldings + fetchFunds (parallel)
    API->>BR: kite.positions + kite.holdings + kite.margins
    BR-->>API: rows
    API-->>PS: rows
    PS->>CACHE: Update dataCache
    loop every 30s (marketAwareInterval)
        PS->>API: re-fetch
    end
    note over PS: positionsPnl = sum(p.pnl)<br/>positionsToday = sum(p.day_change_val)<br/>holdingsToday = sum(h.day_change_val)<br/>holdingsTotal = sum(h.pnl)
```

**Key files:**
- `frontend/src/lib/PositionStrip.svelte` — navbar strip aggregations
- `backend/api/routes/positions.py`, `holdings.py`, `funds.py` — REST endpoints
- `backend/shared/helpers/broker_apis.py::fetch_positions / fetch_holdings / fetch_margins`
- `backend/api/cache.py` — server-side cache (per-key locking + TTL)

**`/admin/derivatives` Snapshot TOTAL reconciles to PositionStrip** by adding back the rows the page filters out (equity intraday positions + derivative-looking holdings) via `_excludedByAccount`. See `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` (search `_byUnderlyingTotal`).

⚙ **TECH — `marketAwareInterval` polling vs WebSocket** — `WHY` Position state changes when fills happen; we already get fills via KiteTicker, but positions are aggregated server-side. Polling is cheaper than rebuilding aggregations client-side. `WHAT` `marketAwareInterval(fn, 30000)` polls every 30s during market hours, pauses on `document.hidden`. `HOW` Use the helper from `$lib/stores`; never raw `setInterval`. `WHERE` `frontend/src/lib/stores.js::marketAwareInterval`.

---

## 22. Demo mode

```mermaid
flowchart LR
    Anon([Anonymous prod visitor]) --> AUTH{authStore.user}
    AUTH -->|null + branch=main| DEMO[Demo session<br/>state.is_demo = True]
    AUTH -->|signed in| AUTHED[Authenticated session]

    DEMO --> RB[Read paths: real data, accounts masked<br/>ZG0790 → ZG####]
    DEMO --> WB[Write paths: blocked at API]
    WB -->|POST /orders/place| 403
    WB -->|POST /orders/ticket mode=live| DOWNGRADE[Silently downgraded to paper]
    WB -->|/api/admin/*| 401

    DEMO --> UI[UI shows:<br/>· Sign In button replaces user pill<br/>· Settings/Brokers/Users hidden<br/>· Template picker shows muted note]
```

**Key files:**
- `backend/api/auth_guard.py::is_demo_request` + `auth_or_demo_guard`
- `frontend/src/routes/(algo)/+layout.svelte` — demo nav-link gating
- `frontend/src/lib/SymbolPanel.svelte` — template row demo gate (L-3)
- `backend/shared/helpers/broker_apis.py::mask_column` — for demo + public

---

# Part VII — Operations

## 23. How to add a new template field

Templates have grown organically. The current schema is wide (5 mandatory + 7 optional fields). To add a new one:

### Backend

1. **Add the column** to `OrderTemplate` in `backend/api/models.py`.
2. **Idempotent ALTER TABLE** in `backend/api/database.py::init_db`.
3. **Schema fields** in `backend/api/schemas.py` — `OrderTemplate` (response), `OrderTemplateCreate`, `OrderTemplatePatch`. Also `TicketOrderRequest` + `BasketLeg` if you want a per-submit override.
4. **`_build_overrides_json`** in `orders.py` (search for the function name) — add the override → JSON key.
5. **`resolve_template_plan`** in `template_attach.py` — add the `_pick()` call and the GTT spec emission.
6. **Seeded defaults** — update `SYSTEM_TEMPLATES` in `templates_seed.py` if your field should ship with a value.

### Frontend

7. **Template management UI** at `/automation/templates` — add the input.
8. **Override input** at the shell-level Template container in `SymbolPanel.svelte` — add the override field + reset on template change.
9. **Preview** — `previewTicketTemplate` should already wire it because the backend handles it; double-check the chip render handles the new shape.

### Documents

10. **Add a row** to §10 if it's a default field.
11. **Update §11** if your field has unusual merge semantics.

---

## 24. Testing philosophy

The codebase has fewer tests than ideal — that's a known debt. Where tests exist:

- **`backend/tests/`** — pytest + pytest-asyncio. Run via `pytest backend/tests/`.
- **`frontend/e2e/`** — Playwright. Run via `cd frontend && npx playwright test`.
- **No unit tests for frontend** — relies on `svelte-check` + manual flows + e2e.

The Playwright tests run against `dev.ramboq.com` (deployed dev branch). They're slow but high-confidence. Use them for any UX flow that changes; backend pytest for any algo/broker change.

**Rule of thumb:** if you're touching `chase.py`, `template_attach.py`, or any broker adapter, add a pytest test. If you're touching SymbolPanel / OrderTicket flow, add a Playwright spec.

⚙ **TECH — Why Playwright over Cypress** — `WHY` Multi-tab support, native browser context isolation, better async waits. Cypress's same-origin restrictions don't fit our auth flow (OAuth-like JWT). `WHAT` Specs in `frontend/e2e/*.spec.js`. Run with `--workers=1` so dev DB writes don't race. `HOW` Use `expect(...).toContainText(...)` for chip assertions; `toHaveAttribute('placeholder', ...)` for input placeholders. `WHERE` `frontend/e2e/`.

---

## 25. Logging discipline

Three log files matter:

- `api_log_file` — full API log (5MB rotating × 5). Read this first when debugging.
- `api_error_file` — stdout+stderr tee from systemd. Catches uncaught exceptions.
- `hook.log` — webhook listener output.

Log levels by intent:
- `DEBUG` — for trace-style detail. Verbose; filtered out in prod.
- `INFO` — operator-visible events. Order placed, agent fired, chase replaced.
- `WARNING` — recoverable failures. Broker auth retry, asymmetric GTT, partial OCO failure.
- `ERROR` — uncaught exceptions, lost state. Should also trigger Telegram.

**Don't log inside hot loops** without a rate limit. `_task_performance` ran a `logger.info` per row early on; quickly buried `api_log_file` under non-actionable noise.

---

## 26. Deployment notes

Both `dev` and `main` deploy via webhook. Push triggers:

```
GitHub push → webhook.ramboq.com → /etc/webhook/dispatch.sh
  → main:  /opt/ramboq/webhook/deploy.sh prod main
  → other: /opt/ramboq_dev/webhook/deploy.sh dev <branch>
```

`deploy.sh` (per env):
1. `git pull`
2. `pip install` (production deps)
3. `npm run build` (vite)
4. `systemctl restart ramboq_api.service` / `ramboq_dev_api.service`
5. `notify_deploy.py` (Telegram-only since May 2026)

**Per-environment serialisation:** a host-wide `/tmp/ramboq_deploy.lock` prevents concurrent prod + dev builds from race-condition npm conflicts. `nice -n 19 ionice -c 3` on npm so background builds never starve API responsiveness.

**Manual server work after SSH:** always `chown -R www-data:www-data /opt/ramboq /opt/ramboq_dev`. Webhook deploys fail silently if file owner is wrong.

⚙ **TECH — Webhook-based deploy vs CI/CD platform** — `WHY` We're a single-server setup; GitHub Actions would add 30-60s to every deploy plus a $/runner cost. The webhook is bash + git, zero dependencies. `WHAT` `webhook` (Adnan Hajdarbegovic's daemon) listens on port 9000, validates the HMAC, runs `dispatch.sh`. `HOW` Push to a watched branch triggers it automatically. Logs in `hook.log`. `WHERE` `/etc/webhook/hooks.json` (on server); `webhook/dispatch.sh` + `webhook/deploy.sh` (in repo).

---

## 27. Sprint history + audit fixes

These previous fixes are documented in code via comment headers. Knowing them saves you from re-introducing the bug:

| Sprint | What it fixed | Lookup |
|---|---|---|
| Sprint A | Reconcile + paper-engine fire template attach paths | grep `Sprint A` |
| Sprint B | Partial-fill DB persistence + lock TTL | grep `Sprint B` |
| Sprint C | Dhan two-leg `modify_gtt` (ENTRY_LEG + TARGET_LEG) + Groww emulated OCO | grep `Sprint C` |
| Sprint D | OrderCard CANCELLED chip + PROXY chip stale-β + MCX unit-mismatch fix | grep `Sprint D` |
| Sprint E | Composite `(mode, status)` index + ChaseStatus.PARTIAL + rate-limit sweep | grep `Sprint E` |
| Sprint F | USER_GUIDE + ADMIN_GUIDE updates for Sprint A-E | grep `Sprint F` |
| Phase 0–3 | Templates → on-fill GTT pipeline (the whole template_attach stack) | grep `Phase \d` |
| Gap closure | 3-audit synthesis → 28 commits across B/C/H/M/L tiers | grep `audit fix` `-i` |

### Gap closure audit lineage

```mermaid
flowchart LR
    A([3-audit report<br/>parallel agents]) --> S([Synthesis: 28 findings])
    S --> B1[Top-5 batch<br/>5 commits]
    B1 --> B2[Backend safety<br/>C-3 C-4 C-5 H-8]
    B2 --> B3[Frontend visibility<br/>H-1 H-2 H-3 H-4 H-6]
    B3 --> B4[Cap warnings<br/>H-4 H-8]
    B4 --> B5[H-5 cross-account]
    B5 --> B6[M items<br/>M-1 to M-6]
    B6 --> B7[L items<br/>L-2 to L-6]
    B7 --> C[All tiers ✅]
    C --> R[Redo audit found Sc.5<br/>retry_template regression]
    R --> RF[Sc.5 fixed in 10cf52e6]
```

**Closed gaps reference:** see commit history `git log --oneline --grep="audit fix" --regexp-ignore-case` for inline traceback. Each commit's body cites the specific gap ID.

---

# Part VIII — Wrap-up

## 28. Reading order for a new developer

If you've got a week to onboard:

**Day 1 — understand the shape:**
- This doc end-to-end
- `CLAUDE.md` skim (it's the operator-facing manual; some route URLs may reference `/agents/*` which has been redirected to `/automation/*` — see §29)
- `backend/api/app.py` startup wiring
- `backend/api/models.py` schema

**Day 2 — order flow:**
- `frontend/src/lib/SymbolPanel.svelte` + `OrderTicket.svelte` (the modal)
- `backend/api/routes/orders.py::ticket_order` (single submit path)
- `backend/api/algo/chase.py::chase_order` (the loop)

**Day 3 — templates:**
- `backend/api/algo/template_attach.py` (resolve + apply)
- `backend/api/algo/templates_seed.py` (the matrix)
- Trace one BUY CE order from click → fill → attach end-to-end

**Day 4 — brokers:**
- `backend/shared/brokers/base.py` (the ABC)
- `backend/shared/brokers/kite.py` (reference impl)
- `backend/shared/brokers/dhan.py` + `groww.py` (vendor quirks)

**Day 5 — background + extras:**
- `backend/api/background.py` (every task)
- `backend/api/algo/actions.py` (agent action handlers)
- `frontend/src/lib/order/ChaseCard.svelte` + `OrderCard.svelte` (display)

If you've got a day: read §7 (chase loop) above, then read `chase.py::chase_order` source. Everything else extends from that one function.

---

## 29. When in doubt

Open an `Agent` with `subagent_type=audit` and ask it to trace your specific scenario. The audit agents in this codebase are well-calibrated for finding subtle issues. Don't merge a change to `chase.py` or `template_attach.py` without one.

**Known doc-drift in CLAUDE.md** (as of the most recent doc audit): the older operator manual still references `/agents`, `/agents/activity`, `/agents/fragments` URLs. These have been redirected to `/automation`, `/automation/activity`, etc. The redirect routes still work; the URLs in CLAUDE.md are just stale. The current canonical URLs are under `/automation/*`.

---

## 30. Operator's mental model — the one-page summary

| Action | Read this section |
|---|---|
| "What happens when I click Submit on Ticket?" | §5 — single ticket sequence |
| "What does the chase loop do between attempts?" | §7 — chase lifecycle |
| "How does TP/SL get attached?" | §9 — template attach pipeline |
| "Why is my SL not ratcheting on Dhan?" | §13 — trail-stop subsystem |
| "How does the Default pill pick the right template?" | §10 — 4-default matrix |
| "When does the preview chip swap on Chain?" | §17 — frontend modal state |
| "What runs in the background?" | §20 — task topology |
| "Why does the navbar strip not match the dashboard?" | §21 — data refresh paths |
| "What can a demo visitor do?" | §22 — demo mode flow |
| "How do I add a new broker?" | §15 |
| "How do I add a new template field?" | §23 |
| "What's the tech stack?" | §2 — overview; also inline ⚙ TECH callouts throughout |

---

# Part IX — Change recipes (cookbook)

This section turns the design knowledge above into runnable change recipes. Each recipe lists the **exact files to edit**, the **exact pattern to copy**, and the **verification step** before commit. Use these as templates — copy-paste, rename, tweak.

The cookbook is intentionally prescriptive. You do not need to read the full doc above to follow a recipe; you only need §3 (architectural principles) for the philosophy, then jump straight here.

---

## 31. Recipe: add a new route

**Scenario:** you want a new endpoint, e.g. `GET /api/positions/heatmap` that returns aggregated per-symbol stats.

### Steps

1. **Pick the right route file.** `backend/api/routes/` is grouped by domain (`orders.py`, `positions.py`, `holdings.py`, `agents.py`, etc.). Add the new route to the matching file. New domain? Create `heatmap.py`.

2. **Write the msgspec response type** in `backend/api/schemas.py`:
   ```python
   class HeatmapRow(msgspec.Struct):
       symbol: str
       pnl: float
       weight: float
   class HeatmapResponse(msgspec.Struct):
       rows: list[HeatmapRow]
       refreshed_at: str
   ```

3. **Add the route** to the controller (mirror an existing simple route as a template — e.g. `PositionsController.list_positions` in `backend/api/routes/positions.py`):
   ```python
   @get("/heatmap", guards=[auth_or_demo_guard])
   async def heatmap(self, request: Request) -> HeatmapResponse:
       is_demo = request.state.is_demo
       rows = await self._build_heatmap()
       if is_demo:
           rows = [_mask_row(r) for r in rows]
       return HeatmapResponse(rows=rows, refreshed_at=timestamp_display())
   ```

4. **Register the controller** in `backend/api/app.py` if the file is new. Existing controllers don't need re-registration.

5. **Frontend wrapper** in `frontend/src/lib/api.js`:
   ```js
   export const fetchHeatmap = () => _get('/api/positions/heatmap');
   ```
   The wrapper handles auth, retries, demo masking display, and error trimming automatically. **Never call `fetch()` directly from a component** — always go through `api.js`.

6. **Demo masking.** Read paths must mask account values for demo sessions (§22). Use `mask_column(col)` helper, never roll your own.

7. **Verify.** `pytest backend/tests/test_routes_smoke.py` (add a smoke test if the route is non-trivial) + manual curl.

---

## 32. Recipe: add a column to an existing table

**Scenario:** you want `algo_orders.last_chase_quote` to store the last broker depth snapshot.

### Steps

1. **Edit `backend/api/models.py`.** Add the field to the SQLAlchemy model:
   ```python
   last_chase_quote: Mapped[str | None] = mapped_column(Text, nullable=True)
   ```

2. **Add an idempotent ALTER** to `backend/api/database.py::init_db`:
   ```python
   await conn.exec_driver_sql(
       "ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS last_chase_quote TEXT"
   )
   ```
   The `IF NOT EXISTS` is non-negotiable — `init_db` runs on every startup, idempotency required.

3. **Update msgspec schema** in `backend/api/schemas.py` if the column should be returned over the wire:
   ```python
   class AlgoOrderInfo(msgspec.Struct):
       ...
       last_chase_quote: str | None = None
   ```

4. **Populate it.** Decide which code path writes to it. For our example, `chase.py::chase_order` writes after each depth quote fetch.

5. **Frontend render** (optional). Add a column to `OrderCard.svelte` or `OrderTab.svelte` ag-Grid config; render via cellRenderer.

6. **Verify.** `psql -d ramboq_dev -c "\d algo_orders"` shows the new column; `pytest`; e2e if frontend-visible.

⚠️ **Never write a migration script.** The `init_db` `ALTER TABLE ... IF NOT EXISTS` pattern is the migration mechanism. We don't use Alembic.

---

## 33. Recipe: add a new background task

**Scenario:** you want `_task_unrealized_pnl_alert` that fires every 60s during market hours.

### Steps

1. **Define the coroutine** in `backend/api/background.py`. Copy the shape of `_task_oco_pair_watcher` — it's the simplest template:
   ```python
   async def _task_unrealized_pnl_alert():
       interval = 60
       while True:
           try:
               await _run_unrealized_pnl_check()
           except Exception as e:
               logger.exception(f"_task_unrealized_pnl_alert failed: {e}")
           await asyncio.sleep(interval)
   ```
   **The try/except around the loop body is non-negotiable** — without it, an uncaught exception silently kills the task forever.

2. **Spawn it at startup.** In `backend/api/app.py::on_startup`:
   ```python
   asyncio.create_task(_task_unrealized_pnl_alert())
   ```

3. **Gate by market hours** if appropriate. Use `is_any_segment_open()` from `backend/shared/helpers/date_time_utils.py`:
   ```python
   if not is_any_segment_open():
       await asyncio.sleep(interval)
       continue
   ```

4. **Gate by capability flag.** If the task hits an external service, wrap in `is_enabled('telegram' | 'mail' | …)` so dev branches don't spam.

5. **Verify.** Start dev (`uvicorn backend.api.app:app`), watch `.log/api_log_file` for the task's INFO/DEBUG logs, kill, restart, confirm it picks up cleanly.

⚠️ **Do not use `time.sleep`.** Always `await asyncio.sleep(...)`. Sync sleep blocks the entire event loop.

---

## 34. Recipe: add a new agent action

**Scenario:** you want `square_off_underlying` so an agent can close every position on a given underlying.

### Steps

1. **Add the handler** in `backend/api/algo/actions.py`. Mirror the shape of `_action_close_position`:
   ```python
   async def _action_square_off_underlying(
       action: AgentAction,
       context: dict[str, Any],
   ) -> ActionResult:
       params = action.params or {}
       underlying = params.get("underlying")
       if not underlying:
           return ActionResult(ok=False, reason="missing underlying")
       ...
   ```

2. **Register it** in the `_ACTION_HANDLERS` map at the bottom of `actions.py`:
   ```python
   _ACTION_HANDLERS["square_off_underlying"] = _action_square_off_underlying
   ```

3. **Add the grammar token** in `backend/api/algo/grammar.py::_SYSTEM_TOKENS`:
   ```python
   {
       "grammar_kind": "action",
       "token_kind": "action_type",
       "token": "square_off_underlying",
       "value_type": "enum",
       "resolver": "backend.api.algo.actions._action_square_off_underlying",
       "params_schema": {
           "required": ["underlying"],
           "properties": {"underlying": {"type": "string"}},
       },
       "is_system": True,
       "is_active": True,
   }
   ```

4. **Mode resolution.** Honor `_resolve_mode()` — never call broker directly. Use `get_broker(account)` and respect the row's mode.

5. **Verify.** Create a test agent in dev via `/automation/tokens`, fire-in-simulator, confirm event row + broker call.

---

## 35. Recipe: add a new template field (worked example)

**Scenario:** add `tp_breakeven_lock: bool` — when true, after TP1 fires, modify SL to entry price (free trade).

### Steps

1. **Backend column** in `backend/api/models.py::OrderTemplate`:
   ```python
   tp_breakeven_lock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
   ```

2. **Idempotent ALTER** in `backend/api/database.py::init_db`:
   ```python
   await conn.exec_driver_sql(
       "ALTER TABLE order_templates ADD COLUMN IF NOT EXISTS tp_breakeven_lock BOOLEAN"
   )
   ```

3. **Schemas** in `backend/api/schemas.py`:
   - `OrderTemplate` (response): `tp_breakeven_lock: bool | None = None`
   - `OrderTemplateCreate`, `OrderTemplatePatch`: same field
   - `TicketOrderRequest`: optional `tp_breakeven_lock_override: bool | None = None` if you want per-submit override
   - `BasketLeg`: same per-leg

4. **Override JSON build** in `backend/api/routes/orders.py::_build_overrides_json` (search for the function):
   ```python
   if data.tp_breakeven_lock_override is not None:
       result["tp_breakeven_lock"] = data.tp_breakeven_lock_override
   ```

5. **Plan resolution** in `backend/api/algo/template_attach.py::resolve_template_plan`:
   ```python
   breakeven_lock = _pick("tp_breakeven_lock", template, overrides)
   ```
   Then emit the appropriate behavior — for breakeven lock, you'd persist it into `attached_gtts_json` and watch for TP1 fill events.

6. **Seeded defaults** in `backend/api/algo/templates_seed.py::SYSTEM_TEMPLATES` — add to whichever defaults should ship with it ON.

7. **Frontend management UI** at `frontend/src/routes/(algo)/automation/templates/+page.svelte`:
   - Add a toggle to the create/edit form
   - Surface it in the listing

8. **Frontend override input** in `frontend/src/lib/SymbolPanel.svelte`:
   - Add a `_sharedTpBreakevenLockOverride = $state(null)` shell-level
   - Render in the Template row alongside the existing override inputs
   - Reset on template change (search the `$effect` block that watches `_sharedTemplateId`)
   - Pass through to OrderTicket and per-leg basket logic

9. **Preview chip.** Backend handles the math; frontend just renders. The preview will automatically pick up the new override because `previewTicketTemplate` passes through the full overrides dict.

10. **Update this doc.** Add a row to §10 (4-default matrix) if any default ships with it, and to §11 (merge order) if your field has unusual merge semantics.

11. **Verify.** `svelte-check`, `pytest`, e2e on `dev.ramboq.com`: create a template with the field, place a paper order, watch the chain of events in `.log/api_log_file`.

---

## 36. Recipe: add a new broker capability flag

**Scenario:** you discovered Dhan supports `place_co` (cover order) but Groww doesn't.

### Steps

1. **Add the flag** to `backend/shared/brokers/capabilities.py`:
   ```python
   @dataclass(frozen=True)
   class BrokerCapabilities:
       ...
       supports_cover_order: bool = False
   ```
   Default `False` — opt-in.

2. **Set per-broker explicitly** (don't rely on default):
   ```python
   KITE_CAPS = BrokerCapabilities(..., supports_cover_order=True)
   DHAN_CAPS = BrokerCapabilities(..., supports_cover_order=True)
   GROWW_CAPS = BrokerCapabilities(..., supports_cover_order=False)
   ```

3. **Capability registry** in `backend/shared/brokers/registry.py::CAPS_BY_BROKER_ID` already routes by broker_id — no change needed.

4. **Frontend warning helper** in `frontend/src/lib/data/brokerCapWarnings.js`:
   - Update `capWarningFor(template, caps, exchange)` to surface a warning when a template asks for a cover order but `!caps.supports_cover_order`.

5. **Consumer code** queries via `get_broker(account).caps.supports_cover_order` or the HTTP endpoint `/api/admin/brokers/{account}/capabilities`.

6. **Verify.** Inspect `/admin/brokers` page; the new cap should surface in the row.

---

## 37. Recipe: add a new page

**Scenario:** new admin page at `/admin/funds-history`.

### Steps

1. **Create the route file** `frontend/src/routes/(algo)/admin/funds-history/+page.svelte`. Copy structure from `/admin/brokers/+page.svelte` — it's the simplest admin page template.

2. **Page header.** Use the canonical pattern (see "Page-header rule" in CLAUDE.md or §17 of this doc):
   ```svelte
   <div class="page-header">
     <span class="algo-title-group">
       <h1 class="page-title-chip">Funds History</h1>
     </span>
     <span class="algo-ts">{$nowStamp}</span>
     <span class="ml-auto"></span>
     <span class="page-header-actions">
       <RefreshButton onClick={load} loading={_loading} label="Refresh" />
       <PageHeaderActions />
     </span>
   </div>
   ```

3. **Navbar entry** in `frontend/src/routes/(algo)/+layout.svelte` `navItems`. Pick a group (`monitor` | `analyze` | `modes` | `build` | `config`). Set `adminOnly: true` if it should hide in demo mode.

4. **Data loading.** Always:
   - Use `$effect` for mount + cleanup (not legacy onMount)
   - Use `marketAwareInterval` from `$lib/stores` for polling (not raw setInterval)
   - Read via `$lib/api.js` wrappers

5. **Verify.** Visit the route, check navbar entry, confirm Refresh works, watch console for errors. Playwright spec if non-trivial.

---

## 38. Recipe: add a setting

**Scenario:** add `chase.max_consecutive_errors` so operator can tune the abort threshold (currently hardcoded `_MAX_CHASE_ERRORS=5`).

### Steps

1. **Add to `SEEDS`** in `backend/shared/helpers/settings.py`:
   ```python
   ("chase", "chase.max_consecutive_errors", "int", 5,
    "Abort a chase after this many consecutive API failures", None, "errors"),
   ```

2. **Read it** wherever the constant was used. Replace `_MAX_CHASE_ERRORS` with `get_int('chase.max_consecutive_errors', 5)`. The cache invalidates on every PATCH; reads are O(1).

3. **Verify.** Visit `/admin/settings` → confirm new row in the Chase bucket. Edit it, watch the change take effect on next chase iteration without restart.

⚙ **TECH** — the seeder preserves operator overrides on deploy; only the description / schema / default_value refresh. So bumping the default in code only affects fresh installs.

---

## 39. Recipe: change an existing default template

**Scenario:** operator wants `default-long-option` to have SL −40% instead of no SL.

### Steps

1. **Edit `SYSTEM_TEMPLATES`** in `backend/api/algo/templates_seed.py`:
   ```python
   {
       "name": "default-long-option",
       ...
       "sl_pct": 40,  # was None
       "sl_type": "LIMIT",
   }
   ```

2. **Re-seeder behavior.** On startup, `seed_system_templates` rebuilds system templates by `name` — operator's edits to custom templates are preserved, but system templates are overwritten. **The operator's pulls of `default-long-option` will get the new SL on next deploy.**

3. **If operator has saved-instance edits** (i.e. clicked Edit on a system template and saved), those land in a separate row keyed by user_id. They survive system re-seed. To force-refresh, the operator deletes their saved copy.

4. **Verify.** Restart dev, hit `/automation/templates`, confirm SL value is updated. Place a test order — fill should trigger SL GTT placement.

---

## 40. Recipe: wire a new notification channel

**Scenario:** add Slack as a notify channel alongside Telegram + email.

### Steps

1. **Add config keys** in `backend/config/secrets.yaml` (manually on server, gitignored):
   ```yaml
   slack_webhook_url: "https://hooks.slack.com/services/..."
   ```

2. **Add capability flag** in `backend/config/backend_config.yaml::cap_in_dev`:
   ```yaml
   slack: True
   ```
   `is_enabled('slack')` returns `True` on main always, else respects this flag.

3. **Add the helper** in `backend/shared/helpers/alert_utils.py`:
   ```python
   def _send_slack(message: str) -> None:
       if not is_enabled('slack'):
           return
       webhook = secrets.get('slack_webhook_url')
       if not webhook:
           return
       requests.post(webhook, json={"text": message}, timeout=5)
   ```

4. **Add the grammar token** for the notify channel:
   ```python
   # backend/api/algo/grammar.py::_SYSTEM_TOKENS
   {
       "grammar_kind": "notify",
       "token_kind": "channel",
       "token": "slack",
       ...
   }
   ```

5. **Wire it in `_dispatch`** in `alert_utils.py` — for each notify event, check `channel == 'slack'` and call `_send_slack`.

6. **UI checkbox** in agent editor (`frontend/src/routes/(algo)/automation/+page.svelte`) — add Slack to the events grid.

7. **Verify.** Create a test agent with Slack notify, fire in simulator, confirm message lands.

---

## 41. Recipe: ship a fix to dev + main

**Scenario:** you've finished a small fix on `dev` branch and want to deploy to prod.

### Steps

1. **Run pre-flight checks locally.**
   - `cd backend && pytest` (or scoped to affected files)
   - `cd frontend && npm run check` (svelte-check)
   - `cd frontend && npx playwright test --workers=1` if frontend-touching

2. **Commit on dev.** Use the conventional format:
   ```
   <Sprint/scope>: <one-line summary>

   <optional body explaining why>

   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
   ```

3. **Push dev.** `git push origin dev`. Webhook triggers dev deploy automatically. Watch logs:
   ```bash
   ssh ramboq "tail -f /opt/ramboq_dev/.log/api_log_file"
   ```

4. **Verify on `dev.ramboq.com`.** Manual smoke test of the changed feature.

5. **Merge to main.**
   ```bash
   git checkout main
   git merge --ff-only dev
   git push origin main
   git checkout dev  # back to dev for next change
   ```

6. **Webhook deploys prod automatically.** Watch `/opt/ramboq/.log/api_log_file`.

7. **Telegram deploy ping** lands in `RamboQuant Alerts` group — confirm.

⚠️ Don't squash-merge. We keep linear history via `--ff-only`.
⚠️ Don't tag releases; we deploy on every push.

---

## 42. Cross-cutting checklist before every commit

Run mentally before every commit. Skipping any of these has burned us before:

- [ ] **Demo mode honored?** Read paths mask accounts; write paths return 403 or downgrade to paper (§22).
- [ ] **Idempotency on side-effects?** Anything that places orders/GTTs needs a guard (§3.2).
- [ ] **Mode resolution?** Order code reads `row.mode` instead of branching by `if live` (§3.4).
- [ ] **Logger discipline?** No `print()`; no logger calls in hot loops (§25).
- [ ] **Hot-loop sleep?** `asyncio.sleep`, never `time.sleep` (§4.3).
- [ ] **TODO/FIXME?** None in code paths; finish or extract.
- [ ] **Old tests still pass?** `pytest` on the closest test file.
- [ ] **svelte-check clean?** No new errors introduced.
- [ ] **CLAUDE.md updated?** If a fact in CLAUDE.md changes, fix it here.
- [ ] **DESIGN_GUIDE.md updated?** This file. Especially recipe sections + line-anchored grep targets if functions moved.
- [ ] **Sprint/audit fix labeled?** Commit body mentions the gap ID or sprint letter so `git log --grep` finds it.
- [ ] **Co-author trailer?** Co-Authored-By line at the end of the commit body.

If you can't tick every box, hold the commit. The cost of pausing is low; the cost of regression is high.

---

## Where to learn more

| If you want to learn… | Read |
|---|---|
| How the operator uses the platform end-to-end | `USER_GUIDE.md` |
| Day-to-day operations + troubleshooting | `ADMIN_GUIDE.md` |
| Agent authoring + testing | `AGENTS_GUIDE.md` |
| Simulator scenarios + Run-in-Simulator | `SIMULATOR_GUIDE.md` |
| Lab + MCP-driven research workflow | `LAB_MCP_GUIDE.md` |
| Operator's-eye-view of every page | `CLAUDE.md` (large, but indexed) |
| **Architecture + change recipes** | **This document** |
