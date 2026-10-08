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

Nine builtin agents ship as hardcoded rows (`BUILTIN_AGENTS` in `agent_engine.py`):

Loss agents:
- `loss-aggregate` — firm P&L threshold
- `loss-positions` — position-level P&L threshold
- `loss-holdings` — holding-level loss threshold
- `loss-day-percent` — daily intraday move threshold
- `loss-fund-negative` — available funds exhausted
- `loss-rate-acct` — rolling P&L rate threshold per account

Market-lifecycle agents:
- `expiry-auto-close-nse` — Auto-close F&O legs at 15:25 IST
- `expiry-auto-close-mcx` — Auto-close F&O at 23:25 IST
- `expiry-day-equity-itm-auto-close` — Auto-close ITM equity options on expiry day

### Orphan pruning

On startup, database rows with `is_system=True` that are NOT in `BUILTIN_AGENTS`
are deleted (orphans from removed builtin rules). Non-system agents are never pruned.

### Seed status guard

A builtin agent whose `description` contains the phrase "Ships INACTIVE" (case-sensitive
substring match) must always seed or resync with `status="inactive"`, regardless of the
seed dict's own `status` field. The `_ae_guard_seed_status()` function
([`agent_engine.py:1566–1592`](../../backend/api/algo/agent_engine.py#L1566-L1592)) enforces this:

- On insert (new agent): the guard checks at row-build time and forces any active status to
  "inactive" if the description includes the phrase.
- On sync (existing agent): the guard checks again at startup, so every process restart
  converges existing rows to "inactive" if the description says so.
- Failed guard applies an ERROR log and continues (fail-safe, no crash).

This protects destructive auto-close agents (`expiry-day-equity-itm-auto-close` and
`expiry-day-commodity-itm-auto-close`) from accidentally being enabled at seed time when
their own documentation promises they ship inactive. Operator activations of these agents
are reverted on the next process restart, so enable them only after understanding they are
destructive (chase-close every ITM/NTM position without operator confirmation).

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

### Condition parsing

v2 grammar evaluator (`agent_evaluator.py`) consumes JSON condition trees and
tokens table to resolve free-form condition strings into structured leaves
(`metric`, `scope`, `op`, `value`).

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
| `expiry_close` | Close position held until expiry cutoff | `release_held_order` | Operator clicks Release |
| `template_exit` | GTTs held until operator confirms | `release_template_exit` | Operator clicks Release |
| `agent_order` | Resume order held after repeated rejections | `release_repeated_rejection_hold` | Operator clicks Release |
| (unregistered) | Fallback default | `release_held_order` | Any unregistered category |

Every held order is persisted as an `AlgoOrder` row with `status="HELD"` and a JSON
`hold_json` record carrying `{"category", "reason", "price_policy", "override",
"held_at"}`. The operator releases via `/api/orders/held/{id}/release` route
([`orders_held.py`](../../backend/api/routes/orders_held.py)), which dispatches
through `get_release_handler(category)` to the registered handler. Cancelling
via `/api/orders/held/{id}/cancel` abandons the hold instead.

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

## Change log

| Date | Change |
|---|---|
| 2026-10-08 | c5e8814b: Expiry-close agents seeded as inactive with `_ae_guard_seed_status()` — validates that seed dicts with "Ships INACTIVE" in description are never seeded active, wired into both insert and sync paths, logs ERROR and force-corrects on mismatch. |
| 2026-10-08 | a87db772: Generalized hold/release into a reusable registry — `record_held_order()` replaces category-hardcoded functions, `held_for()` generic check, `get_release_handler()` dispatches via `_RELEASE_HANDLERS` dict. Adding a new hold category is a 4-step registration, not a route edit. |
| 2026-10-07 | e37fab01: Event agents UI — full CRUD in `/automation`, new `GET /api/agents/renderers`, `kind` write vocabulary with read-time normalization, `tier` and `topic` persistence, `kind` immutability post-creation, event-agent writes validated via `validate_seed_spec()` (422 on failure). |
| 2026-07-11 | v1.0 initial spec from codebase audit |
