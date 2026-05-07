<script>
  // Live execution dashboard (/admin/live).
  //
  // Single master toggle: execution.paper_trading_mode (bool, default true).
  // When paper_trading_mode = true  → every broker action lands as paper.
  // When paper_trading_mode = false → real Kite orders fire on every rule.
  //
  // Keeps the PriceChart grid and LogPanel embeds from Wave E.

  import { onMount, onDestroy } from 'svelte';
  import { authStore, visibleInterval, branchLabel } from '$lib/stores';
  import {
    fetchLiveStatus, fetchAlgoOrdersRecent,
    updateSetting,
    fetchChartSymbols, fetchChartBatch,
  } from '$lib/api';
  import LogPanel   from '$lib/LogPanel.svelte';
  import PriceChart from '$lib/PriceChart.svelte';
  import InfoHint   from '$lib/InfoHint.svelte';

  let status         = $state(/** @type {any} */ ({}));
  let orders         = $state(/** @type {any[]} */ ([]));
  let chartSymbols   = $state(/** @type {string[]} */ ([]));
  /** @type {Array<{symbol:string, kind:string, underlying:string|null}>} */
  let chartItems     = $state([]);
  let chartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let error          = $state('');
  let note           = $state('');
  let loading        = $state(true);
  let saving         = $state(false);
  // Confirmation modal state for switching to LIVE (paper_trading_mode → false).
  let confirmOpen    = $state(false);
  let confirmInput   = $state('');
  let refreshTeardown;

  async function load() {
    try {
      const [stat, ord, syms] = await Promise.all([
        fetchLiveStatus(),
        fetchAlgoOrdersRecent(50, 'live').catch(() => []),
        fetchChartSymbols('live').catch(() => ({ items: [], symbols: [] })),
      ]);
      status       = stat;
      orders       = ord || [];
      chartSymbols = syms?.symbols || [];
      chartItems   = syms?.items   || [];

      if (chartSymbols.length) {
        try {
          const batch = await fetchChartBatch('live', chartSymbols);
          const map = /** @type {Record<string, any>} */ ({});
          for (const c of (batch?.charts || [])) map[c.symbol] = c;
          chartsBySymbol = map;
        } catch (_) { /* charts fall back to per-chart self-poll */ }
      } else {
        chartsBySymbol = {};
      }

      error = '';
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

  const enabled = $derived(status?.enabled !== false);
  const branch  = $derived(branchLabel(status?.branch || ''));

  // paper_trading_mode: true = PAPER (safe), false = LIVE (real orders).
  const isPaper = $derived(status?.paper_trading_mode !== false);

  const effectiveLabel = $derived({
    dev_paper: 'DEV PAPER',
    paper:     'PAPER',
    shadow:    'SHADOW',
    live:      'LIVE',
    mixed:     'MIXED',
  }[status?.effective_mode] || 'UNKNOWN');

  const effectiveColor = $derived({
    dev_paper: '#94a3b8',
    paper:     '#38bdf8',
    shadow:    '#fb923c',
    live:      '#4ade80',
    mixed:     '#fbbf24',
  }[status?.effective_mode] || '#94a3b8');

  // Click the master toggle:
  //   PAPER → LIVE requires typed confirmation.
  //   LIVE  → PAPER flips immediately (safe direction).
  function handleToggleClick() {
    if (!enabled) return;
    if (isPaper) {
      // Going to LIVE — open confirmation modal.
      confirmInput = '';
      confirmOpen  = true;
    } else {
      // Going to PAPER — safe, no confirmation needed.
      commitToggle(true);
    }
  }

  async function commitToggle(/** @type {boolean} */ newPaperMode) {
    saving = true;
    error  = '';
    note   = '';
    try {
      await updateSetting('execution.paper_trading_mode', newPaperMode);
      await load();
      note = newPaperMode ? 'Switched to PAPER mode.' : 'Switched to LIVE mode.';
      setTimeout(() => { note = ''; }, 3000);
    } catch (e) {
      error = e.message;
    } finally {
      saving = false;
    }
  }

  function confirmGoLive() {
    if (confirmInput.trim() !== 'LIVE') return;
    confirmOpen = false;
    commitToggle(false);
  }

  function cancelConfirm() {
    confirmOpen  = false;
    confirmInput = '';
  }
</script>

<svelte:head><title>Live Execution — RamboQuant</title></svelte:head>

<div class="sim-page">
  <header class="sim-header">
    <h2>
      Live Execution
      <InfoHint popup text="Master toggle for live vs paper mode. PAPER (default) — every broker-hitting action lands as a paper trade; no real orders. LIVE — real Kite orders fire on every agent rule and operator ticket submit. Toggles write directly to the DB — no deploy needed." />
    </h2>
    <span class="badge-effective" style="color: {effectiveColor}; border-color: {effectiveColor}40; background: {effectiveColor}15">
      {effectiveLabel}
    </span>
  </header>

  <!-- Safety banner -->
  <div class="exec-banner {isPaper ? 'exec-banner-safe' : 'exec-banner-live'}">
    {#if isPaper}
      Every broker action is in <strong>PAPER</strong> mode — no real orders will hit the broker.
    {:else}
      <span class="exec-warn-icon">⚠</span>
      <strong>Live mode</strong> — real Kite orders fire on every agent rule and operator ticket submit.
    {/if}
  </div>

  {#if !enabled}
    <div class="sim-banner sim-banner-warn">
      Live execution is only available on <strong>prod</strong>. Current branch: <strong>{branch}</strong>. All actions are paper on dev.
    </div>
  {/if}

  {#if error}
    <div class="sim-banner sim-banner-error">{error}</div>
  {/if}
  {#if note}
    <div class="sim-banner sim-banner-note">{note}</div>
  {/if}

  <!-- Master toggle card -->
  <div class="sim-controls">
    <div class="live-grid">
      <div class="live-stat">
        <span class="sim-label">Branch</span>
        <span class="live-val">{branch}</span>
      </div>
      <div class="live-stat">
        <span class="sim-label">Shadow mode</span>
        <span class="live-val" class:shadow-on={status?.shadow_mode}>
          {status?.shadow_mode ? 'ON (orders → shadow log)' : 'OFF'}
        </span>
      </div>
      <div class="live-stat">
        <span class="sim-label">Effective mode</span>
        <span class="live-val" style="color: {effectiveColor}">{effectiveLabel}</span>
      </div>
    </div>

    <!-- Hero toggle -->
    <div class="master-toggle-wrap">
      <div class="master-toggle-label">
        <span class="master-toggle-title">Paper trading mode</span>
        <span class="master-toggle-sub {isPaper ? 'sub-paper' : 'sub-live'}">
          {#if isPaper}
            <span class="sub-icon">✓</span> PAPER — all actions safe
          {:else}
            <span class="sub-icon">⚠</span> LIVE — real broker orders
          {/if}
        </span>
      </div>
      <button
        type="button"
        class="master-toggle-btn"
        class:toggle-paper={isPaper}
        class:toggle-live={!isPaper}
        disabled={!enabled || saving}
        onclick={handleToggleClick}
        title={enabled
          ? (isPaper ? 'Click to switch to LIVE mode' : 'Click to switch back to PAPER mode')
          : 'Only available on prod'}
      >
        {#if saving}
          <span class="master-dot master-dot-saving"></span>
        {:else}
          <span class="master-dot" class:dot-paper={isPaper} class:dot-live={!isPaper}></span>
        {/if}
      </button>
    </div>
  </div>

  <!-- Confirmation modal — only shown when switching paper → live -->
  {#if confirmOpen}
    <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
    <div class="modal-overlay" role="presentation" onclick={cancelConfirm}>
      <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
      <div class="modal-box" role="dialog" aria-modal="true" tabindex="-1" onclick={(e) => e.stopPropagation()}>
        <h3 class="modal-title">Switch to LIVE mode?</h3>
        <p class="modal-body">
          Real Kite orders will fire from agent rules and the order ticket.
          Type <strong>LIVE</strong> below to confirm.
        </p>
        <input
          class="modal-input"
          type="text"
          placeholder="Type LIVE to confirm"
          bind:value={confirmInput}
          onkeydown={(e) => { if (e.key === 'Enter') confirmGoLive(); if (e.key === 'Escape') cancelConfirm(); }}
        />
        <div class="modal-actions">
          <button type="button" class="modal-btn modal-btn-cancel" onclick={cancelConfirm}>Cancel</button>
          <button
            type="button"
            class="modal-btn modal-btn-confirm"
            disabled={confirmInput.trim() !== 'LIVE'}
            onclick={confirmGoLive}
          >Go LIVE</button>
        </div>
      </div>
    </div>
  {/if}

  <!-- Recent live orders table -->
  {#if orders.length > 0}
    <section class="sim-section">
      <h3>Recent Live Orders ({orders.length})</h3>
      <div class="sim-table-wrap">
        <table class="sim-table">
          <thead>
            <tr><th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th><th>Broker ID</th><th>Time</th></tr>
          </thead>
          <tbody>
            {#each orders as o}
              <tr>
                <td>{o.id}</td>
                <td>{o.symbol}</td>
                <td class={o.transaction_type === 'BUY' ? 'sim-buy' : 'sim-sell'}>{o.transaction_type}</td>
                <td>{o.quantity}</td>
                <td class="sim-td-mono">{o.initial_price != null ? `₹${o.initial_price.toLocaleString()}` : '—'}</td>
                <td><span class="sim-pill sim-pill-live">{o.status}</span></td>
                <td class="sim-td-mono">{o.broker_order_id || '—'}</td>
                <td class="sim-td-mono">{o.created_at ? new Date(o.created_at).toLocaleTimeString() : '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {:else if !loading}
    <p class="sim-empty">No live orders yet.</p>
  {/if}

  <!-- Chart grid — one mini chart per live symbol with captured ticks -->
  {#if chartSymbols.length}
    <div class="live-charts">
      {#each chartSymbols as sym (sym)}
        <PriceChart mode="live" symbol={sym} height={170}
                    data={chartsBySymbol[sym]}
                    {chartsBySymbol} />
      {/each}
    </div>
  {:else if !loading}
    <div class="sim-empty-charts">
      No symbols with captured ticks yet. Charts populate when live orders are
      placed and the chase loop records quotes.
    </div>
  {/if}

  <!-- LogPanel — Order tab scoped to live mode -->
  <LogPanel
    heightClass="h-[40vh]"
    initialTab="order"
    cmdHistory={[]}
    orderLog={[]}
    orderRows={orders}
    agentLog={[]}
    systemLog={[]}
    simLog={[]}
  />
</div>

<style>
  .sim-page          { max-width: 72rem; margin: 0 auto; padding: 0; }
  .sim-header        { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem; }
  .sim-header h2     { font-size: 1.25rem; font-weight: 700; color: #e2e8f0; margin: 0; }
  .badge-effective   { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.06em;
                        padding: 0.15rem 0.5rem; border-radius: 9999px; border: 1px solid; }

  /* Safety banner */
  .exec-banner {
    padding: 0.5rem 0.75rem;
    border-radius: 0.375rem;
    font-size: 0.72rem;
    margin-bottom: 0.75rem;
    border: 1px solid;
    display: flex;
    align-items: center;
    gap: 0.35rem;
    flex-wrap: wrap;
  }
  .exec-banner-safe {
    background: rgba(74,222,128,0.08);
    color: #86efac;
    border-color: rgba(74,222,128,0.25);
  }
  .exec-banner-live {
    background: rgba(239,68,68,0.10);
    color: #fca5a5;
    border-color: rgba(239,68,68,0.30);
  }
  .exec-warn-icon {
    font-style: normal;
    font-weight: 700;
  }

  .sim-banner        { padding: 0.5rem 0.75rem; border-radius: 0.375rem; font-size: 0.75rem; margin-bottom: 0.75rem; }
  .sim-banner-warn   { background: rgba(251,191,36,0.10); color: #fbbf24; border: 1px solid rgba(251,191,36,0.20); }
  .sim-banner-error  { background: rgba(239,68,68,0.10); color: #f87171; border: 1px solid rgba(239,68,68,0.20); }
  .sim-banner-note   { background: rgba(74,222,128,0.08); color: #86efac; border: 1px solid rgba(74,222,128,0.25); }

  .sim-controls      { background: rgba(15,23,42,0.6); border: 1px solid rgba(148,163,184,0.12);
                        border-radius: 0.5rem; padding: 1rem; margin-bottom: 1rem; }

  .live-grid         { display: flex; gap: 2rem; margin-bottom: 1.25rem; flex-wrap: wrap; }
  .live-stat         { display: flex; flex-direction: column; gap: 0.15rem; }
  .sim-label         { font-size: 0.6rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; }
  .live-val          { font-size: 0.85rem; color: #e2e8f0; font-weight: 600; }
  .shadow-on         { color: #fb923c; }

  /* Hero master toggle */
  .master-toggle-wrap {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: rgba(15,23,42,0.5);
    border-radius: 0.5rem;
    border: 1px solid rgba(148,163,184,0.12);
  }
  .master-toggle-label { display: flex; flex-direction: column; gap: 0.3rem; }
  .master-toggle-title {
    font-size: 0.85rem;
    font-weight: 700;
    color: #cbd5e1;
    letter-spacing: 0.02em;
  }
  .master-toggle-sub {
    font-size: 0.72rem;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }
  .sub-paper  { color: #86efac; }
  .sub-live   { color: #fca5a5; }
  .sub-icon   { font-style: normal; }

  /* Toggle switch — 52×28px hero pill */
  .master-toggle-btn {
    position: relative;
    width: 3.25rem;
    height: 1.75rem;
    border-radius: 9999px;
    border: 2px solid;
    cursor: pointer;
    transition: background 0.2s, border-color 0.2s;
    padding: 0;
    flex-shrink: 0;
  }
  .master-toggle-btn.toggle-paper {
    background: rgba(56,189,248,0.12);
    border-color: rgba(56,189,248,0.35);
  }
  .master-toggle-btn.toggle-live {
    background: rgba(239,68,68,0.18);
    border-color: rgba(239,68,68,0.50);
  }
  .master-toggle-btn:disabled {
    opacity: 0.40;
    cursor: not-allowed;
  }
  .master-dot {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 1.1rem;
    height: 1.1rem;
    border-radius: 9999px;
    transition: left 0.2s, background 0.2s;
  }
  .master-dot.dot-paper {
    left: 3px;
    background: #38bdf8;
    box-shadow: 0 0 8px rgba(56,189,248,0.6);
  }
  .master-dot.dot-live {
    left: calc(100% - 1.1rem - 3px);
    background: #ef4444;
    box-shadow: 0 0 8px rgba(239,68,68,0.6);
  }
  .master-dot-saving {
    left: 50%;
    transform: translate(-50%, -50%);
    background: #fbbf24;
    animation: pulse-dot 0.7s ease-in-out infinite alternate;
  }
  @keyframes pulse-dot {
    from { opacity: 0.4; }
    to   { opacity: 1.0; }
  }

  /* Confirmation modal */
  .modal-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.65);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 200;
  }
  .modal-box {
    background: #0f172a;
    border: 1px solid rgba(239,68,68,0.40);
    border-radius: 0.5rem;
    padding: 1.5rem;
    width: min(90vw, 26rem);
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  .modal-title {
    font-size: 1rem;
    font-weight: 700;
    color: #f87171;
    margin: 0;
  }
  .modal-body {
    font-size: 0.78rem;
    color: #cbd5e1;
    line-height: 1.5;
    margin: 0;
  }
  .modal-input {
    background: rgba(15,23,42,0.9);
    border: 1px solid rgba(148,163,184,0.25);
    border-radius: 0.375rem;
    padding: 0.4rem 0.6rem;
    color: #e2e8f0;
    font-size: 0.8rem;
    width: 100%;
  }
  .modal-input:focus { outline: none; border-color: rgba(239,68,68,0.5); }
  .modal-actions { display: flex; justify-content: flex-end; gap: 0.5rem; }
  .modal-btn {
    padding: 0.4rem 1rem;
    border-radius: 0.375rem;
    font-size: 0.75rem;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid transparent;
    transition: all 0.15s;
  }
  .modal-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .modal-btn-cancel {
    background: rgba(148,163,184,0.10);
    color: #94a3b8;
    border-color: rgba(148,163,184,0.2);
  }
  .modal-btn-confirm {
    background: rgba(239,68,68,0.15);
    color: #f87171;
    border-color: rgba(239,68,68,0.35);
  }
  .modal-btn-confirm:hover:not(:disabled) { background: rgba(239,68,68,0.28); }

  /* Chart grid */
  .live-charts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

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
  .sim-pill-live     { color: #4ade80; background: rgba(74,222,128,0.12); }
  .sim-empty         { font-size: 0.75rem; color: #64748b; text-align: center; padding: 2rem; }
  .sim-empty-charts  { font-size: 0.65rem; color: #64748b; font-style: italic; margin-bottom: 0.75rem; }

  /* Mobile */
  @media (max-width: 768px) {
    .live-grid           { gap: 1rem; }
    .master-toggle-wrap  { flex-direction: column; align-items: flex-start; gap: 0.75rem; }
    .live-charts         { grid-template-columns: 1fr; }
    .sim-controls        { padding: 0.75rem; }
    .sim-table td,
    .sim-table th        { padding: 0.25rem 0.35rem; font-size: 0.65rem; }
  }
</style>
