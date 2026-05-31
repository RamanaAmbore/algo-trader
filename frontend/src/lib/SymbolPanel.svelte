<script>
  // SymbolPanel — unified symbol-keyed modal. Replaces three older
  // surfaces: OrderEntryShell (tabbed order entry), SymbolChartModal
  // (standalone chart popup), and SymbolActions (the ⋯ row-menu).
  //
  // Four tabs:
  //   chart    — 30 d historical OHLCV line (lifted from
  //              SymbolChartModal). Default when invoked from a row
  //              click — operator's intent is "what does this look
  //              like?", not "place a trade".
  //   ticket   — single-leg OrderTicket. Default when an explicit
  //              "trade this" intent is signalled (chain +CE / +PE
  //              click, Trade row action).
  //   chain    — option-chain basket builder (OptionChainTab).
  //              Disabled for cash-equity symbols.
  //   command  — terminal-style command input (CommandLineTab).
  //              Power-user surface; lands last in the tab strip.
  //
  // Industry shape — matches IB TWS "Symbol Detail", ThinkOrSwim
  // "Active Trader", and TradingView "Order Form" patterns: one
  // symbol-keyed panel, tabs for the different actions on that
  // symbol, no hidden context menus.

  import { onMount, onDestroy } from 'svelte';
  import { placeTicketOrder, fetchLiveStatus, fetchOrders, fetchAlgoOrdersRecent, fetchOptionsHistorical } from '$lib/api';
  import { logTime } from '$lib/stores';
  import { priceFmt } from '$lib/format';
  import OrderTicket      from '$lib/order/OrderTicket.svelte';
  import CommandLineTab   from '$lib/order/CommandLineTab.svelte';
  import OptionChainTab   from '$lib/order/OptionChainTab.svelte';
  import UnifiedLog       from '$lib/UnifiedLog.svelte';

  /** @type {{
   *   defaultTab?:     'chart' | 'command' | 'ticket' | 'chain',
   *   symbol?:         string,
   *   exchange?:       string,
   *   instrument?:     { kind?: string, exchange?: string },
   *   side?:           'BUY' | 'SELL',
   *   action?:         'open' | 'close' | 'modify' | 'repeat' | 'cancel',
   *   qty?:            number,
   *   product?:        'CNC' | 'MIS' | 'NRML',
   *   orderType?:      'MARKET' | 'LIMIT' | 'SL' | 'SL-M',
   *   variety?:        'regular' | 'co' | 'bo' | 'amo' | 'iceberg',
   *   price?:          number,
   *   trigger?:        number,
   *   lotSize?:        number,
   *   accounts?:       string[],
   *   account?:        string,
   *   orderId?:        string,
   *   defaultMode?:    'draft' | 'paper' | 'live',
   *   availableModes?: Array<'draft' | 'paper' | 'live'>,
   *   currentQty?:     number,
   *   onSubmit:        (payload: any) => void | Promise<void>,
   *   onClose:         () => void,
   *   onAddToBasket?:  ((payload: any) => void) | null,
   *   onAddToWatchlist?: ((sym: string, exch?: string) => void | Promise<void>) | null,
   *   inline?:         boolean,
   *   headerless?:     boolean,
   *   onSymbolChange?: ((sym: string) => void) | null,
   * }} */
  let {
    defaultTab     = /** @type {'chart'|'command'|'ticket'|'chain'} */ ('ticket'),
    symbol         = '',
    exchange       = '',
    instrument     = /** @type {{kind?:string,exchange?:string}} */ ({}),
    side           = /** @type {'BUY'|'SELL'} */ ('BUY'),
    action         = /** @type {'open'|'close'|'modify'|'repeat'|'cancel'} */ ('open'),
    qty            = 0,
    product        = /** @type {'CNC'|'MIS'|'NRML'|undefined} */ (undefined),
    orderType      = /** @type {'MARKET'|'LIMIT'|'SL'|'SL-M'} */ ('LIMIT'),
    variety        = /** @type {'regular'|'co'|'bo'|'amo'|'iceberg'} */ ('regular'),
    price          = /** @type {number|undefined} */ (undefined),
    trigger        = /** @type {number|undefined} */ (undefined),
    lotSize        = 0,
    accounts       = /** @type {string[]} */ ([]),
    account        = '',
    orderId        = '',
    defaultMode    = /** @type {'draft'|'paper'|'live'} */ ('live'),
    availableModes = /** @type {Array<'draft'|'paper'|'live'>} */ (['draft', 'live']),
    currentQty     = 0,
    onSubmit,
    onClose,
    onAddToBasket  = /** @type {((payload:any)=>void)|null} */ (null),
    // Optional — when provided, renders a `+W` (add to watchlist)
    // affordance in the panel header. Callers wire it conditionally:
    //   • options chain pick → wire it so operator can track a
    //     not-yet-owned strike
    //   • MarketPulse "add symbol" picker → same
    //   • Performance row click → omit (positions/holdings are
    //     already auto-merged into MarketPulse's watchlist)
    // Receives (symbol, exchange); returning a promise lets the
    // panel show a brief success/error flash without blocking.
    onAddToWatchlist = /** @type {((sym:string,exch?:string)=>void|Promise<void>)|null} */ (null),
    // When true, render flat inline (no overlay, no fixed positioning,
    // no close button). Used by /console which hosts the shell as the
    // page's primary content rather than as a popup over another page.
    inline         = false,
    // When true, omit the shell's own header strip (symbol picker +
    // exchange chip + close + watchlist add). The host page renders
    // its own symbol picker — typically inside a bucket-card header
    // — and the operator picks symbol there, so duplicating it inside
    // the shell would be redundant. Used by /orders. The shell still
    // tracks `_localSymbol` and propagates to every tab; the host
    // just needs to two-way bind it via the `symbol` prop and an
    // `onSymbolChange` callback if it wants to mirror picks back.
    headerless     = false,
    // Fired whenever the operator picks a symbol inside the shell
    // (currently only via the header picker, but reserved for future
    // tab-level picks). Hosts that pass `headerless` typically wire
    // this to their own state so a chain-tab pick still lands in the
    // header chip.
    onSymbolChange = /** @type {((sym: string) => void) | null} */ (null),
  } = $props();

  // Local mutable copy of the symbol prop — operator can edit it from
  // the top search input so every tab (Chart / Ticket / Chain /
  // Command) re-renders against the new symbol. Synced from the prop
  // via $effect so an external pick (chain row + CE click, dashboard
  // row click, MarketPulse symbol pick) still updates the shell.
  let _localSymbol = $state(String(symbol || '').toUpperCase());
  $effect(() => {
    const next = String(symbol || '').toUpperCase();
    if (next && next !== _localSymbol) _localSymbol = next;
  });

  // Symbol search dropdown state.
  let _symbolQuery = $state('');
  let _symbolSuggestions = $state(/** @type {any[]} */ ([]));
  let _symbolOpen = $state(false);
  let _symbolDebounce;
  function _onSymbolInput(/** @type {string} */ v) {
    _symbolQuery = v;
    _symbolOpen = true;
    if (_symbolDebounce) clearTimeout(_symbolDebounce);
    _symbolDebounce = setTimeout(async () => {
      try {
        const { searchByPrefix } = await import('$lib/data/instruments');
        _symbolSuggestions = await searchByPrefix(v, 12);
      } catch (_) { _symbolSuggestions = []; }
    }, 150);
  }
  function _pickSymbol(/** @type {any} */ inst) {
    _localSymbol = String(inst?.sym || inst?.tradingsymbol || _symbolQuery).toUpperCase();
    _symbolQuery = '';
    _symbolOpen = false;
    _symbolSuggestions = [];
    onSymbolChange?.(_localSymbol);
  }

  // Determine whether Chain tab applies.
  // Equity = no FUT/CE/PE suffix AND (kind=equity OR exchange is cash-equity).
  const _sym = $derived(_localSymbol);
  const _isDerivative = $derived(/(?:CE|PE|FUT)$/.test(_sym));
  const _isEquityExch = $derived(
    (!_isDerivative) &&
    (
      (instrument?.kind === 'equity') ||
      (['NSE', 'BSE'].includes(String(instrument?.exchange || exchange || '').toUpperCase()) && !_isDerivative)
    )
  );
  const chainDisabled = $derived(_isEquityExch && !_isDerivative);

  // Resolve initial tab — fall through chain → ticket when equity.
  function _resolveInitialTab() {
    const req = defaultTab || 'ticket';
    if (req === 'chain' && chainDisabled) return 'ticket';
    return req;
  }
  let _activeTab = $state(/** @type {'chart'|'command'|'ticket'|'chain'} */ (_resolveInitialTab()));

  // ── Chart tab state ───────────────────────────────────────────────
  // Mirrors the body of the retired SymbolChartModal — same fetch +
  // SVG plot, just lifted into a tab. Lazy load: only triggers when
  // the Chart tab activates so opening the panel on Ticket doesn't
  // hit /api/options/historical unnecessarily.
  /** @type {Array<{ts: string, open: number, high: number, low: number, close: number, volume: number}>} */
  let _chartBars = $state([]);
  let _chartLoading = $state(false);
  let _chartError = $state('');
  let _chartLoaded = $state(false);   // sentinel — only fetch once per panel open
  // Range selector — 1W / 1M / 3M. Default 1 M matches the retired
  // SymbolChartModal's behaviour.
  let _chartDays = $state(/** @type {number} */ (30));
  // Chart-type toggle — line (default) / area / candle. Computed
  // entirely client-side from the same OHLCV bar stream; no extra
  // /api/options/historical fetch.
  let _chartType = $state(/** @type {'line'|'area'|'candle'} */ ('line'));
  // Indicator toggles — SMA(20), SMA(50), volume bars. Each
  // computed lazily from `_chartBars` via $derived below.
  let _showSma20 = $state(false);
  let _showSma50 = $state(false);
  // Fullscreen mode — modal expands to fill the viewport so the chart
  // gets max screen real estate for inspection. The rest of the panel
  // (ticket / chain / command tabs) stays accessible — only the modal
  // size changes; chart fills its enlarged container naturally.
  let _fullscreen = $state(false);
  function _toggleFullscreen() { _fullscreen = !_fullscreen; }
  let _showVol   = $state(false);
  // Hover crosshair (lifted from the retired SymbolChartModal).
  /** @type {{x:number,y:number,bar:any}|null} */
  let _chartHover = $state(null);

  // ── Watchlist add — flash feedback ───────────────────────────────
  // Mirrors the toast pattern from the retired SymbolActions
  // component. State lives here (not in the parent) so callers don't
  // have to wire a separate toast queue per page.
  /** @type {{msg: string, ok: boolean} | null} */
  let _wlToast = $state(null);
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _wlToastTimer = null;
  let _wlInFlight = $state(false);

  function _wlFlash(/** @type {string} */ msg, /** @type {boolean} */ ok = true) {
    _wlToast = { msg, ok };
    if (_wlToastTimer) clearTimeout(_wlToastTimer);
    _wlToastTimer = setTimeout(() => { _wlToast = null; }, 1600);
  }

  async function _addToWatchlist() {
    if (!onAddToWatchlist || _wlInFlight || !_localSymbol) return;
    _wlInFlight = true;
    try {
      await onAddToWatchlist(_localSymbol, exchange);
      _wlFlash('✓ added to watchlist', true);
    } catch (e) {
      _wlFlash(`Watchlist: ${/** @type {any} */ (e)?.message || 'failed'}`, false);
    } finally {
      _wlInFlight = false;
    }
  }

  async function _loadChart(/** @type {boolean} */ force = false) {
    if (!_localSymbol) return;
    if (!force && _chartLoaded) return;
    _chartLoading = true; _chartError = '';
    try {
      const r = await fetchOptionsHistorical(_localSymbol, { days: _chartDays, exchange });
      _chartBars = Array.isArray(r?.bars) ? r.bars : [];
      if (!_chartBars.length) _chartError = 'No bars available — broker may not list this contract.';
      _chartLoaded = true;
    } catch (e) {
      _chartError = /** @type {any} */ (e)?.message || String(e);
      _chartBars = [];
    } finally {
      _chartLoading = false;
    }
  }

  // Auto-load when the Chart tab becomes active OR the range changes.
  // `_chartLoaded` resets to false on range switch so the force path
  // refetches; subsequent activations without a range change skip.
  // Also resets when the operator picks a NEW symbol from the shell-
  // level search so the chart re-fetches against the right symbol.
  $effect(() => {
    if (_activeTab === 'chart') _loadChart();
  });
  $effect(() => {
    // Symbol change → invalidate cached chart so the next tab activation
    // (or the current one) re-fetches. Reading _localSymbol here is what
    // triggers the rerun.
    if (_localSymbol) { _chartLoaded = false; if (_activeTab === 'chart') _loadChart(true); }
  });

  function _setChartRange(/** @type {number} */ days) {
    if (days === _chartDays) return;
    _chartDays = days;
    _chartLoaded = false;
    _chartHover = null;
    _loadChart(true);
  }

  // ── Chart geometry (mirrors SymbolChartModal) ─────────────────────
  const CW = 720;
  const CH = 360;
  const CPAD_L = 56;
  const CPAD_R = 16;
  const CPAD_T = 16;
  const CPAD_B = 30;
  const _chartInnerW = CW - CPAD_L - CPAD_R;
  const _chartInnerH = CH - CPAD_T - CPAD_B;

  const _chartXs = $derived(_chartBars.map((b) => Date.parse(b.ts)).filter(Number.isFinite));
  const _chartXDomain = $derived(_chartXs.length
    ? { lo: Math.min(..._chartXs), hi: Math.max(..._chartXs) }
    : null);
  const _chartYDomain = $derived.by(() => {
    if (!_chartBars.length) return { lo: 0, hi: 1 };
    let lo = Infinity, hi = -Infinity;
    for (const b of _chartBars) {
      const l = Number(b.low), h = Number(b.high);
      if (Number.isFinite(l)) lo = Math.min(lo, l);
      if (Number.isFinite(h)) hi = Math.max(hi, h);
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { lo: 0, hi: 1 };
    const pad = (hi - lo) * 0.06 || 1;
    return { lo: lo - pad, hi: hi + pad };
  });
  const _chartXOf = (/** @type {number} */ ts) => {
    if (!_chartXDomain) return CPAD_L;
    if (_chartXDomain.hi === _chartXDomain.lo) return CPAD_L + _chartInnerW / 2;
    return CPAD_L + ((ts - _chartXDomain.lo) / (_chartXDomain.hi - _chartXDomain.lo)) * _chartInnerW;
  };
  const _chartYOf = (/** @type {number} */ v) =>
    CPAD_T + ((_chartYDomain.hi - v) / (_chartYDomain.hi - _chartYDomain.lo)) * _chartInnerH;
  const _chartLinePath = $derived.by(() => {
    if (!_chartBars.length || !_chartXDomain) return '';
    let d = '';
    for (let i = 0; i < _chartBars.length; i++) {
      const t = Date.parse(_chartBars[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = _chartXOf(t);
      const y = _chartYOf(Number(_chartBars[i].close));
      d += (i === 0 ? `M${x.toFixed(2)},${y.toFixed(2)}`
                    : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  });

  // Area path — same close-price polyline as the line chart, closed
  // back to the bottom-of-plot baseline so the SVG fill paints the
  // triangle below the curve. Re-uses _chartLinePath's geometry.
  const _chartAreaPath = $derived.by(() => {
    if (!_chartBars.length || !_chartXDomain) return '';
    const last = _chartBars[_chartBars.length - 1];
    const lastT = Date.parse(last.ts);
    if (!Number.isFinite(lastT)) return '';
    const baselineY = (CH - CPAD_B).toFixed(2);
    return `${_chartLinePath} L${_chartXOf(lastT).toFixed(2)},${baselineY} L${_chartXOf(Date.parse(_chartBars[0].ts)).toFixed(2)},${baselineY} Z`;
  });

  // Candle geometry — one rect (body) + one line (wick) per bar.
  // Width auto-sized to ~60 % of the per-bar slot so candles don't
  // touch but don't get lost either.
  const _chartCandles = $derived.by(() => {
    if (!_chartBars.length || !_chartXDomain) return [];
    const n = _chartBars.length;
    const slot = n > 1 ? _chartInnerW / (n - 1) : _chartInnerW;
    const w = Math.max(2, Math.min(12, slot * 0.6));
    /** @type {Array<{x:number,bodyY:number,bodyH:number,w:number,wickTop:number,wickBot:number,up:boolean}>} */
    const out = [];
    for (const b of _chartBars) {
      const t = Date.parse(b.ts);
      if (!Number.isFinite(t)) continue;
      const x = _chartXOf(t);
      const o = Number(b.open), c = Number(b.close);
      const hi = Number(b.high), lo = Number(b.low);
      const up = c >= o;
      const yTop = _chartYOf(Math.max(o, c));
      const yBot = _chartYOf(Math.min(o, c));
      out.push({
        x, bodyY: yTop, bodyH: Math.max(1, yBot - yTop), w,
        wickTop: _chartYOf(hi), wickBot: _chartYOf(lo), up,
      });
    }
    return out;
  });

  /** Simple moving average path for the given window (e.g. 20, 50).
   * Returns '' if fewer than `window` bars are available — silently
   * hides the line instead of plotting a partial curve. */
  function _smaPath(/** @type {number} */ window) {
    if (!_chartBars.length || !_chartXDomain || _chartBars.length < window) return '';
    let d = '';
    let started = false;
    for (let i = window - 1; i < _chartBars.length; i++) {
      let sum = 0;
      for (let j = i - window + 1; j <= i; j++) sum += Number(_chartBars[j].close);
      const avg = sum / window;
      const t = Date.parse(_chartBars[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = _chartXOf(t);
      const y = _chartYOf(avg);
      d += (!started ? `M${x.toFixed(2)},${y.toFixed(2)}`
                     : ` L${x.toFixed(2)},${y.toFixed(2)}`);
      started = true;
    }
    return d;
  }
  const _sma20Path = $derived(_showSma20 ? _smaPath(20) : '');
  const _sma50Path = $derived(_showSma50 ? _smaPath(50) : '');

  // Volume bars — bottom 48 px band of the chart when enabled.
  // Auto-scales to the period max so the tallest bar fills the band.
  const VOL_H = 48;
  const _chartVol = $derived.by(() => {
    if (!_chartBars.length || !_chartXDomain || !_showVol) return [];
    let vMax = 0;
    for (const b of _chartBars) {
      const v = Number(b.volume || 0);
      if (v > vMax) vMax = v;
    }
    if (vMax <= 0) return [];
    const n = _chartBars.length;
    const slot = n > 1 ? _chartInnerW / (n - 1) : _chartInnerW;
    const w = Math.max(2, Math.min(10, slot * 0.55));
    const baseline = CH - CPAD_B;
    const top = baseline - VOL_H;
    /** @type {Array<{x:number,y:number,h:number,w:number,up:boolean}>} */
    const out = [];
    for (let i = 0; i < n; i++) {
      const b = _chartBars[i];
      const t = Date.parse(b.ts);
      if (!Number.isFinite(t)) continue;
      const v = Number(b.volume || 0);
      const h = (v / vMax) * (VOL_H - 4);
      const x = _chartXOf(t) - w / 2;
      const up = Number(b.close) >= Number(b.open);
      out.push({ x, y: baseline - h, h, w, up });
    }
    return out;
  });
  const _chartYTicks = $derived.by(() => {
    if (!_chartBars.length) return [];
    const out = [];
    const step = (_chartYDomain.hi - _chartYDomain.lo) / 4;
    for (let i = 0; i <= 4; i++) out.push(_chartYDomain.lo + i * step);
    return out;
  });
  const _chartXLabels = $derived.by(() => {
    if (!_chartXDomain) return [];
    const out = [];
    for (let i = 0; i < 5; i++) {
      const t = _chartXDomain.lo + ((_chartXDomain.hi - _chartXDomain.lo) * i) / 4;
      const d = new Date(t);
      const mm = String(d.getMonth() + 1).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      out.push({ x: _chartXOf(t), label: `${dd}/${mm}` });
    }
    return out;
  });
  const _chartFirstClose = $derived(_chartBars[0]?.close);
  const _chartLastClose = $derived(_chartBars[_chartBars.length - 1]?.close);
  const _chartPct = $derived(
    (_chartFirstClose && _chartLastClose)
      ? ((_chartLastClose - _chartFirstClose) / _chartFirstClose) * 100
      : null,
  );

  // ── Bottom-panel tab state ───────────────────────────────────────────
  let _bottomTab = $state(/** @type {'log'|'orders'} */ ('log'));

  // ── Basket state (shared across all tabs) ───────────────────────────
  // When basketMode is active (Chain tab is selected), submissions from
  // Command and Ticket tabs accumulate here instead of firing immediately.
  const basketMode = $derived(_activeTab === 'chain');
  /** @type {any[]} */
  let basketLegs   = $state([]);
  /** @type {string} */ let basketResultMsg = $state('');
  let basketSubmitting = $state(false);

  function addToBasket(/** @type {any} */ leg) {
    basketLegs = [...basketLegs, leg];
  }
  function removeBasketLeg(/** @type {number} */ i) {
    basketLegs = basketLegs.filter((_, k) => k !== i);
  }
  function clearBasket() { basketLegs = []; basketResultMsg = ''; }

  // ── Shared account state ─────────────────────────────────────────
  // Single source of truth for the routable account across all three
  // tabs (command / ticket / chain). Earlier each tab maintained its
  // own _account so picking it in one tab didn't sync to the others
  // — operators could submit a Ticket-tab order on one account while
  // the Chain-tab basket was staged for another. Lifted here as $state
  // initialised from the `account` prop; each tab receives it as
  // `account` and calls `onAccountChange` when its picker changes.
  let _sharedAccount = $state(account || '');
  // Re-sync from the prop when the caller updates it externally (e.g.
  // when /admin/options opens the modal with a new default after the
  // operator picks a different position).
  $effect(() => { _sharedAccount = account || _sharedAccount; });
  function _onAccountChange(/** @type {string} */ a) {
    _sharedAccount = a;
  }

  // Single-pass leg update — used by chain merge (sym+side dedupe) and
  // +/- steppers. Maps in place so rapid clicks accumulate cleanly
  // instead of relying on the remove+re-add pattern which can drop
  // updates if the prop hasn't propagated back to the child between
  // calls.
  function updateLegByKey(/** @type {string} */ key,
                         /** @type {(leg:any) => any} */ updater) {
    basketLegs = basketLegs.map(b => b.key === key ? updater(b) : b);
  }

  /** Submit every leg in the shell basket via placeTicketOrder. */
  async function submitBasket() {
    if (basketSubmitting || !basketLegs.length) return;
    basketSubmitting = true; basketResultMsg = '';
    let ok = 0; /** @type {string[]} */ const fails = [];
    let basketMode2 = 'paper';
    try {
      const live = await fetchLiveStatus();
      if (live && live.paper_trading_mode === false && live.branch === 'main') basketMode2 = 'live';
    } catch { /* safe default */ }

    // Track failed-leg indices explicitly. The earlier code pruned
    // legs by `i >= ok` which assumed all failures were at the END of
    // the array — but the loop continues on error, so `ok` is a
    // counter of successes, not a positional index. A failing leg in
    // the MIDDLE would survive while a passing leg at the end got
    // dropped. Bug now fixed with an explicit failedIdx set.
    /** @type {Set<number>} */
    const failedIdx = new Set();
    for (let i = 0; i < basketLegs.length; i++) {
      const leg = basketLegs[i];
      try {
        const hasLimit = Number(leg.limit) > 0;
        if (!hasLimit) {
          // Every order is LIMIT + chase[low] by default — silently
          // downgrading to MARKET when the quote hadn't arrived yet
          // bypassed the operator's intent and made the chase engine
          // a no-op (MARKET fills immediately). Force the operator to
          // either wait for the quote or override per-leg manually.
          fails.push(`${leg.side} ${leg.sym}: no quote yet — re-open the chain so the bid/ask price loads, then submit again.`);
          failedIdx.add(i);
          continue;
        }
        const brokerResp = await placeTicketOrder({
          mode:             basketMode2,
          side:             leg.side,
          tradingsymbol:    leg.sym,
          exchange:         leg.exchange || 'NFO',
          quantity:         (leg.lots || 1) * (leg.lotSize || 1),
          product:          leg.product || 'NRML',
          order_type:       'LIMIT',
          variety:          'regular',
          price:            Number(leg.limit),
          account:          leg.account || account || '',
          chase:            true,
          chase_aggressiveness: leg.chaseAgg || 'low',
        });
        ok++;
        // Surface the full ticket-shape payload to the parent (mode +
        // broker_response are what /admin/options needs to push a
        // completion toast and link the fill broadcast back to the
        // right order). Earlier we only spread `leg` which had no
        // mode/broker_response, so the parent's toast logic silently
        // fell through and the operator saw nothing land.
        onSubmit?.({
          mode:           basketMode2,
          side:           leg.side,
          symbol:         leg.sym,
          quantity:       (leg.lots || 1) * (leg.lotSize || 1),
          price:          Number(leg.limit),
          account:        leg.account || account || '',
          broker_response: brokerResp,
          _basketLeg:     true,
          ...leg,
        });
      } catch (e) {
        fails.push(`${leg.side} ${leg.sym}: ${/** @type {any} */ (e)?.message || 'failed'}`);
        failedIdx.add(i);
      }
    }
    basketSubmitting = false;
    const total = basketLegs.length;
    if (!fails.length) {
      basketResultMsg = `${ok}/${total} placed`;
      basketLegs = [];
      setTimeout(onClose, 1500);
    } else if (ok > 0) {
      basketResultMsg = `${ok}/${total} placed — ${fails.length} rejected: ${fails[0]}`;
      // Keep ONLY the legs that failed so the operator can retry just
      // those (after fixing the quote / limit / etc.).
      basketLegs = basketLegs.filter((_, i) => failedIdx.has(i));
    } else {
      basketResultMsg = `Failed: ${fails[0]}`;
    }
  }

  // ── Command tab pre-fill ─────────────────────────────────────────────
  // Ticket pre-fill state — when the Command tab parses an order command
  // it routes back here to switch to the Ticket tab pre-filled (or adds
  // to basket when basketMode is active).
  /** @type {any} */
  let _cmdOrderProps = $state(null);

  function handleParsedOrder(/** @type {any} */ props) {
    _cmdOrderProps = props;
    _activeTab = 'ticket';
  }

  /** Called by CommandLineTab when basketMode is on. */
  function handleCmdAddToBasket(/** @type {any} */ leg) {
    addToBasket(leg);
  }

  // ── Orders tab state ─────────────────────────────────────────────────
  let _orders       = $state(/** @type {any[]} */ ([]));
  let _algoRejected = $state(/** @type {any[]} */ ([]));
  /** @type {ReturnType<typeof setInterval> | undefined} */
  let _ordersPoll;

  // Kite statuses: OPEN / TRIGGER PENDING / VALIDATION PENDING are
  // pending; COMPLETE / REJECTED / CANCELLED / UNFILLED are terminal.
  const PENDING_STATUSES = new Set([
    'OPEN', 'TRIGGER PENDING', 'VALIDATION PENDING', 'PENDING',
  ]);
  const _ordersPending   = $derived(_orders.filter(o => PENDING_STATUSES.has(o.status)));

  // Completed section: Kite terminal orders + LOCAL REJECTED algo_orders.
  // Local rows carry a `_local: true` flag so the template can render
  // a "LOCAL" chip distinguishing "we blocked it" from "broker rejected".
  const _ordersCompleted = $derived.by(() => {
    const kite = /** @type {any[]} */ (_orders).filter(o => !PENDING_STATUSES.has(o.status));
    const local = /** @type {any[]} */ (_algoRejected).map(o => ({ .../** @type {any} */ (o), _local: true }));
    return [...kite, ...local]
      .sort((a, b) => {
        const ta = a.exchange_update_timestamp ?? a.order_timestamp ?? a.filled_at ?? a.created_at ?? '';
        const tb = b.exchange_update_timestamp ?? b.order_timestamp ?? b.filled_at ?? b.created_at ?? '';
        return (tb || '').localeCompare(ta || '');
      })
      .slice(0, 30);
  });

  async function _loadOrdersData() {
    try {
      const [ordRes, algoRejRes] = await Promise.all([
        // Real Kite broker orders — same source the /orders page uses.
        fetchOrders(),
        // Local REJECTED algo_orders that never reached Kite (preflight blocks).
        fetchAlgoOrdersRecent(20, 'live'),
      ]);
      _orders       = (Array.isArray(ordRes) ? ordRes : (ordRes?.rows ?? ordRes ?? []));
      const allAlgo = (Array.isArray(algoRejRes) ? algoRejRes : (algoRejRes?.orders ?? algoRejRes ?? []));
      _algoRejected = allAlgo.filter((/** @type {any} */ o) => (o.status ?? '').toUpperCase() === 'REJECTED');
    } catch (_) { /* silent */ }
  }

  /** Format an ISO UTC timestamp to HH:MM:SS for order-card meta lines. */
  function _fmtEventTime(/** @type {unknown} */ ts) {
    if (!ts || typeof ts !== 'string') return '—';
    const out = logTime(ts.endsWith('Z') ? ts : ts + 'Z');
    return out || '—';
  }

  // Close on Escape + always-on order-data poll (bottom panel is always visible).
  onMount(() => {
    const onKey = (/** @type {KeyboardEvent} */ e) => {
      if (e.key === 'Escape') {
        // Fullscreen exits first; second Esc closes the panel.
        if (_fullscreen) { _fullscreen = false; return; }
        onClose();
      }
    };
    window.addEventListener('keydown', onKey);
    _loadOrdersData();
    _ordersPoll = setInterval(_loadOrdersData, 3000);
    return () => {
      window.removeEventListener('keydown', onKey);
      if (_ordersPoll) { clearInterval(_ordersPoll); _ordersPoll = undefined; }
      if (_wlToastTimer) { clearTimeout(_wlToastTimer); _wlToastTimer = null; }
    };
  });

  // Tab order — Chart first (most common row-click intent: analyse
  // before acting), then Ticket (most common write action), Chain
  // (specialised), Command (power-user). Mirrors TWS / ToS shape.
  const TABS = /** @type {const} */ ([
    { id: 'chart',   label: 'Chart',        dot: '#f0d070', activeTxt: '#f0d070', activeBorder: '#f0d070',
      activeBg: 'rgba(240,208,112,0.14)' },
    { id: 'ticket',  label: 'Order ticket', dot: '#fbbf24', activeTxt: '#fbbf24', activeBorder: '#fbbf24',
      activeBg: 'rgba(251,191,36,0.14)' },
    { id: 'chain',   label: 'Chain',        dot: '#4ade80', activeTxt: '#4ade80', activeBorder: '#4ade80',
      activeBg: 'rgba(74,222,128,0.14)' },
    { id: 'command', label: 'Command line', dot: '#7dd3fc', activeTxt: '#7dd3fc', activeBorder: '#7dd3fc',
      activeBg: 'rgba(125,211,252,0.14)' },
  ]);

  // Effective OrderTicket props — merge _cmdOrderProps (from Command tab
  // parse) on top of the shell's own props, so a typed command wins.
  const _ticketProps = $derived.by(() => {
    if (_cmdOrderProps) return _cmdOrderProps;
    return {
      symbol, exchange, side, action, qty, product, orderType, variety,
      price, trigger, lotSize, accounts, account, orderId,
      defaultMode, availableModes, currentQty, onAddToBasket,
    };
  });
</script>

<!-- Modal overlay (omitted in inline mode — renders as flat page content). -->
<div class="oes-overlay"
     class:oes-inline={inline}
     role={inline ? undefined : 'dialog'}
     aria-modal={inline ? undefined : 'true'}
     aria-label={inline ? undefined : (symbol || 'Symbol panel')}
     onclick={inline ? undefined : onClose}>
  <div class="oes-modal" class:oes-modal-inline={inline}
       class:oes-modal-fs={_fullscreen}
       role="document"
       onclick={inline ? undefined : (e) => e.stopPropagation()}>

    <!-- Header (close button hidden in inline mode). The plain title
         placeholder was replaced with a live Symbol picker — operator
         types a prefix, picks an instrument from the dropdown, and
         every tab (Chart, Ticket, Chain, Command) re-renders against
         the new symbol. Symbol is the only shell-level shared state;
         per-tab values (qty, side, etc.) stay with their tabs.

         When `headerless` is true, the host page renders its own
         symbol picker (typically inside a parent card header) so we
         skip the strip here entirely. -->
    {#if !headerless}
    <div class="oes-header">
      <div class="oes-sym-pick">
        <input
          type="text"
          class="oes-sym-input"
          value={_symbolOpen ? _symbolQuery : (_localSymbol || '')}
          placeholder="Symbol…"
          spellcheck="false"
          autocomplete="off"
          oninput={(e) => _onSymbolInput(/** @type {HTMLInputElement} */ (e.currentTarget).value)}
          onfocus={(e) => { _symbolQuery = ''; _symbolOpen = true; _onSymbolInput(/** @type {HTMLInputElement} */ (e.currentTarget).value); }}
          onblur={() => setTimeout(() => { _symbolOpen = false; }, 150)}
          onkeydown={(e) => {
            if (e.key === 'Enter' && _symbolSuggestions.length) { e.preventDefault(); _pickSymbol(_symbolSuggestions[0]); }
            else if (e.key === 'Escape') { _symbolOpen = false; }
          }} />
        {#if _symbolOpen && _symbolSuggestions.length}
          <div class="oes-sym-drop">
            {#each _symbolSuggestions as inst (inst.sym)}
              <button type="button" class="oes-sym-row"
                onmousedown={(e) => { e.preventDefault(); _pickSymbol(inst); }}>
                <span class="oes-sym-row-sym">{inst.sym}</span>
                <span class="oes-sym-row-meta">{inst.e}{inst.t ? ' · ' + inst.t : ''}</span>
              </button>
            {/each}
          </div>
        {/if}
      </div>
      {#if exchange}<span class="oes-exch">{exchange}</span>{/if}
      {#if _chartLastClose != null}
        <span class="oes-last">₹{priceFmt(_chartLastClose)}</span>
      {/if}
      {#if _chartPct != null}
        <span class="oes-pct {_chartPct >= 0 ? 'up' : 'down'}">
          {_chartPct >= 0 ? '+' : ''}{_chartPct.toFixed(2)}%
        </span>
      {/if}
      {#if onAddToWatchlist}
        <!-- +W (add to watchlist) — visible only when the caller
             wired the callback. Callers that already have the symbol
             on a tracked surface (Performance row click, etc.) omit
             this prop so the button hides automatically. -->
        <button type="button" class="oes-wl-add"
                disabled={_wlInFlight}
                title={`Add ${symbol} to watchlist`}
                aria-label={`Add ${symbol} to watchlist`}
                onclick={_addToWatchlist}>
          ★ +W
        </button>
      {/if}
      {#if _wlToast}
        <span class="oes-wl-toast" class:ok={_wlToast.ok} class:err={!_wlToast.ok}>
          {_wlToast.msg}
        </span>
      {/if}
      {#if !inline}
        <button type="button" class="oes-close" title="Close" aria-label="Close" onclick={onClose}>×</button>
      {/if}
    </div>
    {/if}

    <!-- Tab strip -->
    <div class="oes-tabs" role="tablist">
      {#each TABS as tab}
        {@const disabled   = tab.id === 'chain' && chainDisabled}
        {@const isActive   = _activeTab === tab.id}
        {@const badgeCount = tab.id === 'chain' ? basketLegs.length : 0}
        <button
          type="button"
          role="tab"
          class="oes-tab"
          class:oes-tab-disabled={disabled}
          disabled={disabled}
          title={disabled ? 'Option chain only applies to F&O instruments' : undefined}
          aria-selected={isActive}
          aria-disabled={disabled}
          style="
            color: {isActive ? tab.activeTxt : '#94a3b8'};
            background: {isActive ? tab.activeBg : 'transparent'};
            border-bottom-color: {isActive ? tab.activeBorder : 'transparent'};
            font-weight: {isActive ? '800' : '600'};
            opacity: {disabled ? '0.5' : '1'};
            cursor: {disabled ? 'not-allowed' : 'pointer'};
          "
          onclick={() => { if (!disabled) _activeTab = /** @type {any} */ (tab.id); }}
        >
          <span class="oes-tab-dot" style="background:{tab.dot};"></span>
          {tab.label}
          {#if tab.id === 'chain' && badgeCount > 0}
            <span class="oes-tab-badge">{badgeCount}</span>
          {/if}
        </button>
      {/each}
    </div>

    <!-- Tab content -->
    <div class="oes-body">
      {#if _activeTab === 'chart'}
        <!-- Chart tab — close-price line over the selected window
             with a hover crosshair (OHLCV tooltip). Range buttons
             (1W/1M/3M) above the SVG refire /api/options/historical
             with the new `days` parameter. -->
        <div class="oes-chart">
          <!-- Toolbar — chart type, range, indicators. Three segmented
               groups so the operator's eye groups them by purpose
               (presentation / window / overlays). -->
          <div class="oes-chart-toolbar">
            <div class="oes-chart-controls">
              <button type="button" class="oes-chart-range"
                      class:active={_chartType === 'line'}
                      title="Line — close price only"
                      onclick={() => _chartType = 'line'}>Line</button>
              <button type="button" class="oes-chart-range"
                      class:active={_chartType === 'area'}
                      title="Area — close price with shaded fill below"
                      onclick={() => _chartType = 'area'}>Area</button>
              <button type="button" class="oes-chart-range"
                      class:active={_chartType === 'candle'}
                      title="Candle — OHLC bars (green up / red down)"
                      onclick={() => _chartType = 'candle'}>Candle</button>
            </div>
            <div class="oes-chart-controls">
              <button type="button" class="oes-chart-range"
                      class:active={_chartDays === 7}
                      title="Past 1 week"
                      onclick={() => _setChartRange(7)}>1W</button>
              <button type="button" class="oes-chart-range"
                      class:active={_chartDays === 30}
                      title="Past 1 month"
                      onclick={() => _setChartRange(30)}>1M</button>
              <button type="button" class="oes-chart-range"
                      class:active={_chartDays === 90}
                      title="Past 3 months"
                      onclick={() => _setChartRange(90)}>3M</button>
            </div>
            <div class="oes-chart-controls">
              <button type="button" class="oes-chart-range"
                      class:active={_showSma20}
                      title="20-period simple moving average"
                      onclick={() => _showSma20 = !_showSma20}>SMA20</button>
              <button type="button" class="oes-chart-range"
                      class:active={_showSma50}
                      title="50-period simple moving average"
                      onclick={() => _showSma50 = !_showSma50}>SMA50</button>
              <button type="button" class="oes-chart-range"
                      class:active={_showVol}
                      title="Volume bars in lower band"
                      onclick={() => _showVol = !_showVol}>Vol</button>
            </div>
            <!-- Fullscreen toggle — sits at the end of the toolbar so
                 it doesn't compete visually with the chart-type /
                 range / indicator clusters. Esc also exits FS. -->
            <button type="button" class="oes-chart-fs"
                    class:active={_fullscreen}
                    title={_fullscreen ? 'Exit fullscreen (Esc)' : 'Fullscreen chart'}
                    aria-label={_fullscreen ? 'Exit fullscreen' : 'Enter fullscreen'}
                    onclick={_toggleFullscreen}>
              {_fullscreen ? '⤡' : '⤢'}
            </button>
          </div>
          {#if _chartLoading && !_chartBars.length}
            <div class="oes-chart-state">Loading bars…</div>
          {:else if _chartError}
            <div class="oes-chart-state oes-chart-err">{_chartError}</div>
          {:else if !_chartBars.length}
            <div class="oes-chart-state">No bars to plot.</div>
          {:else}
            <svg viewBox="0 0 {CW} {CH}" preserveAspectRatio="none"
                 class="oes-chart-svg"
                 onpointermove={(ev) => {
                   if (!_chartBars.length || !_chartXDomain) { _chartHover = null; return; }
                   const svg = /** @type {SVGSVGElement} */ (ev.currentTarget);
                   const rect = svg.getBoundingClientRect();
                   const xRel = ((ev.clientX - rect.left) / rect.width) * CW;
                   const tMs = _chartXDomain.lo + ((xRel - CPAD_L) / _chartInnerW) * (_chartXDomain.hi - _chartXDomain.lo);
                   let best = _chartBars[0], bestD = Infinity;
                   for (const b of _chartBars) {
                     const d = Math.abs(Date.parse(b.ts) - tMs);
                     if (d < bestD) { bestD = d; best = b; }
                   }
                   const tx = Date.parse(best.ts);
                   _chartHover = { x: _chartXOf(tx), y: _chartYOf(Number(best.close)), bar: best };
                 }}
                 onpointerleave={() => { _chartHover = null; }}>
              {#each _chartYTicks as v}
                <line x1={CPAD_L} x2={CW - CPAD_R} y1={_chartYOf(v)} y2={_chartYOf(v)}
                      stroke="rgba(200,216,240,0.18)" stroke-width="1" stroke-dasharray="2 3" />
                <text x={CPAD_L - 6} y={_chartYOf(v) + 3} text-anchor="end"
                      fill="#c8d8f0" font-size="11" font-weight="600">₹{priceFmt(v)}</text>
              {/each}
              {#each _chartXLabels as l}
                <line x1={l.x} x2={l.x} y1={CPAD_T} y2={CH - CPAD_B}
                      stroke="rgba(200,216,240,0.10)" stroke-width="1" stroke-dasharray="2 3" />
                <text x={l.x} y={CH - CPAD_B + 14} text-anchor="middle"
                      fill="#c8d8f0" font-size="11" font-weight="600">{l.label}</text>
              {/each}
              <!-- Volume bars (lower band, when enabled) — drawn
                   BEFORE the price layer so the line/candle sits on
                   top and hover crosshair logic isn't shadowed. -->
              {#if _showVol}
                {#each _chartVol as v}
                  <rect x={v.x} y={v.y} width={v.w} height={v.h}
                        fill={v.up ? 'rgba(74,222,128,0.35)' : 'rgba(248,113,113,0.35)'} />
                {/each}
              {/if}

              <!-- Price layer — chart-type branch. Line keeps the
                   original amber polyline. Area paints the same
                   polyline + a translucent fill below. Candle draws
                   one OHLC rect+wick per bar (green up / red down). -->
              {#if _chartType === 'area'}
                <path d={_chartAreaPath} fill="rgba(251,191,36,0.16)"
                      stroke="none" />
                <path d={_chartLinePath} fill="none"
                      stroke="#fbbf24" stroke-width="1.8"
                      stroke-linejoin="round" stroke-linecap="round" />
              {:else if _chartType === 'candle'}
                {#each _chartCandles as c}
                  <line x1={c.x} x2={c.x} y1={c.wickTop} y2={c.wickBot}
                        stroke={c.up ? '#4ade80' : '#f87171'} stroke-width="1" />
                  <rect x={c.x - c.w / 2} y={c.bodyY} width={c.w} height={c.bodyH}
                        fill={c.up ? '#4ade80' : '#f87171'} />
                {/each}
              {:else}
                <path d={_chartLinePath} fill="none"
                      stroke="#fbbf24" stroke-width="1.8"
                      stroke-linejoin="round" stroke-linecap="round" />
              {/if}

              <!-- SMA overlays — dashed coloured lines on top of the
                   price layer when their respective toggles are on. -->
              {#if _sma20Path}
                <path d={_sma20Path} fill="none"
                      stroke="#7dd3fc" stroke-width="1.4"
                      stroke-dasharray="4 3"
                      stroke-linejoin="round" stroke-linecap="round" />
              {/if}
              {#if _sma50Path}
                <path d={_sma50Path} fill="none"
                      stroke="#c084fc" stroke-width="1.4"
                      stroke-dasharray="6 3"
                      stroke-linejoin="round" stroke-linecap="round" />
              {/if}
              {#if _chartHover}
                <line x1={_chartHover.x} x2={_chartHover.x} y1={CPAD_T} y2={CH - CPAD_B}
                      stroke="rgba(251,191,36,0.5)" stroke-width="1" stroke-dasharray="3 2" />
                <circle cx={_chartHover.x} cy={_chartHover.y} r="3"
                        fill="#fbbf24" stroke="#fff" stroke-width="1" />
                {@const _tx = Math.min(CW - 150 - CPAD_R, Math.max(CPAD_L, _chartHover.x + 8))}
                {@const _ty = Math.max(CPAD_T + 4, _chartHover.y - 64)}
                <rect x={_tx} y={_ty} width="150" height="62" rx="3"
                      fill="#1d2a44" stroke="rgba(251,191,36,0.4)" stroke-width="1" />
                <text x={_tx + 6} y={_ty + 14} fill="#fbbf24"
                      font-size="10" font-weight="800" font-family="monospace">
                  {_chartHover.bar.ts.slice(0, 10)}
                </text>
                <text x={_tx + 6} y={_ty + 28} fill="#c8d8f0"
                      font-size="9" font-family="monospace">
                  O ₹{priceFmt(_chartHover.bar.open)}  H ₹{priceFmt(_chartHover.bar.high)}
                </text>
                <text x={_tx + 6} y={_ty + 42} fill="#c8d8f0"
                      font-size="9" font-family="monospace">
                  L ₹{priceFmt(_chartHover.bar.low)}  C ₹{priceFmt(_chartHover.bar.close)}
                </text>
                <text x={_tx + 6} y={_ty + 56} fill="#7e97b8"
                      font-size="9" font-family="monospace">
                  Vol {Number(_chartHover.bar.volume || 0).toLocaleString()}
                </text>
              {/if}
            </svg>
            <div class="oes-chart-meta">
              {_chartBars.length} bars · last {_chartDays === 7 ? '1 w' : _chartDays === 30 ? '1 m' : '3 m'}
            </div>
          {/if}
        </div>

      {:else if _activeTab === 'command'}
        <CommandLineTab
          onParsedOrder={handleParsedOrder}
          onAddToBasket={handleCmdAddToBasket}
          prefillSide={side}
          prefillAccount={account}
          prefillSymbol={_localSymbol}
          prefillQty={qty}
          prefillPrice={price ?? 0}
          prefillOrderType={orderType} />

      {:else if _activeTab === 'ticket'}
        <!-- OrderTicket renders its own overlay/modal chrome; inside
             the shell we only want the body. We render it without the
             outer overlay by mounting it directly — the shell provides
             the modal chrome above, so we suppress OrderTicket's own
             overlay by setting a container class that strips position:fixed. -->
        <div class="oes-ticket-body">
          <OrderTicket
            symbol={_ticketProps.symbol || _localSymbol}
            exchange={_ticketProps.exchange ?? exchange}
            side={_ticketProps.side ?? side}
            action={_ticketProps.action ?? action}
            qty={_ticketProps.qty ?? qty}
            product={_ticketProps.product ?? product}
            orderType={_ticketProps.orderType ?? orderType}
            variety={_ticketProps.variety ?? variety}
            price={_ticketProps.price ?? price}
            trigger={_ticketProps.trigger ?? trigger}
            lotSize={_ticketProps.lotSize ?? lotSize}
            accounts={_ticketProps.accounts ?? accounts}
            account={_sharedAccount || _ticketProps.account || account}
            onAccountChange={_onAccountChange}
            orderId={_ticketProps.orderId ?? orderId}
            defaultMode={_ticketProps.defaultMode ?? defaultMode}
            availableModes={_ticketProps.availableModes ?? availableModes}
            currentQty={_ticketProps.currentQty ?? currentQty}
            onAddToBasket={addToBasket}
            basketMode={basketMode}
            {onSubmit}
            {onClose} />
        </div>

      {:else if _activeTab === 'chain'}
        <!-- OptionChainTab's own basket state is migrated to the shell.
             The tab receives the shared basket as props and calls back
             into the shell to mutate it. Its own placeBasket is unused
             when routed through onSubmitBasket. -->
        <OptionChainTab
          symbol={_localSymbol}
          account={_sharedAccount || account}
          onAccountChange={_onAccountChange}
          {accounts}
          basketLegs={basketLegs}
          onAddLeg={addToBasket}
          onRemoveLeg={(/** @type {any} */ leg) => {
            const i = basketLegs.findIndex(b => b.key === leg.key);
            if (i >= 0) removeBasketLeg(i);
          }}
          onUpdateLeg={updateLegByKey}
          onSubmitBasket={submitBasket}
          onClearBasket={clearBasket}
          onPlaceLeg={handleParsedOrder}
          onBasketPlace={({ ok, fail }) => {
            if (ok > 0 && fail === 0) {
              onSubmit?.({ mode: 'paper', _basketLegs: ok });
              setTimeout(onClose, 400);
            }
          }} />

      {/if}
    </div>

    <!-- Shell-level basket bar — visible from any tab when legs exist.
         Per-leg pills (B/S · sym · lots stepper · × remove) sit on the
         left; Clear / Submit on the right. Same shape as the chain
         tab's in-tab basket so the operator sees what's pending from
         any tab without flipping back to Chain. -->
    {#if basketLegs.length > 0}
      <div class="oes-basket-bar">
        <div class="oes-basket-pills" role="list">
          {#each basketLegs as leg, i (leg.key)}
            <span class="oes-basket-pill oes-basket-pill-{leg.side === 'BUY' ? 'buy' : 'sell'} oes-basket-pill-type-{/CE$/.test(leg.sym) ? 'ce' : /PE$/.test(leg.sym) ? 'pe' : /FUT$/.test(leg.sym) ? 'fut' : 'eq'}"
                  class:is-disabled={basketSubmitting}
                  role="listitem"
                  title="Click × to remove from basket">
              <span class="oes-basket-pill-side">{leg.side === 'BUY' ? 'B' : 'S'}</span>
              <span class="oes-basket-pill-sym">{leg.sym}</span>
              <button type="button" class="oes-basket-pill-step"
                      title="Decrease lots"
                      disabled={basketSubmitting || (leg.lots || 1) <= 1}
                      onclick={() => updateLegByKey(leg.key, b => ({ ...b, lots: Math.max(1, (b.lots || 1) - 1) }))}>−</button>
              <span class="oes-basket-pill-lots">{leg.lots || 1}</span>
              <button type="button" class="oes-basket-pill-step"
                      title="Increase lots"
                      disabled={basketSubmitting}
                      onclick={() => updateLegByKey(leg.key, b => ({ ...b, lots: (b.lots || 1) + 1 }))}>+</button>
              {#if leg.lotSize > 1}
                <span class="oes-basket-pill-qty">× {leg.lotSize} = {(leg.lots || 1) * leg.lotSize}</span>
              {/if}
              <button type="button" class="oes-basket-pill-remove"
                      title="Remove leg from basket"
                      disabled={basketSubmitting}
                      onclick={() => removeBasketLeg(i)}>×</button>
            </span>
          {/each}
        </div>
        <div class="oes-basket-meta">
          {#if basketResultMsg}
            <span class="oes-basket-result">{basketResultMsg}</span>
          {/if}
          <div class="oes-basket-actions">
            <button type="button" class="oes-basket-clear" disabled={basketSubmitting} onclick={clearBasket}>Clear</button>
            <button type="button" class="oes-basket-submit" disabled={basketSubmitting} onclick={submitBasket}>
              {basketSubmitting ? 'Placing…' : `Submit basket (${basketLegs.length})`}
            </button>
          </div>
        </div>
      </div>
    {/if}

    <!-- Bottom panel — Log / Orders — always visible. -->
    <div class="oes-bottom-panel">
      <div class="oes-bottom-tabs" role="tablist">
        <button type="button" role="tab" class="oes-bottom-tab"
                class:active={_bottomTab === 'log'}
                aria-selected={_bottomTab === 'log'}
                onclick={() => _bottomTab = 'log'}>Log</button>
        <button type="button" role="tab" class="oes-bottom-tab"
                class:active={_bottomTab === 'orders'}
                aria-selected={_bottomTab === 'orders'}
                onclick={() => _bottomTab = 'orders'}>
          Orders
          {#if _ordersPending.length > 0}
            <span class="oes-bottom-badge">{_ordersPending.length}</span>
          {/if}
        </button>
      </div>

      <div class="oes-bottom-body">
        {#if _bottomTab === 'log'}
          <UnifiedLog
            filter={{}}
            pollMs={3000}
            maxRows={30}
            heightClass="oes-bottom-scroll"
            cardMode={true}
          />

        {:else}
          <!-- PENDING orders -->
          {#if _ordersPending.length === 0 && _ordersCompleted.length === 0}
            <div class="oes-orders-empty">No orders yet.</div>
          {:else}
            {#if _ordersPending.length > 0}
              <header class="oes-orders-head">PENDING <span class="oes-orders-count">{_ordersPending.length}</span></header>
              {#each _ordersPending as o (o.order_id ?? o.id)}
                <article class="oes-order-card">
                  <div class="oes-card-head">
                    <span class="oes-status oes-status-{(o.status ?? '').toLowerCase().replace(/\s+/g, '-')}">{o.status}</span>
                    <span class="oes-side oes-side-{(o.transaction_type ?? '').toLowerCase()}">{o.transaction_type}</span>
                    <span class="oes-card-qty">{o.quantity}</span>
                    <span class="oes-card-sym">{o.tradingsymbol}</span>
                    <span class="oes-card-px">{priceFmt(o.price ?? o.initial_price ?? 0)}</span>
                  </div>
                  <div class="oes-card-meta">
                    acct={o.account ?? '—'} · #{o.order_id ?? o.id} ·
                    {_fmtEventTime(o.order_timestamp ?? o.created_at)}
                  </div>
                </article>
              {/each}
            {/if}
            {#if _ordersCompleted.length > 0}
              <header class="oes-orders-head" style="margin-top: 0.3rem;">COMPLETED <span class="oes-orders-count">{_ordersCompleted.length}</span></header>
              {#each _ordersCompleted as o (o.order_id ?? o.id)}
                <article class="oes-order-card oes-order-card-done">
                  <div class="oes-card-head">
                    <span class="oes-status oes-status-{(o.status ?? '').toLowerCase().replace(/\s+/g, '-')}">{o.status}</span>
                    {#if o._local}<span class="oes-local-chip">LOCAL</span>{/if}
                    <span class="oes-side oes-side-{(o.transaction_type ?? o.side ?? '').toLowerCase()}">{o.transaction_type ?? o.side}</span>
                    <span class="oes-card-qty">{o.quantity}</span>
                    <span class="oes-card-sym">{o.tradingsymbol}</span>
                    <span class="oes-card-px">{priceFmt(o.average_price ?? o.fill_price ?? o.price ?? o.initial_price ?? 0)}</span>
                  </div>
                  <div class="oes-card-meta">
                    acct={o.account ?? '—'} · #{o.order_id ?? o.id} ·
                    {_fmtEventTime(o.exchange_update_timestamp ?? o.order_timestamp ?? o.filled_at ?? o.created_at)}
                  </div>
                </article>
              {/each}
            {/if}
          {/if}
        {/if}
      </div>
    </div>

  </div>
</div>

<style>
  .oes-overlay {
    position: fixed;
    inset: 0;
    /* Light wash — earlier 0.55 alpha blacked out the entire viewport
       behind the modal, which on /pulse felt like 'symbols disappear'
       when the operator clicked a row. 0.25 dims the background
       enough to focus the eye on the modal while keeping the grid
       legible behind. Click-outside still closes the modal so the
       UX contract is preserved. */
    background: rgba(0,0,0,0.25);
    display: flex;
    /* Anchor modal to a fixed Y from the top instead of vertically
       centering — earlier `align-items: center` caused the modal to
       re-center every time the body grew (chart bars arriving,
       margin preview rendering, etc.), producing a visible "open
       then resize" jump. With flex-start the modal opens at the
       same Y position and grows downward without moving. */
    align-items: flex-start;
    justify-content: center;
    z-index: 100;
    padding: 3rem 1rem 1rem;
  }
  /* Inline mode strips the modal chrome — used by /console which hosts
     the shell as the page's primary content. */
  .oes-overlay.oes-inline {
    position: static;
    inset: auto;
    background: transparent;
    display: block;
    z-index: auto;
    padding: 0;
  }
  .oes-modal {
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 8px;
    /* Widened to 50 rem so the 720 px chart SVG fits at its native
       aspect ratio without forcing the modal to grow on Chart-tab
       activation. Earlier 34 rem (~544 px) was sized for the order
       ticket only — chart bars arriving pushed the modal wider,
       contributing to the "open then resize" jank. */
    width: min(50rem, calc(100vw - 2rem));
    max-height: calc(100vh - 4rem);
    overflow-y: auto;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    box-shadow: 0 12px 32px rgba(0,0,0,0.6);
    display: flex;
    flex-direction: column;
  }
  .oes-modal.oes-modal-inline {
    width: 100%;
    max-height: none;
    border-radius: 0;
    box-shadow: none;
    /* Drop the amber outline + gradient background — the host card
       provides its own chrome, so a second nested border would
       double-frame the content. Per operator request "remove the
       current yellow border". */
    border: none;
    background: transparent;
  }
  /* Fullscreen — chart fills the viewport with minimal chrome around
     it. Click ⤡ in the toolbar or press Esc to exit. The chart SVG
     stretches into the enlarged container via flex; no extra layout
     tricks needed. */
  .oes-modal.oes-modal-fs {
    width: calc(100vw - 1rem);
    max-width: none;
    height: calc(100vh - 1rem);
    max-height: calc(100vh - 1rem);
    border-radius: 4px;
  }
  /* In fullscreen, let the chart container grow vertically into the
     available space (it was fixed to ~380px to prevent open-then-
     resize jank in the regular modal). */
  .oes-modal.oes-modal-fs :global(.oes-chart) {
    min-height: calc(100vh - 14rem);
  }
  .oes-modal.oes-modal-fs :global(.oes-chart svg) {
    height: 100%;
  }

  /* Fullscreen toggle button in the chart toolbar. */
  .oes-chart-fs {
    background: rgba(251,191,36,0.10);
    border: 1px solid rgba(251,191,36,0.30);
    color: #c8d8f0;
    padding: 0.18rem 0.45rem;
    font-size: 0.72rem;
    font-family: ui-monospace, monospace;
    border-radius: 3px;
    cursor: pointer;
    margin-left: auto;  /* push to right edge of toolbar */
    line-height: 1;
  }
  .oes-chart-fs:hover { background: rgba(251,191,36,0.18); }
  .oes-chart-fs.active {
    background: rgba(251,191,36,0.25);
    color: #fbbf24;
    border-color: rgba(251,191,36,0.60);
  }

  /* Header */
  .oes-header {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.7rem 1rem 0.5rem;
    border-bottom: 1px solid rgba(251,191,36,0.15);
    flex-shrink: 0;
  }
  .oes-title {
    font-size: 0.85rem;
    font-weight: 800;
    color: #fbbf24;
    letter-spacing: 0.04em;
    font-family: ui-monospace, monospace;
  }
  /* Shared Symbol picker — replaces the static `.oes-title` placeholder
     ("Symbol") with a live search input. Operator can pick a new
     symbol; every tab (Chart / Ticket / Chain / Command) re-renders
     against the chosen instrument. Same amber typography as the old
     title so the position reads identically. */
  .oes-sym-pick {
    position: relative;
    display: inline-flex;
    align-items: center;
  }
  .oes-sym-input {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 3px;
    padding: 0.18rem 0.45rem;
    color: #fbbf24;
    font-size: 0.85rem;
    font-weight: 800;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.04em;
    width: 11rem;
    text-transform: uppercase;
  }
  .oes-sym-input:focus {
    outline: none;
    border-color: rgba(251, 191, 36, 0.55);
    background: rgba(251, 191, 36, 0.06);
  }
  .oes-sym-input::placeholder {
    color: rgba(251, 191, 36, 0.40);
    font-weight: 600;
  }
  .oes-sym-drop {
    position: absolute;
    top: 100%;
    left: 0;
    z-index: 60;
    margin-top: 2px;
    min-width: 14rem;
    max-height: 14rem;
    overflow-y: auto;
    background: #1b2540;
    border: 1px solid rgba(251, 191, 36, 0.35);
    border-radius: 4px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.55);
    display: flex;
    flex-direction: column;
  }
  .oes-sym-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.2rem 0.4rem;
    background: transparent;
    border: 0;
    color: #c8d8f0;
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    cursor: pointer;
    text-align: left;
    width: 100%;
  }
  .oes-sym-row:hover {
    background: rgba(251, 191, 36, 0.12);
    color: #fbbf24;
  }
  .oes-sym-row-sym {
    font-weight: 700;
    letter-spacing: 0.03em;
  }
  .oes-sym-row-meta {
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
  }
  /* Exchange tag — small, muted, matches the LogPanel chip palette. */
  .oes-exch {
    color: #7e97b8;
    background: rgba(126, 151, 184, 0.15);
    border: 1px solid rgba(126, 151, 184, 0.32);
    padding: 0.06rem 0.32rem;
    border-radius: 2px;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
    font-family: ui-monospace, monospace;
  }
  /* Last price / pct chips — read from the chart fetch when the
     Chart tab has loaded its bars. Hidden cleanly when no bars
     are present yet (the conditional in the template). */
  .oes-last {
    color: #f1f7ff;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    font-size: 0.72rem;
    font-family: ui-monospace, monospace;
  }
  .oes-pct {
    padding: 0.1rem 0.4rem;
    border-radius: 2px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    font-size: 0.68rem;
    font-family: ui-monospace, monospace;
  }
  .oes-pct.up   { color: #4ade80; background: rgba(74, 222, 128, 0.12); }
  .oes-pct.down { color: #f87171; background: rgba(248, 113, 113, 0.12); }
  /* Push the close button to the right edge regardless of how many
     header chips render. */
  .oes-close { margin-left: auto; }

  /* +W (add to watchlist) — outlined champagne button, sits between
     the price chips and the close button. Visible only when the
     caller wires `onAddToWatchlist`. */
  .oes-wl-add {
    margin-left: auto;
    background: transparent;
    border: 1px solid rgba(251,191,36,0.45);
    color: #fbbf24;
    padding: 0.18rem 0.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    line-height: 1;
  }
  .oes-wl-add:hover:not(:disabled) {
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.7);
  }
  .oes-wl-add:disabled { opacity: 0.55; cursor: wait; }
  /* When +W is rendered, the close button no longer needs to push
     itself; +W has margin-left:auto and close sits next to it. */
  .oes-wl-add ~ .oes-close { margin-left: 0.35rem; }
  /* Brief success/error flash next to the +W button after a click. */
  .oes-wl-toast {
    padding: 0.18rem 0.45rem;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .oes-wl-toast.ok  { color: #4ade80; background: rgba(74,222,128,0.16); }
  .oes-wl-toast.err { color: #f87171; background: rgba(248,113,113,0.16); }

  /* Chart tab body — same SVG plot style as the retired
     SymbolChartModal, just laid out as a tab content slot inside
     the panel.

     `min-height: 380 px` reserves vertical space for the SVG
     (viewBox is 360 px + ~20 px chrome / meta line) BEFORE the
     bars arrive — without it the modal jumped from ~80 px (the
     "Loading bars…" line) to ~440 px once /api/options/historical
     responded, producing the visible "open then resize" jank the
     operator reported. */
  .oes-chart {
    padding: 0.6rem 0.85rem 0.4rem;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    min-height: 380px;
  }
  .oes-chart-svg {
    width: 100%;
    display: block;
    cursor: crosshair;
  }
  .oes-chart-state {
    text-align: center;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    padding: 3rem 1rem;
  }
  .oes-chart-state.oes-chart-err { color: #fda4a4; }
  .oes-chart-meta {
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    align-self: flex-end;
  }
  /* Toolbar — three groups of segmented buttons (type / range /
     indicators), each a horizontal pill cluster, flex-wrapped so
     narrow viewports stack groups vertically instead of overflowing
     the chart card. */
  .oes-chart-toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 0.8rem;
    justify-content: flex-end;
  }
  /* Range selector — segmented pill row right-aligned above the
     chart. Active state matches the algo amber palette so it reads
     as a primary selection chip. */
  .oes-chart-controls {
    display: flex;
    gap: 0.2rem;
  }
  .oes-chart-range {
    background: transparent;
    border: 1px solid rgba(126,151,184,0.32);
    color: #94a3b8;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    cursor: pointer;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.06em;
  }
  .oes-chart-range:hover {
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
  }
  .oes-chart-range.active {
    background: rgba(251,191,36,0.14);
    border-color: rgba(251,191,36,0.7);
    color: #fbbf24;
  }
  .oes-close {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.15);
    color: #c8d8f0;
    width: 1.55rem;
    height: 1.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .oes-close:hover { border-color: #f87171; color: #f87171; }

  /* Tab strip — bottom-border underline active tab; each tab has its own
     accent colour via inline style (applied from the TABS metadata).     */
  .oes-tabs {
    display: flex;
    gap: 0;
    padding: 0 1rem;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    flex-shrink: 0;
  }
  .oes-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.45rem 0.75rem;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    transition: color 0.12s, border-color 0.12s, opacity 0.12s;
    white-space: nowrap;
  }
  .oes-tab:hover:not(.oes-tab-disabled) {
    opacity: 0.8 !important;
  }
  .oes-tab-disabled {
    cursor: not-allowed !important;
  }
  /* Colour dot — small circle before the tab label */
  .oes-tab-dot {
    display: inline-block;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    flex-shrink: 0;
    opacity: 0.75;
  }
  /* Count badge on Chain tab */
  .oes-tab-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.1rem;
    height: 1.1rem;
    padding: 0 0.25rem;
    border-radius: 999px;
    background: rgba(74,222,128,0.25);
    border: 1px solid rgba(74,222,128,0.6);
    color: #4ade80;
    font-size: 0.55rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }

  /* Basket bar — sticky bottom strip inside the modal when legs exist. */
  .oes-basket-bar {
    position: sticky;
    bottom: 0;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem 0.7rem;
    padding: 0.55rem 1rem;
    border-top: 2px solid #4ade80;
    background: rgba(74,222,128,0.18);
    box-shadow: inset 0 4px 12px rgba(0,0,0,0.25);
    flex-shrink: 0;
    z-index: 2;
  }
  .oes-basket-result {
    font-family: monospace;
    font-size: 0.62rem;
    color: #c8d8f0;
    margin-right: 0.4rem;
  }
  .oes-basket-meta {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: auto;
  }
  .oes-basket-actions {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
  }

  /* Per-leg pills. Mirror the chain-tab basket palette so an
     operator scanning the bottom strip from any tab gets the same
     B / S colour, sym, lots stepper, and × remove affordance. */
  .oes-basket-pills {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 0.3rem 0.35rem;
    align-items: center;
    flex: 1 1 auto;
    min-width: 0;
  }
  .oes-basket-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.18rem 0.4rem;
    border-radius: 2px;
    border: 1px solid;
    font-family: monospace;
    font-size: 0.58rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    line-height: 1;
    white-space: nowrap;
  }
  .oes-basket-pill-buy {
    color:        #67e8f9;
    border-color: rgba(103,232,249,0.55);
    background:   rgba(103,232,249,0.10);
  }
  .oes-basket-pill-sell {
    color:        #fbbf24;
    border-color: rgba(251,191,36,0.55);
    background:   rgba(251,191,36,0.10);
  }
  .oes-basket-pill-side {
    font-weight: 900;
    padding-right: 0.18rem;
    border-right: 1px solid currentColor;
    opacity: 0.9;
  }
  .oes-basket-pill-sym {
    font-weight: 800;
    color: #f1f7ff;
  }
  .oes-basket-pill-step {
    border: none;
    background: transparent;
    color: currentColor;
    font-weight: 800;
    font-family: monospace;
    cursor: pointer;
    padding: 0 0.2rem;
    line-height: 1;
  }
  .oes-basket-pill-step:hover:not(:disabled) { color: #fff; }
  .oes-basket-pill-step:disabled { opacity: 0.35; cursor: not-allowed; }
  .oes-basket-pill-lots {
    min-width: 1rem;
    text-align: center;
    font-weight: 800;
    color: #f1f7ff;
  }
  .oes-basket-pill-qty {
    font-size: 0.52rem;
    opacity: 0.7;
    font-weight: 600;
  }
  .oes-basket-pill-remove {
    border: none;
    background: transparent;
    color: rgba(255,255,255,0.55);
    font-weight: 900;
    font-family: monospace;
    cursor: pointer;
    padding: 0 0.2rem;
    margin-left: 0.1rem;
    line-height: 1;
  }
  .oes-basket-pill-remove:hover:not(:disabled) { color: #f87171; }
  .oes-basket-pill-remove:disabled { opacity: 0.35; cursor: not-allowed; }
  .oes-basket-pill.is-disabled { opacity: 0.55; }
  .oes-basket-clear,
  .oes-basket-submit {
    height: 1.9rem;
    padding: 0 0.85rem;
    border-radius: 2px;
    font-family: monospace;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    border: 1px solid currentColor;
    background: transparent;
    white-space: nowrap;
  }
  .oes-basket-clear  { color: #a3b9d0; }
  .oes-basket-clear:hover:not(:disabled) { background: rgba(163,185,208,0.08); }
  .oes-basket-submit {
    color: #fff;
    background: #4ade80;
    border-color: #4ade80;
    font-weight: 800;
  }
  .oes-basket-submit:hover:not(:disabled) { background: #4ade80; border-color: #4ade80; }
  .oes-basket-clear:disabled,
  .oes-basket-submit:disabled { opacity: 0.45; cursor: progress; }

  /* Body — the tab content area. */
  .oes-body {
    flex: 1 1 auto;
    overflow-y: auto;
  }

  /* Ticket body — OrderTicket renders its OWN overlay + modal shell,
     which conflicts when nested inside oes-modal. We override those
     fixed-position styles so it renders inline. The outer shell
     already provides the backdrop + border + padding. */
  .oes-ticket-body :global(.ot-overlay) {
    position: static !important;
    background: none !important;
    padding: 0 !important;
    display: block !important;
    z-index: auto !important;
  }
  .oes-ticket-body :global(.ot-modal) {
    width: 100% !important;
    max-height: none !important;
    /* Critical: OrderTicket's own .ot-modal has overflow-y: auto so it
       can scroll independently when used as a standalone modal. Inside
       the shell, that creates a double-scroll (outer .oes-body also
       scrolls), which pushes the Submit/Place footer off the visible
       viewport, behind the sticky bottom panel. Force visible so the
       ticket grows to its natural height inside .oes-body's single
       scroll region — Submit is then reachable by scrolling the body. */
    overflow-y: visible !important;
    border: none !important;
    border-radius: 0 !important;
    background: none !important;
    box-shadow: none !important;
    padding: 0.6rem 0.9rem !important;
  }
  /* Hide the OrderTicket's own × close button — the shell has one. */
  .oes-ticket-body :global(.ot-close) {
    display: none !important;
  }
  /* Hide OrderTicket header (symbol line) — the shell title carries it. */
  .oes-ticket-body :global(.ot-header) {
    display: none !important;
  }

  /* ── Orders tab ──────────────────────────────────────────────────── */
  .oes-orders-wrap {
    max-height: 30rem;
    overflow-y: auto;
    padding: 0.5rem 0.75rem;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
  }
  .oes-orders-section {
    padding: 0.55rem 0;
  }
  .oes-orders-section + .oes-orders-section {
    border-top: 1px solid rgba(255,255,255,0.07);
  }
  .oes-orders-head {
    color: #7e97b8;
    font-size: 0.55rem;
    letter-spacing: 0.08em;
    font-weight: 700;
    margin-bottom: 0.35rem;
    text-transform: uppercase;
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .oes-orders-count {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.1rem;
    height: 1.1rem;
    padding: 0 0.25rem;
    border-radius: 999px;
    background: rgba(192,132,252,0.2);
    border: 1px solid rgba(192,132,252,0.5);
    color: #c084fc;
    font-size: 0.55rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .oes-orders-empty {
    color: #7e97b8;
    font-style: italic;
    padding: 0.3rem 0;
  }

  /* Event rows inside the Orders tab */
  .oes-events-list { display: flex; flex-direction: column; gap: 0.42rem; }
  /* Stacked row: time on its own subtle line, kind + message below.
     Time block widened beyond grid cells caused the kind / message
     to disappear off-screen on narrow modals — the dual IST|EDT
     format is ~58 chars wide. Stacking is the only layout that
     handles every viewport width without truncation. */
  .oes-event-row {
    display: flex;
    flex-direction: column;
    gap: 0.08rem;
    color: #c8d8f0;
  }
  .oes-event-time {
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    font-size: 0.5rem;
    letter-spacing: 0.02em;
  }
  .oes-event-line {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
    flex-wrap: wrap;
  }
  .oes-event-kind {
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.55rem;
    flex-shrink: 0;
  }
  .oes-event-kind-placed          { color: #38bdf8; }
  .oes-event-kind-chase_modify    { color: #fbbf24; }
  .oes-event-kind-fill            { color: #4ade80; }
  .oes-event-kind-unfill          { color: #f87171; }
  .oes-event-kind-reject          { color: #f87171; }
  .oes-event-kind-preflight_ok    { color: #94a3b8; }
  .oes-event-kind-preflight_block { color: #f87171; }
  .oes-event-kind-cancel          { color: #94a3b8; }
  .oes-event-kind-postback        { color: #c084fc; }
  /* Agent-sourced event kinds — violet/pink palette so "rule fired"
     is instantly distinguishable from "manual order" events.         */
  .oes-event-kind-agent_fire          { color: #e879f9; }
  .oes-event-kind-agent_match         { color: #d946ef; }
  .oes-event-kind-agent_action_success { color: #a855f7; }
  .oes-event-kind-agent_action_error  { color: #f472b6; }
  .oes-event-kind-agent_skipped       { color: #94a3b8; }
  .oes-event-kind-agent_paused        { color: #7e97b8; }
  .oes-event-msg { color: #c8d8f0; }

  /* Order cards */
  .oes-order-card {
    background: rgba(15,23,42,0.6);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 3px;
    padding: 0.35rem 0.55rem;
    margin-bottom: 0.3rem;
  }
  .oes-order-card-done {
    opacity: 0.75;
  }
  .oes-card-head {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.3rem 0.5rem;
    font-size: 0.62rem;
    font-weight: 700;
  }
  .oes-card-meta {
    margin-top: 0.2rem;
    font-size: 0.58rem;
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
  }
  .oes-card-sym  { color: #c8d8f0; font-weight: 800; }
  .oes-card-qty  { color: #c8d8f0; font-variant-numeric: tabular-nums; }
  .oes-card-px   { color: #c8d8f0; font-variant-numeric: tabular-nums; }
  .oes-card-chase {
    font-size: 0.55rem;
    color: #fbbf24;
    border: 1px solid rgba(251,191,36,0.4);
    padding: 0 0.3rem;
    border-radius: 2px;
  }

  /* Status pills */
  .oes-status {
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
  }
  .oes-status-open,
  .oes-status-pending,
  .oes-status-trigger-pending,
  .oes-status-validation-pending  { background: rgba(251,191,36,0.15); color: #fbbf24; border: 1px solid rgba(251,191,36,0.4); }
  .oes-status-complete,
  .oes-status-filled              { background: rgba(74,222,128,0.12);  color: #4ade80; border: 1px solid rgba(74,222,128,0.4); }
  .oes-status-unfilled,
  .oes-status-rejected            { background: rgba(248,113,113,0.12);  color: #f87171; border: 1px solid rgba(248,113,113,0.4); }
  .oes-status-cancelled           { background: rgba(148,163,184,0.1); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }
  /* LOCAL chip — marks algo_order rows that never reached Kite (preflight blocks). */
  .oes-local-chip {
    font-size: 0.52rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
    background: rgba(251,191,36,0.12);
    color: #fbbf24;
    border: 1px solid rgba(251,191,36,0.35);
  }

  /* Side pills */
  .oes-side {
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 0.1rem 0.3rem;
    border-radius: 2px;
  }
  .oes-side-buy  { background: rgba(74,222,128,0.12); color: #4ade80; border: 1px solid rgba(74,222,128,0.35); }
  .oes-side-sell { background: rgba(248,113,113,0.12); color: #f87171; border: 1px solid rgba(248,113,113,0.35); }

  /* ── Bottom panel (Log / Orders) ──────────────────────────────────── */
  .oes-bottom-panel {
    border-top: 1px solid rgba(255,255,255,0.10);
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    flex-shrink: 0;
  }
  .oes-bottom-tabs {
    display: flex;
    gap: 0;
    padding: 0 0.75rem;
    border-bottom: 1px solid rgba(255,255,255,0.07);
  }
  .oes-bottom-tab {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.35rem 0.65rem;
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
    font-size: 0.6rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #94a3b8;
    cursor: pointer;
    white-space: nowrap;
    transition: color 0.12s, border-color 0.12s;
  }
  .oes-bottom-tab.active {
    color: #c084fc;
    border-bottom-color: #c084fc;
    font-weight: 800;
  }
  .oes-bottom-tab:hover:not(.active) { color: #c8d8f0; }
  .oes-bottom-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.0rem;
    height: 1.0rem;
    padding: 0 0.2rem;
    border-radius: 999px;
    background: rgba(192,132,252,0.22);
    border: 1px solid rgba(192,132,252,0.55);
    color: #c084fc;
    font-size: 0.52rem;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    line-height: 1;
  }
  .oes-bottom-body {
    max-height: 14rem;
    overflow-y: auto;
    padding: 0.45rem 0.75rem;
  }
  /* Height class passed to UnifiedLog inside the bottom panel.
     cardMode is on (see the {#if _bottomTab === 'log'} block) so
     UnifiedLog uses its .ul-card layout; per-row font overrides
     would conflict with the card styling and are unnecessary. */
  :global(.oes-bottom-scroll) {
    max-height: 13rem;
    padding: 0.1rem 0.25rem;
  }
</style>
