<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { logTime, lifespanChip, visibleInterval } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import StaleBanner from '$lib/StaleBanner.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import {
    fetchAgents, activateAgent, deactivateAgent, updateAgent, createAgent,
    fetchSimStatus,
    startSimForAgent, aiDraftAgent, fetchGrammarTokens,
    fetchAgentRenderers, fetchSetting,
  } from '$lib/api';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import Select   from '$lib/Select.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import DisclosureChevron from '$lib/DisclosureChevron.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import ChaseAggPicker from '$lib/order/ChaseAggPicker.svelte';
  import { loadOrderTemplates, orderTemplatesStore } from '$lib/data/templates';

  let agents      = $state([]);
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
    tier: string, topic: string,
    trade_mode: string, debounce_minutes: number,
    tags: string, blackout_windows: string,
  }} */ ({
    name: '', long_name: '', description: '',
    conditions: '{}', events: '[]', actions: '[]',
    cooldown_minutes: 30, scope: 'total', schedule: 'market_hours',
    fire_at_time: '',
    lifespan_type: 'persistent', lifespan_max_fires: '', lifespan_expires_at: '',
    tier: 'medium', topic: 'general',
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
      // Priority / topic — tier drives topic-scoped suppression in run_cycle.
      tier:                 agent.tier  || 'medium',
      topic:                agent.topic || 'general',
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
  // THRESHOLD-agent channel set only — do NOT reuse for event agents,
  // which use a different, backend-enforced set (EVENT_CHANNELS below).
  const ALERT_CHANNELS = [
    { id: 'telegram',  label: 'Telegram',  desc: 'Push to the ops Telegram group' },
    { id: 'email',     label: 'Email',     desc: 'SMTP to alert recipients' },
    { id: 'websocket', label: 'WebSocket', desc: 'Live UI toast / chart overlay' },
    { id: 'log',       label: 'Log',       desc: 'Server log file only (no push)' },
  ];

  // EVENT-agent channel set (matches backend/api/algo/event_agents.py:CHANNELS).
  // Deliberately separate from ALERT_CHANNELS — event agents (kind='event')
  // are dispatched by a different pipeline with a different channel list
  // (ntfy + telegram_info instead of websocket/log).
  const EVENT_CHANNELS = [
    { id: 'ntfy',           label: 'ntfy',           desc: 'Push via ntfy.sh (supports priority)' },
    { id: 'telegram',       label: 'Telegram',        desc: 'Push to the ops Telegram group' },
    { id: 'telegram_info',  label: 'Telegram (info)', desc: 'Low-noise info channel' },
    { id: 'email',          label: 'Email',            desc: 'SMTP to alert recipients' },
  ];
  // event_agents.py seed specs use CRITICAL (chase-cancel, partial-gtt);
  // the threshold editor's LOG_LEVELS below deliberately stays unchanged.
  const EVENT_LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

  // ── "+ New Agent" entry point ──────────────────────────────────────
  // Independent of the Ask-AI flow above. null = closed, 'pick' = kind
  // selector shown, 'threshold' = reuses the inline editor (editForm,
  // editing='__new__'), 'event' = the dedicated event-agent builder.
  let creatingKind  = $state(/** @type {'pick'|'threshold'|'event'|null} */ (null));
  let newAgentSlug  = $state('');

  function toggleNewAgentPanel() {
    if (creatingKind) {
      creatingKind = null;
      if (editing === '__new__') editing = null;
    } else {
      creatingKind = 'pick';
    }
  }

  /** @type {{key:string, label:string, description?:string}[]} */
  let renderers      = $state([]);
  let renderersError = $state('');
  async function loadRenderers() {
    try {
      const rows = await fetchAgentRenderers();
      renderers = Array.isArray(rows) ? rows : [];
      renderersError = '';
    } catch (e) {
      // Never clobber a previously-successful list with [] on a
      // transient failure (templates.js cache-poisoning rule).
      renderersError = e.message || 'Renderers unavailable';
    }
  }

  // Global default trade mode — informational "Global: X" display next
  // to the per-agent trade_mode override (which already exists as the
  // editable field; no separate override mechanism is added here).
  let globalDefaultTradeMode = $state('paper');
  async function loadGlobalDefaultTradeMode() {
    try {
      const s = await fetchSetting('execution.default_agent_trade_mode');
      globalDefaultTradeMode = s?.value || 'paper';
    } catch (_) { /* keep last-known value */ }
  }

  /** @type {{slug:string, name:string, description:string, renderer:string,
   *          tag:string, minLevel:string, whereKey:string, whereValue:string,
   *          channels: Record<string,boolean>, ntfyPriority:string,
   *          gateBypass: Record<string,boolean>}} */
  let eventCreateForm = $state({
    slug: '', name: '', description: '', renderer: '',
    tag: '', minLevel: 'INFO', whereKey: '', whereValue: '',
    channels:   { ntfy: false, telegram: false, telegram_info: false, email: false },
    ntfyPriority: 'normal',
    gateBypass: { ntfy: false, telegram: false, telegram_info: false, email: false },
  });
  let eventCreateErrors = $state(/** @type {string[]} */ ([]));
  let eventCreateBusy   = $state(false);

  /** Reset state and open either the threshold editor (blank editForm)
   *  or the event-agent builder (blank eventCreateForm). */
  function startCreate(/** @type {'threshold'|'event'} */ kind) {
    creatingKind = kind;
    validationErrors = []; validationGrammar = '';
    if (kind === 'threshold') {
      editing = '__new__';
      expandedSlug = null;
      newAgentSlug = '';
      editForm = {
        name: '', long_name: '', description: '',
        conditions: '{}', events: '[]', actions: '[]',
        cooldown_minutes: 30, scope: 'total', schedule: 'market_hours',
        fire_at_time: '',
        lifespan_type: 'persistent', lifespan_max_fires: '', lifespan_expires_at: '',
        tier: 'medium', topic: 'general',
        trade_mode: 'paper', debounce_minutes: 0,
        tags: '', blackout_windows: '[]',
      };
    } else {
      editing = null;
      eventCreateErrors = [];
      eventCreateForm = {
        slug: '', name: '', description: '', renderer: '',
        tag: '', minLevel: 'INFO', whereKey: '', whereValue: '',
        channels:   { ntfy: false, telegram: false, telegram_info: false, email: false },
        ntfyPriority: 'normal',
        gateBypass: { ntfy: false, telegram: false, telegram_info: false, email: false },
      };
      if (!renderers.length) loadRenderers();
    }
  }

  /** Build the create payload for a new event agent and POST it.
   *  Full backend validation errors surface inline (eventCreateErrors). */
  async function saveEventCreate() {
    eventCreateErrors = [];
    const f = eventCreateForm;
    const slug = (f.slug.trim() || f.name.trim())
      .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    if (!slug)            { eventCreateErrors = ['Slug or name is required']; return; }
    if (!f.renderer)      { eventCreateErrors = ['Pick a renderer']; return; }
    if (!f.tag.trim())    { eventCreateErrors = ['Log tag is required']; return; }
    const enabledIds = EVENT_CHANNELS.filter((ch) => f.channels[ch.id]).map((ch) => ch.id);
    if (!enabledIds.length) { eventCreateErrors = ['Pick at least one channel']; return; }
    const events = enabledIds.map((id) => {
      const row = /** @type {any} */ ({ channel: id, enabled: true });
      if (id === 'ntfy' && f.ntfyPriority !== 'normal') row.priority = f.ntfyPriority;
      if (f.gateBypass[id]) row.gate = false;
      return row;
    });
    const conditions = {
      log: {
        tag: f.tag.trim(),
        min_level: f.minLevel,
        ...(f.whereKey.trim() ? { where: { [f.whereKey.trim()]: f.whereValue } } : {}),
      },
    };
    eventCreateBusy = true;
    try {
      await createAgent({
        slug, name: f.name.trim() || slug,
        description: f.description || '',
        kind: 'event',
        conditions, events,
        actions: [{ type: 'render', render: f.renderer }],
      });
      toast.success(`Agent created: ${slug}`);
      creatingKind = null;
      await loadAgents();
    } catch (e) {
      toast.error(`Create failed: ${e.message}`);
      eventCreateErrors = [e.fullMessage || e.message];
    } finally { eventCreateBusy = false; }
  }

  /** Read-only renderer key for an existing event agent (actions[0].render). */
  function eventRendererKey(/** @type {any} */ agent) {
    return (agent?.actions || []).find((a) => a?.type === 'render')?.render || null;
  }
  function rendererLabel(/** @type {string|null} */ key) {
    if (!key) return '—';
    return renderers.find((r) => r.key === key)?.label || key;
  }

  /** Per-channel priority (ntfy-only in practice) — read/write editForm.events. */
  function channelPriority(/** @type {string} */ channelId) {
    const list = parsedEvents.ok ? (parsedEvents.value || []) : [];
    return list.find((e) => e?.channel === channelId)?.priority || 'normal';
  }
  function setChannelPriority(/** @type {string} */ channelId, /** @type {string} */ priority) {
    let list = [];
    try { list = JSON.parse(editForm.events || '[]'); } catch { list = []; }
    if (!Array.isArray(list)) list = [];
    const idx = list.findIndex((e) => e?.channel === channelId);
    if (idx < 0) return;
    const row = { ...list[idx] };
    if (priority === 'normal') delete row.priority; else row.priority = priority;
    list[idx] = row;
    editForm.events = JSON.stringify(list, null, 2);
  }
  /** Per-channel capability gate (default true = respect capability flag). */
  function channelGate(/** @type {string} */ channelId) {
    const list = parsedEvents.ok ? (parsedEvents.value || []) : [];
    const row = list.find((e) => e?.channel === channelId);
    return row?.gate !== undefined ? row.gate : true;
  }
  function setChannelGate(/** @type {string} */ channelId, /** @type {boolean} */ gate) {
    let list = [];
    try { list = JSON.parse(editForm.events || '[]'); } catch { list = []; }
    if (!Array.isArray(list)) list = [];
    const idx = list.findIndex((e) => e?.channel === channelId);
    if (idx < 0) return;
    const row = { ...list[idx] };
    if (gate === true) delete row.gate; else row.gate = gate;
    list[idx] = row;
    editForm.events = JSON.stringify(list, null, 2);
  }

  /** Minimal save path for an existing event agent — channels (incl.
   *  per-channel priority/gate) only. Renderer / condition / slug are
   *  fixed at creation and never sent. Does NOT go through
   *  runValidation() (that posts conditions to the grammar validator,
   *  irrelevant here since conditions never change). */
  async function saveEventEdit(/** @type {any} */ agent) {
    let events;
    try { events = JSON.parse(editForm.events || '[]'); }
    catch (e) { validationErrors = [`events JSON invalid: ${e.message}`]; return; }
    try {
      await updateAgent(editing, { events });
      editing = null; validationErrors = [];
      toast.success(`Agent saved: ${agent.name}`);
      await loadAgents();
    } catch (e) {
      toast.error(`Save failed: ${e.message}`);
      validationErrors = [e.fullMessage || e.message];
    }
  }

  // ── Log tags and log matches ───────────────────────────────────────
  // Tags come from the grammar registry (grammar_kind 'log'). A channel row may carry
  // a tags array; the backend sends the channel only when a matched record carries one.
  let logTags = $state([]);
  let logTagPick = $state('');
  let logMinLevel = $state('INFO');
  const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR'];

  async function loadLogTags() {
    try {
      const rows = await fetchGrammarTokens('log');
      logTags = (rows || [])
        .filter((r) => r.token_kind === 'tag' && r.is_active !== false)
        .map((r) => r.token);
    } catch {
      logTags = [];
    }
  }

  function channelTags(/** @type {string} */ channelId) {
    const list = parsedEvents.ok ? (parsedEvents.value || []) : [];
    const row = list.find((e) => e?.channel === channelId);
    return Array.isArray(row?.tags) ? row.tags : [];
  }

  /** Add or remove one tag on an enabled channel row. Re-serializes editForm.events. */
  function toggleChannelTag(/** @type {string} */ channelId, /** @type {string} */ tag) {
    let list = [];
    try { list = JSON.parse(editForm.events || '[]'); } catch { list = []; }
    if (!Array.isArray(list)) list = [];
    const idx = list.findIndex((e) => e?.channel === channelId && e?.enabled);
    if (idx < 0) return;
    const current = Array.isArray(list[idx].tags) ? list[idx].tags : [];
    const next = current.includes(tag) ? current.filter((t) => t !== tag) : [...current, tag];
    const row = { ...list[idx], tags: next };
    if (next.length === 0) delete row.tags;
    list[idx] = row;
    editForm.events = JSON.stringify(list, null, 2);
  }

  /** Append a log leaf to the conditions tree (AND-ed with any existing condition). */
  function addLogMatch() {
    if (!logTagPick) return;
    const leaf = { log: { tag: logTagPick, min_level: logMinLevel } };
    let cond = {};
    try { cond = JSON.parse(editForm.conditions || '{}'); } catch { cond = {}; }
    const empty = !cond || Object.keys(cond).length === 0;
    editForm.conditions = JSON.stringify(empty ? leaf : { all: [cond, leaf] }, null, 2);
  }

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

  // ── Structured place_order controls ───────────────────────────────────
  // Convenience mini-form over the raw Actions JSON textarea: shown only
  // when the textarea currently parses to exactly ONE place_order action.
  // Two-way synced with the textarea (the textarea stays the single
  // source of truth) — reading `parsedActions` here means a manual
  // textarea edit re-parses and the mini-form reflects it immediately;
  // a control edit re-serializes the whole action back into
  // editForm.actions via _updatePlaceOrderParam below.
  const _singlePlaceOrderAction = $derived.by(() => {
    if (!parsedActions.ok) return null;
    const arr = parsedActions.value;
    if (!Array.isArray(arr) || arr.length !== 1) return null;
    const a = arr[0];
    if (!a || a.type !== 'place_order') return null;
    return a;
  });

  /** Set (or delete, when value===undefined) one params.<key> on the
   *  single place_order action, then re-serialize into editForm.actions.
   *  Re-parses fresh from the textarea (not the `_singlePlaceOrderAction`
   *  snapshot) so this never clobbers a manual edit made a moment ago. */
  function _updatePlaceOrderParam(/** @type {string} */ key, /** @type {any} */ value) {
    let arr;
    try { arr = JSON.parse(editForm.actions || '[]'); }
    catch (_) { return; }
    if (!Array.isArray(arr) || arr.length !== 1 || arr[0]?.type !== 'place_order') return;
    const params = { ...(arr[0].params || {}) };
    if (value === undefined) delete params[key];
    else params[key] = value;
    arr[0] = { ...arr[0], params };
    editForm.actions = JSON.stringify(arr, null, 2);
  }

  // Template catalog for the structured dropdown — same module-level
  // cache + store every other surface (TemplateBar, OrderTicket,
  // /automation/templates) reads; loadOrderTemplates() below is a
  // cheap no-op if already warm.
  let _templateRows = $state(/** @type {any[]} */ ([]));
  $effect(() => {
    const rows = $orderTemplatesStore;
    if (rows) _templateRows = rows.filter((t) => t.is_active);
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
    const isCreate = editing === '__new__';
    if (isCreate && !newAgentSlug.trim()) {
      validationErrors = ['Slug is required'];
      return;
    }
    try {
      if (isCreate) {
        await createAgent({
          slug: newAgentSlug.trim(),
          kind: 'threshold',
          ..._buildEditPayload(tagsList, bwResult.value),
        });
        toast.success(`Agent created: ${editForm.name}`);
        creatingKind = null;
      } else {
        await updateAgent(editing, _buildEditPayload(tagsList, bwResult.value));
        toast.success(`Agent saved: ${editForm.name}`);
      }
      editing = null;
      validationErrors = []; validationGrammar = '';
      await loadAgents();
    } catch (e) {
      toast.error(`Save failed: ${e.message}`);
      validationErrors = [e.fullMessage || e.message];
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
  // Classify by consequence ("can this agent act on its own") using real
  // AgentInfo fields (actions / conditions / fire_at_time / is_system)
  // instead of slug-text guessing. Motivating bug: expiry-day-positions-alert
  // is notify-only (no broker-verb action) and must land in Risk Alerts, not
  // Automated Actions, even though its slug contains "expiry".
  //
  // Action types that place/modify/cancel/close a real broker order — every
  // type EXCEPT the pure-notification ones (monitor_order, deactivate_agent,
  // set_flag, emit_log). Confirmed exhaustive against the full action_type
  // catalog in backend/config/grammars/agent_grammar.yaml (11 tokens total:
  // these 7 + those 4 notify-only ones).
  const AUTOMATED_ACTION_TYPES = new Set([
    'place_order', 'modify_order', 'cancel_order', 'cancel_all_orders',
    'close_position', 'chase_close_positions', 'expiry_auto_close',
  ]);

  // Built-in schedule-only pings (market-open-nse, market-preclose-mcx) hand-
  // author an "always-true" sentinel condition (e.g. avail_margin >= -999999999)
  // to gate a fire_at_time schedule rather than carrying a real condition —
  // same backend convention agent_engine.py's _v2_format_threshold guards
  // against (abs(threshold) >= 1e8 renders as "n/a"). `conditions` is `null`
  // only for the excluded 'manual' pseudo-agent; defensively also treat an
  // empty `{}` as schedule-only, rather than assuming `null` per the
  // AgentInfo schema (`conditions: dict`, not `dict | None`).
  function _isScheduleOnlySentinel(/** @type {any} */ conditions) {
    if (!conditions) return true;
    if (typeof conditions === 'object' && Object.keys(conditions).length === 0) return true;
    const v = conditions.value;
    return typeof v === 'number' && Math.abs(v) >= 1e8;
  }

  function categoryFor(/** @type {any} */ agent) {
    if (!agent?.slug) return 'Custom';
    if (agent.slug === 'manual') return null;
    if (Array.isArray(agent.actions) && agent.actions.some(a => AUTOMATED_ACTION_TYPES.has(a?.type))) {
      return 'Automated Actions';
    }
    if (_isScheduleOnlySentinel(agent.conditions) && agent.fire_at_time) {
      return 'Scheduled Info';
    }
    if (agent.is_system) return 'Risk Alerts';
    return 'Custom';
  }

  const CATEGORY_ORDER = ['Automated Actions', 'Risk Alerts', 'Scheduled Info', 'Custom'];

  function groupedAgents() {
    const out = {};
    for (const a of agents) {
      const cat = categoryFor(a);
      if (!cat) continue;
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
    loadLogTags();
    loadGlobalDefaultTradeMode();
    connectWS();
    pollSimStatus();
    refreshTeardown   = visibleInterval(loadAll, 30000);
    simStatusTeardown = visibleInterval(pollSimStatus, 4000);
    // Warm the template catalog for the place_order structured controls'
    // Template dropdown — idempotent, module-level cached.
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
  <!-- Ask AI toggle is LEFT-aligned per canonical header rule (only
       Refresh + Order + Chart + Activity + Collapse + Fullscreen +
       Default-size icons sit RIGHT of ml-auto). -->
  <button class="ai-pill" onclick={() => aiOpen = !aiOpen}>
    {aiOpen ? '× Close AI' : '✦ Ask AI'}
  </button>
  <!-- "+ New Agent" — independent of Ask-AI. Kind selector (Threshold
       reuses the inline editor form; Notification opens the dedicated
       event-agent builder). -->
  <button class="ai-pill new-agent-pill" onclick={toggleNewAgentPanel}>
    {creatingKind ? '× Close' : '+ New Agent'}
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

{#if creatingKind}
  <!-- "+ New Agent" panel. 'pick' = kind selector only; 'threshold' reuses
       thresholdEditorBody(null) below (the exact same inline-editor form
       threshold agents already use, seeded blank); 'event' renders the
       dedicated event-agent builder (eventCreateBuilder snippet). -->
  <div class="ai-card new-agent-card">
    {#if creatingKind === 'pick'}
      <div class="ai-head">
        <span class="ai-title" style="color: var(--c-long)">+ New Agent</span>
        <span class="ai-hint">Pick a kind</span>
      </div>
      <div class="flex gap-2 mt-2">
        <button type="button" class="btn-primary text-[length:var(--fs-md)] py-1 px-3"
          onclick={() => startCreate('threshold')}>Threshold</button>
        <button type="button" class="btn-primary text-[length:var(--fs-md)] py-1 px-3"
          onclick={() => startCreate('event')}>Notification</button>
      </div>
      <p class="text-[length:var(--fs-xs)] opacity-60 mt-2">
        Threshold — fires when a metric (P&amp;L, margin, etc.) crosses a value.
        Notification — fires on a matching log event (renderer-driven, no metric).
      </p>
    {:else if creatingKind === 'threshold'}
      <div class="ai-head">
        <span class="ai-title" style="color: var(--c-long)">New Threshold Agent</span>
        <span class="ai-hint">Lands using the same editor as an existing agent — review before activating.</span>
      </div>
      <div class="mt-2 mb-1 max-w-xs">
        <span class="field-label">Slug</span>
        <input class="field-input" bind:value={newAgentSlug} placeholder="my-new-agent" />
      </div>
      {@render thresholdEditorBody(null)}
    {:else if creatingKind === 'event'}
      {@render eventCreateBuilder()}
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

<!-- Threshold-agent editor body — used BOTH to edit an existing
     threshold-kind agent (agent = the real row, editing=agent.slug) and
     to create a brand-new one (agent = null, editing='__new__'). Every
     field is always editable here — event-kind agents never reach this
     snippet (they render eventEditorBody instead), so no fieldset-level
     disable is needed. -->
{#snippet thresholdEditorBody(/** @type {any} */ agent)}
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <span class="field-label">Name</span>
                  <input bind:value={editForm.name} class="field-input" />
                </div>
                <div>
                  <span class="field-label">
                    Long name
                    <InfoHint popup panel title="Long name" text="Operator-readable 3-part label: <b>when:&lt;condition&gt;</b> &mdash; <b>alert:&lt;notify&gt;</b> &mdash; <b>do:&lt;action&gt;</b>. Surfaces under the short name on the agents row so an operator scanning the list sees what each agent actually does without expanding." />
                  </span>
                  <input bind:value={editForm.long_name}
                         placeholder="when:positions.total.pnl<=-50k   alert:critical/tg+email   do:notify-only"
                         class="field-input font-mono text-[length:var(--fs-sm)]" />
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
                    <InfoHint popup panel title="Debounce (minutes)" text="Fire only when the condition holds for N consecutive evaluations spanning at least N minutes. <b>0</b> = fire immediately on first true tick. Use to suppress single-tick spikes (e.g. a Kite glitch dropping pnl_pct to -2.1% for one cycle). Industry analogue: Datadog/Grafana <b>For:</b>, CloudWatch <b>EvaluationPeriods</b>." />
                  </span>
                  <input type="number" min="0"
                         bind:value={editForm.debounce_minutes}
                         class="field-input" />
                </div>
                <div>
                  <span class="field-label">
                    Trade mode
                    <InfoHint popup panel title="Trade mode" text="Per-agent execution mode. <b>paper</b> = simulated fills against real bid/ask (default). <b>live</b> = real broker orders. Resolved against the engine's master <code>execution.paper_trading_mode</code> setting + the branch gate — dev always forces paper regardless." />
                  </span>
                  <Select ariaLabel="Trade mode" bind:value={editForm.trade_mode}
                    options={[
                      { value: 'paper', label: 'Paper (simulated)' },
                      { value: 'live',  label: 'Live (real broker)' },
                    ]} />
                  <div class="text-[length:var(--fs-xs)] text-[var(--c-muted)] mt-1">
                    Global default: <b>{globalDefaultTradeMode.toUpperCase()}</b>
                    {#if editForm.trade_mode !== globalDefaultTradeMode}
                      <span class="text-[var(--c-action)]">(overridden)</span>
                    {:else}
                      <span class="opacity-70">(inherited)</span>
                    {/if}
                  </div>
                </div>
                <div>
                  <span class="field-label">
                    Fire at (IST)
                    <InfoHint popup panel title="Fire at (IST)" text="Optional <b>HH:MM IST</b> time-of-day gate. When set, agent only evaluates inside a small window around this wall-clock time (covers one background poll cycle ~ 6 min). Empty = no gate, evaluates every tick. Use for daily summaries, EOD scans, expiry-day close orders." />
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
                  {#if agent && lifespanChip(agent)}
                    {@const _ls = lifespanChip(agent)}
                    <div class="text-[length:var(--fs-xs)] text-[var(--c-muted)] mt-1" title={_ls.tooltip}>
                      Current: <span class={'lifespan-chip lifespan-chip-' + _ls.color}>{_ls.label}</span>
                    </div>
                  {:else if !agent || agent.lifespan_type === 'persistent'}
                    <div class="text-[length:var(--fs-xs)] text-[var(--c-muted)] mt-1 italic">
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

              <!-- ── Alert hierarchy strip (tier + topic) ─────
                   Single tight row. Tier as a 4-pill segmented control
                   so all severity options are visible without a dropdown
                   click. Topic as a small text input with datalist
                   autocomplete (writes the existing topics back, so
                   ops can group new agents alongside the loss-* ones
                   in a single click). -->
              <div class="tier-strip">
                <div class="tier-strip-left">
                  <span class="field-label" style="margin-right: 0.5rem; display: inline-flex; align-items: center; gap: 0.25rem;">
                    Priority
                    <InfoHint popup panel title="Priority" text="Severity bucket (a.k.a. <b>tier</b>) — <b>critical &gt; high &gt; medium &gt; low</b>. Drives topic-scoped suppression: when multiple agents in the same topic fire on one tick, only the highest priority dispatches; the others are logged as suppressed. Industry analogue: PagerDuty <b>Urgency</b>, Opsgenie <b>Priority P1-P5</b>, Datadog <b>monitor priority</b>." />
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
                    <InfoHint popup panel title="Tags" text="Free-form labels for filtering. Comma-separated. Examples: <b>iron-condor, nifty, review-q3</b>. Surfaces on the agents list as chips. Industry analogue: Datadog tags, Grafana labels." />
                  </span>
                  <input bind:value={editForm.tags}
                         placeholder="iron-condor, nifty, review-q3"
                         class="field-input" />
                </div>
                <div>
                  <span class="field-label">
                    Blackout windows (JSON)
                    <InfoHint popup panel title="Blackout windows (JSON)" text="List of <b>&#123;start: 'HH:MM', end: 'HH:MM'&#125;</b> entries in IST. Agent is skipped while wall-clock IST is inside any window. Crossing-midnight windows like <code>&#123;start:'23:00',end:'01:00'&#125;</code> are supported. Industry analogue: PagerDuty maintenance windows, Grafana silences, Datadog <b>mute_until</b>." />
                  </span>
                  <textarea bind:value={editForm.blackout_windows}
                            class="field-input font-mono text-[length:var(--fs-sm)]" rows="3"
                            placeholder={'[{"start":"12:00","end":"13:00"}]'}></textarea>
                </div>
              </div>

              <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3">
                <div>
                  <span class="field-label">Conditions (JSON)</span>
                  <textarea bind:value={editForm.conditions} class="field-input font-mono text-[length:var(--fs-sm)]" rows="5"></textarea>
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
                      {#if isChannelEnabled(ch.id) && logTags.length}
                        <div class="channel-tags" aria-label="{ch.label} tag filter">
                          {#each logTags as t}
                            <button type="button"
                                    class="tag-chip"
                                    class:on={channelTags(ch.id).includes(t)}
                                    aria-pressed={channelTags(ch.id).includes(t)}
                                    onclick={() => toggleChannelTag(ch.id, t)}>{t}</button>
                          {/each}
                        </div>
                      {/if}
                    {/each}
                  </div>
                  <div class="log-match-row">
                    <span class="field-label">Log match</span>
                    <select class="log-match-select" bind:value={logTagPick} aria-label="Log tag">
                      <option value="">Tag…</option>
                      {#each logTags as t}<option value={t}>{t}</option>{/each}
                    </select>
                    <select class="log-match-select" bind:value={logMinLevel} aria-label="Minimum level">
                      {#each LOG_LEVELS as lv}<option value={lv}>{lv}</option>{/each}
                    </select>
                    <button type="button" class="log-match-add" onclick={addLogMatch} disabled={!logTagPick}>
                      Add to conditions
                    </button>
                  </div>
                </div>
                <div>
                  <div class="flex items-center justify-between flex-wrap gap-1">
                    <span class="field-label">
                      Actions (JSON)
                      <InfoHint popup panel title="Actions (JSON)" text="The single <b>+ place_order</b> pill appends an entry + <code>template_slug=&quot;default-bull&quot;</code> skeleton (change the slug to <code>default-short-vol</code> for a SELL-side credit spread, or <code>none</code> to opt out of auto-attachments). Whenever this JSON parses to exactly ONE <code>place_order</code> action, a structured mini-form appears below the textarea — <b>Product</b> (NRML/MIS), <b>Chase</b> aggressiveness (L/M/H, maps to <code>chase_aggressiveness</code> — the same pacing a manual order ticket's chase uses; default <b>med</b> when unset), and <b>Bracket</b> (keyed by slug from the catalog, 'None' clears <code>template_slug</code>). Edits there re-serialize back into this textarea; editing the JSON directly updates the mini-form the same way. The Bracket runs on fill — sim path goes through SimGttBook; live path through broker GTT. Catalog at <a href='/automation/templates' target='_blank'>Brackets</a>." />
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
                  <textarea bind:value={editForm.actions} data-testid="actions-json-textarea" class="field-input font-mono text-[length:var(--fs-sm)]" rows="5"></textarea>
                  {#if _singlePlaceOrderAction}
                    <div class="place-order-struct" data-testid="place-order-struct">
                      <div class="pos-field">
                        <span class="pos-label">Product</span>
                        <Select ariaLabel="Product"
                          value={_singlePlaceOrderAction.params?.product || 'NRML'}
                          options={[
                            { value: 'NRML', label: 'NRML' },
                            { value: 'MIS',  label: 'MIS' },
                          ]}
                          onValueChange={(v) => _updatePlaceOrderParam('product', v)} />
                      </div>
                      <div class="pos-field">
                        <span class="pos-label">Chase</span>
                        <ChaseAggPicker
                          value={_singlePlaceOrderAction.params?.chase_aggressiveness || 'med'}
                          onChange={(v) => _updatePlaceOrderParam('chase_aggressiveness', v)} />
                      </div>
                      <div class="pos-field">
                        <span class="pos-label">Bracket</span>
                        <Select ariaLabel="Bracket"
                          value={_singlePlaceOrderAction.params?.template_slug ?? 'none'}
                          options={[
                            { value: 'none', label: 'None' },
                            ..._templateRows.map((t) => ({ value: t.slug, label: t.name || t.slug })),
                          ]}
                          onValueChange={(v) => _updatePlaceOrderParam('template_slug', v === 'none' ? undefined : v)} />
                      </div>
                    </div>
                  {/if}
                </div>
              </div>

              {#if validationErrors.length}
                <div class="mt-3 p-2 rounded bg-red-500/15 text-[var(--algo-red-text-bright)] text-[length:var(--fs-sm)] border border-red-500/40">
                  <div class="font-semibold mb-1">Condition validation failed:</div>
                  <ul class="list-disc ml-4">{#each validationErrors as err}<li>{err}</li>{/each}</ul>
                </div>
              {:else if validationGrammar}
                <div class="mt-3 p-2 rounded bg-[var(--algo-green-bg)] text-[var(--algo-green-text)] text-[length:var(--fs-sm)] border border-[var(--algo-green-border-soft)]">
                  Validated — ready to save.
                </div>
              {/if}

              <div class="flex gap-2 mt-3">
                <button type="button" onclick={async () => { await runValidation(); }}
                  class="text-[length:var(--fs-md)] py-1 px-3 rounded border border-[#7dd3fc]/50 bg-[#7dd3fc]/15 text-[#7dd3fc] hover:bg-[#7dd3fc]/25 font-semibold disabled:opacity-40">
                  Validate
                </button>
                <button type="button" onclick={saveEdit}
                  class="btn-primary text-[length:var(--fs-md)] py-1 px-4 disabled:opacity-40">{agent ? 'Save' : 'Create'}</button>
                <button type="button" onclick={() => { editing = null; creatingKind = null; validationErrors = []; validationGrammar = ''; }}
                  class="btn-secondary text-[length:var(--fs-md)] py-1 px-4">Cancel</button>
              </div>

              <!-- ── LIVE TREE PREVIEW (below the form) ── -->
              <div class="agent-preview mt-4 pt-3 border-t" style="border-top-color: rgba(126,151,184,0.10)">
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
{/snippet}

<!-- Event-agent editor body (existing event-kind agents only). Renderer,
     condition, and slug are FIXED at creation — shown read-only. Channel
     enabled/priority/capability-gate stay editable per plan item 3. -->
{#snippet eventEditorBody(/** @type {any} */ agent)}
              <div class="mb-3 p-2 rounded bg-[#7dd3fc]/10 text-[#7dd3fc] text-[length:var(--fs-sm)] border border-[#7dd3fc]/30">
                System event agent — renderer, condition, and slug are fixed at creation.
                Channels, priority, and the capability gate stay editable below.
                Use Activate / Deactivate to change whether it fires.
              </div>
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mb-3">
                <div>
                  <span class="field-label">Slug <span class="opacity-50">(fixed)</span></span>
                  <input class="field-input" value={agent.slug} disabled />
                </div>
                <div>
                  <span class="field-label">Renderer <span class="opacity-50">(fixed)</span></span>
                  <input class="field-input" value={rendererLabel(eventRendererKey(agent))} disabled />
                </div>
              </div>
              <div class="mb-3">
                <span class="field-label">Condition <span class="opacity-50">(fixed)</span></span>
                <div class="preview-tree">{@render renderCondNode(agent.conditions)}</div>
              </div>
              <div class="mb-1">
                <span class="field-label">Alert channels</span>
                <div class="channel-grid">
                  {#each EVENT_CHANNELS as ch}
                    <label class="channel-row">
                      <input type="checkbox"
                             class="channel-check"
                             checked={isChannelEnabled(ch.id)}
                             onchange={(e) => toggleChannel(ch.id, /** @type {HTMLInputElement} */(e.target).checked)} />
                      <span class="channel-label">{ch.label}</span>
                      <span class="channel-desc">{ch.desc}</span>
                    </label>
                    {#if isChannelEnabled(ch.id)}
                      <div class="channel-tags" style="padding-left: 1.4rem; display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap;">
                        {#if ch.id === 'ntfy'}
                          <span class="text-[length:var(--fs-xs)] opacity-60">Priority</span>
                          <Select ariaLabel="Priority" value={channelPriority(ch.id)}
                            onValueChange={(v) => setChannelPriority(ch.id, String(v))}
                            options={[
                              { value: 'normal', label: 'normal' },
                              { value: 'high',   label: 'high' },
                              { value: 'urgent', label: 'urgent' },
                            ]} />
                        {/if}
                        <label class="flex items-center gap-1 text-[length:var(--fs-xs)] opacity-70 cursor-pointer">
                          <input type="checkbox" checked={channelGate(ch.id) === false}
                            onchange={(e) => setChannelGate(ch.id, /** @type {HTMLInputElement} */(e.target).checked ? false : true)} />
                          Always send (bypass capability gate)
                        </label>
                      </div>
                    {/if}
                  {/each}
                </div>
              </div>
              {#if validationErrors.length}
                <div class="mt-2 p-2 rounded bg-red-500/15 text-[var(--algo-red-text-bright)] text-[length:var(--fs-sm)] border border-red-500/40">
                  <ul class="list-disc ml-4">{#each validationErrors as err}<li>{err}</li>{/each}</ul>
                </div>
              {/if}
              <div class="flex gap-2 mt-3">
                <button type="button" onclick={() => saveEventEdit(agent)}
                  class="btn-primary text-[length:var(--fs-md)] py-1 px-4">Save</button>
                <button type="button" onclick={() => { editing = null; validationErrors = []; }}
                  class="btn-secondary text-[length:var(--fs-md)] py-1 px-4">Cancel</button>
              </div>
{/snippet}

<!-- New event-agent builder (kind='event' creation). Renderer fetched
     live from GET /api/agents/renderers — never hardcoded. -->
{#snippet eventCreateBuilder()}
  <div class="ai-head">
    <span class="ai-title" style="color: var(--c-long)">New Notification Agent</span>
    <span class="ai-hint">Fires on a matching log event, not a threshold tick.</span>
  </div>
  <div class="grid grid-cols-1 md:grid-cols-2 gap-3 mt-2">
    <div>
      <span class="field-label">Name</span>
      <input class="field-input" bind:value={eventCreateForm.name} placeholder="My alert" />
    </div>
    <div>
      <span class="field-label">Slug <span class="opacity-50">(auto from name if blank)</span></span>
      <input class="field-input" bind:value={eventCreateForm.slug} placeholder="my-alert" />
    </div>
    <div class="md:col-span-2">
      <span class="field-label">Description</span>
      <input class="field-input" bind:value={eventCreateForm.description} />
    </div>
  </div>

  <div class="mt-3">
    <span class="field-label">
      Renderer
      <InfoHint popup panel title="Renderer" text="The server-side template that formats this event into a message. Fetched live from the alert pipeline — never hardcoded in the frontend." />
    </span>
    {#if renderersError && !renderers.length}
      <div class="text-[length:var(--fs-sm)] text-[var(--algo-red-text-bright)]">
        {renderersError} <button type="button" class="underline" onclick={loadRenderers}>Retry</button>
      </div>
    {:else}
      <div class="flex flex-wrap gap-1">
        {#each renderers as r}
          <button type="button"
            class="tag-chip" class:on={eventCreateForm.renderer === r.key}
            title={r.description || ''}
            onclick={() => eventCreateForm.renderer = r.key}>{r.label}</button>
        {/each}
        {#if !renderers.length}<span class="text-[length:var(--fs-sm)] opacity-60">loading…</span>{/if}
      </div>
    {/if}
  </div>

  <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3">
    <div>
      <span class="field-label">Log tag</span>
      <select class="log-match-select w-full" bind:value={eventCreateForm.tag} aria-label="Log tag">
        <option value="">Tag…</option>
        {#each logTags as t}<option value={t}>{t}</option>{/each}
      </select>
    </div>
    <div>
      <span class="field-label">Min level</span>
      <select class="log-match-select w-full" bind:value={eventCreateForm.minLevel} aria-label="Minimum level">
        {#each EVENT_LOG_LEVELS as lv}<option value={lv}>{lv}</option>{/each}
      </select>
    </div>
    <div>
      <span class="field-label">Where <span class="opacity-50">(optional)</span></span>
      <div class="flex gap-1">
        <input class="field-input" placeholder="key (e.g. alert_event)" bind:value={eventCreateForm.whereKey} />
        <input class="field-input" placeholder="value" bind:value={eventCreateForm.whereValue} />
      </div>
    </div>
  </div>

  <div class="mt-3">
    <span class="field-label">Alert channels</span>
    <div class="channel-grid">
      {#each EVENT_CHANNELS as ch}
        <label class="channel-row">
          <input type="checkbox"
                 class="channel-check"
                 checked={eventCreateForm.channels[ch.id]}
                 onchange={(e) => eventCreateForm.channels[ch.id] = /** @type {HTMLInputElement} */(e.target).checked} />
          <span class="channel-label">{ch.label}</span>
          <span class="channel-desc">{ch.desc}</span>
        </label>
        {#if eventCreateForm.channels[ch.id]}
          <div style="padding-left: 1.4rem; display:flex; gap:0.6rem; align-items:center; flex-wrap:wrap;">
            {#if ch.id === 'ntfy'}
              <span class="text-[length:var(--fs-xs)] opacity-60">Priority</span>
              <select class="log-match-select" bind:value={eventCreateForm.ntfyPriority} aria-label="Priority">
                <option value="normal">normal</option>
                <option value="high">high</option>
                <option value="urgent">urgent</option>
              </select>
            {/if}
            <label class="flex items-center gap-1 text-[length:var(--fs-xs)] opacity-70 cursor-pointer">
              <input type="checkbox" bind:checked={eventCreateForm.gateBypass[ch.id]} />
              Always send (bypass capability gate)
            </label>
          </div>
        {/if}
      {/each}
    </div>
  </div>

  {#if eventCreateErrors.length}
    <div class="mt-3 p-2 rounded bg-red-500/15 text-[var(--algo-red-text-bright)] text-[length:var(--fs-sm)] border border-red-500/40">
      <ul class="list-disc ml-4">{#each eventCreateErrors as err}<li>{err}</li>{/each}</ul>
    </div>
  {/if}

  <div class="flex gap-2 mt-3">
    <button type="button" class="btn-primary text-[length:var(--fs-md)] py-1 px-4" disabled={eventCreateBusy} onclick={saveEventCreate}>
      {eventCreateBusy ? 'Creating…' : 'Create'}
    </button>
    <button type="button" class="btn-secondary text-[length:var(--fs-md)] py-1 px-4" onclick={() => creatingKind = null}>Cancel</button>
  </div>
{/snippet}

<!-- Grouped agent list — compact rows, click to expand.
     Two-column magazine-flow on ≥1024 px (lg:columns-2): items
     fill column 1 top-to-bottom, then column 2 starts; expanding a
     card just grows its own column without pulling its row-neighbour
     down (true CSS-columns behaviour, unlike a 2-col Grid where
     row siblings would equalise heights). Single column on mobile. -->
{#each groupedAgents() as group}
  <h2 class="section-heading mt-3 mb-1.5 border-b border-white/10 pb-0.5">
    {group.name}
    <span class="opacity-60 font-normal ml-1">({group.agents.length})</span>
  </h2>
  <div class="page-grid agent-group-grid mb-3">
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
            <span class="text-[length:var(--fs-lg)] text-[var(--c-action)] truncate">{agent.name}</span>
            {#if agent.long_name}
              <span class="text-[length:var(--fs-lg)] font-mono truncate" style="color: var(--c-muted)">{agent.long_name}</span>
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
            class="text-[length:var(--fs-xs)] px-1.5 py-0 rounded font-bold border flex-shrink-0
              {(agent.trade_mode || 'paper') === 'live'
                ? 'bg-red-500/15 text-red-400 border-red-500/40'
                : 'bg-[var(--algo-sky-bg)] text-[var(--algo-sky)] border-sky-500/40'}">
            {(agent.trade_mode || 'paper') === 'live' ? 'L' : 'P'}
          </button>
          <button type="button"
            onclick={(e) => { e.stopPropagation(); toggle(agent); }}
            class="text-[length:var(--fs-xs)] px-1.5 py-0 rounded font-medium border flex-shrink-0
              {agent.status !== 'inactive'
                ? 'bg-green-500/15 text-[var(--algo-green)] border-green-500/40'
                : 'bg-slate-700/40 text-slate-400 border-slate-500/30'}">
            {agent.status !== 'inactive' ? 'ON' : 'OFF'}
          </button>
          <DisclosureChevron open={isOpen} ariaLabel={isOpen ? 'Collapse row' : 'Expand row'} />
        </div>

        {#if isOpen}
          {#if editing === agent.slug}
            <!-- ──────── Inline editor ──────── -->
            <div class="px-3 pb-3 pt-2 border-t" style="border-top-color: rgba(126,151,184,0.10)">
              {#if agent.kind === 'event'}
                {@render eventEditorBody(agent)}
              {:else}
                {@render thresholdEditorBody(agent)}
              {/if}
            </div>
          {:else}
            {@const _aiMeta = parseAIDescription(agent.description)}
            <!-- ──────── Normal expanded view ──────── -->
            <div class="px-2 pb-2 border-t" style="border-top-color: rgba(126,151,184,0.10)">
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
                <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic mt-1.5 mb-1">{agent.description}</div>
              {/if}

              <!-- Condition tree (always shown; falls back to text summary when parse fails) -->
              <div class="preview-section-label mt-1">Condition</div>
              {#if agent.conditions && Object.keys(agent.conditions).length}
                <div class="preview-tree">{@render renderCondNode(agent.conditions)}</div>
              {:else}
                <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic">no conditions</div>
              {/if}

              <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] mt-2 mb-1 flex items-center flex-wrap gap-x-2 gap-y-0.5">
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
                <div class="text-[length:var(--fs-sm)] text-[var(--c-muted)] italic">alert-only (no actions)</div>
              {/if}
              <div class="flex items-center justify-between text-[length:var(--fs-xs)] text-[var(--c-muted)] mt-2">
                <span>
                  Last fire: {agent.last_triggered_at ? logTime(new Date(agent.last_triggered_at)) : '—'}
                  <span class="mx-1">|</span>
                  Count: {agent.trigger_count}{#if agent.lifespan_type === 'n_fires' && agent.lifespan_max_fires}/{agent.lifespan_max_fires}{/if}
                  <span class="mx-1">|</span>
                  Cooldown: {agent.cooldown_minutes}m
                  <span class="mx-1">|</span>
                  Scope: {agent.scope}
                  {#if agent.kind !== 'event'}
                    <span class="mx-1">|</span>
                    <!-- The trade_mode Select in the editor IS the per-agent
                         override — this is purely informational so the
                         operator can see at a glance whether the row is
                         inheriting the global default or has been overridden. -->
                    Mode: {(agent.trade_mode || 'paper').toUpperCase()}
                    <span class="opacity-60">(global: {globalDefaultTradeMode.toUpperCase()})</span>
                  {/if}
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
  /* Mobile: the bare section heading sits directly in .algo-content
     (padding-left: 0 on mobile) — indent it to match the cards beside it. */
  @media (max-width: 640px) {
    .section-heading { padding-left: 0.7rem; }
  }
  /* ── Agent group grid — uses canonical .page-grid for layout ───────
     Overrides auto-fill with a fixed 2-column layout on desktop so each
     agent card takes exactly half the row width. Child overflow fix:
     `min-width: 0` lets the track honour `1fr` and allows inner overflow
     guards (word-break, overflow-x) to engage. */
  .agent-group-grid {
    grid-template-columns: repeat(2, 1fr);
  }
  @media (max-width: 768px) {
    .agent-group-grid { grid-template-columns: 1fr; }
  }
  .agent-group-grid > * {
    min-width: 0;
    max-width: 100%;
  }

  /* ── Agent row card chrome — matches agent-templates' .frag-row look
     (same literal gradient/border/radius) for visual parity across the
     two automation sub-pages. Scoped to this component only — the shared
     global .algo-status-card in app.css (used on 9+ other pages) is
     untouched; [data-status="..."] variants there only set --st-fg/
     --st-bg/--st-border for the nested .algo-status-pill badge, never
     the card container's own background/border, so no conflict. */
  .algo-status-card {
    background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    border: 1px solid rgba(126,151,184,0.10);
    border-radius: 0.3rem;
    overflow: hidden;
    transition: border-color 0.08s;
  }
  .algo-status-card:hover { border-color: rgba(251,191,36,0.25); }

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
  /* "+ New Agent" pill — green-tinted variant of .ai-pill, same shape. */
  .new-agent-pill {
    border-color: rgba(74,222,128,0.45);
    background: var(--c-long-10);
    color: var(--c-long);
  }
  .new-agent-pill:hover { background: rgba(74,222,128,0.20); }
  .new-agent-card { border-color: rgba(74,222,128,0.30); }

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
  .ai-hint { font-size: var(--fs-sm); color: var(--algo-muted); font-family: var(--font-numeric); }
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
    color: var(--algo-slate-muted);
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
    color: var(--algo-slate-muted);
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

  /* ── Tier + topic strip ────────────────────────────────────────────────
     Single row, two halves. Left half hosts the 4-tier pill row; right
     half hosts the topic input. Built as a
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
    background: rgba(126,151,184,0.10);
    color: var(--algo-slate-muted);
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
    font-size: var(--fs-xs);
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

  /* Structured place_order controls — convenience mini-form shown below
     the Actions textarea when it parses to exactly one place_order
     action. Mobile-first: wraps to a single column under 600px. */
  .place-order-struct {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    margin-top: 0.4rem;
    padding: 0.4rem 0.5rem;
    border: 1px solid rgba(148,163,184,0.18);
    border-radius: 4px;
    background: rgba(255,255,255,0.02);
  }
  .pos-field {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
    min-width: 6rem;
  }
  .pos-label {
    font-size: var(--fs-xs);
    color: var(--c-muted);
    font-weight: 600;
    letter-spacing: 0.02em;
  }
  @media (max-width: 600px) {
    .place-order-struct { flex-direction: column; }
    .pos-field { min-width: 0; }
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
  onTabChange={(id) => { logTab = id; }}
/>
