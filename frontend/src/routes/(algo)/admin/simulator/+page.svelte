<script>
  // Market Simulator control plane (/admin/simulator).
  // Pairs with backend/api/routes/simulator.py and backend/api/algo/sim/driver.py.
  // Gated by cap_in_<branch>.simulator in backend_config.yaml. Default: dev
  // on, prod off. The server returns 400 when the flag is off and this page
  // surfaces the error inline.

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import { clientTimestamp, visibleInterval, branchLabel } from '$lib/stores';
  import {
    fetchSimScenarios, fetchSimStatus, startSim, stopSim, stepSim,
    runSimCycle, clearSimArtefacts, seedSimLive, fetchSimEvents,
    fetchSimTicks, fetchAgents, fetchAlgoOrdersRecent,
    fetchChartSymbols, fetchChartBatch, fetchAdminLogs,
  } from '$lib/api';
  import LogPanel    from '$lib/LogPanel.svelte';
  import Select      from '$lib/Select.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import PriceChart  from '$lib/PriceChart.svelte';
  import InfoHint    from '$lib/InfoHint.svelte';

  let scenarios = $state(/** @type {any[]} */ ([]));
  let status    = $state(/** @type {any} */ ({}));
  let events    = $state(/** @type {any[]} */ ([]));
  let agents    = $state(/** @type {any[]} */ ([]));
  let error     = $state('');
  let note      = $state('');
  let pickedSlug = $state('');
  let seedMode  = $state(/** @type {'scripted'|'live'|'live+scenario'} */ ('scripted'));
  let rateMs    = $state(2000);
  // Positions cadence override — blank means "use scenario / DB default".
  let positionsEveryN = $state(/** @type {number | ''} */ (''));
  // Simulated market state — overrides the scenario's YAML preset. Blank
  // = use whatever the scenario declares (defaulting to mid_session).
  let marketStatePreset = $state(/** @type {''|'pre_open'|'at_open'|'mid_session'|'pre_close'|'at_close'|'post_close'|'expiry_day'} */(''));
  // Editable per-tick pct overrides. Populated when the operator picks
  // a scenario that has pct-shaped ticks (crash / euphoria / extreme /
  // wild-swings). Values are shown to the operator as % (5 not 0.05).
  let pctOverrides = $state(/** @type {Array<number | ''>} */([]));
  // Tradingsymbols to restrict the sim to. Empty array = all positions.
  // Options are sourced from live snapshot, current sim state, and the
  // scenario's scripted initial so the list is never stale. Multi-select
  // so the operator can tag several symbols at once.
  let symbolFilter = $state(/** @type {string[]} */([]));
  // Bid/ask spread in percent (0.10 = 10 bps). Drives side-aware limit
  // prices in the sim's paper-trade chase engine.
  let spreadPct    = $state(/** @type {number | ''} */(0.10));
  // Custom positions — operator-typed rows that get layered on top of
  // the seeded book at Start time. Lets you stress-test a synthetic F&O
  // book (NIFTY24500CE x −50 + NIFTY24500PE x +50, etc.) without touching
  // your real positions or editing scenarios.yaml. Empty rows are dropped
  // server-side via _normalise_custom_positions.
  let customRows = $state(/** @type {Array<{tradingsymbol:string, quantity:string|number, last_price:string|number, account:string}>} */ ([]));
  function addCustomRow() {
    customRows = [...customRows, { tradingsymbol: '', quantity: '', last_price: '', account: '' }];
  }
  function removeCustomRow(/** @type {number} */ i) {
    customRows = customRows.filter((_, idx) => idx !== i);
  }
  // Pre-armed agent id (from `?agent_id=<id>` when the user clicked "Run in
  // Simulator" on the /algo page). Empty string = run all agents.
  let agentId   = $state('');
  let liveSnap  = $state(/** @type {any} */ (null));
  // Symbols with captured price-history ticks. Independent from
  // status.symbols (which is the live position list) so charts persist
  // after a fill removes the position from the book — the operator still
  // wants to see how the price moved leading up to the close.
  let chartSymbols = $state(/** @type {string[]} */ ([]));
  // Batched chart payload — `chartsBySymbol[sym]` is the full
  // `{ticks, events, kind, underlying}` blob the API returns. Distributed
  // to each PriceChart via the `data` prop so 10 charts cost 1 round-trip
  // (instead of 10 + 10 for the underlying overlays).
  let chartsBySymbol = $state(/** @type {Record<string, any>} */ ({}));
  let refreshTeardown;

  // ── Log panel feeds ──────────────────────────────────────────────────
  // Mirror the feeds shown on /agents, but every list is SIMULATOR-scoped
  // on this page: the Agent tab shows sim_mode=True agent events, the
  // Order tab is derived from the same sim events, and the Simulator tab
  // streams live tick price changes from the driver's rolling buffer.
  let simLog    = $state(/** @type {any[]} */ ([]));
  let systemLog = $state(/** @type {string[]} */ ([]));
  let logTab    = $state('simulator');
  // orderLog = raw agent events (Terminal tab fallback).
  // orderRows = structured AlgoOrder rows (both sim + live) — what the
  // Order tab renders with full side / qty / symbol / price / account.
  const orderLog  = $derived(events);
  let   orderRows = $state(/** @type {any[]} */ ([]));

  async function loadOrderRows() {
    try { orderRows = await fetchAlgoOrdersRecent(100, 'all') || []; }
    catch (_) { /* ignore */ }
  }

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
    if (logTab === 'simulator') loadSimLog();
    else if (logTab === 'system') loadSystemLog();
    else if (logTab === 'order')  loadOrderRows();
    // agent tab piggybacks on `events` (refreshed by loadAll every 3s)
  }

  // Hot-path (every 3s): status + events. These actually change per tick.
  // Scenarios + agents are near-static — fetched once on mount and only
  // refreshed on explicit Reload (scenarios) or after an Agent ON/OFF
  // toggle from /agents (out of scope here). Cutting them from the hot
  // loop halves the request count per refresh.
  async function loadHot() {
    try {
      const [stat, ev, chartSyms] = await Promise.all([
        fetchSimStatus(), fetchSimEvents(100),
        fetchChartSymbols('sim').catch(() => ({ symbols: [] })),
      ]);
      status = stat;
      events = ev;
      chartSymbols = chartSyms?.symbols || [];
      // Coalesce per-symbol fetches into one /charts/batch call. With 10
      // open positions + 1 underlying that would otherwise be 11 polls
      // every 3 s; this cuts it to 1.
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

  // Kept as a single entry point — doSeedLive / doClear / doRunCycle / doStart
  // all call loadAll() to immediately reflect the change in the UI without
  // waiting for the next poll tick.
  async function loadAll() {
    await Promise.all([loadHot(), loadStatic()]);
  }

  async function doStart() {
    error = ''; note = '';
    try {
      const opts = { seed_mode: seedMode };
      if (agentId) opts.agent_ids = [Number(agentId)];
      // Blank input = use scenario / DB default; a number = override.
      if (positionsEveryN !== '' && positionsEveryN != null) opts.positions_every_n_ticks = Number(positionsEveryN);
      if (marketStatePreset) opts.market_state_preset = marketStatePreset;
      // Per-tick pct overrides — send as decimal fractions (0.05 for 5%).
      // Null slots = leave scenario default. Only send the array if the
      // operator has actually touched at least one value.
      if (pctOverrides.some(v => v !== '' && v != null)) {
        opts.pct_overrides = pctOverrides.map(v =>
          v === '' || v == null ? null : Number(v) / 100);
      }
      if (symbolFilter && symbolFilter.length) opts.symbols = [...symbolFilter];
      if (spreadPct !== '' && spreadPct != null) opts.spread_pct = Number(spreadPct);
      // Custom positions — drop blank rows client-side so an empty form
      // doesn't ship null fields the backend would have to filter anyway.
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

  // Pick the display name for the armed agent (if any) so the banner is
  // meaningful instead of just a number.
  const armedAgent = $derived.by(() => {
    if (!agentId) return null;
    return agents.find(a => String(a.id) === String(agentId)) || null;
  });

  // Reactive helpers used by the Scenario / Symbol / Tick-% row.
  const pickedScenario = $derived(scenarios.find(s => s.slug === pickedSlug));
  // True when cap_in_<branch>.simulator is off — the form greys out
  // and a banner explains why. Default: on in dev, off in prod.
  const simOff = $derived(status?.enabled === false);
  // Union of every known symbol source so the picker stays fresh whether
  // the operator loaded a live book, started a sim, or just picked a
  // scripted scenario. Deduped and sorted.
  const symbolOptions  = $derived.by(() => {
    /** @type {Set<string>} */
    const pool = new Set();
    for (const s of (liveSnap?.symbols         || [])) if (s) pool.add(s);
    for (const s of (status?.symbols           || [])) if (s) pool.add(s);
    for (const s of (pickedScenario?.initial_symbols || [])) if (s) pool.add(s);
    return [...pool].sort().map(s => ({ value: s, label: s }));
  });

  // Clean-up the scenario name for the dropdown label — the YAML stores
  // long names like "Extreme euphoria (+3% / +6% / +10% positions)"; we
  // keep the brief headline ("Extreme euphoria") in the trigger and push
  // the bracketed detail into the option's hint line.
  /** @param {string} name */
  function shortName(name) {
    return (name || '').replace(/\s*\([^)]*\)\s*/g, '').trim();
  }

  // Whenever the scenario picker changes, refresh the pct-override inputs
  // with the new scenario's YAML defaults. Each tick becomes one editable
  // input showing the default as a % (5 not 0.05). Null entries (ticks
  // whose moves aren't pct-shaped) are rendered as disabled placeholders.
  $effect(() => {
    const picked = scenarios.find(s => s.slug === pickedSlug);
    const pcts = picked?.tick_pcts || [];
    pctOverrides = pcts.map(v => v == null ? '' : Number((v * 100).toFixed(2)));
  });


  onMount(() => {
    // Read agent_id=<id> from URL — lets the /algo "Run in Simulator" button
    // pre-arm this page with a specific agent.
    const q = page.url.searchParams.get('agent_id');
    if (q) agentId = q;
    loadAll();
    loadSimLog();
    loadSystemLog();
    loadOrderRows();
    // Auto-snapshot the live book on page load and switch to Live seed
    // mode. Every shipped scenario runs without an `initial:` block, so
    // Scripted seeding would force the operator to press "Load live
    // book" manually before Start — this sidesteps that: symbols
    // populate in the dropdown, Seed flips to Live, and the "no
    // scripted initial state" warning never fires. Silent-failure if
    // the cap flag is off or the broker call is down.
    (async () => {
      try {
        liveSnap = await seedSimLive();
        if (seedMode === 'scripted') seedMode = 'live';
      } catch (_) { /* ignore — picker will fall back to scenario initial */ }
    })();
    // Hot loop: status + events + algo orders + current-tab log every 3s.
    // Static data (scenarios, agents) fetched once above; not re-polled.
    refreshTeardown = visibleInterval(() => { loadHot(); loadOrderRows(); loadCurrentLog(); }, 3000);
  });
  onDestroy(() => { refreshTeardown?.(); });
</script>

<svelte:head><title>Market Simulator | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">Simulator</h1>
  <InfoHint text={'Feeds fabricated positions into the live agent engine. Every alert, email, and paper-traded order is tagged <span class="font-mono">SIMULATOR</span> so it can\'t be confused with a real fire. Gated by <span class="font-mono">cap_in_&lt;branch&gt;.simulator</span> in <span class="font-mono">backend_config.yaml</span>.'} />
  <span class="algo-ts">{clientTimestamp()}</span>
</div>

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

  <!-- Live sim book — shrinks as chase fills close positions. One pill
       per row, colour-coded by side. Empty when the sim has closed
       every position (or hasn't started). -->
  {#if status?.positions?.length}
    <div class="sim-pills mt-2">
      <span class="sim-pills-label">Positions ({status.positions.length}):</span>
      {#each status.positions as p}
        <span class="sim-pill sim-pill-{p.quantity >= 0 ? 'long' : 'short'}"
              title={`LTP ₹${p.last_price?.toFixed?.(2) ?? '—'} · bid ₹${p.bid?.toFixed?.(2) ?? '—'} · ask ₹${p.ask?.toFixed?.(2) ?? '—'}`}>
          <span class="sim-pill-side">{p.quantity >= 0 ? 'LONG' : 'SHORT'}</span>
          <span class="sim-pill-sym">{p.symbol}</span>
          <span class="sim-pill-qty">{Math.abs(p.quantity ?? 0)}</span>
          <span class="sim-pill-pnl {(p.pnl ?? 0) < 0 ? 'neg' : (p.pnl ?? 0) > 0 ? 'pos' : ''}">
            ₹{(p.pnl ?? 0).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
          </span>
        </span>
      {/each}
    </div>
  {/if}

  <!-- Open chase orders — one pill each showing attempt count + current
       limit price. Lets the operator actually see the chase engine at
       work instead of guessing from the order table's status column. -->
  {#if status?.open_order_details?.length}
    <div class="sim-pills mt-1">
      <span class="sim-pills-label">Chasing ({status.open_order_details.length}):</span>
      {#each status.open_order_details as o}
        <span class="sim-pill sim-pill-chase">
          <span class="sim-pill-side sim-pill-side-{o.side === 'BUY' ? 'buy' : 'sell'}">{o.side}</span>
          <span class="sim-pill-sym">{o.symbol}</span>
          <span class="sim-pill-qty">{o.qty}</span>
          <span class="sim-pill-limit">@₹{o.limit_price?.toFixed?.(2) ?? '—'}</span>
          <span class="sim-pill-attempts">#{o.attempts}</span>
        </span>
      {/each}
    </div>
  {/if}

  <!-- Per-symbol price chart panel — one mini chart per symbol with
       captured ticks. Persists after a fill removes the position so the
       operator can still see the trajectory that led to the close. -->
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

<!-- Controls card — no header label (the fields + buttons speak for themselves) -->
<div class="algo-status-card cmd-surface p-3 mb-3" data-status="inactive">
  <!-- Row 1 — Scenario + Symbol + Tick % overrides together on one row.
       Scenario is given most of the width (long names), Symbol is a
       dropdown sourced from the live snapshot / scenario initial, and
       the per-tick pct inputs sit beside them so the operator can tweak
       magnitude without hunting a separate row. -->
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
  </div>

  <!-- Row 2 — Seed / Rate / Pos / Market — four compact fields in
       the same row. Smaller font + tighter padding so all four fit
       without wrapping on a normal desktop. -->
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
  <!-- Buttons row — all uniform width so the block reads as one action
       bar. Wraps on narrow widths; on mobile each row fits 2-3 buttons.
       Every button is disabled when the simulator capability is off for
       this branch (cap_in_<branch>.simulator=false, e.g. prod default).
       A banner below surfaces the reason. -->
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

<!-- Custom positions panel — operator-typed rows that get layered onto
     whatever scripted/live seeding produced. Useful for stress-testing a
     synthetic NIFTY24500CE without changing the real book. The
     backend's _normalise_custom_positions fills sensible defaults (account
     ZG####, exchange NFO for F&O syms / NSE otherwise, average=last_price)
     so you only have to type symbol / qty / price. -->
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
      positions on top of the seeded book — useful for testing a
      hypothetical strike before you take the trade.
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
      <span class="font-mono">underlying_*</span> move fires (BS pricing
      with calibrated IV); cash equities track simple pct/abs moves.
    </div>
  {/if}
</div>

<!-- The old "Recent SIMULATOR agent events" and "Recent SIMULATOR orders"
     tables used to live here. Both were duplicating data that the
     LogPanel below now surfaces cleanly:
       - Agent tab:   sim_mode=True agent events (with SIM badge)
       - Order tab:   AlgoOrder rows with side/qty/symbol/price + SIM/LIVE mode tag
     Keeping them both would just eat ~60vh of screen and force the
     operator to scan two places. Removed. -->

<style>
  /* Controls layout: Scenario on its own row (long names get full width),
     then Seed/Rate/Pos/Market on one compact row (smaller + tighter).
     Buttons follow below. */
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
    /* column layout so the label sits cleanly above the control, and
       every cell reports the same gap between label and input regardless
       of whether the control is a Select, MultiSelect or one of the
       pct inputs. */
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  /* Normalise the label row on the Scenario row so Spread / Tick
     labels align with the Scenario / Symbol labels — same font-size,
     same baseline, no extra bottom margin fighting the column gap. */
  :global(.sim-scenario-row .field-label) {
    font-size: 0.5rem;
    margin-bottom: 0;
  }
  /* Normalise every control height on the Scenario row. Select trigger,
     MultiSelect trigger, Spread input and every Tick % input all collapse
     to the same 1.7rem box so the row reads as one horizontal strip. */
  :global(.sim-scenario-row .rbq-select-trigger),
  :global(.sim-scenario-row .rbq-multi-trigger),
  :global(.sim-scenario-row input.sim-pct-input) {
    height: 1.7rem !important;
    min-height: 1.7rem !important;
    box-sizing: border-box;
    font-size: 0.62rem !important;
  }
  /* Scenario row sizing. Scenario and Symbol take equal base units;
     Spread : Tick % run at a 1 : 3 ratio because Tick % hosts three
     inline inputs while Spread is a single narrow field. All four
     grow and shrink with available space (flex-grow + flex-shrink);
     min-widths keep each cell legible and trigger a wrap once the
     card gets too narrow. */
  /* Row proportions: Scenario + Symbol together take two-thirds of the
     row; Spread + Tick share the remaining one-third, with Spread at a
     1:3 ratio to Tick. In raw flex-grow units: 4 : 4 : 1 : 3 (total 12,
     so scenario+symbol = 8/12 = 2/3, spread+tick = 4/12 = 1/3). */
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
  /* Each pct cell takes an equal share of its parent container — so the
     three Tick % inputs split the Tick field width evenly, and the single
     Spread input fills its cell completely. `align-items: stretch` so
     the input grows to the cell's full height and matches the Select
     triggers in the same row. */
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
    /* Match .rbq-select-trigger / .rbq-multi-trigger so Scenario,
       Symbol, Spread and Tick inputs line up at identical heights. */
    min-height: 1.55rem !important;
    height: 1.55rem;
    text-align: right;
    box-sizing: border-box;
  }

  /* Compact row = tighter field-input paddings so all 4 fit on one line
     at normal desktop widths. Pin every control on this row to the
     same 1.7rem box the Scenario row uses — Rate / Pos number inputs,
     the Seed and Market Select triggers all align at identical heights. */
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

  /* Base look shared by every .sim-btn — colours, font, border radius. */
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

  /* Inside the Simulator's action strip, every button splits the row
     equally. `flex: 1 1 0` drops the fixed basis so all six grow in
     lock-step; min-width floors them so the labels stay readable;
     max-width is released so they fill the card at any width. */
  :global(.sim-buttons-row .sim-btn) {
    flex: 1 1 0;
    min-width: 90px;
    max-width: none;
  }

  /* sim-btn-primary is the shared "go" look — used by Simulator Start,
     Terminal Run, Orders Submit, and Orders BUY. Light mint green so
     it reads as affirmative without being loud; dark-navy text keeps
     contrast high. */
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
  /* Stop — reverted to the default neutral button style. Distinct from
     the red Clear button so the two can't be confused: Stop is a routine
     halt, Clear is destructive. Matches the slate/tonal aesthetic of the
     other utility buttons. */
  :global(.sim-btn-secondary) {
    background: rgba(148,163,184,0.12);
    color: #e2e8f0;
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
    background: rgba(248,113,113,0.1); color: #fca5a5; border-color: rgba(248,113,113,0.5);
  }
  :global(.sim-btn-danger:hover) {
    background: rgba(248,113,113,0.2); border-color: #f87171;
  }

  /* Sim status pills — one row per position / chase. Compact enough to
     fit a dozen on one line without dominating the status card. */
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
    color: #e2e8f0;
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
  /* Long positions = cyan accent; short = warm orange. Matches the
     position-row styling on the performance / dashboard grids. */
  .sim-pill-long  { border-color: rgba(56,189,248,0.45); }
  .sim-pill-long  .sim-pill-side { background: rgba(56,189,248,0.22); color: #38bdf8; }
  .sim-pill-short { border-color: rgba(251,146,60,0.45); }
  .sim-pill-short .sim-pill-side { background: rgba(251,146,60,0.22); color: #fb923c; }
  /* Chase pills use the Buy (emerald) / Sell (rose) accents that order
     entry uses everywhere else. */
  .sim-pill-chase { border-color: rgba(251,191,36,0.45); background: rgba(251,191,36,0.06); }
  .sim-pill-side-buy  { background: rgba(110,231,183,0.22); color: #6ee7b7; }
  .sim-pill-side-sell { background: rgba(244,63,94,0.22);  color: #fda4af; }
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

  /* Mini-chart grid — two charts per row at typical desktop widths,
     one per row on narrow viewports. */
  .sim-charts {
    margin-top: 0.6rem;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
  }

  /* Custom-positions table — compact monospace grid that mirrors the
     LogPanel order rows (same gutter heights, same field-input theme). */
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

  /* Tighten the inputs to match button height so the row doesn't wobble. */
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

<!-- Shared log panel — same widget as /agents, defaulted to the Simulator
     tab so the first thing an operator sees on this page is the live tick
     price stream. Every tab's feed is SIMULATOR-scoped: agent = sim events,
     order = sim events filtered client-side, simulator = tick diffs,
     system = API log tail. -->
<LogPanel
  heightClass="h-[40vh]"
  initialTab={logTab}
  cmdHistory={[]}
  {orderLog}
  {orderRows}
  agentLog={events}
  {systemLog}
  {simLog}
  onTabChange={(id) => { logTab = id; loadCurrentLog(); }}
/>
