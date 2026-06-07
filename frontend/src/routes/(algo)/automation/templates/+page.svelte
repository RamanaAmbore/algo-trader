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
  import { authStore, nowStamp } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import DisclosureChevron from '$lib/DisclosureChevron.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import Select from '$lib/Select.svelte';
  import {
    fetchOrderTemplates,
    createOrderTemplate,
    patchOrderTemplate,
    deleteOrderTemplate,
  } from '$lib/api';

  let templates = $state(/** @type {any[]} */ ([]));
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
  let formIsDefault = $state(false);
  let formIsActive = $state(true);
  let formError = $state('');
  let busy = $state(false);

  // Create-new toggle
  let creatingNew = $state(false);

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);

  const isDemo = $derived(!$authStore.user);

  async function load() {
    loading = true; error = '';
    try {
      templates = await fetchOrderTemplates();
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
      await load();
      resetForm();
    } catch (e) {
      formError = e.message || 'save failed';
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
      await load();
      resetForm();
    } catch (e) {
      formError = e.message || 'create failed';
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
      await load();
    } catch (e) {
      error = e.message || 'delete failed';
    }
  }

  function fmtPct(v) { return v == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${Number(v).toFixed(1)}%`; }
  function fmtOffset(v) { return v == null ? '—' : `${Number(v) >= 0 ? '+' : ''}${v}`; }
  function appliesLabel(v) {
    return v === 'buy_any' ? 'Buy' : v === 'sell_option' ? 'Sell-Option' : 'Both';
  }
</script>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Templates</h1>
    <InfoHint popup align="right" text="<b>Order templates</b> are reusable exit-rule presets you pick at order entry — TP %, SL %, and (for SELL options) a protective wing leg. The selected template translates to a broker-native GTT for TP/SL and a paired basket order for the wing. Edit a template here and every future order using it inherits the new values; bulk-apply lets you push the change to open positions too." />
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="Templates" />
    <PageHeaderActions />
  </span>
</div>

<AutomationTabs />

{#if isDemo}
  <div class="algo-status-card p-3 mb-3">
    <p class="text-xs text-slate-300">
      Demo mode — system templates visible (read-only); create / edit / delete require sign-in.
    </p>
  </div>
{/if}

<!-- Filter strip + create button -->
<section class="bucket-card bucket-card-data p-3 mb-3">
  <div class="flex items-center justify-between gap-2 flex-wrap">
    <div class="flex items-center gap-1">
      <span class="mp-section-label">Filter:</span>
      {#each [
        ['all', 'All'],
        ['buy_any', 'Buy'],
        ['sell_option', 'Sell-Option'],
        ['both', 'Both'],
      ] as [k, label]}
        <button
          class="tpl-chip {filterScope === k ? 'tpl-chip-on' : ''}"
          onclick={() => { filterScope = k; }}
          type="button"
        >{label}</button>
      {/each}
    </div>

    {#if !isDemo}
      <button class="tpl-create-btn" onclick={startCreate} type="button">
        + Create custom template
      </button>
    {/if}
  </div>
</section>

{#if error}
  <div class="algo-status-card bg-red-900/30 border-red-700/50 p-3 mb-3">
    <p class="text-xs text-red-300">{error}</p>
  </div>
{/if}

{#if loading}
  <div class="algo-status-card p-3 text-xs text-slate-400">Loading…</div>
{:else if sorted.length === 0}
  <div class="algo-status-card p-3 text-xs text-slate-400">
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
            <span class="tpl-num tpl-num-tp">TP {fmtPct(t.tp_pct)}</span>
            <span class="tpl-num tpl-num-sl">SL {fmtPct(t.sl_pct)}</span>
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
                      { value: 'buy_any', label: 'Buy (any instrument)' },
                      { value: 'sell_option', label: 'Sell-Option (CE / PE)' },
                      { value: 'both', label: 'Both' },
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
                          disabled={busy}
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

<!-- Create-new form (separate from edit so they don't fight over state) -->
{#if creatingNew}
  <section id="tpl-create-form" class="bucket-card bucket-card-data p-3 mt-3">
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
            { value: 'buy_any', label: 'Buy (any instrument)' },
            { value: 'sell_option', label: 'Sell-Option (CE / PE)' },
            { value: 'both', label: 'Both' },
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
                disabled={busy}
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
  /* Filter chips */
  .tpl-chip {
    padding: 0.25rem 0.7rem;
    font-size: 0.7rem;
    font-family: ui-monospace, monospace;
    color: rgba(180,200,230,0.75);
    background: rgba(255,255,255,0.03);
    border: 1px solid rgba(180,200,230,0.20);
    border-radius: 4px;
    cursor: pointer;
    transition: color 0.06s, background 0.06s, border-color 0.06s;
  }
  .tpl-chip:hover { color: #fbbf24; border-color: rgba(251,191,36,0.45); }
  .tpl-chip-on {
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.55);
    font-weight: 700;
  }

  .tpl-create-btn {
    padding: 0.30rem 0.8rem;
    font-size: 0.72rem;
    font-weight: 600;
    color: #fbbf24;
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
    font-size: 0.78rem;
    text-align: left;
  }
  .tpl-row-head:hover { background: rgba(255,255,255,0.025); }
  .tpl-name { font-weight: 700; flex-shrink: 0; }

  .tpl-badge {
    padding: 0.08rem 0.45rem;
    font-size: 0.58rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    border-radius: 3px;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .tpl-badge-default {
    color: #fbbf24;
    background: rgba(251,191,36,0.14);
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
    font-size: 0.65rem;
    font-family: ui-monospace, monospace;
    color: #67e8f9;
    background: rgba(34,211,238,0.08);
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
    font-family: ui-monospace, monospace;
    font-size: 0.66rem;
    font-weight: 600;
    padding: 0.10rem 0.45rem;
    border-radius: 3px;
  }
  .tpl-num-tp {
    color: #4ade80;
    background: rgba(74,222,128,0.08);
    border: 1px solid rgba(74,222,128,0.30);
  }
  .tpl-num-sl {
    color: #f87171;
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
    font-size: 0.72rem;
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
    font-size: 0.7rem;
    color: rgba(180,200,230,0.85);
  }
  .tpl-field span {
    font-weight: 600;
    font-family: ui-monospace, monospace;
    letter-spacing: 0.02em;
  }
  .tpl-field-full {
    grid-column: 1 / -1;
  }
  .tpl-input {
    padding: 0.32rem 0.55rem;
    font-size: 0.72rem;
    color: #e5e7eb;
    background: rgba(15,23,41,0.7);
    border: 1px solid rgba(180,200,230,0.20);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
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
    font-size: 0.72rem;
    color: rgba(200,216,240,0.85);
    cursor: pointer;
  }

  .tpl-form-err {
    grid-column: 1 / -1;
    color: #fca5a5;
    font-size: 0.7rem;
    font-family: ui-monospace, monospace;
  }
  .tpl-form-actions {
    grid-column: 1 / -1;
    display: flex;
    gap: 0.45rem;
    margin-top: 0.4rem;
  }
  .tpl-btn {
    padding: 0.35rem 0.85rem;
    font-size: 0.72rem;
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
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.55);
  }
  .tpl-btn-primary:hover {
    background: rgba(251,191,36,0.18);
    color: #fcd34d;
  }
  .tpl-btn-danger {
    color: #f87171;
    background: rgba(248,113,113,0.10);
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
