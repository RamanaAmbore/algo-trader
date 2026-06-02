# RamboQuant Admin Guide

> **First time here?** Read [USER_GUIDE.md](USER_GUIDE.md) first — it explains the *concepts* (what an agent is, why we have a simulator, what the chase engine does) in plain English. This file is the *operations reference*: exact button labels, JSON shapes, API endpoints, config keys. Read USER_GUIDE first; then come back here when you need step-by-step instructions on a specific task.

A walkthrough of the four things an admin cares about on this site:

1. **Agents** — rules that watch the portfolio and fire when something worth knowing happens.
2. **Tokens** — the vocabulary agents are written in.
3. **The Simulator** — lets you safely test what an agent would do under a hypothetical market.
4. **Settings** — runtime-editable thresholds and toggles you tune from the UI.

No code reading required. If you can use a dropdown and a form, you can use all of this.

> **TL;DR for the impatient:** want to see a loss-threshold rule auto-close your positions safely before you turn it on?
> 1. `/agents` → find `loss-pos-total-auto-close` → expand it. Condition is `pnl ≤ −₹50k on positions.total`; action is `chase_close_positions`; ships **inactive**.
> 2. Click **Run in Simulator** on that row. The sim synthesises a scenario that trips exactly this condition, auto-loads your live book, and dry-fires the agent.
> 3. Watch the bottom **Log** panel. The **Simulator** tab streams per-symbol price ticks; the **Order** tab shows the paper-traded `SELL N <symbol> @₹price · acct=…` lines for each position the action would have closed, one per position, all tagged `SIM`.
> 4. When you're confident the thresholds and actions match your risk tolerance, flip the agent **ON** from the row — it will do the real thing next time conditions fire.

---

## The four words

Everything on the admin site revolves around four ideas:

| Word | Plain English |
|---|---|
| **Agent** | A rule — "if X happens, tell me (and maybe do something)". Lives as a row on the **Agents** page. |
| **Alert** | The moment an agent's rule became true. Sent through Telegram, email, and the live log. |
| **Notify** | *How* an alert reaches you — telegram / email / browser / log. |
| **Action** | *What* the system does in response — e.g. place an order, close a position, deactivate a flag. |

Think of an agent as a sentence: **"When _condition_ is true, _notify_ me through these channels and _do_ these actions."**

### When agents re-fire (latching semantic)

To avoid spamming you when a drawdown persists, agents follow a two-tier rule:

- **Static agents** (absolute ₹ floor or % floor — e.g. "pnl ≤ −₹30k", "day loss ≥ 3%") fire **once** the first time the threshold is crossed, then **latch silent** as long as the condition keeps matching. They re-arm automatically when the value recovers above the threshold. This means a loss that sits flat at −₹40k won't fire the static agent every tick — you get one notification at the crossing, not a stream.
- **Rate-of-change agents** ("pnl bleeding ≥ ₹3k/min") keep re-firing while the bleed is accelerating, gated by cooldown (default 30 min) and a material-change threshold (default ₹15k move between fires). The whole point of these agents is to wake you up when the situation is getting materially worse.
- **Session rollover** (new trading day, detected automatically) clears every latch.

Put together: the first alert when things go bad is the static agent. After that, it's rate agents telling you if it's getting worse faster. When things recover, everything re-arms silently.

---

## How agents work — end to end

Here's what happens every 5 minutes during market hours:

```
Kite broker  ──►  holdings / positions / margins  ──►  summarise per account + total
                                                        │
                                                        ▼
                         ┌──────────────── run_cycle ────────────────┐
                         │  for each ACTIVE agent:                    │
                         │    1. is the market open?                  │
                         │    2. is the cooldown finished?            │
                         │    3. does the condition match?            │
                         │    4. has something material changed since │
                         │       the last alert?                      │
                         │    5. if yes: send alerts + run actions    │
                         └────────────────────┬───────────────────────┘
                                              ▼
           Telegram + email + browser live log + agent-events row in the DB
                                              │
                                              ▼
                       (if the agent has actions:  place_order / chase_close / …)
```

Each of those numbered gates exists to keep noise down. Cooldowns prevent spam. The baseline gate prevents opening-bell panic alerts. The suppression gate prevents re-alerting when the loss hasn't changed much.

---

## Anatomy of an agent

Every row on the **Agents** page (`/agents`) has four moving parts:

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

A list like:

```json
[
  { "channel": "telegram", "enabled": true },
  { "channel": "email",    "enabled": true },
  { "channel": "log",      "enabled": true }
]
```

You can mix and match `telegram` / `email` / `websocket` / `log`. Disable one by flipping `enabled` to `false`.

### 3. Actions

Empty list `[]` means "alert only — don't do anything." Otherwise the list describes what to *do* when the alert fires:

```json
[
  { "type": "chase_close_positions",
    "params": { "account": "ZG0790", "exchange": "NFO" } }
]
```

The action shapes (place order, modify, cancel, close, log, deactivate, etc.) all come from the **Tokens** page — more on that next.

### 4. Metadata

- **Scope** — `total` or `per_account` (display grouping)
- **Schedule** — `market_hours` (default) or `always`
- **Cooldown (minutes)** — minimum time between two fires of this agent. Default 30.
- **Status** — `active` / `inactive` / `cooldown`. Toggle with the ON/OFF button on the agent row.

---

## Tokens — the vocabulary agents are written in

You can't invent words out of thin air when writing a condition. Every `metric`, `scope`, `op`, `channel`, and `action type` must be a **registered token**. The Tokens page (`/admin/tokens`) is where those words live.

### Three categories

The Tokens page has three tabs — three *kinds* of tokens, each playing a different role:

| Category | Tokens inside | Answers the question… |
|---|---|---|
| **Condition** | `metric`, `scope`, `operator` | *What can agents check?* |
| **Notify**    | `channel`, `format`, `template` | *How can agents alert?* |
| **Action**    | `action_type` | *What can agents do?* |

### System tokens vs custom tokens

- **System** tokens ship with the application. They have the orange "system" badge. You can **enable/disable** them (is_active toggle) but you cannot rename or delete them. Built-in metrics like `pnl`, `day_pct`, `cash`, built-in scopes like `holdings.any_acct`, `positions.total`, and built-in action types like `place_order`, `chase_close_positions` all live here.
- **Custom** tokens are ones you create on this page. Full CRUD — you can edit, delete, and change anything about them.

### What one token row looks like

Each row carries more than just a name. The fields you'll see when editing:

| Field | Meaning |
|---|---|
| **Category** | `condition`, `notify`, or `action` |
| **Token kind** | Sub-type. For condition: `metric` / `scope` / `operator`. For notify: `channel` / `format` / `template`. For action: `action_type`. |
| **Token** | The word itself — what authors type into an agent (e.g. `pnl`, `<=`, `telegram`). Must be unique within its (category, kind). |
| **Value type** | What kind of value this token produces or accepts — `number`, `string`, `boolean`, `enum`, `array`, `object`, `void`. |
| **Units** | Human-readable label for numeric metrics: `₹`, `%`, `₹/min`, `%/min`, `min`. |
| **Description** | One line explaining what it means. Shows in the Agents-page editor as tooltip-like help. |
| **Resolver** | Python dotted path to the function that implements the token. Required for system-integrated tokens; omit for simple enum/string tokens that the engine doesn't need to call into. |
| **Params schema** | For action tokens — what arguments the action expects (account, symbol, quantity, side, …). JSON schema. |
| **Enum values** | For enum value types — the allowed strings. |
| **Template body** | For notify template tokens — the message body with `${placeholder}` syntax. |

### Creating a token

1. Go to `/admin/tokens` → click **+ New token** (emerald button, top right).
2. Pick a **Category** (condition / notify / action).
3. Pick a **Token kind** — the form adapts. A condition `metric` needs a `resolver` (Python function that returns a number for a given row); a `scope` needs a resolver that returns a list of rows; an `operator` is usually a built-in comparator.
4. Fill in the token string, description, value type, optional units.
5. For action tokens, define the `params_schema` (JSON) — this is what the Agents page uses to render the action's sub-form.
6. Save → the row appears in the list.
7. **Click "Reload registry"** (yellow button, top right). Tokens are loaded into an in-memory dispatch table at startup; the reload button rebuilds that table so your new token is usable *without* restarting the server.

That's it. The new token is now a word agents can use.

### How tokens help agent creation

Every dropdown in the Agents editor is populated from the Tokens table:

- The condition editor autocompletes metric/scope/operator names from whatever's in the catalog.
- The "validate" button (on the Agents editor) checks every token referenced in your condition against the live registry — typos fail here, not at runtime.
- The actions editor renders its form using the action token's `params_schema`, so you only see fields relevant to the action you picked.

In short: **the Tokens page is the one place you extend the system.** Adding a new kind of check or a new kind of action is "one token row + one Python function," no deploy, no code change in the engine.

---

## Creating an agent — step by step

Open `/agents`. Agents are grouped by category (Loss & Risk, Summaries, Automation, Other) so existing ones are easy to find.

To create or edit:

1. Click a row to expand it and read the current condition tree in plain English.
2. Click **Edit** on the row. The inline form appears:
   - **Name** / **Description** — human-readable
   - **Scope** — `total` / `per_account`
   - **Schedule** — `market_hours` / `always`
   - **Cooldown (minutes)** — 30 is a reasonable default
   - **Conditions (JSON)** — the condition tree (see above)
   - **Events (JSON)** — notify channels list
   - **Actions (JSON)** — action list, or `[]` for alert-only
3. **Click Validate** (cyan button). The server dry-checks your condition tree against the live token registry and returns any unknown tokens. You'll get a green banner or an error list.
4. Click **Save** once validation passes.

A freshly-saved agent is `inactive` by default. Click the OFF pill to flip it to ON. It will start evaluating on the next tick.

### Copy-an-existing pattern (recommended)

The easiest way to build your first custom agent is to click into any `loss-*` row, look at its condition tree, and clone-edit a new agent with slightly different thresholds. The 14 seeded loss agents cover static `%`, static `₹`, rate `₹/min`, and rate `%/min` variants at both per-account and total scope — between them they exercise almost every condition-grammar feature.

---

## The Simulator — try it before you trust it

The Simulator (`/admin/execution?mode=sim`) answers the question:

> "If the market did X, what would my agents do?"

It feeds fabricated holdings / positions / margins into the **same** agent engine the real pipeline uses. Alerts, Telegram messages, email sends, paper-traded orders — all of them fire, but every one is tagged `SIMULATOR` so nobody confuses them with real alerts.

### What you see on the page

- **Status bar** — `RUNNING` / `idle`, active scenario, seed mode, tick number.
- **Controls row** — scenario dropdown, seed mode, rate, plus buttons:
  - **Load live book** — snapshots your real Kite positions into the sim.
  - **Start / Stop / Step** — run continuously, halt, or apply one tick manually.
  - **Run cycle** — run the agent engine against the current sim state right now.
  - **Clear sim** — delete every past SIMULATOR event and order row from the DB.
- **Recent SIMULATOR agent events** — table of fires since the last Clear.
- **Recent SIMULATOR orders** — paper-traded orders from sim actions.
- **Log panel** (shared across the admin pages) — the **Simulator** tab streams one line per tick with per-symbol price diffs in real time.

### Simulated market clock

Time-aware agents (rate rules with a baseline gate, anything that checks `minutes_until_close`, expiry-day auto-close rules) need a realistic clock. The simulator provides one via **market-state presets**:

| Preset | What it simulates |
|---|---|
| `pre_open`     | Before the session (no segment flags open) |
| `at_open`      | Market just opened (1 min into session) |
| `mid_session`  | Default — 3 hours into the session |
| `pre_close`    | 6 hours in, close not yet passed |
| `at_close`     | Just after equity close, MCX still running |
| `post_close`   | Both segments done |
| `expiry_day`   | Mid-session on an expiry day — flips `is_expiry_day=true` so expiry agents engage |

Three places to set it (most specific wins): **Market dropdown** on the Simulator page → **scenario YAML** (`market_state: {preset: pre_close}`) → default `mid_session`. The Run-in-Simulator button on the Agents page picks a sensible preset from the agent's metric (`expiry_day` for expiry-slug agents, `mid_session` otherwise) so per-agent tests just work.

The running sim's status bar shows `market: <preset>` next to the tick counter.

### Tick cadence — positions only

The simulator is **positions-only**. Holdings aren't simulated at all because intraday risk lives in F&O positions + fund negatives; holdings-based agents (`day_pct`, `day_rate_abs`, `day_rate_pct`) validate against live production data only. If you press **Run in Simulator** on a holdings agent, you'll get a clear error telling you so.

Positions refresh every tick by default (cadence = 1). Three places to override, most specific wins:

1. **Pos / N** input on the Simulator page — blank = use scenario / DB default.
2. **Scenario YAML** — top-level `positions_every_n_ticks: <N>`.
3. **DB setting** — `simulator.positions_every_n_ticks` on `/admin/settings`.

Margin patches via `set_margin` are cadence-independent — they fire on whatever tick the scenario schedules them on. Tick 0 always refreshes (matches how a live session feels at market open).

### Three seeding modes

Scenarios decide what moves; seeding decides what moves them.

| Mode | Starting state | Use it when… |
|---|---|---|
| **Scripted** | The scenario's built-in `initial` block (fake accounts + symbols) | Deterministic regression test — same inputs every run |
| **Live** | Snapshot of your real Kite positions | You want to see what your *actual* book would look like after a hypothetical move |
| **Live + scenario** | Live snapshot, then the scenario's scripted extras layered on top | You want a real-book stress test (e.g. `generic-crash` scenario with your real positions) |

For Live / Live+scenario: you can press **Load live book** to snapshot now, or just pick Live / Live+scenario and hit **Start** — the driver auto-snapshots if you haven't already. Load live book manually when you want a *fresh* snapshot right before starting (e.g. after placing real orders).

### Running it

1. Go to `/admin/execution?mode=sim`.
2. Pick a scenario. All five (`generic-crash`, `generic-euphoria`, `extreme-crash`, `extreme-euphoria`, `random-walk`) are positions-only and work with Live or Live+scenario. Start with a generic one; move to extreme variants when you want to trip every threshold at once.
3. Pick seed mode and (optionally) press **Load live book** — the driver auto-snapshots if you pick Live / Live+scenario and haven't already.
4. Set **Rate (ms)** — how fast to advance ticks. 2000 ms = 1 tick every 2 seconds is fine.
5. Press **Start**.
6. Switch to the **Simulator** tab at the bottom. Every tick shows up with per-symbol price moves:

   ```
   [12:34:56] SIMULATOR tick 3 · generic-crash
     holdings.ZG####.RELIANCE:  4822.03→4725.59 (Δ -96.44)
     holdings.ZG####.ACMESOLAR: 298.90→292.92   (Δ -5.98)
     ...
   ```

7. Sim auto-stops at 30 minutes. Stop early with the red **Stop** button.

### Testing one specific agent

On the `/agents` page, every row has a **Run in Simulator** button. Click it:

- Navigates to `/admin/simulator?agent_id=<id>`
- Pre-arms the page to run *only that agent*, bypassing schedule / cooldown / baseline gates
- Pick a scenario and Start → the chosen agent fires as often as its condition is true, without affecting any real state

This is the safest way to test a new agent before activating it.

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

## Paper trading dashboard (`/admin/execution?mode=paper`)

`/admin/execution?mode=paper` is the visual surface for what's actually happening in **mode 2** on prod — real Kite quotes feeding the paper trade engine. Same layout as the simulator page, but reading from the live engine instead of a sim driver.

You'll see:

- **Status banner** at the top:
  - Green/sky **`CHASING`** when paper orders are in flight on main.
  - Amber **`IDLE`** when the engine is enabled but no chase is running.
  - Grey **`DEV`** when you're on a non-main branch (engine exists but no background tick — paper trading is prod-only by design).
- **Chase pills** — one per in-flight paper order, showing side / qty / symbol / current limit / attempt count. Same shape as the simulator page so it reads as a sibling surface.
- **Chart grid** — one mini chart per symbol with captured ticks. Underlyings come first (sky-blue `SPOT` tag), derivatives below grouped by underlying with the spot overlaid as a dashed line.
- **LogPanel** at the bottom — canonical 6-tab strip (Orders · Agents · Terminal · Ticks · System · News) inherited from LogPanel's default. The Orders tab shows recent `mode='paper'` `AlgoOrder` rows; charts render inline in the page above (the standalone Chart tab inside LogPanel was retired since the page already mounts the chart grid).

**When the page is most useful**: during the soak phase, after you've toggled into PAPER mode from the navbar and want to watch the chase against the live market without any order touching the broker. Every paper fire shows up here, and you can compare it to what the agent's `[PAPER]` Telegram alert said would happen. The seeded default on a fresh install is LIVE — flip to PAPER from the navbar (or `/admin/execution`) when you want to soak-test, then flip back to LIVE when you're ready for the real broker to take orders.

---

## Options analytics (`/admin/options`)

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
- The `/agents` page's event table auto-switches to showing only sim events.
- The `/performance` page keeps showing **real** data — the live Kite refresh continues even during a sim, only the live agent engine is paused.

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

## Recipes

**A) "Add a custom loss rule for one specific account"**
1. Go to `/agents` → Copy the condition from `loss-pos-acct-static-abs`.
2. Edit a new agent slug like `loss-zg0790-positions`.
3. In the condition, replace `"scope": "positions.any_acct"` with a scope that matches only the account you care about (e.g. a new custom scope token you create on the Tokens page).
4. Validate → Save → **Run in Simulator** on the agent row → confirm it fires (the synthesizer builds a scenario targeting this specific agent's condition; no need to pick one manually).
5. Flip it to ON.

**B) "Add a new metric the engine doesn't have yet"**
1. Write a Python function somewhere under `backend/api/algo/` that takes `(ctx, row)` and returns a number.
2. Go to `/admin/tokens` → **+ New token** → Category `condition`, kind `metric`, resolver set to the dotted path of your function.
3. Save → Reload registry.
4. Your metric is now usable in any agent's condition leaf.

**C) "Find out what would happen if the market drops 6% right now"**
1. `/admin/execution?mode=sim` → Scenario `generic-crash` → Load live book → Seed `Live + scenario` → Start.
2. Watch the Simulator log tab for price moves; watch the events table for which agents would fire.
3. Stop when you've seen enough. Hit **Clear sim** to wipe the test rows.

**D) "Auto-close on severe loss — but safely"**
1. `/agents` → `loss-pos-total-auto-close`. Ships inactive with a `chase_close_positions` action on `pnl ≤ −₹50k`.
2. Open the row → "Actions" section shows the action type + params (scope=total, timeout 10 min, adjust_pct 0.1).
3. Click **Run in Simulator** — the synthesizer targets this agent's condition; the sim seeds your live book automatically.
4. Flip the bottom panel to the **Order** tab. You'll see one `SIM ◆ SELL N <symbol> @₹price · acct=…` line per open position the chase engine would try to close.
5. Flip to **Simulator** tab — the same lines appear interleaved with price ticks, so you can see the sequence: price move → threshold cross → orders placed.
6. When the output matches your risk tolerance, flip the agent **ON**. Next time conditions fire for real, the chase engine runs against the broker.

**E) "Add a close-position action to an existing loss agent"**
1. `/agents` → pick a loss agent → **Edit**.
2. Next to the **Actions (JSON)** textarea, click the `+ close_position` pill — a skeleton action lands in the textarea.
3. Tune the params: set `account`, `symbol`, `quantity`. (For scope-level auto-close on any matching position, use `+ chase_close` instead — it doesn't need a specific symbol.)
4. **Validate** → **Save** → **Run in Simulator** to confirm → flip **ON**.

**F) "Tune a threshold without redeploying"**
1. `/admin/settings` → type the keyword in the filter box (e.g. `cooldown`, `rate`, `baseline`).
2. Click the **(i)** chip on the row to read what the setting does, its default, and its valid range.
3. Edit the value → **Save**. The next agent tick (within 5 min) picks up the new value.
4. If you change your mind, click **Reset** to restore the code default.

**G) "Flip the pipeline from paper to live"**
1. Soak in PAPER first — pick PAPER from the navbar dropdown and watch the chase loop on `/admin/execution?mode=paper`. Every fire writes a `mode='paper'` `AlgoOrder` row with Kite's `basket_margin` verdict in `.detail`.
2. When the fires look right, pick LIVE from the navbar dropdown. The page banner flips from green to red.
3. The next agent fire hits the real broker. Telegram alerts drop their `[PAPER]` tag, signaling the action went live.
4. Inspect `/orders` for the new `mode='live'` `AlgoOrder` row.
5. If something looks wrong, flip the navbar dropdown back to PAPER. The next tick reverts every action to paper.

---

## Safety checklist before flipping a new agent to ON

- [ ] Description explains what it does and why.
- [ ] Condition tree passes **Validate** on the Agents editor.
- [ ] Cooldown is at least a few minutes (prevents spam).
- [ ] Schedule is `market_hours` unless you actually want it firing overnight.
- [ ] Ran in the Simulator with a representative scenario and got the expected fires.
- [ ] If the agent has **actions** (not just alerts): the action's params are correct and the action is safe to run against your book.

---

## Brokers — managing accounts from the UI (`/admin/brokers`)

The broker accounts (Kite or future-other-vendor) are managed from `/admin/brokers` — no SSH, no editing `secrets.yaml`, no service restart.

### What's on the page

- **Account table** — one row per account with code / broker / API key / source IP / status pill / notes / Test / Edit / Delete.
  - **Status pill**:
    - **LOADED** (green) — the account is in the live `Connections` map and ready to take broker calls
    - **PENDING** (amber) — the row exists in the DB but `Connections` hasn't picked it up yet (most common right after a save; the next 15 s poll will refresh)
    - **DISABLED** (grey) — `is_active = false`; the row exists but the engine ignores it
- **+ New account** button — opens the form below in Create mode.
- **Edit / Delete** per row — Edit puts the form in update mode (secret fields default to blank, meaning "leave unchanged"); Delete is irreversible after a confirm.
- **Test** per row — hits `broker.profile()` to verify the credential chain authenticates. ✓ next to the button on success (hover for the authenticated user name); ✗ on failure (hover for the broker's error).

### Adding a new account

1. Click **+ New account** → fill in:
   - **Account code** — your internal identifier (e.g. `ZG0790`). Must be unique.
   - **Broker** — defaults to `kite`; future vendors plug in here.
   - **API key** — Kite app's API key. Plaintext (not credential-grade alone).
   - **API secret / Password / TOTP seed** — the three secrets. Encrypted at rest.
   - **Source IP** (optional) — IPv6 address to bind outgoing Kite traffic to. Only needed for multi-account setups (Kite enforces one IP per app).
   - **Notes** (optional) — anything you want to remember about this account.
2. Click **Create**. The Connections singleton reloads immediately. Click **Test** on the row — you should see ✓ within a couple of seconds.

### Editing

- Click **Edit** → form populates with current metadata. The secret fields are deliberately blank with a `(blank = unchanged)` hint.
- Type a new value into any field you want to rotate; leave the rest blank.
- Click **Save changes** → reload + Test as above.

### Where the encryption lives

Secrets are Fernet-encrypted at rest with a key derived from `cookie_secret` (the JWT signing secret) via HKDF. Practical implications:

- The API never returns decrypted secrets. The page never reads them back.
- If you rotate `cookie_secret` on the server, every existing `broker_accounts` row will fail to decrypt — you'll have to re-add the accounts. (Don't rotate cookie_secret without a plan.)
- The `secrets.yaml::kite_accounts` block stays as a recovery backup. On first deploy of this feature with an empty DB, the YAML rows are auto-seeded into the table once. After that, the table is the source of truth and the YAML is just there as a fallback if you ever need to reset.

### When the table is empty

On a fresh server with no DB rows AND no `secrets.yaml::kite_accounts`, every broker call will fail until you add at least one account. The page surfaces this clearly: empty list + "Use **+ New account** to add one" prompt.

---

## Reusable order ticket

The `<OrderTicket>` modal in [`frontend/src/lib/order/`](frontend/src/lib/order/) is the single place every order op (open / close / modify / repeat / cancel) goes through, across every instrument (EQ / FUT / OPT). The ticket auto-detects the instrument kind from the symbol and only shows fields that apply (e.g. CNC/MIS for cash equities, NRML/MIS for F&O; trigger field only for SL / SL-M).

Three submit modes:

- **DRAFT** — appends to the caller's local drafts array. No API hit. Used today by `/admin/options` chain `+CE` / `+PE` clicks.
- **PAPER** — `POST /api/orders/ticket` with `mode=paper`. Creates an `AlgoOrder` row + registers with the prod paper engine; the chase loop fills it from real bid/ask via `LiveQuoteSource`. Same lifecycle agent fires use.
- **LIVE** — same endpoint with `mode=live`. Two backend gates:
  1. Branch must be `main` (non-prod returns 403).
  2. `execution.paper_trading_mode` must be `False` (set from the navbar mode dropdown).
  Then `kite.place_order()` with the operator's payload, tagged `ramboq-ticket`. UI fires a `window.confirm()` with the exact order line before any broker call.

Today the ticket opens from `/admin/options` chain clicks and from the **page-header Order icon** (amber `+` glyph) present on every algo page — alongside the **Chart icon** (cyan line glyph) and **Activity icon** (violet 3-line glyph). The three icons open canonical modals at a consistent viewport position (.canonical-modal-overlay/-panel) so muscle memory carries across pages. Symbol anchors are auto-resolved to the tradeable contract before the modals open — `NIFTY 50` → `NIFTY26JUNFUT`, `CRUDEOIL` → `CRUDEOILM26JUNFUT`. Future migration of click-targets: `/orders` row Edit / Cancel / Repeat, `/agents` fire-confirm, `/performance` row "Square off" / "Sell", `/console` `place …` command.

---

## Demo mode — public algo console for visitors

Anonymous visitors on the prod (`ramboq.com`) site can browse the algo pages without a login. The demo session sees the operator's **real broker data with accounts masked** (`ZG####` / `ZJ####`), can place paper orders that go through the real chase loop (against real bid/ask), but never touches a broker. Zero fixture maintenance — no synthetic fixture file, no nightly cron, no data drift.

**How a visitor reaches it**: the public navbar's "Algo Site" cross-link is now visible to everyone (renamed to "Algo Demo" when not logged in). Clicking lands on `/dashboard` → demo mode kicks in.

**Backend chokepoint** ([`backend/api/auth_guard.py`](backend/api/auth_guard.py)):

- `is_demo_request(connection)` returns true when on `main` branch + no admin JWT.
- `auth_or_demo_guard` admits anonymous requests on prod, sets `connection.state.is_demo = True`. On dev, falls through to strict `jwt_guard` (devs are expected to log in).
- Every order-write endpoint checks the flag: `place` / `modify` / `cancel` return 403; `/api/orders/ticket` silently downgrades `mode=live → paper` for demo so the click-Submit flow keeps working.

**Read-path data**: demo sees **real broker data with accounts masked** (same masking the public `/performance` page applies). `mask_column()` turns `ZG0790` into `ZG####`. The shared rule across `/api/positions`, `/api/holdings`, `/api/funds`, `/api/orders/`, `/api/orders/algo/recent`: any non-admin caller gets masked accounts.

**Frontend** ([`(algo)/+layout.svelte`](frontend/src/routes/(algo)/+layout.svelte)):

- Anonymous visitor on prod no longer redirects to `/signin`. The layout polls `/api/charts/paper-status` for the `branch` flag and treats `branch=main` + `!user` as demo.
- Settings / Brokers / Users nav links flag `adminOnly: true` and drop out of the navbar in demo mode.
- "Sign In" button replaces the user pill so the operator has a one-click upgrade.

**Mode badges** in the navbar: `DEMO` (purple, anonymous prod), `PAPER` (blue, paper engine has open orders), `SIM` (red, dev only).

**Maintenance**: zero. Demo shares the operator's real broker book — there's no fixture file to refresh, no data drift, no nightly cron. If the operator is busy trading, demo looks busy too. If the operator is idle, demo is idle. The only thing isolating them is the account mask + the write gates.

---

## Where to learn more — deep dives

The day-to-day operator finds it here. For extended walkthroughs:

- **[AGENTS_GUIDE.md](AGENTS_GUIDE.md)** — agent authoring + the four-stage validation ladder (validate → dry-run → simulator → activate). Every metric / scope / op, fragments, lifespan, troubleshooting.
- **[SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md)** — Lab page workflow. Scenarios, Run-in-Simulator, custom positions, iteration mode, market-state presets.
- **[LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md)** — Claude Code MCP integration. The 24 tools, confirm-token gate, audit trail.

---

## Where to go when something looks wrong

| Problem | Look here |
|---|---|
| Agent didn't fire when it should have | `/agents` → Last fire timestamp + Count on the row; also the Simulator tab in the log panel |
| Sim shows ticks but no price changes | You probably picked a scenario with no scripted initial in Scripted mode — switch to Live+scenario and Load live book first |
| Custom token won't appear in the Agents editor | Did you press **Reload registry** after saving? Is the token's `is_active` on? |
| Alerts not reaching Telegram/email | Check `cap_in_<branch>.telegram` and `cap_in_<branch>.mail` in `backend_config.yaml`; dev may have these off by default |
| Settings change didn't take effect | Most apply on the next agent tick (≤ 5 min). `logging.*` apply at Save. If you flipped an `execution.live.*` flag on dev, note that the branch gate forces every action back to paper regardless. |
| Live agent fired but no real broker order | Check `/admin/settings` → **execution** section. If the relevant `execution.live.<action>` is `false`, the action wrote a paper `AlgoOrder` row instead. Telegram subject would have shown `[PAPER]`. |
| "Invalid username or password" on sign-in | Ask the server admin to reset your password — there's no forgot-password flow yet |

---

## Glossary

- **Branch**: `main` = production (`ramboq.com`), everything else = dev (`dev.ramboq.com`). Agents, tokens, and sims on dev don't affect production.
- **Capability flag**: `cap_in_dev.<feature>` / `cap_in_prod.<feature>` in `backend_config.yaml`. Toggles whether a capability (simulator, telegram, mail, genai, market_feed) is live on that branch.
- **Dispatch registry**: The in-memory index that maps each token name to its implementation. Rebuilt from the Tokens table at startup and on **Reload registry**.
- **Execution mode**: Sim / paper / live, decided per agent fire. Sim = fabricated quotes + paper trade engine (dev only). Paper = real quotes + paper trade engine, validated by Kite's `basket_margin`. Live = real broker order. Per-action flags in **Settings** decide paper-vs-live on prod.
- **Masked account**: Accounts are rendered as `ZG####` / `ZJ####` in the UI and in alerts to avoid leaking numeric IDs. Internally the real IDs (`ZG0790`, `ZJ6294`) are used.
- **Tick**: One step of the simulator. Each tick applies a set of price moves and then invokes the agent engine once.
