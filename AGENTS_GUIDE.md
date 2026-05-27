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

## Testing your agent — the validation ladder

The platform ships **four** test surfaces, ordered by realism + risk:

| Stage | Where | What it does | Risk |
|---|---|---|---|
| 1 — Validate | `/agents` editor → **Validate** button | Static check: every token + ref resolves; tree shape is well-formed | none |
| 2 — Dry-run | `/agents/<slug>/dry-run` API or `Dry-run` button | Evaluates the condition tree against CURRENT live market state, but does NOT fire | none |
| 3 — Simulator | `/admin/execution?mode=sim` or the **Run in Simulator** button on each agent row | Fabricated price moves; condition evaluator + dispatcher + actions all run with `sim_mode=True`. Telegram / email pings are prefixed `SIMULATOR` so you don't confuse them with real fires | low — paper orders only |
| 4 — Activate | `/agents` → flip **Status: inactive → active** | Real ticks, real broker, real money (in LIVE mode) or paper engine (in PAPER mode) | full |

### Stage 1 — Validate (instant feedback)

Open the agent in `/agents`, click **Edit**, paste a condition tree, click **Validate**. Errors list:

- `unknown metric token 'pnl_typo'`
- `unknown condition fragment 'frag-name'`
- `cycle — 'frag-a' is already in the resolution chain`

Validate uses the same `agent_evaluator.validate()` the engine uses, so a passing validate means the tree's well-formed against the live grammar registry.

### Stage 2 — Dry-run

Returns the result of evaluating the agent's condition tree against right-now live market data, without actually firing.

```bash
curl -s -H "Authorization: Bearer $TOK" \
  https://ramboq.com/api/agents/loss-positions-total/dry-run | jq
```

Response:

```jsonc
{
  "agent_slug":   "loss-positions-total",
  "matches":      [/* one entry per leaf that matched */],
  "match_count":  3,
  "would_fire":   true,
  "blocked_by":   null,             // or "schedule" | "cooldown" | "fire_at_time" | "blackout" | "debounce" | "eval_error"
  "evaluated_at": "2026-05-27T08:35:51+00:00"
}
```

Use cases:
- **Sanity-check a new agent against current market state** before activating
- **Verify a known-loss scenario** triggers as expected
- **Debug `blocked_by`** when the agent isn't firing on real ticks

### Stage 3 — Run in Simulator (recommended for every new agent)

This is the most powerful test. Each `/agents` row has a **Run in Simulator** button that:

1. Synthesises a scenario that will trip THIS agent's first leaf (no `scenarios.yaml` entry needed)
2. Drops you on `/admin/execution?mode=sim&agent_id=<id>` with the agent armed
3. Bypasses cooldown / baseline / schedule gates so the agent fires immediately
4. Mutates SIM positions only — no real broker contact

What you see:
- **Telegram / email**: pings arrive with `SIMULATOR` prefix + red `⚠ SIMULATOR RUN` banner
- **`/agents/activity`** automatically scopes to sim events when a sim is running
- **`AlgoOrder` rows** for any actions land with `mode='sim'` + `SIM` pill in the order log

See [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md) for the full simulator workflow — scenarios, custom positions, iteration mode.

### Stage 4 — Activate

Once stages 1-3 pass, flip the agent's **Status** from `inactive` to `active` on `/agents`. The engine picks it up on the next tick.

Watch the agent on:
- `/agents/activity` — first real fire shows up here
- Telegram alert channel — first fire pings you within ~30 s of the tick
- The agent row's **Events** panel — full firing history

---

## Operational gates — why your agent isn't firing

Six gates run between "tick happens" and "alert dispatches". Dry-run's `blocked_by` field names the one that stopped you.

1. **schedule** — `schedule: market_hours` agents skip outside session hours (NSE 09:15-15:30 IST equity, MCX 09:00-23:30 IST commodity). Match the agent's exchange.
2. **cooldown** — after a fire, the agent is in `status: cooldown` for `cooldown_minutes` (default 30). Subsequent matches are suppressed.
3. **baseline** — rate-of-change metrics (`*_rate_*`) silently return None for the first `alerts.baseline_offset_min` (default 15) minutes of the session, so they don't fire on cold-start when there's no history.
4. **fire_at_time** — when set, the agent only evaluates inside a 30-min window centred on the chosen IST time.
5. **blackout** — `blackout_windows` like `[{start: "12:00", end: "13:00"}]` block firing during the listed IST ranges. Midnight-crossing windows work (`{start: "23:00", end: "01:00"}`).
6. **debounce** — when `debounce_minutes > 0`, the condition must hold continuously for that many minutes. The first true tick arms a latch; the agent fires only when the latch survives the debounce window. Single-tick spikes don't trip it.
7. **suppression** — after a fire, re-fire requires the cooldown AND `|Δpnl| ≥ alerts.suppress_delta_abs` OR `|Δpct| ≥ alerts.suppress_delta_pct`. Flat losses go silent for the rest of the session.

Plus one final gate that ONLY affects actions (not notifications):

8. **exchange-open gate** (Phase 23) — `place_order` / `modify_order` / `close_position` actions are blocked when the order's target exchange's market is closed. Bypassed by sim and replay modes.

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

## Action handlers — what gets called when an agent fires

| Token | Real-mode handler | Sim-mode handler |
|---|---|---|
| `place_order` | `broker.place_order(...)` | Writes an `AlgoOrder(mode='sim')` row with `initial_price = sim bid/ask` |
| `chase_close_positions` | `ExpiryEngine.close_positions(...)` adaptive chase | Per-position rows in SimDriver's chase queue, fills via the spread-aware simulator chase loop |
| `close_position` | One-shot LIMIT at current LTP | Paper `AlgoOrder` row at sim LTP |
| `cancel_order` / `modify_order` | Real broker calls | Updates the sim's open-order book |
| `monitor_order` | Polls broker every N seconds for status | Polls sim driver state |
| `deactivate_agent` | DB update — sets agent.status='inactive' | Same — sim doesn't isolate this from real |
| `set_flag` | Writes to `alert_state` flag dict | Same |
| `emit_log` | Writes `agent_events` row with kind='log' | Same — sim_mode tag flows through |

`engine='sim'` + `mode='sim'` on the AlgoOrder row distinguish sim fills from real broker fills. The order-log Mode pill (SIM / PAPER / LIVE / SHADOW) makes it visual.

---

## Common patterns — copy-paste starting points

### "Alert me when this account's positions cross -5%"

```jsonc
{
  "slug": "my-acct-loss-5pct",
  "name": "Specific account loss > 5%",
  "conditions": {
    "metric": "pnl_pct",
    "scope":  "positions.any_acct",
    "op":     "<=", "value": -5.0
  },
  "events": [ {"$ref": "notify-critical-trio"} ],
  "cooldown_minutes": 30
}
```

### "Wake me only if loss persists for 10 min"

```jsonc
{
  "slug": "my-persistent-loss",
  "name": "Persistent loss (10 min hold)",
  "debounce_minutes": 10,
  "conditions": {
    "metric": "pnl", "scope": "positions.total",
    "op": "<=", "value": -25000
  },
  "events": [ {"$ref": "notify-telegram-only"} ]
}
```

### "Fire at exactly 14:30 IST, check the close-time guard fragment"

```jsonc
{
  "slug": "my-near-close-check",
  "name": "Near-close drawdown check",
  "fire_at_time": "14:30",
  "conditions": { "all": [
    {"$ref": "loss-positions-total-default"},
    {"$ref": "near-market-close-30m"}
  ]},
  "events": [ {"$ref": "notify-critical-trio"} ]
}
```

### "One-shot: alert me once if BANKNIFTY drops 2% today"

```jsonc
{
  "slug": "bn-down-2pct-today",
  "name": "BankNifty -2% one-shot",
  "lifespan_type": "one_shot",
  "conditions": {
    "metric": "day_pct",
    "scope":  "holdings.any_acct",
    "op":     "<=", "value": -2.0
  },
  "tags": ["one-shot", "banknifty"]
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
