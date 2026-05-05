<script>
  // Agent-fire history view (/admin/alerts).
  //
  // Shows every agent event row from agent_events: triggered fires,
  // action outcomes, cooldown suppressions. Filterable by agent,
  // event type, time window, and sim-mode. Polls every 10 s while
  // the page is visible.

  import { onMount, onDestroy } from 'svelte';
  import { authStore, clientTimestamp, visibleInterval } from '$lib/stores';
  import { fetchAgents, fetchAlertsHistory } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';

  // ── State ──────────────────────────────────────────────────────────
  /** @type {any[]} */
  let rows       = $state([]);
  /** @type {any[]} */
  let agents     = $state([]);
  let loading    = $state(true);
  let error      = $state('');
  let refreshedAt = $state('');

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
      refreshedAt = clientTimestamp();
      error = '';
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    loadAgents();
    load();
    teardown = visibleInterval(load, 10000);
  });
  onDestroy(() => teardown?.());

  // ── Derived — filtered rows ────────────────────────────────────────
  // All server-side filtering is done via the API params; this is
  // just a safety net in case the server returns extra rows.
  const filtered = $derived(rows);

  // ── Helpers ────────────────────────────────────────────────────────
  /** Short IST time from ISO string. */
  function _ts(/** @type {string|null|undefined} */ iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleString('en-GB', {
        day: '2-digit', month: 'short',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hour12: false, timeZone: 'Asia/Kolkata',
      }).replace(',', '') + ' IST';
    } catch (_) { return iso; }
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
  <h1 class="page-title-chip">Alerts</h1>
  <InfoHint popup text="History of agent fires (real and simulated). Each row shows when an agent's condition matched, what action ran, and which channels were notified. Use the filters to scope by agent, event type, or time window." />
  {#if refreshedAt}
    <span class="algo-ts">{refreshedAt}</span>
  {/if}
</div>

{#if error}
  <div class="err-banner">{error}</div>
{/if}

<!-- ── Filter bar ──────────────────────────────────────────────────── -->
<div class="filter-bar">
  <!-- Agent dropdown -->
  <div class="filter-item">
    <label class="filter-label" for="filter-agent">Agent</label>
    <select id="filter-agent" class="algo-select" bind:value={filterAgent}
            onchange={() => load()}>
      <option value="">All agents</option>
      {#each agents as a}
        <option value={a.slug}>{a.name ?? a.slug}</option>
      {/each}
    </select>
  </div>

  <!-- Period dropdown -->
  <div class="filter-item">
    <label class="filter-label" for="filter-period">Period</label>
    <select id="filter-period" class="algo-select" bind:value={filterPeriod}
            onchange={() => load()}>
      {#each PERIODS as p}
        <option value={p.value}>{p.label}</option>
      {/each}
    </select>
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
  <div class="empty-state">Loading…</div>
{:else if !filtered.length}
  <div class="empty-state">No alert events in this window.</div>
{:else}
  <div class="alerts-table-wrap">
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
            <td class="td-time">{_ts(r.triggered_at)}</td>
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

<style>
  /* ── Page header ────────────────────────────────────────────────── */
  .page-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.65rem;
  }
  .algo-ts {
    margin-left: auto;
    font-size: 0.6rem;
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .err-banner {
    margin-bottom: 0.5rem;
    padding: 0.25rem 0.65rem;
    border-radius: 3px;
    background: rgba(248,113,113,0.12);
    border: 1px solid rgba(248,113,113,0.35);
    color: #f87171;
    font-size: 0.65rem;
  }

  /* ── Filter bar ─────────────────────────────────────────────────── */
  .filter-bar {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6rem;
    align-items: flex-end;
    margin-bottom: 0.75rem;
    padding: 0.5rem 0.65rem;
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
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
    font-size: 0.55rem;
    font-weight: 700;
    color: #7e97b8;
    text-transform: uppercase;
    letter-spacing: 0.07em;
  }
  .algo-select {
    appearance: none;
    background: #0f172a;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 3px;
    color: #c8d8f0;
    font-size: 0.65rem;
    padding: 0.2rem 0.5rem;
    cursor: pointer;
    outline: none;
    min-width: 9rem;
  }
  .algo-select:focus { border-color: rgba(251,191,36,0.5); }

  /* Event-type pill buttons */
  .pill-group {
    display: flex;
    gap: 0.25rem;
    flex-wrap: wrap;
  }
  .ev-pill {
    font-size: 0.6rem;
    font-weight: 600;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.15);
    background: transparent;
    color: #7e97b8;
    cursor: pointer;
    letter-spacing: 0.03em;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .ev-pill:hover { background: rgba(255,255,255,0.06); }
  .ev-pill-active.chip-amber  { background: rgba(251,191,36,0.18); border-color: rgba(251,191,36,0.6); color: #fbbf24; }
  .ev-pill-active.chip-green  { background: rgba(74,222,128,0.14); border-color: rgba(74,222,128,0.5); color: #4ade80; }
  .ev-pill-active.chip-red    { background: rgba(248,113,113,0.14); border-color: rgba(248,113,113,0.5); color: #f87171; }
  .ev-pill-active.chip-grey   { background: rgba(148,163,184,0.14); border-color: rgba(148,163,184,0.4); color: #94a3b8; }
  .ev-pill-active:not([class*="chip-"]) { background: rgba(200,216,240,0.12); border-color: rgba(200,216,240,0.35); color: #c8d8f0; }

  /* Sim toggle */
  .toggle-btn {
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 0.15rem 0.5rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.15);
    background: transparent;
    color: #7e97b8;
    cursor: pointer;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .toggle-btn.toggle-on {
    background: rgba(248,113,113,0.15);
    border-color: rgba(248,113,113,0.55);
    color: #f87171;
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
    font-size: 0.68rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
  }
  .alerts-table thead tr {
    background: #0a1020;
  }
  .alerts-table th {
    padding: 0.3rem 0.5rem;
    font-size: 0.55rem;
    font-weight: 700;
    color: #fbbf24;
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
    color: #c8d8f0;
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
    font-size: 0.62rem;
  }
  .td-agent {
    white-space: nowrap;
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }
  .sim-tag {
    display: inline-block;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0 0.3rem;
    border-radius: 2px;
    background: rgba(248,113,113,0.18);
    color: #f87171;
    border: 1px solid rgba(248,113,113,0.4);
    margin-bottom: 0.1rem;
    align-self: flex-start;
  }
  .agent-slug {
    font-weight: 600;
    color: #fbbf24;
    font-size: 0.66rem;
  }
  .agent-name {
    font-size: 0.6rem;
    color: #7e97b8;
    font-weight: 400;
  }

  /* Event-type chips */
  .ev-chip {
    display: inline-block;
    font-size: 0.55rem;
    font-weight: 700;
    padding: 0.1rem 0.4rem;
    border-radius: 2px;
    letter-spacing: 0.04em;
    white-space: nowrap;
  }
  .chip-amber { background: rgba(251,191,36,0.18); color: #fbbf24; border: 1px solid rgba(251,191,36,0.4); }
  .chip-green { background: rgba(74,222,128,0.14); color: #4ade80; border: 1px solid rgba(74,222,128,0.35); }
  .chip-red   { background: rgba(248,113,113,0.14); color: #f87171; border: 1px solid rgba(248,113,113,0.35); }
  .chip-grey  { background: rgba(148,163,184,0.10); color: #94a3b8; border: 1px solid rgba(148,163,184,0.3); }

  /* Conditions + detail */
  .td-cond {
    max-width: 14rem;
    font-size: 0.62rem;
    color: #7dd3fc;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .td-detail {
    max-width: 22rem;
    font-size: 0.62rem;
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
    font-size: 0.5rem;
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
  .text-muted { color: #4a5a70; }

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

  /* ── Mobile: horizontal scroll on the table; filter bar wraps ──── */
  @media (max-width: 768px) {
    .filter-bar { gap: 0.4rem; }
    .algo-select { min-width: 7rem; font-size: 0.6rem; }
    .td-cond  { max-width: 8rem; }
    .td-detail { max-width: 10rem; }
  }
</style>
