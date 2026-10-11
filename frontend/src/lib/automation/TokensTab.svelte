<!--
  TokensTab — reusable notify / condition fragment ("Tokens") browse/manage
  surface, folded into /automation as a tab (Sprint 9). Ported from the
  former /automation/agent-templates route, which is now deleted outright.

  Two kinds today:
    • notify templates    — saved channel lists (telegram + email + log)
    • condition templates — saved condition sub-trees (any/all/not/leaf)

  Pure browse/manage: filter + expand + toggle + edit + delete. NO creation
  entry point on /automation per Sprint 9 design. Token creation happens
  at /console via `token(kind=notify) <channels> #name="..."` CLI statements.
-->
<script>
  import { onMount } from 'svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import {
    fetchAgentFragments,
    patchAgentFragment, deleteAgentFragment, reloadFragments, fetchFragmentReferences,
  } from '$lib/api';
  import DisclosureChevron  from '$lib/DisclosureChevron.svelte';
  import ConfirmModal       from '$lib/ConfirmModal.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

  let fragments = $state(/** @type {any[]} */ ([]));
  let filterKind = $state(/** @type {'all'|'notify'|'condition'} */ ('all'));
  let loading = $state(true);
  let error = $state('');
  let _colTemplates = $state(false);
  let _fsTemplates  = $state(false);

  let expandedId = $state(/** @type {number | null} */ (null));

  // Inline edit form (description + body only — kind/name fixed post-create).
  let editingId = $state(/** @type {number | null} */ (null));
  let formDescription = $state('');
  let formBodyText = $state('[]');
  let formError = $state('');
  let busy = $state(false);

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);

  export async function load() {
    loading = true; error = '';
    try {
      fragments = await fetchAgentFragments();
    } catch (e) {
      error = e.message || 'failed to load Tokens';
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
    formDescription = '';
    formBodyText = '[]';
    formError = '';
  }

  function startEdit(/** @type {any} */ f) {
    editingId = f.id;
    formDescription = f.description || '';
    formBodyText = JSON.stringify(f.body, null, 2);
    formError = '';
  }

  function escapeHtml(/** @type {string} */ s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  }

  /** Agents that reference this fragment. Returns null when the check itself fails. */
  async function impactFor(/** @type {any} */ f) {
    try {
      const impact = await fetchFragmentReferences(f.id);
      return impact.agents || [];
    } catch (e) {
      toast.error(`Impact check failed: ${e.message || 'unknown error'}`);
      return null;
    }
  }

  /** Ask the operator to confirm a change that would reach these agents. True means proceed. */
  async function confirmAgents(/** @type {any} */ f, /** @type {string} */ verb, /** @type {any[]} */ agents) {
    if (agents.length === 0) return true;
    const rows = agents.map((a) =>
      `<li><b>${escapeHtml(a.slug)}</b> (${escapeHtml(a.status)})</li>`).join('');
    return !!(await _confirmRef?.ask({
      title: `${verb} shared template?`,
      message: `<b>${escapeHtml(f.name)}</b> is used by ${agents.length} agent(s):<ul>${rows}</ul>`,
      danger: verb !== 'Save',
      confirmLabel: verb,
    }));
  }

  async function saveForm(/** @type {any} */ f) {
    formError = '';
    let body;
    try {
      body = JSON.parse(formBodyText);
    } catch (e) {
      formError = `body JSON parse: ${e.message}`;
      return;
    }
    const agents = await impactFor(f);
    if (agents === null || !(await confirmAgents(f, 'Save', agents))) return;
    busy = true;
    try {
      await patchAgentFragment(f.id, { body, description: formDescription });
      toast.success(`Token saved: ${f.name}`);
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
    const next = !f.is_active;
    if (!next) {
      const agents = await impactFor(f);
      if (agents === null || !(await confirmAgents(f, 'Deactivate', agents))) return;
    }
    busy = true;
    try {
      await patchAgentFragment(f.id, { is_active: next });
      toast.success(`Token ${next ? 'activated' : 'deactivated'}: ${f.name}`);
      await load();
    } catch (e) {
      toast.error(`Toggle failed: ${e.message || 'unknown error'}`);
    } finally {
      busy = false;
    }
  }

  async function removeFragment(/** @type {any} */ f) {
    const agents = await impactFor(f);
    if (agents === null) return;
    const impact = agents.length
      ? `<br>Used by ${agents.length} agent(s): ${agents.map((a) => `<b>${escapeHtml(a.slug)}</b>`).join(', ')}.`
      : '';
    const ok = await _confirmRef?.ask({
      title: 'Delete Token?',
      message: `Delete <b>${escapeHtml(f.name)}</b>? This cannot be undone.${impact}`,
      danger: true,
      confirmLabel: 'Delete',
    });
    if (!ok) return;
    busy = true;
    try {
      await deleteAgentFragment(f.id);
      toast.success(`Token deleted: ${f.name}`);
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

<section class="bucket-card p-3 mb-3"
  class:fs-card-on={_fsTemplates}
  class:is-collapsed={_colTemplates}>
  <CardHeader
    bind:isCollapsed={_colTemplates}
    bind:isFullscreen={_fsTemplates}
    cardId="automation-agent-templates"
    label="Tokens"
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
      <div class="muted">Loading Tokens…</div>
    {:else if error}
      <div class="err">{error}</div>
    {:else}
      {@const g = grouped()}
      {#each ['notify', 'condition'] as kind}
        {#if g[kind] && g[kind].length > 0}
          <h2 class="grp-title">{kind.toUpperCase()}</h2>
          <ul class="frag-list">
            {#each g[kind] as f, i}
              <li class="frag-row {i % 2 === 0 ? 'row-tint-odd' : 'row-tint-even'}"
                  class:frag-row-open={expandedId === f.id}
                  class:frag-row-system={f.is_system}
                  class:frag-row-inactive={!f.is_active}>
                <button class="frag-head" onclick={() => expandedId = expandedId === f.id ? null : f.id}>
                  <span class="frag-name">{f.name}</span>
                  {#if f.is_system}<span class="frag-pill frag-pill-system">SYSTEM</span>{/if}
                  {#if !f.is_active}<span class="frag-pill frag-pill-off">OFF</span>{/if}
                  {#if f.group}<span class="frag-pill frag-pill-group">{f.group}</span>{/if}
                  <span class="frag-desc">{f.description || '—'}</span>
                  <DisclosureChevron open={expandedId === f.id} ariaLabel={expandedId === f.id ? 'Collapse Token' : 'Expand Token'} />
                </button>
                {#if expandedId === f.id}
                  <div class="frag-body">
                    {#if editingId === f.id}
                      <div class="frag-edit-form">
                        <label class="frag-edit-row">
                          <span>Description</span>
                          <input class="frag-edit-input" bind:value={formDescription} />
                        </label>
                        <label class="frag-edit-row frag-edit-row-body">
                          <span>Body (JSON)</span>
                          <textarea class="frag-edit-input frag-edit-body" bind:value={formBodyText} rows="8"></textarea>
                        </label>
                        {#if formError}<div class="err mb-2">{formError}</div>{/if}
                        <div class="frag-actions">
                          <button class="action-btn action-btn-primary" onclick={() => saveForm(f)} disabled={busy}>
                            {busy ? 'Saving…' : 'Save'}
                          </button>
                          <button class="action-btn" onclick={resetForm} disabled={busy}>Cancel</button>
                        </div>
                      </div>
                    {:else}
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
                    {/if}
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

<style>
  .filter-row { display: flex; align-items: center; gap: 0.4rem; width: 100%; min-width: 0; }
  .filter-label {
    font-size: var(--fs-md);
    color: var(--c-muted);
    font-family: var(--font-numeric);
    letter-spacing: 0.04em;
    text-transform: uppercase;
    margin-right: 0.3rem;
  }
  .filter-btn {
    font-size: var(--fs-sm);
    border-radius: 3px;
    padding: 0.2rem 0.55rem;
    color: var(--c-muted);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(180,200,230,0.18);
    cursor: pointer;
    font-family: var(--font-numeric);
    letter-spacing: 0.03em;
    text-transform: lowercase;
    transition: background-color 0.06s, color 0.06s, border-color 0.06s;
  }
  .filter-btn:hover { background: rgba(251,191,36,0.08); color: var(--c-action); border-color: rgba(251,191,36,0.3); }
  .filter-btn-on { color: var(--c-action); background: rgba(251,191,36,0.12); border-color: rgba(251,191,36,0.45); font-weight: 700; }
  .filter-hint { font-size: var(--fs-sm); color: var(--c-muted); font-family: var(--font-numeric); }

  .grp-title {
    font-size: var(--fs-md);
    font-weight: 700;
    color: rgba(251,191,36,0.65);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    margin: 1.1rem 0 0.4rem 0.2rem;
    font-family: var(--font-numeric);
  }
  .frag-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 0.25rem; }
  .frag-row {
    background: linear-gradient(180deg, #0f1729 0%, #0a1020 100%);
    border: 1px solid rgba(126,151,184,0.10);
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
    color: color-mix(in srgb, var(--algo-slate) 85%, transparent);
    cursor: pointer;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 500;
    text-align: left;
  }
  .frag-name { font-size: var(--fs-lg); font-weight: 700; color: var(--c-action); letter-spacing: 0.02em; padding-left: 4px; flex-shrink: 0; }
  .frag-pill {
    font-size: var(--fs-sm);
    font-weight: 700;
    padding: 0.1rem 0.45rem;
    border-radius: 999px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    flex-shrink: 0;
  }
  .frag-pill-system { color: #94a3b8; background: rgba(148,163,184,0.12); border: 1px solid rgba(148,163,184,0.32); }
  .frag-pill-off { color: #fb7185; background: rgba(251,113,133,0.12); border: 1px solid rgba(251,113,133,0.32); }
  .frag-pill-group { color: var(--algo-slate-muted); background: rgba(126,151,184,0.10); border: 1px solid rgba(255,255,255,0.12); text-transform: none; letter-spacing: 0.02em; }
  .frag-desc { color: var(--c-muted); font-size: var(--fs-md); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .frag-body { padding: 0.55rem 0.85rem 0.75rem; border-top: 1px solid rgba(255,255,255,0.06); }
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
  .frag-actions { display: flex; gap: 0.4rem; align-items: center; }
  .frag-edit-form { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 0.5rem; }
  .frag-edit-row { display: flex; flex-direction: column; gap: 0.2rem; font-size: var(--fs-md); color: var(--c-muted); font-family: var(--font-numeric); }
  .frag-edit-input {
    padding: 0.3rem 0.55rem;
    font-size: var(--fs-lg);
    color: var(--algo-slate);
    background: rgba(0,0,0,0.3);
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 0.25rem;
    font-family: var(--font-numeric);
  }
  .frag-edit-body { resize: vertical; }

  .action-btn {
    font-size: var(--fs-sm);
    font-weight: 600;
    border-radius: 3px;
    padding: 0.25rem 0.7rem;
    color: color-mix(in srgb, var(--algo-slate) 85%, transparent);
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(180,200,230,0.2);
    cursor: pointer;
    font-family: var(--font-numeric);
    letter-spacing: 0.03em;
    transition: background-color 0.06s, color 0.06s, border-color 0.06s;
  }
  .action-btn:hover { color: var(--c-action); background: rgba(251,191,36,0.10); border-color: rgba(251,191,36,0.40); }
  .action-btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .action-btn-danger:hover { color: var(--c-short); background: var(--c-short-10, rgba(248,113,113,0.10)); border-color: rgba(248,113,113,0.45); }
  .action-btn-primary { color: var(--c-action); background: rgba(251,191,36,0.15); border-color: rgba(251,191,36,0.5); font-weight: 700; }
  .action-btn-primary:hover { background: rgba(251,191,36,0.28); }

  .muted { color: var(--c-muted); font-size: var(--fs-md); font-family: var(--font-numeric); }
  .err { color: #fb7185; font-size: var(--fs-md); padding: 0.3rem 0; font-family: var(--font-numeric); }
  .mb-2 { margin-bottom: 0.5rem; }
</style>
