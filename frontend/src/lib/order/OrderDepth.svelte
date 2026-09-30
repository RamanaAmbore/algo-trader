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
  import { priceFmt, qtyFmt, aggCompact } from '$lib/format';

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

  // B2/B3: OI + Volume display — Indian-scale compact notation via the
  // app's canonical `aggCompact` formatter ($lib/format), which every
  // other ₹-aggregate surface in the app already uses. Was a local
  // `fmtLakh` producing "12.3K"/"1.5Cr" against aggCompact's own
  // "12K"/"1.50C" — two different compact-number conventions for the
  // same magnitude of number. `aggCompact` returns '—' for non-finite/
  // null the same way the old helper did.
  /** @param {number|null|undefined} n */
  function fmtLakh(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v <= 0) return '—';
    return aggCompact(v);
  }
</script>

<div class="ot-depth">
  <!-- Operator: "I don't want to see DEPTH · CRUDEOIL26JUNFUT · MCX
       in market depth". Dropped the prefix. Prev chip kept
       since it's useful market context above the bid/ask ladder.
       LTP chip removed — canonical LTP lives in the tab bar (oes-tab-ltp). -->
  {#if (q && q.ltp && q.ohlc?.close && q.ohlc.close > 0) || err}
    <div class="ot-depth-h">
      {#if q && q.ltp && q.ohlc?.close && q.ohlc.close > 0}
        <span class="ot-depth-prev">Prev {priceFmt(q.ohlc.close)}</span>
      {:else if err}
        <span class="ot-depth-meta">{err}</span>
      {/if}
      {#if err && q}
        <span class="ot-depth-stale" title="Showing last known depth">stale</span>
      {/if}
    </div>
  {/if}

  <!-- B2/B3/B4: OI · Spread · Volume stats row — Volume moved to the
       end (2026-09-30, operator request); was OI/Volume/Spread. -->
  {#if q && (q.oi != null || q.volume != null || _spread != null)}
    <div class="ot-depth-stats">
      {#if q.oi != null && q.oi > 0}
        <span class="ot-depth-stat">
          <span class="ot-depth-stat-lbl">OI</span>
          <span class="ot-depth-stat-val">{fmtLakh(q.oi)}</span>
        </span>
      {/if}
      {#if _spread != null && _spread >= 0}
        <span class="ot-depth-stat">
          <span class="ot-depth-stat-lbl">Spd</span>
          <span class="ot-depth-stat-val ot-depth-spread">{priceFmt(_spread)}</span>
        </span>
      {/if}
      {#if q.volume != null && q.volume > 0}
        <span class="ot-depth-stat">
          <span class="ot-depth-stat-lbl">Vol</span>
          <span class="ot-depth-stat-val">{fmtLakh(q.volume)}</span>
        </span>
      {/if}
    </div>
  {/if}
  <div class="ot-depth-grid">
    <div class="ot-depth-header-bg" aria-hidden="true"></div>
    <span class="ot-depth-label">Bid qty</span>
    <span class="ot-depth-label">Bid</span>
    <span class="ot-depth-label">Ask</span>
    <span class="ot-depth-label">Ask qty</span>
    {#each buyRows as b, i (i)}
      {@const a = sellRows[i]}
      <span class="ot-depth-cell ot-depth-bid-qty">{b ? qtyFmt(b.quantity) : '—'}</span>
      <span class="ot-depth-cell ot-depth-bid">{b ? priceFmt(b.price) : '—'}</span>
      <span class="ot-depth-cell ot-depth-ask">{a ? priceFmt(a.price) : '—'}</span>
      <span class="ot-depth-cell ot-depth-ask-qty">{a ? qtyFmt(a.quantity) : '—'}</span>
    {/each}
  </div>
  <!-- Raw response diagnostic (2026-09-30, operator request) — surfaces
       exactly what the /api/quote response actually carries, so "is
       this field really populated?" never needs a code read again.
       Levels count is the UNPADDED depth_buy/depth_sell length (0-5),
       distinct from buyRows/sellRows above which always render 5 rows
       (null-padded for visual alignment). -->
  {#if q}
    <div class="ot-depth-diag" title="Raw /api/quote response — depth level counts and volume as returned by the broker, unpadded">
      <span class="ot-depth-diag-item">
        <span class="ot-depth-diag-lbl">Buy levels</span>
        <span class="ot-depth-diag-val">{q.depth_buy?.length ?? 0}</span>
      </span>
      <span class="ot-depth-diag-item">
        <span class="ot-depth-diag-lbl">Sell levels</span>
        <span class="ot-depth-diag-val">{q.depth_sell?.length ?? 0}</span>
      </span>
      <span class="ot-depth-diag-item">
        <span class="ot-depth-diag-lbl">Volume (raw)</span>
        <span class="ot-depth-diag-val">{q.volume ?? '—'}</span>
      </span>
    </div>
  {/if}
</div>

<style>
  .ot-depth {
    margin-top: 0.4rem;
    padding: 0.45rem 0.5rem;
    /* Surface background history: generic black overlay -> --algo-bg-elev2
       (too dark/different from other cards) -> --card-bg-gradient (too
       blended into generic cards) -> --chain-depth-bg (parity with the
       Chain tab's strike grid). REVERSED AGAIN (2026-09-30, same day,
       operator: "keep the order quote depth in sync with chart
       background") — the price chart itself was reverted off
       --chain-depth-bg back to plain --card-bg-gradient the same day;
       this now follows the chart's own token instead of Chain's, per
       explicit operator instruction. See app.css token comments for
       the full back-and-forth. */
    background: var(--card-bg-gradient);
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
    justify-content: flex-start;
    gap: 0.4rem;
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    /* Highlighted header band (2026-09-30, operator: "header
       highlighted... slightly different color underlined") — a
       subtle amber wash + underline so this row reads as the
       ladder's own distinct header, not just plain text floating
       above the bid/ask grid. Negative side margins pull the wash
       out to the card's own edges (undoing .ot-depth's padding) so
       the highlight reads as a full-width band, not an inset strip. */
    margin: -0.45rem -0.5rem 0.3rem;
    padding: 0.25rem 0.5rem;
    background: rgba(251,191,36,0.05);
    /* border-bottom REMOVED (2026-09-30, same day, operator: "the
       border above the labels should be removed") — this band sits
       directly above the BID QTY/BID/ASK/ASK QTY label row (through
       the conditional .ot-depth-stats gap), and its own underline
       read as a second, confusing border stacked right above the
       label row's own .ot-depth-header-bg underline below. Background
       highlight kept; only the border-bottom is gone. */
  }
  .ot-depth-meta {
    color: var(--algo-muted);
    font-style: italic;
    font-size: var(--fs-2xs);
    text-transform: none;
    letter-spacing: 0;
    opacity: 0.7;
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
  }
  .ot-depth-grid {
    display: grid;
    /* Content-sized columns, centered as a group (2026-09-30, operator:
       "the columns should be centered in the middle instead of
       expanding to the available width") — was `1fr 1fr 1fr 1fr`,
       stretching each column to fill the full card width regardless of
       how narrow the actual bid/ask/qty text is. */
    grid-template-columns: repeat(4, max-content);
    justify-content: center;
    /* Column-gap widened 0.4rem -> 0.9rem (2026-09-30, operator: "the
       columns are too close. keep them away to accommodate the
       number") — max-content columns (above) size EXACTLY to their
       content with zero internal buffer, so the original 0.4rem gap
       read as visually cramped, especially between the qty and price
       columns on each side. Row-gap (between price rows) unchanged. */
    gap: 0.15rem 0.9rem;
    font-family: var(--font-numeric);
    /* Audit fix — explicit tabular-nums on the price/qty cells. The
       shared --font-numeric stack covers digit-width consistency, but
       `tabular-nums` is the canonical spec per the CLAUDE.md
       number-formatting rule. */
    font-variant-numeric: tabular-nums;
    font-size: var(--fs-sm);
  }
  .ot-depth-label {
    font-size: var(--fs-2xs);
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    text-align: right;
    padding-bottom: 0.2rem;
    /* Sits ABOVE .ot-depth-header-bg (next rule) in stacking order so
       the label text paints over the header band's background. */
    position: relative;
    z-index: 1;
  }
  .ot-depth-label:nth-child(1),
  .ot-depth-label:nth-child(2) { color: var(--algo-green, var(--c-long)); opacity: 0.8; }
  .ot-depth-label:nth-child(3),
  .ot-depth-label:nth-child(4) { color: var(--algo-red, var(--c-short)); opacity: 0.8; }
  /* Header row band (2026-09-30, operator: "let the row [with BID QTY
     / BID / ASK / ASK QTY] have a lower border end to end and a
     slightly different background to show this [as] a header") — a
     dedicated grid item spanning ALL 4 columns of row 1
     (grid-column: 1 / -1), placed FIRST in the DOM so it paints
     behind the label text. This avoids the column-gap seam problem a
     per-label background would have (.ot-depth-grid's columns are
     content-sized + centered, not stretched full-width, per the
     earlier same-day fix, so 4 separate per-label backgrounds would
     leave visible gaps between them) — one element spanning the
     whole row is genuinely edge-to-edge, not four disconnected
     chips. */
  .ot-depth-header-bg {
    grid-column: 1 / -1;
    grid-row: 1;
    background: rgba(255,255,255,0.04);
    border-bottom: 1px solid rgba(255,255,255,0.14);
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
    font-family: var(--font-numeric);
  }
  .ot-depth-spread {
    color: var(--algo-sky, #7dd3fc);
  }

  /* Raw response diagnostic row (2026-09-30) — plainer than
     .ot-depth-stats (no color-coding), but NOT so faint it's hard to
     notice: the original 0.65-opacity italic treatment read as
     "not showing" (operator report) even though it was technically
     rendering. Dropped the opacity/italic, kept it visually distinct
     via smaller font + muted (not faded) color + explicit label/value
     pairs (matching .ot-depth-stat's own label+value pattern) instead
     of one run-on text string per item. */
  .ot-depth-diag {
    /* inline-flex (was flex) — (2026-09-30, operator: "the border
       above should be limited to the content") — a block-level `flex`
       container takes its PARENT's full width by default, so
       border-top spanned the whole card even though justify-content:
       center only centered the TEXT within that full-width box.
       inline-flex shrinks the container itself to fit its own content
       (the 3 labeled items), so the border-top is exactly as wide as
       "Buy levels 0 · Sell levels 0 · Volume (raw) 0" — genuinely
       limited to the content, not stretching edge to edge.
       align-self: center (replaces the old justify-content: center,
       now redundant on a content-sized box) centers that content-
       sized box within .ot-depth's column, aligning it with
       .ot-depth-grid's own centered column group above it (2026-09-30
       earlier same-day request: "additional info... centered below
       the market depth aligning market depth"). */
    display: inline-flex;
    align-self: center;
    gap: 0.7rem;
    flex-wrap: wrap;
    margin-top: 0.35rem;
    padding-top: 0.25rem;
    border-top: 1px solid rgba(255,255,255,0.10);
    font-size: var(--fs-2xs);
  }
  .ot-depth-diag-item {
    display: inline-flex;
    align-items: baseline;
    gap: 0.2rem;
  }
  .ot-depth-diag-lbl {
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .ot-depth-diag-val {
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }
</style>
