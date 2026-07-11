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
| `pnl_pct` | positions, holdings, account | P&L as % of cost/value | Percent (0–100) |
| `day_pct` | positions, holdings, account | Today's intraday move | Percent (0–100) |

### Rate-of-change (per minute over window)

| Metric | Scope | Definition | Window | Units |
|---|---|---|---|---|
| `pnl_rate_abs` | positions, holdings, account | dP&L / dt absolute | `alert_rate_window_min` | Currency/min |
| `pnl_rate_pct` | positions, holdings, account | d(P&L%) / dt | `alert_rate_window_min` | Percent/min |

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

Five loss agents ship as hardcoded rows (`BUILTIN_AGENTS` in `agent_engine.py`):
- `loss-aggregate` — firm P&L threshold
- `loss-positions` — position-level P&L threshold
- `loss-holdings` — holding-level loss threshold
- `loss-day-percent` — daily intraday move threshold
- `loss-fund-negative` — available funds exhausted

Plus market-lifecycle agents:
- `expiry-auto-close-nse` — Auto-close F&O legs at 15:25 IST
- `expiry-auto-close-mcx` — Auto-close F&O at 23:25 IST

### Orphan pruning

On startup, database rows with `is_system=True` that are NOT in `BUILTIN_AGENTS`
are deleted (orphans from removed builtin rules). Non-system agents are never pruned.

### Editing loss agents

Loss-agent conditions are editable live via `/automation` page. Edition does NOT
invalidate the current run — changes apply on the next `run_cycle()`.

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

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
