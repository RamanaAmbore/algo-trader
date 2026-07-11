# Dashboard Specification

Single source of truth for the admin dashboard behavior, layout composition, and NAV
calculation. The dashboard surfaces performance analytics, portfolio breakdown, and
activity feeds in a single consolidated operator view.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/routes/(algo)/dashboard/+page.svelte` · `frontend/src/lib/PerformancePage.svelte` · `frontend/src/lib/NavBreakdown.svelte` · `backend/api/algo/nav.py`

---

## Contents

1. [Layout and Composition](#1-layout-and-composition)
2. [P&L Analysis Left Panel](#2-pl-analysis-left-panel)
3. [Sidebar Tabs and Data](#3-sidebar-tabs-and-data)
4. [NAV Calculation — v4 Formula](#4-nav-calculation--v4-formula)
5. [Day P&L SSOT and Override](#5-day-pnl-ssot-and-override)
6. [Activity Card Integration](#6-activity-card-integration)
7. [Closed-Hours and Snapshot Behavior](#7-closed-hours-and-snapshot-behavior)
8. [Execution Mode Display](#8-execution-mode-display)
9. [Edge Cases](#9-edge-cases)
10. [API Contract](#10-api-contract)
11. [Test Coverage Map](#11-test-coverage-map)

---

## 1. Layout and Composition

**Two-pane layout**:
- **Left**: P&L Analysis chart (80% width on desktop)
- **Right**: Sidebar with three tabs (20% width)

**Chart** (PnlAnalysis component):
- Time-series P&L curve (daily snapshots or intraday ticks, depending on historical span)
- Y-axis: profit/loss (INR)
- X-axis: dates (1W / 1M / 3M / 6M / 1Y / YTD / ALL range picker)
- Hover tooltip: date + P&L + % return
- No legend; single line (firm-wide aggregate)

**Sidebar tabs** (mutually exclusive):
- NAV — portfolio breakdown by asset class + pie chart
- Capital — margin capacity utilization + available margin
- Equity — holdings composition + dividend yield aggregates

**Data refresh**: Chart + sidebar update every 5 min during market hours via background task.
Manual refresh button (Cmd+R keyboard shortcut) forces immediate fetch.

---

## 2. P&L Analysis Left Panel

**Data source**: Daily `nav_daily` snapshots (daily_book aggregate at market close).

**Time ranges**:
- 1W (7 days) — last market week
- 1M (30 days) — calendar month or last 30 market days
- 3M (90 days)
- 6M (180 days)
- 1Y (365 days)
- YTD (year-to-date from Jan 1)
- ALL (full history, clamped to 5 years max)

**Y-axis formula** (for each date):
```
Daily P&L = nav_current − nav_previous
```

**Cumulative P&L** (chart line):
```
Cumulative = Σ(Daily P&L) from inception to date
```

**Rendering**:
- Line chart (SVG or lightweight library)
- Directional color: green (positive slope), red (negative slope)
- No animation on range switch (instant redraw)

**Hover detail**: Date + absolute P&L value + percentage gain/loss from inception.

---

## 3. Sidebar Tabs and Data

### NAV Tab

**Content**: Breakdown by asset class (Equities / F&O / Commodities / Cash).

**Pie chart**: Slice per asset class; label + value.

**Table**: Rows per account:
- Account code
- Cash + Holdings value
- Open position P&L
- Total NAV
- Allocation %

**NavBreakdown component**: SSOT for sidebar calculations.
Reads from `backend/api/algo/nav.py:compute_firm_nav()` (daily) + live refreshes.

### Capital Tab

**Margin breakdown**:
- Deployed (used) margin: Σ(funds.used_margin)
- Available margin: Σ(funds.avail_margin)
- Total capacity: deployed + available
- Utilization % bar

**Per-account drill-down** (if provided):
- Account code
- Deployed margin
- Available margin
- Utilization %

### Equity Tab

**Holdings composition**:
- Symbol, Qty, Current Value, Lifetime P&L, Div Yield %

**Filters**: Account selector (if multi-account).

**Sort**: Value DESC (largest holdings first).

**Aggregation** (if shown per-class): Σ Dividend yield across holdings, weighted by value.

---

## 4. NAV Calculation — v4 Formula

**Backend SSOT** (`backend/api/algo/nav.py:compute_firm_nav`):
```
firm_nav = Σ(cash_sod + option_premium)  [all accounts]
         + Σ(position.unrealised)         [all open positions where qty ≠ 0]
         + Σ(holdings.cur_val)            [all holdings]
```

**Cash component** (cash_sod + option_premium):
- `cash_sod`: Start-of-day available cash (funds.avail_opening_balance or funds.cash)
- `option_premium`: Tied-up premium on long options (util option_premium or option_premium)
- Adding premium back: broker debits cash on long-option purchase; `util option_premium` surfaces
  the cash tied-up. Re-adding it ensures the position P&L (already in unrealised) doesn't double-count.

**Position component** (position.unrealised):
- Broker's pre-computed mark-to-market for each open position
- Formula: (LTP − average_price) × quantity
- Only summed when quantity ≠ 0 (excludes flat positions)

**Holdings component** (holdings.cur_val):
- Broker's mark-to-market for equity/ETF holdings
- Formula: qty × LTP (calculated by broker)
- Non-pledged holdings only (pledged shares already counted in funds.net collateral)

**Why v4 replaces v3**:
- v3 added `used_margin` to cash, double-counting SPAN margin already in position.unrealised
- v4 uses `option_premium` only, cleanly separating cash from futures margin flows

**Frontend equivalent** (`frontend/src/lib/data/nav.js:navByAccount`):
- Identical v4 formula
- Both surfaces sync; any revision must update both files together
- Operator-verified for account-level reconciliation vs Kite `profile().net`

---

## 5. Day P&L SSOT and Override

**Today P&L display** (chart hero metric, cards, NavStrip):
- **Canonical source**: `baseDayPnlForPosition(p)` in `frontend/src/lib/data/nav.js`
- **Formula**: Decomposed intraday, not naive `(LTP − close) × qty`

**New-position override** (critical fix):
When `overnight_quantity = 0` AND `day_change_val = 0` AND `pnl ≠ 0`:
- Broker omitted intraday decomposition (new positions bought today)
- Fallback: use lifetime `pnl` as safest approximation
- Applied consistently across PositionStrip, MarketPulse, derivatives surfaces, dashboard

**Dashboard hero display**:
- `_todayPnl` sum: Σ baseDayPnlForPosition(p) for all positions
- Refreshed every 5 min + on manual refresh
- Color: green (positive), red (negative), slate (zero)

**Never read** `position.day_change_val` directly; always route through `baseDayPnlForPosition()`.

---

## 6. Activity Card Integration

**Location**: Dashboard sidebar (right side, below/above NAV/Capital/Equity tabs).

**Content**: Replaced legacy NEWS card; now shows Activity surface (Orders tab by default).

**Mount point**: Embedded activity card uses local filter state (not shared with modal/page activity).
Default tab: 'order'. User can switch tabs within the card.

**Row limit**: Last 10 order events (compact view for sidebar).

**Interaction**: Click row to open full `/activity` page.

---

## 7. Closed-Hours and Snapshot Behavior

**During closed hours** (23:30–09:15 IST weekdays, all-day weekends):

- Chart shows historical data unchanged (no live updates)
- Sidebar tabs (NAV, Capital, Equity) serve last-good `daily_book` snapshots from prior session close
- No polling; data frozen at market close timestamp
- `as_of` label shows timestamp of last snapshot (e.g., "as of 15:30 IST")

**Snapshot persistence**:
- On `<exch>:close` event (NSE 15:30, MCX 23:30 IST): `snapshot_daily_book()` writes to DB
- On next market open: `daily_book` row from prior session reloaded; display updates
- Survives app restart (data persisted)

**Sim mode exception**:
- Dashboard shows real Kite data during sim (public page `PerformancePage` visible)
- Operator can compare strategy results vs live market

---

## 8. Execution Mode Display

**Mode indicator** (header badge):
- SIM: green pill "SIM"
- PAPER: sky-blue pill "PAPER"
- LIVE: red pill "LIVE"
- SHADOW: orange pill "SHADOW"
- REPLAY: green pill "REPLAY"

**Chart behavior**:
- SIM: shows fabricated broker state (simulator quotes)
- PAPER: shows live quotes (5s refresh) but no real orders executed
- LIVE: shows real broker data + real orders (prod only)
- SHADOW: shows live data, logs order payloads (no execution)
- REPLAY: shows historical OHLCV only (backtest mode)

**Sidebar behavior** (consistent across all modes):
- NAV, Capital, Equity always computed from current mode's positions/holdings
- SIM mode shows sim positions; switching to LIVE updates sidebar (no stale bleed)

---

## 9. Edge Cases

### No positions or holdings

- NAV pie chart empty (all value in cash)
- Capital tab shows all available (nothing deployed)
- Equity tab empty
- Chart shows flat line (zero P&L for the day)

### Market just opened (0 poll cycles complete)

- Day P&L shows 0 until first poll lands (wait suppression via timestamp guard)
- Prevents yesterday's stale `day_change_val` from painting briefly
- Chart remains unchanged (no intraday ticks yet)

### Overnight position bought today

- Position row: `overnight_qty=0`, `pnl≠0`, `day_change_val=0`
- `baseDayPnlForPosition()` returns `pnl` (not `day_change_val`)
- Dashboard hero `_todayPnl` uses correct P&L

### Broker outage or session loss

- Sidebar serves last-good snapshot from 5 min ago
- "Data may be stale" warning badge appears
- Recovery on next successful poll (warning clears)
- Chart unchanged (historical data unaffected)

### Empty historical data

- Chart has no `nav_daily` rows (new account, cold start)
- Chart shows placeholder: "No historical data yet" or blank canvas
- Sidebar functional (live snapshot available)
- Data populates after first market close

---

## 10. API Contract

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/nav/me` | GET | Required | Current NAV + day delta |
| `/api/nav/me/history?days=180` | GET | Required | NAV curve (daily snapshots) |
| `/api/funds` | GET | Required | Margin + cash aggregates |
| `/api/positions` | GET | Required | Open position list + P&L |
| `/api/holdings` | GET | Required | Holdings list + MTM value |
| `/api/admin/nav/compute` | POST | Admin | Trigger daily NAV recalc |

**NAV history response** (`/api/nav/me/history`):
```json
{
  "history": [
    {"date": "2026-01-01", "nav": 1000000.0, "pnl_day": 5000.0, "pnl_pct": 0.50},
    {"date": "2026-01-02", "nav": 1005000.0, "pnl_day": 5000.0, "pnl_pct": 0.50}
  ]
}
```

**Funds response** (`/api/funds`):
```json
{
  "accounts": [
    {
      "account": "ZG0790",
      "cash": 500000.0,
      "avail_margin": 200000.0,
      "used_margin": 100000.0,
      "collateral": 0.0
    }
  ]
}
```

---

## 11. Test Coverage Map

### Frontend — Playwright

- **Chart range**: Switch 1W → 1M → ALL; bars update correctly
- **Sidebar tab switching**: NAV → Capital → Equity; correct data per tab
- **NAV formula SSOT**: Dashboard NAV matches MarketPulse Positions grid TOTAL NAV
- **Day P&L hero**: Dashboard `_todayPnl` matches NavStrip P:1; uses `baseDayPnlForPosition()`
- **Closed-hours snapshot**: After 15:30, sidebar shows frozen snapshot with `as_of` timestamp
- **Activity card**: Orders tab active; click row opens `/activity` page
- **Mode badge**: SIM/PAPER/LIVE shown correctly in header

### Backend — pytest

- **NAV v4 formula**: Σ(cash + premium) + Σ(unrealised) + Σ(holdings.cur_val) matches expected total
- **Daily book snapshots**: Idempotent UPSERT at market close; survive restart
- **Closed-hours routes**: `/api/nav/me` returns snapshot with `as_of` when market closed
- **Funds aggregation**: `avail_margin` + `used_margin` sums correctly across accounts
- **New-position override**: Overnight qty=0 position shows correct day P&L (uses `pnl` not `day_change_val`)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit; NAV v4 formula, day P&L SSOT, activity card integration |
