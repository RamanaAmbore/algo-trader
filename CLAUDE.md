# CLAUDE.md — RamboQuant Project Reference

For Claude Code. Three-layer architecture reference + guardrails. Sprint diaries + 
completed-slice history live in [CLAUDE_HISTORY.md](CLAUDE_HISTORY.md).

**Docs layout** — all markdown except CLAUDE.md / CLAUDE_HISTORY.md / README.md lives under `docs/`:

| Path | Contents |
|---|---|
| `docs/specs/` | Feature behavioral contracts — PULSE_SPEC, BROKER_SPEC, NAVSTRIP_SPEC |
| `docs/guides/` | Operator guides — USER_GUIDE, ADMIN_GUIDE, AGENTS_GUIDE, LAB_MCP_GUIDE, SIMULATOR_GUIDE |
| `docs/audits/` | Point-in-time audit snapshots — AUDIT_DEAD_CODE, AUDIT_PERF, AUDIT_UI |
| `docs/DESIGN_GUIDE.md` | Complete architecture + design reference (source for PDF) |
| `docs/MIGRATION.md` | DB migration history |
| `docs/deployment.md` | Server / infra ops runbook |

Spec files are **not** auto-loaded — read them explicitly when working on or testing the relevant surface.

---

## Contents

**Orientation** — [Multi-agent coordination](#multi-agent-coordination-read-first) · 
[Project Overview](#project-overview) · [Deployment](#deployment)

**Cross-cutting** — [Key Patterns](#key-patterns) · [Things to Avoid](#things-to-avoid) · 
[Critical math guards](#critical-math-guards) · [Common Tasks](#common-tasks-where-to-make-changes) ·
[Custom slash commands](#custom-slash-commands)

**Agent-specific docs** — Layer 1: see `~/.claude/agents/broker.md` · 
Layer 2: see `~/.claude/agents/backend.md` · Layer 3: see `~/.claude/agents/frontend.md`

---

## Model Usage

- **Default**: Sonnet for all agents (frontend, backend, broker, audit). Haiku only per the table below. Opus ONLY when operator explicitly says "use opus".
- Local Qwen proxy (`qwen on|off|status`) routes haiku model IDs to LM Studio when enabled — prefer it for cheap orchestration to save cost.

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
| `doc` | All | CLAUDE.md / docs/guides/ / docs/specs/ | haiku |

**Parallel by default** — independent sub-tasks fire together. Sequence only when 
one output feeds another or when audit finds defects.

## Bug Fix Workflow (Self-Audit Required)

After implementing any bug fix:
1. Run a self-audit pass — check for structurally unreachable code, overwritten state, SSOT consistency.
2. For any P&L / NavStrip / market-data fix: grep all consumers (derivatives, dashboard, NavStrip, MarketPulse) and verify the fix propagates to every one of them — not just the primary page.
3. Only commit after the self-audit passes.

## Default Workflow

Three-step pipeline for any non-trivial change:

```
plan mode  →  /impl  →  /ddev  →  /dprod (on request)
(agree)       (build)    (gate)    (ship)
```

**Plan before implement** — always enter plan mode for non-trivial tasks. During plan mode, write `.claude/PLAN.md` using the format below, then call ExitPlanMode for operator approval.

**Operator's role**: requirements, design, defect identification — plan mode only.  
**Claude's role**: research, implementation, test loops, doc updates, deployment — background.

**Implement** (`/impl`): reads `.claude/PLAN.md` → dispatches agents → loops tests to green → commits. Never pushes.  
**Dev deploy** (`/ddev`): pytest + svelte-check green → push dev. Never push dev with failing tests.  
**Prod deploy** (`/dprod`): operator explicitly requests → docs/spec/DESIGN_GUIDE/PDF/CC updated → merge dev→main → push. Never push to prod without explicit request.

### Plan file format (`.claude/PLAN.md`)

Write this file during plan mode before calling ExitPlanMode:

```markdown
# Plan: <short title>

## Task
<what needs to be done — 2-5 sentences>

## Agents
- backend: <task for backend agent, or "skip">
- frontend: <task for frontend agent, or "skip">
- broker: <task for broker agent, or "skip">
- doc: <task for doc agent, or "skip">
- backend-test: <task for test agent, or "skip">
- playwright: <task for playwright agent, or "skip">

## Tests
- pytest: yes/no
- svelte-check: yes/no
- playwright: yes/no

## Commit message
<draft commit message>

## Done when
<human-readable done criteria>
```

Keep agent tasks self-contained — each agent gets its section text as its full brief.

## Scope Discipline

When a change is tied to a specific entity (role, company, page, file, symbol): confirm scope before editing broadly. Do NOT propagate changes to sibling entities unless explicitly asked.

## Long-Running Agents

- Cap background agents at ~30 min wall-clock. If a task hasn't returned by then, surface status and ask before continuing.
- Verify any "broken import" claim by actually running the import before reporting — past runs surfaced false positives.
- When dispatching parallel agents for the same logical task, bundle related files into one agent rather than spawning duplicates that touch the same modules.

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
ceiling in `kite.py:place_order` is bypassed when `intent="close"` — close orders of any
size are allowed through; the ceiling only guards new open orders.

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
| Update feature spec | `docs/specs/<NAME>_SPEC.md` |
| Update operator guide | `docs/guides/<NAME>_GUIDE.md` |
| Update architecture doc | `docs/DESIGN_GUIDE.md` → regenerate with `python3 docs/generate_pdf.py` |
| Add audit snapshot | `docs/audits/AUDIT_<TOPIC>.md` |
| Update ops runbook | `docs/deployment.md` |

---

## Custom slash commands

Workflow shortcuts in `/.claude/commands/`:

- **`/impl`** — Read `.claude/PLAN.md`, dispatch agents, loop tests to green, commit — ready for `/ddev`
- **`/ddev`** — Run tests (pytest + svelte-check); push to dev only if both pass
- **`/dprod`** — Update docs/spec/DESIGN_GUIDE/PDF + CC gate; merge dev→main; push prod
- **`/tlm`** — Run daily TLM audit pipeline, parse P1 findings, fix + commit
- **`/cc`** — Show cyclomatic complexity grades (C/D/E/F summary + top 10 hotspots)
- **`/push`** — Quick push dev+main (no gates — use only for doc/config-only changes)
- **`/audit-cc`** — Block push if any D/E/F-grade functions exist; unblock if clean
