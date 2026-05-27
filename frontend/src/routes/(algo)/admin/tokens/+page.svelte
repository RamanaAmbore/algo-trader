<script>
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, nowStamp } from '$lib/stores';

  // Demo gate — on dev, anonymous visitors get redirected to /signin by
  // the algo layout before this page loads, so an unsigned viewer here
  // can only be a recruiter / cold visitor on the prod (main) branch.
  // Backend opens GET endpoints to demo (no secrets in catalog rows);
  // every write button hides so the viewer can't try edits that would
  // 401 server-side.
  const isDemo = $derived(!$authStore.user);
  import {
    fetchGrammarTokens, patchGrammarToken, createGrammarToken,
    deleteGrammarToken, reloadGrammarRegistry,
  } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';
  import Select   from '$lib/Select.svelte';
  import AgentWorkspaceTabs from '$lib/AgentWorkspaceTabs.svelte';

  // Agent Tokens page — read + is_active toggle for every token in the
  // grammar_tokens table. (The DB table and backend class keep the
  // compiler-theory name "grammar" because that's accurate; the UI calls
  // it "Tokens" because that's what this page actually shows.) System
  // tokens are toggle-only; custom tokens get full CRUD via the form below.

  /** @type {{id:number, grammar_kind:string, token_kind:string, token:string,
   *          value_type:string|null, units:string|null, description:string,
   *          resolver:string|null, params_schema:object|null,
   *          enum_values:any[]|null, template_body:string|null,
   *          is_system:boolean, is_active:boolean}[]} */
  let tokens     = $state([]);
  let loading    = $state(true);
  let error      = $state('');
  let reloading  = $state(false);
  let activeTab  = $state(/** @type {'condition'|'notify'|'action'} */('condition'));
  let expandedId = $state(/** @type {number|null} */(null));

  async function load() {
    loading = true; error = '';
    try {
      tokens = await fetchGrammarTokens();
    } catch (e) { error = e.message || 'Failed to load'; tokens = []; }
    loading = false;
  }

  async function toggle(id, currentActive) {
    try {
      const updated = await patchGrammarToken(id, { is_active: !currentActive });
      const idx = tokens.findIndex(t => t.id === id);
      if (idx >= 0) tokens[idx] = updated;
    } catch (e) { error = e.message || 'Toggle failed'; }
  }

  async function doReload() {
    reloading = true; error = '';
    try { await reloadGrammarRegistry(); }
    catch (e) { error = e.message || 'Reload failed'; }
    reloading = false;
  }

  // ── Create / edit custom token ───────────────────────────────────────────
  let showForm   = $state(false);
  let editingId  = $state(/** @type {number|null} */(null));
  let formError  = $state('');
  let submitting = $state(false);
  let form       = $state({
    grammar_kind:  'condition',
    token_kind:    'metric',
    token:         '',
    value_type:    'number',
    units:         '',
    description:   '',
    resolver:      '',
    params_schema_json: '',
    enum_values_json:   '',
    template_body:  '',
    is_active:      true,
  });

  function resetForm() {
    form = {
      grammar_kind:  activeTab,
      token_kind:    activeTab === 'condition' ? 'metric'
                    : activeTab === 'notify'  ? 'channel' : 'action_type',
      token:         '',
      value_type:    activeTab === 'condition' ? 'number' : 'enum',
      units:         '',
      description:   '',
      resolver:      '',
      params_schema_json: '',
      enum_values_json:   '',
      template_body:  '',
      is_active:      true,
    };
    formError = '';
    editingId = null;
  }

  function openCreate() { resetForm(); showForm = true; }

  function openEdit(t) {
    form = {
      grammar_kind:  t.grammar_kind,
      token_kind:    t.token_kind,
      token:         t.token,
      value_type:    t.value_type ?? '',
      units:         t.units ?? '',
      description:   t.description ?? '',
      resolver:      t.resolver ?? '',
      params_schema_json: t.params_schema ? JSON.stringify(t.params_schema, null, 2) : '',
      enum_values_json:   t.enum_values   ? JSON.stringify(t.enum_values)           : '',
      template_body:  t.template_body ?? '',
      is_active:      t.is_active,
    };
    formError = '';
    editingId = t.id;
    showForm = true;
  }

  function closeForm() { showForm = false; formError = ''; }

  async function submitForm() {
    formError = ''; submitting = true;
    // Parse JSON fields
    let parsed_params = null, parsed_enum = null;
    try {
      if (form.params_schema_json.trim()) parsed_params = JSON.parse(form.params_schema_json);
    } catch (e) { formError = `params_schema JSON invalid: ${e.message}`; submitting = false; return; }
    try {
      if (form.enum_values_json.trim()) parsed_enum = JSON.parse(form.enum_values_json);
    } catch (e) { formError = `enum_values JSON invalid: ${e.message}`; submitting = false; return; }

    const payload = {
      value_type:    form.value_type || null,
      units:         form.units || null,
      description:   form.description,
      resolver:      form.resolver || null,
      params_schema: parsed_params,
      enum_values:   parsed_enum,
      template_body: form.template_body || null,
      is_active:     !!form.is_active,
    };
    try {
      if (editingId == null) {
        payload.grammar_kind = form.grammar_kind;
        payload.token_kind   = form.token_kind;
        payload.token        = form.token;
        if (!payload.token) { formError = 'Token name required'; submitting = false; return; }
        await createGrammarToken(payload);
      } else {
        await patchGrammarToken(editingId, payload);
      }
      showForm = false;
      await load();
    } catch (e) {
      formError = e.message || 'Save failed';
    }
    submitting = false;
  }

  async function doDelete(t) {
    if (t.is_system) return;  // backend blocks; UI too
    if (!confirm(`Delete custom token "${t.token}"?`)) return;
    try { await deleteGrammarToken(t.id); await load(); }
    catch (e) { error = e.message || 'Delete failed'; }
  }

  function filtered() {
    return tokens.filter(t => t.grammar_kind === activeTab);
  }

  function tokenCount(kind) {
    return tokens.filter(t => t.grammar_kind === kind).length;
  }

  onMount(() => {
    load();
  });
</script>

<svelte:head><title>Tokens | RamboQuant Analytics</title></svelte:head>

<AgentWorkspaceTabs />

<div class="algo-status-card p-4 mb-3" data-status="inactive">
  <div class="flex items-center justify-between mb-2 gap-2 flex-wrap">
    <h1 class="text-sm font-bold uppercase tracking-wider text-[#fbbf24]">
      Agent Tokens
    </h1>
    <InfoHint popup text="Grammar tokens: extend the agent language. <b>Condition</b> tokens (metric / scope / op), <b>notify</b> tokens (channel / template), and <b>action</b> tokens (place_order, set_flag…). System tokens toggle-only; custom tokens full CRUD." />
    <span class="algo-ts ml-auto">{$nowStamp}</span>
    <div class="flex gap-2">
      {#if !isDemo}
        <button onclick={openCreate}
          class="text-[0.65rem] py-1 px-3 rounded border border-emerald-500/50 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25 font-semibold">
          + New token
        </button>
        <button onclick={doReload} disabled={reloading}
          class="text-[0.65rem] py-1 px-3 rounded border border-[#fbbf24]/50 bg-[#fbbf24]/15 text-[#fbbf24] hover:bg-[#fbbf24]/25 font-semibold disabled:opacity-50">
          {reloading ? 'Reloading…' : 'Reload registry'}
        </button>
      {/if}
    </div>
  </div>
  {#if isDemo}
    <!-- Demo read-only banner. The catalog is shown verbatim — operators
         can browse every metric / scope / op / action handler the engine
         supports — but creates / edits / deletes / registry reload hide
         since the backend rejects them with admin_guard. -->
    <div class="mt-2 p-2 rounded bg-purple-500/10 border border-purple-500/30 text-[0.65rem] text-purple-200">
      <strong class="text-purple-100">Read-only in demo.</strong>
      Browse every condition / notify / action token the engine supports.
      The catalog drives every alerting + automation rule — see
      <a href="/showcase" class="underline hover:text-purple-50">the tour</a>
      for context.
    </div>
  {/if}
</div>

{#if error}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
{/if}

<!-- Create / edit form (shown when showForm is true) -->
{#if showForm}
  <div class="algo-status-card p-4 mb-3" data-status="running">
    <div class="flex items-center justify-between mb-3">
      <h3 class="text-xs font-bold uppercase tracking-wider text-[#fbbf24]">
        {editingId == null ? 'New token' : `Edit token #${editingId}`}
      </h3>
      <button onclick={closeForm} class="text-xs text-[#7e97b8] hover:text-[#fbbf24]">Cancel</button>
    </div>

    {#if formError}
      <div class="mb-2 p-1.5 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{formError}</div>
    {/if}

    <div class="grid grid-cols-2 md:grid-cols-4 gap-2">
      <div>
        <label class="field-label">Category</label>
        <Select ariaLabel="Category" bind:value={form.grammar_kind}
          disabled={editingId != null}
          options={[
            { value: 'condition', label: 'condition' },
            { value: 'notify',    label: 'notify' },
            { value: 'action',    label: 'action' },
          ]} />
      </div>
      <div>
        <label class="field-label">Token kind</label>
        {#if form.grammar_kind === 'condition'}
          <Select ariaLabel="Token kind" bind:value={form.token_kind}
            disabled={editingId != null}
            options={[
              { value: 'metric',   label: 'metric' },
              { value: 'scope',    label: 'scope' },
              { value: 'operator', label: 'operator' },
            ]} />
        {:else if form.grammar_kind === 'notify'}
          <Select ariaLabel="Token kind" bind:value={form.token_kind}
            disabled={editingId != null}
            options={[
              { value: 'channel',  label: 'channel' },
              { value: 'format',   label: 'format' },
              { value: 'template', label: 'template' },
            ]} />
        {:else}
          <Select ariaLabel="Token kind" bind:value={form.token_kind}
            disabled={editingId != null}
            options={[{ value: 'action_type', label: 'action_type' }]} />
        {/if}
      </div>
      <div>
        <label class="field-label">Token name</label>
        <input bind:value={form.token} disabled={editingId != null} class="field-input" placeholder="e.g. pnl_rate_abs" />
      </div>
      <div>
        <label class="field-label">Value type</label>
        <Select ariaLabel="Value type" bind:value={form.value_type}
          options={[
            { value: '',        label: '—' },
            { value: 'number',  label: 'number' },
            { value: 'string',  label: 'string' },
            { value: 'boolean', label: 'boolean' },
            { value: 'enum',    label: 'enum' },
            { value: 'array',   label: 'array' },
            { value: 'object',  label: 'object' },
            { value: 'void',    label: 'void' },
          ]} />
      </div>
      <div>
        <label class="field-label">Units</label>
        <input bind:value={form.units} class="field-input" placeholder="e.g. ₹  or %/min" />
      </div>
      <div class="col-span-2 md:col-span-3">
        <label class="field-label">Description</label>
        <input bind:value={form.description} class="field-input" />
      </div>
      <div class="col-span-2 md:col-span-4">
        <label class="field-label">Resolver (python dotted path)</label>
        <input bind:value={form.resolver} class="field-input font-mono text-[0.65rem]"
               placeholder="backend.api.algo.grammar._metric_pnl" />
      </div>
      <div class="col-span-2">
        <label class="field-label">params_schema (JSON)</label>
        <textarea bind:value={form.params_schema_json} class="field-input font-mono text-[0.6rem]" rows="5"
                  placeholder={'{"account": {"type": "string", "required": true}}'}></textarea>
      </div>
      <div class="col-span-2">
        <label class="field-label">enum_values (JSON array)</label>
        <textarea bind:value={form.enum_values_json} class="field-input font-mono text-[0.6rem]" rows="5"
                  placeholder='["BUY","SELL"]'></textarea>
      </div>
      <div class="col-span-2 md:col-span-4">
        <label class="field-label">Template body (for notify.template tokens)</label>
        <textarea bind:value={form.template_body} class="field-input font-mono text-[0.6rem]" rows="4"
                  placeholder="Use dollar-brace placeholders like timestamp and row_lines"></textarea>
      </div>
      <div class="flex items-center gap-2">
        <input type="checkbox" bind:checked={form.is_active} id="is_active" />
        <label for="is_active" class="text-[0.65rem] text-[#c8d8f0]">Active</label>
      </div>
    </div>

    <div class="flex gap-2 mt-3">
      <button onclick={submitForm} disabled={submitting}
        class="text-[0.65rem] py-1 px-4 rounded border border-emerald-500/50 bg-emerald-500/20 text-emerald-300 hover:bg-emerald-500/30 font-semibold disabled:opacity-50">
        {submitting ? 'Saving…' : (editingId == null ? 'Create' : 'Save')}
      </button>
    </div>
  </div>
{/if}

<!-- Tab row -->
<div class="flex gap-1 mb-3">
  {#each /** @type {['condition'|'notify'|'action', string][]} */([['condition', 'Condition'], ['notify', 'Notify'], ['action', 'Action']]) as [key, label]}
    <button onclick={() => { activeTab = key; expandedId = null; }}
      class="px-3 py-1 text-xs font-medium border-b-2 transition-colors
        {activeTab === key
          ? 'border-[#fbbf24] text-[#fbbf24]'
          : 'border-transparent text-[#b4c8e6] hover:text-[#fbbf24]'}">
      {label}
      <span class="ml-1 text-[0.55rem] opacity-70">({tokenCount(key)})</span>
    </button>
  {/each}
</div>

{#if loading}
  <div class="text-center text-[#7e97b8] text-xs animate-pulse py-6">Loading tokens…</div>
{:else if !filtered().length}
  <div class="text-center text-[#7e97b8] text-xs py-6">No tokens in this category.</div>
{:else}
  <div class="algo-status-card p-0 overflow-hidden" data-status="inactive">
    <table class="w-full text-[0.65rem]">
      <thead>
        <tr class="bg-[#0a1020] text-[#fbbf24]">
          <th class="text-left py-1.5 px-2">Kind</th>
          <th class="text-left py-1.5 px-2">Token</th>
          <th class="text-left py-1.5 px-2">Value</th>
          <th class="text-left py-1.5 px-2">Units</th>
          <th class="text-left py-1.5 px-2">Description</th>
          <th class="text-left py-1.5 px-2">Origin</th>
          <th class="text-left py-1.5 px-2">Active</th>
        </tr>
      </thead>
      <tbody>
        {#each filtered() as t}
          <tr class="border-t border-white/5 hover:bg-white/5 cursor-pointer"
              onclick={() => expandedId = expandedId === t.id ? null : t.id}>
            <td class="py-1.5 px-2 text-[#7e97b8] font-mono uppercase text-[0.55rem]">{t.token_kind}</td>
            <td class="py-1.5 px-2 font-mono text-[#fbbf24]">{t.token}</td>
            <td class="py-1.5 px-2 text-[#c8d8f0]">{t.value_type ?? '—'}</td>
            <td class="py-1.5 px-2 text-[#c8d8f0]">{t.units ?? '—'}</td>
            <td class="py-1.5 px-2 text-[#c8d8f0]/80 text-[0.6rem] max-w-[360px] truncate"
                title={t.description}>{t.description || '—'}</td>
            <td class="py-1.5 px-2">
              {#if t.is_system}
                <span class="px-1.5 py-0.5 rounded bg-slate-500/20 text-slate-300 text-[0.55rem] font-semibold uppercase border border-slate-500/40">System</span>
              {:else}
                <span class="px-1.5 py-0.5 rounded bg-emerald-500/15 text-emerald-300 text-[0.55rem] font-semibold uppercase border border-emerald-500/40">Custom</span>
              {/if}
            </td>
            <td class="py-1.5 px-2" onclick={(e) => e.stopPropagation()}>
              {#if isDemo}
                <span class="text-[0.6rem] px-2 py-0.5 rounded font-medium border
                  {t.is_active
                    ? 'bg-green-500/10 text-green-400/70 border-green-500/30'
                    : 'bg-slate-700/30 text-slate-400/70 border-slate-500/25'}"
                  title="Read-only in demo">
                  {t.is_active ? 'ON' : 'OFF'}
                </span>
              {:else}
                <button onclick={() => toggle(t.id, t.is_active)}
                  class="text-[0.6rem] px-2 py-0.5 rounded font-medium border
                    {t.is_active
                      ? 'bg-green-500/15 text-green-400 border-green-500/40'
                      : 'bg-slate-700/40 text-slate-400 border-slate-500/30'}">
                  {t.is_active ? 'ON' : 'OFF'}
                </button>
              {/if}
            </td>
          </tr>
          {#if expandedId === t.id}
            <tr class="bg-[#0a1020]">
              <td colspan="7" class="py-2 px-3 text-[0.6rem] text-[#c8d8f0]/80">
                <div class="grid grid-cols-2 gap-x-6 gap-y-1">
                  {#if t.resolver}
                    <div><span class="text-[#7e97b8]">Resolver:</span> <span class="font-mono">{t.resolver}</span></div>
                  {/if}
                  {#if t.params_schema}
                    <div class="col-span-2">
                      <div class="text-[#7e97b8] mb-0.5">Params schema</div>
                      <pre class="text-[0.55rem] bg-black/30 p-2 rounded overflow-x-auto">{JSON.stringify(t.params_schema, null, 2)}</pre>
                    </div>
                  {/if}
                  {#if t.enum_values}
                    <div class="col-span-2">
                      <span class="text-[#7e97b8]">Enum values:</span> {JSON.stringify(t.enum_values)}
                    </div>
                  {/if}
                  {#if t.template_body}
                    <div class="col-span-2">
                      <div class="text-[#7e97b8] mb-0.5">Template body</div>
                      <pre class="text-[0.55rem] bg-black/30 p-2 rounded whitespace-pre-wrap">{t.template_body}</pre>
                    </div>
                  {/if}
                  <div class="col-span-2 flex gap-2 mt-1 pt-1 border-t border-white/5">
                    {#if isDemo}
                      <span class="text-[#7e97b8] text-[0.55rem] italic">Read-only in demo.</span>
                    {:else if t.is_system}
                      <span class="text-[#7e97b8] text-[0.55rem] italic">System tokens edit only via the toggle above.</span>
                    {:else}
                      <button onclick={() => openEdit(t)}
                        class="text-[0.6rem] px-2 py-0.5 rounded border border-[#fbbf24]/50 text-[#fbbf24] hover:bg-[#fbbf24]/15">Edit</button>
                      <button onclick={() => doDelete(t)}
                        class="text-[0.6rem] px-2 py-0.5 rounded border border-red-500/50 text-red-300 hover:bg-red-500/15">Delete</button>
                    {/if}
                  </div>
                </div>
              </td>
            </tr>
          {/if}
        {/each}
      </tbody>
    </table>
  </div>
{/if}
