// chartStore.svelte.js — shared Svelte 5 chart state store.
//
// Holds the "current chart" state that must be shared between
// ChartModal and the /charts page so switching surfaces never
// triggers a duplicate fetch.
//
// Reactive fields (all private $state, exposed via getter properties):
//   symbol      — active tradingsymbol (uppercased)
//   exchange    — exchange hint ('NSE', 'MCX', 'CDS', '' = auto)
//   days        — numeric range matching _RANGE_OPTS (1/7/30/90/180/365)
//   ohlcv       — array of OHLCV bars (null until first load)
//   spotBars    — array of underlying spot bars for derivatives
//   overlays    — array of active overlay keys (e.g. ['sma20','vwap'])
//   indicators  — subset of overlays that are sub-panel indicators (rsi, macd)
//   loading     — true while a fetch is in flight
//   lastFetched — { symbol, exchange, days, at: timestamp } — TTL guard
//
// Overlay persistence:
//   Overlays are written to localStorage under 'rbq.cache.chart-overlays.v1'
//   (same key ChartWorkspace used before this refactor). Call
//   hydrateOverlays() once from ChartWorkspace's onMount to seed.
//
// TTL:
//   30 seconds.  ChartWorkspace reads isFresh() before fetching so a
//   modal→page transition never round-trips when data < 30s old.
//
// Consumer contract:
//   Components READ via chartStore.symbol (getter) — reactive in $derived
//   and $effect.  Components WRITE via chartStore.setSymbol(v) etc.
//   Raw state cells are never exported so consumers cannot write directly.
//
// Svelte 5 note:
//   $state() at module scope in a .svelte.js file is valid (Svelte 5
//   processes runes in files ending with .svelte.js). Getter properties
//   on the returned object preserve reactivity because Svelte tracks reads
//   through any JS property access, not just direct $state variable reads.

import { readChartPref, writeChartPref } from '$lib/data/chartPrefs';

const _OVERLAY_LS_KEY   = 'rbq.cache.chart-overlays.v1';
const _SYMBOL_LS_KEY    = 'rbq.cache.chart-symbol.v1';
const _EXCHANGE_LS_KEY  = 'rbq.cache.chart-exchange.v1';
const _DAYS_LS_KEY      = 'rbq.cache.chart-days.v1';
const _CHART_TYPE_LS_KEY = 'rbq.cache.chart-type.v1';
const _FETCH_TTL_MS     = 30_000;

// ── Sub-panel indicator keys ─────────────────────────────────────────
// These overlay keys trigger dedicated sub-panels below the price chart
// (RSI 14, MACD).  Used by consumers that need to know whether to reserve
// vertical space.
const _SUB_PANEL_KEYS = new Set(['rsi', 'macd']);

function createChartStore() {
  // ── Reactive cells ────────────────────────────────────────────────
  let _symbol   = $state(readChartPref(_SYMBOL_LS_KEY, ''));
  let _exchange = $state(readChartPref(_EXCHANGE_LS_KEY, ''));
  let _days     = $state(readChartPref(_DAYS_LS_KEY, 30, (v) => typeof v === 'number' && v > 0));

  /** @type {'line'|'area'|'candle'|'plot'} */
  let _chartType = $state(/** @type {'line'|'area'|'candle'|'plot'} */ (
    readChartPref(_CHART_TYPE_LS_KEY, 'candle',
      (v) => typeof v === 'string' && ['line','area','candle','plot'].includes(v))
  ));

  /** @type {any[] | null} */
  let _ohlcv    = $state(null);
  /** @type {any[]} */
  let _spotBars = $state([]);

  /** @type {string[]} */
  let _overlays = $state([]);   // hydrated from LS by hydrateOverlays()
  let _loading  = $state(false);

  /** @type {{ symbol: string, exchange: string, days: number, at: number } | null} */
  let _lastFetched = $state(null);

  // ── Derived ───────────────────────────────────────────────────────
  // Indicators are the subset of overlays that occupy their own
  // sub-panel rather than being drawn on the price series itself.
  const _indicators = $derived(
    _overlays.filter(k => _SUB_PANEL_KEYS.has(k))
  );

  // ── Public API ────────────────────────────────────────────────────
  return {
    // ── Getters (reactive) ────────────────────────────────────────
    get symbol()     { return _symbol },
    get exchange()   { return _exchange },
    get days()       { return _days },
    get chartType()  { return _chartType },
    get ohlcv()      { return _ohlcv },
    get spotBars()   { return _spotBars },
    get overlays()   { return _overlays },
    get indicators() { return _indicators },
    get loading()    { return _loading },
    get lastFetched(){ return _lastFetched },

    // ── Setters ───────────────────────────────────────────────────
    /**
     * Set the active symbol (auto-uppercased).
     * Does NOT trigger a fetch — ChartWorkspace's $effect does that.
     * @param {string} v
     */
    setSymbol(v) {
      _symbol = String(v || '').toUpperCase();
      writeChartPref(_SYMBOL_LS_KEY, _symbol);
    },

    /**
     * Set the exchange hint.  Empty string means "auto-detect."
     * @param {string} v
     */
    setExchange(v) {
      _exchange = String(v || '');
      writeChartPref(_EXCHANGE_LS_KEY, _exchange);
    },

    /**
     * Set the active range in days.
     * @param {number} v
     */
    setDays(v) {
      _days = Number(v) || 30;
      writeChartPref(_DAYS_LS_KEY, _days);
    },

    /**
     * Set the chart display type (line / area / candle / plot).
     * @param {'line'|'area'|'candle'|'plot'} v
     */
    setChartType(v) {
      const known = new Set(['line','area','candle','plot']);
      if (known.has(v)) {
        _chartType = v;
        writeChartPref(_CHART_TYPE_LS_KEY, _chartType);
      }
    },

    /**
     * Write back fetched OHLCV results and record the fetch timestamp.
     * Pass null to clear the data (e.g. on symbol change).
     * @param {any[] | null} bars
     * @param {any[]} [spotBars]
     */
    setOhlcv(bars, spotBars = []) {
      _ohlcv    = bars;
      _spotBars = spotBars;
      if (bars !== null) {
        _lastFetched = {
          symbol:   _symbol,
          exchange: _exchange,
          days:     _days,
          at:       Date.now(),
        };
      }
    },

    /**
     * Clear cached bars without altering lastFetched.
     * Used when symbol changes and we want to show the loading state.
     */
    clearOhlcv() {
      _ohlcv    = null;
      _spotBars = [];
    },

    /**
     * Single-slot clear: wipe data, mark loading, reset fetch stamp.
     * Call immediately when symbol changes so old symbol's bars are
     * never visible under the new symbol even for a single frame.
     * Overlays and indicators are user-preferences and are NOT cleared.
     */
    clearData() {
      _ohlcv       = null;
      _spotBars    = [];
      _loading     = true;
      _lastFetched = null;
    },

    /**
     * Set the overlays selection and persist to localStorage.
     * @param {string[]} v
     */
    setOverlays(v) {
      _overlays = Array.isArray(v) ? v.slice() : [];
      writeChartPref(_OVERLAY_LS_KEY, _overlays);
    },

    /**
     * Set loading flag.
     * @param {boolean} v
     */
    setLoading(v) {
      _loading = !!v;
    },

    // ── Hydration ─────────────────────────────────────────────────
    /**
     * Seed overlays from localStorage.  Call once from ChartWorkspace's
     * onMount (SSR-safe — localStorage is undefined on the server).
     * @param {string[]} knownKeys — valid overlay keys from _OVERLAY_OPTS
     */
    hydrateOverlays(knownKeys) {
      const stored = readChartPref(_OVERLAY_LS_KEY, null,
        (v) => Array.isArray(v)
      );
      if (!stored) return;
      const known = new Set(knownKeys);
      const valid = stored.filter(/** @param {string} k */(k) => known.has(k));
      if (valid.length) _overlays = valid;
    },

    // ── TTL guard ─────────────────────────────────────────────────
    /**
     * Returns true when the currently cached bars are for the same
     * symbol/exchange/days and were fetched within the last 30 seconds.
     * ChartWorkspace checks this before firing a network request.
     */
    isFresh() {
      const lf = _lastFetched;
      if (!lf) return false;
      if (lf.symbol   !== _symbol)   return false;
      if (lf.exchange !== _exchange)  return false;
      if (lf.days     !== _days)      return false;
      return (Date.now() - lf.at) < _FETCH_TTL_MS;
    },
  };
}

export const chartStore = createChartStore();
