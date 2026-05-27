# Simulator — Operator Test Guide

Hands-on walkthrough of every Lab / Simulator path. Pair with [AGENTS_GUIDE.md](AGENTS_GUIDE.md) for the agent-authoring side, [ADMIN_GUIDE.md](ADMIN_GUIDE.md) for routine ops.

---

## TL;DR — what the simulator does

The simulator feeds **fabricated per-symbol price moves** into the **same agent engine** the real pipeline uses. Alerts fire, actions execute, events log — exactly as on real ticks, but with `sim_mode=True` tagged everywhere downstream. Operator's real broker book is never touched.

Why it's the canonical pre-prod test surface:

- Same `run_cycle` → same evaluator → same dispatcher → same action handlers
- Telegram + email surface alerts with a `SIMULATOR` prefix so they don't pollute live ops
- Paper trades land with `mode='sim'` + `SIM` pill in order logs
- Schedule / cooldown / baseline / suppression gates all **bypassed** so the agent fires on the first tick — no waiting

Use cases:
- **Per-agent dry-fire** — click _Run in Simulator_ on `/agents`, see the alert fire
- **Stress test the whole book** — feed a `generic-crash` scenario, watch every guardrail fire in sequence
- **Validate auto-close logic** — confirm the chase engine closes the right positions when an agent ticks

---

## Where everything lives

| Surface | URL | What it does |
|---|---|---|
| Sim workspace | `/admin/execution?mode=sim` | Controls + monitoring cards |
| Per-agent test | `/agents` → **Run in Simulator** button on each row | Synthesises a scenario for that agent + arms it + lands you on the workspace |
| Iteration history | `/admin/simulator/iterations` | List of completed sim runs (saved as `SimIteration` rows) |
| Iteration detail | `/admin/simulator/iterations/<slug>` | Replay one run + see its event log |

---

## Anatomy of a sim run

1. **Seed** — operator picks where the initial positions / margins come from
2. **Ticks** — driver advances one fabricated price-move event at a time
3. **Evaluation** — the agent engine runs against the mutated book at each tick
4. **Dispatch** — alerts fire with `sim_mode=True` tag
5. **Stop** — operator clicks Stop, or driver auto-stops after `simulator.auto_stop_minutes` (default 30)

---

## The Lab page anatomy

After the Phase post-24 redesign, the page flows top-to-bottom:

```
[ Iteration mode card ]      ← collapsible, hidden when collapsed
[ Run controls card ]        ← Scenario picker · Symbol filter · Spread · Spot · Start / Stop / Step / Reset
[ Custom positions card ]    ← collapsible, for ad-hoc seeding
─────────────────────────────────────
[ Indices card ]             ← underlying spot pills
[ Live Activity card ]       ← agent fires + sim orders, newest first
[ Underlyings card ]         ← per-underlying spot charts
[ Positions Summary ]   [ Holdings Summary ]    ← side-by-side on wide viewports
[ Past Simulations card ]    ← last N completed runs
```

Every monitoring card spans the full page width on wide viewports; controls collapse to a thin strip on top.

---

## Seeding — three options

`seed_mode` on the Start request decides what positions land in the sim driver at t=0:

| Mode | What you get | When to use |
|---|---|---|
| `scripted` | Scenario's `initial:` block (fails loudly if scenario has none) | Repeatable canned tests — same starting book every time |
| `live` | Fresh broker fetch — your actual positions, margins (holdings skipped, sim is positions-only) | "What would my real book do under a crash?" |
| `live+scenario` | Live book first, then scripted `initial:` rows layered on top | Stress-test real book with extra synthetic exposure |

Operator path: click **Load live book** in the Run controls card, then pick `Live` or `Live + scenario` from the Seed dropdown, then Start.

---

## Move primitives — what scenarios can do to prices

Each scenario tick contains one or more `moves`. The driver supports six primitives:

| Primitive | Effect |
|---|---|
| `pct {scope, value}` | LTP × (1 + value). `value: -0.03` = -3% drop |
| `abs {scope, value}` | LTP + value (₹). Per-share. |
| `random_walk {scope, drift, vol}` | `LTP ← LTP × (1 + drift + vol·N(0,1))`. Seed-deterministic when `scenario.seed:` is set |
| `target_pnl {scope, value}` | Solves `ΔLTP × Σqty = target − currentPnl` to drive the scope to a target P&L |
| `set_margin {scope, fields}` | Price-decoupled — set specific margin columns. Doesn't touch LTP |
| `underlying_pct / underlying_abs / underlying_target {scope, value}` | Move an UNDERLYING's spot. Every derivative on that underlying then re-prices coherently via Black-Scholes (Phase 24+) |

### Scope glob

`section.account.tradingsymbol` with `*` (single-segment) and `**` (any-remaining):

```yaml
positions.**                    # every position row
positions.ZG*.*                 # any ZG account, any symbol
positions.*.NIFTY*              # any account, NIFTY-prefixed symbol
positions.ZG####.NIFTY25APRFUT  # exact symbol in masked account
underlying.NIFTY                # NIFTY spot
underlying.*                    # every underlying
margins.ZG####                  # masked-account margins row
```

`holdings.*` globs are silently ignored — sim is positions-only by design.

---

## Shipped scenarios

Five canned scenarios live in `backend/api/algo/sim/scenarios.yaml`:

| Slug | Effect | Notes |
|---|---|---|
| `generic-crash` | -3% LTP over 3 ticks | Default book-wide stress |
| `generic-euphoria` | +3% LTP over 3 ticks | Profit-bleed-through tests |
| `extreme-crash` | -19% LTP over 3 ticks | Black-swan |
| `extreme-euphoria` | +19% LTP over 3 ticks | Mirror |
| `random-walk` | Seeded GBM | Tests stdev / max_drawdown rolling metrics |
| `nifty-down-3pct` | -3% on NIFTY underlying over 3 ticks | Coherent F&O re-pricing |
| `nifty-up-3pct` | +3% on NIFTY underlying over 3 ticks | |

All work against any seeded book (`seed_mode: live` recommended for the underlying-move scenarios).

---

## Market-state presets — making the engine think time is different

Time-aware agents (rate metrics with baseline gates, `minutes_until_close`, expiry rules) need a simulated clock — at 03:00 AM IST wall-clock, every market segment is closed. Each scenario + each Start request can declare a `market_state` block:

```yaml
market_state: {preset: pre_close}                      # NSE closes in 30 min
market_state: {preset: expiry_day, is_expiry_day: true}
market_state:                                          # explicit overrides
  nse_open: true
  mcx_open: false
  minutes_since_nse_open: 360
```

Seven presets shipped in `MARKET_STATE_PRESETS` (driver.py):

`pre_open` · `at_open` · `mid_session` (default) · `pre_close` · `at_close` · `post_close` · `expiry_day`.

`run_cycle` merges overrides on top of live values. Real path passes None and behaviour is unchanged.

---

## Run in Simulator — the operator-friendly path

Don't write a scenario yaml. Just click **Run in Simulator** on the agent row at `/agents`:

1. Backend calls `synthesize_for_agent(agent)` which walks the condition tree, picks the "nearest-to-fire" leaf, and maps it to a ticks-shape
2. Returns an inline scenario dict (no yaml entry created)
3. `SimDriver.start(inline_scenario=…)` accepts it directly
4. Drops you on `/admin/execution?mode=sim&agent_id=<id>` with the agent armed
5. The engine bypasses cooldown / baseline / schedule gates for `only_agent_ids=[<id>]` runs — your agent fires on the first tick

Synthesiser support matrix:

| metric family | Synthesised? | How |
|---|---|---|
| `pnl` | ✓ | `target_pnl` driving the scope to the threshold |
| `pnl_pct` | ✓ | `target_pnl` sized to `value% × util_margin` |
| `pnl_rate_abs` / `pnl_rate_pct` | ✓ | Scheduled `target_pnl` decay over the rate window |
| `cash` | ✓ | `set_margin` driving `avail opening_balance < 0` |
| `avail_margin` | ✓ | `set_margin` driving `net < 0` |
| Rolling-window (drawdown / mean / stdev / range) | ✓ | climb-then-crash for drawdown; flat hold for mean; oscillate for stdev/range |
| Expiry (`days_until_expiry`, `is_itm`, `is_ntm`) | partial | The scenario seeds an expiring contract; ITM/NTM checks require spot |
| Holdings metrics (`day_pct`, `day_rate_abs`, `day_rate_pct`) | ✗ — returns 400 | Holdings aren't simulated; validate against live data instead |

---

## Custom positions — ad-hoc seeding

The Run controls card has a **Custom positions** sub-section. Fill rows inline:

| Column | Notes |
|---|---|
| Account | Masked code (ZG####) — case-insensitive |
| Symbol | Tradingsymbol (NIFTY25APR22000CE, RELIANCE, CRUDEOIL26JUNFUT…). Auto-uppercases |
| Qty | Positive = long, negative = short. Lots, not contracts |
| LTP | Starting last-price. Average price defaults to LTP if unset |

`POST /api/simulator/start` accepts `custom_positions: list[dict]`. Driver normalises (uppercases symbols, infers exchange — NFO for parseable F&O, NSE otherwise), then appends to whatever scripted/live seed produced.

Rows land BEFORE `_seed_derivatives` runs — so synthetic NIFTY/BANKNIFTY/etc. options pick up underlying spots + IV calibration the same way real positions do.

---

## Iteration mode — multi-iteration sweeps

The collapsed **Iteration mode** card hides an N-iteration sweep with cross-scenario / cross-regime variation:

| Field | Purpose |
|---|---|
| Iterations | How many times to run (N) |
| Regimes | List of scenario slugs to round-robin (or one if you want every iteration identical) |
| Seed | Deterministic re-runs — same seed = same fills, same draws |
| Correlation table | Pair of (source, target, beta) tuples — when scenario fires `underlying_pct` on source, target also moves at `β × primary_delta`. Single-hop only |

Each iteration writes a `SimIteration` row visible on `/admin/simulator/iterations`. Click any row to replay with the same seed + parameters — perfect for "show me what happened when ATR_5_drop kicked in across 100 NIFTY paths".

---

## Spread + chase engine — paper trades during sim

When an agent fires during a sim, paper-trade actions land as `AlgoOrder` rows tagged `mode='sim'`. Every position has a derived bid/ask from `spread_pct` (default 0.10%, tunable per Start). The chase engine ticks alongside the price driver:

- **Fills** when bid/ask crosses the limit. Position removed from `_positions_rows`, AlgoOrder flips to `FILLED` with slippage logged
- **Modifies** the limit at the current opposite side, bumping `attempts`. Capped at `simulator.chase_max_attempts` (default 5)
- **Auto-stops** the sim when `_positions_rows` is empty + no orders remain OPEN

The status snapshot carries `positions[]` (current book) + `open_order_details[]` (in-flight chases). The Lab panel renders both as pill strips so you watch the book shrink and the chase re-quote live.

---

## Sim_mode = True — what changes downstream

| Surface | Tag |
|---|---|
| Telegram message | `SIMULATOR` prefix + red `⚠ SIMULATOR RUN — fabricated market data` line |
| Email subject | `RamboQuant SIMULATOR Agent: …` |
| Email body | Red banner identical to Telegram |
| `agent_events.sim_mode` column | `TRUE` |
| `algo_orders.mode` column | `'sim'` + `engine='sim'` |
| Log lines | `[SIM]` prefix (shorter than the user-facing `SIMULATOR`) |
| WebSocket `agent_alert` payload | `sim_mode: true` |

The shared dispatcher writes both real + sim events to the same tables. `/agents/activity` auto-scopes (real events when no sim is running, sim events when one is).

---

## Common test workflows

### 1. Validate a new loss-* agent against the current book

1. Author agent on `/agents` with `status: inactive`
2. Click **Validate** → fix typos
3. Click **Dry-run** → confirm the conditions evaluate against right-now state (sometimes shows `would_fire: true` which is fine in dry-run, it doesn't actually fire)
4. Click **Run in Simulator** → synthesised scenario trips the agent within 3-5 ticks
5. Watch Telegram for `SIMULATOR` alert ✓
6. Open `/agents/activity` (auto-scoped to sim) → see the row land
7. Flip `status: active` on `/agents`

### 2. Stress-test the whole book with a real-data crash

1. Open `/admin/execution?mode=sim`
2. Click **Load live book**
3. Seed mode: **Live**
4. Scenario: `generic-crash` or `extreme-crash`
5. Start
6. Watch which agents fire in sequence as the book bleeds
7. Spot anything that should have fired but didn't → tweak the agent, re-run

### 3. Test an expiry-day auto-close (DESTRUCTIVE — sim only)

1. Add a custom position with an expiring-today symbol (e.g. `NIFTY26APR22000CE`)
2. Seed mode: **Custom + scenario**
3. Scenario: any (the expiry agent triggers on `days_until_expiry`, not price)
4. Market state preset: `expiry_day` + `is_expiry_day: true`
5. Start
6. Watch `expiry-day-itm-auto-close` agent fire (if you've activated it) — chase orders should land for every ITM contract

### 4. Stress-test a single agent without writing yaml

1. `/agents` → click **Run in Simulator** on the agent row
2. You'll auto-land on the workspace with the scenario synthesised
3. Click Start
4. Agent fires on first eligible tick

---

## Simulator API — `/api/simulator/*`

| Route | Purpose |
|---|---|
| `GET /scenarios` | List available scenarios (slug · name · mode · has_initial · tick count) |
| `GET /status` | Driver snapshot — active, scenario, seed_mode, tick_index, positions, open_order_details, spread_pct |
| `POST /start` | Body: `{scenario, rate_ms, seed_mode, agent_ids?, positions_every_n_ticks?, market_state_preset?, pct_overrides?, symbols?, spread_pct?, custom_positions?}` |
| `POST /start-for-agent/{id}` | Synthesise + start in one call (no yaml entry required) |
| `POST /stop` | Halt |
| `POST /step` | Apply one tick (deterministic debugging) |
| `POST /seed-live` | Snapshot live positions + margins (holdings skipped) |
| `POST /run-cycle` | Immediately run the agent engine against current sim state (skip the tick scheduling) |
| `POST /clear` | Delete every `sim_mode=True` row from `agent_events` + `algo_orders` |
| `GET /events/recent?limit=N` | Recent sim-tagged agent events |
| `GET /orders/recent?limit=N` | Recent `mode='sim'` algo orders |
| `GET /ticks/recent?limit=N` | Rolling driver tick log (oldest-first, per-symbol diffs) |

All admin-guarded; demo sessions on prod see 403.

---

## What sim does NOT do

- **Holdings re-pricing** — sim is positions-only by design. Holdings metrics (day_pct, day_rate_abs, etc.) require live data
- **Real broker calls** — even `place_order` / `cancel_order` actions write paper AlgoOrder rows tagged sim; the Kite SDK is never invoked
- **Real margin math** — `set_margin` directly mutates the margin frame; the engine never asks Kite for span/elm/etc.
- **Cross-account margin pooling** — each account's margin is independent (matches real Kite behaviour, not the rebalance fantasy)
- **Order book depth realism** — depth is approximated from `spread_pct` only; deep book information (10-level depth, queue priority) isn't simulated

For each of these, the test path is:
- Holdings → real data + dry-run; or live activation in a quiet session
- Real broker errors → SHADOW mode (prod only); records exact Kite payload + validates via `basket_margin` without executing
- Deep margin behaviour → preflight check via `/api/orders/preflight`

---

## Auto-stop + cleanup

- **Auto-stop** — after `simulator.auto_stop_minutes` (default 30), the driver stops itself. Prevents a forgotten sim from bleeding through.
- **Clear** — POST `/api/simulator/clear` deletes every `sim_mode=True` agent_events row + every `mode='sim'` algo_orders row. Use before a fresh test session if you want a clean event log.
- **Past simulations** card on the Lab page lists the last 5 + a link to `/admin/simulator/iterations` for the full history.

---

## Troubleshooting

| Symptom | Likely cause | Where to look |
|---|---|---|
| `/api/simulator/status` returns `enabled: false` | `cap_in_<branch>.simulator` flag is off in `backend_config.yaml` | Server config; flag toggles per branch (dev / main) |
| Sim won't start with `seed_mode: scripted` | The scenario has no `initial:` block | Pick `live` or `live+scenario`, or load a scenario that ships an `initial:` block |
| Agent doesn't fire even on **Run in Simulator** | Holdings-metric agent (day_pct, day_rate_abs, etc.) — not synthesisable | Backend returns 400 explaining this. Test against live data instead |
| Telegram pings during sim DON'T have `SIMULATOR` prefix | `sim_mode` not flowing through — engine bug | File an issue; should never happen |
| Order log shows real orders during sim | A separate operator pressed Submit on `/admin/execution?mode=live` while the sim was running. Sim and real coexist; sim doesn't lock you out of real trading | Stop the manual order; sim continues normally |
| Live `/performance` shows stale data while sim is running | This is correct — the live `_task_performance` task keeps fetching real Kite data; only the agent engine's `run_cycle` is skipped during a sim. Real performance refresh continues | Watch `/dashboard` for the fresh real-data ticks |

---

## See also

- [AGENTS_GUIDE.md](AGENTS_GUIDE.md) — agent authoring + testing ladder
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) — exact button labels, API endpoints, scenario YAML
- [USER_GUIDE.md](USER_GUIDE.md) — concepts in plain English
- [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md) — LLM-driven agent authoring + sim from Claude Code
- [CLAUDE.md § Simulator](CLAUDE.md) — architectural reference for engineers
