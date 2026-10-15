<script>
  import { onMount, onDestroy, getContext, untrack } from 'svelte';
  import { logTime, lifespanChip, visibleInterval } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import StaleBanner from '$lib/StaleBanner.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import {
    fetchAgents, activateAgent, deactivateAgent, deleteAgent, updateAgent,
    fetchSimStatus,
    startSimForAgent,
    fetchAgentRenderers, fetchSetting,
    deleteOrderTemplate,
  } from '$lib/api';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import Select from '$lib/Select.svelte';
  import DisclosureChevron from '$lib/DisclosureChevron.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import { loadOrderTemplates, reloadOrderTemplates, orderTemplatesStore } from '$lib/data/templates';
  import ConsoleTab from '$lib/automation/ConsoleTab.svelte';
  import {
    KIND_CARDS, classifyAgentCardId, groupRowsByGroup, kindCounts,
    filterAgentRowsByCard,
  } from '$lib/data/automationKinds.js';
  import {
    fetchAgentCatalog, decompileAgent, decompileEventAgent,
    decompileBracket, decompileToken,
  } from '$lib/command/grammars/agents.js';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';

  let agents      = $state([]);
  let loading     = $state(true);
  let error       = $state('');
  let logTab      = $state('agent');
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);

  // ── Tab state (Sprint B 2026-10 added Console; Sprint C 2026-10 folded
  //    Brackets + Tokens tabs into the Agents-tab kind-card structure, so
  //    only 'console'/'agents' remain). Seeded from ?tab= on mount;
  //    written back via replaceState so a reload / bookmark preserves
  //    the tab without adding a history entry per click. A stale
  //    bookmarked `?tab=brackets`/`?tab=tokens` (Sprint 9 era, before
  //    this redesign) lands on 'agents' with that kind-card preselected
  //    instead of a dead tab — see the `$effect` below. */
  let activeTab = $state(/** @type {'console'|'agents'} */ ('agents'));
  function setActiveTab(/** @type {string} */ id) {
    activeTab = /** @type {any} */ (id);
    const url = new URL(window.location.href);
    url.searchParams.set('tab', id);
    goto(url.pathname + '?' + url.searchParams.toString(), { replaceState: true, noScroll: true, keepFocus: true });
  }

  // Console tab's CLI text + edit-mode target — kept at page level (not
  // inside ConsoleTab's own internal state) so the Agents-tab row list's
  // Edit action (below) can write into it, and so both survive a tab
  // switch away from Console and back. Bound two-way into ConsoleTab.
  let _consoleCliText = $state('');
  /** @type {{rowKind: 'cycle'|'event'|'template'|'bracket', ref: string|number, label: string} | null} */
  let _consoleEditTarget = $state(null);

  // Statement-kind hint selector (relocated from ConsoleTab.svelte, 2026-10
  // "remove Workspace header" pass) — now a dropdown sharing the Console/
  // Agents tab row instead of a separate pills row inside ConsoleTab. Pure
  // UI-hint state driven only by `_consoleCliText`: NOT a hard mode-lock —
  // the real dispatch decision always comes from ConsoleTab's own
  // `classifyConsoleStatement(value)` on the typed text, never from this
  // selector. It (a) reflects whatever kind the typed text already,
  // confidently resolved to (synced below), and (b) when the box is EMPTY,
  // picking an option seeds a short starter snippet for that kind — a
  // convenience, never a destructive overwrite of real typed text.
  const _KIND_OPTIONS = ['order', 'bracket', 'agent', 'alert'];
  const _KIND_LABELS = { order: 'Order', bracket: 'Bracket', agent: 'Agent', alert: 'Alert' };
  const _KIND_SEED = {
    order: 'order(account="", symbol="", side="BUY", lots=1)',
    bracket: 'bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20) #name=""',
    agent: 'WHEN pnl_pct@positions.total <= -5 ALERT telegram DO nop',
    alert: 'ALERT telegram',
  };
  let _kindHint = $state('order');

  function _pickKind(/** @type {string} */ k) {
    _kindHint = k;
    if (!_consoleCliText || !_consoleCliText.trim()) _consoleCliText = _KIND_SEED[k] || '';
  }

  /** Leading-TOKEN detector for the kind-selector sync below — deliberately
   *  reads the raw text's own first word, NOT the compiled statement's own
   *  `kind`. Those two disagree for the "Alert" bucket: a bare
   *  `ALERT telegram` statement (no WHEN, no event-meta leading kwarg)
   *  compiles to `kind:'agent'` (compileStatement's own dispatch — see
   *  agents.js), the SAME compiled kind an ordinary `WHEN...ALERT...DO...`
   *  statement gets, even though textually it's the "Alert" bucket the
   *  operator picked. The selector's job is to reflect what the operator
   *  TYPED, not which JSON shape it happens to compile to. */
  function _detectKindHint(/** @type {string} */ text) {
    const t = String(text || '').trimStart();
    if (/^(order|place_order)\s*\(/i.test(t)) return 'order';
    if (/^bracket\s*\(/i.test(t)) return 'bracket';
    if (/^(when|given|do)\b/i.test(t)) return 'agent';
    if (/^alert\b/i.test(t)) return 'alert';
    return null;
  }

  // Keeps the selector in sync with whatever the typed text's leading
  // token actually is. Intentionally does nothing when the text doesn't
  // start with one of these five keywords (mid-typing, a bare
  // `token(...)`, or legacy/empty text) — the selector simply keeps
  // showing whatever the operator last picked.
  $effect(() => {
    const k = _detectKindHint(_consoleCliText);
    if (k) _kindHint = k;
  });

  // Sprint B (2026-10) — single reactive effect (not a one-shot onMount
  // read) on `page.url`: `?tab=` must keep working on a WITHIN-page
  // navigation to this same route, not just on first mount — SvelteKit
  // reuses this component instance for a same-route navigation.
  // Sprint C — `?tab=brackets`/`?tab=tokens` (pre-redesign bookmarks)
  // redirect to the Agents tab with the matching kind-card preselected,
  // rather than landing on a tab that no longer exists. `?tab=tokens`
  // now lands on the 'agents' card itself (2026-10-12 — Templates folded
  // into Agents, no separate card left to preselect).
  // Bug fix (2026-10, operator: "need to click two times on agents tab to
  // switch — that is a bug") — this effect's job is ONE-DIRECTIONAL sync
  // (URL -> activeTab), for direct navigation/bookmarks/back-forward. But
  // reading `activeTab`/`_activeCard` for the comparison checks ALSO
  // subscribed this effect to THEM, not just to `page.url` — so the
  // instant `setActiveTab('agents')` wrote `activeTab = 'agents'`
  // synchronously, this effect re-ran before `goto()`'s async navigation
  // had updated `page.url`, saw the STILL-STALE `t === 'console'`, and
  // "corrected" activeTab right back to 'console' — one click looked like
  // a no-op, and a second click (by then `page.url` had caught up) was
  // needed to make it stick. `untrack()` keeps the comparison/write reads
  // out of this effect's dependency list, so it only ever re-runs in
  // response to a genuine `page.url` change, never activeTab's own.
  $effect(() => {
    const t = page.url.searchParams.get('tab');
    untrack(() => {
      if (t === 'console' && activeTab !== 'console') activeTab = 'console';
      else if (t === 'brackets' && _activeCard !== 'brackets') { activeTab = 'agents'; _activeCard = 'brackets'; }
      else if (t === 'tokens' && _activeCard !== 'agents') { activeTab = 'agents'; _activeCard = 'agents'; }
    });
  });

  // Global simulator status — when active, the Agent-events panel swaps to
  // the simulator's event stream so operators only see sim results in the
  // algo pages while the sim is running.
  let simActive   = $state(false);

  // Template catalog for the Brackets card — same module-level cache +
  // store every other surface (TemplateBar, OrderTicket) reads;
  // loadOrderTemplates() below is a cheap no-op if already warm.
  // Unfiltered — every OrderTemplate row is a Bracket row (Sprint 7's
  // "table identity alone decides the kind" storage note).
  let _allTemplates = $state(/** @type {any[]} */ ([]));
  $effect(() => {
    const rows = $orderTemplatesStore;
    if (rows) _allTemplates = rows;
  });

  // ── Kind-card toggle-filter / row-list (2026-10-12, revised per operator
  //    instruction: "use order status cards as reference... keep agent
  //    status cards always, and show the agent rows below when a status
  //    card pressed... toggle... instead of using go back functionality").
  //    Matches OrderBook.svelte's `.ob-status-bar`/`.ob-sc` exactly: the
  //    kind cards are ALWAYS rendered (never replaced by a drilled-down
  //    screen), exactly one is active at a time (radio-style, no deselect,
  //    mirroring `_activeStatus`/`_internalStatus` there), and clicking a
  //    DIFFERENT card just changes which kind's rows render in the SAME
  //    row-list below — no navigation, no Back button.
  //
  //    The separate per-kind `#group=` TAB layer (Sprint C) is REMOVED —
  //    operator instruction: "show the group tag in the row, sort the
  //    rows on group tag" instead of a tab-per-group UI. `_expandedRowKey`
  //    is a `${cardId}:${slugOrId}` key for the one row currently expanded
  //    for "view" (read-only), at most one open at a time, same UX as the
  //    old flat grid's `expandedSlug`. */
  let _activeCard = $state('agents');
  let _expandedRowKey = $state(/** @type {string|null} */ (null));

  function selectCard(/** @type {string} */ cardId) {
    _activeCard = cardId;
    _expandedRowKey = null;
    if (cardId === 'alerts' && !renderers.length) loadRenderers();
  }

  const _cardCounts = $derived(kindCounts(agents, _allTemplates));

  /** Every row belonging to the currently-selected card, in its own
   *  kind's own shape (`AgentInfo` for agents/alerts/templates,
   *  `OrderTemplateOut` for brackets). */
  const _activeCardRows = $derived.by(() => {
    if (_activeCard === 'brackets') return _allTemplates;
    return filterAgentRowsByCard(agents, _activeCard);
  });

  /** Rows sorted by `group` (reuses `groupRowsByGroup`'s existing
   *  alphabetical-named-groups-first, ungrouped-bucket-last convention —
   *  flattened into one list instead of one list per tab, now that the
   *  tab UI itself is gone). Groups are still strictly per-kind
   *  (CLAUDE.md, Sprint C) since `_activeCardRows` is already filtered to
   *  ONE card's own rows above. */
  const _sortedActiveCardRows = $derived(
    groupRowsByGroup(_activeCardRows).flatMap((g) => g.rows)
  );

  function toggleRowView(/** @type {string} */ key) {
    _expandedRowKey = _expandedRowKey === key ? null : key;
  }

  // One shared CLI-catalog fetch for Edit's decompile step — separate
  // from ConsoleTab's OWN internal fetch (ConsoleTab is usable/testable
  // standalone); a small duplicate network call, deliberately accepted
  // rather than threading the catalog through as a prop this sprint.
  let _editCatalog = $state(/** @type {any} */ (null));
  async function _ensureEditCatalog() {
    if (_editCatalog) return _editCatalog;
    _editCatalog = await fetchAgentCatalog();
    return _editCatalog;
  }

  /** Edit action for any row, any kind — decompiles the row's current
   *  JSON into CLI text and switches to the Console tab with edit-mode
   *  armed (Save routes through UPDATE, never CREATE — see
   *  ConsoleTab's own `_EDIT_KIND_FOR_ROW_KIND`). On a decompile
   *  failure (e.g. a system/default/slugged/inactive Bracket — see
   *  `_bracketUnexpressibleFields` in agents.js) shows the error and
   *  stays on the row — NEVER switches to Console with an empty/broken
   *  box.
   *
   *  Dispatch is keyed on `row.kind`, NOT `cardId` — since 2026-10-12,
   *  `kind='template'` rows share the 'agents' card with ordinary cycle
   *  agents (Templates folded into Agents), so `cardId === 'agents'` no
   *  longer implies `row.kind` is 'cycle'. */
  async function editRow(/** @type {string} */ cardId, /** @type {any} */ row) {
    let catalog;
    try { catalog = await _ensureEditCatalog(); }
    catch { toast.error('CLI catalog unavailable'); return; }

    let dec, ref;
    /** @type {'cycle'|'event'|'template'|'bracket'|undefined} */
    let rowKind;
    try {
      if (cardId === 'brackets') {
        rowKind = 'bracket';
        ref = row.id;
        dec = decompileBracket(row, catalog);
      } else if (row.kind === 'template') {
        rowKind = 'template';
        ref = row.slug;
        dec = decompileToken(row, catalog);
      } else if (cardId === 'agents' || cardId === 'alerts') {
        rowKind = row.kind === 'event' ? 'event' : 'cycle';
        ref = row.slug;
        dec = rowKind === 'event'
          ? decompileEventAgent(row, catalog)
          : decompileAgent(row, catalog);
      } else {
        return;
      }
    } catch (e) {
      // Decompilers are written to return {text:null, errors} rather than
      // throw — this catch is a defensive backstop only, so a genuine bug
      // surfaces as a toast instead of a silent no-op click.
      toast.error(`Decompile failed: ${(e?.message || 'unknown error').slice(0, 100)}`);
      return;
    }

    if (!dec.text) {
      toast.error((dec.errors?.[0]?.message || 'cannot be edited via CLI yet').slice(0, 120));
      return;
    }
    _consoleCliText = dec.text;
    _consoleEditTarget = { rowKind, ref, label: row.name || row.slug };
    setActiveTab('console');
  }

  function _deleteConfirmMessage(/** @type {string} */ cardId, /** @type {any} */ row) {
    if (cardId === 'brackets') return `Delete bracket "${row.name}"? Existing orders that used it are not affected.`;
    if (row.kind === 'template') return `Delete token "${row.name}"? Any agent still $ref-ing it will log a warning and skip that reference.`;
    return `Delete agent "${row.name}"? This cannot be undone.`;
  }

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _deleteConfirmRef = $state(null);

  async function deleteRow(/** @type {string} */ cardId, /** @type {any} */ row) {
    const ok = await _deleteConfirmRef?.ask({
      title: 'Delete?',
      message: _deleteConfirmMessage(cardId, row),
      confirmLabel: 'Delete',
      cancelLabel: 'Cancel',
      destructive: true,
    });
    if (!ok) return;
    try {
      if (cardId === 'brackets') {
        await deleteOrderTemplate(row.id);
        await reloadOrderTemplates().catch(() => {});
      } else {
        // 'agents' / 'alerts' / 'templates' — all three are Agent rows
        // (templates = kind='template') deleted via the one unified
        // endpoint (Sprint D2, Agent/Bracket/Template unification phase).
        await deleteAgent(row.slug);
        await loadAgents();
      }
      toast.success(`${row.name}: deleted`);
      if (_expandedRowKey === `${cardId}:${row.id ?? row.slug}`) _expandedRowKey = null;
    } catch (e) {
      toast.error(`Delete failed: ${e.message}`);
    }
  }

  // ── Trade-mode confirm modal ──────────────────────────────────────────
  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _liveAgentConfirmRef = $state(null);

  let ws;
  let _wsDestroyed = false;
  let refreshTeardown;
  let simStatusTeardown;

  async function loadAgents() {
    try {
      const data = await fetchAgents();
      agents = data;
    } catch (e) { error = e.message; }
  }

  async function pollSimStatus() {
    try {
      const s = await fetchSimStatus();
      simActive = !!s.active;
      // simActive flows down to LogPanel via the `simScope` prop; LogPanel
      // owns its own polling for the Agent + Simulator tabs and re-fetches
      // on prop change. Page-level event/sim log fetches removed in BF —
      // they wrote to dead state that no part of the template rendered,
      // doubling broker calls for the log endpoints.
    } catch (_) { /* cap flag off — treat as idle */ }
  }

  async function loadAll() {
    loading = true;
    await loadAgents();
    loading = false;
  }

  async function toggle(/** @type {any} */ agent) {
    const next = agent.status === 'inactive' ? 'active' : 'inactive';
    try {
      if (agent.status === 'inactive') await activateAgent(agent.slug);
      else await deactivateAgent(agent.slug);
      toast.success(`${agent.name}: ${next === 'active' ? 'activated' : 'deactivated'}`);
      await loadAgents();
    } catch (e) {
      toast.error(`Toggle failed: ${e.message}`);
    }
  }

  /** Flip the agent's trade mode between paper and live in-place.
   *  Paper → live shows a confirm modal; live → paper flips immediately.
   *  Optimistic update so the chip flips instantly on the slow link. */
  async function toggleTradeMode(/** @type {any} */ agent) {
    const cur = agent.trade_mode || 'paper';
    const next = cur === 'live' ? 'paper' : 'live';
    if (next === 'live') {
      const ok = await _liveAgentConfirmRef?.ask({
        title: 'Set to LIVE mode?',
        message: `<b>${agent.name}</b> — every action this agent fires will hit the real Kite broker (subject to the master <span class="font-mono">execution.paper_trading_mode</span> kill-switch).`,
        danger: true,
        confirmLabel: 'Set LIVE',
        cancelLabel: 'Cancel',
      });
      if (!ok) return;
      applyTradeMode(agent, next);
      return;
    }
    applyTradeMode(agent, next);
  }

  async function applyTradeMode(/** @type {any} */ agent, /** @type {string} */ next) {
    const cur = agent.trade_mode || 'paper';
    // Optimistic update — flip the chip immediately.
    agent.trade_mode = next;
    agents = [...agents];
    try {
      await updateAgent(agent.slug, { trade_mode: next });
      toast.success(`${agent.name}: trade mode → ${next.toUpperCase()}`);
      await loadAgents();
    } catch (e) {
      toast.error(`Trade mode update failed: ${e.message}`);
      // Roll back if the PATCH failed.
      agent.trade_mode = cur;
      agents = [...agents];
    }
  }

  // Sprint C (2026-10, Phase 2 "Automation redesign") — the old in-row
  // Structured-JSON editor (startEdit/saveEdit/saveEventEdit/editForm +
  // every channel-checkbox/tier-pill/blackout-window helper feeding it)
  // is RETIRED. Edit now decompiles a row's JSON straight into the
  // Console tab's CLI text (see `_editRow`/`_consoleEditTarget` below) —
  // giving the already-built decompiler functions their first production
  // caller and fixing the page-switching pain that started this phase.
  // Fields the CLI grammar has no syntax for yet (schedule/cooldown/tier/
  // topic/trade_mode/lifespan_*/debounce_minutes/blackout_windows) are
  // simply not editable from this page anymore — saving via Console only
  // ever sends the fields the grammar CAN express (see
  // `updateConsoleAgent`'s own doc), so those settings are PRESERVED
  // (never reset), just not reachable from a UI control until a future
  // grammar extension covers them.
  // Sprint C note (see comment above): EVENT_CHANNELS/ALERT_CHANNELS and
  // every per-channel-checkbox/log-tag/renderer-picker helper that used
  // to feed the in-row Structured editor are gone — the generic CLI
  // grammar (agents.js) is the only Edit surface now.

  // Global default trade mode — informational "Global: X" display next
  // to the per-agent trade_mode chip on each row.
  let globalDefaultTradeMode = $state('paper');
  async function loadGlobalDefaultTradeMode() {
    try {
      const s = await fetchSetting('execution.default_agent_trade_mode');
      globalDefaultTradeMode = s?.value || 'paper';
    } catch (_) { /* keep last-known value */ }
  }

  // Event-kind row's fixed Renderer field — shown read-only on the row's
  // expanded "view" (no edit control; renderer/condition/slug are fixed
  // at creation). Lazy-loaded the first time an Alerts-card row is
  // expanded.
  /** @type {{key:string, label:string, description?:string}[]} */
  let renderers      = $state([]);
  async function loadRenderers() {
    try {
      const rows = await fetchAgentRenderers();
      renderers = Array.isArray(rows) ? rows : [];
    } catch (_) { /* keep last-known list */ }
  }
  function eventRendererKey(/** @type {any} */ agent) {
    return (agent?.actions || []).find((a) => a?.type === 'render')?.render || null;
  }
  function rendererLabel(/** @type {string|null} */ key) {
    if (!key) return '—';
    return renderers.find((r) => r.key === key)?.label || key;
  }

  /** Detect AI provenance in agent.description and split into chips.
   *  Backend's _compose_ai_description writes the lines:
   *    [AI prompt] <prompt>
   *    [AI why] <why_summary>
   *    <optional remaining description>
   *  We round-trip those into structured fields here. Returns {prompt, why, rest}. */
  function parseAIDescription(/** @type {string|null|undefined} */ desc) {
    const out = { prompt: '', why: '', rest: '' };
    if (!desc) return out;
    const lines = String(desc).split('\n');
    const restLines = [];
    for (const line of lines) {
      if (line.startsWith('[AI prompt] '))      out.prompt = line.slice(12).trim();
      else if (line.startsWith('[AI why] '))    out.why    = line.slice(9).trim();
      else if (line.trim())                     restLines.push(line);
    }
    out.rest = restLines.join('\n').trim();
    return out;
  }

  function leafLabel(/** @type {any} */ node) {
    if (!node || !node.metric || !node.scope) return JSON.stringify(node);
    const v = typeof node.value === 'number' && Math.abs(node.value) >= 1000
      ? `₹${node.value.toLocaleString('en-IN')}`
      : JSON.stringify(node.value);
    return `${node.metric}@${node.scope} ${node.op || '?'} ${v}`;
  }

  function connectWS() {
    if (_wsDestroyed) return;
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${proto}//${location.host}/ws/algo`);
    ws.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data);
        if (evt.event === 'agent_state') {
          const idx = agents.findIndex(a => a.slug === evt.slug);
          if (idx >= 0) agents[idx].status = evt.status;
          agents = [...agents];
        }
        // LogPanel owns its own poll cadence for agent events — pushing
        // a redundant page-level fetch on every WS event wrote to dead
        // state and doubled broker calls without ever rendering the
        // result.
      } catch { /* ignore */ }
    };
    // Without the _wsDestroyed guard, the onClose reconnect schedule
    // survives onDestroy: ws.close() fires onclose, which schedules another
    // connectWS in 3s, whose own onclose schedules another, forever. Closures
    // hold $state alive (agents, agentLog) and the operator pays a permanent
    // background reconnect loop after navigating away from /automation.
    ws.onclose = () => { if (!_wsDestroyed) setTimeout(connectWS, 3000); };
  }

  // LED-style status dots — all routed through the canonical 400-level
  // tokens with /70 alpha so they sit at glass-level brightness instead
  // of the over-saturated solid look the pre-fix mix gave them. (Slice N8.)
  const statusDot = (/** @type {string} */ s) => ({
    active: 'bg-green-400/70', inactive: 'bg-slate-500',
    triggered: 'bg-red-400/70', running: 'bg-amber-400/70',
    cooldown: 'bg-amber-400/40', error: 'bg-red-400',
  }[s] || 'bg-slate-500');

  function channelSummary(/** @type {any[]} */ events) {
    if (!events) return '—';
    return events.filter(e => e.enabled).map(e => e.channel).join(', ');
  }

  /** Map a channel id → single emoji. Used on the agent row for the
   *  notify-icon strip so the operator scans which agents page Telegram
   *  vs which only log silently. Keep glyphs ascii-light so the row
   *  height doesn't jump. */
  const CHANNEL_ICON = {
    telegram:  `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`,
    email:     `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="2"/><polyline points="2,4 12,13 22,4"/></svg>`,
    websocket: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
    log:       `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
    // Event-agent channels (EVENT_CHANNELS) — ntfy gets its own bell
    // glyph; telegram_info reuses the telegram paper-plane (same
    // underlying transport, lower-noise routing) so the icon strip
    // stays recognizable without a third distinct shape.
    ntfy:          `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>`,
    telegram_info: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#22d3ee" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`,
  };
  function enabledChannels(/** @type {any[]} */ events) {
    if (!Array.isArray(events)) return [];
    return events.filter(e => e?.enabled).map(e => e.channel).filter(Boolean);
  }

  // ── Category grouping ────────────────────────────────────────────────────
  // Sprint C (2026-10, Phase 2 "Automation redesign") — the old content-
  // based categorization (`categoryFor`/`groupedAgents`: "Automated
  // Actions"/"Risk Alerts"/"Scheduled Info"/"Custom") and the custom
  // `#group=` drag-and-drop grids (`customGroups`/`_dragStart`/
  // `_dragOver`/`setAgentGroup`/`setTemplateGroup`/`onGridDrop`) are both
  // RETIRED — superseded by the kind-card → per-kind `#group=` tabs →
  // row-list drill-down built above (`_activeCard`/`_activeCardGroups`).
  // Changing a row's group now happens via Edit → decompile → edit the
  // `#group=` meta tag in the Console tab → Save, not by dragging a
  // card between grids.

  async function runInSim(/** @type {any} */ agent) {
    // Call the synthesizer endpoint — the backend builds a scenario from
    // THIS agent's condition tree at call time (no scenarios.yaml entry
    // needed), then starts the sim scoped to just this agent, with
    // suppression and schedule gates bypassed so every tick that matches
    // fires. Flip the log panel to the Simulator tab so the operator sees
    // the tick stream immediately.
    try {
      await startSimForAgent(agent.id);
      logTab = 'simulator';
      toast.info(`Sim started for: ${agent.name}`);
      // LogPanel re-fetches on its own poll once `simScope` flips to true;
      // no page-level kick needed.
    } catch (e) {
      toast.error(`Sim start failed: ${e.message || 'unknown error'}`);
    }
  }

  onMount(() => {
    // Active-tab + ?cli= seeding now lives in the reactive $effect above
    // (Sprint B, 2026-10) so a same-route navigation (not just first
    // mount) keeps working — see that effect's own comment.
    loadAll();
    loadGlobalDefaultTradeMode();
    connectWS();
    pollSimStatus();
    refreshTeardown   = visibleInterval(loadAll, 30000);
    simStatusTeardown = visibleInterval(pollSimStatus, 4000);
    // Warm the template catalog for the Brackets kind-card — idempotent,
    // module-level cached.
    loadOrderTemplates().catch(() => { /* silent — store stays empty */ });
  });

  onDestroy(() => {
    _wsDestroyed = true;
    if (ws) ws.close();
    refreshTeardown?.();
    simStatusTeardown?.();
  });
</script>

<ConfirmModal bind:this={_liveAgentConfirmRef} />
<ConfirmModal bind:this={_deleteConfirmRef} />

<svelte:head>
  <title>Automation | RamboQuant Analytics</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">
      Automation
      {#if simActive}
        <span class="ml-2 align-middle text-[length:var(--fs-sm)] px-1.5 py-0.5 rounded bg-[var(--c-long-22)] text-[var(--c-long)] border border-[var(--algo-green-border)] font-mono">
          SIMULATOR EVENTS
        </span>
      {/if}
    </h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={loadAll} loading={loading} label="agents" />
    <PageHeaderActions />
  </span>
</div>

<!-- Workspace header removed entirely (2026-10, operator request) — no
     title row, no Fullscreen button. The Console/Agents tab strip now
     shares ONE decorated row with the statement-kind dropdown + Trader
     Guide link (relocated from ConsoleTab.svelte), console-only since
     neither is meaningful on the Agents tab. `showControls={false}`
     suppresses CardHeader's whole CardControls cluster (Fullscreen
     included) while `left`/`right` snippets still render — see
     CardHeader.svelte.
     All three controls (tabs, dropdown, Trader Guide link) now live
     together in the SAME `left` zone, in that order (operator: "keep
     order dropdown just after agents tab... make it [Trader Guide] to
     display after the dropdown") — `.ch-left`'s own flex row +
     `align-items: center` + `gap` keeps them tightly adjacent and
     vertically centered as one group, rather than split across left/
     right zones with a spacer pushing them apart. `right` is no longer
     used here. -->
<section class="aw-view">
  <CardHeader showControls={false}>
    {#snippet left()}
      <span class="aw-tabs-in-header"><AutomationTabs value={activeTab} onChange={setActiveTab} /></span>
      {#if activeTab === 'console'}
        <span class="aw-kind-select">
          <Select
            value={_kindHint}
            options={_KIND_OPTIONS.map((k) => ({ value: k, label: _KIND_LABELS[k] }))}
            ariaLabel="Statement kind"
            onValueChange={_pickKind}
          />
        </span>
        <a href="/automation/guide" title="CLI grammar guide" class="btn-secondary console-guide-btn">Trader Guide</a>
      {/if}
    {/snippet}
  </CardHeader>
  <div class="aw-view-body">
    {#if activeTab === 'console'}
      <ConsoleTab bind:value={_consoleCliText} bind:editTarget={_consoleEditTarget} />
    {:else}

    <StaleBanner {error} hasData={agents.length > 0} label="Agents" />

<!-- Recursive tree renderer used by both the normal expanded view and the
     inline editor. Grammar nodes are:
       { all: [...] } | { any: [...] } | { not: node } | { metric, scope, op, value } -->
{#snippet renderCondNode(/** @type {any} */ node)}
  {#if !node || typeof node !== 'object'}
    <div class="tree-leaf">{JSON.stringify(node)}</div>
  {:else if Array.isArray(node.all)}
    <div class="tree-node tree-node-all">
      <div class="tree-op">ALL</div>
      <div class="tree-children">
        {#each node.all as child}{@render renderCondNode(child)}{/each}
      </div>
    </div>
  {:else if Array.isArray(node.any)}
    <div class="tree-node tree-node-any">
      <div class="tree-op">ANY</div>
      <div class="tree-children">
        {#each node.any as child}{@render renderCondNode(child)}{/each}
      </div>
    </div>
  {:else if node.not !== undefined}
    <div class="tree-node tree-node-not">
      <div class="tree-op">NOT</div>
      <div class="tree-children">{@render renderCondNode(node.not)}</div>
    </div>
  {:else}
    <div class="tree-leaf">{leafLabel(node)}</div>
  {/if}
{/snippet}

<!-- Per-row "view" body — shared across all four kinds. Agents/Alerts/
     Templates all share the Agent-row (`AgentInfo`) condition/events
     shape (a Token's `conditions`/`events` ARE an agent's, per
     template_registry.py's `_tr_fragment_to_agent_fields` — reusing
     `renderCondNode`/`channelSummary` here is not a coincidence, it's
     the same underlying shape). Brackets have their own distinct
     TP/SL/Wing shape, rendered separately below. -->
{#snippet agentRowView(/** @type {any} */ agent)}
  {@const _aiMeta = parseAIDescription(agent.description)}
  {#if _aiMeta.prompt || _aiMeta.why}
    <div class="ai-meta-box">
      <span class="ai-meta-pill" title="Created by AI">✦ AI</span>
      {#if _aiMeta.why}<span class="ai-meta-why">{_aiMeta.why}</span>{/if}
      {#if _aiMeta.prompt}
        <details class="ai-meta-prompt">
          <summary>prompt</summary>
          <span>{_aiMeta.prompt}</span>
        </details>
      {/if}
      {#if _aiMeta.rest}<span class="ai-meta-rest">{_aiMeta.rest}</span>{/if}
    </div>
  {:else if agent.description}
    <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic mt-1.5 mb-1">{agent.description}</div>
  {/if}

  <div class="preview-section-label mt-1">Condition</div>
  {#if agent.conditions && Object.keys(agent.conditions).length}
    <div class="preview-tree">{@render renderCondNode(agent.conditions)}</div>
  {:else}
    <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic">no conditions</div>
  {/if}

  <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] mt-2 mb-1 flex items-center flex-wrap gap-x-2 gap-y-0.5">
    <span class="text-[var(--c-muted)]">Alert via:</span> <span>{channelSummary(agent.events)}</span>
    {#if agent.tier && agent.tier !== 'medium'}
      <span class={'tier-badge tier-badge-' + agent.tier} title="Severity tier — drives topic-scoped suppression in run_cycle.">{agent.tier}</span>
    {/if}
    {#if agent.topic && agent.topic !== 'general'}
      <span class="topic-badge" title="Agents sharing a topic get cross-suppressed by tier.">{agent.topic}</span>
    {/if}
    {#if agent.kind === 'event'}
      <span class="topic-badge" title="Fixed render template, set at creation.">renderer: {rendererLabel(eventRendererKey(agent))}</span>
    {/if}
  </div>

  {#if agent.kind !== 'template'}
    <div class="preview-section-label mt-2">Actions</div>
    {#if agent.actions && agent.actions.length}
      <div class="space-y-1">
        {#each agent.actions as a}
          <div class="preview-action">
            <span class="preview-action-type">{a.type || '?'}</span>
            {#if a.params && Object.keys(a.params).length}
              <pre class="preview-action-params">{JSON.stringify(a.params, null, 2)}</pre>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic">alert-only (no actions)</div>
    {/if}
  {/if}

  {#if agent.tags && agent.tags.length}
    <div class="preview-section-label mt-2">Tags</div>
    <div class="flex flex-wrap gap-1">
      {#each agent.tags as t}<span class="topic-badge">{t}</span>{/each}
    </div>
  {/if}

  <div class="flex items-center justify-between text-[length:var(--fs-xs)] text-[var(--c-muted)] mt-2">
    <span>
      Last fire: {agent.last_triggered_at ? logTime(new Date(agent.last_triggered_at)) : '—'}
      {#if agent.kind !== 'template'}
        <span class="mx-1">|</span>
        Count: {agent.trigger_count}{#if agent.lifespan_type === 'n_fires' && agent.lifespan_max_fires}/{agent.lifespan_max_fires}{/if}
        <span class="mx-1">|</span>
        Cooldown: {agent.cooldown_minutes}m
        <span class="mx-1">|</span>
        Scope: {agent.scope}
      {/if}
      {#if agent.kind !== 'event' && agent.kind !== 'template'}
        <span class="mx-1">|</span>
        Mode: {(agent.trade_mode || 'paper').toUpperCase()}
        <span class="opacity-60">(global: {globalDefaultTradeMode.toUpperCase()})</span>
      {/if}
      {#if lifespanChip(agent)}
        {@const _lc = lifespanChip(agent)}
        <span class="mx-1">|</span>
        <span class={'lifespan-chip lifespan-chip-' + _lc.color} title={_lc.tooltip}>{_lc.label}</span>
      {/if}
    </span>
  </div>
{/snippet}

{#snippet bracketRowView(/** @type {any} */ tpl)}
  {#if tpl.description}
    <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic mt-1.5 mb-1">{tpl.description}</div>
  {/if}
  <div class="preview-section-label mt-1">Applies to</div>
  <div class="text-[length:var(--fs-sm)]">{tpl.applies_to}</div>
  <div class="preview-section-label mt-2">TP / SL / Wing</div>
  <div class="text-[length:var(--fs-sm)] flex flex-wrap gap-x-3 gap-y-0.5">
    {#if tpl.tp_pct != null}<span>TP {tpl.tp_pct}% ({tpl.tp_order_type})</span>{/if}
    {#if tpl.sl_pct != null}<span>SL {tpl.sl_pct}%</span>{/if}
    {#if tpl.sl_trail_pct != null}<span>Trail {tpl.sl_trail_pct}%</span>{/if}
    {#if tpl.wing_premium_pct != null}<span>Wing premium {tpl.wing_premium_pct}%</span>{/if}
    {#if tpl.wing_strike_offset != null}<span>Wing offset {tpl.wing_strike_offset}</span>{/if}
    {#if tpl.tp_pct == null && tpl.sl_pct == null && tpl.wing_premium_pct == null && tpl.wing_strike_offset == null}
      <span class="text-[var(--c-muted)] italic">none set</span>
    {/if}
  </div>
  {#if tpl.tags && tpl.tags.length}
    <div class="preview-section-label mt-2">Tags</div>
    <div class="flex flex-wrap gap-1">{#each tpl.tags as t}<span class="topic-badge">{t}</span>{/each}</div>
  {/if}
  <div class="flex items-center gap-2 text-[length:var(--fs-xs)] text-[var(--c-muted)] mt-2">
    {#if tpl.is_system}<span class="topic-badge" title="Seeded built-in — body/description are code-owned">system</span>{/if}
    {#if tpl.is_default}<span class="topic-badge" title="Default bracket for this applies_to scope">default</span>{/if}
    {#if !tpl.is_active}<span class="topic-badge" title="Deactivated">inactive</span>{/if}
  </div>
{/snippet}

<!-- 2026-10-12 (revised, supersedes Sprint C's kind-count-card → per-kind
     #group= tabs → row-list drill-down) — kind cards are now ALWAYS
     visible (never replaced by a drilled-down screen), exactly one is
     active at a time, and the row list for the active kind renders
     directly below — matches OrderBook.svelte's `.ob-status-bar`/`.ob-sc`
     reference model exactly. Group tabs are gone; each row shows its own
     `group` as a tag instead, and rows are sorted by group
     (`_sortedActiveCardRows`). -->
<div class="page-grid kind-card-grid mb-3" role="list">
  {#each KIND_CARDS as card (card.id)}
    <button type="button" class="kind-card" class:is-active={_activeCard === card.id} data-kind={card.id}
            onclick={() => selectCard(card.id)}>
      <span class="kind-card-n">{_cardCounts[card.id] ?? 0}</span>
      <span class="kind-card-l">{card.label}</span>
    </button>
  {/each}
</div>

  <div class="page-grid agent-group-grid mt-2" role="list">
    {#each _sortedActiveCardRows as row (row.id ?? row.slug)}
      {@const _rowKey = `${_activeCard}:${row.id ?? row.slug}`}
      {@const isOpen = _expandedRowKey === _rowKey}
      <div class="algo-status-card {row.status === 'triggered' ? 'animate-pulse' : ''}"
           data-status={row.status ?? (row.is_active ? 'active' : 'inactive')} role="listitem" style="padding: 0">
        <div role="button" tabindex="0" aria-expanded={isOpen}
             onclick={() => toggleRowView(_rowKey)}
             onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleRowView(_rowKey); } }}
             class="w-full flex flex-col gap-0.5 px-2 py-1 text-left cursor-pointer select-none">
        <div class="flex items-center gap-2">
          {#if _activeCard === 'agents' || _activeCard === 'alerts'}
            <span class="w-2 h-2 rounded-full {statusDot(row.status)} flex-shrink-0"></span>
          {/if}
          <span class="flex-1 min-w-0 flex flex-col leading-tight">
            <span class="text-[length:var(--fs-sm)] text-[var(--c-action)] truncate">{row.name}</span>
            {#if row.long_name}
              <span class="text-[length:var(--fs-sm)] font-mono truncate" style="color: var(--c-muted)">{row.long_name}</span>
            {/if}
          </span>
          <DisclosureChevron open={isOpen} ariaLabel={isOpen ? 'Collapse row' : 'Expand row'} />
        </div>
        <!-- Collapsed-state detail chip row (2026-10-12, operator: "make
             agent row card styling similar to order card styling... name,
             group and short description") — reuses the SAME `.log-chip`
             class OrderCard.svelte's own chip row already uses (app.css,
             explicitly documented there as shared across log/orders/
             notification surfaces), instead of a bespoke style, so a
             collapsed agent row reads with the same density/chrome as a
             collapsed order row. Expand/collapse-on-click behavior
             itself is unchanged — only the collapsed view shows more.

             Group folded into the description text as a trailing
             `#group:name` tag (2026-10-12, operator: "instead of having
             a separate group button... put it in the description, as
             #group: at the end of the description. this will reduce
             space wastage") — replaces the earlier standalone "group:"
             chip. Only appends the tag for a REAL custom group (an
             ungrouped row shows nothing extra, which is the actual
             space-saving — tagging every row "#group:Ungrouped" would
             be more noise, not less). The whole chip is omitted only
             when there's neither a description nor a group. -->
        {#if row.description || row.group}
          <div class="flex flex-wrap items-center gap-y-1 px-2 pb-1">
            <span class="log-chip" title={row.description || ''}>{row.description}{row.description && row.group ? ' ' : ''}{row.group ? `#group:${row.group}` : ''}</span>
          </div>
        {/if}
        </div>

        {#if isOpen}
          <div class="px-2 pb-2 border-t" style="border-top-color: rgba(126,151,184,0.10)">
            <!-- 2026-10-13 (operator: declutter the collapsed row) — the
                 notify-channel icons, trade-mode L/P toggle, and ON/OFF
                 toggle are action/detail controls, not identity, so they
                 now live here (expanded view only) instead of cluttering
                 the always-visible collapsed row above. Same gating as
                 before (template rows never get trade-mode/ON-OFF). -->
            <div class="flex items-center justify-end gap-2 pb-2">
              {#if _activeCard === 'agents' || _activeCard === 'alerts'}
                <span class="agent-row-icons" title={'Notify: ' + (enabledChannels(row.events).join(', ') || 'none')}>
                  {#each enabledChannels(row.events) as ch (ch)}
                    <span class="agent-notify-ico" aria-label={ch}>{@html CHANNEL_ICON[ch] || '•'}</span>
                  {/each}
                </span>
              {/if}
              {#if _activeCard === 'agents' && row.kind !== 'template'}
                <button type="button"
                  onclick={(e) => { e.stopPropagation(); toggleTradeMode(row); }}
                  title={`Trade mode: ${(row.trade_mode || 'paper').toUpperCase()} — click to flip (paper ↔ live)`}
                  class="text-[length:var(--fs-xs)] px-1.5 py-0 rounded font-bold border flex-shrink-0
                    {(row.trade_mode || 'paper') === 'live'
                      ? 'bg-red-500/15 text-red-400 border-red-500/40'
                      : 'bg-[var(--algo-sky-bg)] text-[var(--algo-sky)] border-sky-500/40'}">
                  {(row.trade_mode || 'paper') === 'live' ? 'L' : 'P'}
                </button>
              {/if}
              {#if (_activeCard === 'agents' || _activeCard === 'alerts') && row.kind !== 'template'}
                <button type="button"
                  onclick={(e) => { e.stopPropagation(); toggle(row); }}
                  class="text-[length:var(--fs-xs)] px-1.5 py-0 rounded font-medium border flex-shrink-0
                    {row.status !== 'inactive'
                      ? 'bg-green-500/15 text-[var(--algo-green)] border-green-500/40'
                      : 'bg-slate-700/40 text-slate-400 border-slate-500/30'}">
                  {row.status !== 'inactive' ? 'ON' : 'OFF'}
                </button>
              {/if}
            </div>
            {#if _activeCard === 'brackets'}
              {@render bracketRowView(row)}
            {:else}
              {@render agentRowView(row)}
            {/if}
            <div class="flex items-center justify-end gap-3 text-[length:var(--fs-xs)] mt-2">
              {#if (_activeCard === 'agents' || _activeCard === 'alerts') && !isDemo && row.kind !== 'template'}
                <button type="button" onclick={(e) => { e.stopPropagation(); runInSim(row); }}
                  title="Dry-fire this agent in the Simulator (bypasses schedule / cooldown / baseline)"
                  class="text-[var(--c-long)] hover:underline">Run in Simulator</button>
              {/if}
              <button type="button" onclick={(e) => { e.stopPropagation(); editRow(_activeCard, row); }}
                class="text-[var(--c-action)] hover:underline">Edit</button>
              {#if !(row.is_system)}
                <button type="button" onclick={(e) => { e.stopPropagation(); deleteRow(_activeCard, row); }}
                  class="text-[var(--c-short)] hover:underline">Delete</button>
              {/if}
            </div>
          </div>
        {/if}
      </div>
    {/each}
    {#if !_sortedActiveCardRows.length}
      <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic">No rows in this card.</div>
    {/if}
  </div>

    {/if}
  </div>
</section>

<style>
  /* Mobile: the bare section heading sits directly in .algo-content
     (padding-left: 0 on mobile) — indent it to match the cards beside it. */
  @media (max-width: 640px) {
    .section-heading { padding-left: 0.7rem; }
  }

  /* ── Console/Agents tab-strip card — wraps the CardHeader (tabs +
     kind-dropdown/Guide link) and whichever sub-view is active. No
     Fullscreen control here (Workspace header removed, 2026-10 operator
     request) — plain flex column, no `.fs-card-on`/`.fs-content-fill`. */
  .aw-view {
    display: flex;
    flex-direction: column;
  }
  .aw-view-body {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  /* Guide link + kind-select — sized/aligned to match the Orders CLI
     tab's own reference convention (2026-10-15, operator: "console
     dropdown and trader guide elements on header should look like
     order cli header in terms of size, position, and vertical
     alignment") — `.oes-cli-guide-link` (SymbolPanel.svelte /
     OrderCliTab.svelte) uses `min-height: var(--ctl-h, 1.7rem)` with
     ZERO vertical padding (height comes purely from min-height, not
     from padding), not the `.btn-secondary`-derived vertical padding
     this recipe used before. That earlier recipe (0.25rem vertical
     padding, converging on Select's own then-24.8px natural height)
     is why Automation's header row (33.6px) measured taller than the
     Orders CLI tab's reference row (28.2px) — this still isn't 0.6rem
     horizontal + min-height, it's padding-driven height, same class of
     mismatch the CLI-tab fix (earlier this session) already corrected
     once on Orders. Converging both elements on the SAME --ctl-h floor
     here fixes the height mismatch at the source instead of fighting
     it with a different padding recipe per element. */
  .console-guide-btn {
    display: inline-flex;
    align-items: center;
    white-space: nowrap;
    text-decoration: none;
    min-height: 1.7rem;
    padding: 0 0.6rem;
    font-size: var(--fs-sm);
    box-sizing: border-box;
  }
  /* Scopes the shared $lib/Select.svelte's trigger to the SAME --ctl-h
     floor as .console-guide-btn above. Select.svelte has no `class`/
     style passthrough prop, so this is scoped via the wrapping <span>
     in the markup rather than touching the shared component (narrower
     blast radius — Select is also used on /admin/settings).
     2026-10-15 — operator follow-up: width reduced 20% from the first
     pass's 7.5rem (still comfortably fits "Bracket", the longest kind
     label) down to 6rem; "it is touching the lower border of the
     header" — the trigger's own 1.7rem min-height left only ~1px
     clearance from the header's 1px bottom border at the achieved
     28.1875px row height (confirmed live) — a 1px vertical margin now
     gives real breathing room without changing the overall row height
     target from the earlier alignment fix; margin-right adds the
     requested gap before the Trader Guide link (separate from
     `.ch-left`'s own smaller `--ch-gap` default, which otherwise spaces
     every header child identically). */
  .aw-kind-select {
    display: inline-flex;
    min-width: 6rem;
    margin: 1px 0.5rem 1px 0;
  }
  .aw-kind-select :global(.rbq-select-trigger) {
    min-height: 1.7rem;
    padding: 0 0.5rem 0 0.4rem;
    box-sizing: border-box;
  }
  /* 2026-10-15 — operator: "I want trader guide and dropdown control in
     console/agents tab header to be aligned with tab text, increasing
     the height of the header like ticket/chain/cli tab header." Live
     measurement found the CONSOLE/AGENTS tab text sitting flush to the
     TOP of `.ch-left` (not vertically centered with Select/Guide, which
     DO correctly center via `.ch-left`'s own `align-items: center`) —
     root cause: `AutomationTabs.svelte`'s `.aw-tabs-wrap` carries
     `margin: 0 0 0.6rem 0` (meant for page-body spacing below the strip
     in its original standalone usage), and that bottom margin is
     counted as part of the flex item's cross-axis box here, consuming
     almost the entire gap `align-items: center` would otherwise use to
     push the wrapper down — leaving it pinned near the top instead.
     Zeroing the margin ONLY in this CardHeader context (not touching
     the shared component) lets `align-items: center` center the tab
     strip the same way it already centers Select/Guide, matching the
     Orders Ticket/Chain/CLI tab row's own reference behavior (confirmed
     live: tab text center there already matches LTP/CHASE center
     exactly). */
  .aw-tabs-in-header {
    display: inline-flex;
  }
  .aw-tabs-in-header :global(.aw-tabs-wrap) {
    margin: 0;
  }
  /* ── Agent group grid — uses canonical .page-grid for layout ───────
     Overrides auto-fill with a fixed 2-column layout on desktop so each
     row card takes exactly half the row width. Child overflow fix:
     `min-width: 0` lets the track honour `1fr` and allows inner overflow
     guards (word-break, overflow-x) to engage. Scroll-capped so a
     large group's row list stays a bounded scrollable panel rather
     than growing the whole page unbounded. Sprint C (2026-10) reuses
     this SAME class for every kind's row list (agents/alerts/brackets/
     templates) — previously Agents-only, with a separate
     `.automation-mini-grid` for Brackets/custom groups (removed, no
     longer needed now there's one row-list shape per kind). */
  .agent-group-grid {
    grid-template-columns: repeat(2, 1fr);
    max-height: 32rem;
    overflow-y: auto;
  }
  @media (max-width: 768px) {
    .agent-group-grid { grid-template-columns: 1fr; }
  }
  .agent-group-grid > * {
    min-width: 0;
    max-width: 100%;
  }

  /* ── Kind-count-card row (Sprint C, 2026-10) — Agents/Alerts/Brackets/
     Templates summary cards, same status-chip-count pattern as
     OrderBook.svelte's `.ob-status-bar`/`.ob-sc` (count + label per
     button, clicking drills into that kind).
     REVERSED 2026-10-11 (explicit operator instruction) — originally
     deliberately palette-neutral ("all four cards are peers, not a
     severity/state spectrum"). Operator now wants each kind visually
     distinct, matching the decoration level of OrderBook.svelte's
     `.ob-sc[data-status="..."]` chips. Same mechanism: color keyed off
     the existing `data-kind={card.id}` attribute, reusing existing
     app.css palette tokens (no new hex colors) — amber/sky/cyan/violet,
     deliberately NOT green/red (long/short) to avoid implying a P&L
     sentiment these peer cards don't carry. */
  .kind-card-grid {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .kind-card {
    flex: 1 1 8rem;
    min-width: 7rem;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.15rem;
    padding: 0.6rem 0.5rem;
    border-radius: 0.4rem;
    border: 1px solid var(--card-divider);
    background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    cursor: pointer;
    transition: border-color 0.08s;
  }
  .kind-card:hover { border-color: rgba(251,191,36,0.35); }
  .kind-card[data-kind="agents"]:hover    { border-color: var(--algo-amber-border); }
  .kind-card[data-kind="alerts"]:hover    { border-color: var(--algo-sky-border); }
  .kind-card[data-kind="brackets"]:hover  { border-color: var(--algo-green-border); }

  /* ── Always-active kind-tinted background + border (2026-10-12) —
     matches OrderBook.svelte's `.ob-sc[data-status]` decoration level
     exactly: a persistent tinted gradient overlay (not hover-only) plus
     an amber inset ring on `.is-active`, since exactly one kind card is
     always the active filter now (no more drill-down/back navigation —
     operator: "keep agents status cards always... toggle... instead of
     using go back"). RGB triples match the existing --algo-amber/-sky/
     -green tokens verbatim (app.css) — no new colors.

     2026-10-13 (operator request) — Brackets moved OFF the amber/"Open"
     scheme it briefly shared with Agents, onto the GREEN/"Filled" scheme
     instead — the exact tokens Orders' `.ob-sc[data-status="active"]`
     chip uses (`rgba(74,222,128,...)` / `--c-long` / `--algo-green-
     border[-soft]`). Agents and Alerts are unaffected; all three
     kind-cards are distinct again. */
  .kind-card.is-active {
    box-shadow: 0 0 0 2px rgba(251,191,36,0.55) inset;
  }
  .kind-card[data-kind="agents"] {
    background:
      linear-gradient(180deg, rgba(251,191,36,0.14) 0%, rgba(251,191,36,0.04) 60%, rgba(0,0,0,0.08) 100%),
      linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    border-color: var(--algo-amber-border-soft);
  }
  .kind-card[data-kind="alerts"] {
    background:
      linear-gradient(180deg, rgba(125,211,252,0.14) 0%, rgba(125,211,252,0.04) 60%, rgba(0,0,0,0.08) 100%),
      linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    border-color: var(--algo-sky-border-soft);
  }
  .kind-card[data-kind="brackets"] {
    background:
      linear-gradient(180deg, rgba(74,222,128,0.14) 0%, rgba(74,222,128,0.04) 60%, rgba(0,0,0,0.08) 100%),
      linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    border-color: var(--algo-green-border-soft);
  }
  .kind-card.is-active[data-kind="agents"]    { border-color: var(--algo-amber-border); }
  .kind-card.is-active[data-kind="alerts"]    { border-color: var(--algo-sky-border); }
  .kind-card.is-active[data-kind="brackets"]  { border-color: var(--algo-green-border); }

  .kind-card-n {
    font-size: var(--fs-xl);
    font-weight: 700;
    font-family: var(--font-numeric);
    color: var(--c-action);
  }
  .kind-card[data-kind="agents"]    .kind-card-n { color: var(--c-action); }
  .kind-card[data-kind="alerts"]    .kind-card-n { color: var(--algo-sky); }
  .kind-card[data-kind="brackets"]  .kind-card-n { color: var(--c-long); }
  .kind-card-l {
    font-size: var(--fs-sm);
    color: var(--c-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  /* Label text colored per kind too (2026-10-12, operator: "alerts and
     brackets background, border, text colors can be different from
     agents") — same per-kind tokens `.kind-card-n` already uses, so
     background/border/number/label all agree on one color per card. */
  .kind-card[data-kind="agents"]    .kind-card-l { color: var(--c-action); }
  .kind-card[data-kind="alerts"]    .kind-card-l { color: var(--algo-sky); }
  .kind-card[data-kind="brackets"]  .kind-card-l { color: var(--c-long); }

  /* ── Agent row card chrome — matches agent-templates' .frag-row look
     (same literal gradient/border/radius) for visual parity across the
     two automation sub-pages. Scoped to this component only — the shared
     global .algo-status-card in app.css (used on 9+ other pages) is
     untouched; [data-status="..."] variants there only set --st-fg/
     --st-bg/--st-border for the nested .algo-status-pill badge, never
     the card container's own background/border, so no conflict. */
  .algo-status-card {
    background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    /* 0.10 (matching agent-templates' .frag-row) read as near-invisible
       against this dark gradient — originally bumped to --card-divider's
       pre-2026-10-13-round-2 value (0.20) so each row's edge is actually
       perceptible while staying subtle. Pinned to that literal 0.20 value
       directly (rather than `var(--card-divider)`, now 0.40 after the
       round-2 CardHeader-contrast bump) so this row chrome keeps the
       exact subtlety it was tuned for — do NOT repoint this back to
       var(--card-divider), that would make every agent row's edge nearly
       as strong as the kind-count-card borders above, which isn't the
       look this was tuned for. */
    border: 1px solid rgba(126, 151, 184, 0.20);
    border-radius: 0.3rem;
    overflow: hidden;
    transition: border-color 0.08s;
  }
  .algo-status-card:hover { border-color: rgba(251,191,36,0.25); }

  /* ── Per-row status border accent (2026-10) — real agent status
     vocabulary (statusDot(), above): active/inactive/triggered/running/
     cooldown/error. Brackets/Templates rows carry no native `status`
     field; the markup falls back to is_active → 'active'/'inactive' so
     every kind gets a color. Distinct from app.css's shared
     [data-status] block (sets --st-fg/--st-bg/--st-border for the
     nested .algo-status-pill badge used by OrderBook/OrderCard — never
     a border) — this is a LOCAL border-only override, scoped to this
     file, reusing the same existing palette tokens. `border-left`
     (not the full border) preserves the flat gradient background +
     thin divider while giving each row a readable at-a-glance accent,
     matching the decoration level of OrderBook.svelte's
     .ob-sc[data-status] chips. */
  .algo-status-card[data-status="active"]    { border-left: 3px solid var(--algo-green-border); }
  /* Pinned to the same 0.20 literal as .algo-status-card's own base
     border above (not var(--card-divider), now 0.40 post-round-2) —
     "inactive" is meant to read as "same neutral edge color as the rest
     of the card, just a touch thicker," not a visibly different shade
     on the left edge only. */
  .algo-status-card[data-status="inactive"]  { border-left: 3px solid rgba(126, 151, 184, 0.20); }
  .algo-status-card[data-status="running"]   { border-left: 3px solid var(--algo-amber-border); }
  .algo-status-card[data-status="cooldown"]  { border-left: 3px solid var(--algo-amber-border-soft); }
  .algo-status-card[data-status="triggered"],
  .algo-status-card[data-status="error"]     { border-left: 3px solid var(--algo-red-border); }

  /* Notify-channel icon strip on each agent row — sits between the
     name and the trade-mode / ON-OFF cluster on the right. Single
     consistent palette (sky-300 at low alpha) so all controller-style
     icons (notify, chevron, refresh, fullscreen) read as one family. */
  .agent-row-icons {
    display: inline-flex;
    align-items: center;
    gap: 0.15rem;
    flex-shrink: 0;
  }
  .agent-notify-ico {
    font-size: var(--fs-md);
    line-height: 1;
    opacity: 0.85;
    filter: grayscale(0.4);
  }
  /* AI-provenance meta box on the expanded agent row.
     Shows the violet "✦ AI" pill, the LLM's why_summary, and a
     foldable prompt details element. Uses --algo-ai color palette. */
  .ai-meta-box {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin: 0.4rem 0 0.5rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }
  .ai-meta-pill {
    border: 1px solid rgba(167,139,250,0.45);
    background: rgba(167,139,250,0.10);
    color: var(--algo-ai);
    padding: 0.08rem 0.4rem;
    border-radius: 3px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    flex-shrink: 0;
  }
  .ai-meta-why { color: var(--algo-slate-muted); flex: 1 1 8rem; min-width: 0; }
  .ai-meta-prompt { font-size: var(--fs-sm); color: var(--algo-muted); }
  .ai-meta-prompt summary { cursor: pointer; color: #a78bfa; }
  .ai-meta-prompt summary:hover { color: #c4b5fd; }
  .ai-meta-prompt span {
    display: block;
    margin-top: 0.2rem;
    color: var(--algo-slate-muted);
    background: rgba(167,139,250,0.06);
    padding: 0.3rem 0.5rem;
    border-left: 2px solid #a78bfa;
    border-radius: 2px;
  }
  .ai-meta-rest { color: var(--algo-slate-muted); font-style: italic; flex-basis: 100%; }

  /* Live-preview styling — compact, dense, matches algo dark palette. */
  .agent-preview {
    font-size: var(--fs-md);
    color: var(--algo-slate);
    border-left: 1px dashed rgba(255,255,255,0.08);
    padding-left: 0.75rem;
  }
  .preview-heading {
    font-size: var(--fs-xs);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--algo-muted);
    margin-bottom: 0.5rem;
  }
  .preview-header { margin-bottom: 0.5rem; }
  .preview-title { font-weight: 700; color: var(--c-action); font-size: var(--fs-xl); }
  .preview-desc  { font-style: italic; color: var(--algo-slate-muted); font-size: var(--fs-sm); margin-top: 0.1rem; }
  .preview-meta  { font-size: var(--fs-xs); color: var(--algo-muted); margin-top: 0.2rem; }
  .preview-sep   { margin: 0 0.35rem; color: var(--algo-muted)40; }
  .preview-section-label {
    font-size: var(--fs-xs);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--c-action);
    margin: 0.65rem 0 0.3rem;
    border-bottom: 1px solid rgba(251,191,36,0.15);
    padding-bottom: 0.1rem;
  }
  .preview-muted { color: var(--algo-muted); font-style: italic; }
  .preview-error {
    color: var(--c-short);
    background: var(--c-short-10);
    border: 1px solid rgba(248,113,113,0.35);
    padding: 0.3rem 0.5rem;
    border-radius: 4px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }
  .preview-tree { font-family: var(--font-numeric); }
  /* Nested node pattern — indent on the left, operator badge at top, children below */
  :global(.tree-node) {
    border-left: 2px solid rgba(255,255,255,0.12);
    padding: 0.15rem 0 0.15rem 0.5rem;
    margin: 0.15rem 0;
  }
  :global(.tree-node-all) { border-left-color: var(--c-long); }
  :global(.tree-node-any) { border-left-color: var(--c-action); }
  :global(.tree-node-not) { border-left-color: var(--c-short); }
  :global(.tree-op) {
    font-size: var(--fs-xs);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    font-weight: 700;
    color: inherit;
    margin-bottom: 0.1rem;
  }
  :global(.tree-node-all .tree-op) { color: var(--c-long); }
  :global(.tree-node-any .tree-op) { color: var(--c-action); }
  :global(.tree-node-not .tree-op) { color: var(--c-short); }
  :global(.tree-children) { padding-left: 0.25rem; }
  :global(.tree-leaf) {
    font-size: var(--fs-sm);
    background: rgba(125,211,252,0.08);
    border: 1px solid rgba(125,211,252,0.2);
    color: var(--algo-slate);
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    margin: 0.15rem 0;
    display: inline-block;
    max-width: 100%;
    word-break: break-word;
    overflow-wrap: anywhere;
  }
  .preview-chip {
    font-size: var(--fs-sm);
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    border: 1px solid;
    font-family: var(--font-numeric);
  }
  .chip-on  { background: rgba(74,222,128,0.15);  color: var(--c-long); border-color: rgba(74,222,128,0.4); }
  .chip-off { background: rgba(180,200,230,0.08); color: var(--algo-muted); border-color: rgba(180,200,230,0.2); }
  .preview-action {
    background: rgba(251,191,36,0.06);
    border: 1px solid rgba(251,191,36,0.2);
    border-radius: 3px;
    padding: 0.3rem 0.4rem;
  }
  .preview-action-type { color: var(--c-action); font-weight: 700; font-family: var(--font-numeric); font-size: var(--fs-sm); }

  /* Lifespan chip — shows next to row meta when an agent is non-
     persistent. Uses the sky-blue utility palette so it reads as an
     "info tag" rather than a status (which is already colour-coded
     by the dot pill). */
  .agent-lifespan-tag {
    display: inline-block;
    padding: 0 0.3rem;
    border-radius: 2px;
    border: 1px solid var(--btn-sky-border);
    background: var(--btn-sky-bg);
    color: var(--btn-sky);
    font-family: var(--font-numeric);
    font-weight: 700;
    font-size: var(--fs-sm);
    letter-spacing: 0.04em;
  }
  /* New lifespanChip variants — color progresses sky → amber → red as
     the agent's budget is consumed. Grey is the "exhausted / done"
     terminal state. Mirrors lifespanChip()'s `color` field in stores.js. */
  .lifespan-chip {
    display: inline-block;
    padding: 0 0.3rem;
    border-radius: 2px;
    border: 1px solid;
    font-family: var(--font-numeric);
    font-weight: 700;
    font-size: var(--fs-sm);
    letter-spacing: 0.04em;
    cursor: help;
  }
  .lifespan-chip-sky   { color: #7dd3fc; border-color: rgba(125,211,252,0.45); background: rgba(125,211,252,0.10); }
  .lifespan-chip-amber { color: var(--c-action); border-color: rgba(251,191,36,0.55);  background: rgba(251,191,36,0.10); }
  .lifespan-chip-red   { color: var(--c-short); border-color: rgba(248,113,113,0.55); background: rgba(248,113,113,0.12); }
  .lifespan-chip-grey  { color: #94a3b8; border-color: rgba(148,163,184,0.40); background: rgba(148,163,184,0.10); }
  .preview-action-params {
    font-size: var(--fs-xs);
    background: rgba(0,0,0,0.25);
    color: var(--algo-slate-muted);
    padding: 0.25rem 0.35rem;
    border-radius: 2px;
    margin-top: 0.2rem;
    overflow-x: auto;
    max-width: 100%;
    min-width: 0;
    box-sizing: border-box;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* Tier badge — non-editable mini-pill rendered in each agent's row to
     surface severity at a glance. Same colour family as the edit pills
     but smaller + lowercase to read as a status marker rather than a
     button. Hidden when tier=medium (the default) so default rows stay
     visually quiet. */
  .tier-badge {
    font-size: var(--fs-xs);
    font-family: var(--font-numeric);
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0.05rem 0.32rem;
    border-radius: 999px;
    border: 1px solid;
    text-transform: lowercase;
  }
  .tier-badge-critical { background: rgba(248,113,113,0.15); color: var(--c-short); border-color: rgba(248,113,113,0.55); }
  .tier-badge-high     { background: rgba(251,191,36,0.15);  color: var(--c-action); border-color: rgba(251,191,36,0.55); }
  .tier-badge-low      { background: rgba(125,211,252,0.15); color: #7dd3fc; border-color: rgba(125,211,252,0.55); }

  /* Topic badge — secondary identifier shown alongside the tier so the
     operator can see grouped agents at a glance ("these three fires are
     all about holdings_loss"). Lower-key palette than the tier badge. */
  .topic-badge {
    font-size: var(--fs-xs);
    font-family: var(--font-numeric);
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    background: rgba(126,151,184,0.10);
    color: var(--algo-slate-muted);
    border: 1px solid rgba(255,255,255,0.12);
  }

</style>

<ActivityLogSurface
  context="page"
  heightClass="h-[50vh]"
  defaultTab={logTab}
  label="Log"
  simScope={simActive}
  multiColumn={true}
  hideInlineAccountFilter={false}
  tabs={['order','agent','alerts','terminal','simulator','system','conn','news']}
  onTabChange={(id) => { logTab = id; }}
/>
