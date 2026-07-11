# Chart Specification

Single source of truth for the Chart Workspace behavior across symbol selections,
data ranges, and market states. The chart unifies historical OHLCV, intraday ticks,
and optional overlay indicators into a single canonical view.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/ChartWorkspace.svelte` · `frontend/src/lib/data/chartStore.svelte.js` · `backend/api/routes/charts.py`

---

## Contents

1. [Chart Types and Surfaces](#1-chart-types-and-surfaces)
2. [Indicators and Overlays](#2-indicators-and-overlays)
3. [ChartStore State Management](#3-chartstore-state-management)
4. [Symbol Change Lifecycle](#4-symbol-change-lifecycle)
5. [Data Freshness and TTL](#5-data-freshness-and-ttl)
6. [Overlay and Indicator Persistence](#6-overlay-and-indicator-persistence)
7. [Edge Cases](#7-edge-cases)
8. [API Contract](#8-api-contract)
9. [Test Coverage Map](#9-test-coverage-map)

---

## 1. Chart Types and Surfaces

ChartWorkspace renders four different chart modes: Line, Area, Candle, and Plot.
The chart displays price action in the primary panel with volume bars always visible
(hard-coded ON).

**Surfaces**:
- `/charts` page: full workspace with symbol picker, range selector, and overlay controls
- `ChartModal`: compact embedded chart (compact=true) suppresses picker bar and Greeks strip
- Derivative overlays: Greeks strip for options (delta/gamma/vega/theta/rho)
- Spot overlay: underlying price line for options and futures (trades against the same primary axis)

**Symbol classification**:
- Underlying (e.g., NIFTY, BANKNIFTY): renders spot history, no Greeks
- Derivative (e.g., NIFTY25APR22000CE): renders derivative + underlying spot + Greeks
- Other (equity, unrecognized): renders price only

---

## 2. Indicators and Overlays

**Price-panel overlays** (drawn on the main price series):
- SMA 20 and SMA 50 (simple moving averages)
- EMA 20 and EMA 50 (Wilder's exponential moving averages)
- VWAP (volume-weighted average price; null on zero-volume bars, cyan color)
- Bollinger Bands (±2 sigma, 20-period; lower/middle/upper lines)

**Sub-panels** (dedicated vertical space below price):
- RSI 14 (relative strength index; reference lines at 30/70)
- MACD (12/26/9 histogram + signal line + zero-crossover line)

Overlay set is user-selectable via MultiSelect and persisted across page reloads
via localStorage key `rbq.cache.chart-overlays.v1`.

---

## 3. ChartStore State Management

**chartStore** is a single-slot Svelte 5 state store exposed via module-level `$state`.
Both the `/charts` page and `ChartModal` read/write the same store, preventing duplicate
fetches when switching between surfaces.

**Reactive cells**:
- `symbol` — active tradingsymbol (auto-uppercased on set)
- `exchange` — exchange hint ('' = auto-detect)
- `days` — numeric range (1/7/30/90/180/365)
- `ohlcv` — array of OHLCV bars (null until first load)
- `spotBars` — array of underlying spot bars for derivatives
- `overlays` — array of active overlay keys (e.g., ['sma20', 'vwap'])
- `indicators` — derived subset of overlays that occupy sub-panels
- `loading` — true while a fetch is in flight
- `lastFetched` — `{symbol, exchange, days, at: timestamp}` TTL guard

**Consumer contract**:
- Read via getter properties: `chartStore.symbol`, `chartStore.ohlcv` (reactive in `$derived` and `$effect`)
- Write via setter methods: `chartStore.setSymbol(v)`, `chartStore.setOhlcv(bars, spotBars)`
- Direct state mutation not exposed (prevents stale-cache patterns)

---

## 4. Symbol Change Lifecycle

**Single-slot design**: Chart holds exactly one symbol at a time. No per-symbol LRU cache.

On symbol change (user picks a new symbol via search or pinned dropdown):

1. Call `chartStore.clearData()` — wipes ohlcv/spotBars, sets loading=true, clears lastFetched
2. Immediately triggers `$effect` watching `symbol`
3. `$effect` calls `_loadHistorical(true)` to fetch new bars
4. On fetch complete: `chartStore.setOhlcv(bars, spotBars)` records the data and lastFetched timestamp
5. Overlays and indicators **persist** across symbol changes (user preference, not data-driven)

**Invariant**: Old symbol's bars are never visible under the new symbol, even for a single frame.

---

## 5. Data Freshness and TTL

**TTL**: 30 seconds. Before fetching, `ChartWorkspace` checks `chartStore.isFresh()`:
```
isFresh() → true when cached bars match symbol/exchange/days AND fetched < 30s ago
```

**Freshness gate**:
- Same symbol, same exchange, same days range, fetched < 30s ago → serve cached data
- Any parameter changed → wipe cache via `clearData()` and fetch fresh
- Transitions between `/charts` page and `ChartModal` never trigger a duplicate fetch when the data is fresh

**Transition example**: Operator opens ChartModal for RELIANCE, browses 30 days of data (5 bars fetched);
closes modal; returns to /charts page with RELIANCE still loaded. The store's lastFetched timestamp
is < 30s old, so isFresh() returns true and the page uses the cached bars immediately.

---

## 6. Overlay and Indicator Persistence

**Overlay persistence key**: `rbq.cache.chart-overlays.v1` in localStorage

**Hydration**: Called once from `ChartWorkspace.onMount()` (SSR-safe):
```
chartStore.hydrateOverlays(knownKeys) — reads LS, validates against known set
```

**Write-back**: Any overlay selection change calls `chartStore.setOverlays(v)`, which
persists to localStorage immediately.

**Survival**:
- Page reload → hydrated from localStorage on next mount
- Operator switches symbols → overlays persist in-memory
- Operator closes and reopens ChartModal → hydrated fresh on next mount
- Browser storage cleared → next mount defaults to empty (no error)

**Indicator derivation**: `indicators` is a `$derived` filtering overlays to keys in
`_SUB_PANEL_KEYS` (currently ['rsi', 'macd']). Consumers can use this to reserve
vertical space for sub-panels or hide them entirely based on user selection.

---

## 7. Edge Cases

### No OHLCV bars returned

- Broker returns empty array (cold-start symbol, delisted security)
- Chart displays empty canvas; no error state (graceful failure)
- `reason` in API response distinguishes causes: `historical_fetch_fail`, `warm_universe_empty`

### Zero-volume bars

- VWAP computation guards against division by zero and returns null for that bar
- Chart renders a gap (null-suppression) or flat line, never a crash

### Missing intraday data

- Range < 30 days populated from ticks buffer; gaps render as breaks in the line
- Range ≥ 30 days falls back to OHLCV only (ticks buffer too small)

### Derivative without underlying

- Underlying spot data unavailable (delisted index, exchange outage)
- Chart renders derivative bars only; spot overlay omitted gracefully

### MCX/CDS contract rollover

- Virtual symbol (CRUDEOIL) must resolve to current-month contract before fetch
- Unsolved: chart picks a fixed contract; no auto-rollover during intraday viewing
- Mitigated by operator manually selecting the new contract before rollover expiry

---

## 8. API Contract

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/options/historical?mode=…&symbol=…&days=…` | GET | Optional | OHLCV bars + spot bars (derivatives) |
| `/api/charts/price-history?mode=…&symbol=…&since=…` | GET | Optional | Intraday tick buffer + order events |
| `/api/charts/symbols?mode=…` | GET | Optional | List all symbols with captured ticks |

**Response shape** (`options/historical`):
```json
{
  "symbol": "NIFTY25APR22000CE",
  "exchange": "NFO",
  "bars": [
    {"date": "2026-01-01", "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 1000000}
  ],
  "spot_bars": [
    {"date": "2026-01-01", "close": 18500.0}
  ]
}
```

**Response shape** (`charts/price-history`):
```json
{
  "mode": "live",
  "symbol": "NIFTY25APR22000CE",
  "kind": "derivative",
  "underlying": "NIFTY",
  "ticks": [
    {"ts": "2026-01-01T09:15:00Z", "ltp": 100.5, "bid": 100.3, "ask": 100.7}
  ],
  "events": [
    {"ts": "2026-01-01T09:16:00Z", "kind": "placed", "side": "BUY", "price": 100.0, "status": "PENDING", "order_id": 1, "attempts": 1, "qty": 1}
  ]
}
```

---

## 9. Test Coverage Map

### Frontend — Playwright

- **Symbol change**: `clearData()` wipes ohlcv/loading correctly; old bars never visible
- **TTL freshness**: Same symbol/exchange/days < 30s old skips fetch; any parameter change fetches
- **Overlay persistence**: Selection writes to localStorage; survives page reload
- **Indicator derivation**: Sub-panel keys correctly filtered from overlay array
- **Range selection**: Switching 1D ↔ 1M ↔ 1Y fetches new bars; TTL still applies
- **Zero-volume VWAP**: Bars with zero volume render without crashing; VWAP null on those bars

### Backend — pytest

- **Historical fetch ladder**: DB → broker → write-back
- **Price-history tick buffer**: Deque trimming, symbol classification (underlying/derivative/other)
- **Order event derivation**: AlgoOrder lifecycle transitions map to chart markers
- **Underlying resolution**: Options/futures parse correctly; invalid symbols skip spot overlay

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit; single-slot design, 30s TTL, overlay persistence |
