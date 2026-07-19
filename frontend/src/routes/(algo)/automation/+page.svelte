<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { nowStamp, lastRefreshAt, formatDualTz, logTime, lifespanChip, visibleInterval } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import StaleBanner from '$lib/StaleBanner.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import {
    fetchAgents, activateAgent, deactivateAgent, updateAgent, createAgent,
    fetchSimStatus,
    startSimForAgent, aiDraftAgent,
  } from '$lib/api';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import Select   from '$lib/Select.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import DisclosureChevron from '$lib/DisclosureChevron.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';

  let agents      = $state([]);
  let _showLiveTs = $state(false);
  let loading     = $state(true);
  let error       = $state('');
  let logTab      = $state('agent');
  const algoStatus = getContext('algoStatus');
  const isDemo = $derived(algoStatus.isDemo);

  // ── Ask AI form ────────────────────────────────────────────────────
  let aiOpen     = $state(false);
  let aiPrompt   = $state('');
  let aiBusy     = $state(false);
  let aiDraft    = $state(/** @type {any} */ (null));
  let aiErrors   = $state(/** @type {string[]} */ ([]));
  let aiWarnings = $state(/** @type {string[]} */ ([]));
  let aiWhy      = $state('');
  let aiSlug     = $state('');

  async function runAIDraft() {
    if (!aiPrompt.trim()) return;
    aiBusy = true; aiErrors = []; aiWarnings = []; aiDraft = null; aiWhy = '';
    try {
      const r = await aiDraftAgent(aiPrompt.trim());
      aiDraft    = r?.draft || null;
      aiErrors   = r?.errors || [];
      aiWarnings = r?.warnings || [];
      aiWhy      = r?.why_summary || '';
    } catch (e) {
      aiErrors = [e.message || 'AI draft failed'];
    } finally { aiBusy = false; }
  }

  /** Save the AI draft as a new agent — paper, inactive, one_shot. */
  async function saveAIDraft() {
    if (!aiDraft || aiErrors.length) return;
    aiBusy = true;
    try {
      const slug = (aiSlug.trim()) || (aiDraft.name || 'ai-agent')
        .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
      await createAgent({
        slug,
        name: aiDraft.name || 'AI agent',
        description: aiDraft.description || aiWhy,
        conditions: aiDraft.conditions || {},
        events: aiDraft.events || ['telegram', 'email'],
        actions: aiDraft.actions || [],
        scope: aiDraft.scope || 'total',
        schedule: aiDraft.schedule || 'market_hours',
        cooldown_minutes: Number(aiDraft.cooldown_minutes ?? 30),
        lifespan_type: aiDraft.lifespan_type || 'one_shot',
        trade_mode: 'paper',
      });
      // Reset + reload.
      aiOpen = false; aiPrompt = ''; aiDraft = null; aiSlug = '';
      aiErrors = []; aiWarnings = []; aiWhy = '';
      await loadAgents();
    } catch (e) {
      aiErrors = [e.message || 'Save failed'];
    } finally { aiBusy = false; }
  }
  // Global simulator status — when active, the Agent-events panel swaps to
  // the simulator's event stream so operators only see sim results in the
  // algo pages while the sim is running.
  let simActive   = $state(false);
  // Symbols with captured price-history ticks. Sourced from the active
  let editing     = $state(null);     // slug of agent being edited
  let expandedSlug = $state(/** @type {string|null} */(null));
  let editForm    = $state(/** @type {{
    name: string, long_name: string, description: string,
    conditions: string, events: string, actions: string,
    cooldown_minutes: number, scope: string, schedule: string,
    fire_at_time: string,
    lifespan_type: string, lifespan_max_fires: number|string,
    lifespan_expires_at: string,
    tier: string, topic: string, digest_window_sec: number,
    trade_mode: string, debounce_minutes: number,
    tags: string, blackout_windows: string,
  }} */ ({
    name: '', long_name: '', description: '',
    conditions: '{}', events: '[]', actions: '[]',
    cooldown_minutes: 30, scope: 'total', schedule: 'market_hours',
    fire_at_time: '',
    lifespan_type: 'persistent', lifespan_max_fires: '', lifespan_expires_at: '',
    tier: 'medium', topic: 'general', digest_window_sec: 30,
    trade_mode: 'paper', debounce_minutes: 0,
    tags: '', blackout_windows: '[]',
  }));

  // Tier order matters — UI segmented control lists them critical → low
  // so the eye reads them as severity descending.
  const TIER_PILLS = [
    { value: 'critical', label: 'Critical', desc: 'Suppresses every lower tier in the same topic' },
    { value: 'high',     label: 'High',     desc: 'Suppressed by critical; suppresses medium + low' },
    { value: 'medium',   label: 'Medium',   desc: 'Default — suppressed by higher tiers in same topic' },
    { value: 'low',      label: 'Low',      desc: 'Always-suppressible — logs only when a peer fires' },
  ];
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

  function startEdit(/** @type {any} */ agent) {
    editing = agent.slug;
    // Keep the agent's row expanded so the inline editor actually renders
    // where the operator clicked.
    expandedSlug = agent.slug;
    validationErrors = [];
    validationGrammar = '';
    editForm = {
      name: agent.name,
      long_name: agent.long_name || '',
      description: agent.description || '',
      conditions: JSON.stringify(agent.conditions, null, 2),
      events: JSON.stringify(agent.events, null, 2),
      actions: JSON.stringify(agent.actions, null, 2),
      cooldown_minutes: agent.cooldown_minutes,
      scope: agent.scope,
      schedule: agent.schedule || 'market_hours',
      fire_at_time: agent.fire_at_time || '',
      lifespan_type:        agent.lifespan_type || 'persistent',
      lifespan_max_fires:   agent.lifespan_max_fires == null ? '' : agent.lifespan_max_fires,
      // ISO datetime → "YYYY-MM-DDTHH:MM" (datetime-local input format).
      // Trim seconds + tz so the native input accepts the value.
      lifespan_expires_at:  agent.lifespan_expires_at
        ? String(agent.lifespan_expires_at).slice(0, 16)
        : '',
      // Priority / topic / digest — tier drives topic-scoped suppression
      // in run_cycle; digest_window_sec batches dispatches.
      tier:                 agent.tier  || 'medium',
      topic:                agent.topic || 'general',
      digest_window_sec:    typeof agent.digest_window_sec === 'number'
                              ? agent.digest_window_sec : 30,
      // Trade mode + debounce — execution routing + spike suppression.
      trade_mode:           agent.trade_mode || 'paper',
      debounce_minutes:     typeof agent.debounce_minutes === 'number'
                              ? agent.debounce_minutes : 0,
      // Tags: comma-joined CSV in the input, parsed back to list on save.
      // Blackout windows: list of {start: "HH:MM", end: "HH:MM"} as JSON.
      tags:                 Array.isArray(agent.tags) ? agent.tags.join(', ') : '',
      blackout_windows:     JSON.stringify(agent.blackout_windows || [], null, 2),
    };
  }

  let validationErrors = $state(/** @type {string[]} */([]));
  let validationGrammar = $state('');

  // ── Live tree view of the agent under edit/create ────────────────────
  // Parsed state is derived from the three JSON textareas so every keystroke
  // reflects into the graphical tree without an explicit refresh.
  const parsedConditions = $derived.by(() => {
    try { return { ok: true, value: JSON.parse(editForm.conditions || '{}') }; }
    catch (e) { return { ok: false, error: e.message }; }
  });
  const parsedEvents = $derived.by(() => {
    try { return { ok: true, value: JSON.parse(editForm.events || '[]') }; }
    catch (e) { return { ok: false, error: e.message }; }
  });

  // ── Notify-channel checkbox helpers ────────────────────────────────
  // The four supported channels (matches backend/api/algo/events.py:dispatch).
  // Each one is one row in the edit-form checkbox grid. Description
  // shown next to the channel name so operators pick the right one.
  const ALERT_CHANNELS = [
    { id: 'telegram',  label: 'Telegram',  desc: 'Push to the ops Telegram group' },
    { id: 'email',     label: 'Email',     desc: 'SMTP to alert recipients' },
    { id: 'websocket', label: 'WebSocket', desc: 'Live UI toast / chart overlay' },
    { id: 'log',       label: 'Log',       desc: 'Server log file only (no push)' },
  ];

  /** Returns true if the channel is enabled in editForm.events (parsed). */
  function isChannelEnabled(/** @type {string} */ channelId) {
    const list = parsedEvents.ok ? (parsedEvents.value || []) : [];
    const row = list.find((e) => e?.channel === channelId);
    return !!(row && row.enabled);
  }

  /** Flip a single channel on/off in editForm.events. Re-serializes the
   *  JSON so the existing save path keeps working unchanged. Channels
   *  not present in the array are added; existing rows are toggled in
   *  place (preserves order operators set). */
  function toggleChannel(/** @type {string} */ channelId, /** @type {boolean} */ enabled) {
    let list = [];
    try { list = JSON.parse(editForm.events || '[]'); } catch { list = []; }
    if (!Array.isArray(list)) list = [];
    const idx = list.findIndex((e) => e?.channel === channelId);
    if (idx >= 0) {
      list[idx] = { ...list[idx], channel: channelId, enabled };
    } else {
      list.push({ channel: channelId, enabled });
    }
    editForm.events = JSON.stringify(list, null, 2);
  }
  const parsedActions = $derived.by(() => {
    try { return { ok: true, value: JSON.parse(editForm.actions || '[]') }; }
    catch (e) { return { ok: false, error: e.message }; }
  });

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

  async function runValidation() {
    validationErrors = []; validationGrammar = '';
    let parsed;
    try { parsed = JSON.parse(editForm.conditions); }
    catch (e) { validationErrors = [`conditions JSON invalid: ${e.message}`]; return false; }
    try {
      const { validateAgentCondition } = await import('$lib/api');
      const res = await validateAgentCondition(parsed);
      validationGrammar = res.grammar || '';
      validationErrors = res.errors || [];
      return res.ok;
    } catch (e) {
      validationErrors = [e.message || 'Validation failed'];
      return false;
    }
  }

  /**
   * Parse the blackout_windows textarea JSON.
   * Returns {ok: true, value} on success, {ok: false, error} on failure.
   */
  function _parseBlackoutWindows() {
    let bw;
    try { bw = JSON.parse(editForm.blackout_windows || '[]'); }
    catch (e) { return { ok: false, error: `blackout_windows JSON invalid: ${e.message}` }; }
    if (!Array.isArray(bw)) {
      return { ok: false, error: 'blackout_windows must be a JSON array of {start, end} entries' };
    }
    return { ok: true, value: bw };
  }

  /**
   * Build the PATCH payload from editForm + resolved tags and blackout windows.
   * All conditional field coercions (lifespan, nulls) live here.
   * @param {string[]} tagsList
   * @param {any[]} bw
   */
  function _buildEditPayload(tagsList, bw) {
    return {
      name: editForm.name,
      long_name: editForm.long_name || null,
      description: editForm.description,
      conditions: JSON.parse(editForm.conditions),
      events: JSON.parse(editForm.events),
      actions: JSON.parse(editForm.actions),
      cooldown_minutes: editForm.cooldown_minutes,
      scope: editForm.scope,
      schedule: editForm.schedule,
      fire_at_time: editForm.fire_at_time || '',
      lifespan_type: editForm.lifespan_type || 'persistent',
      lifespan_max_fires: (editForm.lifespan_type === 'n_fires'
        && editForm.lifespan_max_fires !== '' && editForm.lifespan_max_fires != null)
        ? Number(editForm.lifespan_max_fires) : null,
      lifespan_expires_at: (editForm.lifespan_type === 'until_date'
        && editForm.lifespan_expires_at)
        ? String(editForm.lifespan_expires_at) : null,
      tier:              editForm.tier  || 'medium',
      topic:             editForm.topic || 'general',
      digest_window_sec: Number(editForm.digest_window_sec) || 30,
      trade_mode:        editForm.trade_mode || 'paper',
      debounce_minutes:  Number(editForm.debounce_minutes) || 0,
      tags:              tagsList,
      blackout_windows:  bw,
    };
  }

  async function saveEdit() {
    // Server-side validation must pass for v2 trees before we touch the
    // agent row — v1 trees are accepted as-is.
    const ok = await runValidation();
    if (!ok) return;
    // Parse blackout_windows JSON — invalid surfaces as a validation error
    // (saves get blocked) instead of a silent 400 from the backend.
    const bwResult = _parseBlackoutWindows();
    if (!bwResult.ok) { validationErrors = [bwResult.error]; return; }
    // Tags: split on comma, trim, drop empty. Operator types
    // "iron-condor, nifty, review-q3" — round-tripped to a list.
    const tagsList = String(editForm.tags || '')
      .split(',').map(t => t.trim()).filter(Boolean);
    try {
      await updateAgent(editing, _buildEditPayload(tagsList, bwResult.value));
      editing = null;
      validationErrors = []; validationGrammar = '';
      toast.success(`Agent saved: ${editForm.name}`);
      await loadAgents();
    } catch (e) {
      toast.error(`Save failed: ${e.message}`);
    }
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
  };
  function enabledChannels(/** @type {any[]} */ events) {
    if (!Array.isArray(events)) return [];
    return events.filter(e => e?.enabled).map(e => e.channel).filter(Boolean);
  }

  // ── Category grouping ────────────────────────────────────────────────────
  // Derive category from slug prefix so new agents bucket automatically
  // without needing a DB field. If the catalog grows unwieldy, promote
  // this to an Agent column later.
  function categoryFor(/** @type {string} */ slug) {
    if (!slug) return 'Other';
    if (slug.startsWith('loss-')) return 'Loss & Risk';
    if (slug.includes('summary')) return 'Summaries';
    if (slug.includes('expiry') || slug.includes('close') || slug.includes('order')) return 'Automation';
    return 'Other';
  }

  const CATEGORY_ORDER = ['Loss & Risk', 'Summaries', 'Automation', 'Other'];

  function groupedAgents() {
    const out = {};
    for (const a of agents) {
      const cat = categoryFor(a.slug);
      (out[cat] = out[cat] || []).push(a);
    }
    for (const cat of Object.keys(out)) {
      out[cat].sort((a, b) => a.name.localeCompare(b.name));
    }
    return CATEGORY_ORDER
      .filter(c => out[c]?.length)
      .map(c => ({ name: c, agents: out[c] }));
  }

  // Action-type skeletons used by the quick-add pills below the Actions
  // textarea. Each entry is a legal action dict the operator can tune after
  // it lands in the JSON. Keys match the seeded grammar_tokens action list.
  const ACTION_SKELETONS = {
    close_position: {
      type: "close_position",
      params: { account: "ZG####", symbol: "<tradingsymbol>", exchange: "NFO", product: "NRML" },
    },
    // place_order — entry + template_slug. The agent places the order
    // and the standard template-attach pipeline kicks in on fill
    // (TP/SL GTTs + wing on SELL options). The skeleton ships with
    // template_slug="default-bull" as a sensible default for a BUY
    // entry; operator changes the slug to "default-short-vol" for a
    // SELL-side credit-spread strategy, or "none" to opt out of
    // auto-attachments. The earlier split into place_order +
    // place_order_templated buttons confused the operator (two
    // pills doing essentially the same thing); consolidated in
    // audit pass 6 to one pill + an editable slug field.
    place_order: {
      type: "place_order",
      params: { account: "ZG####", symbol: "<tradingsymbol>", exchange: "NFO",
                side: "BUY", qty: 50, order_type: "LIMIT",
                template_slug: "default-bull" },
    },
    chase_close_positions: {
      type: "chase_close_positions",
      params: { scope: "total", timeout_minutes: 10, adjust_pct: 0.1 },
    },
    cancel_all_orders: {
      type: "cancel_all_orders",
      params: { scope: "total" },
    },
    emit_log: {
      type: "emit_log",
      params: { level: "info", message: "Agent fired" },
    },
  };

  /** @type {(kind: keyof ACTION_SKELETONS) => void} */
  function addAction(kind) {
    let arr;
    try { arr = JSON.parse(editForm.actions || '[]'); }
    catch (_) { arr = []; }
    if (!Array.isArray(arr)) arr = [];
    arr.push(ACTION_SKELETONS[kind]);
    editForm.actions = JSON.stringify(arr, null, 2);
  }

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
    loadAll();
    connectWS();
    pollSimStatus();
    refreshTeardown   = visibleInterval(loadAll, 30000);
    simStatusTeardown = visibleInterval(pollSimStatus, 4000);
  });

  onDestroy(() => {
    _wsDestroyed = true;
    if (ws) ws.close();
    refreshTeardown?.();
    simStatusTeardown?.();
  });
</script>

<ConfirmModal bind:this={_liveAgentConfirmRef} />

<svelte:head>
  <title>Automation | RamboQuant Analytics</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">
      Automation
      {#if simActive}
        <span class="ml-2 align-middle text-[0.6rem] px-1.5 py-0.5 rounded bg-[var(--c-long)]/20 text-[var(--c-long)] border border-[var(--c-long)]/40 font-mono">
          SIMULATOR EVENTS
        </span>
      {/if}
    </h1>
  </span>
  <span class="algo-ts-group">
    <span class="algo-ts" class:algo-ts-hidden={_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Live clock — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {$nowStamp}
    </span>
    <span class="algo-ts-vsep" aria-hidden="true">|</span>
    <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Last refresh — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {formatDualTz($lastRefreshAt)}
    </span>
  </span>
  <!-- History chip + Ask AI toggle are LEFT-aligned per canonical
       header rule (only Refresh + Order + Chart + Activity + Collapse
       + Fullscreen + Default-size icons sit RIGHT of ml-auto). -->
  <a href="/automation/activity" class="history-pill" title="View agent fire history">
    🔔 History
  </a>
  <button class="ai-pill" onclick={() => aiOpen = !aiOpen}>
    {aiOpen ? '× Close AI' : '✦ Ask AI'}
  </button>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={loadAll} loading={loading} label="agents" />
    <PageHeaderActions />
  </span>
</div>

<AutomationTabs />

<StaleBanner {error} hasData={agents.length > 0} label="Agents" />

{#if aiOpen}
  <!-- AI agent draft form — operator describes the rule in plain English,
       Gemini produces a draft. Lands paper + inactive + one_shot by default. -->
  <div class="ai-card">
    <div class="ai-head">
      <span class="ai-title">✦ Describe the agent</span>
      <span class="ai-hint">Lands paper · inactive · one_shot — review before activating.</span>
    </div>
    <textarea
      class="ai-prompt"
      bind:value={aiPrompt}
      placeholder='e.g. "Alert me when total positions P&L drops below -50000 — paper auto-close at -100000"'
      rows="2"
    ></textarea>
    <div class="ai-actions">
      <button class="ai-btn" onclick={runAIDraft} disabled={aiBusy || !aiPrompt.trim()}>
        {aiBusy ? 'Drafting…' : 'Draft'}
      </button>
      {#if aiDraft && !aiErrors.length}
        <input class="ai-slug" bind:value={aiSlug}
               placeholder={(aiDraft.name || 'ai-agent').toLowerCase().replace(/[^a-z0-9]+/g, '-')} />
        <button class="ai-btn ai-btn-save" onclick={saveAIDraft} disabled={aiBusy}>
          Save (paper · inactive)
        </button>
      {/if}
    </div>
    {#if aiWhy}
      <div class="ai-why">{aiWhy}</div>
    {/if}
    {#if aiWarnings.length}
      <ul class="ai-warns">
        {#each aiWarnings as w}<li>⚠ {w}</li>{/each}
      </ul>
    {/if}
    {#if aiErrors.length}
      <ul class="ai-errs">
        {#each aiErrors as e}<li>✗ {e}</li>{/each}
      </ul>
    {/if}
    {#if aiDraft}
      <details class="ai-json">
        <summary>Draft JSON</summary>
        <pre>{JSON.stringify(aiDraft, null, 2)}</pre>
      </details>
    {/if}
  </div>
{/if}

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

<!-- Grouped agent list — compact rows, click to expand.
     Two-column magazine-flow on ≥1024 px (lg:columns-2): items
     fill column 1 top-to-bottom, then column 2 starts; expanding a
     card just grows its own column without pulling its row-neighbour
     down (true CSS-columns behaviour, unlike a 2-col Grid where
     row siblings would equalise heights). Single column on mobile. -->
{#each groupedAgents() as group}
  <h2 class="algo-section-title mt-3 mb-1.5 border-b border-white/10 pb-0.5">
    {group.name}
    <span class="opacity-60 font-normal ml-1">({group.agents.length})</span>
  </h2>
  <div class="agent-group-grid mb-3">
    {#each group.agents as agent}
      {@const isOpen = expandedSlug === agent.slug}
      <div class="algo-status-card {agent.status === 'triggered' ? 'animate-pulse' : ''}"
           data-status={agent.status}
           style="padding: 0">
        <!-- Compact row (always visible). Div + role="button" so the inner
             ON/OFF can stay a real <button> — nested buttons aren't valid. -->
        <div role="button" tabindex="0"
          aria-expanded={isOpen}
          onclick={() => expandedSlug = isOpen ? null : agent.slug}
          onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); expandedSlug = isOpen ? null : agent.slug; } }}
          class="w-full flex items-center gap-2 px-2 py-1 text-left cursor-pointer select-none">
          <span class="w-2 h-2 rounded-full {statusDot(agent.status)} flex-shrink-0"></span>
          <!-- Name column: display name on top, 3-part long_name
               (condition - alert - action) below in muted mono so an
               operator can scan "what does this agent do" without
               expanding the row. -->
          <span class="flex-1 min-w-0 flex flex-col leading-tight">
            <span class="text-xs text-[var(--c-action)] truncate">{agent.name}</span>
            {#if agent.long_name}
              <span class="text-[0.55rem] text-[var(--c-muted)] font-mono truncate">{agent.long_name}</span>
            {/if}
          </span>
          <!-- Notify-channel icon strip — one tiny emoji per enabled
               channel. Grouped on the right alongside the trade-mode +
               ON/OFF buttons + chevron so every controller-style affordance
               clusters in one visual zone. Operator scans "📨✉" to know
               "this agent pages Telegram + email"; the tooltip carries
               the full channel list for accessibility. -->
          <span class="agent-row-icons" title={'Notify: ' + (enabledChannels(agent.events).join(', ') || 'none')}>
            {#each enabledChannels(agent.events) as ch (ch)}
              <span class="agent-notify-ico" aria-label={ch}>{@html CHANNEL_ICON[ch] || '•'}</span>
            {/each}
          </span>
          <button type="button"
            onclick={(e) => { e.stopPropagation(); toggleTradeMode(agent); }}
            title={`Trade mode: ${(agent.trade_mode || 'paper').toUpperCase()} — click to flip (paper ↔ live)`}
            class="text-[0.55rem] px-1.5 py-0 rounded font-bold border flex-shrink-0
              {(agent.trade_mode || 'paper') === 'live'
                ? 'bg-red-500/15 text-red-400 border-red-500/40'
                : 'bg-sky-500/15 text-sky-400 border-sky-500/40'}">
            {(agent.trade_mode || 'paper') === 'live' ? 'L' : 'P'}
          </button>
          <button type="button"
            onclick={(e) => { e.stopPropagation(); toggle(agent); }}
            class="text-[0.55rem] px-1.5 py-0 rounded font-medium border flex-shrink-0
              {agent.status !== 'inactive'
                ? 'bg-green-500/15 text-green-400 border-green-500/40'
                : 'bg-slate-700/40 text-slate-400 border-slate-500/30'}">
            {agent.status !== 'inactive' ? 'ON' : 'OFF'}
          </button>
          <DisclosureChevron open={isOpen} ariaLabel={isOpen ? 'Collapse row' : 'Expand row'} />
        </div>

        {#if isOpen}
          {#if editing === agent.slug}
            <!-- ──────── Inline editor (form on top, tree preview below) ──────── -->
            <div class="px-3 pb-3 pt-2 border-t border-white/5">
              <!-- ── FORM FIELDS ── -->
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <span class="field-label">Name</span>
                  <input bind:value={editForm.name} class="field-input" />
                </div>
                <div>
                  <span class="field-label">
                    Long name
                    <InfoHint popup text="Operator-readable 3-part label: <b>when:&lt;condition&gt;</b> &mdash; <b>alert:&lt;notify&gt;</b> &mdash; <b>do:&lt;action&gt;</b>. Surfaces under the short name on the agents row so an operator scanning the list sees what each agent actually does without expanding." />
                  </span>
                  <input bind:value={editForm.long_name}
                         placeholder="when:positions.total.pnl<=-50k   alert:critical/tg+email   do:notify-only"
                         class="field-input font-mono text-[0.6rem]" />
                </div>
                <div class="md:col-span-2">
                  <span class="field-label">Description</span>
                  <input bind:value={editForm.description} class="field-input" />
                </div>
                <div>
                  <span class="field-label">Scope</span>
                  <Select ariaLabel="Scope" bind:value={editForm.scope}
                    options={[
                      { value: 'total',       label: 'Total Only' },
                      { value: 'per_account', label: 'Per Account' },
                    ]} />
                </div>
                <div>
                  <span class="field-label">Schedule</span>
                  <Select ariaLabel="Schedule" bind:value={editForm.schedule}
                    options={[
                      { value: 'market_hours', label: 'Market Hours' },
                      { value: 'always',       label: 'Always' },
                    ]} />
                </div>
                <div>
                  <span class="field-label">Cooldown (minutes)</span>
                  <input type="number" bind:value={editForm.cooldown_minutes} class="field-input" />
                </div>
                <div>
                  <span class="field-label">
                    Debounce (minutes)
                    <InfoHint popup text="Fire only when the condition holds for N consecutive evaluations spanning at least N minutes. <b>0</b> = fire immediately on first true tick. Use to suppress single-tick spikes (e.g. a Kite glitch dropping pnl_pct to -2.1% for one cycle). Industry analogue: Datadog/Grafana <b>For:</b>, CloudWatch <b>EvaluationPeriods</b>." />
                  </span>
                  <input type="number" min="0"
                         bind:value={editForm.debounce_minutes}
                         class="field-input" />
                </div>
                <div>
                  <span class="field-label">
                    Trade mode
                    <InfoHint popup text="Per-agent execution mode. <b>paper</b> = simulated fills against real bid/ask (default). <b>live</b> = real broker orders. Resolved against the engine's master <code>execution.paper_trading_mode</code> setting + the branch gate — dev always forces paper regardless." />
                  </span>
                  <Select ariaLabel="Trade mode" bind:value={editForm.trade_mode}
                    options={[
                      { value: 'paper', label: 'Paper (simulated)' },
                      { value: 'live',  label: 'Live (real broker)' },
                    ]} />
                </div>
                <div>
                  <span class="field-label">
                    Fire at (IST)
                    <InfoHint popup text="Optional <b>HH:MM IST</b> time-of-day gate. When set, agent only evaluates inside a small window around this wall-clock time (covers one background poll cycle ~ 6 min). Empty = no gate, evaluates every tick. Use for daily summaries, EOD scans, expiry-day close orders." />
                  </span>
                  <input type="time"
                    bind:value={editForm.fire_at_time}
                    placeholder="HH:MM"
                    class="field-input" />
                </div>
                <!-- Lifespan — controls whether the agent persists or
                     auto-completes after firing. one_shot / n_fires let
                     algos spawn temporary agents (expiry-day auto-close,
                     "watch this until X" rules) that drop out of the
                     active set on completion instead of needing a manual
                     deactivate. -->
                <div>
                  <span class="field-label">Lifespan</span>
                  <Select ariaLabel="Lifespan" bind:value={editForm.lifespan_type}
                    options={[
                      { value: 'persistent', label: 'Persistent (default)' },
                      { value: 'one_shot',   label: 'One-shot (fires once)' },
                      { value: 'n_fires',    label: 'N fires' },
                      { value: 'until_date', label: 'Until date' },
                    ]} />
                  {#if lifespanChip(agent)}
                    {@const _ls = lifespanChip(agent)}
                    <div class="text-[0.55rem] text-[var(--c-muted)] mt-1" title={_ls.tooltip}>
                      Current: <span class={'lifespan-chip lifespan-chip-' + _ls.color}>{_ls.label}</span>
                    </div>
                  {:else if agent.lifespan_type === 'persistent'}
                    <div class="text-[0.55rem] text-[var(--c-muted)] mt-1 italic">
                      Persistent — fires until manually deactivated.
                    </div>
                  {/if}
                </div>
                {#if editForm.lifespan_type === 'n_fires'}
                  <div>
                    <span class="field-label">Max fires</span>
                    <input type="number" min="1"
                           bind:value={editForm.lifespan_max_fires}
                           class="field-input"
                           placeholder="e.g. 3" />
                  </div>
                {/if}
                {#if editForm.lifespan_type === 'until_date'}
                  <div>
                    <span class="field-label">Expires at (UTC)</span>
                    <input type="datetime-local"
                           bind:value={editForm.lifespan_expires_at}
                           class="field-input" />
                  </div>
                {/if}
              </div>

              <!-- ── Alert hierarchy strip (tier + topic + digest) ─────
                   Single tight row. Tier as a 4-pill segmented control
                   so all severity options are visible without a dropdown
                   click. Topic as a small text input with datalist
                   autocomplete (writes the existing topics back, so
                   ops can group new agents alongside the loss-* ones
                   in a single click). Digest stepper to the right —
                   reserved for future batching, hidden behind a tiny
                   label so it doesn't dominate. -->
              <div class="tier-strip">
                <div class="tier-strip-left">
                  <span class="field-label" style="margin-right: 0.5rem; display: inline-flex; align-items: center; gap: 0.25rem;">
                    Priority
                    <InfoHint popup text="Severity bucket (a.k.a. <b>tier</b>) — <b>critical &gt; high &gt; medium &gt; low</b>. Drives topic-scoped suppression: when multiple agents in the same topic fire on one tick, only the highest priority dispatches; the others are logged as suppressed. Industry analogue: PagerDuty <b>Urgency</b>, Opsgenie <b>Priority P1-P5</b>, Datadog <b>monitor priority</b>." />
                  </span>
                  <div class="tier-pill-row">
                    {#each TIER_PILLS as t}
                      <button type="button"
                              class={'tier-pill tier-pill-' + t.value}
                              class:on={editForm.tier === t.value}
                              onclick={() => { editForm.tier = t.value; }}
                              title={t.desc}>
                        {t.label}
                      </button>
                    {/each}
                  </div>
                </div>
                <div class="tier-strip-right">
                  <div>
                    <span class="field-label">Topic</span>
                    <input list="agent-topics"
                           bind:value={editForm.topic}
                           class="field-input"
                           placeholder="general"
                           title="Agents sharing a topic get cross-suppressed by tier. 'general' (default) opts out — no suppression." />
                    <datalist id="agent-topics">
                      <option value="holdings_loss"></option>
                      <option value="positions_loss"></option>
                      <option value="funds_warning"></option>
                      <option value="general"></option>
                    </datalist>
                  </div>
                  <div>
                    <span class="field-label" title="Reserved for future digest batching. 0 = fire immediately.">Digest&nbsp;(s)</span>
                    <input type="number" min="0" max="600"
                           bind:value={editForm.digest_window_sec}
                           class="field-input"
                           style="max-width: 5rem" />
                  </div>
                </div>
              </div>

              <!-- Tags + Blackout windows — operator-facing labels for
                   filtering on /automation (tags) and IST quiet hours
                   (blackout windows). Industry analogue: Datadog tags +
                   Grafana silences / PagerDuty maintenance windows. -->
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
                <div>
                  <span class="field-label">
                    Tags
                    <InfoHint popup text="Free-form labels for filtering. Comma-separated. Examples: <b>iron-condor, nifty, review-q3</b>. Surfaces on the agents list as chips. Industry analogue: Datadog tags, Grafana labels." />
                  </span>
                  <input bind:value={editForm.tags}
                         placeholder="iron-condor, nifty, review-q3"
                         class="field-input" />
                </div>
                <div>
                  <span class="field-label">
                    Blackout windows (JSON)
                    <InfoHint popup text="List of <b>&#123;start: 'HH:MM', end: 'HH:MM'&#125;</b> entries in IST. Agent is skipped while wall-clock IST is inside any window. Crossing-midnight windows like <code>&#123;start:'23:00',end:'01:00'&#125;</code> are supported. Industry analogue: PagerDuty maintenance windows, Grafana silences, Datadog <b>mute_until</b>." />
                  </span>
                  <textarea bind:value={editForm.blackout_windows}
                            class="field-input font-mono text-[0.6rem]" rows="3"
                            placeholder={'[{"start":"12:00","end":"13:00"}]'}></textarea>
                </div>
              </div>

              <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3">
                <div>
                  <span class="field-label">Conditions (JSON)</span>
                  <textarea bind:value={editForm.conditions} class="field-input font-mono text-[0.6rem]" rows="5"></textarea>
                </div>
                <div>
                  <span class="field-label">Alert channels</span>
                  <!-- Per-agent notify routing. Each tick adds a row
                       {channel, enabled:true} to the agent's events
                       JSONB; the dispatcher (backend/api/algo/events.py)
                       fans out the alert to every enabled channel. Saves
                       round-trip with the JSON shape unchanged on the
                       wire. -->
                  <div class="channel-grid">
                    {#each ALERT_CHANNELS as ch}
                      <label class="channel-row">
                        <input type="checkbox"
                               class="channel-check"
                               checked={isChannelEnabled(ch.id)}
                               onchange={(e) => toggleChannel(ch.id, /** @type {HTMLInputElement} */(e.target).checked)} />
                        <span class="channel-label">{ch.label}</span>
                        <span class="channel-desc">{ch.desc}</span>
                      </label>
                    {/each}
                  </div>
                </div>
                <div>
                  <div class="flex items-center justify-between flex-wrap gap-1">
                    <span class="field-label">
                      Actions (JSON)
                      <InfoHint popup text="Each <code>place_order</code> action can attach an <b>Order Template</b> (TP/SL/Wing exit rules) via <code>template_slug</code> on its params. Use <b>+ place_order (templated)</b> for entries with auto-exit attach; use <b>+ place_order</b> for entry-only. The template runs on fill — sim path goes through SimGttBook; live path through broker GTT (when fill-postback wiring lands). Catalog at <a href='/automation/templates' target='_blank'>/automation/templates</a>." />
                    </span>
                    <!-- Quick-add pills — click appends a skeleton action
                         entry so operators don't have to remember the
                         exact shape. Params are templated to legal values;
                         the operator tunes them after. -->
                    <div class="flex flex-wrap gap-1">
                      <button type="button" onclick={() => addAction('close_position')}
                        class="action-add-pill action-add-close">+ close_position</button>
                      <button type="button" onclick={() => addAction('place_order')}
                        class="action-add-pill action-add-place" title="Entry + template_slug (default-bull). Change slug to default-short-vol for SELL, or 'none' to opt out of auto-attachments.">+ place_order</button>
                      <button type="button" onclick={() => addAction('chase_close_positions')}
                        class="action-add-pill action-add-chase">+ chase_close</button>
                      <button type="button" onclick={() => addAction('cancel_all_orders')}
                        class="action-add-pill action-add-cancel">+ cancel_all</button>
                      <button type="button" onclick={() => addAction('emit_log')}
                        class="action-add-pill action-add-log">+ log</button>
                    </div>
                  </div>
                  <textarea bind:value={editForm.actions} class="field-input font-mono text-[0.6rem]" rows="5"></textarea>
                </div>
              </div>

              {#if validationErrors.length}
                <div class="mt-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.6rem] border border-red-500/40">
                  <div class="font-semibold mb-1">Condition validation failed:</div>
                  <ul class="list-disc ml-4">{#each validationErrors as err}<li>{err}</li>{/each}</ul>
                </div>
              {:else if validationGrammar}
                <div class="mt-3 p-2 rounded bg-emerald-500/10 text-emerald-300 text-[0.6rem] border border-emerald-500/30">
                  Validated — ready to save.
                </div>
              {/if}

              <div class="flex gap-2 mt-3">
                <button type="button" onclick={async () => { await runValidation(); }}
                  class="text-[0.65rem] py-1 px-3 rounded border border-[#7dd3fc]/50 bg-[#7dd3fc]/15 text-[#7dd3fc] hover:bg-[#7dd3fc]/25 font-semibold">
                  Validate
                </button>
                <button type="button" onclick={saveEdit} class="btn-primary text-[0.65rem] py-1 px-4">Save</button>
                <button type="button" onclick={() => { editing = null; validationErrors = []; validationGrammar = ''; }}
                  class="btn-secondary text-[0.65rem] py-1 px-4">Cancel</button>
              </div>

              <!-- ── LIVE TREE PREVIEW (below the form) ── -->
              <div class="agent-preview mt-4 pt-3 border-t border-white/5">
                <div class="preview-heading">Live preview</div>
                <div class="grid grid-cols-1 md:grid-cols-[1fr_1fr] gap-3">
                  <div>
                    <div class="preview-header">
                      <div class="preview-title">{editForm.name || '(unnamed agent)'}</div>
                      {#if editForm.description}
                        <div class="preview-desc">{editForm.description}</div>
                      {/if}
                      <div class="preview-meta">
                        Scope: <b>{editForm.scope}</b>
                        <span class="preview-sep">|</span>
                        Schedule: <b>{editForm.schedule}</b>
                        <span class="preview-sep">|</span>
                        Cooldown: <b>{editForm.cooldown_minutes}m</b>
                        {#if editForm.fire_at_time}
                          <span class="preview-sep">|</span>
                          Fire at: <b>{editForm.fire_at_time} IST</b>
                        {/if}
                      </div>
                    </div>
                    <div class="preview-section-label">Condition tree</div>
                    {#if parsedConditions.ok}
                      <div class="preview-tree">{@render renderCondNode(parsedConditions.value)}</div>
                    {:else}
                      <div class="preview-error">Invalid JSON: {parsedConditions.error}</div>
                    {/if}
                  </div>
                  <div>
                    <div class="preview-section-label">Notify</div>
                    {#if parsedEvents.ok}
                      {#if parsedEvents.value.length}
                        <div class="flex flex-wrap gap-1">
                          {#each parsedEvents.value as ev}
                            {@const on = ev.enabled !== false}
                            <span class="preview-chip {on ? 'chip-on' : 'chip-off'}">{ev.channel || '?'}{on ? '' : ' (off)'}</span>
                          {/each}
                        </div>
                      {:else}
                        <div class="preview-muted">no channels configured</div>
                      {/if}
                    {:else}
                      <div class="preview-error">Invalid JSON: {parsedEvents.error}</div>
                    {/if}

                    <div class="preview-section-label">Actions</div>
                    {#if parsedActions.ok}
                      {#if parsedActions.value.length}
                        <div class="space-y-1">
                          {#each parsedActions.value as a}
                            <div class="preview-action">
                              <span class="preview-action-type">{a.type || '?'}</span>
                              {#if a.params && Object.keys(a.params).length}
                                <pre class="preview-action-params">{JSON.stringify(a.params, null, 2)}</pre>
                              {/if}
                            </div>
                          {/each}
                        </div>
                      {:else}
                        <div class="preview-muted">alert-only (no actions)</div>
                      {/if}
                    {:else}
                      <div class="preview-error">Invalid JSON: {parsedActions.error}</div>
                    {/if}
                  </div>
                </div>
              </div>
            </div>
          {:else}
            {@const _aiMeta = parseAIDescription(agent.description)}
            <!-- ──────── Normal expanded view ──────── -->
            <div class="px-2 pb-2 border-t border-white/5">
              {#if _aiMeta.prompt || _aiMeta.why}
                <div class="ai-meta-box">
                  <span class="ai-meta-pill" title="Created by AI">✦ AI</span>
                  {#if _aiMeta.why}
                    <span class="ai-meta-why">{_aiMeta.why}</span>
                  {/if}
                  {#if _aiMeta.prompt}
                    <details class="ai-meta-prompt">
                      <summary>prompt</summary>
                      <span>{_aiMeta.prompt}</span>
                    </details>
                  {/if}
                  {#if _aiMeta.rest}
                    <span class="ai-meta-rest">{_aiMeta.rest}</span>
                  {/if}
                </div>
              {:else if agent.description}
                <div class="text-[0.6rem] text-[#c8d8f0]/60 italic mt-1.5 mb-1">{agent.description}</div>
              {/if}

              <!-- Condition tree (always shown; falls back to text summary when parse fails) -->
              <div class="preview-section-label mt-1">Condition</div>
              {#if agent.conditions && Object.keys(agent.conditions).length}
                <div class="preview-tree">{@render renderCondNode(agent.conditions)}</div>
              {:else}
                <div class="text-[0.6rem] text-[#c8d8f0]/60 italic">no conditions</div>
              {/if}

              <div class="text-[0.6rem] text-[#c8d8f0]/75 mt-2 mb-1 flex items-center flex-wrap gap-x-2 gap-y-0.5">
                <span class="text-[var(--c-muted)]">Alert via:</span> <span>{channelSummary(agent.events)}</span>
                {#if agent.tier && agent.tier !== 'medium'}
                  <span class={'tier-badge tier-badge-' + agent.tier}
                        title="Severity tier — drives topic-scoped suppression in run_cycle.">
                    {agent.tier}
                  </span>
                {/if}
                {#if agent.topic && agent.topic !== 'general'}
                  <span class="topic-badge"
                        title="Agents sharing a topic get cross-suppressed by tier.">
                    {agent.topic}
                  </span>
                {/if}
              </div>
              <!-- Actions list — surface each action and its params so
                   close_position / place_order / chase_close_positions are
                   visible at a glance with the account / symbol / qty they
                   target. Previously this was just a comma-joined type
                   list and the params were invisible unless the operator
                   hit Edit. -->
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
                <div class="text-[0.6rem] text-[#c8d8f0]/60 italic">alert-only (no actions)</div>
              {/if}
              <div class="flex items-center justify-between text-[0.55rem] text-[var(--c-muted)] mt-2">
                <span>
                  Last fire: {agent.last_triggered_at ? logTime(new Date(agent.last_triggered_at)) : '—'}
                  <span class="mx-1">|</span>
                  Count: {agent.trigger_count}{#if agent.lifespan_type === 'n_fires' && agent.lifespan_max_fires}/{agent.lifespan_max_fires}{/if}
                  <span class="mx-1">|</span>
                  Cooldown: {agent.cooldown_minutes}m
                  <span class="mx-1">|</span>
                  Scope: {agent.scope}
                  {#if lifespanChip(agent)}
                    {@const _lc = lifespanChip(agent)}
                    <span class="mx-1">|</span>
                    <span class={'lifespan-chip lifespan-chip-' + _lc.color} title={_lc.tooltip}>
                      {_lc.label}
                    </span>
                  {/if}
                </span>
                <span class="flex items-center gap-3">
                  {#if !isDemo}
                  <button type="button"
                    onclick={(e) => { e.stopPropagation(); runInSim(agent); }}
                    title="Dry-fire this agent in the Simulator (bypasses schedule / cooldown / baseline)"
                    class="text-[var(--c-long)] hover:underline">Run in Simulator</button>
                  {:else}
                  <span title="Demo: sim disabled"
                    class="text-[var(--c-muted)] cursor-not-allowed opacity-50 select-none">Run in Simulator</span>
                  {/if}
                  <button type="button"
                    onclick={(e) => { e.stopPropagation(); startEdit(agent); }}
                    class="text-[var(--c-action)] hover:underline">Edit</button>
                </span>
              </div>
            </div>
          {/if}
        {/if}
      </div>
    {/each}
  </div>
{/each}

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  /* ── Agent group grid — 2-col on ≥720px, 1-col on mobile ────────── */
  .agent-group-grid {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.25rem;
  }
  /* Grid children default to `min-width: auto` which resolves to
     `max-content` — so any wide descendant (a long condition leaf,
     a JSON params <pre>, an inline ai-meta-why) pushes the 1fr
     track wider than the viewport. Forcing `min-width: 0` makes
     the track honour `1fr` and lets inner overflow guards
     (word-break, overflow-x) actually engage. */
  .agent-group-grid > * {
    min-width: 0;
    max-width: 100%;
  }
  @media (min-width: 720px) {
    .agent-group-grid {
      grid-template-columns: repeat(2, 1fr);
      gap: 0.5rem;
    }
  }

  /* ── Ask-AI form ─────────────────────────────────────────────────── */
  .ai-pill {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.18rem 0.55rem;
    border-radius: 4px;
    border: 1px solid rgba(167,139,250,0.45);
    background: rgba(167,139,250,0.10);
    color: var(--algo-ai);
    cursor: pointer;
    transition: background 0.1s;
  }
  .ai-pill:hover { background: rgba(167,139,250,0.20); }
  .history-pill {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.18rem 0.55rem;
    border-radius: 4px;
    border: 1px solid rgba(125,211,252,0.45);
    background: rgba(125,211,252,0.08);
    color: #7dd3fc;
    text-decoration: none;
    transition: background 0.1s;
  }
  .history-pill:hover { background: rgba(125,211,252,0.18); }

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
  .ai-card {
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid rgba(167,139,250,0.30);
    border-radius: 5px;
    padding: 0.6rem 0.75rem;
    margin-bottom: 0.6rem;
  }
  .ai-head { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; flex-wrap: wrap; }
  .ai-title {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    color: var(--algo-ai);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .ai-hint { font-size: var(--fs-xs); color: var(--algo-muted); font-family: var(--font-numeric); }
  .ai-prompt {
    width: 100%;
    background: rgba(0,0,0,0.30);
    border: 1px solid rgba(167,139,250,0.20);
    border-radius: 4px;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    padding: 0.4rem 0.55rem;
    resize: vertical;
  }
  .ai-prompt:focus { outline: none; border-color: rgba(167,139,250,0.55); }
  .ai-actions { display: flex; gap: 0.4rem; margin-top: 0.4rem; align-items: center; flex-wrap: wrap; }
  .ai-btn {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.22rem 0.65rem;
    border-radius: 3px;
    border: 1px solid rgba(167,139,250,0.45);
    background: rgba(167,139,250,0.12);
    color: var(--algo-ai);
    cursor: pointer;
  }
  .ai-btn:hover:not(:disabled) { background: rgba(167,139,250,0.22); }
  .ai-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .ai-btn-save {
    border-color: rgba(74,222,128,0.45);
    background: var(--c-long-10);
    color: var(--c-long);
  }
  .ai-btn-save:hover:not(:disabled) { background: rgba(74,222,128,0.20); }
  .ai-slug {
    background: rgba(0,0,0,0.30);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 3px;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    padding: 0.18rem 0.45rem;
    width: 12rem;
  }
  .ai-why {
    margin-top: 0.45rem;
    font-size: var(--fs-md);
    color: var(--algo-slate);
    background: rgba(167,139,250,0.06);
    border-left: 2px solid #a78bfa;
    padding: 0.32rem 0.55rem;
    font-family: var(--font-numeric);
  }
  .ai-warns, .ai-errs { margin: 0.4rem 0 0; padding-left: 0.4rem; list-style: none; }
  .ai-warns li {
    color: var(--c-action);
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    padding: 0.08rem 0;
  }
  .ai-errs li {
    color: var(--c-short);
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    padding: 0.08rem 0;
  }
  .ai-json {
    margin-top: 0.45rem;
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    font-family: var(--font-numeric);
  }
  .ai-json summary { cursor: pointer; }
  .ai-json pre {
    margin-top: 0.3rem;
    background: rgba(0,0,0,0.30);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 3px;
    padding: 0.4rem 0.55rem;
    color: var(--algo-slate);
    overflow: auto;
    max-height: 18rem;
  }

  /* AI-provenance meta box on the expanded agent row.
     Shows the violet "✦ AI" pill, the LLM's why_summary, and a
     foldable prompt details element. Mirrors the .ai-pill palette. */
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
  .ai-meta-why { color: var(--algo-slate); flex: 1 1 8rem; min-width: 0; }
  .ai-meta-prompt { font-size: var(--fs-sm); color: var(--algo-muted); }
  .ai-meta-prompt summary { cursor: pointer; color: #a78bfa; }
  .ai-meta-prompt summary:hover { color: #c4b5fd; }
  .ai-meta-prompt span {
    display: block;
    margin-top: 0.2rem;
    color: var(--algo-slate);
    background: rgba(167,139,250,0.06);
    padding: 0.3rem 0.5rem;
    border-left: 2px solid #a78bfa;
    border-radius: 2px;
  }
  .ai-meta-rest { color: var(--algo-slate)aa; font-style: italic; flex-basis: 100%; }

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
  .preview-desc  { font-style: italic; color: var(--algo-slate)aa; font-size: var(--fs-sm); margin-top: 0.1rem; }
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
    font-size: var(--fs-2xs);
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
    font-size: var(--fs-xs);
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
    font-size: var(--fs-xs);
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
    font-size: var(--fs-xs);
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
    color: var(--algo-slate);
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

  /* ── Tier + topic + digest strip ───────────────────────────────────────
     Single row, two halves. Left half hosts the 4-tier pill row; right
     half hosts the topic input + a compact digest stepper. Built as a
     flex strip so the left collapses to 4 inline pills (saves vertical
     space vs a dropdown) and the right wraps gracefully on narrow
     viewports. */
  .tier-strip {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 1rem;
    margin-top: 0.6rem;
    padding: 0.5rem 0.6rem;
    background: rgba(8, 14, 30, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 3px;
  }
  .tier-strip-left {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.3rem;
    min-width: 0;
  }
  .tier-strip-right {
    display: flex;
    align-items: flex-end;
    gap: 0.6rem;
  }
  .tier-pill-row {
    display: inline-flex;
    gap: 0.2rem;
  }
  .tier-pill {
    font-size: var(--fs-sm);
    padding: 0.22rem 0.55rem;
    border-radius: 999px;
    border: 1px solid rgba(255, 255, 255, 0.15);
    background: transparent;
    color: var(--algo-muted);
    font-family: var(--font-numeric);
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: background-color 0.08s, color 0.08s, border-color 0.08s;
  }
  .tier-pill:hover { color: var(--algo-slate); border-color: rgba(255,255,255,0.3); }
  /* When ON, pill picks its severity colour. Match the algo palette
     (red/orange/amber/grey for crit/high/med/low). */
  .tier-pill-critical.on { background: rgba(248,113,113,0.18); color: var(--c-short); border-color: var(--c-short); }
  .tier-pill-high.on     { background: rgba(251,191,36,0.18);  color: var(--c-action); border-color: var(--c-action); }
  .tier-pill-medium.on   { background: rgba(251,191,36,0.18);  color: var(--c-action); border-color: var(--c-action); }
  .tier-pill-low.on      { background: rgba(125,211,252,0.16); color: #7dd3fc; border-color: #7dd3fc; }

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
    background: rgba(255,255,255,0.05);
    color: var(--algo-slate);
    border: 1px solid rgba(255,255,255,0.12);
  }

  /* Alert-channel checkbox grid — replaces the prior raw-JSON textarea.
     Each row is one channel (telegram / email / websocket / log) with
     a label + short description. Stacked vertically so the description
     wraps cleanly on narrow viewports. */
  .channel-grid {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    padding: 0.4rem 0.5rem;
    background: rgba(8, 14, 30, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 3px;
  }
  .channel-row {
    display: grid;
    grid-template-columns: auto auto 1fr;
    align-items: center;
    gap: 0.5rem;
    font-size: var(--fs-md);
    color: var(--algo-slate);
    cursor: pointer;
  }
  .channel-check {
    accent-color: #fbbf24;
    cursor: pointer;
  }
  .channel-label {
    font-weight: 700;
    color: var(--c-action);
    letter-spacing: 0.02em;
  }
  .channel-desc {
    color: var(--algo-muted);
    font-size: var(--fs-sm);
    line-height: 1.25;
  }

  /* Quick-add action pills next to the Actions textarea. Compact, colour-
     coded by rough semantic group so they don't visually blend together. */
  .action-add-pill {
    font-size: var(--fs-2xs);
    padding: 0.1rem 0.4rem;
    border-radius: 999px;
    border: 1px solid;
    font-family: var(--font-numeric);
    font-weight: 700;
    letter-spacing: 0.02em;
    cursor: pointer;
    white-space: nowrap;
    transition: background-color 0.08s, border-color 0.08s;
  }
  .action-add-close  { background: rgba(248,113,113,0.12); color: var(--c-short); border-color: rgba(248,113,113,0.4); }
  .action-add-close:hover  { background: rgba(248,113,113,0.25); border-color: var(--c-short); }
  .action-add-place  { background: rgba(74,222,128,0.12);  color: var(--c-long); border-color: rgba(74,222,128,0.4); }
  .action-add-place:hover  { background: rgba(74,222,128,0.25); border-color: var(--c-long); }
  /* `.action-add-place-tpl` removed in audit pass 6 — the
     +place_order pill now ships with template_slug="default-bull" in
     the skeleton so a separate "templated" variant was redundant. */
  .action-add-chase  { background: rgba(251,191,36,0.12);  color: var(--c-action); border-color: rgba(251,191,36,0.4); }
  .action-add-chase:hover  { background: rgba(251,191,36,0.25); border-color: var(--c-action); }
  .action-add-cancel { background: rgba(148,163,184,0.12); color: var(--algo-slate); border-color: rgba(148,163,184,0.35); }
  .action-add-cancel:hover { background: rgba(148,163,184,0.25); border-color: #94a3b8; }
  .action-add-log    { background: rgba(125,211,252,0.12); color: #7dd3fc; border-color: rgba(125,211,252,0.4); }
  .action-add-log:hover    { background: rgba(125,211,252,0.25); border-color: #7dd3fc; }

</style>

<ActivityLogSurface
  context="page"
  heightClass="h-[50vh]"
  defaultTab={logTab}
  simScope={simActive}
  multiColumn={true}
  hideInlineAccountFilter={false}
  onTabChange={(id) => { logTab = id; }}
/>
