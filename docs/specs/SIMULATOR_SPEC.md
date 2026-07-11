# Simulator Engine Specification

Single source of truth for the real-time market simulator and stress-test framework.
Defines scenario execution, tick generation, GTT lifecycle, and recording/replay mechanics.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/algo/sim/driver.py` · `backend/api/routes/simulator.py` · `backend/api/algo/sim/gtt_book.py` · `backend/api/algo/sim/scenarios.yaml`

---

## Contents

1. [SimDriver Singleton](#1-simdriver-singleton)
2. [Scenario Types](#2-scenario-types)
3. [Tick Generation and Evolution](#3-tick-generation-and-evolution)
4. [GTT Book in Simulation](#4-gtt-book-in-simulation)
5. [Recording and Playback](#5-recording-and-playback)
6. [Market-State Presets](#6-market-state-presets)
7. [Spread Model](#7-spread-model)
8. [Lifecycle and Auto-Stop](#8-lifecycle-and-auto-stop)
9. [Execution Modes and Tagging](#9-execution-modes-and-tagging)
10. [Edge Cases](#10-edge-cases)
11. [Test Coverage Map](#11-test-coverage-map)

---

## 1. SimDriver Singleton

SimDriver is a module-level singleton — only one active simulation per process at a time.
Concurrent sims would race for shared state (`_sim_alert_state`, `_tick_log`, `_gtt_book`).

### Architecture

| Component | Purpose |
|---|---|
| `_quote_source` | `SimQuoteSource` generating bid/ask each tick |
| `_paper` | `PaperTradeEngine` executing paper orders against sim prices |
| `_tick_log` | Deque of recent ticks (up to `TICK_LOG_LIMIT=200` entries) |
| `_gtt_book` | `SimGttBook` tracking GTT placements and triggers |
| `_price_history` | Per-symbol rolling deque of `(t, bid, mid, ask)` |
| `_underlying_history` | Spot history for F&O chart overlays |
| `_recording_events` | Buffer of state mutations during recording mode |

### Lifecycle methods

- `start(scenario_name, rate_ms, ...)` — Load scenario, initialize state, spawn tick loop
- `stop()` — Cancel tick task, finalize recording if active
- `step()` — Apply exactly one tick (manual debugging)
- `reset()` — Wipe all state, clear tick_log, close paper orders
- `snapshot()` — Return current driver state for status endpoint

---

## 2. Scenario Types

### Six stress-move scenarios (book-wide)

| Scenario | Move Pattern | Use |
|---|---|---|
| `generic-crash` | Uniform crash (e.g., -2% per tick) | Market-wide selloff |
| `generic-euphoria` | Uniform rally (e.g., +2% per tick) | Market-wide rally |
| `extreme-crash` | Larger negative moves (e.g., -5%) | Tail-risk stress |
| `extreme-euphoria` | Larger positive moves (e.g., +5%) | Euphoria tail |
| `wild-swings` | Alternating large up/down moves | Whipsaw regime |
| `random-walk` | Brownian motion per position (drift + σ) | Natural market behavior |

### Seeded position-level scenarios

- **Per-position random walk** — Each position moves independently via its own drift + vol
- **Per-underlying random walk** — Underlying (NIFTY, BANKNIFTY) drifts; positions track delta

### Auto-synthesized scenarios from agent trees

When operator selects `run_scenario_for_agent`, the system synthesizes a scenario
from the agent's condition tree:
- Derives stressed symbol set and move direction from the condition
- Generates ticks that would trigger the agent's condition
- Records the run so operator can replay and debug

---

## 3. Tick Generation and Evolution

### Per-tick lifecycle

1. **Price generation** — `_tick_generator()` yields a new move (pct, scenario-driven)
2. **Position update** — `_mutate_positions(pct)` updates `last_price` on matching symbols
3. **PnL recompute** — Backend automatically recalculates `pnl` from new LTP
4. **GTT check** — `_gtt_book.check_triggers()` walks GTT orders, fires those within new bid/ask
5. **Paper fills** — `_paper.step()` checks open orders against new bid/ask, produces fills
6. **Recording** — If `_recording_active`, append event to `_recording_events`
7. **Broadcast** — Push tick_log snapshot to WebSocket so UI refreshes

### Tick rate control

Operator specifies `rate_ms` (default 2000 ms = 0.5 Hz). Tick loop sleeps `(rate_ms / speed)`
so speed multiplier compresses or stretches the timeline without changing tick content.

---

## 4. GTT Book in Simulation

### SimGttBook state machine

| State | Condition | Transitions |
|---|---|---|
| **Placed** | GTT registered | → Triggered (cross), Cancelled |
| **Triggered** | Price crossed trigger | → Executed (immediate order place) or Expired |
| **Cancelled** | Operator cancels | terminal |
| **Expired** | Validity window passed | terminal |

### Per-tick crossing check

On each tick:
1. Fetch new bid/ask from `_quote_source`
2. Walk all `SimGttBook.placed_orders` (list of GTT rows)
3. Check if `trigger_price` is within [bid, ask]
4. If crossed: move to `triggered` state, record event to tick_log

### Event recording

GTT state changes are written to `_tick_log` as structured events:
```json
{"kind": "gtt_placed", "payload": {"gtt_order_id": 123, "symbol": "NIFTY24500CE"}}
{"kind": "gtt_triggered", "payload": {"gtt_order_id": 123, "timestamp": "…"}}
{"kind": "gtt_cancelled", "payload": {"gtt_order_id": 123}}
```

### No re-trigger

Once triggered, the GTT order is moved to a separate set. Subsequent ticks never re-trigger
the same order — idempotency guarded by state machine, not price re-evaluation.

---

## 5. Recording and Playback

### Recording mode

When `record_mode=True` on `start()`:
- Each tick appends a dict to `_recording_events`: `{t, kind, payload}`
- On `stop()`, events are serialized to `SimRecording` table (JSON payload)
- Operator can later replay the recording via `SimReplayDriver`

### Replay driver

Separate singleton `SimReplayDriver` reuses SimDriver's surface state:
- Loads events from `SimRecording` row
- Wipes display state on start
- Plays events at user-controlled speed (0.5x to 10.0x)
- Pause/step available for frame-by-frame debugging

### Event projection

Replay applies each event to SimDriver's state as if the original sim were re-running:
- `gtt_placed` → calls `_gtt_book.place()`
- `gtt_cancelled` → calls `_gtt_book.cancel()`
- `gtt_triggered` → flips GTT state (no re-fire logic)
- Tick events → regenerate tick_log entries with original payloads

### Snapshot sync

During replay, `SimDriver.snapshot()` returns the current playhead state, so the
UI's status panel and Simulator tab show playback progress without any code divergence.

---

## 6. Market-State Presets

Seven presets override the clock for time-aware agents:

| Preset | NSE | MCX | min_since_NSE | min_since_MCX | Expiry |
|---|---|---|---|---|---|
| `pre_open` | Closed | Closed | 0 | 0 | No |
| `at_open` | Open | Open | 1 | 1 | No |
| `mid_session` | Open | Open | 180 | 180 | No |
| `pre_close` | Open | Open | 360 | 360 | No |
| `at_close` | Closed | Open | 375 | 375 | No |
| `post_close` | Closed | Closed | 375 | 375 | No |
| `expiry_day` | Open | Open | 240 | 240 | Yes |

Operator specifies `market_state_preset` on `start()`, overriding the scenario's preset.
Individual fields can also override the preset (e.g., "at_close but set is_expiry_day=False").

---

## 7. Spread Model

### Bid/ask derivation

On each tick, `_quote_source` computes:
```
mid = last_price (from positions)
spread_pct = configurable, default reads DB setting
bid = mid × (1 - spread_pct / 2)
ask = mid × (1 + spread_pct / 2)
```

### Spread configuration

- **Per-run override** — `spread_pct` param on `start()` (e.g., 0.10 = 10 bps = 0.10% total)
- **DB default** — Falls back to `simulator.default_spread_pct` in settings
- **Per-symbol override** — Not yet supported (future enhancement)

### Paper-fill logic

Paper orders fill at mid if limit is wider than spread, or unfilled if limit is tighter.
GTTs trigger when trigger_price crosses the bid/ask band.

---

## 8. Lifecycle and Auto-Stop

### Auto-stop window

Sim runs for a maximum duration (default 30 min, tunable via `simulator.auto_stop_minutes`).
After the window expires:
1. Tick loop cancels
2. All open paper orders marked UNFILLED
3. Recording finalizes (if active)
4. Operator must explicitly `start()` again for a new sim

### Positions cadence

On every N ticks, the engine calls `_fetch_positions_direct()` to sync broker positions.
Operator specifies `positions_every_n_ticks` (default 1 = every tick), configurable to
reduce broker load during high-frequency sim ticks.

### Custom position injection

Operator can append `custom_positions` at `start()` time — synthetic positions layered
on top of seeded positions. Allows stress-testing single symbols without seeding a full book.

---

## 9. Execution Modes and Tagging

### Mode flag

SimDriver sets `sim_mode=True` in `alert_state` so downstream consumers tag alerts.

### Alert prefixes

- **Telegram/Email subjects** — Append `[SIM]` prefix to distinguish from live/paper
- **Logs** — `[SIMULATOR]` prefix on every log line from the driver
- **AlgoOrder.mode** — Set to `'sim'` for all orders placed during simulator

### Palette and UI

- **Navbar pill** — SIM (green, always visible when active)
- **Banner alert** — "Simulator running" in system message area
- **Order grid tags** — SIM mode visible in orders tab

---

## 10. Edge Cases

### Sim without positions

If seeded positions list is empty (e.g., operator chooses scripted scenario but account has no open positions):
- Tick generator still produces moves
- No positions to update; PnL stays at 0
- GTT book may be pre-seeded (if scenario includes GTT initial), or empty
- Agent engine sees empty positions, empty holdings — some agents fire anyway (e.g., `negative_funds`)

### Auto-stop reached mid-tick

When the 30-min window expires mid-tick:
- Current tick completes normally
- Next tick is rejected
- `active` flag set to False
- UI shows "auto-stop reached" message

### Recording overflow

Recorded events are capped (no hardcoded limit in driver, but DB storage is finite).
If recording grows unbounded, the operator should stop and clear via `POST /api/simulator/clear`.

### GTT double-trigger

Same GTT order can NOT trigger twice (state machine prevents it). If price whipsaws
back through the trigger, the GTT stays in triggered state and does not re-fire.

---

## 11. Test Coverage Map

### Backend — core

- **Scenario load** — YAML parsing, preset override, symbol filtering
- **Tick generation** — Percent moves, bid/ask spread derivation, price history trimming
- **GTT lifecycle** — place, trigger, cancel state transitions; idempotent no re-trigger
- **Paper fill logic** — Limit-price crossing, attempt bumping, UNFILLED on max-attempts
- **Recording events** — Correct event shapes, relative timestamps, round-trip deserialization
- **Market state** — Presets correctly set min_since_open, is_expiry_day, segment flags

### Backend — integration

- **Mode flag** — `sim_mode=True` propagated through alert dispatch
- **Alert tagging** — [SIM] prefix on Telegram + Email subjects
- **Auto-stop** — Timer fires at configured minutes, cancels tick loop
- **Position updates** — `positions_every_n_ticks` batching honored

### Frontend

- **Simulator tab** — Tick log displays in real-time, tick payloads shown
- **Chart overlays** — Price history and underlying history rendered on chart
- **Paper order grid** — SIM mode visible, fills/unfilled states shown
- **Status endpoint** — `/api/simulator/status` reflects active / tick_count / cursor

### Gaps

- Missing: multi-symbol correlated moves (all symbols move same %, hard for stress)
- Missing: intraday order flow (GTT book initialization from scenarios.yaml)
- Missing: position-level greeks updates during sim (Greek values stale)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
