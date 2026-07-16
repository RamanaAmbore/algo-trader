# RamboQuant Admin Guide

> Read [USER_GUIDE.md](USER_GUIDE.md) first for concepts; this file is the operations reference.

**Quick start — test an auto-close rule before activating**:
1. `/automation` → `loss-pos-total-auto-close` → **Run in Simulator**
2. Watch the **Simulator** tab at the bottom — one `SELL` line per position
3. Flip **ON** when confident

---

## Core concepts

| Term | Meaning |
|---|---|
| **Agent** | A rule row: "if X, notify me and maybe do Y" |
| **Alert** | Runtime event when rule triggers |
| **Notify** | Delivery channel (Telegram / email / log) |
| **Action** | Side-effect (place order, close position, …) |

Agent as a sentence: **"When _condition_, _notify_ and _do_ these actions."**

### Firing rules (prevent spam)

- **Static agents** — fire once at threshold crossing, latch silent while condition holds, re-arm on recovery
- **Rate agents** — re-fire when bleeding accelerates (gated by cooldown + material-change threshold)
- **Session rollover** — clears all latch state daily

---

## Order Templates — auto-attach TP/SL/trail on position fill

Pick a template at order entry → platform attaches broker-native GTT when position fills. Multi-broker coverage (Sprint C):

| Broker | TP single | SL single | OCO (TP+SL) | Trail (modify GTT) | MCX / commodity |
|---|---|---|---|---|---|
| Kite | ✅ native | ✅ native | ✅ native | ✅ native | ✅ |
| Dhan | ✅ native | ✅ native | ✅ native (Forever Order) | ✅ native | ❌ Forever doesn't cover MCX — `place_gtt` raises a clear `RuntimeError` |
| Groww | ✅ native | ✅ native | ⚠ emulated (two singles + 15s pair-watcher) | ❌ no modify_gtt | ✅ (single-trigger only) |

When the operator picks a template whose features exceed the selected broker's natives, OrderTicket renders an amber capability warning chip below the template summary:
- "Groww OCO emulated — ~15s race window"
- "Dhan can't trail — SL stays fixed" (only on brokers without `modify_gtt`)
- "{broker} has no GTT — scale-out won't attach"

The chip is sourced from `GET /api/admin/brokers/{account}/capabilities` — a pure read of the `BrokerCapabilities` dataclass; no broker round-trip.

### `/admin/templates` page

Columns: Name | TP | SL | Trail | Wing | Scale | Active | Default (radio). Edit form fields: name / description / tp_type / tp_pct / tp_abs / sl_pct / sl_abs / sl_trail_pct / wing_strike_offset / wing_premium_pct / tp_scales_json.

Row chips: `TP` (tp_pct/tp_abs) · `SL` (sl_pct/sl_abs) · `SL X% trail` · `WING ±N` · `Scale ×M` · `MKT` (if MARKET, else LIMIT default)

### OrderTicket integration

Two-pill toggle: `[Default ✓]` (uses marked default) or `[None]` (no exits). Shows inline summary: `"TP +50%, SL −1%, Scale ×3"`.

### OrderCard exit-rule chip

After fill: `tmpl:#1 ✓` (all GTTs placed) or `tmpl:#1 …` (placing, ~1 sec) or none. Tooltip: template name + summary.

### Settings (`/admin/settings → templates.*`)

| Key | Default | Purpose |
|---|---|---|
| `wing_min_oi` | 1000 | Filter illiquid strikes |
| `wing_max_spread_pct` | 10 | Max bid-ask spread % |
| `wing_chain_radius` | 20 | ±N strikes around parent |
| `trail_poll_interval_seconds` | 30 | LTP check frequency for trailing stop |
| `oco_pair_poll_seconds` | 15 | Sibling-cancel check for emulated OCO (Groww) |

### Seeded system templates

| Template | TP | SL | Wing | Scale | Trail |
|---|---|---|---|---|---|
| `default-long-option` | +80% MARKET | none | none | none | none |
| `default-short-vol` | +10% LIMIT | −20% LIMIT | −1 strike | none | none |

### API routes (admin-guarded)

| Route | Purpose |
|---|---|
| `GET /api/admin/templates` | List all (system + user-created) |
| `GET /api/admin/templates/{id}` | Read one |
| `POST /api/admin/templates` | Create custom template |
| `PATCH /api/admin/templates/{id}` | Edit (including setting is_default; auto-demotes prior default) |
| `DELETE /api/admin/templates/{id}` | Delete (including seeded ones if customized away) |
| `POST /api/orders/ticket` | Place order with template. Template ID optional in request body. |

### Execution flow at order fill

1. Operator enters order + picks template (or uses default)
2. `POST /api/orders/ticket` validates mode (templates on LIVE / PAPER only)
3. Broker.place_order() fires; order lands
4. Order fill detected (Kite postback or chase engine terminal)
5. `apply_template_to_order()` resolves TP/SL/wing/scale/trail → N GTT specs
6. For each spec: `broker.place_gtt()` fires → enters broker's GTT engine
7. `AlgoOrder.attached_gtts_json` populated with context (idempotent; re-fire = no-op)
8. OrderCard chip renders `tmpl:#N ✓`

### Wing hedge chain scan

When resolving wing (Phase 1B):

```
parent = SELL NIFTY25APR22000CE (example)
scope = NIFTY25APR22XXX PE (same underlying + expiry, opposite type)
radius = wing_chain_radius (default ±20 strikes)
```

Broker.quote() fetches ~40 candidate PE strikes. Score each by: `|ltp − wing_premium_pct × parent_ltp| + spread_penalty`. Filter out OI < wing_min_oi and spread% > wing_max_spread_pct. Pick closest-scored candidate (greedy).

If no candidates pass the filters, wing skips (error logged; parent still places). Common on low-liquidity options.

### Scaled close (multi-target TP)

Example `tp_scales_json`:
```json
[
  {"at_pct": 50, "close_pct": 30},
  {"at_pct": 100, "close_pct": 40},
  {"at_pct": 150, "close_pct": 30}
]
```

When parent (100 qty) fills → create 3 GTTs:
- GTT 1: trigger at fill × 1.50 → close 30 qty (30%)
- GTT 2: trigger at fill × 2.00 → close 40 qty (40%)
- GTT 3: trigger at fill × 2.50 → close 30 qty (30%)

Total closed: 100 qty. Each GTT is independent (fires in any order, doesn't block others). Qty allocation uses floor + remainder-fix so the sum always equals parent qty.

### Trailing stop mechanics

When parent fills + sl_trail_pct is set:

```
for long:
  highest_ltp_seen = fill_price
  initial_trigger = fill_price × (1 - sl_pct)
  
  [every trail_poll_interval_seconds]
  highest_ltp_seen = max(highest_ltp_seen, current_ltp)
  current_trigger = highest_ltp_seen × (1 - sl_trail_pct)
  if current_trigger > initial_trigger:
    broker.modify_gtt(trigger=current_trigger, price=current_trigger)
```

For shorts, invert (lowest_ltp_seen, subtraction becomes addition). Context (`highest_ltp_seen`, `current_trigger`, etc.) lives in `AlgoOrder.attached_gtts_json` so the task survives restarts.

### Troubleshooting table

| Issue | Check |
|---|---|
| Template not attaching after fill | Order's `attached_gtts_json` null? Check `/api/admin/logs` for "template attach error". As of Sprint A the attach fires from FOUR paths: postback handler, chase terminal, reconcile path, AND paper-engine fill. If one of those is silent the row still has another shot. Per-row `asyncio.Lock` (WeakValueDictionary) serialises concurrent fire attempts so duplicate GTTs can't be placed. |
| Wing not filling / showing wrong strike | Check `templates.wing_min_oi` and `templates.wing_max_spread_pct` settings. Highly illiquid options may have no candidates. Check broker logs. |
| Trailing stop not advancing | Check `templates.trail_poll_interval_seconds` (default 30s). Two-leg OCO trails: Sprint A persists `tp_trigger` in `attached_gtts_json` so the poller can pass both `[tp, new_sl]` to `modify_gtt`. Pre-Sprint-A entries without `tp_trigger` log a one-time INFO line and skip — re-attach to enable trailing. Dhan OCO trail: Sprint C fixed the silent ENTRY_LEG-only bug; both legs now modify correctly. Groww: `modify_gtt` for compound `oco:` ids is not yet wired for trail (it's emulated single trails only). |
| Operator says "I can't pick my template in OrderTicket" | Confirm template `is_active=True` (soft-delete, if you want to hide it). No UI toggle yet; DB edit required (or re-save from admin form). |
| Scale-out close_pct doesn't sum to 100% | Submit will raise 422 validation error. Fix in the template form; cumulative must be ≤ 100% (can be less if operator wants some qty to remain). |
| Operator sees "Groww OCO emulated — ~15s race window" warning chip | Expected behaviour — Groww has no native OCO. Mitigation: lower `templates.oco_pair_poll_seconds` (default 15s) at the cost of more broker.get_gtts() polling. For zero race window, use Kite or Dhan. |
| Operator sees "Dhan Forever Order does not cover MCX/NCO" error | Expected — Dhan's Forever Order doesn't cover commodity. The error is raised at `place_gtt` time with a clear RuntimeError so the operator can mirror the parent to a Kite account before re-attaching. |
| Postback handler 500s after Kite fill, with Dhan/Groww accounts loaded | Pre-Sprint-A bug — `.api_secret` was called on every connection and Dhan/Groww raised AttributeError. Sprint A skips non-Kite connections in the HMAC loop. If you see this in old logs from before `24cced42`, the symptom would be Kite retrying the postback every few seconds. |
| Partial-fill chase looks like it's stuck | Sprint B + D — chase loop's `_record_partial_fill` accumulates `filled_quantity` across partials and rolls a qty-weighted `fill_price`. Row `detail` reads `PARTIAL N/M @ ₹X (chasing residual M-N)`. MCX: Sprint D's `from_kite_qty` reverse-translates lots to contracts before the partial comparison, so a 1-lot fill on a 100-contract MCX order no longer triggers a phantom partial every poll. Persistent state writes happen on every partial so a chase that aborts at max_attempts shows the truthful UNFILLED residual, not the original ask. |

---

## Agent execution — every 5 min during market hours

Broker data → summarise → `run_cycle()` for each ACTIVE agent:
1. Market open?
2. Cooldown finished?
3. Condition matches?
4. Material change since last alert?
5. Fire: Telegram + email + log + actions

Gates: cooldown (spam), baseline (open-bell), suppression (flat loss)

---

## Anatomy of an agent

Every row on the **Agents** page (`/automation`) has four moving parts:

### 1. Conditions — the rule itself

Conditions are a tree you can read left-to-right. Three shapes are allowed:

| Shape | Meaning |
|---|---|
| **leaf** | A single test: `metric` + `scope` + `operator` + `value` |
| **all** | AND of children — every child must be true |
| **any** | OR of children — at least one child must be true |
| **not** | NEGATION — true when the child is false |

A leaf looks like this in JSON:

```json
{
  "metric": "pnl",
  "scope":  "positions.any_acct",
  "op":     "<=",
  "value":  -30000
}
```

Read it in English:
> "The **pnl** (metric) of **any account's positions** (scope) is **≤** (operator) **₹-30,000** (value)."

A composite tree:

```json
{
  "any": [
    { "metric": "day_pct", "scope": "holdings.any_acct", "op": "<=", "value": -3.0 },
    { "metric": "day_pct", "scope": "holdings.total",    "op": "<=", "value": -5.0 }
  ]
}
```

> "Either any account's day loss is at least 3 %, or the total day loss is at least 5 %."

### 2. Notify channels

```json
[
  { "channel": "telegram", "enabled": true },
  { "channel": "email",    "enabled": true }
]
```
Mix and match: `telegram` / `email` / `websocket` / `log`.

### 3. Actions

`[]` = alert only. Otherwise:
```json
[{"type": "chase_close_positions", "params": {"account": "ZG0790", "exchange": "NFO"}}]
```

See **Tokens** page for all action types.

### 4. Metadata

- **Scope**: `total` or `per_account`
- **Schedule**: `market_hours` or `always`
- **Cooldown**: default 30 min
- **Status**: `active` / `inactive`

---

## Tokens — vocabulary for agent conditions

Every `metric`, `scope`, `op`, `channel`, `action_type` must be registered at `/admin/tokens`.

**Three categories:**
- **Condition**: `metric`, `scope`, `operator` — what agents can check?
- **Notify**: `channel`, `format`, `template` — how to alert?
- **Action**: `action_type` — what to do?

**System vs custom**: System (ship with app, toggle-only) vs Custom (full CRUD)

### Token row fields

| Field | Purpose |
|---|---|
| **Token** | The word (e.g. `pnl`, `<=`, `telegram`) |
| **Category** | `condition` / `notify` / `action` |
| **Token kind** | `metric` / `scope` / `operator` / `channel` / `action_type` |
| **Value type** | `number`, `string`, `boolean`, `enum`, `array`, `object`, `void` |
| **Units** | For metrics: `₹`, `%`, `₹/min`, `%/min`, `min` |
| **Description** | Tooltip in Agents editor |
| **Resolver** | Python dotted path (required for system tokens) |
| **Params schema** | For action tokens — JSON schema of arguments |
| **Enum values** | Allowed strings for enum types |
| **Template body** | Message body with `${placeholder}` syntax |

### Creating a token

1. `/admin/tokens` → **+ New token**
2. Pick Category and Token kind
3. Fill Token, Description, Value type, Units
4. For actions: define `params_schema` (JSON)
5. Save → **Reload registry** (yellow button, top right)

New token usable immediately without server restart.

### Integration with Agents editor

- Dropdowns auto-populate from Tokens table
- **Validate** button checks tokens against registry (catches typos)
- **Actions editor** renders form from action token's `params_schema`

Tokens page is the single extension point: new check = 1 token row + 1 Python function, no engine code change.

---

## Creating an agent

1. `/automation` → expand a row → **Edit**
2. Fill: Name / Description / Scope / Schedule / Cooldown / Conditions (JSON) / Events / Actions
3. **Validate** → **Save**
4. Flip OFF pill to ON when ready

**Tip**: Copy a seeded `loss-*` agent and edit thresholds — fastest way to learn.

---

## The Simulator — test agents safely

`/admin/execution?mode=sim` feeds fabricated data through the real agent engine. All alerts tagged `SIMULATOR`.

**Page surface:**
- Status bar: `RUNNING` / `idle`, scenario, tick count
- Controls: scenario dropdown · seed mode · rate · Load live book · Start/Stop/Step · Run cycle · Clear sim
- Recent SIMULATOR agent events table
- Recent SIMULATOR orders table
- LogPanel **Simulator** tab: per-tick price diffs in real time

### Market-state presets

| Preset | Simulates |
|---|---|
| `pre_open`, `at_open`, `mid_session` (default), `pre_close`, `at_close`, `post_close`, `expiry_day` | Realistic market clock for time-aware agents |

Set via: Simulator page dropdown (most specific) → scenario YAML → default. Run-in-Simulator button auto-picks sensible preset.

### Tick cadence

Simulator is **positions-only** (holdings aren't simulated). Holdings agents get a clear error. Positions refresh every tick by default (cadence = 1). Override via: Pos / N input (most specific) → scenario YAML → `/admin/settings`. Margin patches (`set_margin`) fire on scheduled ticks independent of cadence.

### Seeding modes

| Mode | Starting state | Use |
|---|---|---|
| **Scripted** | Scenario's `initial` block | Deterministic regression test |
| **Live** | Real Kite positions snapshot | Stress your actual book |
| **Live + scenario** | Real snapshot + scenario extras | Real book + hypothetical moves |

Manual **Load live book** for a fresh snapshot before starting.

### Running a simulation

1. `/admin/execution?mode=sim` → pick scenario (start with `generic-crash`)
2. Pick seed mode → **Load live book** if using Live / Live+scenario
3. Set **Rate** (2000 ms = 1 tick/2 sec) → **Start**
4. Watch **Simulator** tab at bottom for per-symbol price diffs
5. Auto-stops at 30 min; click red **Stop** to exit early

### Testing one agent

On `/automation` page, every row has **Run in Simulator** button. Pre-arms the page to run only that agent, bypassing schedule / cooldown / baseline gates. Safest way to test before activating.

### Underlying-driven F&O scenarios

For options + futures books, the simulator can move the **underlying spot** (NIFTY, BANKNIFTY, etc.) and have every contract on that underlying re-price coherently — so a "−3% NIFTY" tick gives you realistic gamma + skew effects instead of moving each strike in isolation.

**How it works (one paragraph)**: at sim start, the driver detects every underlying in your book, snapshots its spot price (from a futures contract on it, or from `scenario.initial.underlyings`, or as a crude ATM proxy), and calibrates **implied volatility per option** by inverting Black-Scholes against each option's current premium. When you fire an `underlying_pct -0.03` move, the spot drops 3 %, and every option re-prices via Black-Scholes with the IV that was locked at start. Futures track spot 1:1.

**Two new built-in scenarios**:

| Slug | What it does |
|---|---|
| `nifty-down-3pct` | NIFTY spot −1% / −2% / −3% over three ticks. ITM puts inflate, OTM calls collapse, futures fall 1:1. |
| `nifty-up-3pct`   | NIFTY spot +1% / +2% / +3% over three ticks. ITM calls inflate, OTM puts collapse — squeezes short-call writers. |

**Use it like this**:
1. `/admin/execution?mode=sim` → press **Load live book** → switch Seed to **Live** or **Live + scenario**.
2. Pick `nifty-down-3pct`.
3. Press **Start**.
4. Watch the chart panel — the NIFTY chart (sky-blue `SPOT` tag) shows the 3 % drop; each option chart (amber `F&O` tag) shows the derived premium move with the underlying overlaid as a dashed sky-blue line.

**Caveats**:
- Vega and theta are intentionally ignored — sim runs are minutes, not days.
- IV is locked at sim start. Real-world IV expands during sell-offs; the sim doesn't model that. If you want to study IV-expansion effects, layer a per-option `pct` move on top of the underlying move.
- Stock-option books need explicit underlyings in `scenario.initial.underlyings: {RELIANCE: 2800}` if no futures contract is in the book — the driver can't resolve a stock spot from an arbitrary option chain alone.

### Adding custom positions to a sim run

Don't have the position you want to test in your live book? Add it inline:

1. `/admin/execution?mode=sim` → scroll to the **Custom positions** panel below the controls.
2. Click **+ Add row** → fill in the row:

   | Field | Example | Notes |
   |---|---|---|
   | Symbol | `NIFTY25APR22000CE` | Any Kite-style F&O symbol or cash equity |
   | Qty | `-50` | Negative = short, positive = long |
   | LTP | `180` | Last-traded price; used as the seed |
   | Account | (blank) | Defaults to `ZG####` if blank — the engine treats it as a label |

3. Add as many rows as you want. Click the red **×** to remove a row.
4. Press **Start** — your custom rows are layered on top of whatever scripted/live seeding produced. F&O symbols re-price coherently when an `underlying_*` move fires; cash equities track simple `pct/abs` moves.

This is the right move for "what-if" testing before you take a real trade — you see exactly how the agent engine + chase + Black-Scholes pricing react to the position, without ever sending an order to the broker.

---

## Execution mode — navbar dropdown only

Five modes: SIM (rose) · PAPER (sky-blue) · LIVE (red) · SHADOW (orange) · REPLAY (green). Branch gate: `main` allows any; non-`main` forces PAPER. Confirm modal before LIVE → PAPER/SHADOW. Telegram tag: empty = LIVE, `[PAPER]` = PAPER mode.

---

## Paper mode dashboard (`/admin/execution?mode=paper`)

Real Kite quotes + paper engine. Status: `CHASING` (orders in flight) · `IDLE` (enabled, no orders) · `DEV` (non-main). Chase pills (side / qty / symbol / limit / attempts) + chart grid + LogPanel Orders tab. Watch the live chase without broker touches; compare to `[PAPER]` Telegram alerts.

---

## Basket orders — multi-leg, multi-account

Orders > Ticket tab: **+ Basket** button → add legs, pick account per pill. Margin strip shows Required / Avail / After per account. Routes: `GET /api/orders/basket/margin`, `POST /api/orders/basket` (PAPER), `POST /api/orders/basket/place` (LIVE).

---

## Auto profit target

OrderTicket carries Target row. Default `algo.default_target_pct` (ships +30%), override per-ticket (% ↔ ₹ toggle). On fill: auto-places LIMIT TP order (SELL for long, BUY for short) at fill × (1 + target_pct).

---

## Derivatives page — `/admin/derivatives`

Three visual sections:
- **ITM ON EXPIRY** (amber) — NSE ITM = spot > CE strike or < PE strike; action required before close
- **NETTED** (slate, MCX only) — CE/PE pairs netting to zero auto-settle; informational
- **OUT OF THE MONEY** (muted) — expire worthless; informational

### Picker & settings

Underlying dropdown filters by book. Pick underlying → load every option + future as checkboxes. Add drafts via `+` (option-chain picker). Settings: `connections.price_account` (Kite account for Greeks/margin/historical; blank = first available) · `algo.default_target_pct` (default +30%).

---

## Symbol identity

Charts, Derivatives, Positions display: root (CRUDEOIL, GOLDM) + contract chip (CRUDEOIL26JUNFUT). Contract chip amber when ≤3 days to expiry. Auto-resolve: `NIFTY 50` → `NIFTY26JUNFUT` · `CRUDEOIL` → `CRUDEOILM26JUNFUT` · equity → `NSE:<SYM>` spot.

## Options analytics (`/admin/derivatives`)

A separate workspace from the tick-chart pages — this is for **options research**: pick an underlying, see the aggregated payoff for everything you hold on it, plus the Greeks and risk metrics on the side. One leg or twenty — same view; the page doesn't distinguish single-leg from multi-leg.

### Picker bar

Two dropdowns and a `+` toggle. That's it.

| Control | Purpose |
|---|---|
| **Account** (multi-select) | Scopes which broker accounts the candidates pull from. Blank = all accounts. |
| **Underlying** (single-select) | NIFTY / BANKNIFTY / FINNIFTY / … — derived from your loaded book. Picks the universe. |
| **+ / −** (toggle pill) | Opens an **option-chain** picker; clicks land as **drafts** (hypothetical positions) you can edit. |

Live vs sim is auto-detected. While a simulator is running the page works off sim positions and the header carries a `SIMULATOR` chip; otherwise it works off your live broker book. No mode switch — just pick the underlying.

### What you see

```
┌──────────────────────────────────────────────┬────────────────────┐
│ ┌── overlay (top-left of chart) ──┐          │  Aggregate         │
│ │ SPOT  ₹9,000                    │          │    Spot / Net cost │
│ │ TDAY  +₹1,500                   │          │  Greeks (position) │
│ │ EXP   −₹2,400                   │          │    Δ Γ Θ V ρ       │
│ │ MAX P +₹5,000                   │          │  Risk + EV         │
│ │ MAX L −₹8,000                   │          │    max P / L       │
│ └─────────────────────────────────┘          │    R:R             │
│                                              │    breakevens      │
│  Aggregate payoff diagram                    │    POP / EV        │
│   - amber line: today (BS, current DTE/IV)   │                    │
│   - sky dashed: expiry (intrinsic)           │                    │
│   - green zone: profit                       │                    │
│   - red zone:   loss                         │                    │
│   - vertical markers: spot · strikes · BEs   │                    │
└──────────────────────────────────────────────┴────────────────────┘
│  Candidates (checkbox list — uncheck to drop a leg from payoff)   │
└────────────────────────────────────────────────────────────────────┘
```

The **stat overlay** in the top-left corner of the chart shows the at-a-glance numerics — **SPOT**, **TDAY** P&L (today's value at spot, BS-priced), **EXP** P&L (expiry value at spot, intrinsic), **MAX P** (max profit), **MAX L** (max loss). Color-coded green/red so you can read the position health at a glance without looking at the side cards. The chart's hover tooltip uses the same `TDAY` / `EXP` labels for consistency.

### Adding draft positions

Click `+` to open the option-chain picker. Pick an expiry, browse strikes, click `+ CE` / `+ PE` next to any row (or a futures pill above the strike grid) to drop the contract into **Drafts**. Drafts whose symbol matches the selected underlying surface in Candidates immediately and feed the strategy analytics like any other leg. Edit qty / avg cost / LTP inline; hit `×` to drop a draft.

Drafts are how you model contracts you don't own — "what if I add NIFTY24500CE to my book?". They sit beside live + sim positions in Candidates and the chart re-renders on every checkbox toggle.

### Key metrics — what they mean

| Metric | What it tells you |
|---|---|
| **BS theo** | Black-Scholes fair value at the current spot, IV, and DTE. |
| **Diff** | Market LTP minus theoretical. Positive = market is asking more than fair (you'd be overpaying to buy). Negative = market is cheap (potential edge, or a stale quote). |
| **IV** | Implied volatility back-solved from the current LTP. The chart and Greeks use this σ — change it via the URL query if you want a what-if. |
| **Delta** | ₹ change in option value per ₹1 change in spot. Position-scaled = delta × signed qty (long short calls have negative delta). |
| **Gamma** | Rate of change of delta. Tiny number for index options; multiply by 100 to get "delta change per ₹100 spot move". |
| **Theta** | Daily time decay in rupees. Always negative for long options, positive for short. |
| **Vega** | ₹ change per 1 % IV change. Sign tells you if you're long or short volatility. |
| **Rho** | ₹ change per 1 % risk-free rate change. Mostly cosmetic for short-dated index options. |
| **Max profit / loss** | Position-level absolute rupees at expiry. ∞ for unlimited-payoff legs (long calls, short puts). |
| **Breakeven** | Spot price at expiry where position P&L is zero. |
| **POP (probability of profit)** | P(spot at expiry crosses your breakeven), under the Black-Scholes log-normal assumption. Greater than 60 % shows green; less than 40 % red. |

### Pricing-account setting

The page (and the paper-trading underlying-spot fetch) routes shared market-data calls through whichever account is set as `connections.price_account` in [Settings](`/admin/settings`). Blank = auto-pick the first account in `secrets.yaml`. Pin it explicitly if you want a specific Kite handle to take the load.

### Caveats

- **Single underlying per chart**. All checked candidates must share the same underlying — the page rejects mixed underlyings (would be unplottable on one x-axis). Pick a different Underlying to see a different book.
- **Single expiry per chart**. All legs must share the same expiry. Calendar / diagonal spreads aren't supported yet — uncheck the off-expiry leg in Candidates.
- **IV is locked at the moment of the call**. The page polls every 5 s so a fast-moving market refreshes the IV calibration; the payoff curve uses whatever σ the latest poll resolved.

### Stale prices — what to do when the broker has nothing to say

When you're looking at an option that's illiquid, off-hours, or just had a stale quote, the page shows **yellow `stale: <source>` chips** so you know which numbers came from a fallback. The fallback chain:

1. **live** — the broker's current `last_price`. This is what you want.
2. **close** — yesterday's closing price (from the broker's `ohlc.close`). Useful when the market is closed or a contract just hasn't traded today.
3. **depth** — midpoint of the top-of-book bid/ask. Last resort when there's no live trade and no recent close.
4. **avg_cost** — the average cost from your position. Falls back to "what you paid" when literally nothing else is available.
5. **default IV** — an amber `·default` tag on the IV cell means the calibrator couldn't extract a sensible σ (typically because the LTP itself was a fallback) and used 15 % as a reasonable working assumption.

The payoff curve still draws regardless. The Greeks and POP computed off a stale price are best treated as "shape-of-position" — directionally right, but don't rely on the absolute rupee numbers when the chip is yellow.

### Building a complex strategy

Picking an underlying loads every option + future you hold on it into Candidates. To model a hypothetical strategy on top:

1. Click `+` to open the option-chain picker.
2. Pick the expiry from the chain dropdown, optionally toggle Long / Short.
3. Click `+ CE` / `+ PE` next to a strike (or a futures pill) to drop a draft into your basket. The contract appears in Drafts (editable) and Candidates (checkable) immediately.
4. Tick / untick rows in Candidates to include / exclude legs from the payoff. The chart auto-updates.

The chart marks every leg's strike and every breakeven (an iron condor draws 2; a butterfly 2; a vertical 1). The side panel surfaces:

- **Net cost** — debit / credit; sign tells you whether you paid or collected.
- **Position Greeks** (Δ Γ Θ V ρ) — summed across legs, signed by qty.
- **Risk + EV** — max profit / loss within the charted spot range, R:R, every breakeven, POP, expected value.

Use it before you put on a complex trade — pick the legs, look at the BE markers and POP, *then* hit the broker. The numbers won't lie.



### Charts — see what the price did

While a sim is running, the page renders one **mini chart per active symbol** directly under the position pills:

- Amber line — last-traded price tick by tick
- Faint cyan band — bid/ask spread
- Markers:
  - **Amber** dot — order placed (where the chase started)
  - **Emerald** dot — order filled
  - **Red** dot — order unfilled (chase gave up)

Hover any marker to see the side, fill price, and order id. The chart polls every 3 s and persists across fills, so once a position closes you can still see the full trajectory that led to it.

The same chart grid renders inline on `/admin/execution?mode=paper` so you can watch the live chase engine in **paper mode** (mode 2) on prod. The Activity LogPanel below carries the canonical 6-tab strip but no standalone Chart tab — every page that needs charts mounts them as first-class page content rather than buried inside a log tab.

History is in-memory only (no DB writes). The buffer holds ~20 minutes of per-symbol history at the default tick rates; older points fall off automatically. A service restart resets the chart — that's by design (the chart is for live monitoring, not post-mortem).

### What gets tagged

Because `sim_mode=True` flows through the pipeline, every artefact is marked:

| Surface | Tag |
|---|---|
| Telegram | `SIMULATOR` prefix + red "SIMULATOR RUN — fabricated market data" line |
| Email subject | `RamboQuant SIMULATOR Agent: …` |
| Email body | Red banner at the top |
| agent_events row | `sim_mode = true` |
| algo_orders row | `mode = 'sim'` |
| Log line | `[SIM] …` (short prefix to conserve log width) |

So real alerts and simulated alerts are never in the same bucket.

### While the sim runs

- The red **SIMULATOR ACTIVE** banner pins to the top of every admin page.
- The `/automation` page's event table auto-switches to showing only sim events.
- The `/performance` page keeps showing **real** data — the live Kite refresh continues even during a sim, only the live agent engine is paused.

---

## Proxy hedges — GOLDBEES ↔ GOLD options

Automatically converts ETF holdings to option-hedging equivalents. Pick GOLDM → GOLDBEES auto-surfaces in Legs with gram-equiv + lot count. All-live math: `market_val = qty × LTP`, `effective_qty = β × market_val ÷ spot`, `lots = effective_qty ÷ lot_size`. No factor table.

Seeded defaults: GOLDBEES/SILVERBEES ↔ GOLD/GOLDM; NIFTYBEES ↔ NIFTY; BANKBEES ↔ BANKNIFTY. For retail books, GOLDM/SILVERM more practical than full-size GOLD/SILVER lots.

### Adding pairs & computing β

`/admin/settings → Hedge proxies → + Add`. ETF pairs work immediately (β=1.0 implicit). For stock-vs-index: **Compute β** button runs 60-day regression (~5s). Auto-recompute daily at 02:30 IST for rows older than `hedge_proxies.regression_max_age_days` (default 7).

Settings: `regression_enabled` · `regression_window_days` (60) · `regression_max_age_days` (7). Row shows: β | R² | date of last run | **Compute β** button.

### Failed regression — `regression_error` column

Sprint D added the `regression_error` column to the `hedge_proxies` table. Every failed regression run writes a one-line reason here ("too few overlapping bars (n=8, need ≥ 15)", "broker error: rate-limited", etc.); every successful run clears it. The admin row shows ⚠ next to the date with the error in the tooltip; on `/admin/derivatives` the PROXY chip turns red ⚠ instead of amber when the error is present.

The chip's age tag carries three states:
- **No suffix** (β fresh, ≤ 2 days) — green/normal styling.
- **"β computed Nd ago"** with amber tag — β is 2–7 days old, still usable but ageing.
- **"⚠ regression failed: <reason>"** OR **"β computed Nd ago (STALE)"** with red tag — last attempt errored, OR β is older than 7 days. Hit Compute β to retry.

### Pathological β rejection

Sprint E added a `|β| > 5` guard in `_compute_regression`. A pathological β value typically comes from a single bad bar (split day, bad tick, fat-finger trade) driving the regression off the rails. Bloomberg PRM caps to ±3; we're slightly more permissive (5) because leveraged ETFs can legitimately overshoot. Rejection logs a clear WARNING line and the regression returns `(None, None, n)` so the caller treats it identically to "too few bars".

### ETF check: GOLDBEES → GOLD

After the first regression run you'll see something like `β=0.98 R²=0.99`. That's the empirical confirmation that GOLDBEES tracks gold spot ~1:1. The math doesn't change (it uses β when set, falls back to 1.0 when NULL), but you now have validation that the proxy is working.

---

## Settings — runtime tunables

The Settings page (`/admin/settings`) is where you tune the knobs that change more often than a deploy cycle: alert thresholds, refresh cadences, simulator defaults, and the **execution mode** flags that decide whether an action hits the broker or stays in paper. Edits take effect on the **next agent tick / sim run** — no service restart, no redeploy.

### Page layout

Each parameter is a single row:

```
[i]   alerts.cooldown_minutes  [mod]      [   30   ] min   [ Save ]  [ Reset ]
```

- **(i)** — click the amber chip to expand a panel showing the description, default, range, and units. Click again to collapse. Use it when you don't recognise a key.
- **`[mod]` badge** — appears when the live value differs from the code-shipped default.
- **Value field** — input adapts to the type: text for strings, number with min/max for ints/floats, dropdown for booleans / enums.
- **Save** — disabled until you change the value. Writes the new value and refreshes the row.
- **Reset** — disabled until the row is modified. Restores the code-shipped default.

A **filter box** at the top searches both keys and descriptions — useful when you know roughly what you want but not the exact key.

Categories are rendered in deliberate order — **execution → alerts → algo → performance → simulator → notifications → logging → misc** — so the things you'll actually touch sit at the top of the page.

### Settings vs YAML

Settings are for runtime knobs an operator changes without thinking about a deploy. **Infrastructure parameters stay in YAML deliberately**:

| In Settings (DB) | In `backend_config.yaml` |
|---|---|
| Alert thresholds, cooldowns | DB credentials |
| Refresh cadences | Market hours |
| Sim defaults | Kite URLs |
| Execution mode flags | IPv6 source addresses |
| Notification toggles | Capability flags (`cap_in_<branch>`) |
| Log levels | Log file paths |

The seeder behaves well across deploys: it inserts new keys, refreshes descriptions / schemas / defaults, **preserves your overrides**, and auto-prunes keys that have been retired in code.

### Execution mode banner

The first thing you see on the page is the execution mode banner:

- **Red — `LIVE mode`** — `execution.paper_trading_mode = False`. Every fired agent that wants to hit Kite places a real broker order. This is the seeded default on a fresh install.
- **Green — `PAPER mode`** — `execution.paper_trading_mode = True`. Every fired agent writes a paper `AlgoOrder` row instead of touching Kite. Real positions don't change.
- **Pink — `SIMULATOR running`** / **Orange — `SHADOW mode`** / **Sky — `REPLAY running`** — the corresponding mode is active.

The single master toggle `execution.paper_trading_mode` (flipped via the navbar dropdown or `/admin/execution`) decides PAPER vs LIVE; no per-action flags. SHADOW and REPLAY are separate opt-ins on top.

### The five execution modes

Every agent fire that touches the broker gets routed by mode:

| Mode | Where it runs | Quote source | Trade engine |
|---|---|---|---|
| **1 — Simulator** | Both dev + prod (via navbar SIM) | Fabricated (scenario-driven) | `PaperTradeEngine` against fabricated bid/ask |
| **2 — Paper** | Prod when `paper_trading_mode=True`; dev always | Real Kite quote API (batched) | `PaperTradeEngine` against live bid/ask, validated by Kite's `basket_margin` |
| **3 — Live** | Prod when `paper_trading_mode=False` (default on fresh install) | Real Kite | Real `place_order` / `modify_order` / `cancel_order` |
| **4 — Shadow** | Prod when `shadow_mode=True` | Real Kite | Logged payload + `basket_margin` only; no execution |
| **5 — Replay** | Both dev + prod | Historical OHLCV candles | `PaperTradeEngine` against historical bid/ask |

The `main` branch is a **hard outer gate**: on dev (any non-main branch), every broker-hitting action is forced to paper regardless of `execution.paper_trading_mode`.

Every alert email + Telegram message gets a tag so you can tell at a glance what mode the actions ran in:

| Tag | Meaning |
|---|---|
| (no tag) | Every broker action in this fire ran live (master toggle `execution.paper_trading_mode=False`) |
| `[PAPER]` | Every broker action in this fire was paper (master toggle `execution.paper_trading_mode=True`) |

### Recommended promotion order

Promotion is now a single master-toggle flip (`execution.paper_trading_mode`), not per-action flags. The recommended path:

1. Soak with the master toggle at `True` (PAPER) and watch the chase loop on `/admin/execution?mode=paper`
2. Watch the LogPanel's Order tab — every fire writes an `AlgoOrder` row with `mode='paper'` and Kite's `basket_margin` verdict in `.detail`. REJECTED rows tell you "Kite would have kicked this back anyway."
3. When the fires look right, flip the navbar dropdown to LIVE. The next agent fire hits the real broker.

If anything looks off in LIVE mode, flip the navbar dropdown back to PAPER. The next tick reverts every action to paper. No per-action staging — one master toggle owns the whole pipeline.

### How edits take effect

Most settings update on the next agent tick (5-minute cadence). A few are special-cased:

- **`performance.refresh_interval`** / **`performance.market_refresh_time`** — picked up live by the background loop.
- **`alerts.*`** — applied next time `run_cycle` fires.
- **`execution.paper_trading_mode`** / **`execution.shadow_mode`** — applied at the next mode-resolution call (effectively immediately).

You don't have to memorise this — the **(i)** info chip on each row tells you what the setting governs.

---

## Persistence & refresh-cycle controls

Platform stores market data in three tiers: memory cache (Tier 1) → PostgreSQL
database (Tier 2) → broker API (Tier 3). Most read paths check the hierarchy;
if data is stale or corrupt at a tier, skip it and re-fetch from the broker.
This section explains when to bypass the cache + DB and force a fresh fetch.

**Why this matters**: After a code defect or broker API hiccup, cached bars or
symbol maps can linger for hours. The refresh-cycle mode lets you heal the
persistent layers without waiting for TTL expiry.

### Refresh modes

Three modes are available via `/admin/health` navbar chip (admin-only) or
`POST /api/admin/persistence/mode/{off|soft|hard}`:

| Mode | Use | Impact |
|---|---|---|
| **OFF** (default) | Normal reads use cache + DB. Use this almost always. | None — all tiers consulted in order. |
| **SOFT** | Bypass Tier 1 + Tier 2; every read fetches fresh from broker and heals both tiers on write-back. Live ticker untouched. | Charts, intraday bars, symbols may briefly show a few-second re-fetch delay. |
| **HARD** | Same as SOFT + ticker recycle (live WebSocket drops → reconnects → resubscribes). | 2–3 second LTP gap; SSE clients auto-reconnect and show cached prices in the meantime. |

**When to flip SOFT**:
- A chart shows yesterday's wrong close price.
- An instrument symbol is missing from dropdowns.
- Holiday calendar appears incomplete.
- Intraday bars have gaps that shouldn't exist.

**When to flip HARD**:
- SOFT didn't fix it AND you suspect the live ticker's in-memory state is also wrong
  (stale LTP, missing subscriptions).
- Or: ticker is stuck and needs a clean reconnect.

**Process**:
1. Flip mode to SOFT (green chip appears in navbar).
2. Refresh `/charts` or the affected page.
3. Verify data looks correct.
4. Flip mode back to OFF (navbar chip disappears).

Mode resets to OFF on process restart (safe default).

### Reading tier-hit metrics (`/admin/health`)

Each store exposes cache-pressure metrics. Look for:

- **Green (≥80% hit_rate)** — cache is doing its job; broker calls minimal.
- **Amber (50–79% hit_rate)** — warming up after deploy or rotating universe
  (movers entering/leaving). Normal during market open.
- **Red (<50% hit_rate)** — broker-heavy. Either a cold deploy (wait ~5 min) or
  something is wrong (high request volume, broker rate-limiting).

**Columns per store**:
- `tier1_hits` — memory cache hits (cheapest).
- `tier2_hits` — DB hits (post-deploy this is high).
- `tier3_fetches` — broker API calls (most expensive).
- `hit_rate` — (tier1 + tier2) / total.

### Selective invalidation

When an entire refresh mode is too broad, wipe just one store:

```
POST /api/admin/persistence/invalidate?store=ohlcv_daily&symbol=NIFTY50
```

Scopes:
- `store=ohlcv_daily` + `symbol=NIFTY50` → delete daily bars for NIFTY50 only
- `store=ohlcv_daily` + `symbol=NIFTY50&exchange=NSE` → same (exchange
  disambiguates multi-exchange symbols)
- `store=instruments_snapshot&exchange=MCX` → wipe MCX instrument map
- `store=intraday_bars` (no params) → full wipe of intraday cache + DB rows

Next read re-fetches from broker.

### Write-queue health

The `persistence` key in `/admin/health` exposes two worker health snapshots:

```json
{
  "disk_queue": {
    "depth": 42,
    "dropped": 0,
    "last_flush_epoch": 1719325234.561,
    "worker_alive": true
  },
  "db_queue": {
    "depth": 0,
    "dropped": 0,
    "last_flush_epoch": 1719325230.125,
    "worker_alive": true
  }
}
```

- **`depth`** — how many items waiting to write (should be <100 under normal load).
- **`dropped`** — cumulative items that didn't fit in the queue (queue was full). Next
  read re-fetches from broker, so no data loss, just re-work.
- **`last_flush_epoch`** — wall-clock time of the last successful batch write. Subtract
  from `now` to see staleness. Should be <5 sec old during market hours.
- **`worker_alive`** — false if the worker task crashed (operator should check logs +
  restart the service).

### DB tables

Rows older than retention policy are purged by two daily background tasks:

**03:10 IST** — `bg-purge-persistence` — persistence-layer tables + operational tables:

| Table | Retention | Setting key | Purpose |
|---|---|---|---|
| `ohlcv_daily` | 5 years | hard-coded | Daily OHLCV bars for every symbol / exchange. |
| `instruments_snapshot` | 7 days | hard-coded | Per-exchange symbol→token map snapshots. Refreshed daily. |
| `holidays_snapshot` | Forever | no purge | Exchange holiday calendars per year. Immutable. |
| `intraday_bars` | 90 days | hard-coded | 5/15/30/60-minute bars. Growing window per trading day. |
| `algo_events` | 30 days | `retention.algo_events_days` | Write-only agent-state diagnostic journal (~750 rows/day). |
| `algo_order_events` | 90 days | `retention.algo_order_events_days` | Per-order chase timeline. Covers all UI query windows. |
| `auth_tokens` | 7 days after expiry | `retention.auth_tokens_days` | Expired one-time email-verify / password-reset tokens. Active tokens are never deleted. |

**03:15 IST** — `bg-mcp-audit-cleanup`:

| Table | Retention | Setting key | Purpose |
|---|---|---|---|
| `mcp_audit` | 90 days | `mcp.audit_retention_days` | MCP-initiated mutations (Lab / Claude Code actions). |

**03:20 IST** — `bg-purge-audit-log`:

| Table | Retention | Setting key | Purpose |
|---|---|---|---|
| `audit_log` | 365 days | `retention.audit_log_days` | Forensic trail for operator investigations. Not the SEBI-compliance ledger (that is `nav_daily` + `daily_book`, kept forever). |

**Financial records — never purged**:

| Table | Reason |
|---|---|
| `nav_daily` | SEBI Cat-III 8-year horizon; ~365 rows/year |
| `daily_book` | P&L record; unique constraint caps growth at ~160 rows/day |
| `investor_events` | Capital ledger; immutable audit trail per LP |
| `monthly_statements` | Statement log; 1 row/user/month |

**Tuning retention** — all configurable keys are editable live from `/admin/settings` → Retention section. Set any key to `0` to disable auto-purge for that table.

---

## Market lifecycle events

Per-exchange transition system. Singleton `MarketLifecycle` polled
every 30s by `_task_market_lifecycle`. Fires registered async
handlers on session-boundary crossings.

| Event | When |
|---|---|
| `nse:open` | NSE equity open (09:15 IST) |
| `nse:close` | NSE equity close (15:30 IST) |
| `nse:close_settled` | 45 min after close (catches Kite's late close_price adjustment) |
| `mcx:open` / `:close` / `:close_settled` | 09:00 / 23:30 / 00:15 IST |
| `cds:open` / `:close` / `:close_settled` | 09:15 / 15:30 / 16:15 IST |

**Default snapshot handlers** (`market_lifecycle_handlers.py`):
- `<exch>:close` → `snapshot_daily_book()` — captures positions / holdings / funds / trades
- `<exch>:close_settled` → same handler runs again; UPSERT overwrites with broker's adjusted close_price + last_price (the values that drive `daily_book.ltp` reads in `positions.py`)
- `nse:close` → also writes NAV snapshot via `write_nav_snapshot()`

**Tunables** (`/admin/settings`):
- `market_lifecycle.settled_offset_min` (default 45) — minutes between
  close and close_settled. Lower it to catch faster brokers; raise it
  if late-publishing brokers (Dhan / Groww) need more time.

**Debugging failed handlers**:
1. `SELECT * FROM market_lifecycle_events ORDER BY fired_at DESC LIMIT 20;`
2. Rows with `handlers_failed > 0` indicate a handler raised — check `.log/api_log_file` for the matching `market_lifecycle: handler ... raised` line.
3. The dispatcher swallows handler exceptions so a single failure does NOT block other handlers in the chain.

**Off-hours funds refresh** — `_task_funds_offhours` fires the cheap
`fetch_margins()` broker call every 30 min while no segment is open.
Catches operator fund transfers (e.g. NEFT after-hours) so NavCard +
`/performance` reflect new cash balance without waiting for the next
session.

---

## Persistence + chart data backfill

**Problem**: Charts blank ("no data for X symbols"), sparklines stale, or you
need historical bars urgently. Three-tier persistence layer (memory → DB →
broker) + auto-backfill solves this.

### Auto-backfill on startup

Process boot runs `_task_warm_backfill` (60 s delay) which:
1. Inspects 300-symbol universe (watchlist + holdings + positions + movers)
2. For each symbol with <70% daily coverage (365-day window), calls
   `backfill_ohlcv_daily()` → forces broker fetch
3. For today's bars, if any segment open, calls `backfill_intraday_today()` →
   fetches 30-min candles

Startup backfill respects broker rate-limits and cool-off windows; skips
accounts in rate-limit cooloff.

### Admin endpoint — on-demand backfill

```
POST /api/admin/persistence/backfill?kind=daily|intraday|both
```

Async repair for the 300-symbol universe. Response: `{"message": "…",
"task_id": "…"}`. Check the activity log to see progress. Admin-guarded.

### CLI — immediate recovery

For production defect (e.g. "all charts are empty"):

```bash
# Option 1: wrapper that reads secrets from operator login
python scripts/persistence_mode.py off|soft|hard|status

# Option 2: force-backfill all symbols immediately (blocks until done)
python scripts/backfill_ohlcv.py --daily --intraday
```

Both skip broker accounts in rate-limit cooloff (checks `_RATE_LIMIT_COOLOFF`
set + checks seconds since last rate-limit exception).

### Chart self-heal during live market

When you navigate to a chart mid-session:

1. `/api/options/historical?symbol=RELIANCE&days=30` checks DB coverage
2. If <70% of requested days present (configurable, tunable via
   `/admin/settings` → `chart_self_heal_coverage_threshold`)
3. Auto-triggers broker fetch (if ≥1 broker available + not in cooloff)
4. Response carries `"partial": bool` → UI renders hint "Partial data — retrying"
5. One healing per symbol per 60s (throttled logging)

Graceful under rate-limiting: if broker unavailable, returns what's in DB.

### Closed-hours data layer (db_only mode)

When all segments are closed:
- Every data read (positions, holdings, sparklines, charts) skips broker → reads DB snapshot
- In-session closed-hours sparkline refresh (5-min cadence) uses `db_only=True` mode
- DB row must exist; if not, graceful fallback (blank / retry next open)

Eliminates blank grids + "connection lost" errors. Snapshots captured at:
- `nse:close` / `nse:close_settled` — NSE positions / holdings / cash
- `mcx:close` / `mcx:close_settled` — MCX positions
- `cds:close` / `cds:close_settled` — CDS positions + NAV

Closed-hours + open-hours flows use the same `closed_hours_or_broker()` gate in
`backend/api/helpers/snapshot_gate.py` — single SSOT for the decision.

### Persistence mode tunables

Three modes (runtime-only, reset to `off` on restart):

| Mode | Tier 1 | Tier 2 | Tier 3 | Use case |
|---|---|---|---|---|
| **off** (default) | ✓ | ✓ | ✓ | Normal |
| **soft** | ✗ | ✗ | ✓ | Force broker fetch (bypass cache); write-back heals all tiers |
| **hard** | ✗ | ✗ | ✓ | soft + unsubscribe/resubscribe KiteTicker (drastic; last resort) |

Flip via:
```
POST /api/admin/persistence/mode/soft  (or hard, or off)
```

Returns current mode + active timestamp. Resets to `off` on process restart.

---

## Common tasks

**Add custom loss rule** → `/automation` → copy `loss-pos-acct-static-abs` → replace scope with custom account matcher → Validate → Run in Simulator → ON

**Add new metric** → Write Python `(ctx, row) → number` → `/admin/tokens` → + New token → Category condition, kind metric, resolver → Reload registry

**Market drops 6%?** → `/admin/execution?mode=sim` → `generic-crash` → Load live book → Start → watch Simulator tab

**Auto-close safely** → `/automation` → `loss-pos-total-auto-close` → Run in Simulator → check Order tab output → ON

**Tune threshold live** → `/admin/settings` → edit value → Save (takes effect next agent tick, ≤5 min)

**Flip paper → live** → Navbar dropdown PAPER → watch `/admin/execution?mode=paper` → Navbar dropdown LIVE → next fire hits broker → inspect `/orders` for `mode='live'` row

---

## Pre-activation checklist

- [ ] Description explains what and why
- [ ] Condition tree passes **Validate**
- [ ] Cooldown ≥ few minutes
- [ ] Schedule = `market_hours` (unless overnight OK)
- [ ] Tested in Simulator with representative scenario
- [ ] Actions (if any) params correct and safe

---

## Broker connection service (`ramboq_conn.service`)

**Overview**: Separate systemd service that owns all broker sessions (Kite
WebSocket, Dhan/Groww tokens). Runs on `/tmp/ramboq_conn.sock` (UDS). Main API
(`ramboq_api`) is client-only when `RAMBOQ_USE_CONN_SERVICE=1` is set.

### Installation + systemd

**File paths**:
- Service file: `/etc/systemd/system/ramboq_conn.service`
- Drop-in for main API: `/etc/systemd/system/ramboq_api.service.d/conn.conf`
- Drop-in for dev API: `/etc/systemd/system/ramboq_dev_api.service.d/conn.conf`

**Check service status**:
```bash
systemctl status ramboq_conn
journalctl -fu ramboq_conn  # tail logs
```

**Manual restart** (broker state reset):
```bash
systemctl restart ramboq_conn
```

**Reload after broker code changes**:
`deploy.sh` auto-restarts if files under `backend/brokers/` changed.
Frontend-only pushes don't touch conn-service.

### Logs and troubleshooting

Conn logs live at `/opt/ramboq/.log/conn_log_file` (same directory as main API
logs). Same rotation: 5MB × 5 files.

**View logs**:
```bash
tail -f /opt/ramboq/.log/conn_log_file
# Or via navbar: /admin/activity → Conn tab
```

**Common issues**:

| Symptom | Check |
|---|---|
| Main API 500s on broker call | `systemctl status ramboq_conn` — is the service running? Check `/tmp/ramboq_conn.sock` exists. |
| Ticker stale (no LTP updates) | Conn service health: `curl --unix-socket /tmp/ramboq_conn.sock http://localhost/health` (should be 200 with `{"status": "healthy"}`). Check `/dev/shm/ramboq_ticks` exists (mmap buffer). |
| "Failed to connect to socket" errors in main API logs | Conn service crashed or didn't start. Check `systemctl start ramboq_conn` manually. |
| Postback 401 errors | Conn service must restart after broker credentials change (so it reloads API secrets). Automatic via deploy, or manual `systemctl restart ramboq_conn`. |

### Health endpoint

`GET /api/admin/health` (main API) includes conn-service status under `broker_connection`:

```json
{
  "broker_connection": {
    "status": "healthy",
    "ticker": {
      "started": true,
      "connected": true,
      "stale_count": 0,
      "max_age_seconds": 42
    }
  }
}
```

- **status**: "healthy" (all checks pass) / "degraded" (service reachable but ticker down) / "unreachable" (UDS error)
- **ticker.started**: Kite WebSocket initialized
- **ticker.connected**: Kite WebSocket actively receiving ticks
- **stale_count**: # symbols with no tick in last 60s (even if subscribed)
- **max_age_seconds**: oldest stale symbol age

### Dev shares prod UDS

By design, `ramboq_dev_api` mounts the same `/tmp/ramboq_conn.sock` as prod.
**No separate dev conn-service instance** — dev API uses prod's broker sessions.

This avoids:
- Parallel Dhan logins (single IP per token; the same IP source-bound account
  can't have two Dhan sessions).
- Operator confusion (two separate ticker streams, duplicate account load).
- Extra systemd management.

When you run dev:
1. Prod conn-service must be running (or will fail immediately)
2. Dev API connects to prod broker sessions over UDS
3. A broker credential update on prod's `/admin/brokers` is live to dev instantly

**If you need isolated dev testing**:
- Stop ramboq_conn: `systemctl stop ramboq_conn`
- Drop `/tmp/ramboq_conn.sock` if stale: `rm /tmp/ramboq_conn.sock`
- Unset `RAMBOQ_USE_CONN_SERVICE=1` from dev API drop-in, restart dev
- Dev API will spawn its own local Connections() singleton

---

## Brokers — `/admin/brokers`

Manage accounts via UI, no SSH/YAML/restart. Page: account table (code | broker
| API key | source IP | status pill | notes | Test/Edit/Delete) + **+ New
account** button.

**Add account**: code (unique) · broker (Zerodha Kite / Dhan / Groww) · API
key/secret/password/TOTP (encrypted at rest) · source IP (IPv6 for multi-account
Kite binding) · notes. Click **Test** to verify.

**Edit**: click Edit → secret fields blank by default (blank = unchanged) → edit
+ Save. Change takes effect when conn-service picks up the DB update (typically
within 2–3 seconds).

**How it works**: Credentials are stored encrypted in `broker_accounts` DB table.
When you save a new account or edit one, the main API signals the conn-service
via a health-check call (or direct RPC in future versions). The conn-service
reloads broker credentials and applies them to its live sessions. No main API
restart required.

**Capabilities endpoint** (`GET /api/admin/brokers/{account}/capabilities`):
returns `BrokerCapabilities` dataclass (gtt_single / gtt_oco / gtt_modify / etc.)
for in-page feature gating (OrderTicket warning chips).

### Circuit-breaker opt-in (Dhan accounts only)

Dhan accounts support a **circuit-breaker** that pauses all broker fetch calls for a
configurable back-off window after repeated consecutive failures, then re-probes
automatically. This avoids hammering a temporarily-unavailable Dhan endpoint on
every poll tick and reducing error log noise.

**Enabling**: In the `/admin/brokers` table, each Dhan row shows a **"breaker"**
checkbox in the Poll column. Check it, then click Save. The change is stored in
`broker_accounts.circuit_breaker_enabled` and takes effect on the next fetch cycle
(no restart required). Kite and Groww rows do not show this checkbox because their
adapters do not implement the circuit-breaker protocol.

**How it works**:
- After `circuit_breaker_consecutive_fail_threshold` consecutive failures (default 3),
  the circuit **OPENS** and all fetches for that account return an empty result
  immediately (no broker call) until the back-off window expires.
- After the window, the circuit enters **PROBE** state: one real fetch is attempted.
  Success → circuit CLOSES (normal operation). Failure → window resets and the circuit
  opens again.
- Accounts with `circuit_breaker_enabled = False` (the default) never enter OPEN/PROBE
  state; they retry on every poll regardless of consecutive failures.

**Navbar health badge**: The `BrokerHealthBadge` (top-right navbar, admin only) shows a
per-account modal when clicked. For opt-in accounts:
- **OPEN chip** (amber) — circuit is tripped; shows "circuit open until `<time>`" tooltip.
- **PROBE chip** (sky-blue) — back-off expired; next fetch is a live probe.

For non-opt-in accounts in red state, the tooltip says "retrying every poll" — no OPEN/PROBE
chips are shown even if the underlying circuit state column has a value.

**API surface**:
- `PATCH /api/admin/brokers/{account}` accepts `circuit_breaker_enabled: bool`.
- `GET /api/admin/broker-health` response includes `circuit_breaker_enabled`,
  `circuit_state`, `circuit_open_until`, and `consecutive_fail_count` per account.

**Startup behaviour**: On first boot after the column was added to the DB schema, the
platform automatically enables the circuit breaker for `DH6847` (the production Dhan
account on the shared IP). All other Dhan accounts default to `False`. To override,
use the checkbox in the UI or PATCH the endpoint.

### Market-status resolution

`probe_market_active(exchange)` (the gate every `market_hours`-scheduled agent + the daily snapshot pipeline uses) resolves in this order:

1. **Broker market-status API** — iterate `all_brokers()`, call `broker.market_status(exchange)`. First definitive `True`/`False` wins + caches 60s. Adapters that don't implement the method return `None`; the loop continues to the next broker.
2. **Bellwether-quote probe** — fallback for brokers (Kite) without a market-status endpoint. Calls `kite.quote()` on configured bellwether symbols and checks `last_trade_time` freshness. Defaults: `NSE:NIFTY 50` + `NSE:NIFTY BANK` for NSE/NFO, `BSE:SENSEX` for BSE/BFO, `NSE:NIFTY 50` for CDS. MCX uses **dynamic instrument discovery** — `_discover_mcx_bellwethers` pulls the live instruments dump and picks the nearest unexpired futures contract for the most-liquid commodities (CRUDEOIL → NATURALGAS → GOLD priority); contract months roll automatically as expiries pass.
3. **Calendar verdict** — if neither path yields, the platform falls back to its weekday + holiday-set logic.

Adapter coverage today:

| Broker | `market_status()` | Falls through to bellwether? |
|---|---|---|
| Kite (zerodha_kite) | Returns `None` (no SDK endpoint) | Yes |
| Dhan | Probes `get_market_status` / `market_status` / `get_exchange_status` across SDK versions. Maps NSE/BSE/NFO/BFO/CDS/MCX → NSE_EQ/BSE_EQ/NSE_FNO/BSE_FNO/NSE_CURRENCY/MCX_COMM. | Only if SDK miss / call failure |
| Groww | Same SDK-method probe + segment mapping. Wraps in `_retry_groww_auth` so token rotation is transparent. | Only if SDK miss / call failure |

**Cache**: 60s per exchange in `_PROBE_CACHE`. Clear via `market_probe.invalidate_cache(exchange=None)` from a Python shell if you need an immediate re-evaluation.

**Operator override**: bellwether symbols are configurable via the `market.bellwether_symbols` setting (CSV of `EXCHANGE:SYMBOL` entries). Only matters if the broker market-status path returns `None` for all loaded accounts.

### Order size limits + close-position intent

All broker adapters (`backend/brokers/base.py` + implementations: kite.py, dhan.py,
groww.py, remote_broker.py) implement quantity guards on `place_order()`. When the
platform needs to bypass these guards (e.g., force-closing a large position), a
keyword-only `intent: str | None = None` parameter instructs the adapter how to
proceed.

**Quantity ceilings** — Kite adapter:
- **Equity (EQ/BSE)**: no enforced ceiling (broker-validated).
- **F&O (NFO)**: **50-lot ceiling** for open orders. MCX (CRUDEOIL, GOLDM, etc.) and
  BFO / CDS (F&O indices, currency) have **50,000-contract ceiling**.
- **Close orders** (`intent="close"`): both the 50-lot and 50,000-contract ceilings
  are **bypassed**. Any quantity is forwarded to the broker. An INFO log is emitted
  whenever an MCX close bypasses the ceiling (e.g., "MCX order qty 51+ lots").
- Dhan / Groww adapters: accept but ignore the `intent` parameter (no behavioural change;
  passed as a kwarg to prevent SDK interference).
- RemoteBroker (conn-service RPC layer): forwards `intent` via the RPC call so the
  real adapter at the conn endpoint receives it.

**Supervisor responsibility** — the order-placement routes and agent engine pass
`intent="close"` for close-position actions (hedge legs, ladder scale-outs, agent
auto-close). The adapter layer is the last line of defense; a 51-lot open order on
MCX will still hard-block at the adapter ceiling even if the route passed `intent=None`.

**Troubleshooting close-position size rejections**:
- Check the activity log (`/admin/activity` → Orders tab) — the AlgoOrder row `.detail`
  should show either the broker's error message or a backend guard rejection (e.g.,
  "QTY_MULTIPLE", "FAT_FINGER", "MARGIN_SHORTFALL").
- If the row is absent, the order never reached `place_order()` — check the route's
  preflight guards (`backend/api/algo/actions.py`).
- A close order hitting the 50,000-contract MCX ceiling despite `intent="close"` suggests
  the intent wasn't propagated; inspect the logs for "quantity bypass invoked" INFO lines.

### Broker postback webhooks

| Broker | Webhook URL | Status |
|---|---|---|
| Kite (Zerodha) | `https://ramboq.com/api/orders/postback` | Wired with HMAC-SHA256 validation (`order_id + order_timestamp + api_secret`). Configure in Kite developer console per app. |
| Dhan | `https://ramboq.com/api/orders/dhan_postback` | Wired with status translation (`_DHAN_STATUS_TO_KITE`) and full fan-out (AlgoOrder sync, audit log, cache invalidate, WS broadcasts). Configure in Dhan partner dashboard per account. |
| Groww | `https://ramboq.com/api/orders/groww_postback` | Scaffold route — same shape as Dhan. Groww postback support is uncertain per the broker-API audit. |

All three routes:
- `guards=[]` (broker webhooks deliver unauthenticated; integrity ensured by signature where supported)
- Best-effort: never 5xx (broker retries on non-2xx)
- Same fan-out as Kite: AlgoOrder row sync by `broker_order_id`, audit log entry tagged `order.fill|cancel|reject`, cache invalidation (orders / positions / holdings on terminal), WS broadcasts (order_update + position_filled on COMPLETE + book_changed on terminal)

Without postback configuration, the chase loop's 20-second poll catches the fill — operator-visible lag of up to 20s. With postback configured, fills land in roughly a second.

### Connection Audit Log

Every broker lifecycle event (auth failures, token rotations, fetch errors, circuit-breaker transitions, ticker reconnects) is logged to `broker_connection_events` table. Use the audit log to diagnose credential issues, network timeouts, and account health without digging through logs.

**Where to find it:**

```
GET /api/admin/health/broker-connection-events?
  account=ZG0790&
  event_type=auth_fail&
  limit=50
```

Or open the navbar Health chip → Per-account modal → click a row to see recent events.

**Event types and meaning:**

| Type | Meaning | Action |
|---|---|---|
| `auth_fail` | OAuth/TOTP rejected or connection failed | Check broker dashboard (API key active? IP whitelisted?). TOTP may be stale on server — sync time. |
| `fetch_fail` | Market data call (positions/holdings/margins) raised exception (429, 401, 500) | Check broker status page. If rate-limited (429), wait 30s cooloff. If auth (401), re-mint credentials. |
| `token_ok` | Token refresh succeeded | Informational — no action needed. |
| `rotation_detected` | New token differs from prior token (2FA re-mint, or broker key rotation) | Informational. New token persisted successfully. |
| `fetch_ok_recovery` | First successful fetch after consecutive failures | Circuit breaker transitioned to HALF-OPEN → CLOSED. Account health recovered. |
| `circuit_open` | Account skipped for 5-30 min due to ≥3 consecutive failures | Check `fetch_fail` events in same time window. Enable circuit-breaker opt-in if not already. |
| `circuit_close` | Circuit breaker re-armed after cooloff period | Normal. Account resuming normal polling. |
| `ticker_close` | KiteTicker WebSocket disconnected | Informational. TickerManager watchdog will auto-reconnect. |
| `ticker_error` | KiteTicker on_error callback fired | Check network + broker status. WebSocket likely corrupted; watchdog will swap to backup. |
| `ticker_reconnect` | TickerManager.swap() executed (cycled WebSocket) | Informational. Caused by upstream ticker error or stale connection. |

**Filter by time range:**

```
GET /api/admin/health/broker-connection-events?
  since=2026-07-15T00:00:00Z&
  account=ZG0790&
  limit=100
```

`since` accepts ISO 8601 timestamps. Default: last 24 hours if omitted.

**Diagnostic workflow:**

1. **"Account not refreshing"** → filter by account + `event_type=fetch_fail` → check the most recent error message in `detail` field → decide if transient (retry) or persistent (re-auth).
2. **"Orders stuck in OPEN"** → filter `event_type=ticker_error|ticker_close` in the last hour → ticker health matters for chase loops (status polling falls back if ticks stale).
3. **"Circuit breaker tripped"** → filter by account + `event_type=circuit_open` → look back 30s for the preceding `fetch_fail` events → understand what caused the trip.

**Retention:** Log entries are kept indefinitely. No automatic purge.

---

## Investor portal — mint URL for an LP

LP-facing read-only page at `/investor/<token>` showing the LP's NAV slice + 180-day curve. **Token is the credential** — no LP login, no password. Operator mints + forwards URL through their own channel (WhatsApp / email).

**Mint a URL:**
1. `/admin` → find the LP's user row → click **Portal** (cyan button, designated-only)
2. Modal opens. Set **Expires in** (default 90 days, cap 10y) + optional **Note** (e.g. "WhatsApp to LP 2026-06-23")
3. Click **Mint** → the full URL appears in a green panel
4. Click **Copy** → paste into WhatsApp / email → send to LP

**Token is shown ONCE.** After the modal closes the token-list table surfaces only the first-8-char preview. To re-share, mint a new one.

**Revoke a URL:**
- Same modal lists every minted token (active / revoked / expired pills + last-visit timestamp + visit count)
- Click **Revoke** on the row → confirm → URL 401s immediately on next visit
- Idempotent — revoking an already-revoked row is a no-op

**Operator visibility:**
- `last_visit_at` + `visit_count` on each row so you can see "this LP last looked at statements 3 weeks ago" without leaving the modal
- Visit counter increments per slice + per history fetch (so a single page load bumps it by 2)

**Endpoints:**

| Route | Cap | Purpose |
|---|---|---|
| `GET /api/admin/users/{id}/investor-tokens` | `manage_investor_tokens` | List rows (no full token, just preview) |
| `POST /api/admin/users/{id}/investor-tokens` | `manage_investor_tokens` | Mint (returns full token + portal URL ONCE) |
| `DELETE /api/admin/users/{id}/investor-tokens/{tid}` | `manage_investor_tokens` | Revoke |
| `GET /api/investor/{token}/slice` | none — token in URL | Current NAV slice (public) |
| `GET /api/investor/{token}/history?days=180` | none — token in URL | NAV curve (public) |

**Cap**: `manage_investor_tokens` is `designated`-only. Trader, risk, admin, and partner cannot mint — LP onboarding is a designated activity.

**Math** (units model — slice 7N+):

```
units_held(user, t)   = Σ units_delta for events <= t
total_units(t)        = Σ units_held across every LP
nav_per_unit(t)       = firm_nav(t) / total_units(t)
slice(user, t)        = units_held × nav_per_unit
cost_basis(user, t)   = Σ amount (sub+bootstrap) − Σ amount (redemption)
pnl(user, t)          = slice − cost_basis
```

All four surfaces — `/api/nav/me`, `/api/nav/me/history`, `/api/investor/{token}/slice` + `/history`, and the monthly PDF — use the same `investor_units.compute_slice()` helper.

**Auto-bootstrap** runs on first compute. For every eligible LP (active + share_pct > 0) without events, inserts one synthetic event:

| Field | Value |
|---|---|
| `event_type` | `bootstrap` |
| `units_delta` | `User.share_pct` |
| `amount` | `User.contribution` |
| `nav_per_unit` | `contribution / share_pct` (or `1.0` when contribution=0) |
| `event_date` | `contribution_date` → `created_at` → today (fallback chain) |
| `note` | `auto-bootstrap from v1 share_pct` |

When share_pcts sum to 100 across all eligible LPs, this reproduces v1 numbers exactly. When sum != 100 (operator residual implied via low share_pct), units math redistributes proportionally and slices sum to `firm_nav` by construction.

**Verifying bootstrap after deploy:**

1. Hit `/nav` once (as the operator, or any authenticated LP) → triggers auto-bootstrap
2. Open `/admin` → pick an eligible LP → Portal → Events tab → confirm one `Bootstrap` pill row exists with the right `units_delta` + `amount`
3. Spot-check `slice = units_held × nav_per_unit` against the headline value the LP sees

**Logging real subscription / redemption events:**

In `/admin` → user row → Portal → Events tab → Add event:

| Field | What to enter |
|---|---|
| Type | Subscription (capital in) / Redemption (capital out) / Bootstrap (correction) |
| Date | `YYYY-MM-DD` of the bank transfer / wire |
| Amount | Positive rupees, regardless of direction |
| NAV/unit | Per-unit value on that date (compute from firm_nav ÷ total_units, or trust the operator's reconciliation) |
| Note | Optional — e.g. "Wire ref 12345" |

The backend computes signed `units_delta = ±amount/nav_per_unit` automatically.

**Security model:** The URL IS the credential, same shape as Carta / SS&C investor magic-links. Don't email it to a shared inbox. If you suspect leakage, revoke + re-mint.

---

## OrderTicket modal

Single entry point for all order ops (open/close/modify/repeat/cancel) across all instruments. Auto-detects instrument kind from symbol (CNC/MIS for EQ, NRML/MIS for F&O). Side toggle: ADD/CLOSE (if position open) or BUY/SELL (if closed). Three submit modes: DRAFT (append to caller's array, `/admin/derivatives` chain clicks) · PAPER (`mode=paper`, real bid/ask via chase) · LIVE (branch=main + paper_trading_mode=False). Opens from: `/admin/derivatives` chain · every page-header Order icon (amber `+`)  · Chart icon (cyan line) · Activity icon (violet 3-line). Symbol anchors auto-resolve to contract (`NIFTY 50` → `NIFTY26JUNFUT`).

---

## Demo mode — public algo console

Anonymous prod visitors see real broker data with accounts masked (`ZG####`), can place paper orders (real chase loop, no broker touch). Zero maintenance — no fixture file.

Backend: `is_demo_request()` = main + no JWT. `auth_or_demo_guard` admits + sets `is_demo=True`. Write endpoints: `place`/`modify`/`cancel` 403; `/api/orders/ticket` downgrades `live → paper`. Read data masked via `mask_column()`.

Frontend: `branch=main + !user` = demo. Settings/Brokers/Users nav links hide. Navbar badges: `DEMO` (purple) / `PAPER` (blue) / `SIM` (red).

---

## Roles — the canonical 5 + the designated escape hatch

The platform's RBAC surface is **5 canonical roles** + 1 legacy preserved role + a synthetic role for anonymous visitors:

| Role | Caps | Notes |
|---|---|---|
| `designated` | Everything per the matrix in `backend/api/rbac.py::CAPS` | Firm owner / top tier |
| `trader` | place / modify / cancel orders + view book; horizontal scope via `assigned_accounts` + `assigned_strategies` | PM tier |
| `risk` | view everything + kill-switch + adjust risk floors | Compliance + on-call monitoring |
| `admin` | manage brokers, test broker connection, view audit, view health, trigger NAV compute, view settings (read-only) | Operational support |
| `partner` | view-only aggregate; no trading, no settings | LP-style read access |
| `demo` | view-only on prod (anonymous visitor) | Public surface, no auth |

**Where `designated` still matters at the code level:**
- `designated_guard` on `/admin/users/{username}/terminate` and `/admin/users/{username}/toggle-designated` endpoints (can't terminate other admins or promote/demote between admin↔designated unless you're designated)
- `alert_utils.get_alert_recipients()` always includes designated emails regardless of `receive_alerts` toggle
- `/admin` UI hides terminate / promote / view-as buttons unless the actor is designated

The designated role has all admin caps PLUS three super-admin gates. Going forward new users should land on one of the 5 canonical roles (`designated / trader / risk / admin / partner`). The auto-bootstrap migration (init_db) is complete and no longer runs. Operator can promote an admin → designated via `/admin` → user row → Promote button.

### Audit workflow — the #audit tag

The operator runs a periodic comprehensive audit by writing `#audit` (literal hashtag) in chat. The platform's coding agent dispatches parallel audit subagents across 8 dimensions and synthesizes findings:

| Dimension | Scope |
|---|---|
| Performance | Hot-path latencies, sequential awaits, missing caches, polling cadences |
| Defects | Race conditions, broken error paths, type confusion, off-by-one |
| Stale code | Dead branches, unused imports / functions, deprecated comments |
| UX consistency | Duplicate components, mismatched card chrome, inconsistent button placement |
| Color palette | Off-palette hex codes, CSS custom-prop vs hardcoded rgba |
| Broker API parity | Per-adapter (Kite/Dhan/Groww) implementation gaps, Kite-shape compliance |
| Data layer | Schema drift, missing indexes, unused columns, FK ON DELETE behavior |
| Docs | Every .md guide + .pdf assets — drift, gaps, inconsistency |

Reports are summarized into a punch list with severity (HIGH / MED / LOW) and proposed remediation slices. Audit findings shipped to date are tracked in commits with the `audit slice` prefix (`audit slice A`, `audit slice B`, etc.).

---

## Navbar surface

Pages grouped + ordered by daily-operator frequency. Two inline + two dropdown groups + one "Tour" entry:

| Group | Items | Visibility |
|---|---|---|
| `monitor` (inline) | Tour · Pulse · Dashboard · Orders · Derivatives · Charts · Automation · Strategies · NAV | Always visible |
| `explore` (inline) | Sandbox (URL `/admin/execution`) | Always visible |
| `build` (dropdown) | Console · Research · Tokens | Click trigger to expand |
| `config` (dropdown, admin) | Brokers · Settings · Users · Statements · History · Audit · Health | `adminOnly: true` |

Renames (Jun 2026):
- **`modes` group → `explore`** — the old name was vestigial from the sim/paper/live/shadow/replay terminology before the mode toggles moved to the navbar dropdown.
- **`Lab` label → `Sandbox`** — matches industry-standard naming (QuantConnect / Streak / Sensibull all use "Sandbox" for this surface). The URL `/admin/execution` is unchanged so deep links + bookmarks keep working.
- **Monitor resequenced** — Orders moved ahead of analysis surfaces (Derivatives / Charts); Strategies + NAV moved to the end of the group (lower daily frequency for a working trader).

Group rendering: `INLINE_GROUPS` in `(algo)/+layout.svelte` (`monitor`, `analyze`, `explore`) controls which groups render inline; the rest collapse to dropdown triggers. Mobile drawer shows EVERY group with a `GROUP_LABELS` caption.

---

## Order placement latency — preflight + tick + paper-skip

Three perf fixes shipped Jun 2026 to address the ticket-path slowdown that accumulated across recent slices:

**1. Parallel preflight** ([backend/api/algo/actions.py::run_preflight](backend/api/algo/actions.py)):
- Pre-fix: 4 sequential `broker.{profile, instruments, basket_order_margins, margins}` calls — ~800-1200ms total on Kite.
- Now: one helper coroutine per call (`_fetch_profile` / `_fetch_instruments` / `_fetch_basket_margin` / `_fetch_account_margins`), all four fired via `asyncio.gather`. Wall-time drops to `max(individual call)` ≈ 300ms.
- Each helper preserves its own exception handling so a broker-side failure on one doesn't sink the others.

**2. Tick-size index** ([backend/api/routes/orders.py::_align_price_to_tick](backend/api/routes/orders.py)):
- Pre-fix: linear scan through ~10-50k instrument rows on every call. Ticket route calls twice (price + trigger), so a single order paid ~100k linear iterations.
- Now: `_TICK_INDEX` dict keyed by `(exchange, symbol)` → tick_size, built lazily from the instruments cache. `_TICK_INDEX_STAMP` tracks the cached response instance; identity flip → rebuild. Subsequent lookups are O(1).

**3. PAPER skips route-level preflight**:
- Pre-fix: `ticket_order` called `run_preflight()` for BOTH LIVE and PAPER. The paper engine itself already runs basket_margin internally (`PaperTradeEngine.register_open_order`'s REJECTED-vs-OPEN gate), so the route-level check was duplicate work.
- Now: PAPER branch skips the preflight call entirely. Same QTY_FREEZE / SEGMENT_INACTIVE / MARGIN_SHORTFALL conditions still surface in the AlgoOrder row's `.detail` field within one engine tick.
- LIVE preflight stays — only chance to block before `kite.place_order` fires.

**Combined savings**: ~600ms LIVE, ~1500ms PAPER per ticket.

---

## Frontend perf budgets (Jul 2026 audit)

Comprehensive frontend perf audit closed three systemic regression classes flagged by repeated operator complaints ("dropdown lag" on `/admin/derivatives`, "refresh button stuck" mid-animation). Patches at `frontend/src/lib/ws.js`, `frontend/src/lib/RefreshButton.svelte`, `frontend/src/lib/CollapseButton.svelte`, `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`. Regression guards in `frontend/e2e/main_thread_perf.spec.js`.

**Three patches**:

1. **Subscription leaks closed**. RefreshButton + CollapseButton `.subscribe()` calls were module-top-level (no unsub). 1-3 RefreshButtons + dozens of CollapseButtons per page × no route-transition teardown = unbounded listener growth. Bound subscribes inside `onMount` + tear down in `onDestroy`.

2. **Singleton WebSocket pool**. `createPerformanceSocket` / `createAlgoSocket` now share a ref-counted singleton per endpoint. Pre-fix: 3-5 parallel `/ws/performance` connections per tab. Post-fix: ≤1 active connection, fan-out to all subscribers, auto-close when the last unsubs.

3. **Dropdown click debounce + click-defer**. `<Select>` pick on `/admin/derivatives` fired `goto({replaceState:true})` synchronously inside the underlying-change `$effect`. Debounced to 150 ms. RefreshButton click now defers `onClick` via `queueMicrotask` so the disabled/spinner state paints BEFORE the parent's sync prep work begins.

**Perf budgets** (enforced by `main_thread_perf.spec.js`):

| Dimension | Budget |
|---|---|
| Max long-task during interaction | < 100 ms |
| Click-to-feedback latency | < 350 ms |
| JS heap growth (idle) | < 5 MB/min |
| `/ws/performance` connections per tab | ≤ 2 (singleton + reconnect) |
| Dropdown pick → panel-close (derivatives) | < 500 ms |
| Dropdown pick max long-task | < 150 ms |
| Heap growth over 5-page nav lap | < 8 MB |
| New WS opens on second nav lap | ≤ 2 |

**Audit rule**: any `.subscribe()` call outside a singleton module MUST have a paired unsub in the component's `onDestroy`. Any new page that subscribes to `/ws/performance` or `/ws/algo` MUST go through `createPerformanceSocket` / `createAlgoSocket` — never `new WebSocket(...)` directly.

---

## Postback fan-out — book_changed bus

On every terminal order status from a broker postback (COMPLETE / CANCELLED / REJECTED / EXPIRED), the backend invalidates the orders + positions + holdings caches and broadcasts a `book_changed` WebSocket event. Every algo page subscribes to a shared debounced bus and refetches its primary loader in lockstep — single-iteration UI settle, replaces the prior "wait for the next 5–15 s poll" path.

**Backend chain** ([orders.py::order_postback](backend/api/routes/orders.py)):

```
postback received
  → invalidate("orders")               # always
  → if terminal:
      invalidate("positions")
      invalidate("holdings")
      broadcast({event: "book_changed", account, exchange,
                 tradingsymbol, reason, ts})
  → if COMPLETE:
      broadcast({event: "position_filled", qty: signed_delta, ...})
```

`position_filled` is preserved — it carries the signed-qty delta for the per-cell optimistic patch on Pulse + Performance. `book_changed` is the new coordinated coverage that ALSO catches cancel / reject paths.

**Frontend subscriber** ([$lib/data/bookChanged.js](frontend/src/lib/data/bookChanged.js)):

- Singleton WebSocket subscriber started from the algo layout's `onMount`. Idempotent — multiple calls share one connection.
- Listens for `book_changed`, debounces 200ms (coalesces basket-order bursts into one refresh), increments a monotonic `$bookChanged` store + sets `$lastBookEvent` to the latest payload.
- Pages subscribe with a `$effect` that watches the counter and calls their primary loader once per increment.

**Surfaces wired:**

| Page | Loader called on bookChanged |
|---|---|
| `/admin/derivatives` | `loadPositions()` + `loadStrategy()` |
| `/dashboard` | `loadHero()` |
| `/pulse` | `loadPulse()` |
| `/orders` | `_debouncedLoadOrders()` |
| `/performance` | `loadAll({ fresh: true })` |

**Troubleshooting:**

- **"Page didn't refresh after fill"** — open browser DevTools console; the bus warns when its WS startup fails. Hit page-header Refresh button to manually catch up.
- **"WebSocket keeps reconnecting"** — Cloudflare in orange-cloud mode blocks raw WS upgrades. `webhook.ramboq.com` MUST be grey cloud (DNS only). Verify in Cloudflare DNS settings.
- **"Bus fires too often"** — debounce is 200ms. A burst within that window collapses to one refresh; bursts spanning the window produce multiple refreshes (intended — distinct events should each trigger).

**Performance**: zero cost on the placement path. All invalidations + the broadcast run inside the existing `asyncio.create_task` block in the postback handler. The frontend bus pays one extra JSON parse per terminal status.

---

## History — `/admin/history`

Multi-day forensic surface for the three "book of record" datasets — Orders (every order the platform placed), Trades (broker-confirmed fills), Funds (per-account margins ledger). Cap-gated by `view_audit` (designated / admin / risk) for read access; backfill endpoint gated by `admin_guard` (designated / admin only). Read-only; pairs with `/admin/audit` (event-level log) — this is the row-level book view.

**Endpoints** ([backend/api/routes/history.py](backend/api/routes/history.py)):

| Route | Source | Defaults | Pagination |
|---|---|---|---|
| `GET /api/admin/history/orders` | `algo_orders` table | last 30 days | 50/page, cap 500 |
| `GET /api/admin/history/trades` | `daily_book.kind='trades'` | last 30 days | 50/page, cap 500 |
| `GET /api/admin/history/funds`  | `daily_book.kind='funds'`  | last 90 days | unpaged (low cardinality) |

**Common filter params:** `from_date=YYYY-MM-DD`, `to_date=YYYY-MM-DD`, `accounts=ZG0790,DH3747` (comma-list), `symbols=NIFTY,GOLDM` (comma-list — Funds tab ignores this).

**Orders-specific filters:** `status=FILLED|OPEN|REJECTED|CANCELLED|UNFILLED`, `mode=live|paper|sim|shadow|replay`. Response includes a `counts` histogram so the UI's summary chip row can show "20 FILLED · 3 REJECTED · 1 OPEN" without paginating.

**Trades response** includes `summary.total_notional` (Σ qty × avg_cost across the full filtered set, computed in SQL so pagination doesn't degrade accuracy).

**Funds response** includes `earliest_date` — the first day funds capture wrote a row. The UI surfaces this as "Tracking started 23 Jun 2026" so the operator knows how far back the data goes.

**Funds capture** ([backend/api/algo/daily_snapshot.py::_funds_rows](backend/api/algo/daily_snapshot.py)):

- Runs inside the existing 15:35 IST `_task_daily_snapshot`.
- Per account, per segment (equity / commodity), one row per day. Idempotent via the existing `(date, account, kind, symbol)` unique constraint — re-running the same day's snapshot updates the row.
- Column mapping into `daily_book`:
  - `qty`           → `utilised.debits`            (₹ debited today)
  - `avg_cost`      → `available.cash`             (free cash)
  - `ltp`           → `available.opening_balance`  (SOD cash)
  - `day_pnl`       → `utilised.realised_m2m`      (today's realised P&L)
  - `total_pnl`     → `net`                        (segment net worth)
- Special sentinel: `symbol='__seg__'` (the table's unique constraint requires a symbol; funds rows are per-segment, not per-symbol).

**Audit drill on Orders rows:**

- Every Orders row carries an **Audit ↗** column linking to `/admin/audit?request_id=<uuid>`.
- The `request_id` column was added Jun 2026 + auto-populated by `POST /api/orders/ticket` from the middleware's `scope.state.request_id`. Rows placed before this column existed land NULL and the column renders em-dash.
- `/admin/audit` reads `?request_id=…` URL param on mount + widens `since_hours` to 90 days so older rows surface. Manual filter input also available in the audit page's Filter row ("Request id").

**Cashbook Δ on Funds:**

- Funds tab carries a **Δ vs prior** column — day-over-day change in `cash_available` within each `(account, segment)` series.
- Computed server-side (`HistoryController.list_funds` does one O(N) walk; groups by `(account, segment)`, sorts ASC by date, sets `prior_cash = current cash` each step).
- Sign-tinted (green positive / red negative); first row in a series renders em-dash (no prior to compare).

**Backfill (Funds tab):**

- `POST /api/admin/history/funds/backfill` endpoint accepting `{account, from_date, to_date}`. Cap-gated by `admin_guard` (designated / admin only).
- Looks up the broker via the registry; if the adapter doesn't expose `funds_ledger(from_date, to_date)`, returns **501** with operator-facing guidance.
- Broker support today:
  - **Kite (zerodha_kite)** — no programmatic ledger ever (Console download only). Always 501.
  - **Dhan** — wired. `DhanBroker.funds_ledger` probes the SDK for `get_ledger_report` (v2) / `get_funds_ledger` / `ledger_report` (fork variants) and kwarg → positional fallback on TypeError. Aggregates voucher-level entries per `(voucherdate, segment)`; segment mapping in `_DHAN_SEGMENT_MAP` collapses Dhan exchange codes (NSE_EQ / NSE_FNO / BSE_EQ / BSE_FNO / NSE_CURRENCY / BSE_CURRENCY → `equity`, MCX_COMM → `commodity`).
  - **Groww** — adapter wiring still pending; same single-file pattern.
- UI Backfill row (account input + "Pull ledger ↓" button) surfaces success / 501 / error messages inline with sign-tinted status text. Re-running with a wider date range upserts the same dates idempotently.

**Operator workflow** for a Dhan backfill:

1. `/admin/history` → Funds tab
2. Set the date range filter (e.g. From=2025-01-01 To=today)
3. In the Backfill row, type the Dhan account code (e.g. `DH3747`)
4. Click **Pull ledger ↓** → status pill shows `+N rows upserted from dhan ledger`
5. Hit page Refresh → Funds tab now shows historical rows back to the start of the range

**Notes on the Dhan ledger mapping:**

- `cash_available` = end-of-day `runbal` (Dhan's running balance after the last voucher entry that day).
- `opening_balance` = derived as `close_runbal - net_daily_move` (sum of credits − debits). When close_runbal is unknown the first entry's runbal acts as a fallback.
- `debits_today` = Σ debit across all voucher entries that day.
- `realised_m2m` = `credits − debits` (net daily cash flow). **NOT pure mark-to-market** — Dhan's voucher entries include brokerage, STT, exchange charges, etc. The column reads as net daily cash move; operator should not interpret it as P&L attribution.
- Empty range or no entries: endpoint returns `{rows_added: 0, ..., detail: "no ledger entries in range"}` rather than 5xx. Operator can scope down or pick a different account.
- **Backfill OVERWRITES existing rows** on the unique key `(date, account, kind, symbol)`. Re-running with a wider range clobbers any prior backfill numbers in the overlapping window — by design, since the voucher-aggregated ledger is more accurate than the single broker.margins() snapshot the daily cron captures. Operator-side: treat funds rows as read-only; don't hand-edit. If a row looks wrong, investigate the upstream broker statement, not the row.

**Remaining limit:**

- **Cashbook view** as a separate tab (running balance walk that reconciles trade-leg deltas against funds snapshots) — not yet. The Δ column above gives the daily move on cash but doesn't surface the trade-by-trade contribution; a 4th tab could layer that on.

---

## Activity logs — system + order + agent runtime

Multi-mount unified log viewer for all categories (System / Conn / Orders /
Agents / Terminal / News). Accessible via:
- **Navbar Log icon** → ActivityLogModal (full-screen, all tabs)
- **Execution page** → inline Activity card (Orders default)
- **Dashboard activity card** → replaces the legacy MARKET NEWS strip (Jun 2026);
  defaults to **News** tab so the dashboard still lands on market headlines,
  but a click reveals the full Orders / Agents / Terminal / Conn / System paper trail
- **`/activity` page** → bookmarkable, defaults to Orders, full tabs available

All four surfaces share reusable components + filter state. Filters (`account`, `level`)
persist across tab switches.

**Log levels**: All/Error/Warning/Info. Default 'All'. Parsing:
- System/Conn: extract `[ERROR]`, `[WARNING]`, `[INFO]` tokens from message text
- Agents: map `event_type` → level (e.g. agent-fired = info, agent-error = error)
- Orders: no level token (all info by default)

**Multi-column layout** — CSS `column-count: 2` at ≥900px container width
(magazine-style flow), single column below 900px.

**Conn log endpoint** (`GET /api/admin/logs/conn`) — tails `/opt/ramboq/.log/conn_log_file`.
Path resolver prefers absolute `/opt/ramboq` over CWD-relative so dev API (running
from `/opt/ramboq_dev`) accesses the shared prod conn log.

---

## Audit log — `/admin/audit`

Single forensic surface for every mutating event the platform produces. Cap-gated (`view_audit` — designated / admin / risk); writes happen via [`AuditMiddleware`](backend/api/audit.py) (HTTP) + `write_audit_event()` (non-HTTP). All writes are out-of-band via `asyncio.create_task` so **zero latency cost** on the caller's hot path.

**What's captured:**

| Category | Source | Actor |
|---|---|---|
| `order.place` | `POST /api/orders/ticket` | operator (JWT) |
| `order.modify` / `order.cancel` | `PUT/DELETE /api/orders/{id}` | operator |
| `order.fill` / `order.reject` | `POST /api/orders/postback` | `broker` |
| `agent.action` | every agent fire that triggers an action | `agent:<slug>` |
| `user` | `/api/admin/users/*` | operator |
| `config.broker` / `config.grammar` / `config.fragment` / `config.hedge` | `/api/admin/{brokers,grammar,fragments,hedge-proxies}/*` | operator |
| `system.nav` | NAV compute (daily 16:00 IST + ad-hoc) | `system` |
| `system.statement` | monthly statement send (1st of month) | `system` |
| `strategy` / `agent` | `/api/strategies/*`, `/api/agents/*` | operator |

**Filter pills** above the column filters scope the view: **All / Orders / Agents / Users / Config / System**. Each pill maps to one or more categories (comma-separated OR server-side). Combine with column filters (actor / action / target / status / since-hours) for drill-down.

**Capturing 4xx/5xx mutations:** OFF by default. Flip `audit.log_failed_mutations` to ON in `/admin/settings` when you want defect-tracking rows (e.g. "operator clicked SUBMIT and got 422; what blocked?"). Toggle off again afterwards — the audit log balloons with validation noise otherwise.

**Cross-referencing**: every row carries a `request_id` UUID mirrored in the `X-Request-ID` response header. To trace a specific operator action end-to-end: copy the request_id from the audit row, grep `api_log_file` on the server for that ID.

**Retention**: `audit_log` is the forensic UI trail — rows older than 365 days are purged daily at 03:20 IST (tunable via `retention.audit_log_days`). Note this is NOT the SEBI Cat-III financial record; `nav_daily` + `daily_book` are kept forever for that purpose.

---

## Code metrics — `/admin/metrics`

Per-release codebase-health dashboard. Eight metrics captured by
`scripts/capture_metrics.py` and stored in `code_metrics_snapshots`
so you can watch complexity / coverage / duplicated-lines / bug-count
trend across releases.

**Metrics captured per snapshot:**

| Metric | Tool | What it measures |
|---|---|---|
| `backend_complexity_avg/max` | `radon cc -j` | Cyclomatic complexity of every Python function |
| `backend_loc` | `radon raw -j` | Source LOC (excludes comments + blanks) |
| `backend_stale_count` | `vulture --min-confidence 80` | Unused imports/functions/variables |
| `backend_coverage_pct` | `pytest --cov=backend` | Statement coverage (off unless `--with-coverage`) |
| `frontend_loc` | recursive `wc -l` on `.svelte/.js/.ts` | Source LOC, excludes node_modules/.svelte-kit |
| `frontend_complexity_avg/max` | ESLint `complexity` rule | JS/Svelte function complexity |
| `frontend_duplicated_lines` | `jscpd` (via npx) | Cut-and-paste detector |
| `frontend_stale_count` | ESLint `no-unused-vars` | Unused JS/Svelte variables |
| `bug_count_since_last_release` | `git log` heuristic | Commits matching `fix:|fix(|bug:|URGENT|P0` |
| `per_page_latency_ms` | reads `/tmp/ramboq_perf.json` | DCL/Idle/LCP per page from Playwright spec |
| `test_response_times` | `pytest-json-report` + Playwright JSON reporter | Per-test durations: total_tests, total_wall_time_s, median_s, max_s, slow_count, top_10_slowest |

Decoupling (afferent/efferent coupling) is **deferred to Phase 2** —
needs an import-graph build via `pydeps` or a custom AST walker.

**Capture a snapshot manually:**

```bash
ssh ramboq
cd /opt/ramboq
sudo -u www-data ./venv/bin/python scripts/capture_metrics.py \
    --release-tag manual-$(date +%Y-%m-%d) \
    --notes "post-sprint-G capture"
```

Flags:
- `--release-tag` — required. Convention: `v<X.Y.Z>` for release tags,
  `dev-<sha>` for branch deploys, `manual-YYYY-MM-DD` for ad-hoc runs.
- `--force` — overwrite an existing same-tag row in place (rather
  than skipping); keeps the trend chart from accumulating duplicates.
- `--with-coverage` — also run `pytest --cov`. **Slow** (5-30 minutes
  depending on suite); skip unless coverage % is the metric you're
  measuring this run.
- `--with-test-times` — collect per-test execution times. Backend: uses
  `pytest-json-report` (falls back to `--durations=0` text parsing).
  Frontend: reads `/tmp/ramboq_pw_report.json` from a prior Playwright
  JSON run. Deploy pipeline passes this flag automatically (D12 step).
  Produces `test_response_times` column with `max_s`, `total_wall_time_s`,
  `slow_count`, `top_10_slowest` per backend/frontend.
- `--notes "<text>"` — free-text appears on the drill-in modal.

Idempotency: omitting `--force` and re-running with an existing tag
logs + skips (the deploy pipeline relies on this).

**Auto-capture from deploy:** `webhook/deploy.sh` calls the script after
every successful non-frontend-only deploy:
- `main` branch → tag = `git describe --tags --abbrev=0` (e.g. `v2.1.0`)
- dev branches → tag = `dev-<short-sha>`

Best-effort: a failed capture never fails the deploy itself.

**Reading the page:**

Top of `/admin/metrics`: twelve **trend tiles** (small SVG line charts).
Each tile shows the latest value + the historical sparkline + the
range (min → max). Cyan-400 palette consistent with other admin
surfaces. The tiles plot oldest-left, newest-right. The last two
tiles ("Slowest test — backend" and "Backend test wall time") are
populated once `--with-test-times` starts being passed by the deploy
pipeline — they show `—` on earlier snapshots.

Below the tiles: the **snapshots table**, newest-first. Headline
metrics in tabular-nums columns. Click **Detail** on any row to
open the drill-in modal, which shows:
- **Test response times** — backend (pytest) and frontend (Playwright)
  panels with total_tests, wall time, median, slowest, slow_count, and
  a numbered list of the **top 10 slowest tests** so you can identify
  which test accumulated the most time across releases.
- per-page latency JSON (from the Playwright perf spec)
- `raw_payload` — full radon JSON, vulture stdout, jscpd report,
  coverage summary. Cap'd at 1.5 MB; truncated for storage when
  raw output is huge.

**Troubleshooting:**

| Symptom | Cause + fix |
|---|---|
| Trend tile shows `—` for latest value | The tool couldn't run (radon/vulture/jscpd missing). Drill into the snapshot's raw_payload to see the `_skipped` / `_error` tag. Install the tool, re-run capture with `--force`. |
| `per_page_latency_ms` is `{}` | No Playwright perf JSON found. Run `npx playwright test e2e/main_thread_perf.spec.js` first (writes `/tmp/ramboq_perf.json`), then capture. |
| `test_response_times` is `null` | Snapshot was captured without `--with-test-times`. The deploy pipeline now passes the flag automatically; re-run with `--force --with-test-times` to backfill. |
| Frontend test times show `_skipped` | No `/tmp/ramboq_pw_report.json` found. Run Playwright with `--reporter=json` and save to that path, then re-capture. |
| Bug count is 0 | Either no previous tag exists (first release) OR no commit since prev tag matches the heuristic. The script falls back to "last 30 days" when no prev tag exists. |
| `bug_count` looks wrong | The heuristic is intentionally permissive (`fix:|fix(|fix |bug:|URGENT|P0`). Tune for accuracy by editing `_count_bug_commits` in the capture script. |
| Snapshot row missing from table | Forgot `--force`? Re-running with an existing tag silently skips by design. Check the script's stderr for `already exists`. |

---

## Deep dives

- [AGENTS_GUIDE.md](AGENTS_GUIDE.md) — agent authoring + validation ladder
- [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md) — scenarios, Run-in-Simulator, market-state presets
- [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md) — Claude Code MCP, 25 tools, audit trail

## Brand assets — regenerate + optimize

Static images under `frontend/static/` (favicons, app-icons, OG cards, bull logo, navbar background) are sources of truth: bytes ship straight to browsers and social-card scrapers.

Two scripts manage the bundle, both run via the `.icon-venv` interpreter (Pillow + resvg-py installed):

- `scripts/build_app_icons.py` — re-renders every PNG and the ICO from the master `app-icon.svg` + the two `og-image-*.svg` files. PNGs are quantized to a 256-colour palette (Fast Octree) on the way out; favicon.ico carries only 16/32/48 layers (the SVG link covers anything modern). Run this after editing any source SVG.
- `scripts/optimize_images.py` — idempotent sweep over the whole `frontend/static/` directory: re-encodes WebP (bull at q=80, nav_image at q=60, the rest at q=75), strips comments + collapses whitespace inside SVGs, re-slims `favicon.ico`, and re-quantizes any drifted PNGs. Safe to call from a pre-commit hook. Exposed as `npm run optimize:images` (from `frontend/`).

Pre-optimization baseline of the hot-path bundle (every page-load assets) was ~156 KB; post-optimization it lands at ~54 KB (65% smaller). The full static bundle dropped 569 KB → 166 KB (71% smaller). `frontend/e2e/image_perf.spec.js` freezes a 120 KB hot-path budget — exceeding it on any new asset drop fails the spec, forcing a re-run of the optimizer.

## Troubleshooting

| Problem | Fix |
|---|---|
| Agent didn't fire | `/automation` last fire timestamp; also Simulator tab in log |
| Sim shows no price changes | Scripted mode needs `initial` block; try Live+scenario |
| Custom token invisible | Did you press **Reload registry**? Token `is_active=True`? |
| Alerts not reaching Telegram/email | Check `cap_in_<branch>.telegram` and `.mail` in `backend_config.yaml` |
| Settings change didn't take effect | Next agent tick ≤5 min; `logging.*` at Save. Dev branch forces paper anyway. |
| Fired but no broker order | Check `/admin/settings` execution flags; might be `execution.live.<action>=false` |
| Day P&L looks wrong | Verify against broker. Uses decomposed formula (see CLAUDE.md). MCX: ensure multiplier applied. |
| "Invalid username or password" | Admin resets password (no forgot-password flow yet) |

---

## Glossary

- **Branch**: `main` = prod, other = dev. Agents/tokens/sims on dev don't affect prod.
- **Capability flag**: `cap_in_<branch>.<feature>` in `backend_config.yaml` (simulator/telegram/mail/genai/market_feed).
- **Dispatch registry**: In-memory token → impl map, rebuilt at startup + **Reload registry**.
- **Execution mode**: Sim / paper / live, decided per agent fire. Sim = fabricated quotes + paper trade engine (dev only). Paper = real quotes + paper trade engine, validated by Kite's `basket_margin`. Live = real broker order. Per-action flags in **Settings** decide paper-vs-live on prod.
- **Masked account**: Accounts are rendered as `ZG####` / `ZJ####` in the UI and in alerts to avoid leaking numeric IDs. Internally the real IDs (`ZG0790`, `ZJ6294`) are used.
- **Tick**: One step of the simulator. Each tick applies a set of price moves and then invokes the agent engine once.
