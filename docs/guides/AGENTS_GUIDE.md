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
| Agents list | `/automation` | Rule editor — create, edit, activate, deactivate, dry-run, run-in-sim |
| Brackets | `/automation/templates` | Per-position TP/SL/wing/scale/trail exit rules attached at order fill |
| Agent Templates | `/automation/agent-templates` | Reusable saved sub-trees (notify channel sets + condition snippets) |
| Activity | `/activity?tab=agent` | Recent fires (real, not sim) |
| Tokens | `/admin/tokens` | Grammar catalog — every metric / scope / op / action (Config group) |
| MCP | `/admin/mcp` | LLM-driven research → draft agents via Claude Code MCP (Explore group; tabs: Research, Drafts, Audit, Settings) |
| Simulator | `/admin/execution?mode=sim` | Fabricated price-move workspace for dry-firing agents (Explore group) |

Legacy `/agents`, `/agents/activity`, `/agents/fragments` paths still
308-redirect for old bookmarks; new docs link the canonical URLs.
`/automation/activity` redirects to `/activity?tab=agent` (Activity page, Agent tab).

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
  "events":           [ { "channel": "telegram", "enabled": true }, … ],  // ntfy routed at urgent priority for agent_alert events

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

**Point-in-time** — `pnl`, `pnl_pct`, `day_val`, `day_pct`, `inv_val`, `cur_val`,
`cash`, `sod_cash`, `avail_margin`, `used_margin`, `collateral`.

The `day_val` metric reads `day_change_val` from positions data — today's session
P&L calculated from the previous session's settlement close price. This is the same
value displayed in the NavStrip as "Day P&L". Use `day_val` to detect intra-session
losses independent of the position's total unrealized P&L (e.g. an option that gained
on entry but lost ground today will fire a loss alert on `day_val` alone, even if
`pnl` remains positive overall).

For `pnl_pct` metric: when `util_debits = 0` (intraday/MIS positions with no margin utilization), the metric falls back to using `net` (available) margin as the denominator. Returns `None` (leaf skipped) only when both denominator options are zero.

**Rate of change** (over `alerts.rate_window_min`, default 10 min) — `pnl_rate_abs`,
`pnl_rate_pct`, `day_rate_abs`, `day_rate_pct`. These metrics return `None` (and are
silent — no alert fires) until at least 3 samples span ≥80% of the effective rate
window (widened from configured window when observed poll cadence is slower than
2.2× the samples' median gap). This filters out single-tick noise and window-boundary
spikes.

**Rolling-window aggregates** (Phase 24) — `mean_pnl_30m / _1h`, `mean_day_30m / _1h`, `max_drawdown_pnl_30m / _1h / _4h`, `max_drawdown_pnl_pct_30m / _1h`, `max_drawdown_day_1h`, `stdev_pnl_30m / _1h`, `range_pnl_30m / _1h`.

**Parameterized call syntax** (Phase 26) — any rolling-window metric above can also be written as a function call with an arbitrary window in minutes, e.g. `mean_pnl(45)`, `max_drawdown_pnl(90)`, `stdev_pnl(120)`, `range_pnl(15)`, `mean_day(20)`, `max_drawdown_day(180)`, `max_drawdown_pnl_pct(30)`. Only a single, bare numeric literal argument is accepted — no expressions, names, or keyword args. The fixed tokens (`mean_pnl_30m` etc.) remain as permanent shortcuts for the common windows and are unaffected.

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

## The CLI tab — single-line agent authoring

A faster authoring path for threshold agents: type a single-line statement in the 
CLI tab instead of hand-editing JSON. The statement compiles live to the same 
JSON tree the Structured tab produces.

### Statement shape

```
WHEN <condition> [ALERT <channels>] [DO <actions>]
```

Both `ALERT` and `DO` are independently optional — you can omit either or both 
(but not both together). Order is flexible: `DO` can come before `ALERT`.

Examples:

```
WHEN pnl@positions.total <= -50000 ALERT telegram DO nop
```

(Fire when total P&L drops to -₹50k, notify Telegram only, no action.)

```
WHEN max_drawdown_pnl(minutes=90)@funds.any_acct <= -100000 DO 
  close_position(exchange="NFO")
```

(Fire when any account's 90-min max drawdown hits -₹100k; close F&O positions, 
no notification.)

```
WHEN always ALERT nop DO emit_log(message="Market open check")
```

(Schedule-only agent; fires on the market-open tick, logs a message.)

### Syntax highlights

**Boolean logic**: `&` (AND) and `|` (OR) have equal precedence, left-to-right 
evaluation — **use parentheses to disambiguate**:
```
WHEN (pnl@positions.total <= -50000 & day_pct@positions.total <= -2) 
  | pnl_rate_abs@positions.total <= -5000
```

**NOT**: `~` prefix:
```
WHEN ~(pnl@positions.total > 0)
```

**Metric windows**: Parameterized call syntax for arbitrary rolling windows:
```
WHEN mean_pnl(minutes=45)@positions.total <= -25000
WHEN max_drawdown_day(minutes=120)@holdings.total <= -50000
```

**Arguments**: Keyword-only (every arg is `name=value`, even the first). 
Shorthand aliases provided:
- `acct` ← `account`, `sym` ← `symbol`, `exch` ← `exchange`, `otype` ← `order_type`,
  `px` ← `price`, `prod` ← `product`, `var` ← `variety`, `chase` ← `chase_level`

```
place_order(acct="ZG0790", sym="NIFTY25JULFUT", side="BUY", qty=1)
```

**List literals**: Allowed only on the right-hand side of a condition:
```
WHEN side@positions in ["BUY", "SELL"]
```

**Reserved words**: `when`, `alert`, `do`, `nop`, `always`, `in`, `not`, `true`, 
`false` — cannot be used as metric/scope/action names (parser rejects them).

### Editing workflow

- **Create new**: Start in the CLI tab, type your statement. Live compilation 
  updates the JSON preview below as you type (debounce ~150ms). Errors appear 
  in red.
- **Edit existing**: Open an agent's edit panel. If its JSON cleanly decompiles to 
  CLI text, the CLI tab shows the equivalent statement by default. If not 
  (e.g., has a `$ref` fragment), the tab is greyed out and you must use Structured.
- **Switching tabs**: Changing from Structured to CLI re-decompiles the current 
  JSON if you edited it since the last sync. Invalid JSON blocks the switch. 
  Switching back to Structured preserves any JSON edits.
- **Save**: Click Save from either tab. Structured saves clear any prior CLI text; 
  CLI saves persist the typed statement as audit/history in the `cli_source` field.

### Direct order placement (no agent)

Type a bare `order(...)` call (or several comma-separated) with no `WHEN`:

```
order(acct="ZG0790", sym="NIFTY25JULFUT", side="BUY", qty=1, px=25500)
```

The CLI tab shows a **Place order** button instead of Save. Clicking it submits 
directly to the broker (single leg via ticket endpoint, multiple legs via basket). 
LIVE mode orders require confirmation before submission.

**Caution**: This places a real broker order immediately — not a template, not 
a simulator. Confirm the statement before clicking Place order.

---

## Fragments — reuse without copy-paste

Two kinds at `/automation/agent-templates`:

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
| 1 — Validate | `/automation` → **Validate** | Static: tokens + shape well-formed | none |
| 2 — Dry-run | `/api/agents/<slug>/dry-run` or button | Evaluate against live data (no fire) | none |
| 3 — Simulator | `/automation` → **Run in Simulator** | Synthetic ticks, `sim_mode=True`, no real broker | low |
| 4 — Activate | `/automation` → flip Status to active | Real ticks, real money (LIVE) or paper | full |

**Validate:** `/automation` editor → click **Validate**. Reports token typos + shape errors.

**Dry-run:** Returns matches + `would_fire` bool against current market WITHOUT firing. Check `blocked_by` field if you expect true but see false.

**Run in Simulator:** Synthesises a scenario to trip THIS agent's first leaf. Bypasses gates (cooldown / baseline / schedule) so the agent fires immediately on the first tick. Telegram / email pings carry `SIMULATOR` prefix. See [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md).

**Activate:** Flip `status: inactive → active`. Engine picks it up next tick. Watch `/activity?tab=agent`, Telegram, or agent row's Events panel.

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

## Alert routing for loss agents

Loss agents (`loss-positions-acct`, `loss-rate-acct`, `loss-positions-total`) emit
`agent_alert` events that route to **all configured channels at urgent priority**.
This includes ntfy.sh in addition to Telegram and email. Routing matches the priority
of `order_failure` alerts — operators monitoring ntfy see loss events immediately.

**Three built-in loss agents — behaviour and gating:**

- **`loss-positions-acct`** (high tier, 30-min cooldown) — per-account absolute loss.
  Includes conditions for both `pnl` (total unrealized P&L) and `day_val` (intra-session
  loss from prior close). Suppressed by `loss-positions-total` when both fire on the
  same tick. Routes to ntfy, Telegram, email at urgent priority.

- **`loss-rate-acct`** (critical tier, 10-min cooldown + 15-min baseline window) —
  per-account rate-of-loss. Fires only when **both** conditions breach simultaneously:
  absolute loss rate ≤ -₹10,000/min **AND** percentage loss rate ≤ -0.25%/min. Single-
  condition breaches (only absolute OR only percentage) do not trigger. Blocked from
  firing for the first 15 minutes after market open (baseline window, by design) — no
  early false alarms. Routes to ntfy, Telegram, email at urgent priority.

- **`loss-positions-total`** (critical tier, suppresses per-acct) — book-wide absolute
  loss. Includes conditions for both `pnl` and `day_val`. Fires independently; suppresses
  re-fire of `loss-positions-acct` on same topic. Routes to ntfy, Telegram, email at
  urgent priority.

**Simulator engine suppression:** When the simulator is running (`execution.sim_mode:
true`), all real loss alerts are blocked — only sim-tagged events fire. This prevents
cross-alert noise during dry-runs. Stop the sim or wait 30 min for auto-stop.

---

## Lifespan — let the agent retire itself

Three options beyond perpetual:

| `lifespan_type` | Behaviour | Use case |
|---|---|---|
| `perpetual` (default) | Runs forever until you deactivate | Long-running guardrails |
| `one_shot` | Auto-deactivates after first fire | "Alert me once when X happens today" |
| `n_fires` | Set `lifespan_max_fires=N`. Auto-deactivates on the Nth fire | Bounded campaigns |
| `until_date` | Set `lifespan_expires_at`. Auto-deactivates when wall-clock IST passes the date | Event-window agents |

Auto-deactivation is final — to re-enable, flip `status` back to `active` on `/automation`.

---

## Built-in agents you can study

Open `/automation` and look at these — 14 builtin agents include teaching examples:

| Slug | Topic | Why it's worth reading |
|---|---|---|
| `loss-positions-acct` | per-account guardrail (status=inactive) | Uses an `any:` block to OR two thresholds (`day_val ≤ -30k`, `day_pct ≤ -2%`); routes to ntfy at high priority |
| `loss-rate-acct` | per-account burn-rate (10-min cooldown) | Rate-of-loss metric + hysteresis; fires only when **both** absolute AND percentage conditions hold; silent first 15 min; routes to ntfy at urgent priority |
| `loss-positions-total` | book-wide guardrail (critical tier) | Uses `any:` to OR four sub-conditions; suppresses `loss-positions-acct` on same fire; routes to ntfy at urgent priority |
| `loss-margin-low` | margin warning (status=inactive) | Disabled: cross-account false positive with Dhan/Groww |
| `loss-funds-negative` | negative cash or margin (critical) | Fire when either balance goes < 0 |
| `loss-pos-total-auto-close` | auto-close on loss (status=inactive) | Destructive: takes action `expiry_auto_close` with scope total; ships INACTIVE |
| `expiry-day-positions-alert` | expiry review (status=inactive) | Notifies of positions expiring today; 180-min cooldown (one alert per half-day) |
| `expiry-day-equity-itm-auto-close` | equity expiry auto-close (status=inactive) | Fires at 15:15 IST on expiry day; action `expiry_auto_close` exchange=NFO; ships INACTIVE |
| `expiry-day-commodity-itm-auto-close` | commodity expiry auto-close (status=inactive) | Fires at 23:00 IST; action `expiry_auto_close` exchange=MCX; ships INACTIVE |
| `expiry-nfo-risk-alert` | equity expiry risk (active) | Notifies on expiry day when any NFO is ITM or future |
| `expiry-mcx-risk-alert` | commodity expiry risk (active) | Notifies on expiry day when any MCX position is unhedged |
| `market-open-nse` | NSE open (active) | Fires at 09:15 IST; uses sentinel condition for timing gate |
| `market-preclose-mcx` | MCX pre-close (active) | Fires at 23:00 IST; same timing-gate pattern |
| `manual` | operator order (audit trail) | Every manual ticket / chain order writes a `manual` event here |

Built-in agents are **force-reseeded on every boot** — your changes to their `conditions / cooldown / events / actions` are PRESERVED, but `slug / schedule / status` are pinned to code. To customise, clone to a new slug.

---

## Authoring workflow

1. **Spike the condition tree** in the `/automation` editor (CLI tab for text, 
   Structured tab for JSON) or via the Claude Code MCP (see [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md))
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
| CLI tab is greyed out (disabled) | Agent's JSON uses a `$ref` fragment (condition, events, or actions); cannot decompile to CLI form | Use the Structured tab and JSON textarea instead; edit as JSON |
| CLI statement rejects with "argument must be written as name=value" | Attempted positional argument (first arg without a key). CLI grammar requires every argument to be keyword-only | Change `place_order(account, symbol, ...)` to `place_order(acct=account, sym=symbol, ...)` |
| `dry-run` shows `would_fire: false` but you expect true | Condition mismatch — operator's threshold vs current state | Use the dry-run `matches` array; each entry shows the metric, scope, threshold, and actual value |
| `dry-run` shows `blocked_by: "schedule"` | Agent has `schedule: market_hours` but markets are closed | Either wait for session, or flip `schedule: always` for diagnostic agents |
| Agent never fires on real ticks | Rate metric without baseline crossed; or in cooldown; or suppressed | `/automation/<slug>` Events tab + `/admin/alerts` log; or set `cooldown_minutes: 0` temporarily |
| Loss agent silent for first 10–15 min after market open | ROC agents (`loss-rate-acct`) silent during baseline window; point-in-time agents normal | Wait for window or check `loss-rate-acct` baseline gate |
| Real loss alerts never fire but sim fires OK | Simulator engine running — real loss alerts suppressed while sim is active | Stop the sim via `/admin/execution` or wait for auto-stop (30 min default) |
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
