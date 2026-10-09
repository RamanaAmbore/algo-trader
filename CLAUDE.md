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
| `frontend/src/lib/*.js` or `*.svelte` | `frontend/tests/*.spec.js` (Playwright) |

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
`market_holidays` (daily 04:00 IST refresh, retry 30min until 08:00 IST) → NSE API (cold-boot). 
Empty sets cached; buster = date rollover Tiers 1+2, UPSERT Tier 3.

**Market segments** — blocks carry `sessions: list[{start, end}]` + `evening_open_on_holidays`. 
`is_market_open()` signature unchanged; keyword-only overrides when passed.

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

**Market daily window** — 08:00–23:31 IST. At 08:00: `fix_daily_book_prev_close()` sets BOTH `daily_book.ltp = prev_close = settlement close_price` — the only moment prev_close changes. NON-MCX snapshot at 15:45 (close+15 min) writes `ltp` only, no prev_close change. MCX snapshot at 23:45 (close+15 min) same. Ticker stops at 23:31. Closed window: 23:31→08:00 IST — routes serve `daily_book` snapshot only. Full schedule: memory `project_market_daily_window`.

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

**Order price tick-rounding carries floating-point residue (2026-09-28, commit 4c61e47f, CRITICAL)** — Order submission (`OrderTicket.svelte:_roundToTick`, line 1183; used by ticket, basket, chain, and modify routes) was snapping prices to the nearest tick but carrying genuine IEEE 754 floating-point residue from that snap operation. Example: 590.80 @ tick 0.05 snapped to 590.8000000000001 (before rounding), sent directly to Kite → rejected with "invalid price / decimal places" error. Live incident verified on multiple orders. Fix: after snapping to tick via `Math.round(px / tick) × tick`, re-round the result to the tick's own decimal precision: `Number(result.toFixed(decimalPlaces))`. Also applied in `backend/api/routes/orders_helpers.py:_align_price_to_tick` for POST endpoint validation. Invariant: tick-snap results are always rounded to the tick's own precision before transmission to broker.

**Order-lifecycle races — final-status guards and locking (2026-09-28, commit 97ed1c8c)** — Five linked defects across postback fan-out, chase cancel-and-replace, admin reconcile, and take-profit armature converged around race windows during order fills. Key invariants:

- **Final-status write guard** — `backend/api/models.py` defines two status sets: `ALGO_ORDER_TERMINAL_STATUSES` (the full lifecycle vocabulary, informational) and the narrower `ALGO_ORDER_FINAL_STATUSES` (`{FILLED, REJECTED}` only) — the set every status-writer actually guards against. Deliberately EXCLUDES `CANCELLED`/`CANCEL_FAILED`/`UNFILLED`: a failed cancel can still have a broker-side order resting live that fills later, so a genuine late FILLED from those states must still apply — only FILLED (can never un-fill) and REJECTED (broker refused it outright) are truly final. `_sync_apply_row_status`/`_pb_apply_status_to_row` (`orders_postback.py`) and `_chase_terminal_update_db` (`chase.py`) each check `row.status in ALGO_ORDER_FINAL_STATUSES` before mutating and refuse (log + no-op) if so — an application-level guard on the read row, not a model-level `__setattr__` interceptor. Admin reconcile (`orders.py:list_active_chases`) deliberately does NOT get this guard — it's the repair path that must be able to correct stuck rows from broker truth.
- **SELECT...FOR UPDATE locking across all mutation paths** — Kite postback (`orders_postback.py:_pb_event_kite`), Dhan/Groww postback (`orders_postback.py:_sync_algo_order_rows`), chase's terminal update (`chase.py:_chase_terminal_update_db`), and admin reconcile's bulk scan (`orders.py:list_active_chases`) all lock the `AlgoOrder` row(s) via `.with_for_update()` before mutating, serialising concurrent fill detections and manual edits.
- **FIFO ledger writes on all fill paths** — Only the Kite postback path (`_pb_write_ledger_fills`) ever wrote FIFO ledger entries; Dhan/Groww postback, chase's own terminal-fill path, and admin reconcile all silently skipped it. Fixed: all four paths now call `_pb_write_ledger_fills` (orders_postback.py) after a row transitions to FILLED, so a strategy's lot ledger reflects every fill regardless of which detection path caught it.
- **Take-profit double-arm race fixed** — Parent order check (`already has child take_profit`) now runs INSIDE the `with_for_update()` lock, not before; prevents two concurrent fills racing past the existence check and creating sibling TP orders.
- **Partial-fill TP qty correction** — `_arm_take_profit` now threads `filled_qty` (actual quantity already filled, not the full `order.quantity`) through to the TP order quantity calculation, closing a window where a partial fill could arm a TP sized for the pre-fill quantity.
- **Take-profit product correction** — `parent_product` was defaulting to NRML in one code path and being threaded correctly in another. Fixed: both callers of `_arm_take_profit` (postback + chase) now pass `parent_product` explicitly, eliminating the default.
- **Chase offsetting-position guard** — Chase path now verifies the same offsetting-position guard the Kite postback path was already running (reject close-legs with opposite-sign position or quantity exceeding open), bringing all paths to parity.

**Live ticket placement fail-closed on AlgoOrder pre-persist (2026-09-28, commit 8fca413b)** — Ticket placement (`orders_place.py:ticket_order_handler`) used to silently ignore DB insert failures when pre-persisting the `AlgoOrder` row, swallowing exceptions and proceeding to place a live order completely untracked. Root-caused via live prod incident: `AlgoOrder #1088` was placed live, chased for ~12 minutes, never appeared in operator views, and was never reconciled post-session. Fix: any DB insert failure (constraint violation, transaction rollback, timeout) now returns `HTTPException(503 SERVICE_UNAVAILABLE)` immediately, refusing the order and rolling back to client. Client sees a clear failure and can retry; no untracked live orders escape.

**OrderBook no longer flickers on transient broker-fetch failure (2026-09-28, commit 298cd38c)** — Order display grid (`OrderBook.svelte`) was falling through to algo-only stale data (a 5-min-old cache) on any transient broker fetch failure (network timeout, 429, DNS), causing rows to flicker cancelled/filled back to OPEN and breaking operator visibility. Related to "Staleness indicator freeze rule" — this is a sibling consumer. Fix: froze to last-known-good broker state on failure, showing the last fetch's result (empty if never fetched) instead of silently collapsing to local-only stale cache.

**Order submission gated on market depth; chase classifies recoverable vs non-recoverable failures (2026-10, commit daee65d2)** — Every template/ticket places only LIMIT or GTT orders (no MARKET), so submission needs the active strike's own bid/ask at least once before a price can be meaningful. `OrderTicket.svelte`'s `_lastQuote` (set via `onDepthQuote`) was never reset on a strike change, so switching strikes left a PREVIOUS strike's stale quote satisfying the "depth has loaded" check — an operator who clicked Submit the instant a new strike opened could reach the server and get a late, confusing "limit price required" rejection instead of a disabled button. Fix: `_lastQuote` resets to `null` on every resolved-symbol change (same effect that already resets `_lots`/`_lotsTouched`); a new `_depthPending` derived (`showLimit && !_lastQuote`) gates `submit()` (explicit early return before the broker call), OrderTicket's own footer Submit button (excluded for `_draftMode`, which never calls the broker), and — piped through the existing `onTicketStateChange` mirror — SymbolPanel's shared common-action Submit button (`_ticketDepthPending`) and its `_modalFireSubmit()` click handler.

Separately, `backend/api/algo/chase.py` now classifies order failures as recoverable or not instead of only special-casing `BrokerInputError`. `_ch_is_recoverable_error()` treats `BrokerInputError`/`BrokerCapabilityError`/`BrokerAuthError` as non-recoverable (abort the chase immediately, same as `BrokerInputError` always did); everything else — `BrokerRateLimitError`, `BrokerNetworkError`, and any untyped exception — retries (still capped by `_MAX_CHASE_ERRORS = 3`) with a fresh depth-derived price on the next attempt, since chase always recomputes price from live depth every iteration anyway. `BrokerRateLimitError` gets a longer backoff (`_CH_RATE_LIMIT_BACKOFF_SECONDS = 30`) than the normal re-quote interval so a burst of retries doesn't re-trip the same broker cooldown. A broker `REJECTED` poll status (previously ALWAYS terminal, no retry) is now re-examined by `_ch_rejection_is_recoverable()`: a price-shaped reason (`"price"`, `"circuit"`, `"tick"`, `"range"`, `"stale"`, `"band"`) returns a new `"rejected_continue"` signal that `_ch_handle_poll_signal()` treats exactly like the existing `"cancelled_continue"` signal — back off `cfg.rejection_backoff_seconds`, reset `current_order_id`, and let the loop reprice and retry — completing a design the `ChaseConfig.rejection_backoff_seconds` docstring already described but the code never wired up for REJECTED. **Operator instruction, explicit and non-negotiable: margin/RMS/risk/permission rejections are NEVER recoverable**, even if the same message also mentions price (e.g. "margin shortfall for this price range") — `_ch_rejection_is_recoverable()` checks `_CH_NON_RECOVERABLE_REJECTION_HINTS = ("margin", "rms", "risk", "permission", "blocked")` FIRST and returns `False` immediately on a match, before ever consulting the price-hint list.

**Frontend request-generation guard against out-of-order fetches (2026-09-28, commit aae9fd03)** — `createDataStore` in `frontend/src/lib/data/dataStore.svelte.js` now carries a generation/request-ID on every fetch and discards responses from older generations. A slower older fetch could previously clobber a faster newer one's result, leaving stale data displayed as current. Invariant: response consumer always rejects any payload with gen_id < current_gen_id before applying to store state.

**MCX virtual-root alias no longer flips between calendar-spread legs (2026-09-28, commit d49d003d)** — `_add_mcx_spot_anchors`'s Pass 2 (`backend/api/background.py`, pure-futures positions with no CE/PE) aliased whatever specific-expiry symbol was already held, not necessarily the front-month contract. `_virtual_root_aliases` (`kite_ticker.py`) is keyed by TOKEN, not root, so holding both legs of a calendar spread on the same root (e.g. `CRUDEOIL26OCTFUT` + `CRUDEOIL26NOVFUT` both held at once) registered TWO tokens under the one virtual-root symbol "CRUDEOIL" — whichever leg ticked most recently silently won the shared slot any SSE/`getSnapshot("CRUDEOIL")` consumer reads. Fix: Pass 2 now resolves the SAME canonical front-month contract Pass 1 uses (`list_active_futures(root, limit=1)`) for these roots too, deduplicated per root, instead of aliasing whichever expiry happens to be held. Invariant: any virtual root resolves to exactly one active contract token at any given time, for both options-anchored and pure-futures-only roots.

**NAV forced write now scoped to genuine gaps (2026-09-28, amends existing entry)** — The "NAV write-skip / force-write policy" entry already documents the skip-on-understated behavior. Amending: `_run_nav_compute_once` (`backend/api/background.py:7075`) now passes a pinned `target_date` to `write_nav_snapshot()` instead of letting the function recompute "today" AFTER its own potentially-slow `compute_firm_nav()` call. If NAV compute straddles IST midnight, the write could land on the wrong calendar date and break date-based query filters. By pinning the date at entry, the write goes to the intended date regardless of compute duration.

**Alert-latch hydration query now bounded to today at SQL level (2026-09-28, amends existing entry)** — The "Alert evaluation and latching" entry's "Deploy-survival hydration" paragraph already documents hydration from today's `agent_events` rows. Amending: the database query bound is now enforced at the SQL level (`_v2_hydrate_latch()` in `backend/api/algo/agent_engine.py:2170`) instead of pulling full history and filtering in Python. On process restart, this bounds the hydration cost to one day's data instead of scanning the entire `agent_events` table, important for long-running production instances with years of historical events.


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
| Day P&L formula | `backend/api/algo/pnl_math.py` (`current_total_profit`, `baseline_diff_day_pnl`) + `frontend/src/lib/data/nav.js` (`baseDayPnlForPosition`, `currentTotalProfit`) |
| Day P&L rollup stores (positions) | `frontend/src/lib/data/portfolioStore.svelte.js` (account/symbol/total rollups); per-row `prev_settlement_pnl` backfilled in route-level responses |
| Baseline query (Day P&L snapshot path) | `backend/api/routes/positions.py:_SESSION_ANCHOR_CUTOFF_TS_SQL` + `_fetch_baseline_pnl_map` (derives 08:00 IST session boundary from `captured_at`, not `date` column; handles holdings-sold CNC split via `kind IN ('positions','holdings')`) |
| Market daily window / WebSocket lifecycle | `backend/api/background.py` + `backend/brokers/kite_ticker.py` |
| Postback subscribe new instrument | `backend/api/routes/orders.py:_subscribe_filled_pairs()` — canonical helper resolves (symbol, exchange) to token and calls `subscribe_with_sym()` (NOT bare `subscribe()`, which silently drops ticks). Wired into: Kite/Dhan/Groww postbacks, admin reconcile sweep, open-order watchdog, chase terminal-fill detection |
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
