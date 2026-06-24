<script>
  // System health diagnostics (/admin/health).
  //
  // One-glance "is everything healthy" view for pre-market-open checks.
  // Compact card grid — 2 columns desktop / 1 mobile. Polls every 15 s.

  import { onDestroy } from 'svelte';
  import { nowStamp, branchLabel, visibleInterval } from '$lib/stores';
  import { userRole, userCaps, hasCap } from '$lib/rbac';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import { fetchSystemHealth } from '$lib/api';
  import StaleBanner from '$lib/StaleBanner.svelte';

  /** @type {any} */
  let health      = $state(null);
  let error       = $state('');
  let loading     = $state(true);
  let teardown;

  async function load() {
    try {
      health      = await fetchSystemHealth();
      error       = '';
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  // Canonical $effect-gated auth (slice N2). view_audit admits
  // admin + risk + ops; pre-fix the page hard-redirected to /signin
  // before /whoami had a chance to hydrate the role.
  const _canView = $derived(hasCap('view_audit', $userCaps, $userRole));
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      load();
      teardown = visibleInterval(load, 15000);
    }
  });
  onDestroy(() => teardown?.());

  // ── Helpers ────────────────────────────────────────────────────────
  /** Format an integer with commas, no decimals. */
  function _n(/** @type {number|null|undefined} */ v) {
    if (v == null) return '—';
    return Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 });
  }

  /** Broker-account status pill class. */
  function _brokerPillCls(/** @type {string} */ status) {
    if (status === 'LOADED')   return 'status-loaded';
    if (status === 'PENDING')  return 'status-pending';
    return 'status-disabled';
  }
</script>

<svelte:head><title>Health | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Health</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="health" />
    <PageHeaderActions />
  </span>
</div>

{#if !_canView}
  <div class="empty-state">
    <h2>Access denied</h2>
    <p>System health requires the <code>view_audit</code> capability
       (designated, admin, or risk role). Your current role is
       <strong>{$userRole}</strong> — contact an admin to request access.</p>
  </div>
{:else}

<StaleBanner {error} hasData={!!health} label="Health snapshot" />

{#if loading && !health}
  <div class="empty-state">Loading…</div>
{:else if health}
  <div class="health-grid">

    <!-- ── Header card: branch + build + uptime ──────────────────── -->
    <div class="hcard hcard-wide">
      <div class="hcard-title">Deployment</div>
      <div class="kv-row">
        <span class="kv-key">Branch</span>
        <span class="kv-val kv-amber">{branchLabel(health.branch ?? '') || '—'}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Git hash</span>
        <span class="kv-val kv-mono">{health.git_hash ?? '—'}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Last commit</span>
        <span class="kv-val kv-muted" title={health.git_subject ?? ''}>{health.git_subject ?? '—'}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Uptime</span>
        <span class="kv-val">{health.uptime ?? '—'}</span>
      </div>
    </div>

    <!-- ── Broker accounts card ──────────────────────────────────── -->
    <div class="hcard">
      <div class="hcard-title">Broker Accounts</div>
      {#if health.broker_accounts?.length}
        {#each health.broker_accounts as b}
          <div class="broker-row">
            <span class="kv-mono">{b.account ?? '—'}</span>
            <span class="broker-key">{b.api_key_last4 ? `…${b.api_key_last4}` : '—'}</span>
            <span class="broker-ip">{b.source_ip ?? '—'}</span>
            <span class="status-pill {_brokerPillCls(b.status)}">{b.status ?? '?'}</span>
          </div>
        {/each}
      {:else}
        <div class="kv-muted">No broker accounts found.</div>
      {/if}
    </div>

    <!-- ── Database row-counts card ──────────────────────────────── -->
    <div class="hcard">
      <div class="hcard-title">Database</div>
      <div class="kv-row">
        <span class="kv-key">Users</span>
        <span class="kv-val kv-num">{_n(health.db?.users)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Agents</span>
        <span class="kv-val kv-num">{_n(health.db?.agents)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Algo orders</span>
        <span class="kv-val kv-num">{_n(health.db?.algo_orders)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Agent events</span>
        <span class="kv-val kv-num">{_n(health.db?.agent_events)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">News headlines</span>
        <span class="kv-val kv-num">{_n(health.db?.news_headlines)}</span>
      </div>
    </div>

    <!-- ── Cache card ────────────────────────────────────────────── -->
    <div class="hcard">
      <div class="hcard-title">Cache</div>
      <div class="kv-row">
        <span class="kv-key">Keys</span>
        <span class="kv-val kv-num">{_n(health.cache?.key_count)}</span>
      </div>
      {#if health.cache?.keys?.length}
        <div class="cache-keys">
          {#each health.cache.keys as k}
            <span class="cache-key-chip">{k}</span>
          {/each}
        </div>
      {:else}
        <div class="kv-muted">Cache is empty.</div>
      {/if}
    </div>

    <!-- ── Simulator card ────────────────────────────────────────── -->
    <div class="hcard">
      <div class="hcard-title">Simulator</div>
      {#if health.sim?.enabled === false}
        <div class="kv-row">
          <span class="kv-key">Status</span>
          <span class="status-pill status-disabled">GATED</span>
        </div>
        <div class="kv-muted" style="margin-top:0.3rem">
          Simulator is off on this branch (<span class="kv-mono">{branchLabel(health.branch) || '?'}</span>).
        </div>
      {:else if health.sim?.active}
        <div class="kv-row">
          <span class="kv-key">Status</span>
          <span class="status-pill status-loaded">ACTIVE</span>
        </div>
        <div class="kv-row">
          <span class="kv-key">Scenario</span>
          <span class="kv-val kv-amber">{health.sim.scenario ?? '—'}</span>
        </div>
        <div class="kv-row">
          <span class="kv-key">Ticks</span>
          <span class="kv-val kv-num">{_n(health.sim.tick_index)}</span>
        </div>
      {:else}
        <div class="kv-row">
          <span class="kv-key">Status</span>
          <span class="status-pill status-pending">IDLE</span>
        </div>
      {/if}
    </div>

    <!-- ── Paper engine card ─────────────────────────────────────── -->
    <div class="hcard">
      <div class="hcard-title">Paper Engine</div>
      {#if health.paper?.enabled === false}
        <div class="kv-row">
          <span class="kv-key">Status</span>
          <span class="status-pill status-disabled">DEV</span>
        </div>
        <div class="kv-muted" style="margin-top:0.3rem">
          Paper engine is not running on this branch.
        </div>
      {:else}
        <div class="kv-row">
          <span class="kv-key">Status</span>
          <span class="status-pill status-loaded">ENABLED</span>
        </div>
        <div class="kv-row">
          <span class="kv-key">Open orders</span>
          <span class="kv-val kv-num">{_n(health.paper?.open_order_count)}</span>
        </div>
      {/if}
    </div>

    <!-- ── KiteTicker card ───────────────────────────────────────── -->
    <div class="hcard hcard-wide">
      <div class="hcard-title">KiteTicker (live WebSocket)</div>
      <div class="kv-row">
        <span class="kv-key">Status</span>
        {#if health.ticker?.connected}
          <span class="status-pill status-loaded">CONNECTED</span>
        {:else if health.ticker?.started}
          <span class="status-pill status-pending">STARTED · DISCONNECTED</span>
        {:else}
          <span class="status-pill status-disabled">NOT STARTED</span>
        {/if}
      </div>
      <div class="kv-row">
        <span class="kv-key">Subscribed tokens</span>
        <span class="kv-val kv-num">{_n(health.ticker?.subscribed_count)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Live tick map size</span>
        <span class="kv-val kv-num">{_n(health.ticker?.ticks_held)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">Stale (&gt;60s old) tokens</span>
        <span class="kv-val kv-num" class:stale-warn={(health.ticker?.stale_count ?? 0) > 0}>
          {_n(health.ticker?.stale_count)}
        </span>
      </div>
      {#if (health.ticker?.max_age_seconds ?? 0) > 0}
        <div class="kv-row">
          <span class="kv-key">Oldest tick age</span>
          <span class="kv-val kv-num">{Math.round(health.ticker.max_age_seconds)}s</span>
        </div>
      {/if}
      {#if health.ticker?.stale_top?.length}
        <div class="ticker-stale-list">
          <div class="ticker-stale-label">Worst offenders:</div>
          {#each health.ticker.stale_top as entry}
            <div class="ticker-stale-row">{entry}</div>
          {/each}
        </div>
      {/if}
    </div>

    <!-- ── IPv6 source IPs card ──────────────────────────────────── -->
    <div class="hcard hcard-wide">
      <div class="hcard-title">IPv6 Bindings</div>
      {#if health.broker_accounts?.filter(b => b.source_ip)?.length}
        {#each health.broker_accounts.filter(b => b.source_ip) as b}
          <div class="ip-row">
            <span class="kv-mono kv-amber">{b.account ?? '—'}</span>
            <span class="ip-addr">{b.source_ip}</span>
          </div>
        {/each}
      {:else}
        <div class="kv-muted">No source_ip bindings configured.</div>
      {/if}
    </div>

  </div>
{:else}
  <div class="empty-state">No health data available.</div>
{/if}

{/if}

<style>
  /* Scoped .page-header override removed — was shadowing the
     layout's :global(.page-header) fixed-strip rule due to
     Svelte's class-hash specificity. All algo pages now share
     the same sticky-strip behavior from +layout.svelte. */
  .err-banner {
    margin-bottom: 0.5rem;
    padding: 0.25rem 0.65rem;
    border-radius: 3px;
    background: rgba(248,113,113,0.12);
    border: 1px solid rgba(248,113,113,0.35);
    color: #f87171;
    font-size: 0.65rem;
  }

  /* ── Card grid ──────────────────────────────────────────────────── */
  .health-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0.55rem;
  }
  @media (max-width: 768px) {
    .health-grid { grid-template-columns: 1fr; }
  }
  .hcard {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 4px;
    padding: 0.55rem 0.7rem;
  }
  .hcard-wide {
    grid-column: 1 / -1;
  }
  .hcard-title {
    font-size: 0.6rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    margin-bottom: 0.45rem;
    padding-bottom: 0.3rem;
    border-bottom: 1px solid rgba(251,191,36,0.2);
  }

  /* KV rows */
  .kv-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 0.4rem;
    padding: 0.18rem 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .kv-row:last-child { border-bottom: none; }
  .kv-key {
    font-size: 0.6rem;
    color: var(--algo-muted);
    font-weight: 500;
    flex: 0 0 auto;
  }
  .kv-val {
    font-size: 0.68rem;
    color: var(--algo-slate);
    text-align: right;
    flex: 1 1 auto;
    font-variant-numeric: tabular-nums;
  }
  .kv-num { font-variant-numeric: tabular-nums; text-align: right; }
  .kv-amber  { color: #fbbf24 !important; }
  .kv-mono   { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.65rem; }
  .kv-muted  { font-size: 0.6rem; color: #4a5a70; font-style: italic; }

  /* Broker rows */
  .broker-row {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.2rem 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
    font-size: 0.65rem;
  }
  .broker-row:last-child { border-bottom: none; }
  .broker-key {
    font-size: 0.6rem;
    color: var(--algo-muted);
    flex: 1 1 auto;
  }
  .broker-ip {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.6rem;
    color: #7dd3fc;
    flex: 2 1 auto;
    text-align: right;
  }

  /* Status pills */
  .status-pill {
    display: inline-block;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.07em;
    padding: 0.1rem 0.45rem;
    border-radius: 2px;
    text-transform: uppercase;
    white-space: nowrap;
    flex: 0 0 auto;
  }
  .status-loaded   { background: rgba(74,222,128,0.16); color: #4ade80; border: 1px solid rgba(74,222,128,0.45); }
  .status-pending  { background: rgba(251,191,36,0.16); color: #fbbf24; border: 1px solid rgba(251,191,36,0.45); }
  .status-disabled { background: rgba(148,163,184,0.12); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }

  /* Cache keys */
  .cache-keys {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    margin-top: 0.35rem;
  }
  .cache-key-chip {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.55rem;
    padding: 0.08rem 0.35rem;
    border-radius: 2px;
    background: rgba(125,211,252,0.08);
    color: #7dd3fc;
    border: 1px solid rgba(125,211,252,0.22);
  }

  /* IPv6 rows */
  .ip-row {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    padding: 0.2rem 0;
    border-bottom: 1px solid rgba(255,255,255,0.04);
  }
  .ip-row:last-child { border-bottom: none; }
  .ip-addr {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.65rem;
    color: #7dd3fc;
  }

  /* Ticker card — staleness list + warn-tint on the stale_count cell */
  .stale-warn {
    color: #fbbf24;
    font-weight: 700;
  }
  .ticker-stale-list {
    margin-top: 0.55rem;
    padding-top: 0.45rem;
    border-top: 1px solid rgba(255,255,255,0.05);
    max-height: 12rem;
    overflow-y: auto;
  }
  .ticker-stale-label {
    font-size: 0.55rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #94a3b8;
    margin-bottom: 0.3rem;
  }
  .ticker-stale-row {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.62rem;
    color: #c8d8f0;
    padding: 0.1rem 0;
  }

  /* Empty state */
  .empty-state {
    padding: 2rem;
    text-align: center;
    color: #4a5a70;
    font-size: 0.7rem;
    font-family: ui-monospace, monospace;
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 4px;
  }
</style>
