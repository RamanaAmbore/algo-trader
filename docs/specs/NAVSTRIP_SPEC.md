# NavStrip Specification

Single source of truth for the NavStrip header band behavior across all market states
and execution modes. The NavStrip is a fixed band pinned below the navbar showing live
P&L, margin, cash, and holdings aggregates across all broker accounts.

**Version**: 1.1 — 2026-07-19  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/PositionStrip.svelte` · `frontend/src/lib/InfoHint.svelte` · `frontend/src/lib/data/nav.js` · `backend/api/routes/positions.py` · `backend/api/algo/pnl_math.py`

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
| 1 | Today's Day P&L (clickable) | All positions (NSE/BSE/NFO/MCX/CDS) | `Σ baseDayPnlForPosition(p)` + live-tick delta |
| 2 | Lifetime P&L | All positions (NSE/BSE/NFO/MCX/CDS) | `Σ p.pnl` + live-tick delta |
| 3 | F&O expiry profit | Derivatives only (NFO/MCX/CDS/BFO) | See EXP Slot spec below |

**New-position override** (slot 1 only): When `overnight_quantity = 0` AND `day_change_val = 0`
AND `pnl ≠ 0`, the broker omitted intraday decomposition. Fall back to lifetime `pnl` as the
safest approximation. Applied by `baseDayPnlForPosition()`.

#### Slot 1 Day P&L Breakup modal

Clicking the **Day P&L value in slot 1** opens a `DayPnlBreakup` modal displaying the detailed
composition of today's intraday profit/loss:

- **Header**: per-account subtotal and grand total across all accounts
- **Table rows**: one row per (account, symbol) pair with non-zero day P&L contribution
- **Columns**: 
  - prev_close (frozen at first intraday snapshot, from `daily_book.previous_close`)
  - ltp (current last-traded price)
  - overnight_qty (qty held from prior close)
  - buy/sell volumes (intraday buy and sell leg quantities + values)
  - lifetime_pnl (cumulative P&L on the position)
  - settlement_pnl (yesterday's closed-out settled value, from daily_book)
  - computed_day_pnl (derived per the baseDayPnlForPosition formula)
- **Zero-row treatment**: rows with zero day P&L show a ⚠ icon with a tooltip explaining why
  (e.g., "Position held overnight, no settlement differential")
- **Close**: Esc key or backdrop click closes the modal

#### EXP Slot Specification (Slot 3)

The expiry profit (EXP) slot shows the profit/loss across all F&O positions if they expired 
(were settled) at the current spot price, right now. Equity positions are excluded from this 
slot (but included in the H pill).

**Exchange gate**: Only rows where `exchange` is one of `['NFO', 'MCX', 'CDS', 'BFO']` 
contribute to EXP. Equity rows (`NSE`, `BSE`) are skipped.

**Formula by leg state**:

1. **Open leg** (qty ≠ 0): `expiryPnl(leg, spot) + (leg.realised || 0)`
   - `expiryPnl` computes intrinsic profit at current spot
   - `leg.realised` is added for partial-close positions (contracts closed earlier in the 
     same position entry; e.g., sold 5 of 10 NIFTY CE contracts today)
   - Example: long 2 CE 2850, spot 2875, 1 contract closed for +30 today
     → `2 × (2875 − 2850) = +50` (intrinsic) + `30` (realised) = `+80` total

2. **Closed leg** (qty = 0): `leg.realised || leg.pnl`
   - When qty is 0, the entire position was exited during the session
   - Realized P&L is locked in `leg.pnl` (broker snapshot field)
   - No spot lookup needed — the value is certain, independent of current market price
   - Example: sold 2 CE 2850 short, covered today for +100 profit → contributes +100 total

**Spot resolution** (for open legs):
- Backend-stamped `underlying_ltp` (SSOT, via Pass 3 enrichment) takes precedence
- Fallback chain: `symbolStore` snapshot for resolved tradingsymbol → underlying root → 
  positions/holdings row-scan for a matching symbol's LTP
- If no spot can be resolved, the leg contributes 0 (no error thrown)

**Throttling**: Recomputed at 4 Hz max (same `_throttledTick` gate as `_liveDeltaByRow`) 
to avoid scheduler pressure during high-frequency SSE ticks.

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

### Pill label accent colors

Each pill identifier (P / M / C / H) carries its own fixed accent hue so pills are
distinguishable without reading the letter:

| Pill | Label class | Color | Token |
|---|---|---|---|
| P | `.ps-k-p` | Amber | `var(--c-action)` `#fbbf24` |
| M | `.ps-k-m` | Violet | `var(--algo-violet)` `#a78bfa` |
| C | `.ps-k-c` | Sky | `var(--algo-sky)` `#38bdf8` |
| H | `.ps-k-h` | Cyan | `var(--algo-cyan)` `#22d3ee` |

### Pill label panel popups

Each pill label (P / M / C / H) is wrapped in `<InfoHint popup panel>` with the pill's
accent color supplied via `accentColor`. Clicking the label opens a **panel popup** — a
richer floating overlay distinct from the compact tooltip style used elsewhere:

- **Background**: `linear-gradient(180deg, #1c2840 0%, #141e33 100%)`
- **Border**: `1px solid var(--algo-amber-border-soft)` + 3px left accent stripe in the
  pill's own color
- **Shadow**: `0 8px 32px rgba(0,0,0,0.6)`
- **Title**: uppercase, 700-weight, in the pill accent color, separated by a thin divider
- **Body**: `var(--fs-sm)`, `var(--algo-slate)`, line-height 1.5

This panel aesthetic matches the DayPnlBreakup modal so all overlay surfaces in the strip
have a unified visual language.

### Per-slot hover hints

Each slot value (except the clickable Day P&L) has an **ⓘ** icon immediately to its
right. The icon is rendered via `<InfoHint popup panel showOnHover label="ⓘ">` and uses
the parent pill's accent color. Behavior:

- Icon is low-opacity (0.5) at rest, full-opacity (`var(--c-info)`) on hover
- `showOnHover=true` — the panel opens on mouse-enter, closes on mouse-leave; no click needed
- Focus/blur keyboard equivalents fire the same open/close
- The popup content describes what the slot measures and how the value is computed

Slot hint content by position:

| Slot | Title | Content |
|---|---|---|
| P:1 Day P&L | "Day P&L" | Live tick price − prev close × net qty across all accounts. New intraday positions (overnight_quantity=0) use pnl directly. |
| P:2 Lifetime P&L | "Lifetime P&L" | Cumulative P&L since position opened. Includes realised + unrealised. |
| P:3 Expiry P&L | "Expiry P&L" | Projected P&L at expiry using lognormal model. Shows what the F&O portfolio returns if held to expiry at current spot. |
| M:1 Available Margin | "Available Margin" | Cash deployable right now for new orders. = Total margin − used margin. Updated after every order fill. |
| M:2 Total Margin | "Total Margin" | Full collateral picture across all accounts. = Available + margin blocked for open positions. |
| C:1 Cash Available | "Cash Available (CA)" | Live deployable cash. Nets realised P&L + premium debits from long options already paid. |
| C:2 Total Cash | "Total Cash (C)" | CA + premium tied up in long options (recoverable if closed). Full liquid wealth excluding positions. |
| H:1 Holdings Today MTM | "Holdings Today MTM" | Live LTP − prev close × qty for all long-term holdings. Intraday MTM only; excludes overnight positions. |
| H:2 Holdings Value | "Holdings Value" | Broker-reported current market value of all holdings across all accounts. |
| H:3 Holdings Lifetime P&L | "Holdings Lifetime P&L" | Cumulative P&L since purchase. (current price − avg cost) × qty, all holdings. |

### Slot differentiation within pills

Within each pill, slot 1 (primary / available) uses the **bright** variant of the accent
and slot 2+ (secondary / total) uses the **pastel** (dim) variant — so the more actionable
figure reads louder:

**M pill (Margin)**

| Slot | Class | Color | Notes |
|---|---|---|---|
| M:1 available | `.ps-margin` | Bright violet `#c084fc` | Deployable capacity |
| M:2 total | `.ps-margin-dim` | Pastel violet `var(--algo-violet-text)` `#d8b4fe` | Full capacity |

**C pill (Cash)**

| Slot | Class | Color | Notes |
|---|---|---|---|
| C:1 available | `.ps-cash` | Bright sky `#7dd3fc` | Deployable cash |
| C:2 total | `.ps-cash-dim` | Pastel sky `var(--algo-sky-text)` `#bae6fd` | Incl. tied-up option premium |

**H pill (Holdings)**

| Slot | Class | Notes |
|---|---|---|
| H:1 today MTM | `.ps-pos` / `.ps-neg` / `.ps-flat` | Bright green/red — same P&L palette as P pill |
| H:2 current value | `.ps-cash` | Sky blue — non-directional market value |
| H:3 lifetime P&L | `.ps-pos-dim` / `.ps-neg-dim` / `.ps-flat` | Pastel green `var(--algo-green-text)` / pastel red `var(--algo-red-text)` |

### Directional P&L

- **Positive** (P:1, P:2, H:1, H:3 > 0): bright green (`ps-pos`, `var(--c-long)` `#4ade80`)
- **Negative**: bright red (`ps-neg`, `var(--c-short)` `#f87171`)
- **Flat** (= 0): muted slate (`ps-flat`)
- **Lifetime dim** (H:3): pastel green `#6ee7b7` / pastel red `#fecaca` via `ps-pos-dim` / `ps-neg-dim`

### Non-directional values

- **Expiry profit** (P:3): amber action palette (`ps-exp`, `var(--c-action)`), neutral/time-bound signal
- When a normally-positive slot (M:1, M:2, C:1, C:2) goes negative: flips to `ps-neg` (red)

### Stale state

- Background darkens to brown when `_staleFailCount >= 2`
- Border softens to orange-tinted amber
- Text colors unchanged; visual hint only

### Mobile trailing clip

`.ps-strip` uses `padding: 0 0.75rem 0 0.5rem` — the 0.75rem right padding ensures
H pill's last slot does not clip against the viewport edge when the strip overflows
horizontally on narrow screens.

---

## 7. Edge Cases

### No positions or holdings

- P pill shows `0.0 / 0.0 / 0.0`
- H pill shows `0.0 / 0.0 / 0.0`
- C pill reflects broker cash only
- M pill reflects margin capacity (typically all available)

### Derivatives closed legs and partial-close P&L

When an F&O position is partially or fully exited during the day, the closed contracts' 
realized P&L must appear in the P:3 (EXP) slot immediately.

**Closed leg (qty = 0)**: The entire position is gone; `leg.pnl` or `leg.realised` contains 
the full locked-in profit. EXP reads this field directly — no spot calculation needed.

**Partially closed leg (qty ≠ 0)**: Some contracts are still open, others closed earlier. 
EXP adds two components:
- Current intrinsic of remaining contracts via `expiryPnl(leg, spot)`
- Locked-in P&L from the closed portion via `leg.realised`

Without the `+ leg.realised` term, partial closes would show only the remaining intrinsic, 
missing the realized gain/loss from the exited contracts — causing EXP to diverge from 
the true all-in position profit.

### Market just opened (0 poll cycles complete)

- Day-delta slots (P:1, H:1) show 0 until the first successful poll lands
- Gate via `_openTransitionStamp` snapshot: display suppressed while `_pollCycleStamp <= _openTransitionStamp`
- Prevents yesterday's stale `day_change_val` (from prior session close) from painting briefly

### baseDayPnlForPosition formula

The `baseDayPnlForPosition(p)` helper (in `frontend/src/lib/data/nav.js`) computes the intraday 
profit/loss delta for a position using two-tier resolution:

**Primary path** (when `prev_settlement_pnl` is available):

```
day_delta = pnl − prev_settlement_pnl
```

where `prev_settlement_pnl` is yesterday's cumulative P&L for that `(account, symbol)` 
pair, sourced from the `daily_book` table (the `total_pnl` snapshot from the prior session close).

**Fallback** (for positions opened today, not yet in `daily_book`):

```
day_delta = pnl − overnight_quantity × (close_price − average_price)
```

This fallback handles new intraday positions by subtracting the opening-session unrealized 
P&L from the current total, leaving only today's delta. When `overnight_quantity = 0`, 
this reduces to `pnl` itself (the full intraday gain/loss for a new position).

**Case 4 (stale close guard)** — When `close_price <= 0` (broker returned zero or missing
prev_close), `baseDayPnlForPosition` returns 0. When `close > 0` and `dcv === 0`, returns
`pnl − oq×(close−avg)` regardless of whether `close === ltp`. The earlier `close === ltp`
guard was a regression (removed 8474a17e) — it incorrectly zeroed realized P&L when the
broker hadn't refreshed `close_price`. Source: `frontend/src/lib/data/nav.js:109`.

Applied consistently across PositionStrip, MarketPulse, and derivatives surfaces for 
accurate session-relative P&L reporting.

**Snapshot path parity (closed hours)** — As of July 2026, `_positions_snapshot` (the 
closed-hours read path) now populates `prev_settlement_pnl` from a second SQL query that 
fetches yesterday's `daily_book` entry per `(account, symbol)`. This enables Branch A 
(the primary `pnl − prev_settlement_pnl` formula) for overnight positions viewed during 
closed hours (e.g. MCX overnight window, weekend). Day P&L is now identical whether the 
position is read during market open (live path via `_backfill_prev_settlement_pnl`) or 
after close (snapshot path). See [DESIGN_GUIDE.md §21.5.5](DESIGN_GUIDE.md) for technical details.

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
- **EXP slot (P:3)**: F&O positions only; excludes equity; closed legs show realized P&L; partial-close legs add intrinsic + realised
- **Freeze behavior**: During closed hours, slots show non-zero as_of values; animations suppressed
- **Stale indicator**: CSS class `ps-stale` appears after 2 broker errors; disappears on recovery
- **Color consistency**: Positive/negative values use correct palette across all slots
- **Heartbeat and tick-border**: Animations fire correctly on poll cycles and SSE ticks
- **New-position day P&L**: New intraday position (oq=0) shows correct P:1 value via fallback formula
- **Closed-leg EXP**: Fully exited F&O leg (qty=0) contributes `p.pnl` to EXP; not skipped
- **Partial-close EXP**: Partially exited F&O leg (qty≠0) adds realised + intrinsic to EXP
- **Panel popups (Round 6)**: Click P/M/C/H label → panel opens with accent-colored title, left-border stripe, and gradient background; accent color matches pill identity
- **Per-slot hover hints (Round 6)**: Hover any slot value → ⓘ icon visible; hover icon → panel opens with correct title and slot description; leaves on mouse-out

### Backend (Python)

- **baseDayPnlForPosition()**: Computes day delta from `prev_settlement_pnl` (primary) or fallback `oq × (close − avg)` formula
- **Daily book snapshots**: Idempotent UPSERT; survive across restarts; correct as_of stamp; supplies `prev_settlement_pnl` lookup
- **Closed-hours routes**: `/api/positions`, `/api/holdings` return snapshot with `as_of` when market closed
- **Margin aggregation**: `/api/funds` sums avail_margin and used_margin correctly across accounts
- **Long options cash**: Premium tied up in current holdings computed correctly (avg × lot_size × num_lots)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase implementation |
| 2026-07-13 | EXP Slot spec: documented closed-leg (qty=0) handler; partial-close realized P&L (`leg.realised`) in open-leg formula |
| 2026-07-18 | Color coding: per-pill label accents (P=amber, M=violet, C=sky, H=cyan); slot bright/dim differentiation for M, C, H pills; mobile trailing-clip padding note |
| 2026-07-19 | Round 6: InfoHint `panel` mode spec (gradient bg, accent left border, titled header); per-slot ⓘ hover hints for all 10 slots; corrected M and C accent hex values; font-size upgrade for `.ps-agg-k` (xs→sm desktop, 2xs→xs mobile/380px); `frontend/src/lib/InfoHint.svelte` + `PositionStrip.svelte` updated |
