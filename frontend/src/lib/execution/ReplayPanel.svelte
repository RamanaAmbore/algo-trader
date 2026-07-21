<script>
  // Replay panel — extracted from /admin/replay/+page.svelte.
  // Self-contained: polls /api/replay/*, renders controls, results,
  // chart grid, and an embedded LogPanel. Accepts no required props.

  import { onMount, onDestroy } from 'svelte';
  import { branchLabel, visibleInterval } from '$lib/stores';
  import {
    fetchReplayStatus, startReplay, stopReplay,
    fetchReplayResults, fetchReplayOrders, clearReplayData,
    fetchChartSymbols, fetchChartBatch,
    fetchAgents,
  } from '$lib/api';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import CardHeader   from '$lib/CardHeader.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import ConfirmModal   from '$lib/ConfirmModal.svelte';
  import PriceChart  from '$lib/PriceChart.svelte';
  import InfoHint    from '$lib/InfoHint.svelte';
  import Select      from '$lib/Select.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';

  // Card fullscreen / collapse state for the replay chart grid.
  let _chartFs  = $state(false);
  let _chartCol = $state(false);

  let status       = $state(/** @type {any} */ ({}));
  let results      = $state(/** @type {any[]} */ ([]));
  let orders       = $state(/** @type {any[]} */ ([]));
  let chartSymbols = $state(/** @type {string[]} */ ([]));
  /** @type {Array<{symbol:string, kind:string, underlying:string|null}>} */
  let chartItems   = $state([]);
  let chartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let error        = $state('');
  let loading      = $state(true);
  let starting     = $state(false);

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef  = $state(null);
  let refreshTeardown;

  // Form state — mirrors the Scenario form's structure for consistency.
  let runName     = $state('');
  let _runNameTouched = $state(false);
  let symbolList  = $state(/** @type {string[]} */ ([]));   // MultiSelect of futures
  let symbolOptions = $state(/** @type {{value:string,label:string}[]} */ ([]));
  let dateFrom    = $state('');
  let dateTo      = $state('');
  let interval    = $state('5minute');
  let rateMs      = $state(100);
  let agentIdList = $state(/** @type {string[]} */ ([]));   // MultiSelect of agent ids → strings
  let agentOptions = $state(/** @type {{value:string,label:string}[]} */ ([]));
  let spreadPct   = $state(0.10);

  // Auto-generated run-name pattern — `backtest-{HHMMSS}`, refreshed
  // on every From-date change until the operator overtypes.
  function _autoRunName() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `backtest-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }
  $effect(() => {
    // Re-trigger when dateFrom or dateTo changes; stop after the
    // operator overtypes.
    const _ = dateFrom + dateTo;
    if (_runNameTouched) return;
    runName = _autoRunName();
  });

  let _replayRefreshing = $state(false);
  async function _refreshReplay() {
    _replayRefreshing = true;
    try { await load(); } finally { _replayRefreshing = false; }
  }

  async function load() {
    try {
      const [stat, res, ord, syms] = await Promise.all([
        fetchReplayStatus(),
        fetchReplayResults().catch(() => []),
        fetchReplayOrders(50).catch(() => []),
        fetchChartSymbols('replay').catch(() => ({ items: [], symbols: [] })),
      ]);
      status       = stat;
      results      = res || [];
      orders       = ord || [];
      // Only assign symbols/items when the /api/charts/symbols call
      // actually returned a payload; otherwise keep last-good. Earlier
      // a transient 500 here turned syms into null → chartSymbols=[]
      // → the `else` branch wiped chartsBySymbol = {} → every embedded
      // PriceChart blanked.
      if (syms && Array.isArray(syms.symbols)) {
        chartSymbols = syms.symbols;
        chartItems   = syms.items || chartItems;
        if (chartSymbols.length) {
          try {
            const batch = await fetchChartBatch('replay', chartSymbols);
            const map = /** @type {Record<string, any>} */ ({});
            for (const c of (batch?.charts || [])) map[c.symbol] = c;
            chartsBySymbol = map;
          } catch (_) { /* charts fall back to per-chart self-poll; keep last batch */ }
        } else {
          // GENUINELY empty symbol set from the backend — safe to drop charts.
          chartsBySymbol = {};
        }
      }

      error = '';
    } catch (e) {
      if (!status?.branch) error = e.message;
    } finally {
      loading = false;
    }
  }

  async function loadFormOptions() {
    // Symbols MultiSelect — futures-first from the instruments cache.
    // Filter to t='FUT' so the picker stays focused; operator can
    // still type to extend (MultiSelect supports free-form add via
    // its options list).
    try {
      const inst = await fetch('/api/instruments', {
        headers: { ...(typeof window !== 'undefined'
          ? { Authorization: `Bearer ${sessionStorage.getItem('ramboq_token') || ''}` } : {}) },
      }).then((r) => r.ok ? r.json() : { items: [] });
      const items = inst.items || [];
      const futs = items.filter((x) => x.t === 'FUT')
        .slice(0, 200) // cap so the picker doesn't load thousands
        .map((x) => ({ value: x.s, label: `${x.s} (${x.e || 'NFO'})` }));
      symbolOptions = futs;
    } catch (_) { /* ignore — operator can still type ticker into the picker */ }
    // Agents MultiSelect.
    try {
      const ags = await fetchAgents();
      agentOptions = (ags || [])
        .filter((a) => ['active', 'cooldown', 'inactive'].includes(a.status))
        .map((a) => ({ value: String(a.id), label: `${a.slug} (${a.id})` }));
    } catch (_) { /* ignore */ }
  }

  onMount(() => {
    load();
    loadFormOptions();
    refreshTeardown = visibleInterval(load, 5000);
  });
  onDestroy(() => refreshTeardown?.());

  async function handleStart() {
    error = '';
    starting = true;
    try {
      const aidList = agentIdList
        .map((s) => parseInt(s, 10))
        .filter((n) => Number.isFinite(n));
      await startReplay({
        symbols:    symbolList.map((s) => String(s).toUpperCase()),
        date_from:  dateFrom,
        date_to:    dateTo,
        interval,
        rate_ms:    rateMs,
        agent_ids:  aidList.length ? aidList : undefined,
        spread_pct: spreadPct,
      });
      await load();
    } catch (e) {
      error = e.message;
    } finally {
      starting = false;
    }
  }

  async function handleStop() {
    try {
      await stopReplay();
      await load();
    } catch (e) {
      error = e.message;
    }
  }

  async function handleClear() {
    const ok = await _confirmRef?.ask({
      title: 'Delete replay data?',
      message: 'Delete all replay orders and events? This cannot be undone.',
      danger: true,
      confirmLabel: 'Delete',
    });
    if (!ok) return;
    try {
      await clearReplayData();
      await load();
    } catch (e) {
      error = e.message;
    }
  }

  const enabled = $derived(status?.enabled !== false);
  const branch  = $derived(branchLabel(status?.branch || ''));
</script>

<ConfirmModal bind:this={_confirmRef} />

{#if !enabled}
  <div class="sim-banner sim-banner-warn">
    Replay is disabled on <strong>{branch}</strong>. Enable <code>cap_in_{branch === 'prod' ? 'prod' : 'dev'}.replay</code> in backend_config.yaml.
  </div>
{/if}

{#if error}
  <div class="sim-banner sim-banner-error">{error}</div>
{/if}

<!-- Progress -->
{#if status?.active}
  <div class="sim-progress mb-3">
    <div class="sim-progress-bar">
      <div class="sim-progress-fill" style="width: {status.total_ticks ? (status.tick_index / status.total_ticks * 100) : 0}%"></div>
    </div>
    <span class="sim-progress-label">
      Tick {status.tick_index} / {status.total_ticks}
      {#if status.date_from && status.date_to}
        &nbsp;·&nbsp; {status.date_from} → {status.date_to}
      {/if}
    </span>
  </div>
{/if}

<!-- Backtest control panel — grid layout mirrors the Scenario form. -->
<div class="bt-form">
  <!-- Row 1: Run name (full width, auto-generated, overtype-able). -->
  <div class="bt-field bt-field-wide">
    <label class="field-label" for="bt-run-name"
           title="Default auto-generated from a timestamp. Overtype to label your backtest.">Run name</label>
    <input id="bt-run-name" type="text"
           class="field-input"
           placeholder="auto"
           bind:value={runName}
           oninput={() => { _runNameTouched = true; }} />
  </div>

  <!-- Row 2: Symbols MultiSelect (full width). -->
  <div class="bt-field bt-field-wide">
    <label class="field-label" for="bt-symbols"
           title="Futures contracts from the live instruments cache. Pick one or more.">Symbols</label>
    <MultiSelect id="bt-symbols" bind:value={symbolList}
                 options={symbolOptions}
                 placeholder="Pick at least one future" />
  </div>

  <!-- Row 3: From | To | Interval. -->
  <div class="bt-field">
    <label class="field-label" for="bt-from">From</label>
    <input id="bt-from" type="date" class="field-input" bind:value={dateFrom} />
  </div>
  <div class="bt-field">
    <label class="field-label" for="bt-to">To</label>
    <input id="bt-to" type="date" class="field-input" bind:value={dateTo} />
  </div>
  <div class="bt-field">
    <label class="field-label" for="bt-interval">Interval</label>
    <Select ariaLabel="Interval" bind:value={interval}
            options={[
              { value: 'minute',   label: '1 min' },
              { value: '5minute',  label: '5 min' },
              { value: '15minute', label: '15 min' },
              { value: 'day',      label: 'Day' },
            ]} />
  </div>

  <!-- Row 4: Playback rate stepper | Spread % | Agents MultiSelect. -->
  <div class="bt-field">
    <label class="field-label" for="bt-rate">Playback (ms)</label>
    <div class="iter-stepper">
      <button type="button" class="iter-stepper-btn"
              onclick={() => rateMs = Math.max(10, Number(rateMs) - 10)}
              aria-label="Decrement rate">−</button>
      <span class="iter-stepper-val" id="bt-rate">{rateMs}</span>
      <button type="button" class="iter-stepper-btn"
              onclick={() => rateMs = Math.min(5000, Number(rateMs) + 10)}
              aria-label="Increment rate">+</button>
    </div>
  </div>
  <div class="bt-field">
    <label class="field-label" for="bt-spread">Spread %</label>
    <input id="bt-spread" type="number" class="field-input bt-input-num"
           bind:value={spreadPct} min="0" max="5" step="0.01" />
  </div>
  <div class="bt-field bt-field-wide">
    <label class="field-label" for="bt-agents"
           title="Restrict the backtest to these agent IDs. Leave empty to run every active agent.">Agents</label>
    <MultiSelect id="bt-agents" bind:value={agentIdList}
                 options={agentOptions}
                 placeholder="(all active)" />
  </div>

  <!-- Row 5: action buttons. -->
  <div class="bt-actions">
    {#if !status?.active}
      <button class="sim-btn sim-btn-start" onclick={handleStart}
              disabled={!enabled || starting || !symbolList.length || !dateFrom || !dateTo}>
        {starting ? 'Starting…' : 'Start Backtest'}
      </button>
    {:else}
      <button class="sim-btn sim-btn-stop" onclick={handleStop}>Stop</button>
    {/if}
    <button class="sim-btn sim-btn-clear" onclick={handleClear} disabled={status?.active}>Clear</button>
  </div>
</div>

<!-- Results -->
{#if results.length > 0}
  <section class="sim-section">
    <h3>Agent Fires ({results.length})</h3>
    <div class="sim-table-wrap">
      <table class="sim-table">
        <thead>
          <tr><th>Tick</th><th>Time</th><th>Agent</th><th>Event</th><th>Detail</th></tr>
        </thead>
        <tbody>
          {#each results as r}
            <tr>
              <td>{r.tick_index}</td>
              <td class="sim-td-mono">{r.timestamp || '—'}</td>
              <td>{r.agent_slug}</td>
              <td>{r.event_type}</td>
              <td class="sim-td-detail">{r.detail || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
{/if}

<!-- Recent orders -->
{#if orders.length > 0}
  <section class="sim-section">
    <h3>Replay Orders ({orders.length})</h3>
    <div class="sim-table-wrap">
      <table class="sim-table">
        <thead>
          <tr><th>ID</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Status</th><th>Detail</th></tr>
        </thead>
        <tbody>
          {#each orders as o}
            <tr>
              <td>{o.id}</td>
              <td>{formatSymbol(o.symbol)}</td>
              <td class={o.side === 'BUY' ? 'sim-buy' : 'sim-sell'}>{o.side}</td>
              <td>{o.quantity}</td>
              <td class="sim-td-mono">{o.initial_price != null ? `₹${o.initial_price.toLocaleString()}` : '—'}</td>
              <td><span class="sim-pill sim-pill-replay">{o.status}</span></td>
              <td class="sim-td-detail">{o.detail || '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </section>
{/if}

<!-- Chart grid -->
{#if chartSymbols.length || (!loading && status?.active)}
  <section class="sim-card" class:fs-card-on={_chartFs} class:is-collapsed={_chartCol}>
    <CardHeader
      title="Replay Charts"
      detectOverflow={false}
      bind:isFullscreen={_chartFs}
      bind:isCollapsed={_chartCol}
      showSearch={false}
      onRefresh={_refreshReplay}
      bind:refreshLoading={_replayRefreshing}
    />
    <div hidden={_chartCol}>
      {#if chartSymbols.length}
        <div class="replay-charts fs-content-fill mb-3" style="--fs-chrome-h: 5rem;">
          {#each chartSymbols as sym (sym)}
            <PriceChart mode="replay" symbol={sym} height={170}
                        data={chartsBySymbol[sym]}
                        {chartsBySymbol} />
          {/each}
        </div>
      {:else}
        <div class="sim-empty-charts">
          No chart ticks captured yet — charts populate as the replay progresses.
        </div>
      {/if}
    </div>
  </section>
{/if}

<ActivityLogSurface
  context="card"
  heightClass="h-[40vh]"
  label="Log"
  defaultTab="order"
  mode="replay"
  hideInlineAccountFilter={false}
/>

<style>
  /* ── Backtest form (post-rewrite) ──────────────────────────────────
     Grid layout mirrors the Scenario form's `.iter-form`. 4 columns
     on desktop; `bt-field-wide` spans 2 columns for full-width
     fields (Run name, Symbols MultiSelect). */
  .bt-form {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.5rem 0.6rem;
    align-items: end;
    background: rgba(15,23,42,0.6);
    border: 1px solid rgba(148,163,184,0.12);
    border-radius: 0.5rem;
    padding: 0.85rem 0.95rem;
    margin-bottom: 0.75rem;
  }
  .bt-field { min-width: 0; display: flex; flex-direction: column; gap: 0.2rem; }
  .bt-field-wide { grid-column: span 2; }
  .bt-field .field-label {
    font-size: var(--fs-xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #94a3b8;
  }
  .bt-field .field-input {
    background: rgba(13, 21, 38, 0.6);
    border: 1px solid rgba(148, 163, 184, 0.25);
    color: var(--algo-slate);
    padding: 0.3rem 0.45rem;
    border-radius: 4px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    width: 100%;
  }
  .bt-field .field-input:focus {
    outline: none;
    border-color: rgba(251, 191, 36, 0.50);
  }
  .bt-input-num { font-variant-numeric: tabular-nums; }
  .bt-actions {
    grid-column: 1 / -1;
    display: flex;
    gap: 0.5rem;
    margin-top: 0.3rem;
  }
  /* Stepper — shared with iter-stepper. Re-declared locally so the
     panel renders the same regardless of cascade order. */
  .bt-form .iter-stepper {
    display: inline-flex;
    align-items: center;
    background: rgba(13, 21, 38, 0.6);
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: 4px;
    overflow: hidden;
    height: 1.6rem;
  }
  .bt-form .iter-stepper-btn {
    appearance: none;
    background: transparent;
    border: none;
    color: var(--c-action);
    font-family: var(--font-numeric);
    font-size: var(--fs-xl);
    font-weight: 700;
    line-height: 1;
    width: 1.6rem;
    cursor: pointer;
  }
  .bt-form .iter-stepper-btn:hover {
    background: rgba(251, 191, 36, 0.15);
    color: #fde68a;
  }
  .bt-form .iter-stepper-val {
    min-width: 2.5rem;
    text-align: center;
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    color: #fde68a;
    font-variant-numeric: tabular-nums;
    border-left: 1px solid var(--algo-amber-border-soft);
    border-right: 1px solid var(--algo-amber-border-soft);
    padding: 0 0.35rem;
  }
  /* Mobile collapse — single column. */
  @media (max-width: 640px) {
    .bt-form { grid-template-columns: 1fr; padding: 0.7rem; }
    .bt-field-wide { grid-column: span 1; }
  }

  .sim-banner        { padding: 0.5rem 0.75rem; border-radius: 0.375rem; font-size: var(--fs-lg); margin-bottom: 0.75rem; }
  .sim-banner-warn   { background: rgba(251,191,36,0.10); color: var(--c-action); border: 1px solid rgba(251,191,36,0.20); }
  .sim-banner-error  { background: var(--c-short-10); color: var(--c-short); border: 1px solid rgba(248,113,113,0.20); }

  .sim-btn           { padding: 0.4rem 1rem; border-radius: 0.375rem; font-size: var(--fs-lg); font-weight: 600;
                        cursor: pointer; border: 1px solid transparent; transition: all 0.15s; }
  .sim-btn:disabled  { opacity: 0.4; cursor: not-allowed; }
  .sim-btn-start     { background: rgba(74,222,128,0.15); color: var(--c-long); border-color: rgba(74,222,128,0.3); }
  .sim-btn-start:hover:not(:disabled) { background: rgba(74,222,128,0.25); }
  .sim-btn-stop      { background: rgba(248,113,113,0.15); color: var(--c-short); border-color: rgba(248,113,113,0.3); }
  .sim-btn-clear     { background: rgba(148,163,184,0.10); color: #94a3b8; border-color: rgba(148,163,184,0.2); }

  .sim-progress      { margin-bottom: 0; }
  .sim-progress-bar  { height: 6px; background: rgba(148,163,184,0.15); border-radius: 3px; overflow: hidden; }
  .sim-progress-fill { height: 100%; background: var(--c-long); border-radius: 3px; transition: width 0.3s; }
  .sim-progress-label { font-size: var(--fs-md); color: #94a3b8; margin-top: 0.25rem; display: block; }

  .replay-charts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
  }
  :global(.sim-card.fs-card-on) .replay-charts {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  :global(.sim-card.fs-card-on) .replay-charts :global(.price-chart) {
    flex: 1 1 0;
    min-height: 200px;
  }

  .sim-section       { margin-bottom: 1.5rem; }
  .sim-section h3    { font-size: var(--fs-xl); font-weight: 600; color: var(--algo-slate); margin-bottom: 0.5rem; }
  .sim-table-wrap    { overflow-x: auto; }
  .sim-table         { width: 100%; border-collapse: collapse; font-size: var(--fs-lg); }
  .sim-table th      { text-align: left; padding: 0.35rem 0.5rem; color: #94a3b8; border-bottom: 1px solid rgba(148,163,184,0.15); font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; font-size: var(--fs-sm); }
  .sim-table td      { padding: 0.3rem 0.5rem; color: var(--algo-slate); border-bottom: 1px solid rgba(148,163,184,0.06); }
  .sim-td-mono       { font-family: 'JetBrains Mono', monospace; font-size: var(--fs-md); }
  .sim-td-detail     { max-width: 24rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sim-buy           { color: #38bdf8; }
  .sim-sell          { color: #fb923c; }
  .sim-pill          { font-size: var(--fs-sm); font-weight: 700; padding: 0.1rem 0.4rem; border-radius: 9999px; }
  .sim-pill-replay   { color: var(--c-long); background: rgba(74,222,128,0.12); }
  .sim-empty-charts  { font-size: var(--fs-md); color: #64748b; font-style: italic; margin-bottom: 0.75rem; }

  @media (max-width: 768px) {
    .replay-charts       { grid-template-columns: 1fr; }
    .sim-table-wrap      { -webkit-overflow-scrolling: touch; }
    .sim-table td,
    .sim-table th        { padding: 0.25rem 0.35rem; font-size: var(--fs-md); }
    .sim-td-detail       { max-width: 12rem; }
  }
</style>
