<script module>
  // Module-level LRU cache removed (2026-07-09).
  // Design: chart store holds ONE symbol at a time only. On symbol
  // change chartStore.clearData() wipes ohlcv immediately so old
  // bars never bleed into the new symbol's view. No per-symbol cache
  // map — single slot, no LRU, no TTL accumulation across symbols.
  //
  // prefetchChartBars is kept as a no-op export so callers
  // (PageHeaderActions, MarketPulse) compile without changes.
  // The hover-warmup UX is intentionally removed with the cache.

  /**
   * No-op stub — previously pre-warmed the module-level LRU cache.
   * The cache has been removed per the single-slot design; this export
   * stays so existing callers (PageHeaderActions.svelte,
   * MarketPulse.svelte) don't need to change.
   * @param {string} _symbol
   * @param {string} [_exchange]
   * @param {number} [_days]
   */
  export async function prefetchChartBars(_symbol, _exchange = '', _days = 30) {
    // intentional no-op — single-slot design, no pre-warming cache
  }
</script>

<script>
  // ChartWorkspace — unified symbol charting surface.
  //
  // Combines historical OHLCV (from /api/options/historical) with
  // intraday tick streaming (from /api/charts/price-history) plus
  // optional spot overlay for derivatives and a Greeks strip for
  // options. No chart library — pure SVG.
  //
  // Works standalone (full /charts page) or embedded in a compact
  // surface (compact=true suppresses the picker bar + Greeks strip).
  //
  // Props:
  //   symbol           — initial tradingsymbol (uppercased)
  //   exchange?        — optional exchange hint for historical fetch
  //   compact?         — hide picker + Greeks strip (embedded use)
  //   showHeader?      — false when the parent renders its own page-header
  //   onSymbolChange?  — callback when the operator picks a new symbol

  import { onMount, onDestroy, getContext, untrack } from 'svelte';
  import { readChartPref, writeChartPref } from '$lib/data/chartPrefs';
  import { chartStore } from '$lib/data/chartStore.svelte.js';
  import { createChartRefreshPulse } from '$lib/data/chartRefreshPulse.svelte.js';
  import {
    fetchOptionsHistorical,
    fetchChartPriceHistory,
    fetchSimStatus,
    fetchPaperStatus,
    fetchStrategyAnalytics,
    fetchWatchlists,
    fetchWatchlist,
  } from '$lib/api';
  import {
    ema as calcEma, vwap as calcVwap, macd as calcMacd,
    bollinger as calcBollinger, rsi as calcRsi,
    emaSignals, vwapSignals, bollingerSignals, rsiSignals, macdSignals,
  } from '$lib/chart/indicators.js';
  import { smaPath, emaPath, vwapPath, bbPaths, rsiSeries, macdSeries } from '$lib/chart/paths.js';
  import {
    loadInstruments, findNearestFuture,
  } from '$lib/data/instruments';
  import { resolveUnderlying, MCX_COMMODITIES, CDS_CURRENCIES, INDEX_LTP_KEY } from '$lib/data/resolveUnderlying';
  import { SYM_TYPE_OPTS } from '$lib/data/symbolTypes';
  import { displaySymbol } from '$lib/data/displaySymbol.js';
  import { visibleInterval } from '$lib/stores';
  import { priceFmt } from '$lib/format';
  import InfoHint from '$lib/InfoHint.svelte';
  import Select from '$lib/Select.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';
  import OhlcvTooltip from '$lib/chart/OhlcvTooltip.svelte';
  import TickTooltip from '$lib/chart/TickTooltip.svelte';

  // The Pinned dropdown is driven entirely by the operator's actual
  // pinned watchlists (rows on /pulse with `is_pinned=true`). No
  // hardcoded fallback list — operator: "it should not hard code
  // these symbols. it should always refer to pinned symbols in the
  // dropdown". `_hydratePins()` fetches the watchlists on mount and
  // refreshes the option set; the dropdown reads empty in the brief
  // pre-fetch window which is fine — the symbol search box still
  // works as the primary picker.

  // Tracks the exchange hint of the last pinned-or-search pick.
  // MCX commodities resolve to 'MCX'; indices/equities to 'NSE'/'BSE'.
  // Cleared when the operator types a symbol manually via search.
  let _resolvedExchange = $state('');

  let {
    symbol        = $bindable(''),
    exchange      = '',
    mode          = $bindable(/** @type {'live'|'sim'|'paper'} */('live')),
    compact       = false,
    showHeader    = true,
    bump          = 0,
    loading       = $bindable(false),
    onSymbolChange = /** @type {((sym: string) => void) | undefined} */ (undefined),
  } = $props();

  // ── Demo gate — skip status polling on anonymous prod sessions ────
  const _algoStatus = getContext('algoStatus');
  const _isDemo = $derived(_algoStatus?.isDemo ?? false);

  // ── Type filter for SymbolSearchInput ────────────────────────────
  // EQ = equities + indices; FUT = futures; OPT = CE+PE; ALL = no filter.
  let _symType        = $state(/** @type {'ALL'|'EQ'|'FUT'|'OPT'} */('ALL'));
  /** @type {Array<{value:string,label:string}>} */
  // Shared 4-option constant — same vocabulary on every surface
  // (imported at top of script).
  const _SYM_TYPE_OPTS = SYM_TYPE_OPTS;

  /** @type {Array<{value:string,label:string}>} */
  const _CHART_TYPE_OPTS = [
    { value: 'candle', label: 'Candle' },
    { value: 'line',   label: 'Line' },
    { value: 'area',   label: 'Area' },
    { value: 'plot',   label: 'Plot' },
  ];

  /** @type {Array<{value:number,label:string}>} */
  const _RANGE_OPTS = [
    { value: 1,   label: '1D' },
    { value: 7,   label: '1W' },
    { value: 30,  label: '1M' },
    { value: 90,  label: '3M' },
    { value: 180, label: '6M' },
    { value: 365, label: '1Y' },
  ];

  /** @type {Array<{value:string,label:string}>} */
  const _OVERLAY_OPTS = [
    { value: 'sma20',    label: 'SMA 20' },
    { value: 'sma50',    label: 'SMA 50' },
    { value: 'ema20',    label: 'EMA 20' },
    { value: 'ema50',    label: 'EMA 50' },
    { value: 'vwap',     label: 'VWAP' },
    { value: 'bb',       label: 'Bollinger' },
    { value: 'rsi',      label: 'RSI 14' },
    { value: 'macd',     label: 'MACD' },
  ];
  // Volume bars are ALWAYS on — operator: "remove volume chip from
  // chart and always keep volume on for chart in the modal and
  // page". _showVol below is hard-coded true; the toggle function
  // and the toolbar chip are gone.

  /** Called by SymbolSearchInput when the operator picks a symbol. */
  function _onPickSymbol(/** @type {string} */ sym) {
    const upper = String(sym || '').toUpperCase();
    if (!upper) return;
    _pinnedValue = '';       // search pick — clear active pin
    _resolvedExchange = '';  // clear stale MCX/CDS hint from prior pin
    _chartLoaded = false;
    _intradayOn = false;
    onSymbolChange?.(upper);
    // Record as the operator's most recent symbol so /orders +
    // /charts (page or modal) default to it on next open.
    import('$lib/data/accounts')
      .then(m => m.setRecentSymbol(upper))
      .catch(() => {});
    _loadHistorical(true);
  }

  // Pinned-symbols Select — dedicated dropdown so the operator can
  // load any pinned-watchlist symbol without typing the 3-char
  // threshold through the symbol search. Source: the operator's
  // actual pinned watchlists (`is_pinned=true`) from /api/watchlists.
  // Option: { value: tradingsymbol from the watchlist,
  //           label: resolved tradeable contract from the instruments
  //                  cache when applicable (e.g. CRUDEOIL → CRUDEOIL
  //                  26JUNFUT), falls back to the raw tradingsymbol }.
  function _pinLabel(/** @type {string} */ anchor) {
    const r = resolveUnderlying(String(anchor || '').toUpperCase(), findNearestFuture);
    return r?.tradingsymbol && r.tradingsymbol !== anchor ? r.tradingsymbol : anchor;
  }
  /** @type {Array<{value:string,label:string}>} */
  let _PIN_OPTS = $state(/** @type {{value:string,label:string}[]} */ ([]));
  let _pinnedValue = $state('');

  async function _hydratePins() {
    try {
      const lists = await fetchWatchlists();
      const rows  = Array.isArray(lists) ? lists : (lists?.watchlists ?? []);
      const pinned = rows.filter(w => w?.is_pinned);
      if (!pinned.length) { _PIN_OPTS = []; return; }
      // Fetch each pinned list in parallel; merge in source order,
      // dedupe symbols. The watchlist API already expands MCX/CDS
      // roots to actual futures, so every `tradingsymbol` returned
      // here is a tradeable contract.
      const details = await Promise.all(pinned.map(w =>
        fetchWatchlist(w.id).catch(() => null)
      ));
      const out  = [];
      const seen = new Set();
      for (const d of details) {
        const items = d?.items || [];
        for (const it of items) {
          const sym = String(it?.tradingsymbol || '').trim();
          if (sym && !seen.has(sym)) {
            out.push({ value: sym, label: _pinLabel(sym) });
            seen.add(sym);
          }
        }
      }
      _PIN_OPTS = out;
    } catch (_) { /* leave list as-is; symbol search still works */ }
  }

  /** Pick from the Pinned dropdown — resolves to tradeable + loads. */
  function _onPickPin(/** @type {string} */ pin) {
    const r = resolveUnderlying(String(pin || '').toUpperCase(), findNearestFuture);
    if (!r?.tradingsymbol) return;
    _pinnedValue = pin;
    _resolvedExchange = r.exchange || '';  // capture MCX/CDS/NSE so _loadHistorical can hint the backend
    symbol = r.tradingsymbol;
    _chartLoaded = false;
    _intradayOn = false;
    onSymbolChange?.(r.tradingsymbol);
    _loadHistorical(true);
  }

  // ── Symbol classification ─────────────────────────────────────────
  // Require a digit before CE/PE to avoid false-positives on equities
  // like RELIANCE (which ends in "CE"). Real option tradingsymbols
  // always have a numeric strike before the CE/PE suffix.
  const _isOption     = $derived(/\d(?:CE|PE)$/i.test(symbol));
  const _isFuture     = $derived(/FUT$/i.test(symbol));
  const _isDerivative = $derived(_isOption || _isFuture);
  const _underlying   = $derived.by(() => {
    if (!_isDerivative) return null;
    const m = symbol.match(/^([A-Z]+)/i);
    return m ? m[1].toUpperCase() : null;
  });

  // ── Front-month resolution for bare MCX commodity roots ──────────
  // When the operator passes e.g. symbol="CRUDEOIL" (a bare MCX root,
  // not a tradeable tradingsymbol), the chart internally charts the
  // nearest-expiry future. The info strip shows the resolved contract,
  // and a "Front-month" chip renders below the controls row carrying
  // the expiry date and a roll-warning when expiry is ≤ 3 days away.
  //
  // INDEX roots (NIFTY/BANKNIFTY/…) are excluded — those have their
  // own spot tickers and are handled by _KITE_INDEX_TO_ROOT / _resolveFetchSymbol.
  // The _resolveFetchSymbol path already handles the OHLCV fetch for
  // MCX roots; this derived is purely for the display chip.
  const _frontMonthInfo = $derived.by(() => {
    const upper = String(symbol || '').toUpperCase();
    if (!MCX_COMMODITIES.has(upper)) return null;    // only MCX roots
    if (INDEX_LTP_KEY[upper]) return null;            // guard (shouldn't overlap, but safe)
    const fut = findNearestFuture(upper);
    if (!fut?.s || !fut?.x) return null;
    const expiryIso = String(fut.x);                 // "YYYY-MM-DD"
    const todayMs   = Date.now();
    const expiryMs  = Date.parse(expiryIso + 'T23:59:00+05:30'); // IST EOD
    const daysLeft  = Math.ceil((expiryMs - todayMs) / 86_400_000);
    const rolling   = daysLeft <= 3;
    // Format expiry as "19 Jun" for the chip label.
    const expDate   = new Date(expiryMs);
    const MONTHS    = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    const expLabel  = `${expDate.getDate()} ${MONTHS[expDate.getMonth()]}`;
    return {
      contract:   String(fut.s),
      exchange:   String(fut.e || 'MCX'),
      expiryIso,
      expLabel,
      daysLeft,
      rolling,
    };
  });

  // ── Mode auto-detection ───────────────────────────────────────────
  // Internal only — no mode pills in the UI. The mode value drives
  // intraday tick streaming when enabled.
  let _simActive   = $state(false);
  let _paperActive = $state(false);
  let _statusTimer = /** @type {any} */ (null);

  async function _pollStatus() {
    try {
      const [sim, paper] = await Promise.all([fetchSimStatus(), fetchPaperStatus()]);
      _simActive   = !!sim?.active;
      _paperActive = !!paper?.open_order_count;
      // Auto-flip mode when sim is active (highest priority)
      if (_simActive && mode !== 'sim') mode = 'sim';
      else if (!_simActive && _paperActive && mode === 'sim') mode = 'paper';
    } catch (_) { /* silent */ }
  }

  // ── Chart-refresh pulse ───────────────────────────────────────────
  const _pulse = createChartRefreshPulse();

  // ── Historical OHLCV ──────────────────────────────────────────────
  // ── OHLCV + loading — backed by chartStore ──────────────────────
  // _bars and _histLoading are local reactive aliases that mirror the
  // store so existing template bindings and derived computations work
  // without modification.  All writes go through the store setters so
  // ChartModal and the /charts page always see the same data.
  /** @type {Array<{ts:string,open:number,high:number,low:number,close:number,volume:number}>} */
  let _bars        = $state(/** @type {any[]} */(chartStore.ohlcv ?? []));
  // Sync from store → local (e.g. another surface loaded data).
  // untrack on the read+write breaks the self-dependency cycle: reading
  // _bars in the condition tracked it as a dependency, causing infinite
  // re-runs when chartStore.ohlcv is null (new [] ref each time).
  $effect(() => {
    const next = chartStore.ohlcv ?? [];
    untrack(() => { if (_bars !== next) _bars = next; });
  });
  // Fire when _bars changes to a non-empty array (new data landed from
  // broker/cache). Skip on symbol-change blanks (length === 0) and on
  // zoom/pan/overlay changes (those don't touch _bars).
  $effect(() => {
    if (_bars.length) untrack(() => _pulse.notify('chart'));
  });
  let _histLoading = $state(chartStore.loading);
  // Sync loading from store.
  $effect(() => { _histLoading = chartStore.loading; });
  // _histLoadingSlow flips true ~150ms after _histLoading starts so
  // cache-hits (which complete in one frame) don't flash a spinner.
  // When true, the chart shows a "Fetching from broker…" overlay
  // because we know we're past the Tier 1/Tier 2 fast paths.
  let _histLoadingSlow = $state(false);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _histLoadingTimer = null;
  let _histError   = $state('');
  // _histRetrying flips true when the first fetch returned partial-empty
  // (backend signal `partial=true`) AND we have a delayed retry queued.
  // While true, the empty-state branch is suppressed — the chart shows
  // the loading affordance until the retry completes (or the count cap
  // _emptyRetryCount guards against an infinite loop and finally lets
  // "No data available" render). Operator-caught BEL race fix: the prior
  // path cleared _histLoading=false BEFORE the retry fired, so the
  // catchall {:else if !_bars.length} branch rendered "No data available"
  // for the entire retry window.
  let _histRetrying = $state(false);
  // ── 3-second empty-state suppression gate ────────────────────────
  // Operator-approved race fix (option B): never render "No data
  // available" within 3 seconds of a symbol change, regardless of
  // what _histLoading / _histRetrying / _bars are doing in between.
  // This closes the entire race CLASS (not just the specific sequence
  // of the BEL bug) because the gate is TIME-based rather than
  // state-machine-based.  After 3 s, if bars are still empty, the
  // EmptyState renders normally.
  //
  // Implementation:
  //   _emptyGateSuppressed — $state(true): the gate is ACTIVE (suppressing
  //     the empty state). A single setTimeout at 3000 ms flips it to false,
  //     which is a plain $state write that Svelte picks up immediately and
  //     re-renders the template. On every new symbol the gate is re-armed:
  //     cancel the old timer, set _emptyGateSuppressed=true, start a new
  //     3000ms timer. This avoids any derived/performance.now() subtlety.
  //   _suppressTimer — handle so onDestroy can cancel a pending wakeup.
  //
  // Initialized to `true` so the gate is active from the very first render
  // (before onMount fires and _loadHistorical runs).
  let _emptyGateSuppressed = $state(true);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _suppressTimer = null;
  let _chartLoaded = $state(false);
  // Range — sourced from chartStore.days so modal and page share the same
  // last-picked range.  The local _chartDays is a reactive alias that stays
  // in sync with the store; writes go through chartStore.setDays() which
  // also persists to localStorage via writeChartPref.
  const _RANGE_LS_KEY = 'rbq.cache.chart-range.v1';
  let _chartDays   = $state(chartStore.days);  // seed from store (default 30)
  let _rangeHydrated = $state(false);
  // Bridge: store → local (e.g. /charts page updated the range).
  $effect(() => {
    const d = chartStore.days;
    if (!_rangeHydrated) return;
    untrack(() => {
      if (d !== _chartDays) _chartDays = d;
    });
  });
  // Bridge: local → store + LS (operator clicks a range pill).
  $effect(() => {
    const snap = _chartDays;
    if (!_rangeHydrated) return;
    if (snap !== chartStore.days) chartStore.setDays(snap);
    writeChartPref(_RANGE_LS_KEY, snap);
  });
  // Chart type — sourced from chartStore so modal and page share the same
  // last-picked type. Local _chartType is a reactive alias; store is SSOT.
  // Legacy _SERIES_LS_KEY retained so existing stored preferences carry over.
  const _SERIES_LS_KEY = 'rbq.cache.chart-series.v1';
  let _chartType   = $state(/** @type {'line'|'area'|'candle'|'plot'} */ (chartStore.chartType));
  let _seriesHydrated = $state(false);
  // Bridge: store → local (e.g. another surface changed the chart type).
  $effect(() => {
    const ct = chartStore.chartType;
    if (!_seriesHydrated) return;
    untrack(() => {
      if (ct !== _chartType) _chartType = /** @type {'line'|'area'|'candle'|'plot'} */ (ct);
    });
  });
  // Bridge: local → store + LS (operator picks a chart type).
  $effect(() => {
    const snap = _chartType;
    if (!_seriesHydrated) return;
    if (snap !== chartStore.chartType) chartStore.setChartType(snap);
    // Also keep legacy key in sync for any existing code that reads it.
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(_SERIES_LS_KEY, JSON.stringify(snap));
      }
    } catch (_) { /* quota — skip silently */ }
  });
  // Overlays — sourced from chartStore so the selection is shared
  // between ChartModal and the /charts page.  Hydrated from localStorage
  // in onMount via chartStore.hydrateOverlays().  Writes go through
  // chartStore.setOverlays() which persists to LS under the same key
  // ('rbq.cache.chart-overlays.v1') — no separate persist effect needed.
  //
  // NOTE: _overlays is a local reactive alias so existing template
  // bindings (bind:value={_overlays}) continue to work.  When the
  // MultiSelect writes back via bind: it calls the setter defined in
  // the object spread below via a $effect watcher.
  //
  // Cannot call localStorage during $state() init — SSR guard.
  // _overlaysHydrated retained so the MultiSelect bind: round-trip
  // (template writes _overlays → $effect → chartStore.setOverlays)
  // does not fire before hydration is complete, avoiding a spurious
  // LS write of [] that would wipe the stored selection.
  let _overlays    = $state(/** @type {string[]} */([]));
  let _overlaysHydrated = $state(false);
  // Bridge: keep _overlays in sync when chartStore.overlays changes
  // (e.g. another surface called setOverlays).
  $effect(() => {
    const storeOverlays = chartStore.overlays;
    if (!_overlaysHydrated) return;
    untrack(() => {
      // Only update local if the reference differs (avoids loop).
      if (JSON.stringify(_overlays) !== JSON.stringify(storeOverlays)) {
        _overlays = storeOverlays.slice();
      }
    });
  });
  // Bridge: write back to store when operator toggles overlays via the
  // local MultiSelect.  LS persistence happens inside setOverlays().
  $effect(() => {
    const snap = _overlays.slice();
    if (!_overlaysHydrated) return;
    if (JSON.stringify(snap) !== JSON.stringify(chartStore.overlays)) {
      chartStore.setOverlays(snap);
    }
  });
  // Tracks whether the Overlays MultiSelect dropdown is open — used to
  // suppress both hover popups so they don't clash with the open panel.
  let _overlayOpen = $state(false);

  // ── Buy/sell signal markers ──────────────────────────────────────
  // Operator toggle: show buy/sell triangles for active indicators
  // (golden cross / death cross / VWAP cross / BB pierce / RSI 30-70 /
  // MACD cross). Default ON when at least one indicator is selected;
  // persisted to localStorage so the choice survives reload.
  const _SIGNALS_LS_KEY = 'rbq.cache.chart-signals.v1';
  let _signalsOn = $state(true);
  let _signalsHydrated = $state(false);
  $effect(() => {
    const snap = _signalsOn;
    if (!_signalsHydrated) return;
    try {
      if (typeof localStorage !== 'undefined') {
        localStorage.setItem(_SIGNALS_LS_KEY, JSON.stringify(snap));
      }
    } catch (_) { /* quota — silently skip */ }
  });
  // Intraday tick stream — toggled by a chip in the toolbar. Persisted to
  // localStorage so the operator's preference survives page navigation.
  // Note: symbol changes always reset this to false (both _onPickSymbol and
  // the symbol-change $effect write false) — the pref is the starting state
  // on mount, not sticky within a session across symbol picks.
  const _INTRADAY_LS_KEY = 'rbq.cache.chart-intraday.v1';
  let _intradayOn = $state(false);
  let _intradayHydrated = $state(false);
  $effect(() => {
    const snap = _intradayOn;
    if (!_intradayHydrated) return;
    writeChartPref(_INTRADAY_LS_KEY, snap);
  });
  const _showSma20 = $derived(_overlays.includes('sma20'));
  const _showSma50 = $derived(_overlays.includes('sma50'));
  // Always on — volume bars render unconditionally.
  const _showVol   = true;
  const _showEma20 = $derived(_overlays.includes('ema20'));
  const _showEma50 = $derived(_overlays.includes('ema50'));
  const _showVwap  = $derived(_overlays.includes('vwap'));
  const _showBb    = $derived(_overlays.includes('bb'));
  const _showRsi   = $derived(_overlays.includes('rsi'));
  const _showMacd  = $derived(_overlays.includes('macd'));

  // Expose loading state to parent via $bindable prop.
  $effect(() => { loading = _histLoading; });

  /** @type {Array<{ts:string,close:number}>} */
  let _spotBars = $state(/** @type {any[]} */(chartStore.spotBars ?? []));
  // Sync spotBars from store.
  $effect(() => {
    const next = chartStore.spotBars ?? [];
    untrack(() => { if (_spotBars !== next) _spotBars = next; });
  });

  // Kite returns the index spot under its quote-key name (e.g.
  // "NIFTY BANK", "NIFTY 50", "NIFTY FIN SERVICE") via /quote, but
  // those strings are NOT tradeable tradingsymbols — the historical
  // endpoint walks every exchange trying to find them and returns
  // empty bars after ~10s of broker calls. Any incoming symbol that
  // matches one of those keys gets routed to the underlying root's
  // nearest future on NFO instead. The chart still LABELS the chart
  // with the original symbol so the operator sees what they clicked.
  /** @type {Record<string, string>} */
  const _KITE_INDEX_TO_ROOT = {
    'NIFTY 50':           'NIFTY',
    'NIFTY BANK':         'BANKNIFTY',
    'NIFTY FIN SERVICE':  'FINNIFTY',
    'NIFTY MID SELECT':   'MIDCPNIFTY',
    'NIFTY NEXT 50':      'NIFTYNXT50',
  };
  async function _resolveFetchSymbol(/** @type {string} */ sym) {
    // Resolve any non-tradeable anchor (Kite index quote-key, MCX
    // commodity root, CDS currency root) to a symbol Kite's
    // historical_data API will accept.
    //
    // Routing per asset class:
    //
    //   • Kite indices (NIFTY 50, NIFTY BANK, NIFTY MIDCAP 100, …)
    //     → keep the literal index name + route to NSE. Kite's
    //     historical endpoint accepts the index instrument_token
    //     (permanent, years of data). Mapping to the front-month
    //     future broke 1Y / 6M ranges because the future was only
    //     listed ~30 days ago — the chart silently showed 23 bars
    //     for what the operator expected to be 250+. Operator
    //     caught this via the chart_1y_and_overlay Playwright spec.
    //
    //   • MCX commodities (CRUDEOIL, GOLD, SILVER, NATURALGAS, …)
    //     → still map to the front-month future. MCX commodities
    //     don't have a separate spot/index series in Kite — the
    //     front-month future IS the de-facto reference. The 1-2
    //     month rollover limit is intrinsic to the asset class.
    //
    //   • CDS currencies (USDINR, EURINR, GBPINR, JPYINR)
    //     → also front-month future. Same reasoning as MCX.
    const upper = String(sym || '').toUpperCase();
    const indexRoot = _KITE_INDEX_TO_ROOT[upper];        // 'NIFTY 50' → 'NIFTY'
    const isMcx     = MCX_COMMODITIES.has(upper);        // 'CRUDEOIL', 'GOLD', …
    const isCds     = CDS_CURRENCIES.has(upper);         // 'USDINR'

    // Index branch — keep the spot string, route to NSE.
    // This is the path that unlocks 1Y / 6M for NIFTY 50 / BANK
    // NIFTY / etc. without losing the per-tick live-LTP behaviour
    // elsewhere (the live ticker is a separate code path inside
    // ChartWorkspace and isn't routed through _resolveFetchSymbol).
    if (indexRoot && !(isMcx || isCds)) {
      return { sym: upper, exch: 'NSE' };
    }

    const root = indexRoot || (isMcx || isCds ? upper : null);
    if (!root) {
      // Plain NSE equity (RELIANCE, TCS, HDFCBANK, M&M, …).
      // Matches symbols that are alphabetic (with optional & or -) and
      // carry no FUT/CE/PE suffix — i.e. not a derivative, not an index,
      // not an MCX commodity, not a CDS currency. Route to NSE spot so
      // the backend can look up the instrument_token and return bars.
      // Returning an empty exchange here caused "No data available" for
      // every NSE equity because the historical endpoint couldn't resolve
      // the symbol without an exchange hint.
      const _isPlainEquity = /^[A-Z][A-Z0-9&-]*$/.test(upper) &&
        !/(?:FUT|CE|PE)$/.test(upper);
      if (_isPlainEquity) return { sym: upper, exch: 'NSE' };
      return { sym, exch: '' };
    }

    // Sync first — instruments cache is usually warm by the time the
    // operator clicks a chart icon. If null, force a load and retry.
    let fut = null;
    try { fut = findNearestFuture(root); } catch (_) {}
    if (!fut?.s) {
      try {
        // 3-second timeout on the instruments load so a stalled IndexedDB
        // hydration doesn't keep _loadHistorical in a pre-race hang
        // (the 25s race timeout only starts AFTER this resolver returns).
        await Promise.race([
          loadInstruments(),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error('inst-timeout')), 3000)
          ),
        ]);
        fut = findNearestFuture(root);
      } catch (_) { /* still no instruments — fall through to literal */ }
    }
    if (fut?.s) {
      // Default exchange: MCX for commodities, CDS for currencies.
      // (Index branch above intercepts before this line — that's why
      // 'NFO' is no longer in the fallback chain.)
      const defaultExch = isMcx ? 'MCX' : 'CDS';
      return { sym: String(fut.s), exch: String(fut.e || defaultExch) };
    }
    return { sym, exch: '' };
  }

  // Cancellation token — guards against concurrent _loadHistorical
  // calls that race when both the symbol-change effect AND the
  // bump-driven force-refresh effect fire on first ChartModal mount.
  // Previously a successful early fetch could be overwritten by a
  // late timeout from the second in-flight call, leaving the chart
  // empty with a "Slow response — try again" banner even though the
  // bars were already fetched. Each call grabs a token; results +
  // errors are silently dropped when the token is no longer current.
  let _loadToken = 0;

  // Empty-response retry — operator-caught BEL race: when the BACKEND
  // ohlcv hierarchy returns zero bars because the instruments map hasn't
  // warmed yet (process just booted, or post-deploy cold call lands before
  // the 08:30 IST warm), the response is cached for _HIST_CACHE_TTL_EMPTY
  // (2 s) on the server. Every reload within that window saw empty bars
  // and rendered "No data available."
  //
  // Frontend now silently re-attempts up to 3× with increasing back-off
  // delays so the server's empty-cache window and any broker warm-up have
  // elapsed before giving up. The delays are intentionally larger than the
  // 2 s backend TTL — the first delay (4 s) provides 2 s of headroom;
  // subsequent delays allow for broker cold-start (ohlcv_demand_fill
  // takes 2–10 s for a 90–365 day range). Only fires when _bars is still
  // empty AND the retry count is below 3 for this (symbol, exch, days)
  // key — prevents an infinite loop on symbols with genuinely no data.
  const _RETRY_DELAYS_MS = [4000, 8000, 15000];
  /** @type {Map<string, number>} */
  const _emptyRetryCount = new Map();
  let _emptyRetryTimer = null;
  const _partialRetryFired = new Set();
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _partialRetryTimer = null;
  let _histPartial = $state(false);

  /**
   * Build a dedup key for the empty-response retry latch.
   * Ensures at most one retry per (symbol, exchange, days) combination
   * regardless of how many concurrent _loadHistorical calls fire.
   */
  function _buildRetryKey(fetchSym, fetchExch, days) {
    const under = _isDerivative ? (_underlying || '') : '';
    return `${fetchSym}|${(fetchExch || '').toUpperCase()}|${days}|${under}`;
  }

  /**
   * Force a dimension re-measure after bars land. The ResizeObserver may
   * have fired while the modal/portal was still laying out (container at
   * zero width), leaving _chartW/_chartH stale. One rAF ensures the
   * container has its final CSS dimensions.
   */
  function _measureChartContainer() {
    requestAnimationFrame(() => {
      const el = _chartContainerEl;
      if (!el) return;
      const r = el.getBoundingClientRect();
      const w = Math.max(360, Math.round(r.width));
      const h = Math.max(200, Math.round(r.height));
      if (w !== _chartW || h !== _chartH) { _chartW = w; _chartH = h; }
    });
  }

  /**
   * Handle the empty-bars case after a broker fetch.
   * Returns true when the caller should early-return (retry scheduled
   * or bars kept from previous good fetch); false when the caller
   * should surface "No data available." and finish normally.
   *
   * Side-effects: may set _histRetrying, _histError, _emptyRetryTimer,
   * _chartLoaded, _bars (guard keeps old bars on silent keep).
   */
  function _handleEmptyBars(hist, retryKey, prevBars) {
    // Guard: keep last-good bars rather than flashing "no data".
    if (prevBars.length > 0) {
      _chartLoaded = true;
      return true; // caller should return early
    }
    // Empty response. Two cases:
    //   1. partial=true → backend says "transient, retry soon".
    //      Retry with back-off up to _RETRY_DELAYS_MS.length times.
    //   2. partial=false → backend confirmed no data — show error.
    //
    // _emptyRetryCount tracks how many retries have fired per key so
    // symbols that genuinely have no data don't loop forever.
    const isPartial = !!hist?.partial;
    const count     = _emptyRetryCount.get(retryKey) ?? 0;
    const canRetry  = isPartial && count < _RETRY_DELAYS_MS.length;
    if (canRetry) {
      _emptyRetryCount.set(retryKey, count + 1);
      _histError = '';
      // _histRetrying keeps the loading branch active so the
      // {:else if !_bars.length} "No data available" branch is NOT
      // rendered during the retry window. Each delay exceeds the
      // backend _HIST_CACHE_TTL_EMPTY (2000ms) and grows to allow
      // for the ohlcv_demand_fill async write to complete.
      _histRetrying = true;
      const delayMs = _RETRY_DELAYS_MS[count]; // count before increment
      if (_emptyRetryTimer) clearTimeout(_emptyRetryTimer);
      _emptyRetryTimer = setTimeout(() => {
        _emptyRetryTimer = null;
        if (!_mounted) return;
        if (!_bars.length) _loadHistorical(true);
        else _histRetrying = false;
      }, delayMs);
      _chartLoaded = true;
      return true; // caller should return early
    }
    // Confirmed-no-data or all retries exhausted.
    _histRetrying = false;
    _histError = 'No data available.';
    return false;
  }

  async function _loadHistorical(/** @type {boolean} */ force = false, /** @type {boolean} */ fresh = false) {
    if (!symbol) {
      _histLoading = false;
      chartStore.setLoading(false);
      return;
    }
    // Single-slot design: no cross-surface TTL cache.
    // chartStore.clearData() is called on every symbol change so old
    // bars never serve as a "fresh" hit for a new symbol.
    // Intra-mount dedup only: onMount fires once; _firstSymEffect skips
    // the initial effect run. force=true bypasses this (bump, range change,
    // operator refresh, symbol-change effect).
    if (!force && _chartLoaded) return;
    const token = ++_loadToken;
    _histLoading = true; _histError = '';
    chartStore.setLoading(true);
    // Defer the visible spinner by ~150ms — warm cache-hits complete
    // before it flips on (no flash); broker round-trips show it promptly.
    if (_histLoadingTimer) clearTimeout(_histLoadingTimer);
    _histLoadingSlow = false;
    _histLoadingTimer = setTimeout(() => {
      if (_histLoading) _histLoadingSlow = true;
    }, 150);
    // Hard timeout — prevents RefreshButton spinner from stranding when
    // a broker call hangs (Kite rate-limit retry loop on backend).
    const TIMEOUT_MS = 25000;
    const timeout = new Promise((_, reject) =>
      setTimeout(() => reject(new Error('Slow response — try again.')), TIMEOUT_MS)
    );
    try {
      // Map Kite index quote-keys to their tradeable future before any
      // backend call. NIFTY BANK / NIFTY 50 etc. would otherwise walk
      // every exchange arm and time out (~10s of broker calls).
      const _resolved = await _resolveFetchSymbol(symbol);
      if (token !== _loadToken) return;
      const fetchSym  = _resolved.sym;
      const fetchExch = _resolved.exch || _resolvedExchange || exchange || undefined;

      // Dedup key — used by the empty-response retry counter
      // (_emptyRetryCount) to cap retries for symbols that genuinely
      // have no historical data.
      const retryKey = _buildRetryKey(fetchSym, fetchExch, _chartDays);

      const promises = [
        fetchOptionsHistorical(fetchSym, { days: _chartDays, exchange: fetchExch, fresh }),
      ];
      if (_isDerivative && _underlying) {
        promises.push(
          fetchOptionsHistorical(_underlying, { days: _chartDays })
            .catch(() => ({ bars: [] }))
        );
      }
      const [hist, spotHist] = /** @type {any} */ (
        await Promise.race([Promise.all(promises), timeout])
      );
      if (token !== _loadToken) return; // a newer call superseded this one

      const _nextBars = Array.isArray(hist?.bars) ? hist.bars : [];
      if (_nextBars.length === 0) {
        // Snapshot the current bars before overwriting so _handleEmptyBars
        // can decide whether to keep them or surface the error.
        const prevBars = _bars;
        _bars     = _nextBars;
        _spotBars = spotHist ? (Array.isArray(spotHist.bars) ? spotHist.bars : []) : [];
        chartStore.setOhlcv(_bars, _spotBars);
        if (_handleEmptyBars(hist, retryKey, prevBars)) return;
      } else {
        _bars     = _nextBars;
        _spotBars = spotHist ? (Array.isArray(spotHist.bars) ? spotHist.bars : []) : [];
        chartStore.setOhlcv(_bars, _spotBars);
        if (hist?.partial && !_partialRetryFired.has(retryKey)) {
          _partialRetryFired.add(retryKey);
          _histPartial = true;
          if (_partialRetryTimer) clearTimeout(_partialRetryTimer);
          _partialRetryTimer = setTimeout(() => {
            _histPartial = false;
            _loadHistorical(true, true);
          }, 5000);
        } else {
          _histRetrying = false;
          _histPartial  = false;
        }
      }
      _chartLoaded = true;
    } catch (e) {
      if (token !== _loadToken) return; // newer call in flight — its result is canonical
      _histError = /** @type {any} */ (e)?.message || 'Load failed';
      _histRetrying = false;
      _histPartial = false;
      _bars = [];
      chartStore.setOhlcv(null);
    } finally {
      // Only the newest call flips loading off.
      if (token === _loadToken) {
        _histLoading = false;
        _histLoadingSlow = false;
        chartStore.setLoading(false);
        if (_histLoadingTimer) { clearTimeout(_histLoadingTimer); _histLoadingTimer = null; }
      }
      _measureChartContainer();
    }
  }

  function _setRange(/** @type {number} */ d) {
    if (d === _chartDays) return;
    _chartDays = d;
    _chartLoaded = false;
    _chartHover = null;
    zoom = null;
    // Full state reset — mirrors the symbol-change $effect so stale bars /
    // retry timers from the previous range don't bleed into the new fetch.
    // Excludes symbol-specific parts (chartStore.clearData, _greeks) because
    // the symbol hasn't changed — only the range window has.
    _histLoading = true;
    _histError = '';
    _histRetrying = false;
    _bars = [];
    _spotBars = [];
    _intradayOn = false;
    if (_emptyRetryTimer) { clearTimeout(_emptyRetryTimer); _emptyRetryTimer = null; }
    if (_partialRetryTimer) { clearTimeout(_partialRetryTimer); _partialRetryTimer = null; }
    _histPartial = false;
    _emptyRetryCount.clear();
    _partialRetryFired.clear();
    _emptyGateSuppressed = true;
    if (_suppressTimer) { clearTimeout(_suppressTimer); _suppressTimer = null; }
    _suppressTimer = setTimeout(() => {
      _suppressTimer = null;
      if (_mounted) _emptyGateSuppressed = false;
    }, 3000);
    _loadHistorical(true);
  }

  // ── Intraday tick stream ──────────────────────────────────────────
  const _intradayEnabled = $derived(_intradayOn);
  /** @type {Array<{ts:string,ltp:number,bid:number|null,ask:number|null}>} */
  let _ticks  = $state([]);
  /** @type {Array<{ts:string,kind:string,side:string,price:number|null}>} */
  let _events = $state([]);
  let _tickKind       = $state(/** @type {'underlying'|'derivative'|'other'} */('other'));
  let _tickUnderlying = $state(/** @type {string|null} */ (null));
  /** @type {Array<{ts:string,ltp:number}>} */
  let _underlyingTicks = $state([]);
  let _intradayError  = $state('');
  let _tickTimer      = /** @type {any} */ (null);

  async function _loadIntraday() {
    if (!symbol || !mode) return;
    try {
      const r = await fetchChartPriceHistory(mode, symbol);
      _ticks          = r?.ticks  || [];
      _events         = r?.events || [];
      _tickKind       = r?.kind   || 'other';
      _tickUnderlying = r?.underlying || null;
      _intradayError  = '';
      if (_tickKind === 'derivative' && _tickUnderlying) {
        try {
          const u = await fetchChartPriceHistory(mode, _tickUnderlying);
          _underlyingTicks = (u?.ticks || []).map(/** @param {any} t */ (t) => ({ ts: t.ts, ltp: t.ltp }));
        } catch (_) { _underlyingTicks = []; }
      } else {
        _underlyingTicks = [];
      }
    } catch (e) {
      _intradayError = /** @type {any} */ (e)?.message || 'Tick load failed';
    }
  }

  function _startTickPoll() {
    _stopTickPoll();
    _tickTimer = visibleInterval(_loadIntraday, 3000);
  }
  function _stopTickPoll() {
    if (_tickTimer) { _tickTimer(); _tickTimer = null; }
  }

  // ── Greeks strip (options only) ───────────────────────────────────
  let _greeks     = $state(/** @type {any} */ (null));
  let _greeksError = $state('');

  async function _loadGreeks() {
    if (!_isOption || !symbol) { _greeks = null; return; }
    try {
      const r = await fetchStrategyAnalytics(
        [{ symbol, qty: 1, avg_cost: null, ltp: null, iv: null }],
        {}
      );
      _greeks = r?.legs?.[0] ?? r ?? null;
      _greeksError = '';
    } catch (e) {
      _greeksError = /** @type {any} */ (e)?.message || '';
      _greeks = null;
    }
  }

  // ── Chart geometry (dynamic via ResizeObserver) ───────────────────
  let _chartContainerEl = $state(/** @type {HTMLElement|null} */(null));
  let _chartW = $state(720);
  let _chartH = $state(320);

  const CPAD_L  = 52;   // rotated Y-labels (-45°, 12px font) need ~52px after readability bump
  const CPAD_R  = 16;
  const CPAD_T  = 16;
  const CPAD_B  = 30;
  const RSI_H   = 56;   // RSI sub-panel height in SVG user units
  const MACD_H  = 56;   // MACD sub-panel height in SVG user units
  const _innerW = $derived(_chartW - CPAD_L - CPAD_R);
  // _bandH reserves vertical space at the bottom for sub-panels (RSI + MACD).
  // Volume bars always sit in the bottom VOL_H px of the price area; sub-panels
  // stack below that in reservation order (RSI first, MACD below RSI).
  // _innerH is the price-chart's usable height — overlays + price lines must
  // not draw below CPAD_T + _innerH.
  const _bandH  = $derived((_showRsi ? RSI_H : 0) + (_showMacd ? MACD_H : 0));
  const _innerH = $derived(_chartH - CPAD_T - CPAD_B - _bandH);

  $effect(() => {
    const el = _chartContainerEl;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const cr = entry.contentRect;
        _chartW = Math.max(360, Math.round(cr.width));
        _chartH = Math.max(200, Math.round(cr.height));
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  });

  // ── Zoom + pan (historical) ───────────────────────────────────────
  /** @type {{xMin:number,xMax:number}|null} */
  let zoom = $state(null);
  /** @type {{startClientX:number,startMin:number,startMax:number}|null} */
  let pan  = $state(null);

  const _barXs    = $derived(_bars.map(b => Date.parse(b.ts)).filter(Number.isFinite));

  // Defensive: if bars arrived but every timestamp failed to parse,
  // surface an error instead of rendering a blank chart silently.
  $effect(() => {
    if (_bars.length > 0 && _barXs.length === 0) {
      console.warn('[ChartWorkspace] bars present but timestamps unparseable — sample:', _bars[0]?.ts);
      _histError = 'Timestamps unparseable.';
    }
  });

  const _rangeStartMs = $derived(Date.now() - _chartDays * 86400 * 1000);
  const _dataXMin     = $derived(_barXs.length
      ? Math.min(Math.min(..._barXs), _rangeStartMs)
      : _rangeStartMs);
  const _dataXMax = $derived(_barXs.length ? Math.max(..._barXs) : 1);
  const _xMin     = $derived(zoom ? zoom.xMin : _dataXMin);
  const _xMax     = $derived(zoom ? zoom.xMax : _dataXMax);
  const _xSpan    = $derived(Math.max(1, _xMax - _xMin));
  const isZoomed  = $derived(zoom !== null);

  const _visibleBars = $derived(
    _bars.filter(b => {
      const t = Date.parse(b.ts);
      return t >= _xMin && t <= _xMax;
    })
  );
  const _yDomain = $derived.by(() => {
    const src = (_visibleBars.length ? _visibleBars : _bars);
    if (!src.length) return { lo: 0, hi: 1 };
    let lo = Infinity, hi = -Infinity;
    for (const b of src) {
      const l = Number(b.low), h = Number(b.high);
      if (Number.isFinite(l)) lo = Math.min(lo, l);
      if (Number.isFinite(h)) hi = Math.max(hi, h);
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { lo: 0, hi: 1 };
    const pad = (hi - lo) * 0.06 || 1;
    return { lo: lo - pad, hi: hi + pad };
  });

  function _xOf(/** @type {number} */ ts) {
    return CPAD_L + ((ts - _xMin) / _xSpan) * _innerW;
  }
  function _yOf(/** @type {number} */ v) {
    const span = Math.max(0.001, _yDomain.hi - _yDomain.lo);
    return CPAD_T + ((_yDomain.hi - v) / span) * _innerH;
  }

  // ── Price paths ───────────────────────────────────────────────────
  // All paths use _visibleBars so off-canvas bars are skipped when zoomed.
  const _linePath = $derived.by(() => {
    const src = _visibleBars.length ? _visibleBars : _bars;
    if (!src.length) return '';
    let d = '';
    for (let i = 0; i < src.length; i++) {
      const t = Date.parse(src[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = _xOf(t), y = _yOf(Number(src[i].close));
      d += (d === '' ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  });

  // Scatter-plot points — same source data as _linePath but rendered
  // as dots (no connecting lines) when chartType === 'plot'. Useful
  // for seeing the data density and any sparse/gappy bars at a glance.
  const _plotPoints = $derived.by(() => {
    const src = _visibleBars.length ? _visibleBars : _bars;
    if (!src.length) return [];
    /** @type {Array<{x:number,y:number}>} */
    const pts = [];
    for (let i = 0; i < src.length; i++) {
      const t = Date.parse(src[i].ts);
      if (!Number.isFinite(t)) continue;
      pts.push({ x: _xOf(t), y: _yOf(Number(src[i].close)) });
    }
    return pts;
  });

  const _areaPath = $derived.by(() => {
    const src = _visibleBars.length ? _visibleBars : _bars;
    if (!src.length) return '';
    const last  = src[src.length - 1];
    const lastT = Date.parse(last.ts);
    if (!Number.isFinite(lastT)) return '';
    // Close to the bottom of the price area (CPAD_T + _innerH), not the full SVG bottom.
    const base   = (CPAD_T + _innerH).toFixed(2);
    const firstT = Date.parse(src[0].ts);
    return `${_linePath} L${_xOf(lastT).toFixed(2)},${base} L${_xOf(firstT).toFixed(2)},${base} Z`;
  });

  const _candles = $derived.by(() => {
    const src = _visibleBars.length ? _visibleBars : _bars;
    if (!src.length) return [];
    const n    = src.length;
    const slot = n > 1 ? _innerW / (n - 1) : _innerW;
    const w    = Math.max(2, Math.min(12, slot * 0.6));
    /** @type {Array<{x:number,bodyY:number,bodyH:number,w:number,wickTop:number,wickBot:number,up:boolean}>} */
    const out = [];
    for (const b of src) {
      const t = Date.parse(b.ts);
      if (!Number.isFinite(t)) continue;
      const x = _xOf(t);
      const o = Number(b.open), c = Number(b.close);
      const up = c >= o;
      out.push({
        x, w,
        bodyY:   _yOf(Math.max(o, c)),
        bodyH:   Math.max(1, _yOf(Math.min(o, c)) - _yOf(Math.max(o, c))),
        wickTop: _yOf(Number(b.high)),
        wickBot: _yOf(Number(b.low)),
        up,
      });
    }
    return out;
  });

  function _smaPath(/** @type {number} */ window) { return smaPath(_bars, window, _xOf, _yOf); }
  const _sma20Path = $derived(_showSma20 ? _smaPath(20) : '');
  const _sma50Path = $derived(_showSma50 ? _smaPath(50) : '');

  const VOL_H = 48;
  const _volBars = $derived.by(() => {
    const src = _visibleBars.length ? _visibleBars : _bars;
    if (!src.length || !_showVol) return [];
    // Scale vMax against all bars so the bar heights stay stable while panning.
    let vMax = 0;
    for (const b of _bars) { const v = Number(b.volume || 0); if (v > vMax) vMax = v; }
    if (vMax <= 0) return [];
    const n        = src.length;
    const slot     = n > 1 ? _innerW / (n - 1) : _innerW;
    const w        = Math.max(2, Math.min(10, slot * 0.55));
    // Volume bars baseline sits at the bottom of the price area.
    // When RSI is on, _innerH already excludes RSI_H so this naturally
    // positions volume just above the RSI sub-panel.
    const baseline = CPAD_T + _innerH;
    /** @type {Array<{x:number,y:number,h:number,w:number,up:boolean}>} */
    const out = [];
    for (const b of src) {
      const t = Date.parse(b.ts);
      if (!Number.isFinite(t)) continue;
      const v = Number(b.volume || 0);
      const h = (v / vMax) * (VOL_H - 4);
      out.push({
        x: _xOf(t) - w / 2, y: baseline - h, h, w,
        up: Number(b.close) >= Number(b.open),
      });
    }
    return out;
  });

  const _spotOverlayPath = $derived.by(() => {
    if (!_spotBars.length || !_isDerivative) return '';
    let lo = Infinity, hi = -Infinity;
    for (const b of _spotBars) {
      const c = Number(b.close);
      if (Number.isFinite(c)) { lo = Math.min(lo, c); hi = Math.max(hi, c); }
    }
    const span = Math.max(0.001, hi - lo);
    const top  = CPAD_T + 0.10 * _innerH;
    const bot  = CPAD_T + 0.90 * _innerH;
    let d = '';
    for (let i = 0; i < _spotBars.length; i++) {
      const t = Date.parse(_spotBars[i].ts);
      if (!Number.isFinite(t)) continue;
      const norm = (Number(_spotBars[i].close) - lo) / span;
      const x    = _xOf(t), y = bot - norm * (bot - top);
      d += (d === '' ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  });

  // ── EMA path helper ───────────────────────────────────────────────
  // Classic EMA: EMA_t = close_t × k + EMA_{t-1} × (1−k), k = 2/(N+1).
  // Seed is the SMA of the first N bars. Returns '' when not enough bars.
  function _emaPath(/** @type {number} */ n) { return emaPath(_bars, n, _xOf, _yOf); }
  const _ema20Path = $derived(_showEma20 ? _emaPath(20) : '');
  const _ema50Path = $derived(_showEma50 ? _emaPath(50) : '');

  // ── VWAP overlay ─────────────────────────────────────────────────
  // Volume-weighted average price from bar[0] to current bar (cumulative).
  // Uses the pure vwapPath() from paths.js. Plotted as a solid cyan
  // line on the price panel — no separate sub-panel needed.
  const _vwapPath = $derived.by(() => {
    if (!_showVwap || !_bars.length) return '';
    return vwapPath(_bars, _xOf, _yOf);
  });

  // ── Bollinger Bands (20-period, ±2σ) ─────────────────────────────
  // Returns mid / upper / lower path strings + a closed fill path.
  const _bbPaths = $derived.by(() => {
    if (!_showBb || _bars.length < 20) return { mid: '', upper: '', lower: '', fill: '' };
    return bbPaths(_bars, _xOf, _yOf);
  });

  // ── RSI 14 (Wilder's smoothed RSI) ───────────────────────────────
  // Returns a series of {ts, rsi} points for sub-panel rendering.
  // The sub-panel has its own y-scale 0–100 (independent of price).
  const RSI_N = 14;
  const _rsiSeries = $derived.by(() => {
    if (!_showRsi || _bars.length < RSI_N + 1) return /** @type {Array<{ts:string,rsi:number}>} */ ([]);
    return rsiSeries(_bars, RSI_N);
  });

  // ── MACD (12/26/9) ────────────────────────────────────────────────
  // Rendered in a separate sub-panel below RSI (when both active, MACD
  // sits below RSI). Uses macdSeries() from paths.js.
  const _macdSeries = $derived.by(() => {
    // Need at least 26+9+1 = 36 bars for signal to appear; check minimum
    if (!_showMacd || _bars.length < 27) return /** @type {Array<{ts:string,macd:number|null,signal:number|null,histogram:number|null}>} */ ([]);
    return macdSeries(_bars);
  });

  // ── Buy/sell signal markers ──────────────────────────────────────
  // Per indicator, derive a list of {bar, type, indicator, label} that
  // the SVG layer renders as green-up / red-down triangles with a tag.
  //
  // Detection lives in indicators.js (pure functions); this $derived
  // re-runs only when _bars or _overlays change. Cost is O(N × K) for
  // N bars and K active indicators — < 2000 ops on 365-bar 1Y range.
  //
  // To avoid clutter on dense ranges (≥180 bars), markers from any
  // single indicator are density-capped to MAX_PER_IND_DENSE. Operator-
  // facing: a quiet chart stays readable; recent + most-actionable
  // signals stay visible.
  const _MAX_PER_IND = 12;            // cap per indicator on long ranges
  const _DENSE_THRESHOLD = 180;       // bars at which density throttle kicks in

  /**
   * Returns the signal series the markers layer consumes.
   * Shape:
   *   [{ bar: <object>, i: number, type: 'buy'|'sell',
   *      indicator: 'EMA cross'|'VWAP'|'BB'|'RSI'|'MACD',
   *      tag: 'EMA↑', tooltip: '…' }]
   * Throttled so dense charts stay legible.
   */
  const _signalMarkers = $derived.by(() => {
    if (!_signalsOn || !_bars.length) return [];
    /** @type {Array<{bar:any,i:number,type:'buy'|'sell',indicator:string,tag:string,tooltip:string}>} */
    const out = [];
    const dense = _bars.length >= _DENSE_THRESHOLD;

    /** @param {Array<{i:number,type:'buy'|'sell'}>} evts @param {string} ind @param {string} tagBuy @param {string} tagSell */
    function pushSignals(evts, ind, tagBuy, tagSell) {
      // Density cap: keep the most-recent N events per indicator.
      const trimmed = dense && evts.length > _MAX_PER_IND
        ? evts.slice(-_MAX_PER_IND)
        : evts;
      for (const ev of trimmed) {
        const bar = _bars[ev.i];
        if (!bar) continue;
        const tag = ev.type === 'buy' ? tagBuy : tagSell;
        const verb = ev.type === 'buy' ? 'Buy' : 'Sell';
        out.push({
          bar, i: ev.i, type: ev.type, indicator: ind, tag,
          tooltip: `${verb} signal — ${ind} @ ${bar.ts}`,
        });
      }
    }

    // EMA cross — only when BOTH ema20 and ema50 are selected (golden/death cross is a pair).
    if (_overlays.includes('ema20') && _overlays.includes('ema50') && _bars.length >= 50) {
      const fast = calcEma(_bars, 20).map(p => p.value);
      const slow = calcEma(_bars, 50).map(p => p.value);
      pushSignals(emaSignals(fast, slow), 'EMA cross', 'EMA↑', 'EMA↓');
    }

    // VWAP cross — needs volume; skip indices (volume=0 → vwap all-null).
    if (_overlays.includes('vwap') && _bars.length >= 2) {
      const v = calcVwap(_bars).map(p => p.value);
      pushSignals(vwapSignals(_bars, v), 'VWAP', 'VWAP↑', 'VWAP↓');
    }

    // Bollinger pierce — close touches lower/upper.
    if (_overlays.includes('bb') && _bars.length >= 20) {
      const bb = calcBollinger(_bars, 20, 2);
      pushSignals(bollingerSignals(_bars, bb), 'BB pierce', 'BB↓', 'BB↑');
    }

    // RSI 14 — 30/70 crossover.
    if (_overlays.includes('rsi') && _bars.length >= 15) {
      const r = calcRsi(_bars, 14).map(p => p.value);
      pushSignals(rsiSignals(r), 'RSI 14', 'RSI↑', 'RSI↓');
    }

    // MACD line cross signal line.
    if (_overlays.includes('macd') && _bars.length >= 27) {
      const series = calcMacd(_bars, 12, 26, 9);
      pushSignals(macdSignals(series, series), 'MACD', 'MACD↑', 'MACD↓');
    }

    return out;
  });

  /**
   * Layout-ready markers: x/y coords + stack offset for ties.
   * Same-bar markers stacked vertically so they don't overlap. Buy
   * triangles sit BELOW the bar's low; sell triangles sit ABOVE the
   * bar's high.
   */
  const _signalLayout = $derived.by(() => {
    if (!_signalMarkers.length) return [];
    // Group by bar index so we can stack within a single x.
    /** @type {Map<number, Array<typeof _signalMarkers[number]>>} */
    const byBar = new Map();
    for (const m of _signalMarkers) {
      const arr = byBar.get(m.i) ?? [];
      arr.push(m);
      byBar.set(m.i, arr);
    }
    /** @type {Array<{x:number,y:number,type:'buy'|'sell',indicator:string,tag:string,tooltip:string,stack:number}>} */
    const out = [];
    const STACK_PX = 16;
    const PAD_PX   = 8;     // gap between bar high/low and marker tip
    for (const [i, arr] of byBar) {
      const bar = _bars[i];
      if (!bar) continue;
      const t = Date.parse(bar.ts);
      if (!Number.isFinite(t)) continue;
      const x = _xOf(t);
      const yHigh = _yOf(Number(bar.high));
      const yLow  = _yOf(Number(bar.low));
      // Split into buys (below) and sells (above) for stacking.
      const buys = arr.filter(m => m.type === 'buy');
      const sells = arr.filter(m => m.type === 'sell');
      buys.forEach((m, idx) => {
        out.push({ x, y: yLow + PAD_PX + idx * STACK_PX,
          type: 'buy', indicator: m.indicator, tag: m.tag, tooltip: m.tooltip, stack: idx });
      });
      sells.forEach((m, idx) => {
        out.push({ x, y: yHigh - PAD_PX - idx * STACK_PX,
          type: 'sell', indicator: m.indicator, tag: m.tag, tooltip: m.tooltip, stack: idx });
      });
    }
    return out;
  });

  // ── Grid + axis labels ────────────────────────────────────────────
  const _yTicks = $derived.by(() => {
    if (!_bars.length) return [];
    const n    = 5;
    const span = Math.max(0.001, _yDomain.hi - _yDomain.lo);
    return Array.from({ length: n }, (_, i) => {
      const v = _yDomain.lo + (span * i) / (n - 1);
      return { v, y: _yOf(v) };
    });
  });

  // X-axis labels: switch to month abbreviations for long ranges (≥90d).
  const _xLabels = $derived.by(() => {
    if (!_barXs.length) return [];
    const useMon = _chartDays >= 90;
    const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return Array.from({ length: 5 }, (_, i) => {
      const t = _xMin + (_xSpan * i) / 4;
      const d = new Date(t);
      let label;
      if (useMon) {
        label = MON[d.getMonth()];
      } else {
        const mm = String(d.getMonth() + 1).padStart(2, '0');
        const dd = String(d.getDate()).padStart(2, '0');
        label = `${dd}/${mm}`;
      }
      return { x: _xOf(t), label };
    });
  });

  // ── Hover crosshair ───────────────────────────────────────────────
  /** @type {{x:number,y:number,bar:any,pxLeft:number,pxTop:number}|null} */
  let _chartHover = $state(null);
  /** @type {{tick:any,pxLeft:number,pxTop:number}|null} */
  let _intradayHover = $state(null);
  // Click-to-pin — operator: "when I click on the chart, it should show
  // popup window like payoff chart and show x and y values and other
  // important values of the curve. It should be default in all the
  // charts." While pinned, pointerleave doesn't dismiss the popup and a
  // visible × close button on the popup itself toggles it off. Pin
  // auto-clears when the underlying data changes (new symbol / range).
  let _chartPinned    = $state(false);
  let _intradayPinned = $state(false);

  // Popup dimensions used for clamping (px).
  const _TIP_W = 180;
  const _TIP_H = 110;
  const _ITIP_H = 80;

  function _onChartPointerMove(/** @type {PointerEvent} */ e) {
    const src = _visibleBars.length ? _visibleBars : _bars;
    if (!src.length) { _chartHover = null; return; }
    const svg  = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const containerRect = _chartContainerEl?.getBoundingClientRect();
    const xRel = ((e.clientX - rect.left) / rect.width) * _chartW;
    const tMs  = _xMin + ((xRel - CPAD_L) / _innerW) * _xSpan;
    let best = src[0], bestD = Infinity;
    for (const b of src) {
      const d = Math.abs(Date.parse(b.ts) - tMs);
      if (d < bestD) { bestD = d; best = b; }
    }
    const tx = Date.parse(best.ts);
    const baseLeft = containerRect?.left ?? rect.left;
    const baseTop  = containerRect?.top  ?? rect.top;
    const rawLeft = e.clientX - baseLeft + 14;
    const rawTop  = e.clientY - baseTop  - _TIP_H - 4;
    const cW = _chartContainerEl?.clientWidth  ?? 0;
    const cH = _chartContainerEl?.clientHeight ?? 0;
    const pxLeft = Math.max(6, Math.min(cW - _TIP_W - 6, rawLeft));
    const pxTop  = Math.max(6, Math.min(cH - _TIP_H - 6, rawTop));
    _chartHover = { x: _xOf(tx), y: _yOf(Number(best.close)), bar: best, pxLeft, pxTop };
  }

  function _onIntradayPointerMove(/** @type {PointerEvent} */ e) {
    if (!_ticks.length) { _intradayHover = null; return; }
    const svg  = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    // The intraday SVG uses a fixed 720×160 viewBox with preserveAspectRatio=none.
    const W2 = 720, P2L = 44, P2R = 8;
    const xRel = ((e.clientX - rect.left) / rect.width) * W2;
    const xs   = _ticks.map(t => +new Date(t.ts));
    const tMin = Math.min(...xs);
    const tMax = Math.max(...xs);
    const span = Math.max(1, tMax - tMin);
    const tMs  = tMin + ((xRel - P2L) / (W2 - P2L - P2R)) * span;
    let best = _ticks[0], bestD = Infinity;
    for (const t of _ticks) {
      const d = Math.abs(+new Date(t.ts) - tMs);
      if (d < bestD) { bestD = d; best = t; }
    }
    const sectionEl = svg.closest('.cw-intraday-section');
    const sectionRect = sectionEl?.getBoundingClientRect() ?? rect;
    const rawLeft = e.clientX - sectionRect.left + 14;
    const rawTop  = e.clientY - sectionRect.top  - _ITIP_H - 4;
    const cW = /** @type {HTMLElement|null} */ (sectionEl)?.clientWidth  ?? 0;
    const cH = /** @type {HTMLElement|null} */ (sectionEl)?.clientHeight ?? 0;
    const pxLeft = Math.max(6, Math.min(Math.max(cW - _TIP_W - 6, 6), rawLeft));
    const pxTop  = Math.max(6, Math.min(Math.max(cH - _ITIP_H - 6, 6), rawTop));
    _intradayHover = { tick: best, pxLeft, pxTop };
  }

  // Click toggles pin. Browser-native click fires after pointerup IF
  // the down-up was a tap (no significant drag), so the existing
  // pan/zoom drag flow doesn't accidentally pin on every release.
  function _onChartClick(/** @type {MouseEvent} */ e) {
    if (!_bars.length || pan) return;
    if (_chartPinned) {
      _chartPinned = false;
      _chartHover  = null;
      return;
    }
    // Ensure the hover anchor reflects the click position (in case the
    // operator clicked without a preceding pointermove, e.g. on touch).
    _onChartPointerMove(/** @type {any} */ (e));
    if (_chartHover) _chartPinned = true;
  }
  function _onIntradayClick(/** @type {MouseEvent} */ e) {
    if (!_ticks.length) return;
    if (_intradayPinned) {
      _intradayPinned = false;
      _intradayHover  = null;
      return;
    }
    _onIntradayPointerMove(/** @type {any} */ (e));
    if (_intradayHover) _intradayPinned = true;
  }
  // Auto-clear pin when the underlying data changes — pinning a bar at
  // 30d range and then switching to 1Y would leave a stale popup
  // anchored at a spot that no longer reflects the data beneath it.
  $effect(() => {
    void symbol; void _chartDays;
    _chartPinned    = false;
    _intradayPinned = false;
  });



  // ── Zoom + pan handlers ───────────────────────────────────────────
  function _xValueAt(/** @type {SVGSVGElement} */ svg, /** @type {number} */ clientX) {
    const rect = svg.getBoundingClientRect();
    const xPx  = (clientX - rect.left) * (_chartW / rect.width);
    return _xMin + ((xPx - CPAD_L) / _innerW) * _xSpan;
  }

  function _onWheel(/** @type {WheelEvent} */ e) {
    if (!_bars.length) return;
    e.preventDefault();
    const svg    = /** @type {SVGSVGElement} */ (e.currentTarget);
    const xVal   = _xValueAt(svg, e.clientX);
    const factor = e.deltaY > 0 ? 1.25 : 1 / 1.25;
    let newMin   = xVal - (xVal - _xMin) * factor;
    let newMax   = xVal + (_xMax - xVal) * factor;
    if (newMin <= _dataXMin && newMax >= _dataXMax) { zoom = null; return; }
    if (newMax - newMin < 86400000) return;
    zoom = { xMin: Math.max(newMin, _dataXMin - _xSpan), xMax: Math.min(newMax, _dataXMax + _xSpan) };
  }

  function _onPointerDown(/** @type {PointerEvent} */ e) {
    if (!_bars.length || e.button !== 0) return;
    /** @type {any} */ const tgt = e.currentTarget;
    tgt.setPointerCapture?.(e.pointerId);
    pan = { startClientX: e.clientX, startMin: _xMin, startMax: _xMax };
  }
  function _onPointerUp(/** @type {PointerEvent} */ e) {
    if (pan) {
      /** @type {any} */ const tgt = e.currentTarget;
      if (tgt?.releasePointerCapture) tgt.releasePointerCapture(e.pointerId);
    }
    pan = null;
  }
  function _onPointerMove(/** @type {PointerEvent} */ e) {
    if (pan) {
      const rect = /** @type {SVGSVGElement} */ (e.currentTarget).getBoundingClientRect();
      const dxPx  = (e.clientX - pan.startClientX) * (_chartW / rect.width);
      const dxVal = (dxPx / _innerW) * (pan.startMax - pan.startMin);
      zoom = { xMin: pan.startMin - dxVal, xMax: pan.startMax - dxVal };
      _chartHover = null;
    } else {
      _onChartPointerMove(e);
    }
  }
  function _resetZoom() { zoom = null; pan = null; }

  // ── Info strip ────────────────────────────────────────────────────
  const _firstClose = $derived(Number(_bars[0]?.close) || null);
  const _lastClose  = $derived(Number(_bars[_bars.length - 1]?.close) || null);
  const _dayPct     = $derived(
    (_firstClose && _lastClose)
      ? ((_lastClose - _firstClose) / _firstClose) * 100
      : null
  );
  const _dayAbs     = $derived(
    (_firstClose && _lastClose) ? (_lastClose - _firstClose) : null
  );
  const _rangeLabel = $derived(
    _RANGE_OPTS.find(o => o.value === _chartDays)?.label ?? `${_chartDays}D`
  );

  // ── Greeks formatting helpers ─────────────────────────────────────
  function _gv(/** @type {number|null|undefined} */ v, /** @type {number} */ dp = 2) {
    if (v == null || !Number.isFinite(v)) return '—';
    return v.toFixed(dp);
  }

  // Persist whatever symbol the chart shows (caller prop OR operator
  // pick) so the next /orders or /charts open carries the same
  // context. Operator: "when I go to orders page, that symbol ...
  // is not getting defaulted".
  $effect(() => {
    const s = String(symbol || '').toUpperCase();
    if (!s) return;
    import('$lib/data/accounts')
      .then(m => m.setRecentSymbol(s))
      .catch(() => {});
  });

  // ── Lifecycle ─────────────────────────────────────────────────────
  let _mounted = true;

  onMount(async () => {
    // Seed the store with this surface's symbol + exchange so the TTL
    // check in _loadHistorical uses the right key.  The /charts page
    // seeds the store before mounting ChartWorkspace; ChartModal seeds
    // via its `symbol` prop.  Either way, this onMount write is the
    // canonical "I am the active chart" signal.
    chartStore.setSymbol(symbol);
    chartStore.setExchange(exchange || '');

    // Hydrate overlay preferences via chartStore — it reads the same
    // localStorage key ('rbq.cache.chart-overlays.v1') and validates
    // against the known overlay set.  Mirror into local _overlays so
    // existing template bindings work unchanged.
    chartStore.hydrateOverlays(_OVERLAY_OPTS.map((o) => o.value));
    _overlays = chartStore.overlays.slice();
    _overlaysHydrated = true;

    // Hydrate signals toggle — operator may have turned markers off.
    try {
      const raw = localStorage.getItem(_SIGNALS_LS_KEY);
      if (raw !== null) {
        const parsed = JSON.parse(raw);
        if (typeof parsed === 'boolean') _signalsOn = parsed;
      }
    } catch (_) { /* localStorage unavailable — keep default ON */ }
    _signalsHydrated = true;

    // Hydrate series-type choice from localStorage. First visit (key
    // absent) keeps the candle default initialized above. Only accept
    // known values to defend against legacy / hand-edited keys.
    try {
      const raw = localStorage.getItem(_SERIES_LS_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        const known = new Set(_CHART_TYPE_OPTS.map((o) => o.value));
        if (typeof parsed === 'string' && known.has(parsed)) {
          _chartType = /** @type {'line'|'area'|'candle'|'plot'} */ (parsed);
        }
      }
    } catch (_) { /* localStorage unavailable — keep default */ }
    _seriesHydrated = true;

    // Hydrate range (days) — operator's last-picked range (1D–1Y).
    // Only accept values from the _RANGE_OPTS set to guard against
    // legacy / hand-edited keys.
    {
      const knownRanges = new Set(_RANGE_OPTS.map((o) => o.value));
      const storedRange = readChartPref(_RANGE_LS_KEY, _chartDays,
        (v) => typeof v === 'number' && knownRanges.has(v));
      if (storedRange !== _chartDays) _chartDays = storedRange;
    }
    _rangeHydrated = true;

    // Hydrate intraday toggle.
    {
      const storedIntraday = readChartPref(_INTRADAY_LS_KEY, _intradayOn,
        (v) => typeof v === 'boolean');
      if (storedIntraday !== _intradayOn) _intradayOn = storedIntraday;
    }
    _intradayHydrated = true;

    await loadInstruments().catch(() => {});
    // Pin hydration runs in the background — don't block the historical
    // load. Operator can use DEFAULT_PINS immediately; the full list
    // swaps in once the watchlist API responds (typically <300ms).
    _hydratePins();
    // Arm the 3-second empty-state suppression gate for the initial load.
    // _emptyGateSuppressed starts true; we schedule a 3000 ms timer that
    // flips it to false. The symbol-change $effect is skipped on mount
    // (_firstSymEffect guard), so we arm the timer here too.
    _emptyGateSuppressed = true;
    if (_suppressTimer) { clearTimeout(_suppressTimer); _suppressTimer = null; }
    _suppressTimer = setTimeout(() => {
      _suppressTimer = null;
      if (_mounted) _emptyGateSuppressed = false;  // open the gate
    }, 3000);
    // If the store holds a different symbol's bars (stale 30s TTL cache
    // from a prior open), wipe them before loading so A's bars are never
    // rendered under B's symbol label during the ~200-500ms broker fetch.
    // Same-symbol re-opens are unaffected: lastFetched.symbol === symbol
    // keeps the instant cache-hit path intact.
    if (symbol && chartStore.lastFetched?.symbol !== symbol) {
      chartStore.clearData();
    }
    await _loadHistorical();
    if (!_isDemo) {
      await _pollStatus();
      _statusTimer = visibleInterval(_pollStatus, 5000);
    }
    // Defer Greeks fetch until after the chart first-paints.
    // Greeks are supplemental (shown in the strip below the chart);
    // the operator sees a complete chart immediately and the Greeks
    // strip populates within one rAF. This shaves ~30-80ms off
    // time-to-interactive on initial open.
    if (_isOption) setTimeout(() => { if (_mounted) _loadGreeks(); }, 0);
  });

  onDestroy(() => {
    _mounted = false;
    if (_statusTimer) { try { _statusTimer(); } catch (_) { clearInterval(_statusTimer); } }
    if (_emptyRetryTimer) { clearTimeout(_emptyRetryTimer); _emptyRetryTimer = null; }
    if (_partialRetryTimer) { clearTimeout(_partialRetryTimer); _partialRetryTimer = null; }
    if (_suppressTimer) { clearTimeout(_suppressTimer); _suppressTimer = null; }
    _histRetrying = false;
    _histPartial = false;
    _stopTickPoll();
  });

  // Re-load historical when symbol changes externally.
  // The intraday-toggle reset is wrapped in untrack() so reading
  // _intradayOn here doesn't make this effect depend on it —
  // otherwise writing back false re-fires the effect endlessly
  // (caught: 80+ /api/options/historical calls in 2s).
  // _firstSymEffect skips the initial Svelte-fires-on-mount run
  // because onMount already kicked off _loadHistorical(); was
  // causing 2 fetches per ChartModal open (audit defect #7).
  let _firstSymEffect = true;
  $effect(() => {
    void symbol; void exchange;
    if (!_mounted) return;
    if (_firstSymEffect) { _firstSymEffect = false; return; }
    if (!symbol) return;
    // Single-slot clear: wipe old symbol's bars from the store immediately
    // so no render frame sees stale data under the new symbol.
    // chartStore.clearData() sets ohlcv=null, loading=true, lastFetched=null.
    chartStore.clearData();
    chartStore.setSymbol(symbol);
    chartStore.setExchange(exchange || '');
    _chartLoaded = false;
    zoom = null;
    _chartHover = null;
    // Mirror store state into local reactive aliases atomically so the
    // template stays in the "Loading…" branch during the brief window
    // before _loadHistorical fires and sets its own copy of the flags.
    _histLoading = true;
    _histError = '';
    _histRetrying = false;
    _bars = [];
    _spotBars = [];
    _greeks = null;
    // Cancel any pending empty-response retry from a previous symbol so
    // it doesn't fire AFTER the new symbol's fetch has already landed.
    if (_emptyRetryTimer) { clearTimeout(_emptyRetryTimer); _emptyRetryTimer = null; }
    if (_partialRetryTimer) { clearTimeout(_partialRetryTimer); _partialRetryTimer = null; }
    _histPartial = false;
    // Clear per-symbol retry count so the new symbol gets a fresh retry
    // budget (no stale keys from previous symbols bleed through).
    _emptyRetryCount.clear();
    _partialRetryFired.clear();
    // 3-second gate: re-arm the suppression gate for the new symbol.
    // Cancel the previous timer, flip the gate active, then schedule
    // 3000 ms to open it. The template reads _emptyGateSuppressed ($state)
    // so Svelte re-renders immediately when the setTimeout writes false.
    _emptyGateSuppressed = true;
    if (_suppressTimer) { clearTimeout(_suppressTimer); _suppressTimer = null; }
    _suppressTimer = setTimeout(() => {
      _suppressTimer = null;
      if (_mounted) _emptyGateSuppressed = false;  // open the gate
    }, 3000);
    untrack(() => {
      if (_intradayOn) _intradayOn = false;
    });
    _loadHistorical(true);
    if (_isOption) setTimeout(() => { if (_mounted) _loadGreeks(); }, 0);
  });

  // External reload trigger.
  $effect(() => {
    if (bump > 0 && _mounted) _loadHistorical(true);
  });

  // Toggle intraday polling when enabled.
  $effect(() => {
    if (_intradayEnabled) {
      _loadIntraday();
      _startTickPoll();
    } else {
      _stopTickPoll();
      _ticks = [];
      _events = [];
    }
  });

  // When the Overlays dropdown opens, flush any stale hover popup so
  // the two UI elements never visually clash.
  $effect(() => {
    if (_overlayOpen) {
      _chartHover = null;
      _intradayHover = null;
    }
  });
</script>

<div class="cw-root {_pulse.classOf('chart')}">
  <div class="cw-header">
  <!-- Picker bar — type filter (1st) sets the instrument-kind scope,
       then the combined pinned+search combo box (2nd) lets the
       operator either click a pin OR type to search from a single
       field. -->
  {#if !compact || showHeader}
    <div class="cw-picker" class:cw-picker-busy={_histLoading}
         aria-busy={_histLoading ? 'true' : 'false'}>
      <!-- Type filter — leading element so the operator scopes the
           instrument family FIRST. "EQ · FUT · OPT" label spells out
           what the unfiltered ALL value actually contains. -->
      <div class="cw-type-wrap">
        <Select
          options={_SYM_TYPE_OPTS}
          bind:value={_symType}
          disabled={_histLoading}
          ariaLabel="Symbol type filter" />
      </div>

      <!-- Symbol combo — 2nd element. Pinned section shows the
           resolved tradeable contract per anchor (NIFTY 50 →
           NIFTY26JUNFUT, CRUDEOIL → CRUDEOILM26JUNFUT). Picking a pin
           routes through _onPickPin so the exchange hint is captured;
           typing falls through to the regular search path. -->
      <SymbolSearchInput
        bind:value={symbol}
        pins={_PIN_OPTS.map(o => o.label)}
        resolvePin={(label) => label}
        type={_symType}
        placeholder="Pick or type 3+ chars…"
        onPick={(sym, meta) => {
          if (meta?.pinLabel) {
            // Reverse-look up the anchor (NIFTY 50) from the resolved
            // label (NIFTY26JUNFUT) so _onPickPin can grab the right
            // exchange via resolveUnderlying.
            const opt = _PIN_OPTS.find(o => o.label === meta.pinLabel);
            _onPickPin(opt?.value ?? meta.pinLabel);
          } else {
            if (meta?.exchange) _resolvedExchange = meta.exchange;
            _onPickSymbol(sym);
          }
        }}
        ariaLabel="Symbol — pinned or search" />

      <!-- Chart type — moved to picker row so series toggle lives beside
           the symbol picker (operator: "move line to first row along
           with symbol"). -->
      <div class="cw-toolbar-select cw-type-chart-wrap">
        <Select
          options={_CHART_TYPE_OPTS}
          bind:value={_chartType}
          disabled={_histLoading}
          ariaLabel="Chart type" />
      </div>
    </div>
  {/if}

  <!-- Chart controls row — chart type, intraday toggle, date range
       (1D/1W/1M/3M/6M/1Y), reset zoom. Operator request: "keep chart
       overlay with 1M 3M 6M 1Y row in charts model and page".
       Pulled OUT of the compact gate so the interval pills stay
       visible in every embed (modal, page, any future compact mount)
       — operator's primary affordance for switching range is always
       one click away. Overlays panel below mirrors this. -->
  <!-- Controls row — every interactive control here triggers either a
       network fetch (range pills, intraday toggle, symbol picker) or a
       redraw against the live bars. All are disabled while
       `_histLoading` is true so the operator can't queue a second
       request behind an in-flight one (operator: "prevent user trying
       to generate chart when previous chart is in progress"). The busy
       spinner in the row's leading slot tells the operator a fetch is
       in flight; clears the moment data lands. -->
  <div class="cw-controls" class:cw-controls-busy={_histLoading}>
    <!-- Leading-slot spinner removed — the modal title-glyph rotation
         and the page-header RefreshButton's icon swap already carry
         the loading state across both surfaces. The `.cw-controls-
         busy` class still applies the disabled state + opacity dim so
         the controls visibly lock during a fetch. -->

    <!-- Intraday tick stream — single toggle chip.
         .cw-intraday-full hides on mobile; .cw-intraday-short shows "Intra" -->
    <button type="button"
      class="cw-range-btn cw-intraday-btn"
      class:active={_intradayOn}
      disabled={_histLoading}
      title={_intradayOn ? 'Intraday tick stream ON — click to turn off' : 'Intraday tick stream OFF — click to turn on'}
      aria-pressed={_intradayOn}
      onclick={() => _intradayOn = !_intradayOn}>
      <span class="cw-intraday-full">Intraday</span><span class="cw-intraday-short">Intra</span>
    </button>

    <!-- Date range — segmented pill row (1D/1W/1M/3M/6M/1Y) -->
    <div class="cw-range-group" role="group" aria-label="Date range">
      {#each _RANGE_OPTS as opt}
        <button type="button"
          class="cw-range-btn"
          class:active={_chartDays === opt.value}
          disabled={_histLoading}
          title="Past {opt.label}"
          onclick={() => _setRange(Number(opt.value))}>
          {opt.label}
        </button>
      {/each}
    </div>

    {#if _histPartial}
      <span class="chart-partial-hint">Loading more history…</span>
    {/if}

    <!-- Volume chip retired (operator: "remove volume chip from
         chart and always keep volume on for chart in the modal
         and page"). _overlays['vol'] is hard-coded ON in the
         state init so the bars render unconditionally. -->

    <!-- Indicators dropdown — single MultiSelect control, TradingView-style.
         Operator: "Convert the current row-2 overlay-toggle strip into a
         single multi-select control" and "one button opens a list of
         checkboxes, multiple can be active". The MultiSelect is the
         canonical pattern (matches AccountMultiSelect / WatchlistMultiSelect).
         Placeholder reads "Indicators" so the trigger label is self-describing
         when nothing is picked. -->
    <div class="cw-overlay-panel" role="region" aria-label="Chart indicators">
      <MultiSelect
        options={_OVERLAY_OPTS}
        bind:value={_overlays}
        bind:open={_overlayOpen}
        disabled={_histLoading}
        placeholder="Indicators"
        ariaLabel="Chart indicators" />
    </div>

    <!-- Signals toggle — surface buy/sell triangles for active indicators.
         Hidden when no indicator is selected (nothing to signal). Operator:
         "show buy point or sell point based on each indicator when indicator
         is selected on the price chart". -->
    {#if _overlays.length}
      <button type="button"
        class="cw-range-btn cw-signals-btn"
        class:active={_signalsOn}
        disabled={_histLoading}
        title={_signalsOn ? 'Buy/sell signal markers ON — click to hide' : 'Buy/sell signal markers OFF — click to show'}
        aria-pressed={_signalsOn}
        onclick={() => _signalsOn = !_signalsOn}>
        <span class="cw-signals-full">Signals</span><span class="cw-signals-short">Sig</span>
      </button>
    {/if}

    <!-- Reset zoom action button — trailing edge, only when zoomed -->
    {#if isZoomed}
      <button type="button" class="cw-reset-zoom"
              disabled={_histLoading}
              onclick={_resetZoom}
              title="Reset zoom — show full range">Reset</button>
    {/if}
  </div>
  </div><!-- /.cw-header -->

  <!-- Front-month chip — shown when symbol is a bare MCX commodity
       root. Amber roll-warning when expiry is ≤ 3 days away. -->
  {#if _frontMonthInfo}
    <div class="cw-frontmonth-bar">
      <span class="cw-fm-chip" class:cw-fm-rolling={_frontMonthInfo?.rolling}
            title="Instrument cache resolved {symbol} → {_frontMonthInfo?.contract} (exchange: {_frontMonthInfo?.exchange})">
        {#if _frontMonthInfo?.rolling}
          Front-month: {_frontMonthInfo?.contract} · rolls in {_frontMonthInfo?.daysLeft} {_frontMonthInfo?.daysLeft === 1 ? 'day' : 'days'}
        {:else}
          Front-month: {_frontMonthInfo?.contract} · expiry {_frontMonthInfo?.expLabel}
        {/if}
      </span>
    </div>
  {/if}

  <!-- Historical OHLCV chart — fills available height via flex -->
  <div class="cw-chart-container" bind:this={_chartContainerEl}>
    <!-- Visible fetch overlay — appears ~150ms after the load starts
         so warm cache hits (which return in one frame) don't flash.
         When visible, signals the operator that we're going past the
         in-memory + DB tiers to the broker. Distinct from cw-empty's
         text-only state, which renders when the load completes with
         no bars. -->
    {#if _histLoadingSlow}
      <div class="cw-fetch-overlay" role="status" aria-live="polite">
        <span class="cw-fetch-spinner" aria-hidden="true"></span>
        <span class="cw-fetch-msg">Fetching from broker…</span>
        <span class="cw-fetch-sub">Cached after first load</span>
      </div>
    {/if}

    <!-- HTML hover popup for OHLCV (replaces the SVG rect+text block).
         Pinned state (after a click) keeps the popup anchored at the
         click location and renders a small × close button so the
         operator can dismiss without clicking the chart again. -->
    {#if _chartHover && !_overlayOpen && !pan}
      <OhlcvTooltip
        bar={_chartHover.bar}
        pxLeft={_chartHover.pxLeft}
        pxTop={_chartHover.pxTop}
        pinned={_chartPinned}
        onClose={() => { _chartPinned = false; _chartHover = null; }}
      />
    {/if}

    {#if !symbol}
      <div class="cw-state cw-state-hint">Pick a symbol to chart — type 3+ chars in the box above.</div>
    {:else if (_histLoading || _histRetrying) && !_bars.length}
      <!-- _histRetrying guard: when a partial-empty response is being
           re-fetched (operator-caught BEL race), keep the loading branch
           active so the catchall "No data available" below never renders
           during the retry window. Without this, the empty state flashes
           for ~800 ms between the first empty response and the retry. -->
      <div class="cw-state">Loading…</div>
    {:else if _histError && _histError !== 'No data available.' && !_bars.length}
      <!-- Real fetch errors (timeout, load-failed) surface immediately,
           unaffected by the 3-second suppression gate. -->
      <div class="cw-state cw-err">{_histError}</div>
    {:else if !_histLoading && !_histRetrying && !_bars.length && !_emptyGateSuppressed}
      <!-- 3-second gate: _emptyGateSuppressed is $state(true) on symbol
           change. A 3000 ms setTimeout flips it to false, which is a
           plain $state write — Svelte picks it up immediately and
           re-renders this branch. Covers both the plain "No data
           available." case and the _histError === 'No data available.'
           variant (confirmed-empty or exhausted-retry path). -->
      <div class="cw-state {_histError ? 'cw-err' : ''}">No data available.</div>
    {:else}
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
      <!-- Chart SVG: wheel-zoom + drag-pan are pointer-native interactions;
           role="application" communicates this to AT. -->
      <svg
        viewBox="0 0 {_chartW} {_chartH}"
        preserveAspectRatio="none"
        class="cw-svg"
        class:cw-panning={pan !== null}
        role="application"
        aria-label="Price chart — wheel to zoom, drag to pan, click to pin"
        onwheel={_onWheel}
        onpointerdown={_onPointerDown}
        onpointerup={_onPointerUp}
        onpointermove={_onPointerMove}
        onpointerleave={() => { if (!_chartPinned) _chartHover = null; }}
        onclick={_onChartClick}
      >
        <!-- Plot-area background tint — first child so it sits behind
             grid lines, candles, and overlays. --chart-bg-tint in app.css. -->
        <rect class="chart-bg" x={CPAD_L} y={CPAD_T} width={_innerW} height={_innerH}
              fill="var(--chart-bg-tint)" rx="0"/>
        <!-- Y-axis baseline (left edge of plot area) -->
        <line x1={CPAD_L} x2={CPAD_L} y1={CPAD_T} y2={CPAD_T + _innerH}
              stroke="rgba(200,216,240,0.25)" stroke-width="1"/>

        <!-- Y-axis grid + labels — labels rotated -45° to save horizontal space -->
        {#each _yTicks as tick}
          <line x1={CPAD_L} x2={_chartW - CPAD_R} y1={tick.y} y2={tick.y}
                stroke="rgba(200,216,240,0.15)" stroke-width="1"/>
          <text x={CPAD_L - 4} y={tick.y}
                class="cw-yaxis-label"
                text-anchor="end" dominant-baseline="middle"
                transform="rotate(-45 {CPAD_L - 4} {tick.y})"
                fill="#c8d8f0" font-size="12" font-weight="600" font-family="monospace">
            ₹{priceFmt(tick.v)}
          </text>
        {/each}

        <!-- X-axis grid + labels — clamped to price area bottom -->
        {#each _xLabels as xl, i}
          {#if i > 0}
            <line x1={xl.x} x2={xl.x} y1={CPAD_T} y2={CPAD_T + _innerH}
                  stroke="rgba(200,216,240,0.10)" stroke-width="1" stroke-dasharray="2 3"/>
          {/if}
          <text x={xl.x} y={CPAD_T + _innerH + 14}
                text-anchor={i === 0 ? 'start' : (i === 4 ? 'end' : 'middle')}
                fill="#c8d8f0" font-size="12" font-weight="600">
            {xl.label}
          </text>
        {/each}

        <!-- X-axis baseline — bottom of price area -->
        <line x1={CPAD_L} x2={_chartW - CPAD_R}
              y1={CPAD_T + _innerH} y2={CPAD_T + _innerH}
              stroke="rgba(255,255,255,0.22)" stroke-width="1"/>

        <!-- Volume bars (lower band of price area) -->
        {#if _showVol}
          {#each _volBars as v}
            <rect x={v.x} y={v.y} width={v.w} height={v.h}
                  fill={v.up ? 'rgba(74,222,128,0.30)' : 'rgba(248,113,113,0.30)'}/>
          {/each}
        {/if}

        <!-- Spot overlay (derivatives) — sky dashed line -->
        {#if _spotOverlayPath}
          <path d={_spotOverlayPath} fill="none"
                stroke="#7dd3fc" stroke-width="1.2" stroke-dasharray="4 3" stroke-opacity="0.65"
                class="data-path"/>
        {/if}

        <!-- Bollinger Bands fill (drawn before lines so lines appear on top).
             Gated on the overlays array (not just the derived boolean) to
             match the SMA/EMA/VWAP pattern — guarantees DOM removal on
             uncheck even if the $derived bbPaths re-evaluation lags. -->
        {#if _overlays.includes('bb') && _bbPaths.fill}
          <path class="overlay-bb overlay-bb-fill data-path" d={_bbPaths.fill} fill="rgba(125,211,252,0.06)" stroke="none"/>
        {/if}
        {#if _overlays.includes('bb') && _bbPaths.upper}
          <path class="overlay-bb overlay-bb-upper data-path" d={_bbPaths.upper} fill="none" stroke="#7dd3fc" stroke-width="1" stroke-dasharray="3 2"/>
          <path class="overlay-bb overlay-bb-lower data-path" d={_bbPaths.lower} fill="none" stroke="#7dd3fc" stroke-width="1" stroke-dasharray="3 2"/>
          <path class="overlay-bb overlay-bb-mid data-path"   d={_bbPaths.mid}   fill="none" stroke="#7dd3fc" stroke-width="1"/>
        {/if}

        <!-- Price layer — line / area / candle / plot -->
        {#if _chartType === 'area'}
          <path d={_areaPath} fill="rgba(251,191,36,0.14)" stroke="none" class="data-path"/>
          <path d={_linePath} fill="none" stroke="#fbbf24" stroke-width="1.8"
                stroke-linejoin="round" stroke-linecap="round" class="data-path"/>
        {:else if _chartType === 'candle'}
          {#each _candles as c}
            <line x1={c.x} x2={c.x} y1={c.wickTop} y2={c.wickBot}
                  stroke={c.up ? 'var(--c-long)' : 'var(--c-short)'} stroke-width="1"/>
            <rect x={c.x - c.w / 2} y={c.bodyY} width={c.w} height={c.bodyH}
                  fill={c.up ? 'var(--c-long)' : 'var(--c-short)'}/>
          {/each}
        {:else if _chartType === 'plot'}
          {#each _plotPoints as p}
            <circle cx={p.x} cy={p.y} r="1.8" fill="#fbbf24"/>
          {/each}
        {:else}
          <path d={_linePath} fill="none" stroke="#fbbf24" stroke-width="1.8"
                stroke-linejoin="round" stroke-linecap="round" class="data-path"/>
        {/if}

        <!-- SMA overlays — gate on _overlays array directly so toggle-off
             removes the <path> from the DOM in the same tick. Operator
             reported the old gate (`{#if _sma20Path}`) left stale paths
             rendered after uncheck — `$derived` paths can hold prior
             content for a tick if the path-builder depends on bar data
             that hasn't changed. -->
        {#if _overlays.includes('sma20') && _sma20Path}
          <path class="overlay-sma overlay-sma20 data-path" d={_sma20Path} fill="none" stroke="#7dd3fc" stroke-width="1.4"
                stroke-dasharray="4 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}
        {#if _overlays.includes('sma50') && _sma50Path}
          <path class="overlay-sma overlay-sma50 data-path" d={_sma50Path} fill="none" stroke="#c084fc" stroke-width="1.4"
                stroke-dasharray="6 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}

        <!-- EMA overlays — same gating pattern as SMA -->
        {#if _overlays.includes('ema20') && _ema20Path}
          <path class="overlay-ema overlay-ema20 data-path" d={_ema20Path} fill="none" stroke="#4ade80" stroke-width="1"
                stroke-dasharray="4 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}
        {#if _overlays.includes('ema50') && _ema50Path}
          <path class="overlay-ema overlay-ema50 data-path" d={_ema50Path} fill="none" stroke="#fb923c" stroke-width="1"
                stroke-dasharray="6 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}

        <!-- VWAP overlay — solid cyan line on the price panel -->
        {#if _overlays.includes('vwap') && _vwapPath}
          <path class="overlay-vwap data-path" d={_vwapPath} fill="none" stroke="#7dd3fc"
                stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}

        <!-- RSI 14 sub-panel — gate on overlays array directly so
             the entire panel (background tint, threshold lines, label,
             path) is removed from DOM on toggle off. -->
        {#if _overlays.includes('rsi') && _rsiSeries.length}
          {@const rsiTop = _chartH - CPAD_B - RSI_H}
          {@const rsiBot = _chartH - CPAD_B}
          {@const rsiYOf = (/** @type {number} */ val) => rsiTop + ((100 - val) / 100) * (RSI_H - 6)}
          <!-- Background tint -->
          <rect x={CPAD_L} y={rsiTop} width={_chartW - CPAD_L - CPAD_R} height={RSI_H}
                fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.06)" stroke-width="0.5"/>
          <!-- Overbought / oversold / mid threshold lines -->
          <line x1={CPAD_L} x2={_chartW - CPAD_R} y1={rsiYOf(70)} y2={rsiYOf(70)}
                stroke="rgba(248,113,113,0.5)" stroke-width="1" stroke-dasharray="3 3"/>
          <line x1={CPAD_L} x2={_chartW - CPAD_R} y1={rsiYOf(30)} y2={rsiYOf(30)}
                stroke="rgba(74,222,128,0.5)" stroke-width="1" stroke-dasharray="3 3"/>
          <line x1={CPAD_L} x2={_chartW - CPAD_R} y1={rsiYOf(50)} y2={rsiYOf(50)}
                stroke="rgba(200,216,240,0.20)" stroke-width="1" stroke-dasharray="2 4"/>
          <!-- RSI level labels (left edge) -->
          <text x={CPAD_L - 4} y={rsiYOf(70) + 3} text-anchor="end"
                fill="rgba(248,113,113,0.7)" font-size="9" font-family="monospace">70</text>
          <text x={CPAD_L - 4} y={rsiYOf(30) + 3} text-anchor="end"
                fill="rgba(74,222,128,0.7)" font-size="9" font-family="monospace">30</text>
          <!-- RSI line -->
          {#each [_rsiSeries] as series}
            {@const rsiPath = series.reduce((acc, pt, idx) => {
              const t = Date.parse(pt.ts);
              if (!Number.isFinite(t)) return acc;
              const x = _xOf(t);
              const y = rsiYOf(pt.rsi);
              return acc + (idx === 0 ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
            }, '')}
            <path class="overlay-rsi data-path" d={rsiPath} fill="none" stroke="#fbbf24" stroke-width="1.5" stroke-linecap="round"/>
          {/each}
          <!-- RSI label -->
          <text x={_chartW - CPAD_R - 4} y={rsiTop + 12}
                text-anchor="end" fill="#fbbf24" font-size="10" font-weight="700" font-family="monospace">
            RSI 14
          </text>
        {/if}

        <!-- MACD (12/26/9) sub-panel — below RSI when both active.
             Gated on overlays array directly so the entire sub-panel
             is removed from DOM on toggle off. -->
        {#if _overlays.includes('macd') && _macdSeries.length}
          {@const macdTop = _chartH - CPAD_B - (_showRsi ? RSI_H : 0) - MACD_H}
          {@const macdBot = _chartH - CPAD_B - (_showRsi ? RSI_H : 0)}
          {@const macdPanelH = MACD_H}
          <!-- Compute y-scale: range = max(|hist|, |macd|, |signal|) values -->
          {@const macdVals = _macdSeries.flatMap(p => [p.macd, p.signal, p.histogram]).filter(v => v != null)}
          {@const macdMax = macdVals.length ? Math.max(...macdVals.map(Math.abs)) : 1}
          {@const macdRange = Math.max(macdMax * 1.15, 0.001)}
          {@const macdYOf = (/** @type {number} */ val) => macdTop + ((macdRange - val) / (2 * macdRange)) * (macdPanelH - 6)}
          {@const macdZero = macdTop + (macdPanelH - 6) / 2}
          <!-- Background tint -->
          <rect x={CPAD_L} y={macdTop} width={_chartW - CPAD_L - CPAD_R} height={macdPanelH}
                fill="rgba(255,255,255,0.02)" stroke="rgba(255,255,255,0.06)" stroke-width="0.5"/>
          <!-- Zero line -->
          <line x1={CPAD_L} x2={_chartW - CPAD_R} y1={macdZero} y2={macdZero}
                stroke="rgba(200,216,240,0.25)" stroke-width="1" stroke-dasharray="2 4"/>
          <!-- Histogram bars -->
          {#each _macdSeries as pt}
            {#if pt.histogram != null}
              {@const t = Date.parse(pt.ts)}
              {#if Number.isFinite(t)}
                {@const x = _xOf(t)}
                {@const yH = macdYOf(pt.histogram)}
                {@const barUp = pt.histogram >= 0}
                <line x1={x} x2={x} y1={Math.min(yH, macdZero)} y2={Math.max(yH, macdZero)}
                      stroke={barUp ? 'rgba(74,222,128,0.55)' : 'rgba(248,113,113,0.55)'}
                      stroke-width="2" stroke-linecap="round"/>
              {/if}
            {/if}
          {/each}
          <!-- MACD line -->
          {@const macdLinePath = _macdSeries.reduce((acc, pt, idx) => {
            if (pt.macd == null) return acc;
            const t = Date.parse(pt.ts);
            if (!Number.isFinite(t)) return acc;
            const x = _xOf(t);
            const y = macdYOf(pt.macd);
            return acc + (acc === '' ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
          }, '')}
          <path class="overlay-macd data-path" d={macdLinePath} fill="none" stroke="#fbbf24"
                stroke-width="1.4" stroke-linecap="round"/>
          <!-- Signal line -->
          {@const macdSignalPath = _macdSeries.reduce((acc, pt) => {
            if (pt.signal == null) return acc;
            const t = Date.parse(pt.ts);
            if (!Number.isFinite(t)) return acc;
            const x = _xOf(t);
            const y = macdYOf(pt.signal);
            return acc + (acc === '' ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
          }, '')}
          <path class="overlay-macd data-path" d={macdSignalPath} fill="none" stroke="#f87171"
                stroke-width="1" stroke-dasharray="3 2" stroke-linecap="round"/>
          <!-- MACD label -->
          <text x={_chartW - CPAD_R - 4} y={macdTop + 12}
                text-anchor="end" fill="#fbbf24" font-size="10" font-weight="700" font-family="monospace">
            MACD
          </text>
        {/if}

        <!-- Buy/sell signal markers — TradingView-style triangles + indicator tag.
             Green up-arrow below the bar's low for buys; red down-arrow above the
             bar's high for sells. Stacked vertically when multiple indicators
             fire on the same bar. Signal-detection lives in indicators.js. -->
        {#each _signalLayout as sig}
          <g class="signal-marker signal-{sig.type}">
            <title>{sig.tooltip}</title>
            {#if sig.type === 'buy'}
              <!-- Up-pointing triangle anchored at (x, y) with tip up -->
              <polygon points="{sig.x},{sig.y} {sig.x - 5},{sig.y + 8} {sig.x + 5},{sig.y + 8}"
                       fill="#4ade80" stroke="#0a0a0a" stroke-width="0.5"/>
              <text x={sig.x} y={sig.y + 19}
                    text-anchor="middle"
                    font-size="9" font-weight="700"
                    fill="#4ade80" font-family="monospace"
                    class="signal-tag">{sig.tag}</text>
            {:else}
              <!-- Down-pointing triangle anchored at (x, y) with tip down -->
              <polygon points="{sig.x},{sig.y} {sig.x - 5},{sig.y - 8} {sig.x + 5},{sig.y - 8}"
                       fill="#f87171" stroke="#0a0a0a" stroke-width="0.5"/>
              <text x={sig.x} y={sig.y - 11}
                    text-anchor="middle"
                    font-size="9" font-weight="700"
                    fill="#f87171" font-family="monospace"
                    class="signal-tag">{sig.tag}</text>
            {/if}
          </g>
        {/each}

        <!-- Hover crosshair — vertical line + dot only; OHLCV text is in the HTML popup -->
        {#if _chartHover && !pan}
          <line x1={_chartHover.x} x2={_chartHover.x} y1={CPAD_T} y2={CPAD_T + _innerH}
                stroke="rgba(251,191,36,0.5)" stroke-width="1" stroke-dasharray="3 2"/>
          <circle cx={_chartHover.x} cy={_chartHover.y} r="3"
                  fill="#fbbf24" stroke="#fff" stroke-width="1"/>
        {/if}
      </svg>
      <!-- Loading overlay removed — the modal's title-glyph rotation
           (.cm-title-icon-loading) carries the refresh state across
           both modal + page contexts now. No duplicate spinner on the
           canvas. -->
    {/if}
  </div>

  <!-- Intraday tick chart (below historical, opt-in, fixed height) -->
  {#if _intradayEnabled}
    <div class="cw-intraday-section" style="position: relative;">
      <div class="cw-intraday-label">
        <span>Intraday ticks</span>
        <span class="cw-intraday-mode cw-mode-{mode}">{mode.toUpperCase()}</span>
        {#if _simActive || _paperActive}
          <span class="cw-intraday-source">{_simActive ? 'SIM' : 'PAPER'}</span>
        {/if}
        {#if _ticks.length}
          <span class="cw-meta-text">{_ticks.length} ticks · {_events.length} events</span>
        {/if}
        {#if _intradayError}
          <span class="cw-err-text">{_intradayError}</span>
        {/if}
      </div>
      {#if !_ticks.length && !_intradayError}
        <div class="cw-state cw-state-sm">
          No ticks captured for {symbol}{mode === 'sim' ? ' (start the simulator)' : ''}.
        </div>
      {:else if _ticks.length}
        {@const W2 = 720}
        {@const H2 = 160}
        {@const P2L = 44} {@const P2R = 8} {@const P2T = 8} {@const P2B = 20}
        {@const _t2xs  = _ticks.map(t => +new Date(t.ts))}
        {@const _t2min = Math.min(..._t2xs)}
        {@const _t2max = Math.max(..._t2xs)}
        {@const _t2span = Math.max(1, _t2max - _t2min)}
        {@const _t2prices = _ticks.flatMap(t => [t.ltp, t.bid, t.ask].filter(v => v != null))}
        {@const _t2pmin = Math.min(.../** @type {number[]} */ (_t2prices))}
        {@const _t2pmax = Math.max(.../** @type {number[]} */ (_t2prices))}
        {@const _t2pad  = Math.max((_t2pmax - _t2pmin) * 0.05, _t2pmin * 0.0005, 0.5)}
        {@const _t2ymin = _t2pmin - _t2pad}
        {@const _t2ymax = _t2pmax + _t2pad}
        {@const _t2yspan = Math.max(0.001, _t2ymax - _t2ymin)}
        {@const _t2xOf  = (/** @type {string} */ ts) => P2L + ((+new Date(ts) - _t2min) / _t2span) * (W2 - P2L - P2R)}
        {@const _t2yOf  = (/** @type {number} */ v) => P2T + (1 - (v - _t2ymin) / _t2yspan) * (H2 - P2T - P2B)}
        {@const _t2path = _ticks.map((t, i) => `${i===0?'M':'L'}${_t2xOf(t.ts).toFixed(1)},${_t2yOf(t.ltp).toFixed(1)}`).join(' ')}
        {@const _t2YTicks = Array.from({length: 4}, (_, i) => {
          const v = _t2ymin + (_t2yspan * i) / 3;
          return { v, y: _t2yOf(v) };
        })}
        <!-- Intraday hover popup -->
        {#if _intradayHover && !_overlayOpen}
          <TickTooltip
            tick={_intradayHover.tick}
            pxLeft={_intradayHover.pxLeft}
            pxTop={_intradayHover.pxTop}
            pinned={_intradayPinned}
            onClose={() => { _intradayPinned = false; _intradayHover = null; }}
          />
        {/if}
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
        <!-- Intraday SVG: hover + click-pin are pointer-native; role="application" set. -->
        <svg viewBox="0 0 {W2} {H2}" preserveAspectRatio="none"
             class="cw-intraday-svg" role="application" aria-label="Intraday tick chart — click to pin"
             onpointermove={_onIntradayPointerMove}
             onpointerleave={() => { if (!_intradayPinned) _intradayHover = null; }}
             onclick={_onIntradayClick}>
          <!-- Plot-area background tint — first child. --chart-bg-tint in app.css. -->
          <rect class="chart-bg" x={P2L} y={P2T} width={W2 - P2L - P2R} height={H2 - P2T - P2B}
                fill="var(--chart-bg-tint)" rx="0"/>
          <!-- Intraday Y-axis baseline -->
          <line x1={P2L} x2={P2L} y1={P2T} y2={H2 - P2B}
                stroke="rgba(200,216,240,0.25)" stroke-width="1"/>
          <!-- Intraday X-axis baseline -->
          <line x1={P2L} x2={W2 - P2R} y1={H2 - P2B} y2={H2 - P2B}
                stroke="rgba(255,255,255,0.22)" stroke-width="1"/>
          {#each _t2YTicks as yt}
            <line x1={P2L} x2={W2 - P2R} y1={yt.y} y2={yt.y}
                  stroke="rgba(200,216,240,0.15)" stroke-width="1"/>
            <text x={P2L - 3} y={yt.y}
                  class="cw-yaxis-label"
                  text-anchor="end" dominant-baseline="middle"
                  transform="rotate(-45 {P2L - 3} {yt.y})"
                  fill="#7e97b8" font-size="11" font-family="monospace">
              {priceFmt(yt.v)}
            </text>
          {/each}
          <path d={_t2path} fill="none"
                stroke={_tickKind === 'underlying' ? '#7dd3fc' : 'var(--c-action)'}
                stroke-width="1.4"/>
          {#each _events as ev}
            {#if ev.ts >= _ticks[0].ts && ev.ts <= _ticks[_ticks.length - 1].ts}
              {@const cx = _t2xOf(ev.ts)}
              {@const cy = _t2yOf(ev.price ?? _ticks[_ticks.length - 1].ltp)}
              {@const evColor = ev.kind === 'filled' ? 'var(--c-long)' : ev.kind === 'unfilled' ? 'var(--c-short)' : 'var(--c-action)'}
              <circle {cx} {cy} r="4" fill={evColor} fill-opacity="0.25" stroke={evColor} stroke-width="1.5"/>
              <circle {cx} {cy} r="2" fill={evColor}/>
            {/if}
          {/each}
        </svg>
      {/if}
    </div>
  {/if}

  <!-- Info strip -->
  <div class="cw-info-strip">
    <span class="cw-info-sym">{_frontMonthInfo ? _frontMonthInfo.contract : (symbol || '—')}</span>
    {#if _frontMonthInfo && symbol !== _frontMonthInfo.contract}
      <span class="cw-info-root">({displaySymbol(symbol)})</span>
    {/if}
    {#if _lastClose != null}
      <span class="cw-info-close">₹{priceFmt(_lastClose)}</span>
    {/if}
    {#if _dayPct != null}
      <span class="cw-info-pct" class:cw-pos={_dayPct >= 0} class:cw-neg={_dayPct < 0}>
        {_dayPct >= 0 ? '+' : ''}{_dayPct.toFixed(2)}%
        {#if _dayAbs != null}
          ({_dayAbs >= 0 ? '+' : ''}₹{priceFmt(Math.abs(_dayAbs))})
        {/if}
      </span>
    {/if}
    {#if _bars.length}
      <span class="cw-info-meta">{_bars.length} bars · {_rangeLabel}</span>
    {/if}
    {#if _isDerivative && _underlying}
      <span class="cw-info-meta">
        <span class="cw-legend-dash" aria-hidden="true"></span>
        {_underlying}
      </span>
    {/if}
  </div>

  <!-- Greeks strip — options only. Omitted for equities/futures/indices. -->
  {#if !compact && _isOption}
    <div class="cw-greeks-strip">
      <div class="cw-greeks-label">Greeks</div>
      {#if _greeksError}
        <span class="cw-err-text cw-greeks-err">{_greeksError}</span>
      {:else if _greeks}
        {@const g = _greeks}
        <div class="cw-greek-item">
          <span class="cw-gk-label">Δ</span>
          <span class="cw-gk-val">{_gv(g.delta ?? g.greeks?.delta)}</span>
          <InfoHint popup text="Delta — how much the option price moves per ₹1 move in the underlying. Call Δ is positive; put Δ is negative." />
        </div>
        <div class="cw-greek-item">
          <span class="cw-gk-label">Γ</span>
          <span class="cw-gk-val">{_gv(g.gamma ?? g.greeks?.gamma)}</span>
          <InfoHint popup text="Gamma — rate of change of Delta per ₹1 move. High Gamma = Delta changes fast near expiry." />
        </div>
        <div class="cw-greek-item">
          <span class="cw-gk-label">Θ</span>
          <span class="cw-gk-val">{_gv(g.theta ?? g.greeks?.theta)}</span>
          <InfoHint popup text="Theta — daily time decay in ₹ (trader units). Long options lose Θ per day; short options gain it." />
        </div>
        <div class="cw-greek-item">
          <span class="cw-gk-label">V</span>
          <span class="cw-gk-val">{_gv(g.vega ?? g.greeks?.vega)}</span>
          <InfoHint popup text="Vega — P&amp;L change per 1% move in implied volatility. Long options have positive Vega." />
        </div>
        <div class="cw-greek-item">
          <span class="cw-gk-label">ρ</span>
          <span class="cw-gk-val">{_gv(g.rho ?? g.greeks?.rho)}</span>
          <InfoHint popup text="Rho — P&amp;L change per 1% move in interest rate. Usually small compared to other Greeks." />
        </div>
        {#if (g.iv ?? g.greeks?.iv) != null}
          <div class="cw-greek-item">
            <span class="cw-gk-label">IV</span>
            <span class="cw-gk-val cw-gk-amber">
              {((g.iv ?? g.greeks?.iv) * 100).toFixed(1)}%
            </span>
            <InfoHint popup text="Implied Volatility — the market's consensus forecast of how much the underlying will move. Higher IV = more expensive options." />
          </div>
        {/if}
        {#if (g.ltp ?? g.pricing?.ltp) != null}
          <div class="cw-greek-item cw-greek-item-sep">
            <span class="cw-gk-label">LTP</span>
            <span class="cw-gk-val cw-gk-sky">₹{priceFmt(g.ltp ?? g.pricing?.ltp)}</span>
          </div>
        {/if}
      {:else}
        <span class="cw-meta-text">Loading Greeks…</span>
      {/if}
    </div>
  {/if}
</div>

<style>
  .cw-root {
    display: flex;
    flex-direction: column;
    width: 100%;
    flex: 1 1 0;
    min-height: 0;
    box-sizing: border-box;
    background: var(--card-bg-gradient);
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    border-radius: 6px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.08);
    /* overflow: visible so the Overlays MultiSelect dropdown panel
       (position:absolute) is not clipped by the rounded card edge.
       The chart container below has its own `overflow: hidden` so the
       SVG content still stays inside; only the dropdown panel — which
       is the operator's "Indicators" picker — escapes downward into
       the chart area, matching the TradingView pattern. */
    overflow: visible;
  }

  /* ── Header wrapper: merges picker + controls into one row on desktop ── */
  .cw-header {
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
  }
  @media (min-width: 640px) {
    .cw-header {
      flex-direction: row;
      flex-wrap: nowrap;
      align-items: center;
    }
    .cw-header .cw-picker  { flex-shrink: 0; border-bottom: none; border-right: 1px solid rgba(255,255,255,0.06); }
    .cw-header .cw-controls { flex: 1 1 0; min-width: 0; border-bottom: none; }
  }

  /* ── Picker bar ─────────────────────────────────────────── */
  .cw-picker {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    /* Reduced bottom padding (was 0.4rem) so the picker row sits
       tight against the controls row below — operator: "reduce the
       wasted vertical gap between button and chart minimal". */
    padding: 0.35rem 0.75rem 0.2rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    flex-wrap: wrap;
    flex-shrink: 0;
  }

  /* Chart controls row (Type · Intraday · 1D/1W/1M/3M/6M/1Y · Reset).
     Same layout family as .cw-picker but the row above it carries the
     symbol search; this one always renders, hosted outside the
     compact-mode gate so the date-range pills are present in every
     embed surface. Bottom padding minimised to 0.15rem so the SVG
     starts almost flush with the toolbar row — operator: "reduce the
     wasted vertical gap between button and chart minimal". */
  .cw-controls {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.2rem 0.75rem 0.15rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    flex-wrap: wrap;
    flex-shrink: 0;
  }

  .cw-type-wrap {
    /* Fixed width — sized to fit the widest label ("EQ · FUT · OPT")
       so the trigger doesn't reflow when the operator picks a
       narrower value (Equity / Futures / Options). Earlier min/max
       range let the trigger grow / shrink with the selection. */
    width: 8.5rem;
    flex-shrink: 0;
  }
  .cw-type-wrap :global(.rbq-select-trigger) {
    width: 100%;
  }
  /* ── Range pill group ────────────────────────────────────── */
  .cw-range-group {
    display: inline-flex;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 3px;
    overflow: hidden;
    flex-shrink: 0;
  }
  .cw-range-btn {
    /* SSOT chart-toolbar height — keeps range pills, Select triggers,
       MultiSelect trigger, symbol input and intraday chip on the same
       baseline. inline-flex + align-items center so the text glyph
       sits on the visual midline regardless of font ascent metrics. */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: var(--chart-toolbar-h);
    min-height: var(--chart-toolbar-h);
    padding: 0 0.55rem;
    background: transparent;
    border: 0;
    border-right: 1px solid rgba(255, 255, 255, 0.06);
    color: var(--algo-muted);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
  }
  .cw-range-btn:last-child:not(.cw-signals-btn) { border-right: 0; }
  .cw-range-btn:hover { background: var(--algo-cyan-bg-soft); color: var(--algo-slate); }
  .cw-range-btn.active {
    /* Active state — cyan-400 (canonical). Operator: "check for colour
       consistency in charts and dashboard". Matches card-header trio
       (collapse / fullscreen / refresh) + AlgoTabs `algo-tab-c-cyan`. */
    background: var(--algo-cyan-bg);
    color: var(--algo-cyan);
    font-weight: 800;
  }
  /* Standalone intraday chip — same shape as a range pill, but
     rounded on both ends (not in the segmented .cw-range-group). */
  .cw-intraday-btn {
    border: 1px solid var(--algo-cyan-border-soft);
    border-radius: 4px;
    flex-shrink: 0;
  }
  .cw-intraday-btn.active {
    background: var(--algo-cyan-bg);
    border-color: var(--algo-cyan-border);
    color: var(--algo-cyan);
  }
  /* Signals toggle — same chip shape as intraday button.
     Active state surfaces buy/sell markers on the chart. */
  .cw-signals-btn {
    border: 1px solid var(--algo-cyan-border);
    border-radius: 4px;
    flex-shrink: 0;
  }
  .cw-signals-btn.active {
    background: var(--algo-cyan-bg);
    border-color: var(--algo-cyan-border);
    color: var(--algo-cyan);
  }

  /* ── Buy/sell signal markers ──────────────────────────────────
     Triangles + tag labels rendered on the price panel. The
     `pointer-events: bounding-box` lets the <title> tooltip fire
     when the operator hovers anywhere over the marker glyph or its
     tag (without intercepting the click-to-pin behaviour, which
     fires on the SVG itself).  Green = BUY (emerald-400), red =
     SELL (red-400) per CLAUDE.md algo palette. */
  :global(.signal-marker) {
    pointer-events: bounding-box;
    cursor: help;
  }
  :global(.signal-marker .signal-tag) {
    user-select: none;
    pointer-events: none;
    paint-order: stroke fill;
    stroke: rgba(10, 14, 20, 0.85);
    stroke-width: 2.5px;
    stroke-linejoin: round;
  }

  /* ── Toolbar Select wrappers ─────────────────────────────── */
  .cw-toolbar-select {
    flex-shrink: 0;
    min-width: 6rem;
    max-width: 9rem;
  }
  /* Force every Select trigger inside the toolbar (chart-type, symbol
     type filter) to the SSOT height. Without this they default to the
     Select component's own min-height (1.55rem ≈ 25 px) which left
     them visibly shorter than the 28 px range pills. Operator: "the
     button and dropdown sizes are inconsistent". */
  .cw-toolbar-select :global(.rbq-select-trigger),
  .cw-type-wrap :global(.rbq-select-trigger) {
    height: var(--chart-toolbar-h);
    min-height: var(--chart-toolbar-h);
    padding-top: 0;
    padding-bottom: 0;
  }
  /* Chart-type select lives in the picker row, immediately to the right
     of the symbol search — flush-left layout per the canonical card-
     header rule (no auto-margin spacer). Operator: "line/area/candle
     picker should sit flush-left with symbol picker on desktop".
     Mobile sizing is overridden in the @media block below.
     `margin-left: 0` is explicit (overriding any inherited auto) so the
     picker row reads as one left-aligned control cluster. */
  .cw-type-chart-wrap {
    margin-left: 0;
  }
  /* Symbol search input also rides the SSOT toolbar height so it lines
     up with the chart-type Select beside it. Operator: "the button and
     dropdown sizes are inconsistent". Scoped to .cw-picker so the
     override doesn't leak into other surfaces (Pulse / Orders) that
     mount SymbolSearchInput at its native compact height. */
  .cw-picker :global(.ssi-input) {
    height: var(--chart-toolbar-h);
    min-height: var(--chart-toolbar-h);
    padding-top: 0;
    padding-bottom: 0;
    box-sizing: border-box;
  }

  /* ── Overlays panel — inline in controls row (row 2, after 1Y) ── */
  /* Previously position:absolute top-right of the chart; now a flex
     item in .cw-controls so it lives beside the range pill group.
     Wrapper is transparent — the inner .rbq-multi-trigger carries its
     own border (matches Select trigger palette: amber-soft border on
     a navy gradient). Operator: "MultiSelect trigger color matches
     Select triggers". */
  .cw-overlay-panel {
    position: relative;
    flex-shrink: 0;
    pointer-events: auto;
  }
  /* Trigger height locked to SSOT var; padding zeroed vertically so the
     fixed height drives the box. Hover/focus state mirrors Select
     trigger (amber-soft → amber on intent). */
  .cw-overlay-panel :global(.rbq-multi-trigger) {
    height: var(--chart-toolbar-h);
    min-height: var(--chart-toolbar-h);
    padding-top: 0;
    padding-bottom: 0;
    font-size: var(--fs-sm);
  }
  /* Dropdown panel — keep right-aligned so it doesn't clip viewport,
     and lifted above the chart SVG via z-index so the checkbox list is
     clickable when it overlaps the bars. Higher than the SVG hover
     popup's z-index (10) so dropdown beats popup on overlap. */
  .cw-overlay-panel :global(.rbq-multi-panel) {
    left: auto;
    right: 0;
    min-width: 9rem;
    z-index: 80;
  }

  /* ── Hover popup (shared by historical and intraday) ─────────────────
     Canonical styles live in app.css (.chart-tooltip family).
     The .up/.down directional colors for .chart-tooltip-value are in
     app.css as global selectors (.chart-tooltip-value.up / .down). */

  .cw-reset-zoom {
    /* SSOT chart-toolbar height — Reset rides the same --chart-toolbar-h
       var as range pills, Select triggers, MultiSelect trigger, intraday
       toggle and symbol input so the whole toolbar row reads as a single
       baseline (operator: "reset also should have the same height"). */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    height: var(--chart-toolbar-h);
    min-height: var(--chart-toolbar-h);
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 0 0.55rem;
    border-radius: 3px;
    border: 1px solid rgba(251,191,36,0.50);
    background: rgba(251,191,36,0.12);
    color: var(--c-action);
    cursor: pointer;
    margin-left: auto;
  }
  .cw-reset-zoom:hover { background: var(--c-action-22); }

  /* ── Chart container + SVG ───────────────────────────────── */
  /* flex:1 makes this absorb ALL available vertical space. The chain
     `.charts-page-wrap (flex col, fills algo-content) → .chart-body
     (flex:1, min-height:0) → .cw-root (flex col, h:100%) → here` lets
     the SVG claim every residual pixel without overflowing. Toolbar
     rows (.cw-picker / .cw-controls / .cw-frontmonth-bar / .cw-info-
     strip / .cw-greeks-strip) are all flex-shrink:0 so they hold
     their natural height while this container stretches into all the
     leftover space.

     Safety floor `min-height: 200px` — prevents the chart from
     collapsing to zero if the flex chain ever resolves residual to a
     non-positive value (e.g. .algo-card / .algo-viewport using
     min-height instead of fixed height, parent ancestor with display
     other than flex, ResizeObserver firing pre-hydration). The flex
     chain still claims every leftover pixel above this floor; the
     floor only engages when the chain breaks. Earlier slice
     (a398ab81) dropped the floor and shipped a desktop regression
     where the chart card briefly contracted with no visible content.
     Compact-mode override below still enforces its own 160 px floor
     for embedded mounts (e.g. modal preview); fullscreen override
     uses calc(100vh - chrome). */
  .cw-chart-container {
    flex: 1 1 0;
    min-height: 200px;
    width: 100%;
    position: relative;
    overflow: hidden;
  }
  /* Fetch-from-broker overlay — see template above. Sits ABOVE the
     chart SVG; pointer-events: none so the operator can still
     interact with the underlying axis hovers + the Overlays
     dropdown if they want. */
  .cw-fetch-overlay {
    position: absolute;
    inset: 0;
    z-index: 5;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
    pointer-events: none;
    background: linear-gradient(180deg,
                  rgba(10, 16, 32, 0.35) 0%,
                  rgba(10, 16, 32, 0.65) 100%);
    backdrop-filter: blur(2px);
    animation: cw-fetch-fade-in 0.18s ease-out;
  }
  @keyframes cw-fetch-fade-in {
    from { opacity: 0; }
    to   { opacity: 1; }
  }
  .cw-fetch-spinner {
    width: 1.4rem;
    height: 1.4rem;
    border: 2px solid rgba(251, 191, 36, 0.18);
    border-top-color: var(--c-action);
    border-radius: 50%;
    animation: cw-fetch-spin 0.9s linear infinite;
  }
  @keyframes cw-fetch-spin {
    to { transform: rotate(360deg); }
  }
  .cw-fetch-msg {
    color: var(--c-action);
    font-size: var(--fs-lg);
    font-weight: 600;
    letter-spacing: 0.03em;
  }
  .cw-fetch-sub {
    color: rgba(155, 176, 208, 0.65);
    font-size: var(--fs-sm);
    letter-spacing: 0.02em;
  }
  .cw-svg {
    width: 100%;
    height: 100%;
    display: block;
    cursor: crosshair;
    touch-action: pan-y;
  }
  .cw-svg.cw-panning { cursor: grabbing; }

  /* compact mode: keep a fixed reasonable height */
  :global(.cw-root.cw-compact) .cw-chart-container {
    height: var(--chart-h, 240px);
    min-height: 160px;
    flex: 0 0 auto;
  }

  /* Fullscreen card override — chart fills most of viewport */
  :global(.fs-card-on) .cw-chart-container {
    min-height: calc(100vh - 14rem) !important;
  }
  @media (max-width: 600px) {
    :global(.fs-card-on) .cw-chart-container {
      min-height: calc(100vh - 12rem) !important;
    }
  }

  /* In-flight chart-load lock — applied to the symbol picker bar and
     the controls row while `_histLoading` is true. CSS
     `pointer-events: none` covers child elements that don't accept
     the `disabled` prop (e.g. SymbolSearchInput's input field) so
     every fetch-triggering control is uniformly inert. Opacity dims
     the cluster so the lock state reads at a glance. */
  .cw-picker-busy,
  .cw-controls-busy {
    pointer-events: none;
    opacity: 0.55;
  }
  /* Spinner glyphs retired — the rotating chart title-icon in
     ChartModal + the page-header RefreshButton's loading swap are the
     canonical refresh indicators now; no in-chart spinner needed. */

  /* ── State / empty ───────────────────────────────────────── */
  .cw-state {
    height: 100%;
    min-height: 160px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--algo-muted);
    font-size: var(--fs-md);
    font-family: monospace;
    padding: 0 1rem;
    text-align: center;
  }
  .cw-state-sm { min-height: 80px; height: auto; }
  .cw-err { color: var(--c-short); }
  .cw-err-text { color: var(--c-short); font-size: var(--fs-xs); font-family: monospace; }

  /* ── Intraday section ────────────────────────────────────── */
  .cw-intraday-section {
    border-top: 1px dashed rgba(125,211,252,0.20);
    padding-top: 0;
    flex-shrink: 0;
    height: 25vh;
    min-height: 120px;
    max-height: 200px;
    display: flex;
    flex-direction: column;
  }
  .cw-intraday-label {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.75rem;
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    font-family: monospace;
    flex-shrink: 0;
  }
  .cw-intraday-mode {
    font-weight: 700;
    font-size: var(--fs-2xs);
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
  }
  .cw-mode-live  { color: var(--c-long); }
  .cw-mode-sim   { color: var(--c-action); }
  .cw-mode-paper { color: #7dd3fc; }
  .cw-intraday-source {
    font-weight: 700;
    font-size: var(--fs-2xs);
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid var(--algo-amber-border);
    color: var(--c-action);
    letter-spacing: 0.04em;
  }
  .cw-intraday-svg {
    flex: 1 1 0;
    width: 100%;
    min-height: 0;
    display: block;
  }

  /* ── Info strip ──────────────────────────────────────────── */
  .cw-info-strip {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.3rem 0.75rem;
    border-top: 1px solid rgba(255,255,255,0.06);
    font-family: monospace;
    font-size: var(--fs-sm);
    flex-wrap: wrap;
    flex-shrink: 0;
  }
  .cw-info-sym   { color: #7dd3fc; font-weight: 700; }
  .cw-info-close { color: var(--algo-slate); font-variant-numeric: tabular-nums; }
  .cw-info-pct   { font-variant-numeric: tabular-nums; font-weight: 700; }
  .cw-pos { color: var(--c-long); }
  .cw-neg { color: var(--c-short); }
  .cw-info-meta { color: var(--algo-muted); }
  .cw-meta-text { color: var(--algo-muted); font-size: var(--fs-xs); font-family: monospace; }
  .cw-info-root {
    color: #4a5a7a;
    font-size: var(--fs-xs);
    font-family: monospace;
  }
  .cw-legend-dash {
    display: inline-block;
    width: 14px;
    height: 0;
    border-top: 1px dashed #7dd3fc;
    opacity: 0.7;
    vertical-align: middle;
    margin-right: 2px;
  }

  /* ── Front-month resolution chip ────────────────────────────── */
  .cw-frontmonth-bar {
    display: flex;
    align-items: center;
    /* Tightened from 0.2rem to 0.1rem vertical so it adds minimal
       vertical chrome between the controls row and the chart SVG. */
    padding: 0.1rem 0.75rem;
    flex-shrink: 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .cw-fm-chip {
    display: inline-flex;
    align-items: center;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--algo-muted);
    background: rgba(125, 145, 184, 0.08);
    border: 1px solid rgba(125, 145, 184, 0.22);
    border-radius: 3px;
    padding: 0.12rem 0.45rem;
    white-space: nowrap;
    cursor: default;
    user-select: none;
  }
  /* Amber roll-warning variant — expiry ≤ 3 days */
  .cw-fm-chip.cw-fm-rolling {
    color: var(--c-action);
    background: var(--algo-amber-bg);
    border-color: rgba(251, 191, 36, 0.42);
  }
  @media (max-width: 600px) {
    .cw-frontmonth-bar { padding: 0.2rem 0.5rem; }
    .cw-fm-chip { font-size: var(--fs-xs); }
  }

  /* ── Greeks strip ────────────────────────────────────────── */
  .cw-greeks-strip {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.4rem 0.75rem;
    border-top: 1px solid rgba(255,255,255,0.06);
    background: rgba(0,0,0,0.12);
    flex-wrap: wrap;
    flex-shrink: 0;
  }
  .cw-greeks-label {
    font-family: monospace;
    font-size: var(--fs-xs);
    font-weight: 700;
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .cw-greeks-err { margin-left: 0.5rem; }
  .cw-greek-item {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }
  .cw-greek-item-sep {
    margin-left: 0.5rem;
    padding-left: 0.5rem;
    border-left: 1px solid rgba(255,255,255,0.10);
  }
  .cw-gk-label {
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    color: var(--algo-muted);
  }
  .cw-gk-val {
    font-family: monospace;
    font-size: var(--fs-md);
    font-variant-numeric: tabular-nums;
    color: var(--algo-slate);
  }
  .cw-gk-amber { color: var(--c-action); }
  .cw-gk-sky   { color: #7dd3fc; }

  @media (max-width: 600px) {
    .cw-picker        { gap: 0.35rem; padding: 0.4rem 0.5rem; }
    .cw-info-strip    { padding: 0.25rem 0.5rem; gap: 0.4rem; }
    .cw-greeks-strip  { padding: 0.3rem 0.5rem; gap: 0.5rem; }
    .cw-sym-input     { min-width: 7rem; }
    .cw-intraday-section { height: 30vh; }
    /* Let toolbar dropdowns go full-flex on phone so they wrap cleanly */
    .cw-toolbar-select { min-width: 0; flex: 1 1 auto; }
    /* Chart-type wrap loses the auto-margin on narrow viewports so it
       wraps onto a second line of the picker row rather than cramping
       the symbol search. */
    .cw-type-chart-wrap { margin-left: 0; }
  }

  /* ── Mobile toolbar: fit both rows on one line at ≤520px ─────────
     Row 1: [type filter] [symbol search — flex-grow] [chart type]
     Row 2: [Intra] [1D 1W 1M 3M 6M 1Y] [Overlays]
     All controls are kept ≥32px height for tap-target compliance.
     No flex-wrap on either row at this breakpoint so they stay single-line. */
  @media (max-width: 520px) {
    /* Row 1 — prevent wrapping; tighter gaps + padding */
    .cw-picker {
      flex-wrap: nowrap;
      gap: 0.25rem;
      padding: 0.35rem 0.4rem;
    }

    /* Type-filter (All/Equity/Futures/Options) — narrow to 4.2rem;
       text-overflow ellipsis on the trigger label handles "Futures". */
    .cw-type-wrap {
      width: 4.2rem;
      flex-shrink: 0;
    }

    /* Symbol search — fills remaining space between type filter and
       chart-type select; min-width 0 lets it compress. */
    .cw-picker :global(.ssi-wrap) {
      flex: 1 1 0;
      min-width: 0;
    }
    /* ssi-input: fills wrapper width. Height comes from the SSOT var
       (--chart-toolbar-h = 32px on mobile via app.css :root override). */
    .cw-picker :global(.ssi-input) {
      width: 100%;
    }

    /* Chart-type select — fixed narrow width; auto-margin removed so
       the symbol search expands between the two selects. */
    .cw-type-chart-wrap {
      width: 4.2rem;
      flex-shrink: 0;
      margin-left: 0;
    }
    .cw-toolbar-select {
      min-width: 0;
    }

    /* Row 2 — prevent wrapping; tighter gaps + padding */
    .cw-controls {
      flex-wrap: nowrap;
      gap: 0.22rem;
      padding: 0.3rem 0.4rem;
    }

    /* Intraday button — tighter horizontal padding; show short label.
       Height comes from --chart-toolbar-h (32 px at this breakpoint). */
    .cw-intraday-btn {
      padding: 0 0.32rem;
      font-size: var(--fs-xs);
      flex-shrink: 0;
    }
    .cw-intraday-full { display: none; }
    .cw-intraday-short { display: inline; }
    .cw-signals-full { display: none; }
    .cw-signals-short { display: inline; }

    /* Signals button — tighter horizontal padding; show short label. */
    .cw-signals-btn {
      padding: 0 0.32rem;
      font-size: var(--fs-xs);
      flex-shrink: 0;
    }

    /* Range pills — squeeze horizontal padding; height stays on SSOT var. */
    .cw-range-btn {
      padding: 0 0.3rem;
      font-size: var(--fs-xs);
    }

    /* Overlay panel — just enough to show "Overlays" text. Height
       inherited from --chart-toolbar-h. */
    .cw-overlay-panel {
      flex-shrink: 0;
    }
    .cw-overlay-panel :global(.rbq-multi-trigger-wrap),
    .cw-overlay-panel :global(.rbq-multi-trigger) {
      padding: 0 0.28rem;
      font-size: var(--fs-xs);
      white-space: nowrap;
    }

    /* Reset zoom — on mobile, fill the available trailing space so the
       affordance is easy to tap (operator: "let the reset button use
       available space on mobile for charts"). margin-left: auto would
       consume the free space as margin and starve flex-grow — clear it
       and use flex: 1 1 auto so the button claims the leftover width.
       min-width 4rem floors the affordance at a tappable size even when
       the rest of the toolbar gets greedy. */
    .cw-reset-zoom {
      flex: 1 1 auto;
      min-width: 4rem;
      margin-left: 0;
      padding: 0 0.4rem;
      font-size: var(--fs-sm);
    }
  }

  /* Default (≥521px): "Intraday" full label, short label hidden */
  .cw-intraday-full  { display: inline; }
  .cw-intraday-short { display: none; }
  .cw-signals-full   { display: inline; }
  .cw-signals-short  { display: none; }

  .chart-partial-hint {
    font-size: 11px;
    color: var(--text-faint);
    opacity: 0.8;
    white-space: nowrap;
  }
</style>
