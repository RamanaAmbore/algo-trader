<script>
  import { onDestroy, onMount } from 'svelte';
  import { parseLogLineTime, parseLogLineDate, logTime, formatDualTz } from '$lib/stores';
  import {
    fetchRecentAgentEvents, fetchSimEvents,
    fetchSimTicks, fetchAdminLogs, fetchAlgoOrdersRecent,
  } from '$lib/api';
  import NewsList from '$lib/NewsList.svelte';
  import { priceFmt, aggCompact } from '$lib/format';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import { chipsHtml, chipsFromJson } from '$lib/logChips';
  import ChartModal from '$lib/ChartModal.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';

  // mode (sim/paper/live/shadow/replay): when set, auto-flips logTab to
  // the mapped tab AND auto-applies the matching order filter — sim →
  // simulator tab; everything else → order tab filtered to that mode.
  /** @type {{
   *   heightClass?: string,
   *   tabs?: string[],
   *   defaultTab?: string,
   *   simScope?: boolean,
   *   pollMs?: number,
   *   cmdHistory?: Array<{status: string, message: string, fields?: Record<string,string>, time: string}>,
   *   onTabChange?: (tab: string) => void,
   *   mode?: string | null,
   * }} */
  let {
    heightClass = 'flex-1 min-h-0',
    tabs        = ['order','terminal','agent','simulator','system','news'],
    defaultTab  = 'order',
    simScope    = false,
    pollMs      = 3000,
    cmdHistory  = [],
    onTabChange = () => {},
    mode        = /** @type {string | null} */ (null),
  } = $props();

  let logTab = $state(defaultTab);
  // Re-sync logTab whenever the parent updates defaultTab (e.g. /agents
  // calling runInSim flips defaultTab to 'simulator'). Without this the
  // tab strip silently ignores parent-driven tab switches.
  $effect(() => {
    if (defaultTab && defaultTab !== logTab) logTab = defaultTab;
  });

  // Mode → tab + filter mapping. When the parent passes `mode`, we
  // auto-switch the Order-tab filter chip AND (for sim) flip to the
  // simulator tab so an operator selecting SIM in the navbar lands on
  // the price-tick stream automatically.
  //
  // Fires ONLY on mode CHANGE (tracked via _lastMode), not on every
  // logTab change. Without the gate, manually picking a different tab
  // while mode='sim' would retrigger this $effect (it reads logTab in
  // the condition), see mode is still 'sim', and slam logTab back to
  // 'simulator' — locking the operator on one tab.
  let _lastMode = $state(/** @type {string | null} */ (null));
  $effect(() => {
    if (!mode) return;
    if (mode === _lastMode) return;
    _lastMode = mode;
    // Clear stale in-memory rows on mode switch so the chip filter
    // doesn't briefly render wrong-mode rows before the next poll
    // lands. Each stream refetches immediately via _every() / the
    // poll cadence — no functional gap, just no flash of stale data.
    orderRows = [];
    agentLog  = [];
    simLog    = [];
    if (mode === 'sim') {
      orderModeFilter = 'sim';
      if (tabs.includes('simulator')) logTab = 'simulator';
    } else if (['paper','live','shadow','replay'].includes(mode)) {
      orderModeFilter = /** @type {any} */ (mode);
      if (tabs.includes('order')) logTab = 'order';
    }
  });

  // ── Internally-fetched data streams ──────────────────────────────────
  // Each callsite previously polled these endpoints itself and passed the
  // arrays in as props — we now fetch once per LogPanel instance, on the
  // same cadence (`pollMs`), so a page mounting a LogPanel doesn't have to
  // wire up four independent loaders. Order tab still uses UnifiedLog
  // (already centralised).
  let orderRows = $state(/** @type {any[]} */ ([]));   // for Terminal tab embedding
  let agentLog  = $state(/** @type {any[]} */ ([]));
  let systemLog = $state(/** @type {string[]} */ ([]));
  let simLog    = $state(/** @type {any[]} */ ([]));

  /** @type {Array<ReturnType<typeof setInterval>>} */
  const _intervals = [];
  function _every(/** @type {() => Promise<void> | void} */ fn) {
    fn();
    if (pollMs > 0) {
      const id = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return;
        fn();
      }, pollMs);
      _intervals.push(id);
    }
  }

  async function _loadAgents() {
    try {
      // Sim-scoped surfaces (e.g. /admin/simulator) want the sim-only
      // event stream; everywhere else gets the real-mode stream.
      const data = simScope ? await fetchSimEvents(100)
                            : await fetchRecentAgentEvents(100);
      agentLog = Array.isArray(data) ? data : [];
    } catch (_) { /* keep last-good */ }
  }
  async function _loadSystem() {
    try {
      const d = await fetchAdminLogs(200);
      systemLog = d?.lines || [];
    } catch (_) {}
  }
  async function _loadSim() {
    try {
      const data = await fetchSimTicks(100);
      simLog = Array.isArray(data) ? data : [];
    } catch (_) {}
  }
  async function _loadOrders() {
    try {
      const data = await fetchAlgoOrdersRecent(100, 'all');
      orderRows = Array.isArray(data) ? data : [];
    } catch (_) {}
  }

  onMount(() => {
    if (tabs.includes('agent'))     _every(_loadAgents);
    if (tabs.includes('system'))    _every(_loadSystem);
    if (tabs.includes('simulator')) _every(_loadSim);
    if (tabs.includes('terminal'))  _every(_loadOrders);  // Terminal tab embeds order rows
  });
  onDestroy(() => { for (const id of _intervals) clearInterval(id); });

  // ── Chart modal state (Order tab) ────────────────────────────────────────
  let _chartModalSym  = $state('');
  let _chartModalExch = $state('');
  // ── SymbolPanel + context menu state (Order tab delegation) ─────────────
  /** @type {string} */
  let _symPanelSym  = $state('');
  /** @type {string} */
  let _symPanelExch = $state('');
  /** @type {{ symbol: string, exchange: string, x: number, y: number } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | 'log' | null} */ let _ctxAction = $state(null);
  /** @type {string} */ let _ctxSym  = $state('');
  /** @type {string} */ let _ctxExch = $state('');
  /** Escape HTML attribute values from broker-supplied strings (defence-in-depth). */
  const _escAttr = (/** @type {any} */ v) =>
    String(v || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

  const _ALL_TABS = [
    ['order',     'Orders'],
    ['agent',     'Agents'],
    ['terminal',  'Terminal'],
    ['simulator', 'Simulator'],
    ['system',    'System'],
    ['news',      'News'],
  ];
  // Filter to the per-page subset (defaults to all six). Lets /console
  // hide Order / Simulator / Agent that don't apply.
  const VISIBLE_TABS = $derived(_ALL_TABS.filter(([id]) => tabs.includes(id)));

  // ── Order-tab mode filter ───────────────────────────────────────────
  // [All] uses the unified merged feed (orders + agent fires); the
  // mode-specific filters fall back to the order-only rendering so
  // operators can scope to "what paper orders did I just place" or
  // "what live orders did Kite take" without agent-fire noise.
  /** @type {'all'|'paper'|'live'|'sim'|'shadow'|'replay'} */
  let orderModeFilter = $state('all');
  const _ORDER_MODE_TABS = [
    ['all',    'All'],
    ['paper',  'Paper'],
    ['live',   'Live'],
    ['sim',    'Sim'],
    ['shadow', 'Shadow'],
    ['replay', 'Replay'],
  ];
  const filteredOrderRows = $derived.by(() => {
    if (orderModeFilter === 'all') return orderRows;
    return (orderRows || []).filter(o => (o?.mode || 'live') === orderModeFilter);
  });

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

  /**
   * Emit the dual-zone stacked HTML for a log timestamp.
   * Falls back to `_shortTime` (single IST line) when the input
   * isn't a parseable ISO — preserves UI for non-ISO inputs like
   * raw HH:MM:SS strings from upstream payloads.
   */
  function _dualTsHtml(input) {
    if (!input) {
      // Never silently swallow a timestamp slot — show a muted dash so
      // the column position is preserved and the row scans the same
      // way as its neighbours.
      return `<span class="log-ts log-ts-empty">—</span>`;
    }
    const d = input instanceof Date
      ? input
      : (typeof input === 'string' && /^\d{4}-\d{2}-\d{2}/.test(input))
        ? new Date(input.includes('T') || input.includes('Z') ? input : input + 'Z')
        : null;
    if (d && !isNaN(d.getTime())) {
      // Short dual-zone format for log rows — just the times, no date
      // prefix, since hundreds of rows on the same page don't need the
      // date repeated. Same monospace + cyan-200 + tabular-nums as the
      // .algo-ts page-header wall clock. Hover tooltip carries the full
      // dual-tz date + time (page-header format) for forensic context.
      const ist = d.toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit', hour12: false,
        timeZone: 'Asia/Kolkata',
      });
      const est = d.toLocaleTimeString('en-GB', {
        hour: '2-digit', minute: '2-digit', hour12: false,
        timeZone: 'America/New_York',
      });
      const estTz = d.toLocaleTimeString('en-US', {
        timeZoneName: 'short', timeZone: 'America/New_York',
      }).split(' ').pop();
      const full = formatDualTz(d);
      return `<span class="log-ts" title="${full}">${ist} IST · ${est} ${estTz}</span>`;
    }
    // Non-ISO string we couldn't parse — render as a fallback chip but
    // still keep the column position aligned with sibling rows.
    const raw = typeof input === 'string' ? _shortTime(input) : '—';
    return `<span class="log-ts log-ts-fallback">${raw || '—'}</span>`;
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
      // ≥ 1k → compact K/L; < 1k → keep 2-dp precision (sim-tick
      // entries are a mix of per-share prices and P&L deltas, so
      // the cutoff is the same threshold the helper uses anyway).
      if (Math.abs(v) >= 1000) return aggCompact(v);
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
    const ts = _dualTsHtml(entry.ts);
    const scen = entry.scenario || '';
    if (entry.kind === 'started')  return `<span class="log-agent-success">${ts} ▶ START ${scen} · ${entry.note || ''}</span>`;
    if (entry.kind === 'stopped')  return `<span class="log-info">${ts} ■ STOP ${scen} · ${entry.note || ''}</span>`;
    if (entry.kind === 'order') {
      const o = entry.order || {};
      const sideCls = o.side === 'BUY' ? 'log-agent-success' : 'log-info';
      const price   = (o.price != null) ? '@' + priceFmt(o.price) : '';
      return `<span class="${sideCls}">${ts} ${SIM_PILL}◆ ${o.side || '?'} ${o.qty ?? '?'} ${o.symbol || '?'} ${price} · ${o.account || '?'} · ${o.agent || ''} ${o.action || ''}</span>`;
    }
    const cls = _classifySimLine(entry);
    const diffs = (entry.changes || []).map(c => {
      const leaf  = c.symbol ? c.symbol : c.col;
      const field = `${c.section}.${c.account}.${leaf}`;
      const arrow = `${_fmtVal(c.prev)}→${_fmtVal(c.next)}`;
      const delta = (typeof c.delta === 'number') ? ` (Δ ${_fmtVal(c.delta)})` : '';
      return `<span class="log-chip"><span class="log-chip-key">${field}:</span>${arrow}${delta}</span>`;
    }).join(' ');
    const head = `tick ${entry.tick_index} · ${scen}`;
    return `<span class="${cls}">${ts} ${SIM_PILL}${head} ${diffs || '(no changes)'}</span>`;
  }

  function stripTs(l) {
    return String(l ?? '').replace(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*-?\s*/, '');
  }

  function sysClass(l) {
    const s = String(l ?? '');
    return s.includes('ERROR') ? 'log-error' : s.includes('WARNING') ? 'log-warning' : 'log-info';
  }

  function _cmdEntryHtml(h) {
    const cls = h.status === '✓' ? 'log-agent-success' : h.status === '✗' ? 'log-agent-failed' : 'log-info';
    const chips = h.fields ? Object.entries(h.fields)
      .map(([k, v]) => `<span class="log-chip"><span class="log-chip-key">${k}:</span>${v}</span>`)
      .join(' ') : '';
    // Command entries: h.time is already-formatted (from `logTime(new
    // Date())` at the callsite in orders/+page.svelte). Render it as a
    // single-zone fallback inside .log-ts-fallback so it picks up the
    // muted slate color without forcing IST+EDT extraction.
    return `<span class="${cls}"><span class="log-ts log-ts-fallback">${h.time || ''}</span> ${h.status} ${h.message} ${chips}</span>`;
  }

  // Render one AlgoOrder row (mode=live or sim) for the Order tab. Keeps
  // order details — side, qty, symbol, price, account — on one line so
  // operators can scan placements the same way they'd read a broker blotter.
  function _orderRowHtml(o) {
    const t    = _dualTsHtml(o.created_at);
    const tag  = _modePill(o.mode);
    // Colour by terminal state first, then by side. FILLED = green,
    // UNFILLED = red, OPEN (still chasing) = amber, everything else
    // falls back to the old side-based cue.
    const status = (o.status || '').toUpperCase();
    let rowCls = 'log-info';
    if      (status === 'FILLED')          rowCls = 'log-agent-success';
    else if (status === 'UNFILLED')        rowCls = 'log-agent-failed';
    else if (status === 'OPEN')            rowCls = 'log-agent-alert';
    else if (status === 'CANCELLED')       rowCls = 'log-order-cancelled';
    else if (status === 'REJECTED')        rowCls = 'log-order-rejected';
    else if (status === 'SHADOW_OK')       rowCls = 'log-order-shadow-ok';
    else if (status === 'SHADOW_REJECTED') rowCls = 'log-order-rejected';
    else if (o.transaction_type === 'BUY') rowCls = 'log-agent-success';
    const fillPrice = (o.fill_price != null) ? '@' + priceFmt(o.fill_price) : null;
    const initPrice = (o.initial_price != null) ? '@' + priceFmt(o.initial_price) : '';
    // Prefer fill_price once the chase landed; otherwise show the initial
    // limit price the operator submitted.
    const price     = fillPrice || initPrice;
    // Preflight verdict — a tiny ✓/✗ chip whose title= carries the
    // detail. ✓ when the order made it past preflight (FILLED / OPEN
    // / SHADOW_OK / FILLED-on-live). ✗ when REJECTED / SHADOW_REJECTED
    // (basket_margin pushed back). The chip is invisible for ambiguous
    // states so it doesn't cry wolf on transient OPEN rows.
    let preflightChip = '';
    if (status === 'REJECTED' || status === 'SHADOW_REJECTED') {
      const t = (o.detail || 'preflight blocked').replace(/"/g, '&quot;');
      preflightChip = ` <span class="log-pf log-pf-bad" title="${t}">✗</span>`;
    } else if (status === 'FILLED' || status === 'SHADOW_OK') {
      preflightChip = ` <span class="log-pf log-pf-ok" title="Preflight OK">✓</span>`;
    }
    // Detail chips — keyed via the shared chipsHtml helper so order rows
    // and agent rows render the same key:value pattern. order param
    // fixes display order ahead of chipsHtml's insertion-order default.
    const chips = chipsHtml({
      status:  o.status || null,
      chase:   (o.attempts != null && o.attempts > 0) ? `#${o.attempts}` : null,
      engine:  o.engine || null,
    }, { order: ['status', 'chase', 'engine'] });
    // Agent-id chip stays bespoke (it's an <a>, not a plain span — the
    // chipsHtml helper deliberately doesn't emit anchors). No leading
    // space — the .log-agent-chip CSS uses a tiny 0.02rem margin so it
    // sits flush against the last log-chip, keeping the right-edge
    // cluster compact.
    const agentChip = o.agent_id
      ? `<a class="log-agent-chip" href="/agents?focus=${o.agent_id}">agent #${o.agent_id}</a>`
      : '';
    const chipsBlock = chips ? ' ' + chips : '';
    // Symbol as a clickable/right-clickable span. data-sym / data-exch drive
    // event delegation in the <pre> click + contextmenu handlers below.
    const symSpan = o.symbol
      ? `<span class="log-sym-cell" role="button" tabindex="0" data-sym="${_escAttr(o.symbol)}" data-exch="${_escAttr(o.exchange || '')}" title="${_escAttr(o.symbol)}">${_escAttr(o.symbol)}</span>`
      : '';
    const symBlock = o.symbol ? symSpan : '';
    return `<span class="${rowCls}">${t} ${tag}◆ ${o.transaction_type} ${o.quantity} ${symBlock} ${price} · ${o.account}${preflightChip}${chipsBlock}${agentChip}</span>`;
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
    // Order rows from the internal stream — the Terminal tab interleaves
    // operator commands with the order lifecycle they produced.
    const orderLines = (orderRows || []).map(o => ({
      ts: _shortTime(o.created_at), html: _orderRowHtml(o),
    }));
    const agentLines = (agentLog || []).map(e => {
      const t = _dualTsHtml(e.timestamp);
      const simPill = e.sim_mode ? '<span class="log-sim-pill" title="Simulator entry — not live activity">SIM</span> ' : '';
      return { ts: _shortTime(e.timestamp), html: `<span class="log-agent-default">${t} ${simPill}${e.event_type||''} ${e.trigger_condition||''}</span>` };
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
  {#each VISIBLE_TABS as [id, label]}
    <button onclick={() => setTab(id)}
      class="log-tab-btn border-b-2 transition-colors
        {logTab === id ? 'border-[#d97706] text-[#fbbf24]' : 'border-transparent text-[#b4c8e6] hover:text-[#fbbf24]'}"
    >{label}</button>
  {/each}
</div>

{#if logTab === 'news'}
  <!-- News tab — rendered via shared NewsList component in algo palette. -->
  <div class="log-panel log-news-panel {heightClass}">
    <NewsList
      limit={50}
      showRefreshTime={true}
      pollMs={10 * 60 * 1000}
      emptyMessage="No headlines."
    />
  </div>
{:else if logTab === 'order'}
  <!-- Order tab — [All] uses the unified merged feed (orders + agent
       fires). Paper / Live / Sim filter to mode-specific algo_orders
       rows so operators can isolate "did my paper test order land?"
       or "what did live place?" without agent-fire interleaving. -->
  <div class="om-bar">
    {#each _ORDER_MODE_TABS as [val, label]}
      <button class="om-chip {orderModeFilter === val ? 'om-on' : ''} om-chip-{val}"
              onclick={() => orderModeFilter = /** @type {any} */ (val)}>
        {label}
      </button>
    {/each}
  </div>
  {#if orderModeFilter === 'all'}
    <UnifiedLog
      filter={{}}
      pollMs={pollMs}
      maxRows={50}
      heightClass="log-panel log-unified {heightClass}"
    />
  {:else}
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions a11y_click_events_have_key_events -->
    <pre class="log-panel {heightClass}"
         role="log"
         onclick={(e) => {
           const sym = /** @type {HTMLElement|null} */ (/** @type {HTMLElement} */ (e.target).closest?.('.log-sym-cell'));
           if (sym) {
             e.stopPropagation();
             _symPanelSym  = sym.dataset.sym  || '';
             _symPanelExch = sym.dataset.exch || '';
             return;
           }
           const btn = /** @type {HTMLElement|null} */ (/** @type {HTMLElement} */ (e.target).closest?.('.log-chart-btn'));
           if (!btn) return;
           e.stopPropagation();
           _chartModalSym  = btn.dataset.sym  || '';
           _chartModalExch = btn.dataset.exch || '';
         }}
         oncontextmenu={(e) => {
           const sym = /** @type {HTMLElement|null} */ (/** @type {HTMLElement} */ (e.target).closest?.('.log-sym-cell'));
           if (!sym) return;
           e.preventDefault();
           e.stopPropagation();
           _ctxMenu = { symbol: sym.dataset.sym || '', exchange: sym.dataset.exch || '', x: e.clientX, y: e.clientY };
         }}>{#if filteredOrderRows.length}{@html filteredOrderRows.map(_orderRowHtml).join('\n')}{:else}<span class="log-debug">No {orderModeFilter} orders.</span>{/if}</pre>
  {/if}
{:else}
<pre class="log-panel {heightClass}">{#if logTab === 'terminal'}{@html _terminalHtml()}{:else if logTab === 'agent'}{#if agentLog.length}{@html agentLog.map(e => {
  const t = _dualTsHtml(e.timestamp);
  const cls = e.event_type === 'action_failed'  ? 'log-agent-failed'
            : e.event_type === 'action_success' ? 'log-agent-success'
            : e.event_type === 'cooldown'        ? 'log-agent-cooldown'
            : 'log-agent-default';
  // SIM indicator — sim_mode=true rows interleave with live rows on
  // this tab; the pill makes them impossible to confuse with real
  // agent activity. Amber matches the SIMULATOR banner palette.
  const simPill = e.sim_mode ? '<span class="log-sim-pill" title="Simulator entry — not live activity">SIM</span> ' : '';
  // trigger_condition is usually a JSON object like
  //   {metric:'pnl', scope:'positions.total', op:'<=', value:-50000}
  // — render as the same key:value chip pattern the order rows use
  // (chipsFromJson silently falls through for plain-text triggers).
  const cond = chipsFromJson(e.trigger_condition);
  const condBlock = cond ? ' ' + cond : '';
  return `<span class="${cls}">${t} ${simPill}${e.event_type||''}${condBlock}</span>`;
}).join('\n')}{:else}<span class="log-debug">No agent events.</span>{/if}{:else if logTab === 'simulator'}{#if simLog.length}{@html simLog.map(_renderSimLine).join('\n')}{:else}<span class="log-debug">No simulator ticks. Start a scenario at /admin/simulator to stream price changes here.</span>{/if}{:else}{#if systemLog.length}{@html systemLog.map(l => {
  // System log lines carry a leading 'YYYY-MM-DD HH:MM:SS' timestamp
  // (UTC — the prod box runs in UTC). We pass the parsed Date through
  // _dualTsHtml so the row gets the same page-header timestamp format
  // every other tab uses. If parse fails (unexpected) the helper still
  // renders a muted '—' chip so the column position stays aligned.
  const d = parseLogLineDate(l);
  const tHtml = _dualTsHtml(d);
  const rest = d ? stripTs(l) : l;
  // [SIM] marker surface as a pill so sim-source entries are visually
  // distinct from live system entries on the same tab.
  const simPill = /\[SIM\]/.test(l) ? '<span class="log-sim-pill" title="Simulator log line">SIM</span> ' : '';
  return `<span class="${sysClass(l)}">${tHtml} ${simPill}${rest}</span>`;
}).join('\n')}{:else}<span class="log-debug">No log entries.</span>{/if}{/if}</pre>
{/if}

{#if _chartModalSym}
  <ChartModal
    symbol={_chartModalSym}
    exchange={_chartModalExch}
    onClose={() => { _chartModalSym = ''; _chartModalExch = ''; }} />
{/if}

{#if _symPanelSym}
  <SymbolPanel
    symbol={_symPanelSym}
    exchange={_symPanelExch}
    onSubmit={() => {}}
    onClose={() => { _symPanelSym = ''; _symPanelExch = ''; }}
  />
{/if}

{#if _ctxMenu}
  <SymbolContextMenu
    symbol={_ctxMenu.symbol}
    exchange={_ctxMenu.exchange}
    x={_ctxMenu.x}
    y={_ctxMenu.y}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      _ctxAction = /** @type {any} */ (action);
      _ctxMenu = null;
    }}
  />
{/if}

{#if _ctxAction === 'chart'}
  <ChartModal
    symbol={_ctxSym}
    exchange={_ctxExch}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'place-order'}
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    onSubmit={() => {}}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'log'}
  <ActivityLogModal onClose={() => { _ctxAction = null; }} />
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

  /* Unified-log container inside the LogPanel — matches the <pre>
     visual context (same background, same overflow) but is a <div>
     so UnifiedLog can use flex layout internally. */
  :global(.log-unified) {
    padding: 0.3rem 0.5rem !important;
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

  /* Order-status row classes — supplement the existing log-agent-* set.
     CANCELLED: muted slate — the order is gone but not an error.
     REJECTED: stronger red than UNFILLED — broker hard-rejected.
     SHADOW_OK: orange — matches the SHADOW mode pill, not a fill. */
  :global(.log-order-cancelled) { color: #7e97b8; }
  :global(.log-order-rejected)  { color: #f87171; }
  :global(.log-order-shadow-ok) { color: #fb923c; }

  /* Order-tab mode subnav — [All / Paper / Live / Sim] chip strip.
     Colours mirror the .mode-pill-* tokens so the subnav reads as the
     same vocabulary the mode pills inside the rows already use. */
  .om-bar {
    display: inline-flex;
    gap: 0;
    padding: 0.18rem 0;
    margin-bottom: 0.2rem;
    border-bottom: 1px dashed rgba(251,191,36,0.12);
  }
  .om-chip {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.10);
    border-right-width: 0;
    padding: 0.1rem 0.55rem;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: rgba(200,216,240,0.55);
    cursor: pointer;
    transition: background 0.1s, color 0.1s;
  }
  .om-chip:first-child { border-top-left-radius: 3px; border-bottom-left-radius: 3px; }
  .om-chip:last-child  { border-right-width: 1px; border-top-right-radius: 3px; border-bottom-right-radius: 3px; }
  .om-chip:hover { color: #c8d8f0; }
  .om-chip.om-on.om-chip-all   { background: rgba(251,191,36,0.14); color: #fbbf24; border-color: rgba(251,191,36,0.45); }
  .om-chip.om-on.om-chip-paper { background: rgba(56,189,248,0.14); color: #7dd3fc; border-color: rgba(56,189,248,0.45); }
  .om-chip.om-on.om-chip-live  { background: rgba(16,185,129,0.14); color: #6ee7b7; border-color: rgba(16,185,129,0.45); }
  .om-chip.om-on.om-chip-sim   { background: rgba(251,191,36,0.14); color: #fbbf24; border-color: rgba(251,191,36,0.45); }

  /* Preflight verdict chip — ✓ when basket_margin / Kite preflight
     accepted the order, ✗ when it pushed back. Hover the chip to see
     the broker's reason in the title attribute. */
  :global(.log-pf) {
    display: inline-block;
    margin: 0 0.15rem;
    padding: 0 0.25rem;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 800;
    border-radius: 2px;
    cursor: help;
  }
  :global(.log-pf-ok)  { color: #4ade80; background: rgba(74,222,128,0.10); }
  :global(.log-pf-bad) { color: #f87171; background: rgba(248,113,113,0.12); }

  /* Chart icon inside the <pre> log rows — inline-flex so it sits flush
     next to the monospace text without breaking the line. Overrides the
     global .row-chart-btn margin-left with a tighter value for the dense
     log context. SVG pointer-events: none ensures target.closest() always
     lands on the <button>, not the inner <path>. */
  :global(.log-chart-btn) {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    vertical-align: middle;
    width: 1rem;
    height: 1rem;
    margin: 0 0.18rem 0 0.08rem;
    padding: 0;
    border: 1px solid rgba(34, 211, 238, 0.45);
    background: rgba(34, 211, 238, 0.12);
    color: #22d3ee;
    border-radius: 2px;
    cursor: pointer;
    flex-shrink: 0;
    line-height: 1;
  }
  :global(.log-chart-btn:hover) {
    background: rgba(34, 211, 238, 0.22);
    border-color: rgba(103, 232, 249, 0.65);
    color: #67e8f9;
  }
  :global(.log-chart-btn svg) { pointer-events: none; }

  /* Symbol span inside log rows — acts as a clickable affordance.
     Underline on hover keeps it scan-tight (no icon, just text). */
  :global(.log-sym-cell) {
    cursor: pointer;
    border-radius: 2px;
    transition: color 0.1s;
  }
  :global(.log-sym-cell:hover) {
    color: #7dd3fc;
    text-decoration: underline;
  }

</style>
