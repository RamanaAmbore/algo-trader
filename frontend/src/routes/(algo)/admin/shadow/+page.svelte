<script>
  // Shadow mode dashboard (/admin/shadow).
  //
  // Shows orders validated via basket_margin with their exact Kite payload,
  // but never executed. Prod only — the confidence step between Paper and Live.

  import { onMount, onDestroy } from 'svelte';
  import { authStore, visibleInterval, branchLabel } from '$lib/stores';
  import {
    fetchShadowStatus, fetchShadowOrders,
    promoteShadowToLive, clearShadowData,
  } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';

  let status   = $state(/** @type {any} */ ({}));
  let orders   = $state(/** @type {any[]} */ ([]));
  let error    = $state('');
  let loading  = $state(true);
  let promoting = $state(false);
  let refreshTeardown;

  async function load() {
    try {
      const [stat, ord] = await Promise.all([
        fetchShadowStatus(),
        fetchShadowOrders(50).catch(() => []),
      ]);
      status = stat;
      orders = ord || [];
      error  = '';
    } catch (e) {
      if (!status?.branch) error = e.message;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load();
    refreshTeardown = visibleInterval(load, 5000);
  });
  onDestroy(() => refreshTeardown?.());

  async function handlePromote() {
    if (!confirm(
      'This will enable ALL live execution flags and disable shadow mode.\n\n' +
      'Real broker orders will be placed. Are you sure?'
    )) return;
    promoting = true;
    try {
      const result = await promoteShadowToLive();
      alert('Promoted to live:\n' + (result?.promoted || []).join('\n'));
      await load();
    } catch (e) {
      error = e.message;
    } finally {
      promoting = false;
    }
  }

  async function handleClear() {
    if (!confirm('Delete all shadow orders?')) return;
    try {
      await clearShadowData();
      await load();
    } catch (e) {
      error = e.message;
    }
  }

  const enabled = $derived(status?.enabled !== false);
  const branch  = $derived(branchLabel(status?.branch || ''));
  const shadowActive = $derived(status?.shadow_active === true);

  /** Parse the Kite payload from the detail string (after --- KITE PAYLOAD ---) */
  function parsePayload(detail) {
    if (!detail) return null;
    const marker = '--- KITE PAYLOAD ---';
    const idx = detail.indexOf(marker);
    if (idx < 0) return null;
    try {
      return JSON.parse(detail.slice(idx + marker.length));
    } catch { return null; }
  }

  let expandedId = $state(null);
</script>

<svelte:head><title>Shadow Mode — RamboQuant</title></svelte:head>

<div class="sim-page">
  <header class="sim-header">
    <h2>
      Shadow Mode
      <InfoHint popup text="Shadow mode validates orders via Kite's basket_margin and logs the exact broker payload — but never places the order. Review shadow orders, then promote to live when confident." />
    </h2>
    {#if shadowActive}
      <span class="badge badge-shadow">SHADOW ACTIVE</span>
    {:else}
      <span class="badge badge-shadow-off">SHADOW OFF</span>
    {/if}
  </header>

  {#if !enabled}
    <div class="sim-banner sim-banner-warn">
      Shadow mode is only available on <strong>prod</strong>. Current branch: <strong>{branch}</strong>.
    </div>
  {/if}

  {#if error}
    <div class="sim-banner sim-banner-error">{error}</div>
  {/if}

  <!-- Status + controls -->
  <div class="sim-controls">
    <div class="shadow-status-row">
      <div class="shadow-stat">
        <span class="sim-label">Branch</span>
        <span class="shadow-val">{branch}</span>
      </div>
      <div class="shadow-stat">
        <span class="sim-label">Shadow orders</span>
        <span class="shadow-val">{status?.order_count ?? 0}</span>
      </div>
      <div class="shadow-stat">
        <span class="sim-label">Status</span>
        <span class="shadow-val" class:shadow-on={shadowActive} class:shadow-off={!shadowActive}>
          {shadowActive ? 'Active' : 'Inactive'}
        </span>
      </div>
    </div>
    <div class="sim-btn-row">
      <button class="sim-btn sim-btn-promote" onclick={handlePromote}
              disabled={!enabled || promoting || !shadowActive}>
        {promoting ? 'Promoting…' : 'Promote to Live'}
      </button>
      <button class="sim-btn sim-btn-clear" onclick={handleClear}>Clear</button>
    </div>
    {#if enabled && !shadowActive}
      <p class="shadow-hint">
        Enable shadow mode in <a href="/admin/settings">Settings</a> → <code>execution.shadow_mode</code>
      </p>
    {/if}
  </div>

  <!-- Shadow orders -->
  {#if orders.length > 0}
    <section class="sim-section">
      <h3>Shadow Orders</h3>
      <div class="sim-table-wrap">
        <table class="sim-table">
          <thead>
            <tr><th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th><th>Time</th><th></th></tr>
          </thead>
          <tbody>
            {#each orders as o}
              <tr>
                <td>{o.id}</td>
                <td>{o.symbol}</td>
                <td class={o.side === 'BUY' ? 'sim-buy' : 'sim-sell'}>{o.side}</td>
                <td>{o.quantity}</td>
                <td class="sim-td-mono">{o.initial_price != null ? `₹${o.initial_price.toLocaleString()}` : '—'}</td>
                <td>
                  <span class="sim-pill" class:sim-pill-ok={o.status === 'SHADOW_OK'} class:sim-pill-rej={o.status === 'SHADOW_REJECTED'}>
                    {o.status === 'SHADOW_OK' ? 'OK' : o.status === 'SHADOW_REJECTED' ? 'REJECTED' : o.status}
                  </span>
                </td>
                <td class="sim-td-mono">{o.created_at ? new Date(o.created_at).toLocaleTimeString() : '—'}</td>
                <td>
                  <button class="sim-btn-xs" onclick={() => expandedId = expandedId === o.id ? null : o.id}>
                    {expandedId === o.id ? '▾' : '▸'} Payload
                  </button>
                </td>
              </tr>
              {#if expandedId === o.id}
                <tr class="shadow-detail-row">
                  <td colspan="8">
                    <pre class="shadow-payload">{JSON.stringify(parsePayload(o.detail), null, 2) || o.detail}</pre>
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {:else if !loading}
    <p class="sim-empty">No shadow orders yet.</p>
  {/if}
</div>

<style>
  .sim-page          { max-width: 72rem; margin: 0 auto; padding: 0; }
  .sim-header        { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem; }
  .sim-header h2     { font-size: 1.25rem; font-weight: 700; color: #e2e8f0; margin: 0; }
  .badge-shadow      { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.06em;
                        padding: 0.15rem 0.5rem; border-radius: 9999px;
                        color: #fb923c; background: rgba(251,146,60,0.12); border: 1px solid rgba(251,146,60,0.25); }
  .badge-shadow-off  { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.06em;
                        padding: 0.15rem 0.5rem; border-radius: 9999px;
                        color: #94a3b8; background: rgba(148,163,184,0.10); border: 1px solid rgba(148,163,184,0.15); }

  .sim-banner        { padding: 0.5rem 0.75rem; border-radius: 0.375rem; font-size: 0.75rem; margin-bottom: 0.75rem; }
  .sim-banner-warn   { background: rgba(251,191,36,0.10); color: #fbbf24; border: 1px solid rgba(251,191,36,0.20); }
  .sim-banner-error  { background: rgba(239,68,68,0.10); color: #f87171; border: 1px solid rgba(239,68,68,0.20); }

  .sim-controls      { background: rgba(15,23,42,0.6); border: 1px solid rgba(148,163,184,0.12);
                        border-radius: 0.5rem; padding: 1rem; margin-bottom: 1rem; }
  .shadow-status-row { display: flex; gap: 2rem; margin-bottom: 0.75rem; flex-wrap: wrap; }
  .shadow-stat       { display: flex; flex-direction: column; gap: 0.15rem; }
  .sim-label         { font-size: 0.6rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; }
  .shadow-val        { font-size: 0.85rem; color: #e2e8f0; font-weight: 600; }
  .shadow-on         { color: #fb923c; }
  .shadow-off        { color: #94a3b8; }
  .shadow-hint       { font-size: 0.7rem; color: #64748b; margin-top: 0.5rem; }
  .shadow-hint a     { color: #38bdf8; text-decoration: underline; }
  .shadow-hint code  { background: rgba(148,163,184,0.12); padding: 0.1rem 0.3rem; border-radius: 0.2rem; font-size: 0.65rem; }

  .sim-btn-row       { display: flex; gap: 0.5rem; }
  .sim-btn           { padding: 0.4rem 1rem; border-radius: 0.375rem; font-size: 0.75rem; font-weight: 600;
                        cursor: pointer; border: 1px solid transparent; transition: all 0.15s; }
  .sim-btn:disabled  { opacity: 0.4; cursor: not-allowed; }
  .sim-btn-promote   { background: rgba(239,68,68,0.15); color: #f87171; border-color: rgba(239,68,68,0.3); }
  .sim-btn-promote:hover:not(:disabled) { background: rgba(239,68,68,0.25); }
  .sim-btn-clear     { background: rgba(148,163,184,0.10); color: #94a3b8; border-color: rgba(148,163,184,0.2); }
  .sim-btn-xs        { font-size: 0.6rem; padding: 0.15rem 0.4rem; background: rgba(148,163,184,0.08);
                        color: #94a3b8; border: 1px solid rgba(148,163,184,0.15); border-radius: 0.25rem; cursor: pointer; }

  .sim-section       { margin-bottom: 1.5rem; }
  .sim-section h3    { font-size: 0.85rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.5rem; }
  .sim-table-wrap    { overflow-x: auto; }
  .sim-table         { width: 100%; border-collapse: collapse; font-size: 0.72rem; }
  .sim-table th      { text-align: left; padding: 0.35rem 0.5rem; color: #94a3b8; border-bottom: 1px solid rgba(148,163,184,0.15); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-size: 0.6rem; }
  .sim-table td      { padding: 0.3rem 0.5rem; color: #e2e8f0; border-bottom: 1px solid rgba(148,163,184,0.06); }
  .sim-td-mono       { font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; }
  .sim-buy           { color: #38bdf8; }
  .sim-sell          { color: #fb923c; }
  .sim-pill          { font-size: 0.6rem; font-weight: 700; padding: 0.1rem 0.4rem; border-radius: 9999px; }
  .sim-pill-ok       { color: #4ade80; background: rgba(74,222,128,0.12); }
  .sim-pill-rej      { color: #f87171; background: rgba(239,68,68,0.12); }
  .sim-empty         { font-size: 0.75rem; color: #64748b; text-align: center; padding: 2rem; }

  .shadow-detail-row td { padding: 0; background: rgba(15,23,42,0.8); }
  .shadow-payload    { font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; color: #94a3b8;
                        padding: 0.75rem 1rem; margin: 0; white-space: pre-wrap; line-height: 1.5; }

  @media (max-width: 768px) {
    .sim-controls        { padding: 0.75rem; }
    .shadow-status-row   { gap: 1rem; }
    .sim-btn-row         { flex-wrap: wrap; }
    .sim-table-wrap      { -webkit-overflow-scrolling: touch; }
    .sim-table td,
    .sim-table th        { padding: 0.25rem 0.35rem; font-size: 0.65rem; }
    .shadow-payload      { font-size: 0.6rem; padding: 0.5rem 0.6rem; }
  }
</style>
