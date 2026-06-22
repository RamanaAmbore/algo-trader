<!--
  /strategies — Strategies catalog (slice 6).

  Lists every Strategy with live order-count + realised/unrealised
  P&L rollup. Inline-create + edit + soft-delete. Per-strategy
  detail (lot ledger, capacity charts) lands in slice 7 when the
  ledger ships.

  Demo can READ the list (view_strategies cap) — same gate that
  lets the showcase tour link here. Mutations require
  `manage_own_strategies` (admin / trader).
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { nowStamp, marketAwareInterval } from '$lib/stores';
  import {
    fetchStrategies, createStrategy, updateStrategy, deleteStrategy,
  } from '$lib/api';
  import { userRole, userCaps, hasCap } from '$lib/rbac';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';

  /** @typedef {{
   *   id: number, slug: string, name: string,
   *   description: string|null,
   *   owner_user_id: number|null, owner_username: string|null,
   *   capacity_cap_inr: number|null, target_volatility: number|null,
   *   is_active: boolean,
   *   open_order_count: number, closed_order_count: number,
   *   realised_pnl: number, unrealised_pnl: number,
   *   created_at: string, updated_at: string,
   * }} StrategyRow */

  /** @type {StrategyRow[]} */
  let rows = $state([]);
  let loading = $state(false);
  let error = $state('');

  let createForm = $state({
    slug: '', name: '', description: '',
    capacity_cap_inr: '', target_volatility: '',
    is_active: true,
  });
  let creating = $state(false);

  /** @type {number | null} */
  let editingId = $state(null);
  let editForm = $state(/** @type {Record<string,any>} */ ({}));

  const canEdit = $derived(hasCap('manage_own_strategies', $userCaps, $userRole));

  async function load() {
    loading = true; error = '';
    try {
      const r = await fetchStrategies();
      rows = Array.isArray(r?.rows) ? r.rows : [];
    } catch (e) {
      error = e?.message || 'Strategies fetch failed';
    } finally {
      loading = false;
    }
  }

  async function doCreate() {
    if (!createForm.slug || !createForm.name) {
      error = 'slug + name required';
      return;
    }
    creating = true; error = '';
    try {
      await createStrategy({
        slug: createForm.slug.trim().toLowerCase(),
        name: createForm.name.trim(),
        description: createForm.description?.trim() || null,
        capacity_cap_inr: createForm.capacity_cap_inr === ''
                            ? null : Number(createForm.capacity_cap_inr),
        target_volatility: createForm.target_volatility === ''
                            ? null : Number(createForm.target_volatility),
        is_active: !!createForm.is_active,
      });
      createForm = { slug:'', name:'', description:'', capacity_cap_inr:'', target_volatility:'', is_active:true };
      await load();
    } catch (e) { error = e?.message || 'Create failed'; }
    finally { creating = false; }
  }

  function startEdit(/** @type {StrategyRow} */ row) {
    editingId = row.id;
    editForm = {
      slug: row.slug,
      name: row.name,
      description: row.description ?? '',
      capacity_cap_inr: row.capacity_cap_inr ?? '',
      target_volatility: row.target_volatility ?? '',
      is_active: row.is_active,
    };
  }
  function cancelEdit() { editingId = null; }
  async function saveEdit() {
    if (editingId == null) return;
    try {
      await updateStrategy(editingId, {
        slug: editForm.slug?.trim().toLowerCase(),
        name: editForm.name?.trim(),
        description: editForm.description ?? null,
        capacity_cap_inr: editForm.capacity_cap_inr === '' ? null
                          : Number(editForm.capacity_cap_inr),
        target_volatility: editForm.target_volatility === '' ? null
                            : Number(editForm.target_volatility),
        is_active: !!editForm.is_active,
      });
      editingId = null;
      await load();
    } catch (e) { error = e?.message || 'Save failed'; }
  }
  async function doDelete(/** @type {StrategyRow} */ row) {
    if (!confirm(`Delete strategy "${row.slug}"? Historical orders keep their data but lose attribution.`)) return;
    try {
      await deleteStrategy(row.id);
      await load();
    } catch (e) { error = e?.message || 'Delete failed'; }
  }

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let _teardown = null;
  onMount(() => {
    load();
    _teardown = marketAwareInterval(load, 30000);
  });
  onDestroy(() => { _teardown?.(); });

  function _fmtInr(/** @type {number|null} */ v) {
    if (v == null || !isFinite(v)) return '—';
    if (Math.abs(v) >= 100000) return `₹${(v/100000).toFixed(2)}L`;
    if (Math.abs(v) >= 1000)   return `₹${(v/1000).toFixed(1)}k`;
    return `₹${Number(v).toFixed(0)}`;
  }
  function _fmtPctOpt(/** @type {number|null} */ v) {
    if (v == null) return '—';
    return `${(Number(v) * 100).toFixed(1)}%`;
  }
</script>

<svelte:head>
  <title>Strategies · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Strategies</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="strategies" />
    <PageHeaderActions />
  </span>
</div>

{#if error}
  <div class="strat-error">{error}</div>
{/if}

<!-- Create form. Designated / admin / trader only. Hidden for read-only roles. -->
{#if canEdit}
  <div class="strat-create">
    <h2 class="strat-section-heading">New strategy</h2>
    <div class="strat-create-grid">
      <label class="strat-flbl">Slug
        <input bind:value={createForm.slug} class="field-input" placeholder="nifty-mean-reversion" />
      </label>
      <label class="strat-flbl">Name
        <input bind:value={createForm.name} class="field-input" placeholder="NIFTY Mean Reversion" />
      </label>
      <label class="strat-flbl strat-flbl-wide">Description
        <input bind:value={createForm.description} class="field-input" />
      </label>
      <label class="strat-flbl">Capacity (₹)
        <input bind:value={createForm.capacity_cap_inr} type="number" class="field-input" placeholder="optional" />
      </label>
      <label class="strat-flbl">Target σ
        <input bind:value={createForm.target_volatility} type="number" step="0.01" class="field-input" placeholder="0.15" />
      </label>
      <button class="btn-primary" onclick={doCreate} disabled={creating}>
        {creating ? 'Creating…' : '+ Create'}
      </button>
    </div>
  </div>
{/if}

<!-- Table. Always renders, even for demo / observer (view_strategies includes them). -->
<div class="strat-table-wrap">
  <table class="strat-table">
    <thead>
      <tr>
        <th class="th-slug">Slug</th>
        <th>Name</th>
        <th>Owner</th>
        <th class="th-num">Open</th>
        <th class="th-num">Closed</th>
        <th class="th-num">Realised P&amp;L</th>
        <th class="th-num">Unrealised P&amp;L</th>
        <th class="th-num">Capacity</th>
        <th class="th-num">σ tgt</th>
        <th>Active</th>
        {#if canEdit}<th></th>{/if}
      </tr>
    </thead>
    <tbody>
      {#if rows.length === 0 && !loading}
        <tr><td colspan="11" class="strat-empty">No strategies yet — create one above to attribute orders.</td></tr>
      {/if}
      {#each rows as r (r.id)}
        {#if editingId === r.id}
          <tr class="strat-row-editing">
            <td><input bind:value={editForm.slug} class="field-input field-input-sm" /></td>
            <td><input bind:value={editForm.name} class="field-input field-input-sm" /></td>
            <td>{r.owner_username ?? '—'}</td>
            <td class="td-num">{r.open_order_count}</td>
            <td class="td-num">{r.closed_order_count}</td>
            <td class="td-num">{_fmtInr(r.realised_pnl)}</td>
            <td class="td-num">{_fmtInr(r.unrealised_pnl)}</td>
            <td><input bind:value={editForm.capacity_cap_inr} type="number" class="field-input field-input-sm field-input-num" /></td>
            <td><input bind:value={editForm.target_volatility} type="number" step="0.01" class="field-input field-input-sm field-input-num" /></td>
            <td><input type="checkbox" bind:checked={editForm.is_active} /></td>
            <td class="td-actions">
              <button class="btn-primary btn-sm" onclick={saveEdit}>Save</button>
              <button class="btn-secondary btn-sm" onclick={cancelEdit}>Cancel</button>
            </td>
          </tr>
        {:else}
          <tr class:strat-row-inactive={!r.is_active}>
            <td class="td-slug">{r.slug}</td>
            <td>{r.name}</td>
            <td>{r.owner_username ?? '—'}</td>
            <td class="td-num">{r.open_order_count}</td>
            <td class="td-num">{r.closed_order_count}</td>
            <td class="td-num {r.realised_pnl > 0 ? 'pnl-pos' : r.realised_pnl < 0 ? 'pnl-neg' : ''}">{_fmtInr(r.realised_pnl)}</td>
            <td class="td-num {r.unrealised_pnl > 0 ? 'pnl-pos' : r.unrealised_pnl < 0 ? 'pnl-neg' : ''}">{_fmtInr(r.unrealised_pnl)}</td>
            <td class="td-num">{_fmtInr(r.capacity_cap_inr)}</td>
            <td class="td-num">{_fmtPctOpt(r.target_volatility)}</td>
            <td>
              <span class={r.is_active ? 'pill-active' : 'pill-inactive'}>
                {r.is_active ? 'active' : 'paused'}
              </span>
            </td>
            {#if canEdit}
              <td class="td-actions">
                <button class="btn-secondary btn-sm" onclick={() => startEdit(r)}>Edit</button>
                <button class="btn-secondary btn-sm btn-danger" onclick={() => doDelete(r)}>×</button>
              </td>
            {/if}
          </tr>
        {/if}
      {/each}
    </tbody>
  </table>
</div>

<style>
  .strat-error {
    padding: 0.6rem 0.9rem;
    background: rgba(248, 113, 113, 0.10);
    border: 1px solid rgba(248, 113, 113, 0.40);
    border-radius: 4px;
    color: #fca5a5; font-size: 0.7rem;
    margin-bottom: 0.7rem;
  }
  .strat-section-heading {
    font-size: 0.65rem; font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase; color: #a3b9d0;
    margin: 0 0 0.5rem;
    font-family: ui-monospace, monospace;
  }
  .strat-create {
    padding: 0.8rem 1rem;
    background: rgba(15, 23, 42, 0.45);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
    margin-bottom: 0.9rem;
  }
  .strat-create-grid {
    display: flex; flex-wrap: wrap; gap: 0.55rem 0.8rem;
    align-items: flex-end;
  }
  .strat-flbl {
    display: flex; flex-direction: column; gap: 0.15rem;
    font-size: 0.55rem; font-weight: 700; letter-spacing: 0.05em;
    text-transform: uppercase; color: #a3b9d0;
    font-family: ui-monospace, monospace;
  }
  .strat-flbl-wide :global(input) { min-width: 18rem; }
  .strat-flbl :global(input) {
    font-size: 0.72rem; min-width: 9rem;
  }

  .strat-table-wrap {
    overflow-x: auto;
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
  }
  .strat-table {
    width: 100%; border-collapse: collapse;
    font-family: ui-sans-serif, system-ui, sans-serif;
    font-size: 0.72rem;
  }
  .strat-table th {
    text-align: left;
    padding: 0.45rem 0.6rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
    color: #a3b9d0;
    font-size: 0.55rem;
    font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase;
    background: rgba(15, 23, 42, 0.65);
    font-family: ui-monospace, monospace;
  }
  .strat-table th.th-num { text-align: right; }
  .strat-table td {
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.10);
    color: #c8d8f0;
  }
  .strat-table td.td-num {
    text-align: right;
    font-family: ui-monospace, monospace;
    font-variant-numeric: tabular-nums;
  }
  .strat-table td.td-slug {
    color: #fbbf24; font-weight: 700; font-family: ui-monospace, monospace;
  }
  .strat-table tbody tr:hover td { background: rgba(34, 211, 238, 0.05); }
  .strat-row-inactive td { opacity: 0.5; }
  .strat-row-editing td { background: rgba(251, 191, 36, 0.06); }
  .strat-empty {
    text-align: center; padding: 1.5rem !important;
    color: #7e97b8; font-style: italic;
  }
  .pnl-pos { color: #4ade80; }
  .pnl-neg { color: #f87171; }

  .pill-active   { display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px;
                   background: rgba(74,222,128,0.16); color: #4ade80;
                   border: 1px solid rgba(74,222,128,0.40);
                   font-size: 0.55rem; font-weight: 700; letter-spacing: 0.04em;
                   text-transform: uppercase; font-family: ui-monospace, monospace; }
  .pill-inactive { display: inline-block; padding: 0.05rem 0.4rem; border-radius: 3px;
                   background: rgba(126,151,184,0.16); color: #7e97b8;
                   border: 1px solid rgba(126,151,184,0.40);
                   font-size: 0.55rem; font-weight: 700; letter-spacing: 0.04em;
                   text-transform: uppercase; font-family: ui-monospace, monospace; }

  .btn-sm { font-size: 0.6rem; padding: 0.2rem 0.55rem; }
  .btn-danger { color: #f87171; border-color: rgba(248,113,113,0.40); }
  .btn-danger:hover { background: rgba(248,113,113,0.15); }

  .field-input-sm { font-size: 0.7rem; padding: 0.18rem 0.4rem; }
  .field-input-num { text-align: right; font-variant-numeric: tabular-nums;
                     font-family: ui-monospace, monospace; }
  .td-actions { white-space: nowrap; }
  .td-actions :global(button + button) { margin-left: 0.3rem; }
</style>
