<!--
  /automation/agent-templates — reusable notify / condition templates
  ($ref-able from inside an agent's conditions / events tree).

  Two kinds today:
    • notify templates    — saved channel lists (telegram + email + log)
    • condition templates — saved condition sub-trees (any/all/not/leaf)
    • action templates    — RESERVED for a future stage

  Operator workflow:
    - Browse all templates (filter by kind via the segmented control)
    - Expand a row to see its body
    - Custom templates: full edit / delete
    - System templates: toggle is_active only (body + description
      are owned by code seeds)
    - Create a new custom template via the form at the bottom

  Renamed from /automation/fragments in v2.1 — vocabulary unified
  under "templates" (order templates + agent templates). The
  /automation/fragments URL still works via 308 redirect.
-->
<script>
  import { onMount } from 'svelte';
  import { authStore, nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import {
    fetchAgentFragments, createAgentFragment,
    patchAgentFragment, deleteAgentFragment, reloadFragments,
  } from '$lib/api';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import DisclosureChevron  from '$lib/DisclosureChevron.svelte';
  import ConfirmModal       from '$lib/ConfirmModal.svelte';
  import Select             from '$lib/Select.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

  let fragments = $state(/** @type {any[]} */ ([]));
  let filterKind = $state(/** @type {'all'|'notify'|'condition'} */ ('all'));
  let loading = $state(true);
  let error = $state('');
  let _colTemplates = $state(false);
  let _fsTemplates  = $state(false);

  // ── Expanded row state ────────────────────────────────────────────
  let expandedId = $state(/** @type {number | null} */ (null));

  // ── Edit / Create form ────────────────────────────────────────────
  let editingId = $state(/** @type {number | null} */ (null));
  let formKind = $state('notify');
  let formName = $state('');
  let formDescription = $state('');
  let formBodyText = $state('[]');
  let formError = $state('');
  let _showLiveTs = $state(false);
  let busy = $state(false);

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);

  const isDemo = $derived(!$authStore.user);

  async function load() {
    loading = true; error = '';
    try {
      fragments = await fetchAgentFragments();
    } catch (e) {
      error = e.message || 'failed to load templates';
    } finally {
      loading = false;
    }
  }

  onMount(load);

  const visible = $derived(
    filterKind === 'all'
      ? fragments
      : fragments.filter(f => f.kind === filterKind)
  );

  // Group for display: notify first, condition second.
  const grouped = $derived(() => {
    const groups = { notify: [], condition: [] };
    for (const f of visible) {
      (groups[f.kind] ?? (groups[f.kind] = [])).push(f);
    }
    return groups;
  });

  function resetForm() {
    editingId = null;
    formKind = 'notify';
    formName = '';
    formDescription = '';
    formBodyText = '[]';
    formError = '';
  }

  function startEdit(/** @type {any} */ f) {
    editingId = f.id;
    formKind = f.kind;
    formName = f.name;
    formDescription = f.description || '';
    formBodyText = JSON.stringify(f.body, null, 2);
    formError = '';
    // Scroll into view.
    setTimeout(() => {
      document.getElementById('frag-form')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 50);
  }

  async function saveForm() {
    formError = '';
    let body;
    try {
      body = JSON.parse(formBodyText);
    } catch (e) {
      formError = `body JSON parse: ${e.message}`;
      return;
    }
    busy = true;
    try {
      if (editingId) {
        await patchAgentFragment(editingId, {
          body, description: formDescription,
        });
        toast.success(`Template saved: ${formName}`);
      } else {
        await createAgentFragment({
          kind: formKind,
          name: formName.trim().toLowerCase(),
          body,
          description: formDescription,
        });
        toast.success(`Template created: ${formName.trim().toLowerCase()}`);
      }
      resetForm();
      await load();
    } catch (e) {
      formError = e.message || 'save failed';
      toast.error(`Save failed: ${e.message || 'unknown error'}`);
    } finally {
      busy = false;
    }
  }

  async function toggleActive(/** @type {any} */ f) {
    busy = true;
    const next = !f.is_active;
    try {
      await patchAgentFragment(f.id, { is_active: next });
      toast.success(`Template ${next ? 'activated' : 'deactivated'}: ${f.name}`);
      await load();
    } catch (e) {
      toast.error(`Toggle failed: ${e.message || 'unknown error'}`);
    } finally {
      busy = false;
    }
  }

  async function removeFragment(/** @type {any} */ f) {
    const ok = await _confirmRef?.ask({
      title: 'Delete agent template?',
      message: `Delete <b>${f.name}</b>? This cannot be undone.`,
      danger: true,
      confirmLabel: 'Delete',
    });
    if (!ok) return;
    busy = true;
    try {
      await deleteAgentFragment(f.id);
      toast.success(`Template deleted: ${f.name}`);
      await load();
    } catch (e) {
      toast.error(`Delete failed: ${e.message || 'unknown error'}`);
    } finally {
      busy = false;
    }
  }

  async function doReload() {
    busy = true;
    try {
      await reloadFragments();
      await load();
      toast.success('Grammar reloaded');
    } catch (e) {
      toast.error(`Reload failed: ${e.message || 'unknown error'}`);
    } finally { busy = false; }
  }
</script>

<ConfirmModal bind:this={_confirmRef} />

<svelte:head><title>Agent Templates | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Agent Templates</h1>
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
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={doReload} loading={busy} label="templates" />
    <PageHeaderActions />
  </span>
</div>

<AutomationTabs />

<section class="bucket-card p-3 mb-3"
  class:fs-card-on={_fsTemplates}
  class:is-collapsed={_colTemplates}>
  <CardHeader
    bind:isCollapsed={_colTemplates}
    bind:isFullscreen={_fsTemplates}
    cardId="automation-agent-templates"
    label="Agent Templates"
    onRefresh={doReload}
    bind:refreshLoading={busy}
    showSearch={false}
  >
    {#snippet middle()}
      <div class="filter-row">
        <span class="filter-label">Show:</span>
        {#each ['all', 'notify', 'condition'] as k}
          <button
            class="filter-btn"
            class:filter-btn-on={filterKind === k}
            onclick={() => filterKind = /** @type {any} */ (k)}
            type="button">{k}</button>
        {/each}
        <span class="filter-hint">
          {fragments.length} total · {visible.length} shown
        </span>
      </div>
    {/snippet}
  </CardHeader>

  <div class="card-body" hidden={_colTemplates}>

    {#if loading}
      <div class="muted">Loading templates…</div>
    {:else if error}
      <div class="err">{error}</div>
    {:else}
      {@const g = grouped()}
      {#each ['notify', 'condition'] as kind}
        {#if g[kind] && g[kind].length > 0}
          <h2 class="grp-title">{kind.toUpperCase()}</h2>
          <ul class="frag-list">
            {#each g[kind] as f}
              <li class="frag-row" class:frag-row-open={expandedId === f.id}
                  class:frag-row-system={f.is_system}
                  class:frag-row-inactive={!f.is_active}>
                <button class="frag-head" onclick={() => expandedId = expandedId === f.id ? null : f.id}>
                  <span class="frag-name">{f.name}</span>
                  {#if f.is_system}<span class="frag-pill frag-pill-system">SYSTEM</span>{/if}
                  {#if !f.is_active}<span class="frag-pill frag-pill-off">OFF</span>{/if}
                  <span class="frag-desc">{f.description || '—'}</span>
                  <DisclosureChevron open={expandedId === f.id} ariaLabel={expandedId === f.id ? 'Collapse template' : 'Expand template'} />
                </button>
                {#if expandedId === f.id}
                  <div class="frag-body">
                    <pre class="frag-body-pre"><code>{JSON.stringify(f.body, null, 2)}</code></pre>
                    <div class="frag-actions">
                      <button class="action-btn" onclick={() => toggleActive(f)} disabled={busy}>
                        {f.is_active ? 'Deactivate' : 'Activate'}
                      </button>
                      {#if !f.is_system}
                        <button class="action-btn" onclick={() => startEdit(f)} disabled={busy}>Edit</button>
                        <button class="action-btn action-btn-danger" onclick={() => removeFragment(f)} disabled={busy}>Delete</button>
                      {:else}
                        <span class="muted">system rows are toggle-only</span>
                      {/if}
                    </div>
                  </div>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      {/each}
    {/if}
  </div>
</section>

{#if !isDemo}
  <section id="frag-form" class="algo-card mt-4 form-card">
    <h2 class="form-title">{editingId ? `Edit template #${editingId}` : 'Create custom template'}</h2>

    <div class="form-row">
      <span>Kind</span>
      {#if editingId}
        <span class="form-readonly">{formKind} (cannot change after create)</span>
      {:else}
        <Select bind:value={formKind} options={[
          { value: 'notify',    label: 'notify' },
          { value: 'condition', label: 'condition' },
        ]} />
      {/if}
    </div>

    <div class="form-row">
      <span>Name <span class="muted">(lowercase, hyphens)</span></span>
      {#if editingId}
        <span class="form-readonly">{formName}</span>
      {:else}
        <input class="form-input" bind:value={formName} placeholder="my-template-name" />
      {/if}
    </div>

    <div class="form-row">
      <span>Description</span>
      <input class="form-input" bind:value={formDescription} placeholder="One-line summary of what this template does" />
    </div>

    <div class="form-row form-row-body">
      <span>Body <span class="muted">(JSON)</span></span>
      <textarea class="form-body-area" bind:value={formBodyText} rows="10"
                placeholder={formKind === 'notify'
                  ? '[{"channel":"telegram","enabled":true}]'
                  : '{"metric":"pnl","scope":"positions.total","op":"<=","value":-50000}'}></textarea>
    </div>

    {#if formError}<div class="err mb-2">{formError}</div>{/if}

    <div class="form-actions">
      <button class="primary-btn" onclick={saveForm} disabled={busy}>
        {editingId ? 'Save' : 'Create'}
      </button>
      {#if editingId}
        <button class="action-btn" onclick={resetForm} disabled={busy}>Cancel</button>
      {/if}
    </div>
  </section>
{/if}

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  .filter-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    width: 100%;
    min-width: 0;
  }
  .filter-label {
    font-size: var(--fs-md);
    color: rgba(180,200,230,0.6);
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-right: 0.3rem;
  }
  .filter-btn {
    padding: 0.22rem 0.6rem;
    font-size: var(--fs-md);
    font-weight: 500;
    color: rgba(180,200,230,0.75);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 0.25rem;
    cursor: pointer;
    font-family: var(--font-numeric);
    letter-spacing: 0.03em;
    text-transform: lowercase;
    transition: background-color 0.06s, color 0.06s, border-color 0.06s;
  }
  .filter-btn:hover {
    background: rgba(251,191,36,0.08);
    color: var(--c-action);
    border-color: rgba(251,191,36,0.3);
  }
  .filter-btn-on {
    background: rgba(251,191,36,0.18);
    color: var(--c-action);
    font-weight: 700;
    border-color: rgba(251,191,36,0.5);
  }
  .filter-hint {
    font-size: var(--fs-sm);
    color: rgba(180,200,230,0.5);
    font-family: var(--font-numeric);
  }

  .grp-title {
    font-size: var(--fs-md);
    font-weight: 700;
    color: rgba(251,191,36,0.65);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin: 1.1rem 0 0.4rem 0.2rem;
    font-family: var(--font-numeric);
  }
  .frag-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .frag-row {
    background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    border: 1px solid rgba(255,255,255,0.05);
    border-radius: 0.3rem;
    overflow: hidden;
    transition: border-color 0.08s;
  }
  .frag-row:hover { border-color: rgba(251,191,36,0.25); }
  .frag-row-open { border-color: rgba(251,191,36,0.4); }
  .frag-row-system .frag-name { color: var(--algo-slate); }
  .frag-row-inactive { opacity: 0.55; }

  .frag-head {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    width: 100%;
    padding: 0.5rem 0.85rem;
    background: transparent;
    border: none;
    color: rgba(200,216,240,0.85);
    cursor: pointer;
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    text-align: left;
  }
  .frag-name {
    font-weight: 700;
    color: var(--c-action);
    letter-spacing: 0.02em;
    flex-shrink: 0;
  }
  .frag-pill {
    font-size: var(--fs-xs);
    font-weight: 700;
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .frag-pill-system {
    color: #94a3b8;
    background: rgba(148,163,184,0.12);
    border: 1px solid rgba(148,163,184,0.32);
  }
  .frag-pill-off {
    color: #fb7185;
    background: rgba(251,113,133,0.12);
    border: 1px solid rgba(251,113,133,0.32);
  }
  .frag-desc {
    color: rgba(180,200,230,0.6);
    font-size: var(--fs-md);
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .frag-body {
    padding: 0.55rem 0.85rem 0.75rem;
    border-top: 1px solid rgba(255,255,255,0.06);
  }
  .frag-body-pre {
    margin: 0 0 0.5rem 0;
    padding: 0.5rem 0.7rem;
    background: rgba(0,0,0,0.35);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 0.25rem;
    font-size: var(--fs-md);
    color: var(--algo-slate);
    max-height: 18rem;
    overflow: auto;
  }
  .frag-actions {
    display: flex;
    gap: 0.4rem;
    align-items: center;
  }

  .action-btn {
    padding: 0.22rem 0.65rem;
    font-size: var(--fs-md);
    font-weight: 500;
    color: rgba(200,216,240,0.85);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 0.25rem;
    cursor: pointer;
    font-family: var(--font-numeric);
    letter-spacing: 0.03em;
    transition: background-color 0.06s, color 0.06s, border-color 0.06s;
  }
  .action-btn:hover {
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
    border-color: rgba(251,191,36,0.35);
  }
  .action-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .action-btn-danger:hover {
    background: rgba(251,113,133,0.12);
    color: #fb7185;
    border-color: rgba(251,113,133,0.45);
  }

  .primary-btn {
    padding: 0.32rem 0.95rem;
    font-size: var(--fs-lg);
    font-weight: 700;
    color: var(--c-action);
    background: rgba(251,191,36,0.18);
    border: 1px solid rgba(251,191,36,0.5);
    border-radius: 0.25rem;
    cursor: pointer;
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
    transition: background-color 0.06s;
  }
  .primary-btn:hover { background: rgba(251,191,36,0.28); }
  .primary-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .form-card {
    border: 1px solid rgba(251,191,36,0.18);
  }
  .form-title {
    font-size: var(--fs-xl);
    font-weight: 700;
    color: var(--c-action);
    margin: 0 0 0.6rem 0;
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  .form-row {
    display: grid;
    grid-template-columns: 9rem 1fr;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.5rem;
  }
  .form-row span {
    font-size: var(--fs-md);
    color: rgba(180,200,230,0.7);
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
  }
  .form-row-body { align-items: start; }
  .form-input {
    padding: 0.3rem 0.55rem;
    font-size: var(--fs-lg);
    color: var(--algo-slate);
    background: rgba(0,0,0,0.3);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 0.25rem;
    font-family: var(--font-numeric);
    outline: none;
  }
  .form-input:focus { border-color: rgba(251,191,36,0.5); }
  .form-readonly {
    font-size: var(--fs-lg);
    color: rgba(180,200,230,0.6);
    font-family: var(--font-numeric);
    padding: 0.3rem 0;
  }
  .form-body-area {
    padding: 0.5rem 0.7rem;
    font-size: var(--fs-md);
    color: var(--algo-slate);
    background: rgba(0,0,0,0.35);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 0.25rem;
    font-family: var(--font-numeric);
    resize: vertical;
    outline: none;
  }
  .form-body-area:focus { border-color: rgba(251,191,36,0.5); }
  .form-actions {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.6rem;
  }

  .muted {
    color: rgba(180,200,230,0.5);
    font-size: var(--fs-md);
    font-family: var(--font-numeric);
  }
  .err {
    color: #fb7185;
    font-size: var(--fs-md);
    padding: 0.3rem 0;
    font-family: var(--font-numeric);
  }
  .mb-2 { margin-bottom: 0.5rem; }
  .mb-3 { margin-bottom: 0.75rem; }
  .mt-4 { margin-top: 1rem; }
  .ml-auto { margin-left: auto; }
</style>
