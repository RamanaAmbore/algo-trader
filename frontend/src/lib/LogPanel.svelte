<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { parseLogLineTime, parseLogLineDate, logTime, formatDualTz, executionMode } from '$lib/stores';
  import {
    fetchRecentAgentEvents, fetchSimEvents,
    fetchSimTicks, fetchAdminLogs, fetchAlgoOrdersRecent,
    fetchOrders, cancelOrder,
  } from '$lib/api';
  import NewsList from '$lib/NewsList.svelte';
  import { priceFmt, aggCompact } from '$lib/format';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import OrderCard from '$lib/order/OrderCard.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import { chipsHtml, chipsFromJson } from '$lib/logChips';
  import ChartModal from '$lib/ChartModal.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';

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
   *   gateByMode?: boolean,
   * }} */
  let {
    heightClass = 'flex-1 min-h-0',
    // Canonical order: Orders → Agents → Terminal → Ticks → System → News.
    // Every LogPanel mount inherits this — drop the explicit `tabs=`
    // prop at callsites unless a page genuinely needs a subset
    // (e.g. /console hiding Order in a future iteration).
    tabs        = ['order','agent','terminal','simulator','system','news'],
    defaultTab  = 'order',
    simScope    = false,
    pollMs      = 3000,
    cmdHistory  = [],
    onTabChange = () => {},
    mode        = /** @type {string | null} */ (null),
    // When true (default), all activity tabs are scoped to the current
    // executionMode from the global store. Set to false on surfaces that
    // deliberately want a cross-mode view (e.g. dashboard UnifiedLog).
    gateByMode  = true,
  } = $props();

  let logTab = $state(defaultTab);
  // Re-sync logTab whenever the parent updates defaultTab (e.g. /agents
  // calling runInSim flips defaultTab to 'simulator'). Read logTab via
  // untrack() so the operator's own tab clicks (setTab → logTab = id)
  // don't re-trigger this effect — without untrack the comparison sees
  // the new tab, decides it differs from defaultTab, and slams the
  // tab back to 'order' (or whatever defaultTab was). Result was that
  // clicking Agents / Terminal / Ticks etc. inside any LogPanel mount
  // immediately reverted to the Orders tab.
  $effect(() => {
    if (defaultTab && defaultTab !== untrack(() => logTab)) logTab = defaultTab;
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

  // Derived row arrays for the keyed {#each} renderers below —
  // operator-visible benefit: text selection inside an agent /
  // sim / system row is no longer wiped on every poll (audit
  // defect #11 — the old @html-joined-string approach destroyed
  // DOM identity per row). Each entry carries a stable `key` so
  // Svelte can diff per row; the `html` string is the same as
  // _logRow() output but wrapped one DOM node at a time.
  const _agentRows = $derived.by(() => {
    return agentLog.slice()
      .sort((a, b) => _tsKey(b.timestamp) - _tsKey(a.timestamp))
      .map((e, i) => {
        const cls = e.event_type === 'action_failed'  ? 'log-agent-failed'
                  : e.event_type === 'action_success' ? 'log-agent-success'
                  : e.event_type === 'cooldown'        ? 'log-agent-cooldown'
                  : 'log-agent-default';
        const cond = chipsFromJson(e.trigger_condition) || '';
        const tag  = (e.event_type || '').replace(/_/g, ' ');
        return {
          key: e.id != null ? `a${e.id}` : `a${e.timestamp || ''}-${i}`,
          html: _logRow(e.timestamp, cond, tag, cls),
        };
      });
  });
  const _simRows = $derived.by(() => {
    return simLog.slice()
      .sort((a, b) => _tsKey(b.ts) - _tsKey(a.ts))
      .map((entry, i) => ({
        key: `s${entry.ts || ''}-${entry.kind || ''}-${i}`,
        html: _renderSimLine(entry),
      }));
  });
  const _sysRows = $derived.by(() => {
    return systemLog.slice()
      .map(l => ({ l, d: parseLogLineDate(l) }))
      .sort((a, b) => _tsKey(b.d) - _tsKey(a.d))
      .map(({ l, d }, i) => {
        const rest = d ? stripTs(l) : l;
        const levelMatch = String(rest || '').match(/^(ERROR|WARN(?:ING)?|INFO|DEBUG)\b/i);
        const tag = levelMatch ? levelMatch[1].toUpperCase() : '';
        return {
          // System log keys can't use timestamp alone (multiple lines
          // per second possible); compose with line content hash via
          // a length+first-32-chars tuple — cheap to compute, stable
          // across polls for the same source line.
          key: `y${(d ? +d : 0)}-${String(l).length}-${String(l).slice(0, 32)}`,
          html: _logRow(d || null, rest, tag, sysClass(l)),
        };
      });
  });

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
      // Sim mode (via simScope prop OR gateByMode+executionMode=sim):
      // use the sim-only event stream. All other modes share the
      // real-mode stream (AgentEvent only has sim_mode bool, no
      // paper/live/shadow field).
      const wantSim = simScope || (gateByMode && $executionMode === 'sim');
      const data = wantSim ? await fetchSimEvents(100)
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
    // Merge broker orders + algo orders so the Orders tab carries the
    // same data the /orders page renders PLUS the platform's paper /
    // sim / shadow rows that never reach the broker. Broker rows win
    // on duplicate order_id (most complete fields); algo-only rows
    // (paper / sim / shadow) keep their AlgoOrderInfo shape and are
    // distinguished by .mode chip on the OrderCard.
    try {
      const [brokerResp, algoResp] = await Promise.allSettled([
        fetchOrders(),
        fetchAlgoOrdersRecent(100, 'all'),
      ]);
      const brokerRows = (brokerResp.status === 'fulfilled' && Array.isArray(brokerResp.value?.rows))
        ? brokerResp.value.rows
        : [];
      const algoRows = (algoResp.status === 'fulfilled' && Array.isArray(algoResp.value))
        ? algoResp.value
        : [];
      // Dedup: keep every broker row by order_id; only include algo
      // rows whose order_id isn't already in the broker set.
      const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
      const algoOnly  = algoRows.filter(o => {
        const oid = String(o?.order_id || o?.id || '');
        return !brokerIds.has(oid);
      });
      // Newest first — broker orders carry order_timestamp; algo rows
      // carry created_at. Date.parse falls back to 0 on bad strings so
      // empty/null timestamps land at the bottom.
      const merged = [...brokerRows, ...algoOnly];
      merged.sort((a, b) => {
        const ta = Date.parse(a.order_timestamp || a.created_at || '') || 0;
        const tb = Date.parse(b.order_timestamp || b.created_at || '') || 0;
        return tb - ta;
      });
      orderRows = merged;
    } catch (_) { /* keep last-good */ }
  }

  onMount(() => {
    if (tabs.includes('agent'))     _every(_loadAgents);
    if (tabs.includes('system'))    _every(_loadSystem);
    if (tabs.includes('simulator')) _every(_loadSim);
    // Orders + Terminal tabs both consume orderRows; load whenever
    // either one is in the visible set.
    if (tabs.includes('order') || tabs.includes('terminal'))
      _every(_loadOrders);
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

  // ── Order-card inline actions (Cancel / Modify) ──────────────────────────
  // Cancel hits the broker DELETE endpoint directly; Modify dispatches a
  // custom event so the host page can open its own modify modal (LogPanel
  // is mounted in many surfaces; they own the modify flow).
  /** @type {Set<string>} */
  let _cancelling = $state(new Set());
  /** @type {string} */
  let _cancelErr  = $state('');

  /** Returns true when the order is an OPEN broker order that can be acted on. */
  function _isOpenBroker(/** @type {any} */ o) {
    const st = (o?.status || '').toUpperCase();
    return (st === 'OPEN' || st === 'TRIGGER PENDING') && !o?.mode;
  }

  async function _cancelRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (_cancelling.has(key)) return;
    _cancelling = new Set([..._cancelling, key]);
    _cancelErr = '';
    try {
      await cancelOrder(o.order_id, o.account, o.variety || 'regular');
      // Force immediate refresh so cancelled row disappears.
      await _loadOrders();
    } catch (e) {
      _cancelErr = /** @type {any} */ (e)?.message || 'cancel failed';
      setTimeout(() => { _cancelErr = ''; }, 3000);
    } finally {
      const next = new Set(_cancelling);
      next.delete(key);
      _cancelling = next;
    }
  }

  // Dispatches a custom DOM event so the host can open its modify modal.
  // Hosts that don't listen receive a no-op (bubbles up and is ignored).
  function _requestModify(/** @type {any} */ o, /** @type {HTMLElement | null} */ el) {
    el?.dispatchEvent(new CustomEvent('lp:modify-order', {
      detail: o,
      bubbles: true,
      composed: true,
    }));
  }

  /** Escape HTML attribute values from broker-supplied strings (defence-in-depth). */
  const _escAttr = (/** @type {any} */ v) =>
    String(v || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');

  // Tab labels are noun-style content descriptors (Orders / Agents / etc.).
  // The simulator tab carries the per-tick event stream from the sim
  // driver (price changes, sim-tick-generated orders), so it's labelled
  // 'Ticks' — the SIM pill on each row already conveys the mode. Earlier
  // 'Simulator' overlapped with the same SIM pill on filtered Orders rows.
  const _ALL_TABS = [
    ['order',     'Orders'],
    ['agent',     'Agents'],
    ['terminal',  'Terminal'],
    ['simulator', 'Ticks'],
    ['system',    'System'],
    ['news',      'News'],
  ];
  // Filter to the per-page subset (defaults to all six). Lets /console
  // hide Order / Simulator / Agent that don't apply.
  // Additionally, when gateByMode is active and the current mode is not
  // sim, hide the Ticks (simulator) tab — ticks are sim-only by definition.
  const VISIBLE_TABS = $derived(_ALL_TABS.filter(([id]) => {
    if (!tabs.includes(id)) return false;
    if (id === 'simulator' && _gatingMode && _gatingMode !== 'sim') return false;
    return true;
  }));

  // ── Mode gating via executionMode store ──────────────────────────────
  // When gateByMode is true, the global executionMode store is the
  // implicit filter for Orders, Agent, and Terminal tabs. The mode chip
  // strip (om-bar) is hidden — mode is already shown in the header chip.
  // Ticks tab is hidden entirely when not in sim mode.

  // The effective mode for gating: null means "show everything" (gateByMode
  // is false). When gateByMode is true, read the live store value.
  const _gatingMode = $derived(gateByMode ? $executionMode : null);

  // Terminal mode prefix — maps execution mode to the [TAG] prefix used
  // in terminal detail strings. Events without any known prefix are global
  // and always shown.
  const _TERMINAL_PREFIXES = ['[SIM]', '[PAPER]', '[LIVE]', '[SHADOW]', '[REPLAY]'];
  /** @param {string} mode */
  function _terminalPrefixFor(mode) {
    const map = {
      sim:    '[SIM]',
      paper:  '[PAPER]',
      live:   '[LIVE]',
      shadow: '[SHADOW]',
      replay: '[REPLAY]',
    };
    return map[mode] || null;
  }

  // Ticks tab: hide when gating is active and mode is not 'sim'.
  // If the operator is on the ticks tab and the mode changes away from
  // sim, auto-flip to 'order'.
  $effect(() => {
    if (_gatingMode && _gatingMode !== 'sim' && untrack(() => logTab) === 'simulator') {
      logTab = 'order';
    }
  });

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
  // Account multi-select filter — matches the /orders Order Activity
  // card. Operator: "order activity tabs have all accounts in order
  // page. the order modal should also have it"; "keep the activity
  // panel of orders page should be in sync with activity panel of
  // orders modal". Empty array = no account filter (show every row);
  // any selection narrows the order grid to just those accounts.
  /** @type {string[]} */
  let orderAccountFilter = $state([]);
  const _availableAccounts = $derived.by(() => {
    const s = new Set();
    for (const o of orderRows || []) {
      const a = String(o?.account || '').trim();
      if (a) s.add(a);
    }
    return [...s].sort();
  });
  const filteredOrderRows = $derived.by(() => {
    let rows = orderRows || [];
    // When gateByMode is active, filter by executionMode directly.
    // The om-bar chip strip is hidden in this state so orderModeFilter
    // is not applicable — the store is the filter.
    if (_gatingMode) {
      rows = rows.filter(o => (o?.mode || 'live') === _gatingMode);
    } else if (orderModeFilter !== 'all') {
      rows = rows.filter(o => (o?.mode || 'live') === orderModeFilter);
    }
    if (orderAccountFilter.length > 0) {
      const want = new Set(orderAccountFilter);
      rows = rows.filter(o => want.has(String(o?.account || '')));
    }
    return rows;
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
    // Kite's broker order_timestamp uses "YYYY-MM-DD HH:MM:SS" (IST,
    // no T separator, no Z suffix). Normalise the space to T + tag
    // as IST so the resulting Date instance is in the correct zone.
    // The existing ISO path with T / Z is unchanged.
    const d = input instanceof Date
      ? input
      : (typeof input === 'string' && /^\d{4}-\d{2}-\d{2}/.test(input))
        ? new Date(
            input.includes('T') || input.includes('Z')
              ? input
              : input.replace(' ', 'T') + '+05:30'
          )
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
    const scen = entry.scenario || '';
    if (entry.kind === 'started') {
      return _logRow(entry.ts, `▶ ${scen} · ${entry.note || ''}`, 'START', 'log-agent-success');
    }
    if (entry.kind === 'stopped') {
      return _logRow(entry.ts, `■ ${scen} · ${entry.note || ''}`, 'STOP', 'log-info');
    }
    if (entry.kind === 'order') {
      const o = entry.order || {};
      const sideCls = o.side === 'BUY' ? 'log-agent-success' : 'log-info';
      const price   = (o.price != null) ? '@' + priceFmt(o.price) : '';
      // Ticks tab is sim-only by definition (entries come from the sim
      // driver's tick log) — drop the SIM mode pill since every row
      // already implies that mode.
      return _logRow(
        entry.ts,
        `◆ ${o.side || '?'} ${o.qty ?? '?'} ${o.symbol || '?'} ${price} · ${o.account || '?'} · ${o.agent || ''} ${o.action || ''}`,
        'ORDER',
        sideCls,
      );
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
    return _logRow(entry.ts, `${head} ${diffs || '(no changes)'}`, 'TICK', cls);
  }

  function stripTs(l) {
    return String(l ?? '').replace(/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*-?\s*/, '');
  }

  /**
   * Page-header dual-zone timestamp ("Sun 30 May · 21:42 IST · 12:12
   * EDT") rendered into a log row's time chip via the same
   * `formatDualTz()` the `.algo-ts` wall clock uses, so the operator
   * sees the exact format above and below the chrome. Operator:
   * "add timestamp in page header timestamp format for agents
   * terminal ticks system".
   */
  function _simpleTime(input) {
    if (!input) {
      return `<span class="log-row-time log-row-time-empty">—</span>`;
    }
    const d = input instanceof Date
      ? input
      : (typeof input === 'string' && /^\d{4}-\d{2}-\d{2}/.test(input))
        ? new Date(
            input.includes('T') || input.includes('Z')
              ? input
              : input.replace(' ', 'T') + '+05:30'
          )
        : null;
    if (d && !isNaN(d.getTime())) {
      return `<span class="log-row-time">${_escAttr(formatDualTz(d))}</span>`;
    }
    // Non-ISO fallback — pass the upstream-formatted string through
    // unchanged so it still reads in the same dual-zone column slot.
    const raw = typeof input === 'string' ? input : '—';
    return `<span class="log-row-time">${_escAttr(raw || '—')}</span>`;
  }

  /** Plain-text version of _simpleTime for use as React-style child
   *  text in {#each}-rendered rows where Svelte owns the wrapping
   *  span. Returns a string (or '—' fallback). */
  function _simpleTimeText(input) {
    if (!input) return '—';
    const d = input instanceof Date
      ? input
      : (typeof input === 'string' && /^\d{4}-\d{2}-\d{2}/.test(input))
        ? new Date(
            input.includes('T') || input.includes('Z')
              ? input
              : input.replace(' ', 'T') + '+05:30'
          )
        : null;
    if (d && !isNaN(d.getTime())) return formatDualTz(d);
    return typeof input === 'string' ? (input || '—') : '—';
  }

  /**
   * Wrap a log row in the unified News-style grid: time on the left,
   * message content in the middle (flex-grow), optional tag chip on
   * the right. All four tabs (Agent / Terminal / Ticks / System)
   * route through this helper so every row reads with the same shape.
   */
  function _logRow(timeInput, contentHtml, tagText, rowClass) {
    const ts  = _simpleTime(timeInput);
    const tag = tagText ? `<span class="log-row-tag">${_escAttr(String(tagText))}</span>` : '';
    return `<div class="log-row ${rowClass || ''}">${ts}<span class="log-row-msg">${contentHtml || ''}</span>${tag}</div>`;
  }

  /**
   * Numeric sort key for timestamp inputs. Accepts a Date, an ISO
   * string, or an upstream-formatted log line. Used by every tab's
   * rendering branch to sort rows latest-first (operator: "agents,
   * terminal, ticks, system, news should show the rows with latest
   * timestamps first in descending order"). Unparseable inputs sort
   * to 0 so they land at the bottom rather than scrambling the order.
   */
  function _tsKey(input) {
    if (!input) return 0;
    if (input instanceof Date) return +input || 0;
    if (typeof input !== 'string') return 0;
    if (/^\d{4}-\d{2}-\d{2}/.test(input)) {
      const norm = input.includes('T') || input.includes('Z')
        ? input
        : input.replace(' ', 'T') + '+05:30';
      return +new Date(norm) || 0;
    }
    return Date.parse(input) || 0;
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
    // Live orders come from Kite (fetchOrders → broker rows) and carry
    // `order_timestamp` rather than `created_at`. Algo / paper / sim
    // rows carry `created_at`. Read whichever exists so LIVE rows in the
    // Terminal tab don't lose their timestamp column.
    const t    = _dualTsHtml(o.created_at || o.order_timestamp);
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

  // Terminal tab mode filter — when gateByMode is active, only show events
  // whose detail text starts with the mode's [TAG] prefix OR has no known
  // mode prefix (global/system events are always shown).
  function _terminalMatchesMode(/** @type {string} */ detail) {
    if (!_gatingMode) return true;
    const prefix = _terminalPrefixFor(_gatingMode);
    const hasKnownPrefix = _TERMINAL_PREFIXES.some(p => detail.startsWith(p));
    if (!hasKnownPrefix) return true; // global event — always show
    return prefix ? detail.startsWith(prefix) : true;
  }

  function _terminalHtml() {
    // Each source contributes a row in the unified News-style grid
    // (time · message · tag). Tag is the source family — CMD / ORDER /
    // AGENT — so the operator can scan the Terminal stream at a glance.
    // Rows are kept as {ts, html} pairs so the merged stream can be
    // sorted latest-first across all three sources.
    const cmdLines = cmdHistory.map(h => {
      const cls = h.status === '✓' ? 'log-agent-success'
                : h.status === '✗' ? 'log-agent-failed'
                : 'log-info';
      const chips = h.fields ? Object.entries(h.fields)
        .map(([k, v]) => `<span class="log-chip"><span class="log-chip-key">${k}:</span>${v}</span>`)
        .join(' ') : '';
      const content = `${h.status || ''} ${h.message || ''} ${chips}`.trim();
      return { ts: h.time, html: _logRow(h.time, content, 'CMD', cls) };
    });
    // Order lines — filter by mode when gateByMode is active.
    const orderLines = (orderRows || [])
      .filter(o => {
        if (!_gatingMode) return true;
        return (o?.mode || 'live') === _gatingMode;
      })
      .map(o => {
        const status = (o.status || '').toUpperCase();
        let cls = 'log-info';
        if      (status === 'FILLED')          cls = 'log-agent-success';
        else if (status === 'UNFILLED')        cls = 'log-agent-failed';
        else if (status === 'OPEN')            cls = 'log-agent-alert';
        else if (o.transaction_type === 'BUY') cls = 'log-agent-success';
        const fillPrice = (o.fill_price != null) ? '@' + priceFmt(o.fill_price) : null;
        const initPrice = (o.initial_price != null) ? '@' + priceFmt(o.initial_price) : '';
        const price     = fillPrice || initPrice;
        const sym = o.symbol
          ? `<span class="log-sym-cell" role="button" tabindex="0" data-sym="${_escAttr(o.symbol)}" data-exch="${_escAttr(o.exchange || '')}" title="${_escAttr(o.symbol)}">${_escAttr(o.symbol)}</span>`
          : '';
        const content = `◆ ${o.transaction_type || '?'} ${o.quantity ?? '?'} ${sym} ${price} · ${o.account || '?'}`;
        const ts = o.created_at || o.order_timestamp;
        return { ts, html: _logRow(ts, content, 'ORDER', cls) };
      });
    // Agent lines — in sim mode show simLog entries; in real modes show agentLog.
    // detail text is used for terminal gating (substring match on [TAG] prefix).
    const agentLines = (agentLog || [])
      .filter(e => {
        const detail = String(e.trigger_condition || e.detail || '');
        return _terminalMatchesMode(detail);
      })
      .map(e => {
        const cond = chipsFromJson(e.trigger_condition) || (e.trigger_condition || '');
        return { ts: e.timestamp, html: _logRow(e.timestamp, cond, 'AGENT', 'log-agent-default') };
      });
    const all = [...cmdLines, ...orderLines, ...agentLines]
      .sort((a, b) => _tsKey(b.ts) - _tsKey(a.ts));
    return all.length ? all.map(x => x.html).join('') : '<div class="log-row log-debug"><span class="log-row-msg">No events.</span></div>';
  }

</script>

<div class="flex items-stretch mb-2 log-tab-row" style="border-bottom: 1px solid rgba(255,255,255,0.07);">
  <AlgoTabs
    tabs={VISIBLE_TABS.map(([id, label]) => ({ id, label }))}
    bind:value={logTab}
    onChange={onTabChange}
    compact={true}
  />
  {#if gateByMode && _gatingMode}
    <span class="lp-mode-chip mode-pill mode-pill-{_gatingMode}"
          title="Activity filtered to current execution mode"
          style="margin-left:auto;align-self:center;">
      {_gatingMode.toUpperCase()}
    </span>
  {/if}
</div>

{#if logTab === 'news'}
  <!-- News tab — rendered via shared NewsList component in algo palette. -->
  <div class="log-panel log-news-panel {heightClass}">
    <NewsList
      limit={50}
      showRefreshTime={true}
      pollMs={2 * 60 * 1000}
      emptyMessage="No headlines."
    />
  </div>
{:else if logTab === 'order'}
  <!-- Order tab — orders render as bordered cards via <OrderCard>, the
       same component the /orders Order Activity book uses, so the
       Activity-modal Orders tab and the dedicated /orders page format
       are visually identical. The mode filter strip below selects which
       AlgoOrder rows are shown (All shows everything — matching the
       "All status" card on /orders); previously this branch routed
       through UnifiedLog (which merged agent fires in) and the
       mode-specific branches rendered text spans inside a <pre>. -->
  <div class="om-bar">
    {#if !_gatingMode}
      <!-- Mode chip strip is hidden when gateByMode is active — the header
           chip already shows the active mode and the filter is implicit. -->
      {#each _ORDER_MODE_TABS as [val, label]}
        <button class="om-chip {orderModeFilter === val ? 'om-on' : ''} om-chip-{val}"
                onclick={() => orderModeFilter = /** @type {any} */ (val)}>
          {label}
        </button>
      {/each}
    {/if}
    {#if _availableAccounts.length > 1}
      <span class="om-acct-wrap">
        <AccountMultiSelect
          bind:value={orderAccountFilter}
          options={_availableAccounts.map(a => ({ value: a, label: a }))} />
      </span>
    {/if}
  </div>
  <div class="lp-order-scroll {heightClass}">
    {#if filteredOrderRows.length}
      <div class="oc-book-grid">
        {#each filteredOrderRows as o (o.order_id ?? o.id)}
          {@const _oKey = String(o.order_id || o.id || '')}
          <OrderCard order={o}
            onSymbolClick={(ord) => { _symPanelSym = ord.tradingsymbol || ord.symbol || ''; _symPanelExch = ord.exchange || ''; }}
            onSymbolContext={(ord, e) => { _ctxMenu = { symbol: ord.tradingsymbol || ord.symbol || '', exchange: ord.exchange || '', x: e.clientX, y: e.clientY }; }}>
            {#snippet actions(ord)}
              {#if _isOpenBroker(ord)}
                <div class="lp-oc-actions" role="group" aria-label="Order actions">
                  <!-- Modify — dispatches lp:modify-order so the host page opens its modal -->
                  <button type="button" class="lp-oc-btn lp-oc-modify"
                    title="Modify order"
                    aria-label="Modify"
                    onclick={(e) => { e.stopPropagation(); _requestModify(ord, e.currentTarget?.closest('.alm-body, .lp-order-scroll')); }}>
                    <!-- Pencil / edit glyph — cyan-400 -->
                    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path d="M11.5 2.5l2 2L5 13H3v-2L11.5 2.5z" stroke="currentColor" stroke-width="1.6"
                            stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                  </button>
                  <!-- Cancel — calls broker DELETE endpoint -->
                  <button type="button" class="lp-oc-btn lp-oc-cancel"
                    title="Cancel order"
                    aria-label="Cancel"
                    disabled={_cancelling.has(_oKey)}
                    onclick={(e) => { e.stopPropagation(); _cancelRow(ord); }}>
                    <!-- X glyph — red-400 -->
                    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                      <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"
                            stroke-linecap="round"/>
                    </svg>
                  </button>
                </div>
              {/if}
            {/snippet}
          </OrderCard>
        {/each}
        {#if _cancelErr}
          <div class="log-row log-agent-failed">{_cancelErr}</div>
        {/if}
      </div>
    {:else}
      <div class="log-debug py-2 text-center">No {_gatingMode || orderModeFilter} orders yet.</div>
    {/if}
  </div>
{:else}
<!-- Unified News-style row layout — every row is `[time · msg · tag]`
     so the four tabs (Agents · Terminal · Ticks · System) read with
     the same shape as the News tab above. Time chip inherits the
     page-header `.algo-ts` font (mono cyan tabular-nums) so the
     wall clock and every log row are in the same visual rhythm. -->
<!-- Each log row gets its own DOM node via keyed {#each}, so a
     poll-driven update only touches rows whose data actually
     changed. Audit defect #11 (the old @html-joined-string
     approach destroyed all row DOM identity on every poll,
     killing text selection inside the log). -->
<div class="log-panel log-rows {heightClass}">
  {#if logTab === 'terminal'}
    {@html _terminalHtml()}
  {:else if logTab === 'agent'}
    {#if _agentRows.length}
      {#each _agentRows as r (r.key)}{@html r.html}{/each}
    {:else}
      <div class="log-row log-debug"><span class="log-row-msg">No agent events yet.</span></div>
    {/if}
  {:else if logTab === 'simulator'}
    {#if _simRows.length}
      {#each _simRows as r (r.key)}{@html r.html}{/each}
    {:else}
      <div class="log-row log-debug"><span class="log-row-msg">No simulator ticks yet.</span></div>
    {/if}
  {:else}
    {#if _sysRows.length}
      {#each _sysRows as r (r.key)}{@html r.html}{/each}
    {:else}
      <div class="log-row log-debug"><span class="log-row-msg">No system entries yet.</span></div>
    {/if}
  {/if}
</div>
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

  /* Mode gate chip — right-aligned in the tab row, shows which execution
     mode is currently scoping all activity tabs. Reuses .mode-pill base;
     extra horizontal padding so it reads as a header label, not an inline
     order pill. Tooltip explains the filtering behaviour. */
  .lp-mode-chip {
    padding: 0.1rem 0.55rem !important;
    font-size: 0.52rem !important;
    letter-spacing: 0.07em;
    cursor: help;
  }

  /* Unified row layout for Agents · Terminal · Ticks · System tabs.
     Mirrors the News tab's `.newslist-row` grid so every log row reads
     with the same shape — time chip on the left, message in the
     middle, optional tag pill on the right. Time chip inherits the
     page-header `.algo-ts` font (mono cyan tabular-nums) but at log
     density (just HH:MM, full dual-zone in the tooltip) so it doesn't
     eat horizontal space the way the previous IST·EDT inline format
     did. Operator: "follow header timestamp in the same font so that
     it does not occupy a lot of space". */
  :global(.log-panel.log-rows) {
    /* Strip the legacy `<pre>` whitespace + monospace block constraints
       so `.log-row` flex children lay out as one row each. */
    white-space: normal;
    padding: 0.25rem 0.55rem;
    line-height: 1.35;
  }
  /* Operator: "for agents, terminal, ticks, and system, the row
     data is indented wasting space. Change the font of timestamp
     so that it occupied less space. make the row data to display
     in the next line of timestamp in the same row, so that you
     can use full available space."
     Layout split into two rows inside the same .log-row:
       row 1: [ time · TAG ]      (compact metadata strip)
       row 2: [ message ]          (full width, uses everything)
     Achieved via flex-wrap + per-child `order` so we don't have
     to restructure the markup. */
  :global(.log-panel.log-rows .log-row) {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    column-gap: 0.4rem;
    row-gap: 0.05rem;
    padding: 0.28rem 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    /* Operator: "agents, terminal, ticks and system text size
       should be equal to news tab text size of the data". News
       row is 0.72rem; matching here. */
    font-size: 0.72rem;
    color: var(--algo-slate);
    border-left: none;
    background: transparent;
  }
  :global(.log-panel.log-rows .log-row:last-child) { border-bottom: 0; }
  :global(.log-panel.log-rows .log-row:hover) { background: rgba(255, 255, 255, 0.02); }

  :global(.log-panel.log-rows .log-row-time) {
    flex: 0 0 auto;
    order: 0;
    font-family: ui-monospace, monospace;
    /* Matches .newslist-time (0.62rem) — same scan-fodder
       proportion as the news tab. */
    font-size: 0.62rem;
    color: #7dd3fc;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
    line-height: 1.1;
    opacity: 0.85;
  }
  :global(.log-panel.log-rows .log-row-time-empty) {
    color: var(--algo-muted);
    opacity: 0.55;
    font-style: italic;
  }
  :global(.log-panel.log-rows .log-row-tag) {
    flex: 0 0 auto;
    order: 1;
    font-family: ui-monospace, monospace;
    /* Bumped with the message size — same proportion as
       .newslist-src on the news tab. */
    font-size: 0.6rem;
    color: var(--algo-muted);
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.25);
    padding: 0 5px;
    border-radius: 2px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    white-space: nowrap;
    line-height: 1.3;
  }
  :global(.log-panel.log-rows .log-row-msg) {
    /* flex-basis: 100% pushes msg to its own line below time + tag */
    flex: 1 1 100%;
    order: 2;
    min-width: 0;
    word-break: break-word;
    line-height: 1.3;
  }
  /* Row-class semantics now carry through TEXT COLOR ONLY — no inside
     accent (left-border stripe, background tint) per operator
     feedback. Every row sits on the same flat surface, severity reads
     from the message colour so the three-column [time · msg · tag]
     grid stays uncluttered. */
  :global(.log-panel.log-rows .log-row.log-agent-success) { color: #4ade80; }
  :global(.log-panel.log-rows .log-row.log-agent-failed)  { color: #f87171; }
  :global(.log-panel.log-rows .log-row.log-agent-alert)   { color: #facc15; }
  :global(.log-panel.log-rows .log-row.log-agent-triggered) { color: #fb923c; }
  :global(.log-panel.log-rows .log-row.log-agent-cooldown)  { color: #94a3b8; }
  :global(.log-panel.log-rows .log-row.log-error)   { color: #f87171; }
  :global(.log-panel.log-rows .log-row.log-warning) { color: #fbbf24; }
  :global(.log-panel.log-rows .log-row.log-debug)   { color: #94a3b8; font-style: italic; }

  /* Orders-tab card grid — mirrors /orders' .oc-book-grid so the
     Activity-modal Orders tab and the dedicated /orders page lay out
     identically. One column on phone; two on tablet; three on desktop. */
  .lp-order-scroll {
    flex: 1 1 0;
    min-height: 0;
    overflow-y: auto;
    padding: 0.4rem 0.2rem;
  }
  .lp-order-scroll .oc-book-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.5rem;
  }
  @media (min-width: 640px) {
    .lp-order-scroll .oc-book-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 1024px) {
    .lp-order-scroll .oc-book-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
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
  /* LIVE pill — red to match the navbar LIVE badge + LIVE banner.
     Reads as "this row hit the real broker" at a glance. Earlier
     palette put LIVE on emerald which collided with REPLAY (also
     emerald), making the two indistinguishable on a quick scan. */
  :global(.mode-pill-live) {
    background: rgba(248,113,113,0.14);
    color: #f87171;
    border-color: rgba(248,113,113,0.45);
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
  :global(.log-order-cancelled) { color: var(--algo-muted); }
  :global(.log-order-rejected)  { color: #f87171; }
  :global(.log-order-shadow-ok) { color: #fb923c; }

  /* Order-tab mode subnav — [All / Paper / Live / Sim] chip strip.
     Colours mirror the .mode-pill-* tokens so the subnav reads as the
     same vocabulary the mode pills inside the rows already use. */
  .om-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.18rem 0;
    margin-bottom: 0.2rem;
    border-bottom: 1px dashed rgba(251,191,36,0.12);
    flex-wrap: wrap;
  }
  /* Mode chips and the account multi-select are siblings now —
     `.om-bar` flips from inline-flex to flex so the chip group on
     the left and the multi-select on the right co-exist on one
     row. The chip group keeps its segmented look via inline-flex
     on the wrapping span. */
  .om-bar > .om-chip ~ .om-chip { margin-left: 0; }
  .om-acct-wrap { display: inline-flex; min-width: 8rem; max-width: 16rem; }
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
  .om-chip:hover { color: var(--algo-slate); }
  .om-chip.om-on.om-chip-all    { background: rgba(251,191,36,0.14); color: #fbbf24; border-color: rgba(251,191,36,0.45); }
  .om-chip.om-on.om-chip-paper  { background: rgba(56,189,248,0.14); color: #7dd3fc; border-color: rgba(56,189,248,0.45); }
  /* Canonical green (#4ade80) for live — replaces off-palette emerald #10b981 */
  /* LIVE chip on the Order-tab mode filter — red to match LIVE pill
     + LIVE banner. Operator selects "Live" → red glow signals "I'm
     about to look at real-broker orders". */
  .om-chip.om-on.om-chip-live   { background: rgba(248,113,113,0.14); color: #f87171; border-color: rgba(248,113,113,0.45); }
  .om-chip.om-on.om-chip-sim    { background: rgba(251,191,36,0.14); color: #fbbf24; border-color: rgba(251,191,36,0.45); }
  /* Shadow — orange #fb923c, matching the mode pill palette */
  .om-chip.om-on.om-chip-shadow { background: rgba(251,146,60,0.14); color: #fb923c; border-color: rgba(251,146,60,0.45); }
  /* Replay — canonical green #4ade80 */
  .om-chip.om-on.om-chip-replay { background: rgba(74,222,128,0.14); color: #4ade80; border-color: rgba(74,222,128,0.45); }

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
    background: var(--algo-cyan-bg-strong);
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

  /* ── Inline Cancel / Modify action strip on OPEN broker OrderCards ─── */
  .lp-oc-actions {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    margin-top: 0.4rem;
  }
  .lp-oc-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    border-radius: 3px;
    cursor: pointer;
    background: none;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
    flex-shrink: 0;
  }
  .lp-oc-btn:disabled { opacity: 0.4; cursor: progress; }
  /* Modify — cyan-400 palette (matches all other edit affordances) */
  .lp-oc-modify {
    border: 1px solid rgba(34,211,238,0.45);
    color: #22d3ee;
  }
  .lp-oc-modify:hover:not(:disabled) {
    background: rgba(34,211,238,0.14);
    border-color: rgba(103,232,249,0.65);
    color: #67e8f9;
  }
  /* Cancel — red-400 palette (matches order rejection / kill affordances) */
  .lp-oc-cancel {
    border: 1px solid rgba(248,113,113,0.45);
    color: #f87171;
  }
  .lp-oc-cancel:hover:not(:disabled) {
    background: rgba(248,113,113,0.14);
    border-color: rgba(252,165,165,0.65);
    color: #fca5a5;
  }

</style>
