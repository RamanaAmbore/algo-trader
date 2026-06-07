<script module>
  // Module-level OHLCV cache — operator reported chart re-fetches
  // when re-opening the same symbol. Each ChartWorkspace instance
  // fires _loadHistorical(true) more than once on mount, and a new
  // instance is created every time ChartModal mounts; without this
  // cache every open round-trips even when the backend returns the
  // same bytes. Audit caught that previously this cache lived in
  // the per-instance <script> block, making it a no-op — moved here
  // so it's truly module-scoped.
  //
  // Key: `${symbol}|${exchange}|${days}|${interval}|${underlying}`.
  // The `interval` segment was missing originally — if the chart
  // ever wires a 1h toggle the FE would serve daily bars for an
  // hourly request without it.
  /** @type {Map<string, {bars:any[], spotBars:any[], expiresAt:number}>} */
  const _BAR_CACHE = new Map();
  const _BAR_CACHE_TTL_MS = 60_000;
  const _BAR_CACHE_MAX    = 60;

  export function _cacheGet(/** @type {string} */ key) {
    const e = _BAR_CACHE.get(key);
    if (!e) return null;
    if (Date.now() >= e.expiresAt) { _BAR_CACHE.delete(key); return null; }
    // True LRU — touch on access so the next eviction targets the
    // genuinely least-recently-used entry rather than oldest-by-
    // insertion.
    _BAR_CACHE.delete(key);
    _BAR_CACHE.set(key, e);
    return e;
  }

  export function _cachePut(/** @type {string} */ key,
                            /** @type {any[]} */ bars,
                            /** @type {any[]} */ spotBars) {
    _BAR_CACHE.set(key, { bars, spotBars, expiresAt: Date.now() + _BAR_CACHE_TTL_MS });
    if (_BAR_CACHE.size > _BAR_CACHE_MAX) {
      const oldest = _BAR_CACHE.keys().next().value;
      if (oldest != null) _BAR_CACHE.delete(oldest);
    }
  }

  /** Pre-warm the OHLCV cache for a symbol — operator hovers the
   *  chart-open affordance, we fetch the bars in the background, then
   *  the click-to-open is instant (cache hit). Operator: "I see delay
   *  chart plotting first time when I open the modal." Idempotent
   *  per-symbol within the 60 s TTL; multiple hover events collapse
   *  into one network round-trip.
   *  @param {string} symbol
   *  @param {string} [exchange]
   *  @param {number} [days]
   */
  export async function prefetchChartBars(symbol, exchange = '', days = 30) {
    if (!symbol) return;
    const _interval = 'day';
    const key = `${symbol.toUpperCase()}|${(exchange || '').toUpperCase()}|${days}|${_interval}|`;
    if (_cacheGet(key)) return;  // already warm
    try {
      // Lazy-import to avoid a circular static-import graph between
      // ChartWorkspace.svelte and api.js at module load.
      const { fetchOptionsHistorical } = await import('$lib/api');
      const hist = await fetchOptionsHistorical(symbol, { days, exchange: exchange || undefined });
      const bars = Array.isArray(hist?.bars) ? hist.bars : [];
      if (bars.length) _cachePut(key, bars, []);
    } catch (_) { /* silent — best-effort prefetch */ }
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
  import {
    fetchOptionsHistorical,
    fetchChartPriceHistory,
    fetchSimStatus,
    fetchPaperStatus,
    fetchStrategyAnalytics,
    fetchChartSymbols,
    fetchWatchlists,
    fetchWatchlist,
  } from '$lib/api';
  import {
    loadInstruments, searchByPrefix, suggestUnderlyings,
    findEquity, findNearestFuture, getInstrument,
  } from '$lib/data/instruments';
  import { resolveUnderlying, MCX_COMMODITIES, CDS_CURRENCIES, INDEX_LTP_KEY } from '$lib/data/resolveUnderlying';
  import { SYM_TYPE_OPTS } from '$lib/data/symbolTypes';
  import { visibleInterval } from '$lib/stores';
  import { priceFmt } from '$lib/format';
  import InfoHint from '$lib/InfoHint.svelte';
  import Select from '$lib/Select.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import SymbolSearchInput from '$lib/SymbolSearchInput.svelte';

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
    { value: 'line',   label: 'Line' },
    { value: 'area',   label: 'Area' },
    { value: 'candle', label: 'Candle' },
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
    { value: 'bb',       label: 'Bollinger' },
    { value: 'rsi',      label: 'RSI 14' },
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
  const _isOption     = $derived(/(?:CE|PE)$/i.test(symbol));
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

  // ── Historical OHLCV ──────────────────────────────────────────────
  /** @type {Array<{ts:string,open:number,high:number,low:number,close:number,volume:number}>} */
  let _bars        = $state([]);
  let _histLoading = $state(false);
  let _histError   = $state('');
  let _chartLoaded = $state(false);
  let _chartDays   = $state(30);
  let _chartType   = $state(/** @type {'line'|'area'|'candle'|'plot'} */('line'));
  // Overlays MultiSelect — drives derived booleans below. Volume
  // is no longer in this list (always-on via _showVol const below).
  let _overlays    = $state(/** @type {string[]} */([]));
  // Tracks whether the Overlays MultiSelect dropdown is open — used to
  // suppress both hover popups so they don't clash with the open panel.
  let _overlayOpen = $state(false);
  // Intraday tick stream — single boolean, toggled by a chip in the toolbar.
  let _intradayOn = $state(false);
  const _showSma20 = $derived(_overlays.includes('sma20'));
  const _showSma50 = $derived(_overlays.includes('sma50'));
  // Always on — volume bars render unconditionally.
  const _showVol   = true;
  const _showEma20 = $derived(_overlays.includes('ema20'));
  const _showEma50 = $derived(_overlays.includes('ema50'));
  const _showBb    = $derived(_overlays.includes('bb'));
  const _showRsi   = $derived(_overlays.includes('rsi'));

  // Expose loading state to parent via $bindable prop.
  $effect(() => { loading = _histLoading; });

  /** @type {Array<{ts:string,close:number}>} */
  let _spotBars = $state([]);

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
    // commodity root, CDS currency root) to its nearest-month future.
    // Without this, "CRUDEOIL" / "GOLD" / "USDINR" hit the historical
    // endpoint literally — the backend walks every exchange and
    // returns empty bars (the "No data available" first-load bug).
    // The pinned-dropdown picker already goes through this resolver
    // via _loadPin, so a second click on the same pin renders the
    // chart correctly — the fix is making the initial render do the
    // same translation.
    const upper = String(sym || '').toUpperCase();
    const indexRoot = _KITE_INDEX_TO_ROOT[upper];        // 'NIFTY 50' → 'NIFTY'
    const isMcx     = MCX_COMMODITIES.has(upper);        // 'CRUDEOIL', 'GOLD', …
    const isCds     = CDS_CURRENCIES.has(upper);         // 'USDINR'
    const root      = indexRoot || (isMcx || isCds ? upper : null);
    if (!root) return { sym, exch: '' };

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
      // Default exchange: MCX for commodities, CDS for currencies, NFO
      // for indices. The instruments row's `e` field is the source of
      // truth when present (handles edge cases like FUT-on-BFO).
      const defaultExch = isMcx ? 'MCX' : (isCds ? 'CDS' : 'NFO');
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

  async function _loadHistorical(/** @type {boolean} */ force = false) {
    if (!symbol) return;
    if (!force && _chartLoaded) return;
    const token = ++_loadToken;
    _histLoading = true; _histError = '';
    // Hard timeout — if a broker call hangs (e.g. Kite rate-limit retry
    // loop on backend), Promise.race ensures _histLoading clears within
    // 25s instead of stranding the page-header RefreshButton spinner
    // forever. The error surfaces in _histError as a short banner.
    const TIMEOUT_MS = 25000;
    const timeout = new Promise((_, reject) =>
      setTimeout(() => reject(new Error('Slow response — try again.')), TIMEOUT_MS)
    );
    try {
      // Map Kite index quote-keys to their tradeable future before any
      // backend call. NIFTY BANK / NIFTY 50 etc. would otherwise walk
      // every exchange arm and time out (~10s of broker calls).
      // Awaited because the resolver may need to hydrate the
      // instruments cache before findNearestFuture can return.
      const _resolved = await _resolveFetchSymbol(symbol);
      if (token !== _loadToken) return;
      const fetchSym  = _resolved.sym;
      const fetchExch = _resolved.exch || _resolvedExchange || exchange || undefined;

      // Module-level cache lookup — see _BAR_CACHE comment in the
      // <script module> block at top. Cache key includes interval
      // (currently always "day" but future-proofed against an
      // intraday/hourly toggle) so a 1h request can never serve
      // cached daily bars.
      const _interval = 'day';
      const cacheKey = `${fetchSym}|${(fetchExch || '').toUpperCase()}|${_chartDays}|${_interval}|${_isDerivative ? (_underlying || '') : ''}`;
      const _cached = _cacheGet(cacheKey);
      if (_cached) {
        _bars = _cached.bars;
        _spotBars = _cached.spotBars;
        if (!_bars.length) _histError = 'No data available.';
        _chartLoaded = true;
        return;
      }

      const promises = [
        fetchOptionsHistorical(fetchSym, { days: _chartDays, exchange: fetchExch }),
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
      if (token !== _loadToken) return;   // a newer call superseded this one
      _bars     = Array.isArray(hist?.bars) ? hist.bars : [];
      _spotBars = spotHist ? (Array.isArray(spotHist.bars) ? spotHist.bars : []) : [];
      if (!_bars.length) _histError = 'No data available.';
      _chartLoaded = true;

      // Cache write — only when we got non-empty bars; empty results
      // happen on rate-limit / preview blocked and shouldn't poison
      // the next open. LRU eviction handled by _cachePut.
      if (_bars.length) {
        _cachePut(cacheKey, _bars, _spotBars);
      }
    } catch (e) {
      if (token !== _loadToken) return;   // newer call already in flight — its result is the canonical one
      _histError = /** @type {any} */ (e)?.message || 'Load failed';
      _bars = [];
    } finally {
      // Only the newest call flips loading off; older tokens are no-ops
      // here so the spinner stays visible while the canonical fetch is
      // still in flight.
      if (token === _loadToken) _histLoading = false;
      // Force a dimension re-measure after load. The ResizeObserver may
      // have fired while the modal/portal was still laying out (container
      // at zero width), leaving _chartW/_chartH stale and all SVG paths
      // computed against a degenerate viewBox. One rAF after the bars
      // land ensures the container has its final CSS dimensions.
      requestAnimationFrame(() => {
        const el = _chartContainerEl;
        if (el) {
          const r = el.getBoundingClientRect();
          const w = Math.max(360, Math.round(r.width));
          const h = Math.max(200, Math.round(r.height));
          if (w !== _chartW || h !== _chartH) {
            _chartW = w;
            _chartH = h;
          }
        }
      });
    }
  }

  function _setRange(/** @type {number} */ d) {
    if (d === _chartDays) return;
    _chartDays = d;
    _chartLoaded = false;
    _chartHover = null;
    zoom = null;
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
    _tickTimer = setInterval(_loadIntraday, 3000);
  }
  function _stopTickPoll() {
    if (_tickTimer) { clearInterval(_tickTimer); _tickTimer = null; }
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

  const CPAD_L  = 56;
  const CPAD_R  = 16;
  const CPAD_T  = 16;
  const CPAD_B  = 30;
  const RSI_H   = 56;   // RSI sub-panel height in SVG user units
  const _innerW = $derived(_chartW - CPAD_L - CPAD_R);
  // _bandH reserves vertical space at the bottom for sub-panels (volume + RSI).
  // Volume bars always sit in the bottom VOL_H px of the price area; RSI sits
  // below that. _innerH is the price-chart's usable height — overlays + price
  // lines must not draw below CPAD_T + _innerH.
  const _bandH  = $derived((_showRsi ? RSI_H : 0));
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

  const _dataXMin = $derived(_barXs.length ? Math.min(..._barXs) : 0);
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

  function _smaPath(/** @type {number} */ window) {
    if (!_bars.length || _bars.length < window) return '';
    let d = '';
    for (let i = window - 1; i < _bars.length; i++) {
      let sum = 0;
      for (let j = i - window + 1; j <= i; j++) sum += Number(_bars[j].close);
      const avg = sum / window;
      const t   = Date.parse(_bars[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = _xOf(t), y = _yOf(avg);
      d += (d === '' ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  }
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
  function _emaPath(/** @type {number} */ n) {
    if (_bars.length < n) return '';
    const k = 2 / (n + 1);
    let ema = _bars.slice(0, n).reduce((s, b) => s + Number(b.close), 0) / n;
    let d = '';
    for (let i = n - 1; i < _bars.length; i++) {
      if (i > n - 1) ema = Number(_bars[i].close) * k + ema * (1 - k);
      const t = Date.parse(_bars[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = _xOf(t), y = _yOf(ema);
      d += (d ? ` L${x.toFixed(2)},${y.toFixed(2)}` : `M${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  }
  const _ema20Path = $derived(_showEma20 ? _emaPath(20) : '');
  const _ema50Path = $derived(_showEma50 ? _emaPath(50) : '');

  // ── Bollinger Bands (20-period, ±2σ) ─────────────────────────────
  // Returns mid / upper / lower path strings + a closed fill path.
  const _bbPaths = $derived.by(() => {
    if (!_showBb || _bars.length < 20) return { mid: '', upper: '', lower: '', fill: '' };
    const N = 20, K = 2;
    let mid = '', upper = '', lower = '';
    /** @type {Array<{x:number,yU:number,yL:number}>} */
    const ribbon = [];
    for (let i = N - 1; i < _bars.length; i++) {
      let sum = 0;
      for (let j = i - N + 1; j <= i; j++) sum += Number(_bars[j].close);
      const m = sum / N;
      let v = 0;
      for (let j = i - N + 1; j <= i; j++) {
        const diff = Number(_bars[j].close) - m;
        v += diff * diff;
      }
      const sd = Math.sqrt(v / N);
      const u = m + K * sd, l = m - K * sd;
      const t = Date.parse(_bars[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = _xOf(t);
      mid   += (mid   ? ` L${x.toFixed(2)},${_yOf(m).toFixed(2)}` : `M${x.toFixed(2)},${_yOf(m).toFixed(2)}`);
      upper += (upper ? ` L${x.toFixed(2)},${_yOf(u).toFixed(2)}` : `M${x.toFixed(2)},${_yOf(u).toFixed(2)}`);
      lower += (lower ? ` L${x.toFixed(2)},${_yOf(l).toFixed(2)}` : `M${x.toFixed(2)},${_yOf(l).toFixed(2)}`);
      ribbon.push({ x, yU: _yOf(u), yL: _yOf(l) });
    }
    // Shaded fill — upper line forward, lower reversed, closed.
    let fill = '';
    if (ribbon.length) {
      fill = `M${ribbon[0].x.toFixed(2)},${ribbon[0].yU.toFixed(2)}`;
      for (let i = 1; i < ribbon.length; i++) fill += ` L${ribbon[i].x.toFixed(2)},${ribbon[i].yU.toFixed(2)}`;
      for (let i = ribbon.length - 1; i >= 0; i--) fill += ` L${ribbon[i].x.toFixed(2)},${ribbon[i].yL.toFixed(2)}`;
      fill += ' Z';
    }
    return { mid, upper, lower, fill };
  });

  // ── RSI 14 (Wilder's smoothed RSI) ───────────────────────────────
  // Returns a series of {ts, rsi} points for sub-panel rendering.
  // The sub-panel has its own y-scale 0–100 (independent of price).
  const RSI_N = 14;
  const _rsiSeries = $derived.by(() => {
    if (!_showRsi || _bars.length < RSI_N + 1) return /** @type {Array<{ts:string,rsi:number}>} */ ([]);
    /** @type {Array<{ts:string,rsi:number}>} */
    const out = [];
    let avgGain = 0, avgLoss = 0;
    // Seed: first RSI_N changes
    for (let i = 1; i <= RSI_N; i++) {
      const ch = Number(_bars[i].close) - Number(_bars[i - 1].close);
      if (ch >= 0) avgGain += ch; else avgLoss -= ch;
    }
    avgGain /= RSI_N; avgLoss /= RSI_N;
    for (let i = RSI_N; i < _bars.length; i++) {
      if (i > RSI_N) {
        const ch = Number(_bars[i].close) - Number(_bars[i - 1].close);
        const g = ch > 0 ? ch : 0;
        const l = ch < 0 ? -ch : 0;
        avgGain = (avgGain * (RSI_N - 1) + g) / RSI_N;
        avgLoss = (avgLoss * (RSI_N - 1) + l) / RSI_N;
      }
      const rs  = avgLoss === 0 ? 100 : avgGain / avgLoss;
      const rsi = 100 - (100 / (1 + rs));
      out.push({ ts: _bars[i].ts, rsi });
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

  // ── Timestamp formatters for hover popups ─────────────────────────
  function _fmtBarTs(/** @type {string} */ ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (!Number.isFinite(d.getTime())) return ts.slice(0, 10);
    const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`;
  }
  function _fmtTickTs(/** @type {string} */ ts) {
    if (!ts) return '';
    const d = new Date(ts);
    if (!Number.isFinite(d.getTime())) return ts.slice(11, 19) || ts;
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const ss = String(d.getSeconds()).padStart(2, '0');
    return `${hh}:${mm}:${ss} IST`;
  }

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
    await loadInstruments().catch(() => {});
    // Pin hydration runs in the background — don't block the historical
    // load. Operator can use DEFAULT_PINS immediately; the full list
    // swaps in once the watchlist API responds (typically <300ms).
    _hydratePins();
    await _loadHistorical();
    if (!_isDemo) {
      await _pollStatus();
      _statusTimer = visibleInterval(_pollStatus, 5000);
    }
    if (_isOption) _loadGreeks();
  });

  onDestroy(() => {
    _mounted = false;
    if (_statusTimer) { try { _statusTimer(); } catch (_) { clearInterval(_statusTimer); } }
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
    _chartLoaded = false;
    zoom = null;
    _chartHover = null;
    _bars = [];
    _spotBars = [];
    _greeks = null;
    untrack(() => {
      if (_intradayOn) _intradayOn = false;
    });
    _loadHistorical(true);
    if (_isOption) _loadGreeks();
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

<div class="cw-root">
  <!-- Picker bar — type filter (1st) sets the instrument-kind scope,
       then the combined pinned+search combo box (2nd) lets the
       operator either click a pin OR type to search from a single
       field. -->
  {#if !compact}
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

    <!-- Chart type -->
    <div class="cw-toolbar-select">
      <Select
        options={_CHART_TYPE_OPTS}
        bind:value={_chartType}
        disabled={_histLoading}
        ariaLabel="Chart type" />
    </div>

    <!-- Intraday tick stream — single toggle chip -->
    <button type="button"
      class="cw-range-btn cw-intraday-btn"
      class:active={_intradayOn}
      disabled={_histLoading}
      title={_intradayOn ? 'Intraday tick stream ON — click to turn off' : 'Intraday tick stream OFF — click to turn on'}
      aria-pressed={_intradayOn}
      onclick={() => _intradayOn = !_intradayOn}>
      Intraday
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

    <!-- Volume chip retired (operator: "remove volume chip from
         chart and always keep volume on for chart in the modal
         and page"). _overlays['vol'] is hard-coded ON in the
         state init so the bars render unconditionally. -->

    <!-- Reset zoom action button — trailing edge, only when zoomed -->
    {#if isZoomed}
      <button type="button" class="cw-reset-zoom"
              disabled={_histLoading}
              onclick={_resetZoom}
              title="Reset zoom — show full range">Reset</button>
    {/if}
  </div>

  <!-- Front-month chip — shown when symbol is a bare MCX commodity
       root. Amber roll-warning when expiry is ≤ 3 days away. -->
  {#if _frontMonthInfo}
    <div class="cw-frontmonth-bar">
      <span class="cw-fm-chip" class:cw-fm-rolling={_frontMonthInfo.rolling}
            title="Instrument cache resolved {symbol} → {_frontMonthInfo.contract} (exchange: {_frontMonthInfo.exchange})">
        {#if _frontMonthInfo.rolling}
          Front-month: {_frontMonthInfo.contract} · rolls in {_frontMonthInfo.daysLeft} {_frontMonthInfo.daysLeft === 1 ? 'day' : 'days'}
        {:else}
          Front-month: {_frontMonthInfo.contract} · expiry {_frontMonthInfo.expLabel}
        {/if}
      </span>
    </div>
  {/if}

  <!-- Historical OHLCV chart — fills available height via flex -->
  <div class="cw-chart-container" bind:this={_chartContainerEl}>
    <!-- Floating Overlays panel — TradingView-style, anchored top-right -->
    <div class="cw-overlay-panel" role="region" aria-label="Chart overlays">
      <MultiSelect
        options={_OVERLAY_OPTS}
        bind:value={_overlays}
        bind:open={_overlayOpen}
        disabled={_histLoading}
        placeholder="Overlays"
        ariaLabel="Overlays" />
    </div>

    <!-- HTML hover popup for OHLCV (replaces the SVG rect+text block).
         Pinned state (after a click) keeps the popup anchored at the
         click location and renders a small × close button so the
         operator can dismiss without clicking the chart again. -->
    {#if _chartHover && !_overlayOpen && !pan}
      {@const ch = Number(_chartHover.bar.close) - Number(_chartHover.bar.open)}
      {@const pct = Number(_chartHover.bar.open) ? (ch / Number(_chartHover.bar.open)) * 100 : 0}
      <div class="cw-hover-popup" class:cw-hover-popup-pinned={_chartPinned}
           style="left: {_chartHover.pxLeft}px; top: {_chartHover.pxTop}px;">
        {#if _chartPinned}
          <button type="button" class="cw-hp-close"
                  aria-label="Close pinned popup"
                  title="Close (or click the chart again)"
                  onclick={(e) => { e.stopPropagation(); _chartPinned = false; _chartHover = null; }}>×</button>
        {/if}
        <div class="cw-hp-ts">{_fmtBarTs(_chartHover.bar.ts)}</div>
        <div class="cw-hp-row">
          <span class="cw-hp-label">O</span>
          <span class="cw-hp-val">₹{priceFmt(_chartHover.bar.open)}</span>
          <span class="cw-hp-label">H</span>
          <span class="cw-hp-val">₹{priceFmt(_chartHover.bar.high)}</span>
        </div>
        <div class="cw-hp-row">
          <span class="cw-hp-label">L</span>
          <span class="cw-hp-val">₹{priceFmt(_chartHover.bar.low)}</span>
          <span class="cw-hp-label">C</span>
          <span class="cw-hp-val">₹{priceFmt(_chartHover.bar.close)}</span>
        </div>
        {#if _chartHover.bar.volume}
          <div class="cw-hp-row">
            <span class="cw-hp-label">Vol</span>
            <span class="cw-hp-val">{Number(_chartHover.bar.volume).toLocaleString()}</span>
          </div>
        {/if}
        <div class="cw-hp-row">
          <span class="cw-hp-label">Δ</span>
          <span class="cw-hp-val" class:up={ch >= 0} class:down={ch < 0}>
            {ch >= 0 ? '+' : ''}{ch.toFixed(2)} ({pct.toFixed(2)}%)
          </span>
        </div>
      </div>
    {/if}

    {#if !symbol}
      <div class="cw-state cw-state-hint">Pick a symbol to chart — type 3+ chars in the box above.</div>
    {:else if _histLoading && !_bars.length}
      <div class="cw-state">Loading…</div>
    {:else if _histError && !_bars.length}
      <div class="cw-state cw-err">{_histError}</div>
    {:else if !_bars.length}
      <div class="cw-state">No data available.</div>
    {:else}
      <svg
        viewBox="0 0 {_chartW} {_chartH}"
        preserveAspectRatio="none"
        class="cw-svg"
        class:cw-panning={pan !== null}
        role="img"
        aria-label="Price chart — wheel to zoom, drag to pan, click to pin"
        onwheel={_onWheel}
        onpointerdown={_onPointerDown}
        onpointerup={_onPointerUp}
        onpointermove={_onPointerMove}
        onpointerleave={() => { if (!_chartPinned) _chartHover = null; }}
        onclick={_onChartClick}
      >
        <!-- Y-axis grid + labels -->
        {#each _yTicks as tick}
          <line x1={CPAD_L} x2={_chartW - CPAD_R} y1={tick.y} y2={tick.y}
                stroke="rgba(200,216,240,0.10)" stroke-width="1"/>
          <text x={CPAD_L - 6} y={tick.y + 3} text-anchor="end"
                fill="#c8d8f0" font-size="11" font-weight="600" font-family="monospace">
            ₹{priceFmt(tick.v)}
          </text>
        {/each}

        <!-- X-axis grid + labels — clamped to price area bottom -->
        {#each _xLabels as xl, i}
          {#if i > 0}
            <line x1={xl.x} x2={xl.x} y1={CPAD_T} y2={CPAD_T + _innerH}
                  stroke="rgba(200,216,240,0.07)" stroke-width="1" stroke-dasharray="2 3"/>
          {/if}
          <text x={xl.x} y={CPAD_T + _innerH + 14}
                text-anchor={i === 0 ? 'start' : (i === 4 ? 'end' : 'middle')}
                fill="#c8d8f0" font-size="11" font-weight="600">
            {xl.label}
          </text>
        {/each}

        <!-- X-axis baseline — bottom of price area -->
        <line x1={CPAD_L} x2={_chartW - CPAD_R}
              y1={CPAD_T + _innerH} y2={CPAD_T + _innerH}
              stroke="rgba(255,255,255,0.18)" stroke-width="1"/>

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
                stroke="#7dd3fc" stroke-width="1.2" stroke-dasharray="4 3" stroke-opacity="0.65"/>
        {/if}

        <!-- Bollinger Bands fill (drawn before lines so lines appear on top) -->
        {#if _showBb && _bbPaths.fill}
          <path d={_bbPaths.fill} fill="rgba(125,211,252,0.06)" stroke="none"/>
        {/if}
        {#if _showBb && _bbPaths.upper}
          <path d={_bbPaths.upper} fill="none" stroke="#7dd3fc" stroke-width="1" stroke-dasharray="3 2"/>
          <path d={_bbPaths.lower} fill="none" stroke="#7dd3fc" stroke-width="1" stroke-dasharray="3 2"/>
          <path d={_bbPaths.mid}   fill="none" stroke="#7dd3fc" stroke-width="1"/>
        {/if}

        <!-- Price layer — line / area / candle / plot -->
        {#if _chartType === 'area'}
          <path d={_areaPath} fill="rgba(251,191,36,0.14)" stroke="none"/>
          <path d={_linePath} fill="none" stroke="#fbbf24" stroke-width="1.8"
                stroke-linejoin="round" stroke-linecap="round"/>
        {:else if _chartType === 'candle'}
          {#each _candles as c}
            <line x1={c.x} x2={c.x} y1={c.wickTop} y2={c.wickBot}
                  stroke={c.up ? '#4ade80' : '#f87171'} stroke-width="1"/>
            <rect x={c.x - c.w / 2} y={c.bodyY} width={c.w} height={c.bodyH}
                  fill={c.up ? '#4ade80' : '#f87171'}/>
          {/each}
        {:else if _chartType === 'plot'}
          {#each _plotPoints as p}
            <circle cx={p.x} cy={p.y} r="1.8" fill="#fbbf24"/>
          {/each}
        {:else}
          <path d={_linePath} fill="none" stroke="#fbbf24" stroke-width="1.8"
                stroke-linejoin="round" stroke-linecap="round"/>
        {/if}

        <!-- SMA overlays -->
        {#if _sma20Path}
          <path d={_sma20Path} fill="none" stroke="#7dd3fc" stroke-width="1.4"
                stroke-dasharray="4 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}
        {#if _sma50Path}
          <path d={_sma50Path} fill="none" stroke="#c084fc" stroke-width="1.4"
                stroke-dasharray="6 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}

        <!-- EMA overlays -->
        {#if _ema20Path}
          <path d={_ema20Path} fill="none" stroke="#4ade80" stroke-width="1"
                stroke-dasharray="4 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}
        {#if _ema50Path}
          <path d={_ema50Path} fill="none" stroke="#fb923c" stroke-width="1"
                stroke-dasharray="6 3" stroke-linejoin="round" stroke-linecap="round"/>
        {/if}

        <!-- RSI 14 sub-panel -->
        {#if _showRsi && _rsiSeries.length}
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
            <path d={rsiPath} fill="none" stroke="#fbbf24" stroke-width="1.5" stroke-linecap="round"/>
          {/each}
          <!-- RSI label -->
          <text x={_chartW - CPAD_R - 4} y={rsiTop + 12}
                text-anchor="end" fill="#fbbf24" font-size="10" font-weight="700" font-family="monospace">
            RSI 14
          </text>
        {/if}

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
          <div class="cw-hover-popup" class:cw-hover-popup-pinned={_intradayPinned}
               style="left: {_intradayHover.pxLeft}px; top: {_intradayHover.pxTop}px;">
            {#if _intradayPinned}
              <button type="button" class="cw-hp-close"
                      aria-label="Close pinned popup"
                      title="Close (or click the chart again)"
                      onclick={(e) => { e.stopPropagation(); _intradayPinned = false; _intradayHover = null; }}>×</button>
            {/if}
            <div class="cw-hp-ts">{_fmtTickTs(_intradayHover.tick.ts)}</div>
            <div class="cw-hp-row">
              <span class="cw-hp-label">LTP</span>
              <span class="cw-hp-val">₹{priceFmt(_intradayHover.tick.ltp)}</span>
            </div>
            {#if _intradayHover.tick.bid != null && _intradayHover.tick.ask != null}
              <div class="cw-hp-row">
                <span class="cw-hp-label">Bid</span>
                <span class="cw-hp-val">₹{priceFmt(_intradayHover.tick.bid)}</span>
                <span class="cw-hp-label">Ask</span>
                <span class="cw-hp-val">₹{priceFmt(_intradayHover.tick.ask)}</span>
              </div>
            {/if}
          </div>
        {/if}
        <svg viewBox="0 0 {W2} {H2}" preserveAspectRatio="none"
             class="cw-intraday-svg" role="img" aria-label="Intraday tick chart — click to pin"
             onpointermove={_onIntradayPointerMove}
             onpointerleave={() => { if (!_intradayPinned) _intradayHover = null; }}
             onclick={_onIntradayClick}>
          {#each _t2YTicks as yt}
            <line x1={P2L} x2={W2 - P2R} y1={yt.y} y2={yt.y}
                  stroke="rgba(200,216,240,0.10)" stroke-width="1"/>
            <text x={P2L - 4} y={yt.y + 3} text-anchor="end"
                  fill="#7e97b8" font-size="10" font-family="monospace">
              {priceFmt(yt.v)}
            </text>
          {/each}
          <path d={_t2path} fill="none"
                stroke={_tickKind === 'underlying' ? '#7dd3fc' : '#fbbf24'}
                stroke-width="1.4"/>
          {#each _events as ev}
            {#if ev.ts >= _ticks[0].ts && ev.ts <= _ticks[_ticks.length - 1].ts}
              {@const cx = _t2xOf(ev.ts)}
              {@const cy = _t2yOf(ev.price ?? _ticks[_ticks.length - 1].ltp)}
              {@const evColor = ev.kind === 'filled' ? '#4ade80' : ev.kind === 'unfilled' ? '#f87171' : '#fbbf24'}
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
      <span class="cw-info-root">({symbol})</span>
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

  <!-- Greeks strip — always rendered for options; reserved height for non-options
       prevents layout jump when operator switches between option ↔ equity. -->
  {#if !compact}
    <div class="cw-greeks-strip" class:cw-greeks-empty={!_isOption}>
      {#if _isOption}
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
      {:else if symbol}
        <span class="cw-greeks-placeholder">Equity — no Greeks</span>
      {/if}
    </div>
  {/if}
</div>

<style>
  .cw-root {
    display: flex;
    flex-direction: column;
    width: 100%;
    height: 100%;
    min-height: 0;
    box-sizing: border-box;
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: 6px;
    box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35), 0 0 0 1px rgba(251, 191, 36, 0.05) inset;
    overflow: hidden;
  }

  /* ── Picker bar ─────────────────────────────────────────── */
  .cw-picker {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem 0.4rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    flex-wrap: wrap;
    flex-shrink: 0;
  }

  /* Chart controls row (Type · Intraday · 1D/1W/1M/3M/6M/1Y · Reset).
     Same layout family as .cw-picker but the row above it carries the
     symbol search; this one always renders, hosted outside the
     compact-mode gate so the date-range pills are present in every
     embed surface. */
  .cw-controls {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.75rem;
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
    padding: 0.18rem 0.55rem;
    background: transparent;
    border: 0;
    border-right: 1px solid rgba(255, 255, 255, 0.06);
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
  }
  .cw-range-btn:last-child { border-right: 0; }
  .cw-range-btn:hover { background: rgba(125, 211, 252, 0.10); color: var(--algo-slate); }
  .cw-range-btn.active {
    background: rgba(251, 191, 36, 0.18);
    color: #fbbf24;
    font-weight: 800;
  }
  /* Standalone intraday chip — same shape as a range pill, but
     rounded on both ends (not in the segmented .cw-range-group). */
  .cw-intraday-btn {
    border: 1px solid rgba(125, 211, 252, 0.32);
    border-radius: 4px;
    flex-shrink: 0;
  }
  .cw-intraday-btn.active {
    background: rgba(125, 211, 252, 0.18);
    border-color: var(--algo-sky-border);
    color: #7dd3fc;
  }

  /* ── Toolbar Select wrappers ─────────────────────────────── */
  .cw-toolbar-select {
    flex-shrink: 0;
    min-width: 6rem;
    max-width: 9rem;
  }

  /* ── Floating Overlays panel (TradingView-style, top-right of chart) */
  .cw-overlay-panel {
    position: absolute;
    top: 0.5rem;
    right: 0.5rem;
    z-index: 5;
    background: rgba(29, 42, 68, 0.82);
    border: 1px solid rgba(125, 211, 252, 0.32);
    border-radius: 3px;
    padding: 2px;
    backdrop-filter: blur(4px);
    pointer-events: auto;
    min-width: 7.5rem;
  }
  /* Compact trigger so it reads as a chart control, not a form field */
  .cw-overlay-panel :global(.rbq-multi-trigger) {
    background: transparent;
    border: 0;
    color: var(--algo-slate);
    font-size: 0.6rem;
    padding: 0.18rem 0.4rem;
    min-height: 1.2rem;
  }
  .cw-overlay-panel :global(.rbq-multi-trigger:hover:not(:disabled)) {
    background: rgba(125, 211, 252, 0.10);
    border: 0;
  }
  .cw-overlay-panel :global(.rbq-multi-trigger:focus) {
    outline: none;
    border: 0;
  }
  /* Dropdown panel aligns to the right edge of the floating panel */
  .cw-overlay-panel :global(.rbq-multi-panel) {
    left: auto;
    right: 0;
    min-width: 9rem;
  }

  /* ── Hover popup (shared by historical and intraday) ─────── */
  .cw-hover-popup {
    position: absolute;
    pointer-events: none;
    background: rgba(15, 25, 45, 0.95);
    border: 1px solid rgba(251, 191, 36, 0.45);
    border-radius: 4px;
    padding: 0.3rem 0.45rem;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: var(--algo-slate);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
    z-index: 10;
    min-width: 9rem;
    max-width: 13rem;
  }
  /* Pinned variant — brighter cyan border so the operator sees the
     popup is locked in place, and `pointer-events: auto` so the ×
     close button is clickable. */
  .cw-hover-popup-pinned {
    pointer-events: auto;
    border-color: rgba(125, 211, 252, 0.85);
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.6), 0 0 0 1px rgba(125, 211, 252, 0.20);
  }
  .cw-hp-close {
    position: absolute;
    top: 0.15rem;
    right: 0.2rem;
    width: 0.95rem;
    height: 0.95rem;
    padding: 0;
    background: none;
    border: 0;
    color: rgba(248, 113, 113, 0.85);
    font-family: monospace;
    font-size: 0.75rem;
    line-height: 1;
    cursor: pointer;
    border-radius: 2px;
  }
  .cw-hp-close:hover { color: #f87171; background: rgba(248, 113, 113, 0.12); }
  .cw-hp-ts {
    color: #fbbf24;
    font-weight: 800;
    font-size: 0.58rem;
    margin-bottom: 0.2rem;
    padding-bottom: 0.18rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    white-space: nowrap;
  }
  .cw-hp-row {
    display: flex;
    gap: 0.35rem;
    align-items: baseline;
    margin-top: 0.12rem;
  }
  .cw-hp-label {
    color: var(--algo-muted);
    font-weight: 700;
    min-width: 1.2rem;
    flex-shrink: 0;
  }
  .cw-hp-val {
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
  }
  .cw-hp-val.up   { color: #4ade80; }
  .cw-hp-val.down { color: #f87171; }

  .cw-reset-zoom {
    font-family: monospace;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 2px 8px;
    border-radius: 3px;
    border: 1px solid rgba(251,191,36,0.50);
    background: rgba(251,191,36,0.12);
    color: #fbbf24;
    cursor: pointer;
    margin-left: auto;
  }
  .cw-reset-zoom:hover { background: rgba(251,191,36,0.22); }

  /* ── Chart container + SVG ───────────────────────────────── */
  /* flex:1 makes this absorb all available vertical space */
  .cw-chart-container {
    flex: 1 1 0;
    min-height: 200px;  /* floor so chart is never invisible */
    width: 100%;
    position: relative;
    overflow: hidden;
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
    font-size: 0.65rem;
    font-family: monospace;
    padding: 0 1rem;
    text-align: center;
  }
  .cw-state-sm { min-height: 80px; height: auto; }
  .cw-err { color: #f87171; }
  .cw-err-text { color: #f87171; font-size: 0.55rem; font-family: monospace; }

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
    font-size: 0.6rem;
    color: var(--algo-muted);
    font-family: monospace;
    flex-shrink: 0;
  }
  .cw-intraday-mode {
    font-weight: 700;
    font-size: 0.5rem;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid currentColor;
  }
  .cw-mode-live  { color: #4ade80; }
  .cw-mode-sim   { color: #fbbf24; }
  .cw-mode-paper { color: #7dd3fc; }
  .cw-intraday-source {
    font-weight: 700;
    font-size: 0.5rem;
    padding: 1px 5px;
    border-radius: 2px;
    border: 1px solid var(--algo-amber-border);
    color: #fbbf24;
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
    font-size: 0.6rem;
    flex-wrap: wrap;
    flex-shrink: 0;
  }
  .cw-info-sym   { color: #7dd3fc; font-weight: 700; }
  .cw-info-close { color: var(--algo-slate); font-variant-numeric: tabular-nums; }
  .cw-info-pct   { font-variant-numeric: tabular-nums; font-weight: 700; }
  .cw-pos { color: #4ade80; }
  .cw-neg { color: #f87171; }
  .cw-info-meta { color: var(--algo-muted); }
  .cw-meta-text { color: var(--algo-muted); font-size: 0.55rem; font-family: monospace; }
  .cw-info-root {
    color: #4a5a7a;
    font-size: 0.55rem;
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
    padding: 0.2rem 0.75rem;
    flex-shrink: 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .cw-fm-chip {
    display: inline-flex;
    align-items: center;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
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
    color: #fbbf24;
    background: var(--algo-amber-bg);
    border-color: rgba(251, 191, 36, 0.42);
  }
  @media (max-width: 600px) {
    .cw-frontmonth-bar { padding: 0.2rem 0.5rem; }
    .cw-fm-chip { font-size: 0.55rem; }
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
    min-height: 2rem;  /* reserve height even when empty so layout doesn't jump */
  }
  /* Non-options placeholder — subtle, doesn't look like an error */
  .cw-greeks-placeholder {
    font-family: monospace;
    font-size: 0.55rem;
    color: #4a5a7a;
    font-style: italic;
  }
  .cw-greeks-label {
    font-family: monospace;
    font-size: 0.55rem;
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
    font-size: 0.6rem;
    font-weight: 700;
    color: var(--algo-muted);
  }
  .cw-gk-val {
    font-family: monospace;
    font-size: 0.65rem;
    font-variant-numeric: tabular-nums;
    color: var(--algo-slate);
  }
  .cw-gk-amber { color: #fbbf24; }
  .cw-gk-sky   { color: #7dd3fc; }

  @media (max-width: 600px) {
    .cw-picker        { gap: 0.35rem; padding: 0.4rem 0.5rem; }
    .cw-info-strip    { padding: 0.25rem 0.5rem; gap: 0.4rem; }
    .cw-greeks-strip  { padding: 0.3rem 0.5rem; gap: 0.5rem; }
    .cw-sym-input     { min-width: 7rem; }
    .cw-intraday-section { height: 30vh; }
    /* Let toolbar dropdowns go full-flex on phone so they wrap cleanly */
    .cw-toolbar-select { min-width: 0; flex: 1 1 auto; }
  }
</style>
