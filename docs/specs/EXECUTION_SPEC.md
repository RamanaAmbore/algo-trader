# Execution Modes Specification

Single source of truth for the five-mode execution ladder from simulation through live trading.
Defines mode resolution, state transitions, and platform-wide behavior contracts across all modes.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/routes/execution.py` · `backend/api/algo/paper.py` · `backend/api/algo/sim/driver.py` · `backend/api/algo/agent_engine.py`

---

## Contents

1. [Five Execution Modes](#1-five-execution-modes)
2. [Mode Resolution](#2-mode-resolution)
3. [Branch Gate](#3-branch-gate)
4. [Mode Indicator](#4-mode-indicator)
5. [Paper Trade Engine](#5-paper-trade-engine)
6. [Alert Prefixing](#6-alert-prefixing)
7. [Order Tagging](#7-order-tagging)
8. [Chase Loop and Fill Logic](#8-chase-loop-and-fill-logic)
9. [Mode Transitions](#9-mode-transitions)
10. [Shadow Mode Details](#10-shadow-mode-details)
11. [Test Coverage Map](#11-test-coverage-map)

---

## 1. Five Execution Modes

### Mode definitions

| Mode | Quotes | Engine | Broker Call | Branch | Order Visible |
|---|---|---|---|---|---|
| **1-Simulator** | Fabricated | SimQuoteSource (PaperTradeEngine) | No | Both | Yes (mode=sim) |
| **2-Paper** | Live (5s) | LiveQuoteSource (PaperTradeEngine) | No | Both | Yes (mode=paper) |
| **3-Live** | Live | Broker (real gateway) | Yes | Prod only | Yes (mode=live) |
| **4-Replay** | Historical OHLCV | SimQuoteSource (replay playhead) | No | Both | No (read-only) |
| **5-Shadow** | Live | Broker (validation only) | Validate only | Prod only | Yes (mode=shadow) |

### Behavioral matrix

| Mode | Risk | Latency | Determinism | Debug |
|---|---|---|---|---|
| Simulator | None | ~2s/tick | High (seed reproducible) | Excellent (step, record) |
| Paper | None | ~5s | Medium (live data) | Good (chase log) |
| Live | Full | ~100ms | Low (broker latency) | Fair (postback log) |
| Replay | None | User-controlled | Perfect (deterministic) | Excellent (step, scrub) |
| Shadow | None | ~100ms | Low (broker latency) | Good (validation log) |

---

## 2. Mode Resolution

### _resolve_mode() priority ladder

The backend computes the effective execution mode in this order:

```
1. if sim_driver.active:
     return "sim"
2. else if replay_driver.active:
     return "replay"
3. else if not is_prod_branch():
     # Non-main branch: force paper regardless of DB flags
     return "paper"
4. else if execution.shadow_mode (DB setting):
     return "shadow"
5. else if execution.paper_trading_mode (DB setting):
     return "paper"
6. else:
     return "live"
```

**Key**: SIM/REPLAY trump all other checks (transient states). Non-prod branch forces PAPER
even if DB says LIVE. On prod, DB flags control SHADOW/PAPER/LIVE.

### Per-agent override

Individual agents carry their own `trade_mode` field (optional). When evaluating an agent:
- If `trade_mode` is set, use it for that agent's actions
- Otherwise, use the global mode from `_resolve_mode()`

Example: A loss agent might hardcode `trade_mode='paper'` so it never executes live, even
if the global mode is LIVE.

---

## 3. Branch Gate

### Non-main branch behavior

On any branch other than `main` (dev branches, feature branches):
- **Force PAPER mode** regardless of DB `execution.paper_trading_mode` or `execution.shadow_mode`
- **Dev-only flag** — `cap_in_dev.execution.allow_live` must be explicitly True to override (default False)
- **Rationale** — Prevent accidental live orders on a dev branch

### Prod branch (`main`)

Only `main` branch can reach LIVE or SHADOW modes. DB flags determine the effective mode.

---

## 4. Mode Indicator

### Navbar pill (execution mode chip)

Persistent modes (LIVE/PAPER/SHADOW) show in navbar dropdown:

| Mode | Pill Color | Label |
|---|---|---|
| idle | Gray | IDLE |
| paper | Sky-blue | PAPER |
| live | Red | LIVE |
| shadow | Orange | SHADOW |

Transient modes (SIM/REPLAY) show as read-only badges in the navbar banner area (never in dropdown).

### Badge styling

- **SIM badge** — Green, "SIMULATOR RUNNING"
- **REPLAY badge** — Green, "REPLAY RUNNING"
- **Transient badges are read-only** — Clicking opens the driver control panel (/admin/execution)

### Mode transitions animation

When mode changes mid-session, the pill color animates (fade 300ms) to the new mode.

---

## 5. Paper Trade Engine

### Dual-feed architecture

`PaperTradeEngine` is a generic fill engine fed by any `QuoteSource`:

- **Mode 1 (SIM)** — Receives `SimQuoteSource` (fabricated ticks from SimDriver)
- **Mode 2 (PAPER)** — Receives `LiveQuoteSource` (5s interval, live broker data)
- **Mode 3/5 (LIVE/SHADOW)** — No PaperTradeEngine involved (direct broker call)

### Chase loop

Every 30 seconds during market hours, a background task calls `engine.step()`:

1. Prefetch quotes for all open paper orders (bid/ask from QuoteSource)
2. Walk open orders, check if limit crossed
3. For each filled order: update status → FILLED, write final event
4. For each unfilled after max_attempts: update status → UNFILLED, write error event
5. Broadcast WebSocket update so UI refreshes order grid

### Open-order state

Each paper order carries:
- `status` — OPEN | FILLED | UNFILLED
- `attempts` — count of fill attempts (incremented per chase)
- `detail` — human-readable attempt log (e.g., "Attempt 3/5: bid=199.2 < limit=199.8")

### Recovery from broker outage

If a paper order's symbol loses quotes (broker temporarily down), the chase loop retries
on subsequent cycles. No order loss — recovery is automatic when quotes return.

---

## 6. Alert Prefixing

### Message subjects and prefixes

When an agent fires, alert messages are tagged with the execution mode:

| Mode | Telegram Subject | Email Subject | Log Prefix |
|---|---|---|---|
| Simulator | `[SIM]` appended | `[SIM]` appended | `[SIMULATOR]` |
| Paper | `[PAPER]` appended | `[PAPER]` appended | `[PAPER]` |
| Live | No prefix | No prefix | `[LIVE]` |
| Replay | N/A (replay is read-only) | N/A | `[REPLAY:<label>]` |
| Shadow | `[SHADOW]` appended | `[SHADOW]` appended | `[SHADOW]` |

### Example messages

**Simulator**:
```
Telegram: "RamboQuant Agent: loss-aggregate [SIM]"
```

**Paper**:
```
Telegram: "RamboQuant Agent: loss-positions [PAPER]"
```

**Live**:
```
Telegram: "RamboQuant Agent: loss-aggregate"
```

---

## 7. Order Tagging

### AlgoOrder.mode field

Every order carries a `mode` field indicating which execution engine placed it:

| Mode | AlgoOrder.mode Value | Grid Display | Execution |
|---|---|---|---|
| Simulator | "sim" | SIM tag | PaperTradeEngine (SimQuoteSource) |
| Paper | "paper" | PAPER tag | PaperTradeEngine (LiveQuoteSource) |
| Live | "live" | No tag | Broker gateway (Kite/Dhan/Groww) |
| Replay | (none) | N/A | Read-only, no new orders |
| Shadow | "shadow" | SHADOW tag | Broker validation (no execute) |

### Order grid filtering

Operator can filter the Orders tab by mode to view sim/paper/live/shadow orders separately.

---

## 8. Chase Loop and Fill Logic

### Preflight validation (Mode 1/2 only)

Before registering an order with PaperTradeEngine, the action handler runs preflight checks:

| Guard | Applies to | Action |
|---|---|---|
| G1 (LOT_MULTIPLE) | F&O | Qty must be multiple of lot_size (removed after refactor) |
| G2 (FAT_FINGER) | Equity + F&O | 5-lot cap NSE, 20-lot cap MCX (bypassable via intent="close") |
| Margin validation | All | Sufficient margin required (shadow mode validates only) |

**G2 bypass**: Close orders (intent="close") skip the FAT_FINGER cap to allow position exit.

### Chase fill criteria

On each `step()` tick, for each open paper order:

```
quote = quote_source.fetch(symbol)
bid, mid, ask = quote.bid, quote.mid, quote.ask

if order.side == "BUY":
    if order.limit >= ask:
        status = "FILLED"
        fill_price = min(order.limit, ask)
else:  # SELL
    if order.limit <= bid:
        status = "FILLED"
        fill_price = max(order.limit, bid)
```

Orders fill at the best available price (limit if tighter than market, market if limit is wider).

### Max attempts and unfilled state

If an order remains OPEN after reaching `max_attempts` (configurable, default 5):
- Status → UNFILLED
- Detail → "Failed to fill after 5 attempts; last bid/ask: …"
- No further chase attempts

---

## 9. Mode Transitions

### Mid-session mode switch (SIM ↔ LIVE)

When the operator flips execution mode mid-session:

1. **StripReset** — NavStrip day-delta slots (P:1, H:1) zeroed immediately
2. **Close paper orders** — All open orders from the old mode marked UNFILLED
3. **Fetch fresh data** — Force `_load()` to refresh positions/holdings/funds
4. **Suppress until next poll** — Day-delta display suppressed until first fresh poll from new mode
5. **Resume polling** — 30s interval polling resumes in the new mode

### Rationale

Switching modes mid-session could cause display inconsistencies (mixing sim P&L with live P&L).
Reset ensures clean state transition.

### Replay → Live transition

When operator stops replay and returns to live mode:
1. Replay state cleared (cursor, events, _task)
2. SimDriver state remains (for potential restart)
3. Live mode polling resumes
4. Previous replay orders are terminal (not replayable)

---

## 10. Shadow Mode Details

### What shadow does

Shadow mode allows the operator to:
1. Place orders through the normal UI workflow
2. Broker validates margin/qty/symbol/account
3. Validation payload captured (logged to `algo_orders` with mode='shadow')
4. Order NOT sent to exchange
5. No fills, no executions

### Validation flow

```
Order ticket submitted
  → preflight checks (G1, G2, margin)
  → broker.translate_qty() (F&O lots→contracts)
  → broker.validate_margin() [shadow-specific]
  → log payload [shadow-specific]
  → return success (but no execute)
```

### Use cases

- Pre-prod testing of order parameters without risk
- Margin calculation validation before going live
- Audit trail of "what would have happened"

### Shadow orders visibility

Orders appear in the Order grid with `mode='shadow'` tag. No fill history or postback events
(unlike paper orders which fill after 30s chase).

---

## 11. Test Coverage Map

### Backend — mode resolution

- **_resolve_mode() priority** — SIM > REPLAY > branch check > SHADOW > PAPER > LIVE
- **Branch gate** — Non-main forces PAPER even if DB says LIVE
- **Prod branch** — Only main reaches LIVE/SHADOW
- **Per-agent override** — Agent.trade_mode takes precedence over global mode

### Backend — paper trade engine

- **Chase loop cadence** — Runs every 30s during market hours, skips closed hours
- **Fill logic** — Limit-price crossing, best-price selection (limit vs market)
- **Max attempts** — Order marked UNFILLED after N attempts, no further retries
- **Unfilled detail** — Last bid/ask captured and logged
- **Recovery** — Missing quotes auto-retry on next cycle

### Backend — mode transitions

- **Mid-session switch** — NavStrip reset, paper orders closed, data refresh
- **Suppress until poll** — Day-delta display suppressed via `_openTransitionStamp` check
- **Replay stop** — Transient badge cleared, live polling resumes

### Backend — shadow mode

- **Validation call chain** — Margin check before logging payload
- **No execute** — Order never reaches broker gateway
- **Payload logged** — Captured to algo_orders for audit

### Frontend — indicator

- **Navbar pill color** — LIVE=red, PAPER=sky-blue, SHADOW=orange, IDLE=gray
- **Transient badge** — SIM/REPLAY show in banner area (green)
- **Filter by mode** — Order grid filtering works for all modes

### Gaps

- Missing: Batch mode transition (e.g., switch all open paper orders to shadow)
- Missing: Scheduled mode transitions (e.g., auto-switch to LIVE at market open)
- Missing: Multi-mode backtesting (replay multiple runs in LIVE mode view)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
