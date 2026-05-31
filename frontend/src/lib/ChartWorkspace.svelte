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

  import { onMount, onDestroy, getContext } from 'svelte';
  import {
    fetchOptionsHistorical,
    fetchChartPriceHistory,
    fetchSimStatus,
    fetchPaperStatus,
    fetchStrategyAnalytics,
    fetchChartSymbols,
  } from '$lib/api';
  import { loadInstruments, searchByPrefix, suggestUnderlyings } from '$lib/data/instruments';
  import { nowStamp, visibleInterval } from '$lib/stores';
  import { priceFmt } from '$lib/format';
  import InfoHint from '$lib/InfoHint.svelte';

  let {
    symbol        = $bindable(''),
    exchange      = '',
    mode          = $bindable(/** @type {'live'|'sim'|'paper'} */('live')),
    compact       = false,
    showHeader    = true,
    bump          = 0,
    onSymbolChange = /** @type {((sym: string) => void) | undefined} */ (undefined),
  } = $props();

  // ── Demo gate — skip status polling on anonymous prod sessions ────
  const _algoStatus = getContext('algoStatus');
  const _isDemo = $derived(_algoStatus?.isDemo ?? false);

  // ── Symbol search ─────────────────────────────────────────────────
  // Always bind input to _symQuery. On mount + external symbol change,
  // sync _symQuery = symbol. Never revert to symbol on blur — the
  // operator's deliberate clear stays cleared.
  let _symQuery       = $state('');
  let _symOpen        = $state(false);
  let _symSuggestions = $state(/** @type {any[]} */ ([]));
  let _symDebounce    = /** @type {any} */ (null);

  // Sync _symQuery when the symbol prop changes externally (initial mount
  // or parent-driven symbol swap).
  $effect(() => {
    // Reading `symbol` makes this reactive to external symbol changes.
    const s = symbol;
    if (s && s !== _symQuery) _symQuery = s;
  });

  async function _onSymInput(/** @type {string} */ v) {
    _symQuery = v;
    _symOpen = true;
    if (!v) { _symSuggestions = []; return; }
    try {
      const sync = suggestUnderlyings(v, 16);
      if (Array.isArray(sync) && sync.length) {
        _symSuggestions = sync.map(s => ({ sym: s, e: '', t: 'EQ' }));
      }
    } catch (_) { /* sync path failed */ }
    if (_symDebounce) clearTimeout(_symDebounce);
    _symDebounce = setTimeout(async () => {
      try {
        await loadInstruments();
        const full = await searchByPrefix(v, 16);
        if (Array.isArray(full) && full.length) _symSuggestions = full;
      } catch (_) { /* keep sync result */ }
    }, 60);
  }

  function _pickSym(/** @type {any} */ inst) {
    const sym = String(inst?.sym || inst?.tradingsymbol || _symQuery).toUpperCase();
    // Set _symQuery to the picked symbol so input stays populated after pick.
    _symQuery = sym;
    _symOpen = false;
    _symSuggestions = [];
    symbol = sym;
    onSymbolChange?.(sym);
    _chartLoaded = false;
    _intradayEnabled = false;
    _loadHistorical(true);
  }

  function _handleSymKeydown(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') { _symOpen = false; _symSuggestions = []; }
    if (e.key === 'Enter' && _symSuggestions.length) _pickSym(_symSuggestions[0]);
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
  let _chartType   = $state(/** @type {'line'|'area'|'candle'} */('line'));
  let _showSma20   = $state(false);
  let _showSma50   = $state(false);
  let _showVol     = $state(false);

  /** @type {Array<{ts:string,close:number}>} */
  let _spotBars = $state([]);

  async function _loadHistorical(/** @type {boolean} */ force = false) {
    if (!symbol) return;
    if (!force && _chartLoaded) return;
    _histLoading = true; _histError = '';
    try {
      const promises = [
        fetchOptionsHistorical(symbol, { days: _chartDays, exchange: exchange || undefined }),
      ];
      if (_isDerivative && _underlying) {
        promises.push(
          fetchOptionsHistorical(_underlying, { days: _chartDays })
            .catch(() => ({ bars: [] }))
        );
      }
      const [hist, spotHist] = await Promise.all(promises);
      _bars     = Array.isArray(hist?.bars) ? hist.bars : [];
      _spotBars = spotHist ? (Array.isArray(spotHist.bars) ? spotHist.bars : []) : [];
      if (!_bars.length) _histError = 'No data available.';
      _chartLoaded = true;
    } catch (e) {
      _histError = /** @type {any} */ (e)?.message || 'Load failed';
      _bars = [];
    } finally {
      _histLoading = false;
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
  let _intradayEnabled = $state(false);
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
  const _innerW = $derived(_chartW - CPAD_L - CPAD_R);
  const _innerH = $derived(_chartH - CPAD_T - CPAD_B);

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
  const _linePath = $derived.by(() => {
    if (!_bars.length) return '';
    let d = '';
    for (let i = 0; i < _bars.length; i++) {
      const t = Date.parse(_bars[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = _xOf(t), y = _yOf(Number(_bars[i].close));
      d += (d === '' ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  });

  const _areaPath = $derived.by(() => {
    if (!_bars.length) return '';
    const last  = _bars[_bars.length - 1];
    const lastT = Date.parse(last.ts);
    if (!Number.isFinite(lastT)) return '';
    const base  = (_chartH - CPAD_B).toFixed(2);
    const firstT = Date.parse(_bars[0].ts);
    return `${_linePath} L${_xOf(lastT).toFixed(2)},${base} L${_xOf(firstT).toFixed(2)},${base} Z`;
  });

  const _candles = $derived.by(() => {
    if (!_bars.length) return [];
    const n    = _bars.length;
    const slot = n > 1 ? _innerW / (n - 1) : _innerW;
    const w    = Math.max(2, Math.min(12, slot * 0.6));
    /** @type {Array<{x:number,bodyY:number,bodyH:number,w:number,wickTop:number,wickBot:number,up:boolean}>} */
    const out = [];
    for (const b of _bars) {
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
    if (!_bars.length || !_showVol) return [];
    let vMax = 0;
    for (const b of _bars) { const v = Number(b.volume || 0); if (v > vMax) vMax = v; }
    if (vMax <= 0) return [];
    const n        = _bars.length;
    const slot     = n > 1 ? _innerW / (n - 1) : _innerW;
    const w        = Math.max(2, Math.min(10, slot * 0.55));
    const baseline = _chartH - CPAD_B;
    /** @type {Array<{x:number,y:number,h:number,w:number,up:boolean}>} */
    const out = [];
    for (const b of _bars) {
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
  /** @type {{x:number,y:number,bar:any}|null} */
  let _chartHover = $state(null);

  function _onChartPointerMove(/** @type {PointerEvent} */ e) {
    if (!_bars.length) { _chartHover = null; return; }
    const svg  = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const xRel = ((e.clientX - rect.left) / rect.width) * _chartW;
    const tMs  = _xMin + ((xRel - CPAD_L) / _innerW) * _xSpan;
    let best = _bars[0], bestD = Infinity;
    for (const b of _bars) {
      const d = Math.abs(Date.parse(b.ts) - tMs);
      if (d < bestD) { bestD = d; best = b; }
    }
    const tx = Date.parse(best.ts);
    _chartHover = { x: _xOf(tx), y: _yOf(Number(best.close)), bar: best };
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
    _chartDays === 7   ? '1W' :
    _chartDays === 30  ? '1M' :
    _chartDays === 90  ? '3M' :
    _chartDays === 180 ? '6M' :
    _chartDays === 365 ? '1Y' : `${_chartDays}D`
  );

  // ── Greeks formatting helpers ─────────────────────────────────────
  function _gv(/** @type {number|null|undefined} */ v, /** @type {number} */ dp = 2) {
    if (v == null || !Number.isFinite(v)) return '—';
    return v.toFixed(dp);
  }

  // ── Lifecycle ─────────────────────────────────────────────────────
  let _mounted = true;

  onMount(async () => {
    await loadInstruments().catch(() => {});
    await _loadHistorical();
    if (!_isDemo) {
      await _pollStatus();
      _statusTimer = visibleInterval(_pollStatus, 5000);
    }
    if (_isOption) _loadGreeks();
  });

  onDestroy(() => {
    _mounted = false;
    if (_symDebounce) { clearTimeout(_symDebounce); _symDebounce = null; }
    if (_statusTimer) { try { _statusTimer(); } catch (_) { clearInterval(_statusTimer); } }
    _stopTickPoll();
  });

  // Re-load historical when symbol changes externally.
  $effect(() => {
    void symbol; void exchange;
    if (_mounted) {
      _chartLoaded = false;
      zoom = null;
      _chartHover = null;
      _bars = [];
      _spotBars = [];
      _greeks = null;
      _intradayEnabled = false;
      _loadHistorical(true);
      if (_isOption) _loadGreeks();
    }
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
</script>

<div class="cw-root">
  <!-- Picker bar — symbol search (no mode pills) -->
  {#if !compact}
    <div class="cw-picker">
      <div class="cw-sym-wrap">
        <input
          class="cw-sym-input"
          type="text"
          placeholder="Symbol…"
          value={_symQuery}
          oninput={(e) => _onSymInput(/** @type {HTMLInputElement} */ (e.target).value)}
          onfocus={() => { /* keep _symQuery as-is */ }}
          onblur={() => {
            // Delay close so dropdown clicks register before the dropdown
            // disappears. Do NOT reset _symQuery — operator's clear is intentional.
            setTimeout(() => { _symOpen = false; }, 180);
          }}
          onkeydown={_handleSymKeydown}
          aria-label="Symbol search"
          autocomplete="off"
          spellcheck="false"
        />
        {#if _symOpen && _symSuggestions.length}
          <div class="cw-sym-dropdown" role="listbox">
            {#each _symSuggestions.slice(0, 14) as inst}
              <button type="button" class="cw-sym-opt" role="option" aria-selected="false"
                      onmousedown={(e) => { e.preventDefault(); _pickSym(inst); }}>
                <span class="cw-sym-sym">{inst.sym || inst.tradingsymbol}</span>
                {#if inst.e}<span class="cw-sym-exch">{inst.e}</span>{/if}
                {#if inst.t}<span class="cw-sym-type">{inst.t}</span>{/if}
              </button>
            {/each}
          </div>
        {/if}
      </div>

      {#if _isDerivative}
        <span class="cw-kind-pill cw-kind-deriv">F&O</span>
      {:else if symbol}
        <span class="cw-kind-pill cw-kind-equity">EQ</span>
      {/if}

      <!-- Auto-detected mode chip — informational, not clickable -->
      {#if _simActive || _paperActive}
        <span class="cw-auto-mode-chip cw-auto-mode-{_simActive ? 'sim' : 'paper'}"
              title="{_simActive ? 'Simulator active — intraday data from sim' : 'Paper orders in flight — intraday data from paper engine'}">
          {_simActive ? 'SIM' : 'PAPER'}
        </span>
      {/if}
    </div>
  {/if}

  <!-- Chart toolbar: Type | Range | Overlays | Intraday -->
  <div class="cw-toolbar">
    <div class="cw-ctrl-group">
      {#each ['line', 'area', 'candle'] as t}
        <button type="button" class="cw-pill" class:active={_chartType === t}
                onclick={() => _chartType = /** @type {'line'|'area'|'candle'} */ (t)}
                title="{t.charAt(0).toUpperCase() + t.slice(1)} chart">
          {t.charAt(0).toUpperCase() + t.slice(1)}
        </button>
      {/each}
    </div>
    <div class="cw-ctrl-sep" aria-hidden="true"></div>
    <div class="cw-ctrl-group">
      {#each /** @type {[number,string][]} */ ([[7,'1W'],[30,'1M'],[90,'3M'],[180,'6M'],[365,'1Y']]) as [d, label]}
        <button type="button" class="cw-pill" class:active={_chartDays === d}
                onclick={() => _setRange(/** @type {number} */ (d))}
                title="Past {label}">
          {label}
        </button>
      {/each}
    </div>
    <div class="cw-ctrl-sep" aria-hidden="true"></div>
    <div class="cw-ctrl-group">
      <button type="button" class="cw-pill" class:active={_showSma20}
              onclick={() => _showSma20 = !_showSma20}
              title="20-period SMA">SMA20</button>
      <button type="button" class="cw-pill" class:active={_showSma50}
              onclick={() => _showSma50 = !_showSma50}
              title="50-period SMA">SMA50</button>
      <button type="button" class="cw-pill" class:active={_showVol}
              onclick={() => _showVol = !_showVol}
              title="Volume bars">Vol</button>
    </div>
    {#if !compact}
      <div class="cw-ctrl-sep" aria-hidden="true"></div>
      <div class="cw-ctrl-group">
        <button type="button" class="cw-pill cw-intraday-pill"
                class:active={_intradayEnabled}
                onclick={() => _intradayEnabled = !_intradayEnabled}
                title="Show live intraday tick chart below">
          Intraday{_intradayEnabled ? (_simActive ? ' (SIM)' : _paperActive ? ' (PAPER)' : ' (LIVE)') : ''}
        </button>
      </div>
    {/if}
    {#if isZoomed}
      <button type="button" class="cw-reset-zoom" onclick={_resetZoom}
              title="Reset zoom — show full range">Reset</button>
    {/if}
  </div>

  <!-- Historical OHLCV chart — fills available height via flex -->
  <div class="cw-chart-container" bind:this={_chartContainerEl}>
    {#if _histLoading && !_bars.length}
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
        aria-label="Price chart — wheel to zoom, drag to pan"
        onwheel={_onWheel}
        onpointerdown={_onPointerDown}
        onpointerup={_onPointerUp}
        onpointermove={_onPointerMove}
        onpointerleave={() => { _chartHover = null; }}
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

        <!-- X-axis grid + labels -->
        {#each _xLabels as xl, i}
          {#if i > 0}
            <line x1={xl.x} x2={xl.x} y1={CPAD_T} y2={_chartH - CPAD_B}
                  stroke="rgba(200,216,240,0.07)" stroke-width="1" stroke-dasharray="2 3"/>
          {/if}
          <text x={xl.x} y={_chartH - CPAD_B + 14}
                text-anchor={i === 0 ? 'start' : (i === 4 ? 'end' : 'middle')}
                fill="#c8d8f0" font-size="11" font-weight="600">
            {xl.label}
          </text>
        {/each}

        <!-- X-axis baseline -->
        <line x1={CPAD_L} x2={_chartW - CPAD_R} y1={_chartH - CPAD_B} y2={_chartH - CPAD_B}
              stroke="rgba(255,255,255,0.18)" stroke-width="1"/>

        <!-- Volume bars (lower band) -->
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

        <!-- Price layer — line / area / candle -->
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

        <!-- Hover crosshair -->
        {#if _chartHover && !pan}
          <line x1={_chartHover.x} x2={_chartHover.x} y1={CPAD_T} y2={_chartH - CPAD_B}
                stroke="rgba(251,191,36,0.5)" stroke-width="1" stroke-dasharray="3 2"/>
          <circle cx={_chartHover.x} cy={_chartHover.y} r="3"
                  fill="#fbbf24" stroke="#fff" stroke-width="1"/>
          {@const _tx = Math.min(_chartW - 156 - CPAD_R, Math.max(CPAD_L, _chartHover.x + 8))}
          {@const _ty = Math.max(CPAD_T + 4, _chartHover.y - 68)}
          <rect x={_tx} y={_ty} width="156" height="66" rx="3"
                fill="#1d2a44" stroke="rgba(251,191,36,0.4)" stroke-width="1"/>
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
      <!-- Loading overlay — keeps prior bars visible during reload -->
      {#if _histLoading}
        <div class="cw-chart-spinner">Loading…</div>
      {/if}
    {/if}
  </div>

  <!-- Intraday tick chart (below historical, opt-in, fixed height) -->
  {#if _intradayEnabled}
    <div class="cw-intraday-section">
      <div class="cw-intraday-label">
        <span>Intraday ticks</span>
        <span class="cw-intraday-mode cw-mode-{mode}">{mode.toUpperCase()}</span>
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
        <svg viewBox="0 0 {W2} {H2}" preserveAspectRatio="none"
             class="cw-intraday-svg" role="img" aria-label="Intraday tick chart">
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
    <span class="cw-info-sym">{symbol || '—'}</span>
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
    border-radius: 4px;
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

  .cw-sym-wrap {
    position: relative;
  }
  .cw-sym-input {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(125,211,252,0.30);
    border-radius: 4px;
    color: #7dd3fc;
    font-family: monospace;
    font-size: 0.7rem;
    font-weight: 700;
    padding: 3px 8px;
    min-width: 10rem;
    outline: none;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .cw-sym-input:focus {
    border-color: rgba(125,211,252,0.65);
    background: rgba(125,211,252,0.08);
  }
  .cw-sym-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    z-index: 50;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.35);
    border-radius: 4px;
    min-width: 16rem;
    max-height: 18rem;
    overflow-y: auto;
    box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    margin-top: 2px;
  }
  .cw-sym-opt {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
    background: none;
    border: none;
    padding: 5px 10px;
    cursor: pointer;
    color: #c8d8f0;
    font-size: 0.65rem;
    font-family: monospace;
    text-align: left;
  }
  .cw-sym-opt:hover { background: rgba(251,191,36,0.12); }
  .cw-sym-sym  { font-weight: 700; color: #fbbf24; }
  .cw-sym-exch { color: #7e97b8; }
  .cw-sym-type {
    font-size: 0.55rem;
    padding: 1px 4px;
    border-radius: 2px;
    border: 1px solid rgba(255,255,255,0.15);
    color: #7e97b8;
  }

  .cw-kind-pill {
    font-family: monospace;
    font-size: 0.5rem;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 2px;
    letter-spacing: 0.05em;
  }
  .cw-kind-deriv  { background: rgba(251,191,36,0.10); color: #fbbf24; border: 1px solid rgba(251,191,36,0.35); }
  .cw-kind-equity { background: rgba(125,211,252,0.08); color: #7dd3fc; border: 1px solid rgba(125,211,252,0.30); }

  /* Auto-detected mode chip — informational badge, not interactive */
  .cw-auto-mode-chip {
    font-family: monospace;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    padding: 2px 6px;
    border-radius: 3px;
    border: 1px solid transparent;
    user-select: none;
  }
  .cw-auto-mode-sim   { color: #fbbf24; background: rgba(251,191,36,0.10); border-color: rgba(251,191,36,0.40); }
  .cw-auto-mode-paper { color: #7dd3fc; background: rgba(125,211,252,0.10); border-color: rgba(125,211,252,0.40); }

  /* ── Toolbar ─────────────────────────────────────────────── */
  .cw-toolbar {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.75rem;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    flex-wrap: wrap;
    row-gap: 0.25rem;
    flex-shrink: 0;
  }
  .cw-ctrl-group {
    display: flex;
    gap: 2px;
  }
  /* Visual separator between toolbar groups */
  .cw-ctrl-sep {
    width: 1px;
    height: 14px;
    background: rgba(255,255,255,0.12);
    flex-shrink: 0;
    align-self: center;
  }
  .cw-pill {
    font-family: monospace;
    font-size: 0.55rem;
    font-weight: 600;
    letter-spacing: 0.03em;
    padding: 2px 7px;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.04);
    color: #7e97b8;
    cursor: pointer;
    transition: all 0.1s;
  }
  .cw-pill:hover { background: rgba(251,191,36,0.10); border-color: rgba(251,191,36,0.30); color: #fbbf24; }
  .cw-pill.active {
    background: rgba(251,191,36,0.16);
    border-color: rgba(251,191,36,0.55);
    color: #fbbf24;
    font-weight: 700;
  }

  .cw-intraday-pill.active { background: rgba(125,211,252,0.14); border-color: rgba(125,211,252,0.45); color: #7dd3fc; }

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
    min-height: 0;      /* critical for flex children */
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

  /* Loading overlay — shown on top of prior bars while a reload is in flight */
  .cw-chart-spinner {
    position: absolute;
    top: 0.5rem;
    right: 0.75rem;
    color: #7e97b8;
    font-size: 0.55rem;
    font-family: monospace;
    background: rgba(13,24,41,0.7);
    padding: 2px 6px;
    border-radius: 3px;
    pointer-events: none;
  }

  /* ── State / empty ───────────────────────────────────────── */
  .cw-state {
    height: 100%;
    min-height: 160px;
    display: flex;
    align-items: center;
    justify-content: center;
    color: #7e97b8;
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
    color: #7e97b8;
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
  .cw-info-close { color: #c8d8f0; font-variant-numeric: tabular-nums; }
  .cw-info-pct   { font-variant-numeric: tabular-nums; font-weight: 700; }
  .cw-pos { color: #4ade80; }
  .cw-neg { color: #f87171; }
  .cw-info-meta { color: #7e97b8; }
  .cw-meta-text { color: #7e97b8; font-size: 0.55rem; font-family: monospace; }
  .cw-legend-dash {
    display: inline-block;
    width: 14px;
    height: 0;
    border-top: 1px dashed #7dd3fc;
    opacity: 0.7;
    vertical-align: middle;
    margin-right: 2px;
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
    color: #7e97b8;
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
    color: #7e97b8;
  }
  .cw-gk-val {
    font-family: monospace;
    font-size: 0.65rem;
    font-variant-numeric: tabular-nums;
    color: #c8d8f0;
  }
  .cw-gk-amber { color: #fbbf24; }
  .cw-gk-sky   { color: #7dd3fc; }

  @media (max-width: 600px) {
    .cw-picker      { gap: 0.35rem; padding: 0.4rem 0.5rem; }
    .cw-toolbar     { gap: 0.35rem; padding: 0.25rem 0.5rem; }
    .cw-info-strip  { padding: 0.25rem 0.5rem; gap: 0.4rem; }
    .cw-greeks-strip { padding: 0.3rem 0.5rem; gap: 0.5rem; }
    .cw-sym-input   { min-width: 7rem; }
    .cw-intraday-section { height: 30vh; }
  }
</style>
