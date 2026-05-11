<script>
  // Simulator panel — extracted from /admin/simulator/+page.svelte.
  // Self-contained: polls /api/simulator/*, renders controls, status,
  // chart grid, and an embedded LogPanel. Accepts no required props.

  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/state';
  import { clientTimestamp, visibleInterval, branchLabel } from '$lib/stores';
  import {
    fetchSimScenarios, fetchSimStatus, startSim, stopSim, stepSim,
    runSimCycle, clearSimArtefacts, seedSimLive, fetchSimEvents,
    fetchSimTicks, fetchAgents,
    fetchChartSymbols, fetchChartBatch, fetchAdminLogs,
  } from '$lib/api';
  import LogPanel    from '$lib/LogPanel.svelte';
  import Select      from '$lib/Select.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import PriceChart  from '$lib/PriceChart.svelte';
  import InfoHint    from '$lib/InfoHint.svelte';
  import { priceFmt, aggFmt, qtyFmt } from '$lib/format';

  let scenarios = $state(/** @type {any[]} */ ([]));
  let status    = $state(/** @type {any} */ ({}));
  let events    = $state(/** @type {any[]} */ ([]));
  let agents    = $state(/** @type {any[]} */ ([]));
  let error     = $state('');
  let note      = $state('');
  let pickedSlug = $state('');
  let seedMode  = $state(/** @type {'scripted'|'live'|'live+scenario'} */ ('scripted'));
  let rateMs    = $state(2000);
  let positionsEveryN = $state(/** @type {number | ''} */ (''));
  let marketStatePreset = $state(/** @type {''|'pre_open'|'at_open'|'mid_session'|'pre_close'|'at_close'|'post_close'|'expiry_day'} */(''));
  let pctOverrides = $state(/** @type {Array<number | ''>} */([]));
  let symbolFilter = $state(/** @type {string[]} */([]));
  let spreadPct    = $state(/** @type {number | ''} */(0.10));
  // Random-walk parameter overrides — surfaced only when the picked
  // scenario contains a walk-shaped move. Drift and vol are entered as
  // percent (UI surface) and converted to decimal fraction at submit.
  let walkDrift    = $state(/** @type {number | ''} */ (''));   // e.g. -0.05 (%)
  let walkVol      = $state(/** @type {number | ''} */ (''));   // e.g.  0.30 (%)
  let walkSeed     = $state(/** @type {number | ''} */ (''));   // e.g.  42
  // Chase-engine cap override for this run. When blank, falls back to
  // the DB setting `simulator.chase_max_attempts` (default 5).
  let chaseMaxAttempts = $state(/** @type {number | ''} */ (''));
  let customRows = $state(/** @type {Array<{tradingsymbol:string, quantity:string|number, last_price:string|number, account:string}>} */ ([]));

  function addCustomRow() {
    customRows = [...customRows, { tradingsymbol: '', quantity: '', last_price: '', account: '' }];
  }
  function removeCustomRow(/** @type {number} */ i) {
    customRows = customRows.filter((_, idx) => idx !== i);
  }

  let agentId   = $state('');
  let liveSnap  = $state(/** @type {any} */ (null));
  let chartSymbols = $state(/** @type {string[]} */ ([]));
  let chartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let refreshTeardown;

  // Log panel feeds
  let simLog    = $state(/** @type {any[]} */ ([]));
  let systemLog = $state(/** @type {string[]} */ ([]));
  let logTab    = $state('simulator');

  async function loadSimLog() {
    try { simLog = await fetchSimTicks(100) || []; }
    catch (_) { /* ignore — cap flag off */ }
  }
  async function loadSystemLog() {
    try {
      const d = await fetchAdminLogs(100);
      systemLog = d.lines || [];
    } catch (_) { /* ignore */ }
  }
  function loadCurrentLog() {
    // 'order' tab is now self-fetching via UnifiedLog in LogPanel.
    if (logTab === 'simulator') loadSimLog();
    else if (logTab === 'system') loadSystemLog();
  }

  async function loadHot() {
    try {
      const [stat, ev, chartSyms] = await Promise.all([
        fetchSimStatus(), fetchSimEvents(100),
        fetchChartSymbols('sim').catch(() => ({ symbols: [] })),
      ]);
      status = stat;
      events = ev;
      chartSymbols = chartSyms?.symbols || [];
      if (chartSymbols.length) {
        try {
          const batch = await fetchChartBatch('sim', chartSymbols);
          const map = /** @type {Record<string, any>} */ ({});
          for (const c of (batch?.charts || [])) map[c.symbol] = c;
          chartsBySymbol = map;
        } catch (_) { /* ignore — charts fall back to self-poll */ }
      } else {
        chartsBySymbol = {};
      }
    } catch (e) { error = e.message; }
  }

  async function loadStatic() {
    try {
      const [scList, ag] = await Promise.all([
        fetchSimScenarios(), fetchAgents(),
      ]);
      scenarios = scList;
      agents    = ag;
      if (!pickedSlug && scenarios.length) pickedSlug = scenarios[0].slug;
    } catch (e) { error = e.message; }
  }

  async function loadAll() {
    await Promise.all([loadHot(), loadStatic()]);
  }

  async function doStart() {
    error = ''; note = '';
    try {
      const opts = { seed_mode: seedMode };
      if (agentId) opts.agent_ids = [Number(agentId)];
      if (positionsEveryN !== '' && positionsEveryN != null) opts.positions_every_n_ticks = Number(positionsEveryN);
      if (marketStatePreset) opts.market_state_preset = marketStatePreset;
      if (pctOverrides.some(v => v !== '' && v != null)) {
        opts.pct_overrides = pctOverrides.map(v =>
          v === '' || v == null ? null : Number(v) / 100);
      }
      if (symbolFilter && symbolFilter.length) opts.symbols = [...symbolFilter];
      if (spreadPct !== '' && spreadPct != null) opts.spread_pct = Number(spreadPct);
      // Random-walk overrides — drift / vol entered as percent in the
      // UI, sent as decimal fractions (the same convention as
      // pct_overrides). Seed is a plain integer.
      if (walkDrift !== '' && walkDrift != null) opts.walk_drift = Number(walkDrift) / 100;
      if (walkVol   !== '' && walkVol   != null) opts.walk_vol   = Number(walkVol)   / 100;
      if (walkSeed  !== '' && walkSeed  != null) opts.walk_seed  = Number(walkSeed);
      if (chaseMaxAttempts !== '' && chaseMaxAttempts != null) {
        opts.chase_max_attempts = Math.max(1, Math.min(50, Number(chaseMaxAttempts)));
      }
      const customClean = customRows
        .map(r => ({
          tradingsymbol: String(r.tradingsymbol || '').trim().toUpperCase(),
          quantity:      r.quantity === '' ? null : Number(r.quantity),
          last_price:    r.last_price === '' ? null : Number(r.last_price),
          account:       String(r.account || '').trim() || undefined,
        }))
        .filter(r => r.tradingsymbol && r.quantity != null && r.last_price != null);
      if (customClean.length) opts.custom_positions = customClean;
      status = await startSim(pickedSlug, rateMs, opts);
      const tag = agentId ? ` (agent #${agentId} only)` : '';
      const cadTag = ` · P:${status.positions_every_n_ticks}`;
      const msTag  = status.market_state_preset ? ` · market=${status.market_state_preset}` : '';
      note = `Started ${pickedSlug} · seed=${seedMode} · ${rateMs}ms${cadTag}${msTag}${tag}`;
    } catch (e) { error = e.message; }
  }
  async function doStop() {
    error = ''; note = '';
    try { status = await stopSim(); note = 'Stopped.'; }
    catch (e) { error = e.message; }
  }
  async function doStep() {
    error = ''; note = '';
    try { status = await stepSim(); note = `Applied tick ${status.tick_index}`; }
    catch (e) { error = e.message; }
  }
  async function doRunCycle() {
    error = ''; note = '';
    try { await runSimCycle(); note = 'Agent engine run on current sim state.'; loadAll(); }
    catch (e) { error = e.message; }
  }
  async function doSeedLive() {
    error = ''; note = '';
    try {
      const snap = await seedSimLive();
      liveSnap = snap;
      note = `Live book snapshot: ${snap.positions_count} positions · ${snap.margins_count} margins · accounts=[${snap.accounts.join(', ')}]`;
      if (seedMode === 'scripted') seedMode = 'live';
      loadAll();
    } catch (e) { error = e.message; }
  }
  async function doClear() {
    error = ''; note = '';
    try {
      const r = await clearSimArtefacts();
      note = `Cleared ${r.events_deleted} events + ${r.orders_deleted} simulator orders`;
      loadAll();
    } catch (e) { error = e.message; }
  }

  const armedAgent = $derived.by(() => {
    if (!agentId) return null;
    return agents.find(a => String(a.id) === String(agentId)) || null;
  });

  const pickedScenario = $derived(scenarios.find(s => s.slug === pickedSlug));
  const simOff = $derived(status?.enabled === false);
  const symbolOptions  = $derived.by(() => {
    /** @type {Set<string>} */
    const pool = new Set();
    for (const s of (liveSnap?.symbols         || [])) if (s) pool.add(s);
    for (const s of (status?.symbols           || [])) if (s) pool.add(s);
    for (const s of (pickedScenario?.initial_symbols || [])) if (s) pool.add(s);
    return [...pool].sort().map(s => ({ value: s, label: s }));
  });

  /** @param {string} name */
  function shortName(name) {
    return (name || '').replace(/\s*\([^)]*\)\s*/g, '').trim();
  }

  $effect(() => {
    const picked = scenarios.find(s => s.slug === pickedSlug);
    const pcts = picked?.tick_pcts || [];
    pctOverrides = pcts.map(v => v == null ? '' : Number((v * 100).toFixed(2)));
  });

  onMount(() => {
    const q = page.url.searchParams.get('agent_id');
    if (q) agentId = q;
    loadAll();
    loadSimLog();
    loadSystemLog();
    (async () => {
      try {
        liveSnap = await seedSimLive();
        if (seedMode === 'scripted') seedMode = 'live';
      } catch (_) { /* ignore */ }
    })();
    refreshTeardown = visibleInterval(() => { loadHot(); loadCurrentLog(); }, 3000);
  });
  onDestroy(() => { refreshTeardown?.(); });
</script>

{#if error}
  <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">
    {error}
  </div>
{/if}
{#if note}
  <div class="mb-3 p-2 rounded bg-emerald-500/10 text-emerald-300 text-[0.65rem] border border-emerald-500/30">
    {note}
  </div>
{/if}

{#if armedAgent}
  <div class="mb-3 p-2 rounded bg-[#fbbf24]/15 text-[#fbbf24] text-[0.65rem] border border-[#fbbf24]/50">
    Isolated run armed — will dry-fire <b>#{armedAgent.id} {armedAgent.name}</b>
    (bypasses schedule / cooldown / baseline gates).
    <button type="button" onclick={() => { agentId = ''; }}
      class="ml-2 text-[0.6rem] underline">Clear</button>
  </div>
{/if}

<!-- Status bar -->
<div class="algo-status-card p-3 mb-3" data-status={status.active ? 'triggered' : 'inactive'}>
  <div class="flex items-center flex-wrap gap-2 text-[0.7rem]">
    <span class="w-2 h-2 rounded-full {status.active ? 'bg-red-500 animate-pulse' : 'bg-slate-500'}"></span>
    <span class="text-[#fbbf24] font-semibold">{status.active ? 'RUNNING' : 'idle'}</span>
    {#if status.scenario}
      <span class="font-mono text-[#7dd3fc]">scenario: {status.scenario}</span>
      <span class="text-[#7e97b8]">|</span>
      <span>seed: {status.seed_mode}</span>
      <span class="text-[#7e97b8]">|</span>
      <span>tick {status.tick_index}/{status.total_ticks}</span>
      <span class="text-[#7e97b8]">|</span>
      <span>rate: {status.rate_ms}ms</span>
      <span class="text-[#7e97b8]">|</span>
      <span title="Positions refresh every N ticks">
        cadence P:{status.positions_every_n_ticks}
      </span>
      <span class="text-[#7e97b8]">|</span>
      <span title="Simulated market state — segment flags + minutes-since-open drive time-aware agents">
        market: <span class="text-[#fde68a]">{status.market_state_preset ?? 'mid_session'}</span>
      </span>
      <span class="text-[#7e97b8]">|</span>
      <span>started: {status.started_at?.slice(11, 19) ?? '—'}</span>
      {#if status.only_agent_ids?.length}
        <span class="text-[#7e97b8]">|</span>
        <span class="text-[#fbbf24]">agents=[{status.only_agent_ids.join(',')}]</span>
      {/if}
    {/if}
  </div>
  {#if liveSnap}
    <div class="text-[0.6rem] text-[#c8d8f0]/70 mt-1">
      Live snapshot: {liveSnap.snapshot_at?.slice(11, 19)} ·
      {liveSnap.positions_count}P / {liveSnap.margins_count}M
      · accounts=[{liveSnap.accounts.join(', ')}]
    </div>
  {/if}

  {#if status?.positions?.length}
    <div class="sim-pills mt-2">
      <span class="sim-pills-label">Positions ({status.positions.length}):</span>
      {#each status.positions as p}
        <span class="sim-pill sim-pill-{p.quantity >= 0 ? 'long' : 'short'}"
              title={`LTP ₹${priceFmt(p.last_price)} · bid ₹${priceFmt(p.bid)} · ask ₹${priceFmt(p.ask)}`}>
          <span class="sim-pill-side">{p.quantity >= 0 ? 'LONG' : 'SHORT'}</span>
          <span class="sim-pill-sym">{p.symbol}</span>
          <span class="sim-pill-qty">{qtyFmt(Math.abs(p.quantity ?? 0))}</span>
          <span class="sim-pill-pnl {(p.pnl ?? 0) < 0 ? 'neg' : (p.pnl ?? 0) > 0 ? 'pos' : ''}">
            ₹{aggFmt(p.pnl ?? 0)}
          </span>
        </span>
      {/each}
    </div>
  {/if}

  {#if status?.open_order_details?.length}
    <div class="sim-pills mt-1">
      <span class="sim-pills-label">Chasing ({status.open_order_details.length}):</span>
      {#each status.open_order_details as o}
        <span class="sim-pill sim-pill-chase">
          <span class="sim-pill-side sim-pill-side-{o.side === 'BUY' ? 'buy' : 'sell'}">{o.side}</span>
          <span class="sim-pill-sym">{o.symbol}</span>
          <span class="sim-pill-qty">{qtyFmt(o.qty)}</span>
          <span class="sim-pill-limit">@₹{priceFmt(o.limit_price)}</span>
          <span class="sim-pill-attempts">#{o.attempts}</span>
        </span>
      {/each}
    </div>
  {/if}

  {#if chartSymbols.length}
    <div class="sim-charts">
      {#each chartSymbols as sym (sym)}
        <PriceChart mode="sim" symbol={sym} height={150}
                    data={chartsBySymbol[sym]}
                    {chartsBySymbol} />
      {/each}
    </div>
  {/if}
</div>

<!-- Controls card -->
<div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
  <div class="sim-scenario-row">
    <div class="sim-field sim-field-scenario">
      <label for="sim-scenario" class="field-label">Scenario</label>
      <Select id="sim-scenario" bind:value={pickedSlug}
        options={scenarios.map(s => ({
          value: s.slug,
          label: shortName(s.name),
        }))} />
    </div>
    <div class="sim-field sim-field-symbol">
      <label for="sim-symbol" class="field-label" title="Restrict sim to one or more tradingsymbols. Default: all positions.">Symbol</label>
      <MultiSelect id="sim-symbol" bind:value={symbolFilter}
        options={symbolOptions}
        placeholder="(all positions)" />
    </div>
    <div class="sim-field sim-field-spread">
      <label for="sim-spread" class="field-label" title="Bid/ask spread applied to every position. SELL orders quote the bid, BUY orders quote the ask. Drives the paper-trade chase engine.">Spread %</label>
      <div class="sim-pct-cell">
        <input id="sim-spread" type="number" min="0" step="0.01"
               class="field-input sim-pct-input"
               bind:value={spreadPct} />
      </div>
    </div>
    <div class="sim-field">
      <label for="sim-chase-max" class="field-label" title="Max chase attempts before a paper order is marked UNFILLED. Blank = DB default (simulator.chase_max_attempts).">Chase max</label>
      <div class="sim-pct-cell">
        <input id="sim-chase-max" type="number" min="1" max="50" step="1"
               class="field-input sim-pct-input"
               placeholder="(default)" bind:value={chaseMaxAttempts} />
      </div>
    </div>
    {#if pctOverrides.length > 0}
      <div class="sim-field sim-field-pcts">
        <span class="field-label">Tick %</span>
        <div class="sim-pct-inline">
          {#each pctOverrides as _pct, i}
            <div class="sim-pct-cell">
              <input type="number" step="0.5"
                class="field-input sim-pct-input"
                placeholder={String(pickedScenario?.tick_pcts?.[i] != null
                  ? (pickedScenario.tick_pcts[i] * 100).toFixed(2)
                  : '—')}
                disabled={pickedScenario?.tick_pcts?.[i] == null}
                bind:value={pctOverrides[i]} />
            </div>
          {/each}
        </div>
      </div>
    {/if}
    <!-- Walk parameter overrides — visible when the scenario contains
         random_walk or underlying_random_walk moves. Drift and vol are
         entered as percent (UI convention) and converted to decimal
         fraction at submit. Pair with seed for deterministic re-runs. -->
    {#if pickedScenario?.has_walk}
      <div class="sim-field">
        <label for="sim-drift" class="field-label" title="Per-tick drift component for random_walk / underlying_random_walk moves (percent). Negative = bear, positive = bull.">Walk drift %</label>
        <input id="sim-drift" type="number" step="0.01" class="field-input sim-pct-input"
               placeholder="(YAML)" bind:value={walkDrift} />
      </div>
      <div class="sim-field">
        <label for="sim-vol" class="field-label" title="Per-tick volatility σ for random_walk / underlying_random_walk moves (percent). Higher = more chop.">Walk vol %</label>
        <input id="sim-vol" type="number" step="0.01" min="0" class="field-input sim-pct-input"
               placeholder="(YAML)" bind:value={walkVol} />
      </div>
      <div class="sim-field">
        <label for="sim-seed-walk" class="field-label" title="RNG seed for reproducible walks. Same seed = identical tick stream.">Walk seed</label>
        <input id="sim-seed-walk" type="number" step="1" class="field-input sim-pct-input"
               placeholder="(YAML)" bind:value={walkSeed} />
      </div>
    {/if}
  </div>

  <div class="sim-fields-row sim-fields-compact">
    <div class="sim-field">
      <label for="sim-seed" class="field-label">Seed</label>
      <Select id="sim-seed" bind:value={seedMode}
        options={[
          { value: 'scripted',      label: 'Scripted' },
          { value: 'live',          label: 'Live book' },
          { value: 'live+scenario', label: 'Live + scenario' },
        ]} />
    </div>
    <div class="sim-field">
      <label for="sim-rate" class="field-label">Rate (ms)</label>
      <input id="sim-rate" type="number" min="200" step="100" bind:value={rateMs} class="field-input" />
    </div>
    <div class="sim-field">
      <label for="sim-pos-n" class="field-label" title="Positions refresh every N ticks (1 = every tick)">Pos / N</label>
      <input id="sim-pos-n" type="number" min="1" step="1" placeholder="1"
             bind:value={positionsEveryN} class="field-input" />
    </div>
    <div class="sim-field">
      <label for="sim-market" class="field-label" title="Simulated market clock — overrides the scenario's YAML value">Market</label>
      <Select id="sim-market" bind:value={marketStatePreset}
        options={[
          { value: '',            label: '(scenario)' },
          { value: 'pre_open',    label: 'Pre-open' },
          { value: 'at_open',     label: 'At open' },
          { value: 'mid_session', label: 'Mid-session' },
          { value: 'pre_close',   label: 'Pre-close' },
          { value: 'at_close',    label: 'At close' },
          { value: 'post_close',  label: 'Post-close' },
          { value: 'expiry_day',  label: 'Expiry day' },
        ]} />
    </div>
  </div>

  <div class="sim-buttons-row">
    <button type="button" onclick={doSeedLive}
      disabled={simOff}
      class="sim-btn sim-btn-load disabled:opacity-40">Load live book</button>
    <button type="button" onclick={doStart}
      disabled={simOff || status.active}
      class="sim-btn sim-btn-primary disabled:opacity-40">Start</button>
    <button type="button" onclick={doStop}
      disabled={simOff || !status.active}
      class="sim-btn sim-btn-secondary disabled:opacity-40">Stop</button>
    <button type="button" onclick={doStep}
      disabled={simOff}
      class="sim-btn sim-btn-step disabled:opacity-40">Step</button>
    <button type="button" onclick={doRunCycle}
      disabled={simOff}
      class="sim-btn sim-btn-cycle disabled:opacity-40">Run cycle</button>
    <button type="button" onclick={doClear}
      disabled={simOff}
      class="sim-btn sim-btn-danger disabled:opacity-40">Clear sim</button>
  </div>
  {#if simOff}
    <div class="mt-2 p-2 rounded text-[0.65rem] text-amber-200
                bg-amber-500/10 border border-amber-500/40">
      Simulator is disabled on the <b>{branchLabel(status?.branch ?? 'current')}</b>
      branch (cap_in_<b>{branchLabel(status?.branch ?? 'branch')}</b>.simulator is
      off). Toggle it in <code>backend_config.yaml</code> or from the
      Settings page to re-enable.
    </div>
  {/if}
  {#if pickedSlug}
    {@const picked = scenarios.find(s => s.slug === pickedSlug)}
    {#if picked}
      <div class="text-[0.6rem] text-[#c8d8f0]/60 italic mt-2">{picked.description}</div>
      {#if seedMode === 'scripted' && picked.has_initial === false}
        <div class="text-[0.6rem] text-amber-300 mt-2">
          Scenario <b>{picked.slug}</b> has no scripted initial state — price
          moves would have nothing to apply to. Press <b>Load live book</b>
          and switch Seed to <b>Live</b> (or <b>Live + scenario</b>), or pick
          a scenario with scripted data.
        </div>
      {/if}
    {/if}
  {/if}
  {#if seedMode !== 'scripted' && !liveSnap}
    <div class="text-[0.6rem] text-amber-300 mt-2">
      Seed mode <b>{seedMode}</b> requires a live-book snapshot — press
      <b>Load live book</b> before Start.
    </div>
  {/if}
</div>

<!-- Custom positions panel -->
<div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
  <div class="custom-pos-header">
    <h3 class="text-[0.65rem] font-bold uppercase tracking-wider text-[#fbbf24]">
      Custom positions
      {#if customRows.length}<span class="opacity-60 font-normal ml-1">({customRows.length})</span>{/if}
    </h3>
    <button type="button" class="sim-btn sim-btn-order"
            title="Add a synthetic position to layer on top of the seeded book."
            onclick={addCustomRow}>+ Add row</button>
  </div>
  {#if !customRows.length}
    <div class="text-[0.6rem] text-[#7e97b8] mt-1">
      No custom positions. Click <b>+ Add row</b> to layer synthetic
      positions on top of the seeded book.
    </div>
  {:else}
    <div class="custom-pos-grid">
      <div class="custom-pos-headrow">
        <span>Symbol</span>
        <span>Qty</span>
        <span>LTP</span>
        <span>Account</span>
        <span></span>
      </div>
      {#each customRows as _row, i (i)}
        <div class="custom-pos-row">
          <input type="text" class="field-input"
            placeholder="NIFTY25APR22000CE"
            bind:value={customRows[i].tradingsymbol} />
          <input type="number" class="field-input"
            placeholder="±qty"
            bind:value={customRows[i].quantity} />
          <input type="number" class="field-input"
            placeholder="₹ last"
            step="0.05"
            bind:value={customRows[i].last_price} />
          <input type="text" class="field-input"
            placeholder="ZG####"
            bind:value={customRows[i].account} />
          <button type="button" class="custom-pos-del"
                  title="Remove this row"
                  aria-label="Remove row {i + 1}"
                  onclick={() => removeCustomRow(i)}>×</button>
        </div>
      {/each}
    </div>
    <div class="text-[0.55rem] text-[#7e97b8] mt-1">
      Negative qty = short. F&O symbols re-price coherently when an
      <span class="font-mono">underlying_*</span> move fires.
    </div>
  {/if}
</div>

<LogPanel
  heightClass="h-[40vh]"
  defaultTab={logTab}
  simScope
  onTabChange={(id) => { logTab = id; }}
/>

<style>
  .sim-scenario-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 0.35rem 0.4rem;
    margin-bottom: 0.4rem;
    font-size: 0.62rem;
  }
  .sim-fields-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 0.35rem 0.4rem;
    font-size: 0.6rem;
    margin-bottom: 0.5rem;
  }
  .sim-field {
    min-width: 0;
    flex: 1 1 100px;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  :global(.sim-scenario-row .field-label) {
    font-size: 0.5rem;
    margin-bottom: 0;
  }
  :global(.sim-scenario-row .rbq-select-trigger),
  :global(.sim-scenario-row .rbq-multi-trigger),
  :global(.sim-scenario-row input.sim-pct-input) {
    height: 1.7rem !important;
    min-height: 1.7rem !important;
    box-sizing: border-box;
    font-size: 0.62rem !important;
  }
  .sim-field-scenario,
  .sim-field-symbol {
    flex: 4 1 0;
    min-width: 120px;
  }
  .sim-field-spread {
    flex: 1 1 0;
    min-width: 70px;
  }
  .sim-field-pcts {
    flex: 3 1 0;
    min-width: 160px;
  }
  .sim-pct-inline {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.2rem 0.3rem;
    min-height: 1.55rem;
  }
  .sim-pct-cell {
    display: flex;
    align-items: stretch;
    gap: 0.15rem;
    flex: 1 1 0;
    min-width: 0;
  }
  :global(.sim-pct-input) {
    flex: 1 1 0;
    min-width: 0;
    width: 100%;
    font-size: 0.62rem !important;
    padding: 0.25rem 0.4rem !important;
    min-height: 1.55rem !important;
    height: 1.55rem;
    text-align: right;
    box-sizing: border-box;
  }
  :global(.sim-fields-compact .field-input) {
    font-size: 0.62rem !important;
    padding: 0.25rem 0.4rem !important;
    height: 1.7rem !important;
    min-height: 1.7rem !important;
    box-sizing: border-box;
  }
  :global(.sim-fields-compact .rbq-select-trigger),
  :global(.sim-fields-compact .rbq-multi-trigger) {
    height: 1.7rem !important;
    min-height: 1.7rem !important;
    box-sizing: border-box;
    font-size: 0.62rem !important;
  }
  :global(.sim-fields-compact .field-label) {
    font-size: 0.5rem !important;
    margin-bottom: 0 !important;
  }
  .sim-buttons-row {
    display: flex;
    flex-wrap: wrap;
    align-items: stretch;
    gap: 0.35rem;
  }
  :global(.sim-btn) {
    flex: 0 0 auto;
    font-size: 0.6rem;
    line-height: 1;
    padding: 0.35rem 0.5rem;
    border-radius: 3px;
    font-weight: 600;
    font-family: ui-monospace, monospace;
    border: 1px solid transparent;
    cursor: pointer;
    white-space: nowrap;
    letter-spacing: 0.02em;
    text-align: center;
    transition: background-color 0.08s, border-color 0.08s, color 0.08s;
  }
  :global(.sim-btn:disabled) { cursor: not-allowed; }
  :global(.sim-buttons-row .sim-btn) {
    flex: 1 1 0;
    min-width: 90px;
    max-width: none;
  }
  :global(.sim-btn-primary) {
    background: #6ee7b7; color: #022c1e; border-color: #6ee7b7;
    font-weight: 700;
  }
  :global(.sim-btn-primary:hover:not(:disabled)) {
    background: #a7f3d0; border-color: #a7f3d0;
  }
  :global(.sim-btn-primary:disabled) {
    background: rgba(110,231,183,0.3);
    color: rgba(2,44,30,0.7);
    border-color: rgba(110,231,183,0.5);
  }
  :global(.sim-btn-secondary) {
    background: rgba(148,163,184,0.12);
    color: #c8d8f0;
    border-color: rgba(148,163,184,0.45);
  }
  :global(.sim-btn-secondary:hover:not(:disabled)) {
    background: rgba(148,163,184,0.22);
    border-color: #94a3b8;
  }
  :global(.sim-btn-load) {
    background: rgba(16,185,129,0.15); color: #6ee7b7; border-color: rgba(16,185,129,0.5);
  }
  :global(.sim-btn-load:hover) {
    background: rgba(16,185,129,0.25); border-color: #10b981;
  }
  :global(.sim-btn-step) {
    background: rgba(125,211,252,0.15); color: #7dd3fc; border-color: rgba(125,211,252,0.5);
  }
  :global(.sim-btn-step:hover) {
    background: rgba(125,211,252,0.25); border-color: #7dd3fc;
  }
  :global(.sim-btn-cycle) {
    background: rgba(251,191,36,0.15); color: #fbbf24; border-color: rgba(251,191,36,0.5);
  }
  :global(.sim-btn-cycle:hover) {
    background: rgba(251,191,36,0.25); border-color: #fbbf24;
  }
  :global(.sim-btn-danger) {
    background: rgba(248,113,113,0.1); color: #f87171; border-color: rgba(248,113,113,0.5);
  }
  :global(.sim-btn-danger:hover) {
    background: rgba(248,113,113,0.2); border-color: #f87171;
  }
  .sim-pills {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.3rem 0.4rem;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
  }
  .sim-pills-label {
    color: rgba(200,216,240,0.55);
    font-size: 0.52rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .sim-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.15rem 0.45rem;
    border-radius: 3px;
    border: 1px solid rgba(255,255,255,0.12);
    background: rgba(13,22,42,0.55);
    color: #c8d8f0;
    white-space: nowrap;
  }
  .sim-pill-side {
    font-weight: 700;
    font-size: 0.5rem;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0 0.25rem;
    border-radius: 2px;
  }
  .sim-pill-long  { border-color: rgba(56,189,248,0.45); }
  .sim-pill-long  .sim-pill-side { background: rgba(56,189,248,0.22); color: #38bdf8; }
  .sim-pill-short { border-color: rgba(251,146,60,0.45); }
  .sim-pill-short .sim-pill-side { background: rgba(251,146,60,0.22); color: #fb923c; }
  .sim-pill-chase { border-color: rgba(251,191,36,0.45); background: rgba(251,191,36,0.06); }
  .sim-pill-side-buy  { background: rgba(110,231,183,0.22); color: #6ee7b7; }
  .sim-pill-side-sell { background: rgba(248,113,113,0.22);  color: #fda4af; }
  .sim-pill-sym { color: #fde68a; font-weight: 600; }
  .sim-pill-qty { color: #c8d8f0; }
  .sim-pill-limit { color: #7dd3fc; }
  .sim-pill-attempts {
    color: #fbbf24;
    font-weight: 700;
    border-left: 1px solid rgba(251,191,36,0.35);
    padding-left: 0.35rem;
    margin-left: 0.1rem;
  }
  .sim-pill-pnl.neg { color: #f87171; }
  .sim-pill-pnl.pos { color: #4ade80; }
  .sim-charts {
    margin-top: 0.6rem;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
  }
  .custom-pos-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .custom-pos-grid {
    margin-top: 0.4rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .custom-pos-headrow,
  .custom-pos-row {
    display: grid;
    grid-template-columns: minmax(0,2fr) minmax(0,1fr) minmax(0,1fr) minmax(0,1fr) auto;
    gap: 0.35rem;
    align-items: center;
  }
  .custom-pos-headrow {
    font-family: monospace;
    font-size: 0.55rem;
    color: #7e97b8;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding-bottom: 0.15rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
  }
  :global(.custom-pos-row .field-input) {
    font-size: 0.62rem;
    padding: 0.25rem 0.4rem;
    font-family: monospace;
  }
  .custom-pos-del {
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 3px;
    border: 1px solid rgba(248,113,113,0.4);
    background: rgba(248,113,113,0.08);
    color: #f87171;
    font-size: 0.85rem;
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }
  .custom-pos-del:hover {
    background: rgba(248,113,113,0.18);
    border-color: rgba(248,113,113,0.65);
  }
  :global(.sim-fields-row .field-input) {
    font-size: 0.62rem;
    padding: 0.25rem 0.4rem;
    height: auto;
    min-height: 1.55rem;
    width: 100%;
  }
  :global(.sim-fields-row .field-label) {
    font-size: 0.5rem;
    margin-bottom: 0.1rem;
  }
</style>
