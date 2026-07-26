<script>
  // Top-of-book depth ladder for the order ticket.
  //
  // Polls `GET /api/quote?exchange=…&tradingsymbol=…` every 2 s
  // while mounted. Backend wraps `kite.quote()` and returns LTP +
  // top-5 buy/sell depth (already shipped before phase 2). When
  // the broker call fails (off-hours, illiquid), the row falls
  // back to em-dashes — the ticket still functions, the ladder
  // just shows "no depth".

  import { onMount, onDestroy } from 'svelte';
  import { fetchQuote } from '$lib/api';
  import { priceFmt, qtyFmt } from '$lib/format';

  /** @type {{
   *   symbol: string,
   *   exchange?: string,
   *   onQuote?: (q: any) => void,
   *   refreshKey?: number,
   *   paused?: boolean,
   * }} */
  let { symbol, exchange = 'NFO', onQuote = null, refreshKey = 0, paused = false } = $props();

  /** @type {{ ltp: number, bid: number|null, ask: number|null, depth_buy: any[], depth_sell: any[], depth_total_buy?: number|null, depth_total_sell?: number|null, oi?: number|null, volume?: number|null, ohlc?: { close?: number } | null } | null} */
  let q = $state(null);
  /** @type {string} */
  let err = $state('');
  // In-flight race guard: each poll() call claims a generation id.
  // When the response arrives we only write `q` if the id still
  // matches — prevents a slow response from the OLD symbol overwriting
  // data already populated by the NEW symbol's first poll.
  let _pollGen = 0;

  async function poll() {
    if (!symbol) return;
    const gen = ++_pollGen;
    try {
      const result = await fetchQuote(exchange || 'NFO', symbol);
      if (gen !== _pollGen) return;   // stale response — discard
      if (result) {
        // Only update state when we got a valid response.
        // When the response is falsy (null / empty), keep the last
        // known depth visible and set an error indicator instead —
        // this prevents a momentary null from blanking the ladder.
        q = result;
        err = '';
        try { onQuote?.(q); } catch (_) { /* ignore */ }
      } else {
        // Falsy response: surface the error but preserve stale q.
        err = 'no quote';
      }
    } catch (e) {
      if (gen !== _pollGen) return;
      err = /** @type {any} */ (e)?.message || 'depth unavailable';
      // Keep stale q — do not null it. Operator sees last-known
      // depth with the error indicator until a good poll lands.
    }
  }

  // Audit fix — consolidate timer lifecycle into a single `$effect` so
  // the visibility handler can't race the paused-effect on `timer`.
  // Symbol-change fix: Svelte 5 $effect cleanup pattern — when the
  // `symbol` reactive dependency changes, the cleanup fn runs first
  // (clearing the old interval), then the body re-runs immediately
  // with the new symbol. No `_prevSymbol` tracking needed.
  let _hidden = $state(typeof document !== 'undefined' && document.hidden);
  function _onVisibilityChange() {
    _hidden = !!document.hidden;
  }

  onMount(() => {
    // Regression-audit fix: sync `_hidden` once at mount so a page
    // loaded with the tab already in the background doesn't sit there
    // polling depth quietly until the first visibilitychange event.
    _hidden = !!document.hidden;
    document.addEventListener('visibilitychange', _onVisibilityChange);
  });
  onDestroy(() => {
    document.removeEventListener('visibilitychange', _onVisibilityChange);
  });

  // Single lifecycle effect — start polling iff: have symbol, not
  // paused by host, not hidden. Stop otherwise.
  // Svelte 5 cleanup return: when symbol / paused / _hidden changes,
  // the returned cleanup fn fires (clearInterval) BEFORE the next run,
  // so a symbol change immediately starts polling the new symbol
  // without having to wait for the old 2s interval to tick first.
  $effect(() => {
    if (!symbol || paused || _hidden) return;
    poll();
    const t = setInterval(poll, 2000);
    return () => clearInterval(t);
  });

  // Host-triggered refresh — when the host increments refreshKey we
  // re-poll immediately so depth always reflects the latest tick on
  // tab activation / modal re-open. Skipped when key is still 0
  // (initial render; the effect above handles the first fetch).
  $effect(() => {
    if (refreshKey > 0 && !paused) poll();
  });

  // 5-row scaffold filled from the response. Shorter arrays pad
  // with `null` so the rows stay aligned visually.
  /** @param {any[]} arr */
  function pad(arr) {
    const out = [];
    for (let i = 0; i < 5; i++) out.push(arr?.[i] || null);
    return out;
  }
  const buyRows  = $derived(pad(q?.depth_buy));
  const sellRows = $derived(pad(q?.depth_sell));

  // B4: bid-ask spread from top-of-book.
  const _spread = $derived.by(() => {
    const b = q?.depth_buy?.[0]?.price;
    const a = q?.depth_sell?.[0]?.price;
    if (b == null || a == null || !(a > 0) || !(b > 0)) return null;
    return a - b;
  });

  // B2/B3: OI + Volume display — format in Indian lakh notation.
  // 1,00,000 → "1L", 1,23,456 → "1.23L", 12,34,567 → "12.35L"
  /** @param {number|null|undefined} n */
  function fmtLakh(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v <= 0) return '—';
    if (v >= 1e7) return (v / 1e7).toFixed(2).replace(/\.?0+$/, '') + 'Cr';
    if (v >= 1e5) return (v / 1e5).toFixed(2).replace(/\.?0+$/, '') + 'L';
    if (v >= 1e3) return (v / 1e3).toFixed(1).replace(/\.0$/, '') + 'K';
    return String(Math.round(v));
  }
</script>

<div class="ot-depth">
  <!-- Operator: "I don't want to see DEPTH · CRUDEOIL26JUNFUT · MCX
       in market depth". Dropped the prefix. LTP / Prev chips kept
       since they're useful market context above the bid/ask ladder. -->
  {#if (q && q.ltp) || err}
    <div class="ot-depth-h">
      {#if q && q.ltp}
        <span class="ot-depth-ltp">LTP ₹{priceFmt(q.ltp)}</span>
        {#if q.ohlc?.close && q.ohlc.close > 0}
          <span class="ot-depth-prev">Prev ₹{priceFmt(q.ohlc.close)}</span>
        {/if}
      {:else if err}
        <span class="ot-depth-meta">{err}</span>
      {/if}
      {#if err && q}
        <span class="ot-depth-stale" title="Showing last known depth">stale</span>
      {/if}
    </div>
  {/if}

  <!-- B2/B3/B4: OI · Volume · Spread stats row -->
  {#if q && (q.oi != null || q.volume != null || _spread != null)}
    <div class="ot-depth-stats">
      {#if q.oi != null && q.oi > 0}
        <span class="ot-depth-stat">
          <span class="ot-depth-stat-lbl">OI</span>
          <span class="ot-depth-stat-val">{fmtLakh(q.oi)}</span>
        </span>
      {/if}
      {#if q.volume != null && q.volume > 0}
        <span class="ot-depth-stat">
          <span class="ot-depth-stat-lbl">Vol</span>
          <span class="ot-depth-stat-val">{fmtLakh(q.volume)}</span>
        </span>
      {/if}
      {#if _spread != null && _spread >= 0}
        <span class="ot-depth-stat">
          <span class="ot-depth-stat-lbl">Spd</span>
          <span class="ot-depth-stat-val ot-depth-spread">₹{priceFmt(_spread)}</span>
        </span>
      {/if}
    </div>
  {/if}
  <div class="ot-depth-grid">
    <span class="ot-depth-label">Bid qty</span>
    <span class="ot-depth-label">Bid</span>
    <span class="ot-depth-label">Ask</span>
    <span class="ot-depth-label">Ask qty</span>
    {#each buyRows as b, i (i)}
      {@const a = sellRows[i]}
      <span class="ot-depth-cell ot-depth-bid-qty">{b ? qtyFmt(b.quantity) : '—'}</span>
      <span class="ot-depth-cell ot-depth-bid">{b ? '₹' + priceFmt(b.price) : '—'}</span>
      <span class="ot-depth-cell ot-depth-ask">{a ? '₹' + priceFmt(a.price) : '—'}</span>
      <span class="ot-depth-cell ot-depth-ask-qty">{a ? qtyFmt(a.quantity) : '—'}</span>
    {/each}
  </div>
</div>

<style>
  .ot-depth {
    margin-top: 0.4rem;
    padding: 0.45rem 0.5rem;
    background: rgba(0,0,0,0.18);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 3px;
    /* Match the Chain-tab strike grid height when the parent (the
       order modal) advertises --chain-depth-h. The depth content
       sits at the top; the remaining vertical space pads out the
       frame so the modal's body stays the same size on Ticket ↔
       Chain tab flip. The variable falls back to `auto` so
       standalone callers (where OrderDepth is rendered outside
       SymbolPanel) keep their natural ~5-row height. */
    min-height: var(--chain-depth-h, auto);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
  }
  .ot-depth-h {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.4rem;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.3rem;
  }
  .ot-depth-meta {
    color: var(--algo-muted);
    font-style: italic;
    font-size: var(--fs-2xs);
    text-transform: none;
    letter-spacing: 0;
    opacity: 0.7;
  }
  .ot-depth-ltp {
    color: var(--algo-amber, var(--c-action));
    font-weight: 700;
    font-size: var(--fs-sm);
    text-transform: none;
    letter-spacing: 0;
  }
  /* Prev close anchor right beside LTP — neutral cyan, lighter
     weight so the eye reads LTP first (the live number) and PREV
     second (the reference). */
  .ot-depth-prev {
    color: var(--algo-sky, #7dd3fc);
    font-weight: 600;
    font-size: var(--fs-sm);
    text-transform: none;
    letter-spacing: 0;
    margin-left: 0.5rem;
  }
  .ot-depth-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1fr;
    gap: 0.15rem 0.4rem;
    font-family: monospace;
    /* Audit fix — explicit tabular-nums on the price/qty cells. Monospace
       covers digit-width consistency in most faces, but `tabular-nums`
       is the canonical spec per the CLAUDE.md number-formatting rule. */
    font-variant-numeric: tabular-nums;
    font-size: var(--fs-sm);
  }
  .ot-depth-label {
    font-size: var(--fs-2xs);
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    text-align: right;
  }
  .ot-depth-cell {
    text-align: right;
    color: var(--algo-slate);
  }
  .ot-depth-bid     { color: var(--algo-green, var(--c-long)); }
  .ot-depth-bid-qty { color: var(--algo-green, var(--c-long)); opacity: 0.7; }
  .ot-depth-ask     { color: var(--algo-red, var(--c-short)); }
  .ot-depth-ask-qty { color: var(--algo-red, var(--c-short)); opacity: 0.7; }

  /* Stale indicator — shows when error exists but old q is preserved */
  .ot-depth-stale {
    font-size: var(--fs-2xs);
    color: var(--algo-amber, var(--c-action));
    opacity: 0.7;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    margin-left: auto;
  }

  /* B2/B3/B4: OI · Volume · Spread stats strip */
  .ot-depth-stats {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin-bottom: 0.3rem;
    font-size: var(--fs-2xs);
  }
  .ot-depth-stat {
    display: inline-flex;
    align-items: baseline;
    gap: 0.2rem;
  }
  .ot-depth-stat-lbl {
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .ot-depth-stat-val {
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
    font-family: monospace;
  }
  .ot-depth-spread {
    color: var(--algo-sky, #7dd3fc);
  }
</style>
