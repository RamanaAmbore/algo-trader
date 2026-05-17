<script>
  // Simulator panel — extracted from /admin/simulator/+page.svelte.
  // Self-contained: polls /api/simulator/*, renders controls, status,
  // chart grid, and an embedded LogPanel. Accepts no required props.

  import { onMount, onDestroy } from 'svelte';
  import { page } from '$app/state';
  import { clientTimestamp, visibleInterval, branchLabel, logTime, dualTsHtml } from '$lib/stores';
  import {
    fetchSimScenarios, fetchSimStatus, startSim, stopSim, stepSim,
    runSimCycle, clearSimArtefacts, seedSimLive, fetchSimEvents,
    fetchSimTicks, fetchAgents,
    fetchChartBatch, fetchAdminLogs,
    fetchSimDefaults, startSimRun,
    fetchSimOrders, fetchSimIterations, replaySimIteration,
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
  // Sim-mode AlgoOrder rows + past iteration summaries. Both poll on
  // the same cadence as `events` so the activity feed and past-runs
  // panel stay reactive while sim is running AND keep showing the
  // last run's history when the driver is idle.
  let simOrders     = $state(/** @type {any[]} */ ([]));
  let pastIterations = $state(/** @type {any[]} */ ([]));
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
  // Chart grid is now per-UNDERLYING (one chart per NIFTY / BANKNIFTY /
  // FINNIFTY) instead of per-contract — drastic reduction in chart count
  // since options reprice off spot via BS. The list comes from
  // status.underlyings (a {name: spot} dict).
  let chartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let refreshTeardown;
  // Per-underlying spot snapshots from status.underlyings. The chart
  // grid below iterates these names. status.summary_positions /
  // summary_holdings come from the same /api/simulator/status payload.
  const underlyingNames = $derived(Object.keys(status?.underlyings || {}).sort());
  const summaryPositions = $derived(status?.summary_positions || []);
  const summaryHoldings  = $derived(status?.summary_holdings  || []);
  const indicesSnapshot  = $derived.by(() => {
    const out = [];
    const u = status?.underlyings || {};
    const hist = chartsBySymbol || {};
    for (const name of underlyingNames) {
      const ticks = hist[name]?.ticks || [];
      const first = ticks[0]?.ltp;
      const last  = u[name];
      const pct = (first && last) ? (((last - first) / first) * 100) : 0;
      out.push({ name, spot: last, pct });
    }
    return out;
  });

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
      // Parallel fetch — status, agent events, sim orders, past
      // iterations. Past iterations + sim orders persist across
      // sim runs so the workspace never looks "empty" between runs
      // (operator can always refer to previous activity).
      const [stat, ev, ord, iters] = await Promise.all([
        fetchSimStatus(),
        fetchSimEvents(50),
        fetchSimOrders(50).catch(() => []),
        fetchSimIterations(null, 5).catch(() => []),
      ]);
      status = stat;
      events = Array.isArray(ev) ? ev : [];
      simOrders = Array.isArray(ord) ? ord : [];
      pastIterations = Array.isArray(iters) ? iters : [];
      // Fetch a single batch of per-underlying chart histories rather
      // than one PriceChart self-polling per name. Underlyings are
      // typically 1-3 names (NIFTY / BANKNIFTY / FINNIFTY) so this is
      // one round-trip total.
      const names = Object.keys(stat?.underlyings || {});
      if (names.length) {
        try {
          const batch = await fetchChartBatch('sim', names);
          const map = /** @type {Record<string, any>} */ ({});
          for (const c of (batch?.charts || [])) map[c.symbol] = c;
          chartsBySymbol = map;
        } catch (_) { /* ignore — charts fall back to self-poll */ }
      } else {
        chartsBySymbol = {};
      }
    } catch (e) { error = e.message; }
  }

  // Merged activity feed — agent events + sim orders, newest first.
  // Used by the Live Activity panel above the underlying charts.
  // Industry pattern: QuantConnect Live Log, TradingView List of
  // Trades, NinjaTrader Strategy Analyzer — every meaningful signal
  // surfaced in one place adjacent to the price chart.
  const activityFeed = $derived.by(() => {
    const rows = [];
    for (const e of (events || [])) {
      rows.push({
        ts: e.timestamp,
        kind: 'agent',
        type: e.event_type,
        slug: e.agent_slug,
        detail: e.detail || e.trigger_condition || '',
      });
    }
    for (const o of (simOrders || [])) {
      rows.push({
        ts: o.created_at,
        kind: 'order',
        side: o.transaction_type,
        symbol: o.symbol,
        qty: o.quantity,
        price: o.fill_price ?? o.initial_price,
        status: o.status,
        detail: o.detail || '',
      });
    }
    rows.sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
    return rows.slice(0, 30);
  });

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

  // ── Iteration-mode state + handlers (Phase 2A) ────────────────────
  // Settings-backed defaults, pre-filled on mount via /api/simulator/defaults.
  // Per-run override values land directly on the /start-run payload —
  // they don't touch the settings table. To change permanent defaults,
  // operator goes to /admin/settings.
  let iterIterations  = $state(1);
  let iterMaxMinutes  = $state(10);
  let iterRegimes     = $state(/** @type {string[]} */ ([]));
  let iterAgents      = $state(/** @type {string[]} */ ([]));   // string ids → coerced on submit
  // Which buckets to seed into the sim. Default = ["positions"]
  // (historical behaviour). Picking "holdings" also seeds the
  // holdings book + surfaces the Holdings summary panel.
  let iterInputs      = $state(/** @type {string[]} */ (['positions']));
  const _INPUT_OPTIONS = [
    { value: 'positions', label: 'Positions' },
    { value: 'holdings',  label: 'Holdings'  },
    { value: 'watchlist', label: 'Watchlist' },
  ];
  // Account scope — empty = all loaded accounts. Pre-populated from
  // /api/simulator/defaults.available_accounts.
  let iterAccounts          = $state(/** @type {string[]} */ ([]));
  let iterAvailableAccounts = $state(/** @type {{value:string,label:string}[]} */ ([]));
  let iterSeed        = $state(/** @type {number | ''} */ (''));
  let iterForceClose  = $state(true);
  let iterAvailableRegimes = $state(/** @type {{value:string,label:string}[]} */ ([]));
  let iterMarketBlocked    = $state(false);
  let iterBlockSetting     = $state(true);
  let iterLoadingDefaults  = $state(true);
  // Cross-underlying correlation table from /defaults. Shape:
  // { "NIFTY": [{to: "BANKNIFTY", beta: 1.30}, ...], ... }
  let iterCorrelationBetas = $state(/** @type {Record<string, Array<{to: string, beta: number}>>} */ ({}));

  async function loadIterDefaults() {
    try {
      const d = await fetchSimDefaults();
      iterIterations       = Number(d.iterations) || 1;
      iterMaxMinutes       = Number(d.max_minutes) || 10;
      iterRegimes          = Array.isArray(d.regimes) ? d.regimes : [];
      iterForceClose       = Boolean(d.force_close_on_timeout);
      iterAvailableRegimes = (d.available_regimes || []).map(
        (r) => ({ value: r.slug, label: r.name || r.slug }));
      iterAvailableAccounts = (d.available_accounts || []).map(
        (a) => ({ value: a, label: a }));
      iterMarketBlocked    = Boolean(d.markets_currently_open) && Boolean(d.block_during_market_hours);
      iterBlockSetting     = Boolean(d.block_during_market_hours);
      iterCorrelationBetas = d.correlation_betas || {};
    } catch (_) {
      /* settings unreachable — leave defaults */
    } finally {
      iterLoadingDefaults = false;
    }
  }

  // Compact correlation summary for the chip — "NIFTY→BANKNIFTY β=1.30, …"
  const _correlationSummary = $derived.by(() => {
    const lines = [];
    for (const [src, peers] of Object.entries(iterCorrelationBetas || {})) {
      for (const p of (peers || [])) {
        lines.push(`${src}→${p.to} β=${Number(p.beta).toFixed(2)}`);
      }
    }
    return lines;
  });

  async function doStartRun() {
    error = ''; note = '';
    try {
      const aids = iterAgents
        .map((s) => Number(s))
        .filter((n) => Number.isFinite(n) && n > 0);
      const payload = {
        iterations:             Math.max(1, Number(iterIterations) || 1),
        max_minutes:            Math.max(1, Number(iterMaxMinutes) || 10),
        regimes:                iterRegimes,
        agent_ids:              aids.length ? aids : null,
        seed:                   iterSeed === '' ? null : Number(iterSeed),
        force_close_on_timeout: Boolean(iterForceClose),
        seed_mode:              seedMode,
        rate_ms:                Number(rateMs) || null,
        spread_pct:             spreadPct === '' ? null : Number(spreadPct),
        inputs:                 iterInputs.length ? iterInputs : ['positions'],
        accounts:               iterAccounts.length ? iterAccounts : null,
      };
      if (!payload.regimes?.length) {
        error = 'Pick at least one regime.';
        return;
      }
      status = await startSimRun(payload);
      note = `Run started · ${payload.iterations} iteration${payload.iterations === 1 ? '' : 's'} · max ${payload.max_minutes} min each · regimes=[${payload.regimes.join(',')}]`;
      // Refresh status polling so the new run_active state surfaces.
      loadHot();
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
    loadIterDefaults();
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
      <span title={status.started_at ? logTime(status.started_at) : ''}>
        started:
        {#if status.started_at}{@html dualTsHtml(status.started_at)}{:else}—{/if}
      </span>
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

  <!-- Indices snapshot — always rendered. Compact name·spot·Δ%
       pill row. Falls back to a placeholder when no underlyings are
       seeded yet so the operator sees the structure even pre-run. -->
  <div class="sim-section-label">Indices</div>
  {#if indicesSnapshot.length}
    <div class="sim-indices-row">
      {#each indicesSnapshot as ix (ix.name)}
        <span class="sim-index-pill" class:up={ix.pct >= 0} class:down={ix.pct < 0}>
          <span class="sim-index-name">{ix.name}</span>
          <span class="sim-index-spot">{priceFmt(ix.spot)}</span>
          <span class="sim-index-pct">{ix.pct >= 0 ? '+' : ''}{ix.pct.toFixed(2)}%</span>
        </span>
      {/each}
    </div>
  {:else}
    <div class="sim-empty">No underlyings seeded. Start a sim to populate.</div>
  {/if}

  <!-- Live activity — last 30 agent fires + sim orders, newest first.
       Pattern borrowed from QuantConnect Live Log / TradingView List
       of Trades / NinjaTrader Strategy Analyzer: a prominent stream
       adjacent to the chart so signal + order timing is glanceable.
       Persists across sim runs so the operator can always refer back
       to recent activity even when no sim is currently running. -->
  <div class="sim-section-label">Live activity</div>
  {#if activityFeed.length}
    <div class="sim-activity">
      {#each activityFeed as row, i (row.kind + i + row.ts)}
        <div class="sim-activity-row sim-activity-{row.kind}">
          <span class="sim-activity-ts">{@html dualTsHtml(row.ts)}</span>
          {#if row.kind === 'agent'}
            <span class="sim-activity-chip sim-activity-chip-agent">AGENT</span>
            <span class="sim-activity-slug">{row.slug || ''}</span>
            <span class="sim-activity-detail">{row.type || ''} · {row.detail}</span>
          {:else}
            <span class="sim-activity-chip sim-activity-chip-order">ORDER</span>
            <span class="sim-activity-slug">{row.side} {qtyFmt(row.qty)} {row.symbol}</span>
            <span class="sim-activity-detail">@₹{priceFmt(row.price)} · {row.status} {row.detail ? '· ' + row.detail : ''}</span>
          {/if}
        </div>
      {/each}
    </div>
  {:else}
    <div class="sim-empty">No agent fires or orders yet. Start a sim to populate.</div>
  {/if}

  <!-- Per-underlying charts — always section-labelled. -->
  <div class="sim-section-label">Underlyings</div>
  {#if underlyingNames.length}
    <div class="sim-charts">
      {#each underlyingNames as name (name)}
        <PriceChart mode="sim" symbol={name} height={180}
                    data={chartsBySymbol[name]}
                    {chartsBySymbol} />
      {/each}
    </div>
  {:else}
    <div class="sim-empty">No underlying charts yet. Charts populate when sim positions/holdings have parseable F&amp;O symbols.</div>
  {/if}

  <!-- Positions summary — always rendered with placeholder. -->
  <div class="sim-section-label">Positions summary</div>
  {#if summaryPositions.length}
    <table class="sim-summary-grid">
      <thead><tr><th>Account</th><th>Cur Val</th><th>P&amp;L</th><th>Day P&amp;L</th></tr></thead>
      <tbody>
        {#each summaryPositions as row (row.account)}
          <tr class:sim-summary-total={row.account === 'TOTAL'}>
            <td>{row.account}</td>
            <td class="sim-num">{priceFmt(row.cur_val)}</td>
            <td class="sim-num" class:up={row.pnl > 0} class:down={row.pnl < 0}>{priceFmt(row.pnl)}</td>
            <td class="sim-num" class:up={row.day_pnl > 0} class:down={row.day_pnl < 0}>{priceFmt(row.day_pnl)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <div class="sim-empty">No positions seeded.</div>
  {/if}

  <!-- Holdings summary — same shape, conditional on the operator
       selecting `holdings` in the sim Inputs multi-select. Backend
       returns [] when holdings isn't in inputs; we hide the section
       in that case so dev sessions that only seed positions don't see
       an empty holdings grid. -->
  {#if summaryHoldings.length}
    <div class="sim-section-label">Holdings summary</div>
    <table class="sim-summary-grid">
      <thead><tr><th>Account</th><th>Cur Val</th><th>P&amp;L</th><th>Day P&amp;L</th></tr></thead>
      <tbody>
        {#each summaryHoldings as row (row.account)}
          <tr class:sim-summary-total={row.account === 'TOTAL'}>
            <td>{row.account}</td>
            <td class="sim-num">{priceFmt(row.cur_val)}</td>
            <td class="sim-num" class:up={row.pnl > 0} class:down={row.pnl < 0}>{priceFmt(row.pnl)}</td>
            <td class="sim-num" class:up={row.day_pnl > 0} class:down={row.day_pnl < 0}>{priceFmt(row.day_pnl)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  <!-- Past simulations — last 5 iteration rows, persisted across page
       reloads. Inline Re-run button kicks off a new single-iteration
       sim with the same regime + seed + agent_ids (deterministic).
       Slug is clickable for the detail page. -->
  <div class="sim-section-label">Past simulations</div>
  {#if pastIterations.length}
    <table class="sim-summary-grid sim-past-grid">
      <thead><tr><th>Slug</th><th>Regime</th><th>Started</th><th>End</th><th class="sim-num">Fees</th><th class="sim-num">Net P&amp;L</th><th></th></tr></thead>
      <tbody>
        {#each pastIterations as it (it.id)}
          <tr class="sim-past-row">
            <td class="sim-past-slug">
              <a href={`/admin/simulator/iterations/${it.slug}`}>{it.slug}</a>
            </td>
            <td>{it.regime}</td>
            <td>{@html dualTsHtml(it.started_at)}</td>
            <td class="sim-past-end sim-past-end-{it.end_reason || 'pending'}">{it.end_reason ?? 'pending'}</td>
            <td class="sim-num">{it.summary?.total_fees != null ? priceFmt(it.summary.total_fees) : '—'}</td>
            <td class="sim-num">{it.summary?.net_pnl_remaining != null ? priceFmt(it.summary.net_pnl_remaining) : '—'}</td>
            <td class="sim-past-action">
              <button class="sim-past-rerun"
                      disabled={!it.ended_at || status?.active || status?.run_active}
                      title="Re-run this iteration with the same regime + seed + agent_ids. Deterministic — same fills."
                      onclick={async () => {
                        if (!confirm(`Re-run ${it.slug} with seed ${it.seed ?? '(random)'}?`)) return;
                        try {
                          await replaySimIteration(it.slug);
                          note = `Re-running ${it.slug}…`;
                          await loadHot();
                        } catch (e) { error = e?.message || 'Re-run failed'; }
                      }}>▷ Re-run</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <a class="sim-past-all" href="/admin/simulator/iterations">All past iterations →</a>
  {:else}
    <div class="sim-empty">No past simulations yet.</div>
  {/if}
</div>

<!-- Iteration-mode card (Phase 2A) -->
<div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
  <div class="iter-header">
    <span class="iter-title">Iteration mode</span>
    <InfoHint popup text="Runs N iterations sequentially, round-robining through the picked regimes. Each iteration writes a SimIteration row that you can replay later with the same seed. Defaults pre-filled from /admin/settings." />
    {#if _correlationSummary.length > 0}
      <span class="iter-corr-chip" title={'Cross-underlying correlation propagation: when a scenario moves NIFTY, BANKNIFTY drags at β=1.30, FINNIFTY at β=1.10, etc. Single-hop only.'}>
        <span class="iter-corr-label">Correlation:</span>
        <span class="iter-corr-pairs">{_correlationSummary.join(' · ')}</span>
        <InfoHint popup label="?" text={`<strong>Cross-underlying correlation table</strong><br/><br/>${_correlationSummary.map(l => l + '<br/>').join('')}<br/>When an <code>underlying_pct</code> scenario fires on a source, peers move at <code>β × primary_delta</code>. Propagation is capped at one hop so the chain doesn't recurse.`} />
      </span>
    {/if}
    <a class="iter-history-link" href="/admin/simulator/iterations">Past iterations →</a>
  </div>

  {#if iterMarketBlocked}
    <div class="iter-banner-warn">
      Markets are currently open. Simulation is blocked.
      Set <code>simulator.block_during_market_hours = false</code> on
      <a href="/admin/settings">/admin/settings</a> to override.
    </div>
  {/if}

  <div class="iter-form">
    <div class="iter-field">
      <label class="field-label" for="iter-iterations">Iterations</label>
      <input id="iter-iterations" type="number" min="1" max="100"
             class="field-input sim-pct-input"
             bind:value={iterIterations} />
    </div>
    <div class="iter-field">
      <label class="field-label" for="iter-max-min">Max min / iter</label>
      <input id="iter-max-min" type="number" min="1" max="240"
             class="field-input sim-pct-input"
             bind:value={iterMaxMinutes} />
    </div>
    <div class="iter-field iter-field-wide">
      <label class="field-label" for="iter-regimes" title="Round-robin across iterations">Regimes</label>
      <MultiSelect id="iter-regimes" bind:value={iterRegimes}
        options={iterAvailableRegimes}
        placeholder="Pick at least one" />
    </div>
    <div class="iter-field iter-field-wide">
      <label class="field-label" for="iter-inputs" title="Which buckets to seed into the sim. positions = today's default (F&O book). holdings = also seed long-term equity for holdings-summary + holdings-gating agents. watchlist = re-quote symbols with no open position.">Inputs</label>
      <MultiSelect id="iter-inputs" bind:value={iterInputs}
        options={_INPUT_OPTIONS}
        placeholder="positions" />
    </div>
    <div class="iter-field iter-field-wide">
      <label class="field-label" for="iter-accounts" title="Broker accounts to scope this run to. Leave empty to run across every loaded account (default). Pick one or more to seed the sim with only those accounts' positions/holdings/margins.">Accounts</label>
      <MultiSelect id="iter-accounts" bind:value={iterAccounts}
        options={iterAvailableAccounts}
        placeholder="(all loaded)" />
    </div>
    <div class="iter-field iter-field-wide">
      <label class="field-label" for="iter-agents" title="Empty list = no agents (market explorer). Leave empty AND deselect everything to pass null → run all active agents.">Agents</label>
      <MultiSelect id="iter-agents" bind:value={iterAgents}
        options={agents.map((a) => ({ value: String(a.id), label: `${a.slug} (${a.id})` }))}
        placeholder="(all active)" />
    </div>
    <div class="iter-field">
      <label class="field-label" for="iter-seed" title="seed + N for iteration N (replayable). Blank → random per iteration.">Seed</label>
      <input id="iter-seed" type="number" placeholder="(random)"
             class="field-input sim-pct-input"
             bind:value={iterSeed} />
    </div>
    <div class="iter-field iter-field-toggle">
      <label class="field-label" title="On time_limit with positions remaining, write synthetic close orders at last LTP.">
        <input type="checkbox" bind:checked={iterForceClose} />
        Force-close on timeout
      </label>
    </div>
    <div class="iter-actions">
      <button class="sim-btn sim-btn-go"
              disabled={iterLoadingDefaults || iterMarketBlocked || !iterRegimes.length || status.run_active}
              onclick={doStartRun}>
        {status.run_active ? 'Run in progress…' : 'Start run'}
      </button>
      {#if status.run_active}
        <button class="sim-btn sim-btn-stop" onclick={doStop}>Stop run</button>
      {/if}
    </div>
  </div>
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
      disabled={simOff || status.active || iterMarketBlocked}
      title={iterMarketBlocked
        ? 'Markets are currently open — sim is blocked. Override via /admin/settings (simulator.block_during_market_hours).'
        : ''}
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
  mode="sim"
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
    margin-top: 0.4rem;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
  }
  /* Section label between chart / summary blocks. Same amber as
     MarketPulse's mp-section-label so the sim panel feels consistent
     with /dashboard's existing summary headings. */
  .sim-section-label {
    margin-top: 0.85rem;
    margin-bottom: 0.3rem;
    font-size: 0.6rem;
    font-family: ui-monospace, monospace;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #fbbf24;
  }
  /* Indices snapshot row — compact per-index pill (name · spot · Δ%). */
  .sim-indices-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-bottom: 0.3rem;
  }
  .sim-index-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.5rem;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    border-radius: 4px;
    background: rgba(125, 211, 252, 0.08);
    border: 1px solid rgba(125, 211, 252, 0.25);
    color: #c8d8f0;
  }
  .sim-index-pill.up   { border-color: rgba(74, 222, 128, 0.45); }
  .sim-index-pill.down { border-color: rgba(248, 113, 113, 0.45); }
  .sim-index-name { color: #fbbf24; font-weight: 700; letter-spacing: 0.04em; }
  .sim-index-spot { font-variant-numeric: tabular-nums; color: #fde68a; }
  .sim-index-pct  {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    color: #c8d8f0;
  }
  .sim-index-pill.up   .sim-index-pct { color: #4ade80; }
  .sim-index-pill.down .sim-index-pct { color: #f87171; }
  /* Summary grids — small inline ag-Grid-style table; matches the
     /dashboard cream-on-navy summary panels without dragging in a
     full ag-Grid instance. */
  .sim-summary-grid {
    width: 100%;
    border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    color: #c8d8f0;
  }
  .sim-summary-grid th {
    text-align: left;
    color: #7e97b8;
    font-weight: 600;
    padding: 0.3rem 0.55rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-size: 0.54rem;
  }
  .sim-summary-grid td {
    padding: 0.3rem 0.55rem;
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }
  .sim-summary-grid .sim-num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .sim-summary-grid .sim-num.up   { color: #4ade80; }
  .sim-summary-grid .sim-num.down { color: #f87171; }
  .sim-summary-total td {
    font-weight: 700;
    color: #fde68a;
    border-top: 1px solid rgba(251,191,36,0.25);
  }
  /* Empty placeholders so every section header has SOMETHING under it
     even when there's no live data — operator sees structure, not a
     missing panel. */
  .sim-empty {
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    color: #7e97b8;
    font-style: italic;
    padding: 0.4rem 0.65rem;
    background: rgba(126, 151, 184, 0.05);
    border: 1px dashed rgba(126, 151, 184, 0.25);
    border-radius: 3px;
  }
  /* Live activity feed — agent fires + sim orders interleaved newest-
     first. Industry pattern (QuantConnect Live Log, TradingView
     List of Trades) — visible adjacent to the chart, not buried in
     a tab. Persists across sim runs. */
  .sim-activity {
    max-height: 14rem;
    overflow-y: auto;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    background: rgba(13, 21, 38, 0.4);
    border: 1px solid rgba(251, 191, 36, 0.15);
    border-radius: 4px;
    padding: 0.3rem 0.5rem;
  }
  .sim-activity-row {
    display: grid;
    grid-template-columns: auto auto auto 1fr;
    gap: 0.5rem;
    align-items: baseline;
    padding: 0.2rem 0;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .sim-activity-row:last-child { border-bottom: none; }
  .sim-activity-ts {
    color: #7e97b8;
    font-variant-numeric: tabular-nums;
    font-size: 0.55rem;
    white-space: nowrap;
  }
  .sim-activity-chip {
    display: inline-block;
    padding: 0 0.3rem;
    border-radius: 2px;
    font-size: 0.5rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    text-align: center;
    min-width: 3rem;
  }
  .sim-activity-chip-agent {
    color: #e879f9;
    background: rgba(232, 121, 249, 0.10);
    border: 1px solid rgba(232, 121, 249, 0.35);
  }
  .sim-activity-chip-order {
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.35);
  }
  .sim-activity-slug {
    color: #fde68a;
    font-weight: 700;
    white-space: nowrap;
  }
  .sim-activity-detail {
    color: #c8d8f0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* Past simulations table — same visual lineage as sim-summary-grid
     but rows are clickable to drill into /admin/simulator/iterations/<slug>. */
  .sim-past-grid {
    margin-top: 0.3rem;
  }
  .sim-past-row {
    transition: background 0.08s;
  }
  .sim-past-row:hover {
    background: rgba(251, 191, 36, 0.06);
  }
  .sim-past-slug a {
    color: #fbbf24;
    font-weight: 700;
    text-decoration: none;
  }
  .sim-past-slug a:hover { text-decoration: underline; }
  /* Inline Re-run action button — kicks off a deterministic re-run of
     the iteration without leaving the workspace. Disabled when a sim
     is already active. */
  .sim-past-action { text-align: right; }
  .sim-past-rerun {
    appearance: none;
    background: rgba(74, 222, 128, 0.10);
    border: 1px solid rgba(74, 222, 128, 0.40);
    color: #4ade80;
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0.2rem 0.55rem;
    border-radius: 3px;
    cursor: pointer;
    text-transform: uppercase;
  }
  .sim-past-rerun:hover:not(:disabled) {
    background: rgba(74, 222, 128, 0.18);
    border-color: rgba(74, 222, 128, 0.70);
  }
  .sim-past-rerun:disabled { opacity: 0.35; cursor: not-allowed; }
  .sim-past-end {
    text-transform: uppercase;
    font-size: 0.54rem;
    letter-spacing: 0.05em;
    font-weight: 700;
  }
  .sim-past-end-book_empty,
  .sim-past-end-scenario_complete { color: #4ade80; }
  .sim-past-end-time_limit,
  .sim-past-end-stopped           { color: #fbbf24; }
  .sim-past-end-failed            { color: #f87171; }
  .sim-past-end-pending           { color: #7dd3fc; }
  .sim-past-all {
    display: inline-block;
    margin-top: 0.35rem;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7dd3fc;
    text-decoration: none;
  }
  .sim-past-all:hover { color: #fbbf24; }
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

  /* ── Iteration-mode form (Phase 2A) ────────────────────────────── */
  .iter-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.45rem;
  }
  .iter-title {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .iter-history-link {
    margin-left: auto;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7dd3fc;
    text-decoration: none;
  }
  .iter-history-link:hover { color: #fbbf24; }
  /* Cross-underlying correlation chip — read-only badge that surfaces
     the default beta table so operators see what propagation fires
     when a scenario moves NIFTY/BANKNIFTY/FINNIFTY. Click the (?)
     for the full table in an InfoHint popup. */
  .iter-corr-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    color: #c8d8f0;
    background: rgba(124, 58, 237, 0.10);
    border: 1px solid rgba(124, 58, 237, 0.35);
    padding: 0.15rem 0.45rem;
    border-radius: 9999px;
    margin-left: 0.5rem;
    white-space: nowrap;
    overflow: hidden;
    max-width: 36rem;
  }
  .iter-corr-label {
    color: #c4b5fd;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .iter-corr-pairs {
    color: #c8d8f0;
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .iter-banner-warn {
    margin-bottom: 0.5rem;
    padding: 0.4rem 0.6rem;
    background: rgba(248,113,113,0.10);
    border: 1px solid rgba(248,113,113,0.35);
    color: #f87171;
    border-radius: 3px;
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
  }
  .iter-banner-warn code { color: #fde68a; background: rgba(251,191,36,0.10); padding: 0 0.2rem; border-radius: 2px; }
  .iter-banner-warn a { color: #7dd3fc; text-decoration: underline; }
  .iter-form {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(8rem, 1fr));
    gap: 0.5rem 0.6rem;
    align-items: end;
  }
  .iter-field-wide { grid-column: span 2; min-width: 0; }
  .iter-field-toggle {
    display: flex;
    align-items: center;
    padding-bottom: 0.4rem;
  }
  .iter-field-toggle .field-label {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    cursor: pointer;
    font-size: 0.6rem;
  }
  .iter-field-toggle input[type="checkbox"] {
    accent-color: #fbbf24;
    cursor: pointer;
  }
  .iter-actions {
    grid-column: 1 / -1;
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
</style>
