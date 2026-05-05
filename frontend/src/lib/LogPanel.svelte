<script>
  import { onMount } from 'svelte';
  import { parseLogLineTime } from '$lib/stores';
  import { fetchNews } from '$lib/api';

  /** @type {{
   *   heightClass?: string,
   *   cmdHistory?: Array<{status: string, message: string, fields?: Record<string,string>, time: string}>,
   *   orderLog?: Array<any>,
   *   orderRows?: Array<{id:number, account:string, symbol:string, transaction_type:string,
   *                       quantity:number, initial_price:number|null, status:string,
   *                       engine:string, mode:string, detail:string|null, created_at:string}>,
   *   agentLog?: Array<any>,
   *   systemLog?: string[],
   *   simLog?: Array<any>,
   *   initialTab?: string,
   *   onTabChange?: (tab: string) => void,
   * }} */
  let {
    heightClass = 'flex-1 min-h-0',
    cmdHistory = [],
    orderLog = [],
    orderRows = [],
    agentLog = [],
    systemLog = [],
    simLog = [],
    initialTab = 'order',
    onTabChange = () => {},
  } = $props();

  let logTab = $state(initialTab);
  let newsItems = $state(/** @type {Array<{title:string,link:string,source:string,timestamp:string}>} */ ([]));
  let newsRefresh = $state('');
  let newsLoading = $state(false);
  let newsInterval;

  async function loadNews() {
    newsLoading = true;
    try {
      const data = await fetchNews();
      newsItems   = data?.items || [];
      newsRefresh = data?.refreshed_at || '';
    } catch (_) { /* ignore */ }
    newsLoading = false;
  }

  onMount(() => {
    loadNews();
    newsInterval = setInterval(loadNews, 10 * 60 * 1000);
    return () => { if (newsInterval) clearInterval(newsInterval); };
  });

  const TABS = [
    ['order',     'Order'],
    ['terminal',  'Terminal'],
    ['agent',     'Agent'],
    ['simulator', 'Simulator'],
    ['system',    'System'],
    ['news',      'News'],
  ];

  // ── Shared helpers so every tab renders the same way ─────────────────
  // All log rows show timestamps as HH:MM:SS (8 chars). logTime() in
  // stores.js returns the long dual-zone string used by emails / page
  // headers; that's too verbose for a monospace log row. _shortTime()
  // slices out the time portion whether the input is already short
  // ("18:44:42"), a full ISO string, or a Date.
  function _shortTime(input) {
    if (!input) return '';
    const s = typeof input === 'string' ? input : input.toISOString?.() || '';
    // ISO: 2026-04-20T18:44:42(...)  →  slice(11, 19)
    if (s.length >= 19 && s[10] === 'T') return s.slice(11, 19);
    // Already HH:MM:SS or similar short form
    const m = s.match(/\d{2}:\d{2}:\d{2}/);
    return m ? m[0] : s;
  }

  // News-specific time slicer — the /api/news payload's `timestamp`
  // field is a presentational dual-zone string ("Mon, April 20, 2026,
  // 09:30 AM IST | Mon, April 20, 2026, 12:00 AM EDT"), NOT an ISO
  // string. _shortTime() can't parse it (no HH:MM:SS, no T@10), so
  // it would fall through and dump the full string into the time
  // column, eating the row and pushing the title out of view. Match
  // the public Market-News card's parser: ISO → HH:MM, otherwise the
  // first HH:MM run, otherwise the raw value.
  function _newsTime(/** @type {string} */ ts) {
    if (!ts) return '';
    if (ts.length >= 19 && ts[10] === 'T') return ts.slice(11, 16);
    const m = ts.match(/\d\d:\d\d/);
    return m ? m[0] : ts;
  }

  // Shared SIM / LIVE pills — amber for simulated (matches the page-top
  // "SIMULATOR ACTIVE" banner), emerald for live. Replaces the pink
  // badge that was making the Order log look like an error list.
  const SIM_PILL    = '<span class="mode-pill mode-pill-sim">SIM</span>';
  const LIVE_PILL   = '<span class="mode-pill mode-pill-live">LIVE</span>';
  const PAPER_PILL  = '<span class="mode-pill mode-pill-paper">PAPER</span>';
  const REPLAY_PILL = '<span class="mode-pill mode-pill-replay">REPLAY</span>';
  const SHADOW_PILL = '<span class="mode-pill mode-pill-shadow">SHADOW</span>';

  function _modePill(mode) {
    if (mode === 'sim')    return SIM_PILL;
    if (mode === 'paper')  return PAPER_PILL;
    if (mode === 'replay') return REPLAY_PILL;
    if (mode === 'shadow') return SHADOW_PILL;
    return LIVE_PILL;
  }

  // ── Simulator-tab rendering ──────────────────────────────────────────
  // A sim tick entry from /api/simulator/ticks/recent looks like:
  //   { ts, tick_index, scenario, kind: 'tick'|'started'|'stopped',
  //     moves, changes: [{section, account, symbol, col, prev, next, delta}],
  //     note }
  // Rendered as one line per tick with a color based on the magnitude of
  // the worst change (red = steep rate, yellow = static crossing, neutral).
  function _fmtVal(v) {
    if (v === null || v === undefined) return '–';
    if (typeof v === 'number') {
      if (Math.abs(v) >= 1000) return '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: 0 });
      return (Math.round(v * 100) / 100).toString();
    }
    return String(v);
  }
  function _classifySimLine(entry) {
    if (entry.kind !== 'tick') return 'log-info';
    const worst = (entry.changes || []).reduce((acc, c) => {
      if (typeof c.delta !== 'number') return acc;
      return (acc === null || c.delta < acc) ? c.delta : acc;
    }, null);
    if (worst === null) return 'log-info';
    // Keep colours calm: amber-ish for meaningful price moves, neutral
    // otherwise. No red/pink — that class is reserved for actual errors.
    if (worst <= -500 || worst <= -0.3) return 'log-agent-triggered';
    return 'log-info';
  }
  function _renderSimLine(entry) {
    const ts = _shortTime(entry.ts);
    const scen = entry.scenario || '';
    if (entry.kind === 'started')  return `<span class="log-agent-success"><span class="log-ts">${ts}</span> ▶ START ${scen} · ${entry.note || ''}</span>`;
    if (entry.kind === 'stopped')  return `<span class="log-info"><span class="log-ts">${ts}</span> ■ STOP ${scen} · ${entry.note || ''}</span>`;
    if (entry.kind === 'order') {
      const o = entry.order || {};
      // BUY = green, SELL = neutral (not red — a sell isn't an error).
      const sideCls = o.side === 'BUY' ? 'log-agent-success' : 'log-info';
      const price   = (o.price != null)
        ? '@₹' + Number(o.price).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})
        : '';
      return `<span class="${sideCls}"><span class="log-ts">${ts}</span> ${SIM_PILL}◆ ${o.side || '?'} ${o.qty ?? '?'} ${o.symbol || '?'} ${price} · ${o.account || '?'} · ${o.agent || ''} ${o.action || ''}</span>`;
    }
    const cls = _classifySimLine(entry);
    const diffs = (entry.changes || []).map(c => {
      // For price moves, `c.symbol` carries the tradingsymbol and `c.col` is
      // always last_price — show the symbol so the operator sees which
      // instrument moved. For margin patches `c.symbol` is empty and `c.col`
      // names the field being set.
      const leaf  = c.symbol ? c.symbol : c.col;
      const field = `${c.section}.${c.account}.${leaf}`;
      const arrow = `${_fmtVal(c.prev)}→${_fmtVal(c.next)}`;
      const delta = (typeof c.delta === 'number') ? ` (Δ ${_fmtVal(c.delta)})` : '';
      return `<span class="log-chip"><span class="log-chip-key">${field}:</span>${arrow}${delta}</span>`;
    }).join(' ');
    const head = `tick ${entry.tick_index} · ${scen}`;
    return `<span class="${cls}"><span class="log-ts">${ts}</span> ${SIM_PILL}${head} ${diffs || '(no changes)'}</span>`;
  }

  const ORDER_TYPES = new Set(['order_placed','order_cancelled','order_rejected','order_filled']);

  function orderClass(t) {
    return t?.includes('success') ? 'log-agent-success'
         : t?.includes('fail')    ? 'log-agent-failed'
         : 'log-agent-triggered';
  }

  function stripTs(l) {
    return l.replace(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*-?\s*/, '');
  }

  function sysClass(l) {
    return l.includes('ERROR') ? 'log-error' : l.includes('WARNING') ? 'log-warning' : 'log-info';
  }

  function filteredOrder() {
    return orderLog.filter(e =>
      ORDER_TYPES.has(e.event_type) ||
      (e.event_type?.startsWith('action_') && /place_order|close_position|chase_close/i.test(e.trigger_condition || '')));
  }

  function _cmdEntryHtml(h) {
    const cls = h.status === '✓' ? 'log-agent-success' : h.status === '✗' ? 'log-agent-failed' : 'log-info';
    const chips = h.fields ? Object.entries(h.fields)
      .map(([k, v]) => `<span class="log-chip"><span class="log-chip-key">${k}:</span>${v}</span>`)
      .join(' ') : '';
    return `<span class="${cls}"><span class="log-ts">${h.time}</span> ${h.status} ${h.message} ${chips}</span>`;
  }

  // Render one AlgoOrder row (mode=live or sim) for the Order tab. Keeps
  // order details — side, qty, symbol, price, account — on one line so
  // operators can scan placements the same way they'd read a broker blotter.
  function _orderRowHtml(o) {
    const t    = _shortTime(o.created_at);
    const tag  = _modePill(o.mode);
    // Colour by terminal state first, then by side. FILLED = green,
    // UNFILLED = red, OPEN (still chasing) = amber, everything else
    // falls back to the old side-based cue.
    const status = (o.status || '').toUpperCase();
    let rowCls = 'log-info';
    if      (status === 'FILLED')   rowCls = 'log-agent-success';
    else if (status === 'UNFILLED') rowCls = 'log-agent-failed';
    else if (status === 'OPEN')     rowCls = 'log-agent-alert';
    else if (o.transaction_type === 'BUY') rowCls = 'log-agent-success';
    const fillPrice = (o.fill_price != null)
      ? '@₹' + Number(o.fill_price).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})
      : null;
    const initPrice = (o.initial_price != null)
      ? '@₹' + Number(o.initial_price).toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})
      : '';
    // Prefer fill_price once the chase landed; otherwise show the initial
    // limit price the operator submitted.
    const price     = fillPrice || initPrice;
    const statusChip  = o.status   ? ` <span class="log-chip"><span class="log-chip-key">status:</span>${o.status}</span>` : '';
    const attemptChip = (o.attempts != null && o.attempts > 0)
      ? ` <span class="log-chip"><span class="log-chip-key">chase:</span>#${o.attempts}</span>`
      : '';
    const engine = o.engine ? ` <span class="log-chip"><span class="log-chip-key">engine:</span>${o.engine}</span>` : '';
    // Agent-id chip — links back to the firing agent on /agents.
    const agentChip = o.agent_id
      ? ` <a class="log-agent-chip" href="/agents?focus=${o.agent_id}">agent #${o.agent_id}</a>`
      : '';
    return `<span class="${rowCls}"><span class="log-ts">${t}</span> ${tag}◆ ${o.transaction_type} ${o.quantity} ${o.symbol} ${price} · ${o.account}${statusChip}${attemptChip}${engine}${agentChip}</span>`;
  }

  function _orderLogHtml() {
    // Prefer structured AlgoOrder rows when provided; fall back to the
    // terminal-command history so the /console page still works.
    if (orderRows && orderRows.length) {
      return orderRows.map(_orderRowHtml).join('\n');
    }
    const lines = cmdHistory.map(h => _cmdEntryHtml(h));
    return lines.length ? lines.join('\n') : '<span class="log-debug">No order events.</span>';
  }

  function _terminalHtml() {
    const cmdLines = cmdHistory.map(h => ({ ts: h.time, html: _cmdEntryHtml(h) }));
    const orderLines = filteredOrder().map(e => {
      const t = _shortTime(e.timestamp);
      return { ts: t, html: `<span class="${orderClass(e.event_type)}"><span class="log-ts">${t}</span> ${e.event_type||''} ${e.trigger_condition||''}</span>` };
    });
    const agentLines = agentLog.map(e => {
      const t = _shortTime(e.timestamp);
      return { ts: t, html: `<span class="log-agent-default"><span class="log-ts">${t}</span> ${e.event_type||''} ${e.trigger_condition||''}</span>` };
    });
    const all = [...cmdLines, ...orderLines, ...agentLines];
    return all.length ? all.map(x => x.html).join('\n') : '<span class="log-debug">No events.</span>';
  }

  function setTab(id) {
    logTab = id;
    onTabChange(id);
  }
</script>

<div class="flex items-stretch mb-2 log-tab-row">
  <!-- "log" label rotated 90°. Two-layer split so the flex parent
       (wrap) reserves the column width and the child carries the
       rotation via writing-mode: vertical-lr (text flows top-to-bottom
       naturally, no transform needed). -->
  <span class="log-section-wrap" aria-hidden="true">
    <span class="log-section-text">Log</span>
  </span>
  {#each TABS as [id, label]}
    <button onclick={() => setTab(id)}
      class="log-tab-btn border-b-2 transition-colors
        {logTab === id ? 'border-[#d97706] text-[#fbbf24]' : 'border-transparent text-[#b4c8e6] hover:text-[#fbbf24]'}"
    >{label}</button>
  {/each}
</div>

{#if logTab === 'news'}
  <!-- News tab — proper row layout (matches the public /performance
       Market News card structure: time / title / source per row) but
       in the algo dark palette. Pulled out of the shared <pre> so each
       row gets real padding + a hairline separator instead of being
       squashed onto a single monospace line. -->
  <div class="log-panel log-news-panel {heightClass}">
    {#if newsRefresh}
      <div class="log-news-head">Refreshed at {newsRefresh}</div>
    {/if}
    {#if newsLoading && !newsItems.length}
      <div class="log-debug">Loading headlines…</div>
    {:else if newsItems.length}
      <ul class="log-news-list">
        {#each newsItems as n}
          <li class="log-news-row">
            <span class="log-news-time">{_newsTime(n.timestamp)}</span>
            <a class="log-news-title" href={n.link} target="_blank" rel="noopener">
              {n.title}{#if n.source}<span class="log-news-src">{n.source}</span>{/if}
            </a>
          </li>
        {/each}
      </ul>
    {:else}
      <div class="log-debug">No headlines.</div>
    {/if}
  </div>
{:else}
<pre class="log-panel {heightClass}">{#if logTab === 'order'}{@html _orderLogHtml()}{:else if logTab === 'terminal'}{@html _terminalHtml()}{:else if logTab === 'agent'}{#if agentLog.length}{@html agentLog.map(e => {
  const t = _shortTime(e.timestamp);
  const cls = e.event_type === 'action_failed'  ? 'log-agent-failed'
            : e.event_type === 'action_success' ? 'log-agent-success'
            : e.event_type === 'cooldown'        ? 'log-agent-cooldown'
            : 'log-agent-default';
  return `<span class="${cls}"><span class="log-ts">${t}</span> ${e.event_type||''} ${e.trigger_condition||''}</span>`;
}).join('\n')}{:else}<span class="log-debug">No agent events.</span>{/if}{:else if logTab === 'simulator'}{#if simLog.length}{@html simLog.map(_renderSimLine).join('\n')}{:else}<span class="log-debug">No simulator ticks. Start a scenario at /admin/simulator to stream price changes here.</span>{/if}{:else}{#if systemLog.length}{@html systemLog.map(l => {
  const t = parseLogLineTime(l);
  const rest = t ? stripTs(l) : l;
  return `<span class="${sysClass(l)}">${t ? `<span class="log-ts">${t}</span> ` : ''}${rest}</span>`;
}).join('\n')}{:else}<span class="log-debug">No log entries.</span>{/if}{/if}</pre>
{/if}

<style>
  /* Tab row — another +30% on the previous 0.48rem → 0.62rem. Padding
     scaled proportionally. Still no inter-tab gap so mobile fit holds. */
  .log-tab-row { gap: 0; }
  :global(.log-tab-btn) {
    font-size: 0.62rem;
    font-weight: 600;
    padding: 0.18rem 0.44rem;
    white-space: nowrap;
    letter-spacing: 0.02em;
    font-family: ui-monospace, monospace;
  }

  /* Vertical "log" label — muted monospace section marker, mirrors the
     understated navbar brand text instead of a coloured chip. The
     two-layer split (wrap + text) keeps flex layout and writing-mode
     from conflicting. */
  .log-section-wrap {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 0.7rem;
    padding: 0 0.15rem;
    margin-right: 0.2rem;
    align-self: stretch;
  }
  .log-section-text {
    writing-mode: vertical-lr;
    transform: rotate(180deg);
    font-family: ui-monospace, monospace;
    font-size: 0.48rem;
    font-weight: 500;
    line-height: 1;
    /* Subtle gold — same amber hue as the navbar accents but at low
       saturation so it reads as a quiet section stamp, not a UI control. */
    color: rgba(251, 191, 36, 0.45);
    text-transform: lowercase;
    letter-spacing: 0.08em;
    user-select: none;
  }

  /* Mode pills — amber SIM / emerald LIVE. Consistent across Simulator
     and Order tabs; replaces the prior pink-on-pink SIM styling that
     dominated the Order log. */
  :global(.mode-pill) {
    display: inline-block;
    padding: 0 0.3rem;
    margin-right: 0.25rem;
    font-family: ui-monospace, monospace;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    border-radius: 2px;
    border: 1px solid;
    vertical-align: baseline;
  }
  :global(.mode-pill-sim) {
    background: rgba(251,191,36,0.14);
    color: #fbbf24;
    border-color: rgba(251,191,36,0.45);
  }
  :global(.mode-pill-live) {
    background: rgba(16,185,129,0.14);
    color: #6ee7b7;
    border-color: rgba(16,185,129,0.45);
  }
  /* Mode-2 paper rows — sky-blue tint, distinct from amber sim and
     emerald live so the operator never confuses a paper fill with a
     real one. */
  :global(.mode-pill-paper) {
    background: rgba(56,189,248,0.14);
    color: #7dd3fc;
    border-color: rgba(56,189,248,0.45);
  }
  /* Mode-4 replay — green, matching the navbar REPLAY badge. */
  :global(.mode-pill-replay) {
    background: rgba(74,222,128,0.14);
    color: #4ade80;
    border-color: rgba(74,222,128,0.45);
  }
  /* Mode-5 shadow — orange, matching the navbar SHADOW badge. */
  :global(.mode-pill-shadow) {
    background: rgba(251,146,60,0.14);
    color: #fb923c;
    border-color: rgba(251,146,60,0.45);
  }

</style>
