<script>
  // SymbolChartModal — overlay that charts any symbol's historical
  // OHLCV bars. Triggered by the SymbolActions ⋯ → 📈 Chart item
  // wired from every list (MarketPulse rows, Options candidates,
  // future surfaces). Reads from /api/options/historical (Kite
  // candles, 24h-cached on the backend) — so the modal works for
  // index options, stock options, MCX commodity options, futures,
  // and bare equities alike.
  //
  // Why a separate modal instead of opening /admin/options?symbol=…:
  // operators want a one-click "show me what this thing looks like"
  // affordance from any list, without losing their place. The modal
  // covers the page until dismissed; an Open-in-Options link still
  // sits at the bottom for deeper analysis.

  import { onMount, onDestroy } from 'svelte';
  import { fetchOptionsHistorical } from '$lib/api';
  import { priceFmt } from '$lib/format';

  /** @type {{
   *   symbol:    string,
   *   exchange?: string,
   *   open?:     boolean,
   *   onClose?:  () => void,
   * }} */
  const { symbol = '', exchange = '',
          open = false, onClose = () => {} } = $props();

  /** @type {Array<{ts: string, open: number, high: number, low: number, close: number, volume: number}>} */
  let _bars = $state([]);
  let _loading = $state(false);
  let _error = $state('');

  // ── Geometry ─────────────────────────────────────────────────────
  const W = 720;
  const H = 360;
  const PAD_L = 56;
  const PAD_R = 16;
  const PAD_T = 16;
  const PAD_B = 30;
  const innerW = W - PAD_L - PAD_R;
  const innerH = H - PAD_T - PAD_B;

  async function _load() {
    if (!symbol) return;
    _loading = true; _error = '';
    try {
      const r = await fetchOptionsHistorical(symbol, { days: 30, exchange });
      _bars = Array.isArray(r?.bars) ? r.bars : [];
      if (!_bars.length) _error = 'No bars available — broker may not list this contract.';
    } catch (e) {
      _error = /** @type {any} */ (e)?.message || String(e);
      _bars = [];
    } finally {
      _loading = false;
    }
  }

  // Re-fetch when the symbol changes while open.
  $effect(() => {
    void symbol; void open;
    if (open && symbol) _load();
    else if (!open) { _bars = []; _error = ''; }
  });

  // Esc to close. document-level so it works regardless of focus.
  function _onKey(/** @type {KeyboardEvent} */ ev) {
    if (open && ev.key === 'Escape') {
      ev.preventDefault();
      onClose();
    }
  }
  onMount(() => { document.addEventListener('keydown', _onKey); });
  onDestroy(() => { document.removeEventListener('keydown', _onKey); });

  // ── Domains ──────────────────────────────────────────────────────
  const _xs = $derived(_bars.map((b) => Date.parse(b.ts)).filter(Number.isFinite));
  const _xDomain = $derived(_xs.length
    ? { lo: Math.min(..._xs), hi: Math.max(..._xs) }
    : null);
  const _yDomain = $derived.by(() => {
    if (!_bars.length) return { lo: 0, hi: 1 };
    let lo = Infinity, hi = -Infinity;
    for (const b of _bars) {
      const l = Number(b.low), h = Number(b.high);
      if (Number.isFinite(l)) lo = Math.min(lo, l);
      if (Number.isFinite(h)) hi = Math.max(hi, h);
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { lo: 0, hi: 1 };
    const pad = (hi - lo) * 0.06 || 1;
    return { lo: lo - pad, hi: hi + pad };
  });

  const xOf = (/** @type {number} */ ts) => {
    if (!_xDomain) return PAD_L;
    if (_xDomain.hi === _xDomain.lo) return PAD_L + innerW / 2;
    return PAD_L + ((ts - _xDomain.lo) / (_xDomain.hi - _xDomain.lo)) * innerW;
  };
  const yOf = (/** @type {number} */ v) => {
    return PAD_T + ((_yDomain.hi - v) / (_yDomain.hi - _yDomain.lo)) * innerH;
  };

  const _linePath = $derived.by(() => {
    if (!_bars.length || !_xDomain) return '';
    let d = '';
    for (let i = 0; i < _bars.length; i++) {
      const t = Date.parse(_bars[i].ts);
      if (!Number.isFinite(t)) continue;
      const x = xOf(t);
      const y = yOf(Number(_bars[i].close));
      d += (i === 0 ? `M${x.toFixed(2)},${y.toFixed(2)}`
                    : ` L${x.toFixed(2)},${y.toFixed(2)}`);
    }
    return d;
  });

  // y-axis ticks (5 evenly spaced)
  const _yTicks = $derived.by(() => {
    if (!_bars.length) return [];
    const out = [];
    const step = (_yDomain.hi - _yDomain.lo) / 4;
    for (let i = 0; i <= 4; i++) out.push(_yDomain.lo + i * step);
    return out;
  });
  // x-axis labels (5 evenly spaced dates)
  const _xLabels = $derived.by(() => {
    if (!_xDomain) return [];
    const out = [];
    for (let i = 0; i < 5; i++) {
      const t = _xDomain.lo + ((_xDomain.hi - _xDomain.lo) * i) / 4;
      const d = new Date(t);
      const mm = String(d.getMonth() + 1).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      out.push({ x: xOf(t), label: `${dd}/${mm}` });
    }
    return out;
  });

  // Hover crosshair.
  /** @type {{x:number,y:number,bar:any}|null} */
  let _hover = $state(null);
  function _onPointerMove(/** @type {PointerEvent} */ ev) {
    if (!_bars.length || !_xDomain) { _hover = null; return; }
    const svg = /** @type {SVGSVGElement} */ (ev.currentTarget);
    const rect = svg.getBoundingClientRect();
    const xRel = ((ev.clientX - rect.left) / rect.width) * W;
    const tMs = _xDomain.lo + ((xRel - PAD_L) / innerW) * (_xDomain.hi - _xDomain.lo);
    let best = _bars[0], bestD = Infinity;
    for (const b of _bars) {
      const d = Math.abs(Date.parse(b.ts) - tMs);
      if (d < bestD) { bestD = d; best = b; }
    }
    const tx = Date.parse(best.ts);
    _hover = { x: xOf(tx), y: yOf(Number(best.close)), bar: best };
  }
  function _onPointerLeave() { _hover = null; }

  // Pct change first → last
  const _firstClose = $derived(_bars[0]?.close);
  const _lastClose = $derived(_bars[_bars.length - 1]?.close);
  const _pct = $derived(
    (_firstClose && _lastClose)
      ? ((_lastClose - _firstClose) / _firstClose) * 100
      : null,
  );
</script>

{#if open && symbol}
  <div class="scm-overlay" role="dialog" aria-modal="true"
       aria-label={`Chart for ${symbol}`}
       onclick={onClose}>
    <div class="scm-card" onclick={(e) => e.stopPropagation()}>
      <div class="scm-header">
        <span class="scm-title">{symbol}</span>
        {#if exchange}<span class="scm-exch">{exchange}</span>{/if}
        {#if _lastClose != null}
          <span class="scm-last">₹{priceFmt(_lastClose)}</span>
        {/if}
        {#if _pct != null}
          <span class="scm-pct {_pct >= 0 ? 'up' : 'down'}">
            {_pct >= 0 ? '+' : ''}{_pct.toFixed(2)}%
          </span>
        {/if}
        <span class="scm-meta">{_bars.length} bars · last 30d</span>
        <button type="button" class="scm-close"
                aria-label="Close chart" title="Close (Esc)"
                onclick={onClose}>×</button>
      </div>

      {#if _loading}
        <div class="scm-state">Loading bars…</div>
      {:else if _error}
        <div class="scm-state scm-error">{_error}</div>
      {:else if !_bars.length}
        <div class="scm-state">No bars to plot.</div>
      {:else}
        <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" class="scm-svg"
             onpointermove={_onPointerMove} onpointerleave={_onPointerLeave}>
          <!-- y-axis grid + labels -->
          {#each _yTicks as v}
            <line x1={PAD_L} x2={W - PAD_R} y1={yOf(v)} y2={yOf(v)}
                  stroke="rgba(200,216,240,0.08)" stroke-width="0.7" stroke-dasharray="2 3" />
            <text x={PAD_L - 6} y={yOf(v) + 3} text-anchor="end"
                  fill="#7e97b8" font-size="10">₹{priceFmt(v)}</text>
          {/each}
          <!-- x-axis labels -->
          {#each _xLabels as l}
            <line x1={l.x} x2={l.x} y1={PAD_T} y2={H - PAD_B}
                  stroke="rgba(200,216,240,0.07)" stroke-width="0.7" stroke-dasharray="2 3" />
            <text x={l.x} y={H - PAD_B + 14} text-anchor="middle"
                  fill="#7e97b8" font-size="10">{l.label}</text>
          {/each}

          <!-- Close-price line -->
          <path d={_linePath} fill="none"
                stroke="#fbbf24" stroke-width="1.8"
                stroke-linejoin="round" stroke-linecap="round" />

          <!-- Hover crosshair + dot + tooltip -->
          {#if _hover}
            <line x1={_hover.x} x2={_hover.x} y1={PAD_T} y2={H - PAD_B}
                  stroke="rgba(251,191,36,0.5)" stroke-width="1" stroke-dasharray="3 2" />
            <circle cx={_hover.x} cy={_hover.y} r="3"
                    fill="#fbbf24" stroke="#fff" stroke-width="1" />
            {@const _tx = Math.min(W - 140 - PAD_R, Math.max(PAD_L, _hover.x + 8))}
            {@const _ty = Math.max(PAD_T + 4, _hover.y - 60)}
            <rect x={_tx} y={_ty} width="140" height="56" rx="3"
                  fill="#1d2a44" stroke="rgba(251,191,36,0.4)" stroke-width="1" />
            <text x={_tx + 6} y={_ty + 14} fill="#fbbf24"
                  font-size="10" font-weight="800" font-family="monospace">
              {_hover.bar.ts.slice(0, 10)}
            </text>
            <text x={_tx + 6} y={_ty + 26} fill="#c8d8f0"
                  font-size="9" font-family="monospace">
              O ₹{priceFmt(_hover.bar.open)}  H ₹{priceFmt(_hover.bar.high)}
            </text>
            <text x={_tx + 6} y={_ty + 38} fill="#c8d8f0"
                  font-size="9" font-family="monospace">
              L ₹{priceFmt(_hover.bar.low)}  C ₹{priceFmt(_hover.bar.close)}
            </text>
            <text x={_tx + 6} y={_ty + 50} fill="#7e97b8"
                  font-size="9" font-family="monospace">
              Vol {Number(_hover.bar.volume || 0).toLocaleString()}
            </text>
          {/if}
        </svg>
      {/if}

      <div class="scm-footer">
        <a class="scm-deep" href={`/admin/options?symbol=${encodeURIComponent(symbol)}`}>
          Open in Options →
        </a>
        <span class="scm-hint">Esc · click outside to close</span>
      </div>
    </div>
  </div>
{/if}

<style>
  .scm-overlay {
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
    backdrop-filter: blur(2px);
  }
  .scm-card {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251, 191, 36, 0.45);
    border-radius: 6px;
    width: min(880px, 100%);
    max-height: calc(100vh - 2rem);
    box-shadow: 0 10px 32px rgba(0, 0, 0, 0.6);
    padding: 0.65rem 0.85rem;
    display: flex;
    flex-direction: column;
  }
  .scm-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    margin-bottom: 0.4rem;
  }
  .scm-title {
    color: #fbbf24;
    font-weight: 800;
    font-size: 0.9rem;
    letter-spacing: 0.02em;
  }
  .scm-exch {
    color: #7e97b8;
    background: rgba(126, 151, 184, 0.15);
    border: 1px solid rgba(126, 151, 184, 0.32);
    padding: 0.06rem 0.32rem;
    border-radius: 2px;
    font-size: 0.55rem;
    letter-spacing: 0.06em;
  }
  .scm-last {
    color: #f1f7ff;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }
  .scm-pct {
    padding: 0.1rem 0.4rem;
    border-radius: 2px;
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .scm-pct.up   { color: #4ade80; background: rgba(74, 222, 128, 0.12); }
  .scm-pct.down { color: #f87171; background: rgba(248, 113, 113, 0.12); }
  .scm-meta {
    color: #7e97b8;
    margin-left: auto;
    font-size: 0.58rem;
  }
  .scm-close {
    background: transparent;
    border: 1px solid rgba(251, 191, 36, 0.4);
    color: #fbbf24;
    padding: 0 0.55rem;
    border-radius: 3px;
    cursor: pointer;
    font-size: 1rem;
    line-height: 1;
  }
  .scm-close:hover {
    background: rgba(251, 191, 36, 0.10);
  }
  .scm-svg {
    width: 100%;
    display: block;
    cursor: crosshair;
  }
  .scm-state {
    text-align: center;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    padding: 3rem 1rem;
  }
  .scm-state.scm-error {
    color: #fda4a4;
  }
  .scm-footer {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.35rem;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
  }
  .scm-deep {
    color: #7dd3fc;
    text-decoration: none;
    font-weight: 700;
    padding: 0.1rem 0.35rem;
    border: 1px solid rgba(125, 211, 252, 0.4);
    border-radius: 2px;
  }
  .scm-deep:hover {
    background: rgba(125, 211, 252, 0.10);
  }
  .scm-hint {
    color: #7e97b8;
    margin-left: auto;
  }
</style>
