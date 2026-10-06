# Agent Rules: Unified Grammar, Operand Registry, and Sprints

Status: proposal, not yet approved. Scope: risk management now; technical indicators later.

## MVP (first release)

The first release is the smallest version that can safely place risk-management orders on dev and prod. Everything not listed here is Phase 2 and keeps the detailed sprints in section 6.

**Rule shape**
- `WHEN condition THEN order [NOTIFY channels]`. At least one of `THEN` or `NOTIFY`.
- One order per rule. A failed order always sends a notification; there is no separate failure branch.
- Notify-only rules are allowed.

**Condition**
- Risk metric operands only: the existing registered metrics (P&L, margin, expiry, cash, and similar).
- Comparisons, `AND`, `OR`, `NOT`. No `ELSE`.
- Positions use natural keys: `(account, exchange, tradingsymbol, product)`. Account-level metrics use `account`.

**Lifetime**
- `ACTIVE <window> UNTIL <end>`, with `UNTIL END_OF_SESSION` as the default for risk rules.
- `REPEAT ONCE` (default) or `EVERY_CYCLE` with a cooldown.
- `MAX_FIRINGS`, default 1.
- No `WHILE` retry. Retries stay in the order's existing chase settings.
- Working orders are cancelled on expiry (`CANCEL`). `ORPHAN` is deferred.

**Orders**
- Order fields: verb, lots, order type, price, product, account, and `TIF` of `DAY` or `GTC`.
- Per-rule guards: maximum orders per day and maximum quantity.
- Idempotency key per firing, so a retry cannot place the same order twice.
- Mode comes from the existing resolver: the rule's trade mode, the master paper switch, and shadow mode. No `MODE` clause.
- Live orders need the existing confirmation.

**Notify**
- Channels: `telegram`, `ntfy`, and `log`. `email` is deferred.
- Fixed message format, with no template variables.

**Values**
- Literal or reference to an existing setting. No bounded literals.
- The master kill switch and paper switch are unchanged and override every rule.

**Registry**
- One table for operands, channels, order fields, and settings references, with a unique key on category and name.
- Seeded from a file in the repo. A load-time check rejects an active token whose adapter does not exist.

**Explainability and audit**
- The rule card shows the rule as text, plus each leaf's last evaluated value.
- Setting changes are logged with who and when. Before a setting is saved, the settings page shows how many rules reference it. The full impact list is Phase 2.

**Interfaces**
- The existing agent card and editor, updated to show rule text and the lifetime.
- `propose_agent` in the MCP server: read-only, drafts a rule, and runs a dry run. It never saves or activates.

**Phase 2 (deferred):** `ELSE`, multiple actions per `THEN`, `ON FAILURE` branches, result bindings, `WHILE` retry, `ORPHAN`, GTT and order-state operands, aggregates, identity and alias tables, rule versions, replay, impact preview, profit lock, trailing stop, rollover, re-entry, conflict rules, `MODE` clause, bounded literals, email, webhook, and indicators.

**MVP sprints**
- **M1: registry and validator.** Single token table, seed loader, load-time adapter check, validator for the MVP rule shape. Dual-read with legacy rules. Tests: unique key, missing adapter rejected, every legacy rule validates in both forms.
- **M2: lifetime and notify.** `ACTIVE`, `UNTIL`, `REPEAT`, `MAX_FIRINGS`, and the `NOTIFY` clause, including notify-only rules. Tests: window pause and resume, firing cap, cooldown, log and channel routing.
- **M3: orders with guards.** Single typed order, `TIF` DAY and GTC, `CANCEL` on expiry, guards, idempotency, failure notification. Tests: duplicate retry places one order, guard blocks oversize order, expiry cancels working orders, paper and live gating.
- **M4: proposer and card.** `propose_agent` (read-only), rule text on the card, leaf values, and the setting reference count. Tests: draft validates, no write except the draft, reference count matches stored rules.
- **Pilot:** run on dev for a set period with paper mode, then promote to prod with live orders only after review.

## Hold, release, and expiry close (addendum)

Scope: automated orders only (expiry closes, template exits, agent orders). Manual tickets are not held.

**Timing settings (global, per exchange)**
- **Close time:** the exchange's session close from the market calendar (NFO 15:30 IST, MCX 23:30 IST).
- **Lead time:** minutes before close at which the expiry close is created as held. Default NFO 15, MCX 30.
- **Cut-off time:** close time minus lead time. At cut-off, the scan creates the held orders.
- Release is allowed after cut-off. Lead time controls when the held order is created, not when it is sent.

**Hold switches (global)**
- One switch per category: expiry closes, template exits, agent orders. Default: held.
- Turning a switch to released applies immediately, with no cool-off.
- A per-order override (held yes/no) wins over the global switch and survives later switch changes.

**Price policy (no stored limit price while held)**
- A held order stores a price policy, not a price: MARKET, or CHASE LOW / MED / HIGH.
- Expiry closes default to CHASE MED.
- At release, the price is read from the live bid and ask, rounded to the instrument tick, and checked against the exchange price band before sending.
- If the price fails the band check, the order stays held and the card shows the reason.

**Release checks**
- The position still exists, the quantity matches, and the order has not filled or been cancelled.
- On success: send, chase, and record who released it and when.
- On failure: stay held, show the reason, and log a release-refused event.

**Template exits**
- Exit GTTs are placed only after the entry fills and only if the exit hold is released.
- A held exit shows "unprotected until released" on the order card.

**Screens**
- Settings: the switches, close-time display, lead time, and cut-off display per exchange.
- Ticket: "Hold: yes/no" after the template toggle, defaulting to the global switch.
- Order card: HELD badge with reason, Release button, and held-exit warning.
- Held orders card: top of the orders page and on the Exp-close tab, with per-order and release-all actions.

**Sprint placement:** timing settings and the price policy field go in H1. The release price check goes in H3. The cut-off scan goes in H4.

## Implementation status (hold and release)

- **H1 policy:** done. Override over global switch over default (held). Cut-off and release price rules.
- **H2 gate:** done for expiry closes (held by default) and for template exit GTTs (held by default, until the global switch is released).
- **H3 release:** done. Position and price checks, then send and chase (expiry closes). Template exits are placed on release.
- **H4 cut-off:** done. The expiry scan waits until the cut-off, which is the close minus the lead time (NFO 15, MCX 30 minutes by default).
- **H5 template exits:** done. Exit GTTs wait for release; the parent order stays FILLED with its hold recorded.
- **H6 screens:** partly done. Settings switches and lead times are registered; the held-orders card with release is on the orders page. Not built: the per-order hold flag on the ticket and the held-exit warning on the order card.
- **H7 rollout:** dev first, then prod, once the held state is confirmed on dev.

Related fixes shipped with this work: no implicit 30% take-profit on a request without a target; the order ticket no longer picks a default template; fill alerts show the full account.

# Phase 2 design (reference)

## 1. Problem

Agents today are free-form JSON. Condition leaves use string tokens (`metric`, `scope`, `op`, `value`), and actions are untyped dictionaries (`{"type": "place_order", ...}`). Order grammar (`backend/config/grammars/orders.yaml`) is used only by the UI, and agent actions don't share its vocabulary. Adding a metric means editing grammar code, and nothing identifies an operand uniquely across sources.

## 2. Goals

1. One grammar for a rule: `WHEN <condition> THEN <action>`, where the action is an order or a notification.
2. Operands are data: defined in the database, identified uniquely, and resolved at evaluation time.
3. Operator logic (comparisons, `AND`, `OR`, `NOT`, `IF`, `ELSE`, `WHILE`) stays fixed in code.
4. Operand kinds (table field, broker value, computed function, windowed function, resolver) are extensible, and indicators can be added later without a grammar change.
5. An MCP tool can propose a rule from current positions, holdings, and market state. It never activates a rule.

## 3. Design decisions

- **Operand = entity reference:** `{entity, key, field}`. The key is the entity's identity in its source. Example: `position` keyed by `(account, exchange, tradingsymbol, product)`.
- **Identity table:** one row per entity instance, with a canonical key and a hash under a unique index. Aliases (broker order ID, template ID, old tradingsymbol) map to the canonical identity, unique per `(alias_type, alias_value, account)`.
- **Operand definitions as data:** `operand_def` rows hold the entity type, field, value type, unit, key schema, kind, adapter name, parameter schema, declared inputs, freshness (live or frozen), and version.
- **Reflection for the function library, not for rules:** adapters and computed functions are Python with type hints and docstrings. Registration derives parameter schemas from signatures and adds explicit keys, freshness, and version. Rules are declarative JSON that references registered names. Rules never execute code.
- **Order is part of the action:** an action is either a typed order object (with attached exits such as bracket or OCO) or a notification. Order classes carry their own validation rules.
- **Condition can read order and GTT state:** order status, pending orders, and GTTs (including template-placed ones) are operands. Their fired events are edge-triggered (fire on change), using the existing per-leaf latch approach keyed by identity.
- **Scope is a selector, not an identity:** `positions`, `holdings`, `any_account`, and similar filters stay as scopes.
- **Evaluation mode:** each rule records `evaluation_mode`. Risk rules use `cycle`, the existing refresh-cycle evaluation. Indicator rules will later use `bar` (closed bars only).
- **Notification is part of the grammar:** a rule has an optional `NOTIFY` clause with its own channel list, validated against a channel registry (telegram, ntfy, email, log). It can stand alone for notify-only rules or follow an order. Channels are no longer a separate agent-level setting.
- **Logging is a notification channel:** `log` writes the firing to the agent event log at the level the rule sets (`info`, `warn`, or `error`) and to the application log. It is a channel like the others, so the same clause and the same resolution rules apply.
- **Paper, sim, replay, and shadow:** notifications on fills and rule actions follow the same live-only rule as fill alerts, unless a rule explicitly asks otherwise.

## 4. Target data model

| Table or column | Purpose |
|---|---|
| `operand_def` | Registry of entity fields and computed operands: kind, adapter, parameter schema, inputs, freshness, version |
| `operand_identity` | Canonical identity per entity instance, unique on `(entity_type, key_hash)` |
| `operand_alias` | Alternate identifiers mapped to a canonical identity |
| `agent.schema_version` | Which rule format the row uses (`1` = legacy tokens, `2` = unified grammar) |
| `agent.evaluation_mode` | `cycle` (default) or `bar` (reserved for indicators) |

Existing `agent_events` is reused for trigger history and edge latches. `conditions` and `actions` JSON stay in place during migration.

## 5. Grammar shape (sketch)

```
rule       := WHEN condition [THEN action] [NOTIFY notify]   # at least one of THEN / NOTIFY
condition  := leaf | ALL[condition...] | ANY[condition...] | NOT condition
leaf       := operand OP value           # OP in  < <= > >= = !=
operand    := { entity, key, field, params? }   # validated against operand_def
action     := order                       # omitted for notify-only rules
order      := { verb, instrument, qty(lots), order_type, price?, product, account, exits? }
notify     := { channels: [telegram | ntfy | email | log ...], level?: info | warn | error, message?: template, on: fired | recovered | both }
```

Operators are fixed. Operands, order fields, and channels are validated against the registry at save time.

## 5a. Examples

Notification only:
```
WHEN pnl_pct(position ZG0790/NIFTY26OCT25000CE/NRML) < -10
NOTIFY telegram, ntfy
```

Order only:
```
WHEN days_until_expiry(position ZG0790/NIFTY26OCT25000CE/NRML) <= 0
THEN SELL 1 lot NRML MARKET account ZG0790
```

Both, with the notification reporting the order outcome:
```
WHEN avail_margin(account ZG0790) < 50000
THEN SELL 1 lot NRML MARKET account ZG0790
NOTIFY telegram, ntfy
```

## 5b. Lifetime

A rule's lifetime is part of the rule, separate from its condition and actions.

**Four separate concepts:**
- **Window (`WINDOW`):** the hours of each day when the rule may evaluate and fire, for example `WINDOW 09:20-15:15 IST`. Outside the window the rule is paused, not expired. It resumes at the next window start, and it respects market holidays and the segment's sessions (NSE equity, F&O, MCX evening session).
- **Repeat (`REPEAT`):** the rule re-arms across evaluation cycles. Options: `ONCE`, `EVERY_CYCLE` with a cooldown, or `ONCE_PER_SESSION`.
- **Retry (`WHILE`):** inside a single firing, repeat an action while a condition holds, bounded by `MAX_ATTEMPTS` and `MAX_SECONDS`. A retry never outlives its firing.
- **Validity (`UNTIL`):** when the rule stops being evaluated. Options: `END_OF_SESSION`, `DATE <yyyy-mm-dd>`, or `OPEN` (no end, requires explicit operator confirmation).

**Window rules:**
- A window is a time range in IST plus a segment (equity, F&O, or MCX). Holidays come from the market calendar, not from the rule.
- At window end, working orders follow the same `on_expiry` choice as a rule expiry (`CANCEL` by default).
- A rule can have a window and an `UNTIL`. The window applies each day, and `UNTIL` applies to the whole rule. A window with no `UNTIL` runs until the next day's window ends, which is one session.
- A `WHILE` retry stops at the window end, even if its own limit has not been reached.

**Defaults:**
- Risk rules default to `WINDOW` = the segment's full trading session and `UNTIL END_OF_SESSION`, so a stale rule cannot fire on the next trading day.
- `UNTIL OPEN` and multi-day rules require an explicit end date or an operator confirmation at activation.
- `REPEAT` defaults to `ONCE`.
- Every rule has a `MAX_FIRINGS` cap, default 1.

**Per-order time-in-force:** each order object carries `TIF` of `DAY`, `GTC`, or `GTT`, validated against the order class and the broker's limits.

**Open orders when a rule expires:** a rule that expires with working orders must choose one of:
- `CANCEL`: cancel its working orders at expiry.
- `ORPHAN`: leave them in place, and mark them as orphaned so they show in the orders view and generate a notification.

This choice is required in every rule that places orders. The default is `CANCEL`. `ORPHAN` requires operator confirmation.

**Storage:** lifetime fields live on the rule (`window_start`, `window_end`, `window_segment`, `valid_until`, `repeat_mode`, `max_firings`, `on_expiry`). Firing count and state are in `agent_events`, keyed by rule and identity.

**Sprint placement:** add `WINDOW`, `UNTIL`, and `REPEAT` to S2 (schema and validator), with holiday and segment checks from the market calendar. Add `WHILE` and `on_expiry` handling to S3 (actions). Add the orphan notification to S4.

## 5c. Gaps and additions

**Grammar**
- `ELSE` branch, and `IF` / `ELSE` inside a rule, so one rule can choose between two actions.
- Multiple actions per `THEN`, executed in order, each with its own failure outcome.
- `ON FAILURE` branch: runs when an order is rejected or margin-blocked, with its own action or notification.
- Result bindings: an action's output (order ID, fill price, status) can be referenced by later clauses and by notification templates.
- Every order field has a fixed unit. Quantity is lots at the rule boundary and is converted to contracts once, in one place, with a test for the conversion.
- `on: recovered` is defined: the condition was true at the last evaluation and false now, using the existing latch.

**Safety**
- Per-rule guards: maximum orders per day, maximum quantity, maximum notional, and whether a live order needs operator confirmation.
- Global kill-switch action: cancel all working orders and block new orders across rules, with a single operator control.
- Idempotency key per firing (rule ID, firing ID, action index), so a retry after a timeout cannot place the same order twice.
- Conflict rule: two rules cannot place opposing orders on the same position inside one cycle. The second is held and notified.
- Broker-side GTT that fires but does not fill: the rule notifies the operator and does not re-place the GTT automatically.

**Lifecycle**
- Rule versions: each edit creates a new version. Each firing records the version that produced it.
- Replay before activation: the rule runs against historical data in the existing replay engine, and the result is shown with the draft.
- Rule list with pause, resume, and bulk edit.

**Operands**
- Aggregate operands: totals across accounts or positions, such as total margin or total day loss. The scope selector picks the rows, and the aggregate function combines them.
- Notification templates list their variables: symbol, account, quantity, price, P&L, order ID, rule name, firing time.

**Exits**
- Profit lock and maximum-loss cutoff at strategy level, in addition to per-order exits.
- Trailing stop as an exit type, with its class restrictions validated.
- Re-entry: a rule may re-enter a position after its exit, under the `REPEAT` and `MAX_FIRINGS` limits.

## 5d. Grammar or settings

Every value in a rule takes one of four forms. Global switches and platform settings are the single source of truth for anything shared or safety-relevant.

**The four forms:**
1. **Literal:** written in the rule, with no link to a setting. Example: `pnl_pct < -10`. It has no meaning beyond the rule, so changing it is a rule edit, reviewed like any other change. Literals are marked as literal in the registry, so the impact preview ignores them.
2. **Reference:** the rule points at a setting, for example `@setting:algo.chase_interval_seconds`. Changing the setting changes every rule that references it. The impact preview lists these rules before the change is saved.
3. **Bounded literal:** the rule writes a value, and a setting sets its ceiling. Example: `max_orders_per_day = 3` is accepted only if the platform maximum is 5 or more. The check runs at save time and again when the rule fires.
4. **Switch:** a global setting the rule cannot set or override. The master kill switch, the paper-trading switch, the shadow switch, and the channel-environment flags are switches.

**Resolution order:** switch first, then the rule's literal or reference value, checked against its bound. The effective value is shown on the rule card and in the dry run.

**Rule of thumb:** a literal is fine when the value has no meaning beyond the rule. A shared value, or one that limits money at risk, is a reference or a bounded literal.

**Settings registry:** each setting is a registry entry with its type (switch, bound, or reference target), default, and bound. Grammar tokens and settings use the same registry. The `log` level is a bound: a rule's level can't go below the platform's minimum, so a rule can't silence logs the platform keeps.

**Impact preview and audit:**
- Before a setting changes, the settings page lists the rules that reference it, with their effective values before and after.
- Each setting change records who changed it and when.
- Each firing records the setting values it resolved to.

**Items on the boundary:** chase attempts and mode are bounded literals. The rule states what it wants, and the platform setting is the ceiling. The master switch works the same way: a rule set to `live` still runs in paper when the switch is on.

## 6. Sprints (Phase 2 detail)

### S1: Operand registry and identity (no behaviour change)
- Add `operand_def`, `operand_identity`, `operand_alias`, and the two new agent columns.
- Add the settings registry (switch, bound, reference target types, defaults, bounds) and seed it from the existing settings and switches. Values are unchanged.
- Add the audit log: setting changes (who, when, old and new value) and each firing's resolved setting values.
- Seed `operand_def` for the metric tokens in use today (about 13 in the seeds: `avail_margin`, `pnl_rate_pct`, `pnl_rate_abs`, `pnl`, `is_itm`, `pnl_pct`, `day_val`, `day_pct`, `minutes_until_close`, `is_future`, `days_until_expiry`, `cash`, and `metrics`).
- Map each existing token to its registry row.
- Tests: identity uniqueness, alias resolution, every existing token maps to a definition, every existing setting maps to a registry entry with unchanged value, audit entry written on a setting change.

### S2: Unified condition schema and validator (dual-read)
- Condition leaves accept either the legacy token form or an operand reference.
- Validator checks each operand against its definition: key schema, field, type, unit, and inputs.
- Add `ELSE`, `IF`/`ELSE`, and the `on: recovered` definition.
- Add the fixed unit on every order field, with the lots-to-contracts conversion in one place.
- Tests: valid and invalid references, dual-read equivalence for every legacy rule, unknown fields rejected with a clear error, the lots-to-contracts conversion.

### S3: Typed order actions and safety
- Convert the existing action types (`place_order`, `close_position`, `modify_order`, `cancel_order`, `cancel_all_orders`, `expiry_auto_close`, `chase_close`, `send_summary`) into typed order objects.
- Support multiple actions per `THEN`, each with an `ON FAILURE` branch and result bindings.
- Attach exits as templates (bracket and OCO), validated per order class.
- Per-rule guards (orders per day, quantity, notional, live confirmation) and the global kill switch.
- Idempotency key per firing, and the conflict rule for opposing orders on one position.
- Keep `trade_mode` gating: a rule can place live orders only when its mode is live.
- Tests: conversion of each legacy action, per-class validation, paper and live gating, idempotency under retry, conflict hold, kill switch blocks new orders, guards reject oversize orders.

### S4: Notification clause in the grammar
- Add the `NOTIFY` clause to the rule schema, with a channel registry (telegram, ntfy, email, log) and optional message templates. The `log` channel writes to the agent event log and the application log at the rule's level.
- Validate channels at save time. Legacy `events` lists keep working through a fallback that reads them as a notify clause.
- Support notify-only rules: a rule with no `THEN` clause and at least one `NOTIFY` clause. Validation rejects a rule with neither.
- Tests: channel validation, routing, notify-only rules, legacy fallback, the same message formats as the existing error and fill alerts, and `log` writes one agent event and one application log line per firing at the rule's level.

### S5: Order-state, GTT, and aggregate operands
- Add operands for order status, pending orders, and GTTs, including template-placed GTTs.
- Edge-triggered latches keyed by identity, stored in `agent_events`, with `on: recovered` using the same latch.
- Aggregate operands for totals across accounts or positions.
- A broker-side GTT that fires without filling notifies the operator and is not re-placed automatically.
- Tests: fire once per transition, no repeats across cycles, restart hydration, aggregate totals match the per-row sum, GTT non-fill notifies without re-placing.

### S6: MCP proposer and replay (read-only)
- `propose_agent(intent)` reads positions, holdings, quotes, and expiries, drafts a rule in the unified format, and runs a dry run (including `get_order_margin` for order actions).
- Replay the draft against historical data in the existing replay engine, and show the result with the draft.
- Rule versions: each edit creates a new version, and each firing records its version.
- Saves the draft only. No activation.
- Tests: draft validity, no write other than the draft, read-only grounding, replay result matches the dry run for the same bars, version recorded on each firing.

### S7: Draft review, activation, and rule list in the Lab UI
- Show the draft with its dry-run result, replay result, and validation errors.
- Activate only with a confirm token, using the existing token flow.
- Rule list with pause, resume, and bulk edit.
- Impact preview on the settings page: before a setting changes, list every rule that references it, with its effective value before and after. The change cannot be saved until the list is shown.
- Show each firing's resolved setting values in the rule's history.
- Tests: Playwright flow for review, activation, pause, and resume; impact preview lists exactly the rules that reference the changed setting; a literal-only rule is not listed.

### S8: Consolidation and cleanup
- Merge seeded agents into parameterised templates, for example "loss threshold per account" or "expiry proximity per underlying".
- Remove legacy token handling once no `schema_version = 1` rules remain.

### S9: Exit management
- Profit lock and maximum-loss cutoff at strategy level.
- Trailing stop as an exit type, with class restrictions validated.
- Re-entry after an exit, under `REPEAT` and `MAX_FIRINGS`.
- Tests: profit lock triggers at the threshold, trailing stop only on allowed order classes, re-entry respects firing caps.

### Later: technical indicators
- Add `indicator` as an operand kind, with an `interval` key part and `evaluation_mode = bar`.
- Indicator functions use the same registration path as risk functions.

## 7. Migration and rollout

1. Add tables and columns, the settings registry, and the audit log (S1). Nothing reads them yet.
2. Register the existing settings and switches in the registry, with their types (switch, bound, or reference target). Existing values are unchanged.
3. Enable the audit log for setting changes and for each firing's resolved setting values (S1, recorded but not yet shown).
4. Dual-read in the engine (S2). Legacy rules run unchanged.
5. Backfill operand references and notify clauses for existing rules.
6. Switch all writers (`agent_ai.py`, `template_registry.py`, the MCP tools) to schema version 2.
7. Turn on the impact preview before any setting is changed from the settings page (S7). Until then, setting changes go through the existing settings flow.
8. Retire legacy tokens only after S8.

Each step is deployable on its own, and each can be rolled back by switching the reader back to legacy.

## 8. Risks

- **Live orders from rules:** a rule that places an order must pass dry run, margin checks, and trade-mode gating before it can run.
- **Repeated alerts:** edge latches are required for every state-based operand (S5).
- **Identity drift:** renamed or re-keyed instruments depend on aliases. Missing aliases mean a rule stops matching, so the validator should flag unresolved identities at save time.
- **Expired contracts:** freshness must say whether a value is live or frozen, so rules don't evaluate stale data silently.

## 9. Open questions

1. Should the proposer be allowed to draft order actions, or only notifications, until S3 is complete?
2. Should a draft that touches live orders always need a dry run and a confirm token, even when it's created by the MCP tool?
3. Should legacy rules be migrated automatically in S3, or converted by hand so each one is reviewed?
