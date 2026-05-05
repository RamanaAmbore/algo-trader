<script>
  // Live execution dashboard (/admin/live).
  //
  // Surfaces the execution.live.* flag toggles prominently — previously
  // buried in /admin/settings. Shows the effective execution state and
  // gives the operator a clear view of which actions hit the broker.
  //
  // Enhancement: inline toggles for all 6 flags (PATCH /api/admin/settings),
  // PriceChart grid for live symbols, and LogPanel for order/agent streams.

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
  // Per-flag saving state so each toggle shows its own in-progress indicator.
  let saving         = $state(/** @type {Record<string, boolean>} */ ({}));
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

  const effectiveLabel = $derived({
    dev_paper: 'DEV PAPER',
    paper:     'PAPER',
    shadow:    'SHADOW',
    live:      'LIVE',
    mixed:     'MIXED',
  }[status?.effective] || 'UNKNOWN');

  const effectiveColor = $derived({
    dev_paper: '#94a3b8',
    paper:     '#38bdf8',
    shadow:    '#fb923c',
    live:      '#ef4444',
    mixed:     '#fbbf24',
  }[status?.effective] || '#94a3b8');

  // How many live.* flags are True (from the live status response).
  const liveCount = $derived(status?.live_count ?? 0);
  const totalFlags = $derived(status?.total_flags ?? 6);

  const flagNames = {
    cancel_order:           'Cancel Order',
    cancel_all_orders:      'Cancel All Orders',
    modify_order:           'Modify Order',
    place_order:            'Place Order',
    close_position:         'Close Position',
    chase_close_positions:  'Chase Close Positions',
  };

  // Toggle a single execution.live.* flag and re-poll.
  async function toggleFlag(/** @type {string} */ key, /** @type {boolean} */ currentVal) {
    if (!enabled) return;
    const settingsKey = `execution.live.${key}`;
    saving = { ...saving, [key]: true };
    note = '';
    error = '';
    try {
      await updateSetting(settingsKey, !currentVal);
      await load();
      note = `${flagNames[key] || key} → ${!currentVal ? 'LIVE' : 'PAPER'}`;
      setTimeout(() => { note = ''; }, 2500);
    } catch (e) {
      error = e.message;
    } finally {
      saving = { ...saving, [key]: false };
    }
  }
</script>

<svelte:head><title>Live Execution — RamboQuant</title></svelte:head>

<div class="sim-page">
  <header class="sim-header">
    <h2>
      Live Execution
      <InfoHint popup text="Controls which broker actions are live vs paper. Every action defaults to paper mode. Promote individual actions to live only after validating via Shadow mode. Toggles write directly to the DB — no deploy needed." />
    </h2>
    <span class="badge-effective" style="color: {effectiveColor}; border-color: {effectiveColor}40; background: {effectiveColor}15">
      {effectiveLabel}
    </span>
  </header>

  <!-- Safety banner — mirrors /admin/settings execution banner -->
  <div class="exec-banner
    {liveCount === 0
      ? 'exec-banner-safe'
      : 'exec-banner-live'}">
    {#if liveCount === 0}
      Every broker action is in <strong>PAPER</strong> mode — no real orders
      will hit the broker. Toggle individual flags below to promote an action.
    {:else}
      <span class="exec-warn-badge">⚠ {liveCount} of {totalFlags}</span>
      action{liveCount === 1 ? '' : 's'} are <strong>LIVE</strong> — real orders
      will hit the broker for these.
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

  <!-- Effective state summary -->
  <div class="sim-controls">
    <div class="live-grid">
      <div class="live-stat">
        <span class="sim-label">Branch</span>
        <span class="live-val">{branch}</span>
      </div>
      <div class="live-stat">
        <span class="sim-label">Paper trading mode</span>
        <span class="live-val" class:live-on={!status?.paper_trading_mode} class:live-off={status?.paper_trading_mode}>
          {status?.paper_trading_mode ? 'ON (all orders → paper)' : 'OFF'}
        </span>
      </div>
      <div class="live-stat">
        <span class="sim-label">Shadow mode</span>
        <span class="live-val" class:shadow-on={status?.shadow_mode}>
          {status?.shadow_mode ? 'ON (orders → shadow log)' : 'OFF'}
        </span>
      </div>
      <div class="live-stat">
        <span class="sim-label">Live actions</span>
        <span class="live-val" style="color: {liveCount > 0 ? '#ef4444' : '#94a3b8'}">
          {liveCount} / {totalFlags}
        </span>
      </div>
    </div>

    <!-- Per-action flag toggles -->
    <h3 class="live-flags-title">Per-Action Flags</h3>
    <div class="live-flags">
      {#if status?.live_flags}
        {#each Object.entries(status.live_flags) as [key, isLive]}
          <div class="live-flag-row">
            <span class="live-flag-name">{flagNames[key] || key}</span>
            <div class="flag-toggle-group">
              <!-- Current state pill -->
              <span class="live-flag-pill" class:flag-live={isLive} class:flag-paper={!isLive}>
                {isLive ? 'LIVE' : 'PAPER'}
              </span>
              <!-- Toggle button — disabled on dev where live flags have no effect -->
              <button
                type="button"
                class="flag-toggle-btn"
                class:flag-toggle-on={isLive}
                class:flag-toggle-off={!isLive}
                disabled={!enabled || saving[key]}
                onclick={() => toggleFlag(key, isLive)}
                title={enabled ? (isLive ? `Set ${flagNames[key] || key} → PAPER` : `Set ${flagNames[key] || key} → LIVE`) : 'Only available on prod'}
              >
                {#if saving[key]}
                  <span class="flag-toggle-dot flag-toggle-dot-saving"></span>
                {:else}
                  <span class="flag-toggle-dot" class:dot-on={isLive} class:dot-off={!isLive}></span>
                {/if}
              </button>
            </div>
          </div>
        {/each}
      {:else if loading}
        <div class="text-[0.65rem] text-[#64748b]">Loading flags…</div>
      {/if}
    </div>
  </div>

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

  <!-- Chart grid — one mini chart per live symbol with captured ticks.
       Underlyings render first (sky-blue SPOT tag); derivatives overlay
       the spot as a dashed line. Empty-state on no symbols. -->
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
  .sim-page          { max-width: 72rem; margin: 0 auto; padding: 1.5rem 1rem; }
  .sim-header        { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem; }
  .sim-header h2     { font-size: 1.25rem; font-weight: 700; color: #e2e8f0; margin: 0; }
  .badge-effective   { font-size: 0.65rem; font-weight: 700; letter-spacing: 0.06em;
                        padding: 0.15rem 0.5rem; border-radius: 9999px; border: 1px solid; }

  /* Safety banner — green/red mirrors /admin/settings execution section */
  .exec-banner {
    padding: 0.5rem 0.75rem;
    border-radius: 0.375rem;
    font-size: 0.72rem;
    margin-bottom: 0.75rem;
    border: 1px solid;
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
  .exec-warn-badge {
    display: inline-block;
    padding: 0 0.35rem;
    border-radius: 0.2rem;
    background: rgba(239,68,68,0.25);
    font-weight: 700;
    margin-right: 0.25rem;
  }

  .sim-banner        { padding: 0.5rem 0.75rem; border-radius: 0.375rem; font-size: 0.75rem; margin-bottom: 0.75rem; }
  .sim-banner-warn   { background: rgba(251,191,36,0.10); color: #fbbf24; border: 1px solid rgba(251,191,36,0.20); }
  .sim-banner-error  { background: rgba(239,68,68,0.10); color: #f87171; border: 1px solid rgba(239,68,68,0.20); }
  .sim-banner-note   { background: rgba(74,222,128,0.08); color: #86efac; border: 1px solid rgba(74,222,128,0.25); }

  .sim-controls      { background: rgba(15,23,42,0.6); border: 1px solid rgba(148,163,184,0.12);
                        border-radius: 0.5rem; padding: 1rem; margin-bottom: 1rem; }

  .live-grid         { display: flex; gap: 2rem; margin-bottom: 1rem; flex-wrap: wrap; }
  .live-stat         { display: flex; flex-direction: column; gap: 0.15rem; }
  .sim-label         { font-size: 0.6rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.04em; }
  .live-val          { font-size: 0.85rem; color: #e2e8f0; font-weight: 600; }
  .live-on           { color: #4ade80; }
  .live-off          { color: #f87171; }
  .shadow-on         { color: #fb923c; }

  .live-flags-title  { font-size: 0.75rem; font-weight: 600; color: #cbd5e1; margin-bottom: 0.4rem; }
  .live-flags        { display: flex; flex-direction: column; gap: 0.35rem; }
  .live-flag-row     { display: flex; align-items: center; justify-content: space-between;
                        padding: 0.35rem 0.6rem; background: rgba(15,23,42,0.4);
                        border-radius: 0.375rem; border: 1px solid rgba(148,163,184,0.08); }
  .live-flag-name    { font-size: 0.72rem; color: #cbd5e1; }

  /* Toggle group — pill label + toggle switch side by side */
  .flag-toggle-group { display: flex; align-items: center; gap: 0.5rem; }
  .live-flag-pill    { font-size: 0.6rem; font-weight: 700; padding: 0.1rem 0.45rem; border-radius: 9999px; }
  .flag-live         { color: #ef4444; background: rgba(239,68,68,0.12); }
  .flag-paper        { color: #38bdf8; background: rgba(56,189,248,0.10); }

  /* Toggle switch — 28×16px pill with a sliding dot */
  .flag-toggle-btn {
    position: relative;
    width: 2rem;
    height: 1.1rem;
    border-radius: 9999px;
    border: 1px solid rgba(148,163,184,0.25);
    background: rgba(15,23,42,0.6);
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
    padding: 0;
    flex-shrink: 0;
  }
  .flag-toggle-btn.flag-toggle-on {
    background: rgba(239,68,68,0.20);
    border-color: rgba(239,68,68,0.45);
  }
  .flag-toggle-btn.flag-toggle-off {
    background: rgba(56,189,248,0.10);
    border-color: rgba(56,189,248,0.25);
  }
  .flag-toggle-btn:disabled {
    opacity: 0.40;
    cursor: not-allowed;
  }
  .flag-toggle-dot {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 9999px;
    transition: left 0.15s, background 0.15s;
  }
  .flag-toggle-dot.dot-on {
    left: calc(100% - 0.7rem - 1px);
    background: #ef4444;
  }
  .flag-toggle-dot.dot-off {
    left: 1px;
    background: #38bdf8;
  }
  .flag-toggle-dot-saving {
    left: 50%;
    transform: translate(-50%, -50%);
    background: #fbbf24;
    animation: pulse-dot 0.7s ease-in-out infinite alternate;
  }
  @keyframes pulse-dot {
    from { opacity: 0.4; }
    to   { opacity: 1.0; }
  }

  /* Chart grid — same template as paper / simulator pages */
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
</style>
