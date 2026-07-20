<script>
  // System health diagnostics (/admin/health).
  //
  // One-glance "is everything healthy" view for pre-market-open checks.
  // Compact card grid — 2 columns desktop / 1 mobile. Polls every 15 s.

  import { onDestroy } from 'svelte';
  import { branchLabel, visibleInterval } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import { userRole, userCaps, userCapsReady, hasCap } from '$lib/rbac';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import { fetchSystemHealth, invalidatePersistence } from '$lib/api';
  import { applyPersistenceMode } from '$lib/data/refreshCycle';
  import StaleBanner from '$lib/StaleBanner.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

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

  // Canonical $effect-gated auth. view_audit admits designated + admin + risk.
  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values.
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const _canView = $derived(hasCap('view_audit', _caps, _role));
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      load();
      // Throttle to 60 s on hidden — system health is critical for
      // operator awareness; a slow heartbeat beats going dark entirely.
      teardown = visibleInterval(load, 15000, 'throttle:60000');
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

  // ── Persistence refresh-cycle mode (slice Z) ────────────────────────
  // Three states the operator picks to recover the cache+DB tiers when
  // they've shipped bad data:
  //   off  — normal hierarchy
  //   soft — bypass cache+DB on read; broker fetch + write-back heals
  //   hard — soft + ticker recycle (in-memory _tick_map rebuilt)
  let _modeBusy   = $state(false);
  const _persMode = $derived(health?.persistence?.mode || 'off');
  async function _switchMode(/** @type {'off'|'soft'|'hard'} */ next) {
    if (_modeBusy) return;
    const cur = health?.persistence?.mode || 'off';
    if (next === cur) return;
    _modeBusy = true;
    try {
      const r = await applyPersistenceMode(next);
      await load();
      const cleared = r?.frontend_cleared ?? 0;
      toast.success(
        cleared > 0
          ? `Refresh mode: ${next.toUpperCase()} — ${cleared.toLocaleString('en-IN')} cached symbols cleared`
          : `Refresh mode: ${next.toUpperCase()}`
      );
    } catch (e) {
      toast.error((e && e.message) || 'Mode switch failed');
    } finally {
      _modeBusy = false;
    }
  }

  // Per-store invalidate — wipes in-memory + DB for the chosen store.
  // The store re-fetches from broker on the next read and the queue
  // worker writes back through. Defect-recovery counterpart to mode
  // switching for cases where the operator wants targeted cleanup.
  let _invalBusy  = $state(false);
  async function _invalidate(/** @type {string} */ store) {
    if (_invalBusy) return;
    _invalBusy = true;
    try {
      const r = await invalidatePersistence(store);
      await load();
      const rows = r?.rows_deleted ?? 0;
      toast.success(`Invalidated ${r?.store ?? store} — ${rows.toLocaleString('en-IN')} rows deleted`);
    } catch (e) {
      toast.error((e && e.message) || 'Invalidate failed');
    } finally {
      _invalBusy = false;
    }
  }
</script>

<svelte:head><title>Health | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Health</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="health" />
    <PageHeaderActions />
  </span>
</div>

{#if !$userCapsReady}
  <!-- RBAC bootstrap still in-flight — show a skeleton so a legitimate
       operator never sees the access-denied panel as a false-positive. -->
  <LoadingSkeleton variant="card" rows={3} />
{:else if !_canView}
  <EmptyState title="Access denied" icon="lock">
    {#snippet hintBody()}
      System health requires the <code>view_audit</code> capability
      (designated, admin, or risk role). Your current role is
      <strong>{$userRole}</strong> — contact an admin to request access.
    {/snippet}
  </EmptyState>
{:else}

<StaleBanner {error} hasData={!!health} label="Health snapshot" />

{#if loading && !health}
  <!-- Skeleton grid matches the 2-col card layout so the loading
       state occupies the same visual footprint as the real content. -->
  <div class="health-grid">
    {#each Array(6) as _}
      <LoadingSkeleton variant="card" rows={4} />
    {/each}
  </div>
{:else if health}
  <div class="health-grid content-fade-in">

    <!-- ── Header card: branch + build + uptime ──────────────────── -->
    <div class="algo-card algo-card-wide">
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
    <div class="algo-card">
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
    <div class="algo-card">
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
    <div class="algo-card">
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
    <div class="algo-card">
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
    <div class="algo-card">
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
    <div class="algo-card algo-card-wide">
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

    <!-- ── Persistence refresh-cycle mode card (slice Z) ─────────── -->
    <div class="algo-card algo-card-wide">
      <div class="hcard-title">Persistence refresh cycle</div>
      <div class="kv-row">
        <span class="kv-key">Mode</span>
        {#if _persMode === 'off'}
          <span class="status-pill status-loaded">OFF · normal</span>
        {:else if _persMode === 'soft'}
          <span class="status-pill status-pending">SOFT · bypass cache+DB</span>
        {:else}
          <span class="status-pill status-disabled">HARD · bypass + ticker recycle</span>
        {/if}
      </div>
      <div class="persistence-modes">
        <button type="button"
                class="pm-btn pm-off"
                class:pm-on={_persMode === 'off'}
                disabled={_modeBusy}
                title="Normal hierarchy. Stores read cache → DB → broker."
                onclick={() => _switchMode('off')}>OFF</button>
        <button type="button"
                class="pm-btn pm-soft"
                class:pm-on={_persMode === 'soft'}
                disabled={_modeBusy}
                title="Non-ticker stores bypass cache + DB. Every read hits broker; write-back heals the persistent tiers. Live ticker stream untouched."
                onclick={() => _switchMode('soft')}>SOFT</button>
        <button type="button"
                class="pm-btn pm-hard"
                class:pm-on={_persMode === 'hard'}
                disabled={_modeBusy}
                title="Soft + ticker recycle. The in-memory _tick_map rebuilds from scratch on transition. Brief LTP gap (~2-3s) — SSE clients auto-reconnect."
                onclick={() => _switchMode('hard')}>HARD</button>
      </div>
      <div class="kv-row">
        <span class="kv-key">disk_queue depth</span>
        <span class="kv-val kv-num">{_n(health.persistence?.disk_queue?.depth)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">db_queue depth</span>
        <span class="kv-val kv-num">{_n(health.persistence?.db_queue?.depth)}</span>
      </div>
      <div class="kv-row">
        <span class="kv-key">disk worker</span>
        {#if health.persistence?.disk_queue?.worker_alive}
          <span class="status-pill status-loaded">ALIVE</span>
        {:else}
          <span class="status-pill status-disabled">DEAD</span>
        {/if}
      </div>
      <div class="kv-row">
        <span class="kv-key">db worker</span>
        {#if health.persistence?.db_queue?.worker_alive}
          <span class="status-pill status-loaded">ALIVE</span>
        {:else}
          <span class="status-pill status-disabled">DEAD</span>
        {/if}
      </div>
      {#if health.persistence?.stores}
        <div class="kv-row kv-section">
          <span class="kv-key">Store tier metrics</span>
          <span class="kv-val kv-muted">hit% · keys · t1/t2/t3</span>
        </div>
        {#each Object.entries(health.persistence.stores) as [k, v]}
          {@const hitPct = v?.hit_rate != null ? Math.round(v.hit_rate * 100) + '%' : '—'}
          {@const hitCls = v?.hit_rate == null ? '' : v.hit_rate >= 0.8 ? 'cell-pos' : v.hit_rate >= 0.5 ? 'cell-amber' : 'cell-neg'}
          <div class="kv-row kv-indent">
            <span class="kv-key kv-mono">{k}</span>
            <span class="kv-val kv-mono kv-tier-pct {hitCls}">{hitPct}</span>
            <span class="kv-val kv-mono kv-tier-counts">
              {_n(v?.mem_keys)} · {_n(v?.tier1_hits)}/{_n(v?.tier2_hits)}/{_n(v?.tier3_fetches)}
            </span>
            <button type="button" class="pm-inval-btn"
                    disabled={_invalBusy}
                    title="Wipe in-memory + delete DB rows for {k}. Next read re-fetches from broker. ({_n(v?.tier3_errors)} broker errors so far this process)"
                    onclick={() => _invalidate(k)}>Invalidate</button>
          </div>
        {/each}
        <div class="kv-row kv-indent">
          <span class="kv-key kv-mono">all</span>
          <button type="button" class="pm-inval-btn pm-inval-all"
                  disabled={_invalBusy}
                  title="Wipe every store. Heavy — only use when the entire persistence layer is suspect."
                  onclick={() => _invalidate('all')}>Invalidate all</button>
        </div>
      {/if}
    </div>

    <!-- ── IPv6 source IPs card ──────────────────────────────────── -->
    <div class="algo-card algo-card-wide">
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
  <EmptyState
    title="No health data available"
    hint="Health endpoint returned successfully but the snapshot is empty. Try refresh."
    icon="chart"
  />
{/if}

{/if}

<style>
  /* Scoped .page-header override removed — was shadowing the
     layout's :global(.page-header) fixed-strip rule due to
     Svelte's class-hash specificity. All algo pages now share
     the same sticky-strip behavior from +layout.svelte. */

  /* ── Card grid ──────────────────────────────────────────────────── */
  .health-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 0.55rem;
  }
  @media (max-width: 768px) {
    .health-grid { grid-template-columns: 1fr; }
  }
  /* .hcard / .hcard-wide migrated to canonical .algo-card /
     .algo-card-wide — the chrome (gradient, border, radius,
     padding) was a pixel-perfect duplicate of the canonical class.
     .hcard-title kept as a page-local title decoration since its
     amber underline accent is intentionally distinct from
     .algo-card-title (which is slate, no underline). */
  .hcard-title {
    font-size: var(--fs-sm);
    font-weight: 700;
    color: var(--c-action);
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
    font-size: 0.72rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(126,151,184,0.10);
  }
  .kv-row:last-child { border-bottom: none; }
  .kv-key {
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    font-weight: 500;
    flex: 0 0 auto;
  }
  .kv-val {
    font-size: var(--fs-md);
    color: var(--algo-slate);
    text-align: right;
    flex: 1 1 auto;
    font-variant-numeric: tabular-nums;
  }
  .kv-num { font-variant-numeric: tabular-nums; text-align: right; }
  .kv-amber  { color: var(--c-action) !important; }
  .kv-mono   { font-family: var(--font-numeric); font-size: var(--fs-md); }
  .kv-muted  { font-size: var(--fs-sm); color: #4a5a70; font-style: italic; }

  /* Broker rows */
  .broker-row {
    display: flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.2rem 0;
    font-size: 0.72rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(126,151,184,0.10);
  }
  .broker-row:last-child { border-bottom: none; }
  .broker-key {
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    flex: 1 1 auto;
  }
  .broker-ip {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: #7dd3fc;
    flex: 2 1 auto;
    text-align: right;
  }

  /* Status pills */
  .status-pill {
    display: inline-block;
    font-size: var(--fs-2xs);
    font-weight: 700;
    letter-spacing: 0.07em;
    padding: 0.1rem 0.45rem;
    border-radius: 2px;
    text-transform: uppercase;
    white-space: nowrap;
    flex: 0 0 auto;
  }
  .status-loaded   { background: rgba(74,222,128,0.16); color: var(--c-long); border: 1px solid rgba(74,222,128,0.45); }
  .status-pending  { background: rgba(251,191,36,0.16); color: var(--c-action); border: 1px solid rgba(251,191,36,0.45); }
  .status-disabled { background: rgba(148,163,184,0.12); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }

  /* Cache keys */
  .cache-keys {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
    margin-top: 0.35rem;
  }
  .cache-key-chip {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
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
    font-size: 0.72rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(126,151,184,0.10);
  }
  .ip-row:last-child { border-bottom: none; }
  .ip-addr {
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    color: #7dd3fc;
  }

  /* Ticker card — staleness list + warn-tint on the stale_count cell */
  .stale-warn {
    color: var(--c-action);
    font-weight: 700;
  }
  .ticker-stale-list {
    margin-top: 0.55rem;
    padding-top: 0.45rem;
    border-top: 1px solid rgba(126,151,184,0.10);
    max-height: 12rem;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(126, 151, 184, 0.4) transparent;
  }
  .ticker-stale-label {
    font-size: var(--fs-xs);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #94a3b8;
    margin-bottom: 0.3rem;
  }
  .ticker-stale-row {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: #c8d8f0;
    padding: 0.1rem 0;
  }

  /* Persistence refresh-cycle card */
  .persistence-modes {
    display: flex;
    gap: 0.3rem;
    padding: 0.4rem 0 0.2rem;
    flex-wrap: wrap;
  }
  .pm-btn {
    flex: 1 1 0;
    min-width: 4rem;
    padding: 0.32rem 0.55rem;
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    background: rgba(148, 163, 184, 0.08);
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 3px;
    color: #94a3b8;
    cursor: pointer;
    transition: background 120ms, color 120ms, border-color 120ms;
  }
  .pm-btn:hover:not(:disabled) {
    background: rgba(148, 163, 184, 0.14);
    color: #c8d8f0;
  }
  .pm-btn:disabled { cursor: not-allowed; opacity: 0.55; }
  /* Selected state — each mode adopts its own palette so the operator
     can scan "what mode am I in" at a glance even without reading the
     pill above. */
  .pm-btn.pm-on.pm-off   { background: rgba(74, 222, 128, 0.18); color: var(--c-long); border-color: rgba(74, 222, 128, 0.55); }
  .pm-btn.pm-on.pm-soft  { background: rgba(251, 191, 36, 0.18); color: var(--c-action); border-color: rgba(251, 191, 36, 0.55); }
  .pm-btn.pm-on.pm-hard  { background: rgba(248, 113, 113, 0.20); color: var(--c-short); border-color: rgba(248, 113, 113, 0.55); }
  .pm-err {
    color: var(--c-short);
    font-size: var(--fs-sm);
    padding-top: 0.25rem;
  }
  .kv-section {
    margin-top: 0.4rem;
    padding-top: 0.35rem;
    border-top: 1px solid rgba(126,151,184,0.10);
  }
  .kv-indent {
    padding-left: 0.6rem;
  }
  .kv-muted {
    color: #6e8198;
    font-size: var(--fs-sm);
    font-style: italic;
  }
  .pm-inval-btn {
    margin-left: auto;
    padding: 0.18rem 0.45rem;
    font-size: var(--fs-xs);
    font-weight: 600;
    background: rgba(56, 189, 248, 0.10);
    border: 1px solid rgba(56, 189, 248, 0.35);
    border-radius: 2px;
    color: #7dd3fc;
    cursor: pointer;
  }
  .pm-inval-btn:hover:not(:disabled) {
    background: rgba(56, 189, 248, 0.20);
  }
  .pm-inval-btn:disabled { cursor: not-allowed; opacity: 0.5; }
  .pm-inval-all {
    background: var(--c-short-10);
    border-color: rgba(248, 113, 113, 0.4);
    color: var(--c-short);
  }
  /* Tier-hit metric cells (slice AJ) */
  .kv-tier-pct {
    min-width: 3.2rem;
    text-align: right;
    font-weight: 700;
    color: #94a3b8;
  }
  .kv-tier-pct.cell-pos   { color: var(--c-long); }     /* ≥ 80% hit rate */
  .kv-tier-pct.cell-amber { color: var(--c-action); }     /* 50-79% */
  .kv-tier-pct.cell-neg   { color: var(--c-short); }     /* < 50% — broker-heavy */
  .kv-tier-counts {
    margin-left: 0.4rem;
    color: #94a3b8;
    font-size: var(--fs-sm);
  }
  .pm-inval-all:hover:not(:disabled) {
    background: rgba(248, 113, 113, 0.20);
  }

  /* .empty-state CSS removed — slice AS audit fix migrated the
     no-health-data branch to the canonical EmptyState component. */
</style>
