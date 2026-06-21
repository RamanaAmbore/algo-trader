# Agents — Operator Test Guide

This guide walks an operator from "what is an agent" to "I shipped one and watched it fire on production". Pair with [ADMIN_GUIDE.md](ADMIN_GUIDE.md) for the day-to-day operations reference and [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md) for the sim workflow.

---

## TL;DR — the four-word vocabulary

| Word | Meaning |
|---|---|
| **Agent** | A rule row in the `agents` table. Evaluated every 5-min tick during market hours. |
| **Alert** | The runtime event an agent emits when its condition fires. Persisted to `agent_events`. |
| **Notify** | A delivery channel (telegram / email / log / websocket). |
| **Action** | A side-effect the alert invokes (place order, close position, set flag, etc.). |

Mental model: condition fires → alert emitted → notifies dispatch → actions execute.

---

## Where everything lives

| Surface | URL | Purpose |
|---|---|---|
| Agents list | `/agents` | Rule editor — create, edit, activate, deactivate, dry-run, run-in-sim |
| Activity | `/agents/activity` | Recent fires (real, not sim) |
| Tokens | `/admin/tokens` | Grammar catalog — every metric / scope / op / action |
| Fragments | `/agents/fragments` | Reusable saved sub-trees (notify channel sets + condition snippets) |
| Lab | `/admin/research` | LLM-driven research → draft agents via Claude Code MCP |
| Simulator | `/admin/execution?mode=sim` | Fabricated price-move workspace for dry-firing agents |

---

## Anatomy of an agent

Stored as a row in the `agents` table:

```jsonc
{
  "slug":             "loss-positions-total",
  "name":             "Positions total loss guardrail",
  "description":      "…",
  "tier":             "critical",          // "low" | "medium" | "high" | "critical"
  "topic":            "positions_loss",    // free-text bucket
  "schedule":         "market_hours",      // "market_hours" | "never" | "always"
  "status":           "active",            // "active" | "inactive" | "cooldown"
  "trade_mode":       null,                // forces a specific mode; null = follow execution.paper_trading_mode
  "cooldown_minutes": 30,
  "debounce_minutes": 0,                   // condition must hold for N min before firing
  "blackout_windows": [],                  // [{start: "23:00", end: "01:00"}]
  "fire_at_time":     null,                // "HH:MM" — restricts firing to one window per day
  "lifespan_type":    "perpetual",         // "perpetual" | "one_shot" | "n_fires" | "until_date"
  "lifespan_max_fires": null,
  "lifespan_expires_at": null,
  "tags":             [],
  "conditions":       { /* condition tree, see below */ },
  "events":           [ { "channel": "telegram", "enabled": true }, … ],
  "actions":          [ { "type": "chase_close_positions", "params": { … } }, … ]
}
```

Every field except `slug` + `conditions` has a sane default — the minimum agent is `{slug, name, conditions}`.

---

## The condition tree

Conditions are a recursive JSON tree. Three composite forms + one leaf:

```text
condition ::=  leaf
            |  { "all": [condition, …] }       AND  — every child must fire
            |  { "any": [condition, …] }       OR   — at least one
            |  { "not": condition }            NOT  — child must NOT fire
            |  { "$ref": "<fragment-name>" }   REF  — substitute a saved fragment

leaf      ::=  { "metric": <metric>,
                 "scope":  <scope>,
                 "op":     <op>,
                 "value":  <literal> }
```

A leaf fires when `op(metric(ctx, row), value)` is true for **at least one** row from `scope(ctx)`.

### Example — "fire when total P&L ≤ -₹50k OR drawdown over 1h ≤ -₹100k"

```jsonc
{
  "any": [
    { "metric": "pnl",              "scope": "positions.total", "op": "<=", "value": -50000 },
    { "metric": "max_drawdown_pnl_1h", "scope": "positions.total", "op": "<=", "value": -100000 }
  ]
}
```

### Example — "fire only near market close AND book is bleeding"

```jsonc
{
  "all": [
    { "$ref": "loss-positions-total-default" },
    { "metric": "minutes_until_close", "scope": "positions.total", "op": "<=", "value": 30 }
  ]
}
```

### Example — "fire when a futures contract is expiring today"

```jsonc
{
  "metric": "days_until_expiry",
  "scope":  "positions.expiring_today",
  "op":     "<=",
  "value":  1.5
}
```

---

## The grammar — what tokens you can use

Read the live catalog at `/admin/tokens`. The full canonical list is in [CLAUDE.md § Agent Framework](CLAUDE.md). Highlights:

### Metrics (number-producing)

**Point-in-time** — `pnl`, `pnl_pct`, `day_val`, `day_pct`, `inv_val`, `cur_val`, `cash`, `avail_margin`, `used_margin`, `collateral`.

**Rate of change** (over `alerts.rate_window_min`, default 10 min) — `pnl_rate_abs`, `pnl_rate_pct`, `day_rate_abs`, `day_rate_pct`.

**Rolling-window aggregates** (Phase 24) — `mean_pnl_30m / _1h`, `mean_day_30m / _1h`, `max_drawdown_pnl_30m / _1h / _4h`, `max_drawdown_pnl_pct_30m / _1h`, `max_drawdown_day_1h`, `stdev_pnl_30m / _1h`, `range_pnl_30m / _1h`.

**Time** — `minutes_since_open`, `minutes_until_close`.

**Expiry-aware** (Phase 25) — `days_until_expiry`, `is_itm`, `is_ntm`.

### Scopes (row selectors)

**Aggregate** — `positions.total`, `positions.any_acct`, `positions.worst_acct`, `holdings.total`, `holdings.any_acct`, `holdings.worst_acct`, `holdings.worst_symbol`, `positions.worst_acct`, `funds.total`, `funds.any_acct`.

**Per-symbol** (Phase 25) — `positions.expiring_today`.

### Operators

`<`, `<=`, `==`, `!=`, `>=`, `>`.

### Action types

`place_order`, `modify_order`, `cancel_order`, `cancel_all_orders`, `chase_close_positions`, `close_position`, `monitor_order`, `deactivate_agent`, `set_flag`, `emit_log`. See [CLAUDE.md § Action grammar](CLAUDE.md) for parameter schemas.

---

## Fragments — reuse without copy-paste

Two kinds at `/agents/fragments`:

**Notify fragments** — saved channel lists. Reference from `agent.events`:

```jsonc
{ "events": [ {"$ref": "notify-critical-trio"} ] }
```

Three seeded today: `notify-critical-trio` (telegram + email + log), `notify-log-only`, `notify-telegram-only`.

**Condition fragments** — saved sub-trees. Reference from `agent.conditions`:

```jsonc
{
  "conditions": { "all": [
    {"$ref": "loss-positions-total-default"},
    {"$ref": "near-market-close-30m"}
  ]}
}
```

Three seeded today: `loss-positions-acct-default`, `loss-positions-total-default`, `near-market-close-30m`.

Edit a fragment once → every consumer agent updates. Cycle detection prevents A→B→A from blowing the stack.

---

## Testing your agent — the four-stage ladder

| Stage | Location | What it does | Risk |
|---|---|---|---|
| 1 — Validate | `/agents` → **Validate** | Static: tokens + shape well-formed | none |
| 2 — Dry-run | `/agents/<slug>/dry-run` or button | Evaluate against live data (no fire) | none |
| 3 — Simulator | `/agents` → **Run in Simulator** | Synthetic ticks, `sim_mode=True`, no real broker | low |
| 4 — Activate | `/agents` → flip Status to active | Real ticks, real money (LIVE) or paper | full |

**Validate:** `/agents` editor → click **Validate**. Reports token typos + shape errors.

**Dry-run:** Returns matches + `would_fire` bool against current market WITHOUT firing. Check `blocked_by` field if you expect true but see false.

**Run in Simulator:** Synthesises a scenario to trip THIS agent's first leaf. Bypasses gates (cooldown / baseline / schedule) so the agent fires immediately on the first tick. Telegram / email pings carry `SIMULATOR` prefix. See [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md).

**Activate:** Flip `status: inactive → active`. Engine picks it up next tick. Watch `/agents/activity`, Telegram, or agent row's Events panel.

---

## Operational gates — why your agent isn't firing

Eight gates between tick and dispatch. Dry-run's `blocked_by` names the culprit.

1. **schedule** — `market_hours` skips outside NSE 09:15-15:30 / MCX 09:00-23:30 IST
2. **cooldown** — default 30 min; suppresses re-fires
3. **baseline** — rate metrics silent for first 15 min (no history)
4. **fire_at_time** — when set, fires only in ±15 min window around HH:MM IST
5. **blackout** — `[{start: "12:00", end: "13:00"}]` blocks windows (midnight-crossing OK)
6. **debounce** — condition must hold for N min continuously
7. **suppression** — re-fire requires cooldown + `|ΔP&L| ≥ threshold` (flat loss → silent)
8. **exchange-open** — actions blocked when target exchange closed (sim/replay exempt)

---

## Lifespan — let the agent retire itself

Three options beyond perpetual:

| `lifespan_type` | Behaviour | Use case |
|---|---|---|
| `perpetual` (default) | Runs forever until you deactivate | Long-running guardrails |
| `one_shot` | Auto-deactivates after first fire | "Alert me once when X happens today" |
| `n_fires` | Set `lifespan_max_fires=N`. Auto-deactivates on the Nth fire | Bounded campaigns |
| `until_date` | Set `lifespan_expires_at`. Auto-deactivates when wall-clock IST passes the date | Event-window agents |

Auto-deactivation is final — to re-enable, flip `status` back to `active` on `/agents`.

---

## Built-in agents you can study

Open `/agents` and look at these — every one is a teaching example you can clone:

| Slug | Topic | Why it's worth reading |
|---|---|---|
| `loss-positions-acct` | per-account guardrail | Uses an `any:` block to OR four threshold types |
| `loss-positions-total` | book-wide guardrail | Same shape, scoped to TOTAL |
| `loss-pos-total-auto-close` | destructive action | Wraps `chase_close_positions` — ships INACTIVE for a reason |
| `expiry-day-positions-alert` | expiry alert | Uses `days_until_expiry` + `positions.expiring_today` |
| `expiry-day-itm-auto-close` | expiry close (INACTIVE) | Combines `is_itm` + `chase_close_positions` |
| `manual` | order-trail | Doesn't run on a tick — every manual order writes here for audit |

Built-in agents are **force-reseeded on every boot** — your changes to their `conditions / cooldown / events / actions` are PRESERVED, but `slug / schedule / status` are pinned to code. To customise, clone to a new slug.

---

## Authoring workflow

1. **Spike the condition tree** in the `/agents` editor or via the Claude Code MCP (see [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md))
2. **Validate** — clear all token / shape errors
3. **Dry-run** — sanity check against current market
4. **Run in Simulator** — confirm the alert fires + actions log correctly with the `SIM` pill
5. **Activate on prod with `status: inactive` + a one-shot lifespan** — fire once, observe, deactivate
6. **Promote to perpetual** once you've watched it behave on real ticks

---

## Action handlers

| Token | Real | Sim |
|---|---|---|
| `place_order` | Broker call | `AlgoOrder(mode='sim')` at sim LTP |
| `chase_close_positions` | Adaptive chase via ExpiryEngine | SimDriver chase queue, fills via spread |
| `close_position` | LIMIT at LTP | Paper row at sim LTP |
| `cancel_order` / `modify_order` | Real broker | Sim order book |
| `monitor_order` | Poll broker every N sec | Poll sim driver |
| `deactivate_agent` | DB flip to inactive | Same (shared state) |
| `set_flag` / `emit_log` | Write state | Same (sim_mode tag flows) |

The Order log Mode pill (SIM / PAPER / LIVE / SHADOW) visualises the difference.

**TP auto-attach:** When `place_order` fills, an automatic TP order fires on the flip side at `fill_price × (1 + algo.default_target_pct)` (default 0.30). Idempotent via `parent_order_id` guard. Works in paper, live, and sim.

---

## Common patterns (copy-paste templates)

**Per-account loss -5%:**
```jsonc
{
  "slug": "my-acct-5pct", "name": "Account loss > 5%",
  "conditions": { "metric": "pnl_pct", "scope": "positions.any_acct", "op": "<=", "value": -5.0 },
  "events": [ {"$ref": "notify-critical-trio"} ]
}
```

**Persistent loss (10 min debounce):**
```jsonc
{
  "slug": "my-persistent", "debounce_minutes": 10,
  "conditions": { "metric": "pnl", "scope": "positions.total", "op": "<=", "value": -25000 }
}
```

**Fire at 14:30 IST only:**
```jsonc
{
  "slug": "near-close-check", "fire_at_time": "14:30",
  "conditions": { "all": [{"$ref": "loss-positions-total-default"}, {"$ref": "near-market-close-30m"}] }
}
```

**One-shot: BANKNIFTY -2% today:**
```jsonc
{
  "slug": "bn-2pct-once", "lifespan_type": "one_shot",
  "conditions": { "metric": "day_pct", "scope": "holdings.any_acct", "op": "<=", "value": -2.0 }
}
```

---

## Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| `Validate` rejects with `unknown metric token` | Typo in metric name OR token deactivated on `/admin/tokens` | `/admin/tokens` → Condition tab → search the token |
| `dry-run` shows `would_fire: false` but you expect true | Condition mismatch — operator's threshold vs current state | Use the dry-run `matches` array; each entry shows the metric, scope, threshold, and actual value |
| `dry-run` shows `blocked_by: "schedule"` | Agent has `schedule: market_hours` but markets are closed | Either wait for session, or flip `schedule: always` for diagnostic agents |
| Agent never fires on real ticks | Rate metric without baseline crossed; or in cooldown; or suppressed | `/agents/<slug>` Events tab + `/admin/alerts` log; or set `cooldown_minutes: 0` temporarily |
| Sim shows alert + action but real ticks don't | Real `_task_performance` skipped because sim was active — sims auto-stop in 30 min by default | Stop the sim or wait for auto-stop |
| Action wrote an `AlgoOrder` row but broker didn't see it | `execution.paper_trading_mode: true` — paper engine handled it, real broker untouched | Flip mode via navbar dropdown → LIVE for prod |
| Action raised on prod with `409 Exchange closed` | Phase 23 gate — symbol's exchange is closed | Wait for session; sim mode bypasses |

---

## See also

- [USER_GUIDE.md](USER_GUIDE.md) — concepts in plain English for first-time operators
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) — exact button labels, API endpoints, condition-tree JSON, config keys
- [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md) — extensive simulator testing workflow
- [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md) — LLM-driven agent authoring via Claude Code
- [CLAUDE.md § Agent Framework](CLAUDE.md) — architectural reference for engineers
