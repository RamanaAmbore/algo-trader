<script>
  import { onMount, onDestroy, getContext } from 'svelte';
  import { clientTimestamp, logTime, lifespanChip, visibleInterval } from '$lib/stores';
  import InfoHint from '$lib/InfoHint.svelte';
  import {
    fetchAgents, activateAgent, deactivateAgent, updateAgent,
    fetchRecentAgentEvents, fetchSimTicks, fetchSimEvents, fetchSimStatus,
    startSimForAgent, aiDraftAgent,
  } from '$lib/api';
  import LogPanel from '$lib/LogPanel.svelte';
  import Select   from '$lib/Select.svelte';

  let agents      = $state([]);
  let agentEvents = $state([]);
  let loading     = $state(true);
  let error       = $state('');
  let logTab      = $state('agent');
  let simLog      = $state(/** @type {any[]} */ ([]));
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
      const { createAgent } = await import('$lib/api');
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
    name: string, description: string, conditions: string, events: string, actions: string,
    cooldown_minutes: number, scope: string, schedule: string,
    lifespan_type: string, lifespan_max_fires: number|string,
    lifespan_expires_at: string,
  }} */ ({
    name: '', description: '', conditions: '{}', events: '[]', actions: '[]',
    cooldown_minutes: 30, scope: 'total', schedule: 'market_hours',
    lifespan_type: 'persistent', lifespan_max_fires: '', lifespan_expires_at: '',
  }));
  // ── Trade-mode confirm modal ──────────────────────────────────────────
  let pendingLiveAgent = $state(/** @type {any} */ (null));

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
    // 'order' and 'system' tabs are self-fetching inside LogPanel.
    if (logTab === 'agent') loadAgentLog();
    else if (logTab === 'simulator') loadSimLog();
  }

  async function loadAll() {
    loading = true;
    await Promise.all([loadAgents(), loadAgentLog(), loadSimLog()]);
    loading = false;
  }

  async function toggle(/** @type {any} */ agent) {
    try {
      if (agent.status === 'inactive') await activateAgent(agent.slug);
      else await deactivateAgent(agent.slug);
      await loadAgents();
    } catch (e) { error = e.message; }
  }

  /** Flip the agent's trade mode between paper and live in-place.
   *  Paper → live shows a confirm modal; live → paper flips immediately.
   *  Optimistic update so the chip flips instantly on the slow link. */
  function toggleTradeMode(/** @type {any} */ agent) {
    const cur = agent.trade_mode || 'paper';
    const next = cur === 'live' ? 'paper' : 'live';
    if (next === 'live') {
      pendingLiveAgent = agent;
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
      await loadAgents();
    } catch (e) {
      error = e.message;
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
  <InfoHint popup text="Agents fire on every 5-min tick during market hours. Each agent has a <b>condition tree</b>, <b>notify</b> channels, and <b>actions</b>. Slug is the stable identifier; schedule controls when it runs (<b>market_hours</b> skips outside session); cooldown_minutes throttles re-fires." />
  <span class="ml-auto flex items-center gap-2">
    <button class="ai-pill" onclick={() => aiOpen = !aiOpen}>
      {aiOpen ? '× Close AI' : '✦ Ask AI'}
    </button>
    <span class="algo-ts">{clientTimestamp()}</span>
  </span>
</div>

{#if error}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
{/if}

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
                  <Select ariaLabel="Scope" bind:value={editForm.scope}
                    options={[
                      { value: 'total',       label: 'Total Only' },
                      { value: 'per_account', label: 'Per Account' },
                    ]} />
                </div>
                <div>
                  <label class="field-label">Schedule</label>
                  <Select ariaLabel="Schedule" bind:value={editForm.schedule}
                    options={[
                      { value: 'market_hours', label: 'Market Hours' },
                      { value: 'always',       label: 'Always' },
                    ]} />
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
                  <Select ariaLabel="Lifespan" bind:value={editForm.lifespan_type}
                    options={[
                      { value: 'persistent', label: 'Persistent (default)' },
                      { value: 'one_shot',   label: 'One-shot (fires once)' },
                      { value: 'n_fires',    label: 'N fires' },
                      { value: 'until_date', label: 'Until date' },
                    ]} />
                  {#if lifespanChip(agent)}
                    {@const _ls = lifespanChip(agent)}
                    <div class="text-[0.55rem] text-[#7e97b8] mt-1" title={_ls.tooltip}>
                      Current: <span class={'lifespan-chip lifespan-chip-' + _ls.color}>{_ls.label}</span>
                    </div>
                  {:else if agent.lifespan_type === 'persistent'}
                    <div class="text-[0.55rem] text-[#7e97b8] mt-1 italic">
                      Persistent — fires until manually deactivated.
                    </div>
                  {/if}
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
                    class="text-[#fb7185] hover:underline">Run in Simulator</button>
                  {:else}
                  <span title="Demo: sim disabled"
                    class="text-[#7e97b8] cursor-not-allowed opacity-50 select-none">Run in Simulator</span>
                  {/if}
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
  /* ── Ask-AI form ─────────────────────────────────────────────────── */
  .ai-pill {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 0.18rem 0.55rem;
    border-radius: 4px;
    border: 1px solid rgba(167,139,250,0.45);
    background: rgba(167,139,250,0.10);
    color: #a78bfa;
    cursor: pointer;
    transition: background 0.1s;
  }
  .ai-pill:hover { background: rgba(167,139,250,0.20); }
  .ai-card {
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid rgba(167,139,250,0.30);
    border-radius: 5px;
    padding: 0.6rem 0.75rem;
    margin-bottom: 0.6rem;
  }
  .ai-head { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; flex-wrap: wrap; }
  .ai-title {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    color: #a78bfa;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .ai-hint { font-size: 0.55rem; color: #7e97b8; font-family: ui-monospace, monospace; }
  .ai-prompt {
    width: 100%;
    background: rgba(0,0,0,0.30);
    border: 1px solid rgba(167,139,250,0.20);
    border-radius: 4px;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    padding: 0.4rem 0.55rem;
    resize: vertical;
  }
  .ai-prompt:focus { outline: none; border-color: rgba(167,139,250,0.55); }
  .ai-actions { display: flex; gap: 0.4rem; margin-top: 0.4rem; align-items: center; flex-wrap: wrap; }
  .ai-btn {
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.22rem 0.65rem;
    border-radius: 3px;
    border: 1px solid rgba(167,139,250,0.45);
    background: rgba(167,139,250,0.12);
    color: #a78bfa;
    cursor: pointer;
  }
  .ai-btn:hover:not(:disabled) { background: rgba(167,139,250,0.22); }
  .ai-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .ai-btn-save {
    border-color: rgba(74,222,128,0.45);
    background: rgba(74,222,128,0.10);
    color: #4ade80;
  }
  .ai-btn-save:hover:not(:disabled) { background: rgba(74,222,128,0.20); }
  .ai-slug {
    background: rgba(0,0,0,0.30);
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 3px;
    color: #c8d8f0;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    padding: 0.18rem 0.45rem;
    width: 12rem;
  }
  .ai-why {
    margin-top: 0.45rem;
    font-size: 0.65rem;
    color: #c8d8f0;
    background: rgba(167,139,250,0.06);
    border-left: 2px solid #a78bfa;
    padding: 0.32rem 0.55rem;
    font-family: ui-monospace, monospace;
  }
  .ai-warns, .ai-errs { margin: 0.4rem 0 0; padding-left: 0.4rem; list-style: none; }
  .ai-warns li {
    color: #fbbf24;
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    padding: 0.08rem 0;
  }
  .ai-errs li {
    color: #f87171;
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    padding: 0.08rem 0;
  }
  .ai-json {
    margin-top: 0.45rem;
    font-size: 0.6rem;
    color: #7e97b8;
    font-family: ui-monospace, monospace;
  }
  .ai-json summary { cursor: pointer; }
  .ai-json pre {
    margin-top: 0.3rem;
    background: rgba(0,0,0,0.30);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 3px;
    padding: 0.4rem 0.55rem;
    color: #c8d8f0;
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
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
  }
  .ai-meta-pill {
    border: 1px solid rgba(167,139,250,0.45);
    background: rgba(167,139,250,0.10);
    color: #a78bfa;
    padding: 0.08rem 0.4rem;
    border-radius: 3px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    flex-shrink: 0;
  }
  .ai-meta-why { color: #c8d8f0; flex: 1; min-width: 12rem; }
  .ai-meta-prompt { font-size: 0.6rem; color: #7e97b8; }
  .ai-meta-prompt summary { cursor: pointer; color: #a78bfa; }
  .ai-meta-prompt summary:hover { color: #c4b5fd; }
  .ai-meta-prompt span {
    display: block;
    margin-top: 0.2rem;
    color: #c8d8f0;
    background: rgba(167,139,250,0.06);
    padding: 0.3rem 0.5rem;
    border-left: 2px solid #a78bfa;
    border-radius: 2px;
  }
  .ai-meta-rest { color: #c8d8f0aa; font-style: italic; flex-basis: 100%; }

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
  /* New lifespanChip variants — color progresses sky → amber → red as
     the agent's budget is consumed. Grey is the "exhausted / done"
     terminal state. Mirrors lifespanChip()'s `color` field in stores.js. */
  .lifespan-chip {
    display: inline-block;
    padding: 0 0.3rem;
    border-radius: 2px;
    border: 1px solid;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
    cursor: help;
  }
  .lifespan-chip-sky   { color: #7dd3fc; border-color: rgba(125,211,252,0.45); background: rgba(125,211,252,0.10); }
  .lifespan-chip-amber { color: #fbbf24; border-color: rgba(251,191,36,0.55);  background: rgba(251,191,36,0.10); }
  .lifespan-chip-red   { color: #f87171; border-color: rgba(248,113,113,0.55); background: rgba(248,113,113,0.12); }
  .lifespan-chip-grey  { color: #94a3b8; border-color: rgba(148,163,184,0.40); background: rgba(148,163,184,0.10); }
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
  .action-add-cancel { background: rgba(148,163,184,0.12); color: #c8d8f0; border-color: rgba(148,163,184,0.35); }
  .action-add-cancel:hover { background: rgba(148,163,184,0.25); border-color: #94a3b8; }
  .action-add-log    { background: rgba(125,211,252,0.12); color: #7dd3fc; border-color: rgba(125,211,252,0.4); }
  .action-add-log:hover    { background: rgba(125,211,252,0.25); border-color: #7dd3fc; }

  /* ── LIVE trade-mode confirm modal ──────────────────────────────── */
  .lc-overlay {
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.65);
    display: flex; align-items: center; justify-content: center;
    z-index: 200;
  }
  .lc-modal {
    background: linear-gradient(180deg, #0a1020 0%, #131c33 100%);
    border: 1px solid rgba(248,113,113,0.35);
    border-radius: 6px;
    padding: 1rem 1.1rem 0.85rem;
    max-width: min(24rem, 92vw);
    width: 100%;
  }
  .lc-title {
    font-size: 0.75rem;
    font-weight: 700;
    color: #f87171;
    margin-bottom: 0.55rem;
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }
  .lc-body {
    font-size: 0.68rem;
    color: #c8d8f0;
    line-height: 1.55;
    margin-bottom: 0.85rem;
  }
  .lc-body .font-mono { font-family: ui-monospace, monospace; color: #7dd3fc; }
  .lc-actions { display: flex; justify-content: flex-end; gap: 0.5rem; }
  .lc-btn {
    font-size: 0.65rem;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    padding: 0.28rem 0.85rem;
    border-radius: 4px;
    border: 1px solid;
    cursor: pointer;
  }
  .lc-cancel  { background: rgba(125,151,184,0.10); color: #7e97b8; border-color: rgba(125,151,184,0.30); }
  .lc-cancel:hover  { background: rgba(125,151,184,0.22); }
  .lc-confirm { background: rgba(248,113,113,0.15); color: #f87171; border-color: rgba(248,113,113,0.45); }
  .lc-confirm:hover { background: rgba(248,113,113,0.28); }
</style>

{#if pendingLiveAgent}
  <!-- LIVE trade-mode confirm modal — replaces window.confirm() which is
       blocked / unstyled on iOS Safari standalone mode. -->
  <div class="lc-overlay" role="presentation"
       onclick={() => pendingLiveAgent = null}
       onkeydown={(e) => { if (e.key === 'Escape') pendingLiveAgent = null; }}>
    <div class="lc-modal" role="dialog" aria-modal="true"
         onclick={(e) => e.stopPropagation()}
         onkeydown={(e) => e.stopPropagation()}>
      <div class="lc-title">Set to LIVE mode?</div>
      <div class="lc-body">
        <b>{pendingLiveAgent.name}</b> — every action this agent fires will
        hit the real Kite broker (subject to the master
        <span class="font-mono">execution.paper_trading_mode</span> kill-switch).
      </div>
      <div class="lc-actions">
        <button type="button" class="lc-btn lc-cancel"
                onclick={() => pendingLiveAgent = null}>Cancel</button>
        <button type="button" class="lc-btn lc-confirm"
                onclick={() => { const a = pendingLiveAgent; pendingLiveAgent = null; applyTradeMode(a, 'live'); }}>
          Set LIVE
        </button>
      </div>
    </div>
  </div>
{/if}

<LogPanel
  heightClass="h-[50vh]"
  defaultTab={logTab}
  simScope={simActive}
  onTabChange={(id) => { logTab = id; }}
/>
