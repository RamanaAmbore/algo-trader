<script>
  // Agent-fire history view (/admin/alerts).
  //
  // Shows every agent event row from agent_events: triggered fires,
  // action outcomes, cooldown suppressions. Filterable by agent,
  // event type, time window, and sim-mode. Polls every 10 s while
  // the page is visible.

  import { onDestroy } from 'svelte';
  import { nowStamp, lastRefreshAt, formatDualTz, logTime, logTimeIst, logTimeEdt, visibleInterval } from '$lib/stores';
  import { userRole, userCaps, userCapsReady, hasCap } from '$lib/rbac';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import AutomationTabs from '$lib/AutomationTabs.svelte';
  import { fetchAgents, fetchAlertsHistory } from '$lib/api';
  import StaleBanner from '$lib/StaleBanner.svelte';
  import Select   from '$lib/Select.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';

  // ── State ──────────────────────────────────────────────────────────
  /** @type {any[]} */
  let rows       = $state([]);
  /** @type {any[]} */
  let agents     = $state([]);
  let _showLiveTs = $state(false);
  let loading    = $state(true);
  let error      = $state('');

  // Filter state
  let filterAgent   = $state('');
  let filterType    = $state('all');
  let filterPeriod  = $state('1440');   // default: last 24h (minutes)
  let filterSim     = $state(false);

  let teardown;

  // Period options — value is minutes
  const PERIODS = [
    { label: 'Last hour',  value: '60'  },
    { label: 'Last 24h',   value: '1440' },
    { label: 'Last 7d',    value: '10080' },
    { label: 'All',        value: ''    },
  ];

  const EVENT_TYPES = [
    { id: 'all',            label: 'All',           cls: '' },
    { id: 'triggered',      label: 'Triggered',     cls: 'chip-amber'  },
    { id: 'action_success', label: 'Action OK',     cls: 'chip-green'  },
    { id: 'action_failed',  label: 'Action failed', cls: 'chip-red'    },
    { id: 'cooldown',       label: 'Cooldown',      cls: 'chip-grey'   },
  ];

  // ── Data loading ───────────────────────────────────────────────────
  async function loadAgents() {
    try { agents = await fetchAgents(); }
    catch (_) { /* non-fatal */ }
  }

  async function load() {
    try {
      /** @type {Record<string, any>} */
      const params = { limit: 200 };
      if (filterAgent)  params.agent_slug = filterAgent;
      // Empty filterPeriod = "All" → send 0 explicitly so the backend
      // skips its 1440-minute default and returns the full history.
      params.since_minutes = filterPeriod ? Number(filterPeriod) : 0;
      if (filterType !== 'all') params.event_type = filterType;
      params.sim_mode = filterSim;

      const data = await fetchAlertsHistory(params);
      rows = data?.events ?? data ?? [];
      error = '';
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  // Gate by capability — friendly access-denied panel rather than
  // hard /signin redirect (matches /admin/audit + /admin/history).
  // view_audit covers admin / risk / ops; alerts are essentially the
  // agent-action audit trail so the same cap fits.
  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values — without
  // this the access-denied EmptyState rendered on first paint for
  // legitimately-authorised operators (designated, admin, etc).
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const _canView = $derived(hasCap('view_audit', _caps, _role));

  // $effect-gated load — fires when _canView flips true on first
  // /whoami resolution. Pre-fix the page used onMount which ran once
  // at false (whoami in-flight) and never re-checked, so the operator
  // saw a hard goto('/signin') redirect before auth hydrated.
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      loadAgents();
      load();
      teardown = visibleInterval(load, 10000);
    }
  });
  onDestroy(() => teardown?.());

  // ── Derived — filtered rows ────────────────────────────────────────
  // All server-side filtering is done via the API params; this is
  // just a safety net in case the server returns extra rows.
  const filtered = $derived(rows);

  // ── Helpers ────────────────────────────────────────────────────────
  /** Dual-TZ event timestamp (IST + EST/EDT, with seconds). Routed
   *  through the canonical `logTime` helper so alert events read in
   *  the same form as every other trading-critical log row. */
  function _ts(/** @type {string|null|undefined} */ iso) {
    if (!iso) return '—';
    return logTime(iso) || '—';
  }

  /** CSS class for the event-type chip. */
  function _eventCls(/** @type {string} */ evt) {
    if (evt === 'triggered')      return 'chip-amber';
    if (evt === 'action_success') return 'chip-green';
    if (evt === 'action_failed')  return 'chip-red';
    if (evt === 'cooldown')       return 'chip-grey';
    return 'chip-grey';
  }

  /** Human label for an event type string. */
  function _eventLabel(/** @type {string} */ evt) {
    if (evt === 'triggered')      return 'Triggered';
    if (evt === 'action_success') return 'Action OK';
    if (evt === 'action_failed')  return 'Action failed';
    if (evt === 'cooldown')       return 'Cooldown';
    return evt ?? '—';
  }

  /** Comma-join an array or return a plain string. */
  function _strList(v) {
    if (!v) return '—';
    if (Array.isArray(v)) return v.join(', ') || '—';
    return String(v);
  }

  /** Truncate a long string for the Detail cell. */
  function _trunc(/** @type {string|null|undefined} */ s, max = 80) {
    if (!s) return '—';
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  }
</script>

<svelte:head><title>Alerts | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Alerts</h1>
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
    <RefreshButton onClick={load} loading={loading} label="alerts" />
    <PageHeaderActions />
  </span>
</div>

<AutomationTabs />

{#if !$userCapsReady}
  <!-- /whoami still in flight — show skeleton, NOT access-denied.
       The bootstrap window is ~50-300ms in practice; without this
       guard a slow whoami flashes the EmptyState lock panel before
       caps land, terrifying legitimately-authorised operators. -->
  <LoadingSkeleton variant="card" rows={3} />
{:else if !_canView}
  <EmptyState title="Access denied" icon="lock">
    {#snippet hintBody()}
      Alert history requires the <code>view_audit</code> capability
      (designated, admin, or risk role). Your current role is
      <strong>{$userRole}</strong> — contact an admin to request access.
    {/snippet}
  </EmptyState>
{:else}

<StaleBanner {error} hasData={rows.length > 0} label="Alert history" />

<!-- ── Filter bar ──────────────────────────────────────────────────── -->
<div class="filter-bar">
  <!-- Agent dropdown -->
  <div class="filter-item">
    <label class="filter-label" for="filter-agent">Agent</label>
    <div class="algo-select-wrap">
      <Select id="filter-agent" ariaLabel="Agent"
        bind:value={filterAgent}
        onValueChange={() => load()}
        options={[
          { value: '', label: 'All agents' },
          ...agents.map(a => ({ value: a.slug, label: a.name ?? a.slug })),
        ]} />
    </div>
  </div>

  <!-- Period dropdown -->
  <div class="filter-item">
    <label class="filter-label" for="filter-period">Period</label>
    <div class="algo-select-wrap">
      <Select id="filter-period" ariaLabel="Period"
        bind:value={filterPeriod}
        onValueChange={() => load()}
        options={PERIODS} />
    </div>
  </div>

  <!-- Event-type pills -->
  <div class="filter-item filter-pills">
    <span class="filter-label">Event</span>
    <div class="pill-group">
      {#each EVENT_TYPES as et}
        <button
          type="button"
          class="ev-pill {et.cls}"
          class:ev-pill-active={filterType === et.id}
          onclick={() => { filterType = et.id; load(); }}
        >{et.label}</button>
      {/each}
    </div>
  </div>

  <!-- Sim toggle -->
  <div class="filter-item filter-toggle">
    <label class="filter-label" for="filter-sim">Sim only</label>
    <button id="filter-sim"
            type="button"
            class="toggle-btn"
            class:toggle-on={filterSim}
            onclick={() => { filterSim = !filterSim; load(); }}
            title="Show only simulator events when on">
      {filterSim ? 'SIM' : 'ALL'}
    </button>
  </div>
</div>

<!-- ── Table ───────────────────────────────────────────────────────── -->
{#if loading && !rows.length}
  <LoadingSkeleton variant="grid-row" rows={6} height="1.4rem" />
{:else if !filtered.length}
  <EmptyState
    title="No alert events"
    hint="No agent fires in this time window. Try widening the period or clearing filters."
    icon="inbox"
  />
{:else}
  <div class="alerts-table-wrap content-fade-in">
    <table class="alerts-table">
      <thead>
        <tr>
          <th>Time (IST)</th>
          <th>Agent</th>
          <th>Event</th>
          <th>Conditions</th>
          <th>Channels</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {#each filtered as r (r.id ?? r.triggered_at)}
          <tr class:row-sim={r.sim_mode}>
            <td class="td-time" title={logTime(r.triggered_at)}>
              {#if r.triggered_at}<span class="log-ts"><span class="log-ts-ist">{logTimeIst(r.triggered_at)}</span><span class="log-ts-edt">{logTimeEdt(r.triggered_at)}</span></span>{:else}—{/if}
            </td>
            <td class="td-agent">
              {#if r.sim_mode}
                <span class="sim-tag">SIM</span>
              {/if}
              <span class="agent-slug">{r.agent_slug ?? r.agent?.slug ?? '—'}</span>
              {#if r.agent_name ?? r.agent?.name}
                <span class="agent-name">{r.agent_name ?? r.agent?.name}</span>
              {/if}
            </td>
            <td>
              <span class="ev-chip {_eventCls(r.event_type)}">{_eventLabel(r.event_type)}</span>
            </td>
            <td class="td-cond" title={_strList(r.conditions_matched)}>
              {_strList(r.conditions_matched)}
            </td>
            <td class="td-channels">
              {#if Array.isArray(r.channels_notified) && r.channels_notified.length}
                {#each r.channels_notified as ch}
                  <span class="channel-chip">{ch}</span>
                {/each}
              {:else}
                <span class="text-muted">—</span>
              {/if}
            </td>
            <td class="td-detail" title={r.detail ?? ''}>
              {_trunc(r.detail)}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

{/if}

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
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
    color: var(--c-short);
    font-size: var(--fs-md);
  }

  /* ── Filter bar ─────────────────────────────────────────────────── */
  .filter-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    align-items: flex-end;
    margin-bottom: 0.75rem;
    padding: 0.5rem 0.65rem;
    background: var(--card-bg-gradient);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 4px;
  }
  .filter-item {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .filter-pills {
    flex-direction: row;
    align-items: center;
    gap: 0.45rem;
  }
  .filter-toggle {
    flex-direction: row;
    align-items: center;
    gap: 0.45rem;
  }
  .filter-label {
    font-size: var(--fs-xs);
    font-weight: 700;
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }
  /* Layout-only wrapper for the custom <Select>. Min-width matches
     the old native .algo-select so the filter bar lays out the same. */
  .algo-select-wrap { min-width: 9rem; }

  /* Event-type pill buttons */
  .pill-group {
    display: flex;
    gap: 0.25rem;
    flex-wrap: wrap;
  }
  .ev-pill {
    font-size: var(--fs-sm);
    font-weight: 600;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.15);
    background: transparent;
    color: var(--algo-muted);
    cursor: pointer;
    letter-spacing: 0.03em;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .ev-pill:hover { background: rgba(255,255,255,0.06); }
  .ev-pill-active.chip-amber  { background: rgba(251,191,36,0.18); border-color: rgba(251,191,36,0.6); color: var(--c-action); }
  .ev-pill-active.chip-green  { background: rgba(74,222,128,0.14); border-color: rgba(74,222,128,0.5); color: var(--c-long); }
  .ev-pill-active.chip-red    { background: rgba(248,113,113,0.14); border-color: rgba(248,113,113,0.5); color: var(--c-short); }
  .ev-pill-active.chip-grey   { background: rgba(148,163,184,0.14); border-color: rgba(148,163,184,0.4); color: #94a3b8; }
  .ev-pill-active:not([class*="chip-"]) { background: rgba(200,216,240,0.12); border-color: rgba(200,216,240,0.35); color: var(--algo-slate); }

  /* Sim toggle */
  .toggle-btn {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.15);
    background: transparent;
    color: var(--algo-muted);
    cursor: pointer;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .toggle-btn.toggle-on {
    background: rgba(248,113,113,0.15);
    border-color: rgba(248,113,113,0.55);
    color: var(--c-short);
  }

  /* ── Table ──────────────────────────────────────────────────────── */
  .alerts-table-wrap {
    overflow-x: auto;
    border-radius: 4px;
    border: 1.5px solid rgba(255,255,255,0.10);
    box-shadow: 0 2px 8px rgba(0,0,0,0.45);
  }
  .alerts-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--fs-md);
    font-family: var(--font-numeric);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
  }
  .alerts-table thead tr {
    background: #0a1020;
  }
  .alerts-table th {
    padding: 0.3rem 0.5rem;
    font-size: var(--fs-xs);
    font-weight: 700;
    color: var(--c-action);
    text-transform: uppercase;
    letter-spacing: 0.07em;
    border-bottom: 1px solid rgba(251,191,36,0.35);
    border-right: 1px solid rgba(251,191,36,0.18);
    text-align: left;
    white-space: nowrap;
  }
  .alerts-table th:last-child { border-right: none; }
  .alerts-table td {
    padding: 0.28rem 0.5rem;
    color: var(--algo-slate);
    border-bottom: 1px solid rgba(255,255,255,0.06);
    border-right: 1px solid rgba(255,255,255,0.06);
    vertical-align: top;
  }
  .alerts-table td:last-child { border-right: none; }
  .alerts-table tbody tr:nth-child(odd) { background: rgba(13,22,42,0.45); }
  .alerts-table tbody tr:hover { background: rgba(251,191,36,0.07); }
  .alerts-table tbody tr.row-sim { background: rgba(248,113,113,0.04); }
  .alerts-table tbody tr.row-sim:hover { background: rgba(248,113,113,0.09); }

  .td-time {
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    color: #fde047;
    font-size: var(--fs-sm);
  }
  .td-agent {
    white-space: nowrap;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }
  .sim-tag {
    display: inline-block;
    font-size: var(--fs-2xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0 0.3rem;
    border-radius: 2px;
    background: rgba(248,113,113,0.18);
    color: var(--c-short);
    border: 1px solid rgba(248,113,113,0.4);
    margin-bottom: 0.1rem;
    align-self: flex-start;
  }
  .agent-slug {
    font-weight: 600;
    color: var(--c-action);
    font-size: var(--fs-md);
  }
  .agent-name {
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    font-weight: 400;
  }

  /* Event-type chips */
  .ev-chip {
    display: inline-block;
    font-size: var(--fs-xs);
    font-weight: 700;
    padding: 0.1rem 0.4rem;
    border-radius: 2px;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .chip-amber { background: rgba(251,191,36,0.18); color: var(--c-action); border: 1px solid rgba(251,191,36,0.4); }
  .chip-green { background: rgba(74,222,128,0.14); color: var(--c-long); border: 1px solid rgba(74,222,128,0.35); }
  .chip-red   { background: rgba(248,113,113,0.14); color: var(--c-short); border: 1px solid rgba(248,113,113,0.35); }
  .chip-grey  { background: rgba(148,163,184,0.10); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }

  /* Conditions + detail */
  .td-cond {
    max-width: 14rem;
    font-size: var(--fs-sm);
    color: #7dd3fc;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .td-detail {
    max-width: 22rem;
    font-size: var(--fs-sm);
    color: #94a3b8;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .td-channels {
    white-space: nowrap;
  }
  .channel-chip {
    display: inline-block;
    font-size: var(--fs-2xs);
    font-weight: 600;
    padding: 0.08rem 0.3rem;
    border-radius: 2px;
    background: rgba(125,211,252,0.10);
    color: #7dd3fc;
    border: 1px solid rgba(125,211,252,0.3);
    margin-right: 0.2rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .text-muted { color: #94a3b8; }

  /* Empty state */
  .empty-state {
    padding: 2rem;
    text-align: center;
    color: #94a3b8;
    font-size: var(--fs-lg);
    font-family: var(--font-numeric);
    background: var(--card-bg-gradient);
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 4px;
  }

  /* ── Mobile: horizontal scroll on the table; filter bar wraps ──── */
  @media (max-width: 768px) {
    .filter-bar { gap: 0.4rem; }
    .algo-select-wrap { min-width: 7rem; }
    .td-cond  { max-width: 8rem; }
    .td-detail { max-width: 10rem; }
  }
</style>
