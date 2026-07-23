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

## Closed-hours data — snapshots instead of live feeds

When the market is closed (weeknights, weekends, holidays), every data display
(positions grid, holdings grid, P&L cards) freezes at the last in-session snapshot
instead of showing blanks or stale values. The platform automatically captured
snapshots when NSE / MCX closed, and serves them from the database without calling
the broker.

**What you'll see**:
- Positions grid, holdings grid, nav cards — all show the last live market-hours snapshot
- Charts and sparklines — historical data persists (no live quotes, but the past week is there)
- P&L breakdown pills (P / M / C / H) — frozen snapshot values. The **P pill** (day P&L)
  now uses the authoritative daily settlement (`daily_book.total_pnl`) to compute the day's
  change: `current_pnl − yesterday's_settlement_pnl`. For positions opened today that aren't
  yet in the daily book, the fallback is `pnl − overnight_qty × (prev_close − avg_price)`.
  This replaces the unreliable broker `day_change_val` field and correctly handles:
  - Positions held overnight and fully exited during the session (realized P&L now appears)
  - New positions opened today (uses fallback math)
  - Closed positions are naturally excluded from the base
  
  The same computation propagates to the per-leg Day P&L column in the derivatives legs
  grid and the Performance page TOTAL row, so NavStrip P and the Greeks total match during
  the MCX overnight window.
- Live price LTP field — empty or last-known price (no updates)
- Refresh button — clicking it says "Both NSE and MCX are currently closed" + still fetches
  the snapshot from DB (fast, no broker round-trip)

## NavStrip — reading the four pills

Each pill label (P / M / C / H) in the header strip is now a **clickable panel**. Click any
label to open a floating overlay that explains what the pill measures, what each slot value
represents, and how it is computed. The panel carries a colored left-border accent matching
the pill identity (amber for P, violet for M, sky for C, cyan for H).

Every numeric slot in the strip also carries a small **ⓘ** icon that appears on hover. Mouse
over any value to see the icon; hover the icon to open a detailed panel for that specific
slot — e.g., "Day P&L: live tick price − prev close × net qty across all accounts."

These overlays work on desktop and are keyboard-accessible (Tab to focus, Enter/Space on the
label, or just hover for the slot icons).

## Day P&L Breakup — drill into today's intraday profit/loss

The NavStrip's **P pill (Day P&L)** slot is now clickable. Clicking it opens the **Day P&L Breakup**
modal — a detailed per-account, per-symbol table showing exactly how today's intraday profit
or loss accumulated:

**What the modal shows**:
- Per-account subtotal at the top (sum of all symbols for that account)
- Grand total across all accounts (should match the P pill value)
- One row per symbol with non-zero day P&L, showing:
  - **prev_close**: yesterday's official settlement price (frozen at the first snapshot of
    each trading day, immune to Kite's EOD overwrite)
  - **LTP**: current last-traded price
  - **overnight qty**: how many contracts you held from prior close
  - **buy/sell volumes**: intraday quantities and rupee values for any buys or sells today
  - **lifetime P&L**: cumulative profit/loss on the position since it opened
  - **settlement P&L**: the realized value from yesterday's close (for positions you held
    overnight)
  - **day P&L**: the intraday component — how much you've gained or lost *today*

**Zero-value rows**: symbols with zero day P&L show a ⚠ icon with a tooltip explaining why
(e.g., "Position opened today but flat again" or "Held overnight, no session moves").

**Close the modal**: Press Esc or click the backdrop.

This modal is useful when: you want to understand *which symbols* contributed to your day's
profit or loss, see the settlement vs intraday split, or verify that the platform's day P&L
math matches your expectations.

**Behind the scenes**:
When each market closes (via `nse:close`, `mcx:close`, `cds:close` events), the platform
takes a full snapshot of positions / holdings / cash / margin and writes it to PostgreSQL.
On the next page load during closed hours, every data read checks "is any segment open?"
and if not, loads from the snapshot instead of calling Kite / Dhan / Groww.

**Why this matters**: No blank grids, no "connection lost" errors, no stale overnight data.
Operators see the accurate pre-close state all night, ready for next open. Telegram alerts
and email still arrive in real-time for any fills / agent events that happen overnight.

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

The platform ships with 16 loss / risk / market-status agents pre-seeded:

| Slug pattern | What it watches | Default status |
|---|---|---|
| `loss-pos-*` | Position P&L (intraday F&O exposure) | active |
| `loss-hold-*` | Holdings P&L (long-term equity) | active |
| `loss-margin-low` | Available margin on any account drops below ₹25,000 (early warning) | active |
| `loss-funds-*-negative` | Cash or margin balance below zero | active |
| `market-open-nse` | Fires at NSE open (09:15 IST), info-only notification | active |
| `market-close-mcx` | Fires at MCX close (23:30 IST), info-only notification | active |

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
- **Chart panel** — one mini chart per symbol showing the price move + markers where orders were placed / filled / unfilled. **Mouse-wheel zooms in around the cursor; click-and-drag pans; "reset" button restores the full range.** The chart section now has a **fullscreen button** in its card header — click it to expand the chart panel to full viewport height while the sim is running. The collapse and default-size buttons work the same way as on other cards.
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
- **Post-placement chase failures** — if an order is placed but the fill chase later fails (e.g. price moves too far from the limit and attempts are exhausted), a Telegram + email alert fires so you know the position wasn't fully executed. Previously these post-placement failures were silent.

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

### Technical overlays on the price chart

Click the **Overlays** button in the chart toolbar to toggle indicators. Your choices persist in browser storage and restore across reloads and sessions.

**Price panel overlays** (drawn on the main chart):

| Overlay | Description | Colour |
|---|---|---|
| SMA 20 / SMA 50 | Simple moving averages | Sky-blue / Violet |
| EMA 20 / EMA 50 | Exponential moving averages (Wilder smoothing, TradingView-standard seed) | Green / Orange |
| VWAP | Cumulative volume-weighted average price from the first bar in the selected range | Solid cyan |
| BB | Bollinger Bands: 20-period SMA ± 2× population σ, same math as TradingView | Cyan lines + fill |

**Sub-panel overlays** (stacked below the price chart in the same SVG):

| Overlay | Description |
|---|---|
| RSI 14 | Wilder-smoothed RSI with overbought (70) / oversold (30) reference lines. Requires 15+ bars. |
| MACD 12/26/9 | Histogram (green above zero / red below) + MACD line (amber) + signal (dashed red). Requires 27+ bars for MACD, 36+ for the signal line. |

**Tips**:
- VWAP is meaningful only for tradeable instruments with real volume (equities, futures). Index symbols (NIFTY 50, NIFTY BANK) report zero volume, so VWAP will not appear — this is correct behaviour, not a bug.
- Switch to a longer timeframe (3M / 6M / 1Y) before enabling MACD on a thinly-traded contract; fewer than 27 bars leaves the sub-panel blank.
- Overlays are computed in the browser from the same OHLCV data the chart already has — no extra network calls.

### Buy / sell signal markers

When at least one indicator is selected, a **Signals** chip appears in the chart toolbar (defaults to ON). The chart marks each bar where the selected indicator emits a buy or sell event:

- **Green up-triangle** ▲ below the bar's low = BUY signal (the indicator turned bullish)
- **Red down-triangle** ▼ above the bar's high = SELL signal (the indicator turned bearish)
- A small tag (`EMA↑`, `RSI↓`, `MACD↑`, `BB↓`, `VWAP↑`) next to the triangle says which indicator fired
- Multiple indicators firing on the same bar stack vertically so each one stays readable
- Hover the marker for the full tooltip (`Buy signal — RSI 14 @ 2026-04-15`)

**Standard signal rules** (same as TradingView / Sensibull / Streak):

| Indicator | Buy signal | Sell signal |
|---|---|---|
| **EMA cross** (needs both EMA 20 + EMA 50) | EMA 20 crosses **above** EMA 50 — "golden cross" | EMA 20 crosses **below** EMA 50 — "death cross" |
| **VWAP** | Close crosses **above** VWAP from below | Close crosses **below** VWAP from above |
| **Bollinger** | Close **pierces lower** band (oversold) | Close **pierces upper** band (overbought) |
| **RSI 14** | RSI crosses **above 30** from oversold | RSI crosses **below 70** from overbought |
| **MACD 12/26/9** | MACD line crosses **above** signal line | MACD line crosses **below** signal line |

**Tips**:
- Treat markers as **suggestions, not commands** — every textbook signal misfires in real markets. Pair them with broader context (trend, news, position size).
- The Signals chip persists ON/OFF across reloads (saved in browser storage). Click it to hide the markers while keeping the indicator lines visible.
- On dense ranges (1Y with 250+ bars), only the most recent 12 signals per indicator are shown so the chart stays readable.
- Bollinger band-touch signals are throttled to the **first bar** of each run — if price hugs the lower band for 5 bars in a row, you'll see one buy marker, not five.

### Options analytics — the payoff diagram

`/admin/derivatives` is the dedicated options-research page. Pick an underlying (NIFTY / BANKNIFTY / …) and the page surfaces every option + future you hold on it as **Candidates**. Tick / untick rows to include / exclude legs from the payoff — the chart re-renders on every toggle.

- **Payoff diagram** — your aggregated P&L as a function of where the underlying ends up. Two curves: today's value (Black-Scholes with current IV) and expiry value (intrinsic only). Profit zone shaded green, loss zone red. Vertical markers show current spot, every strike, every breakeven (iron condors draw 2!). A dashed amber line marks the net strategy cost (negative = debit paid, positive = credit received) so you can see your break-even reference at a glance. The chart loads immediately after login — no blank flash while spot price fetches; if you have no F&O positions it shows a flat y=0 line. The page auto-selects the underlying of your first open position rather than always defaulting to NIFTY.
- **Stat overlay (top-left of chart)** — at-a-glance numerics: **SPOT** (current spot), **TDAY** (today's P&L at spot), **EXP** (expiry P&L at spot), **MAX P** (max profit), **MAX L** (max loss). Color-coded green/red so you can read position health without looking at the side panel. The **EXP** stat now consistently matches the payoff chart's expiry curve and the legs grid TOTAL row, even when equity holdings have been sold today, F&O legs have been closed, or proxy hedges are present. **How EXP is calculated per leg**: closed legs (qty = 0) contribute their realized/locked-in P&L directly; open legs contribute intrinsic value at expiry plus any partial-close realized P&L. The **EXP** column in the Legs grid reflects this for every leg — previously it was blank for closed legs, now it shows the locked-in realized amount. This means the stat total and grid total always agree.
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

Positions you've **partially sold intra-day now freeze correctly**. The day P&L stops
drifting once you've sold your shares — if you sold IFCI at 3 PM and it drops 2% after
hours, your day P&L stays locked at the 3 PM fill price (the realized portion is recorded
in daily settlement). Same for any symbol: once qty=0 the day P&L uses the realized amount
from the close, and the intraday metric doesn't move even if the LTP keeps falling.

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

`/admin/brokers` is the CRUD UI for broker credentials. Add a new Kite account
with API key / secret / password / TOTP seed; encrypts secrets at rest with a
key derived from `cookie_secret`; never returns the secrets back through the API.
Every save reloads the broker connection map so the next call uses the new
credentials — no service restart required.

Each row has a **Test** button that hits `broker.profile()` to verify the
credentials authenticate. ✓ next to the button means it's working; ✗ shows the
broker's error in the tooltip.

**Behind the scenes**: Broker sessions are managed by a separate background
service (`ramboq_conn.service`). This service handles Kite WebSocket streaming,
token lifecycle for all three brokers (Kite / Dhan / Groww), and lives
independently from the main web API. When you edit credentials here, the service
picks up the changes automatically within a few seconds. You never need to
restart it manually — it's designed to be transparent to your workflow.

---

## Page timestamps — IST only

Every page header that shows a "last refreshed at" timestamp now displays the time in **IST
only** (`HH:MM IST`, 24-hour). Earlier builds showed dual-timezone (IST + UTC) with a toggle
to switch between them. The toggle and the UTC column are gone — IST is the only timezone that
matters in an Indian market context, and the cleaner format reduces visual noise on narrow
screens and mobile.

## Activity card — scrollable tab strip and download

The **Activity** card's tab strip (Orders / Agents / Terminal / Conn / System / Ticks / News)
now scrolls horizontally on narrow viewports (modal widths, mobile). No visible scrollbar
chrome is added; the strip is touch-scrollable and hides the scrollbar with
`scrollbar-width: none`. Previously tabs overflowed off-screen with no way to reach them.

A **Download** button has been added to the card's button group. Click it to export the
currently visible Activity rows as a CSV file. The button is greyed out on the News tab
(news items do not export). On all other tabs, the export captures exactly the rows shown in
the current filter/search state.

## Day-to-day workflow — what you'll actually do

A typical session for an active operator:

1. **Open `/dashboard`** — quick check: holdings, positions, P&L per account.
   The right-hand sidebar opens on the **NAV** tab by default — that's the
   per-account wealth breakdown using the same arithmetic as the canonical
   `/performance` NAV grid (cash + open-position MTM + holdings MTM, summed
   to a firm TOTAL row at the bottom). Click **Capital** for the margin /
   funds breakdown, or **Equity** for the Positions / Holdings summaries.
   The card on the left is the intraday equity curve plus the historical
   Performance drill-down behind one tab. At the bottom of the page, the
   **Activity** card opens on **News** by default; flip to Orders, Agents,
   Terminal, Conn, System, or Ticks to scan whichever paper trail you need
   without leaving the dashboard.
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

## The navbar — how pages are grouped

The algo navbar reads left-to-right by daily-operator frequency, not alphabetic. Three inline groups + two dropdowns:

- **Monitor** (always visible): Tour · Pulse · Dashboard · **Orders** · Derivatives · Charts · Automation · Strategies · NAV. Sequence tracks the trader's workflow — Pulse + Dashboard for the always-open watch surfaces, then Orders for active trading, then the analysis tier (Derivatives + Charts), then operator-frequency-lower surfaces (Automation rule review, Strategies attribution, NAV for LP / fund views).
- **Explore** (always visible): Sandbox — scenario + replay surface for trying ideas without touching real money. The page itself is `/admin/execution` (the URL didn't change — only the navbar label is "Sandbox" now; older bookmarks still work).
- **Build** (dropdown): Console · Research · Tokens. Surfaces you reach to extend the platform (custom commands, LLM threads, the agent DSL grammar editor).
- **Config** (dropdown): Brokers · Settings · Users · Statements · History · Audit · Health. Admin-side surfaces ordered by edit frequency — Brokers most-touched, Health glance-only.

If a page feels missing — first check the dropdowns (Build and Config collapse). Mobile drawer shows every group with a caption header so you can scan by intent rather than scroll a flat list.

Older builds called the Sandbox page "Lab" and the group "Modes." The labels were renamed to match what every other quant platform (QuantConnect / Streak / Sensibull) calls these surfaces — clearer for first-time visitors. The URL `/admin/execution` is unchanged.

---

## Why every signed-in role can reach the algo surfaces now

Earlier the platform redirected any signed-in user who wasn't an admin or "designated" tier back to the sign-in page the moment they hit an algo URL. That was holdover behavior from the old two-tier model — admin (operator) and partner (LP) — before the platform grew the five-role surface (designated / trader / risk / admin / partner). Today a trader or risk officer signing in can navigate every algo page; specific surfaces still check the user's role before showing data (a trader doesn't see audit logs, a risk officer doesn't see broker credentials), but the navigation itself doesn't bounce.

This fix also unblocked the **Tour** — clicking any showcase link as a logged-in non-admin used to drop the visitor back at /signin. It just opens the surface now.

Same fix on the **History page**: the page used to briefly render "Access denied" until the role-bootstrap completed, then never actually load even after the role was resolved. The data loads as soon as the role check passes.

## Snappier broker postbacks for Dhan accounts

If you trade through a Dhan account, fills used to take up to 20 seconds to appear in the platform — the chase loop polled Dhan's order book on a 20-second cadence and detected the fill on the next tick. Kite has had a near-instant postback hook for years; Dhan only recently exposed an equivalent webhook URL.

A scaffold route is live at `https://ramboq.com/api/orders/dhan_postback`. Configure that URL in your Dhan partner dashboard's webhook settings for each Dhan account. The first fill after configuration writes the raw payload to `api_log_file` (so the parser can be tuned to Dhan's actual field names), and from then on Dhan fills appear in the platform within roughly a second instead of waiting for the next chase poll.

Same setup will work for Groww when their webhook support is confirmed — route already lives at `/api/orders/groww_postback`.

---

## Why placing an order feels snappier now

If you used the platform before July 2026 you might have noticed an order placement taking ~1 second to register. That was preflight overhead — every ticket fired 4 sequential broker calls (account profile, instruments lookup, basket margin, fund balance) to catch obvious blockers (segment not enabled, qty exceeds freeze, margin shortfall) before sending the real order. Each broker call cost ~200-300ms, so 4 sequential calls = ~800-1200ms of waiting per ticket.

What changed:

- **Preflight runs in parallel.** All four broker calls fire at once via async gather; total wall-time is now max(individual call) ≈ 300ms.
- **Tick-size lookups are now O(1).** Pre-fix the platform scanned the 10-50k-row instruments list twice per order (once for the limit price, once for the trigger price). Now it builds a dict once and reads it as a key lookup — saves ~50ms per ticket.
- **Paper orders skip preflight entirely.** The paper engine already runs basket-margin internally as part of its REJECTED-vs-OPEN gate, so the route-level preflight was duplicate work. Paper placements are ~800ms faster as a result; same correctness — rejections still surface in the order row's detail field within a tick.

Net effect: a LIVE placement is roughly half the latency it used to be; PAPER is even faster. The "snappier feeling" is the difference between a button-click and the order showing up on the Orders page being closer to instant.

**Closing large positions** — close orders bypass all lot-size caps. Whether you're closing a 1-lot position or a 50+ lot position, there's no longer a "Quantity too large" error or a false red margin banner. F&O close orders disregard the 5-lot entry-size guard, the MCX 20-lot cap, and any other position-size limits — you can close any size from the ticket and the order will submit.

---

## Why every screen updates at the same time after a fill

When you place an order and the broker fills it, you want the whole platform to catch up at once — the order book showing the new status, the positions grid showing the new qty, the snapshot grid recomputing its per-underlying totals, the payoff curve and Greeks recomputing for the new leg, the dashboard hero card refreshing P&L, the performance page reconciling holdings and positions. Pre-July 2026 this was an embarrassing **two-step refresh**: the qty cell would patch immediately, but the aggregations downstream waited for the next 5–15 second poll cycle. Net effect: a basket-order fill produced a flickery, "settling" UI that looked like it had a bug.

The fix is a single coordinated **book_changed** event the backend now fires on every terminal postback (COMPLETE, CANCELLED, REJECTED, EXPIRED). Every algo page that displays position-derived data — Pulse, Dashboard, Derivatives, Orders, Performance — listens for this event and refetches its primary data feed in lockstep. A 200ms debounce coalesces basket-order bursts so 4 leg fills produce one refresh, not four.

You don't have to do anything to opt into this. As long as the operator's browser tab is open to any algo page, the WebSocket connection is live and a fill propagates everywhere within roughly a second. If a page ever feels stale, hit the page-header Refresh button — that fires every loader the page owns regardless of the bus state.

---

## History — past orders, trades, and ledger

Three things you'll want to look up after a trading day are usually:

- **"What orders did I place last week?"** — broker order books only show today. RamboQuant keeps every order it placed (via the platform, via an agent, via a manual ticket) indefinitely in its own table — so you can query a date range and see every place / modify / cancel / fill row, across every account, in one place.
- **"What did I actually trade in October?"** — separate from order placement. Trades are the *fills* the broker confirmed. The platform takes a daily snapshot of broker-reported trades at 15:35 IST and writes them to its own history table. As long as the platform was running on that day, the trades are there to scroll through.
- **"How much cash did the account have on Diwali?"** — broker portals show today's balance only; nothing historical. RamboQuant now captures a per-account, per-segment funds snapshot in the same 15:35 IST job so the ledger builds up over time.

All three live behind one page at `/admin/history` (designated / admin / risk roles). Three tabs:

- **Orders** — every order the platform recorded. Filter by date range, account, symbol, status (FILLED / OPEN / REJECTED / CANCELLED / UNFILLED), and mode (live / paper / sim / shadow / replay). The summary row shows a status histogram so you can see "20 filled, 3 rejected, 1 cancelled" at a glance.
- **Trades** — the broker-confirmed fills. Filter by date / account / symbol. Summary shows total notional across the filtered set, computed at the database so it stays accurate regardless of pagination.
- **Funds** — per-account margins ledger. Shows cash available, opening balance, debits today, realised M2M, and net per (date × account × segment). A "tracking started X" chip tells you how far back the data goes — funds capture began June 2026, so historical data builds up over time.

The page is read-only. If you need to take action on something you see (re-place a cancelled order, investigate a rejection), use the Audit log to find the request_id and trace what happened.

**Audit drill on Orders rows** — each Orders row carries an **Audit ↗** link in the last column. Click it and `/admin/audit` opens pre-filtered to that order's request_id, with the since-hours window widened to 90 days so you can find rows older than the default 72h window. You see every audit event tied to the same HTTP request (placement, downstream cache writes, postback fills if they correlate). Older orders placed before this column existed show an em-dash instead — they don't have a request_id stamped at insert time.

**Cashbook Δ on Funds** — the Funds tab now carries a **Δ vs prior** column showing the day-over-day change in your cash available, computed within each (account, segment) series. Green = cash went up, red = cash went down, em-dash = first row in the series (no prior to compare against). The running balance you're tracking IS the Cash avail column; the Δ column makes each day's move explicit.

**Backfill button** — at the top of the Funds tab there's a Backfill row where you type an account code and hit "Pull ledger ↓". This pulls historical ledger entries from the broker and seeds them into the Funds tab for the date range you picked.

- **Dhan accounts**: live. Click Backfill, wait a second or two, and the Funds tab shows however far back Dhan's ledger reaches (typically several years of statement history). Re-running with a wider range overwrites the same days — no duplicates.
- **Kite accounts**: always returns a 501 message. Zerodha doesn't expose ledger data through Kite Connect; you have to download the statement from the Zerodha Console and import it manually. Operator workaround: pull the PDF, extract the rows, INSERT them via psql with `kind='funds'`.
- **Groww accounts**: pending. Same single-file follow-up that just landed for Dhan.

What you actually get from a Dhan backfill: one row per (date, account, segment). Each row's Cash avail comes from Dhan's end-of-day running balance; Δ vs prior is computed off the prior day's row. "Realised M2M" here is best-read as "net daily cash flow" (credits minus debits across all voucher entries that day) — it includes brokerage, STT, DP charges, and actual MTM together. Dhan's ledger doesn't break those apart per row.

**Funds rows are read-only in practice.** A re-run of Backfill over the same date range OVERWRITES the existing rows with the canonical broker-ledger numbers. That's intentional — the voucher-aggregated backfill is more accurate than a single broker.margins() snapshot. But it also means: don't hand-edit a funds row expecting your edit to stick. If a row looks wrong, the fix is to investigate the broker ledger source or wait for the next snapshot to overwrite it.

---

## How the platform knows the market is open

You don't have to think about this — the agent engine and the daily snapshot loops gate themselves on whether the market is currently trading. But the *how* used to be a workaround: ask the broker for a quote on a bellwether index (NIFTY 50, SENSEX, MCX crude futures), check when that symbol last traded, and infer "if the last trade was in the last 15 minutes, the exchange is open." That worked but it spent Kite's quote budget on a question Kite couldn't answer directly.

Dhan and Groww both expose a market-status endpoint — a direct "is this exchange open right now" call. The platform now asks the broker first; only when no broker exposes the data does it fall back to the bellwether-quote probe. So on a multi-broker setup (any account with Dhan or Groww loaded), market-hours gating burns zero Kite quote and gives an authoritative answer instead of a "last traded 4 minutes ago" inference.

There's nothing for you to configure — if a broker exposes `market_status` and you have an account with that broker loaded, it just runs. Kite-only deployments keep working via the bellwether path.

---

## After the market closes — what you're looking at

Markets shut at 15:30 IST (NSE) / 23:30 IST (MCX). After that, your screens keep showing prices — but those are **close snapshots**, not live ticks. Three things change at the boundary:

1. **The live LTP stream pauses.** The page stops listening for tick updates because there are no ticks. Position rows, watchlist rows, and the Day P&L column all freeze at the last value they had when the bell rang.
2. **Position and cash data keep updating.** If you transferred money in after-hours, or your broker books a late realised P&L, the platform still polls funds every 30 minutes and the next refresh picks up the change. Click the refresh button anytime to pull fresh broker data on demand.
3. **The "close price" you see settles ~45 min after close.** Kite (and most brokers) use a weighted-average of the last 30 minutes of trades to publish the official close. The platform automatically takes a second snapshot 45 minutes after each exchange closes to capture the adjusted close. That's the value you'll see as "yesterday's close" the next session, and it's what drives the "Day P&L" math on the morning of the next trading day.

When you click Refresh during closed hours, a small toast appears: "Showing close snapshot — markets reopen at 09:00 IST." That's the platform telling you the broker data still refreshed, but the live LTP column will stay frozen until the next session.

---

## Audit log — what's recorded

Every action that changes state in RamboQuant — placing an order, a broker filling that order, an agent firing, you tweaking a setting, a monthly statement going out to an LP, the daily NAV cron writing a snapshot — lands as one row in an audit log. The log is the platform's memory of "who did what, when, and with what outcome." A SEBI Cat-III audit visit doesn't need a fancy UI; it needs the trail. RamboQuant gives them both.

**Who can see it?** Designated, admin, and risk roles. Trader, partner, and demo don't have access — the audit log is forensic data, not operational data.

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

## Principal profile showcase — your contact surface

If you've enabled the **About** page (`/about`), the principal profile section displays your name, role, and a set of contact / portfolio chips. These chips link to your email, GitHub, LinkedIn, Portfolio, and Resume — all fully visible and highlighted so visitors can easily reach you. Each chip carries a subtle background fill and is keyboard-navigable.

Use this surface to show your credentials and work to anyone visiting the platform (investors, partners, LPs, or just curious traders).

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
