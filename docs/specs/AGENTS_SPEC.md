# Agents Engine Specification

Single source of truth for the agent evaluation, alerting, and action system. Defines
the rule lifecycle from condition evaluation through delivery and side-effect execution.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/algo/agent_engine.py` · `backend/api/routes/agents.py` · `backend/api/routes/alerts.py` · `backend/api/models.py` · `backend/shared/helpers/alert_utils.py`

---

## Contents

1. [Four-Term Model](#1-four-term-model)
2. [Agent Lifecycle](#2-agent-lifecycle)
3. [Condition Tree Evaluation](#3-condition-tree-evaluation)
4. [Supported Metrics](#4-supported-metrics)
5. [Alert Delivery](#5-alert-delivery)
6. [Cooldown and Suppression](#6-cooldown-and-suppression)
7. [Agent Suppression](#7-agent-suppression)
8. [BUILTIN_AGENTS Seeding](#8-builtin_agents-seeding)
9. [Grammar Tokens and Registry](#9-grammar-tokens-and-registry)
10. [run_cycle() Timing](#10-run_cycle-timing)
11. [Test Coverage Map](#11-test-coverage-map)
12. [Automated Order Hold and Release](#12-automated-order-hold-and-release)
13. [Agent CLI Grammar](#13-agent-cli-grammar)

---

## 1. Four-Term Model

Agents follow a discrete pipeline:

| Term | Layer | Definition |
|---|---|---|
| **Agent** | Config | Rule specification: condition tree + notify + actions |
| **Alert** | Event | Runtime trigger: agent matched its condition tree at a point in time |
| **Notify** | Delivery | Channel routing: where the alert reaches (Telegram, Email, WebSocket, Log) |
| **Action** | Side-Effect | Executable response: place order, modify, cancel, close position |

Flow: `Agent.condition_tree evaluated` → `Alert row written to agent_events` → 
`Notify channels dispatched` → `Action handlers executed`.

---

## 2. Agent Lifecycle

### Status transitions

| Status | Condition | Transitions |
|---|---|---|
| **inactive** | Agent disabled by operator | → active (via PUT /activate) |
| **active** | Conditions being checked each cycle | → triggered (condition matches) |
| **triggered** | Condition matched; alert sent | → active (cooldown expires) |
| **completed** | Lifespan expired (one_shot, n_fires, until_date) | terminal |
| **expired** | Lifespan limit reached | terminal |

### Lifespan types

| Type | Meaning | Terminal |
|---|---|---|
| **persistent** | Fires indefinitely (default) | No |
| **one_shot** | Fires once then completes | Yes (after 1 fire) |
| **n_fires** | Fires up to N times | Yes (after max_fire_count) |
| **until_date** | Fires until lifespan_expires_at UTC | Yes (after date passes) |

---

## 3. Condition Tree Evaluation

### Grammar v2 (production)

Condition trees are JSON with `all`, `any`, `not` combinators over atomic leaves.

```json
{
  "all": [
    {"metric": "pnl", "scope": "positions", "op": "<=", "value": -50000},
    {"metric": "pnl_pct", "scope": "holdings", "op": "<", "value": -5.0}
  ]
}
```

**Evaluation rules**:
- `all`: returns True if ALL children evaluate True
- `any`: returns True if ANY child evaluates True
- `not`: returns True if the child evaluates False (singular operator)
- **Leaves** (`metric`, `scope`, `op`, `value`): atomic condition
- **Propagation**: Tree walk returns single boolean; agent fires if True

### Scope types

- **positions**: per-position or aggregate (`TOTAL`)
- **holdings**: per-holding or aggregate (`TOTAL`)
- **account**: specific account_id
- **all_accounts**: consolidated across all accounts

---

## 4. Supported Metrics

### Point-in-time (snapshot at evaluation)

| Metric | Scope | Definition | Units |
|---|---|---|---|
| `pnl` | positions, holdings, account | Unrealised P&L at current LTP | Currency |
| `pnl_pct` | positions, holdings, account | P&L as % of used margin (`util debits`). Falls back to `net` margin (available margin) when `util debits = 0`. Returns `None` (leaf skipped) when both are zero. | Percent (0–100) |
| `day_pct` | positions, holdings, account | Today's intraday move | Percent (0–100) |

### Rate-of-change (per minute over window)

| Metric | Scope | Definition | Window | Units |
|---|---|---|---|---|
| `pnl_rate_abs` | positions, holdings, account | dP&L / dt absolute | `alert_rate_window_min` | Currency/min |
| `pnl_rate_pct` | positions, holdings, account | d(P&L%) / dt | `alert_rate_window_min` | Percent/min |

Returns `None` until ≥2 samples are accumulated in the rolling window — no alert fires
during the ~5 min baseline accumulation period at session start.

### Rolling statistics (over `alert_rate_window_min`)

| Metric | Scope | Definition | Units |
|---|---|---|---|
| `mean` | — | Mean P&L over window | Currency |
| `max_drawdown` | — | Largest peak-to-trough within window | Currency |
| `stdev` | — | Standard deviation of P&L | Currency |
| `range` | — | (max - min) within window | Currency |

### Expiry-aware (derivatives only)

| Metric | Scope | Definition | Units |
|---|---|---|---|
| `is_itm` | F&O position | True if in-the-money at spot | Boolean |
| `is_ntm` | F&O position | True if near-the-money (±1 strike) | Boolean |
| `days_until_expiry` | F&O position | Days remaining on contract | Days (int) |

---

## 5. Alert Delivery

### Dispatch skip_channels parameter

`skip_channels: frozenset` — when provided, channels in this set are skipped even if
enabled. Used when a richer alert channel (e.g. `inapp`) has already handled
notification and simpler channels (e.g. `telegram`) should be suppressed.

### Channels

| Channel | Trigger | Recipient | Format |
|---|---|---|---|
| **telegram** | Agent fire | Group chat ID | Code block; [SIM]/[PAPER] prefix |
| **email** | Agent fire | alert_emails list | HTML table; RamboQuant prefix |
| **websocket** | Agent fire | Logged-in operator | JSON; real-time in UI |
| **log** | Any event | Application logs | Structured line with agent slug |

### Message composition

**Dual-timezone display** via `timestamp_display()`—shows IST + UTC in every message.

**Telegram subject**:
```
RamboQuant Agent: <agent_long_name> [SIM/PAPER/—]
```

**Email subject**:
```
RamboQuant Agent: <agent_long_name>
```

**Body header**: the first line of every message (Telegram, ntfy, email) is `Agent: {name} (#{id})`
or `Agent: manual`, built by `format_notification_header()`. Subjects above are unchanged. Origin
is never placed later in the body. Manual orders (no agent) read `Agent: manual` on every alert.

**Content**:
- Matched condition summary (free-text from condition tree evaluation)
- P&L snapshot (positions pnl, holdings pnl, day_pct if available)
- Scope and account masking (account IDs replaced with rambo-xxxx suffix)

---

## 6. Cooldown and Suppression

### Alert cooldown

After an agent fires, it enters a cooldown window (default 30 min, tunable via
`alerts.cooldown_minutes` setting in `/admin/settings`, which falls back to
`alert_cooldown_minutes` in backend_config.yaml). Re-evaluation during cooldown:
- Condition still checked on every cycle
- Alert NOT dispatched until cooldown expires
- Lifespan counter still increments (fired = true even if cooldown prevented send)

### Baseline offset

Loss agents may require a minimum wait after open (default 15 min, tunable via
`alert_baseline_offset_min`) before the first check fires. Prevents spurious
false positives from intraday churn.

### Rate window

Rolling-window metrics compute over `alert_rate_window_min` minutes (default 10 min).
Window samples are capped at 200 entries per (section, scope) bucket to bound memory.

---

## 7. Agent Suppression

**Loss-agent suppression rule**: When two loss agents fire simultaneously (same cycle),
the agent with the LARGER absolute loss suppresses the other. Suppressed agent logs
a `cooldown` event but does NOT dispatch an alert.

**Suppression storage**: Module-level `_V2_LAST_ALERT[agent_slug]` dict tracks
the last fired P&L (`pnl`, `pct`) and timestamp per agent. Winner's state is stored;
suppressed agent's is not updated.

**Daily reset**: Suppression state is cleared each new trading day (checked via
`_maybe_reset_v2_state(today)` on every cycle) so yesterday's history doesn't
influence today's fire order.

---

## 8. BUILTIN_AGENTS Seeding

### Seeded at startup

Fourteen builtin agents ship as hardcoded rows (`BUILTIN_AGENTS` in `agent_engine.py`):

Loss agents (6):
- `loss-positions-acct` (high tier, 30-min cooldown, status=inactive)
- `loss-rate-acct` (critical tier, 10-min cooldown, status=inactive)
- `loss-positions-total` (critical tier, 30-min cooldown)
- `loss-margin-low` (high tier, status=inactive, disabled)
- `loss-funds-negative` (critical tier)
- `loss-pos-total-auto-close` (critical tier, status=inactive, Ships INACTIVE)

Expiry-day agents (3):
- `expiry-day-positions-alert` (high tier, status=inactive, Ships INACTIVE)
- `expiry-day-equity-itm-auto-close` (critical tier, status=inactive, fire_at=15:15,
  Ships INACTIVE)
- `expiry-day-commodity-itm-auto-close` (critical tier, status=inactive, fire_at=23:00,
  Ships INACTIVE)

Expiry risk agents (2):
- `expiry-nfo-risk-alert` (high tier)
- `expiry-mcx-risk-alert` (high tier)

Market lifecycle agents (2):
- `market-open-nse` (info tier, fire_at=09:15)
- `market-preclose-mcx` (info tier, fire_at=23:00)

Manual agent (1):
- `manual` (audit trail only)

### Orphan pruning

On startup, database rows with `is_system=True` that are NOT in `BUILTIN_AGENTS`
are deleted (orphans from removed builtin rules). Non-system agents are never pruned.

### Seed status guard

A builtin agent whose `description` contains the phrase "Ships INACTIVE" (case-sensitive
substring match) must always seed or resync with `status="inactive"`, regardless of the
seed dict's own `status` field. The `_ae_guard_seed_status()` function
([`agent_engine.py:1579–1600`](../../backend/api/algo/agent_engine.py#L1579-L1600)) enforces this:

- On insert (new agent): the guard checks at row-build time and forces any active status to
  "inactive" if the description includes the phrase.
- On sync (existing agent): the guard checks again at startup, so every process restart
  converges existing rows to "inactive" if the description says so.
- Failed guard applies an ERROR log and continues (fail-safe, no crash).

This protects four destructive auto-close agents from accidentally being enabled at seed
time: `loss-pos-total-auto-close`, `expiry-day-positions-alert`,
`expiry-day-equity-itm-auto-close`, and `expiry-day-commodity-itm-auto-close`. Operator
activations of these agents are reverted on the next process restart, so enable them only
after understanding they are destructive (initiate trades or close positions without
operator confirmation).

### Editing loss agents

Loss-agent conditions are editable live via `/automation` page. Edition does NOT
invalidate the current run — changes apply on the next `run_cycle()`.

### Agent kind, tier, and topic fields

**`kind`** — selects dispatch pipeline. Written via `POST /api/agents` with 
write-only vocabulary: `"cycle"` (default, threshold agents), `"threshold"` 
(alias for `"cycle"` normalized via `_age_normalize_kind()`), `"event"` 
(log-driven agents). Stored as-written in the `kind` column. Reads via 
`GET /api/agents/{slug}` always return `"cycle"` or `"event"` (never 
`"threshold"`). `kind` is immutable after creation — attempted changes via 
`PATCH` return 400 with detail "kind cannot be changed after creation". 
The two kinds have entirely different dispatch pipelines (cycle agents via 
`run_cycle()`, event agents via `event_agents.dispatch_rows()`), so 
post-creation flipping would be unsafe.

**`tier`** and **`topic`** — alert hierarchy / noise-reduction fields. Both 
default to `"medium"` and `"general"` respectively if unset on creation. 
Mutable on `PATCH` (no immutability gate).

**Renderers catalog** — `GET /api/agents/renderers` returns a list of 
available renderers for event agents: each entry has `key` (the renderer ID), 
`label` (human-readable name derived from key), and `description` (the 
renderer function's own one-line docstring, empty if none). Sourced from 
`backend/api/algo/event_agents.RENDERS` dict at request time (no caching). 
Frontend event-agent builder uses this endpoint instead of hardcoding the 
renderer list, making new renderers available immediately after code changes.

**Event-agent validation** — `POST` and `PATCH` requests with `kind="event"` 
are validated via `event_agents.validate_seed_spec()` instead of the 
threshold-agent path. On `PATCH`, the MERGED spec (existing row + supplied 
fields) is re-validated, so partial updates still get full validation. 
Validation failure returns 422 with the error list.

**Threshold/cycle-agent condition validation** (2026-10, Sprint 3) — `POST` 
and `PATCH` requests for EVERY kind (not just `event`) now also validate 
`conditions` via `_age_validate_threshold_conditions()`, a kind-agnostic 
companion that calls the same `agent_evaluator.validate()` the optional 
`/validate-condition` pre-check endpoint already used — an unknown or 
malformed `metric`/`scope`/`op` token returns 422 with the error list. On 
`PATCH`, the MERGED `agent.conditions` is re-validated (same merged-spec 
rule as the event-kind path above). A `conditions` value that isn't yet 
grammar-tree-shaped (e.g. an empty `{}` placeholder — checked via 
`agent_engine.is_grammar_tree()`) is left unvalidated, matching 
`/validate-condition`'s own leniency — only a tree that looks like a real 
condition attempt gets its tokens checked. Before this, only `/validate-
condition` and the `/interpret ai create` CLI path ever ran this check; an 
operator saving a threshold agent through the ordinary form (or the AI-
draft flow, which only shows validation errors advisorily) could persist a 
typo'd token with 200/201 and the agent would silently never fire.

**Threshold/cycle-agent action validation** (2026-10, Sprint 1 of the CLI-grammar
effort) — the `actions` array gets the same treatment `conditions` already does
above, via a sibling `_age_validate_action_entries()`: for every entry, the action
type must be a registered token (`grammar.is_known_action_type()`) and every
`required: true` key in its `params_schema` must be present in that entry's
`params` dict — a 422 with the error list otherwise. Gated on `kind != "event"`
(event-kind `actions` entries are a structurally different `{"type": "render",
"render": <key>}` shape, already checked against `event_agents.RENDERS` by the
event-spec validator). On `PATCH`, the MERGED `agent.actions` is re-validated, same
rule as conditions. An empty `[]` actions list (an alert-only agent) is left alone.
Before this, the `actions` array was completely unchecked at save time for
threshold/cycle agents — an unknown action type or a missing required param saved
with 200/201.

---

## 9. Grammar Tokens and Registry

### Token registration

`grammar_tokens` table holds symbols recognized in agent condition text:
- **System tokens** (seeded at boot): metric names, operators, scope keywords
- **Custom tokens** (via `POST /admin/tokens`): user-defined symbol aliases

### Grammar reload

`POST /api/admin/grammar/reload` triggers synchronous `GrammarRegistry.reload()`—
re-parses all custom tokens and rebuilds the evaluator state tree. Called after
editing a custom token to apply changes to live evaluations.

### Resolver-import validation (Wave 2, custom-token CRUD safety boundary)

`POST /api/admin/grammar/tokens` (`create_token`) and `PATCH /tokens/{id}`
(`patch_token`, non-system rows only) validate a non-empty `resolver` string by
actually importing it (`backend/api/routes/grammar.py:_validate_resolver_importable`,
delegating to `grammar_registry._import_dotted`) BEFORE the DB write. A resolver
that fails to import — nonexistent module, nonexistent attribute, or a malformed
dotted path — is rejected with `400` and the row is never created/mutated.

This closes a gap that previously let an unimportable resolver be saved
successfully: `GrammarRegistry.reload()`'s per-row loader treats an import
failure as a silent skip (WARNING + counted in `skipped`), not a hard error, so
the token would exist in the catalog but never actually dispatch. `resolver=None`
/ empty is always accepted — many real token kinds legitimately carry no resolver
(templates, the documented `resolver: null` dead-stub convention, several
format/channel tokens with no backing implementation). System tokens
(`is_system=True`) are unaffected — `patch_token` restricts them to `is_active`
toggles only and never reaches this validation path.

No new DB column was added for this — a custom token's `is_system=False` flag is
already sufficient provenance: `seed_grammar_tokens()`'s own existing-rows query
filters on `is_system == True`, so a custom token is structurally never read,
updated, or deleted by a bulk YAML reseed.

### AI-drafted grammar tokens (Wave 3, /admin/mcp authoring flow)

`POST /api/mcp/draft-token` (`backend/api/routes/lab.py:McpController.draft_token`,
guarded by the same `manage_grammar_tokens` capability Wave 2's own CRUD endpoints
require) drafts a candidate `(resolver .py source, test .py source, params_schema,
description)` bundle for a NEW metric, scope, notify-channel, or action token from
an operator's natural-language prompt. The drafting logic is
`backend/api/algo/agent_ai.py:draft_token_from_prompt` — the token-drafting sibling
of `draft_agent_from_prompt` (the agent-drafting pipeline above it in that file),
reusing the same `google.genai` call pattern and the same "draft-only, caller
persists" split (this module does no DB I/O; the route queries
`_td_existing_token_names()` and passes it in).

**Hard safety boundary — read before extending this surface**: this endpoint and
`draft_token_from_prompt` NEVER write a file, NEVER import or execute the drafted
`resolver_source`/`test_source`, and NEVER call the Wave 2 registration endpoint.
The response is plain JSON — a reviewable diff, not a live artifact. A human
(operator reviewing `/admin/mcp`, or a Claude Code session) writes the two files,
runs the new test, and takes it through the normal `/impl`→`/ddev`→`/dprod` deploy
pipeline; only AFTER that deploy does the EXISTING Wave 2 endpoint
(`POST /api/admin/grammar/tokens`, which already validates `resolver` dotted-path
importability) register it live. The AI-sourced path and the hand-written path
converge on the exact same two steps — there is no separate "AI tokens" mechanism.

**Request** (`TokenDraftRequest`): `prompt: str`, plus OPTIONAL identity hints
`grammar_kind` / `token_kind` / `token`. When all three hints are given, a cheap
collision/unsupported-kind check runs BEFORE any LLM call (same ordering
precedent as Wave 2's duplicate-check-before-import-validation), and the hints
OVERRIDE whatever identity the LLM itself proposes in its response — an operator-
chosen name can never drift from what the LLM returns.

**Response** (`TokenDraftOut`, always HTTP 200 — `errors` carries the failure
signal, never an HTTP error status, so the UI can always render what was
proposed): `draft: dict`, `errors: list[str]`, `warnings: list[str]`,
`why_summary: str`. `draft` is `{}` only on an early hard failure (empty prompt,
GenAI disabled, LLM unavailable/empty/invalid-JSON, a pre-LLM hint
rejection, or a non-dict LLM `draft` field). Otherwise `draft` is always
populated — even when `errors` is non-empty — with:
`grammar_kind`, `token_kind`, `token`, `description`, `params_schema`,
`resolver_source`, `resolver_function_name`, `resolver_module_path`,
`resolver_file_path`, `resolver` (the exact dotted path a human would POST to
Wave 2's `create_token` once the file is deployed — `{resolver_module_path}.
{resolver_function_name}`, or `None` when `resolver_source` didn't parse to
exactly one function), `test_source`, `test_file_path`. `GrammarTokenCreate`
has no `forbid_unknown_fields` — a human can POST the `draft` dict to Wave 2's
endpoint largely verbatim; the extra `resolver_*`/`test_*` keys are simply
ignored by that schema.

**Hints are all-or-none** — `grammar_kind`/`token_kind`/`token` are ignored
entirely unless all three are given (no precheck, no identity override). A
partial set (e.g. only `grammar_kind` + `token_kind`) is treated exactly as if
no hints were supplied.

**Whitelisted token kinds — and which ones actually go live after Wave 2's
registration endpoint** — deliberately only four kinds with a uniform-enough,
statically-checkable call signature: `condition/metric` (`(ctx, row)`),
`condition/scope` (`(ctx)`), `notify/channel` (`(title, body, tg=None,
priority=None)`), `action/action_type` (`async def fn(ctx, params: dict)`).
`condition/operator` resolvers live in code (`grammar.py:OPERATORS`), never
file-based. `notify/format` signatures vary per formatter with no single
contract to validate against yet. `template`/`log_tag` tokens carry no resolver
at all. Widening this set is a future decision.

Of these four, only `metric` and `scope` genuinely converge on Wave 2's "write
files → deploy → register" promise — they dispatch live through
`GrammarRegistry.metric()`/`.scope()`, confirmed by Wave 2's own reload-parity
test. `channel` and `action_type` do NOT: real notification dispatch for the
`kind='event'` agent pipeline runs through `event_agents.py`'s hardcoded
`CHANNELS` dict, and real action dispatch runs through `actions.py`'s explicit
`_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS` tables — `is_known_action_type()`
(the gate deciding whether an agent's `DO` clause may reference an action type)
reads the code-level `SYSTEM_TOKENS` catalog, not the DB row. `REGISTRY.channel()`
and `REGISTRY.action()` both have zero real callers anywhere in the backend
today. A channel/action draft is still generated — the extra wiring is a normal
part of the same human-reviewed deploy that creates the file — but
`draft_token_from_prompt` appends a FIXED, kind-level warning
(`_TOKEN_KIND_EXTRA_WIRING_WARNING`) naming exactly what else is needed, so the
`/admin/mcp` UI never implies these two kinds are a drop-in the way metric/scope
are.

**Resolver module/file path is derived server-side, never trusted from the
LLM** — `_td_derive_paths(grammar_kind, token_kind, token)` computes
`backend.api.algo.grammar_tokens.{prefix}_{token.replace('.', '_')}` from the
whitelist prefix map + the server-verified token name. A drafted resolver's
eventual dotted path is a pure function of its own verified identity — the LLM
cannot point a "metric" at an already-importable, already-reviewed function
elsewhere in the codebase (e.g. a broker-order action in `actions_live.py`) by
naming it in a `resolver` field, because this module never reads or trusts such
a field in the first place.

**Static validation, never execution** — `resolver_source` and `test_source` are
each `ast.parse()`d (a syntax error is a hard `errors` entry, never a silent pass
and never a raised exception — the draft is still returned so the operator sees
what the LLM proposed). The resolver module must define exactly one top-level
function (matching the real one-file-one-function convention under
`grammar_tokens/`); the test module must define at least one (a real pytest
module legitimately has several `test_*` functions, unlike a resolver). The
function's signature is checked against the kind's documented shape (e.g. a
metric resolver with fewer than two parameters, or an action resolver not
declared `async def`, is a hard error). An AST walk flags — as a WARNING, never a
hard error, since the human reviewer decides — an import of
`backend.brokers`/`backend.api.algo.actions_live`/`subprocess`/`os`/`socket`/
`requests`/`httpx`/`urllib`, or a call to `exec`/`eval`/`compile`/`__import__`/
`open`. `params_schema` gets a loose JSON-object shape check only. A separate
WARNING-only check (`_td_check_test_imports_resolver`) flags a `test_source`
that does not import the resolved function name from the resolved module path
— catches the one case the hinted-identity override can't fix itself: the
LLM's own `test_source` import line was written against its ORIGINAL proposed
name, before an operator hint renamed the token.

**Collision check covers active AND inactive rows** — `_td_existing_token_names()`
queries every `(grammar_kind, token_kind, token)` triple in `grammar_tokens`
regardless of `is_active`, matching Wave 2's own `create_token` uniqueness
pre-flight (which has no `is_active` filter either). The comparison is
case-insensitive, matching `buildCatalog()`'s own lowercase/last-write-wins
convention.

Tests: `backend/tests/test_agent_token_draft.py` (the drafting/validation
pipeline), `backend/tests/test_mcp_draft_token_route.py` (the route contract —
guard present, `existing_names`/hints threaded through, `asyncio.to_thread`
offload so a slow Gemini call never blocks the event loop this process also
uses for chase/postback handling).

### Condition parsing

v2 grammar evaluator (`agent_evaluator.py`) consumes JSON condition trees and
tokens table to resolve free-form condition strings into structured leaves
(`metric`, `scope`, `op`, `value`).

### Parameterized call-syntax metric tokens (2026-10, Sprint 2+3)

Rolling-window metrics also accept function-call syntax for an arbitrary
window in minutes, e.g. `mean_pnl(45)`, `max_drawdown_pnl(90)`,
`stdev_pnl(120)`, `range_pnl(15)`, `mean_day(20)`, `max_drawdown_day(180)`,
`max_drawdown_pnl_pct(30)` — in addition to the fixed tokens
(`mean_pnl_30m`, `mean_pnl_1h`, etc.), which remain permanent shortcuts for
the common windows. `GrammarRegistry.metric()`/`.scope()`/`.channel()`/
`.fmt()` fall back to parsing an unresolved token as a single
`ast.parse(..., mode="eval")` Call expression; on an exact base-name +
arg-count match against a `params_schema`-bearing factory row, the bound
result is cached forever under the literal call string. Only a single bare
positive numeric literal argument is accepted per declared param — no
expressions, names, strings, booleans, keyword args, or non-positive
values (a window of 0 or less would resolve to a real callable that then
always silently evaluates to `None`, so it's rejected at parse time
instead). Any mismatch resolves to `None`, identical to an unknown token —
surfaced by `agent_evaluator.validate()` as `"unknown metric token
'<token>'"`.

---

## 10. run_cycle() Timing

The agent engine evaluates all active agents on every performance refresh cycle.

### When it runs

1. Background task `_task_performance` fires every 5 min during market hours
2. On each fire, calls `agent_engine.run_cycle(summary_positions, summary_holdings, funds_df)`
3. Engine walks all `status='active'` agents, evaluates conditions, dispatches alerts
4. Async: all alert channels are sent in parallel (Telegram + Email + WebSocket + Log)

### Market-hours gate

`run_cycle()` respects the `schedule: market_hours` gate on per-agent rules:
- If agent has `schedule='market_hours'` AND any segment is closed → skip evaluation
- All other agents evaluate regardless of market state

### No stale data

At evaluation time, `sum_positions`, `sum_holdings`, `funds_df` are all fresh from
broker or daily_book (closed hours). Rate metrics read from `alert_state['pnl_history']`,
which was populated by `_update_pnl_history()` on the same cycle. No staleness edge case.

---

## 11. Test Coverage Map

### Backend — core logic

- **Condition evaluation**: all/any/not combinators, scope matching, operator precedence
- **Metric hydration**: point-in-time (pnl, pnl_pct, day_pct) from live data
- **Rate metrics**: window samples append to history, oldest trimmed at cap
- **Cooldown**: subsequent fires within window produce `cooldown` event, no alert
- **Suppression**: two loss agents same cycle, larger fires, smaller logged as suppressed
- **Lifespan transitions**: one_shot → completed, n_fires increments counter, until_date checks expiry
- **Alert dispatch**: channels sent in parallel, failed channel doesn't block others
- **Builtin sync**: orphan pruning removes deleted system agents, seeding preserves existing

### Backend — integration

- **Grammar reload**: custom tokens applied to next evaluation
- **Activity surface**: AlertEvent rows carry correct conditions_summary + channels_sent
- **Agent history**: `/api/agents/{slug}/events` returns sorted alerts with trigger conditions
- **Timezone display**: IST + UTC shown in all messages, no duplicates

### Gaps

- Edge case: agent fires during reloading interval (race condition with grammar update)
- Missing: suppression cross-check between expiry-auto-close and loss agents
- Missing: rate-metric history persistence across restarts (in-memory only)

---

## 12. Automated Order Hold and Release

Agents and background tasks that delay or reject an order placement ("hold") use a
category-based registry to re-release it later. Adding a new hold category (a fourth or
beyond) is a registration in code, not a change to dispatcher routes.

### Hold categories and release handlers

| Category | Meaning | Release handler | When |
|---|---|---|---|
| `expiry_close` | Close position held until operator releases it (cutoff controls creation timing, not release) | `release_held_order` | Operator clicks Release |
| `template_exit` | GTTs held until operator confirms | `release_template_exit` | Operator clicks Release |
| `agent_order` | Resume order held after repeated rejections | `release_repeated_rejection_hold` | Operator clicks Release |
| (unregistered) | Fallback default | `release_held_order` | Any unregistered category |

Every held order is persisted as an `AlgoOrder` row with a JSON `hold_json`
record carrying `{"category", "reason", "price_policy", "override", "held_at"}`.
Status persistence is category-dependent: `expiry_close` and `agent_order` set
`status="HELD"`, while `template_exit` leaves the parent order at its original
status (e.g. `"FILLED"`) with only `hold_json` indicating pending exit-attach.
The operator releases via `/api/orders/held/{id}/release` route
([`orders_release.py`](../../backend/api/algo/order_release.py)), which
dispatches through `get_release_handler(category)` to the registered handler.
Cancelling via `/api/orders/held/{id}/cancel` abandons the hold instead.

### Adding a new hold category

The 4-step recipe from
[`order_release.py`](../../backend/api/algo/order_release.py#L1-L43):

1. Add a member to `HoldCategory` enum in
   [`order_hold.py:10–13`](../../backend/api/algo/order_hold.py#L10-L13)
   (e.g., `MY_CATEGORY = "my_category"`).

2. Wherever your agent/task decides to hold instead of fire, call
   `record_held_order(category=HoldCategory.MY_CATEGORY, ...)` from
   [`order_hold_gate.py:44–75`](../../backend/api/algo/order_hold_gate.py#L44-L75)
   to persist the row.

3. Write an async release function matching one of the existing shapes:
   - `release_held_order()` if release means "check live position + quote, then place
     a NEW close order". Example: expiry closes.
   - `release_repeated_rejection_hold()` if release means "resume an already-in-flight
     order from its persisted state without re-checking". Example: chase orders held
     on repeated price rejections.

4. Register it in `_RELEASE_HANDLERS` dict at
   [`order_release.py:275–278`](../../backend/api/algo/order_release.py#L275-L278):
   ```python
   _RELEASE_HANDLERS[HoldCategory.MY_CATEGORY.value] = release_my_category_hold
   ```
   No route code to edit — `/api/orders/held/{id}/release` always dispatches through
   `get_release_handler()`.

### Critical safety guards

**Unregistered categories silently receive close semantics** — A category forgotten
in `_RELEASE_HANDLERS` falls through to `_DEFAULT_RELEASE_HANDLER` (`release_held_order`),
which applies position-matching and close-intent logic correct only for `EXPIRY_CLOSE`.
If your category does not represent "close an existing position", it MUST be registered,
or release will silently misbehave (wrong position check, wrong price, wrong order intent).

**`record_held_order()` hardcodes `engine="live"` / `mode="live"`** — The generic recorder
always writes live-mode rows. A category whose agent runs in paper/sim/replay/shadow mode
cannot use `record_held_order()` as-is for its hold — it would create a live-mode row,
and releasing it later places a REAL broker order on a simulated position. Such a category
needs its own hold-recording path that threads the agent's real mode through, or it must
only run in live mode. See the equivalent guard on `_fire_template_attach_on_fill` in
[`CLAUDE.md`](../../CLAUDE.md) for the same invariant applied elsewhere.

### Global switch and re-resolution

Every category reads a global boolean setting `hold.<category.value>_released` (e.g.
`hold.expiry_close_released`) via `held_for()` in
[`order_hold_gate.py:15–26`](../../backend/api/algo/order_hold_gate.py#L15-L26).
Setting `hold.<category>_released = true` globally releases all held orders of that
category (operator can bypass holds during testing).

The release handler function is re-resolved by name against module globals every call
(see [`order_release.py:282–288`](../../backend/api/algo/order_release.py#L282-L288))
to honor test monkeypatches. A patched handler is always what actually runs — the registry
stores function references at import time, but `get_release_handler()` fetches the
current value by name every call.

---

## 13. Agent CLI Grammar

### Operator-composed text statements (Sprint 4, unified Sprint [grammar-unify] 2026-10)

The agent editor's CLI tab enables authoring agents as single-line (or multi-line)
text statements instead of nested JSON trees. There is ONE shared statement
vocabulary — label assignment, `order(...)`/`place_order(...)`, `bracket(...)`,
`alert(...)`, `debug(...)`, `help(...)`, or any other real action call — and exactly
one thing that distinguishes "runs unconditionally, every cycle" from "runs only on
a condition match": whether a call sits inside an explicit `WHEN ... DO ...` pair or
not.

```
<preamble: any statement>* [WHEN <condition> DO <calls>]
```

**Preamble** — everything BEFORE `WHEN` (or, when there is no `WHEN` at all, the
entire statement) is the preamble: it runs unconditionally, with no introducer
keyword needed (`GIVEN` is removed from the grammar entirely — it carried no
semantic meaning even before this sprint, and the preamble is now self-delimiting
by position). A preamble with no `WHEN ... DO ...` at all is a complete, valid,
standalone statement on its own — the parser never requires or expects a trailing
conditional wrapper. Example (every line before `WHEN` is unconditional; only
`close_position(...)` is gated):

```
order(account="ZG0790", symbol="NIFTY25JULFUT", side="BUY", lots=2)
alert(telegram)
debug(x)
help(pnl)
x = 1
WHEN (x==1) DO close_position(account="ZG0790", symbol="NIFTY25JULFUT")
```

**`WHEN ... DO ...`** — a single, paired, optional conditional wrapper. `WHEN` with
no `DO` is a compile-time error (nothing to gate); `DO` with no `WHEN` is also a
compile-time error (redundant with writing the call directly in the preamble, which
additionally gets correct once-per-activation execution semantics for a
side-effecting call — see the engine design note below). `DO` must contain at least
one real action or `alert(...)` call — `DO nop` alone is rejected.

**`alert(...)` is an ordinary call, never a reserved keyword** — it compiles through
the exact same channel-list/`msg=` machinery the old `ALERT` clause always used
(`compileAlertClause`), reused unchanged under the hood; only the DSL-surface
keyword is gone. It can appear in the preamble or inside `DO`, with identical
compiled output either way (`events: [{channel, enabled, ...}]`). An event
(`kind='event'`) agent — subscribing to the platform's internal log/event stream
rather than polling a metric — is now an ORDINARY `agent_stmt` too (Wave 2,
2026-10-24): the condition is a `log(tag=..., min_level=..., where={...})` call
(`|` combines multiple filters, replacing the old `match=[...]` list) and `DO`
contains exactly one `render(render=..., gate=...)` call, optionally alongside
`alert(...)` channels. The old standalone `ALERT log_tag=... render=...` statement
(`event_stmt`) is retired — see
[AGENTS_GRAMMAR_EBNF.md](AGENTS_GRAMMAR_EBNF.md) for the full grammar.

**`order(...)`/`bracket(...)`/`help(...)` as standalone statements** — a single bare
`order(...)` (or a comma-separated basket of them), a single bare `bracket(...)`, or
a single bare `help(<name>)` each keep their own pre-existing, unchanged standalone
compiled shape (`kind: 'order'`/`'bracket'`/`'help'`) when that is the ENTIRE
statement. The SAME three names are also valid inside a mixed preamble/`DO` list —
resolved by trying the exclusive standalone parser first and falling back to the
unified preamble parser only when the standalone form doesn't consume the whole
statement.

### Syntax overview

**Condition tree**: `&` (AND) and `|` (OR) have equal precedence and evaluate 
left-to-right — use parentheses to disambiguate: `a & b | c` groups as `(a & b) | c`. 
`~` denotes NOT. `always` is a reserved sentinel for schedule-only agents. Between-style 
conditions are supported: `lower < metric@scope < upper`.

**Calls**: All arguments are keyword-only (positional form does not exist in this 
grammar). Argument aliases are provided for brevity:
- `account` → `acct`, `symbol` → `sym`, `exchange` → `exch`, `order_type` → `otype`,
  `price` → `px`, `product` → `prod`, `variety` → `var`, `chase_level` → `chase`

**Values**: Numbers, strings (quoted), names, and list literals `[a, b, c]` — the 
last only in leaf RHS positions, not as function arguments.

**`help(<name>)`** — a read-only catalog lookup, never a broker action: reverse-resolves
`<name>` (always a bare, unquoted word — the same lexer sugar `debug(x)` uses, not a
quoted string) to every matching catalog row (a name can be reused across token
kinds) and logs/returns the description, parameter signature, and docstring for
each. Valid as a standalone CLI statement, inside the preamble, or inside `DO` — the
one real action never subject to the live/paper mode gate (it never touches the
broker). See §14 below for the full action contract.

**Reserved words**: `when`, `do`, `nop`, `always`, `in`, `not`, `true`, `false`. 
`given`/`alert` are explicitly NOT reserved — `given` has no meaning left to mark at
all (a literal leading `given` is rejected with a specific, helpful error pointing
at the preamble's self-delimiting shape); `alert` must be a callable ordinary token
name (`alert(...)`), which a reserved word can never be. `debug`/`help` are also not
reserved (same "contextual, not reserved" treatment `msg=` uses).

**Limits**: 4000 characters per statement, 20 maximum nesting depth.

**Migration note (no action required for existing saved rows)**: the old 4-clause
`GIVEN <bindings/debug> [WHEN <condition>] [ALERT <alert_clause>] [DO <do_clause>]`
form is no longer valid CLI syntax — a saved `Agent.cli_source` containing the word
`GIVEN`/a bare `ALERT <channel-list>` clause is inert, display-only text that is
never re-parsed, so no migration of any existing row is needed. Re-saving an
existing agent through the Edit flow re-decompiles its live JSON into the new
syntax automatically.

### Parameterized metric and scope call syntax

Metrics and scopes may use function-call syntax with an arbitrary window (in minutes):

```
mean_pnl(minutes=30) @ positions.total <= -50000
max_drawdown_pnl(minutes=90) @ funds.any_acct <= -100000
```

The keyword parameter form (`minutes=30`) is CLI surface — it compiles to the 
backend's positional catalog string internally (e.g. `"mean_pnl(30)"`), matching 
the contract documented in §9 above. Only a single bare numeric literal is accepted 
as the argument; expressions, names, and keyword arguments to actions do not support 
this syntax.

### Compilation contract

- Each statement compiles to an atomic agent JSON shape (conditions/events/actions 
  trees), with no intermediate validation — all semantic checks happen after parsing.
- A condition with a `$ref` fragment anywhere (conditions, events, or actions) cannot 
  decompile back to CLI form — the tab is disabled for that session.
- A bare `order(...)` statement (no `WHEN`, no agent state) places a real broker 
  order through the ticket or basket endpoints, bypassing agent creation entirely.
- The compiled JSON is the SSOT — `cli_source` field is audit/display-only, never 
  read by execution. Regenerating CLI from JSON via decompile may normalize the 
  expression tree (e.g. a single-element `all` → bare leaf) while preserving 
  semantics.

### Operator semantics

`cli_source` (new nullable string field on `Agent` row):
- Set to the exact CLI text on `POST` or `PATCH` when saved from the CLI tab.
- Set to empty string `''` when saved from the Structured (JSON) tab, clearing any 
  prior stale value.
- Never read by the condition evaluator or action dispatcher.
- Displayed in the agent history for reference but has no runtime effect.

### UI behavior

- **Edit existing agent**: If the agent's current JSON decompiles cleanly to CLI text, 
  the CLI tab is shown by default with equivalent text pre-filled. If decompilation 
  fails (e.g., `$ref` fragment), the tab is disabled and Structured is the default.
- **Structured ↔ CLI toggling**: Switching from Structured to CLI re-decompiles the 
  current JSON if it has diverged since the last sync. Invalid JSON blocks the switch. 
  Switching back to Structured preserves any JSON edits made there since the last 
  compile.
- **Live compilation**: `onCompile` callback fires only after operator edits to avoid 
  silently overwriting Structured JSON on tab switches or page reloads. A single 
  keystroke in the CLI textarea triggers recompilation with ~150ms debounce, updating 
  the JSON preview and error list.
- **Order statement**: A bare `order(...)` text shows a "Place order" button instead 
  of a Save button, and submitting places the order immediately via the ticket or 
  basket endpoint (single leg → ticket, multiple legs → basket). LIVE mode orders 
  require confirmation before submission.
- **Help statement**: A bare `help(<name>)` text shows a "Look up" button on the
  Console tab (`/automation?tab=console`) only — it is NOT offered on the
  order-scoped CLI mounts (`/orders`' CLI tab, SymbolPanel's order-ticket CLI tab,
  both restricted via `compileStatement`'s `allowedKinds` option). Submitting POSTs
  to `/api/agents/help`, which calls the same `help_lookup` resolver the real
  `help(...)` DSL action dispatches through — the result renders inline in the
  Console result area, not just logged, since the operator wants to SEE the answer.

### Compiled JSON shape — `preamble` (new, Sprint [grammar-unify])

A compiled `kind:'agent'` row's JSON gained one new top-level key, `preamble`
(an array, omitted entirely when empty — same "no stray key" convention `given`
already follows): one entry per preamble call, in textual order. `debug(...)`
compiles to `{type:'log', params:{level:'debug', message:...}}`; `help(...)` to
`{type:'help', params:{name:...}}`; `alert(...)` to `{type:'alert', events:[...]}`
(the identical `events` shape `DO`'s own `alert(...)` produces); any other real
action (including `order`/`bracket`) to the ordinary `{type, params}` shape.
`given`'s own pre-existing semantics (a pure-alias/primitive label has zero
backend footprint; an operator-expression binding gets its own `given[]` entry)
are unchanged and orthogonal to `preamble` — a label assignment is never itself a
`preamble` entry.

### Preamble execution semantics (landed 2026-10-23, commit `4e6df1ec`)

Two execution models coexist. A bare CLI statement with no save step runs every preamble call
once, synchronously, at submission (unchanged from the `order(...)` / `cancel_all_orders(...)`
one-shot behavior). A SAVED, polled `Agent` row runs its preamble on each poll cycle, split by
call type. The engine is `_cycle_run_preamble()` in
[agent_engine.py](../../backend/api/algo/agent_engine.py#L3659).

| Preamble entry | Runs | Gate |
|---|---|---|
| `help`, `log(level="debug")` (`debug(x)`) | Every poll cycle | None. No DB claim. |
| `alert(...)`, non-debug `log(...)` | Every poll cycle | Windowed by the agent's own `cooldown_minutes`, via `Agent.preamble_alert_last_fired_at`. |
| `order`, `place_order`, `close_position`, `cancel_order`, `bracket`, any other action | At most once per activation | Atomic claim on `Agent.preamble_fired_at` (`WHERE ... IS NULL`). |

- **Activation** is a real inactive/paused → active transition in `_age_set_status`
  ([routes/agents.py](../../backend/api/routes/agents.py#L1209)). It resets both marker columns
  to NULL. Re-activating an already-active or `cooldown` agent does not re-arm. Editing an active
  agent's preamble does not re-arm. The operator must deactivate, then reactivate.
- **`bracket`** has no registered action handler. Its entry logs one WARNING per activation and
  is skipped (no order, no error).
- **Replay / backtest** skips the entire preamble, not only side-effecting calls.
- **Simulator** runs track the once-bucket and cooldown on the isolated `alert_state`, never on
  the real DB columns.
- **Claim-then-fire.** A DB failure on the claim fails safe. Nothing fires, and the claim is
  retried next cycle.
- **Known limitations:** `preamble_fired_at` is written on a successful claim but is not exposed
  by the API. A preamble-only fire does not update `trigger_count` or `last_triggered_at`, so
  `/agents` can show "never fired" after its one-shot order went out. Preamble entries are not validated by `_age_validate_action_entries`, which
  runs on `actions` only. MCP's direct status write (`routes/lab.py`) bypasses `_age_set_status`,
  so an MCP re-activation does not re-arm the one-shot.

Full rationale and the P1 incident (a saved `order(...)` in a preamble would have fired every
cooldown window): [CLAUDE.md](../../CLAUDE.md), entry "Saved cycle-agent preamble".

### Explicit alert mode — `has_explicit_alert`

`has_explicit_alert` is `true` only when a `DO` clause compiles to a real `alert(...)` call. A
preamble-level `alert(...)` does not set it. When the flag is true and the agent has a non-empty
`events` list, the engine skips the automatic kind/threshold rich alert and delivers the agent's
own `events` through the normal dispatch path. Console saves are sticky-false: editing an existing
agent whose stored flag is false always saves false, so a seed-style agent cannot be converted by
ordinary editing. The round-trip rule is documented in
[CLAUDE.md](../../CLAUDE.md), entry "`has_explicit_alert`".

### `log(...)` action (renamed from `emit_log`, 2026-10-23)

`log(level=info|warning|error|critical, priority=low|default|high|urgent, message=...)`, per the
`log` entry's `params_schema` in `backend/config/grammars/agent_grammar.yaml`. `level=debug` is not in
that user-facing enum. Only the `debug(x)` sugar emits it. The `priority` argument is optional. When present, a matching event agent uses it as the ntfy priority
override ahead of the tier-derived default. `debug(x)` compiles to `log(level="debug", ...)` with no
`priority`. DEBUG-level records are dropped before any event agent's conditions are evaluated, so
they never reach a notification channel.

### Known gap — autocomplete/hover position classification (status: reworked, unverified)

Commit `eeb4b478` ("full rework of suggestAgentCliAt/describeTokenAt for the unified WHEN/DO
grammar") reworked the two editor functions named below. Treat the entry as open until the
suggest/hover test files pass against the reworked code. The `_clauseKindFromTokenPrefix`
helper name still exists in `agents.js`. The original gap description follows.

The Console CLI editor's own autocomplete (`suggestAgentCliAt`) and hover-description
(`describeTokenAt`) features still key their internal "which clause is the cursor
inside" classifier (`_clauseKindFromTokenPrefix`, `agents.js`) off the OLD 4-clause
model (`given`/`when`/`alert`/`do` as reserved-word markers). Since `given`/`alert`
are no longer reserved words at the lexer level, this classifier can no longer detect
either one via its existing mechanism, and a parallel "which position is a channel
name expected" classifier (keyed off a seen bare `ALERT` marker) has the same
problem for the new `alert(...)` call shape. Compiling, decompiling, and saving a
statement are all unaffected (the real parser/compiler have no dependency on this
classifier) — only the EDITOR'S OWN live suggestions/hover text are stale for
preamble-position and `alert(...)`-position typing. Tracked as follow-up work, not
closed by this entry — see `frontend/src/lib/__tests__/agentsSuggest.test.js` and
`describeTokenAt.test.js` for the specific failing cases this gap covers.

---

## Change log

| Date | Change |
|---|---|
| 2026-10-25 | Wave 3 follow-up — `draft_token_from_prompt` gained a server-derived `draft['resolver']` field (the exact dotted path Wave 2's `create_token` expects, never trusted from the LLM), a fixed per-kind "extra wiring needed" warning for `channel`/`action_type` drafts (only `metric`/`scope` actually dispatch live after Wave 2's registration endpoint — `REGISTRY.channel()`/`.action()` have zero real callers), and a warning when `test_source` doesn't import the resolved function/module (catches drift after a hinted rename). See §9 "AI-drafted grammar tokens". |
| 2026-10-25 | Wave 3 (unify-DSL-token-registration plan) — `POST /api/mcp/draft-token` + `agent_ai.draft_token_from_prompt` draft a candidate metric/scope/channel/action token from an operator prompt as plain reviewable JSON. Never writes a file, never imports/execs the drafted code, never calls Wave 2's registration endpoint — static-only validation (`ast.parse`, one-function/signature/dangerous-import-or-call checks). Resolver module/file path is derived server-side from the whitelisted kind + verified token name, never trusted from the LLM. Collision check covers active and inactive rows, case-insensitive. See §9 "AI-drafted grammar tokens". |
| 2026-10-24 | Wave 2 (grammar-unification) — the standalone `event_stmt` (`ALERT log_tag=... render=...`) dissolves into an ordinary `agent_stmt`: a `log(tag=..., min_level=..., where={...})` condition sets `kind='event'` (`compileAgentStmt`'s `isEventKind`); `|` replaces `match=[...]`; `DO` carries exactly one `render(render=..., gate=...)` call, optionally alongside `alert(...)` channels. `parseEventStmt`/`compileEventStmt`/`decompileEventAgent`/`_EVENT_META_KEYS` are retired — `decompileAgent` now handles both kinds. New real catalog row `(condition, log_filter, log)`; `event_actions.render.params_schema` gained a required `render` field. See [AGENTS_GRAMMAR_EBNF.md](AGENTS_GRAMMAR_EBNF.md). |
| 2026-10-23 | Sprint [grammar-unify] execution + alert (items 0, 0.C): saved cycle-agent `preamble` executes per call type — `help`/`debug` every cycle, `alert`/non-debug `log` every cycle windowed by `cooldown_minutes`, all other calls (`order`, `close_position`, `cancel_order`, `bracket`, …) once per activation (commit `4e6df1ec`; P1 fix). Activation is a real inactive/paused→active transition only. New `has_explicit_alert` flag (a DO-level `alert(...)` replaces the automatic rich alert; commits `08bf4340`, `6aaec581`); Console saves are sticky-false (commit `b6f56be7`). New columns `preamble_fired_at`, `preamble_alert_last_fired_at`, `has_explicit_alert`. |
| 2026-10-23 | Sprint [grammar-unify] (items 1/2/4) — `WHEN ... DO ...` is now the ONE generic, optional conditional wrapper around any statement; everything outside it (the preamble) runs unconditionally, with no `GIVEN` keyword needed — `GIVEN` is removed from the grammar entirely (it carried no semantic meaning, and the preamble is now self-delimiting by position). `ALERT` is removed as a reserved clause keyword — `alert(...)` is an ordinary call, compiling through the exact same `compileAlertClause` machinery whether it appears in the preamble or inside `DO`. New compiled-JSON key `preamble[]` (per-call compiled entries, omitted when empty). New standalone/preamble/DO-callable `help(<name>)` action — read-only catalog introspection, wired to `POST /api/agents/help` on the Console tab only (never the order-scoped CLI mounts). The standalone top-level `event_stmt` (bare `ALERT log_tag=...`) is unchanged. Known gap: the Console editor's own autocomplete/hover classifier (`_clauseKindFromTokenPrefix`) still assumes the old 4-clause shape — tracked as follow-up, does not affect compile/save correctness. |
| 2026-10-10 | (uncommitted Sprint 4) — Agent CLI grammar UI + full compiler suite + decompiler. Operators can now author and edit agents via single-line text statements (WHEN condition [ALERT notifies] [DO actions]) in the `/automation` page's CLI tab, alongside the existing Structured (JSON) tab. Supports parameterized metric/scope call syntax (mean_pnl(minutes=30)), keyword-only arguments with aliases, list literals, and parenthesized boolean logic. A bare order(...) statement places a real order without creating an agent. Commits 2a5cd01d, d05fe54a, 3b615b0b with integration into existing agent create/update routes. |
| 2026-10-09 | 9b7da82f: CLI-grammar effort, Sprint 1 — `_age_validate_action_entries()` closes the same save-time validation gap for the `actions` array that c0f260b9 (below) closed for `conditions`; new `grammar.is_known_action_type()`; new `Agent.cli_source` (nullable, audit-only) column; `expr_eval.py` extended to support string constants and `+` concatenation for `token_ref_ok` action params. |
| 2026-10-09 | c0f260b9: Sprint 3 — `_age_validate_threshold_conditions()` closes the save-time validation gap for threshold/cycle agents (create/update now reject unknown/malformed `metric`/`scope`/`op` tokens with 422, same as the long-standing event-kind path). `_parse_call_token` rejects non-positive call-syntax windows (`mean_pnl(0)`). `_summarise_token()` generalizes `params_schema` surfacing beyond `action_type` to metric/scope/channel/format call-syntax tokens. |
| 2026-10-09 | 1b80b7d8: Sprint 2 — parameterized function-call metric tokens (`mean_pnl(30)` etc.) added to `GrammarRegistry`, documented in §9 above. |
| 2026-10-08 | (pre-existing corrections): Fixed incorrect statement about `expiry_close` being "held until expiry cutoff" — clarified that cutoff controls creation timing only, not release (operator must click Release to remove hold indefinitely). Fixed incorrect statement that "every held order" has `status="HELD"` — `template_exit` holds leave the parent at original status (e.g. FILLED) with only `hold_json` set. |
| 2026-10-08 | c5e8814b: Expiry-close agents seeded as inactive with `_ae_guard_seed_status()` — validates that seed dicts with "Ships INACTIVE" in description are never seeded active, wired into both insert and sync paths, logs ERROR and force-corrects on mismatch. |
| 2026-10-08 | a87db772: Generalized hold/release into a reusable registry — `record_held_order()` replaces category-hardcoded functions, `held_for()` generic check, `get_release_handler()` dispatches via `_RELEASE_HANDLERS` dict. Adding a new hold category is a 4-step registration, not a route edit. |
| 2026-10-07 | e37fab01: Event agents UI — full CRUD in `/automation`, new `GET /api/agents/renderers`, `kind` write vocabulary with read-time normalization, `tier` and `topic` persistence, `kind` immutability post-creation, event-agent writes validated via `validate_seed_spec()` (422 on failure). |
| 2026-07-11 | v1.0 initial spec from codebase audit |
