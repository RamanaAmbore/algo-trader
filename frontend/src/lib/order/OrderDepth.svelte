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
  <!-- Additional-info strip (2026-09-30, operator: "use your judgement
       on how to show the additional info order ticket. get inspired
       from chain to quote depth in order ticket") — CONSOLIDATED from
       two separate strips (an OI/Spread/Volume row ABOVE the grid, and
       a Buy levels/Sell levels/Volume(raw) diagnostic row BELOW it)
       into this single row below the grid. Chain doesn't sandwich its
       strike grid between two separate info bands; one clean strip
       after the data reads more like that. The compact "Vol" stat
       (e.g. "13K") is dropped per explicit operator request — OI and
       Spread are the genuinely useful at-a-glance numbers; the raw,
       unformatted Volume further below is a distinct value (exact
       broker figure, not compact-rounded) kept for the diagnostic
       "is this field really populated?" purpose it was originally
       added for. Levels count is the UNPADDED depth_buy/depth_sell
       length (0-5), distinct from buyRows/sellRows above which always
       render 5 rows (null-padded for visual alignment). -->
  {#if q}
    <div class="ot-depth-diag" title="Depth stats + raw /api/quote response — depth level counts and volume as returned by the broker, unpadded">
      {#if q.oi != null && q.oi > 0}
        <span class="ot-depth-diag-item">
          <span class="ot-depth-diag-lbl">OI</span>
          <span class="ot-depth-diag-val">{fmtLakh(q.oi)}</span>
        </span>
      {/if}
      {#if _spread != null && _spread >= 0}
        <span class="ot-depth-diag-item">
          <span class="ot-depth-diag-lbl">Spd</span>
          <span class="ot-depth-diag-val ot-depth-spread">{priceFmt(_spread)}</span>
        </span>
      {/if}
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
    /* overflow: hidden (2026-09-30, operator: "extend the header in
       order ticket end to end. there is a gap header in order
       ticket. remove it.") — clips .ot-depth-header-bg's box-shadow
       bleed trick (see that rule below) exactly at this card's own
       edges, giving the header band a genuine "end to end" width
       regardless of how narrow the actual centered bid/ask columns
       are. Safe here: .ot-depth has no popovers/tooltips of its own
       that would need to escape this boundary. */
    overflow: hidden;
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
       how narrow the actual bid/ask/qty text is.
       Defined minimum width added (2026-09-30 follow-up, operator: "the
       columns should have a defined width to accommodate the quote
       numbers") — bare `max-content` sizes each column to WHATEVER the
       current quote happens to need, so the grid visibly resized/
       jumped on every poll tick as bid/ask/qty digit counts changed
       (e.g. "99.50" -> "105.25" -> "1050.75"). A shared 3.4rem floor
       keeps all 4 columns visually stable at rest — comfortably fits
       typical price/qty values without truncation — while max-content
       still lets a genuinely longer value grow the column rather than
       clip. */
    grid-template-columns: repeat(4, minmax(3.4rem, max-content));
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
    /* font-size/weight matched to Chain's header typography
       (.chain-th-ce/-pe/-strike, OptionChainTab.svelte) — was --fs-2xs
       with no explicit weight (2026-09-30, operator: "order ticket
       header font decoration should be similar to chain header
       decoration"). */
    font-size: var(--fs-sm);
    font-weight: 700;
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    text-align: right;
    padding-bottom: 0.2rem;
    /* Sits ABOVE .ot-depth-header-bg (next rule) in stacking order so
       the label text paints over the header band's background. */
    position: relative;
    z-index: 1;
    /* grid-row: 1 EXPLICIT (2026-09-30, operator: "why border shows
       above the header row and not decorate like the header in
       chain") — root cause: .ot-depth-header-bg (the element right
       before these labels in the DOM) explicitly claims grid-row: 1
       across ALL 4 columns. CSS Grid auto-placement treats an
       explicitly-placed item's cells as OCCUPIED and skips them for
       any later item with no explicit position — so these 4 labels,
       having no grid-row of their own, got silently auto-placed into
       row 2, not row 1. That left row 1 containing ONLY the empty
       header-bg div (background + border-bottom, no text), rendering
       as a thin colored band ABOVE the actual "Bid qty/Bid/Ask/Ask
       qty" text (which was really sitting in row 2) instead of
       BEHIND it as intended. Chain's header never had this problem —
       its <th> cells are real table cells inside <thead>, not
       CSS-Grid auto-placed items, so there's no equivalent collision
       to dodge.
       First attempt (grid-row: 1 ALONE, still auto column) made it
       WORSE, live-verified: with row fixed but column left to
       auto-placement, the algorithm found every column in row 1
       already "occupied" by header-bg and — rather than overlapping —
       created 4 brand-new IMPLICIT columns past the original 4 to
       place the labels into, splitting labels and data cells into
       two non-aligned column groups entirely (confirmed via
       getBoundingClientRect: labels landed around x=750-916,
       data cells stayed at x=483-675 — completely disjoint). Explicit
       placement only avoids/overlaps correctly on axes it actually
       specifies; the auto column axis still dodges occupied cells by
       creating new tracks. Fix: also give each label its own explicit
       grid-column (1-4, via the nth-of-type block below) so BOTH axes
       are explicit and the labels land in the exact same cells as
       header-bg — true overlap, painted on top per the z-index
       above — instead of auto-placement inventing new columns. */
    grid-row: 1;
  }
  .ot-depth-label:nth-of-type(1) { grid-column: 1; }
  .ot-depth-label:nth-of-type(2) { grid-column: 2; }
  .ot-depth-label:nth-of-type(3) { grid-column: 3; }
  .ot-depth-label:nth-of-type(4) { grid-column: 4; }
  /* nth-of-type, NOT nth-child (2026-09-30, operator: "I think there
     is some hidden header" — diagnosed from a live screenshot: the
     Bid|Ask divider was rendering after "Bid qty" instead of after
     "Bid", and "Bid" itself showed the wrong (red) color). Root
     cause: .ot-depth-header-bg (a <div>, placed first, immediately
     above) is counted by :nth-child since it counts ALL siblings —
     silently shifting every label's index by one ("Bid qty" became
     nth-child(2) instead of (1), etc.), with "Ask qty" landing on
     nth-child(5) and matching no rule at all. :nth-of-type only
     counts same-TAG siblings, so the <div> (a different tag from
     these <span> labels) doesn't participate in the count — index 1
     is genuinely the first <span>, "Bid qty". */
  .ot-depth-label:nth-of-type(1),
  .ot-depth-label:nth-of-type(2) { color: var(--algo-green, var(--c-long)); opacity: 0.8; }
  .ot-depth-label:nth-of-type(3),
  .ot-depth-label:nth-of-type(4) { color: var(--algo-red, var(--c-short)); opacity: 0.8; }
  /* Central Bid|Ask divider (2026-09-30, operator: "apply column
     borders of chain to quote depth headings and quotes") — mirrors
     Chain's Strike-column divider (.chain-th-strike /
     .chain-row>td.chain-td-strike, OptionChainTab.svelte), same
     0.03-alpha "whisper-quiet" weight. Chain has a real middle column
     (Strike) to hang left+right borders off; the depth ladder's
     natural equivalent split is the Bid/Ask boundary itself (columns
     2 and 3), so one divider there rather than one per column. */
  .ot-depth-label:nth-of-type(2) { border-right: 1px solid rgba(255,255,255,0.03); padding-right: 0.3rem; }
  .ot-depth-label:nth-of-type(3) { border-left: 1px solid rgba(255,255,255,0.03); padding-left: 0.3rem; }
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
     chips.
     Restyled to match Chain's header format (2026-09-30, operator:
     "now apply this header format to ticker bid qty, bid, ask, ask
     qty in order ticket") — background switched from a flat white
     wash to --card-bg-elevated (the same genuinely-lighter navy tier
     Chain's .chain-th-ce/-pe/-strike use, app.css), and the
     border-bottom dialed from plain white to the same amber Chain
     settled on. No sticky/border-collapse risk here (this is a plain
     CSS grid, not a <table>), so border-bottom is safe to use
     directly — no box-shadow workaround needed.
     Amber alpha bumped 0.18 -> 0.28 (operator: "the border color
     should be a little strong"), then 0.28 -> 0.40 (operator: "the
     bottom border should be stronger on amber side... i am ferring
     to labels" — confirming this applies to the Bid qty/Bid/Ask/Ask
     qty label row) — kept in sync with the matching bump on Chain's
     own .chain-th-ce/-pe/-strike throughout. */
  .ot-depth-header-bg {
    grid-column: 1 / -1;
    grid-row: 1;
    /* position: relative anchors the ::before bleed layer below. */
    position: relative;
    background: var(--card-bg-elevated);
    border-bottom: 1px solid rgba(251,191,36,0.40);
  }
  /* Full-bleed background layer (2026-09-30, operator: "extend the
     header in order ticket end to end. there is a gap header in
     order ticket. remove it.") — root cause: .ot-depth-grid stretches
     to fill .ot-depth's full width (default flex cross-axis stretch),
     but grid-template-columns sizes the 4 columns to their own
     content (minmax(3.4rem, max-content)) and centers that narrower
     group via justify-content: center, per the earlier, deliberate
     "columns centered, not expanding to available width" decision.
     .ot-depth-header-bg's grid-column: 1/-1 only spans those 4
     EXPLICIT tracks, not the grid container's extra centering gutter
     on either side — so on a wide ticket panel the header band could
     end up covering only a narrow sliver in the middle, nowhere near
     "end to end" (measured live: a 267px header band inside a 1349px
     grid, ~540px of uncovered gap on each side). A plain CSS grid
     item can't reach past its own track span, so this can't be fixed
     by adjusting grid-column alone.
     Fix: a ::before pseudo-element, absolutely positioned relative to
     THIS element (not confined by the grid-track system at all),
     bled ±9999px horizontally — clipped at .ot-depth's own edges via
     that element's `overflow: hidden` (above) — giving the
     background+border a genuine full-card width regardless of how
     narrow the actual centered Bid/Ask columns are. A plain
     background-color box-shadow bleed trick doesn't work here since
     --card-bg-elevated is a gradient, not a solid color (box-shadow's
     color argument can't be a gradient); a real ::before background
     has no such restriction. top/bottom: 0 inherits this element's
     own height, which already correctly matches grid-row 1 via
     normal grid sizing — only the width axis needed fixing. */
  .ot-depth-header-bg::before {
    content: '';
    position: absolute;
    inset: 0 -9999px;
    background: var(--card-bg-elevated);
    border-bottom: inherit;
  }
  .ot-depth-cell {
    text-align: right;
    color: var(--algo-slate);
  }
  .ot-depth-bid     { color: var(--algo-green, var(--c-long)); border-right: 1px solid rgba(255,255,255,0.03); padding-right: 0.3rem; }
  .ot-depth-bid-qty { color: var(--algo-green, var(--c-long)); opacity: 0.7; }
  .ot-depth-ask     { color: var(--algo-red, var(--c-short)); border-left: 1px solid rgba(255,255,255,0.03); padding-left: 0.3rem; }
  .ot-depth-ask-qty { color: var(--algo-red, var(--c-short)); opacity: 0.7; }

  /* Stale indicator — shows when error exists but old q is preserved */
  .ot-depth-stale {
    font-size: var(--fs-2xs);
    color: var(--algo-amber, var(--c-action));
    opacity: 0.7;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  /* B4: Spread value gets its own accent color within the
     consolidated .ot-depth-diag row below (2026-09-30) — the
     .ot-depth-stats/.ot-depth-stat/.ot-depth-stat-lbl/.ot-depth-stat-val
     strip this used to belong to was removed (folded into
     .ot-depth-diag, see markup comment above); this one rule survives
     since it's still referenced. */
  .ot-depth-spread {
    color: var(--algo-sky, #7dd3fc);
  }

  /* Additional-info row (2026-09-30) — the original 0.65-opacity
     italic treatment read as "not showing" (operator report) even
     though it was technically rendering. Dropped the opacity/italic,
     kept it visually distinct via smaller font + muted (not faded)
     color + explicit label/value pairs instead of one run-on text
     string per item. Later consolidated (2026-09-30, same day) to
     also carry OI + Spread, folded in from the separate
     .ot-depth-stats strip that used to sit above the grid — see the
     markup comment near this row for the full rationale. */
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
       align-self: flex-end (2026-09-30, operator: "align the labels
       to right") — was align-self: center (itself a replacement for
       the old justify-content: center, redundant on a content-sized
       box); right-aligns this whole block against .ot-depth's own
       right edge instead of centering it, matching the header
       labels' own text-align: right convention.
       border-top color/alpha matched to the header's own border
       (2026-09-30, operator: "make the border above the labels to
       align with header border") — was a plain white 0.10 alpha line,
       now the same amber used by .ot-depth-header-bg's border-bottom,
       which is itself kept in sync with Chain's .chain-th-*
       box-shadow amber.
       font-size bumped --fs-2xs -> --fs-sm (2026-09-30, operator:
       "the label text size be in sync with chain") — matches Chain's
       own header-label size (.chain-th-ce/-pe/-strike), was smaller
       than Chain's equivalent labels. */
    display: inline-flex;
    align-self: flex-end;
    gap: 0.7rem;
    flex-wrap: wrap;
    margin-top: 0.35rem;
    padding-top: 0.25rem;
    border-top: 1px solid rgba(251,191,36,0.40);
    font-size: var(--fs-sm);
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
    text-align: right;
  }
  .ot-depth-diag-val {
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }
</style>
