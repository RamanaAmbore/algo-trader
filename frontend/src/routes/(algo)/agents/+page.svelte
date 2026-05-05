<script>
  import { onMount, onDestroy } from 'svelte';
  import { clientTimestamp, visibleInterval } from '$lib/stores';
  import {
    fetchAgents, activateAgent, deactivateAgent, updateAgent,
    fetchRecentAgentEvents, fetchSimTicks, fetchSimEvents, fetchSimStatus,
    startSimForAgent, fetchAlgoOrdersRecent, fetchAdminLogs,
  } from '$lib/api';
  import LogPanel from '$lib/LogPanel.svelte';

  let agents      = $state([]);
  let agentEvents = $state([]);
  let loading     = $state(true);
  let error       = $state('');
  let logTab      = $state('agent');
  let systemLog   = $state([]);
  let orderLog    = $state([]);
  // Structured algo-order rows (mode=sim|live) for the Order tab of the
  // LogPanel. Refreshed alongside the rest of the logs; the panel shows
  // side / qty / symbol / price / account for every paper-traded or live
  // order an agent action created.
  let orderRows   = $state(/** @type {any[]} */ ([]));
  let simLog      = $state(/** @type {any[]} */ ([]));
  // Global simulator status — when active, the Agent-events panel swaps to
  // the simulator's event stream so operators only see sim results in the
  // algo pages while the sim is running.
  let simActive   = $state(false);
  // Symbols with captured price-history ticks. Sourced from the active
  let editing     = $state(null);     // slug of agent being edited
  let expandedSlug = $state(/** @type {string|null} */(null));
  let editForm    = $state(/** @type {{
    name: string, description: string, conditions: string, events: string, actions: string,
    cooldown_minutes: number, scope: string, schedule: string,
    lifespan_type: string, lifespan_max_fires: number|string,
    lifespan_expires_at: string,
  }} */ ({
    name: '', description: '', conditions: '{}', events: '[]', actions: '[]',
    cooldown_minutes: 30, scope: 'total', schedule: 'market_hours',
    lifespan_type: 'persistent', lifespan_max_fires: '', lifespan_expires_at: '',
  }));
  let ws;
  let refreshTeardown;
  let simStatusTeardown;

  async function loadAgents() {
    try {
      const data = await fetchAgents();
      agents = data;
    } catch (e) { error = e.message; }
  }

  async function loadAgentLog() {
    // Scope the Agent-events panel by simulator status: while the sim is
    // running, show ONLY the sim's events (sim_mode=True rows). When idle,
    // show the real stream. This way the /algo page tracks the sim end-to-end
    // without the operator having to toggle filters manually.
    try {
      const data = simActive ? await fetchSimEvents(100)
                              : await fetchRecentAgentEvents(100);
      agentEvents = data;
    } catch (e) { /* ignore */ }
  }

  async function pollSimStatus() {
    try {
      const s = await fetchSimStatus();
      const was = simActive;
      simActive = !!s.active;
      // When the sim flips on/off we want the events panel to swap sources
      // immediately, not on the next 30-second refresh tick.
      if (was !== simActive) loadAgentLog();
      // While the sim is running, refresh the Simulator tab's tick stream
      // on every status poll (4s) so /agents shows the same up-to-date
      // stream as /admin/simulator. Without this, simLog only updated on
      // the 30-second loadAll cycle — the Simulator tab looked stale.
      if (simActive) loadSimLog();
    } catch (_) { /* cap flag off — treat as idle */ }
  }

  async function loadSystemLog() {
    try {
      const d = await fetchAdminLogs(100);
      systemLog = d.lines || [];
    } catch (e) { /* ignore */ }
  }

  async function loadOrderLog() {
    // orderLog = raw agent events (kept for the Terminal-tab fallback).
    // orderRows = structured AlgoOrder rows (mode=live or sim) — this is
    // what the Order tab renders. Fetches both so the two tabs stay in
    // sync on a single refresh tick.
    try {
      const [ev, algo] = await Promise.all([
        fetchRecentAgentEvents(100),
        fetchAlgoOrdersRecent(100, 'all'),
      ]);
      orderLog  = ev;
      orderRows = algo;
    } catch (e) { /* ignore */ }
  }

  async function loadSimLog() {
    // Polled every few seconds while the Simulator tab is visible so the
    // sim's tick timeline stays roughly live. Silently ignores failures
    // (sim endpoint 400s when cap_in_<branch>.simulator is off).
    try {
      const data = await fetchSimTicks(100);
      simLog = Array.isArray(data) ? data : [];
    } catch (e) { /* ignore */ }
  }

  function loadCurrentLog() {
    if (logTab === 'agent') loadAgentLog();
    else if (logTab === 'system') loadSystemLog();
    else if (logTab === 'order') loadOrderLog();
    else if (logTab === 'simulator') loadSimLog();
  }

  async function loadAll() {
    loading = true;
    await Promise.all([loadAgents(), loadAgentLog(), loadSystemLog(),
                       loadSimLog()]);
    loading = false;
  }

  async function toggle(/** @type {any} */ agent) {
    try {
      if (agent.status === 'inactive') await activateAgent(agent.slug);
      else await deactivateAgent(agent.slug);
      await loadAgents();
    } catch (e) { error = e.message; }
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
      description: agent.description || '',
      conditions: JSON.stringify(agent.conditions, null, 2),
      events: JSON.stringify(agent.events, null, 2),
      actions: JSON.stringify(agent.actions, null, 2),
      cooldown_minutes: agent.cooldown_minutes,
      scope: agent.scope,
      schedule: agent.schedule || 'market_hours',
      lifespan_type:        agent.lifespan_type || 'persistent',
      lifespan_max_fires:   agent.lifespan_max_fires == null ? '' : agent.lifespan_max_fires,
      // ISO datetime → "YYYY-MM-DDTHH:MM" (datetime-local input format).
      // Trim seconds + tz so the native input accepts the value.
      lifespan_expires_at:  agent.lifespan_expires_at
        ? String(agent.lifespan_expires_at).slice(0, 16)
        : '',
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
  const parsedActions = $derived.by(() => {
    try { return { ok: true, value: JSON.parse(editForm.actions || '[]') }; }
    catch (e) { return { ok: false, error: e.message }; }
  });

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

  async function saveEdit() {
    // Server-side validation must pass for v2 trees before we touch the
    // agent row — v1 trees are accepted as-is.
    const ok = await runValidation();
    if (!ok) return;
    try {
      await updateAgent(editing, {
        name: editForm.name,
        description: editForm.description,
        conditions: JSON.parse(editForm.conditions),
        events: JSON.parse(editForm.events),
        actions: JSON.parse(editForm.actions),
        cooldown_minutes: editForm.cooldown_minutes,
        scope: editForm.scope,
        schedule: editForm.schedule,
        lifespan_type: editForm.lifespan_type || 'persistent',
        lifespan_max_fires: (editForm.lifespan_type === 'n_fires'
          && editForm.lifespan_max_fires !== '' && editForm.lifespan_max_fires != null)
          ? Number(editForm.lifespan_max_fires) : null,
        lifespan_expires_at: (editForm.lifespan_type === 'until_date'
          && editForm.lifespan_expires_at)
          ? String(editForm.lifespan_expires_at) : null,
      });
      editing = null;
      validationErrors = []; validationGrammar = '';
      await loadAgents();
    } catch (e) { error = e.message; }
  }

  function connectWS() {
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
        if (['agent_alert', 'agent_state'].includes(evt.event)) {
          loadAgentLog();
        }
      } catch { /* ignore */ }
    };
    ws.onclose = () => setTimeout(connectWS, 3000);
  }

  const statusDot = (/** @type {string} */ s) => ({
    active: 'bg-green-500', inactive: 'bg-slate-500',
    triggered: 'bg-red-500', running: 'bg-amber-400',
    cooldown: 'bg-amber-300', error: 'bg-red-600',
  }[s] || 'bg-slate-500');

  function channelSummary(/** @type {any[]} */ events) {
    if (!events) return '—';
    return events.filter(e => e.enabled).map(e => e.channel).join(', ');
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
    place_order: {
      type: "place_order",
      params: { account: "ZG####", symbol: "<tradingsymbol>", exchange: "NFO",
                side: "SELL", qty: 50, order_type: "LIMIT" },
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
    error = '';
    try {
      await startSimForAgent(agent.id);
      logTab = 'simulator';
      loadSimLog();
    } catch (e) {
      error = e.message || 'Sim start failed.';
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
    if (ws) ws.close();
    refreshTeardown?.();
    simStatusTeardown?.();
  });
</script>

<svelte:head>
  <title>Agents | RamboQuant Analytics</title>
</svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">
    Agents
    {#if simActive}
      <span class="ml-2 align-middle text-[0.6rem] px-1.5 py-0.5 rounded bg-[#fb7185]/20 text-[#fb7185] border border-[#fb7185]/40 font-mono">
        SIMULATOR EVENTS
      </span>
    {/if}
  </h1>
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

{#if error}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
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

<!-- Grouped agent list — compact rows, click to expand -->
{#each groupedAgents() as group}
  <h2 class="text-[0.6rem] font-bold uppercase tracking-wider text-[#fbbf24] mt-3 mb-1.5 border-b border-[#fbbf24]/25 pb-0.5">
    {group.name}
    <span class="opacity-60 font-normal ml-1">({group.agents.length})</span>
  </h2>
  <div class="space-y-1 mb-3">
    {#each group.agents as agent}
      {@const isOpen = expandedSlug === agent.slug}
      <div class="algo-status-card {agent.status === 'triggered' ? 'animate-pulse' : ''}"
           data-status={agent.status}
           style="padding: 0">
        <!-- Compact row (always visible). Div + role="button" so the inner
             ON/OFF can stay a real <button> — nested buttons aren't valid. -->
        <div role="button" tabindex="0"
          onclick={() => expandedSlug = isOpen ? null : agent.slug}
          onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); expandedSlug = isOpen ? null : agent.slug; } }}
          class="w-full flex items-center gap-2 px-2 py-1 text-left cursor-pointer select-none">
          <span class="w-2 h-2 rounded-full {statusDot(agent.status)} flex-shrink-0"></span>
          <span class="text-xs text-[#fbbf24] flex-1 truncate">{agent.name}</span>
          <button type="button"
            onclick={(e) => { e.stopPropagation(); toggle(agent); }}
            class="text-[0.55rem] px-1.5 py-0 rounded font-medium border flex-shrink-0
              {agent.status !== 'inactive'
                ? 'bg-green-500/15 text-green-400 border-green-500/40'
                : 'bg-slate-700/40 text-slate-400 border-slate-500/30'}">
            {agent.status !== 'inactive' ? 'ON' : 'OFF'}
          </button>
          <span class="text-[#7e97b8] text-[0.65rem] flex-shrink-0">{isOpen ? '▾' : '▸'}</span>
        </div>

        {#if isOpen}
          {#if editing === agent.slug}
            <!-- ──────── Inline editor (form on top, tree preview below) ──────── -->
            <div class="px-3 pb-3 pt-2 border-t border-white/5">
              <!-- ── FORM FIELDS ── -->
              <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div>
                  <label class="field-label">Name</label>
                  <input bind:value={editForm.name} class="field-input" />
                </div>
                <div>
                  <label class="field-label">Description</label>
                  <input bind:value={editForm.description} class="field-input" />
                </div>
                <div>
                  <label class="field-label">Scope</label>
                  <select bind:value={editForm.scope} class="field-input">
                    <option value="total">Total Only</option>
                    <option value="per_account">Per Account</option>
                  </select>
                </div>
                <div>
                  <label class="field-label">Schedule</label>
                  <select bind:value={editForm.schedule} class="field-input">
                    <option value="market_hours">Market Hours</option>
                    <option value="always">Always</option>
                  </select>
                </div>
                <div>
                  <label class="field-label">Cooldown (minutes)</label>
                  <input type="number" bind:value={editForm.cooldown_minutes} class="field-input" />
                </div>
                <!-- Lifespan — controls whether the agent persists or
                     auto-completes after firing. one_shot / n_fires let
                     algos spawn temporary agents (expiry-day auto-close,
                     "watch this until X" rules) that drop out of the
                     active set on completion instead of needing a manual
                     deactivate. -->
                <div>
                  <label class="field-label">Lifespan</label>
                  <select bind:value={editForm.lifespan_type} class="field-input">
                    <option value="persistent">Persistent (default)</option>
                    <option value="one_shot">One-shot (fires once)</option>
                    <option value="n_fires">N fires</option>
                    <option value="until_date">Until date</option>
                  </select>
                </div>
                {#if editForm.lifespan_type === 'n_fires'}
                  <div>
                    <label class="field-label">Max fires</label>
                    <input type="number" min="1"
                           bind:value={editForm.lifespan_max_fires}
                           class="field-input"
                           placeholder="e.g. 3" />
                  </div>
                {/if}
                {#if editForm.lifespan_type === 'until_date'}
                  <div>
                    <label class="field-label">Expires at (UTC)</label>
                    <input type="datetime-local"
                           bind:value={editForm.lifespan_expires_at}
                           class="field-input" />
                  </div>
                {/if}
              </div>
              <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3">
                <div>
                  <label class="field-label">Conditions (JSON)</label>
                  <textarea bind:value={editForm.conditions} class="field-input font-mono text-[0.6rem]" rows="5"></textarea>
                </div>
                <div>
                  <label class="field-label">Events (JSON)</label>
                  <textarea bind:value={editForm.events} class="field-input font-mono text-[0.6rem]" rows="5"></textarea>
                </div>
                <div>
                  <div class="flex items-center justify-between flex-wrap gap-1">
                    <label class="field-label">Actions (JSON)</label>
                    <!-- Quick-add pills — click appends a skeleton action
                         entry so operators don't have to remember the
                         exact shape. Params are templated to legal values;
                         the operator tunes them after. -->
                    <div class="flex flex-wrap gap-1">
                      <button type="button" onclick={() => addAction('close_position')}
                        class="action-add-pill action-add-close">+ close_position</button>
                      <button type="button" onclick={() => addAction('place_order')}
                        class="action-add-pill action-add-place">+ place_order</button>
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
            <!-- ──────── Normal expanded view ──────── -->
            <div class="px-2 pb-2 border-t border-white/5">
              {#if agent.description}
                <div class="text-[0.6rem] text-[#c8d8f0]/60 italic mt-1.5 mb-1">{agent.description}</div>
              {/if}

              <!-- Condition tree (always shown; falls back to text summary when parse fails) -->
              <div class="preview-section-label mt-1">Condition</div>
              {#if agent.conditions && Object.keys(agent.conditions).length}
                <div class="preview-tree">{@render renderCondNode(agent.conditions)}</div>
              {:else}
                <div class="text-[0.6rem] text-[#c8d8f0]/60 italic">no conditions</div>
              {/if}

              <div class="text-[0.6rem] text-[#c8d8f0]/75 mt-2 mb-1">
                <span class="text-[#7e97b8]">Alert via:</span> {channelSummary(agent.events)}
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
              <div class="flex items-center justify-between text-[0.55rem] text-[#7e97b8] mt-2">
                <span>
                  Last fire: {agent.last_triggered_at?.slice(0, 16) || '—'}
                  <span class="mx-1">|</span>
                  Count: {agent.trigger_count}{#if agent.lifespan_type === 'n_fires' && agent.lifespan_max_fires}/{agent.lifespan_max_fires}{/if}
                  <span class="mx-1">|</span>
                  Cooldown: {agent.cooldown_minutes}m
                  <span class="mx-1">|</span>
                  Scope: {agent.scope}
                  {#if agent.lifespan_type && agent.lifespan_type !== 'persistent'}
                    <span class="mx-1">|</span>
                    <span class="agent-lifespan-tag" title={
                      agent.lifespan_type === 'one_shot' ? 'Auto-completes after firing once' :
                      agent.lifespan_type === 'n_fires' ? `Auto-completes after ${agent.lifespan_max_fires || '?'} fires` :
                      agent.lifespan_type === 'until_date' ? `Auto-completes at ${(agent.lifespan_expires_at || '').slice(0,16)} UTC` :
                      ''
                    }>
                      {#if agent.lifespan_type === 'one_shot'}1×
                      {:else if agent.lifespan_type === 'n_fires'}≤{agent.lifespan_max_fires || '?'}×
                      {:else if agent.lifespan_type === 'until_date'}until {(agent.lifespan_expires_at || '').slice(0,10)}
                      {/if}
                    </span>
                  {/if}
                </span>
                <span class="flex items-center gap-3">
                  <button type="button"
                    onclick={(e) => { e.stopPropagation(); runInSim(agent); }}
                    title="Dry-fire this agent in the Simulator (bypasses schedule / cooldown / baseline)"
                    class="text-[#fb7185] hover:underline">Run in Simulator</button>
                  <button type="button"
                    onclick={(e) => { e.stopPropagation(); startEdit(agent); }}
                    class="text-[#fbbf24] hover:underline">Edit</button>
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
  /* Live-preview styling — compact, dense, matches algo dark palette. */
  .agent-preview {
    font-size: 0.65rem;
    color: #c8d8f0;
    border-left: 1px dashed rgba(255,255,255,0.08);
    padding-left: 0.75rem;
  }
  .preview-heading {
    font-size: 0.55rem;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #7e97b8;
    margin-bottom: 0.5rem;
  }
  .preview-header { margin-bottom: 0.5rem; }
  .preview-title { font-weight: 700; color: #fbbf24; font-size: 0.8rem; }
  .preview-desc  { font-style: italic; color: #c8d8f0aa; font-size: 0.6rem; margin-top: 0.1rem; }
  .preview-meta  { font-size: 0.55rem; color: #7e97b8; margin-top: 0.2rem; }
  .preview-sep   { margin: 0 0.35rem; color: #7e97b840; }
  .preview-section-label {
    font-size: 0.55rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #fbbf24;
    margin: 0.65rem 0 0.3rem;
    border-bottom: 1px solid rgba(251,191,36,0.15);
    padding-bottom: 0.1rem;
  }
  .preview-muted { color: #7e97b8; font-style: italic; }
  .preview-error {
    color: #f87171;
    background: rgba(248,113,113,0.1);
    border: 1px solid rgba(248,113,113,0.35);
    padding: 0.3rem 0.5rem;
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
  }
  .preview-tree { font-family: ui-monospace, monospace; }
  /* Nested node pattern — indent on the left, operator badge at top, children below */
  :global(.tree-node) {
    border-left: 2px solid rgba(255,255,255,0.12);
    padding: 0.15rem 0 0.15rem 0.5rem;
    margin: 0.15rem 0;
  }
  :global(.tree-node-all) { border-left-color: #4ade80; }
  :global(.tree-node-any) { border-left-color: #fbbf24; }
  :global(.tree-node-not) { border-left-color: #f87171; }
  :global(.tree-op) {
    font-size: 0.5rem;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    font-weight: 700;
    color: inherit;
    margin-bottom: 0.1rem;
  }
  :global(.tree-node-all .tree-op) { color: #4ade80; }
  :global(.tree-node-any .tree-op) { color: #fbbf24; }
  :global(.tree-node-not .tree-op) { color: #f87171; }
  :global(.tree-children) { padding-left: 0.25rem; }
  :global(.tree-leaf) {
    font-size: 0.6rem;
    background: rgba(125,211,252,0.08);
    border: 1px solid rgba(125,211,252,0.2);
    color: #c8d8f0;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    margin: 0.15rem 0;
    display: inline-block;
  }
  .preview-chip {
    font-size: 0.55rem;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    border: 1px solid;
    font-family: ui-monospace, monospace;
  }
  .chip-on  { background: rgba(74,222,128,0.15);  color: #4ade80; border-color: rgba(74,222,128,0.4); }
  .chip-off { background: rgba(180,200,230,0.08); color: #7e97b8; border-color: rgba(180,200,230,0.2); }
  .preview-action {
    background: rgba(251,191,36,0.06);
    border: 1px solid rgba(251,191,36,0.2);
    border-radius: 3px;
    padding: 0.3rem 0.4rem;
  }
  .preview-action-type { color: #fbbf24; font-weight: 700; font-family: ui-monospace, monospace; font-size: 0.6rem; }

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
    font-family: ui-monospace, monospace;
    font-weight: 700;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
  }
  .preview-action-params {
    font-size: 0.55rem;
    background: rgba(0,0,0,0.25);
    color: #c8d8f0;
    padding: 0.25rem 0.35rem;
    border-radius: 2px;
    margin-top: 0.2rem;
    overflow-x: auto;
  }

  /* Quick-add action pills next to the Actions textarea. Compact, colour-
     coded by rough semantic group so they don't visually blend together. */
  .action-add-pill {
    font-size: 0.5rem;
    padding: 0.1rem 0.4rem;
    border-radius: 999px;
    border: 1px solid;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    letter-spacing: 0.02em;
    cursor: pointer;
    white-space: nowrap;
    transition: background-color 0.08s, border-color 0.08s;
  }
  .action-add-close  { background: rgba(251,113,133,0.12); color: #fb7185; border-color: rgba(251,113,133,0.4); }
  .action-add-close:hover  { background: rgba(251,113,133,0.25); border-color: #fb7185; }
  .action-add-place  { background: rgba(16,185,129,0.12);  color: #6ee7b7; border-color: rgba(16,185,129,0.4); }
  .action-add-place:hover  { background: rgba(16,185,129,0.25); border-color: #10b981; }
  .action-add-chase  { background: rgba(251,191,36,0.12);  color: #fbbf24; border-color: rgba(251,191,36,0.4); }
  .action-add-chase:hover  { background: rgba(251,191,36,0.25); border-color: #fbbf24; }
  .action-add-cancel { background: rgba(148,163,184,0.12); color: #cbd5e1; border-color: rgba(148,163,184,0.35); }
  .action-add-cancel:hover { background: rgba(148,163,184,0.25); border-color: #94a3b8; }
  .action-add-log    { background: rgba(125,211,252,0.12); color: #7dd3fc; border-color: rgba(125,211,252,0.4); }
  .action-add-log:hover    { background: rgba(125,211,252,0.25); border-color: #7dd3fc; }
</style>

<LogPanel
  heightClass="h-[50vh]"
  initialTab={logTab}
  cmdHistory={[]}
  {orderLog}
  {orderRows}
  agentLog={agentEvents}
  {systemLog}
  {simLog}
  onTabChange={(id) => { logTab = id; loadCurrentLog(); }}
/>
