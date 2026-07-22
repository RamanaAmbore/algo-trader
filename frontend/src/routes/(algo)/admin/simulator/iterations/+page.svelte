<script>
  // Past simulator iterations — table view.
  //
  // Lists every SimIteration row (most recent first), grouped visually
  // by parent_run_id so multi-iteration runs read as one bundle. Click
  // a row to open the detail page.

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, logTimeIst, logTimeEdt, logTime, dualTsHtml, visibleInterval } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import { fetchSimIterations } from '$lib/api';
  import { aggCompact } from '$lib/format';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';

  /** @type {any[]} */
  let rows        = $state([]);
  let loading     = $state(true);
  let error       = $state('');
  let teardown;

  onMount(async () => {
    if (!$authStore.user) { goto('/signin'); return; }
    await load();
    // Minimum iteration is 30 s; 15 s keeps the table half-a-step
    // ahead of the run without hammering the endpoint.
    teardown = visibleInterval(load, 15000);
  });
  onDestroy(() => teardown?.());

  async function load() {
    try {
      rows = await fetchSimIterations(null, 100);
      error = '';
    } catch (e) {
      error = e.message || 'Failed to load iterations';
    } finally {
      loading = false;
    }
  }

  /** Group rows by parent_run_id so multi-iteration runs render together. */
  const grouped = $derived.by(() => {
    /** @type {Map<number|string, any[]>} */
    const groups = new Map();
    for (const r of rows) {
      // First-iteration rows have parent_run_id == null; they ARE the
      // root of their run, key by their own id.
      const key = r.parent_run_id ?? r.id;
      const arr = groups.get(key) ?? [];
      arr.push(r);
      groups.set(key, arr);
    }
    // Sort each group by iteration_index ASC, runs by most-recent-first.
    /** @type {{ run_id: any, rows: any[] }[]} */
    const out = [];
    for (const [run_id, arr] of groups) {
      arr.sort((a, b) => (a.iteration_index ?? 0) - (b.iteration_index ?? 0));
      out.push({ run_id, rows: arr });
    }
    out.sort((a, b) => {
      const bt = b.rows[0]?.started_at ?? '';
      const at = a.rows[0]?.started_at ?? '';
      return bt.localeCompare(at);
    });
    return out;
  });

  function _duration(start, end) {
    if (!start || !end) return '—';
    const ms = new Date(end).getTime() - new Date(start).getTime();
    if (!isFinite(ms) || ms <= 0) return '—';
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${s % 60}s`;
  }
  function _summaryPnl(s) {
    if (!s) return '—';
    const v = Number(s.total_pnl_remaining ?? 0);
    if (!isFinite(v)) return '—';
    return aggCompact(v);
  }
  function _endReasonClass(reason) {
    if (!reason) return 'er-pending';
    if (reason === 'book_empty')        return 'er-ok';
    if (reason === 'scenario_complete') return 'er-ok';
    if (reason === 'time_limit')        return 'er-warn';
    if (reason === 'stopped')           return 'er-warn';
    if (reason === 'failed')            return 'er-err';
    return 'er-other';
  }
  function _open(slug) { goto(`/admin/simulator/iterations/${slug}`); }
</script>

<svelte:head>
  <title>Simulator iterations | RamboQuant Analytics</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <a href="/admin/simulator" class="back-link">← Simulator</a>
    <h1 class="page-title-chip">Simulator iterations</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="iterations" />
    <PageHeaderActions />
  </span>
</div>

{#if error}<div class="err-banner">{error}</div>{/if}

{#if loading}
  <LoadingSkeleton variant="block" rows={4} height="1.5rem" />
{:else if grouped.length === 0}
  <EmptyState
    title="No iterations yet"
    hint="Kick off a run from the Simulator page to see results here."
    icon="chart"
    action={{ label: 'Go to Simulator', onClick: () => goto('/admin/simulator') }}
  />
{:else}
  <div class="content-fade-in">
  {#each grouped as group (group.run_id)}
    <div class="run-card">
      <div class="run-header">
        <span class="run-id">run #{group.run_id}</span>
        <span class="run-meta">{group.rows.length} iteration{group.rows.length === 1 ? '' : 's'}</span>
        <span class="run-started" title={logTime(group.rows[0]?.started_at)}>
          {@html dualTsHtml(group.rows[0]?.started_at)}
        </span>
      </div>
      <div class="iter-scroll">
      <table class="algo-table iter-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Slug</th>
            <th>Regime</th>
            <th class="numeric">Seed</th>
            <th>Started</th>
            <th class="numeric">Duration</th>
            <th>End</th>
            <th class="numeric" title="Sum of fees (brokerage + STT + GST) on sim orders this iteration">Fees</th>
            <th class="numeric" title="Gross P&L of positions still open at iteration end (force-closed positions don't count)">P&amp;L (hung)</th>
            <th class="numeric" title="Net P&L after Kite-style fees on every sim order">Net P&amp;L</th>
            <th class="numeric">Hung pos</th>
            <th class="numeric" title="Agents that exhausted their shadow lifespan budget in this iteration">Exhausted</th>
          </tr>
        </thead>
        <tbody>
          {#each group.rows as it (it.id)}
            <tr class="iter-row" onclick={() => _open(it.slug)}>
              <td>{it.iteration_index}/{it.iterations_total}</td>
              <td class="slug">{it.slug}</td>
              <td>{it.regime}</td>
              <td class="numeric">{it.seed ?? '—'}</td>
              <td title={logTime(it.started_at)}>
                {#if it.started_at}<span class="log-ts"><span class="log-ts-ist">{logTimeIst(it.started_at)}</span><span class="log-ts-edt">{logTimeEdt(it.started_at)}</span></span>{:else}—{/if}
              </td>
              <td class="numeric">{_duration(it.started_at, it.ended_at)}</td>
              <td class={'er ' + _endReasonClass(it.end_reason)}>{it.end_reason ?? 'pending'}</td>
              <td class="numeric">{it.summary?.total_fees != null ? aggCompact(it.summary.total_fees) : '—'}</td>
              <td class="numeric">{_summaryPnl(it.summary)}</td>
              <td class="numeric">{it.summary?.net_pnl_remaining != null ? aggCompact(it.summary.net_pnl_remaining) : '—'}</td>
              <td class="numeric">{it.summary?.hung_positions ?? '—'}</td>
              <td class="numeric" title={(it.summary?.lifespan_exhausted_agents || []).length
                ? `Agent ids: ${(it.summary.lifespan_exhausted_agents).join(', ')}`
                : 'No agents exhausted shadow lifespan in this iteration'}>
                {it.summary?.lifespan_exhausted_agents?.length || '—'}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      </div>
    </div>
  {/each}
  </div>
{/if}

<style>
  .back-link {
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    color: #7dd3fc;
    text-decoration: none;
    margin-left: 0.75rem;
  }
  .back-link:hover { color: var(--c-action); }
  .err-banner {
    background: var(--c-short-10);
    border: 1px solid rgba(248,113,113,0.35);
    color: var(--c-short);
    padding: 0.4rem 0.65rem;
    border-radius: 4px;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    margin-bottom: 0.5rem;
  }

  .run-card {
    background: linear-gradient(180deg, rgba(20,30,55,0.65) 0%, rgba(13,21,38,0.65) 100%);
    border: 1px solid rgba(251,191,36,0.18);
    border-radius: 4px;
    margin-bottom: 0.75rem;
    overflow: hidden;
  }
  .run-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    padding: 0.4rem 0.65rem;
    background: rgba(251,191,36,0.05);
    border-bottom: 1px solid rgba(251,191,36,0.12);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }
  .run-id { color: var(--c-action); font-weight: 700; letter-spacing: 0.05em; }
  .run-meta { color: var(--algo-slate); }
  .run-started { color: var(--algo-muted); margin-left: auto; }

  /* Horizontal scroll wrapper — keeps the wide stats table readable
     on narrow viewports without breaking the run-card border. The
     card's overflow:hidden clips the scrollbar to the card's rounded
     corners; the wrapper itself scrolls under it. */
  .iter-scroll {
    overflow-x: auto;
    scrollbar-width: thin;
    scrollbar-color: rgba(126, 151, 184, 0.4) transparent;
  }
  .iter-table {
    /* width: 100% removed — algo-table global provides it. */
    min-width: 38rem;       /* prevents column squeeze on mobile */
  }
  .iter-table th {
    text-align: left;
    color: var(--algo-muted);
    font-weight: 600;
    padding: 0.3rem 0.55rem;
    border-bottom: 1px solid rgba(251,191,36,0.10);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .iter-table th.numeric, .iter-table td.numeric {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .iter-row {
    cursor: pointer;
    transition: background 0.08s;
  }
  .iter-row:hover { background: rgba(251,191,36,0.08); }
  .iter-row td {
    padding: 0.3rem 0.55rem;
  }
  .slug { color: var(--c-action); }
  .er {
    text-transform: uppercase;
    font-size: var(--fs-xs);
    letter-spacing: 0.04em;
    font-weight: 700;
  }
  .er-ok      { color: var(--c-long); }
  .er-warn    { color: var(--c-action); }
  .er-err     { color: var(--c-short); }
  .er-pending { color: #7dd3fc; }
  .er-other   { color: var(--text-muted); }
</style>
