<!--
  /automation/templates — Order Template management.

  Templates are the per-order exit-rule presets the operator picks at
  OrderTicket submit time. Each row carries a TP %, SL %, and Wing
  config (premium % OR strike offset) that translate to a broker-native
  GTT + spread basket leg at submit.

  Operator workflow:
    - Browse system + custom templates
    - Filter by applies_to scope (Buy / Sell-Option / Both)
    - Click a row to expand → numeric fields + description
    - Edit any field → Save → next OrderTicket pick uses the new values
    - Toggle is_default per scope so OrderTicket pre-selects it
    - Custom templates have full delete; system templates are toggle-only

  Industry analogue: NinjaTrader ATM Strategy library, MetaTrader trade
  templates, ThinkOrSwim Order Templates.
-->
<script>
  import { onMount } from 'svelte';
  import { authStore, nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import DisclosureChevron from '$lib/DisclosureChevron.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import Select from '$lib/Select.svelte';
  import {
    createOrderTemplate,
    patchOrderTemplate,
    deleteOrderTemplate,
  } from '$lib/api';
  import { loadOrderTemplates, reloadOrderTemplates } from '$lib/data/templates';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { fmtPctScaled } from '$lib/format';

  let templates = $state(/** @type {any[]} */ ([]));
  let _showLiveTs = $state(false);
  let loading = $state(true);
  let error = $state('');

  // Filter: 'all' | 'buy_any' | 'sell_option' | 'both'
  let filterScope = $state('all');
  let expandedId = $state(/** @type {number | null} */ (null));

  // Inline edit form state (separate from create form so they don't fight)
  let editingId = $state(/** @type {number | null} */ (null));
  let formName = $state('');
  let formDescription = $state('');
  let formAppliesTo = $state('both');
  let formTpPct = $state('');
  let formSlPct = $state('');
  let formWingPremPct = $state('');
  let formWingStrikeOffset = $state('');
  let formTpOrderType = $state(/** @type {'LIMIT'|'MARKET'} */ ('LIMIT'));
  let formTpScalesJson = $state('');
  const _scalesParseErr = $derived.by(() => {
    if (!formTpScalesJson?.trim()) return '';
    try { JSON.parse(formTpScalesJson); return ''; }
    catch (e) { return e.message; }
  });
  let formSlTrailPct = $state('');
  let formIsDefault = $state(false);
  let formIsActive = $state(true);
  let formError = $state('');
  let busy = $state(false);

  // Create-new toggle
  let creatingNew = $state(false);
  let _colTemplates = $state(false);
  let _fsTemplates  = $state(false);

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);

  const isDemo = $derived(!$authStore.user);

  async function load() {
    loading = true; error = '';
    try {
      // First open uses the module-level cache; the explicit reload
      // after a CRUD mutation re-hits the API + broadcasts to other
      // open modals via the store.
      templates = await loadOrderTemplates();
    } catch (e) {
      error = e.message || 'failed to load templates';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  const visible = $derived(
    filterScope === 'all'
      ? templates
      : templates.filter(t => t.applies_to === filterScope)
  );

  // System first, then custom; default templates within each group rise.
  const sorted = $derived(
    [...visible].sort((a, b) => {
      if (a.is_system !== b.is_system) return a.is_system ? -1 : 1;
      if (a.is_default !== b.is_default) return a.is_default ? -1 : 1;
      return a.name.localeCompare(b.name);
    })
  );

  function resetForm() {
    editingId = null;
    creatingNew = false;
    formName = '';
    formDescription = '';
    formAppliesTo = 'both';
    formTpPct = '';
    formSlPct = '';
    formWingPremPct = '';
    formWingStrikeOffset = '';
    formTpOrderType = 'LIMIT';
    formTpScalesJson = '';
    formSlTrailPct = '';
    formIsDefault = false;
    formIsActive = true;
    formError = '';
  }

  function startEdit(/** @type {any} */ t) {
    expandedId = t.id;
    editingId = t.id;
    creatingNew = false;
    formName = t.name;
    formDescription = t.description || '';
    formAppliesTo = t.applies_to;
    formTpPct = t.tp_pct != null ? String(t.tp_pct) : '';
    formSlPct = t.sl_pct != null ? String(t.sl_pct) : '';
    formWingPremPct = t.wing_premium_pct != null ? String(t.wing_premium_pct) : '';
    formWingStrikeOffset = t.wing_strike_offset != null ? String(t.wing_strike_offset) : '';
    formTpOrderType = /** @type {'LIMIT'|'MARKET'} */ (t.tp_order_type || 'LIMIT');
    formTpScalesJson = t.tp_scales_json || '';
    formSlTrailPct = t.sl_trail_pct != null ? String(t.sl_trail_pct) : '';
    formIsDefault = !!t.is_default;
    formIsActive = !!t.is_active;
    formError = '';
  }

  function startCreate() {
    resetForm();
    creatingNew = true;
    expandedId = null;
    setTimeout(() => {
      document.getElementById('tpl-create-form')?.scrollIntoView({
        behavior: 'smooth', block: 'center',
      });
    }, 30);
  }

  function _parseNumOrNull(/** @type {string} */ s) {
    const trimmed = s.trim();
    if (trimmed === '') return null;
    const n = Number(trimmed);
    if (!isFinite(n)) throw new Error('invalid number');
    return n;
  }

  function _payloadFromForm() {
    return {
      name:               formName.trim(),
      description:        formDescription,
      applies_to:         formAppliesTo,
      tp_pct:             _parseNumOrNull(formTpPct),
      sl_pct:             _parseNumOrNull(formSlPct),
      wing_premium_pct:   _parseNumOrNull(formWingPremPct),
      wing_strike_offset: _parseNumOrNull(formWingStrikeOffset),
      tp_order_type:      formTpOrderType,
      tp_scales_json:     formTpScalesJson.trim() === '' ? null : formTpScalesJson.trim(),
      sl_trail_pct:       _parseNumOrNull(formSlTrailPct),
      is_default:         formIsDefault,
      is_active:          formIsActive,
    };
  }

  async function saveEdit(/** @type {any} */ t) {
    if (busy) return;
    busy = true; formError = '';
    try {
      const payload = _payloadFromForm();
      await patchOrderTemplate(t.id, payload);
      templates = await reloadOrderTemplates();
      toast.success(`Template saved: ${formName}`);
      resetForm();
    } catch (e) {
      formError = e.message || 'save failed';
      toast.error(`Save failed: ${e.message || 'unknown error'}`);
    } finally {
      busy = false;
    }
  }

  async function saveCreate() {
    if (busy) return;
    if (!formName.trim()) { formError = 'name required'; return; }
    busy = true; formError = '';
    try {
      const payload = _payloadFromForm();
      // tp_pct + sl_pct + wing_* default to null when blank — that's fine
      await createOrderTemplate(payload);
      templates = await reloadOrderTemplates();
      toast.success(`Template created: ${formName}`);
      resetForm();
    } catch (e) {
      formError = e.message || 'create failed';
      toast.error(`Create failed: ${e.message || 'unknown error'}`);
    } finally {
      busy = false;
    }
  }

  async function confirmDelete(/** @type {any} */ t) {
    if (!_confirmRef) return;
    const ok = await _confirmRef.ask({
      title: `Delete ${t.name}?`,
      message: 'The template will be removed permanently. Existing orders that used it are not affected.',
      confirmLabel: 'Delete',
      cancelLabel: 'Cancel',
      destructive: true,
    });
    if (!ok) return;
    try {
      await deleteOrderTemplate(t.id);
      templates = await reloadOrderTemplates();
      toast.success(`Template deleted: ${t.name}`);
    } catch (e) {
      toast.error(`Delete failed: ${e.message || 'unknown error'}`);
    }
  }

  // Template TP/SL percentages are stored as already-scaled values
  // (e.g. 1.5 = 1.5%). Use *Scaled with signed=true so positive
  // values render with a leading `+` — important here because the
  // template list is monochrome (no color cue for sign).
  function fmtPct(v) { return fmtPctScaled(v, 1, true); }
  function fmtOffset(v) { return v == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${v}`; }
  function appliesLabel(v) {
    if (v === 'buy_any')     return 'BUY EQ/FUT';
    if (v === 'buy_option')  return 'BUY OPT';
    if (v === 'sell_any')    return 'SELL EQ/FUT';
    if (v === 'sell_option') return 'SELL OPT';
    return 'BOTH';
  }
  // The 4 side-aware default scopes. Used by the filter chips, the
  // form selects, and the "defaults matrix" header card so the page's
  // scope vocabulary is one canonical list. `both` is intentionally
  // NOT in this list — it's a write-time choice (a custom template
  // that targets every direction), not a Default slot the platform
  // resolves to.
  const _SCOPES = [
    { value: 'buy_any',     label: 'BUY EQ/FUT' },
    { value: 'buy_option',  label: 'BUY OPT' },
    { value: 'sell_any',    label: 'SELL EQ/FUT' },
    { value: 'sell_option', label: 'SELL OPT' },
  ];
  // For each scope, the active is_default template (or null when no
  // seed claims that slot). Drives the matrix card above the list so
  // the operator sees at a glance which scopes are covered.
  const _defaultByScope = $derived.by(() => {
    const out = /** @type {Record<string, any>} */ ({});
    for (const s of _SCOPES) {
      out[s.value] = templates.find(t =>
        t.is_default && t.is_active && t.applies_to === s.value
      ) || null;
    }
    return out;
  });
</script>

<svelte:head><title>Order Templates | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Templates</h1>
    <InfoHint popup align="right" text="<b>Order templates</b> are reusable exit-rule presets you pick at order entry — TP %, SL %, and (for SELL options) a protective wing leg. The selected template translates to a broker-native GTT for TP/SL and a paired basket order for the wing. Edit a template here and every future order using it inherits the new values; bulk-apply lets you push the change to open positions too." />
  </span>
  <span class="algo-ts-group">
    <span class="algo-ts"
          class:algo-ts-hidden={!!$lastRefreshAt && _showLiveTs}
          class:algo-ts-pulse={!$lastRefreshAt}
          onclick={() => { if ($lastRefreshAt) _showLiveTs = !_showLiveTs; }}
          title={$lastRefreshAt ? 'Live clock — tap to switch' : 'Live clock'}
          role="button" tabindex="0"
          onkeydown={(e) => { if ($lastRefreshAt && e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {$nowStamp}
    </span>
    {#if $lastRefreshAt}
      <span class="algo-ts-vsep" aria-hidden="true">|</span>
      <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}
            onclick={() => _showLiveTs = !_showLiveTs}
            title="Last refresh — tap to switch" role="button" tabindex="0"
            onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
        {formatDualTz($lastRefreshAt)}
      </span>
    {/if}
  </span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="Templates" />
    <PageHeaderActions />
  </span>
</div>

<AutomationTabs />

{#if isDemo}
  <div class="algo-card mb-3">
    <p class="text-xs text-slate-300">
      Demo mode — system templates visible (read-only); create / edit / delete require sign-in.
    </p>
  </div>
{/if}

<!-- Defaults matrix — at-a-glance view of which of the 4 side-scopes
     have an is_default template seeded. The Default pill in the order
     modal resolves to one of these per (side × instrument-type) combo.
     Click a cell to filter the list to that scope below + jump to the
     active row when one exists. -->
<section class="bucket-card p-3 mb-3">
  <div class="tpl-matrix-head">
    <span class="mp-section-label">Side-default coverage</span>
    <InfoHint popup={true} align="right"
      text="Each (BUY/SELL) × (EQ-FUT / OPTION) combo resolves to one is_default template. The order modal's <b>Default</b> pill picks the right one per leg automatically. ✓ = scope covered; — = unclaimed (Default falls back to None on that scope)." />
  </div>
  <div class="tpl-matrix">
    {#each _SCOPES as s}
      {@const def = _defaultByScope[s.value]}
      <button class="tpl-matrix-cell {def ? 'tpl-matrix-cell-on' : ''}"
              onclick={() => {
                filterScope = s.value;
                if (def) expandedId = def.id;
              }}
              title={def
                ? `${def.name} — TP ${fmtPct(def.tp_pct)} · SL ${fmtPct(def.sl_pct)}`
                : `No default template seeded for ${s.label}`}
              type="button">
        <span class="tpl-matrix-scope">{s.label}</span>
        <span class="tpl-matrix-tpl">
          {def ? def.name : '— unclaimed'}
        </span>
        <span class="tpl-matrix-mark">{def ? '✓' : '—'}</span>
      </button>
    {/each}
  </div>
</section>

<!-- Filter strip + create button -->
<section class="bucket-card p-3 mb-3"
  class:fs-card-on={_fsTemplates}
  class:is-collapsed={_colTemplates}>
  <CardHeader
    bind:isCollapsed={_colTemplates}
    bind:isFullscreen={_fsTemplates}
    cardId="automation-templates"
    label="Templates"
    onRefresh={load}
    bind:refreshLoading={loading}
    showSearch={false}
  >
    {#snippet middle()}
      <div style="min-width:0; display:flex; align-items:center; gap:0.25rem; flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none;">
        <span class="mp-section-label">Filter:</span>
        <button class="tpl-chip {filterScope === 'all' ? 'tpl-chip-on' : ''}"
                onclick={() => { filterScope = 'all'; }} type="button">All</button>
        {#each _SCOPES as s}
          <button
            class="tpl-chip {filterScope === s.value ? 'tpl-chip-on' : ''}"
            onclick={() => { filterScope = s.value; }}
            type="button"
          >{s.label}</button>
        {/each}
        <button class="tpl-chip {filterScope === 'both' ? 'tpl-chip-on' : ''}"
                onclick={() => { filterScope = 'both'; }} type="button"
                title="Custom templates that target every direction">Both</button>
        {#if !isDemo}
          <button class="tpl-create-btn" onclick={startCreate} type="button">
            + Create custom template
          </button>
        {/if}
      </div>
    {/snippet}
  </CardHeader>
  <div class="card-body" hidden={_colTemplates}>

    {#if error}
      <div class="algo-status-card bg-red-900/30 border-red-700/50 p-3 mb-3">
        <p class="text-xs text-red-300">{error}</p>
      </div>
    {/if}

    {#if loading}
      <div class="p-3 text-xs text-slate-400">Loading…</div>
    {:else if sorted.length === 0}
      <div class="p-3 text-xs text-slate-400">
        No templates match the current filter.
      </div>
    {:else}
      <section class="tpl-list">
    {#each sorted as t (t.id)}
      <div class="tpl-row {t.is_default ? 'tpl-row-default' : ''}">
        <button
          class="tpl-row-head"
          onclick={() => { expandedId = expandedId === t.id ? null : t.id; if (editingId !== t.id) editingId = null; }}
          type="button"
        >
          <DisclosureChevron open={expandedId === t.id} ariaLabel="Toggle details" />
          <span class="tpl-name">{t.name}</span>

          {#if t.is_default}
            <span class="tpl-badge tpl-badge-default">DEFAULT</span>
          {/if}
          {#if t.is_system}
            <span class="tpl-badge tpl-badge-system">SYSTEM</span>
          {/if}
          {#if !t.is_active}
            <span class="tpl-badge tpl-badge-inactive">INACTIVE</span>
          {/if}

          <span class="tpl-applies">{appliesLabel(t.applies_to)}</span>

          <span class="tpl-numerics">
            {#if t.tp_scales_json}
              {@const _scales = (() => { try { return JSON.parse(t.tp_scales_json) || []; } catch { return []; } })()}
              <span class="tpl-num tpl-num-tp" title={t.tp_scales_json}>TP scale × {_scales.length}{t.tp_order_type === 'MARKET' ? ' MKT' : ''}</span>
            {:else}
              <span class="tpl-num tpl-num-tp">TP {fmtPct(t.tp_pct)}{t.tp_pct != null && t.tp_order_type === 'MARKET' ? ' MKT' : ''}</span>
            {/if}
            <span class="tpl-num tpl-num-sl">SL {fmtPct(t.sl_pct)}{t.sl_trail_pct != null ? ' trail ' + fmtPct(t.sl_trail_pct) : ''}</span>
            {#if t.wing_strike_offset != null}
              <span class="tpl-num tpl-num-wing">Wing {fmtOffset(t.wing_strike_offset)} strike</span>
            {:else if t.wing_premium_pct != null}
              <span class="tpl-num tpl-num-wing">Wing {fmtPct(t.wing_premium_pct)} prem</span>
            {/if}
          </span>
        </button>

        {#if expandedId === t.id}
          <div class="tpl-body">
            {#if t.description}
              <p class="tpl-desc">{t.description}</p>
            {/if}

            {#if editingId === t.id}
              <!-- Edit form -->
              <div class="tpl-form">
                <label class="tpl-field">
                  <span>Name</span>
                  <input type="text" bind:value={formName} disabled={t.is_system}
                         class="tpl-input" />
                </label>
                <label class="tpl-field">
                  <span>Applies to</span>
                  <Select
                    options={[
                      { value: 'buy_any',     label: 'BUY EQ / FUT' },
                      { value: 'buy_option',  label: 'BUY Option (CE / PE)' },
                      { value: 'sell_any',    label: 'SELL EQ / FUT' },
                      { value: 'sell_option', label: 'SELL Option (CE / PE)' },
                      { value: 'both',        label: 'Both (every direction)' },
                    ]}
                    bind:value={formAppliesTo}
                    disabled={t.is_system}
                  />
                </label>
                <label class="tpl-field tpl-field-full">
                  <span>Description</span>
                  <textarea bind:value={formDescription} rows="2"
                            disabled={t.is_system}
                            class="tpl-input"></textarea>
                </label>
                <label class="tpl-field">
                  <span>TP % (blank = none)</span>
                  <input type="number" step="0.5" bind:value={formTpPct}
                         placeholder="e.g. 30" class="tpl-input" />
                </label>
                <label class="tpl-field">
                  <span>SL % (blank = none)</span>
                  <input type="number" step="0.5" bind:value={formSlPct}
                         placeholder="e.g. 20" class="tpl-input" />
                </label>
                <label class="tpl-field">
                  <span>Wing premium % (Sell-Option)</span>
                  <input type="number" step="0.5" bind:value={formWingPremPct}
                         placeholder="e.g. 10" class="tpl-input" />
                </label>
                <label class="tpl-field">
                  <span>Wing strike offset (alternative)</span>
                  <input type="number" step="50" bind:value={formWingStrikeOffset}
                         placeholder="e.g. 500" class="tpl-input" />
                </label>
                <label class="tpl-field">
                  <span>TP order type</span>
                  <Select
                    bind:value={formTpOrderType}
                    options={[
                      { value: 'LIMIT',  label: 'LIMIT — quote at trigger price (slip-protected)' },
                      { value: 'MARKET', label: 'MARKET — take book at trigger (sure fill, no slip cap)' },
                    ]} />
                </label>
                <label class="tpl-field tpl-field-wide">
                  <span>TP scale-out (JSON)
                    <InfoHint popup={true} align="right" label="?"
                      text={'Scale-out ladder. JSON list of at_pct + close_pct entries. Example: [{"at_pct": 30, "close_pct": 50}, {"at_pct": 60, "close_pct": 50}] — close 50 % of the position at +30 %, the rest at +60 %. Sum of close_pct must be ≤ 100. When set, supersedes the single TP %.'} />
                  </span>
                  <textarea bind:value={formTpScalesJson}
                            rows="3"
                            class="tpl-input tpl-input-mono"
                            placeholder={'[{"at_pct": 30, "close_pct": 50}]'}></textarea>
                  {#if _scalesParseErr}
                    <span class="tpl-json-err">{_scalesParseErr}</span>
                  {/if}
                </label>
                <label class="tpl-field">
                  <span>Trailing stop % (blank = none)
                    <InfoHint popup={true} align="right" label="?"
                      text="When set, the background _task_trail_stop poller ratchets the attached SL GTT's trigger toward the favorable side of LTP every templates.trail_poll_interval_seconds. New trigger = peak × (1 − trail/100) for longs, trough × (1 + trail/100) for shorts. Trigger only moves favorably — locks in profits as the position runs. Industry standard trailing stop (NinjaTrader Trail, IBKR Trailing Stop). Kite-only today; Dhan + Groww silently skipped." />
                  </span>
                  <input type="number" step="0.5" bind:value={formSlTrailPct}
                         placeholder="e.g. 10" class="tpl-input" />
                </label>
                <label class="tpl-checkbox">
                  <input type="checkbox" bind:checked={formIsDefault} />
                  <span>Default for this scope</span>
                </label>
                <label class="tpl-checkbox">
                  <input type="checkbox" bind:checked={formIsActive} />
                  <span>Active</span>
                </label>

                {#if formError}
                  <div class="tpl-form-err">{formError}</div>
                {/if}

                <div class="tpl-form-actions">
                  <button class="tpl-btn tpl-btn-primary"
                          disabled={busy || !!_scalesParseErr}
                          onclick={() => saveEdit(t)}
                          type="button">
                    {busy ? 'Saving…' : 'Save'}
                  </button>
                  <button class="tpl-btn"
                          onclick={resetForm} type="button">
                    Cancel
                  </button>
                  {#if !t.is_system}
                    <button class="tpl-btn tpl-btn-danger"
                            onclick={() => confirmDelete(t)}
                            disabled={busy}
                            type="button">
                      Delete
                    </button>
                  {/if}
                </div>
              </div>
            {:else if !isDemo}
              <button class="tpl-btn tpl-btn-primary"
                      onclick={() => startEdit(t)} type="button">
                Edit
              </button>
            {/if}
          </div>
        {/if}
      </div>
    {/each}
      </section>
    {/if}
  </div>
</section>

<!-- Create-new form (separate from edit so they don't fight over state) -->
{#if creatingNew}
  <section id="tpl-create-form" class="bucket-card p-3 mt-3">
    <h2 class="mp-section-label mb-2">Create custom template</h2>
    <div class="tpl-form">
      <label class="tpl-field">
        <span>Name</span>
        <input type="text" bind:value={formName} class="tpl-input"
               placeholder="e.g. Tight Scalp" />
      </label>
      <label class="tpl-field">
        <span>Applies to</span>
        <Select
          options={[
            { value: 'buy_any',     label: 'BUY EQ / FUT' },
            { value: 'buy_option',  label: 'BUY Option (CE / PE)' },
            { value: 'sell_any',    label: 'SELL EQ / FUT' },
            { value: 'sell_option', label: 'SELL Option (CE / PE)' },
            { value: 'both',        label: 'Both (every direction)' },
          ]}
          bind:value={formAppliesTo}
        />
      </label>
      <label class="tpl-field tpl-field-full">
        <span>Description</span>
        <textarea bind:value={formDescription} rows="2" class="tpl-input"></textarea>
      </label>
      <label class="tpl-field">
        <span>TP % (blank = none)</span>
        <input type="number" step="0.5" bind:value={formTpPct} class="tpl-input" />
      </label>
      <label class="tpl-field">
        <span>SL % (blank = none)</span>
        <input type="number" step="0.5" bind:value={formSlPct} class="tpl-input" />
      </label>
      <label class="tpl-field">
        <span>Wing premium %</span>
        <input type="number" step="0.5" bind:value={formWingPremPct} class="tpl-input" />
      </label>
      <label class="tpl-field">
        <span>Wing strike offset</span>
        <input type="number" step="50" bind:value={formWingStrikeOffset} class="tpl-input" />
      </label>
      <label class="tpl-checkbox">
        <input type="checkbox" bind:checked={formIsDefault} />
        <span>Default for this scope</span>
      </label>

      {#if formError}
        <div class="tpl-form-err">{formError}</div>
      {/if}

      <div class="tpl-form-actions">
        <button class="tpl-btn tpl-btn-primary"
                disabled={busy || !!_scalesParseErr}
                onclick={saveCreate} type="button">
          {busy ? 'Creating…' : 'Create template'}
        </button>
        <button class="tpl-btn" onclick={resetForm} type="button">Cancel</button>
      </div>
    </div>
  </section>
{/if}

<ConfirmModal bind:this={_confirmRef} />

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  /* Defaults coverage matrix — at-a-glance grid of the 4 side-default
     slots. Each cell renders the slot's scope (BUY EQ/FUT, SELL OPT,
     etc.) and the seeded template name, with a ✓/— mark. Cells light
     up amber when the slot is covered; muted when unclaimed. Click
     filters the list + auto-expands the active template's row. */
  .tpl-matrix-head {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    margin-bottom: 0.4rem;
  }
  .tpl-matrix {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
    gap: 0.45rem;
  }
  .tpl-matrix-cell {
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-rows: auto auto;
    gap: 0.1rem 0.45rem;
    align-items: center;
    padding: 0.55rem 0.7rem;
    background: rgba(20, 30, 55, 0.55);
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 5px;
    cursor: pointer;
    text-align: left;
    transition: background 0.08s, border-color 0.08s;
  }
  .tpl-matrix-cell:hover { background: rgba(20, 30, 55, 0.80); }
  .tpl-matrix-cell-on {
    background: linear-gradient(90deg,
      rgba(251, 191, 36, 0.10) 0%,
      rgba(251, 191, 36, 0.04) 100%),
      rgba(20, 30, 55, 0.65);
    border-color: rgba(251, 191, 36, 0.55);
  }
  .tpl-matrix-cell-on:hover {
    border-color: rgba(251, 191, 36, 0.80);
  }
  .tpl-matrix-scope {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.05em;
    color: rgba(200, 216, 240, 0.70);
    text-transform: uppercase;
  }
  .tpl-matrix-cell-on .tpl-matrix-scope {
    color: var(--algo-amber, var(--c-action));
  }
  .tpl-matrix-tpl {
    grid-column: 1 / -1;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 600;
    color: #f8fafc;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .tpl-matrix-cell:not(.tpl-matrix-cell-on) .tpl-matrix-tpl {
    color: rgba(148, 163, 184, 0.60);
    font-style: italic;
    font-weight: 500;
  }
  .tpl-matrix-mark {
    grid-row: 1;
    grid-column: 2;
    font-family: var(--font-numeric);
    font-size: var(--fs-xl);
    font-weight: 800;
    color: rgba(148, 163, 184, 0.40);
    line-height: 1;
  }
  .tpl-matrix-cell-on .tpl-matrix-mark {
    color: var(--algo-green, var(--c-long));
  }

  /* Filter chips */
  .tpl-chip {
    padding: 0.25rem 0.7rem;
    font-size: var(--fs-lg);
    font-family: var(--font-numeric);
    color: rgba(180,200,230,0.75);
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(180,200,230,0.20);
    border-radius: 4px;
    cursor: pointer;
    transition: color 0.06s, background 0.06s, border-color 0.06s;
  }
  .tpl-chip:hover { color: var(--c-action); border-color: rgba(251,191,36,0.45); }
  .tpl-chip-on {
    color: var(--c-action);
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.55);
    font-weight: 700;
  }

  .tpl-create-btn {
    padding: 0.30rem 0.8rem;
    font-size: var(--fs-lg);
    font-weight: 600;
    color: var(--c-action);
    background: rgba(251,191,36,0.10);
    border: 1px solid rgba(251,191,36,0.50);
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.06s, border-color 0.06s;
  }
  .tpl-create-btn:hover {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.75);
  }

  /* Row list */
  .tpl-list {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .tpl-row {
    background: linear-gradient(180deg, rgba(20,30,55,0.65) 0%, rgba(12,18,38,0.65) 100%);
    border: 1px solid rgba(180,200,230,0.10);
    border-radius: 6px;
    overflow: hidden;
  }
  .tpl-row-default {
    border-color: rgba(251,191,36,0.35);
  }
  .tpl-row-head {
    width: 100%;
    display: flex;
    align-items: center;
    gap: 0.55rem;
    padding: 0.55rem 0.75rem;
    background: transparent;
    border: none;
    cursor: pointer;
    color: rgba(220,230,245,0.92);
    font-size: var(--fs-lg);
    text-align: left;
  }
  .tpl-row-head:hover { background: rgba(255,255,255,0.025); }
  .tpl-name { font-weight: 700; flex-shrink: 0; }

  .tpl-badge {
    padding: 0.08rem 0.45rem;
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.05em;
    border-radius: 3px;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .tpl-badge-default {
    color: var(--c-action);
    background: var(--c-action-14);
    border: 1px solid rgba(251,191,36,0.50);
  }
  .tpl-badge-system {
    color: #a5b4fc;
    background: rgba(165,180,252,0.10);
    border: 1px solid rgba(165,180,252,0.40);
  }
  .tpl-badge-inactive {
    color: #94a3b8;
    background: rgba(148,163,184,0.10);
    border: 1px solid rgba(148,163,184,0.30);
  }

  .tpl-applies {
    font-size: var(--fs-md);
    font-family: var(--font-numeric);
    color: #67e8f9;
    background: var(--c-info-08);
    padding: 0.10rem 0.45rem;
    border: 1px solid rgba(34,211,238,0.30);
    border-radius: 3px;
    flex-shrink: 0;
  }

  .tpl-numerics {
    display: flex;
    gap: 0.35rem;
    margin-left: auto;
    flex-wrap: wrap;
  }
  .tpl-num {
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 600;
    padding: 0.10rem 0.45rem;
    border-radius: 3px;
  }
  .tpl-num-tp {
    color: var(--c-long);
    background: rgba(74,222,128,0.08);
    border: 1px solid rgba(74,222,128,0.30);
  }
  .tpl-num-sl {
    color: var(--c-short);
    background: rgba(248,113,113,0.08);
    border: 1px solid rgba(248,113,113,0.30);
  }
  .tpl-num-wing {
    color: #c084fc;
    background: rgba(192,132,252,0.08);
    border: 1px solid rgba(192,132,252,0.30);
  }

  /* Expanded body */
  .tpl-body {
    padding: 0.6rem 0.9rem 0.85rem 1.6rem;
    border-top: 1px solid rgba(180,200,230,0.08);
  }
  .tpl-desc {
    font-size: var(--fs-lg);
    color: rgba(200,216,240,0.8);
    line-height: 1.45;
    margin-bottom: 0.6rem;
  }

  /* Edit form */
  .tpl-form {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.55rem 0.9rem;
    margin-top: 0.4rem;
  }
  .tpl-field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    font-size: var(--fs-lg);
    color: rgba(180,200,230,0.85);
  }
  .tpl-field span {
    font-weight: 600;
    font-family: var(--font-numeric);
    letter-spacing: 0.02em;
  }
  .tpl-field-full {
    grid-column: 1 / -1;
  }
  .tpl-field-wide {
    grid-column: span 2;
  }
  .tpl-input-mono {
    font-family: var(--font-numeric);
    line-height: 1.4;
    resize: vertical;
    min-height: 2.6rem;
  }
  .tpl-input {
    padding: 0.32rem 0.55rem;
    font-size: var(--fs-lg);
    color: #e5e7eb;
    background: rgba(15,23,41,0.7);
    border: 1px solid rgba(180,200,230,0.20);
    border-radius: 4px;
    font-family: var(--font-numeric);
  }
  .tpl-input:focus {
    outline: none;
    border-color: rgba(251,191,36,0.55);
    background: rgba(15,23,41,0.85);
  }
  .tpl-input:disabled {
    color: rgba(180,200,230,0.55);
    background: rgba(15,23,41,0.4);
    cursor: not-allowed;
  }
  .tpl-checkbox {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    font-size: var(--fs-lg);
    color: rgba(200,216,240,0.85);
    cursor: pointer;
  }

  .tpl-form-err {
    grid-column: 1 / -1;
    color: #fca5a5;
    font-size: var(--fs-lg);
    font-family: var(--font-numeric);
  }
  .tpl-json-err {
    display: block;
    margin-top: 0.2rem;
    color: var(--c-short);
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
  }
  .tpl-form-actions {
    grid-column: 1 / -1;
    display: flex;
    gap: 0.45rem;
    margin-top: 0.4rem;
  }
  .tpl-btn {
    padding: 0.35rem 0.85rem;
    font-size: var(--fs-lg);
    font-weight: 600;
    color: rgba(200,216,240,0.85);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(180,200,230,0.25);
    border-radius: 4px;
    cursor: pointer;
  }
  .tpl-btn:hover { background: rgba(255,255,255,0.08); color: #fff; }
  .tpl-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .tpl-btn-primary {
    color: var(--c-action);
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.55);
  }
  .tpl-btn-primary:hover {
    background: rgba(251,191,36,0.18);
    color: #fcd34d;
  }
  .tpl-btn-danger {
    color: var(--c-short);
    background: var(--c-short-10);
    border-color: rgba(248,113,113,0.45);
  }
  .tpl-btn-danger:hover {
    background: rgba(248,113,113,0.18);
    color: #fca5a5;
  }

  @media (max-width: 720px) {
    .tpl-form { grid-template-columns: 1fr; }
    .tpl-row-head { flex-wrap: wrap; }
    .tpl-numerics { margin-left: 0; }
  }
</style>
