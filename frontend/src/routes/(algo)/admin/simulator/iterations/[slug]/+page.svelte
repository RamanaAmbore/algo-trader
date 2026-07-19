<script>
  // Iteration detail — summary card + params JSON + Replay button.
  //
  // Replay POSTs /iterations/{slug}/replay which kicks off a NEW
  // single-iteration run using the original iteration's regime +
  // seed + agent_ids. Navigates to /admin/simulator to watch it run.

  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { authStore, logTime, dualTsHtml, nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import { fetchSimIteration, replaySimIteration } from '$lib/api';
  import { aggCompact } from '$lib/format';

  /** @type {{ask: (opts:any)=>Promise<boolean>}|null} */
  let confirmRef = $state(null);
  // aggCompact is used in the summary card below.

  /** @type {any} */
  let iteration = $state(null);
  let _showLiveTs = $state(false);
  let loading   = $state(true);
  let error     = $state('');
  let replaying = $state(false);

  const slug = $derived(page.params.slug);

  onMount(async () => {
    if (!$authStore.user) { goto('/signin'); return; }
    await load();
  });

  async function load() {
    if (!slug) return;
    try {
      iteration = await fetchSimIteration(slug);
      error = '';
    } catch (e) {
      error = e.message || 'Failed to load iteration';
    } finally {
      loading = false;
    }
  }

  async function onReplay() {
    if (replaying) return;
    if (!await confirmRef?.ask({
      title: `Re-run ${slug}?`,
      message: `Seed: ${iteration?.seed ?? '(random)'}. A fresh single-iteration run will start in the simulator.`,
      confirmLabel: 'Re-run',
    })) return;
    replaying = true;
    try {
      await replaySimIteration(slug);
      goto('/admin/simulator');
    } catch (e) {
      error = e.message || 'Replay failed';
      replaying = false;
    }
  }

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
    if (reason === 'book_empty' || reason === 'scenario_complete') return 'er-ok';
    if (reason === 'time_limit' || reason === 'stopped')           return 'er-warn';
    if (reason === 'failed')                                        return 'er-err';
    return 'er-other';
  }
</script>

<svelte:head>
  <title>{slug || 'Iteration'} | RamboQuant Analytics</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <a href="/admin/simulator/iterations" class="back-link">← Iterations</a>
    <a href="/admin/simulator" class="back-link">Simulator</a>
    <h1 class="page-title-chip">Iteration</h1>
    <span class="slug-chip">{slug || '—'}</span>
  </span>
  <span class="algo-ts-group">
    <span class="algo-ts" class:algo-ts-hidden={_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Live clock — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {$nowStamp}
    </span>
    <span class="algo-ts-vsep" aria-hidden="true">|</span>
    <span class="algo-ts algo-ts-data" class:algo-ts-hidden={!_showLiveTs}
          onclick={() => _showLiveTs = !_showLiveTs}
          title="Last refresh — tap to switch" role="button" tabindex="0"
          onkeydown={(e) => { if (e.key === 'Enter') _showLiveTs = !_showLiveTs; }}>
      {formatDualTz($lastRefreshAt)}
    </span>
  </span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="iteration" />
    <PageHeaderActions />
  </span>
</div>

{#if error}<div class="err-banner">{error}</div>{/if}

{#if loading}
  <div class="loading">Loading iteration…</div>
{:else if !iteration}
  <div class="empty">Iteration not found.</div>
{:else}
  <div class="detail-grid">

    <!-- Metadata card -->
    <div class="card">
      <div class="card-title">Metadata</div>
      <table class="kv">
        <tbody>
          <tr><th>Slug</th><td class="slug">{iteration.slug}</td></tr>
          <tr><th>Run ID</th><td>{iteration.parent_run_id ?? '(self)'}</td></tr>
          <tr><th>Position</th><td>{iteration.iteration_index} / {iteration.iterations_total}</td></tr>
          <tr><th>Regime</th><td class="regime">{iteration.regime}</td></tr>
          <tr><th>Seed</th><td>{iteration.seed ?? '(random)'}</td></tr>
          <tr><th>Started</th><td title={logTime(iteration.started_at)}>{#if iteration.started_at}{@html dualTsHtml(iteration.started_at)}{:else}—{/if}</td></tr>
          <tr><th>Ended</th><td title={logTime(iteration.ended_at)}>{#if iteration.ended_at}{@html dualTsHtml(iteration.ended_at)}{:else}—{/if}</td></tr>
          <tr><th>Duration</th><td>{_duration(iteration.started_at, iteration.ended_at)}</td></tr>
          <tr><th>End reason</th><td class={'er ' + _endReasonClass(iteration.end_reason)}>{iteration.end_reason ?? 'pending'}</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Summary card -->
    <div class="card">
      <div class="card-title">Summary</div>
      {#if iteration.summary}
        <table class="kv">
          <tbody>
            <tr><th>Ticks</th><td>{iteration.summary.tick_index ?? '—'}</td></tr>
            <tr><th>Hung positions</th><td>{iteration.summary.hung_positions ?? '—'}</td></tr>
            <tr><th>P&amp;L (hung)</th><td>{_summaryPnl(iteration.summary)}</td></tr>
            <tr><th>Regime tag</th><td>{iteration.summary.regime ?? '—'}</td></tr>
            {#if iteration.summary.total_fees != null}
              <tr><th title="Sum of brokerage + STT + GST + ancillary on every sim order in this iteration">Fees (sim)</th><td>₹{aggCompact(iteration.summary.total_fees)}</td></tr>
              <tr><th title="P&L after Kite-style fees on every sim order — what you'd actually keep">Net P&amp;L</th><td>₹{aggCompact(iteration.summary.net_pnl_remaining)}</td></tr>
            {/if}
            {#if iteration.summary.lifespan_exhausted_agents?.length}
              <tr>
                <th title="Agents that exhausted their shadow lifespan budget during this iteration. Real DB state untouched.">Exhausted</th>
                <td title="Shadow lifespan exhausted — under this regime, the agent would have hit its budget. Real agent state is unchanged.">
                  <span class="exhausted-tag">{iteration.summary.lifespan_exhausted_agents.join(', ')}</span>
                </td>
              </tr>
            {/if}
          </tbody>
        </table>
      {:else}
        <div class="empty-inline">No summary yet — iteration may still be running.</div>
      {/if}

      <button class="replay-btn" onclick={onReplay} disabled={replaying || !iteration.ended_at}
              title="Re-run this iteration deterministically: same regime, same seed, same agent set. Different from 'Replay mode' which is a historical-data backtest.">
        {replaying ? 'Starting re-run…' : 'Re-run iteration'}
      </button>
      {#if !iteration.ended_at}
        <p class="hint">Re-run enabled once the iteration ends.</p>
      {/if}
    </div>

  </div>
{/if}

<ConfirmModal bind:this={confirmRef} />

<style>
  .algo-ts-group { display: inline-flex; align-items: center; gap: 0.3rem; }
  .algo-ts-vsep  { color: rgba(255,255,255,0.25); font-size: var(--fs-md); }
  .algo-ts-data  { cursor: pointer; }
  @media (max-width: 480px) { .algo-ts-hidden { display: none !important; } }
  .slug-chip {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--c-action);
    background: rgba(251,191,36,0.10);
    border: 1px solid rgba(251,191,36,0.35);
    padding: 0.15rem 0.45rem;
    border-radius: 3px;
    letter-spacing: 0.04em;
  }
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
  .loading, .empty {
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    color: var(--algo-muted);
    padding: 1rem;
    text-align: center;
  }

  .detail-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
    gap: 0.75rem;
  }
  .card {
    background: linear-gradient(180deg, rgba(20,30,55,0.65) 0%, rgba(13,21,38,0.65) 100%);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    padding: 0.65rem 0.75rem;
  }
  .card-title {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    color: var(--c-action);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 0.45rem;
  }
  .kv {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
  }
  .kv th {
    text-align: left;
    color: var(--algo-muted);
    font-weight: 500;
    padding: 0.18rem 0.5rem 0.18rem 0;
    width: 9rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-size: var(--fs-xs);
  }
  .kv td {
    color: var(--algo-slate);
    padding: 0.18rem 0;
    font-variant-numeric: tabular-nums;
  }
  .slug    { color: var(--c-action); font-weight: 700; }
  .regime  { color: #c084fc; font-weight: 700; }
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

  .empty-inline {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--algo-muted);
    font-style: italic;
    padding: 0.45rem 0;
  }
  .replay-btn {
    margin-top: 0.75rem;
    width: 100%;
    padding: 0.45rem 0.65rem;
    background: rgba(251,191,36,0.18);
    border: 1px solid rgba(251,191,36,0.55);
    color: var(--c-action);
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    border-radius: 3px;
    text-transform: uppercase;
  }
  .replay-btn:hover:not(:disabled) {
    background: rgba(251,191,36,0.28);
    border-color: rgba(251,191,36,0.85);
  }
  .replay-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  .hint {
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    font-style: italic;
    margin-top: 0.35rem;
    text-align: center;
  }
  .exhausted-tag {
    color: var(--c-short);
    font-weight: 700;
    background: var(--c-short-10);
    border: 1px solid rgba(248,113,113,0.35);
    padding: 0 0.3rem;
    border-radius: 2px;
    font-size: var(--fs-xs);
  }
</style>
