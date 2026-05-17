<script>
  // Iteration detail — summary card + params JSON + Replay button.
  //
  // Replay POSTs /iterations/{slug}/replay which kicks off a NEW
  // single-iteration run using the original iteration's regime +
  // seed + agent_ids. Navigates to /admin/simulator to watch it run.

  import { onMount } from 'svelte';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { authStore, logTimeIst, logTimeEdt, logTime } from '$lib/stores';
  import { fetchSimIteration, replaySimIteration } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';
  import { aggCompact } from '$lib/format';

  /** @type {any} */
  let iteration = $state(null);
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
    if (!confirm(`Replay ${slug} with seed ${iteration?.seed ?? '(random)'}?`)) return;
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
  <title>{slug || 'Iteration'} | RamboQuant Algo</title>
</svelte:head>

<div class="page-header">
  <h1 class="algo-page-title">Iteration</h1>
  <span class="slug-chip">{slug || '—'}</span>
  <InfoHint popup text="Snapshot of one simulator iteration. The Replay button re-runs this regime with the same seed (deterministic — same fills) and the same agent_ids, as a new single-iteration run." />
  <a href="/admin/simulator/iterations" class="back-link">← Iterations</a>
  <a href="/admin/simulator" class="back-link">Simulator</a>
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
          <tr><th>Started</th><td title={logTime(iteration.started_at)}>{#if iteration.started_at}<span class="log-ts log-ts-inline"><span class="log-ts-ist">{logTimeIst(iteration.started_at)}</span><span class="log-ts-sep">|</span><span class="log-ts-edt">{logTimeEdt(iteration.started_at)}</span></span>{:else}—{/if}</td></tr>
          <tr><th>Ended</th><td title={logTime(iteration.ended_at)}>{#if iteration.ended_at}<span class="log-ts log-ts-inline"><span class="log-ts-ist">{logTimeIst(iteration.ended_at)}</span><span class="log-ts-sep">|</span><span class="log-ts-edt">{logTimeEdt(iteration.ended_at)}</span></span>{:else}—{/if}</td></tr>
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
          </tbody>
        </table>
      {:else}
        <div class="empty-inline">No summary yet — iteration may still be running.</div>
      {/if}

      <button class="replay-btn" onclick={onReplay} disabled={replaying || !iteration.ended_at}>
        {replaying ? 'Starting replay…' : 'Replay this iteration'}
      </button>
      {#if !iteration.ended_at}
        <p class="hint">Replay enabled once the iteration ends.</p>
      {/if}
    </div>

  </div>
{/if}

<style>
  .algo-page-title {
    font-size: 0.75rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-family: ui-monospace, monospace;
  }
  :global(.page-header:has(.algo-page-title)) {
    border-bottom: none;
    padding-bottom: 0;
    margin-bottom: 0.5rem;
  }
  .slug-chip {
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
    border: 1px solid rgba(251,191,36,0.35);
    padding: 0.15rem 0.45rem;
    border-radius: 3px;
    letter-spacing: 0.04em;
  }
  .back-link {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    color: #7dd3fc;
    text-decoration: none;
    margin-left: 0.75rem;
  }
  .back-link:hover { color: #fbbf24; }
  .err-banner {
    background: rgba(248,113,113,0.10);
    border: 1px solid rgba(248,113,113,0.35);
    color: #f87171;
    padding: 0.4rem 0.65rem;
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    margin-bottom: 0.5rem;
  }
  .loading, .empty {
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    color: #7e97b8;
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
    border: 1px solid rgba(251,191,36,0.18);
    border-left: 3px solid #fbbf24;
    border-radius: 4px;
    padding: 0.65rem 0.75rem;
  }
  .card-title {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 0.45rem;
  }
  .kv {
    width: 100%;
    border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
  }
  .kv th {
    text-align: left;
    color: #7e97b8;
    font-weight: 500;
    padding: 0.18rem 0.5rem 0.18rem 0;
    width: 9rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    font-size: 0.55rem;
  }
  .kv td {
    color: #c8d8f0;
    padding: 0.18rem 0;
    font-variant-numeric: tabular-nums;
  }
  .slug    { color: #fbbf24; font-weight: 700; }
  .regime  { color: #c084fc; font-weight: 700; }
  .er {
    text-transform: uppercase;
    font-size: 0.55rem;
    letter-spacing: 0.04em;
    font-weight: 700;
  }
  .er-ok      { color: #4ade80; }
  .er-warn    { color: #fbbf24; }
  .er-err     { color: #f87171; }
  .er-pending { color: #7dd3fc; }
  .er-other   { color: #a3b9d0; }

  .empty-inline {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    font-style: italic;
    padding: 0.45rem 0;
  }
  .replay-btn {
    margin-top: 0.75rem;
    width: 100%;
    padding: 0.45rem 0.65rem;
    background: rgba(251,191,36,0.18);
    border: 1px solid rgba(251,191,36,0.55);
    color: #fbbf24;
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
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
    font-size: 0.55rem;
    color: #7e97b8;
    font-style: italic;
    margin-top: 0.35rem;
    text-align: center;
  }
</style>
