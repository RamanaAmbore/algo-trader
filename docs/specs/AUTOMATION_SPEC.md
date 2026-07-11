# Automation Specification

Single source of truth for agents, automation rules, template-driven orders, and
condition-tree evaluation. Covers agent lifecycle, rule syntax, action dispatch,
and run-in-simulator testing.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/routes/agents.py` · `frontend/src/routes/(algo)/automation/+page.svelte` · `backend/api/algo/agent_engine.py`

---

## Contents

1. [Agent Lifecycle and Terminology](#1-agent-lifecycle-and-terminology)
2. [Condition Tree Grammar v2](#2-condition-tree-grammar-v2)
3. [Metrics and Scopes](#3-metrics-and-scopes)
4. [Agent Lifespan and Status](#4-agent-lifespan-and-status)
5. [Action Types and Dispatch](#5-action-types-and-dispatch)
6. [Notification Channels](#6-notification-channels)
7. [Templates and GTT Rules](#7-templates-and-gtt-rules)
8. [Run-in-Simulator Testing](#8-run-in-simulator-testing)
9. [Cooldown, Debounce, and Suppression](#9-cooldown-debounce-and-suppression)
10. [Edge Cases](#10-edge-cases)
11. [API Contract](#11-api-contract)
12. [Test Coverage Map](#12-test-coverage-map)

---

## 1. Agent Lifecycle and Terminology

**Four terms**:
1. **Agent** (rule) — condition-driven rule with actions and notifications
2. **Alert** (event) — runtime fire event (agent triggered, condition true)
3. **Notify** (delivery) — message delivery (Telegram, Email, Log, WebSocket)
4. **Action** (side-effect) — order place/modify/cancel/close or automation trigger

**Agent row schema**:
- `id`, `slug`, `name`, `description`, `long_name` (3-part "condition - alert - action")
- `conditions` (JSON tree), `events` (notification list), `actions` (order/automation list)
- `status` ('inactive' | 'active' | 'triggered' | 'completed' | 'expired')
- `last_triggered_at`, `trigger_count`, `last_error`
- `is_system` (true for built-ins, false for custom)
- `scope` ('total' | 'per_symbol' | 'per_account')
- `schedule` ('market_hours' | 'always' | 'once_daily')
- `cooldown_minutes` (gate re-fire within N minutes)
- `fire_at_time` (HH:MM IST, optional; fire once per IST day near this wall-clock time)

**Status progression**:
- `inactive` → `active` (operator activates via UI or API)
- `active` → `triggered` (condition true, actions fired, notifications sent)
- `triggered` → `completed` (terminal; n_fires or until_date lifespan hit)
- `active` → `expired` (until_date lifespan passed, still active but no longer fires)

---

## 2. Condition Tree Grammar v2

**Syntax** (recursive):
```json
{
  "all": [
    {"metric": "pnl", "scope": "total", "op": ">", "value": 10000},
    {"any": [
      {"metric": "pnl_pct", "scope": "total", "op": ">", "value": 1.0},
      {"metric": "day_pct", "scope": "total", "op": ">", "value": 0.5}
    ]}
  ]
}
```

**Operators**: `>`, `>=`, `<`, `<=`, `==`, `!=`, `in`, `not_in`, `contains`, `regex`

**Leaf structure**:
```json
{
  "metric": "<name>",
  "scope": "total | per_symbol | per_account",
  "op": "<operator>",
  "value": <value | [values]>
}
```

**Composite structure**:
- `"all": [...]` — AND logic, all children must be true
- `"any": [...]` — OR logic, any child may be true
- `"not": [...]` — negation, child must be false

**Nesting**: Arbitrarily deep. No parentheses or operator precedence confusion (tree is the grammar).

---

## 3. Metrics and Scopes

**Point-in-time metrics** (snapshot at evaluation time):
- `pnl` — unrealised P&L in INR
- `pnl_pct` — unrealised P&L as % of cost basis
- `day_pct` — day P&L as % of opening value
- `is_itm` — in-the-money (options only; boolean)
- `is_ntm` — near-the-money (options only; boolean)
- `days_until_expiry` — for derivatives (integer)

**Rate metrics** (change over time):
- `pnl_rate_abs` — P&L change per minute (INR/min)
- `pnl_rate_pct` — P&L % change per minute (%/min)

**Rolling window metrics** (lookback period):
- `mean` — average P&L over rolling window
- `max_drawdown` — largest peak-to-valley loss in window
- `stdev` — standard deviation of returns in window
- `range` — max − min in window

**Scope**:
- `total` — firm-wide aggregate (all positions, all holdings, all accounts)
- `per_symbol` — per-symbol loop (agent fires once per symbol that matches)
- `per_account` — per-account loop (agent fires once per account that matches)

**Loss agents** (prefix `loss-*`):
- Built-in: 5 defaults (loss-0.5%, loss-1%, loss-2%, loss-5%, loss-10%)
- Editable live via `/automation` UI (no custom loss agents; only modify built-in thresholds)

---

## 4. Agent Lifespan and Status

**Lifespan types**:
- `persistent` (default) — fires indefinitely until operator deactivates
- `one_shot` — fires once, then auto-deactivates (status → completed)
- `n_fires` — fires up to `lifespan_max_fires` times, then auto-deactivates
- `until_date` — fires until `lifespan_expires_at` (ISO datetime UTC), then auto-deactivates (status → expired)

**Lifecycle state machine**:
```
inactive ──(activate)──> active ──(fire)──> triggered ──(cooldown elapse)──> active
                              ↓(expired)        ↓(n_fires hit)
                            expired         completed
```

**Status meanings**:
- `inactive` — not running; operator can activate anytime
- `active` — running; fires when condition true (cooldown respected)
- `triggered` — just fired; in cooldown window (won't re-fire until cooldown expires)
- `completed` — lifespan exhausted (n_fires) or one_shot fired; can be reactivated
- `expired` — until_date passed; non-recoverable (requires manual config change to extend)

---

## 5. Action Types and Dispatch

**Supported actions**:
- `place_order` — create new order (symbol, side, qty, price, product, account)
- `modify_order` — update pending order (new price/qty)
- `cancel_order` — cancel pending order
- `close_position` — flatten an open position (reverse trade, market/limit)
- `chase_close_positions` — iteratively chase a position close (loop with delay)
- `cancel_all_orders` — cancel all pending orders for this agent's symbol (or account if scope=per_account)

**Dispatch flow**:
1. Condition evaluates true
2. Agent enters `triggered` status
3. For each action in `actions[]`:
   - Serialize order payload (symbol, qty, price, etc.)
   - Call broker.place_order() or equivalent
   - Write AlgoOrder row (chain to parent agent_id)
   - Log audit_log entry (category: 'agent')
4. For each notify in `events[]`:
   - Serialize message (title, body, tags)
   - Dispatch to channel (Telegram, Email, Log, WebSocket)
5. Set cooldown timer (agent won't re-fire for `cooldown_minutes`)

---

## 6. Notification Channels

**Channel types**:
- `telegram` — Telegram message (operator token in secrets.yaml)
- `email` — Email (SMTP config in backend_config.yaml)
- `websocket` — Live WebSocket broadcast (active subscribers only)
- `log` — Backend system log

**Message types**:
- Market open/close (Telegram only): "Market open: NSE 09:15 IST"
- Agent fire (Telegram + Email): "RamboQuant Agent: <agent_name> — <detail>"
- Custom (operator-defined): Free-form text

**Dual timezone**:
- Backend uses `timestamp_display()` helper (IST + UTC)
- Telegram shows IST (operator-friendly)
- Email shows both

**Rate limiting**:
- `alert_rate_window_min` (default 10): max N alerts per window
- `alert_cooldown_minutes` (default 30): min gap between agent re-fires
- `alert_baseline_offset_min` (default 15): minimum time into market before alerts fire

---

## 7. Templates and GTT Rules

**TemplatePlan** (saved rule set):
- `symbol`, `side`, `entry_price_hint`, `qty`, `lot_size`
- Attach to incoming order (parent_order_id)
- On parent fill: auto-attach take-profit + trail-stop + OCO wings

**GTT attachment** (`apply_plan_live`):
1. Parent order fills (broker postback)
2. TemplatePlan validated and resolved
3. For each wing leg:
   - Call `broker.translate_qty()` (lots → contracts for F&O)
   - G1 guard: lot_size multiples checked
   - Call `broker.place_gtt()`
4. Adapter ceiling (50-lot MCX, other guards per broker)

**Take-profit auto-attach**:
- On parent fill: auto-create reverse order with target_pct=0.30 (default)
- Fields: `parent_order_id`, `target_pct`, `target_abs`, `basket_tag`
- Skipped if parent order rejected or stays PENDING

**API surface**: `/automation/templates` UI or `/api/agent_templates` CRUD.

---

## 8. Run-in-Simulator Testing

**Scenario**: Operator wants to test an agent rule without live orders.

**Run-in-Simulator button** (on `/automation` page):
1. Operator selects an agent + optional symbol override
2. Click "Run in Simulator" → dispatch to simulator
3. Simulator synthesizes a fake market snapshot:
   - Positions: fabricated from DB (current holdings)
   - Ticks: last SSE snapshot + slight random jitter
   - Quotes: synthetic OHLCV (last close ± random walk)
4. Condition evaluated against synthetic state
5. If fire: log to `agent_events` table with `source='simulator'`
6. Orders created as AlgoOrder rows (mode=sim, no broker execution)
7. Operator sees result in Activity log immediately

**Workflow**:
- Operator defines loss-alert agent: "if pnl < -5000, send Telegram + place close order"
- Clicks "Run in Simulator"
- Simulator creates fake -6000 P&L scenario
- Agent fires, places SIM close order
- Operator verifies order payload in Activity log
- Satisfied, operator activates agent for live use

**Bypasses**:
- All gates (market hours, cooldown, suppression) bypassed
- Fires immediately on "Run in Simulator" click
- Does NOT respect `fire_at_time` (one-time test fire)

---

## 9. Cooldown, Debounce, and Suppression

**Cooldown** (`cooldown_minutes`):
- After agent fires, won't re-fire for N minutes
- Global (applies even if condition stays true)
- Default 30 min; operator-tunable per agent

**Debounce** (`debounce_minutes`, Phase 21):
- Condition must be true for N minutes before agent fires
- Prevents false-positive fires from momentary spikes
- 0 = fire immediately; >0 = wait N min (condition must remain true)

**Suppression** (Phase 22 advanced):
- One agent can suppress another (parent → child blacklist)
- Example: macro-hedge agent suppresses loss alerts during big unwind
- Configured via `tags` + `blackout_windows` (free-form IST time ranges)

**Fire-at-time** (`fire_at_time`, HH:MM IST):
- Optional; when set, agent fires at most once per IST calendar day
- Fires within small window around wall-clock time (e.g., fire once at 16:00 daily)
- Useful for nightly summary alerts, daily rebalance

---

## 10. Edge Cases

### Condition tree with no leaves

- All composite (only `all`/`any`/`not` nodes, no metrics)
- Rejected as malformed on create (400 HTTP error)
- Existing agents with bad trees don't fire (safe default)

### Agent fires, action rejected

- Place-order action fails (insufficient margin, circuit breaker, etc.)
- Agent status stays `triggered`; last_error logged
- Cooldown still applied (prevents retry spam)
- Operator sees error in Activity log

### Derivative expiry during active session

- Agent has condition `days_until_expiry < 3` (within 3 days of expiry)
- On last trading day (expiry_date == today): days_until_expiry = 0
- Condition fires; agent closes position or sends alert
- On expiry date +1: position auto-settled by broker; agent won't re-fire (quantity=0)

### Multi-symbol agent scope=per_symbol

- Condition applies to each symbol independently
- Agent fires 3 times (once per symbol that matches) in same evaluation cycle
- Orders tagged with symbol in message; 3 separate activity entries

### Lifespan n_fires exhausted mid-session

- Agent fires 5th time (n_fires=5)
- Status immediately → completed; cooldown NO longer applies
- Operator can reactivate (reset trigger_count) or delete

---

## 11. API Contract

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/agents/` | GET | Required | List all agents |
| `/api/agents/{slug}` | GET | Required | Single agent detail |
| `/api/agents/` | POST | Required | Create agent |
| `/api/agents/{slug}` | PUT | Required | Update agent |
| `/api/agents/{slug}/activate` | PUT | Required | Activate agent |
| `/api/agents/{slug}/deactivate` | PUT | Required | Deactivate agent |
| `/api/agents/{slug}/delete` | DELETE | Admin | Delete agent |
| `/api/agents/{slug}/events` | GET | Required | Agent fire history |
| `/api/agents/interpret` | POST | Required | Terminal command parser |
| `/api/agent_templates/` | GET | Required | List saved GTT templates |
| `/api/agent_templates/` | POST | Required | Create template |

**Agent create/update payload**:
```json
{
  "name": "Loss Alert 5%",
  "slug": "loss-5pct",
  "conditions": {
    "metric": "pnl_pct",
    "scope": "total",
    "op": "<",
    "value": -5.0
  },
  "events": [
    {"channel": "telegram", "title": "RamboQuant Alert", "message": "Loss 5%"}
  ],
  "actions": [
    {"type": "place_order", "symbol": "NIFTY25APR22000CE", "side": "SELL", "qty": 1, "price": null}
  ],
  "scope": "total",
  "schedule": "market_hours",
  "cooldown_minutes": 30,
  "lifespan_type": "persistent",
  "debounce_minutes": 0
}
```

**Agent event response**:
```json
{
  "id": 123,
  "agent_id": 45,
  "event_type": "fire|error",
  "triggered_at": "2026-01-15T09:30:00Z",
  "fired": true,
  "detail": "Condition evaluated true; 2 actions dispatched"
}
```

---

## 12. Test Coverage Map

### Frontend — Playwright

- **Condition tree edit**: Add leaf (metric, scope, op, value); delete node; toggle all/any/not
- **Agent activation**: Inactive → Active → Triggered (after market event); status persists
- **Run-in-Simulator**: Select agent, click button, verify SIM order in Activity log
- **Template attachment**: Fill parent order → auto-attach take-profit + trail-stop
- **Notification preview**: Configure agent, see Telegram/Email preview in edit form

### Backend — pytest

- **Condition eval**: `all/any/not` logic correct; nested trees evaluate correctly
- **Agent fire**: Condition true → status triggered, actions dispatched, cooldown set
- **Cooldown**: Re-fire blocked within cooldown window; fires after window elapses
- **Debounce**: Condition true for < debounce_min doesn't fire; ≥ fires
- **Lifespan n_fires**: Agent fires N times, then auto-deactivates (status → completed)
- **GTT attach**: Parent fill triggers `apply_plan_live`, wings placed with correct qty translation
- **Suppression**: Parent agent suppresses child agent events during blackout window

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit; condition tree v2, lifespan, action dispatch, templates |
