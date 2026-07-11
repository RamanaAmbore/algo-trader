# NavStrip Specification

Single source of truth for the NavStrip header band behavior across all market states
and execution modes. The NavStrip is a fixed band pinned below the navbar showing live
P&L, margin, cash, and holdings aggregates across all broker accounts.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/PositionStrip.svelte` · `frontend/src/lib/data/nav.js` · `backend/api/routes/positions.py` · `backend/api/algo/pnl_math.py`

---

## Contents

1. [Pill Layout and Slot Values](#1-pill-layout-and-slot-values)
2. [Data Sources and SSOT](#2-data-sources-and-ssot)
3. [Live-Tick Delta Correction](#3-live-tick-delta-correction)
4. [Snapshot Freeze and Reset](#4-snapshot-freeze-and-reset)
5. [Data Freshness and Staleness](#5-data-freshness-and-staleness)
6. [Color Coding](#6-color-coding)
7. [Edge Cases](#7-edge-cases)
8. [Test Coverage Map](#8-test-coverage-map)

---

## 1. Pill Layout and Slot Values

Four pills cluster below the navbar, separated by gaps. Each pill shows two or three values
separated by slashes.

### P pill: Positions P&L

Formula: three slots displaying position profit from three perspectives.

| Slot | Value | Scope | Formula |
|---|---|---|---|
| 1 | Today's Day P&L | All positions (NSE/BSE/NFO/MCX/CDS) | `Σ baseDayPnlForPosition(p)` + live-tick delta |
| 2 | Lifetime P&L | All positions (NSE/BSE/NFO/MCX/CDS) | `Σ p.pnl` + live-tick delta |
| 3 | F&O expiry profit | Derivatives only (NFO/MCX/CDS/BFO) | `Σ expiryPnl(p, spot)` at current spot |

**New-position override** (slot 1 only): When `overnight_quantity = 0` AND `day_change_val = 0`
AND `pnl ≠ 0`, the broker omitted intraday decomposition. Fall back to lifetime `pnl` as the
safest approximation. Applied by `baseDayPnlForPosition()`.

### M pill: Margin

Formula: available margin deployed / total margin capacity.

| Slot | Value |
|---|---|
| 1 | Available: `Σ funds.avail_margin` (deployable now) |
| 2 | Total: `Σ funds.used_margin + funds.avail_margin` (full capacity) |

### C pill: Cash

Formula: live cash available / total cash including tied-up option premium.

| Slot | Value |
|---|---|
| 1 | Available: `Σ funds.live_cash` (live broker balance) |
| 2 | Total: `Σ funds.live_cash + longOptionsCashPaid` (includes premium tied up) |

Long options cash paid derives from positions: for each long CE/PE row, compute
`average_price × lot_size × (quantity / lot_size)`. Falls back to `average_price × quantity`
if lot_size is unavailable. Updated on each 30s poll cycle.

### H pill: Holdings

Formula: three slots displaying holding value and profit from three perspectives.

| Slot | Value | Formula |
|---|---|---|
| 1 | Today's MTM move | `Σ holdings.day_change_val + live-tick delta` |
| 2 | Current value | `Σ holdings.cur_val + live-tick delta` |
| 3 | Lifetime P&L | `Σ holdings.pnl + live-tick delta` |

---

## 2. Data Sources and SSOT

Every value must match a canonical source to stay in sync with other surfaces.

| Slot | SSOT Surface | Implementation |
|---|---|---|
| P:1 | MarketPulse Positions grid TOTAL row, Day P&L column | `baseDayPnlForPosition(p)` summed + live delta |
| P:2 | MarketPulse Positions grid TOTAL row, P&L column | `Σ p.pnl` summed + live delta |
| P:3 | `/admin/derivatives` TOTAL row at expiry | `expiryPnl()` helper, F&O only |
| M:1, M:2 | `/api/funds` response margin fields | `funds[].avail_margin`, `funds[].used_margin` |
| C:1 | Broker's live cash (CA) | `fundsStore.load()` → `funds[].live_cash` |
| C:2 | Option premium tied up in long positions | Derived from `positions[]` CE/PE rows |
| H:1 | MarketPulse Holdings grid TOTAL row, Day P&L column | `Σ holdings.day_change_val + delta` |
| H:2 | MarketPulse Holdings grid TOTAL row, Value column | `Σ holdings.cur_val + delta` |
| H:3 | MarketPulse Holdings grid TOTAL row, P&L column | `Σ holdings.pnl + delta` |

---

## 3. Live-Tick Delta Correction

During market open, SSE ticks update position and holdings LTPs in real-time. The strip
adds a live-delta correction on top of the last broker poll value.

### Delta formula

For each row in positions/holdings:

```
live_delta = (live_ltp − poll_ltp) × quantity
```

where `live_ltp` is the current SSE snapshot and `poll_ltp` is the LTP from the last broker
poll. Aggregated by kind (P or H) and row identity (symbol + account) to avoid double-counting
when the same symbol appears in both positions and holdings.

### Market-open gating

Deltas are applied only when the relevant market is open:
- MCX rows during MCX hours (09:00–23:30 IST)
- NSE/BSE/NFO/BFO/CDS rows during NSE hours (09:15–15:30 IST)

Outside these windows, day-delta slots (P:1, H:1) show only the broker-poll value (no live
correction).

### SSE tick source validation

Live LTPs are sourced from `symbolStore` snapshots via `getSnapshot(sym)`. Only snapshots
with `ltp_ts > 0` (genuine SSE ticks, not REST-sourced quotes) contribute to the delta.
REST publishers (`publishPulseQuotes`, `_publishPositionsRows`) use `ltp_ts = 0` — when they
race with real SSE ticks, the timestamp gate prevents oscillation between two stale values.

---

## 4. Snapshot Freeze and Reset

The NavStrip distinguishes two classes of metrics:

**Lifetime metrics** (P:2, P:3, H:2, H:3): Always live, never frozen. Render directly from
live derived values. Reset does not apply.

**Day-delta metrics** (P:1, H:1): Freeze at market close; reset to 0 at next market open.
Frozen values persist all evening and weekend so the operator sees their end-of-session
P&L continuously.

### Freeze sequence at market close

On `<exch>:close` event (NSE 15:30, MCX 23:30 IST):

1. Snapshot current poll-cycle values to `daily_book` table (idempotent UPSERT on
   `(date, account, kind, symbol)`)
2. Strip stops polling (30s interval pauses outside market hours)
3. Day-delta slots display the final poll value; live-tick delta no longer applies

### Reset sequence at market open

On `<exch>:open` event (NSE 09:15, MCX 09:00 IST, after Tier 1 cache clear):

1. Zero dispPositionsToday and dispHoldingsToday immediately
2. Delete localStorage cache key `strip.frozen`
3. Call `_load()` to fetch fresh positions/holdings/funds
4. Suppress day-delta display until the first fresh poll completes (guard via
   `_openTransitionStamp` snapshot + `_pollCycleStamp > _openTransitionStamp` check)
5. After first poll: day-delta slots track live derived values from the new session

### Execution-mode transitions

Mid-session SIM↔LIVE mode switch also triggers the reset sequence (without waiting for a
market boundary):

1. Zero day-delta slots
2. Clear frozen cache
3. Force `_load()` immediately
4. Suppress until first fresh poll completes

This ensures the strip never displays mixed real-broker + sim-fabricated P&L.

---

## 5. Data Freshness and Staleness

### Poll cycle

During market hours, the strip refreshes every 30 seconds via `marketAwareInterval(_load, 30000)`.
All three data stores (positions, holdings, funds) are loaded together; any single failure
increments the stale counter.

### Stale indicator

When 2+ consecutive poll cycles return an error:

- CSS class `ps-stale` applies to the strip root
- Background tint darkens to an amber-tinged brown (offline palette)
- No text changes; visual hint only

Recovers immediately on the next successful poll.

### Polling pauses

`marketAwareInterval` pauses overnight outside market-open windows. The strip retains the
last in-session snapshot until the next market open.

### Heartbeat animation

On every successful poll (independent of whether values moved):

- Subtle amber glow on strip border (300ms decay)
- Signals "data just refreshed" without competing with per-cell directional flash
- Fires every 30s even when values are stable (e.g. off-hours, or all positions flat)

### Tick-border shimmer

On real SSE tick arrivals (independent of polls):

- Sky-blue border flash (300ms decay)
- Throttled to 1 Hz (leading-edge) so a 20-symbol tick burst pulses once, not 20 times
- Signals liveness during tick-burst windows

---

## 6. Color Coding

### Directional P&L

- **Positive** (P:1 > 0, P:2 > 0, H:1 > 0, H:3 > 0): green (`ps-pos`, `--c-long`)
- **Negative**: red (`ps-neg`, `--c-short`)
- **Flat** (= 0): muted slate (`ps-flat`)

### Non-directional values

- **Margin / Cash amounts**: cyan (`ps-cash`, `#7dd3fc`) when positive; red when negative
- **Expiry profit** (P:3): amber action palette (`ps-exp`, `--c-action`), neutral/time-bound signal
- **Holdings value** (H:2): always cyan (balance-sheet figure, not directional)

### Stale state

- Background darkens to brown when `_staleFailCount >= 2`
- Border softens to orange-tinted amber
- Text colors unchanged; visual hint only

---

## 7. Edge Cases

### No positions or holdings

- P pill shows `0.0 / 0.0 / 0.0`
- H pill shows `0.0 / 0.0 / 0.0`
- C pill reflects broker cash only
- M pill reflects margin capacity (typically all available)

### Market just opened (0 poll cycles complete)

- Day-delta slots (P:1, H:1) show 0 until the first successful poll lands
- Gate via `_openTransitionStamp` snapshot: display suppressed while `_pollCycleStamp <= _openTransitionStamp`
- Prevents yesterday's stale `day_change_val` (from prior session close) from painting briefly

### Overnight position bought today

Position row has `overnight_quantity = 0` and `pnl ≠ 0`. Broker omitted `day_change_val`:

- `baseDayPnlForPosition()` detects this and returns `pnl` instead of `day_change_val`
- Applied consistently across PositionStrip, MarketPulse, and derivatives surfaces
- Critical fix for new-position P&L correctness

### Auth outage or broker connection loss

- Strip retains last-good values from successful poll
- Stale indicator appears after 2 consecutive failures
- No blanks or "—"; always shows the last-known state

### Closed-hours display after restart

- `daily_book` snapshot from prior session close persists in DB
- Strip reads it on mount and displays snapshot values
- `_load()` pulls from `daily_book` snapshot during closed hours (via `closed_hours_or_broker()`)
- Survives restarts; no localStorage gymnastics needed

---

## 8. Test Coverage Map

### Playwright (frontend)

- **Slot alignment**: P pill renders slot 1 / slot 2 / slot 3 with correct delimiters
- **SSOT synchronization**: P:1 matches MarketPulse Positions TOTAL row Day P&L; P:2 matches Positions TOTAL P&L
- **Holdings slots**: H:1, H:2, H:3 match Holdings grid TOTAL row values
- **Freeze behavior**: During closed hours, slots show non-zero as_of values; animations suppressed
- **Stale indicator**: CSS class `ps-stale` appears after 2 broker errors; disappears on recovery
- **Color consistency**: Positive/negative values use correct palette across all slots
- **Heartbeat and tick-border**: Animations fire correctly on poll cycles and SSE ticks
- **New-position override**: Overnight position bought today (oq=0) shows correct P:1 value (uses `pnl` not `day_change_val`)

### Backend (Python)

- **baseDayPnlForPosition()**: Applies new-position override when oq=0 && dcv=0 && pnl≠0
- **Daily book snapshots**: Idempotent UPSERT; survive across restarts; correct as_of stamp
- **Closed-hours routes**: `/api/positions`, `/api/holdings` return snapshot with `as_of` when market closed
- **Margin aggregation**: `/api/funds` sums avail_margin and used_margin correctly across accounts
- **Long options cash**: Premium tied up in current holdings computed correctly (avg × lot_size × num_lots)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase implementation |
