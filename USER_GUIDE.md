# RamboQuant User Guide

Plain-language walkthrough of how RamboQuant works, written for someone who just got admin access and isn't a software engineer or quant. If a sentence ever sounds like jargon, that's a bug — please report it.

> **What this guide is vs [ADMIN_GUIDE.md](ADMIN_GUIDE.md):**
> - **USER_GUIDE** (this file) — explains *concepts*: what an agent is, what the chase loop does, why we have a simulator, what Greeks mean. Aimed at someone learning the platform for the first time.
> - **ADMIN_GUIDE** — operational reference: exact buttons / forms / API endpoints / config files. Aimed at someone running the system day-to-day. Read this first; reach for ADMIN_GUIDE when you need step-by-step instructions on a specific task.

---

## The mental model in 60 seconds

RamboQuant is a **rule-based assistant for an Indian options trader**. You define rules ("if my NIFTY positions lose more than ₹50,000 in a day, sell them"); the platform watches the live market, fires the rules when conditions are met, and (if you've authorised it) places the trades. It logs everything, it sends Telegram + email alerts, and it has multiple safety nets so a misconfigured rule can't destroy your book.

The four words that come up everywhere:

| Word | Plain meaning |
|---|---|
| **Agent** | A rule. "If X, do Y." Lives as a row on the **Agents** page. |
| **Alert** | The moment an agent's rule triggered. Goes to your Telegram + email + the live log. |
| **Action** | What the agent does in response to firing — place an order, close a position, send a notification, etc. |
| **Notify** | The delivery channel for the alert (Telegram / email / browser). Independent from Action. |

Read every agent as a sentence: *"When **condition** is true, **notify** me through these channels and **do** these actions."*

---

## The big picture — execution modes

Whenever an agent fires an action that wants to hit your broker, the platform has to decide: is this real, fake, or in-between? It has five modes; you'll use one at a time:

### Simulator (testing)
Fabricated price moves ("NIFTY drops 3%"). Broker never touched. Good for: *"if my book saw this, would my agent fire?"*

### Paper trade
Real Kite quotes + fake order book. Agents see real prices, orders go to paper ledger only. Kite's `basket_margin` validates order (but doesn't execute).

### Live (production trading)
Real broker orders. Seeded default on fresh install. Flip to PAPER from navbar to soak-test. Dev branches ignore this setting — live only on prod.

### Shadow (audit mode)
Logs exact Kite payload + margin validation without executing. Final sanity check before trusting live.

### Replay (historical analysis)
Backtest against pre-loaded historical candles (past market moves).

---

**The single switch on prod**: Pick a mode from the navbar dropdown (SIM · PAPER · LIVE · SHADOW · REPLAY) or visit `/admin/execution`. The page banner reads "LIVE mode" (red — default), "PAPER mode" (green), or the equivalent for sim / shadow / replay. Every alert gets a tag in its Telegram subject:
- (no tag) — every action ran live
- `[PAPER]` — every action was paper

---

## Agents — the rules layer

Open `/agents`. Every row is a rule. Click to expand and read it in plain English.

### Anatomy of a rule

Four parts:

1. **Condition** — what to check. Example in operator-friendly form: *"any account's positions lose ≥ ₹30,000."*
2. **Notify** — where to alert (Telegram / email / browser).
3. **Actions** — what to do (often empty; the alert *is* the response).
4. **Metadata** — when this should run (market hours / always), how long to wait between two fires (cooldown).

### When an agent re-fires

To stop you from getting 100 emails when you've crossed a threshold once:

- **Static thresholds** ("pnl ≤ −₹50k") fire **once** when crossed, then go quiet. They re-arm when you recover above the threshold.
- **Rate thresholds** ("losing ≥ ₹3k/min") keep alerting while the bleed is *accelerating*. Bounded by cooldown (default 30 min) and a "must have moved meaningfully since last fire" gate.
- **Session rollover** (new trading day) resets all latches.

So the first email when things go bad is the static agent. Subsequent emails are rate agents telling you it's getting worse faster.

### Built-in agents

The platform ships with 14 loss / risk agents pre-seeded and active:

| Slug pattern | What it watches |
|---|---|
| `loss-pos-*` | Position P&L (intraday F&O exposure) |
| `loss-hold-*` | Holdings P&L (long-term equity) |
| `loss-funds-cash-negative` | Cash balance below zero |
| `loss-funds-margin-negative` | Available margin below zero |

Edit them from `/agents` — change a threshold, add Telegram-only notification, or attach a `chase_close_positions` action that auto-cuts losses. The conditions are JSON trees you can read like a sentence.

---

## Order templates — per-position exit rules

Think of a template as "my standard exit playbook for selling options: auto-place a TP exit at +0.50% and an SL at −1%." When you place an order, you pick a template from the OrderTicket dropdown. Once the order fills, the platform automatically places the exit orders on the broker.

Every exit rule (TP / SL / scaled close / trailing stop / hedge wing) is independent. You can mix and match: TP + SL, or just TP, or SL with a trailing stop that chases the price higher.

### Three ways to use templates

**Default template (most common)** — you choose one template as your default in `/admin/templates`. Every time you open the OrderTicket, it's pre-filled. You can override it per-ticket by clicking None.

**Per-ticket pick** — Open OrderTicket → toggle "Default" off, or click "None". Pick a different template for that single trade.

**No template** — Leave it as None. Order places with no auto exits; you close it manually or with an agent.

### Exit mechanics (plain English)

**Take-Profit (TP)** — auto-sell at a higher price (long) or auto-buy at a lower price (short). Pick % above your entry or ₹ per contract. When the market touches your target, the platform executes it. Type: LIMIT (exact price, may miss in fast markets) or MARKET (fills fast, no price guarantee).

**Stop-Loss (SL)** — auto-sell (long) or auto-buy (short) if the market moves against you. Same per-entry or per-₹ choices. Always LIMIT to cap your slippage.

**Trailing Stop** — SL that chases profit. Start at −1%, but if the position goes +2%, the stop rises to lock in +1% of gain. Platform checks every 30 seconds and moves the stop higher (long) or lower (short) as price improves. Useful for letting winners run while protecting downside.

**Hedge Wing** — for option sellers. Sell 1 call → auto-buy 1 put at a nearby strike to hedge gamma. Platform scans the option chain, picks a reasonable wing candidate, and buys it when your parent fills.

**Scaled Close** — multi-target TP. Instead of exiting all at once, exit 30% at +50%, 40% at +100%, 30% at +150%. Locks in incremental gains on the way up.

### Seeded templates

Platform ships with two:

| Template | Use for | What happens at fill |
|---|---|---|
| **default-long-option** | Buying calls or puts | TP at +80% MARKET. No SL, no wing, no scale. |
| **default-short-vol** | Selling puts / calls | TP at +10% LIMIT, SL at −20%, buys a hedge wing 1 strike away. |

You can edit these, create new ones, and mark any template as your default so it auto-fills in OrderTicket.

### Multi-broker support — what works where

Templates now work on **all three brokers**, with a small caveat on Groww. The OrderTicket shows an inline warning chip (amber) below the template summary when the selected template asks for a feature the selected broker can't provide natively — you see the gap at submit time, not at fill time.

| Broker | TP only | SL only | TP + SL (OCO) | Trailing stop | Notes |
|---|---|---|---|---|---|
| **Zerodha Kite** | ✅ | ✅ | ✅ native | ✅ native | Full coverage. |
| **Dhan** | ✅ | ✅ | ✅ native | ✅ native | Forever Order maps 1:1. **MCX / commodity not supported** — use a Kite-mirrored account for MCX templates. |
| **Groww** | ✅ | ✅ | ✅ emulated | ⚠ no trail | OCO is emulated by placing two single GTTs + a background "pair-watcher" that cancels the sibling when one side fires. There's a ~15-second race window between the fire and the sibling cancel — under fast moves, both legs can occasionally fill. The warning chip flags this. |

If the broker can't natively do what your template asks, you'll see one of these chips:
- **"Groww OCO emulated — ~15s race window"** — TP+SL template on a Groww account; both legs may fill on a fast move.
- **"Dhan can't trail — SL stays fixed"** (only on future broker that lacks `modify_gtt`)
- **"{broker} has no GTT — scale-out won't attach"** — scale-out ladder on a broker without GTT support.

### Troubleshooting

**My TP/SL didn't attach** — after the order fills, check the order's row on the `/orders` page. You should see a chip `tmpl:#N ✓` once the exit orders are live. If you see `tmpl:#N …` (dots), they're still placing. If there's no chip, the template didn't attach — check the `/api/admin/logs` for errors.

**My trailing stop isn't advancing** — check `/admin/settings` → `templates.trail_poll_interval_seconds` (default 30s). Stop advances on each poll. If the setting is very high, the lag increases. Two-leg OCO trails ratchet the SL slot while the TP slot rides through unchanged.

**Groww OCO: both legs filled** — the ~15s pair-watcher window let a fast move take both sides. There's no current mitigation other than using a tighter `templates.oco_pair_poll_seconds` setting (default 15s) — but lower cadence means more broker polling. Native-OCO brokers (Kite, Dhan) avoid this entirely.

**Dhan template rejected with "Forever Order does not cover MCX/NCO"** — Dhan's Forever Order doesn't support commodity. Place the parent on your Kite-mirrored MCX account and the template attaches there instead.

**Can I use templates with agents?** — yes, independently. An agent can place an order (with a template attached); a separate template can handle the exit. They don't conflict — agents manage _when_ to trade, templates manage _how_ to exit.

### Adding your own agent

Easiest path: copy an existing rule that's close to what you want, change the threshold or the action. The platform validates your edits before saving — you'll see a green check or a red error before the save button works.

---

## Simulator — the safe-test space

The Simulator (`/admin/execution?mode=sim`) is where you ask: *"if the market did X, what would my agents do?"*

You pick:
- A **scenario** — pre-canned price moves like `nifty-down-3pct` (NIFTY drops 1% / 2% / 3% over three ticks, every option re-prices via Black-Scholes)
- A **seed** — where the positions come from:
  - **Scripted** — fake account / fake symbols, fully deterministic
  - **Live** — your real Kite book at this moment
  - **Live + scenario** — real book + scripted extras layered on top
- A **rate** — how fast ticks advance (default 2 seconds = readable but not glacial)

Then press **Start**. Every alert, Telegram message, email, and paper-traded order fires *as if it were real*, but every artefact is tagged `SIMULATOR` so nobody confuses it with a real fire. Auto-stops after 30 minutes.

### What you'll see while it runs

- **Position pills** at the top — each open contract with side / qty / LTP / P&L. Watch them shrink as fills close out positions.
- **Chart panel** — one mini chart per symbol showing the price move + markers where orders were placed / filled / unfilled. **Mouse-wheel zooms in around the cursor; click-and-drag pans; "reset" button restores the full range.**
- **Log panel** at the bottom — Simulator tab streams every tick + price diff; Order tab shows the paper-traded `AlgoOrder` rows; Agent tab shows `sim_mode=true` events.

### Custom positions

If you want to test a position you don't actually have, add it manually: scroll to **Custom positions** below the controls, click **+ Add row**, type a symbol / quantity / last price. Mix it with your real book or use it standalone. F&O symbols re-price coherently when an `underlying_*` move fires (Black-Scholes with calibrated IV); cash equities track simple percentage moves.

### Run-in-Simulator on a specific agent

Every row on `/agents` has a **Run in Simulator** button. Click it, and the simulator builds a one-off scenario *targeted at that agent's condition tree* — no manual scenario picking. Useful for: *"does this new agent I just wrote actually fire when I think it should?"*

---

## The chase / fill / order engine — what happens when an agent says "place order"

Real-world option orders rarely fill at exactly the price you asked. The platform uses an **adaptive limit-order chase** — the same logic for sim, paper, and live:

1. **Place a LIMIT order** at the current bid (if you're SELLing) or ask (if you're BUYing).
2. **Each tick** (every 5 s on prod, every scenario tick in sim):
   - Walk every open order
   - Ask the quote source for the current bid/ask
   - **Fillable?** — bid ≥ limit (SELL) or ask ≤ limit (BUY) → mark `FILLED`, write the fill price
   - **Not fillable?** — bump the limit one step toward the opposite side ("chase"), increment attempt counter
3. **Cap at `simulator.chase_max_attempts`** (default 5). After the cap, mark `UNFILLED` and stop.

You see this in the Order tab as live updates: `chase #2 limit=₹180.00`, then `chase #3 limit=₹181.50`, then `FILLED @₹181.50 after 3 chase(s)`. The chase engine is the same code path for paper and live — just the quote source differs.

**Partial fills** — when the broker fills part of your order and you chase the residual, the row's `detail` updates to `PARTIAL 60/100 @ ₹1234.50 (chasing residual 40)`. The exit GTT (TP/SL) sized when the rest fills correctly reflects only the actually-filled portion, not the original ask. (Pre-Sprint-B the chase ignored partial fills entirely; the UNFILLED give-up showed the original quantity even when 60 % had traded.)

**MCX unit handling** — Kite reports MCX `filled_quantity` in lots while everything else in the chase tracks contracts. The chase converts back automatically; you don't see this — pre-Sprint-D it caused phantom partial-fill loops on every MCX order.

### Where the chase engine matters

- **Auto-close on loss** (`chase_close_positions` action) — when a loss agent fires, it tries to close positions by chasing. You see exactly how aggressive the chase was before it filled.
- **Expiry-day cleanup** — every weekly expiry day the platform automatically chase-closes ITM options before the broker takes them to delivery.
- **Manual orders from Terminal** — when you type `buy NIFTY25APR22000CE 50 @180` on `/console`, the chase engine handles it just like an agent-driven order.

---

## Charts — making sense of the moves

Two chart types, both with the same zoom + pan behaviour:

### Price charts (sim / paper / live)

Live tick streams of last-traded price + bid/ask spread + order-event markers. Find them on:

- `/admin/execution?mode=sim` — one chart per symbol with captured ticks while a sim runs
- `/admin/execution?mode=paper` — same but for the prod paper engine
- `/agents` and `/orders` — inline charts on the page; from any page the Chart icon in the header opens the canonical Chart modal for any symbol

Symbols are classified as:
- **SPOT** (sky-blue tag) — an underlying like NIFTY itself
- **F&O** (amber tag) — an option or future, with the underlying drawn as a faint dashed sky-blue line on the same chart for context

Order events appear as colored dots on the line:
- **Amber** — order placed
- **Emerald** — filled
- **Red** — chase gave up (unfilled)

Hover over any dot to see what side / quantity / price.

### Options analytics — the payoff diagram

`/admin/derivatives` is the dedicated options-research page. Pick an underlying (NIFTY / BANKNIFTY / …) and the page surfaces every option + future you hold on it as **Candidates**. Tick / untick rows to include / exclude legs from the payoff — the chart re-renders on every toggle.

- **Payoff diagram** — your aggregated P&L as a function of where the underlying ends up. Two curves: today's value (Black-Scholes with current IV) and expiry value (intrinsic only). Profit zone shaded green, loss zone red. Vertical markers show current spot, every strike, every breakeven (iron condors draw 2!).
- **Stat overlay (top-left of chart)** — at-a-glance numerics: **SPOT** (current spot), **TDAY** (today's P&L at spot), **EXP** (expiry P&L at spot), **MAX P** (max profit), **MAX L** (max loss). Color-coded green/red so you can read position health without looking at the side panel.
- **Side panel** — Position Greeks (Δ Γ Θ V ρ) summed across all checked legs, plus risk metrics (max profit, max loss, R:R, breakevens, POP, expected value).
- **Candidates panel** below — checkboxes for every position. Source badge tells you whether each row is live, sim, or draft.

The chart x-axis is **always ±2.5 standard deviations from current spot** at expiry — so a 7-DTE option charts a tighter range (~5%) than a 60-DTE option (~15%). You see exactly the "where it could plausibly land" zone, not arbitrary fixed percentages. Wheel to zoom further into the money / out of the money; reset to come back.

### Adding draft (hypothetical) positions

Click the `+` button next to the dropdowns. The option chain opens — pick an expiry, click `+ CE` / `+ PE` next to a strike to drop a contract into **Drafts**. Drafts are editable: change the qty, avg cost, or LTP inline. They show up as a candidate row immediately so you can include / exclude them like any other leg.

Use this when you're modelling a trade you don't own yet — "what if I add a 24500 PE here?". The draft appears alongside your live + sim positions, the chart re-renders, and you can see the breakevens + POP before placing the order.

Live vs sim is **auto-detected**. While a simulator is running the page works off the sim book and shows a `SIMULATOR` chip in the header; otherwise it works off your real broker positions.

Constraint for v1: all legs in the chart share the same underlying and same expiry (calendar / diagonal spreads need different math). The page warns you if a checked draft conflicts.

### When prices look "stale"

If the broker has no live last-trade for a contract (illiquid, off-hours, weekend), the chart still draws — the platform falls back through:

1. live last-traded price
2. previous-day close
3. midpoint of bid/ask
4. your own average cost
5. estimated theoretical price at default 15% IV

You see this as a yellow `stale: <source>` chip on the pricing block, and a `·source` tag next to each price. Treat the absolute rupee numbers with care when the chip is yellow; the *shape* of the payoff is still right.

---

## Mode switching — one navbar dropdown, no ticket overrides

Mode is now set **only** from the navbar dropdown in the top-right corner. Every page shows a pill: **LIVE** (red, the default on prod), **PAPER** (sky-blue), **SHADOW** (orange), **SIM** (rose), or **REPLAY** (green). Click it → pick a mode. The switch takes effect immediately; the next agent fire routes through the new mode.

- On prod (`main`), you can flip between LIVE ↔ PAPER at will. A confirm modal asks once per switch from LIVE to ensure it's intentional.
- On dev (non-main branches), every action is forced to PAPER regardless of the dropdown. LIVE is greyed out.
- Telegram alerts carry the mode tag: (no tag) = LIVE, `[PAPER]` = paper mode.

The order ticket used to carry a per-order mode toggle. That's gone — the navbar pill is the sole source of truth. Simpler mental model, fewer mistakes.

---

## Paper trading dashboard — `/admin/execution?mode=paper`

The visual surface for what mode 2 is doing on prod. Same layout as the simulator but reading from the live paper engine. You'll see:

- **Status banner** — green when there are open paper chases, amber when idle, grey on dev (paper only runs on `main`)
- **Open chase pills** — one per in-flight order with side / qty / current limit / attempt count
- **Mini charts** — per symbol with markers
- **Activity log panel** auto-filtered to PAPER mode only — Order tab shows `mode='paper'` rows; Agents / Terminal / Ticks / System / News tabs surface only mode-2 events

The most useful page during the soak phase: when you've flipped the navbar to PAPER and want to watch the chase against the live market without an order touching the broker.

---

## Multi-account basket orders — one submit, parallel execution

Open `/orders` → **Ticket** tab. The entry form now has a basket-building workflow:

1. Fill a symbol / qty / side / price / type as normal.
2. Click **+ Basket** — the order pills appear below, one per account.
3. On each pill, click the account dropdown to pick which Kite account this leg trades on.
4. (Optional) add more symbols by clicking **+ Add symbol** and repeating steps 1–3.
5. Click **Submit** → one Kite `basket_order` call per account fires in parallel.

You see a margin strip above the pills: Required / Avail / After (post-trade) for each account. Offset-aware via Kite's `kite.basket_order_margins` API. All legs go live or paper together — the navbar mode applies to the whole basket.

Submit fails (400) with a clear message if any account has insufficient margin. Edit the basket and retry.

---

## Auto profit target on every order

Every order on the Entry card has a new **Target** row. Default: **+30%** from the entry price (tunable at `/admin/settings` → `algo.default_target_pct`). The input toggles between % and rupees.

When you submit (PAPER or LIVE), the backend auto-places a take-profit order after the entry fills. You see both orders in the Order Activity panel — the entry as OPEN, FILLED, or UNFILLED; the target as a separate LIMIT order set to close at the TP price.

Use `/admin/settings` to change the default, or override per-order inline on the ticket.

---

## Derivatives page — Holdings toggle + closed positions freeze

`/admin/derivatives` is your options research workspace. The payoff chart now has a **Holdings ON/OFF toggle** (small switch in the legend, sky-cyan when enabled) so you can:

- **ON (default)** — see the full picture: option legs + your held equity positions (DIXON stock + NIFTY puts, for example). P&L reflects the net of all.
- **OFF** — isolate the derivatives only. P&L shows pure option P&L without the stock's cost basis. Useful when you want to analyse the hedge separately from the spot position.

Positions you've **partially sold intra-day now freeze correctly**. The day P&L stops drifting once you've sold your shares — if you sold IFCI at 3 PM and it drops 2% after hours, your day P&L stays locked at the 3 PM fill price. Same for any symbol: once qty=0, the day-change number doesn't move even if the LTP keeps falling.

## Derivatives page — 3-band Close layout

The **Close** tab surfaces three distinct buckets:

| Section | What | When to act |
|---|---|---|
| **● ITM ON EXPIRY** (amber) | Positions that need broker action before expiry | Always before market close on expiry day |
| **⊗ NETTED** (slate) | Positions that cancel at settlement — CE/PE pairs net to zero (MCX commodity only) | Monitor only; broker auto-nets at settlement |
| **○ OUT OF THE MONEY** (muted) | Expires worthless | Monitor only; let them expire |

Netted pairs on MCX carry shared colors (N1 / N2 / … labels) — a teal row and a pink row with the same number are a pair that cancels. NSE equity options skip the NETTED section (exchange has no netting rule).

---

## Proxy hedges — when one instrument can hedge another

**This is a major capability of the terminal**, and one no Indian retail trading platform (Sensibull, Streak, Opstra) currently offers. Institutional desks pay for it on Bloomberg PRM or IBKR Portfolio Margin; here it's a four-column DB row and a "Compute β" button.

If you hold an **ETF** that tracks a commodity or index (GOLDBEES tracks gold, NIFTYBEES tracks the NIFTY index), you don't need to buy the actual underlying to be exposed to it. The ETF's NAV moves with the underlying spot price. When you pick the underlying on `/admin/derivatives` (say GOLDM), your GOLDBEES holding shows up in Legs as a PROXY hedge — automatically converted to gram-equivalent and then to GOLDM option lots.

The math is fully derived from live broker prices: market value of your GOLDBEES ÷ current GOLD spot = grams-equivalent ÷ lot size = GOLD lots. The PROXY chip surfaces all three numbers so you can sanity-check.

**For stocks vs. indices** (RELIANCE → NIFTY etc.), the relationship is statistical, not mechanical. The platform supports a **β regression**: it fetches 60 days of daily closes for both sides, computes the slope β and the R² confidence, and writes them back to the row. When you pick NIFTY, your RELIANCE holding shows as a β-scaled NIFTY-equivalent. A daily background task keeps β fresh on a 7-day cadence; you can also hit "Compute β" manually for an instant rerun.

You only see this when you have the proxy holdings. The default seeded pairs (GOLDBEES/SILVERBEES/NIFTYBEES/BANKBEES) cover most ETF tracking cases out of the box.

**Stale-β indicators** — the PROXY chip's tooltip carries an age tag: amber when β was computed 2–7 days ago (still usable but ageing), red ⚠ when β is older than 7 days OR the last regression attempt errored. If you see the ⚠, hover the chip — the tooltip shows the failure reason ("too few overlapping bars", "broker rate-limit", etc). Hit "Compute β" on `/admin/settings` to retry now. The daily background task at 02:30 IST will also retry on its own.

**EV + POP with proxy legs on** — when proxy/equity legs are included in your strategy, the Expected Value and Probability-of-Profit numbers update to reflect the COMBINED payoff curve. Pre-Sprint-D those values came straight from the backend's option-only calculation; the hedged combined position now reads correctly on the panel.

---

## Symbol identity — commodity root vs contract

In the Chart workspace (`/charts`), Derivatives Candidates grid, Watchlist, Positions, and Orders, symbols now show their **commodity root** as the primary label. A small chip below shows the resolved **contract month** (e.g. CRUDEOIL → `CRUDEOIL26JUNFUT`).

When a contract has ≤3 days until expiry, the contract chip flips amber so you see expiry-day expirations at a glance. You trade the specific contract (the chip); the root commodity (the label) keeps related contracts grouped.

---

## Settings — the runtime knobs

`/admin/settings` is where you tune anything that changes more often than a deploy cycle. Categories:

- **Execution** — the per-action live/paper flags. Top of the list because it's the highest-stakes decision.
- **Alerts** — cooldown, rate window, suppression deltas
- **Algo** — chase cadence, attempt cap, expiry rules
- **Performance** — refresh intervals
- **Simulator** — sim defaults
- **Notifications** — telegram / email toggles
- **Logging** — verbosity per handler

Each row is one parameter. Click the small `(i)` chip next to the key to see what it does, what its valid range is, and what default it shipped with. Edit, **Save**, and the next agent tick picks up the change. **Reset** restores the code default.

The **execution-mode banner** at the top is loud on purpose: green = "every broker action is in PAPER mode"; red = "⚠ N of 6 actions are LIVE — real orders will hit the broker".

---

## Brokers — managing accounts

`/admin/brokers` is the CRUD UI for broker credentials. Add a new Kite account with API key / secret / password / TOTP seed; encrypts secrets at rest with a key derived from `cookie_secret`; never returns the secrets back through the API. Every save reloads the platform's connection map so the next broker call uses the new credentials — no service restart.

Each row has a **Test** button that hits `broker.profile()` to verify the credentials authenticate. ✓ next to the button means it's working; ✗ shows the broker's error in the tooltip.

---

## Day-to-day workflow — what you'll actually do

A typical session for an active operator:

1. **Open `/dashboard`** — quick check: holdings, positions, P&L per account.
2. **Watch `/agents`** — any fires today? Any in cooldown?
3. **If a new strategy is being considered**: `/admin/derivatives` → Strategy mode → build the legs → eyeball the breakevens, max loss, POP → if it looks good, place the trade through the Terminal or your usual flow.
4. **If thresholds need adjusting**: `/admin/settings` or edit the relevant agent on `/agents`.
5. **Before adding a new agent**: write the rule → `Run in Simulator` → confirm it fires on the right conditions and the auto-close action does what you expect → flip it ON.
6. **Once a week or so**: glance at `/admin/paper` to see what mode-2 paper trades fired since you last looked. Compare against what you'd have done manually.

---

## Investor portal — what your LPs see

If RamboQuant is running money for limited partners (LPs / friends-and-family / a small fund), they want to see how their money is doing without bothering you for a screenshot every Friday. The **investor portal** is a token-gated URL that gives each LP a personal read-only view of their NAV slice — what their contribution is now worth, today's move, profit/loss against their initial cheque.

**How it works (operator side):**

- Open `/admin`, find the LP, click **Portal** → modal opens.
- Pick how long the URL stays valid (default 90 days) and add an optional note like "WhatsApp 23 Jun".
- Click **Mint** → a URL appears in green.
- Click **Copy** → paste into WhatsApp / email → send to the LP.

That's it. The LP clicks the URL → lands on a clean cream-and-champagne page (no algo console chrome) showing:

- Their portfolio value at the last NAV snapshot
- Today's move (₹ + %)
- Net P&L vs their contribution
- Their share % of the fund
- A 180-day curve of their value over time

**Why a URL, not a login?** LPs are usually not technical. Asking them to remember a password for a quarterly NAV check is friction. The URL pattern is what Carta and SS&C/GP-Link use for investor portals — same shape, same trade-off.

**If a URL leaks**: same modal lists every minted URL with active / revoked / expired pills. Click **Revoke** on a row → the URL stops working immediately. Mint a new one and re-send.

**Visibility for you**: each row shows the last time the LP visited + total visit count. You can tell at a glance "this LP looks every Friday" vs "this LP hasn't checked in 3 months."

The portal is read-only by definition — there's no submit-an-order surface, no settings, nothing the LP can break. They see one number that matters and a curve telling them whether it's going up or down.

---

## How each LP's slice is calculated — the units model

If a single LP put in ₹10L and the fund is now worth ₹11L, you don't need much math to figure out their slice is ₹11L (assuming they own 100%). It gets interesting when:

- **A second LP joins partway through.** Should they get the same percentage as the original LP? No — they put in at a higher per-unit value because the fund had already gained.
- **An LP takes some money out mid-year.** What price do they redeem at? Whatever the fund's per-unit value is on the day they exit.
- **The fund posts a year of returns and you want to credit each LP their share.** The earlier LP gets credit for the full year; the late joiner only gets credit for their tenure.

The standard solution every real fund admin uses (Carta, CAMSonline, SS&C/GP-Link, every Cat-III AIF) is **units**:

1. **Each LP holds a number of "units."** Think of these like partnership shares — an abstract count, not rupees.
2. **The fund publishes a NAV per unit each day.** It's just `fund value ÷ total units outstanding`.
3. **An LP's slice = their units × today's NAV per unit.** That's it.
4. **A new subscription buys units at the day's NAV per unit.** ₹5L when NAV/unit is ₹1.1L gets them 4.55 units.
5. **A redemption sells units at the day's NAV per unit.** Same arithmetic, in reverse.
6. **An LP's P&L = current slice − total capital they put in (less anything they took out).** Capital movements never count as "gains."

This is what RamboQuant runs under the hood. The portal still shows a single ₹ number to each LP, but the engine computing it is doing proper fund-accounting math, not a flat `share_pct × firm_nav`.

**What you'll see when you first deploy this:**

The first time anyone hits the NAV page after the units-math switch, RamboQuant looks at every LP with a non-zero `share_pct` and auto-creates one **bootstrap** event per LP. This converts the old static-share state into the units model. You'll see these events appear in `/admin` → Portal → Events tab with the label "auto-bootstrap from v1 share_pct."

You don't have to do anything for this conversion. The bootstrap math is designed so that if your LP shares already sum to 100% (e.g. you and two LPs split 70/15/15), the numbers come out identical to the old model on day one. Subsequent gains and capital movements then flow through the units machinery correctly.

**When to log a real event:**

- An LP adds capital → click `/admin` → user row → Portal → Events tab → Add: type=Subscription, amount, NAV/unit at that date, optional note.
- An LP takes capital out → same form, type=Redemption.
- The bootstrap was wrong (e.g. you typed contribution=0 by mistake when the LP actually put in ₹5L) → delete the bootstrap row + manually add a fresh bootstrap with the correct numbers, OR add a corrective subscription event.

The next NAV snapshot (16:00 IST, or the operator's manual recompute) will reflect the new units.

---

## Audit log — what's recorded

Every action that changes state in RamboQuant — placing an order, a broker filling that order, an agent firing, you tweaking a setting, a monthly statement going out to an LP, the daily NAV cron writing a snapshot — lands as one row in an audit log. The log is the platform's memory of "who did what, when, and with what outcome." A SEBI Cat-III audit visit doesn't need a fancy UI; it needs the trail. RamboQuant gives them both.

**Who can see it?** Admin, risk, and ops roles. Trader, observer, and demo don't have access — the audit log is forensic data, not operational data.

**Where do I find it?** `/admin/audit`. Pick a category pill at the top (Orders / Agents / Users / Config / System) or use the column filters to scope down.

**Does logging slow things down?** No. Every audit write is fire-and-forget — the platform schedules the row insert as a background task while your request is already heading back to your browser. You won't measure the difference.

**What if I want to see failed actions too?** By default the log captures only successful mutations (2xx/3xx). If you're debugging "I clicked SUBMIT and got an error — what blocked it?", flip `audit.log_failed_mutations` to ON in `/admin/settings`. You'll start seeing 4xx/5xx rows alongside successes. Toggle off when you're done — it can get noisy.

---

## Glossary

- **Agent** — a rule row on `/agents`.
- **Alert** — the moment an agent fired.
- **Action** — what the agent did (place order / close position / etc).
- **Bid** — the highest price someone is willing to pay right now.
- **Ask** — the lowest price someone is willing to sell at right now.
- **Spread** — bid minus ask. Tight spread = liquid contract; wide spread = illiquid.
- **LTP** — last-traded price. The most recent trade.
- **Strike** — the contracted price for an option. NIFTY 22000 CE has strike 22000.
- **DTE** — days to expiry.
- **IV** — implied volatility, expressed as an annualized percentage.
- **Greeks** — sensitivities of option price: Delta (to spot), Gamma (to delta), Theta (to time), Vega (to vol), Rho (to interest rate).
- **POP** — probability of profit. The chance the position ends profitable at expiry.
- **Underlying** — the asset the option is on. NIFTY for NIFTY25APR22000CE.
- **Spot** — the current price of the underlying.
- **Chase engine** — the loop that re-quotes a stale limit order each tick to follow the market.
- **Paper trade** — a real-data dry-run that writes order rows to the database but never sends them to the broker.
- **Sim** — simulator. Fully fabricated prices, used for testing.

---

## When things go wrong

| Symptom | Where to look |
|---|---|
| Agent didn't fire when I expected it to | `/agents` → expand the row → check "Last fire" timestamp + "Count" + cooldown. Also try **Run in Simulator** to confirm. |
| Telegram / email not arriving | `/admin/settings` → notifications block. Make sure `cap_in_<branch>.telegram` and `mail` are on. |
| Options page shows yellow "stale" chips | The broker has no fresh quote. Likely because: market closed, contract illiquid, weekend. Fallback values are still useful for the *shape* of the payoff, just not absolute P&L. |
| Brokers page shows "PENDING" status pill | The DB row exists but the platform's connection map hasn't picked it up yet. Wait 15 s — the page polls. Or click **Test** to force a load. |
| Sim won't start | Check `cap_in_<branch>.simulator` in `backend_config.yaml` — defaults to ON on both dev and prod. |
| "I'm in LIVE and didn't want to be" | Pick PAPER from the navbar dropdown. Effect is immediate; the next agent fire lands as paper. No service restart needed. |

---

## Demo mode — for visitors / recruiters / investors

If you're not logged in and you visit `ramboq.com`, the algo console opens in **demo mode** — the operator's *real* broker data with accounts masked (`ZG####` / `ZJ####` in place of the real IDs). Everything you click is real product reading from the live system; no synthetic fixture file is maintained.

- Positions, holdings, funds — real broker rows with account IDs masked. If the operator is busy, demo looks busy; if the operator is idle, demo is idle.
- The order ticket works. Place a paper order — it'll register, the chase engine will track bid/ask, you can watch it fill or cancel. Never reaches a broker. Live-mode submits are silently downgraded to paper for demo.
- Settings / Brokers / Users are hidden. Those are operator surfaces; visitors don't need them.
- Mode badge **DEMO** (purple) sits in the navbar so the context is unmistakable.

To exit demo mode → click **Sign In** (top right). The link goes to the operator login.

For an operator who's logged in, demo mode is invisible. Anonymous + prod = demo; everything else = your normal session.

---

## Mode badges in the navbar

The navbar's right-hand side surfaces what the platform is currently doing. Each pill pulses while its mode is active:

| Badge | When it shows |
|---|---|
| **DEMO** (purple) | Anonymous visitor on the prod site — the data you're seeing is synthetic, no broker touch. |
| **PAPER** (blue) | The prod paper engine has at least one open chase order. Could be your manual paper-mode ticket, or an agent fire that landed in paper. |
| **SIM** (red) | A simulator run is in progress. Available on both dev and prod via the navbar mode dropdown. |

Both PAPER and SIM can show at the same time on dev if you've started a sim AND have paper orders open. Below the badges, full-width banners under the navbar carry the scenario / chase-count detail.

---

## Chat-driven research + agent building

The `/admin/research` page lets you ask **Claude Code** (your
terminal) to research a stock end-to-end, build draft agents from
the thesis, and — with explicit per-call operator approval — place
real broker orders. The chat is in your terminal; the page is the
persistence + audit + token-mint surface.

The full operator runbook is **[LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md)**.
Highlights:

- **₹0 incremental cost** — your Claude Code subscription is the
  only LLM. Server-side helpers (auto-title, news sentiment) use
  the free tier of Gemini 2.5 Flash.
- **24 MCP tools** — 16 read, 2 persist, 6 gated write
  (place_order, cancel_order, modify_order, activate_agent,
  deactivate_agent, update_agent).
- **Per-call confirm-token gate** — every write requires a
  60-second, single-use, purpose-bound token you mint on the Lab
  page. The LLM cannot mint, replay, or redirect a token.
- **Telegram deep-links** — every successful gated write fires a
  Telegram message with a `request_id` link that opens the Audit
  tab pre-filtered to that exact row.

If you've never used the Lab, work through
[LAB_MCP_GUIDE.md section 3](LAB_MCP_GUIDE.md#3-one-time-setup-3-minutes)
once (~3 minutes of one-time setup) then come back to the
**Daily Workflow** section.

---

## Where to learn more

- **[AGENTS_GUIDE.md](AGENTS_GUIDE.md)** — extensive walkthrough for authoring + testing agents. The four-stage validation ladder (validate → dry-run → simulator → activate), every metric / scope / op, fragments, lifespan, and copy-paste patterns.
- **[SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md)** — hands-on Lab workflow. Scenarios, Run-in-Simulator, custom positions, iteration mode, market-state presets, troubleshooting.
- **[LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md)** — chat-driven research, agent drafting, and the per-call confirm-token gate. Covers GenAI usage, Claude Code setup, and the 24 MCP tools.
- **[ADMIN_GUIDE.md](ADMIN_GUIDE.md)** — exact button labels, JSON conditions, API endpoints. The operations reference.
- **[CLAUDE.md](CLAUDE.md)** — architectural notes for engineers + AI assistants. Covers the code structure, data flow, and design decisions.
- **`/admin/tokens`** — explore the agent grammar (every metric / scope / operator the platform knows about).
- **`/admin/execution?mode=sim`** — the safest place to learn by experimentation. Nothing you do there touches your real money.
