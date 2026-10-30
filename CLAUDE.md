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
- MCP server + MCP page (`/admin/mcp`, chat-driven research — renamed from "Lab")

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

**Account-masking campaign — central log filter, producer fixes, numeric Dhan client_id
(2026-10-24; commits `fbb9b785`, `d67266f2`, `f04437cc`)** — A broad audit found raw broker
account codes reaching logs and notifications despite the `mask_account()` convention. Three parts.
- **Central log filter (`fbb9b785`).** `_AccountMaskFilter`
  ([log_store.py:59](backend/shared/helpers/log_store.py#L59)) is attached to the same
  `QueueHandler` as `OriginFilter`, inside `get_logger()`
  ([ramboq_logger.py:112](backend/shared/helpers/ramboq_logger.py#L112)). It is NOT in the later
  per-handler loop. That loop runs on the `QueueListener` thread, after `prepare()` has already
  baked the message, and it misses `log_store_handler`. Placing the filter at enqueue time means
  console, file, error-file and `log_events` (and so the alert pipeline) all get masked text. The
  filter also masks `exc_text`, because an exception's own text (e.g. `KeyError: 'ZG0790'`) would
  otherwise bypass message-only masking.
- **CRITICAL alerts now match `ERROR_AGENT`.** `tags_for()`
  ([log_store.py:136](backend/shared/helpers/log_store.py#L136)) stamps `"error"` on every CRITICAL
  record. `ERROR_AGENT` requires that literal tag, so a bare `logger.critical(...)` never alerted
  before. The stored agent condition was deliberately left alone. Editing it would need a
  `SEED_VERSION` bump, and the `in` operator does not fit `_eval_log()`'s matching shape. A
  CRITICAL record that carries `alert_event` is skipped by `_error_agent_self_alerted()`
  ([event_agents.py](backend/api/algo/event_agents.py)), so dedicated CRITICAL agents do not
  double-fire.
- **Producer fixes (`d67266f2`).** The market-open/close summary tables in `alert_utils.py`
  (`_build_funds_rows`, `_summary_holdings_rows`, `_summary_positions_rows`,
  `_partial_outage_warning_lines`) used to send raw account codes in every Telegram and email row.
  They now mask before building content. Four MCP ping sites in `routes/lab.py` (cancel-live,
  cancel-paper, modify-paper, modify-live) now mask, not only the place-ping. The
  `template_attach.py` broker-resolution catch and the three `event_agents.py` renderers
  (`_render_partial_gtt`, `_render_template_attach`, `_render_gtt_not_accepted`) scrub free text with
  `mask_account_in_text()`. `connections.py` logs Dhan token-response status and keys only, never the
  body. Exception text in `chase.py` is masked at source.
  - **Double-mask guard.** `mask_account()` is NOT idempotent. Re-masking `Z1####` yields `Z#####`.
    The `actions_live.py` re-raise sites are therefore left unmasked on purpose, because `chase.py`
    already masked the value upstream. Do not add a second mask at those sites.
- **Numeric Dhan `client_id` (`f04437cc`).** Dhan's numeric `client_id` is a different value from
  the internal `DH3747`-style code. It was never in the registry and fell through to the
  digit-stripping fallback, which yields all `#`. `register_accounts()`
  ([utils.py:243](backend/shared/helpers/utils.py#L243)) now takes `client_ids` and ALIASES each
  one to its account's already-computed mask. It does not derive a new mask from the numeric
  digits, because both real Dhan ids share their first two characters and would collide. The
  mapping is threaded through `connections.py`, `brokers/service/schemas.py` and
  `brokers/service/routes.py`. `_pb_redact_payload_for_log()` in `orders.py` masks the value
  instead of dropping the key.
  - **Deploy coupling.** This touches `backend/brokers/`, so it takes effect only after a PROD deploy
    that sets `CONN_TOUCHED` (see the Things to Avoid note on `ramboq_conn`).
- Invariant: a raw broker account identifier, alphanumeric or numeric, never reaches a log file,
  `log_events`, Telegram, ntfy or email unmasked. Tests: `test_log_store.py` (filter and
  `tags_for`), `test_dhan_numeric_account_mask_2026_10_24.py`.

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

**Ticker idle badge on non-trading days — status-only refresh (2026-10-24; commits `0410c7e3`,
`a252883c`)** — The calendar-gated idle state lives in `_TICKER_CALENDAR_STATE`
([background.py:3764](backend/api/background.py#L3764)). Its only writers were the connect paths,
and all of them run inside 08:00–23:30 IST. Outside that window, or after a restart until the next
in-window tick, the state stayed unset. `get_ticker_calendar_status()` then reported not-idle, and
the badge showed green on a confirmed closed day. Fix: `_ticker_calendar_refresh_idle_status(today)`
([background.py:3980](backend/api/background.py#L3980)) is a status-only helper that can run at any
hour. It takes no `do_connect` parameter, so it cannot connect by construction. It is hooked into
the ticker-watchdog loop, which already runs at all hours. On an OPEN or UNKNOWN verdict it leaves
state alone. "Trading day, not yet connected" must never show as idle.
- **Frontend.** The per-account circles and status text in the popup now respect idle too, not just
  the top-level banner. `brokerAccountDisplayState(state, tickerIdle)`
  ([brokerHealthState.js:116](frontend/src/lib/data/brokerHealthState.js#L116)) keeps the
  red > idle > amber > green precedence, so a genuinely failed account is never masked as idle.
- Tests: `backend/tests/test_ticker_calendar_gate.py` (status-only block),
  `frontend/src/lib/__tests__/data/brokerHealthState.test.js`.
- Invariant: a closed-day idle state comes from the calendar at any hour. A connected or failed
  account is never displayed as idle.
- *Amended 2026-10-28 — explicit clear at 23:31.* `_snapshot_stop_ticker()`
  ([background.py:3315](backend/api/background.py#L3315)) now marks `_TICKER_CALENDAR_STATE` as
  `connected=False, idle=True` in a `finally` ([:3363](backend/api/background.py#L3363)) at the
  23:31 stop. It no longer waits for the midnight date rollover. Before this, the connected flag
  stayed set for about 29 minutes after the real stop, and `ticker_connected_today()` reported a
  connection that no longer existed. Invariant: a stopped ticker is never reported as connected.

**Snapshot freshness checker — `check_stale_snapshots.py` config and DSN (2026-10-24; commit
`b753b0c8`)** — The "never blank during closed hours" check
([scripts/check_stale_snapshots.py](scripts/check_stale_snapshots.py)) appeared to skip on every
real server. Three defects, all fixed.
- **YAML parsing.** A hand-rolled `_load_yaml_simple` treated `#` as a comment even mid-value. A DB
  password containing `#` was truncated, so auth failed and the script's exit code 3 ("DB
  unreachable, expected on local dev") hid a real on-server bug. Replaced with `yaml.safe_load()`.
- **DSN construction.** `_build_dsn()` spliced the raw password into a `postgresql://` URL. `#` then
  starts a URL fragment, which truncated the DSN a second, independent way. `user` and `password` are
  now `urllib.parse.quote(..., safe="")`.
- **Nav surface column.** The `_SURFACES` nav query used a nonexistent `date` column. It now uses
  `NavDaily.as_of_date`. Before this, the check reported `MISSING — column "date" does not exist`.
- Both YAML and DSN fixes were live-verified on prod and dev. Each server file was restored byte-exact
  afterward, confirmed by md5sum.
- Tests: `backend/tests/test_check_stale_snapshots_yaml.py`.
- Invariant: a freshness check must never classify a config-parse or DSN-construction defect as the
  expected "DB unreachable" exit.

**Agent action-dispatch SSOT (2026-10-08; `emit_log`→`log` rename + `help` added 2026-10-23)** —
`_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS`
in `backend/api/algo/actions.py` are the explicit, sole action-dispatch tables — real broker
actions route through `_dispatch_live_action()` to `_action_live_*` functions in
`actions_live.py`; non-broker noops (`monitor_order`/`deactivate_agent`/`set_flag`/`log`/`help`/`send_summary`/
`send_market_status`) route through `_al_run_noop_handler()`. The DSL token `log` (dispatch key,
renamed from `emit_log` 2026-10-23 — see the dedicated entry below) still resolves to the
Python function `emit_log` (unchanged name — a dispatch table's dotted resolver path doesn't
have to match its DSL token string). `help` resolves to `help_lookup` (named to avoid
colliding with the Python builtin). `GrammarRegistry`'s `actions` table
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

**`emit_log`→`log` rename, new `priority` param, `help` action, structural DEBUG exemption
(2026-10-23, items 3/5/7 of the grammar-unification plan)** — Three linked changes, all in
`backend/api/algo/actions.py`, `grammar_registry.py`, `event_agents.py`,
`backend/config/grammars/agent_grammar.yaml`.
- **Rename.** The DSL token `emit_log` → `log` (operator wants simple/small DSL words).
  Confirmed via live DB check on both prod (`ramboq`) and dev (`ramboq_dev`): zero existing
  `Agent` rows referenced `emit_log` in their compiled JSON, so this was a clean rename — no
  migration script, no backward-compat dispatch alias. `_NOOP_ACTION_HANDLERS`'s dispatch KEY
  changed from `"emit_log"` to `"log"`; the Python resolver function kept its original name
  `emit_log` (`_HandlerRef(__name__, "emit_log", True)`), same precedent as `help`/`help_lookup`
  below — a dotted resolver path never has to match its DSL token string.
- **New `priority` param on `log`.** `log`'s `params_schema` grew an optional `priority` field
  (`low`/`default`/`high`/`urgent`, reusing `alert_utils._TIER_TO_NTFY_PRIORITY`'s own
  vocabulary, not a new one) and its `level` enum grew `critical`. `emit_log()` threads
  `priority` into the log call's `extra` dict (`extra={"priority": priority}`) ONLY when set —
  every pre-existing caller with no `priority` param gets the exact same single-positional-arg
  logger call as before, zero regression. This is the same `extra`-dict threading mechanism
  `agent_engine.py:_v2_send_rich_alert()` already uses for `tier` (see the "Rich-alert ntfy
  priority" entry above). `event_agents.py:_rec_tracked_ntfy_priority()` now checks an explicit
  `rec["extra"]["priority"]` FIRST, before falling back to the pre-existing tier-derived
  resolution — a hand-typed `log(level=..., priority=..., message=...)` call's explicit
  priority overrides the matching `kind='event'` agent's own tier default. `debug(x)`'s
  compiled sugar never sets `priority` — confirmed: it compiles to `log(level="debug",
  message=...)` and nothing else, one bare argument, no priority/urgency ever.
- **New `help` action.** Read-only catalog introspection — resolver `help_lookup` (named to
  avoid colliding with the Python builtin `help`). `GrammarRegistry.find_tokens(name)` (new
  method) reverse-resolves a token string to every matching `GrammarToken` row — a name can be
  reused across `token_kind`s (e.g. the real live catalog's own `(notify, channel, log)` token,
  pre-existing, and the renamed `(action, action_type, log)` token both share the string
  `"log"`) — every match is returned, never guessed at. For each match, `_help_resolve_callable`
  dispatches to the right `REGISTRY` accessor by `token_kind` (`.metric()`/`.scope()`/`.op()`/
  `.channel()`/`.fmt()`/`.template()`/`.action()`), and `_help_reflect` renders the resolved
  value's `inspect.signature()` and `inspect.getdoc()` — never hiding a missing piece: `None`
  (a dead/unwired resolver) renders explicit "(signature unavailable)"/"(no docstring)"; a
  real but non-callable value (e.g. a notify `template` token's body STRING) renders the
  actual value rather than Python's own misleading `str.__doc__`. Logged at `logger.debug(...)`
  with **no** `extra={"tags": [...], "alert_event": ...}` at all — belt-and-suspenders on top of
  the structural exemption below, not the real protection.
- **Structural DEBUG exemption.** `event_agents.py:_dispatch_agent`'s per-record loop now drops
  any `rec.get("level") == "DEBUG"` record as the FIRST check, before `agent.conditions` is even
  evaluated — applies uniformly to every `kind='event'` agent (seeded AND any future
  operator-authored one), never a hardcoded list. Protects both `help` and bare `debug(x)` from
  ever reaching a real notification channel regardless of what any event agent's own conditions
  claim to match. Verified: `debug(x)`'s compiled sugar calls `logger.debug(...)`, which tags
  `record.levelname == "DEBUG"` (standard Python logging, uppercase) — matches this exemption's
  string compare exactly, no casing gap. Belt-and-suspenders note: `log_store.py`'s
  `LogStoreHandler` is already instantiated at `level=logging.INFO`, so a real DEBUG record
  never reaches `_dispatch_agent`'s `records` list in production today via the normal
  log→`log_events`→dispatch pipeline — this exemption guards `_dispatch_agent` itself against
  any future change to that threshold (or a synthetic record built some other way), not the
  sole protection. Test: `backend/tests/test_help_lookup_action.py::TestDebugExemption` (confirmed
  to fail against pre-fix code — `sent == 1` instead of `0` — before the exemption line was
  added).
- Golden baseline (`backend/tests/golden/agent_grammar_baseline.json`) updated in lockstep at
  the exact same list position as the live YAML — the renamed `log` row, and a newly-inserted
  `help` row immediately before it (right after `set_flag`) — so
  `test_system_tokens_byte_for_byte_equal_to_baseline` stays a real, order-sensitive check.
- Frontend rename status (verified 2026-10-24): the rename has landed in
  `frontend/src/lib/command/grammars/agents.js`. `debug(x)` and `log(...)` compile to
  `type: 'log'`, and no `emit_log` token remains in that file. The backend dispatch table accepts
  ONLY `"log"`, with no alias. Still stale, and not yet updated: test fixtures in
  `frontend/src/lib/__tests__/consoleDispatch.test.js` (e.g. `DO emit_log()`), which no longer
  name a valid token, plus `agentsDecompiler.test.js` fixtures and comments in `LogPanel.svelte`
  (~L355) and `backend/api/algo/events.py` (~L188). Those are follow-up test/comment cleanups.

**Unified agent DSL — one statement vocabulary, `WHEN…DO…` is the only conditional wrapper
(2026-10-23; commits `e509bcf1`, `dbcf77ec`, `cbfa32e2`, `eeb4b478`)** — The Console grammar in
[agents.js](frontend/src/lib/command/grammars/agents.js) has one shared statement vocabulary:
label assignment, `order(...)`, `bracket(...)`, `alert(...)`, `log(...)`, `debug(...)`, `help(...)`,
and any other real action call. Changes from the old 4-clause `GIVEN/WHEN/ALERT/DO` shape:
- **`GIVEN` is removed.** A leading `given` is rejected with a specific error
  ([agents.js:3237](frontend/src/lib/command/grammars/agents.js#L3237)). `given` and `alert` are
  no longer reserved words ([agents.js:680](frontend/src/lib/command/grammars/agents.js#L680)).
- **`ALERT` is not a clause keyword.** `alert(...)` is an ordinary call. It compiles through the
  unchanged `compileAlertClause` machinery, in the preamble or inside `DO`, with identical output.
  The standalone event statement `ALERT log_tag=... render=...` is unchanged.
- **`WHEN ... DO ...` is one optional, paired wrapper.** `WHEN` without `DO`, or `DO` without
  `WHEN`, is a compile error. `DO nop` alone is rejected.
- **The preamble is everything before `WHEN`.** With no `WHEN`, the whole statement is the
  preamble. It is self-delimiting by position and accepts the full shared vocabulary. A
  preamble-only statement is a complete, standalone statement.
- **Compiled JSON gained `preamble: list[dict]`.** Each entry is `{"type":"alert","events":[...]}`
  or `{"type":<action>,"params":{...}}`. The key is omitted when empty. Execution semantics are in
  the preamble entry below.
- **`help(<name>)`** is a read-only catalog lookup, logged at DEBUG. It is the canonical DEBUG-tier
  example, never alert-eligible (see the DEBUG exemption entry above). It is available on the
  Console tab only. The order-scoped CLI mounts reject it through `allowedKinds`.
- **`log(level=..., priority=..., message=...)`** replaces `emit_log`. `priority` is read by a
  matching event agent's dispatch as an ntfy priority override
  ([event_agents.py:593](backend/api/algo/event_agents.py#L593)).

Invariant: there is one vocabulary. Position decides whether a call is gated by a condition
(preamble vs `DO`). Call type decides how often a saved agent's preamble call runs.

**Saved cycle-agent preamble — per-type execution split; side effects once per activation (2026-10-23;
commits `4e6df1ec`, `6aaec581`; P1 safety fix)** — Two execution models now coexist. A bare CLI
statement with no save step runs once, synchronously, at submission (unchanged). A SAVED `Agent`'s
preamble runs inside `run_cycle` through `_cycle_run_preamble()`
([agent_engine.py:3659](backend/api/algo/agent_engine.py#L3659)). It is called from
`_ae_cycle_eval_and_buffer` before the `in_cooldown` short-circuit
([agent_engine.py:4118](backend/api/algo/agent_engine.py#L4118)). Per type:
- **`help`, and `log(level="debug")` (`debug(x)`)** run on every cycle, uncapped, through the normal
  `execute()` path. They make no DB claim.
- **`alert`, and non-debug `log`** run on every cycle but are windowed by the agent's own
  `cooldown_minutes`. The gate is `_cycle_preamble_alert_cooldown_ready()`
  ([agent_engine.py:3423](backend/api/algo/agent_engine.py#L3423)), which claims
  `Agent.preamble_alert_last_fired_at`. It is a separate mechanism from both the ordered-condition
  cooldown and the once-per-activation marker. Sharing either would corrupt one of them.
- **Everything else** (`order`, `place_order`, `close_position`, `cancel_order`, `bracket`, and any
  other action) fires at most once per activation. An atomic `UPDATE ... WHERE preamble_fired_at IS
  NULL` claim runs in `_cycle_claim_preamble_activation()`
  ([agent_engine.py:3518](backend/api/algo/agent_engine.py#L3518)). It is claim-then-fire, so a DB
  failure on the claim fails safe: nothing fires, and the claim is retried next cycle.
- **`bracket`** has no handler in `_LIVE_ACTION_HANDLERS` or `_NOOP_ACTION_HANDLERS`. Its entry logs
  one WARNING per activation and is skipped
  ([agent_engine.py:3647](backend/api/algo/agent_engine.py#L3647)).
- **Activation** means a real inactive/paused → active transition in `_age_set_status`
  ([routes/agents.py:1209-1216](backend/api/routes/agents.py#L1209)). That transition NULLs both
  `preamble_fired_at` and `preamble_alert_last_fired_at`. Re-clicking Activate on an active or
  cooldown agent does not re-arm. Editing an active agent's preamble does not re-arm either. The
  operator must deactivate, then reactivate.
- **Columns.** `Agent.preamble` (JSONB), `preamble_fired_at`, `preamble_alert_last_fired_at`, and
  `has_explicit_alert`, in [models.py](backend/api/models.py). Migrations
  `_migrate_agents_preamble`, `_migrate_agents_preamble_alert_cooldown` and
  `_migrate_agents_has_explicit_alert` ([database.py:1073](backend/api/database.py#L1073),
  [:1096](backend/api/database.py#L1096), [:1114](backend/api/database.py#L1114)) run in `init_db()`.
- **Sim and bypass isolation.** Sim and bypass-schedule runs track the once-bucket and cooldown on
  `alert_state` (`_sim_preamble_fired`, `_sim_preamble_alert_cooldown`), never on the DB columns.
  Replay skips the whole preamble. `actions_sim._replay_paper_trade` writes a `mode='replay'` order
  row for any action type, including `log`/`help`.
- **P1 this fixes.** Before `4e6df1ec`, a saved agent with `order(...)` in its preamble placed a real
  broker order on every cooldown window (30 min default), indefinitely. The direct regression test is
  `test_side_effecting_order_fires_exactly_once_across_multiple_poll_cycles` in
  [test_agent_preamble_execution.py](backend/tests/test_agent_preamble_execution.py).
- **Second guard, same change.** A preamble-only statement compiles its `conditions` to the
  always-true sentinel that scheduled market-open agents use. `_v2_send_rich_alert` fires on every
  match, so without a guard the agent would emit a spurious breach alert each window and burn
  `lifespan_max_fires`. `_cycle_agent_is_preamble_only()`
  ([agent_engine.py:3321](backend/api/algo/agent_engine.py#L3321)) returns before matches are
  buffered. It type-checks `isinstance(preamble, list)`, not truthiness, because an unconfigured
  `MagicMock` auto-vivifies `.preamble`.
- **Known limitations (deliberate):**
  - Replay skips the entire preamble, not only its side-effecting calls.
  - `bracket` in a preamble or `DO` is inert until a real bracket-creation handler exists.
  - Editing an active agent's preamble requires deactivate + reactivate to re-fire one-shot calls.
  - `preamble_fired_at` is written on a successful claim, but it is not exposed through
    `AgentInfo`/`_agent_to_info`. A preamble-only fire does not update `trigger_count` or
    `last_triggered_at`, so `/agents` shows "never fired" after its one-shot order went out.
  - `_age_validate_action_entries` runs on `actions` only
    ([routes/agents.py:1005](backend/api/routes/agents.py#L1005),
    [:1163](backend/api/routes/agents.py#L1163)), never on `preamble`. A raw API or MCP write can
    store an entry the Console compiler would reject. Execution gates (G1/G2, exchange-open,
    preflight) still apply, so this is a validation gap, not an execution-safety gap.
  - MCP's status write `_res_flip_agent_status_db`
    ([routes/lab.py:1141](backend/api/routes/lab.py#L1141)) bypasses `_age_set_status`. An MCP
    re-activation does not re-arm the one-shot. The first activation does, since the column starts
    NULL. This errs toward under-firing.

Invariant: a side-effecting preamble call never fires on a poll cycle. Only a real activation re-arms
it. Notification-shaped preamble entries are rate-limited by the agent's own cooldown. `help` and
`debug(x)` are never rate-limited.

**`has_explicit_alert` — a DO-level `alert(...)` replaces the automatic rich alert; sticky-false
Console round-trip fix (2026-10-23; commits `08bf4340`, `6aaec581`, `b6f56be7`; item 0.C)** —
- **Set only by a real DO-level call.** The frontend sets `has_explicit_alert: true` only when a
  `DO` clause compiles to a real `alert(...)` call (`doResult.events.length > 0`,
  [agents.js:5897](frontend/src/lib/command/grammars/agents.js#L5897)). A preamble-level
  `alert(...)` does NOT set it. A preamble alert is an unconditional notice, not a
  breach-notification override.
- **Engine.** `run_cycle` computes `use_explicit_alert = has_explicit_alert is True and
  bool(agent.events)` ([agent_engine.py:4657-4660](backend/api/algo/agent_engine.py#L4657)). When
  true, `_v2_send_rich_alert()` is skipped, and the normal `events.dispatch()` delivers the agent's
  own events in a generic body. The `bool(agent.events)` guard means a stale `True` with an emptied
  list falls back to the rich alert, not silence. `is True` is used rather than truthiness for the
  same `MagicMock` reason as above.
- **Opt-in.** Every existing row is `false` (server default, backfilled). Pre-existing agents see no
  behavior change.
- **Round-trip hazard, fixed in `b6f56be7`.** `decompileAgent` renders any non-empty `events` list as
  `DO alert(...)`. Seed-shaped agents (every `loss-*` builtin via `_LOSS_AGENT_DEFAULTS`) carry a
  populated `events` list with `has_explicit_alert=false`. Load-edit-save through Console would have
  silently and permanently flipped them to explicit-alert mode, dropping the kind/threshold table
  with no visible text change. `updateConsoleAgent(slug, compiledAgent, rawText, hadExplicitAlert)`
  ([consoleDispatch.js:550](frontend/src/lib/command/consoleDispatch.js#L550)) is therefore
  sticky-false: `hadExplicitAlert === true ? !!compiledAgent.has_explicit_alert : false`.
  `createConsoleAgent` has no existing row to be sticky against, so it trusts the fresh compile.
  The loaded row's value is threaded through `ConsoleTab`'s edit target, the same load-then-diff
  pattern `given` and `preamble` use.
- **Deliberate consequence.** An operator cannot convert an existing seed-style or form-style agent
  into explicit-alert mode through ordinary Console editing. A dedicated "convert" action is a
  future feature.
- **Tier threading (dormant gap, closed).** `_emit_breach_record`
  ([events.py:425](backend/api/algo/events.py#L425)) carries the firing agent's `tier` into its log
  extras ([events.py:461](backend/api/algo/events.py#L461)). The ntfy-priority resolution from
  "Rich-alert ntfy priority" therefore covers the two new dispatch paths, item 0.C and preamble
  alert/log. Those are the first callers that could reach the gap.
- **Deploy coupling** (same convention as the multi-commit entries above). `6aaec581` (the engine
  skips the rich alert) must not ship without `b6f56be7` (sticky-false save). Without it, any Console
  edit of a seed-style `loss-*` agent silently drops its rich kind/threshold alert. `cbfa32e2` (the
  backend accepts only the `log` token) needs `e509bcf1`'s frontend rename. All of these are
  unpushed on `workshop`, so ship the range together.
- Tests: `test_agent_preamble_execution.py::TestHasExplicitAlertStructWiring`, and
  `frontend/src/lib/__tests__/consoleDispatch.test.js` (sticky-false cases).

Invariant: a saved agent's breach-alert channel changes only through an explicit `alert(...)` in its
`DO`. Re-saving a row through Console must never flip `has_explicit_alert` by accident.

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

**Every alert leads with its origin; manual-sentinel classification; two dead alert paths fixed
(2026-10-23; commit `70802eb5`)** — This extends the "Rich-alert ntfy priority" entry above to every
channel (Telegram, ntfy, email). Three defects:
- **Origin was inconsistent across renderers.** Rich/dispatch alerts carried `Agent:` on line 2. Fill
  alerts put `Source: MANUAL OPERATOR ORDER` on line 4. Template guard/attach-fail, chase-cancel,
  partial-GTT and gtt-not-accepted carried no origin at all.
- **The manual sentinel was misclassified on failure.** `_classify_order_origin_label()` checked
  `agent_id is not None`, which does not recognise the manual sentinel. A manual order's FILL alert
  said "manual" while its REJECTION alert said `[Agent]`.
- **Two alert paths were dead.** `_opp_send_cancel_alert()`
  ([`orders.py:802-818`](backend/api/routes/orders.py#L802)) called `send_ntfy_alert(msg)` with one
  positional argument, which bound to `title` and raised `TypeError` inside a fire-and-forget task.
  Every postback CANCELLED alert was silently lost. `background.py` had five calls to
  `_alert_route()`, which no longer exists (`ImportError`). Those covered gtt-asymmetric,
  OCO-sibling, ticker-recovered, ticker-degraded and visitor-report. Two had no try/except, so the
  error reached the ticker watchdog. Two embedded unmasked account numbers.

The fix:
- `resolve_origin(agent_id, agent_name)` ([`alert_utils.py:535`](backend/shared/helpers/alert_utils.py#L535))
  returns the real name, or `"manual"` for a `None` id, the cached manual-sentinel id, or the
  name "Manual operator order". An unresolved real id yields `Agent #{id}`, never `"manual"`.
- `format_notification_header()` ([`alert_utils.py:568`](backend/shared/helpers/alert_utils.py#L568))
  always emits `Agent: {origin}` as line 1, or `Agent: {name} (#{id})` when a real id is present
  ([`:603`](backend/shared/helpers/alert_utils.py#L603)). Every renderer in
  [`event_agents.py`](backend/api/algo/event_agents.py) routes through it, so fill, failure,
  template, chase-cancel, partial-GTT and gtt-not-accepted all lead with origin.
- `_classify_order_origin_label()` ([`alert_utils.py:1017`](backend/shared/helpers/alert_utils.py#L1017))
  uses `resolve_origin()`, so a manual order's fill and failure alerts agree.
- `_opp_send_cancel_alert()` splits title from message, masks the account, and prefixes
  `format_notification_header(None, None)`. The postback fan-out has no agent context, so it
  reads `Agent: manual`.
- The five `background.py` sites moved to the log-record and event-agent pipeline (e.g.
  `TICKER_RECOVERED_AGENT`, [`background.py:6203-6213`](backend/api/background.py#L6203)). The two
  unguarded ones are wrapped in try/except, and account numbers are masked.
- Known imprecision: `_render_error()` ([`event_agents.py:244`](backend/api/algo/event_agents.py#L244))
  is not order-scoped, so a generic ERROR+ log alert reads `Agent: manual`. That is the honest answer
  for a log line with no agent. A caller that attaches a real `agent_id` gets the real name.
- *Amended 2026-10-25 (commit `17a1c85a`) — the line-1 invariant below does not hold for every
  producer.* `generic_notice` bodies (the five `background.py` sites, plus deploy-sync and
  visitor-report) carry no origin line. Summary dispatches are agent-less by design. See "Alert
  message format" below for the verified scope.
- Invariant: origin is always line 1 of every outbound message. A manual order shows the same origin
  on every alert for that order, never `manual` on one and `[Agent]` on another.

**Alert routing hygiene — chase re-quote suppression, self-alerted ERROR gate, masking (2026-10-23;
commit `6ba6936e`)** — Four alert-path defects, bundled into one commit: routine chase events paging the
operator, an unscoped ERROR catch-all double-alerting, a silently hidden DB write failure, and an
unmasked account number.
- **A chase's own re-quote CANCELLED no longer fires an urgent push.** Every cancel-and-replace
  produces a broker CANCELLED postback for the superseded order. `_opp_cancel_is_own_chase_requote()`
  ([`orders.py:909`](backend/api/routes/orders.py#L909)) recognises it: the row maps to an AlgoOrder
  that `chase.is_chase_active()` reports as live in this process. `_opp_send_cancel_alert()`
  ([`orders.py:940`](backend/api/routes/orders.py#L940)) gates on that check, so a 30-attempt chase no
  longer produces 30 urgent 3x-repeat pushes (see "Rich-alert ntfy priority" above). Fail-safe: any
  lookup error returns False, which means the alert still fires. A genuine cancel always alerts.
- **ERROR_AGENT no longer double-alerts on errors that already alert.** ERROR_AGENT is a blanket
  catch-all: every `logger.error` matches it. Callers that already fire their own operator alert tag
  the log call with `extra={"self_alerted": True}`. Current callers are chase's max-attempts exhaustion
  ([`chase.py:2333-2341`](backend/api/algo/chase.py#L2333)) and `actions_live.py`'s close-position
  failure path ([`actions_live.py:752-759`](backend/api/algo/actions_live.py#L752)). The gate is
  `_error_agent_self_alerted()` ([`event_agents.py:718`](backend/api/algo/event_agents.py#L718)). It
  applies only to ERROR_AGENT, so any other agent keyed on ERROR is unaffected. Note the
  `_render_error()` imprecision above: the `self_alerted` flag does not change how an unscoped ERROR
  line renders.
- **Kite postback DB-write failures are logged at WARNING, not DEBUG.** `_pb_event_kite`'s catch-all
  around the COMPLETE/REJECTED status write hid real failures at the production log level. A failed
  write skips the ledger and template-attach fan-out, and the 5s follow-up only catches the status
  mismatch.
- **Account numbers are masked in every chase-path alert.** One chase alert embedded a raw account
  number. Its source is fixed at the source (`_ch_abort_ambiguous_place_timeout`). Defense in depth
  sits in `order_failure_messages()` ([`alert_utils.py:1045`](backend/shared/helpers/alert_utils.py#L1045)),
  which runs `mask_account_in_text()` on the caller-supplied `error` text before rendering.
- Invariant: a routine, self-resolving chase event never pages the operator. A genuine cancel, error
  or failure always does, and an account number never reaches a channel unmasked.

**generic_notice HTML leak — Telegram showed literal `<b>` tags (2026-10-23; commit `cb9d24d0`)** —
Operator report: visitor-report alerts showed `&lt;b&gt;` text in Telegram (email rendered them
correctly). Root cause: the `generic_notice` renderer, `_render_deploy_sync`
([`event_agents.py:220`](backend/api/algo/event_agents.py#L220), registered at
[`:351`](backend/api/algo/event_agents.py#L351)), returned only `(title, body)` and had no separate
HTML slot. Its body is shared by the agents rendering `generic_notice` (see the `"render":
"generic_notice"` actions at [`event_agents.py:917-964`](backend/api/algo/event_agents.py#L917)). The
visitor-report body is genuine Telegram HTML (`<b>Visitors</b>`). Telegram's escape fallback then
double-escaped those tags. The fix matches the other HTML-capable renderers in the file: the plain
slot (ntfy and the escape fallback) now gets `_html_to_plain(body)`, and Telegram's HTML slot gets
the raw HTML. Tag-free bodies are byte-identical, so the four plain-text callers are unchanged. The five agents that render `generic_notice` are the
same five `background.py` alert sites migrated to the event-agent pipeline in "Every alert leads with
its origin" above.
- *Amended 2026-10-25 (commit `17a1c85a`) — superseded.* Telegram and ntfy are plain text now, so
  no HTML slot is built or sent anywhere in the alert path. The split described above is moot.
  See "Alert message format" below.
Invariant (historical, superseded 2026-10-25): a renderer that takes a body which may contain HTML
must split plain and HTML slots. It must never hand a tagged body to an escape-fallback path.

**Alert channels resolved by a routing tree, not hardcoded per agent; `USER_REGISTERED_AGENT` added (2026-10-24; commits `ec19aa1b`, `42156582`, `9c93dfe9`)** — Seeded event agents (`SEEDED_AGENTS`, [event_agents.py:1303](backend/api/algo/event_agents.py#L1303)) no longer carry a hardcoded `events` channel list. Each carries a `priority` token instead. [alert_routing.py](backend/api/algo/alert_routing.py) resolves the channel set in `resolve_channels()` ([:236](backend/api/algo/alert_routing.py#L236)), first match wins: (1) a `BREACH` record's dynamic `out[4]` channels, read per record; (2) `agent.events` if non-empty ([event_agents.py:609](backend/api/algo/event_agents.py#L609)); (3) a per-slug rule in `_PER_SLUG_OVERRIDES` ([:55](backend/api/algo/alert_routing.py#L55)); (4) a tier rule in `_TIER_DEFAULTS` ([:183](backend/api/algo/alert_routing.py#L183)) keyed on `priority`; (5) `CATCH_ALL_CHANNELS` ([:222](backend/api/algo/alert_routing.py#L222)), logged once per `(name, priority)`. Step 5 is the narrow catch-all for an agent that matched but fits no rule. It is not the condition-level `UNROUTED_ALERT_AGENT` path (§3.13 of ALERT_AGENTS_DESIGN.md).
- Per-slug rows are required, not incidental. The tier table has no `gate: False`. Several agents also deviate from priority in their channel set: `deploy-sync-alert` is `[]` at every tier, `market-summary` and `order-failure-alert` add email, and `agent-alert-rich` adds ntfy and email. This is the Alertmanager shape: specific rules first, general last.
- Empty `events` means "use the tree", not "silence". `if agent_events:` treats `[]` as falsy, so clearing a seeded row's channels in the UI reverts it to the per-slug default. Deactivate the agent to stop it.
- Existing databases keep their stored `events` until a `SEED_VERSION` bump rewrites them ([event_agents.py:1451-1458](backend/api/algo/event_agents.py#L1451)), so step 2 wins and the tree does not apply to them. A fresh database inserts `events=[]` ([:1446-1450](backend/api/algo/event_agents.py#L1446)), so the tree is live there from first boot. Known gap, deferred: `routes/agents.py` still serialises raw `agent.events` ([`_agent_to_info`, :610](backend/api/routes/agents.py#L610); update path, [:1702](backend/api/routes/agents.py#L1702)), so a migrated card on a fresh DB shows no channels. Both serialisers must resolve through `resolve_channels()` before the `SEED_VERSION` bump ships. *(Display half closed 2026-10-24 — see the amendment below.)*
- `USER_REGISTERED_AGENT` ([event_agents.py:1203](backend/api/algo/event_agents.py#L1203)) fixed missing instrumentation, not routing. The registration log line in [routes/auth.py:884](backend/api/routes/auth.py#L884) carried no `tags`/`alert_event`, so nothing could match it. `_registration_alert_extra()` ([auth.py:258](backend/api/routes/auth.py#L258)) supplies them. Adding `auth` to the catalogue would have made the 12 bare `routes/auth.py` calls alert-shaped via `tags_for()`'s logger-name fallback, sending them to the catch-all. Those 12 carry `_AUTH_INTERNAL_TAGS` ([auth.py:215](backend/api/routes/auth.py#L215)), an uncatalogued tag that stops the fallback.
- *Amended 2026-10-24 — routing tree ACTIVATED: `SEED_VERSION` 2→3 (commits `f8cae116`, `be0e14d0`, `e840ca0d`, `d1fd197c`).* [event_agents.py:1307](backend/api/algo/event_agents.py#L1307) bumps `SEED_VERSION` to 3, so the `update` branch now rewrites the 23 seeded `kind='event'` rows' `events` to `[]` on existing databases too, and the tree applies everywhere. Pre-flight: all 23 rows were hash-clean on prod and dev (zero `modified_skip` risk), checked by the new read-only [scripts/alert_seed_audit.py](scripts/alert_seed_audit.py). `AgentInfo.resolved_channels` ([routes/agents.py:160](backend/api/routes/agents.py#L160)) is computed by `_event_agent_resolved_channels()` ([agents.py:631](backend/api/routes/agents.py#L631)) via `alert_routing.resolve_channels()`, gated to `kind == 'event'`. It is ADDITIVE: `events` is never overwritten by the display path, because the Automation Edit button decompiles off `agent.events`, and overloading it would fork a no-op-saved agent off the tree (the `has_explicit_alert` sticky-false class). [automation/+page.svelte](<frontend/src/routes/(algo)/automation/+page.svelte>) reads `resolved_channels` for Alert via / Notify. `AgentInfo.is_builtin_event` ([agents.py:170](backend/api/routes/agents.py#L170)) drives the "Built-in" badge; `delete_agent()` ([agents.py:1599](backend/api/routes/agents.py#L1599)) refuses any seeded event agent with the same 403 as `is_system`, since a deleted seeded row would reappear on restart with its alert dark. `UNROUTED_ALERT_AGENT["events"]` ([event_agents.py:1302](backend/api/algo/event_agents.py#L1302)) is now a per-entry deep copy of `CATCH_ALL_CHANNELS`, not a hand-typed literal; it still bypasses `resolve_channels()` because `CATCH_ALL_CHANNELS` carries `gate: False`. Invariant: the display path never writes `events`, and a seeded event agent cannot be deleted.
- *Amended 2026-10-24 — `agent ai refine` write-back guards.* `_cmd_ai_refine` ([routes/agents.py:2053](backend/api/routes/agents.py#L2053)) reuses the CREATE-path `draft_agent_from_prompt()` → `_clamp_safety` ([agent_ai.py:251](backend/api/algo/agent_ai.py#L251)), which silently forced `paper`, stripped destructive actions and defaulted `lifespan_type` to `one_shot`. Four guards now apply: `trade_mode` is absent from `_AI_REFINE_WRITABLE_FIELDS` ([agents.py:721](backend/api/routes/agents.py#L721)) (commit `d1fd197c`), and a mode-switch instruction is rejected before the LLM call; tree-driven `events` stays excluded; `_clamp_safety` now records its mutations in `AgentDraft.clamp_effects` ([agent_ai.py:65](backend/api/algo/agent_ai.py#L65)), and `_ai_refine_actions_safe_to_write()` ([agents.py:902](backend/api/routes/agents.py#L902)) drops `actions` when the clamp stripped a destructive type the row already has, unless the instruction is about actions; `_ai_refine_lifespan_safe_to_write()` ([agents.py:940](backend/api/routes/agents.py#L940)) keeps a non-`one_shot` row's `lifespan_type` when only the clamp's default would have changed it. Invariant: a refine never removes a destructive action, downgrades lifespan, or flips trade mode unless the instruction names that field; the keyword checks only pick the message, the field and clamp-effect checks are the boundary.
- `Agent.priority` ([models.py:1223](backend/api/models.py#L1223)) is a nullable column with its own idempotent migration (`_migrate_agents_priority`, [database.py:1158](backend/api/database.py#L1158)). It is deliberately excluded from `compute_seed_spec_hash()` ([event_agents.py:1349](backend/api/algo/event_agents.py#L1349)). Hashing it would reclassify every already-seeded row, whose stored hash predates the column, as operator-modified.
- `log(name=..., tags=...)` stores `extra["route_name"]`/`extra["route_tags"]`, not `name`/`tags` ([actions.py:1290](backend/api/algo/actions.py#L1290)). `name` is a reserved `LogRecord` attribute, and `tags` is the structural condition-match key.
- *Amended 2026-10-25 (commit `17a1c85a`) — `deploy-sync-alert` is no longer `[]`.* It routes to the dedicated `telegram_deploy` channel only ([alert_routing.py:117-119](backend/api/algo/alert_routing.py#L117)). See the "Alert message format" entry below. `partial-gtt-alert` and `template-attach-high` gained `telegram` in the same commit.
- Invariant: channel selection lives in `alert_routing.py`, never in a detector agent's spec. Specific rules run before generic ones, and a record that resolves to nothing reaches the catch-all rather than being dropped. An alert must carry explicit `tags` and `alert_event`, so it is never alert-shaped by logger name.
- *Amended 2026-10-25 (commits `7d2b51fc`, `291ae960`) — Alert-channel collapse: 23 seeded event
  agents down to 10. This revisits and partially reverses a 2026-10 6-agent council rejection of a
  flatter version of this same idea (`docs/specs/ALERT_AGENTS_DESIGN.md` §3.9, now marked "PARTIALLY
  REVERSED, see §3.22"). 16 of the 23 `kind='event'` seeded agents (chase-cancel, partial-gtt,
  template-attach ×2, order-failure, order-correction, template-guard, template-attach-fail,
  mcp-ping, gtt-asymmetric, oco-double-fire, ticker-recovered, ticker-degraded, gtt-not-accepted,
  fill, user-registered) are retired from `SEEDED_AGENTS` — deactivated via
  `_retire_collapsed_event_agents()`, never deleted — and replaced by 3 generic channel agents
  (`alert-channel-telegram`/`-ntfy`/`-email`) plus `event_agents.ALERT_EVENT_DISPATCH`, a table
  **derived, never retyped**, from those 16 specs' own `conditions`/`actions` and
  `alert_routing._PER_SLUG_OVERRIDES` rows. Two of the original rejection's four objections
  (one-renderer-per-row, condition duplication) are answered structurally by this derived table;
  the third (per-event Automation-page toggle loss) is an accepted, documented tradeoff — no
  `kind='event'` agent has ever had per-type `trigger_count`/`last_triggered_at` observability to
  lose on top of it (those columns are written only by the cycle-agent path in `agent_engine.py`);
  the fourth (no soak time) no longer applies given the stable period since those four fixes
  landed. `alert_event` survives unchanged in every dispatched record's `extra` dict, so Alerts-tab
  / log-history filtering by type is unaffected even though the Automation page's per-agent toggle
  granularity is not. 7 agents stay dedicated and byte-identical to before: `agent-alert-rich`
  (breach multiplexer), `cycle-breach-alert` (breach render path), `unrouted-alert-catch-all`
  (deliberately bypasses `resolve_channels()` as a last-resort safety net), `deploy-sync-alert` and
  `market-summary` (dedicated `telegram_deploy`/`telegram_info` channels, not generic), `error-alert`
  (a blanket tag-only catch-all with no `alert_event` and its own repeat-gate), and
  `visitor-report-alert` (also dedicated `telegram_info`, found during implementation — it does not
  fit the collapse the way the other 4 `generic_notice` agents do). `SEED_VERSION` 4→5; pre-flight
  (`scripts/alert_seed_audit.py` run against prod and dev live) confirmed all 23 pre-collapse rows
  were hash-clean — zero `modified_skip` risk — before this shipped. `_dispatch_agent()` branches to
  a new `_dispatch_channel_agent()` for any agent whose render action names the internal sentinel
  `"event_table"` and carries a `channel_key`; it sends only that one channel via a new
  `only_channel` param on `_dispatch_record_channels()`, applied AFTER the forced-email
  action-required gate, so a forced email never duplicates across the 3 channel agents for one
  record and never drops to zero either. Full design: `docs/specs/ALERT_AGENTS_DESIGN.md` §3.22.
  Landed on `workshop` only as of this entry — not yet merged to `dev`/`main`, no deploy attempted.
  Invariant: a channel agent never sends a channel absent from its matched event's own verbatim
  list, and `ALERT_EVENT_DISPATCH` is read, never hand-retyped, from the retired specs' own data.

**Alert message format — plain text everywhere, origin line first, dedicated deploy channel, forced email for action-required, Alerts tab merges `alert`-tagged log rows (2026-10-25; commit `17a1c85a`; test-only follow-up `368c98a8`)** — Three standardisation changes and one surface change, all in the alert pipeline.
- **Plain text, no HTML in Telegram or ntfy.** `alert_utils._send_telegram()` ([alert_utils.py:218](backend/shared/helpers/alert_utils.py#L218)) sends no `parse_mode` unless a caller passes one explicitly, and nothing does (payload at [:264-266](backend/shared/helpers/alert_utils.py#L264)). `_send_telegram_info()` ([:276](backend/shared/helpers/alert_utils.py#L276)) never sets one. `dispatch_payload()` builds `telegram_msg` without `<b>`/`<code>` and without `html.escape()` ([:682-685](backend/shared/helpers/alert_utils.py#L682)). The event-path senders `_send_telegram_html` ([event_agents.py:452](backend/api/algo/event_agents.py#L452)) and `_send_telegram_info_html` ([:501](backend/api/algo/event_agents.py#L501)) are now bare pass-throughs. Their `_html` suffix is kept for name stability only. `_html_to_plain()` ([alert_utils.py:319](backend/shared/helpers/alert_utils.py#L319)) has no production caller left. Email keeps its HTML body (`_email_message`).
- **`webhook/notify_deploy.py` is Telegram-only.** Its ntfy send is gone, the email path stays retired ([notify_deploy.py:106-110](webhook/notify_deploy.py#L106)), and no `parse_mode` is sent. Its `tg_body` is plain text ([:77-87](webhook/notify_deploy.py#L77)).
- **Origin line first.** `format_notification_header()` ([alert_utils.py:587](backend/shared/helpers/alert_utils.py#L587)) emits `Agent: {origin}` as line 1, then the dual-tz timestamp. The event renderers that build their own text lead with it: `_render_chase_cancel` ([event_agents.py:94](backend/api/algo/event_agents.py#L94)), `_render_partial_gtt` ([:113](backend/api/algo/event_agents.py#L113)), `_render_template_attach` ([:144](backend/api/algo/event_agents.py#L144)), `_render_order_correction` ([:268](backend/api/algo/event_agents.py#L268)), `_render_gtt_not_accepted` ([:380](backend/api/algo/event_agents.py#L380)) and `_render_error` ([:411](backend/api/algo/event_agents.py#L411)). `_render_order_failure` gets its header from `order_failure_messages()` ([alert_utils.py:1085](backend/shared/helpers/alert_utils.py#L1085), header call at [:1126](backend/shared/helpers/alert_utils.py#L1126)). MCP pings get theirs at the single chokepoint `lab.py` ([routes/lab.py:1141-1143](backend/api/routes/lab.py#L1141)).
  - **Deliberate exception — agent-less summaries.** `dispatch_payload()` adds the agent line only when both `agent_name` and `agent_id` are given ([alert_utils.py:666-668](backend/shared/helpers/alert_utils.py#L666)). Open and close summaries therefore lead with the title line `… — <dual-tz>` and carry no origin line ([:682-685](backend/shared/helpers/alert_utils.py#L682)).
  - **Known gap — `generic_notice` producers do not lead with origin.** `_render_deploy_sync` ([event_agents.py:293](backend/api/algo/event_agents.py#L293)) is a pass-through of `extra["body"]`. Its five `background.py` producers build a bare `msg` with no `Agent:` line: gtt-asymmetric ([background.py:5290-5300](backend/api/background.py#L5290)), oco-double-fire, ticker-recovered, ticker-degraded, and visitor-report ([:7232-7246](backend/api/background.py#L7232)). Deploy-sync also builds its own `message` ([:8999-9001](backend/api/background.py#L8999)). None of these carry an origin line. This is stated here, not fixed.
- **Visitor report is plain text.** The Telegram body is `_summary_text()` ([backend/scripts/visitor_report.py:770](backend/scripts/visitor_report.py#L770)), not the HTML `summary_for_telegram()` builder ([:780](backend/scripts/visitor_report.py#L780)). The 2026-10-23 `generic_notice` HTML leak entry above is therefore moot. Its HTML split no longer exists.
- **`telegram_deploy` channel.** `CHANNELS["telegram_deploy"]` ([event_agents.py:563](backend/api/algo/event_agents.py#L563)) posts through `_send_telegram_deploy_html()` ([:508-523](backend/api/algo/event_agents.py#L508)). It reads secrets `telegram_bot_token_deploy`/`telegram_chat_id_deploy` and falls back to `telegram_bot_token`/`telegram_chat_id`. That is the same credential pair and fallback `notify_deploy.py` already used ([notify_deploy.py:90-91](webhook/notify_deploy.py#L90)). The only rule that names it is `deploy-sync-alert` ([alert_routing.py:117-119](backend/api/algo/alert_routing.py#L117)), with `gate: False`. It is explicitly not the generic `telegram` channel, not ntfy and not email.
- **Two rows gained `telegram`; one did not.** `partial-gtt-alert` ([alert_routing.py:70-73](backend/api/algo/alert_routing.py#L70)) and `template-attach-high` ([:80-83](backend/api/algo/alert_routing.py#L80)) now list `telegram` ahead of ntfy. `gtt-not-accepted-alert` ([:153-155](backend/api/algo/alert_routing.py#L153)) is still ntfy-only.
- **Forced email for action-required records.** `_apply_action_required_email_gate()` ([event_agents.py:854](backend/api/algo/event_agents.py#L854)) mirrors the ntfy gate at [:823](backend/api/algo/event_agents.py#L823). It runs once per record in `_dispatch_record_channels()` ([:685](backend/api/algo/event_agents.py#L685)), before the per-channel ntfy gate. When `extra["action_required"] is True` and the channel list has no `email` entry, it appends `{"channel": "email", "enabled": True, "gate": False}`. It never duplicates an existing entry, and `False` or absent leaves the list untouched.
- **Alerts tab merges `alert`-tagged `LogEvent` rows.** `GET /api/admin/alerts/history` ([routes/alerts.py:222](backend/api/routes/alerts.py#L222)) also selects `LogEvent` rows whose `tags` overlap `["alert"]` ([`_build_log_alert_stmt`, :161](backend/api/routes/alerts.py#L161)). It maps each row to the `AlertEvent` shape via `_log_event_to_alert()` ([:194](backend/api/routes/alerts.py#L194)), so the frontend needs no change. The merge runs only when no `agent_id`, `agent_slug` or `event_type` filter is set ([:297](backend/api/routes/alerts.py#L297)). `sim_mode` is filtered in Python from JSONB `extra`. The two lists are sorted by the real `ts`, never the display string, and truncated to `limit` ([:315](backend/api/routes/alerts.py#L315)).
  - **Id namespacing.** A merged row gets `id = -event.id` ([:201](backend/api/routes/alerts.py#L201)) and `agent_id = 0` ([`_LOG_ALERT_AGENT_ID_SENTINEL`, :50](backend/api/routes/alerts.py#L50)). Negative ids keep `LogEvent`-sourced rows out of the positive `AgentEvent` id space. Neither the code nor the commit message records this reason. The rationale here is the observable effect, not a stated intent.
  - **Which records are tagged.** The additive `alert` catalog token ([agent_grammar.yaml:1534](backend/config/grammars/agent_grammar.yaml#L1534)) is on alert-worthy lines in `fill_notify.py` ([:101](backend/api/algo/fill_notify.py#L101)), `order_poll.py` ([:217](backend/api/algo/order_poll.py#L217)), `template_attach.py`, `chase.py` ([:2769](backend/api/algo/chase.py#L2769)), `orders_place.py` ([:1174](backend/api/routes/orders_place.py#L1174)), `lab.py` ([:1144](backend/api/routes/lab.py#L1144)) and `alert_utils.py` ([:1198](backend/shared/helpers/alert_utils.py#L1198)). `rich_alert` and `breach` are deliberately not tagged. Their firings already exist as `AgentEvent` rows, and tagging them would double each one.
  - **Fixed same day, commit `492fd988` — this was NOT just an Alerts-tab visibility gap, it was zero channel delivery.** `alert_utils` is in `_SKIP_PREFIXES` ([log_store.py:29](backend/shared/helpers/log_store.py#L29)). `LogStoreHandler.emit()` returns before queueing when `record.name.startswith(_SKIP_PREFIXES)` ([:217](backend/shared/helpers/log_store.py#L217)). `_send_order_failure_messages()` ([alert_utils.py:1193](backend/shared/helpers/alert_utils.py#L1193)) and `send_summary()` ([:918](backend/shared/helpers/alert_utils.py#L918)) both logged through `alert_utils.py`'s own module `logger` — so their records never reached `log_events`, never reached `event_agents.dispatch()`, and `ORDER_FAILURE_AGENT`/the summary agent could never fire. **Confirmed via git history: zero Telegram/ntfy/email delivery for order failures and market-open/close summaries for ~19 days**, since `_SKIP_PREFIXES` gained this entry on 2026-10-06 (`54860caa`) — three months after `5f0a6d5c` (2026-07-23) had already migrated both functions off direct `_send_telegram()`/`send_email()` calls onto this log-and-dispatch model. The two changes, three months apart, silently combined into this gap; neither commit's own test suite caught it because tests mock the dispatch layer directly rather than exercising the real skip-list end-to-end. Fix: both functions now log through a new `_DISPATCH_LOGGER` ([alert_utils.py:93](backend/shared/helpers/alert_utils.py#L93), name `backend.api.algo.events` — the exact same non-skip-listed logger name `event_agents.py`'s own `_EVENTS_LOGGER` already uses for this identical purpose) instead of the skip-listed `logger`. The skip-list itself is untouched — it still correctly protects this module's genuine internal diagnostics (Redis/Telegram/email send status). Regression test in `test_log_store.py::test_order_failure_and_summary_records_reach_the_queue` attaches a real `LogStoreHandler` to the real logger and proves the record survives `emit()` — not a mocked call. Invariant: any log call meant to trigger event-agent dispatch must use a logger name outside `_SKIP_PREFIXES`; `_DISPATCH_LOGGER`/`_EVENTS_LOGGER` is that one, shared convention — never log-and-dispatch through a module's own skip-listed `logger`.
- Test-only commit `368c98a8` adds [backend/tests/test_alert_agent_dsl_examples.py](backend/tests/test_alert_agent_dsl_examples.py). It is illustrative DSL-to-dispatch fixtures and changes no runtime code.
- Invariant: an outbound Telegram or ntfy message is plain text with no markup. The origin line is line 1 of every event-renderer message, with the agent-less summary as the one named exception. A deploy-sync notice reaches only `telegram_deploy`. An action-required record always reaches email. An Alerts-tab row never collides with an `AgentEvent` id.

**MCX spot resolution — a transient 4a quote failure never swaps the contract (2026-10-25; commit `21f74618`)** — `_resolve_commodity_spot()` ([`options_helpers.py:191`](backend/api/routes/options_helpers.py#L191)) resolves the MCX future in priority order: the option_symbol contract (`lookup_future_for_option`, [:209](backend/api/routes/options_helpers.py#L209)), then `expiry_hint` ([:213](backend/api/routes/options_helpers.py#L213)), then the front-month ([:215](backend/api/routes/options_helpers.py#L215)). When the option_symbol contract resolved but its 4a price quote failed transiently (broker or thread-pool timeout), step 4b used to rebuild the spot from `expiry_hint` and quote a DIFFERENT contract. Reproduced under forced 4a timeout: the anchor flipped from the option_symbol month to the expiry_hint month. The guard at [:240](backend/api/routes/options_helpers.py#L240) now skips 4b whenever `resolved_via_option_symbol` is set. A 4a failure then returns `(None, "none", …)`, and the caller falls through to its last-resort spot source. Invariant: a transient quote failure changes the price's availability, never which contract anchors it.

**F&O order qty convention** — API now accepts LOTS as input for instruments with 

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

  **Kind classification for cash/avail_margin metrics must consider the leaf's operator (2026-09-28, commit 36aba654)** — Schedule-only sentinel conditions (`market-open-nse` uses `avail_margin >= -999999999` purely to gate its `fire_at_time` schedule; its former sibling `market-preclose-mcx` did the same until it was retired 2026-10-28 in favour of `market-preclose-mcx-v2`, which is condition-based on `minutes_until_mcx_close` and carries no sentinel) were being mislabeled as real threshold breaches by the alert display layer. Root cause: `_v2_derive_kind()` in [`backend/api/algo/agent_engine.py`](backend/api/algo/agent_engine.py) classified based on metric name alone (`avail_margin`, `cash`), ignoring the operator. Fixed: (1) `_v2_derive_kind(metric, op)` now takes the operator; only `<`/`<=` operators classify as `negative_cash`/`negative_margin` floor breaches. (2) `_v2_format_threshold()` belt-and-suspenders guard: thresholds with `abs() >= 1e8` render as `"n/a"` instead, defending against future similar sentinels. (3) `_v2_send_rich_alert()` renders scheduled info-tier agents (`fire_at_time` set, `tier` in `['info', 'low']`) as `"{agent.name} — Scheduled — {fire_at_time} IST"` in Telegram/email, replacing the kind/threshold table to clarify the alert's purpose. `loss-funds-negative` (tier=critical, uses `op: "<"`) and genuinely alert-firing agents unaffected — verified via golden byte-for-byte render tests. Invariant: alert kind classification must never treat a schedule-only sentinel as a real threshold breach.

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

**Closed-window broker calls gated; nightly upstream WebSocket disconnect/reconnect in conn_service
(2026-10-28; one coordinated change, four parts)** — Four background loops made real broker calls
all night with no closed-window gate: `_task_order_poll`
([order_poll.py:278](backend/api/algo/order_poll.py#L278), 5 min), `_task_trail_stop`
([background.py:5909](backend/api/background.py#L5909), 30s), `_task_oco_pair_watcher`
([background.py:6446](backend/api/background.py#L6446), 5–15s) and `_task_funds_offhours`
([background.py:8046](backend/api/background.py#L8046), 30 min). The fix has four parts.
- **Closed-hours gate.** `_resolve_market_status_today()`
  ([background.py:311](backend/api/background.py#L311)) resolves each segment once per IST day into
  `_MARKET_STATUS_CACHE` ([:207](backend/api/background.py#L207)), keyed by
  `_MARKET_STATUS_CACHE_DATE`. It runs from `_hr_run_once()`
  ([:5196](backend/api/background.py#L5196)) right after `warm_special_sessions_cache()`, and lazily
  on the first call each day, so a process that starts after 05:30 still resolves.
  `task_markers.market_status_resolved_date` is a log-dedup marker only, not the gate. Each loop
  calls `is_any_segment_open_cached()` ([:418](backend/api/background.py#L418)) before its broker
  call: `_task_trail_stop` ([:5956](backend/api/background.py#L5956)),
  `_task_oco_pair_watcher` ([:6510](backend/api/background.py#L6510)), and `_task_order_poll`
  ([order_poll.py:314](backend/api/algo/order_poll.py#L314)), which runs after `is_engine_idle()`.
  The check FAILS OPEN. A cold or stale cache returns True, so a missed resolve costs one extra
  poll, never a missed stop. `is_segment_open_cached()` ([:369](backend/api/background.py#L369)) is
  the per-segment or per-exchange variant of the same check.
- **Funds reversal.** `_task_funds_offhours` ([background.py:8046](backend/api/background.py#L8046))
  no longer polls broker in the closed window. This reverses the earlier "poll because closed"
  design. Funds now freeze at the last value fetched before 23:31, which is the "Staleness
  indicator freeze rule" applied to funds. Nothing regressed: the overnight refresh fed a cache that
  the closed-hours route never read while closed.
- **Idle state at 23:31.** `_snapshot_stop_ticker()`
  ([background.py:3315](backend/api/background.py#L3315)) calls `_ticker_calendar_mark(...,
  connected=False, idle=True, reason="ticker stopped for the night at 23:31 IST")` in a `finally`
  ([:3363](backend/api/background.py#L3363)), in both conn-service and direct modes. See the
  amendment under "Ticker idle badge on non-trading days".
- **Conn-service WebSocket cycle, DB-calendar driven.** The upstream KiteTicker inside
  conn_service's own process is disconnected when the MCX quiet window opens (MCX close + 1 min,
  default 23:31 IST) and reconnected when the next session opens (default 08:00 IST). Neither time
  is hardcoded. Both come from `exchange_schedule` for the current IST date, via
  `resolve_mcx_gate_bounds(today)` ([calendar_gate.py:167](backend/brokers/service/calendar_gate.py#L167)).
  A date-specific override row, such as a DST-shifted MCX close, moves the real firing time. The
  table choice is deliberate. `_ds_tick_mcx_stop()` in `background.py` already decides "when does MCX
  close today" from `exchange_schedule` through `exchange_clock._effective_gate_rows("MCX")`. Reading
  the same table keeps conn_service agreeing with the main API process. `market_special_sessions`
  models whether a session exists at all, not its boundaries, so using it could split the two
  processes. The read is `_read_exchange_schedule_rows_async()`
  ([broker_apis.py:3237](backend/brokers/broker_apis.py#L3237)), a plain `async with async_session()`
  read. It deliberately does not use `exchange_clock`'s in-process cache, which only the main API
  process populates.
  - **Fallback.** A DB failure, or no usable row, returns the default 08:00 / 23:30 bounds. These are
    byte-identical to the old hardcoded values. A failure is never cached, so a transient outage
    cannot poison the rest of the day. A successful resolution is cached per IST date, so the DB is
    read at most once per day, not once per 30s tick.
  - **Where it is used.** `ticker_quiet_window_now()`
    ([calendar_gate.py:257](backend/brokers/service/calendar_gate.py#L257)) composes the bounds into
    the close+1 min / open check. `app.py` keeps the old name `_ticker_quiet_window_now()`
    ([service/app.py:794](backend/brokers/service/app.py#L794)) as an async delegator, so existing
    call sites and tests are unchanged. `_task_daily_ticker_cycle()`
    ([service/app.py:964](backend/brokers/service/app.py#L964)) is spawned from `_start_kite_ticker()`
    ([:295](backend/brokers/service/app.py#L295)), next to `_ticker_watchdog()`. Its disconnect
    branch is driven by `ticker_quiet_window_now()`. Its restart window runs from the resolved open
    time for five minutes, once per day. The 08:00 action `_restart_ticker_for_new_day()`
    ([:872](backend/brokers/service/app.py#L872)) reuses the token `_task_prewarm_tokens()` minted in
    the 05:45–05:59 window, reattaches the tick buffer and calls `ticker.restart()`.
  - **Gates.** Phase 1 of `_ticker_watchdog` ([:565](backend/brokers/service/app.py#L565)) and the
    inline boot connect ([:262](backend/brokers/service/app.py#L262)) both `await` the quiet-window
    check. `conn_should_attempt_connect()` is a day-level gate only, and would otherwise reconnect
    right after the disconnect. Log lines name no clock time, since an override day shifts the
    real time.
  - The tests that pin this are in
    `backend/tests/broker/test_ticker_daily_cycle_restart_resiliency.py`.
    `test_non_zero_width_mcx_override_shifts_the_window` covers the override shift, and
    `test_db_error_falls_back_to_default_never_earlier` covers the fallback. The file's parametrised
    cases bring the total to 25.
- **The plan's first assumption was unsafe.** The plan called `get_ticker().stop()` and
  `.restart()`. `stop()` calls Twisted's process-wide `reactor.stop()`, which sets `_reactor_dead`.
  `restart()` then returned at its own `_reactor_dead` guard, so it could never reconnect. Shipping
  the literal plan would have crash-looped conn_service every night: reactor dead, SIGTERM,
  systemd restart, forced Dhan/Groww re-logins, and two CRITICAL alerts. What shipped is
  `TickerManager.disconnect_keep_reactor()` ([kite_ticker.py:758](backend/brokers/kite_ticker.py#L758)).
  It runs the same `stop_retry()` / `close()` / drain / state reset as `stop()`, but never touches
  the reactor. It keeps `_tick_map`, `_subscribed`, the token and symbol maps and
  `_virtual_root_aliases`, because this is a same-account daily cycle, not a failover. `restart()`
  ([kite_ticker.py:832](backend/brokers/kite_ticker.py#L832)) now calls it. `start()` hands its
  connect to `reactor.callFromThread()` when the reactor is already running.
- This is separate from the local `MmapTickReader` forwarder stop/restart described in "Conn-service
  tick forwarder killed nightly, never revived" above. That path is unchanged.
- Invariant: a closed-window broker call is skipped by a cached clock-plus-calendar check that
  fails open. A scheduled nightly action never calls `reactor.stop()`. The quiet window is computed
  from the clock plus the DB calendar (`exchange_schedule`), never stored and never hardcoded.

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
  - *Residual, 2026-10-23 (see commit `a2f21798`, 3A):* the live-fetch filter above approximated an
    MCX future's expiry as the last Thursday of its month. It now reads the real per-contract expiry
    via `_mcx_future_real_expiry()`, which is documented under "MCX base-metal futures dropped from
    live positions" in Critical math guards. `expiry_if_closed_on_own_expiry_day()`, the
    closed-hours sweep classifier, still uses the approximation. Out of scope for that fix; it is an
    open item.

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

**ChaseCard header-drawer column floors on mobile (2026-10-23; commit `bcb9bad6`, amends the entry
above)** — The age-column floor above was the only column with a locked minimum. In the non-compact
grid (`compact={false}`, the header drawer and `/orders` mount), `side`, `qty` and `att` still
collapsed to a single ellipsized character on a ~360px panel. A measurement pass also showed `limit`
and `mode` clipping, which the original finding had not named. Every track in the non-compact
`.cc-row` now has a content-derived floor
([`ChaseCard.svelte:729-744`](frontend/src/lib/order/ChaseCard.svelte#L729)). When the container is
narrower than the sum of the floors, the existing `.cc-grid` `overflow-x: auto` takes over and the row
scrolls. Nothing changes above ~460px. The compact `.cc-row` override
([`:754-760`](frontend/src/lib/order/ChaseCard.svelte#L754)) keeps its `minmax(0, …)` tracks, because
compact cards are not in the drawer. Invariant: a grid track may not squeeze its content past
legibility. Overflow scrolls instead.

**Chase hidden (not greyed) while Draft is on; one submit label (2026-10-23; commit `75685b7b`)** —
Operator report: "when submitting draft order, chase is getting invoked." A live DB query on prod
and dev found no `mode='draft'` `AlgoOrder` row ever created. Both draft-submit paths
(`OrderTicket.svelte`, `SymbolPanel.svelte:submitBasket()`) return before any chase or broker call.
Nothing was invoked. The confusion came from the CHASE toggle and picker staying rendered, greyed,
while Draft was on. They are now hidden entirely (`_chaseEnabled && !_sharedDraftMode`,
[`SymbolPanel.svelte:2965`](frontend/src/lib/SymbolPanel.svelte#L2965)). BRACKET keeps its
grey-not-hide treatment. The shared `.oes-common-submit` button's "Add to Payoff" label is gone. A
single staged leg gets the standard `formatSubmitLabel` like every other mode. A multi-leg basket
keeps the plain "Submit".

**Chain ITM wash spans the strike column; basket-leg pill contrast (2026-10-23; commit `a0e2e3e0`)**
— Cosmetic. The ITM call and put background wash stopped at the CE/PE cell boundary. It now extends
into the shared strike column: ITM calls spread right toward centre, ITM puts spread left toward
centre. The Chain-tab basket-leg pill in `SymbolPanel.svelte` moves from the faint rest-state alpha
(-14) to the existing -22 emphasis tier. No new colour is introduced.

**Chain ITM wash hue swapped — CE is now orange, PE is now green (2026-10-25, commit `14e64d68`,
explicit operator override of the entry above's predecessor)** — A 2026-09-30 fix had deliberately
aligned the ITM cell washes to the header colors (CE=green, PE=red). This reverses that: ITM calls
(`.chain-row-itm-call .chain-td-ce`/`.chain-td-strike`) now wash orange (`--algo-orange`, rgb
251,146,60); ITM puts (`.chain-row-itm-put .chain-td-pe`/`.chain-td-strike`) now wash green
(`--algo-green`/`--c-long`, rgb 74,222,128) — both reused, no new tokens. The weak/OTM-side
cross-tints swap the same way at their existing 0.015 alpha tier. This is a deliberate, confirmed
reversal of the prior documented decision, not a bug fix — do not "correct" it back without a new
explicit operator instruction. Same commit added a top edge to the ATM row's marker
(`frontend/src/lib/order/OptionChainTab.svelte`), migrated from literal `border-top`/`border-bottom`
to `box-shadow: inset 0 1px 0 …, inset 0 -1px 0 …` — a literal border would conflict with the row
above's own `border-bottom` under `border-collapse: collapse` (resolved by cell position, not by
visual strength, so the marker could silently lose in some browsers), and the old bottom-only
`border-bottom` marker was already being silently erased on desktop by an unrelated `@media
(min-width: 640px)` rule that strips `.chain-row > td { border-bottom: none }` for row-divider
removal. `box-shadow` isn't part of the table border model, so neither conflict applies; it renders
identically on mobile and desktop. `.chain-row-active.chain-row-atm`'s compound rule now carries both
edges explicitly, since `.chain-row-active`'s own equal-specificity `box-shadow` would otherwise fully
replace (not merge with) the ATM row's marker when a row is both ATM and active.

**Mobile page header never wraps; footer top-aligns; status pills stay whole (2026-10-23; commits
`b8e0ae0f`, `7d281276`)** — Three mobile overflow defects, all on the algo shell in
[`+layout.svelte`](<frontend/src/routes/(algo)/+layout.svelte>).
- **`.page-header` wrapped on narrow widths, on all 24 algo pages.** The header is
  `position: fixed`, and `.algo-content`'s padding-top reserves exactly one row. A wrapped second row
  therefore covered the page's first card. `AlgoTimestamp`'s shrink-and-scroll never ran, because a
  wrapping parent breaks the line using full max-content width. Mobile `.page-header-actions` also
  had `flex-wrap: wrap; flex-shrink: 1`, the opposite of the desktop rule.
  Fix: `.page-header` is `flex-wrap: nowrap` ([`+layout.svelte:2315`](<frontend/src/routes/(algo)/+layout.svelte#L2315>)).
  `.ats-root` is the only child that shrinks and scrolls. The button group is `flex-shrink: 0` on
  mobile too, so it stays pinned at every width.
- **Footer page-name centred against single-line brand text.** When the page name wrapped to two
  lines, it was vertically centred against the one-line brand. `.algo-footer` now sets
  `align-items: flex-start` at ≤640px ([`+layout.svelte:2461`](<frontend/src/routes/(algo)/+layout.svelte#L2461>)).
  Regression test added in `7d281276`.
- **Status pills and chips clipped at 360px.** `OrderCard`'s `.algo-status-pill` is
  `flex-shrink-0` ([`OrderCard.svelte:229`](frontend/src/lib/order/OrderCard.svelte#L229)), so the
  symbol side gives way instead of pushing the pill off-screen. `OrderBook`'s status-chip row
  ([`OrderBook.svelte:1104-1114`](frontend/src/lib/OrderBook.svelte#L1104)) scrolls horizontally
  rather than ellipsis-clipping "Rejected/Cancelled".
- Invariant: header overflow is absorbed by a shrinking child (AlgoTimestamp's scroll), never by
  wrapping the row. Button groups are always pinned and visible.

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

*Amended 2026-10-23 (commits `fa70a893`, with its `chase.py` half in `6ba6936e`):* the Kill route (`kill_chase`,
[`orders.py:1988`](backend/api/routes/orders.py#L1988), which takes `algo_order_id`) used to resolve
its marker from a `broker_order_id` read at click time. If chase cancelled-and-replaced in that window,
the cancel hit a dead id and the row was wrongly stamped `CANCEL_FAILED`. The Kill route now marks the
algo-order id first, via `mark_order_killed()` ([`chase.py:687`](backend/api/algo/chase.py#L687)),
which survives a broker-id swap. The `mark_killed()` / `is_killed()` contract above still applies to
the other cancel paths.

**Chase live attempts, broker cancel throttling, superseded-row dedup, and header chase popup
(2026-10-23; commits `f008db13`, `ad4b7b0a`, `0033c0a0`, `63580cff`, `60ef55b0`, `b8e0ae0f`)** —
Four defects on the chase cancel-and-replace path, one entry because they share that cycle.
- **`AlgoOrder.attempts` was written only at the terminal transition.** An in-flight chase showed
  `attempts=0` for its whole life. `_ch_apply_broker_sync_fields()`
  ([`chase.py:1137`](backend/api/algo/chase.py#L1137), write at
  [`:1166-1167`](backend/api/algo/chase.py#L1166)) now writes `row.attempts` on every
  cancel-and-replace sync, in the same call that already writes `last_attempt_at`,
  `next_attempt_at` and `current_limit`. Retry and cancel-confirmation logic is unchanged.
- **Kite and Groww `cancel_order` were unthrottled.** Chase calls `cancel_order` on every re-quote,
  and `place_order` was already rate-limited. Kite's `cancel_order`
  ([`kite.py:504`](backend/brokers/adapters/kite.py#L504)) now calls
  `_KITE_RATE_LIMITER.throttle("orders")` and wraps the SDK call in `_kite_exc`
  ([`:505-508`](backend/brokers/adapters/kite.py#L505)). Groww's
  ([`groww.py:1411`](backend/brokers/adapters/groww.py#L1411), inside `cancel_order` at
  [`:1399`](backend/brokers/adapters/groww.py#L1399)) calls `_GROWW_RATE_LIMITER.throttle("orders")`.
  Dhan is unchanged: its SDK proxy already throttles every call by bucket.
- **The superseded broker order rendered as a second "open" card.** During cancel-and-replace the
  old `broker_order_id` is no longer referenced by any `AlgoOrder` row, so until the broker's book
  caught up it appeared as an independent card next to the replacement. `OrderBook.svelte` keeps
  `_supersededBrokerIds` ([`:92-104`](frontend/src/lib/OrderBook.svelte#L92)) with a
  `_SUPERSEDED_GRACE_MS = 25_000` window, pruned each poll. A broker-only row matching a
  superseded id is suppressed ([`:255-267`](frontend/src/lib/OrderBook.svelte#L255)), and the new
  card shows a "repricing — previous order cancelling" note. Frontend only.
- **The header chase badge showed the event-history log, not the live view.**
  `OrderTimelineDrawer` has no countdown and no Kill. The popup now mounts
  `<ChaseCard compact={false}>` ([`+layout.svelte:1611`](<frontend/src/routes/(algo)/+layout.svelte#L1611>)),
  so the countdown and the LIVE/mode pill match "within order". Non-compact keeps the mode pill
  visible in an aggregated real-money view. The drawer shell owns the empty and error states
  (ChaseCard self-hides at zero chases), and the header count is bound to ChaseCard's `activeCount`
  so the number and the popup share one poll. `OrderBook`'s per-order "view timeline" still opens
  the event-log drawer, since terminal orders are not in ChaseCard.
- Invariant: a chase's `attempts` reflects the in-flight cancel-and-replace, and a superseded
  broker order must never render as an independent open order while its own cancel is pending.
  Any operator cancel path must also call `mark_killed()` (see the entry above).

**Chase robustness round 2 — fail-closed finalization, stuck/timeout detection, header drawer,
ChaseCard remount-loop fix (2026-10-23; commits `1d2e369e`, `fc77a8f8`, `7d68b8d4`, `a3ade9db`,
`06c4ee45`, `23651dd4`)** — Deepens the "Chase live attempts" entry above. Grouped by surface.
- **Finalization never guesses broker fate.** Every chase abort that leaves a broker order's fate
  unknown writes `CANCEL_FAILED`, not `REJECTED`. `REJECTED` is final, and every writer refuses to
  overwrite it (see `ALGO_ORDER_FINAL_STATUSES` under Order-lifecycle races). A later real fill on
  a genuinely-unknown-fate order was therefore permanently unrecordable. No abort path may leave a
  row silently `OPEN` with no trace.
- **Broker-call ceiling and ambiguous-placement abort.** `_run()` in
  [`chase.py`](backend/api/algo/chase.py) wraps each broker call in a 30s `asyncio.wait_for`
  ceiling. A `_place_order` timeout takes a dedicated non-retrying ambiguous-abort path, not the
  generic retry, so a timed-out placement cannot become a second live order. A wall-clock stuck
  detector aborts and alerts a chase making no real progress. Its ceiling is always below the
  natural exhaustion budget.
- **Canonical order-truth accessor.** `get_broker_order_truth()` and `apply_order_truth()`
  ([`orders_helpers.py:730`](backend/api/routes/orders_helpers.py#L730),
  [`:762`](backend/api/routes/orders_helpers.py#L762)) are the one way to ask what the broker says
  about an order. They are safe on a missing broker response (never guess a status), row-locked,
  and write the FIFO ledger on a reconciled fill. Chase's in-loop check and its
  post-ambiguous-abort backstop both call them. Every other reconcile path is in the Order-poll
  entry below.
- **ntfy is a binary action-required gate, decided at dispatch.** Every outcome needing operator
  action reaches ntfy as urgent. Fills and other FYI outcomes go to Telegram, email, or log, never
  ntfy. Non-recoverable errors always alert. Recoverable errors still retry, with no alert
  (regression-tested). Alert suppression (market-closed, 10-minute dedup) is bypassed for
  abandonment, exhaustion and stuck alerts, since reconciliation matters regardless of market hours.
- **`AlgoOrder.superseded_broker_order_ids_json`.** New column with a migration. Written at
  cancel-confirm time in `chase.py`, before the replacement is placed
  ([`chase.py:886`](backend/api/algo/chase.py#L886)). The frontend `_supersededBrokerIds` window
  above inferred the same thing from poll timing. This writes it authoritatively, closing the
  duplicate-card race at source.
- **Caller consistency.** Close-position surfaces a FAILED/HELD `ChaseResult` instead of discarding
  it. The ticket path no longer double-alerts on a recoverable error that then succeeds.
- **Countdown status gate.** ChaseCard's countdown and pulse dot were gated on
  `status === 'active'`. `/api/orders/chases/active` never returns that value; it returns only
  `OPEN` and `CANCEL_FAILED`. The countdown showed "—" in production on every surface, while
  Playwright passed because the fixtures mocked the wrong status. The gate is now `OPEN`.
  `CANCEL_FAILED` is excluded on purpose: chase has stopped scheduling re-quotes, and a countdown
  would mislead. The "limit moved" chip's off-by-one is fixed too. `attempts` counts placements
  from 1, not modifications.
- **OrderBook chase-in-flight chip and stale-broker-id Cancel guard.**
  [`OrderBook.svelte`](frontend/src/lib/OrderBook.svelte) and
  [`LogPanel.svelte`](frontend/src/lib/LogPanel.svelte) merged rows so the broker row won over the
  algo row. Broker rows carry no `attempts` / `current_limit` / `next_attempt_at`, so the chip and
  OrderCard's `chase:#N` marker only fired for paper/sim. The merge now copies those fields from
  the algo row. Cancel re-fetches the algo row first, and refuses with a toast and no broker call
  if the `broker_order_id` has changed since the last poll.
- **Header chase drawer, relocated.** The standalone `.chase-chip` is gone. Right-click (desktop)
  or long-press (mobile) on the header Order button opens the chase drawer, via the existing
  `longPress.js` gesture. A normal click still opens a fresh ticket. A plain coloured dot shows
  while a chase is active (no letter, since "C" already means Cash on PositionStrip's pills), with
  a one-time dismissible InfoHint. State lives in the `chaseActiveCount` / `chaseDrawerOpen` stores
  in [`stores.js`](frontend/src/lib/stores.js). `/orders` passes `hideOrder`, since its own
  ChaseCard is always visible.
- **ChaseCard remount-loop fix (`a3ade9db`).** `/orders` mounted two ChaseCard instances as
  mutually exclusive `{#if}`/`{:else}` branches sharing one `bind:activeCount`. Each fresh
  instance wrote its own empty pre-fetch count, which flipped the page's show/hide derivation and
  remounted the other branch, forever. Measured 590-600 req/s against `/chases/active`. Fixed in
  the component, so every consumer is protected: a `_loadedOnce` guard defers the mirror write
  until this instance's first fetch resolves, and the effect's own reads are `untrack()`'d.
- **`/orders` single-instance migration (`23651dd4`).** One always-mounted
  `<ChaseCard headless={!_showChases}>`, matching the header drawer. Section chrome stays mounted
  and toggles with `style:display`, not `{#if}`. There is no empty-state window for the bug class
  to recur in.
- Invariant: a chase never finalizes an order whose broker fate it cannot confirm. A bound value
  written by several component instances must be written only once its writer holds real data.

**Chase Kill lifecycle — algo-order-keyed kill, re-killable CANCEL_FAILED, truthful `ok` (2026-10-23;
commits `fa70a893`, `d53ad4c9`)** — Three linked defects in the Kill path. The Kill route is
`kill_chase` ([`orders.py:1988`](backend/api/routes/orders.py#L1988)).
- **Kill targets the live order, not a snapshot.** The marker is set first, via `mark_order_killed()`
  (see the amendment under "Cancel paths must call chase's mark_killed()" above). If the first cancel
  fails while chase still runs the row, `_rco_kill_resolve_dispatch_race`
  ([`orders.py:1577`](backend/api/routes/orders.py#L1577)) re-reads the row and retries against the
  freshly resolved broker id. If the retry is still ambiguous and chase is active, the response is
  `pending: true` and no status is written. Chase's own terminal write is never stomped.
- **`ok` reflects the real outcome.** The response used to be `ok: true` even on a broker-cancel
  failure or an already-FILLED/REJECTED row. `ok` is now the true outcome. The paper-engine test
  `test_kill_chase_paper.py` pins the new `ok: false` case.
- **CANCEL_FAILED is re-killable.** The gate used to accept only `OPEN`, so a `CANCEL_FAILED` row was
  stuck forever. It now accepts `CANCEL_FAILED` too. A retry first checks broker truth through
  `get_broker_order_truth()`. A row is force-resolved only when two things hold: the broker's error
  text reads as "this order is gone", and the row predates the current trading session. An ambiguous
  failure is never force-resolved. The known AMO variety mismatch is one such ambiguous case.
- **ChaseCard reads `ok` strictly.** `_kill()` in
  [`ChaseCard.svelte`](frontend/src/lib/order/ChaseCard.svelte) treats success as `!!r?.ok && !r?.err`.
  That holds even if the backend regresses to the old shape. A failure fires a sticky toast with the
  real error text, because the in-card banner is cleared by the next 3s poll. `pending: true` fires a
  warning toast, not an error.
- Invariant: a Kill response never reports success for an unconfirmed cancel. A Kill never writes a
  terminal status while a chase still owns the row.
- Deploy coupling: `fa70a893` requires `6ba6936e` (the `mark_order_killed` marker lives in `chase.py`).
  `d53ad4c9` pairs with `fa70a893` (the frontend `ok` contract). Ship them together.

**Order reconcile consolidated onto one accessor; periodic order-poll sweep; postback follow-up
verification (2026-10-23; commit `8c0d5180`)** — Three reconcile paths and the open-order watchdog
each carried their own broker-truth logic. The riskiest guessed `UNFILLED` on a missing or empty
broker response. All four now route through `get_broker_order_truth()` / `apply_order_truth()`
(see the round-2 chase entry above): `list_active_chases`, the admin bulk and per-card
`/algo/reconcile` routes in `backend/api/routes/orders.py`, and `background.py`'s watchdog.
- **The accessor is deliberately thin.** It omits side effects. The new
  [`order_poll.py`](backend/api/algo/order_poll.py#L60) `reconcile_row_and_followup()` adds them
  back: template attach, ws-resubscribe, and an `order_correction` alert on a genuine correction.
  The correction alert is urgent ntfy (action-required). It renders through
  `format_notification_header()` with the corrected row's own `agent_id`, so it leads with origin.
- **Chase-active no-op guard.** `reconcile_row_and_followup()` is a full no-op while
  `is_chase_active()` ([`chase.py:722`](backend/api/algo/chase.py#L722)) is true. The hazard it
  prevents: a background corrector racing a routine, still-in-flight chase fill sees the broker
  fill before chase records it, and fires a false `order_correction` on every normal fill. The
  chase owns that row until it finishes.
  - *Amended 2026-10-23 (commit `64810434`, O5):* the no-op is no longer absolute.
    `reconcile_row_and_followup()` ([`order_poll.py:66`](backend/api/algo/order_poll.py#L66)) takes an
    `owning_chase=True` kwarg, which skips the `is_chase_active` gate for exactly one caller: chase's
    own periodic broker-truth check, `_ch_periodic_broker_truth_check`
    ([`chase.py:3046`](backend/api/algo/chase.py#L3046)). That check runs while the marker is set, so
    without the bypass the migration would be a permanent no-op. It passes `fire_alert=False`, so a
    routine self-resolving chase fill does not raise an `order_correction` alert. The backstop reconcile
    `_ch_backstop_reconcile_one` ([`chase.py:824`](backend/api/algo/chase.py#L824)) needs no bypass,
    because it runs after the marker is cleared.
- **Periodic sweep.** `_task_order_poll()` ([`order_poll.py:258`](backend/api/algo/order_poll.py#L258))
  covers every live `OPEN` / `CANCEL_FAILED` order broker-wide, independent of chase. Cadence is the
  existing 5-minute book-poll backstop (`performance.refresh_interval`, same YAML fallback chain).
- **Postback follow-up.** After every terminal postback event, `orders_postback.py` schedules
  `_pb_followup_verify` 5 seconds later
  ([`_pb_schedule_followup_verify`](backend/api/routes/orders_postback.py#L940)). It is
  non-blocking, so the webhook response is not slowed, and it catches a wrong or incomplete
  payload.
- **Admin HTTP contract changed.** Two behaviours that a frontend or operator doc may still assume
  the old way:
  - Admin bulk reconcile's `missing` field used to mean "broker confirmed gone, marked UNFILLED".
    It now means "broker response unavailable, row left untouched".
  - Per-card reconcile no longer returns HTTP 502 on a broker fetch failure. It returns
    `truth.available=False` with an explanatory `note`. Safer, but a hard error became a soft one.
- **Accepted tradeoff.** A prior-day abandoned `OPEN` row no longer auto-resolves to `UNFILLED`,
  because the watchdog's day-scoped broker snapshot cannot see it. The impact is UI and DB noise
  only. `recover_live_chases()` in `background.py` re-verifies each order with a targeted lookup
  (Kite's `order_history(order_id)`, not the day-scoped bulk list) before resuming a chase, and
  skips the restart if it cannot confirm. A dead row therefore cannot be resurrected into a live
  order.

**Chase correctness — restart safety and circuit-breaker scope (2026-10-23; commits `ceddd178`,
`d6632590`; O2 and O5 are amended into the entries above)** —
- **Restart recovery never trusts a failed cancel (`ceddd178`).**
  `_recover_query_and_cancel_resting` ([`background.py:8012`](backend/api/background.py#L8012)) used to
  swallow a `cancel_order` exception at WARNING and return the post-cancel read. If that read showed the
  order still resting, `recover_live_chases()` ([`background.py:8193`](backend/api/background.py#L8193))
  restarted chase with the full remaining quantity, on top of the live original. Now, when the cancel
  call itself raised, the status must be re-verified as gone (`_CH_CONFIRMED_GONE_STATUSES`). If it
  cannot be, that restart is skipped for this cycle and an alert fires for manual review. This is the
  same fail-closed shape as chase's own ambiguous-cancel abort. A cancel that returns normally is
  unaffected. This extends the "Accepted tradeoff" note in the order-reconcile entry above: restart
  now also refuses on an ambiguous cancel, not only on a failed lookup.
- **A confirm-window timeout is not a broker rejection (`d6632590`).** The 15s ticket-confirm
  `asyncio.wait_for` can expire while `_ch_seed_pre_fill_net_qty` fetches positions across all accounts.
  The order may still land and be handled by the abandonment path. `_start_live_chase` now raises
  `ChaseConfirmTimeout` ([`orders_helpers.py:294`](backend/api/routes/orders_helpers.py#L294), a
  `RuntimeError` subclass) only on that timeout. `_ticket_handle_live_place_error` skips
  `_record_rejection` and `_maybe_send_breaker_alert` for this type alone. A genuine broker rejection
  still counts toward the breaker.
- Invariant: restart and abort paths never guess broker fate. The circuit breaker counts only real
  rejections.

**Live ticket placement fail-closed on AlgoOrder pre-persist (2026-09-28, commit 8fca413b)** — Ticket placement (`orders_place.py:ticket_order_handler`) used to silently ignore DB insert failures when pre-persisting the `AlgoOrder` row, swallowing exceptions and proceeding to place a live order completely untracked. Root-caused via live prod incident: `AlgoOrder #1088` was placed live, chased for ~12 minutes, never appeared in operator views, and was never reconciled post-session. Fix: any DB insert failure (constraint violation, transaction rollback, timeout) now returns `HTTPException(503 SERVICE_UNAVAILABLE)` immediately, refusing the order and rolling back to client. Client sees a clear failure and can retry; no untracked live orders escape.

**OrderBook no longer flickers on transient broker-fetch failure (2026-09-28, commit 298cd38c)** — Order display grid (`OrderBook.svelte`) was falling through to algo-only stale data (a 5-min-old cache) on any transient broker fetch failure (network timeout, 429, DNS), causing rows to flicker cancelled/filled back to OPEN and breaking operator visibility. Related to "Staleness indicator freeze rule" — this is a sibling consumer. Fix: froze to last-known-good broker state on failure, showing the last fetch's result (empty if never fetched) instead of silently collapsing to local-only stale cache.

**Multi-leg basket submit must not close the SymbolPanel modal mid-placement (2026-10-16)** — `SymbolPanel.svelte:submitBasket()` calls the host's `onSubmit` prop once PER PLACED LEG (same payload shape as a single-order submit), by design — SymbolPanel itself never closes on submit ("modal stays open after a successful chain submit so they can place more legs without re-opening"). Every host honors this except `MarketPulse.svelte`, whose `onSubmit={() => closeTicket()}` closed unconditionally on the very first callback — so a multi-leg Chain basket placed from MarketPulse vanished after leg 1 even though every leg genuinely placed. `derivatives/+page.svelte` and `orders/+page.svelte` already use a non-closing pattern (toast/refresh only); MarketPulse now matches — `onSubmit` shows a brief `toast.success(...)` instead of calling `closeTicket()` (which remains wired to the × button / `onClose` only). Invariant: a `SymbolPanel` host's `onSubmit` must never assume "called once" — basket submits call it N times, and closing on the first call truncates the UI mid-placement even when every leg succeeds.

**Order-ticket "Market closed" false warning on MCX CE/PE cache miss (2026-10-23; commit
`20b3b71a`)** — Live report: a GOLD CE ticket showed "Market closed — order will queue" while MCX
was open. The 2026-10-22 root-future rescue in `exchangeOptions`
([`OrderTicket.svelte`](frontend/src/lib/order/OrderTicket.svelte)) covered only `kind === 'FUT'`. A
resolved CE/PE whose exact row missed the instruments cache fell through to the generic
`['NFO','BFO']` list, which has no MCX entry. The rescue now also covers CE/PE, in both
`exchangeOptions` and `_resolvedExchange`: extract the root by regex, then `findNearestFuture`. The
two draft-promotion callers (MarketPulse `_openTicketFromDraftRow`, derivatives `executeDraft`) try
the same rescue before the hardcoded NFO hint. `executeDraft` also never awaited the instruments
cache, a real cold-cache race. This was a misleading warning, not a silent placement failure. The
backend segment gate checks the real segment independently and was never blocked.

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

**Draft positions — long-press selection, OrderBook/LogPanel relabel, per-underlying payoff toggle (2026-10-21; prior round `6bf3dfd7`)** — `6bf3dfd7` made draft positions behave as real positions everywhere except margin, chase, bracket, and close, and deleted the single global `showDraftInPayoff` payoff toggle. This round changes three surfaces. Spec: [PULSE_SPEC.md](docs/specs/PULSE_SPEC.md) §4.4.1.
- **Selection for bulk clear.** The per-row checkbox is gone from the MarketPulse Positions grid and the derivatives Legs grid. Long-press (touch) or right-click (desktop) on a draft row enters selection mode for that grid and selects the row. Real (non-draft) rows keep their existing context menu. The header `Clear Selected Drafts (N)` button renders only while N > 0. A Cancel (✕) button beside it exits selection without deleting. Gesture reuses the `longPress.js` contextmenu+touch pair; the Legs grid row-level handler carries an 800 ms de-dupe guard in `_startDraftSelection` ([`derivatives/+page.svelte`](frontend/src/routes/(algo)/admin/derivatives/+page.svelte)). **Superseded 2026-10-22 (commit `85a275cd`):** the gesture on a draft row now opens the unified menu instead of selecting directly — see the amendment below.
- **Relabel, not hide, in OrderBook and LogPanel.** A draft `AlgoOrder` row (`mode='draft'`, `status='OPEN'`) stays in the merged list with a `DRAFT` pill (`.mode-pill-draft`) in place of its status badge, and is excluded from every status-chip count. Hiding was rejected because an operator could lose track of a draft. Showing it as OPEN was rejected because it risks mistaking it for a resting broker order, which is worse. The predicate is `isDraftOrderRow()` in [`draftOrderRow.js`](frontend/src/lib/data/draftOrderRow.js), shared by both panels so they cannot drift. Chase visibility needed no change: `list_active_chases` excludes `mode='draft'`, and the chase chip requires `attempts > 0`.
- **Per-underlying payoff toggle (`DRAFT ON` / `DRAFT OFF`).** Shown only when the viewed underlying has a draft leg. State is a `SvelteMap` keyed by `selectedUnderlying.toUpperCase()`, defaults ON, and is session-only. The 6bf3dfd7 global toggle was deleted because its exclusion was scattered as `!showDraft &&` across ~8 aggregate sites, and each new aggregate risked skipping it. This version differs in two ways. It is per-underlying, because Snapshot and EV render rows for underlyings other than the charted one, so one global boolean would either leak into or blank those rows. And it is gated in one place: `_isLegEnabled` calls `isDraftLegIncluded()` ([`derivativesMath.js`](frontend/src/lib/data/derivativesMath.js)), so every aggregate that already reads enabled-state inherits it. The button key reuses the page's existing `toUpperCase()` underlying canonicalisation, not a fresh root helper, so MCX virtual-root aliasing cannot make button visibility and the gate disagree.
- Invariant: a draft row is never counted in an order-status chip. The payoff toggle narrows only `source === 'draft'` legs, and only through `_isLegEnabled`. It never hides the Legs or Positions grid row, so Clear stays reachable regardless of toggle state.

**Draft sweep at market open, and draft gestures moved into the unified context menu (2026-10-22; commits `e8ca302b`, `85a275cd`)** — two amendments to the round above.
- **Sweep (`e8ca302b`).** A draft that survives to the next trading-day 08:00 IST boundary after its creation is auto-cleared. `_sweep_stale_drafts()` in [background.py](backend/api/background.py) selects `AlgoOrder` rows with `mode='draft'`, `status='OPEN'`, and `created_at < settlement_cutoff_for("NON-MCX")`. It deletes each through `orders.py:_draft_delete_by_id`, the same guard the manual Clear button uses. The tick `_ds_tick_draft_sweep` runs once per day at or after 08:00 IST and is marked done before the sweep starts, so a mid-sweep failure is not retried every 30s. Drafts created after today's boundary survive until the next day's boundary. The gate is calendar-only (`_draft_sweep_is_trading_day` → `is_trading_day()`), not `exchange_clock.get_nse_open_time()`. That function falls back to 08:00 on a plain weekend, which would have swept Friday's drafts on Saturday. When N > 0, one info-tier Telegram line reports the count. No new UI.
- **Gesture (`85a275cd`).** The round-2 diversion is superseded. Long-press and right-click on any row, draft rows included, open the unified [SymbolContextMenu.svelte](frontend/src/lib/SymbolContextMenu.svelte). "Select for clear" and "Promote to real" are draft-only items inside that menu. "Select for clear" is the only path into selection mode. MarketPulse's hand-rolled menu is gone, and all six of its buckets use the shared component. The touch long-press timer and `pointercancel` handling are unchanged. Only the action taken on the gesture changed. The header Clear and Cancel controls described above are unchanged.
- Invariant: a draft row's gesture opens the one unified menu, like every other row. Selection for clear is reached only through that menu's item, never through a bare gesture.

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

**MCX base-metal futures dropped from live positions on their real expiry day (2026-10-23; commit
`a2f21798`, 3A)** — `is_live_row_past_freeze_window()`
([`expiry_freeze.py:425`](backend/api/algo/expiry_freeze.py#L425)) derived an MCX future's expiry
through `parse_tradingsymbol`'s FUT branch. That branch approximates every MCX future as the last
Thursday of its month. The approximation is right for CRUDEOIL, GOLDM, SILVER and NATURALGAS. It is
wrong for COPPER, ZINC, LEAD, ALUMINIUM and NICKEL (and mini variants), whose real expiry is often the
last business day, which can be a Friday after the approximated Thursday. On its real expiry day the
contract was treated as already expired and dropped from the live `/api/positions` fetch for the whole
day. That corrupted account and symbol rollups and every consumer of the response: NavStrip, the Legs
grid, MarketPulse and the LKG cache.
- Fix: `_mcx_future_real_expiry()` ([`expiry_freeze.py:363`](backend/api/algo/expiry_freeze.py#L363))
  reads the real expiry from the instruments-cache lookup that `list_active_futures` and
  `kite.py:get_lot_size` already share. The check consults it only on the branch that can drop a row.
  On a cache hit it defers to `is_mcx_future_still_current()` against the real expiry. On a miss (for
  example, Kite purged the contract) it falls back to the old approximation, unchanged.
- Non-MCX futures and all options are untouched. Residual: `expiry_if_closed_on_own_expiry_day()`, the
  closed-hours sweep classifier, still uses the same approximation. See the residual note in the
  expiry-day freeze entry above.
- Invariant: a held MCX future is never dropped from the live fetch on its own expiry day. The real
  expiry date, not a calendar approximation, governs that decision.

**MCX front-month parity in the frontend — expiry day and overnight (2026-10-23; commit `5c56af5a`, 3B)**
— `findNearestFuture()` ([`instruments.js:479`](frontend/src/lib/data/instruments.js#L479)) and
`seedRootMapFromInstruments()` (`frontend/src/lib/data/rootOf.js`) rolled an MCX future at bare
midnight on its expiry date. The backend keeps it current through the expiry day and the closed
overnight window, rolling only after the next trading day's 08:00 IST open. So the Snapshot and NavStrip
spot basis flipped depending on which contract last ticked.
- Both paths now use `isMcxFutureStillCurrent()` ([`dateFormat.js:131`](frontend/src/lib/dateFormat.js#L131))
  for `r.e === 'MCX'` rows. The predicate is weekend-aware: a Friday expiry stays current until Monday
  08:00. NFO and CDS keep their bare-midnight compare.
- Residual: there is no client-side holiday calendar, so a holiday immediately after an MCX expiry is a
  known gap. The backend rule covers it.
- Invariant: frontend and backend agree on which MCX contract is front-month at every instant,
  except during the holiday residual above.

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

**Groww GTT refusal in prod, then a live deploy-coupling regression on dev, fixed same day
(commits `7183244b`, `0b935935`).** `RemoteBroker.validate_gtt_exchange`
([`remote_broker.py:256`](backend/brokers/client/remote_broker.py#L256), commit `7183244b`) now
forwards to conn_service. Before, it was the base no-op, so Groww's MCX/NCO GTT refusal was
silently skipped in conn-service mode. Errors were re-raised as `ValueError` so `apply_plan_live`'s
`except ValueError` still catches them — but `conn_service` is the SAME shared process for dev and
prod (`/opt/ramboq` on `main`), and only picks up new `backend/brokers/` code on a prod deploy. The
commit shipped to `dev` before `main` had the new `validate_gtt_exchange` RPC in its whitelist, so
every call 403'd at conn_service's own `_ALLOWED_BROKER_METHODS` check — mapping to
`BrokerNetworkError` (a `BrokerError` subclass) — and the fail-closed design silently skipped the
GTT attach for EVERY exchange, on every broker, confirmed live on dev the same day. Fixed (`0b935935`):
`validate_gtt_exchange` now fails OPEN specifically on `BrokerNetworkError` (covers both "method not
deployed yet" and a genuine transport blip), restoring the pre-existing no-op behavior until
conn_service actually has the method. A real exchange-refusal verdict from an upgraded conn_service
comes back as a different `error_type` and still fails closed. Invariant: any new `RemoteBroker`
override that forwards to conn_service must not fail closed on a transport/whitelist failure if
conn_service's own upgrade is deploy-coupled to a later (prod) push than the one introducing the
override — the pre-existing behavior (here: no-op) is the only safe default until both sides ship.

**Groww's margin call used the wrong SDK signature entirely — the gate has never computed a real
number (2026-10-20, commit `0b935935`).** The parser/adapter fix above assumed Groww's
`get_order_margin_details` took per-order kwargs
(`trading_symbol=, exchange=, segment=, transaction_type=, quantity=, order_type=, product=,
price=`). The real installed SDK's signature, confirmed via server-side introspection, is
`(segment: str, orders: list[dict], timeout=None)` — a batch call. Every real call raised
`TypeError`, confirmed live via the `[MARGIN-RAW-API]` log
(`bm_res=[{'total': 0.0, 'error': "...unexpected keyword argument 'trading_symbol'", 'raw': None}]`)
— Groww's margin gate predates tonight and has never computed a real number; the parser fix just
made the failure visible instead of masking it behind an always-0 result either way. Fixed:
`basket_order_margins` ([`groww.py:1131`](backend/brokers/adapters/groww.py#L1131)) calls
`get_order_margin_details(segment=seg, orders=[{...one order...}])` — one order per call, to keep
the existing per-leg breakdown (the real response is a single aggregate, not a per-order list, so a
multi-order call would collapse the breakdown this adapter's contract requires). Response shape is
Groww's real `OrderMarginResponse` schema (`total_requirement`, `span_margin_required`,
`exposure_margin_required` — confirmed from two independent sources against the published OpenAPI
spec; `GrowwAPI._parse_response` already unwraps `payload`, so the earlier `data.total_margin` guess
was wrong on every level: wrong wrapper key, wrong field name). This endpoint has no
available-balance field — `available` is `None` here, which is fine, since
`_preflight_resolve_available_margin` reads availability from `broker.margins()`, a separate call,
never from this per-leg dict. Test: `test_dhan_groww_margin_shape.py::
test_basket_order_margins_calls_real_batch_signature` uses `create_autospec(GrowwAPI)` (not a bare
`MagicMock`) specifically because a bare mock accepts any kwargs — exactly how the original
signature bug went undetected — confirmed to fail against the pre-fix call with the live `TypeError`
message. Invariant: Dhan's parser fix is live-confirmed against real data (API-layer code, runs on
dev's own checkout); Groww's margin numbers are NOT yet live-confirmed — the fix above is correct by
SDK-signature inspection and schema cross-reference, but has not been exercised against a real
response, because this file lives in `backend/brokers/` and only runs inside conn_service, which (per
the deploy-coupling note above) needs a prod deploy before dev's calls reach this code.

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
  - *Amended 2026-10-23:* the abort also writes a `CANCEL_FAILED` marker on the row, never
    `REJECTED`, which would block a later real fill. Its alert goes through the binary ntfy
    action-required gate, so it still reaches ntfy as urgent. See "Chase robustness round 2" above.
  - *Amended 2026-10-23 (commit `3f4392e4`, O2):* `_ch_exhaust_max_attempts`
    ([`chase.py:2265`](backend/api/algo/chase.py#L2265)) and `_ch_abort_stuck`
    ([`chase.py:3156`](backend/api/algo/chase.py#L3156)) both run a final cancel-and-confirm read
    before choosing the terminal outcome. Each now checks whether that read shows `cumulative_filled`
    has reached `quantity`. If so, the chase is recorded `FILLED`, not `UNFILLED`. This matches the in-loop path `_ch_zero_remaining_fill`. A late full fill
    therefore fires template attach and auto-TP, which matters most for Dhan and Groww.
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

**Template and bracket attach hardening — retry lock, persist-failure hold, scale-out OCO, close gate,
validation (2026-10-23; commits `df9d3189`, `f626457a`, `8e26a0b3`, `1d8622fb`, `91ba7923`, `7fd3eeba`,
`270e7e26`, `1a883590`, `6d4b6c10`, `510e0a30`, `20ee0815`)** — Nine defects in the template attach
path, grouped by mechanism. Each bullet names its own fix.
- **Retry re-checks inside the lock (T13, `df9d3189`).** The retry route checked `attached_gtts_json`
  before the per-row lock. A concurrent fill-triggered attach could persist in that gap, and retry then
  placed a second live GTT and wing set. `_rco_attached_gtts_already_set()`
  ([`orders.py:1317`](backend/api/routes/orders.py#L1317)) re-reads the row inside the lock (called at
  [`orders.py:1389`](backend/api/routes/orders.py#L1389)). It matches the fill path's own in-lock check.
- **GTT persist failure holds and alerts, never silently duplicates (T16, `f626457a`, `8e26a0b3`).**
  `_opp_persist_attached_gtts` runs after broker placement. A DB failure there used to log a WARNING
  only. The exits stayed live but unrecorded, so a redelivery or a manual retry placed a second set.
  Now `_opp_persist_with_retry` ([`orders_place.py:876`](backend/api/routes/orders_place.py#L876))
  retries 3 times (0.5s, then 1s). On exhaustion, `_opp_handle_persist_failure`
  ([`orders_place.py:910`](backend/api/routes/orders_place.py#L910)) holds the payload in
  `_TPL_ATTACH_PERSIST_PENDING` ([`orders_place.py:329`](backend/api/routes/orders_place.py#L329)),
  keyed by parent row id. It then fires `template_attach_failed` with `placed_untracked_ids`. That
  footer says the exits are placed but unrecorded and must not be armed by hand. The default footer
  is unchanged. The hold is checked in three places: `_fire_template_attach_on_fill`
  ([`orders_place.py:1129`](backend/api/routes/orders_place.py#L1129)), `_rco_attached_gtts_already_set`
  and `_retry_precheck_row` ([`orders.py:517`](backend/api/routes/orders.py#L517)). Residual: the hold
  is in-memory, so a process restart drops it and a later trigger could place a real duplicate. The
  alert has already listed the broker ids. The hold is deliberately not DB-persisted. A marker written
  after a failed DB write could block a legitimate retry.
- **Scale-out ladder plus SL gets OCO pairing (T14, `1d8622fb`, `91ba7923`).** The old pairing matched
  only an exact `{"TP","SL"}` two-leg set. A scale-out ladder's rung labels never matched, so every
  surviving rung stayed live after SL fired. `_ta_detect_scale_sl_group()`
  ([`template_attach.py:2442`](backend/api/algo/template_attach.py#L2442)) records an asymmetric
  `(sl_id, [tp_ids])` group on `AttachResult.sibling_groups`. The group is SL-side only, so
  sequential scale-out fires never cancel each other. `_task_oco_pair_watcher` runs
  `_oco_process_group_entry` ([`background.py:5597`](backend/api/background.py#L5597)). Liveness uses
  `_oco_gtt_is_live` ([`background.py:5449`](backend/api/background.py#L5449)), which treats a
  `triggered` or `cancelled` GTT still listed by Kite as gone.
  - Follow-up (`91ba7923`): "gone" is not "closed". A rung rejected at placement, or a fired-but-unfilled
    exit, also reads as gone. Cancelling then could leave a live position with no stop. The cancel now
    requires `_oco_group_position_is_flat()` ([`background.py:5477`](backend/api/background.py#L5477)) to
    return exactly `True`, from a fresh positions read. `None` (fetch failed or no row) is never flat.
    Otherwise sibling ids are left untouched and retried next cycle.
- **The close gate covers override-only tickets (T15, `7fd3eeba`).** The pre-fill close gate ran only
  when `template_id` was set. An override-only ticket (inline `tp_pct_override`, `sl_pct_override`,
  `wing_*_override`) skipped it, and its overrides reached the persisted row. The gate is now
  `_ticket_strip_template_on_close()` ([`orders_place.py:505`](backend/api/routes/orders_place.py#L505)).
  It triggers on `template_id or has_any_override`. On a server-verified close it nulls every override
  field. The fill-time offsetting check in `_pb_check_and_fire_template_attach`
  ([`orders_postback.py:787`](backend/api/routes/orders_postback.py#L787)) cannot see a full close,
  because the position is already flat. This gate runs upstream of it. It does NOT set `intent="close"`,
  which would bypass the G2 and MCX size caps. A sign-only read must never grant that bypass. See
  "Verified-intent propagation" above. Not covered, by design: partial closes fall through to the
  postback check, reconcile has no offsetting check, and `orders_basket.py` has no equivalent gate. A
  basket leg's `template_id` still passes unchecked.
- **A chase-routed wing inherits the parent's product (T18, `270e7e26`).** `_chase_wing` built its
  `ChaseConfig` without `product=`, so a chase-routed wing went NRML while its own row said the parent's
  product. The fix threads `plan.wing.product` through. This is the same class as the close-path product
  defaults above.
- **Template save rejects `wing_premium_pct <= 0` (T19, `1a883590`).**
  `_validate_wing_premium_pct()` ([`templates.py:113`](backend/api/routes/templates.py#L113)) mirrors the
  resolver's own rejection on both create and PATCH. Before, every fill raised a 422 deep in
  `_parse_template_overrides` and logged a WARNING only. For already-saved bad templates,
  `_opp_handle_fill_path_exception` ([`orders_place.py:981`](backend/api/routes/orders_place.py#L981))
  fires the real `template_attach_failed` alert for an HTTPException only. Other exceptions keep their
  silent behaviour. tp and sl values are deliberately not validated, because the resolver soft-degrades
  them.
- **Kite MARKET-leg TP cap warning (T17, `6d4b6c10`).** Kite's `place_gtt` raises
  `BrokerCapabilityError` when any leg is MARKET ([`kite.py:561`](backend/brokers/adapters/kite.py#L561)).
  A TP and SL bracket goes as one two-leg OCO, so `tp_order_type=MARKET` on Kite silently loses the SL
  too. `capWarningFor()` ([`brokerCapWarnings.js:30`](frontend/src/lib/data/brokerCapWarnings.js#L30))
  takes a live override `ov`, so the warning follows the TemplateBar MKT toggle even when the template
  is unsaved. Basket warnings go through `aggregateCapWarnings()`
  ([`:100`](frontend/src/lib/data/brokerCapWarnings.js#L100)).
- **Retry-attach writes OCO pairing (T20, `510e0a30`).** `_retry_build_attached_payload`
  ([`orders.py:394`](backend/api/routes/orders.py#L394)) claimed field-for-field parity with the fill
  path but never wrote `sibling_id` or `sibling_ids`. On Groww, which has no broker-side OCO, the
  watcher then had no pointer to cancel the surviving leg. It now reuses the same map builders. See
  "Fill bookkeeping completeness" below.
- **Off-hours wing-gate text says "refused" (T25, `20ee0815`).** `_check_offhours_wing_gate()`
  ([`template_attach.py:328`](backend/api/algo/template_attach.py#L328)) said "deferred", but nothing
  retries it. TP and SL are dropped too, because the caller returns before building them. The text now
  says the attach was refused and names TP, SL and wing. Text only, no scheduler. See the gate bullet in
  "Order-path gate status" above.
- Invariant: a template attach runs at most once per parent. Every failure is recorded, alerted, or
  refused with an accurate message. The operator is never told to arm exits that are already live.
- Deploy coupling (not independently shippable, same convention as the close_settled entry): `91ba7923`
  requires `1d8622fb`. Without the flat-position gate, the scale-ladder OCO watcher can cancel a live
  SL. `f626457a` requires `8e26a0b3` (the `getattr` guard on the pending-hold check). `510e0a30` and
  `20ee0815` are independent. Ship the full range together.

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

**Payoff chart Exp P&L basis (C1, 2026-09)** — The displayed Exp P&L value shown ON the Payoff chart next to the expiry marker (dart position) is priced at `payoffSpot` (anchor-contract basis) instead of `liveSpot` (front-month), so the overlay value and the marker's drawn position are guaranteed to visually agree — both reference the same contract. This is a deliberate, correctly-scoped choice: the separate Legs-grid TOTAL row's Exp P&L continues to read `liveSpot` / front-month (`_legsExpPnlTotal`), a different and correct consumer. NSE underlyings (no anchor future) also live-tick via the Tier-1b fallback (reusing liveSpot's own cash-ticker lookup) so the overlay ticks live every second instead of stepping once per 5s refetch — the C2 fix for overlay-desync on NSE. A broader cross-surface SSOT consolidation landed in the same timeframe, consolidating spot/LTP, prevClose, Day P&L, and position existence onto `portfolioStore` — `payoffSpot`'s anchor-contract tier remains the one deliberate exception to the single-spot-resolver rule and was not affected by this consolidation. **Future work MUST NOT revert this to liveSpot** — the visual disagreement between marker and number (if it were to happen) is a user-facing defect. Invariant: chart value = marker position visually, always.

**Dart-vs-number second-computation bug, fixed (2026-10-23, amends the entry above)** — The original C1 fix introduced its OWN second computation: `_chartExpPnlAtSpot` in `+page.svelte` independently summed each enabled leg's Exp P&L via `_legExpPnlDisplay`/`expiryPnlWithRealised` (a simple per-leg intrinsic-at-spot formula) and passed the result as the `legsExpPnlAtSpot` prop into `OptionsPayoff.svelte`, overriding the chart's own `curveAtSpot.expiry_value` (the SAME backend curve the dart is drawn from, `yOf(curveAtSpot.expiry_value)`). That local sum could not replicate three things the backend curve (`multileg_payoff_curve`/`_leg_today_expiry_arrays`, `backend/api/algo/derivatives.py`) actually does: (a) a multi-month MCX basket — the backend scales each leg's own spot by its `scale_ratio`, the local sum priced every leg at the same shared spot; (b) a calendar/diagonal spread — the backend Black-Scholes-prices a far leg at its own remaining time-to-expiry, the local sum used plain intrinsic (T=0) for every leg; (c) a futures leg — the backend values it at the shared (scaled) grid spot, the local sum used `resolveExpiryAnchor()`, which prefers the leg's OWN independently-ticking polled LTP. All three visibly broke the "chart value = marker position" invariant whenever basis wasn't zero. Fix: `_chartExpPnlAtSpot` and the `legsExpPnlAtSpot` prop are deleted entirely (not fixed in parallel — eliminating the second computation, not reconciling two formulas, per the operator's explicit instruction). `OptionsPayoff.svelte`'s Exp P&L readout now unconditionally renders `fmtMoney(curveAtSpot.expiry_value)` — the identical value already driving the dart. Test: `frontend/e2e/derivatives_payoff_dart_number_parity.spec.js` (3 live-UI cases, one per divergence class above, each confirmed to fail against the pre-fix code + a static SSOT grep guard). Invariant unchanged: chart value = marker position, always — but now enforced structurally (one value, one reader) rather than by two formulas that happened to agree at zero basis.

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

**Derivatives frontend parity — 08:00 boundary in pageLoad, Snapshot CE/PE misclassification, TDAY
anchor (2026-10-23; commits `79c0ca77`, `7111bbe1`, `33b66acf`)** — Three frontend derivatives
defects. Each is a view that disagreed with the rule the backend or another view already applied.
- **Expired-leg checks use the 08:00 IST boundary (3D, `79c0ca77`).**
  `buildCandidatePositions`'s `isUnresolvable` check
  ([`pageLoad.js:467`](frontend/src/lib/derivatives/pageLoad.js#L467)) and `buildCleanLegs`'s
  safety-net filter ([`pageLoad.js:668`](frontend/src/lib/derivatives/pageLoad.js#L668)) compared expiry
  against bare-midnight `todayIST()`. `expiryPnl.js` already used `tradingSessionDateIST()`. Between
  00:00 and 08:00 IST after an expiry day, the chart dropped a leg still inside its freeze window. That
  reproduced the GOLDM flat-zero curve eight hours early. Both sites now use `tradingSessionDateIST()`.
  The draft-position check further down the same file still uses `todayIST()`. It was outside this
  cluster's scope. `/strategy-analytics` floors T at 0 for an at-or-past-expiry leg, so an included
  leg prices at intrinsic value, not an error. See "Exp P&L session-boundary fix" above.
- **Snapshot no longer reads equity tickers ending in CE or PE as options (3E, `7111bbe1`).**
  `rollupByUnderlying` and `perRootReduce` in
  [`derivativesMath.js`](frontend/src/lib/data/derivativesMath.js#L504) used a bare `/(CE|PE)$/`
  regex. RELIANCE or BAJFINANCE then produced a spurious underlying row, while TOTAL correctly
  excluded it. Both functions now use `isFOSymbol()`
  ([`derivativesMath.js:662`](frontend/src/lib/data/derivativesMath.js#L662)). This is the same class
  as "Auto-pairing is CE/PE-aware" above. Four test fixtures with a malformed `...CFUT` shape were
  corrected to real Kite futures shapes, because the stricter gate rejects them.
- **TDAY-at-spot anchors to the backend's curve anchor (3F, `33b66acf`).** `_pollSpot` returned the
  first enabled candidate's `underlying_ltp`, whatever its contract month. The merged payoff curve is
  anchored to `strategy.spot_anchor_contract`. On a calendar or diagonal MCX basket where a far-month
  leg came first, TDAY interpolated at the wrong price. Then TDAY-at-spot disagreed with the candidates'
  actual P&L. `resolveAnchorPollSpot()` ([`pageLoad.js:251`](frontend/src/lib/derivatives/pageLoad.js#L251))
  matches on contract month via `decomposeSymbol`, because the anchor is always a futures symbol. It
  falls back to the first enabled candidate when there is no anchor (non-MCX) or no leg's month matches
  (the common single-month case). This is the TDAY theoretical curve alignment. It is related to, but
  distinct from, the Exp P&L readout in "Payoff chart Exp P&L basis" above.
- Invariant: the derivatives views agree on one session boundary, one F&O classifier, and one anchor
  contract.

**Pre-open LTP pinning — last_price = prev_close until each row's own segment opens (2026-10-22,
commit `0538f0a7`, MCX open time corrected back to 09:00 later the same day)** — The system's
broker-connectivity window (08:00–23:31 IST) is wider than each exchange segment's own trading
session. Once the broker reconnects at 08:00, live LTP flows immediately, and MCX options can
carry a reference price tied to international gold before the local order book opens. Left alone,
that produced a phantom Day P&L move before any real trade (live incident: `GOLD26OCT142000PE`
last_price 439 vs prev_close 420; about −₹7,050 across two accounts' GOLD books). Real segment
open times, from `market_segments` in
[backend_config.yaml](backend/config/backend_config.yaml): NSE/equity 09:15, MCX/commodity 09:00.
A same-day intermediate change moved MCX to 09:30 on an unverified verbal claim; MCX's own official
circular (MCX/TRD/068/2026) confirms non-agri commodities trade from 09:00 IST, and the 09:30 value
was reverted the same day once that circular was checked. There is no source supporting a 09:30 MCX
open.
- **Predicate:** `is_segment_pending_open(exchange, now)` in
  [snapshot_gate.py](backend/api/helpers/snapshot_gate.py). True when the exchange's segment has not
  genuinely opened per `_segment_is_open()` (holiday and special-session aware). It matches
  `quote.py`'s exchange-membership rule, not `agent_engine.py`'s `_EXCHANGE_TO_SEGMENT` table.
  Fail-open: an unrecognised exchange or a config/holiday error returns False, so the live value is
  kept. A row is never pinned forever by accident.
- **Prewarm:** `prewarm_pending_open_segments()` awaits the special-sessions cache before the sync
  predicate runs on the main loop. This is the same cold-cache fail-open class as the 2026-10-20
  residual sweep.
- **Pin, positions:** `_resolve_pending_open_exchanges` then `_pin_pending_open_rows` in
  [positions.py](backend/api/routes/positions.py); the `price_source` tag comes from
  `_apply_pending_open_tag`. The pin runs AFTER `apply_day_change_backstop`, so it has final
  authority over day_change.
- **Pin, holdings:** `_pin_pending_open_holdings` and `_apply_pending_open_tag_holdings` in
  [holdings.py](backend/api/routes/holdings.py).
- **Upstream prev_close:** `_override_stale_close_from_snapshot` (positions.py) and
  `_override_stale_close_for_holdings` (holdings.py) run first and set prev_close from
  `daily_book.ltp`. The pin reads that value.
- **Call sites:** the perf poller in [background.py](backend/api/background.py) (holdings and
  positions pin blocks) and the live-session NAV fallback in
  [auth.py](backend/api/routes/auth.py).
- **Pinned values:** `last_price = prev_close`; `unrealised = (prev_close − avg_price) × qty`;
  `pnl = realised + unrealised`; `day_change`, `day_change_val`, `day_change_percentage = 0`;
  `price_source = 'pending_open'`. Both Day and Lifetime P&L pin, since no local trade has happened
  yet. The closed-hours snapshot path is untouched. No frontend change is needed:
  `nav.js:baseDayPnlForPosition` reads ~0 once `pnl` equals `prev_settlement_pnl`.
- **Invariant:** last_price never diverges from prev_close until the row's own exchange segment has
  genuinely opened. The 08:00 system window is NOT the trigger.
- **Shared-config note (2026-10-22):** `market_segments.commodity` open moved 09:00 → 09:30 → back
  to 09:00 (final, correct value), all the same day. The value is shared config, so the change is
  not specific to pinning. Confirmed readers of `market_segments` include `/market/status`, the
  quote gate, and watchlist movers, plus the order-path gates that route through
  `_segment_is_open()`. When the `exchange_clock` cache is warm, `_get_segments()`
  ([background.py](backend/api/background.py)) sources gate hours from the DB `exchange_schedule`
  table instead of YAML, so gate-shaped consumers (watchdog, perf probe, simulator guards) may still
  read the DB hours. Frontend duplicate: `MCX_OPEN_MIN` in
  [marketHours.js](frontend/src/lib/marketHours.js) must change in lockstep with the YAML.

**Market-cycle event tokens — agents can trigger on a segment's open/close state (2026-10-22)** —
This resolves the "Dormant" claim in the "Segment flags honour overrides" bullet
([CLAUDE.md](CLAUDE.md#L653)), which said no live reader of `_ae_segment_flags()`'s keys existed.
Before this round that was stronger than dormant: the dict was a local variable in `run_cycle()`
and never reached any grammar resolver. The older `minutes_since_open` token still reads
`alert_state['session_start']`, so that part of the earlier note remains true.
- **Plumbing:** `_build_context()` emits the per-segment flags. `run_cycle()` stores them as
  `context["segment_flags"]` ([agent_engine.py](backend/api/algo/agent_engine.py#L3464)).
  `_cycle_evaluate_agent()` passes them to `Context.segment_flags`
  ([agent_engine.py](backend/api/algo/agent_engine.py#L3002),
  [agent_evaluator.py](backend/api/algo/agent_evaluator.py#L248)). The dry-run path passes its base
  context through `_build_dry_run_response()`
  ([routes/agents.py](backend/api/routes/agents.py#L537)). This dict is separate from the sim
  `market_state` override, so a stale override cannot freeze these flags.
- **Tokens:** ten condition/metric tokens are defined in
  [agent_grammar.yaml](backend/config/grammars/agent_grammar.yaml#L369) and resolved in
  [grammar.py](backend/api/algo/grammar.py#L573).

| Token | Value |
|---|---|
| `is_mcx_open`, `is_nse_open` | 1.0 while the segment's session is open, else 0.0 |
| `minutes_since_{mcx,nse}_open` | Minutes since today's open; `None` while closed |
| `minutes_until_{mcx,nse}_close` | Minutes to today's close; `None` while closed |
| `minutes_until_{mcx,nse}_open` | `0` once open; `None` after today's close |
| `is_past_holiday_refresh` | 1.0 after `holiday_refresh_time` (default 05:30 IST), else 0.0 |
| `is_past_window_open` | 1.0 after today's 08:00 IST boundary, else 0.0 |

- **`is_past_window_open` is not `is_segment_pending_open()`.** That predicate has no 08:00 check
  and is true at 02:00 and 08:30 alike. This token uses `session_date_of(now) == now.date()` from
  [expiry_freeze.py](backend/api/algo/expiry_freeze.py), so 00:00–07:59 counts as the prior session.
  Known mismatch: the YAML description says "0.0 during the closed overnight window", but
  23:31–23:59 reads 1.0. Correct the description; the code is right.
- **`None`, never a coerced 0, when not applicable.** `_ae_segment_flags()` writes
  `minutes_since_*_open = 0` while closed for its other callers. The resolvers gate on the open
  flag, so a closed segment never reads as "just opened".
- **Evening-holiday anchor.** The `and is_open` guard was removed from the 17:00 anchor in
  `_ae_segment_flags()` ([agent_engine.py](backend/api/algo/agent_engine.py#L2464)). Before 17:00 on
  an MCX evening-holiday, `minutes_until_mcx_open` now counts to 17:00, not the regular open.
- **Clock-only refresh check.** `is_past_holiday_refresh` tests the clock only. It does not confirm
  that the refresh job ran or succeeded.
- **Existing agents.** `market-open-nse` and `market-status-report` still use `fire_at_time` and do
  not need migrating. `market-preclose-mcx` was retired 2026-10-28 and replaced by
  `market-preclose-mcx-v2`, which uses `minutes_until_mcx_close <= 30` and has no `fire_at_time`.
  `market-preclose-nonmcx` uses the same condition on `minutes_until_nse_close`. The retired slug
  was not reused, so the stale row is pruned and the new one is seeded fresh.
- **Tests:** [test_market_cycle_tokens.py](backend/tests/test_market_cycle_tokens.py), 32 tests.
- **Invariant:** a state-change trigger must treat a missing flag as unknown, never as closed or
  zero. An empty `segment_flags` (event-feed or hand-built `Context`) yields `None` for every token.

**Expiry pre-close alert + auto-close — one agent each covering MCX + NFO, same identification
for both, order-level hold-by-default (2026-10-29)** — `expiry-day-mcx-v2`/`expiry-day-nse-v2`
(the per-exchange retime from earlier the same day) were retired a few hours later and
consolidated, per three direct operator instructions: "create one agent for order placing and
one agent for alert instead of two", "why can't you have one agent for nfo and mcx" / "let the
function that you register identify the positions, 30 mins before market close, etc.", "alert
agent 30 mins before expiry market closure, close agents 25 mins before". Final shape:
- **`expiry-day-preclose-alert`** (high tier) and **`expiry-day-itm-auto-close`** (critical
  tier), both `schedule="always"`, no `fire_at_time`, `cooldown_minutes=1320` → **180**, each a
  single flat leaf condition (`is_expiry_preclose_alert_window==1` / `is_expiry_preclose_close_
  window==1`). The exchange-loop and lead-time check live inside the metric resolver
  ([grammar_tokens/metric_is_expiry_preclose_alert_window.py](backend/api/algo/grammar_tokens/metric_is_expiry_preclose_alert_window.py)
  + its `_close_window` sibling, threshold 25 not 30), not a DSL compound tree — each reads the
  already-computed cheap tokens (`is_{mcx,nse}_expiry_today`, `minutes_until_{mcx,nse}_close`, no
  broker call) for BOTH exchanges and returns one boolean.
- **180-min cooldown, not 22h — deliberate, not an oversight.** Both agents share ONE metric name
  across MCX and NFO, and the per-fire latch key is `(agent_slug, metric, scope, account)` with
  no exchange dimension (`funds.total`-scoped, no account/underlying to key on). A 22h cooldown
  would let NFO's ~15:00 fire suppress MCX's own ~23:00 fire the SAME day — NFO structurally
  always precedes MCX (15:30 vs 23:30 close), so this is a real bug class, not hypothetical. 180
  min covers one ~30-min firing window (no same-exchange double-fire) while clearing hours before
  the ~7.5h gap to the other exchange's window. Pinned by
  `test_expiry_day_and_preclose_agents.py::TestExpiryDayAgentsSameDayNoSuppression`.
- **Identification is shared, not duplicated.** Both the alert's render helper
  (`_v2_expiry_position_breakdown()`, agent_engine.py, called from `_v2_send_rich_alert`'s
  reminder-only branch) and the order-placing action (`_action_live_expiry_auto_close`,
  actions_live.py) call `ExpiryEngine.scan_positions()` — confirmed read-only (fetches
  positions/instruments/LTP only, no order placement) and the backend's own mirror of the
  Derivatives Exp-close tab (confirmed: `derivativesMath.js:350` —
  `"// Equity: ITM → close, OTM → otm. No netting on NFO."` — matches `ExpiryEngine` exactly). MCX
  gets the real 4-rule theta-priority netting on ITM/NTM legs only (`residual_qty != 0` → needs
  closing); NFO gets unconditional ITM/NTM closing with no netting exception. The action was
  generalized to run ONE scan covering both exchanges (previously two separate agents, one scan
  each, restricted to one exchange via a `params.exchange` the action no longer requires —
  `agent_grammar.yaml`'s `expiry_auto_close` token's `exchange` param moved `required: true` →
  `false`, golden baseline updated to match). `before_cutoff()` per-exchange gating was removed
  from the action — the agent's own condition already gates timing, so a second,
  independently-configured gate inside the action would just be a second place the same decision
  could disagree.
- **Wording never claims a verdict it can't back.** MCX: a root with zero `needs_close` legs
  renders demoted/last as `netted: ROOT`, NEUTRAL wording only — never "safe"/"hedged, no action
  needed" (a long-CE+long-PE pair legitimately nets to zero but can still devolve into a naked
  future on settlement if spot lands between strikes). NFO: an OTM leg renders demoted/last as
  `no action (OTM): ROOT` — never "netted" (no pairing step exists for equity at all; a root can
  genuinely mix an ITM leg needing closure with an OTM leg needing none, and NFO's render loop is
  per-leg, not per-root, specifically so both facts show). A `scan_positions()` failure renders an
  explicit `(position detail unavailable...)` note, never a silent blank (which would read as a
  false "all clear" — the same "Staleness indicator freeze rule" invariant applied here).
- **Order-level safety was already built — no new plumbing.** `expiry-day-itm-auto-close` ships
  `status="active"` deliberately: it is a brand-new slug, so it is NOT bound by
  `_ae_guard_seed_status`'s "Ships INACTIVE" fail-safe (that guard triggers only on the literal
  phrase appearing in a seeded agent's own description, by design — see the "Kind classification"
  entry elsewhere in this file for the sibling schedule-only-sentinel guard). The real safety net
  is `ExpiryEngine.close_positions()`'s pre-existing `expiry_close_held()` check
  (`order_hold_gate.py`, `HoldCategory.EXPIRY_CLOSE`, `DEFAULT_HELD=True`) — every close lands as a
  `HELD` `AlgoOrder` row via `record_held_order()`, no broker call, unless the operator has
  explicitly turned on `hold.expiry_close_released` (exposed as `expiry_close_hold_enabled` on
  `/admin/settings`, inverted naming documented inline in `admin.py`: `true` = held, the default).
  Once released, a close already routes through the real chase engine
  (`chase_order(..., intent="close")`) — this was true of `close_positions()` before this change
  and needed no new wiring.
- **Paper/sim dry-run generalized too.** `_paper_expiry_close`/`_sim_expiry_close`
  (actions_paper.py/actions_sim.py) now default to both exchanges when `params.exchange` is
  absent (matching the live action), while an explicit `exchange` param still restricts to one
  (old shape, or a test).
- Tests: `test_expiry_day_and_preclose_agents.py` (agent shape, flat conditions, cooldown,
  same-day no-suppression regression), `test_expiry_position_breakdown.py` (wording rules, masked
  accounts, lot-size fallback, multi-account never pooled, degraded-scan note).
- **Known coupling with the pre-existing background 09:20 IST task** (`background.py`,
  prod-only, `ExpiryEngine.run()`, `_start_offset_h`=2h before close) — intentionally left
  running side-by-side, unchanged. It also routes through `close_positions()`'s same hold check,
  so the two mechanisms cannot double-place a live order even though they can both independently
  attempt the same close on the same day; whichever runs first stages (or places, once released)
  the HELD row, and the second finds nothing new to close.
- Invariant: alert and auto-close identification can never disagree — they call the identical
  `ExpiryEngine.scan_positions()`. A root's "unnetted"/"ITM" classification is never duplicated
  into a second, independent implementation.

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
| Expiry pre-close alert + auto-close (one agent each, both exchanges) | `backend/api/algo/agent_engine.py` (`expiry-day-preclose-alert`/`expiry-day-itm-auto-close`, `_v2_expiry_position_breakdown`) + `backend/api/algo/actions_live.py:_action_live_expiry_auto_close` + `backend/api/algo/grammar_tokens/metric_is_expiry_preclose_{alert,close}_window.py` |
| Expiry close hold/release | `/admin/settings` → `expiry_close_hold_enabled` (inverted: `true`=held, default) |
| Dhan/Groww margin enforce | `/admin/settings` → `preflight.enforce_margin_{dhan,groww}` |
| Add MCP tool | `backend/mcp/kite_server.py` @app.tool() |
| Tune MCP audit | `/admin/settings` |
| Update macro data | `backend/config/backend_config.yaml` |
| Day P&L formula | `backend/api/algo/pnl_math.py` (`current_total_profit`, `baseline_diff_day_pnl`) + `frontend/src/lib/data/nav.js` (`baseDayPnlForPosition`, `currentTotalProfit`) |
| Day P&L rollup stores (positions) | `frontend/src/lib/data/portfolioStore.svelte.js` (account/symbol/total rollups); per-row `prev_settlement_pnl` backfilled in route-level responses |
| Baseline query (Day P&L snapshot path) | `backend/api/routes/positions.py:_SESSION_ANCHOR_CUTOFF_TS_SQL` + `_fetch_baseline_pnl_map` (derives 08:00 IST session boundary from `captured_at`, not `date` column; handles holdings-sold CNC split via `kind IN ('positions','holdings')`) |
| Market daily window / WebSocket lifecycle | `backend/api/background.py` + `backend/brokers/kite_ticker.py` |
| Market open/closed check | `backend/shared/helpers/date_time_utils.py:_segment_is_open` (YAML config) or `is_market_open` (gate-shaped dicts) |
| 05:35 IST market-status alert (Telegram always; ntfy only when a segment is open) | `backend/api/background.py:_ds_tick_market_status_report` (trigger) + `backend/api/algo/actions_live.py:_action_live_send_market_status` (handler, registered in `_NOOP_ACTION_HANDLERS` in `backend/api/algo/actions.py`) + `backend/config/grammars/agent_grammar.yaml` (`send_market_status` token) + `market-status-report` built-in in `backend/api/algo/agent_engine.py` |
| MCX evening-closed holiday handling | `backend/brokers/broker_apis.py:_fetch_mcx_evening_closed_dates` + `_upsert_mcx_evening_closed_sessions_coro` |
| Pulse Chg% / Day P&L % denominator | `frontend/src/lib/data/pulseUnified.js` (`_prevCloseSsot`, `finalizeRows`) |
| Postback subscribe new instrument | `backend/api/routes/orders.py:_subscribe_filled_pairs()` — canonical helper resolves (symbol, exchange) to token and calls `subscribe_with_sym()` (NOT bare `subscribe()`, which silently drops ticks). Wired into: Kite/Dhan/Groww postbacks, admin reconcile sweep, open-order watchdog, chase terminal-fill detection |
| `cancel_all_orders` CLI statement | `frontend/src/lib/command/grammars/agents.js` (bare-statement compile kind) + `backend/api/routes/orders.py` (`POST /api/orders/cancel-all`) + `backend/api/algo/actions_live.py` (`cancel_all_orders_scoped` shared helper) |
| F&O order qty convention | `backend/api/routes/orders_place.py:_ticket_validate_input` + `frontend/src/lib/order/orderTicketSubmit.js` |
| NAV breakdown | `frontend/src/lib/data/nav.js` + `backend/api/algo/nav.py:compute_firm_nav` |
| LTP-override scaffold | `backend/api/helpers/ltp_patch.py` |
| Mask account in text | `backend/shared/helpers/utils.py:mask_account_in_text` |
| Mask account in log records (all handlers + `log_events`) | `backend/shared/helpers/log_store.py:_AccountMaskFilter` (wired in `ramboq_logger.py:get_logger`) |
| Account mask registry incl. numeric Dhan client_id | `backend/shared/helpers/utils.py:register_accounts(accounts, client_ids=)` |
| Snapshot freshness check config / DSN | `scripts/check_stale_snapshots.py` (`yaml.safe_load`, `_build_dsn` quoting) |
| Ticker idle state on non-trading days | `backend/api/background.py:_ticker_calendar_refresh_idle_status` + `frontend/src/lib/data/brokerHealthState.js:brokerAccountDisplayState` |
| Closed-hours broker-call gating (trail-stop, OCO, order-poll sweeps) | `backend/api/background.py:is_any_segment_open_cached` (fails open) + `_resolve_market_status_today` (once-per-IST-day cache); call site `backend/api/algo/order_poll.py:_task_order_poll` |
| Ticker nightly WS stop/restart, conn_service (DB-calendar driven: MCX close+1 min / next open) | `backend/brokers/service/app.py:_task_daily_ticker_cycle` (+ `_restart_ticker_for_new_day`) + `backend/brokers/service/calendar_gate.py:resolve_mcx_gate_bounds` / `ticker_quiet_window_now` (reads `exchange_schedule`) + `backend/brokers/kite_ticker.py:TickerManager.disconnect_keep_reactor` |
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
| Order reconcile / broker-truth check | `backend/api/routes/orders_helpers.py:get_broker_order_truth` / `apply_order_truth` (the accessor) + `backend/api/algo/order_poll.py` (`reconcile_row_and_followup`, `_task_order_poll`) |
| Update feature spec | `docs/specs/<NAME>_SPEC.md` |
| Update operator guide | `docs/guides/<NAME>_GUIDE.md` |
| Update architecture doc | `docs/DESIGN_GUIDE.md` → regenerate with `python3 docs/generate_pdf.py` |
| Add audit snapshot | `docs/audits/AUDIT_<TOPIC>.md` |
| Update ops runbook | `docs/deployment.md` |
| Chase Kill / CANCEL_FAILED retry | `backend/api/routes/orders.py:kill_chase` + `chase.py:mark_order_killed` |
| Template persist-failure hold (in-memory) | `orders_place.py:_TPL_ATTACH_PERSIST_PENDING` + `_opp_handle_persist_failure` |
| Scale-out ladder OCO pairing | `template_attach.py:_ta_detect_scale_sl_group` + `background.py:_oco_process_group_entry` |
| MCX real per-contract expiry (live freeze filter) | `expiry_freeze.py:_mcx_future_real_expiry` |
| generic_notice body (plain text, no origin line — see "Alert message format") | `event_agents.py:_render_deploy_sync` (pass-through) + producer `msg` in `background.py` |
| Alert channel set for a seeded agent | `backend/api/algo/alert_routing.py` (`_PER_SLUG_OVERRIDES`, `_TIER_DEFAULTS`, `resolve_channels`) — not the agent's spec |
| Deploy-sync Telegram channel (dedicated bot/chat) | `event_agents.py:_send_telegram_deploy_html` + `CHANNELS["telegram_deploy"]` + `alert_routing.py` (`deploy-sync-alert` row); secrets `telegram_bot_token_deploy` / `telegram_chat_id_deploy` |
| Deploy script Telegram ping (no ntfy, no email) | `webhook/notify_deploy.py` |
| Forced email for action-required alerts | `event_agents.py:_apply_action_required_email_gate` (applied in `_dispatch_record_channels`) |
| Alerts tab: merge of `alert`-tagged log rows | `backend/api/routes/alerts.py:_build_log_alert_stmt` / `_log_event_to_alert` / `alert_history` |
| Plain-text Telegram send (no `parse_mode`) | `backend/shared/helpers/alert_utils.py:_send_telegram` / `_send_telegram_info` |
| Origin line on every alert | `backend/shared/helpers/alert_utils.py:format_notification_header` |
| Agent DSL grammar (compile / decompile) | `frontend/src/lib/command/grammars/agents.js` + `consoleDispatch.js` (`updateConsoleAgent` sticky-false) |
| Saved-agent preamble execution (every-cycle / once / cooldown) | `backend/api/algo/agent_engine.py:_cycle_run_preamble` |

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
