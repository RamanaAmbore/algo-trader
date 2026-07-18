<script>
  import { onDestroy, onMount, untrack } from 'svelte';
  import { parseLogLineTime, parseLogLineDate, logTime, formatDualTz, executionMode, visibleInterval } from '$lib/stores';
  import {
    fetchRecentAgentEvents, fetchSimEvents,
    fetchSimTicks, fetchAdminLogs, fetchAdminConnLogs, fetchAlgoOrdersRecent,
    fetchOrders, cancelOrder, reconcileSingleOrder,
  } from '$lib/api';
  import NewsList from '$lib/NewsList.svelte';
  import { priceFmt, aggCompact } from '$lib/format';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import OrderCard from '$lib/order/OrderCard.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import { chipsHtml, chipsFromJson } from '$lib/logChips';
  import ChartModal from '$lib/ChartModal.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import { openActivityModal } from '$lib/stores';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import ActivityHeaderFilters from '$lib/ActivityHeaderFilters.svelte';
  import BellIcon from '$lib/icons/BellIcon.svelte';
  import CollapseButton from '$lib/CollapseButton.svelte';
  import FullscreenButton from '$lib/FullscreenButton.svelte';
  import GridDownloadButton from '$lib/GridDownloadButton.svelte';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';

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
   *   statusFilter?: 'all'|'open'|'complete'|'rejected'|'cancelled',
   *   symbolFilter?: Set<string> | null,
   *   accountFilter?: string[],
   *   hideInlineAccountFilter?: boolean,
   *   availableAccounts?: string[],
   *   multiColumn?: boolean,
   *   levelFilter?: 'all'|'error'|'warning'|'info',
   *   activeTab?: string,
   *   context?: 'page'|'card'|'card-wide'|'modal',
   *   label?: string,
   *   isCollapsed?: boolean,
   *   isFullscreen?: boolean,
   *   onRefresh?: (() => void) | null,
   *   refreshLoading?: boolean,
   *   onDownload?: (() => void) | null,
   *   cardId?: string,
   *   onClose?: (() => void) | null,
   * }} */
  let {
    heightClass = 'flex-1 min-h-0',
    // Canonical order: Orders → Agents → Terminal → Ticks → System → News.
    // Every LogPanel mount inherits this — drop the explicit `tabs=`
    // prop at callsites unless a page genuinely needs a subset
    // (e.g. /console hiding Order in a future iteration).
    tabs        = ['order','agent','terminal','simulator','system','conn','news'],
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
    // Optional status filter from the /orders page counter cards.
    // 'all' (default) shows every row; any other value narrows to rows
    // whose status matches (case-insensitive). 'open' also matches
    // 'TRIGGER PENDING'.
    statusFilter = /** @type {'all'|'open'|'complete'|'rejected'|'cancelled'} */ ('all'),
    // Slice 7g — optional symbol scope. When non-null, the Order tab
    // narrows rows to those whose tradingsymbol is in the Set
    // (UPPER). Null = no symbol filter (every row passes). Used by
    // /orders + ActivityLogModal callers to thread the global
    // strategy filter into the order grid.
    symbolFilter = /** @type {Set<string> | null} */ (null),
    /**
     * Optional external account filter — used by ActivityLogModal so
     * the account dropdown can live in the modal header instead of
     * the tab row. When provided (bindable), LogPanel reads + writes
     * through this prop; when omitted, LogPanel falls back to its
     * internal $state and renders the inline dropdown.
     * @type {string[] | undefined}
     */
    accountFilter = $bindable(/** @type {string[] | undefined} */ (undefined)),
    /**
     * Hide the inline account dropdown in the tab row. Set by
     * ActivityLogModal which renders its own copy in the modal
     * header. Other mounts (orders strip, console, etc.) leave this
     * false and keep the inline behaviour.
     */
    hideInlineAccountFilter = false,
    /**
     * Optional bindable — receives the list of accounts present in
     * the current order rows so parents (ActivityLogModal) can show
     * a matching dropdown without re-deriving the list themselves.
     * @type {string[] | undefined}
     */
    availableAccounts = $bindable(/** @type {string[] | undefined} */ (undefined)),
    /**
     * Enable CSS multi-column flow for the Agents / Terminal / System /
     * Conn tabs on wide containers. Operator: "make agents, terminal,
     * system, conn lines on desktop, I want two lines in a single row
     * as there is more width available, similar to market news in
     * dashboard." Same magazine-style `column-count` NewsList uses.
     * Falls back to 1 column under 900px container width.
     */
    multiColumn = false,
    /**
     * Log-level filter applied to the active tab. Default 'all' keeps
     * pre-filter behaviour for callsites that don't pass the prop;
     * ActivityLogSurface defaults to 'error' so the Activity surfaces
     * land on actionable rows. When set, System / Conn rows are
     * filtered by line-start `(ERROR|WARNING|INFO|DEBUG)` token;
     * Agent rows by their event_type mapping; Order rows by status.
     */
    levelFilter = $bindable(/** @type {'all'|'error'|'warning'|'info'} */ ('all')),
    /**
     * Bindable — mirrors the internally-active tab id so parents
     * (ActivityLogSurface, ActivityLogModal, /activity page) can
     * derive filter visibility without duplicating tab logic.
     * One-way: LogPanel writes logTab → activeTab. Parents must NOT
     * write back or they'll fight with internal tab-click state.
     */
    activeTab = $bindable(/** @type {string} */ ('')),
    /**
     * Surface context — passed through from ActivityLogSurface.
     * Used to suppress the Fullscreen button when already in modal context.
     * @type {'page'|'card'|'card-wide'|'modal'}
     */
    context = /** @type {'page'|'card'|'card-wide'|'modal'} */ ('page'),
    /** Optional label shown in the tab row as a leading chip (e.g. "ACTIVITY").
     *  When provided, the tab row renders its own label + separator + card buttons.
     *  When empty (default), existing mounts get zero new chrome. */
    label           = /** @type {string} */ (''),
    /** Bindable collapse state — passed through when label is set. */
    isCollapsed     = $bindable(false),
    /** Bindable fullscreen state — passed through when label is set. */
    isFullscreen    = $bindable(false),
    /** Refresh callback — rendered as a button in lp-card-btns when provided. */
    onRefresh       = /** @type {(() => void) | null} */ (null),
    /** Bindable refresh-loading spinner state. */
    refreshLoading  = $bindable(false),
    /** Download callback — rendered as a button in lp-card-btns when provided. */
    onDownload      = /** @type {(() => void) | null} */ (null),
    /** Card id for CollapseButton localStorage persistence. */
    cardId          = /** @type {string} */ (''),
    /** Close callback — used in modal context to render a close button. */
    onClose         = /** @type {(() => void) | null} */ (null),
  } = $props();

  // Line-level helpers shared by every text-log tab (System, Conn).
  // Reused inside the derived row arrays so the filter runs once per
  // poll, not per render.
  function _lineLevel(/** @type {string} */ line) {
    const m = String(line || '').match(/-\s*(ERROR|WARN(?:ING)?|INFO|DEBUG)\b/i);
    if (!m) return 'info';
    const t = m[1].toUpperCase();
    if (t === 'ERROR') return 'error';
    if (t.startsWith('WARN')) return 'warning';
    return 'info';
  }
  function _lineMatchesLevel(/** @type {string} */ line) {
    // Treat anything outside {error, warning, info} as no-filter so a
    // prop default that didn't propagate (undefined / null / typo)
    // can't silently drop every conn_service row to a "no entries"
    // empty state. Operator: "conn tab does not have rows displayed."
    if (levelFilter !== 'error' && levelFilter !== 'warning' && levelFilter !== 'info') {
      return true;
    }
    return _lineLevel(line) === levelFilter;
  }
  // Match Kite/Dhan/Groww account patterns inside free-text logs —
  // e.g. `[ZG0790]`, `'DH3747'`, `account=ZJ6294`, `GR87DF`. Returns
  // the first match found or null when the line carries no account
  // context (e.g. a startup/shutdown log).
  function _lineAccount(/** @type {string} */ line) {
    const m = String(line || '').match(/\b([A-Z]{2}[0-9A-Z]{4})\b/);
    return m ? m[1] : null;
  }
  function _lineMatchesAccount(/** @type {string} */ line, /** @type {string[]} */ filter) {
    if (!filter || filter.length === 0) return true;
    const acct = _lineAccount(line);
    // No detectable account = broadcast event (e.g. KiteTicker connect
    // log) — keep visible so the operator doesn't lose context when
    // the filter is set.
    if (!acct) return true;
    return filter.includes(acct);
  }

  // intentional: defaultTab seeds the active tab; $effect below re-syncs on prop changes
  // svelte-ignore state_referenced_locally
  let logTab = $state($state.snapshot(defaultTab));
  // Re-sync logTab whenever the parent updates defaultTab (e.g. /automation
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
  // Mirror logTab → activeTab (one-way only). Parents read this to derive
  // filter visibility; they MUST NOT write back or they'd fight with
  // internal tab-click state.
  $effect(() => { activeTab = logTab; });

  // ── Card button group state ───────────────────────────────────────────
  let _searchOpen  = $state(false);
  let _searchQuery = $state('');
  let _expanded    = $state(false);

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
  let connLog   = $state(/** @type {string[]} */ ([]));
  let simLog    = $state(/** @type {any[]} */ ([]));

  // Derived row arrays for the keyed {#each} renderers below —
  // operator-visible benefit: text selection inside an agent /
  // sim / system row is no longer wiped on every poll (audit
  // defect #11 — the old @html-joined-string approach destroyed
  // DOM identity per row). Each entry carries a stable `key` so
  // Svelte can diff per row; the `html` string is the same as
  // _logRow() output but wrapped one DOM node at a time.
  // Map an agent event_type → log level so the header level filter
  // applies the same semantics across tabs. action_failed = error,
  // cooldown = warning, action_success / fired = info.
  function _agentLevel(/** @type {any} */ e) {
    const t = (e?.event_type || '').toLowerCase();
    if (t.includes('fail') || t.includes('error') || t.includes('reject')) return 'error';
    if (t.includes('cool') || t.includes('warn') || t.includes('skip'))    return 'warning';
    return 'info';
  }
  // ── Search helper ────────────────────────────────────────────────────
  // Text-match predicate for the card button group search. Strips HTML tags
  // from the row's `.html` string so the search works on visible text, not
  // on class names or SVG attribute noise.
  function _rowMatchesSearch(/** @type {string} */ html) {
    if (!_searchQuery) return true;
    const q = _searchQuery.toLowerCase();
    // Strip HTML so "AGENT" doesn't match a class name like "log-agent-success".
    const text = html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').toLowerCase();
    return text.includes(q);
  }

  const _agentRows = $derived.by(() => {
    return agentLog.slice()
      .filter(e => {
        if (levelFilter !== 'all' && _agentLevel(e) !== levelFilter) return false;
        if (orderAccountFilter.length > 0 && e.account
            && !orderAccountFilter.includes(String(e.account))) return false;
        return true;
      })
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
          _raw: e,
        };
      })
      .filter(r => _rowMatchesSearch(r.html));
  });
  const _simRows = $derived.by(() => {
    return simLog.slice()
      .sort((a, b) => _tsKey(b.ts) - _tsKey(a.ts))
      .map((entry, i) => ({
        key: `s${entry.ts || ''}-${entry.kind || ''}-${i}`,
        html: _renderSimLine(entry),
        _raw: entry,
      }))
      .filter(r => _rowMatchesSearch(r.html));
  });
  const _sysRows = $derived.by(() => {
    // Filter BEFORE the sort+map so the level + account checks run
    // once per line, not per render.
    return systemLog.slice()
      .filter(l => _lineMatchesLevel(l) && _lineMatchesAccount(l, orderAccountFilter))
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
          // Same each_key_duplicate guard as _connRows below — same
          // bug applies to System tab when api_log_file emits multiple
          // INFO lines in one second from the same logger.
          key: `y${(d ? +d : 0)}-${String(l).length}-${String(l).slice(0, 32)}-${i}`,
          html: _logRow(d || null, rest, tag, sysClass(l)),
          _rawLine: l,
        };
      })
      .filter(r => _rowMatchesSearch(r.html));
  });
  // Same shape as _sysRows but sourced from connLog. Keeps the
  // rendering loop simple — one #each per tab, no shared list with
  // a filter (which would re-sort + re-key on every poll of either
  // source).
  const _connRows = $derived.by(() => {
    return connLog.slice()
      .filter(l => _lineMatchesLevel(l) && _lineMatchesAccount(l, orderAccountFilter))
      .map(l => ({ l, d: parseLogLineDate(l) }))
      .sort((a, b) => _tsKey(b.d) - _tsKey(a.d))
      .map(({ l, d }, i) => {
        const rest = d ? stripTs(l) : l;
        const levelMatch = String(rest || '').match(/^(ERROR|WARN(?:ING)?|INFO|DEBUG)\b/i);
        const tag = levelMatch ? levelMatch[1].toUpperCase() : '';
        return {
          // Index appended to break ties — conn_service emits multiple
          // lines per second from the same logger module (e.g. four
          // Groww DEBUG calls inside one quote batch), producing the
          // same (timestamp, length, first-32-chars) tuple for several
          // rows. Svelte 5's each_key_duplicate guard threw a pageerror
          // that aborted the entire {#each} block, leaving the Conn tab
          // showing the empty-state sentinel even with 200 lines in
          // hand. Index is safe here: rows are sorted append-only by
          // timestamp; nothing depends on stable cross-poll identity.
          key: `c${(d ? +d : 0)}-${String(l).length}-${String(l).slice(0, 32)}-${i}`,
          html: _logRow(d || null, rest, tag, sysClass(l)),
          _rawLine: l,
        };
      })
      .filter(r => _rowMatchesSearch(r.html));
  });

  /** @type {Array<() => void>} */
  const _intervals = [];
  function _every(/** @type {() => Promise<void> | void} */ fn) {
    fn();
    if (pollMs > 0 && typeof document !== 'undefined') {
      // visibleInterval pauses while hidden and fires fn immediately on
      // tab return — log tabs stay fresh without background polling.
      const teardown = visibleInterval(() => { fn(); }, pollMs);
      _intervals.push(teardown);
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
  async function _loadConn() {
    try {
      const d = await fetchAdminConnLogs(200);
      connLog = d?.lines || [];
    } catch (e) {
      // Surface fetch errors as a sentinel row so an operator who
      // sees an empty Conn tab can diagnose. Previous silent catch
      // made every failure look like 'no entries yet' — indistinguishable
      // from a healthy-but-quiet conn_service.
      const msg = /** @type {any} */ (e)?.message || String(e);
      connLog = [`CONN_FETCH_ERROR ${new Date().toISOString()} ${msg}`];
    }
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

  // Deferred poll flags — system and sim ticks are low-traffic tabs that
  // most operators never visit in a session. Don't start their pollers on
  // mount; start them on first tab activation instead.
  let _sysPollStarted  = false;
  let _connPollStarted = false;
  let _simPollStarted  = false;

  onMount(() => {
    if (tabs.includes('agent')) _every(_loadAgents);
    // Orders + Terminal tabs both consume orderRows; load whenever
    // either one is in the visible set.
    if (tabs.includes('order') || tabs.includes('terminal'))
      _every(_loadOrders);
    // system + simulator polls are deferred — started in the $effect below
    // on first tab click so idle sessions pay zero cost for tabs never visited.
  });
  // Canonical account display order — subscribed so _availableAccounts
  // re-derives when fetchBrokerOrder() resolves after cold load.
  let _logOrderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubLogOrder = accountDisplayOrder.subscribe(m => { _logOrderMap = m; });
  onDestroy(() => {
    for (const teardown of _intervals) teardown();
    _unsubLogOrder();
  });

  // Lazy-start deferred tab pollers on first activation.
  $effect(() => {
    if (logTab === 'system' && !_sysPollStarted && tabs.includes('system')) {
      _sysPollStarted = true;
      _every(_loadSystem);
    }
    if (logTab === 'conn' && !_connPollStarted && tabs.includes('conn')) {
      _connPollStarted = true;
      _every(_loadConn);
    }
    if (logTab === 'simulator' && !_simPollStarted && tabs.includes('simulator')) {
      _simPollStarted = true;
      _every(_loadSim);
    }
  });

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

  /** Returns true when the order is in-flight and reconciling is meaningful. */
  function _isInFlight(/** @type {any} */ o) {
    const st = (o?.status || '').toUpperCase();
    return st === 'OPEN' || st === 'TRIGGER PENDING'
        || st === 'CANCEL_FAILED' || st === 'PARTIAL';
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

  // Per-order reconcile — sync ONE broker order against the broker book
  // and update the matching algo row when it disagrees (postback miss /
  // network drop / stuck OPEN row after REJECTED). Available on every
  // LogPanel mount so the Activity modal + /orders Activity card behave
  // identically.
  let _reconciling = $state(new Set());
  async function _reconcileRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (!key || !o?.account || _reconciling.has(key)) return;
    _reconciling = new Set([..._reconciling, key]);
    try {
      const res = await reconcileSingleOrder(o.order_id, o.account);
      if (res?.updated) await _loadOrders();
    } catch (e) {
      _cancelErr = /** @type {any} */ (e)?.message || 'reconcile failed';
      setTimeout(() => { _cancelErr = ''; }, 3000);
    } finally {
      const next = new Set(_reconciling);
      next.delete(key);
      _reconciling = next;
    }
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
    ['conn',      'Conn'],
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

  const _showAccountFilter = $derived(['order', 'agent', 'system', 'conn'].includes(logTab));
  const _showLevelFilter   = $derived(['agent', 'system', 'conn'].includes(logTab));

  // ── Mode gating via executionMode store ──────────────────────────────
  // When gateByMode is true, the global executionMode store is the
  // implicit filter for Orders, Agent, and Terminal tabs. The mode chip
  // strip (om-bar) is hidden — mode is already shown in the header chip.
  // Ticks tab is hidden entirely when not in sim mode.

  // The effective mode for gating: null means "show everything" (gateByMode
  // is false). When gateByMode is true, read the live store value.
  const _gatingMode = $derived(gateByMode ? $executionMode : null);
  // _terminalHtmlDerived memoises the terminal tab's HTML so it only
  // recomputes when its actual inputs change (cmdHistory, orderRows,
  // agentLog, _gatingMode) — not on every reactivity cycle that touches
  // the template. Without this, formatDualTz was called per-row on every
  // 3 s poll across all mounted instances even when the data was unchanged.
  const _terminalHtmlDerived = $derived.by(() => _terminalHtml());

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
  // Internal state used when the parent doesn't provide an account
  // filter prop. With Svelte 5 $bindable, accountFilter is undefined
  // when no parent binds it; we fall back to this local state so
  // existing callsites (orders strip, console) keep working.
  /** @type {string[]} */
  let _internalAccountFilter = $state([]);
  // Read/write helper that picks the prop when provided, otherwise
  // the internal state. Always returns the effective filter for the
  // filter-by-account predicate below.
  const orderAccountFilter = $derived(
    accountFilter !== undefined ? accountFilter : _internalAccountFilter,
  );

  const _availableAccounts = $derived.by(() => {
    const s = new Set();
    for (const o of orderRows || []) {
      const a = String(o?.account || '').trim();
      if (a) s.add(a);
    }
    return sortAccountsBy([...s], _logOrderMap);
  });
  // Mirror _availableAccounts into the parent's bindable when one
  // is provided. ActivityLogModal uses this to render its own
  // account dropdown in the modal header.
  //
  // Reading `availableAccounts` inside an effect that ALSO writes
  // to it produced an effect_update_depth_exceeded loop (the bind:
  // write was treated as a tracked dependency). Untrack the
  // existence check so the effect only tracks _availableAccounts.
  $effect(() => {
    const ax = _availableAccounts;
    if (untrack(() => availableAccounts) !== undefined) {
      availableAccounts = ax;
    }
  });
  // Sync _internalAccountFilter ↔ parent's accountFilter bindable.
  // When parent provides accountFilter, prime _internalAccountFilter
  // from it (one-time seed, then ActivityHeaderFilters owns local writes).
  $effect(() => {
    const ext = accountFilter;
    if (ext !== undefined) {
      const cur = untrack(() => _internalAccountFilter);
      // Only update when content differs to avoid loops.
      if (JSON.stringify(cur) !== JSON.stringify(ext)) {
        _internalAccountFilter = ext.slice();
      }
    }
  });
  // Push local writes back up to the parent's bindable.
  $effect(() => {
    const local = _internalAccountFilter;
    if (untrack(() => accountFilter) !== undefined) {
      accountFilter = local;
    }
  });
  // ── filteredOrderRows filter predicates ────────────────────────────────
  // Each _apply* helper is a pure function that returns a new rows array.
  // They are called in sequence inside filteredOrderRows.

  /**
   * Apply mode gate: when gateByMode is active use the execution-mode store;
   * otherwise apply the operator-selected orderModeFilter chip (skip on 'all').
   * @param {any[]} rows
   * @param {string|null} gatingMode
   * @param {string} modeFilter
   * @returns {any[]}
   */
  function _applyModeFilter(rows, gatingMode, modeFilter) {
    // When gateByMode is active, filter by executionMode directly.
    // The om-bar chip strip is hidden in this state so orderModeFilter
    // is not applicable — the store is the filter.
    if (gatingMode) {
      return rows.filter(o => (o?.mode || 'live') === gatingMode);
    }
    if (modeFilter !== 'all') {
      return rows.filter(o => (o?.mode || 'live') === modeFilter);
    }
    return rows;
  }

  /**
   * Narrow to rows belonging to the selected accounts.
   * Empty array = no filter (pass all through).
   * @param {any[]} rows
   * @param {string[]} accountFilter
   * @returns {any[]}
   */
  function _applyAccountFilter(rows, accountFilter) {
    if (accountFilter.length === 0) return rows;
    const want = new Set(accountFilter);
    return rows.filter(o => want.has(String(o?.account || '')));
  }

  /** @type {Record<string, (st: string) => boolean>} */
  const _STATUS_PREDICATES = {
    open:      st => st === 'OPEN' || st === 'TRIGGER PENDING',
    complete:  st => st === 'COMPLETE',
    rejected:  st => st === 'REJECTED',
    cancelled: st => st === 'CANCELLED',
  };

  /**
   * Status filter wired from the /orders page counter cards.
   * Pass 'all' or falsy to skip.
   * @param {any[]} rows
   * @param {string|null|undefined} filter
   * @returns {any[]}
   */
  function _applyStatusFilter(rows, filter) {
    if (!filter || filter === 'all') return rows;
    const pred = _STATUS_PREDICATES[filter];
    if (!pred) return rows;
    return rows.filter(o => pred((o?.status || '').toUpperCase()));
  }

  /**
   * Slice 7g — symbol filter (strategy scope). When set, narrow to rows
   * whose tradingsymbol is in the allowed Set. An empty Set with a non-null
   * instance signals "strategy picked but it has no open lots" — return []
   * rather than passing all rows through.
   * @param {any[]} rows
   * @param {Set<string>|null|undefined} filter
   * @returns {any[]}
   */
  function _applySymbolFilter(rows, filter) {
    if (!filter) return rows;
    if (filter.size === 0) return [];
    return rows.filter(o => {
      const sym = String(o?.tradingsymbol || o?.symbol || '').toUpperCase();
      return filter.has(sym);
    });
  }

  const filteredOrderRows = $derived.by(() => {
    let rows = orderRows || [];
    rows = _applyModeFilter(rows, _gatingMode, orderModeFilter);
    rows = _applyAccountFilter(rows, orderAccountFilter);
    rows = _applyStatusFilter(rows, statusFilter);
    // Used by /orders to thread the global strategy filter through.
    rows = _applySymbolFilter(rows, symbolFilter);
    // Card button group search filter — text match on symbol, account, status
    if (_searchQuery) {
      const q = _searchQuery.toLowerCase();
      rows = rows.filter(o => {
        const text = [
          o.symbol || o.tradingsymbol || '',
          o.account || '',
          o.status || '',
          o.transaction_type || '',
          o.order_id || o.id || '',
        ].join(' ').toLowerCase();
        return text.includes(q);
      });
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

  // Table-driven status → CSS class map. Colour by terminal state first,
  // then by side. FILLED = green, UNFILLED = red, OPEN = amber.
  const _ORDER_STATUS_CLASS = {
    FILLED:          'log-agent-success',
    UNFILLED:        'log-agent-failed',
    OPEN:            'log-agent-alert',
    CANCELLED:       'log-order-cancelled',
    REJECTED:        'log-order-rejected',
    SHADOW_OK:       'log-order-shadow-ok',
    SHADOW_REJECTED: 'log-order-rejected',
  };

  /** Resolve CSS row class for an order row (status-first, side fallback). */
  function _orderStatusClass(status, transactionType) {
    return _ORDER_STATUS_CLASS[status]
      ?? (transactionType === 'BUY' ? 'log-agent-success' : 'log-info');
  }

  /**
   * Preflight verdict chip — ✓/✗ with title= carrying the detail.
   * Returns '' for ambiguous states so it doesn't cry wolf on OPEN rows.
   */
  function _preflightChipHtml(status, detail) {
    if (status === 'REJECTED' || status === 'SHADOW_REJECTED') {
      const t = (detail || 'preflight blocked').replace(/"/g, '&quot;');
      return ` <span class="log-pf log-pf-bad" title="${t}">✗</span>`;
    }
    if (status === 'FILLED' || status === 'SHADOW_OK') {
      return ` <span class="log-pf log-pf-ok" title="Preflight OK">✓</span>`;
    }
    return '';
  }

  /**
   * Symbol as a clickable span. data-sym / data-exch drive event
   * delegation in the <pre> click + contextmenu handlers.
   * Returns '' when no symbol is set.
   */
  function _orderSymSpan(sym, exch) {
    if (!sym) return '';
    return `<span class="log-sym-cell" role="button" tabindex="0" data-sym="${_escAttr(sym)}" data-exch="${_escAttr(exch || '')}" title="${_escAttr(sym)}">${_escAttr(sym)}</span>`;
  }

  // Render one AlgoOrder row (mode=live or sim) for the Order tab. Keeps
  // order details — side, qty, symbol, price, account — on one line so
  // operators can scan placements the same way they'd read a broker blotter.
  function _orderRowHtml(o) {
    // Live orders carry `order_timestamp`; algo/paper/sim carry `created_at`.
    const t      = _dualTsHtml(o.created_at || o.order_timestamp);
    const tag    = _modePill(o.mode);
    const status = (o.status || '').toUpperCase();
    const rowCls = _orderStatusClass(status, o.transaction_type);
    // Prefer fill_price once the chase landed; otherwise the initial limit price.
    const price  = ((o.fill_price != null) ? '@' + priceFmt(o.fill_price) : null)
                || ((o.initial_price != null) ? '@' + priceFmt(o.initial_price) : '');
    const preflightChip = _preflightChipHtml(status, o.detail);
    // Detail chips via shared chipsHtml helper. Agent-id chip stays bespoke
    // (it's an <a>, not a plain span — chipsHtml deliberately doesn't emit anchors).
    const chips     = chipsHtml({
      status: o.status || null,
      chase:  (o.attempts != null && o.attempts > 0) ? `#${o.attempts}` : null,
      engine: o.engine || null,
    }, { order: ['status', 'chase', 'engine'] });
    const agentChip  = o.agent_id
      ? `<a class="log-agent-chip" href="/automation?focus=${o.agent_id}">agent #${o.agent_id}</a>`
      : '';
    const chipsBlock = chips ? ' ' + chips : '';
    const symBlock   = _orderSymSpan(o.symbol, o.exchange);
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

  /** Terminal-specific order status class (subset used in merged stream). */
  function _terminalOrderCls(status, transactionType) {
    if (status === 'FILLED')          return 'log-agent-success';
    if (status === 'UNFILLED')        return 'log-agent-failed';
    if (status === 'OPEN')            return 'log-agent-alert';
    if (transactionType === 'BUY')    return 'log-agent-success';
    return 'log-info';
  }

  /** Build {ts, html}[] entries from the command history. */
  function _terminalCmdLines() {
    return cmdHistory.map(h => {
      const cls = h.status === '✓' ? 'log-agent-success'
                : h.status === '✗' ? 'log-agent-failed'
                : 'log-info';
      const chips = h.fields ? Object.entries(h.fields)
        .map(([k, v]) => `<span class="log-chip"><span class="log-chip-key">${k}:</span>${v}</span>`)
        .join(' ') : '';
      const content = `${h.status || ''} ${h.message || ''} ${chips}`.trim();
      return { ts: h.time, html: _logRow(h.time, content, 'CMD', cls) };
    });
  }

  /** Build {ts, html}[] entries from the order rows (filtered by mode). */
  function _terminalOrderLines() {
    return (orderRows || [])
      .filter(o => !_gatingMode || (o?.mode || 'live') === _gatingMode)
      .map(o => {
        const status  = (o.status || '').toUpperCase();
        const cls     = _terminalOrderCls(status, o.transaction_type);
        const price   = ((o.fill_price != null) ? '@' + priceFmt(o.fill_price) : null)
                     || ((o.initial_price != null) ? '@' + priceFmt(o.initial_price) : '');
        const sym = o.symbol
          ? `<span class="log-sym-cell" role="button" tabindex="0" data-sym="${_escAttr(o.symbol)}" data-exch="${_escAttr(o.exchange || '')}" title="${_escAttr(o.symbol)}">${_escAttr(formatSymbol(o.symbol))}</span>`
          : '';
        const content = `◆ ${o.transaction_type || '?'} ${o.quantity ?? '?'} ${sym} ${price} · ${o.account || '?'}`;
        const ts = o.created_at || o.order_timestamp;
        return { ts, html: _logRow(ts, content, 'ORDER', cls) };
      });
  }

  /** Build {ts, html}[] entries from the agent log (filtered by mode prefix). */
  function _terminalAgentLines() {
    return (agentLog || [])
      .filter(e => {
        const detail = String(e.trigger_condition || e.detail || '');
        return _terminalMatchesMode(detail);
      })
      .map(e => {
        const cond = chipsFromJson(e.trigger_condition) || (e.trigger_condition || '');
        return { ts: e.timestamp, html: _logRow(e.timestamp, cond, 'AGENT', 'log-agent-default') };
      });
  }

  function _terminalHtml() {
    // Each source contributes a row in the unified News-style grid
    // (time · message · tag). Rows are kept as {ts, html} pairs so the
    // merged stream can be sorted latest-first across all three sources.
    let all = [
      ..._terminalCmdLines(),
      ..._terminalOrderLines(),
      ..._terminalAgentLines(),
    ].sort((a, b) => _tsKey(b.ts) - _tsKey(a.ts));
    // Apply search filter on terminal rows
    if (_searchQuery) {
      all = all.filter(x => _rowMatchesSearch(x.html));
    }
    return all.length
      ? all.map(x => x.html).join('')
      : '<div class="log-row log-debug"><span class="log-row-msg">No events.</span></div>';
  }

  // ── Download CSV helper ───────────────────────────────────────────────
  /**
   * Escape a CSV field: wrap in quotes and escape internal quotes.
   * @param {any} v
   */
  function _csvEscape(v) {
    const s = String(v ?? '');
    if (s.includes(',') || s.includes('"') || s.includes('\n')) {
      return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
  }
  /** Build and trigger a CSV download for the current visible rows. */
  function _downloadCsv() {
    const date = new Date().toISOString().slice(0, 10);
    let rows = /** @type {string[][]} */ ([]);
    let header = /** @type {string[]} */ ([]);
    let filename = `activity-${logTab}-${date}.csv`;

    if (logTab === 'order') {
      header = ['time', 'ref', 'symbol', 'side', 'qty', 'price', 'status', 'account'];
      rows = filteredOrderRows.map(o => [
        o.created_at || o.order_timestamp || '',
        o.order_id || o.id || '',
        o.symbol || o.tradingsymbol || '',
        o.transaction_type || '',
        o.quantity ?? '',
        o.fill_price ?? o.initial_price ?? '',
        o.status || '',
        o.account || '',
      ]);
    } else if (logTab === 'agent') {
      header = ['time', 'event_type', 'agent_id', 'account', 'message'];
      rows = _agentRows.map(r => {
        const e = r._raw || {};
        const cond = chipsFromJson(e.trigger_condition) || (e.trigger_condition || '');
        // Strip HTML chips from cond for clean CSV output
        const msg = cond.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
        return [
          e.timestamp || '',
          e.event_type || '',
          e.agent_id ?? '',
          e.account || '',
          msg,
        ];
      });
    } else if (logTab === 'system') {
      header = ['time', 'level', 'message'];
      rows = _sysRows.map(r => {
        const l = r._rawLine || '';
        const d = parseLogLineDate(l);
        const rest = d ? stripTs(l) : l;
        const lm = String(rest || '').match(/^(ERROR|WARN(?:ING)?|INFO|DEBUG)\b/i);
        return [d ? d.toISOString() : '', lm ? lm[1].toUpperCase() : '', rest];
      });
    } else if (logTab === 'conn') {
      header = ['time', 'level', 'message'];
      rows = _connRows.map(r => {
        const l = r._rawLine || '';
        const d = parseLogLineDate(l);
        const rest = d ? stripTs(l) : l;
        const lm = String(rest || '').match(/^(ERROR|WARN(?:ING)?|INFO|DEBUG)\b/i);
        return [d ? d.toISOString() : '', lm ? lm[1].toUpperCase() : '', rest];
      });
    } else if (logTab === 'terminal') {
      header = ['time', 'source', 'message'];
      const all = [
        ..._terminalCmdLines().map(x => ({ ...x, src: 'CMD' })),
        ..._terminalOrderLines().map(x => ({ ...x, src: 'ORDER' })),
        ..._terminalAgentLines().map(x => ({ ...x, src: 'AGENT' })),
      ].sort((a, b) => _tsKey(b.ts) - _tsKey(a.ts));
      rows = all.map(x => {
        const msg = x.html.replace(/<[^>]*>/g, '').replace(/\s+/g, ' ').trim();
        return [x.ts || '', x.src || '', msg];
      });
    } else if (logTab === 'simulator') {
      // Operator decision: columns time, kind, scenario, tick_index, symbol, detail
      // No flattening of changes[].
      header = ['time', 'kind', 'scenario', 'tick_index', 'symbol', 'detail'];
      rows = _simRows.map(r => {
        const e = r._raw || {};
        const detail = e.kind === 'started' || e.kind === 'stopped'
          ? (e.note || '')
          : e.kind === 'order'
            ? `${(e.order?.side || '')} ${e.order?.qty ?? ''} ${e.order?.symbol || ''}`
            : `tick ${e.tick_index ?? ''} · ${e.scenario || ''}`;
        const sym = e.kind === 'order' ? (e.order?.symbol || '') : '';
        return [
          e.ts || '',
          e.kind || '',
          e.scenario || '',
          e.tick_index ?? '',
          sym,
          detail,
        ];
      });
    } else {
      return; // news tab: download is disabled (handled in the button)
    }

    const csvLines = [header, ...rows]
      .map(row => row.map(_csvEscape).join(','))
      .join('\r\n');
    const blob = new Blob([csvLines], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

</script>

<div class="flex items-stretch mb-2 log-tab-row" class:ctx-modal={context === 'modal'} style="border-bottom: 1px solid rgba(255,255,255,0.07);">
  {#if label && context !== 'page'}
    {#if context === 'modal'}
      <span class="lp-label">
        <BellIcon width="12" height="12" class="lp-label-icon" />
        {label}
      </span>
    {:else}
      <span class="lp-label">{label}</span>
    {/if}
    <span class="lp-sep" aria-hidden="true"></span>
  {/if}
  <div class="lp-tab-strip-wrap">
    <AlgoTabs
      tabs={VISIBLE_TABS.map(([id, lbl]) => ({ id, label: lbl }))}
      bind:value={logTab}
      onChange={onTabChange}
      compact={true}
    />
  </div>
  <ActivityHeaderFilters
    bind:accountFilter={_internalAccountFilter}
    bind:levelFilter
    availableAccounts={_availableAccounts}
    showAccountFilter={_showAccountFilter}
    showLevelFilter={_showLevelFilter} />
  {#if label}
    <div class="lp-card-btns">
      {#if context !== 'page'}
        {#if context === 'modal'}
          <button type="button" class="alm-close-btn"
                  aria-label="Close activity log"
                  onclick={() => onClose?.()}>×</button>
        {:else}
          <CollapseButton bind:isCollapsed {cardId} />
          <FullscreenButton bind:isFullscreen />
        {/if}
      {/if}
      {#if onDownload}
        <GridDownloadButton onClick={onDownload} />
      {/if}
    </div>
  {:else}
    <!-- Legacy card buttons for mounts without a label prop -->
    <span class="lp-card-btns-legacy" role="group" aria-label="Activity panel controls">
      <!-- Search -->
      <button type="button"
        class="lp-card-btn {_searchOpen ? 'lp-card-btn-on' : ''}"
        title={_searchOpen ? 'Close search' : 'Search rows'}
        aria-label="Search rows"
        aria-pressed={_searchOpen}
        onclick={() => { _searchOpen = !_searchOpen; if (!_searchOpen) _searchQuery = ''; }}>
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.6"/>
          <path d="M10.5 10.5L14 14" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
        </svg>
      </button>
      <!-- Expand / Contract -->
      <button type="button"
        class="lp-card-btn {_expanded ? 'lp-card-btn-on' : ''}"
        title={_expanded ? 'Contract panel' : 'Expand panel'}
        aria-label={_expanded ? 'Contract panel' : 'Expand panel'}
        aria-pressed={_expanded}
        onclick={() => { _expanded = !_expanded; }}>
        {#if _expanded}
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M6 2v4H2M10 14v-4h4M2 10h4v4M14 6h-4V2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        {:else}
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        {/if}
      </button>
      <!-- Fullscreen — hidden when already in modal context -->
      {#if context !== 'modal'}
        <button type="button"
          class="lp-card-btn"
          title="Open in fullscreen modal"
          aria-label="Open in fullscreen modal"
          onclick={() => { openActivityModal(); }}>
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path d="M3 3h4M3 3v4M13 3h-4M13 3v4M3 13h4M3 13v-4M13 13h-4M13 13v-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
          </svg>
        </button>
      {/if}
      <!-- Download — disabled on News tab -->
      <button type="button"
        class="lp-card-btn"
        title={logTab === 'news' ? 'Download not available for News tab' : 'Download visible rows as CSV'}
        aria-label={logTab === 'news' ? 'Download not available for News tab' : 'Download CSV'}
        disabled={logTab === 'news'}
        onclick={_downloadCsv}>
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <path d="M8 2v8M5 7l3 3 3-3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M2 12h12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>
        </svg>
      </button>
    </span>
  {/if}
</div>
<!-- Search input row — visible when _searchOpen is true -->
{#if _searchOpen}
  <div class="lp-search-row">
    <input
      type="search"
      class="lp-search-input"
      placeholder="Filter rows…"
      bind:value={_searchQuery}
      aria-label="Filter log rows"
      autocomplete="off"
    />
    {#if _searchQuery}
      <button type="button" class="lp-search-clear" aria-label="Clear search"
        onclick={() => { _searchQuery = ''; }}>×</button>
    {/if}
  </div>
{/if}

<div class="lp-body-wrap {_expanded ? 'lp-body-expanded' : ''}">
{#if logTab === 'news'}
  <!-- News tab — rendered via shared NewsList component in algo palette.
       Activity surface flavour: 2-column magazine flow (same as dashboard
       activity card), source chip suppressed since the Activity header
       already carries the filter chips and a duplicate per-row source
       pill clutters the flow. Single shared component — no fork. -->
  <div class="log-panel log-news-panel {heightClass}">
    <NewsList
      limit={50}
      showRefreshTime={true}
      pollMs={2 * 60 * 1000}
      emptyMessage="No headlines."
      columns={2}
      showSource={false}
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
    <!-- Account filter moved to the tab row above per operator. -->
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
              <div class="lp-oc-actions" role="group" aria-label="Order actions">
                {#if _isOpenBroker(ord)}
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
                {/if}
                <!-- Reconcile — sync this single row against the broker book.
                     Only shown for in-flight statuses (OPEN, TRIGGER PENDING,
                     CANCEL_FAILED, PARTIAL) where a sync can change the row
                     state. Terminal rows (COMPLETE, REJECTED, CANCELLED,
                     UNFILLED) are already settled — reconcile is a no-op. -->
                {#if _isInFlight(ord)}
                <button type="button" class="lp-oc-btn lp-oc-reconcile"
                  title="Reconcile with broker"
                  aria-label="Reconcile"
                  disabled={_reconciling.has(_oKey)}
                  onclick={(e) => { e.stopPropagation(); _reconcileRow(ord); }}>
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M3 8a5 5 0 0 1 8.6-3.5M13 8a5 5 0 0 1-8.6 3.5"
                      stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                    <path d="M11.5 2v3h-3M4.5 14v-3h3"
                      stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
                {/if}
              </div>
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
<div class="log-panel log-rows {heightClass} {multiColumn && logTab !== 'order' ? 'lp-multicol' : ''}">
  {#if logTab === 'terminal'}
    {@html _terminalHtmlDerived}
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
  {:else if logTab === 'conn'}
    {#if _connRows.length}
      {#each _connRows as r (r.key)}{@html r.html}{/each}
    {:else}
      <div class="log-row log-debug"><span class="log-row-msg">No conn_service entries yet.</span></div>
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
</div><!-- /.lp-body-wrap -->

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
    symbol={_ctxMenu?.symbol}
    exchange={_ctxMenu?.exchange}
    x={_ctxMenu?.x}
    y={_ctxMenu?.y}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      // 'log' opens the singleton ActivityLogModal via the store
      // (mounted once in the (algo) layout). 'place-order' / 'chart'
      // still use local _ctxAction state for their own modal mounts
      // below.
      if (action === 'log') {
        openActivityModal();
        _ctxAction = null;
      } else {
        _ctxAction = /** @type {any} */ (action);
      }
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

<!-- ActivityLogModal mounting is layout-owned via the activityModal
     store — no per-component instance. _ctxAction === 'log' now
     opens the singleton via openActivityModal() in the context-menu
     handler. -->


<style>
  /* Tab row — another +30% on the previous 0.48rem → 0.62rem. Padding
     scaled proportionally. Still no inter-tab gap so mobile fit holds. */
  .log-tab-row { gap: 0; }

  /* Modal context: amber gradient header matching alm-header style */
  .ctx-modal {
    background: linear-gradient(180deg,
      rgba(251, 191, 36, 0.18) 0%,
      rgba(251, 191, 36, 0.06) 100%);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }

  /* Tab strip wrapper — grows to fill remaining space in the flex row */
  .lp-tab-strip-wrap {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    align-items: stretch;
    overflow-x: hidden;
  }

  /* Label chip shown when `label` prop is provided */
  .lp-label {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm, 0.6rem);
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--c-action);
    white-space: nowrap;
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0 0.5rem;
  }
  :global(.lp-label-icon) { color: var(--c-action); flex-shrink: 0; }

  /* Separator between label and tab strip */
  .lp-sep {
    width: 1px;
    align-self: stretch;
    background: rgba(255,255,255,0.10);
    flex-shrink: 0;
    margin: 0.15rem 0.25rem 0.15rem 0.5rem;
  }

  /* Close button rendered in modal context */
  .alm-close-btn {
    margin-left: 0;
    flex-shrink: 0;
    width: 1.4rem;
    height: 1.4rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    border-radius: 3px;
    color: var(--c-short);
    font-size: var(--fs-xl);
    line-height: 1;
    padding: 0;
    cursor: pointer;
    font-family: monospace;
    transition: background 0.1s;
  }
  .alm-close-btn:hover { background: rgba(248, 113, 113, 0.15); }

  /* Legacy card-btns span for mounts without a label */
  .lp-card-btns-legacy {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    margin-left: auto;
    flex-shrink: 0;
    padding: 0 0.15rem;
    align-self: center;
  }

  /* Account multi-select (legacy inline filter — retained for existing
     mounts that pass hideInlineAccountFilter=false). Width clamped so it
     doesn't dominate the tab row. */
  .lp-tabrow-acct {
    align-self: center;
    display: inline-flex;
    min-width: 8rem;
    max-width: 16rem;
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
    /* Scroll containment — without overflow-y:auto the rows overflow
       their parent and the wheel events bubble up to the page,
       scrolling the host instead of the panel. This bit operators
       inside ActivityLogModal where the modal body has overflow:hidden
       so the rows extended past the modal frame. overscroll-behavior
       prevents scroll-chaining once the bottom is reached. */
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  /* News tab uses a different inner component (.log-news-panel) but
     must match .log-rows visually — same padding + same background
     inheritance so switching tabs feels like moving inside one panel,
     not jumping to a different surface. Operator: "news tab background
     should be changed to conn background." */
  :global(.log-panel.log-news-panel) {
    overflow-y: auto;
    overscroll-behavior: contain;
    min-height: 0;
    padding: 0.25rem 0.55rem;
    background: transparent;
  }
  /* Two-column magazine layout for agent / terminal / system / conn tabs
     on wide viewports. Uses CSS Grid (not column-count) because the panel
     has overflow-y:auto — column-count fails inside scrollable containers
     (browser can't determine column heights so columns collapse or scroll
     horizontally). Grid works correctly with vertical overflow.
     align-content:start prevents the last-row items from stretching to fill
     remaining space. Column divider rendered via background-image gradient
     (column-rule only works with column-count, not grid). */
  :global(.log-panel.log-rows.lp-multicol) {
    display: grid;
    grid-template-columns: 1fr 1fr;
    column-gap: 1.5rem;
    align-content: start;
    /* Column divider between the two grid columns */
    background-image: linear-gradient(
      rgba(148, 163, 184, 0.22) 0%,
      rgba(148, 163, 184, 0.22) 100%
    );
    background-size: 1px 100%;
    background-position: center;
    background-repeat: no-repeat;
  }
  :global(.log-panel.log-rows.lp-multicol .log-row) {
    min-width: 0; /* prevent overflow out of grid cell */
  }
  /* Below 900px the row text gets too narrow for the two-line
     timestamp + message layout to be readable; collapse to single
     column (mirrors NewsList's @media breakpoint). */
  @media (max-width: 900px) {
    :global(.log-panel.log-rows.lp-multicol) {
      display: block;
      background-image: none;
    }
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
    font-size: var(--fs-lg);
    color: var(--algo-slate);
    border-left: none;
    background: transparent;
  }
  :global(.log-panel.log-rows .log-row:last-child) { border-bottom: 0; }
  :global(.log-panel.log-rows .log-row:hover) { background: rgba(255, 255, 255, 0.02); }

  :global(.log-panel.log-rows .log-row-time) {
    flex: 0 0 auto;
    order: 0;
    font-family: var(--font-numeric);
    /* Matches .newslist-time (0.62rem) — same scan-fodder
       proportion as the news tab. */
    font-size: var(--fs-sm);
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
    font-family: var(--font-numeric);
    /* Bumped with the message size — same proportion as
       .newslist-src on the news tab. */
    font-size: var(--fs-sm);
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
    /* flex-basis: 100% pushes msg to its own line below time + tag
       on narrow viewports — mobile convention. Desktop override
       below collapses time + msg into one row. */
    flex: 1 1 100%;
    order: 2;
    min-width: 0;
    word-break: break-word;
    line-height: 1.3;
  }
  /* Desktop ≥1024px — collapse the stacked layout into a single
     row with two columns: [time TAG] | [message]. Matches the
     Bloomberg / TradingView / IBKR convention for log surfaces on
     wide viewports — doubles entries-per-screen without info loss.
     Long messages still wrap inside their own column (the message
     col grows + shrinks but `word-break: break-word` from the rule
     above keeps overflow handled). Mobile layout unchanged. */
  @media (min-width: 1024px) {
    :global(.log-panel.log-rows .log-row) {
      flex-wrap: nowrap;
    }
    :global(.log-panel.log-rows .log-row-msg) {
      flex: 1 1 0;
    }
  }
  /* Row-class semantics now carry through TEXT COLOR ONLY — no inside
     accent (left-border stripe, background tint) per operator
     feedback. Every row sits on the same flat surface, severity reads
     from the message colour so the three-column [time · msg · tag]
     grid stays uncluttered. */
  :global(.log-panel.log-rows .log-row.log-agent-success) { color: var(--c-long); }
  :global(.log-panel.log-rows .log-row.log-agent-failed)  { color: var(--c-short); }
  :global(.log-panel.log-rows .log-row.log-agent-alert)   { color: var(--c-action); }
  :global(.log-panel.log-rows .log-row.log-agent-triggered) { color: #fb923c; }
  :global(.log-panel.log-rows .log-row.log-agent-cooldown)  { color: #94a3b8; }
  :global(.log-panel.log-rows .log-row.log-error)   { color: var(--c-short); }
  :global(.log-panel.log-rows .log-row.log-warning) { color: var(--c-action); }
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
    font-family: var(--font-numeric);
    font-size: var(--fs-2xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    border-radius: 2px;
    border: 1px solid;
    vertical-align: baseline;
  }
  :global(.mode-pill-sim) {
    background: var(--c-action-14);
    color: var(--c-action);
    border-color: rgba(251,191,36,0.45);
  }
  /* LIVE pill — red to match the navbar LIVE badge + LIVE banner.
     Reads as "this row hit the real broker" at a glance. Earlier
     palette put LIVE on emerald which collided with REPLAY (also
     emerald), making the two indistinguishable on a quick scan. */
  :global(.mode-pill-live) {
    background: rgba(248,113,113,0.14);
    color: var(--c-short);
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
    color: var(--c-long);
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
  :global(.log-order-rejected)  { color: var(--c-short); }
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
  .om-chip {
    background: transparent;
    border: 1px solid rgba(255,255,255,0.10);
    border-right-width: 0;
    padding: 0.1rem 0.55rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
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
  .om-chip.om-on.om-chip-all    { background: var(--c-action-14); color: var(--c-action); border-color: rgba(251,191,36,0.45); }
  .om-chip.om-on.om-chip-paper  { background: rgba(56,189,248,0.14); color: #7dd3fc; border-color: rgba(56,189,248,0.45); }
  /* Canonical green (var(--c-long)) for live — replaces off-palette emerald #10b981 */
  /* LIVE chip on the Order-tab mode filter — red to match LIVE pill
     + LIVE banner. Operator selects "Live" → red glow signals "I'm
     about to look at real-broker orders". */
  .om-chip.om-on.om-chip-live   { background: rgba(248,113,113,0.14); color: var(--c-short); border-color: rgba(248,113,113,0.45); }
  .om-chip.om-on.om-chip-sim    { background: var(--c-action-14); color: var(--c-action); border-color: rgba(251,191,36,0.45); }
  /* Shadow — orange #fb923c, matching the mode pill palette */
  .om-chip.om-on.om-chip-shadow { background: rgba(251,146,60,0.14); color: #fb923c; border-color: rgba(251,146,60,0.45); }
  /* Replay — canonical green var(--c-long) */
  .om-chip.om-on.om-chip-replay { background: rgba(74,222,128,0.14); color: var(--c-long); border-color: rgba(74,222,128,0.45); }

  /* Preflight verdict chip — ✓ when basket_margin / Kite preflight
     accepted the order, ✗ when it pushed back. Hover the chip to see
     the broker's reason in the title attribute. */
  :global(.log-pf) {
    display: inline-block;
    margin: 0 0.15rem;
    padding: 0 0.25rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 800;
    border-radius: 2px;
    cursor: help;
  }
  :global(.log-pf-ok)  { color: var(--c-long); background: var(--c-long-10); }
  :global(.log-pf-bad) { color: var(--c-short); background: rgba(248,113,113,0.12); }

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
    color: var(--c-info);
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
    color: var(--c-info);
  }
  .lp-oc-modify:hover:not(:disabled) {
    background: var(--c-info-14);
    border-color: rgba(103,232,249,0.65);
    color: #67e8f9;
  }
  /* Cancel — red-400 palette (matches order rejection / kill affordances) */
  .lp-oc-cancel {
    border: 1px solid rgba(248,113,113,0.45);
    color: var(--c-short);
  }
  .lp-oc-cancel:hover:not(:disabled) {
    background: rgba(248,113,113,0.14);
    border-color: rgba(252,165,165,0.65);
    color: #fca5a5;
  }
  /* Reconcile — sky-400 palette ("sync / refresh", distinct from
     destructive red and edit cyan). Matches the /orders standalone
     reconcile button so the affordance reads identically on both
     surfaces. */
  .lp-oc-reconcile {
    border: 1px solid rgba(125,211,252,0.55);
    color: #7dd3fc;
  }
  .lp-oc-reconcile:hover:not(:disabled) {
    background: rgba(125,211,252,0.18);
    border-color: rgba(186,230,253,0.85);
    color: #bae6fd;
  }

  /* ── Card button group (label-based: CollapseButton + FullscreenButton + Download) ──── */
  .lp-card-btns {
    display: flex;
    align-items: center;
    gap: 0.2rem;
    flex-shrink: 0;
  }
  .lp-card-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.35rem;
    height: 1.35rem;
    padding: 0;
    background: none;
    border: 1px solid rgba(148, 163, 184, 0.20);
    border-radius: 3px;
    color: rgba(148, 163, 184, 0.65);
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.08s, border-color 0.08s, color 0.08s;
  }
  .lp-card-btn svg { pointer-events: none; }
  .lp-card-btn:hover:not(:disabled) {
    background: rgba(148, 163, 184, 0.10);
    border-color: rgba(148, 163, 184, 0.45);
    color: var(--algo-slate);
  }
  /* Active state (search open, expanded) — amber tint matching the tab row accent. */
  .lp-card-btn.lp-card-btn-on {
    background: var(--c-action-14);
    border-color: rgba(251, 191, 36, 0.45);
    color: var(--c-action);
  }
  .lp-card-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }

  /* Search input row below the tab strip */
  .lp-search-row {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.25rem 0.3rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    background: rgba(0, 0, 0, 0.12);
  }
  .lp-search-input {
    flex: 1 1 0;
    min-width: 0;
    height: 1.5rem;
    padding: 0.2rem 0.5rem;
    background: rgba(15, 23, 42, 0.7);
    border: 1px solid rgba(251, 191, 36, 0.28);
    border-radius: 3px;
    color: var(--algo-slate);
    font-size: var(--fs-sm);
    font-family: inherit;
    outline: none;
    transition: border-color 0.08s;
  }
  .lp-search-input:focus { border-color: var(--c-action); }
  .lp-search-input::placeholder { color: var(--algo-muted); opacity: 0.7; }
  .lp-search-clear {
    flex-shrink: 0;
    width: 1.2rem;
    height: 1.2rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.30);
    border-radius: 3px;
    color: var(--c-short);
    font-size: var(--fs-base);
    line-height: 1;
    cursor: pointer;
    padding: 0;
    font-family: monospace;
    transition: background 0.08s;
  }
  .lp-search-clear:hover { background: rgba(248, 113, 113, 0.12); }

  /* Expand / contract: the body wrapper is normally flex-1 (inherits
     from the heightClass on the child panels). When expanded, we grow
     the wrapper beyond its default flex allocation — add min-height so
     the panel is visually taller in its flex context. */
  .lp-body-wrap {
    display: contents; /* transparent wrapper — no layout change by default */
  }
  .lp-body-wrap.lp-body-expanded {
    display: flex;
    flex-direction: column;
    flex: 1 1 0;
    min-height: min(600px, 70vh);
    min-height: min(600px, 70svh);
  }

</style>
