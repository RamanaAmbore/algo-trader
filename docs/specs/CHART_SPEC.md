# Chart Specification

Exhaustive behavioral reference for the Chart Workspace across symbol
selections, data ranges, modes (live/paper/sim), overlays, and market states.
The chart unifies historical OHLCV bars, intraday tick streaming, indicator
overlays, and signal markers into a single canonical workspace.

**Version**: 2.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/ChartWorkspace.svelte` ·
`frontend/src/lib/ChartModal.svelte` · `frontend/src/routes/(algo)/charts/+page.svelte` ·
`frontend/src/lib/data/chartStore.svelte.js` · `backend/api/routes/instruments.py`

---

## Contents

1. [Surface Variants — ChartModal vs /charts Page](#1-surface-variants--chartmodal-vs-charts-page)
2. [Chart Store — SSOT](#2-chart-store--ssot)
3. [Symbol Change Lifecycle](#3-symbol-change-lifecycle)
4. [Data Freshness and TTL](#4-data-freshness-and-ttl)
5. [Historical OHLCV Fetch](#5-historical-ohlcv-fetch)
6. [Intraday Tick Streaming](#6-intraday-tick-streaming)
7. [Rendering — Price Chart and Multi-Price Chart](#7-rendering--price-chart-and-multi-price-chart)
8. [Indicators — Calculation Spec](#8-indicators--calculation-spec)
9. [Signal Markers and Events](#9-signal-markers-and-events)
10. [Overlays — Spot and Greeks](#10-overlays--spot-and-greeks)
11. [Overlay and Indicator Persistence](#11-overlay-and-indicator-persistence)
12. [URL Parameter Sync (/charts Page)](#12-url-parameter-sync-charts-page)
13. [Backend API Contract](#13-backend-api-contract)
14. [Keyboard Shortcuts](#14-keyboard-shortcuts)
15. [Known Defects](#15-known-defects)
16. [Edge Cases and Mitigations](#16-edge-cases-and-mitigations)
17. [Test Coverage Map](#17-test-coverage-map)
18. [Change Log](#18-change-log)

---

## 1. Surface Variants — ChartModal vs /charts Page

### ChartModal (Embedded)

**File**: `frontend/src/lib/ChartModal.svelte`

Fixed-position modal overlay (amber-glow navy panel, `inset: 0` positioning).
Wraps ChartWorkspace with `compact=false` (full workspace minus the page-level
header). Optional symbol and exchange prefill. Closed via Esc key, overlay click
(X button), or programmatic `onClose()` callback.

**Opening**: Three entry points:
- Keyboard shortcut `k` (no args — opens with last-used symbol from chartStore)
- Context menu "Open Chart" from Pulse positions/holdings/watchlist rows
  (symbol pre-filled)
- Programmatic `openChartModalTrigger()` called from other surfaces

**Props**:
- `symbol` (optional) — initial tradingsymbol; defaults to chartStore.symbol
- `exchange` (optional) — exchange hint for historical fetch
- `mode` (live|sim|paper) — execution mode, defaults to 'live'
- `onClose` (callback) — fires when modal closes

**Busy state** — while a chart fetch is in flight:
- Chart body becomes `pointer-events: none` so the operator cannot queue a
  second fetch (no range pill change, no symbol re-pick, no overlay toggle)
- X close button + Esc key remain clickable
- Overlay stays `pointer-events: none` so navbar / menu underneath stay
  reachable (operator can navigate while fetching)
- Refresh icon rotates with `cm-refresh-spin` animation (1.1s)

**Header chrome**:
- Left: "Charts" title with chart glyph (cyan, static — no rotation)
- Right: rotating refresh icon + X close button (1.4rem square each)
- Gradient background (cyan-tinted, darker than body)

### /charts Page (Full Workspace)

**File**: `frontend/src/routes/(algo)/charts/+page.svelte`

Full-page route with symbol picker visible on the page (not just modal header).
Bookmarkable; URL params sync in real-time. Page header includes refresh button
and standard PageHeaderActions (Log + Order icons). Chart fills viewport below
the navbar.

**Props** (ChartWorkspace receives):
- `symbol` (bindable) — active tradingsymbol, two-way bound
- `loading` (bindable) — fetch state, true during OHLCV fetch
- `compact` (false) — show full picker + header
- `showHeader` (false) — page renders its own header
- `onSymbolChange` callback — fires when operator picks a new symbol via search

**Default symbol fallback chain**:
1. `?symbol=` URL param (takes highest precedence)
2. Recent symbol from localStorage (user's last pick on any surface)
3. Settings default symbol (operator-configured)
4. Fallback to 'NIFTY 50' (hardcoded final default)

---

## 2. Chart Store — SSOT

**File**: `frontend/src/lib/data/chartStore.svelte.js`

Single-slot Svelte 5 state store shared between ChartModal and /charts page.
Both surfaces read/write the same store, preventing duplicate fetches when
switching between them.

**Reactive state cells** (all private `$state`, exposed via getter properties):

| Cell | Type | Purpose |
|---|---|---|
| `symbol` | string | Active tradingsymbol (auto-uppercased) |
| `exchange` | string | Exchange hint ('' = auto-detect) |
| `days` | number | Range in days: 1/7/30/90/180/365 |
| `ohlcv` | array \| null | Historical bars (null until first load) |
| `spotBars` | array | Underlying spot bars for derivatives |
| `overlays` | array | Active overlay keys (e.g., ['sma20', 'vwap']) |
| `indicators` | array | Subset of overlays that occupy sub-panels (rsi, macd) |
| `loading` | boolean | True while a fetch is in flight |
| `lastFetched` | object \| null | `{symbol, exchange, days, at: timestamp}` TTL guard |

**Consumer contract** (read-only getters and setter methods):
- Read: `chartStore.symbol`, `chartStore.ohlcv` (reactive in `$derived`/`$effect`)
- Write: `chartStore.setSymbol(v)`, `chartStore.setDays(v)`, `chartStore.setOhlcv(bars)`
- Direct mutation forbidden (no access to raw `_symbol` cells)

**Key methods**:
- `setSymbol(v)` — uppercases and stores the symbol
- `setExchange(v)` — stores exchange hint
- `setDays(v)` — stores numeric range (persisted to localStorage)
- `setOhlcv(bars, spotBars)` — writes data and records fetch timestamp
- `clearOhlcv()` — wipes bars without altering lastFetched (symbol-change guard)
- `clearData()` — full reset: nulls ohlcv/spotBars, sets loading=true, clears lastFetched
- `setOverlays(v)` — stores array and persists to localStorage
- `setLoading(v)` — sets fetch state flag
- `hydrateOverlays(knownKeys)` — seeds overlays from localStorage on mount (SSR-safe)
- `isFresh()` — returns true if cached bars match current symbol/exchange/days AND < 30s old

---

## 3. Symbol Change Lifecycle

**Single-slot design**: Chart holds exactly one symbol at a time. No per-symbol LRU cache.

On symbol change (operator picks a new symbol via search or pinned dropdown):

1. **Call `chartStore.clearData()`** — wipes ohlcv/spotBars array, sets `loading=true`,
   clears `lastFetched`. This atomic operation ensures old symbol's bars are never
   visible under the new symbol, even for a single frame.

2. **Trigger `$effect` watching `symbol`** — immediately (synchronous).

3. **`$effect` calls `_loadHistorical(true)`** — initiates the fetch with `force=true`
   (bypasses TTL check).

4. **On fetch complete** — `chartStore.setOhlcv(bars, spotBars)` records both OHLCV
   bars and underlying spot bars (for derivatives), records the fetch timestamp.

5. **Overlays and indicators persist** — user preferences are NOT cleared on symbol
   change. If the operator had RSI and SMA50 active before switching symbols, those
   remain active after.

**Invariant**: Old symbol's bars are never visible under the new symbol, even for
a single render frame. If a fetch fails, the empty state appears after a 3-second
grace period.

---

## 4. Data Freshness and TTL

**TTL**: 30 seconds. Before fetching, ChartWorkspace checks `chartStore.isFresh()`:

```javascript
isFresh() → true when (cached bars match symbol/exchange/days
            AND fetched < 30s ago)
```

**Freshness gate**:
- Same symbol, same exchange, same days range, fetched < 30s ago → serve cached data
- Any parameter changed → wipe cache via `clearData()` and fetch fresh
- Transitions between `/charts` page and `ChartModal` never trigger a duplicate fetch
  when data is fresh

**Transition example**: Operator opens ChartModal for RELIANCE, browses 30 days of
data (5 bars fetched); closes modal; returns to /charts page with RELIANCE still
loaded. The store's `lastFetched` timestamp is < 30s old, so `isFresh()` returns
true and the page uses cached bars immediately without re-fetching.

---

## 5. Historical OHLCV Fetch

**Endpoint**: `GET /api/options/historical`

**Query params**:
- `mode` (required) — 'live', 'sim', 'paper', 'replay', or 'hypothetical'
- `symbol` (required) — tradingsymbol (may be a virtual root like CRUDEOIL)
- `days` (required) — 1/7/30/90/180/365
- `exchange` (optional) — NSE/NFO/BSE/MCX/CDS (auto-detect if omitted)

**Response shape**:
```json
{
  "symbol": "NIFTY25APR22000CE",
  "exchange": "NFO",
  "bars": [
    {
      "dt": "2026-01-01",
      "open": 100.0,
      "high": 105.0,
      "low": 95.0,
      "close": 102.0,
      "volume": 1000000
    }
  ],
  "spot_bars": [
    {
      "dt": "2026-01-01",
      "close": 18500.0
    }
  ],
  "partial": false,
  "reason": "success"
}
```

**Range mapping**:
- **1 day**: intraday 5-minute bars (today only)
- **5/7 days**: daily bars
- **30/90/180/365 days**: daily bars

**Bar count expectations**:
- 1d: ~80–100 bars (5 × ~16 hours market hours)
- 5d: 5 bars (daily)
- 1mo (30d): ~21 bars (business days)
- 1yr (365d): ~252 bars (annual trading days)

**Intraday logic** (1d range):
- 5-minute bars captured live during market hours
- Ticks buffer (in-memory deque) supplies the data
- Refreshed every 30s via ChartWorkspace polling
- Empty bars (zero volume) render as gaps in the line

**Error handling**:
- Empty bars (`[]`) → "No data available" state (after 3s grace period)
- 422 Unprocessable Entity → "No data" message + log error
- Network timeout → "Error fetching chart" + log error
- `partial=true` + delayed retry → suppress empty state until retry completes

**MCX/CDS virtual root resolution**:
- User enters bare root (CRUDEOIL, GOLD, USDINR) in symbol search
- Backend resolves to nearest-expiry future before fetching OHLCV
- Frontend displays the resolved contract in the chart header + "Front-month" chip

---

## 6. Intraday Tick Streaming

**Endpoint**: `GET /api/charts/price-history`

**File**: `frontend/src/lib/PriceChart.svelte` (intraday tick rendering component)

**Query params**:
- `mode` (required) — 'live', 'sim', 'paper', 'replay'
- `symbol` (required) — tradingsymbol

**Response shape**:
```json
{
  "mode": "live",
  "symbol": "NIFTY25APR22000CE",
  "kind": "derivative",
  "underlying": "NIFTY",
  "ticks": [
    {
      "ts": "2026-01-01T09:15:00Z",
      "ltp": 100.5,
      "bid": 100.3,
      "ask": 100.7
    }
  ],
  "events": [
    {
      "ts": "2026-01-01T09:16:00Z",
      "kind": "placed",
      "side": "BUY",
      "price": 100.0,
      "status": "PENDING",
      "order_id": 1,
      "attempts": 1,
      "qty": 1,
      "detail": null
    }
  ]
}
```

**Tick capture lifecycle** (per-symbol, per-mode):
- Ticks recorded once an order is open against the symbol
- Captured during live trading, sim run, paper trades, or replay playback
- No ticks pre-captured for cold-start symbols
- Deleted after 90 days (configurable via settings)

**Bid/ask band rendering**:
- Faint cyan shaded area between bid and ask paths
- Only drawn when both bid and ask are populated
- Live/paper mode shows spread band; pure-LTP-only ticks skip it cleanly

**Underlying overlay** (derivatives only):
- When `kind === 'derivative'` and `underlying` is set, fetched alongside primary ticks
- Rendered as sky-blue dashed line, normalized into the option's y-range
- Allows operator to see "spot −3% → call −40%" correlation at a glance

**Polling cadence**:
- Default 3 seconds (configurable via `pollMs` prop)
- Pauses when tab is hidden (uses `visibleInterval` store)
- Resumes immediately when tab becomes visible
- Parent can supply pre-fetched data via `data` prop to batch multiple symbols
  (one round-trip per refresh instead of N+N per symbol)

---

## 7. Rendering — Price Chart and Multi-Price Chart

### PriceChart (Single Symbol)

**File**: `frontend/src/lib/PriceChart.svelte`

Compact SVG line chart for price history during sim/paper/live trading.
No chart library — hand-rolled SVG is small and ships zero external JS.

**Canvas dimensions**:
- viewBox: 720 × `height` (default 180px)
- Padding: L=40, R=8, T=8, B=22
- Preserves aspect ratio with `preserveAspectRatio="none"` (scales to container width)

**Chart layers** (bottom to top):
1. Plot-area background tint (var(--chart-bg-tint), cyan)
2. Y-axis grid lines + price labels (5 evenly spaced)
3. X-axis grid lines + time labels (4 evenly spaced)
4. Bid/ask band (faint cyan shaded area, when available)
5. Previous close reference line (dashed amber, optional)
6. Underlying overlay line (sky-blue dashed, derivatives only)
7. LTP line (sky-blue for underlyings, amber for derivatives/equities)
8. Order event markers (colored circles: placed/filled/unfilled/chased)
9. Hover crosshair (amber dashed vertical line + tooltip)
10. Replay scrubber anchor (vertical amber dashed line, optional)

**Interactive gestures**:
- **Wheel zoom**: `deltaY > 0` zooms out, `< 0` zooms in (1.25× factor)
  - Narrows/widens x-domain around cursor position
  - Y-axis auto-fits to *visible* price range (tightens on zoom)
  - Clamps: never zoom out past full data range; minimum 1 second width
- **Drag pan**: click + drag left/right to slide the x-domain
  - Captured pointer events; cursor changes to `grabbing`
  - Y-axis updates to visible price range
- **Click-to-pin**: click chart to pin hover tooltip at nearest tick
  - Pins until next click or operator closes the tooltip (× button)
  - Tooltip shows: time (HH:MM:SS IST), LTP, bid/ask, order #, slippage

**Chart state during zoom/pan**:
- `zoom` (null | {xMin, xMax}) — when set, overrides auto x-domain
- `pan` (null | {startClientX, startMin, startMax}) — tracks drag-pan state
- `resetZoom()` button appears in header when `zoom !== null`

**Performance optimization**:
- Visible-ticks filtering: when zoomed, path-build uses only visible ticks
  (30× reduction in work on tight zoom)
- Lazy label updates: x/y tick labels re-calculated only when domain changes

### MultiPriceChart (Multi-Leg Comparison)

**File**: `frontend/src/lib/MultiPriceChart.svelte`

Overlay N price series on one SVG with shared x-axis (timestamps) and
normalized y-axis (% change from each series' first tick). Built for the
Lab simulator's per-leg view: comparing a long-call at ₹50 and short-strangle
wing at ₹2,000 side-by-side.

**Canvas dimensions**:
- viewBox: 720 × `height` (default 240px)
- Padding: L=44, R=16, T=8, B=28

**Series normalization**:
- Each series: base = first non-zero tick's ltp
- % = (current_ltp − base) / base
- Skips series with every tick = 0 (can't normalize)
- Trailing/mid-series zeros passed through (plot as −100% dips)

**Y-axis scales**:
- Linear (default): direct % values
- Symmetric-log (toggle): below ±1% linear, above log-compressed
  - Makes +400% long-call and +5% short-strangle both readable on same chart

**Unified domains**:
- X: union of all timestamps across all series
- Y: symmetric around 0; ±max(abs(pct)) × 1.10 (never < 1%)

**Hover crosshair**:
- Vertical amber dashed line + legend overlay
- Shows: closest tick per series, % change, raw LTP
- Anchors to median tick's x-coordinate

**Legend**:
- One row per series: swatch + LONG/SHORT label + symbol + account
- On hover: appends % change + LTP for each series

---

## 8. Indicators — Calculation Spec

### Price-Panel Overlays (Drawn on Price Series)

**SMA 20 and SMA 50** — Simple Moving Average
- Formula: `SMA_n = Σ(close[i..i+n-1]) / n`
- 20-period and 50-period windows
- Rendered as thin lines
- Null values skipped (gaps in the line)

**EMA 20 and EMA 50** — Exponential Moving Average (Wilder's)
- Smoothing factor: `α = 2 / (period + 1)`
- Seed: first EMA value = SMA over same period
- Formula: `EMA_new = (close × α) + (EMA_prev × (1 − α))`
- Rendered as thin lines

**VWAP** — Volume-Weighted Average Price
- Intraday cumulative only (resets daily)
- Formula: `VWAP = Σ(typical_price × volume) / Σ(volume)`
  - typical_price = (high + low + close) / 3
- Rendered cyan; null when volume = 0 for a bar (gap in line)
- Never shown on overnight windows (MCX close to next open)

**Bollinger Bands ±2σ** — 20-Period Bands
- Midline: SMA 20
- Upper: SMA 20 + (2 × rolling_stddev)
- Lower: SMA 20 − (2 × rolling_stddev)
- Rolling window: exactly 20 bars
- Rendered as shaded area (upper + lower bands + midline)

### Sub-Panel Indicators (Dedicated Vertical Space)

**RSI 14** — Relative Strength Index
- Wilder's smoothing (α = 1/14)
- Formula: `RSI = 100 − (100 / (1 + RS))` where `RS = avg_gain / avg_loss`
- Range: 0–100
- Reference lines: 30 (oversold), 70 (overbought), both light gray
- Rendered in separate panel below price chart

**MACD 12/26/9** — Moving Average Convergence Divergence
- Fast EMA: 12-period
- Slow EMA: 26-period
- MACD line: fast − slow
- Signal line: EMA(9) of MACD
- Histogram: MACD − signal
- Zero line: light gray dashed horizontal
- Rendered in separate panel (histogram + line + signal)

### Indicator Toggle and Persistence

**Overlay selection** — MultiSelect component displays all 8 options
- Default state: no overlays selected on first load
- Toggle: clicking option adds/removes it from the active set
- Persistence: `chartStore.setOverlays(v)` writes to localStorage

**Indicator derivation** — `$derived` filters overlays to sub-panel keys:
- Sub-panel keys: `['rsi', 'macd']`
- If neither selected, sub-panels reserve no vertical space
- Both selected: two dedicated panels below price chart

**localStorage key**: `rbq.cache.chart-overlays.v1`
- Format: JSON array of string keys
- Hydrated on ChartWorkspace mount (SSR-safe)
- Validated against known overlay options on load

---

## 9. Signal Markers and Events

**Source**: `events` array from `/api/charts/price-history` response

**Event types**:
- `placed` — order created (amber circle + dot)
- `filled` — order fully filled (emerald circle + dot)
- `unfilled` — order cancelled/expired (red circle + dot)
- `chased` — order modified during execution (sky-blue circle + dot)

**Marker rendering**:
- Outer circle (r=6): colored fill (low opacity 0.18) + colored stroke (1.5px)
- Inner dot (r=2.5): solid colored fill
- Positioned at event timestamp on x-axis, order price on y-axis

**Marker hover tooltip**:
- Title: event kind · side · qty
- Line 2: price × qty = total @ time
- Line 3: order #N · slippage (if any)
- Box size: 200px wide, height varies (56px base + 14px if slippage)
- Colors: amber title text, cyan body, slate secondary info

**Signal markers** — operator-defined or algo-derived:
- Buy signal: green triangle / arrow at bar bottom
- Sell signal: red triangle / arrow at bar top
- Sourced from agent rules or manual configuration
- Toggle: persisted to localStorage
- Default: signals ON (unless user toggles them off)

**Known issue**: Signal markers not yet implemented in the current codebase —
currently only order event markers (placed/filled/unfilled/chased) are rendered.

---

## 10. Overlays — Spot and Greeks

### Underlying Spot Overlay

**Applicable to**: F&O instruments (options and futures)

**Fetch logic**:
1. Detect if symbol is a derivative (`_isDerivative` derived)
2. Parse underlying from symbol (first N uppercase letters before expiry/strike)
3. When fetching OHLCV, also fetch `/api/charts/price-history` for underlying
4. If underlying fetch fails, silently omit overlay (no error shown)

**Rendering**:
- Sky-blue dashed line (stroke-dasharray: "3 3", opacity 0.7)
- Rescaled into the option's y-range (top = 10%, bottom = 90% of plot area)
- Normalized so operator sees spot move shape alongside option price
  (e.g., spot 22,000 scaled to fit alongside option 180)
- Legend row in header: dashed-dash sample + "NIFTY" (underlying name)

**Visual semantics**:
- Allows operator to read "spot −3% → call −40%" correlation at a glance
- Answers: "did my option decay while spot was flat?"
- Useful for debugging theta/gamma relationships during live trading

### Greeks Strip (Options Only)

**Status**: Planned but not yet implemented in current release

**Expected to show** (when implemented):
- Delta (rate of change vs spot)
- Gamma (rate of delta change)
- Vega (sensitivity to volatility)
- Theta (daily decay)
- Rho (sensitivity to interest rates)

**Fetch logic** (when shipped):
- Triggered by `/api/options/analytics?symbol=…`
- Recalculated whenever LTP or spot changes
- Black-Scholes engine with implied-vol calibration

---

## 11. Overlay and Indicator Persistence

**Persistence key**: `rbq.cache.chart-overlays.v1` in localStorage

**Storage format**: JSON array of string keys
```json
["sma20", "ema20", "vwap", "rsi", "macd"]
```

**Hydration** — Called once from `ChartWorkspace.onMount()`:
```javascript
chartStore.hydrateOverlays(knownKeys) // knownKeys = all overlay options
```
- Reads localStorage
- Validates each key against the set of known overlays
- Filters to valid keys only (guards against stale cache)
- If no valid keys, overlays start empty (no error)

**Write-back** — Any overlay selection change:
- User clicks MultiSelect option
- `chartStore.setOverlays(v)` called with new array
- Immediately written to localStorage (synchronous)

**Survival**:
- Page reload → hydrated from localStorage on next mount
- Operator switches symbols → overlays persist in-memory (user preference, not data-driven)
- Operator closes and reopens ChartModal → hydrated fresh on next mount
- Browser storage cleared (localStorage unavailable) → next mount defaults to empty

**Indicator derivation**:
- `indicators` is a `$derived` computed property filtering overlays to sub-panel keys
- Keys: `['rsi', 'macd']`
- When RSI selected, sub-panel space reserved; when MACD selected, second sub-panel
- When neither selected, no sub-panels drawn

---

## 12. URL Parameter Sync (/charts Page)

**URL structure**: `/charts?symbol=NIFTY&exchange=NSE&range=1mo`

**Params** (all optional, case-insensitive):
- `symbol` — tradingsymbol (uppercased on read)
- `exchange` — NSE/NFO/BSE/MCX/CDS (auto-detect if omitted)
- `range` — days (1/7/30/90/180/365); aliased as `1d`/`1w`/`1mo`/`3mo`/`6mo`/`1y`

**Lifecycle**:

1. **On mount** (`+page.svelte` onMount):
   - Read `page.url.searchParams` for symbol/exchange/range
   - If `?symbol=` param, use it (highest priority)
   - Else if chartStore has symbol, use that (operator closed modal, page reopened)
   - Else resolve default symbol: recent → settings → NIFTY 50 fallback
   - Seed chartStore with resolved symbol

2. **On symbol change** (operator picks via search):
   - Call `_onSymbolChange(sym)`
   - Update URL via `goto(url, { replaceState: true, noScroll: true, keepFocus: true })`
   - No history entry (replaceState), no scroll jump, focus stays on input
   - chartStore.setSymbol() called in parallel

3. **On range change** (operator clicks range pill):
   - URL param updated (replaceState)
   - chartStore.setDays() called
   - Fetch triggered via $effect watching chartStore.days

**Deep-link from cold start**:
- `/charts?symbol=RELIANCE&exchange=NSE&range=30` loads directly
- On mount, symbol resolves to RELIANCE, range to 30 days
- Fetch fires immediately without user interaction
- First paint shows loading state; data renders when fetch completes

**State persistence**:
- URL is primary (survives reload, sharing, bookmarks)
- chartStore secondary (survives modal close/reopen, session context)
- localStorage (series type, overlays, range default) persists across sessions

---

## 13. Backend API Contract

### GET /api/options/historical

Fetch historical OHLCV bars for any symbol (equity, future, option).

**Request**:
```
GET /api/options/historical?mode=live&symbol=NIFTY&days=30&exchange=NSE
```

**Query params**:
| Param | Type | Required | Example | Notes |
|---|---|---|---|---|
| `mode` | string | yes | live, sim, paper, replay, hypothetical | Execution mode |
| `symbol` | string | yes | NIFTY, RELIANCE, NIFTY25APR22000CE | Tradingsymbol (MCX roots OK) |
| `days` | int | yes | 1, 7, 30, 90, 180, 365 | Range in days |
| `exchange` | string | no | NSE, NFO, MCX, CDS, BSE | Auto-detect if omitted |

**Response** (200 OK):
```json
{
  "symbol": "NIFTY25APR22000CE",
  "exchange": "NFO",
  "bars": [
    {
      "dt": "2026-01-01T09:15:00Z",
      "open": 100.0,
      "high": 105.0,
      "low": 95.0,
      "close": 102.0,
      "volume": 1000000
    }
  ],
  "spot_bars": [
    {
      "dt": "2026-01-01T09:15:00Z",
      "close": 18500.0
    }
  ],
  "partial": false,
  "reason": "success"
}
```

**Response codes**:
- 200 — bars fetched (may be empty if no data available)
- 422 — missing required param (symbol, mode, days)
- 500 — internal error (broker outage, DB error)

**Empty bars** (`"bars": []`):
- Returned when symbol exists but has no OHLCV data
- Reasons: delisted, brand new, no tick history
- Frontend shows "No data available" state after 3s grace period

**Partial flag** (`"partial": true`):
- Returned when fetch succeeded but data is incomplete (e.g., broker returned partial bars)
- Frontend enqueues a delayed retry (2.5s) and suppresses empty state until retry completes

### GET /api/charts/price-history

Fetch intraday tick history + order events for a symbol during a specific mode.

**Request**:
```
GET /api/charts/price-history?mode=live&symbol=NIFTY25APR22000CE
```

**Query params**:
| Param | Type | Required | Example |
|---|---|---|---|
| `mode` | string | yes | live, sim, paper, replay |
| `symbol` | string | yes | NIFTY25APR22000CE |

**Response** (200 OK):
```json
{
  "mode": "live",
  "symbol": "NIFTY25APR22000CE",
  "kind": "derivative",
  "underlying": "NIFTY",
  "ticks": [
    {
      "ts": "2026-01-01T09:15:30Z",
      "ltp": 100.5,
      "bid": 100.3,
      "ask": 100.7
    }
  ],
  "events": [
    {
      "ts": "2026-01-01T09:16:00Z",
      "kind": "placed",
      "side": "BUY",
      "price": 100.0,
      "status": "PENDING",
      "order_id": 1,
      "attempts": 1,
      "qty": 1,
      "detail": null
    }
  ]
}
```

**Response codes**:
- 200 — ticks + events fetched (may be empty arrays if no data)
- 500 — internal error

**Tick fields**:
- `ts` — ISO 8601 UTC timestamp
- `ltp` — last traded price (required)
- `bid`, `ask` — optional; only both or neither shown

**Event kinds**:
- `placed` — order created
- `filled` — order fully filled
- `unfilled` — order cancelled/expired
- `chased` — order modified during execution

### GET /api/instruments

Master instrument dump for client-side symbol autocomplete.

**Request**:
```
GET /api/instruments
```

**Response** (200 OK):
```json
{
  "cycle_date": "2026-07-11",
  "count": 156234,
  "items": [
    {
      "s": "RELIANCE",
      "e": "NSE",
      "t": "EQ",
      "ls": 1,
      "ts": 0.05
    },
    {
      "s": "NIFTY25APR22000CE",
      "e": "NFO",
      "t": "CE",
      "u": "NIFTY",
      "x": "2026-04-03",
      "k": 22500,
      "ls": 50,
      "ts": 0.05
    }
  ]
}
```

**Field abbreviations** (payload minimization):
| Field | Type | Example | Meaning |
|---|---|---|---|
| `s` | string | RELIANCE | tradingsymbol |
| `e` | string | NSE | exchange |
| `t` | string | EQ, FUT, CE, PE | instrument_type |
| `u` | string | NIFTY | underlying (F&O only) |
| `x` | string | 2026-04-03 | expiry (F&O only, YYYY-MM-DD) |
| `k` | float | 22500 | strike (options only) |
| `ls` | int | 50 | lot_size |
| `ts` | float | 0.05 | tick_size |

**Cache**:
- Daily TTL (24h), re-warmed at 08:00 IST
- Kite-only source (never falls over to Dhan/Groww)
- MCX lot-size overrides applied (e.g., CRUDEOIL = 100, not 1)

---

## 14. Keyboard Shortcuts

**File**: `frontend/src/routes/(algo)/+layout.svelte`

**Discoverable via `?`** (opens ShortcutCheatsheet modal)

**Rules**:
- Pause while typing in input/textarea/select/contenteditable
- Esc defocuses active field (allows shortcuts to fire next)
- Cmd+K / Ctrl+K bypass input pause (command palette exception)
- All single-key shortcuts case-insensitive

### Navigation (Two-Key, Bloomberg-style)

**Pattern**: `g` then letter within 800ms

| Shortcut | Target | Notes |
|---|---|---|
| `g p` | /pulse | Watchlists + positions + holdings |
| `g d` | /dashboard | Performance + NAV breakdown |
| `g o` | /orders | Order ticket + activity |
| `g e` | /admin/derivatives | Options analytics |
| `g c` | /charts | Chart workspace (this page) |
| `g v` | /performance | Full performance page |
| `g a` | /automation | Agent rules + alerts |
| `g h` | /admin/history | Order history + audit log |
| `g m` | /pulse#movers | Jump to movers section |

### Single-Key Actions

| Shortcut | Action |
|---|---|
| `k` (outside grid) | Open ChartModal with last-used symbol |
| `k` (inside ag-Grid) | Arrow-up navigation (reserved for grid) |
| `t` | Open OrderTicket modal |
| `h` | Open Activity log modal |
| `/` | Focus symbol search input |
| `r` | Refresh page (dispatches `refresh-page` event) |
| `?` | Toggle keyboard shortcut cheatsheet |
| `Esc` | Defocus active field / close cheatsheet |

### Grid-Specific (ag-Grid Focus)

| Shortcut | Action |
|---|---|
| `j` | Arrow-down (next row) |
| `k` | Arrow-up (previous row) |
| `f` | Fullscreen toggle on nearest card |
| `Enter` | Context menu (when cell focused) |

---

## 15. Known Defects

### P1: Chart Hang on Null/Unresolved Symbol

**Symptom**: When ChartModal or /charts page opens with `chartStore.symbol = null`
(first-ever open, or symbol resolve failed), the chart stays in perpetual loading
spinner. No error message shown. Operator cannot see data or close the chart.

**Root cause**: `clearData()` runs but the subsequent fetch fires with null symbol.
Backend returns 422 Unprocessable Entity. Frontend receives the error but does NOT
update the UI — no error state rendered, no clear message shown.

**Workaround**: Close and reopen ChartModal, explicitly select a valid symbol via
the picker.

**Fix** (not yet shipped): Guard fetch with `if (!symbol) return;` and show
"Select a symbol" empty state before any fetch is attempted. Validate symbol
on mount; if empty, render a "Pick a symbol to start" placeholder with the
symbol search input focused.

**Tracked as**: defect P1, affects ChartModal on cold start

### P2: MCX Rollover Stale Bars

**Symptom**: On contract rollover day, OHLCV bars may mix prior-month and
current-month data if the virtual root resolves to the expiring contract.
Chart shows a price discontinuity (e.g., CRUDEOIL close at 7,200 → next bar
open at 6,800).

**Root cause**: Virtual symbol resolution (CRUDEOIL → CRUDEOIL26JUNFUT) is
done once at fetch time. If the contract expires during the operator's viewing
session, a manual refresh would pick the next-month contract. But without
refresh, the chart continues plotting bars from both contracts.

**Mitigated by**: Operator manually selecting the new contract before rollover
expiry (e.g., pick CRUDEOIL29JULFUT once 26JUN expires).

**Fix** (future enhancement): Auto-rollover detection — compare expiry date
of resolved contract to today; if expired, re-resolve and reload.

---

## 16. Edge Cases and Mitigations

### No OHLCV Bars Returned

- Broker returns empty array (cold-start symbol, delisted security)
- Chart displays empty state message "No data available"
- After 3 seconds from symbol change, the empty state is shown (grace period
  suppresses transient races)
- `reason` in API response stored but not user-facing (logged for debugging)

### Zero-Volume Bars

- VWAP computation guards against division by zero and returns null for that bar
- Chart renders a gap (null-suppression) in the VWAP line, never a crash
- Other overlays (SMA, EMA, Bollinger) still compute normally (volume isn't used)

### Missing Intraday Data

- Range < 30 days populated from ticks buffer (captured during trading)
- Range ≥ 30 days falls back to OHLCV only (ticks buffer too small)
- Gaps in tick capture render as breaks in the line (no interpolation)

### Derivative Without Underlying

- Underlying spot data unavailable (delisted index, exchange outage, or API error)
- Chart renders derivative bars only; spot overlay omitted gracefully
- No error shown; legend "Underlying" row simply absent
- Operator still sees the option's Greeks/payoff analysis

### Symbol Change Race (Multi-Click)

- Operator picks symbol A, then symbol B before A's fetch completes
- `clearData()` fires on symbol B change
- Response from A's fetch arrives after B is already loading
- Store checks `lastFetched.symbol` — response's symbol doesn't match current symbol
- Response is discarded silently (guard in ChartWorkspace's `$effect` watch)

### Browser Storage Unavailable (Private Mode)

- localStorage not available (Firefox private, Safari private)
- Overlay/range persistence fails silently (no error thrown)
- Chart still works; overlays default to empty on each session
- No UI error shown

### Very Large Date Ranges (1yr+ on Mobile)

- 252 bars fetch successfully
- Chart canvas scales to container width
- On narrow viewports, bars may appear dense
- Zoom/pan allows operator to inspect detail (no degradation)

---

## 17. Test Coverage Map

### Frontend — Playwright

**Test areas**:

1. **Symbol change**
   - `clearData()` wipes ohlcv immediately; old bars never visible
   - Loading state shown during fetch
   - Concurrent symbol changes (pick A, pick B before A completes): B wins

2. **TTL freshness**
   - Same symbol/exchange/days < 30s old → cached data served
   - Any param change → cache wipes, new fetch fires
   - Modal → page transition with fresh data: no duplicate fetch

3. **Opening and closing**
   - Keyboard `k` → ChartModal opens with last-used symbol
   - Context menu "Open Chart" → modal prefilled with row's symbol
   - Esc key closes modal
   - Overlay click closes modal (future enhancement)

4. **URL params (/charts page)**
   - `/charts?symbol=RELIANCE&exchange=NSE&range=30` cold-start → data fetches
   - Symbol picker change → URL updates (replaceState, no history entry)
   - Range pill change → URL + store both update
   - Deep-link sharing works (paste URL into new tab)

5. **Overlays**
   - MultiSelect toggle → selected overlays render
   - Selection persists across page reload
   - Symbol change → overlay selection preserved (not cleared)
   - Toggle individual overlays: SMA20, EMA50, VWAP, Bollinger, RSI, MACD

6. **Indicators**
   - RSI 14 sub-panel renders when rsi selected
   - MACD sub-panel renders when macd selected
   - Both together: two sub-panels stacked
   - Sub-panel space reclaimed when toggled off

7. **Zoom and pan (PriceChart)**
   - Wheel zoom in/out around cursor
   - Drag pan left/right (pointer capture)
   - `resetZoom()` button restores full data range
   - Y-axis auto-fits to visible price range

8. **Underlying overlay (derivatives)**
   - Options: spot line rendered sky-blue dashed
   - Futures: spot line rendered sky-blue dashed
   - Equities: no spot line shown
   - Missing underlying: graceful omission (no error)

9. **Hover tooltip**
   - Click chart → pins tooltip at nearest tick
   - Tooltip shows: time, price, bid/ask, order # (if event)
   - Click again → unpins tooltip
   - Move mouse away → tooltip disappears (if not pinned)

10. **Edge cases**
    - Empty bars response → "No data available" state
    - 422 error response → "Error fetching chart" message
    - Network timeout → "Error" message (not a crash)
    - Null symbol on cold open → should show "Select a symbol" (currently fails)

### Backend — pytest

**Test areas**:

1. **OHLCV fetch ladder**
   - DB cache hit: returns within 1ms, no broker call
   - Cache miss: broker call → write-back to DB
   - Stale DB row (> TTL): refreshed from broker

2. **Virtual symbol resolution**
   - MCX bare root (CRUDEOIL) → resolves to nearest-expiry future
   - CDS bare root (USDINR) → resolves to nearest-expiry future
   - Index (NIFTY) → maps to spot ticker
   - Invalid symbol → error response (422)

3. **Range mapping**
   - 1d → intraday 5m bars (today only)
   - 5d → daily bars (5 bars)
   - 1mo → daily bars (~21 bars)
   - 1yr → daily bars (~252 bars)

4. **Spot overlay (derivatives)**
   - Option symbol + derivative kind → spot bars fetched in parallel
   - Spot fetch fails → omitted gracefully (no error)
   - Underlying resolution → correct symbol extracted and fetched

5. **Price-history tick buffer**
   - Mode selection: live, sim, paper, replay each read correct buffer
   - Symbol classification: underlying, derivative, other
   - Event markers: placed, filled, unfilled, chased
   - Tick count limits: 600+ ticks retained, older purged

6. **Batch endpoint** (if implemented)
   - Single round-trip fetches OHLCV + underlying spots for N symbols
   - ~300ms latency per symbol + underlying fetch (parallelized)

### Known Coverage Gaps

- Indicator calculation correctness (SMA, EMA, RSI, MACD math not unit-tested)
- Signal marker rendering (feature not yet implemented)
- Tick overlay refresh cadence (3s polling not explicitly tested)
- Null symbol hang — no test for this defect; should add a failing test
  before fixing it

---

## 18. Change Log

| Date | Version | Change |
|---|---|---|
| 2026-07-11 | 2.0 | Expanded from v1.0 with exhaustive coverage: ChartModal/page surfaces, chartStore SSOT, symbol lifecycle, OHLCV/tick fetch, PriceChart/MultiPriceChart rendering, all 8 indicators spec'd, signal markers, spot/Greeks overlays, URL params, keyboard shortcut `k`, known defects P1/P2, edge cases, test map, API contract. |
| 2026-07-11 | 1.0 | Initial spec from codebase audit; single-slot design, 30s TTL, overlay persistence |
