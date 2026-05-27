<script>
  // Top-of-book depth ladder for the order ticket.
  //
  // Polls `GET /api/quote?exchange=…&tradingsymbol=…` every 1.2 s
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
   * }} */
  let { symbol, exchange = 'NFO', onQuote = null } = $props();

  /** @type {{ ltp: number, bid: number|null, ask: number|null, depth_buy: any[], depth_sell: any[], ohlc?: { close?: number } | null } | null} */
  let q = $state(null);
  /** @type {string} */
  let err = $state('');
  /** @type {ReturnType<typeof setInterval> | null} */
  let timer = null;

  async function poll() {
    if (!symbol) return;
    try {
      q   = await fetchQuote(exchange || 'NFO', symbol);
      err = '';
      // Bubble the fresh quote up so the OrderTicket can auto-fill
      // the limit price (BUY → top ask, SELL → top bid). Defensive
      // try/catch — a parent throwing must never break the depth
      // poll.
      try { onQuote?.(q); } catch (_) { /* ignore */ }
    } catch (e) {
      err = /** @type {any} */ (e)?.message || 'depth unavailable';
    }
  }

  onMount(() => {
    poll();
    timer = setInterval(poll, 1200);
  });
  onDestroy(() => { if (timer) clearInterval(timer); });

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
</script>

<div class="ot-depth">
  <div class="ot-depth-h">
    Depth · {symbol}{exchange ? ' · ' + exchange : ''}
    {#if q && q.ltp}
      <span class="ot-depth-ltp">LTP ₹{priceFmt(q.ltp)}</span>
      {#if q.ohlc?.close && q.ohlc.close > 0}
        <span class="ot-depth-prev">Prev ₹{priceFmt(q.ohlc.close)}</span>
      {/if}
    {:else if err}
      <span class="ot-depth-meta">{err}</span>
    {:else}
      <span class="ot-depth-meta">loading…</span>
    {/if}
  </div>
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
  }
  .ot-depth-h {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.4rem;
    font-size: 0.55rem;
    color: #7e97b8;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 0.3rem;
  }
  .ot-depth-meta {
    color: #7e97b8;
    font-style: italic;
    font-size: 0.5rem;
    text-transform: none;
    letter-spacing: 0;
    opacity: 0.7;
  }
  .ot-depth-ltp {
    color: #fbbf24;
    font-weight: 700;
    font-size: 0.62rem;
    text-transform: none;
    letter-spacing: 0;
  }
  /* Prev close anchor right beside LTP — neutral cyan, lighter
     weight so the eye reads LTP first (the live number) and PREV
     second (the reference). */
  .ot-depth-prev {
    color: #7dd3fc;
    font-weight: 600;
    font-size: 0.62rem;
    text-transform: none;
    letter-spacing: 0;
    margin-left: 0.5rem;
  }
  .ot-depth-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1fr;
    gap: 0.15rem 0.4rem;
    font-family: monospace;
    font-size: 0.62rem;
  }
  .ot-depth-label {
    font-size: 0.5rem;
    color: #7e97b8;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    text-align: right;
  }
  .ot-depth-cell {
    text-align: right;
    color: #c8d8f0;
  }
  .ot-depth-bid     { color: #4ade80; }
  .ot-depth-bid-qty { color: #4ade80; opacity: 0.7; }
  .ot-depth-ask     { color: #f87171; }
  .ot-depth-ask-qty { color: #f87171; opacity: 0.7; }
</style>
