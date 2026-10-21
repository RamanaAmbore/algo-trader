# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Development Commands

**Backend tests** (requires `venv/`):
```bash
venv/bin/pytest backend/tests/ -q --tb=line          # all tests
venv/bin/pytest backend/tests/test_X.py -v           # single file
venv/bin/pytest backend/tests/broker/ -q --tb=line   # broker-layer only
```
Set `PYTEST_RUNNING=1` (conftest does this automatically). Do NOT run with `RAMBOQ_USE_CONN_SERVICE=1` unless the conn service is running.

**Frontend type check** (canonical gate before push):
```bash
cd frontend && npx svelte-check --output machine 2>&1
```

**Frontend dev server** (local only — prod/dev deploy via webhook):
```bash
cd frontend && npm run dev    # vite dev server
cd frontend && npm run build  # production build
```

**E2e tests** (targets dev.ramboq.com or localhost):
```bash
cd frontend && npx playwright test
cd frontend && npx playwright test --ui   # interactive
```

**Complexity gate** (blocks push if D/E/F grade):
```bash
venv/bin/python -m radon cc backend/ -s -n D
```

---

# RamboQuant Project Reference

Sprint diaries + completed-slice history live in [CLAUDE_HISTORY.md](CLAUDE_HISTORY.md).

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

- **Alias**: `claude-sonnet` → currently `claude-sonnet-5` (see global `~/.claude/CLAUDE.md` Model Selection for the canonical definition).
- **Default**: `claude-sonnet` for all agents (frontend, backend, broker, audit). Haiku only per the table below. Opus ONLY when operator explicitly says "use opus".
- Local Qwen proxy (`qwen on|off|status`) routes haiku model IDs to LM Studio when enabled — prefer it for cheap orchestration to save cost.

## Multi-agent coordination (read first)

Specialized subagents in `~/.claude/agents/` dispatched in parallel by default:

| Agent | Layer | Use | Model |
|---|---|---|---|
| `broker` | Layer 1 | `backend/brokers/` — connections, ticker, service, adapters, resilience | claude-sonnet |
| `backend` | Layer 2 | `backend/api/` — routes, models, background, persistence, algo engine | claude-sonnet |
| `frontend` | Layer 3 | `frontend/` — SvelteKit, Svelte 5, ag-Grid | claude-sonnet |
| `backend-test` | Layer 1+2 | pytest + pytest-asyncio — broker + API tests | haiku |
| `playwright` | Layer 3 | Playwright e2e — browser flows, mobile viewport | haiku |
| `audit` | All | Read-only defect review — no writes | claude-opus-5-5 |
| `doc` | All | CLAUDE.md / docs/guides/ / docs/specs/ | haiku |

**Parallel by default** — independent sub-tasks fire together. Sequence only when 
one output feeds another or when audit finds defects.

## Test Coverage Rules (Hard Gates — No Exceptions)

Every code change must be paired with a test that covers the changed lines. This applies to every file in every commit — no waiver for "small" changes, refactors, or one-liners.

**Per-file test location map:**
| Changed file | Required test |
|---|---|
| `backend/brokers/*.py` | `backend/tests/broker/test_*.py` |
| `backend/api/**/*.py` | `backend/tests/test_*.py` |
| `frontend/src/lib/data/*.js` | `frontend/src/lib/__tests__/data/*.test.js` (Vitest) |
| `frontend/src/lib/*.js` or `*.svelte` | `frontend/e2e/*.spec.js` (Playwright) |

**Coverage thresholds (enforced in `/ddev` and `/dprod`):**
- `backend/brokers/` ≥ **80%** — blocks push/merge if below (connections.py + service/app.py need live conn service; structurally unreachable in CI)
- `backend/api/` ≥ **45%** — blocks push/merge if below (auth.py/orders.py/ws.py structurally undertested; 47% actual baseline)
- Vitest (`npx vitest run`) must pass with 0 failures

**Enforcement:** `/impl` self-audit (Step 4) diffs every changed source file and requires a corresponding test change. If missing, a test agent is dispatched before commit. `/ddev` runs `coverage report --fail-under` gates. Neither step can be skipped.

## Bug Fix Workflow (Self-Audit Required)

After implementing any bug fix:
1. Run a self-audit pass — check for structurally unreachable code, overwritten state, SSOT consistency.
2. For any P&L / NavStrip / market-data fix: grep all consumers (derivatives, dashboard, NavStrip, MarketPulse) and verify the fix propagates to every one of them — not just the primary page.
3. Check `git diff --name-only HEAD` — every changed source file must have a corresponding test change in the same commit. If any are missing, dispatch a test agent before committing.
4. Only commit after the self-audit passes.

## Default Workflow

Four-step pipeline for any non-trivial change:

```
plan mode  →  /impl        →  /ddev              →  /dprod (on request)
(agree)       (build on       (sync + merge          (merge dev→main,
               workshop)       workshop→dev,           push prod)
                               test, push dev)
```
Or use **`/depl`** to run all three phases in one command.

**Branch strategy** (as of 2026-08-26):

| Branch | Purpose | Deploy target |
|---|---|---|
| `workshop` | All active implementation work — commits land here first | none (local only) |
| `dev` | Testing / staging — workshop merges here after passing tests | dev.ramboq.com |
| `main` | Production — only merges from dev on explicit operator request | ramboq.com |

**Never commit directly to `dev` or `main`.** ALL commits — implementation and docs — go to `workshop` first.

**Branch sync invariant**: when any phase completes, all three branches must be at the same commit.  
After `/dprod` finishes: `git checkout workshop && git merge main --ff-only && git push origin workshop` — always.

**Plan before implement** — always enter plan mode for non-trivial tasks. During plan mode, write `.claude/PLAN.md` using the format below, then call ExitPlanMode for operator approval. After ExitPlanMode, output exactly: *"Plan ready — run `/impl` to build only, or `/depl` to build + deploy to prod."* Then **STOP**. Do not start implementing. Do not ask for permissions. Do not take any further action. Wait silently for the operator to run `/impl`, `/ddev`, `/dprod`, or `/depl`.

**Operator's role**: requirements, design, defect identification — plan mode only.  
**Claude's role**: research, implementation, test loops, doc updates, deployment — background.

**Implement** (`/impl`): reads `.claude/PLAN.md` → dispatches agents → loops tests to green → commits to `workshop`. Never pushes.  
**Dev deploy** (`/ddev`): sync remote+local dev first (`git fetch origin && git checkout dev && git pull origin dev`), then merge workshop→dev, run pytest + svelte-check, push dev only if all pass.  
**Prod deploy** (`/dprod`): operator explicitly requests → all doc/spec commits go to `workshop` first → merge workshop→dev → merge dev→main → push all three. Final step: `git checkout workshop && git merge main --ff-only && git push origin workshop` so all branches are in sync.  
**Full pipeline** (`/depl`): impl → ddev → dprod in one command, bypass-permissions throughout.

**Sync rule before every workshop→dev merge**: always pull `origin/dev` into local `dev` before merging workshop, to avoid divergence conflicts.

**Sync rule after every dprod**: fast-forward workshop to main (`git checkout workshop && git merge main --ff-only && git push origin workshop`) so all three branches end at the same commit.

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
`market_holidays` (daily 05:30 IST refresh, retry 30min until 08:00 IST) → NSE API (cold-boot). 
Empty sets cached; buster = date rollover Tiers 1+2, UPSERT Tier 3.

**Market segments** — blocks carry `sessions: list[{start, end}]` + `evening_open_on_holidays`. 
`is_market_open()` signature unchanged; keyword-only overrides when passed. Open/closed checks go
through `_segment_is_open()` (see the "Market open/close determination" entry under Market daily window).

**Multi-account calls**: `@for_all_accounts` returns list[DataFrame]. Callers use `pd.concat(..., ignore_index=True)`.

**Account masking**: `mask_account(s) → str` (digits → #). Used in all alerts + summaries.

**Singleton Connections** — thread-safe startup init. On `RAMBOQ_USE_CONN_SERVICE=1` populates 
registry with RemoteBroker stubs.

**RemoteBroker.translate_qty** — `RemoteBroker` (active when `RAMBOQ_USE_CONN_SERVICE=1`)
inherits a no-op `translate_qty` from the base class; it MUST override to forward to the
conn service so MCX/NCO contracts→lots translation happens correctly. Fixed 2026-07-15:
`backend/brokers/client/remote_broker.py` delegates via `self._call("translate_qty", ...)`.
Any new broker proxy layer must do the same — failing to do so sends raw contract qty
(e.g. 100 contracts) as 100 lots to the Kite adapter, hitting the 50-lot ceiling.

**Closed-hours route gate** — `closed_hours_or_broker()` in `snapshot_gate.py` CANONICAL gate. 
Invariant: `broker_fn` NEVER called when closed. Returns source tags: `'live'` / `'snapshot'` / 
`'snapshot-fallback'`. Every new data route MUST use. Tests patch `_any_segment_open()`.

**Staleness indicator freeze rule — any degraded/failed fetch must freeze to last-known-good** (2026-09, fixes A1–A4 + B-C) — When any broker data fetch degrades or fails (not just market-close), ALL downstream consumers must show the last-known-good cached value with explicit staleness marking, never silently collapse to 0/blank. Root cause was `backend/brokers/client/sync.py:_fetch_per_account` (A1, lines 51–91) treating HTTP 200 with `accounts=[]` + `errors: [...]` (a genuine failure masked as empty) identically to a real empty-book result. Fix chain: (1) `sync.py:_failed_sentinel()` (lines 42–48) + the `payload.errors` check (lines 77–82) surfaces all degraded responses. (2) `backend/api/routes/positions.py:_is_positions_outage()` (lines 625–651) detects both "all per-account failed" AND "empty list with configured accounts" cases, treating both as outages (A2). (3) `_accounts_flagged_stale()` (lines 654–677) collects stale/failed account codes and passes them to `PositionsResponse.stale_accounts` (line 934, 1021). (4) Frontend `dataStore.svelte.js` (line 135, documented 121–134) adds a `meta` extractor to preserve stale_accounts + source tags across the fetch–parse boundary; consumers (`PositionStrip`, `portfolioStore`) check the metadata to decide between "show last-good + stale badge" (degraded) vs. "show real 0" (confirmed empty). (5) Stale accounts render with CSS class `row-account-stale` (app.css:819–831) — slate desaturation + diagonal hatch — matching the existing `STALE@HH:MM` badge (pulseColumns.js:417). Invariant: any response carrying `stale_accounts` or `source='snapshot-fallback'` is never written to Tier 2 (localStorage) to prevent next page load from poisoning the cache with a masked-failure value. See memory `feedback_market_close_snapshot`.

**Cross-account aggregates must not freeze on single-account degradation (2026-09-29,
commit bc7526f9)** — The freeze-to-LKG rule applies per-account at the backend
(`_stale_substitute_frame` per account) but the FRONTEND must implement
include-not-exclude aggregation: margin/cash totals sum every non-TOTAL row ACTUALLY
PRESENT in the data (including stale accounts with substituted last-known-good values),
never freezing the entire cross-account total to a remembered scalar when one account
goes stale. Old design: degraded account → remembered scalar frozen → whole total
becomes zero on page-load if any early poll is degraded. New design: sum all rows; a
missing/absent account simply contributes nothing, yielding a partial total. Returns
`null` only when `fundRows` itself is null/empty (genuinely unknown, no poll ever
landed). Implementation: `fundsAggregate.js` helpers (`sumMarginAvail`, `sumMarginTotal`,
`sumLiveCashTotal`) loop every non-TOTAL row and sum what's there; `PositionStrip.svelte`
`fmtMoney()` renders `null → '—'`. Invariant: a per-account stale flag never blocks
the cross-account aggregate or causes a false 0.

**Reactive safety (state_unsafe_mutation prevention)** — Never call `get(store)` directly inside `$derived(...)`. Always wrap in `untrack()` or use `safeRead(store)` from `frontend/src/lib/utils/safeRead.js`. For symbol data, use `liveSnap(sym)` from `symbolStore.svelte.js`. The `state_referenced_locally` compiler warning surfaces these at build time — do NOT suppress it globally; add per-line `svelte-ignore state_referenced_locally` with a one-line justification comment at each suppression site.

**Module-level fetch caches must never cache a failure as a truthy empty result (2026-09-30, commit 85ca09f5)** — `frontend/src/lib/data/templates.js:loadOrderTemplates()` used `if (_templates) return _templates;` to memoize a module-level fetch. An empty array (`[]`) is truthy in JS, so the very first time the fetch failed (a transient auth-token-not-yet-attached race very early in page load, or a momentary network blip) the catch block's `_templates = []` got returned as the cached value forever after — no retry, ever, for the rest of that browser tab's session, regardless of how many components remounted or re-called the loader. This silently broke `TemplateBar.svelte`'s Chain-tab toggle (gated on `_templates.length > 0`) for a real operator session while automated Playwright tests (whose auth fixture pre-seeds a valid token before navigation, avoiding the race) never reproduced it — a two-stage debug bypass (temporarily disconnecting the button's own logic, then the mount gate one level up) was needed to rule out both before finding the actual cache-layer bug. Root-cause diagnosis pattern worth reusing: "works in fresh automated test runs, doesn't work in a real persistent browser session even after clearing cache" points at in-memory module state surviving across soft-navigation/re-renders within the same tab, not at HTTP/site-data caching (which "clear cache" UI usually addresses) and not at the component's own render logic. Fix: on a thrown exception, leave the module-level cache variable at its initial unset value (`null`, not `[]`) so the next caller genuinely retries; only a real successful response (including a genuinely-empty one) is cached. Invariant: a module-level "fetch once, cache forever" pattern must distinguish "fetch failed" from "fetch succeeded with nothing" — only the latter is a valid, cacheable answer.

**Broker auth health badge** — `BrokerHealthBadge.svelte` (admin/designated navbar, polls 30s 
via `visibleInterval`). State: green (last_good < 5min), amber (stale), red (last_fail > last_ok). 
Worst state drives color. Click opens per-account modal.

**Agent action-dispatch SSOT (2026-10-08)** — `_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS`
in `backend/api/algo/actions.py` are the explicit, sole action-dispatch tables — real broker
actions route through `_dispatch_live_action()` to `_action_live_*` functions in
`actions_live.py`; non-broker noops (`monitor_order`/`deactivate_agent`/`set_flag`/`emit_log`)
route through `_al_run_noop_handler()`. `GrammarRegistry`'s `actions` table
(`grammar_registry.py`) is catalog metadata only — token name + `params_schema` — and is never
live-dispatched (`REGISTRY.action()` has zero callers); `routes/grammar.py` only reads
`len(REGISTRY.actions)` for an admin stats count. The `grammar_tokens` DB rows' own
`params_schema`/`description` columns (distinct from the in-memory `REGISTRY.actions` dict)
are what `agent_ai.py`'s Lab-chat agent-builder is designed to read via `_grammar_snapshot()`
to describe available actions to the operator. The 7 dead `_log_invoke()`-only
grammar-resolver stub functions that used to seed this table's `resolver` field
(`place_order`/`modify_order`/`cancel_order`/`cancel_all_orders`/`chase_close_positions`/
`expiry_auto_close`/`close_position`) were deleted from `actions.py`, and the matching
`resolver:` lines in `backend/config/grammars/agent_grammar.yaml` set to explicit `resolver:
null` (NOT deleted — an absent key leaves `seed_grammar_tokens()`'s upsert, which does
`spec.get('resolver', row.resolver)`, preserving an already-deployed DB row's stale dotted-path
string forever; an explicit `null` makes the upsert overwrite it with `None`). `_load_action`
degrades gracefully to `fn: None` when `resolver` is falsy, so `REGISTRY.actions` for these 7
tokens is `{token: {"fn": None, "params_schema": {...}}}` on both fresh and already-seeded
databases.

**Rich-alert ntfy priority must come from the REAL triggering agent's tier, never hardcoded or from the dispatcher's own (2026-10-16, live operator-reported incident)** —
`backend/api/algo/event_agents.py:RICH_ALERT_AGENT` is the one dispatcher that delivers
telegram/ntfy/email for EVERY agent's alerts in the system (a generic fan-out multiplexer,
itself always `tier="medium"`) — it is NOT the real triggering agent. Its ntfy channel used to
hardcode `"priority": "urgent"` unconditionally. `send_ntfy_alert()` (`alert_utils.py`)
deliberately sends an urgent-priority push 3 TIMES in a row (`send_count = 3 if priority ==
"urgent" else 1`) so a genuinely critical alert isn't missed on a phone in Do-Not-Disturb — the
hardcode meant EVERY alert, including routine info-tier scheduled notifications
(`market-open-nse`/`market-preclose-mcx`), got this treatment. Live symptom: operator saw 3 ntfy
pushes for one "NSE market open" notification. Fix, three pieces: (1) `_v2_send_rich_alert()`
(`agent_engine.py`) now threads the real agent's `tier` into its logged extras (the log record
`RICH_ALERT_AGENT` matches on). (2) `RICH_ALERT_AGENT`'s ntfy channel no longer hardcodes a
priority. (3) `event_agents.py:_dispatch_agent()`'s per-channel loop resolves ntfy priority from
the record's carried-through `tier` (via `_TIER_TO_NTFY_PRIORITY`, now including an explicit
`"info": "low"` entry — it was previously left unmapped on the assumption that falling through to
`send_ntfy_alert`'s own clock-based day/night default was safe; that default is "urgent" for
essentially the whole IST trading day, so an unmapped tier silently got the same 3x treatment) —
only when the channel itself sets no explicit priority, so a future per-channel override still
wins. Any OTHER log-driven event agent whose records don't carry a `tier` (fill/error/
chase_cancel/etc.) is unaffected — falls through exactly as before, using `_send_channel`'s
existing `agent.tier` fallback (correct for those, since each is tied to one real event, not a
generic multiplexer). Tests: `test_alert_golden.py::test_rich_alert_priority_resolved_from_tracked_tier`
(all 5 real tiers, confirmed to fail pre-fix for every tier except critical) +
`test_notification_header_and_priority.py::TestTierToNtfyPriorityMapping`. Invariant: a generic
multiplexer agent (one that renders OTHER agents' alerts) must never use its own tier/hardcoded
priority for a per-record notification — only the real triggering agent's tier.

**Alert evaluation and latching** (2026-09, fixes 11 confirmed loss/rate-of-change condition bugs) —
Agent condition evaluation enforces three critical invariants via the alert engine 
([`backend/api/algo/agent_engine.py`](backend/api/algo/agent_engine.py)):

  **Missing-vs-zero convention (fix #3):** Broker adapters and grammar resolvers must return
  `None` (not a coerced `0`) when a funds/margin field is genuinely absent or unmapped — only
  a broker-confirmed real `0` should trigger a threshold. Applied to RAW broker columns only
  (`cash`, `sod_cash`, `avail_margin`, `used_margin`, `collateral` via `_num_or_none()` in
  [`grammar.py:76–86`](backend/api/algo/grammar.py#L76-L86)); does NOT apply to computed aggregates
  (`pnl`, `day_val`, `day_pct`, etc.) which are `.fillna(0)` by the background summary builder.
  Dhan/Groww adapters use `_dhan_num_or_none()` ([`dhan.py:1591–1601`](backend/brokers/adapters/dhan.py#L1591-L1601))
  and `_gf_or_none()` ([`groww.py:1475–1490`](backend/brokers/adapters/groww.py#L1475-L1490))
  respectively — these check `is not None` (not truthiness), so a real `0` on the first
  present key passes through. Kite tolerates `None`-safe lookups from `.get()` natively.

  **Missing-vs-zero convention extended to display routes (2026-09-29, commit bc7526f9,
  amends existing entry)** — The convention has been extended from adapter + grammar layers
  to the display route layer (`backend/api/routes/funds.py`). `FundsRow` schema fields
  `cash`, `avail_margin`, `used_margin`, `collateral`, `live_cash`, `option_premium` are now
  `float | None` (not blanket float). Backend `_fetch()` applies targeted fillna, excluding
  funds-meaning columns from blanket zero-fill (see line 172: `[c for c in numeric_cols if c
  not in _COL_MAP]`). `_append_total_row` aggregation uses Polars `.sum()` which skips nulls,
  so a TOTAL row is null for a column only when EVERY account is null (never synthesizes
  false "confirmed zero"). Frontend `fundsAggregate.js` helpers (`sumMarginAvail`,
  `sumMarginTotal`, `sumLiveCashTotal`) return `null` only when `fundRows` is null/empty
  (never polled yet); per-field nulls coerce to 0 via `Number(x || 0)` for individual
  account sums. Invariant: a missing broker field survives the API response and is
  preserved in `FundsRow`; frontend null-guard (`fmtMoney` line 579) renders `null → '—'`.
  
  **Per-leaf latch model with hysteresis and escalation (fixes #8/#9):** `_V2_LATCH` (keyed by
  `(agent_slug, metric, scope, account)`) tracks one independent latch per LEAF per ACCOUNT.
  Recovery clears latches only when a value recovers past the re-arm band: re-arm threshold = 
  `thr − worse_dir × 0.2 × |thr|` (fix #9, hysteresis via `_v2_recovered_past_band()` at
  [`agent_engine.py:86–105`](backend/api/algo/agent_engine.py#L86-L105)). Escalation re-fires
  at linear multiples: after cooldown elapsed, an ordered-op leaf re-fires when value moves
  ≥|threshold| further in the worse direction (`_v2_leaf_should_fire()` at
  [`agent_engine.py:108–151`](backend/api/algo/agent_engine.py#L108-L151)); zero-threshold
  leaves (e.g. `cash < 0`) re-fire on cooldown alone. Recovery runs EVERY tick
  (`_v2_apply_recovery()`) regardless of whether matches exist, and MUST run before escalation
  gating. Never treat an absent key (fetch timeout/failure/empty frame) as recovered (fix #5).
  
  **Deploy-survival hydration (fixes #7/#8/#9):** Without persistence, every process restart
  re-fires every standing breach. Fix: `_v2_hydrate_latch()` (async, called once per process
  at [`agent_engine.py:2170`](backend/api/algo/agent_engine.py#L2170) inside `run_cycle()`)
  seeds `_V2_LATCH` from today's `agent_events` rows (both `triggered` and `triggered_suppressed`
  event types — fix #7 makes suppressed fires also record a latch). Helper `_hydrate_latch_from_rows()`
  at [`agent_engine.py:206–230`](backend/api/algo/agent_engine.py#L206-L230) reuses the EXISTING
  `agent_events` table schema (detail JSON already carries metric/scope/account/value), no
  migration required. Hydration reads only today's IST-dated rows; `_V2_LATCH_HYDRATED` flag
  guards against retry-on-failure; daily reset via `_maybe_reset_v2_state()` wipes the latch
  at trading-day rollover.
  
  **Cash and start-of-day cash tokens (fix #4):** The `cash` metric now reads live available
  cash from `'avail cash'` column (mapped to `live_cash` via [`routes/funds.py:31`](backend/api/routes/funds.py#L31)),
  not start-of-day balance. A new `sod_cash` token (returns `'avail opening_balance'`) preserves
  the old start-of-day reading for agents that specifically need the intraday-stable baseline.
  The token name `cash` was kept unchanged so existing agents (e.g. prod's `loss-funds-negative`)
  get the corrected live-cash semantics without requiring re-save.

  **Kind classification for cash/avail_margin metrics must consider the leaf's operator (2026-09-28, commit 36aba654)** — Schedule-only sentinel conditions (`market-open-nse` and `market-preclose-mcx` agents use `avail_margin >= -999999999` purely to gate their `fire_at_time` schedule) were being mislabeled as real threshold breaches by the alert display layer. Root cause: `_v2_derive_kind()` in [`backend/api/algo/agent_engine.py`](backend/api/algo/agent_engine.py) classified based on metric name alone (`avail_margin`, `cash`), ignoring the operator. Fixed: (1) `_v2_derive_kind(metric, op)` now takes the operator; only `<`/`<=` operators classify as `negative_cash`/`negative_margin` floor breaches. (2) `_v2_format_threshold()` belt-and-suspenders guard: thresholds with `abs() >= 1e8` render as `"n/a"` instead, defending against future similar sentinels. (3) `_v2_send_rich_alert()` renders scheduled info-tier agents (`fire_at_time` set, `tier` in `['info', 'low']`) as `"{agent.name} — Scheduled — {fire_at_time} IST"` in Telegram/email, replacing the kind/threshold table to clarify the alert's purpose. `loss-funds-negative` (tier=critical, uses `op: "<"`) and genuinely alert-firing agents unaffected — verified via golden byte-for-byte render tests. Invariant: alert kind classification must never treat a schedule-only sentinel as a real threshold breach.

**Market daily window** — 08:00–23:31 IST. Ticker stops at 23:31; closed window 23:31→08:00 IST —
routes serve the `daily_book` snapshot only. Settlement snapshots: NON-MCX at 15:45 and MCX at 23:45
(close+15 min) write `ltp` and never touch `prev_close`. The 08:00 job
`fix_daily_book_prev_close()` ([`daily_snapshot.py:1166`](backend/api/algo/daily_snapshot.py#L1166),
scheduled from `_ds_tick_prev_close_fix` at [`background.py:3463`](backend/api/background.py#L3463))
is **no longer the mechanism that sets `prev_close` in normal operation**. Its settlement-map UPDATE
([`daily_snapshot.py:1239-1250`](backend/api/algo/daily_snapshot.py#L1239)) targets
`WHERE d.date = today` (the new session's date), and no rows dated that day exist before 08:00:
snapshots written 00:00–07:59 are attributed to the prior session by `session_date_of()`
([`expiry_freeze.py:180`](backend/api/algo/expiry_freeze.py#L180)). In normal operation the UPDATE
matches zero rows. The job still runs and records `task_markers.prev_close_fix_date` via
`_pcf_mark_done()`. Its live effect is the catch-up path: `_sg_recover_close_reset()`
([`background.py:3177`](backend/api/background.py#L3177)) and the loop tick both consult that same
marker through `_pcf_marker_date()` ([`background.py:3133`](backend/api/background.py#L3133)), so a
second run on the same IST day is blocked. **Late-catch-up stomp guard (fixed, commit `8b4f516e`):**
the marker alone only prevented a *duplicate* run — it did not stop a *missed* 08:00 run from catching
up *after* that same day's own EOD write (15:45/23:45), which would have overwritten the EOD row's
already-correct `ltp` with the stale morning settlement `close_price`. Fixed via
`_pcf_eod_already_fired_today(today)`, consulted by both call sites before running
`fix_daily_book_prev_close`: checks `_snapshot_fired_today` first, then falls back to a direct DB
existence check against the UPDATE's own target set (`date=today AND kind IN ('holdings','positions')`)
— the fallback closes a second gap found while fixing the first: `_preload_snapshot_sentinels()` only
seeds from `kind='positions'` rows, so a holdings-only day would have left the sentinel unseeded and
the stomp would still have gone through. Degrades to "proceed" (not a hard failure) on any DB error.
In steady-state operation this job is now a no-op every single trading day — `_build_settlement_map()`
still makes a real broker round-trip for a job that then matches zero rows; removing the job entirely
is an operator decision, not made here. Full schedule: memory `project_market_daily_window`.

**`close_settled` double-fire on midnight rollover (2026-10-20, commits `5295f432`, `4b2b4f17`,
`2220074d`)** — Root cause: `MarketLifecycle.poll()`
([`market_lifecycle.py`](backend/api/algo/market_lifecycle.py)) reset `_settled_fired` for every
exchange on a bare calendar-date rollover. `_last_close_at` is not cleared across midnight, so for an
exchange already closed and settled, `now >= last_close + settled_offset` was true the instant the
flag cleared. `close_settled` fired a second time after midnight, and `snapshot_daily_book(settled=True)`
wrote a second row dated the next calendar day. That row's `prev_close` came from a broker
`close_price` that had not yet settled, so it equalled `ltp`.
Fix, four parts: (1) The rollover reset is deleted (comment block at
[`market_lifecycle.py:158-173`](backend/api/algo/market_lifecycle.py#L158)); `_last_seen_date` is gone,
guarded by `test_no_dead_last_seen_date_field`. The only re-arm points are the open transition
([`:203`](backend/api/algo/market_lifecycle.py#L203)) and, added in `2220074d`, the close transition
([`:229`](backend/api/algo/market_lifecycle.py#L229)), which clears a flag hydrated from the prior
close cycle. (2) `_hydrate_from_audit()` ([`:315`](backend/api/algo/market_lifecycle.py#L315))
restores `_last_close_at` and `_settled_fired` from `MarketLifecycleEvent` rows (26h lookback) once
per process. An already-elapsed settle window is marked handled and never fired late. (3)
`snapshot_daily_book(target_date=None)` defaults to `session_date_of(now_ist)`
([`daily_snapshot.py:1403-1416`](backend/api/algo/daily_snapshot.py#L1403)), not `now_ist.date()`.
Explicit `target_date` callers are untouched. (4) `_session_guard()` uses the same session date for
the EOD sentinel checks only ([`background.py:3377-3378`](backend/api/background.py#L3377)).

**Downstream regression this fix exposed, also fixed (commit `047aaf9e`):**
`positions.py:_positions_snapshot`'s closed-hours qty=0 keep-filter
(`db.qty != 0 OR db.date = :today_ist`) used bare `now.date()` for `:today_ist`. Before the
`close_settled` fix above, the double-fire bug's spurious midnight-dated batch happened to match
that bare date, which is what kept a flat (`qty=0`) closed-out position visible in the closed-hours
grid overnight — removing the double-fire removed that accidental support, and flat positions
started silently disappearing from the grid during 00:00–08:00 IST (and across weekends/holidays),
violating the "never blank during closed hours" invariant above. Fixed by walking `:today_ist` back
(bounded, 10 iterations, via `fetch_holidays("NSE")` + `is_trading_day()`, no live probe) to the most
recent real NSE trading day, and changing the comparison to `db.date >= :today_ist` — `>=` is
load-bearing on its own: an MCX evening-session EOD batch can legitimately be dated ON an NSE
holiday, which `=` would wrongly exclude after the floor-walk steps past it. `holdings.py`'s
equivalent reader has no analogous qty/date filter at all — confirmed unaffected, no parallel fix
needed. One Sunday-specific case changed behavior as a side effect (a flat position previously
hidden by an unrelated `snapshot_cutoff` boundary quirk is now correctly shown) — a second instance
of the same invariant being restored, not a new regression.

**Deploy coupling — these commits are not independently shippable.** Cherry-picking a subset
reproduces a version of the regression this section describes: `5295f432` requires `a3a88aac` AND
`047aaf9e`; `4b2b4f17` requires `2220074d`; `2a549a3d` requires `8b4f516e`; `0b203d07` requires
`fdfa37a5`. Ship the full range together.

**Empirical note — corruption is narrower than first reported.** The first analysis concluded the
corruption happened "every single night". That came from a flawed check: filtering by `captured_at`
time only catches weekend-frozen artifacts, because a weekday double-fire row is overwritten by that
day's legitimate evening write a few hours later. The corrected method compares each `ltp` with the
next weekday's `prev_close` (prod, 2026-09-01 → 2026-10-19): **88.7% agreement (2731 of 3079
pairs)**. All disagreement sits in one window, 2026-09-07 → 2026-09-18. **Zero disagreements from
2026-09-19 → 2026-10-19.** The practical blast radius is weekend and holiday boundaries, where no
same-day write corrects the row.

**Open item, not root-caused — but likely explained.** The 2026-09-07 → 2026-09-18 disagreement
cluster is not confirmed-fixed by this change or by any code path identified so far; treat it as
unresolved, not as covered by this fix. It most likely reflects the system being mid-refactor on this
exact mechanism during that window — `daily_snapshot.py`, `background.py`, and `positions.py` all had
multiple structural commits landing 2026-09-05 → 2026-09-18 (e.g. `9f58d943` "remove settlement
snapshots; broker.close_price as prev_close reference," `ec95b7db` "calendar-driven prev_close —
two-CTE on closed days," `b7b4d84c` "remove two-CTE snapshot query"). This is corroborating,
contemporaneous commit history, not proof of causation. Monitor with the `ltp`→`prev_close` comparison in the archived plan
([`.claude/PLAN.done37.md`](.claude/PLAN.done37.md)).

**Single arbitrated source per field (Pulse, 2026-10-20, commits `9a983ca5`, `8d6be90e`)** — When one
row shows fields that must move together (`ltp`, `close`, `change_pct`, `day_pnl`), exactly one
arbitrated primary source feeds each field. Any secondary channel (REST batch quote, raw broker OHLC,
a bespoke copy of the apply logic) is fallback-only.

| Field | Primary source | Fallback-only |
|---|---|---|
| Positions/Holdings `ltp` | SSE tick via `symbolStore` (`snap.ltp`) | REST batch quote (`liveQ.ltp`) |
| Day P&L % denominator (`_prev_market_value`) | Backend `prev_close`, accumulated per leg in `_prevCloseSsot` / `_prevCloseHoldSsot` ([`pulseUnified.js:455`](frontend/src/lib/data/pulseUnified.js#L455), [`:567`](frontend/src/lib/data/pulseUnified.js#L567)) | `\|avg × qty\|` cost basis when no leg has a prior settlement ([`finalizeRows`, `pulseUnified.js:836-840`](frontend/src/lib/data/pulseUnified.js#L836)) |
| Underlying-anchor row fields | `_applyQuoteFields(row, snap, q)` at [`pulseUnified.js:712`](frontend/src/lib/data/pulseUnified.js#L712) and [`:730`](frontend/src/lib/data/pulseUnified.js#L730), the same entry point real rows use | None. The bespoke `_applyUnderlyingQuoteFields` is deleted |

`row.close` stays live-quote-sourced for bid/ask/depth display and is deliberately not the Day P&L %
denominator. **Two columns are headed "Chg %"**: the left grid `change_pct` column
([`pulseColumns.js:496`](frontend/src/lib/data/pulseColumns.js#L496)) is the raw symbol day-change
(no qty, live-quote-sourced by design); the right-grid `day_pnl_pct` column
([`pulseColumns.js:643`](frontend/src/lib/data/pulseColumns.js#L643)) is Day P&L ÷ prev market value,
and it is the one this fix changes. Movers keep their own poller and are not consolidated. Spec:
PULSE_SPEC §11.3.

**Market open/close determination — one call path for display routes; order paths prewarmed
(2026-10-20; prior round `0b203d07`, `fdfa37a5`, `9e591248`, `0cabb3ec`, `a7c3375f`, `0e304f40`;
this round `691a7373`, `274019bb`, `59b4c01b`, `634ce8e4`, `940debd2`, `b79cfbb0`, `eb94f67e`,
`c6a947c7`, `c0fabec3`, `eae36fca`)** —
"One path" means one function with one kwarg set, not one literal helper. Canonical helper:
`_segment_is_open(seg_cfg, now)` ([`date_time_utils.py:304`](backend/shared/helpers/date_time_utils.py#L304))
over `is_market_open()` ([`:217`](backend/shared/helpers/date_time_utils.py#L217)).
**Scope of the one-path claim**: it covers the display, status, quote and movers routes listed
below. Order placement, template attach, chase, the agent close-all action and
`market_hours`-scheduled agents did NOT use `_segment_is_open()`. They gated on
`agent_engine.py` `_ae_segment_flags` until the fix described under "Order-path gate status" below.
- **Routed through `_segment_is_open()`** with YAML-shaped config: `/market/status`
  (`routes/market.py` `_compute_market_status`, [`:177-178`](backend/api/routes/market.py#L177)),
  quote gate (`routes/quote.py` `_is_exchange_segment_closed`), watchlist movers gate
  (`routes/watchlist.py` movers handler, [`:1404-1405`](backend/api/routes/watchlist.py#L1404)).
- **Direct `is_market_open()` with gate-shaped dicts** from `_get_segments()`. These pass the same
  `exchange=`, `evening_open_on_holidays=`, `special_sessions=` kwargs but keep each gate's own
  `hours_start`/`hours_end` (the per-date `exchange_schedule` window) instead of the YAML `sessions`
  list: `background.py` `_perf_probe_open_segments` and `_watchdog_check_market_open`, and the
  `routes/simulator.py` guards (`_markets_currently_open`, `_market_hours_block_check`). Commit
  `fdfa37a5` exists because the first pass would have replaced these hours with the YAML sessions
  and broken per-date accuracy.
- **Holiday-branch weekday guard**: the evening-open rule in `is_market_open()` now requires
  `today.weekday() < 5` ([`date_time_utils.py:291`](backend/shared/helpers/date_time_utils.py#L291)).
  Before, a weekend holiday on an evening-open segment read "open" from 17:00.
- **Former gap, fixed 2026-10-20 — `agent_engine.py` `_ae_segment_flags`**. This was NOT a safety
  net layered on a correct check. It was the REAL market-open gate for ticket orders, basket
  orders, GTT/wing template-attach, chase, the agent close-all action, and the `market_hours`
  schedule gate in `run_cycle`. Those sites reached it via `_symbol_exchange_open()` over
  `_build_context()` → `_ae_segment_flags()`. It never honoured `evening_open_on_holidays` or
  `special_sessions`, so it falsely rejected a genuinely open MCX evening session on an NSE holiday
  (live-reported 2026-10-20). Two parts:
  (1) `_ae_segment_flags` now threads `sessions=` / `evening_open_on_holidays=` into
  `is_market_open()`, via `_ae_resolve_matched_window()` (which session window governs the clock, so
  `minutes_since_{prefix}_open` anchors to the real open — 17:00 on a holiday evening, not 09:00) and
  `_ae_had_session_today()` (calendar-only `_closed` check, no probe). The live-probe fallback this
  path used to hit on holidays is removed; it never covered `evening_open_on_holidays` either.
  (2) `special_sessions=fetch_special_sessions(holiday_exchange)` is wired into the same call. The
  DB-backed read cannot be made synchronous from the main loop: `run_on_main_loop()`
  (`backend/shared/helpers/write_queue.py`) cannot block-wait on a coroutine scheduled on the loop it
  is already running on. So the gap is closed by prewarming from async callers, not by the sync path.
  The caches key on `timestamp_indian().date()`, not `date.today()` — the prod/dev server runs on
  UTC (`timedatectl` confirmed), so a system-local key would mis-key the cache every night in the
  IST 00:00–05:30 window.
- **`_EXCHANGE_TO_SEGMENT` was missing `NCO`** (`agent_engine.py`, `_EXCHANGE_TO_SEGMENT`; fixed in
  `a7c3375f`). Every other MCX/NCO call site treated `NCO` like `MCX`; this dict did not, so
  `_symbol_exchange_open("NCO", ctx)` always returned `False`. Every NCO order, basket leg, GTT-wing
  attach and chase was unconditionally rejected as "market closed". Fixed by adding
  `"NCO": "commodity"` — an operator-visible, real-money-path change (NCO now gates on time).
- **Order-path gate status, per call site (no shared denominator — each line is its own status):**
  - Ticket — `_ticket_gate_market_hours_and_align_price()` (`routes/orders_place.py`): **closed**.
    Awaits `_prewarm_special_sessions_for_exchange()` before the sync `_build_now_ctx()`.
  - Basket — per-leg LIVE gate in `routes/orders_basket.py`: **closed**. Same helper, awaited
    inside the leg loop before `_bk_now_ctx()` (commit `0cabb3ec`).
  - Chase — `_chase_order_impl()` (`algo/chase.py`): **closed**. Same helper, awaited before its
    `_symbol_exchange_open` call.
  - Template-attach offhours wing gate — `_check_offhours_wing_gate()` (`algo/template_attach.py`,
    plain `def`). Its only caller, `_apply_template_to_order_impl()` (async), awaits
    `_prewarm_special_sessions_for_exchange(parent_exchange)` immediately before calling it
    (`59b4c01b`, cleaned up in `634ce8e4`). **Closed via the caller**, not the gate.
  - `market_lifecycle.py` `poll()`: **closed**, and the highest-stakes site because it drives
    `close_settled` and `daily_book` snapshot writes. A cold-cache fail-open here would hide a
    DB-confirmed MCX evening closure and fire a spurious `mcx:open` → `close_settled` cycle. Added
    in `691a7373`. It awaits `fetch_special_sessions_async(segment["exchange"])` DIRECTLY for each
    exchange, not via `_prewarm_special_sessions_for_exchange()`. The helper resolves through
    `holiday_exchange`, so the synthesized `cds` entry (raw key `"CDS"`, `holiday_exchange` `"NSE"`)
    warmed the `"NSE"` key while `_exchange_is_open()` read `"CDS"` — CDS stayed cold. Corrected in
    `274019bb`.
  - `apply_plan_live` (`algo/template_attach.py`): **never exposed**. It is reached only via
    `asyncio.to_thread(apply_plan_live, ...)` in `_route_apply_path()`. It runs in a worker thread,
    so `run_on_main_loop()`'s self-deadlock guard never applies and its blocking DB read proceeds
    normally. Earlier docs listing it as exposed were wrong.
  - `_exchange_open_for_attach` (`routes/orders_place.py`): **deleted** in `b79cfbb0`. It was dead
    code with zero production callers, so it was never a real gate. Earlier docs listed it as a
    live ticket-gate call site, which was wrong.
  - `run_cycle` and `actions.py` (`_exchange_gate_passes`): **never exposed**.
    `run_cycle` builds its context via `asyncio.to_thread(_build_context, ...)`, so the cold-cache
    path runs in a worker thread. `actions.py` reads the already-built context dict and does not
    fetch.
  - **4-site residual sweep, closed (2026-10-20, commits `7870d269`, `01bc5057`, `d5a4e70b`,
    `cef9312e`).** The 4 sites previously listed here as "accepted residual, not fixed" — the
    `routes/agents.py` dry-run (`_dry_run_context`), the `routes/watchlist.py` movers gate, the
    `routes/quote.py` segment check, and the `routes/simulator.py` guards — were all confirmed
    genuinely exposed (each reached from a coroutine already on the main loop, no
    `asyncio.to_thread` in between) and fixed with the same prewarm pattern used at the 5 order
    paths above. None turned out to be already-safe.
    - `_dry_run_context` (`routes/agents.py`): awaits the full-dataset `warm_special_sessions_cache()`
      once, before its inline `_build_context(now)` call — a dry-run builds context for every
      segment at once, so it doesn't know a single target exchange in advance the way the
      per-exchange helper needs.
    - `_get_movers_rows` → `_movers_probe_market_state` (`routes/watchlist.py`): new
      `_movers_prewarm_special_sessions()` awaits the `equity` and `commodity` segments' own
      `holiday_exchange` values directly, mirroring `market_lifecycle.poll()`'s direct-await
      pattern rather than `_prewarm_special_sessions_for_exchange()`'s `_EXCHANGE_TO_SEGMENT`
      round trip.
    - `batch_quote` / `batch_sparkline` (`routes/quote.py`): new `_qt_prewarm_special_sessions()`
      re-implements `_qt_is_seg_open()`'s own exchange-membership match rule (segment `exchanges`
      list, falling back to `holiday_exchange`) instead of routing through
      `_EXCHANGE_TO_SEGMENT`, which can drift from this file's own match rule (e.g. it maps
      `NCO`/`BFO`/`BCD` to a segment this file's `exchanges` lists don't cover at all).
    - `_market_hours_block_check` / `_markets_currently_open` (`routes/simulator.py`, 5 call
      sites — `start_for_agent`, `start`, `start_run`, `replay_iteration`, `defaults`): new
      `_sim_prewarm_special_sessions()` awaits `fetch_special_sessions_async` directly on each
      `_get_segments()` entry's own `holiday_exchange` value. Deliberately NOT routed through
      `_prewarm_special_sessions_for_exchange()` — `_get_segments()` can return a GATE name
      (e.g. `"NON-MCX"`) as `holiday_exchange` when the `exchange_clock` cache is warm, a value
      `_EXCHANGE_TO_SEGMENT` does not recognise (would silently no-op). Both gates kept their
      existing sync signatures — converting them to `async def` would have rippled into all 5
      call sites plus the existing sync test suite for this file; each caller instead awaits the
      prewarm immediately before the sync call ("closed via the caller", same pattern as
      `template_attach.py`'s offhours wing gate).
    - The mitigation (`warm_special_sessions_cache()` at startup and after each daily refresh;
      `fetch_special_sessions()` no longer caches a guard-miss as `[]`) remains in place as a
      backstop for any other sync main-loop caller not yet identified.
- **Hardening rounds (items 5–9 of the 2026-10-20 audit), with their real behaviour:**
  - *Malformed NSE feed returns `None`* — `_fetch_mcx_evening_closed_dates()` (`broker_apis.py`)
    returns `None` on a network or parse failure AND on a malformed feed (missing, non-list, or
    empty `COM`). `background.py` only calls the upsert when the result `is not None`.
  - *Reconciliation guard* — `_upsert_mcx_evening_closed_sessions_coro()` skips its reconciliation
    DELETE and logs a warning when the fresh closed-set is empty and existing auto-rows exist. It is a
    second layer behind the `None` check. Fixed same day (`f279f956`): the guard now tests
    `non_weekend` (the weekday-filtered set that actually feeds the DELETE's stale-date computation),
    not the raw `dates` argument — a fetch whose closed dates all fall on a weekend used to pass the
    guard and reconcile away every existing row anyway, despite `dates` itself being non-empty.
  - *IST keying in holiday caches* — `fetch_holidays()` Tiers 1/2 and `_read_market_holidays_async()`
    key on the IST date or year (`timestamp_indian()`), not system-local `date.today()`. Same bug
    class as the special-sessions cache.
  - *Failure logs at WARNING* — prewarm and warm-cache failures log at `warning`, not `debug`.
    Tradeoff: during a persistent DB outage, `market_lifecycle.poll()` alone can log up to 3 warnings
    per 30s poll (NSE, MCX, CDS). Failures are deliberately never cached, so each poll retries.
  - *Segment flags honour overrides* — `_ae_segment_flags()` computes `{prefix}_closed` and
    `minutes_since_{prefix}_open/close` against a `special_sessions` row that applies today. A
    zero-width row means no session today. A real window anchors to its own start and end. **Dormant:
    no live reader of these keys exists.** The `minutes_since_open` grammar token reads
    `alert_state['session_start']`, not the context dict, and `sim/driver.py` overrides replace the
    values rather than reading them. The fix is correct for the context dict (admin introspection,
    future consumers), but it changes no operator-visible behaviour today.
- **Known limitation — same-day edits.** Same-day operator edits to `market_special_sessions` are not
  picked up until a restart or the next 05:30 refresh. Per-call prewarm returns early on a same-day
  cache hit, so it does not help here. Adding an admin cache-bust endpoint is a feature, not
  implemented.
- **Both cache functions** (`fetch_special_sessions`, `fetch_special_sessions_async`) only cache a
  successful query, even an empty one. A DB failure returns `[]` without caching.
- **Remaining unreconciled mechanism**: `exchange_clock`'s DB-schedule open/closed signal, which
  drives EOD scheduling. It is the one fifth mechanism still outstanding, flagged for a future pass.
- **Fixed, same day — dry-run `any_market_open` was always `False`** (`4e04e499`). `_dry_run_context()`
  (`routes/agents.py`) computed `any_market_open = any(s.get("open") for s in
  base.get("segments", []))`. `_build_context()` never emits a `segments` key; it emits flat
  `nse_open` / `mcx_open`. Every `schedule="market_hours"` agent was reported as blocked by
  `_check_dry_run_gates()` regardless of real market state, and the dry-run response's `segments`
  field was always empty. The production path was unaffected — `background.py` builds its own agent
  context WITH `segments`, and `run_cycle` derives `any_market_open` from `nse_open or mcx_open`. Now
  fixed the same way: `any_market_open=bool(base.get("nse_open")) or bool(base.get("mcx_open"))`.

**Simulator market-hours guard restored (2026-10-20, operator-visible behaviour change)** —
`_market_hours_block_check()` ([`routes/simulator.py:187`](backend/api/routes/simulator.py#L187))
imported `_build_segments` from `background.py`, a name removed in commit `7cd6c12c`. The guard raised
`ImportError` on every call. `_markets_currently_open()` ([`:240`](backend/api/routes/simulator.py#L240))
swallowed the error and returned `False`, so the guard never fired. **It now fires.** With
`simulator.block_during_market_hours` true (default true, [`simulator.py:209`](backend/api/routes/simulator.py#L209)),
starting a sim while any segment is open is refused with HTTP 409. Operators who relied on the
bypass must set `simulator.block_during_market_hours=false` on `/admin/settings`. Note that
`_markets_currently_open()` still masks any other exception as `False`.

**Probe cost — monitor after deploy.** Sites that previously omitted `exchange=` (the simulator
guard and status, the ticker watchdog) can now trigger live Kite-quote probes. `is_trading_day()`
probes only when the calendar says closed (weekend or holiday), the clock is inside 09:00–23:30, and
an exchange is passed ([`date_time_utils.py:140-186`](backend/shared/helpers/date_time_utils.py#L140)).
Roughly once a minute per exchange, cached. If this becomes measurable, add a `probe=False`
passthrough to `_segment_is_open()`. **Not implemented.**

**MCX evening-closed holidays (2026-10-20; prior round `85f2ade1`, `53c8da80`, `61307c47`,
`125cbfea`; this round `09e53dd7`, `33e3aa18`)** — The NSE holiday-master `COM` entries carry
`evening_session` ("Open"/"Closed"). `_fetch_mcx_evening_closed_dates()`
([`broker_apis.py:3558`](backend/brokers/broker_apis.py#L3558)) returns the dates marked "Closed".
It returns `None` on any fetch, parse, or malformed-shape failure, which is distinct from a genuine
empty "nothing closed this year" result. `_upsert_mcx_evening_closed_sessions_coro()`
([`broker_apis.py:3633`](backend/brokers/broker_apis.py#L3633)) writes a zero-width
`market_special_sessions` row for each date: `exchange='MCX'`, `start_time = end_time = 00:00`. It
is read as closed all day by `is_market_open()`. Weekend dates are excluded (`non_weekend` in the
same function), because `next_market_open_ist()`'s weekend branch treats row existence as open.
Rows carry `source='nse_auto'`. The caller is `_task_holiday_refresh()` (`background.py`), which
calls the upsert only when the fetch result `is not None`.
- **`MarketSpecialSession.source`** (`backend/api/models.py`, `MarketSpecialSession`, comment near
  L2406): NULL or `"manual"` is operator-entered; `"nse_auto"` is auto-inserted. The ON CONFLICT
  clause updates only `source='nse_auto'` rows, so an operator Muhurat row is never overwritten.
  Migration: `_migrate_market_special_sessions_source()` ([`database.py:842`](backend/api/database.py#L842)),
  idempotent.
- **Reconciliation** (prior round): the upsert DELETEs any `nse_auto` row whose date is no longer in
  the fetch's closed-set — NSE revised that evening back to Open. Scoped to
  `exchange='MCX' AND source='nse_auto' AND start_time=end_time AND date >= today`, computed as a
  set-difference, never touching history or operator rows. It is gated on the fetch having succeeded.
  A failed fetch's empty result would otherwise wipe every real closed-evening row during an NSE
  outage. This round added the malformed-feed guard and a second guard that skips the DELETE when the
  fresh closed-set is empty but auto-rows exist. The known weekend-only edge is documented under
  "Hardening rounds" above.
- **Other gaps closed (prior round):** the upsert's failure path logs a warning instead of returning 0
  silently. `morning_session` was checked against the live feed: all 21 `COM` entries for 2026 carry
  `morning_session: "Closed"`, so there is no date where only the evening is closed. It stays
  unmodelled because there is nothing to model against. `_fetch_nse_holiday_master_json()`
  ([`broker_apis.py:3490`](backend/brokers/broker_apis.py#L3490)) memoizes a successful response for
  5s (`_NSE_HOLIDAY_JSON_MEMO`), so the two per-refresh calls cost one HTTP round-trip. Failures are
  never memoized.
- **Still open**: a COM-listed date is treated as an MCX holiday, so morning trading on such a date
  is closed both before and after this work. Whether the feed ever lists a morning "Open" on such a
  date is unverified.

**Conn-service tick forwarder killed nightly, never revived — LTP froze while Chg% moved (2026-10-19, commit 7bf11fac)** — In conn-service mode (`RAMBOQ_USE_CONN_SERVICE=1`, the prod/dev config), the 23:31 IST `_snapshot_stop_ticker()` ([`background.py:2972`](backend/api/background.py#L2972)) called a real `.stop()` on the API process's local `MmapTickReader`, cancelling `_poll_task` — the task that tails the shared-memory tick buffer and republishes ticks to the `/api/quotes/stream` SSE stream. The method's own docstring claimed no such effect existed in this mode. The 08:00 `_snapshot_restart_ticker()` ([`background.py:3002`](backend/api/background.py#L3002)) checked for a nonexistent `.restart()` method and never revived it. Broker-side ticks kept landing in the buffer, so the outage was invisible server-side: LTP froze on every symbol with no flash (flash is tick-driven), while Chg% kept moving (computed from the independent periodic positions poll). Deploys had been masking it by restarting the process; logs show it recurring most nights since 2026-09-19. Fix: the three `hasattr()` sites now branch on the canonical `is_cutover_on()` ([`client/__init__.py:32`](backend/brokers/client/__init__.py#L32)). In conn-service mode stop and unsub are true no-ops; restart calls `check_and_heal()`. `MmapTickReader.check_and_heal()` ([`mmap_ticker.py:160`](backend/brokers/mmap_ticker.py#L160)) restarts a dead or done poll task, logs `[MMAP-WATCHDOG]` once per dead→alive transition, and escalates with a second critical line on 3+ heals within 10 minutes. `start()` and `ensure_started()` ([`mmap_ticker.py:133`](backend/brokers/mmap_ticker.py#L133), [`:156`](backend/brokers/mmap_ticker.py#L156)) now restart a done-but-non-None task; previously only `None` was restartable. `_task_ticker_watchdog` ([`background.py:5698`](backend/api/background.py#L5698)) calls `check_and_heal()` every 30s in this mode, replacing a 300s no-op sleep. Frontend: `lastLiveTickAt` ([`quoteStream.js:54`](frontend/src/lib/data/quoteStream.js#L54)) is set only by real SSE tick and snapshot events — deliberately not `symbolTickCount`, which background polls also advance and which would have masked this failure. `tickStaleness.svelte.js` (75s threshold, [line 52](frontend/src/lib/data/tickStaleness.svelte.js#L52)) drives an amber `TICKS STALE@HH:MM` badge ([`RefreshButton.svelte:485`](frontend/src/lib/RefreshButton.svelte#L485)) plus one toast per staleness episode. Invariant: a local forwarder stopped by a scheduled job must have a restart path that calls a method which actually exists, and any freshness indicator must key off delivered ticks, never poll activity.

**Expiry-day position freeze — snapshots persist until next market open** (2026-09,
commits 4fa0d711 + fd07dda3) — Once a F&O contract's expiry date passes, its final
`daily_book` position snapshot is frozen in the database and served in all views
until 08:00 IST next trading day. Why freezing is structural, not optional: during
the closed window (23:31→08:00 IST per "Market daily window" rule), the broker
WebSocket and API are disconnected, so `daily_book` is the ONLY data that exists —
deleting a frozen row destroys the sole copy available to serve. SSOT classifier:
`expiry_status()` in [`backend/api/algo/expiry_freeze.py`](backend/api/algo/expiry_freeze.py),
returns `not_expiry` / `frozen` / `refresh_eligible`. Uses contract expiry date,
the row's own 08:00-IST session boundary (same convention as
`_SESSION_ANCHOR_CUTOFF_TS_SQL`), and holiday-aware `next_market_open_ist()`.

  **Expired vs closed distinction:** A position is EXPIRED when the contract's OWN
  expiry date has passed; it gets frozen treatment. A position is CLOSED when the
  operator/algo squared off while the contract still had time left — these use the
  existing 7-day orphan sweep unchanged. Conflating them would resurrect every
  early-closed position as perpetually frozen.

  **DB/persistence pipeline** — Two bugs fixed in `backend/api/algo/daily_snapshot.py`
  and `backend/api/routes/positions.py`: (1) The 7-day orphan-sweep
  (`_delete_orphan_positions()` and `_delete_prior_orphan_positions()`) now checks
  `expiry_status() == "frozen"` before deleting, preventing premature row destruction.
  (2) New `kind='positions_empty'` marker from `_write_confirmed_empty_marker()` —
  written ONLY on broker-confirmed zero, never on failed/ambiguous fetches (links to
  Staleness rule's A1 fix) — ensures flat-account days get an anchor row so the
  orphan-sweep remains functional. (3) `_union_and_filter_expiry_frozen_rows()` prevents
  same-account fresh batches (a still-live symbol) from masking older frozen rows via
  the `latest_batch` CTE. Defensive backstop: `refresh_eligible` rows are excluded
  outright (not stale-marked), covering open-hours outages where `snapshot-fallback`
  is used.

  **Derivatives Exp-close tab** — `derivativesMath.js:annotateOptionCandidates()`
  falls back to `decomposeSymbol()` when `getInstrument(sym)` returns null (Kite
  purges expired contracts from its daily instruments cache), deriving optType/strike/underlying
  from symbol text alone. Expired legs forced unconditionally into the actionable
  `'close'` band without re-spotting to rolled-forward front-month (ITM/OTM fixed at
  settlement). Sibling instance of the earlier 2026-09 GOLDM regression fix in
  `pageLoad.js:buildCandidatePositions()` — same defect class ("silently vanishes when
  Kite purges contract"), different pipeline.

  **Live-fetch path now also expiry-filtered (2026-09-29, commit ec774d40, amends
  existing entry)** — The existing `expiry_status()` classifier was wired only into
  the closed-hours DB-snapshot serving path (`positions.py:_positions_snapshot`), not
  the live broker-fetch path (`positions.py:_fetch()`). The module's own docstring
  assumed the live-fetch "naturally stops returning an expired contract" once market
  reopens — but live incident confirmed this assumption was FALSE. Expired MCX option
  contracts (e.g. `GOLDM26SEP148000PE`) were returned as non-zero-quantity live positions
  with wrong P&L, even though their `price_source: "snapshot_settled"` field showed the
  system already knew the pricing was stale. Fix: new `is_live_row_past_freeze_window()` in
  [`backend/api/algo/expiry_freeze.py`](backend/api/algo/expiry_freeze.py) — distinct from
  `expiry_if_closed_on_own_expiry_day()` (which requires persisted `daily_book` history);
  fast no-I/O path when symbol doesn't parse as F&O or expiry hasn't passed yet; only
  consults `next_market_open_ist()` once expiry has actually passed. Applied via new helper
  `_filter_expired_live_rows()` in `positions.py:_fetch()` immediately after `raw =
  pd.concat()`, BEFORE enrichment/hygiene so filtered rows never corrupt account/symbol
  P&L rollups. Defensive hardening also added to `background.py:_preload_db_lkg_cache()`.
  Invariant: both DB-snapshot and live-fetch paths now check expiry status; no path can
  leak an expired contract with stale pricing into live account totals.

**WebSocket subscription** — `MODE_LTP`, event-driven push. All brokers (Kite, Dhan, Groww) use the **same KiteTicker WebSocket** — there is no Dhan or Groww WebSocket. LTP for Dhan/Groww positions is delivered via KiteTicker after the instrument token is resolved from (tradingsymbol, exchange). **Critical: always use `subscribe_with_sym([(token, symbol)])` to subscribe**, not bare `subscribe([token])`** — bare subscriptions never populate the ticker's `_token_to_sym`/`_sym_to_token` mapping, causing every tick to publish with `sym=""` and be silently dropped by symbol-keyed frontend consumers until the next 5-min background cycle rediscovers the instrument. New instrument from order fill: use `_subscribe_filled_pairs()` helper (`backend/api/routes/orders.py`) to resolve (symbol, exchange) to instrument token and subscribe immediately — this canonical path now covers Kite/Dhan/Groww postbacks, admin reconcile sweep, open-order watchdog, and chase's terminal-fill detection. Full design: memory `project_websocket_design`.

**Dhan/Groww order detection** — Fill detection relies on broker webhooks + periodic book poll. Kite webhooks are reliable. Dhan webhook (`/dhan_postback`) must be manually configured in the Dhan partner dashboard — if not configured, fills are detected only at the next 5-min `_task_performance` poll. Groww webhook support is uncertain (Groww may not send postbacks). Verify Dhan webhook URL is set to `https://ramboq.com/api/orders/dhan_postback` in Dhan's partner portal before relying on immediate fill detection. The 5-min book poll is the guaranteed backstop for all brokers.

**Postback authentication (2026-09 orders-page council audit, fixed)** — `/dhan_postback` and `/groww_postback` (`backend/api/routes/orders.py`) previously had `guards=[]` with no authentication at all — Kite's postback verifies a real HMAC signature (`orders_postback.py:_pb_verify_signature`), Dhan/Groww don't sign their payloads so there was nothing to check. Fixed via `_pb_verify_shared_token()`: a shared-secret query-string token, stored as `dhan_postback_token`/`groww_postback_token` in `secrets.yaml` (already generated and set on local + prod + dev as of this fix). **Remaining operator action**: append `?token=<secret>` to the webhook URL configured in each broker's own partner dashboard (Claude has no access to those dashboards) — until that's done, the token check still passes (brokers aren't sending it yet) but isn't actually being enforced end-to-end. If the secret key is ever absent from `secrets.yaml` (e.g. a fresh environment), verification fails OPEN, not closed, and logs one CRITICAL line per broker per process start so the gap can't be silently missed. Also hardened: `_pb_fallback_lookup_row`'s account-less fallback match (Groww's payload carries no account at all) now narrows candidate `AlgoOrder` rows to accounts resolving to the SAME broker via `_broker_id_for()`, instead of matching across every account platform-wide.

**Position-refresh immediacy (2026-09 orders-page council audit, fixed)** — two gaps found in the "call position APIs immediately after a fill" pipeline. (1) `_positions_refresh_after_fill` (`orders.py`) polls `fetch_positions()` up to 5× over ~7s after a fill, but called it with no `force_refresh` — the function is memoized behind a 30s-TTL cache that only gets busted ONCE (synchronously, right before this poll starts), so every attempt after the first silently re-read the same stale result; `cur_qty` could never differ from `initial_qty`, the loop reliably timed out, and `positions_refreshed` essentially never fired in practice. Fixed: every poll attempt now passes `force_refresh=True`. (2) `_opp_live_handle_success` (`orders_place.py`, the ticket-placement success path) invalidated only the `"orders"` cache and scheduled no positions-refresh poll at all — that machinery lived exclusively in the postback fan-out, which is prompt for Kite but the ONLY path for Dhan/Groww (documented unreliable/manually-configured postback delivery), leaving their live orders with no backstop faster than the 5-min `_task_performance` poll. Fixed: the ticket-success path now also schedules `_positions_refresh_after_fill`, scoped to Dhan/Groww accounts only via `_broker_id_for()` — Kite is deliberately excluded since its own postback already triggers the same function promptly on a real fill; running it twice would just double broker calls for no benefit.

**Account pre-selector freeze — reactive stale-read in order modal (2026-09-28, commit ed3938db, CRITICAL)** — Order modal's account dropdown (`PageHeaderActions.svelte:_effectiveAccount`) was `$derived(resolveAccount(...))`, where `resolveAccount()` (`frontend/src/lib/data/accounts.js`) reads `localStorage` directly — a plain, non-reactive read. `$derived` only re-runs when a TRACKED reactive dependency changes, so `_effectiveAccount` computed once (the first time the accounts list loaded) and never updated again for the session; every later modal open silently reseeded from that frozen value regardless of which account the operator had since picked via `setRecentAccount()`. Live root-cause verified: operator picked account ZJ6294, modal reopened showing ZG0790. Fix: read the reactive `recentAccountStore`/`defaultAccountStore` (the same Svelte `writable` stores `setRecentAccount()`/`loadAccounts()` actually write to) directly inside the `$derived` instead of calling `resolveAccount()`. Invariant: never call a function that reads `localStorage`/a non-store singleton from inside `$derived`/`$effect` — bridge through a real Svelte store (or `$state`) so the dependency is tracked.

**Order Ticket / Bracket-template re-wire — 4th and final decision, reverses a deliberate severance (2026-10-16, commits `abc3851f`→`78392599` series)** — On 2026-09-29 (commit `00f194d8`) the operator made a deliberate, explicit decision — after 3 prior reversals — to sever the Ticket tab from the Bracket/template system entirely ("make sure order ticket is not wired to template"): `SymbolPanel.svelte` stopped binding `templateId`/overrides into `<OrderTicket>`, `OrderTicket.svelte` dropped its `loadOrderTemplates()` fetch, and the real broker-submission payload (`placeCtx.templateId`, feeding `template_id` in the `/api/orders/ticket` POST body) was hardcoded to `null` UNCONDITIONALLY — Chain kept full functionality via its own binding, untouched. On 2026-10-16 the operator reported "what happened to bracket option in order ticket. it no longer shows" — initially treated as a regression and partially "fixed" by restoring just the visual toggle (shell-level render) — but since the underlying `templateId`/override bindings were still severed, that toggle had **zero effect on real orders**: picking a Bracket on the Ticket tab never actually attached anything. Once the full history was surfaced, the operator made their 4th and final call: "fully re-wire it," **and** "keep bracket before draft on the same row" with Bracket/Draft mutually exclusive (auto-switch the other off). Final design:
- `SymbolPanel.svelte` → `<OrderTicket>`: `bind:templateId={_sharedTemplateId}` + the 4 override binds restored (mirrors Chase/ChaseAgg's existing binding pattern); `onSelectDefault`/`onSelectNone`/`onSelectTemplate` callback props wired to the shell's `_onTemplSelectDefault`/etc. (same handlers Chain already uses — extracted so Chain's and Ticket's copies can never drift independently).
- `OrderTicket.svelte`: restored the `loadOrderTemplates()` fetch on mount (needed for `_selectedTemplate`/`_defaultTemplate`/`_nonNoneTemplates` to resolve) but deliberately did **not** restore the old `_autoSelectTemplate()` call — when embedded, the shell's own side/scope-tracking `$effect` is the single owner of auto-selecting a default; running both would be two competing writers to the same bound value. Submit payload reverted to `templateId: _isCloseOrder ? null : templateId` (was the unconditional `null`).
- The Bracket toggle (`<TemplateBar>`) now renders *inside* `OrderTicket.svelte` itself, on the same `.ot-chase-row` as CHASE/DRAFT, instead of at shell level — `_onBracketSelectDefault`/`_onBracketSelectNone`/`_onDraftToggleChange`/`_onBracketSelectTemplate` implement the mutual exclusion (Bracket ON turns Draft off and vice versa; both OFF is the normal rest state).
- **Critical gotcha, same architecture class as the account-freeze bug above**: `OrderTicket` stays mounted with `display:none` (not `{#if}`-unmounted) while Chain is active, specifically so its margin-preflight effect keeps running. Once `templateId` became a real shared binding again, OrderTicket's own pre-existing side/symbol template re-validation effect (the 2026-06-22 incident fix — `$effect` watching `_side`/`_resolvedSymbol`, resetting a stale `templateId` on scope mismatch) and its close-order-clears-template effect were *also* still running while hidden behind the Chain tab — silently resetting the SHARED `_sharedTemplateId` out from under Chain's own auto-selection logic on every Chain-tab side/symbol change. Fixed by gating both effects on `if (suspended) return;` (the same convention the pre-existing margin-preflight effect already uses) — only the visually active tab may touch shared template state. The `<TemplateBar>` markup itself is also gated with `{#if !suspended && ...}` (not left to the ancestor's CSS `display:none` alone) — otherwise a second, invisible `.oes-tpl-button` existed in the DOM at all times whenever Chain was active, and any selector's `.first()` (tests, future code) could resolve to the wrong one.
Invariant: when a component is kept mounted-but-hidden for background-effect reasons, every effect inside it that writes to *shared/bound* state must be gated on the same visibility flag — an effect reading only local state is safe to leave ungated, but one that writes to a value another visible component also owns is not. Tests: `e2e/order_ticket_template_toggle_regression.spec.js` (includes a network-interception test asserting a real `template_id` reaches `/api/orders/ticket` — confirmed to fail against the pre-fix severed code, the strongest guard for this exact class of bug).

**Chase countdown column minimum-width guard on mobile (2026-10-16, commit de68fbda)** — The age/countdown column in `ChaseCard.svelte` had no minimum width (`minmax(0, 0.6fr)`), allowing it to shrink to unreadable slivers on narrow mobile viewports competing with 8+ other columns and a locked-minimum kill button. Fixed by setting `minmax(2.1rem, 0.6fr)` in both `.cc-row` and `.cc-compact .cc-row` grid templates.

**Order Ticket Chase/Bracket/Draft click-to-pin tooltips (2026-10-16, commit bd2f0946)** — Added small "(i)" InfoHint chips next to the CHASE/BRACKET/DRAFT labels in `SymbolPanel.svelte`, `TemplateBar.svelte`, and `OrderTicket.svelte`, separate from their own toggles so help-seeking clicks don't flip real order behavior. Chase tooltip displays per-tier wait times from `backend/api/routes/orders_helpers.py:_live_chase_config` (low=30s/30 attempts, med=20s/20 attempts, high=10s/10 attempts). Test: `frontend/e2e/order_ticket_chase_bracket_draft_tooltips.spec.js`.

**Order price tick-rounding carries floating-point residue (2026-09-28, commit 4c61e47f, CRITICAL)** — Order submission (`OrderTicket.svelte:_roundToTick`, line 1183; used by ticket, basket, chain, and modify routes) was snapping prices to the nearest tick but carrying genuine IEEE 754 floating-point residue from that snap operation. Example: 590.80 @ tick 0.05 snapped to 590.8000000000001 (before rounding), sent directly to Kite → rejected with "invalid price / decimal places" error. Live incident verified on multiple orders. Fix: after snapping to tick via `Math.round(px / tick) × tick`, re-round the result to the tick's own decimal precision: `Number(result.toFixed(decimalPlaces))`. Also applied in `backend/api/routes/orders_helpers.py:_align_price_to_tick` for POST endpoint validation. Invariant: tick-snap results are always rounded to the tick's own precision before transmission to broker.

**Order-lifecycle races — final-status guards and locking (2026-09-28, commit 97ed1c8c)** — Five linked defects across postback fan-out, chase cancel-and-replace, admin reconcile, and take-profit armature converged around race windows during order fills. Key invariants:

- **Final-status write guard** — `backend/api/models.py` defines two status sets: `ALGO_ORDER_TERMINAL_STATUSES` (the full lifecycle vocabulary, informational) and the narrower `ALGO_ORDER_FINAL_STATUSES` (`{FILLED, REJECTED}` only) — the set every status-writer actually guards against. Deliberately EXCLUDES `CANCELLED`/`CANCEL_FAILED`/`UNFILLED`: a failed cancel can still have a broker-side order resting live that fills later, so a genuine late FILLED from those states must still apply — only FILLED (can never un-fill) and REJECTED (broker refused it outright) are truly final. `_sync_apply_row_status`/`_pb_apply_status_to_row` (`orders_postback.py`) and `_chase_terminal_update_db` (`chase.py`) each check `row.status in ALGO_ORDER_FINAL_STATUSES` before mutating and refuse (log + no-op) if so — an application-level guard on the read row, not a model-level `__setattr__` interceptor. Admin reconcile (`orders.py:list_active_chases`) deliberately does NOT get this guard — it's the repair path that must be able to correct stuck rows from broker truth.
- **SELECT...FOR UPDATE locking across all mutation paths** — Kite postback (`orders_postback.py:_pb_event_kite`), Dhan/Groww postback (`orders_postback.py:_sync_algo_order_rows`), chase's terminal update (`chase.py:_chase_terminal_update_db`), and admin reconcile's bulk scan (`orders.py:list_active_chases`) all lock the `AlgoOrder` row(s) via `.with_for_update()` before mutating, serialising concurrent fill detections and manual edits.
- **FIFO ledger writes on all fill paths** — Only the Kite postback path (`_pb_write_ledger_fills`) ever wrote FIFO ledger entries; Dhan/Groww postback, chase's own terminal-fill path, and admin reconcile all silently skipped it. Fixed: all four paths now call `_pb_write_ledger_fills` (orders_postback.py) after a row transitions to FILLED, so a strategy's lot ledger reflects every fill regardless of which detection path caught it.
- **Take-profit double-arm race fixed** — Parent order check (`already has child take_profit`) now runs INSIDE the `with_for_update()` lock, not before; prevents two concurrent fills racing past the existence check and creating sibling TP orders.
- **Partial-fill TP qty correction** — `_arm_take_profit` now threads `filled_qty` (actual quantity already filled, not the full `order.quantity`) through to the TP order quantity calculation, closing a window where a partial fill could arm a TP sized for the pre-fill quantity.
- **Take-profit product correction** — `parent_product` was defaulting to NRML in one code path and being threaded correctly in another. Fixed: both callers of `_arm_take_profit` (postback + chase) now pass `parent_product` explicitly, eliminating the default.
- **Chase offsetting-position guard** — Chase path now verifies the same offsetting-position guard the Kite postback path was already running (reject close-legs with opposite-sign position or quantity exceeding open), bringing all paths to parity.

**Cancel paths must call chase's mark_killed(), or chase silently re-places the "cancelled" order (2026-10-15, commit ecc829c8)** — Chase distinguishes operator-kill from broker auto-cancel via an in-process `is_killed(broker_order_id)` flag (`chase.py`), set ONLY by `mark_killed()`. Before this fix, `mark_killed()` was called ONLY from `_rco_kill_live_mode` (the dedicated `POST /chases/{id}/kill` route) — the admin `cancel_order` REST route, the agent-grammar `_action_live_cancel_order` action, and `_action_live_cancel_all_orders`'s fleet-cancel loop (`_al_cancel_broker_orders`) never called it. Result: cancelling a chase-managed order via any of those three paths got silently undone — chase's poll loop saw CANCELLED, `is_killed()==False`, took the `cancelled_continue` branch, and re-placed the order at a fresh price, leaving the operator believing the position was dead while chase kept working it. Fix: all three paths now call `mark_killed(order_id)` — BEFORE the broker cancel call, not after, since the broker's own `await` yields the event loop and a chase poll could land mid-cancel. Invariant: any operator-initiated cancel — UI, agent DO-action, or CLI — must call `mark_killed()` so chase never re-places a genuinely cancelled order. New `POST /api/orders/cancel-all` route (and the CLI's `cancel_all_orders(...)` bare statement, see Common Tasks) reuses the same `cancel_all_orders_scoped()` helper as the pre-existing agent action, so both get this fix for free.

**Live ticket placement fail-closed on AlgoOrder pre-persist (2026-09-28, commit 8fca413b)** — Ticket placement (`orders_place.py:ticket_order_handler`) used to silently ignore DB insert failures when pre-persisting the `AlgoOrder` row, swallowing exceptions and proceeding to place a live order completely untracked. Root-caused via live prod incident: `AlgoOrder #1088` was placed live, chased for ~12 minutes, never appeared in operator views, and was never reconciled post-session. Fix: any DB insert failure (constraint violation, transaction rollback, timeout) now returns `HTTPException(503 SERVICE_UNAVAILABLE)` immediately, refusing the order and rolling back to client. Client sees a clear failure and can retry; no untracked live orders escape.

**OrderBook no longer flickers on transient broker-fetch failure (2026-09-28, commit 298cd38c)** — Order display grid (`OrderBook.svelte`) was falling through to algo-only stale data (a 5-min-old cache) on any transient broker fetch failure (network timeout, 429, DNS), causing rows to flicker cancelled/filled back to OPEN and breaking operator visibility. Related to "Staleness indicator freeze rule" — this is a sibling consumer. Fix: froze to last-known-good broker state on failure, showing the last fetch's result (empty if never fetched) instead of silently collapsing to local-only stale cache.

**Multi-leg basket submit must not close the SymbolPanel modal mid-placement (2026-10-16)** — `SymbolPanel.svelte:submitBasket()` calls the host's `onSubmit` prop once PER PLACED LEG (same payload shape as a single-order submit), by design — SymbolPanel itself never closes on submit ("modal stays open after a successful chain submit so they can place more legs without re-opening"). Every host honors this except `MarketPulse.svelte`, whose `onSubmit={() => closeTicket()}` closed unconditionally on the very first callback — so a multi-leg Chain basket placed from MarketPulse vanished after leg 1 even though every leg genuinely placed. `derivatives/+page.svelte` and `orders/+page.svelte` already use a non-closing pattern (toast/refresh only); MarketPulse now matches — `onSubmit` shows a brief `toast.success(...)` instead of calling `closeTicket()` (which remains wired to the × button / `onClose` only). Invariant: a `SymbolPanel` host's `onSubmit` must never assume "called once" — basket submits call it N times, and closing on the first call truncates the UI mid-placement even when every leg succeeds.

**Order submission gated on market depth; chase classifies recoverable vs non-recoverable failures (2026-10, commit daee65d2)** — Every template/ticket places only LIMIT or GTT orders (no MARKET), so submission needs the active strike's own bid/ask at least once before a price can be meaningful. `OrderTicket.svelte`'s `_lastQuote` (set via `onDepthQuote`) was never reset on a strike change, so switching strikes left a PREVIOUS strike's stale quote satisfying the "depth has loaded" check — an operator who clicked Submit the instant a new strike opened could reach the server and get a late, confusing "limit price required" rejection instead of a disabled button. Fix: `_lastQuote` resets to `null` on every resolved-symbol change (same effect that already resets `_lots`/`_lotsTouched`); a new `_depthPending` derived (`showLimit && !_lastQuote`) gates `submit()` (explicit early return before the broker call), OrderTicket's own footer Submit button (excluded for `_draftMode`, which never calls the broker), and — piped through the existing `onTicketStateChange` mirror — SymbolPanel's shared common-action Submit button (`_ticketDepthPending`) and its `_modalFireSubmit()` click handler.

Separately, `backend/api/algo/chase.py` now classifies order failures as recoverable or not instead of only special-casing `BrokerInputError`. `_ch_is_recoverable_error()` treats `BrokerInputError`/`BrokerCapabilityError`/`BrokerAuthError` as non-recoverable (abort the chase immediately, same as `BrokerInputError` always did); everything else — `BrokerRateLimitError`, `BrokerNetworkError`, and any untyped exception — retries (still capped by `_MAX_CHASE_ERRORS = 3`) with a fresh depth-derived price on the next attempt, since chase always recomputes price from live depth every iteration anyway. `BrokerRateLimitError` gets a longer backoff (`_CH_RATE_LIMIT_BACKOFF_SECONDS = 30`) than the normal re-quote interval so a burst of retries doesn't re-trip the same broker cooldown. A broker `REJECTED` poll status (previously ALWAYS terminal, no retry) is now re-examined by `_ch_rejection_is_recoverable()`: a price-shaped reason (`"price"`, `"circuit"`, `"tick"`, `"range"`, `"stale"`, `"band"`) returns a new `"rejected_continue"` signal that `_ch_handle_poll_signal()` treats exactly like the existing `"cancelled_continue"` signal — back off `cfg.rejection_backoff_seconds`, reset `current_order_id`, and let the loop reprice and retry — completing a design the `ChaseConfig.rejection_backoff_seconds` docstring already described but the code never wired up for REJECTED. **Operator instruction, explicit and non-negotiable: margin/RMS/risk/permission rejections are NEVER recoverable**, even if the same message also mentions price (e.g. "margin shortfall for this price range") — `_ch_rejection_is_recoverable()` checks `_CH_NON_RECOVERABLE_REJECTION_HINTS = ("margin", "rms", "risk", "permission", "blocked")` FIRST and returns `False` immediately on a match, before ever consulting the price-hint list.

**Frontend request-generation guard against out-of-order fetches (2026-09-28, commit aae9fd03)** — `createDataStore` in `frontend/src/lib/data/dataStore.svelte.js` now carries a generation/request-ID on every fetch and discards responses from older generations. A slower older fetch could previously clobber a faster newer one's result, leaving stale data displayed as current. Invariant: response consumer always rejects any payload with gen_id < current_gen_id before applying to store state.

**MCX virtual-root alias no longer flips between calendar-spread legs (2026-09-28, commit d49d003d)** — `_add_mcx_spot_anchors`'s Pass 2 (`backend/api/background.py`, pure-futures positions with no CE/PE) aliased whatever specific-expiry symbol was already held, not necessarily the front-month contract. `_virtual_root_aliases` (`kite_ticker.py`) is keyed by TOKEN, not root, so holding both legs of a calendar spread on the same root (e.g. `CRUDEOIL26OCTFUT` + `CRUDEOIL26NOVFUT` both held at once) registered TWO tokens under the one virtual-root symbol "CRUDEOIL" — whichever leg ticked most recently silently won the shared slot any SSE/`getSnapshot("CRUDEOIL")` consumer reads. Fix: Pass 2 now resolves the SAME canonical front-month contract Pass 1 uses (`list_active_futures(root, limit=1)`) for these roots too, deduplicated per root, instead of aliasing whichever expiry happens to be held. Invariant: any virtual root resolves to exactly one active contract token at any given time, for both options-anchored and pure-futures-only roots.

**NAV forced write now scoped to genuine gaps (2026-09-28, amends existing entry)** — The "NAV write-skip / force-write policy" entry already documents the skip-on-understated behavior. Amending: `_run_nav_compute_once` (`backend/api/background.py:7075`) now passes a pinned `target_date` to `write_nav_snapshot()` instead of letting the function recompute "today" AFTER its own potentially-slow `compute_firm_nav()` call. If NAV compute straddles IST midnight, the write could land on the wrong calendar date and break date-based query filters. By pinning the date at entry, the write goes to the intended date regardless of compute duration.

**Alert-latch hydration query now bounded to today at SQL level (2026-09-28, amends existing entry)** — The "Alert evaluation and latching" entry's "Deploy-survival hydration" paragraph already documents hydration from today's `agent_events` rows. Amending: the database query bound is now enforced at the SQL level (`_v2_hydrate_latch()` in `backend/api/algo/agent_engine.py:2170`) instead of pulling full history and filtering in Python. On process restart, this bounds the hydration cost to one day's data instead of scanning the entire `agent_events` table, important for long-running production instances with years of historical events.

**Position state badge system (2026-10-16, commit 09ec8c6f; manual pin feature REMOVED
2026-10-17, commits d760f352 + 2467151c)** — The manual "Pair with…"/"Unpin" feature is gone
per operator decision: `PositionPinPicker.svelte`, the `POST`/`DELETE /api/positions/pin`
routes, `PositionRow.pin_group_key`, and `SymbolContextMenu.svelte`'s pair entry were removed.
`_migrate_drop_position_pairs_table` (`backend/api/database.py`) drops the `position_pairs`
table idempotently. Historical record only; do not reintroduce a manual pin without a new
design decision. The auto-pair display system (`pair_group_key`, `is_orphan`,
`_auto_pair_positions()`) is unaffected.

`frontend/src/lib/data/positionStateBadge.js:positionStateBadge(row)` remains the SSOT for
the St-column badge on both MarketPulse's Positions grid and the derivatives Legs grid.
Priority order is GTT > auto-paired > orphan (three states; orphan glyph deliberately not
`○`/letter-O — see the function's own docstring). The derivatives Legs grid's data pipeline
(`frontend/src/lib/data/expiryPnl.js:buildPositionRowFromBroker()`) passes `has_gtt`,
`pair_group_key`, and `is_orphan` through; it previously stripped them via an explicit field
whitelist, which was the root cause of the grid always showing a decorative orphan glyph.
`pairGroupSort.js` keys on `pair_group_key` only (via `${account}|${pair_group_key}`).
Invariant, still in force: chase, bracket armature, and `template_attach.py` do not consult
`pair_group_key` for any execution decision. The two auto-pairing algorithms
(`_auto_pair_positions` quantity-waterfall vs. `_classify_mcx_group` CE/PE-net) disagree in
principle, and reconciling them is a separate decision. `AlgoOrder.parent_order_id` (set by
take-profit, template/wing attach, and chase; no longer settable from the UI) remains the only
order-level pairing signal execution paths read.

**Auto-pairing is CE/PE-aware, and the Positions grid is per-account (2026-10-17, commit
8a59a17e, amends existing entry)** — The lot-waterfall grouping key in
`_auto_pair_positions()` (`backend/api/routes/positions.py`) and in `splitPairedLots()`
(`frontend/src/lib/data/lotPairSplit.js`) is now `(account, root_symbol, option_type)`, with
`option_type` ∈ CE / PE / None. A CE leg can never waterfall-match a PE leg of the same root,
account, and quantity, so a short PE no longer falsely pairs with a long CE. `option_type` comes
from a digit-anchored suffix regex (`_option_type()` backend, `_optTypeFallback()` frontend), not
a plain `endswith('CE')`, so equity tickers that happen to end in "CE" or "PE" (RELIANCE,
BAJFINANCE) are never misread as options. Futures and equity rows carry `option_type = None` and
group as before. Grid side: MarketPulse's Positions grid now keys every row by `(account, symbol)`
(`mergePositionRows()` in `frontend/src/lib/data/pulseUnified.js`), not only lot-split pieces, so
a symbol held in two accounts shows one row per account. The symbol-subtotal row that
`buildPositionsDisplayRows()` injected above multi-account symbols (`_isSubtotal`,
`.mp-subtotal-row`) was REMOVED 2026-10-17 (commit 2467151c, per operator: "the aggregate
rows in positions should be removed"). Spec:
[PULSE_SPEC.md](docs/specs/PULSE_SPEC.md) §14 "Per-account rows".

**Underlying-block-order preservation in grid sort composition (2026-10-17, commits
929a772c, 2bd4b3dc)** — MarketPulse's Positions grid and PerformancePage's Positions grid
both apply a two-layer sort composition: underlying-block grouping (options CE/PE cluster
with their underlying root), then intra-block account-and-pair ordering. Two separate bugs
corrupted the block order when an account-scoped sort ran. (1) MarketPulse
(`_pairGroupPostSort`): the function called a bare `pairGroupSort(params.nodes)` BEFORE
the block-anchor computation, discarding the upstream underlying-block structure; the sort
result fed into `postSortGroups2Level()`, which tried to re-anchor blocks but read corrupted
data. Fix: compose underlying-block preservation with account-then-pair ordering applied ONLY
WITHIN each block. Also fixed `pairGroupSort.js`'s orphan branch to be account-scoped: before,
a bare global key clumped every account's orphans together at the very end; now each
account's orphans sort to the end of that account's own cluster. (2) PerformancePage
(`frontend/src/routes/(algo)/admin/perf/+page.svelte`): the existing `pairGroupSort(params.nodes);
postSortGroups2Level(params)` sequence also ran the pair sort before the block anchor
computation. Fix: drop the `pairGroupSort` call entirely — `postSortGroups2Level`'s own sort
fully overwrites intra-block order anyway, making the earlier pass redundant and harmful.
Invariant: never let an account/pair sort run before a block-anchor computation reads the
result — composition order is critical to preserve cross-option grouping.

---

## UI Card Button Group

Every card that exposes operator actions uses a **button group** for these five actions (show only the subset that applies to that card — omit the rest):

- **Search** — filter/search within card content
- **Expand / Contract** — toggle card height
- **Full screen** — maximise card to viewport
- **Default size** — reset card to default dimensions
- **Download** — export card data (CSV / JSON)

Rule: these buttons must always live inside the shared card button-group slot. Never place them outside the group or inline them ad-hoc. When building a new card, decide at design time which of the five apply.

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
- Don't use `httpx` for outbound ntfy.sh calls from the prod server — server resolves
  ntfy.sh to IPv6 first (happy-eyeballs) and FCM push delivery silently fails despite
  HTTP 200. Use `urllib.request` which picks IPv4 (first in `getaddrinfo`). See
  `send_ntfy_alert()` in `backend/shared/helpers/alert_utils.py`.
- Adapter-side diagnostics in `backend/brokers/` (`[MARGIN-RAW-DHAN]` in `dhan.py`,
  `[MARGIN-RAW-GROWW]` in `groww.py`) run inside `ramboq_conn`, not `ramboq_api`. Dev pushes never
  restart `ramboq_conn` ([`webhook/deploy.sh`](webhook/deploy.sh#L226-L233): `CONN_TOUCHED` is
  prod-only and dev shares the prod UDS), so those lines do NOT appear on dev until a prod deploy
  that touches `backend/brokers/`. This applies to any `backend/brokers/` change, logging included.
  For a dev-visible raw margin response, use `[MARGIN-RAW-API]`: it is logged from `run_preflight`
  ([`actions_preflight.py:1151`](backend/api/algo/actions_preflight.py#L1151)) in the API process
  and fires on dev immediately.

---

## Critical math guards

**MCX underlying-spot future resolution must key off the FUTURE's own expiry, never the paired option's (2026-10-15, live operator-reported incident)** —
`backend/api/algo/derivatives.py:lookup_future_for_option()` → `_resolve_monthly_option_future()` →
`_matched_or_roll()` previously ALSO rolled the resolved underlying future to next month when the
*option's own* instrument row showed an expiry on/before today (`_check_option_expired`, now
deleted), even while the SAME-month future itself was still clearly live. MCX commodity options
routinely expire ~3-5 business days BEFORE their same-month future — e.g. CRUDEOIL's October
option's own last trading day was 2026-10-15, while the October future didn't expire until
2026-10-19 — so this fired every single month for several days, not on a rare edge case. Live
symptom: "option prices are correct [they use a different, unaffected lookup —
`option_underlying_quote_key()`/`futures_symbol_for_expiry()` in the same file], underlying prices
are not correct because of wrong future reference" — the Derivatives Snapshot/Payoff underlying
spot silently referenced next month's future for several days every month. Fix: `_matched_or_roll()`
now returns the matched future whenever ITS OWN expiry is still in the future
(`inst.x > today_iso`), full stop — an option reaching its own earlier scheduled expiry says
nothing about whether the future it's paired with is still live. Test:
`backend/tests/test_options_spot.py:test_option_on_its_own_expiry_day_does_not_roll_past_a_still_live_future`
(constructs both the current- and next-month future so a regression is caught, not masked by a
single-candidate fallback — confirmed to fail against the pre-fix code, reproducing the exact
reported bug). **The `inst.x > today_iso` compare above was itself superseded the very next day
— see the entry immediately below.**

**MCX virtual-root / underlying-spot future resolution must stay on the current contract through its OWN entire expiry day, rolling only at next market-open 08:00 IST (2026-10-16, live operator-reported incident, amends the entry above)** —
The fix directly above only addressed the PAIRED OPTION's expiry rolling the future early; the
future's OWN `inst.x > today_iso` compare it introduced had the exact same bug class one level
up — on the FUTURE's own expiry day, `inst.x > today_iso` is False the moment the calendar date
rolls over to the expiry date (hours before settlement), so the virtual root/underlying-spot
resolver rolled to next month's future immediately, not at session close. Operator: "on expiry
day, [the virtual root] should continue to use the current day underlying... [o]nly on the next
market open day at 8:00 AM it should be reset to next month future." Three sites carried this
pattern — all now delegate to a single new helper, `is_mcx_future_still_current(expiry_iso,
now_ist=None)` in `backend/api/algo/expiry_freeze.py` (mirrors the existing position-side freeze
rule `expiry_status`/`is_live_row_past_freeze_window`, but takes the real instrument `inst.x`
directly rather than re-deriving expiry from symbol text): (1) `symbol_resolver.list_active_futures()`
— MCX-conditional branch, feeds `background.py:_add_mcx_spot_anchors()` (the virtual-root ticker
alias — the originally reported symptom), `resolve_symbol()`, `lookup_mcx_front_month_future()`,
and watchlist.py/symbols.py MCX lookups, all for free since they delegate to this one function;
(2) `derivatives._matched_or_roll()` — gated on `inst.e == "MCX"` so NSE/NFO monthly-option
resolution (which also calls this function) keeps its plain `expiry > today` compare unchanged;
(3) `derivatives.lookup_mcx_future_for_expiry()` — its own inline filter carried a now-corrected
stale comment ("operator: 'when options expire consider the next crudeoil future as the spot'")
reflecting the exact pre-2026-10-15 assumption already proven wrong. MCX-only by design —
operator: "virtual root anyway will not app[l]y to non-mcx" — `_next_nse_future()` (NSE/NFO) is
untouched. Tests: `test_root_next_resolver.py::test_list_active_futures_stays_on_expiry_day_front_month`
+ `test_list_active_futures_rolls_after_next_market_open` + `test_resolve_symbol_stays_on_expiry_day_front_month`,
`test_options_spot.py::test_future_still_resolves_on_its_own_expiry_day` +
`test_lookup_mcx_future_for_expiry_includes_contract_expiring_today` +
`test_near_month_rollover.py::test_mcx_on_its_own_expiry_day_stays_on_jun` +
`test_derivatives_front_month_on_its_own_expiry_day_stays_on_jun` (all confirmed to fail
against pre-fix code). Invariant: an MCX future is "current" for virtual-root/underlying-spot
purposes through its own entire expiry day and the whole closed overnight window (23:31→08:00
IST); it rolls to the next listed contract only once the next trading day's 08:00 IST open has
passed — never at the bare calendar-date boundary.

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

**Verified-intent propagation — a claimed `intent="close"` must be verified ONCE and
propagated, never re-read raw downstream (2026-09-27, orders-page audit deepened)** —
The original close-intent-bypass fix (`_verify_close_intent` in `orders_place.py`,
verifies sign AND magnitude against the real broker position) only ever fed its OWN
local decision inside `_ticket_enforce_lot_and_fat_finger` — every OTHER consumer of
`data.intent` later in the SAME request (`_ticket_check_mcx_size_cap`'s close
exemption, `_ticket_run_preflight`'s dict, the `broker.place_order`/GTT calls) kept
independently re-reading the raw, still-unverified client claim, silently recreating
the exact bypass the first fix had closed. This mattered most for MCX/NCO: G2 (the
5-lot cap) is unconditionally exempt for MCX/NCO, so `_ticket_check_mcx_size_cap`
(the 20-lot cap) was the ONLY guard standing between a claimed-but-fake close and an
unlimited-size live MCX order — and it had zero test coverage before this fix.
**Fix**: `_ticket_enforce_lot_and_fat_finger` now mutates `data.intent` in place to
the server-verified result (`"close"` only if verification passed, else `None`)
right after verifying — `TicketOrderRequest` is a plain (non-frozen) msgspec.Struct
already mutated the same way elsewhere in the file (`_ticket_gate_market_hours_and_
align_price` aligns `data.price`/`data.trigger_price` in place), so every downstream
reader of `data.intent` automatically sees the verified value with no other code
changes needed. **`/basket` had the identical, never-extended bypass** — each leg's
`leg.intent == "close"` was trusted raw for the 5-lot cap, the MCX 20-lot cap, AND
`run_preflight`'s own internal G2 check. Fixed the same way: `_verify_close_intent`
now runs per F&O leg in `orders_basket.py`, storing the verified result in the same
`_leg_close` variable every existing guard already reads, and a new
`_leg_verified_intent` (verified value for F&O, raw passthrough for equity — no cap
is intent-gated there) is what actually reaches `run_preflight` and `broker.place_order`.
Two more basket-only defects found in the same pass: (1) a cold instruments-cache
lot_size resolution failure used to `raise HTTPException(503)` INSIDE the per-leg
loop, aborting the whole account group and discarding any earlier legs' already-
placed results — a client retry after seeing that error would place those legs
again. Now a per-leg `BasketLegResult(status="error")` + `continue`, matching every
other guard in the same loop. (2) basket's preflight-blocker handling only treated
`MARGIN_SHORTFALL`/`SEGMENT_INACTIVE` as real blockers; every other code
(`QTY_FREEZE`, `INSUFFICIENT_FUNDS`, `LOT_MULTIPLE`, `LOT_SIZE_UNKNOWN`,
`FAT_FINGER_5_LOT_CAP`) was logged and the leg placed anyway — fail-open. Now ANY
non-ok preflight result rejects the leg; a preflight block only ever costs one leg
(never the whole request), so there was no correctness reason to demote any code.
The two independently-hardcoded `20`/`5` lot-cap literals (`orders_place.py`'s
`_MCX_MAX_LOTS`, `orders_basket.py`'s `_MCX_CAP`, plus a THIRD `5` inside
`actions_preflight.py:_preflight_validate_lots`'s own FAT_FINGER_5_LOT_CAP check)
are now `MCX_MAX_LOTS` / `FO_FAT_FINGER_LOT_CAP`, defined once in
`actions_preflight.py` and imported by both route files — same value today only by
coincidence before this fix, no guard against future drift. Chain orders (`Option
ChainTab.svelte`) submit through `placeTicketOrder()` → `/ticket`, so this fix
covers them too; no separate chain endpoint exists. Template attach path
(`template_attach.py`) reads no client-supplied intent field at all — nothing to fix
there.

**Cross-reference — margin-gate close exemption (2026-10-20, commit `fa0e3a42`)**: the same
invariant applies to the Dhan/Groww margin exemption. `_preflight_emit_margin_blocker`
([`actions_preflight.py:518`](backend/api/algo/actions_preflight.py#L518)) re-runs
`_verify_close_intent` INSIDE the shared gate rather than trusting the caller's own verification.
Only the margin exemption re-verifies there. Step 0 of `run_preflight` (the G2 5-lot bypass) still
reads the raw `order["intent"]`. That is safe for every live caller, because each passes a verified
or position-derived value. `/preflight` (preview-only, read-only) passes the client's raw intent.
See the "Dhan/Groww margin-shortfall gate was structurally inert" entry in Critical math guards.

**Dhan/Groww margin-shortfall gate was structurally inert; close exemption + log-only default
(2026-10-20, commits `fa0e3a42`, `3ea29a24`; close-path fixes `12b8e166`, `3a28bcbd`)** — 
Root cause: `_preflight_leg_required` ([`actions_preflight.py:272`](backend/api/algo/actions_preflight.py#L272))
read only Kite's nested `final.total` / `initial.total`, plus a top-level `required`. Dhan's
`_margin_for_order` ([`dhan.py:1297`](backend/brokers/adapters/dhan.py#L1297)) and Groww's
`basket_order_margins` ([`groww.py:1131`](backend/brokers/adapters/groww.py#L1131)) return a FLAT
`{"total", "var", "exposure", "available", "raw"}`. None of the checked keys matched, so `required`
was always `0` for every Dhan/Groww order on every exchange. `INSUFFICIENT_FUNDS` needs
`required > 0`, and the shortfall was always `0`, so the gate was dead and the broker's own RMS was
the only backstop. The unit tests stubbed only Kite's nested shape, which is how it went unnoticed.
Both adapters also turned an absent margin field into `0.0`, breaking missing-vs-zero.

Fix, in three parts:
- **Parser.** `_preflight_leg_required` also reads a flat top-level `total`, checked after
  `final`/`initial`/`required` so Kite parses exactly as before. An unparseable value returns
  `None`, never `0.0`. A multi-leg basket with any `None` leg is `None` for the WHOLE basket (no
  partial sum). Per-leg `error` strings still feed the keyword-classification fallback.
- **Adapters.** `_dhan_num_or_none` and `_gf_or_none` return `None` for a genuinely absent field.
  A failed per-order call returns `{"total": None, "error": ...}`.
- **Close exemption (below).**

**Close-margin exemption, server-verified and re-checked inside the shared gate.**
`_preflight_emit_margin_blocker` ([`actions_preflight.py:518`](backend/api/algo/actions_preflight.py#L518))
is the only place a MARGIN_SHORTFALL or INSUFFICIENT_FUNDS blocker may be appended. For Dhan/Groww
ONLY (`margin_ctx["is_dhan_groww"]`), it calls `_verify_close_intent(symbol, exchange, side, account,
qty)` ([`orders_place.py:484`](backend/api/routes/orders_place.py#L484)), which checks sign AND
magnitude against the real position. A verified close sets `diag["close_exemption"] = True` and
returns without blocking. A verification exception logs at debug and falls through to the normal
blocker, so a verification failure fails closed. The caller's raw `intent` is never trusted here.
Kite is excluded by the explicit `{"dhan", "groww"}` allowlist, because `basket_order_margins`
already nets a close via `consider_positions=True`.

**Enforce vs log-only, and the two settings.** `_preflight_margin_policy`
([`actions_preflight.py:476`](backend/api/algo/actions_preflight.py#L476)) reads
`preflight.enforce_margin_dhan` and `preflight.enforce_margin_groww`
([`settings.py:696`](backend/shared/helpers/settings.py#L696), [`:703`](backend/shared/helpers/settings.py#L703)).
Both are bools, **default `False`** (log-only). Log-only records the would-be blocker in
`diag["margin_would_block"]` and logs a WARNING tagged `[PREFLIGHT] margin gate LOG-ONLY`. Flip
either on `/admin/settings` with no deploy. **Kite is unaffected by both**: its gate is always
enforced. Any broker not on the allowlist is also enforced by default.

**Close-path side and product defaults.** `_parse_broker_call_fields`
([`actions_preflight.py:999`](backend/api/algo/actions_preflight.py#L999)) defaults a missing side to
`"BUY"` and a missing product to `"NRML"`. Two close paths omitted `side` from their preflight dict:
`_action_live_close_position` and `_chase_build_tasks` ([`actions_live.py`](backend/api/algo/actions_live.py)).
Consequences: (a) a SELL-to-close was priced as a fresh BUY, so Kite demanded full SPAN instead of
netting. Loss-limit closes were falsely blocked on MARGIN_SHORTFALL when margin was tight. (b)
`_chase_build_tasks` hardcoded `product="NRML"` in the real `ChaseConfig`, so an MIS close was placed
as NRML. That does not net against the MIS position on the broker, so the position stayed open and a
stray NRML position was created. `12b8e166` threads the real side through. `3a28bcbd` reads the real
product via `_fetch_net_position_product` ([`orders_place.py:434`](backend/api/routes/orders_place.py#L434)),
which shares a row-match helper with the qty lookup, so qty and product come from one position row.
Resolution order in `_al_close_resolve_position`: operator `product_param`, then the real position's
product, then `"NRML"`.

**Fixed same round (`2c53e43f`).** `_chase_build_tasks`
([`actions_live.py:1477`](backend/api/algo/actions_live.py#L1477)) had the identical NaN bug —
`str(p.get("product") or "NRML")` silently yielded the literal string `"nan"` for a missing cell,
since `bool(float('nan'))` is `True` in Python. The `pd.isna` guard in `3ea29a24` covered only
`_fetch_net_position_product`; this path used a separate, no-pandas-import `x != x` NaN check
instead (the file has no other pandas dependency) — same effect, no new import.

**Preview matches enforcement.** The margin preview is NAKED: it sends no `paired_legs`, matching
the submit-time check ([`OrderTicket.svelte:2115`](frontend/src/lib/order/OrderTicket.svelte#L2115)).
A template wing is placed only on parent fill, so the broker needs naked margin at placement. Same
round (`e048baa7`, `62313b95`, `9f1dc3cf`, `1557a0e0`): the preview tracks the resolved exchange,
sends LOTS to `/orders/ticket/preview`, and plans bare-underlying templates against the resolved
contract. `/preflight` still accepts `paired_legs` for read-only API callers; that yields a netted
informational verdict, never a placement gate. Invariant: a display must never disagree with
enforcement, and the enforcement path is never loosened to match a display.

**Groww GTT refusal in prod.** `RemoteBroker.validate_gtt_exchange`
([`remote_broker.py:256`](backend/brokers/client/remote_broker.py#L256), commit `7183244b`) now
forwards to conn_service. Before, it was the base no-op, so Groww's MCX/NCO GTT refusal was
silently skipped in conn-service mode. Errors are re-raised as `ValueError` so `apply_plan_live`'s
`except ValueError` still catches them.

Invariant: an absent margin field is `None`, never `0`. A margin gate that reads `0` for missing data
is not a gate. Dhan/Groww stay log-only until the operator flips the setting after confirming real
numbers via the `[MARGIN-RAW-DHAN]` / `[MARGIN-RAW-GROWW]` lines (see Things to Avoid for why those
land late on dev).

**Lot/contract oversize guards (2026-09-24, commit f5db7765)** — Seven confirmed
lot-vs-contract normalization bugs (C1–C7) across chase, template scale-out, trailing
stop ratchet, and order modify paths; all patches deployed together. Key invariants:

- **Chase cumulative_filled counter** — `chase_order()` tracks cumulative fills
  across all cancel-and-replace attempts in a single counter (initialized from
  `already_filled` parameter), not reconstructed as `quantity - remaining_qty`
  per attempt. `_ch_capture_late_fill()` re-queries post-cancel to fold any fill
  racing the cancel before sizing the replacement order (C2 fix).
- **`_exchange_contracts_to_wire` raises on error** — Now raises `ValueError` on
  sub-lot (qty < lot_size) or non-multiple (qty % lot_size != 0) MCX/NCO
  quantities instead of silently passing through or flooring. Caller MUST
  guarantee qty is already a clean lot multiple; broker will silently
  reinterpret any wrong number as lots, not contracts (C7 fix).
- **`Broker.modify_gtt` has qty ceiling** — Both Kite and Dhan adapters now call
  `_check_kite_gtt_qty_ceiling` / `_check_dhan_gtt_qty_ceiling` as a last-line
  defense before SDK call, mirroring `place_gtt`'s existing pattern. Any new
  GTT-modify path must call `broker.translate_qty` before building payload (C6
  fix).
- **G1 lot-multiple preflight applies uniformly** — MCX/NCO skip removed from
  `actions_preflight.py`. Positions ARE converted to contracts before reaching
  this check (per `broker_apis.py:_annotate_lot_size`), so G1's `qty % lot_size`
  guard is correct for all F&O exchanges uniformly (C7 fix).
- **Service-restart chase recovery reverses both sides** — `_recover_chase_already_filled()`
  reverse-translates BOTH `quantity` and `filled_quantity` from broker's native
  unit via `_ch_reverse_translate_mcx_filled()` (gated by `_MCX_LOTS_CONVENTION_BROKERS`)
  before subtracting to derive already-filled count. Translating only one side
  would mix units and silently derive wrong fill (C4 fix).
- **Chase cancel confirmation — never replace an order without verifying the
  cancel landed (2026-09 orders-page council audit, risk lens)** — `_ch_cancel_previous`
  swallows any `broker.cancel_order` exception with only a warning log; the
  caller previously proceeded unconditionally to place a fresh replacement
  order sized at the full `remaining_qty`, abandoning `current_order_id`
  entirely. If the cancel had silently failed (old order still resting live),
  this produced TWO live orders for the same leg — the old one never polled
  or reconciled again — capable of independently filling for up to 2x the
  intended position, with no alert. Fixed: `_ch_capture_late_fill` (already
  doing a post-cancel status read for late-fill capture) now also checks
  whether that status is genuinely terminal (`_CH_CONFIRMED_GONE_STATUSES`:
  CANCELLED/EXPIRED/COMPLETE/REJECTED) and returns a `cancel_confirmed` bool
  — fail-SAFE (False) on any other status or on a status-read failure.
  `_ch_cancel_and_capture` aborts the chase (no replacement order placed,
  CRITICAL log, urgent ntfy alert) when `remaining_qty > 0` and the cancel
  isn't confirmed, instead of blindly proceeding. Separately, `_ch_exhaust_max_attempts`
  (chase gives up after max_attempts) previously fired NO operator alert at
  all, unlike its sibling `_chase_abort_on_consecutive_errors` — now alerts
  consistently and notes explicitly if its own final cancel attempt may have
  also failed.
- **Groww's `translate_qty` had no structural safety net (2026-09 orders-page
  council audit, finding #6)** — Kite and Dhan both inherit the base
  `translate_qty`, decorated with `@exchange_qty_convention`, which routes
  every call through `_exchange_contracts_to_wire` and gets the C7
  sub-lot/non-multiple/cache-miss guard "for free." `GrowwBroker.translate_qty`
  (`groww.py`) is a full override — Groww sends CONTRACTS on the wire for
  every exchange including MCX, so it never converts to lots — and it
  returned `raw_qty` unchanged with zero validation. Fix: split the guard
  conditions out of `_exchange_contracts_to_wire` into a standalone
  `_validate_exchange_qty_multiple(exchange, contracts, lot_size, label)`
  (`base.py`) that raises the same `ValueError`s (lot_size≤1 cache miss,
  sub-lot qty, non-multiple qty) with NO unit conversion; `_exchange_contracts_to_wire`
  now calls it internally (behavior unchanged for Kite/Dhan).
  `GrowwBroker.translate_qty` now calls `_validate_exchange_qty_multiple`
  explicitly before returning `raw_qty` unchanged — same refusal semantics
  as Kite/Dhan, without adopting their lots conversion (which would be
  wrong for Groww's actual wire convention).
- **Admin manual modify-order route had the same unconverted-qty bug (2026-10)**
  — `OrdersController.modify_order` (`backend/api/routes/orders.py`, the
  admin PUT `/{order_id}` route) sent `data.quantity` straight to
  `broker.modify_order()` unconverted, with a comment claiming this was
  deliberate because `ModifyOrderRequest` carries no exchange/tradingsymbol.
  Same trap as the agent `modify_order` action path above. Fix: resolve the
  order's own exchange/symbol from its `AlgoOrder` row
  (`actions_live.py:_al_modify_fetch_order_meta`) and reuse the exact same
  G1 lot-multiple check + `broker.translate_qty()` call
  (`actions_live.py:_al_modify_resolve_qty`) instead of duplicating it —
  fails closed (400, broker never called) on resolution failure.

**Template attach mode gate — paper/sim/replay fills never place real broker 
orders (2026-09-30, commit 05c6f708)** — `_fire_template_attach_on_fill` in 
`backend/api/routes/orders_place.py` is the ONE function that calls 
`apply_plan_live()` and places real broker GTT + wing orders on a templated 
parent fill. The bug: the function hardcoded `apply_path="live"` unconditionally 
regardless of the AlgoOrder row's actual `mode` field. So a templated order 
filled in paper/sim/replay mode (all three share `PaperTradeEngine`) reached 
the broker-placing branch and armed real exit GTTs + a real wing order against 
a position that only existed in the simulator. The fix: `mode` is now a 
required keyword-only kwarg (no default) with an explicit early-return check on 
line 713 — if `mode != "live"`, the function logs and returns immediately, 
before the per-row lock and before any DB or broker work. DO NOT reintroduce a 
default value for `mode` — a default would silently recreate the exact bug for 
any future caller that forgets to pass it (TypeError is the right failure mode). 
Companion fix: `_retry_precheck_row()` in `backend/api/routes/orders.py` now 
refuses `/retry-template` for any row whose mode is not 'live' or 'sim' 
(sim routes to `apply_plan_sim`, no real broker call). This gate is necessary 
because the non-live resting state (`template_id` set, `attached_gtts_json` 
null, `status` FILLED) looks identical to a silently-failed attach, so the UI 
cannot distinguish "attach was correctly skipped for non-live" from "attach 
failed and needs manual retry." All four callers thread `mode=` from the 
AlgoOrder row or engine: admin reconcile (orders_place.py:521), postback 
handler (orders_postback.py:659), paper engine (paper.py:833), and chase 
terminal (chase.py:243). Test: `backend/tests/test_template_attach_paper_mode_safety.py`. 
Invariant: `_fire_template_attach_on_fill` (and anything that could reach 
`apply_plan_live`) must never run for a non-'live' AlgoOrder — 
paper/sim/replay/shadow fills never place real broker orders.

**Chain template scope and remembered preference (2026-09-30, commit b8bd1b7b)** —
Two UI defects blocked option-specific template selection on Chain:
- Scope detection always resolved to the root symbol, never buy_option/sell_option,
  so wing-eligible option-specific templates could never be picked when placing
  from Chain. Fixed: `_currentScope()` in `SymbolPanel.svelte` now prefers the
  focused leg's real option-contract symbol on Chain instead of the root.
- Adding a leg could silently flip Template back to OFF using a stale remembered
  preference from an unrelated scope, even after the operator explicitly turned
  it on in the current session. Fixed: an explicit in-session choice now
  overrides the remembered-preference lookup until the next genuinely fresh
  order.
Invariant: template scope resolution must track the actual focused leg symbol,
not the root, and explicit in-session preference must survive leg additions.

**Fill bookkeeping completeness — filled_quantity and attached GTT entries
(2026-09-30, commit b8bd1b7b)** — Three linked postback/reconcile defects
prevented template attach's full-fill gate from firing for ordinary fills:

- **filled_quantity field was never set** — Postback and reconcile writers never
  set `filled_quantity`, structurally blocking template attach's full-fill gate.
  Fixed in all 6 writer locations: `_rco_apply_fill_price` (orders.py:198),
  `_rco_stamp_fill_price` (orders.py:1124), `_rco_reconcile_apply_target`
  (orders.py:1251), `_pb_apply_status_to_row` (orders_postback.py:475),
  `_sync_apply_row_status` (orders_postback.py:134), and chase.py's
  Dhan/Groww-specific writer. Note: `filled_quantity` is always copied from
  the row's own `quantity` field (contracts), never the broker's raw filled-qty
  (which is in lots for MCX/NCO — a unit mismatch trap). Already-stuck prod
  rows with FILLED status do not self-heal and need an explicit Retry-attach
  click.
- **Wing orders never recorded in attached_gtts_json** — A second trigger
  (chase/postback race, or manual retry) could place a duplicate live wing
  order. Fixed: `_opp_build_attach_entries` (orders_place.py:579) now appends
  a `{"kind":"wing",...}` entry when `result.wing_order_id` is set.
- **"Filled" status predicate missed FILLED vocabulary** — OrderBook/LogPanel
  checked broker vocabulary (COMPLETE) but AlgoOrder rows use FILLED, blocking
  the Filled chip, template chip, and Re-attach button for algo-only fills.
  Fixed in both OrderBook.svelte and LogPanel.svelte: `st === 'COMPLETE' || st
  === 'FILLED'`.

Invariant: filled_quantity must be written whenever a row transitions to FILLED,
copied from the row's own contracts quantity; wing orders must appear in
attached_gtts_json; and status-display predicates must recognize both broker
(COMPLETE) and algo (FILLED) vocabulary.

**Post-fill position freshness (2026-09-30, commit b8bd1b7b, amends existing
entry)** — The "Position-refresh immediacy" entry (line 431) already documents
the polling loop + force_refresh pattern. Amending with two gaps closed in this
batch:
- The post-fill refresh busted only the raw broker-DataFrame cache, not the 30s
  route-level cache, so Payoff/Legs/Holdings routes could stay stale for up to
  30s. Fixed by also calling `invalidate("positions")` and `invalidate("holdings")`
  (orders.py:633-634) alongside the existing raw-cache busts.
- In production, the conn-service process has its own separate 30s cache that
  was never busted, adding another 30s of staleness and silently defeating the
  "Position-refresh immediacy" design. Fixed by threading an explicit
  `force_refresh` flag from `broker_apis.fetch_positions()` through
  `sync_wrapper` all the way to the conn service's own `/internal/positions?force=1`
  endpoint. Important nuance preserved: a routine TTL-expiry auto-force (line
  1540 in broker_apis.py) must NOT also force the conn-service hop — that would
  double every ordinary poll's broker load. Only an EXPLICIT `force_refresh=True`
  call (from `_positions_refresh_after_fill`) does.
Invariant: post-fill position refresh must invalidate both raw broker-cache and
route-level cache, and must thread an explicit force-refresh flag through the
conn-service to bust its own TTL; TTL-expiry auto-forces must not compound
with conn-service forces.

**Session-anchor bug — Day P&L baseline query (2026-09, fixed commit 93689676)** — 
Incident: closed-hours snapshot reader derives baseline batch boundary from a wall-clock-stamped 
`date` column, not from the batch's own `captured_at` timestamp. When a close-reset write 
fires just after IST midnight (e.g. MCX 23:30 + 30-min settled-offset = 00:00 next calendar day), 
it gets written with `date` = next calendar day, so a naive `date`-based cutoff lands AFTER 
that write, incorrectly including it as "yesterday's baseline" when it's actually the same 
trading session. Result: Day P&L collapses toward ~0 for overnight positions. Fix: 
`_SESSION_ANCHOR_CUTOFF_TS_SQL` in `backend/api/routes/positions.py` derives the 08:00 IST 
session boundary from `captured_at` itself (shifting back 8h, truncating, shifting forward), 
making the cutoff immune to whichever calendar day the `date` column happens to carry. 
Invariant: session boundary is always derived from the batch's own `captured_at`, never 
the wall-clock `date` column. Test: `backend/tests/test_positions_snapshot_session_anchor.py`.

**Payoff chart Exp P&L basis (C1, 2026-09)** — The displayed Exp P&L value shown ON the Payoff chart next to the expiry marker (dart position) is priced at `payoffSpot` (anchor-contract basis, lines 1891–1920 in `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`) instead of `liveSpot` (front-month), so the overlay value and the marker's drawn position are guaranteed to visually agree — both reference the same contract. This is a deliberate, correctly-scoped choice: the separate Legs-grid TOTAL row's Exp P&L continues to read `liveSpot` / front-month (line 2222, `_legsExpPnlTotal`), a different and correct consumer. The chart's value is computed by `_chartExpPnlAtSpot` (line 2232, summing enabled legs at `payoffSpot`) and passed to `OptionsPayoff.svelte` as the `legsExpPnlAtSpot` prop (line 30, used at lines 981–985). NSE underlyings (no anchor future) also live-tick via the Tier-1b fallback (lines 1901–1911, reusing liveSpot's own cash-ticker lookup) so the overlay ticks live every second instead of stepping once per 5s refetch — the C2 fix for overlay-desync on NSE. A broader cross-surface SSOT consolidation landed in the same timeframe, consolidating spot/LTP, prevClose, Day P&L, and position existence onto `portfolioStore` — `payoffSpot`'s anchor-contract tier remains the one deliberate exception to the single-spot-resolver rule and was not affected by this consolidation. **Future work MUST NOT revert this to liveSpot** — the visual disagreement between marker and number (if it were to happen) is a user-facing defect. Invariant: chart value = marker position visually, always.

**Exp P&L session-boundary fix (2026-09-27, commit 8b47be7b)** — `isExpiredHeldContract()` 
in `frontend/src/lib/data/expiryPnl.js` now compares a held F&O leg's expiry date against 
`tradingSessionDateIST()` (rolls at 08:00 IST boundary, matching this app's session convention) 
instead of `todayIST()` (bare midnight calendar rollover). All Exp P&L consumers (payoff chart, 
Legs grid, Snapshot grid, NavStrip P-pill — all funnel through this one predicate) now stay 
theoretical mark-to-spot through expiry-day close and the entire overnight window, collapsing 
to the frozen actual/settled P&L only once the next trading session actually begins (08:00 IST), 
never at bare midnight hours before market open. Invariant: Exp P&L session boundary is always 
derived from the trading-session-date convention, matching `_SESSION_ANCHOR_CUTOFF_TS_SQL` 
and the Day P&L / nav rules elsewhere in CLAUDE.md.

**close_price / ltp invariant — DO NOT CHANGE without explicit operator instruction** —
`prev_close` = previous session's **settlement LTP** (frozen from settlement until next session opens at 08:00 IST). `ltp` ticks live during session, freezes at settlement price at close. Day P&L = `(ltp − prev_close) × qty`.

**Canonical source**: `daily_book.ltp` from the most recent settlement snapshot (`captured_at < 08:00 IST`, DESC per account+symbol). **NOT** Kite's `positions.close_price` (BHAV copy, lags ~8AM next day). **NOT** `COALESCE(daily_book.previous_close, ltp)` — `previous_close` is populated from the same stale Kite API.

Code paths: `_override_stale_close_from_snapshot` (positions.py) and `_override_stale_close_for_holdings` (holdings.py) — both must query `daily_book.ltp` directly (COALESCE→ltp fix completed 2026-09, commit 93689676). Do NOT revert to COALESCE. Full rationale: memory `project_prev_close_architecture`.

**Underlying P.Close/Chg% display scope (2026-09)** — The underlying's own P.Close 
(previous close) and Chg% values displayed in Snapshot header and Payoff chart now use 
the `daily_book.ltp` settlement-snapshot basis, but **ONLY when the underlying itself 
is a held position or futures contract** (a `daily_book` row exists for it). For pure 
indices or unheld front-month futures lacking a `daily_book` entry, the display falls 
back to Kite's `ohlc.close` / `quote.prev_close` explicitly. This scoped decision was 
intentional: `daily_book` is account-keyed per held instrument, so it has no row for 
instruments not in the portfolio. Invariant: never use a held position's `daily_book.ltp` 
for an unrelated underlying that lacks its own position entry.

**Day P&L reference price by row type** (SUPERSEDED 2026-09, commit 93689676)

**Historical (do not reintroduce — documented here for incident prevention)**:

| Row type | Reference | Code path |
|---|---|---|
| New position today (oq=0) | entry_price (average_price) | Case 1 backstop: dcv = pnl |
| Open overnight position | prev_close | `(ltp − close_price) × oq` |
| Closed overnight (qty=0, oq>0) | prev_close | Case 2: `pnl − (close − avg) × oq` |
| Closed intraday (qty=0, oq=0) | entry_price → realised | Case 3 backstop: dcv = pnl |
| Holdings | daily_book.ltp (prior settlement, NOT COALESCE) | same as open overnight |

Replaced by unified baseline-diff formula (see "Frontend Day P&L SSOT" below).

**Day P&L formulas by position type — DO NOT CHANGE without explicit operator instruction — (SUPERSEDED 2026-09, commit 93689676)**

**Historical** (three per-state branch formulas, now replaced by single atomic formula):

| Position type | Day P&L formula | Notes |
|---|---|---|
| Overnight open (oq>0, qty>0) | `(ltp − close) × qty` | `close` = prior session settlement LTP |
| Closed overnight (oq>0, qty=0) | `(exit_price − close) × qty` | Case 2: `pnl − (close − avg) × oq`; requires `close > 0` |
| New today (oq=0, qty>0) | `(ltp − entry_price) × qty` | Case 1: dcv = broker pnl; no prior session close exists |

All cases handled by single formula (see "Frontend Day P&L SSOT" below).

**Holdings sold → P&L splits between holdings and positions — DO NOT CHANGE without explicit operator instruction** —
When a holding is sold (fully or partially), the sold quantity moves to positions as a
CNC row. P&L and day P&L for the sold qty are accounted in **positions only**. Holdings
shows only the **remaining quantity** (`quantity`, not `opening_quantity`).

- Holdings day P&L = `(ltp − prev_close) × quantity` (remaining shares only)
- Holdings `inv_val` = `avg × quantity`, `cur_val` = `ltp × quantity` (remaining only)
- Sold portion day P&L = `(sell_price − prev_close) × sold_qty` — lives in the CNC positions row (Case 2 backstop)
- `opening_quantity` is a reference field only — must NOT be used as the basis for any P&L or value computation
- No sharing or double-counting between holdings and positions surfaces

**Kite close_price stale overnight** — Zerodha updates `close_price` from BHAV copy at ~08:00 IST next trading day; weekends lag until Monday 08:00. Never use `positions.close_price`, `quote.ohlc.close`, or `daily_book.previous_close` as day P&L reference. Use `daily_book.ltp` (settlement snapshot, `captured_at < 08:00 IST`). See memory `project_prev_close_architecture`.

**Day P&L formula + backstop** (SUPERSEDED 2026-09, commit 93689676)

**Historical** — Decomposed intraday formula (not used for Day P&L display after redesign):

Positions: `overnight_qty × (LTP − prev_close) + day_buy/sell legs`. Holdings: 
`broker.pnl − (close − cost) × opening_qty`. MCX guard: apply lot_size to intraday qty too. 

Backend `apply_day_change_backstop()` in `pnl_math.py` rescues three edge cases for 
**diagnostic/snapshot-reading purposes only** — Case 1 (new position), Case 2 (overnight 
position), and Case 3 (flat intraday). Still called by snapshot reader 
`positions.py:_apply_flat_row_hygiene()` for per-position edge-case fixes, but the day-change 
values it produces no longer feed account/symbol rollups or Pulse display. Replaced by 
baseline-diff formula (see "Frontend Day P&L SSOT" below).

**Frontend Day P&L SSOT (2026-09 redesign)** — Atomic baseline-diff formula, no branching.

Canonical formula for a single position's day P&L (valid for ALL position states — new entry, 
full exit, partial exit, re-entry, flip):

```
day_pnl = current_total_profit(realised, unrealised) − base_pnl
```

Where:
- `current_total_profit = realised + unrealised` (never a broker's raw `pnl` field directly, 
  to avoid double-counting — except Kite, where native `pnl` is confirmed = realised+unrealised 
  and is used directly)
- `base_pnl` = that position's `current_total_profit` frozen at the most recent trading day's 
  close-reset snapshot (0 if none exists, e.g. position opened today)
- Fallback when realised/unrealised unpopulated: `resolve_realised_unrealised()` trigger 
  (both legs exactly 0) falls back to `pnl` as realised leg — same rule in backend 
  `pnl_math.py`, frontend `nav.js:currentTotalProfit()`, and Polars enrichment

Implementation: `frontend/src/lib/data/nav.js:baseDayPnlForPosition(p)` and vectorised 
variant `currentTotalProfit()` — the sole Day P&L formula (poll-only, §1 redesign; 
`livePositionDayPnl` and its live-tick delta term were removed entirely, not vectorised 
alongside it). **Per-position Day P&L IS still 
displayed** on every position row (Pulse grid, derivatives Legs/Expiry grid, PerformancePage) —
an earlier mid-redesign plan to remove per-row display was reverted by explicit operator
instruction; only the underlying *calculation* changed, not the display. Account-level
rollups (`portfolioStore.byAccount`, pinned MarketPulse summary rows, PositionStrip P slot,
NavStrip) are also shown alongside the per-row values and reconcile with them (both derive
from the same `baseDayPnlForPosition`).

Account-level rollup:
- Sum baseline-diff per account: `Σ baseDayPnlForPosition(row) for rows in account`
- PositionStrip P slot 1 reads total from `portfolioStore.positions.total`; NavStrip same
- Endpoint field: `PositionsResponse.summary` (account-level)
- Backend also computes a symbol-level rollup (`PositionsResponse.symbol_summary`,
  `_build_polars_symbol_summary` in `positions.py`) but no frontend surface currently
  consumes it — the UI wiring for a symbol-rollup grid was built then reverted in the same
  session per the per-row-display reversal above. The field is available for a future UI if
  wanted.

**Historical per-position formulas (pre-2026-09)**: See "Day P&L reference price by row type" 
and "Day P&L formulas by position type" sections above (marked SUPERSEDED). The three-case 
branchy logic is now replaced by single atomic formula. Incidents 8474a17e (close==ltp guard 
removed — formula correct even at session open) and 1769cffc (short position oq check fixed 
to `oq !== 0`) informed the current design's simplicity.

**Holdings day P&L — COALESCE bug (FIXED 2026-09)** — Previously: `_override_stale_close_for_holdings` 
in `holdings.py` queried `COALESCE(daily_book.previous_close, ltp)` as ref_close; since `previous_close` 
is from Kite's stale BHAV-copy API, the epsilon check always passed → no patching → wrong day P&L. 
Fix (commit 93689676): query now uses `daily_book.ltp` directly (same pattern as positions fix). 
No more COALESCE fallback — holdings day P&L now computed from prior-settlement LTP per the 
"close_price / ltp invariant — DO NOT CHANGE" rule.

**NAV write-skip / force-write policy (2026-09, commits 89fa495b / 50d8784e / 436549f2)** — 
`write_nav_snapshot()` in `backend/api/algo/nav.py` now gates entirely on `snap["understated"]` 
(NOT the broader `errors` field, which includes correct-but-stale LKG substitutions and must NOT 
block writes). If understated and NOT forced: skip the write, return `skipped_write: True`, and 
`_run_nav_compute_once` retries. If understated AND forced (`force=True` passed once clock 
passes `target + _NAV_FORCE_GRACE`, a 10-minute module constant in `background.py`): check 
whether a `nav_daily` row ALREADY EXISTS for that date. If one does (e.g. an earlier clean cycle 
or interim snapshot), skip the forced write entirely — never downgrade an already-good row. 
Only when NO row exists for the day does a forced write proceed with a labeled `[FORCED after 
retry grace — value UNDERSTATED]` row. Invariant: an understated write can only fill a genuine 
gap, never regress an existing good/interim snapshot. Related API changes: `/api/auth/firm-nav` 
now caches + returns a `stale` flag; `/api/nav/by-account` masks `errors` for non-admin; 
`POST /api/nav/compute` returns `written: bool`. See `write_nav_snapshot`'s own docstring 
(nav.py, lines 937–1007) for the full reasoning and edge cases.

**Dismissible overlay layout and keyboard handling (2026-10-17, commits b1d5979d + 92d29cbe)** —
`PositionStrip.svelte`'s NavBreakdown popup (opened by clicking the P/M/C/H pills in the top NavStrip summary) had two structural defects in one investigation. (1) **Header/close-button placement**: the popup's header (containing the close button) and content were both children of the same element with `overflow-y: auto`, so tall content triggered a scrollbar that ate into the header's flex width, pushing the close button away from the panel's right edge — visually inconsistent with every other dark-theme popup in the app (which keep headers outside scrolling containers). Fixed: outer panel switched to `overflow: hidden` + flex column layout, with a new inner `.ps-bd-body` wrapper delegated to do the actual scrolling. (2) **Escape-key handling**: Escape registration lived as a raw `onkeydown` handler on a non-focusable `<div>` overlay, which can never actually receive keyboard focus or events; the popup couldn't be dismissed via Escape in real usage. Fixed: migrated to the shared `layerStack.js` Escape-stack coordinator (same pattern `PositionPinPicker.svelte` already uses), which listens once at the `document` capture phase independent of per-element focus. Invariant: (a) any dismissible overlay must keep its header/close-button OUTSIDE any element with `overflow-y: auto` (scrollbar gutter shrinks shared flex siblings), and (b) any dismissible overlay must register Escape via the shared `layerStack.js` coordinator via `pushLayer()/popLayer()`, never a raw per-element `onkeydown` on a non-focusable element.

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
| Dhan/Groww margin enforce | `/admin/settings` → `preflight.enforce_margin_{dhan,groww}` |
| Add MCP tool | `backend/mcp/kite_server.py` @app.tool() |
| Tune MCP audit | `/admin/settings` |
| Update macro data | `backend/config/backend_config.yaml` |
| Day P&L formula | `backend/api/algo/pnl_math.py` (`current_total_profit`, `baseline_diff_day_pnl`) + `frontend/src/lib/data/nav.js` (`baseDayPnlForPosition`, `currentTotalProfit`) |
| Day P&L rollup stores (positions) | `frontend/src/lib/data/portfolioStore.svelte.js` (account/symbol/total rollups); per-row `prev_settlement_pnl` backfilled in route-level responses |
| Baseline query (Day P&L snapshot path) | `backend/api/routes/positions.py:_SESSION_ANCHOR_CUTOFF_TS_SQL` + `_fetch_baseline_pnl_map` (derives 08:00 IST session boundary from `captured_at`, not `date` column; handles holdings-sold CNC split via `kind IN ('positions','holdings')`) |
| Market daily window / WebSocket lifecycle | `backend/api/background.py` + `backend/brokers/kite_ticker.py` |
| Market open/closed check | `backend/shared/helpers/date_time_utils.py:_segment_is_open` (YAML config) or `is_market_open` (gate-shaped dicts) |
| MCX evening-closed holiday handling | `backend/brokers/broker_apis.py:_fetch_mcx_evening_closed_dates` + `_upsert_mcx_evening_closed_sessions_coro` |
| Pulse Chg% / Day P&L % denominator | `frontend/src/lib/data/pulseUnified.js` (`_prevCloseSsot`, `finalizeRows`) |
| Postback subscribe new instrument | `backend/api/routes/orders.py:_subscribe_filled_pairs()` — canonical helper resolves (symbol, exchange) to token and calls `subscribe_with_sym()` (NOT bare `subscribe()`, which silently drops ticks). Wired into: Kite/Dhan/Groww postbacks, admin reconcile sweep, open-order watchdog, chase terminal-fill detection |
| `cancel_all_orders` CLI statement | `frontend/src/lib/command/grammars/agents.js` (bare-statement compile kind) + `backend/api/routes/orders.py` (`POST /api/orders/cancel-all`) + `backend/api/algo/actions_live.py` (`cancel_all_orders_scoped` shared helper) |
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
| Underlying spot resolution | `frontend/src/lib/data/resolveUnderlying.js:resolveUnderlyingTradingsymbol` + `frontend/src/lib/data/underlyingSpotStore.svelte.js:getUnderlyingSpot` (derivatives Snapshot, NavStrip, and Pulse Exp P&L) |
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
- **`/depl`** — Full pipeline: impl → ddev → dprod in one command (bypass-permissions)
- **`/tlm`** — Run daily TLM audit pipeline, parse P1 findings, fix + commit
- **`/cc`** — Show cyclomatic complexity grades (C/D/E/F summary + top 10 hotspots)
- **`/push`** — Quick push dev+main (no gates — use only for doc/config-only changes)
- **`/audit-cc`** — Block push if any D/E/F-grade functions exist; unblock if clean
