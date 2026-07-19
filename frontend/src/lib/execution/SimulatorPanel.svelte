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
    fetchStrategyAnalytics,
  } from '$lib/api';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import CardHeader    from '$lib/CardHeader.svelte';
  import Select        from '$lib/Select.svelte';
  import MultiSelect   from '$lib/MultiSelect.svelte';
  import PriceChart    from '$lib/PriceChart.svelte';
  import MultiPriceChart from '$lib/MultiPriceChart.svelte';
  import EquityCurve from '$lib/EquityCurve.svelte';
  import ReplayScrubber from '$lib/ReplayScrubber.svelte';
  import InfoHint      from '$lib/InfoHint.svelte';
  import ConfirmModal  from '$lib/ConfirmModal.svelte';
  import StaleBanner   from '$lib/StaleBanner.svelte';
  import OptionsPayoff from '$lib/OptionsPayoff.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import RecordingsPanel from '$lib/execution/RecordingsPanel.svelte';
  import { priceFmt, aggFmt, qtyFmt } from '$lib/format';

  // Card fullscreen / collapse state for chart sections.
  let _underlyingChartsFs  = $state(false);
  let _underlyingChartsCol = $state(false);

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

  /** @type {{ ask: (opts: any) => Promise<boolean> } | null} */
  let _confirmRef = $state(null);
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
  // Recording — when true, the next sim run captures every
  // state-mutating event into a sim_recordings row (flushed on stop).
  // Label is operator-supplied; blank → auto-generated from scenario.
  let recordMode     = $state(false);
  let recordingLabel = $state(/** @type {string} */ (''));

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
  // Collapsible advanced cards — default closed so the page leads
  // with the run controls + single-scenario form. Operators only
  // open Iteration mode for multi-iteration sweeps and Custom
  // positions for synthetic-position layering — both rare paths.
  let _iterCardOpen   = $state(false);
  let _customPosOpen  = $state(false);
  // Replay scrubber — one shared "scrubbedTs" drives all charts on
  // the Lab page. Null = follow live (default); a timestamp string =
  // pin every chart's anchor line to that historical moment.
  /** @type {string | null} */
  let _scrubbedTs = $state(null);
  // Union of all captured timestamps — drives the scrubber range +
  // each chart's anchor lookup. $derived so it auto-updates when
  // chartsBySymbol gains new ticks per poll.
  const _scrubTimestamps = $derived(_buildScrubTimestamps(chartsBySymbol));
  // Per-symbol last-known LTP at scrubbedTs (null when LIVE). Drives
  // the per-card spot marker + the legend rows + the total-P&L
  // banner. All look up symbols in this map; null returns mean "no
  // captured tick yet at or before scrub" and the UI falls back to
  // the live status.positions[].last_price.
  const _scrubbedLtps = $derived(_ltpAtScrub(_scrubbedTs, chartsBySymbol));
  const _scrubMode = $derived(_scrubbedTs != null);

  /**
   * Build the cumulative-P&L curve for the equity-curve chart.
   * At each unique tick timestamp T (union across all legs in this
   * underlying card), compute total P&L = Σ leg qty × (latest_ltp_at_T
   * − avg_price). Late-arriving legs use last-known-LTP carry-forward
   * before their first tick (effectively no contribution).
   *
   * Pure function in scope so the template's {@const _pnlCurve = ...}
   * re-evaluates each render when chartsBySymbol or positions change.
   *
   * @param {Array<any>} positions
   * @param {Record<string, any>} chartsBySymbol
   * @returns {Array<{ts: string, pnl: number}>}
   */
  /**
   * Union of all captured tick timestamps across every symbol in
   * chartsBySymbol. Drives the ReplayScrubber's slider range. Sorted
   * ascending so slider index 0 = oldest tick, max = newest.
   *
   * @returns {string[]}
   */
  function _buildScrubTimestamps(chartsBySymbol) {
    const tsSet = new Set();
    for (const sym of Object.keys(chartsBySymbol || {})) {
      const tk = chartsBySymbol[sym]?.ticks || [];
      for (const t of tk) tsSet.add(t.ts);
    }
    return Array.from(tsSet).sort();
  }

  /**
   * Last-known LTP per symbol at a given scrubbed timestamp. Walks
   * each symbol's tick history and returns the most-recent tick at
   * or before `ts`. Symbols with no ticks ≤ ts fall back to null.
   * Used by the legend rows + spot marker + total-P&L banner so the
   * pills reflect the historical moment, not "now".
   *
   * @param {string | null} ts
   * @param {Record<string, any>} chartsBySymbol
   * @returns {Record<string, number | null>}
   */
  function _ltpAtScrub(ts, chartsBySymbol) {
    /** @type {Record<string, number | null>} */
    const out = {};
    if (!ts) return out;
    for (const sym of Object.keys(chartsBySymbol || {})) {
      const tk = chartsBySymbol[sym]?.ticks || [];
      let last = null;
      for (const t of tk) {
        if (t.ts <= ts) last = Number(t.ltp);
        else break;
      }
      out[sym] = last;
    }
    return out;
  }

  function _buildPnlCurve(positions, chartsBySymbol) {
    const legs = positions.filter((p) =>
      p?.symbol && Number(p?.quantity) !== 0
              && Number(p?.average_price) > 0);
    if (!legs.length) return [];
    // Union of timestamps across all leg histories, sorted.
    const tsSet = new Set();
    for (const p of legs) {
      const tk = chartsBySymbol?.[p.symbol]?.ticks || [];
      for (const t of tk) tsSet.add(t.ts);
    }
    if (!tsSet.size) return [];
    const sortedTs = Array.from(tsSet).sort();
    // Per-leg cursor for last-known LTP. Pre-first-tick = avg_price
    // (zero contribution).
    const cursors = new Map();
    for (const p of legs) {
      cursors.set(p.symbol, { idx: 0, lastLtp: Number(p.average_price) });
    }
    const result = [];
    for (const ts of sortedTs) {
      let pnl = 0;
      for (const p of legs) {
        const tk = chartsBySymbol[p.symbol]?.ticks || [];
        const cur = cursors.get(p.symbol);
        while (cur.idx < tk.length && tk[cur.idx].ts <= ts) {
          cur.lastLtp = Number(tk[cur.idx].ltp);
          cur.idx++;
        }
        pnl += Number(p.quantity) * (cur.lastLtp - Number(p.average_price));
      }
      result.push({ ts, pnl });
    }
    return result;
  }
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

  // Known F&O underlyings the platform actively trades. Sorted
  // longest-first so "BANKNIFTY..." matches before "NIFTY...".
  // (Symbols like BANKNIFTY26JUN... start with NIFTY's substring;
  // the longer-first ordering avoids the miscategorisation.)
  const _KNOWN_UNDERLYINGS = [
    'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYIT', 'NIFTY',
    'CRUDEOIL', 'CRUDEOILM',
    'GOLDPETAL', 'GOLDM', 'GOLD',
    'SILVERMIC', 'SILVERM', 'SILVER',
    'NATURALGAS', 'COPPER', 'ZINC',
  ];
  function _underlyingFromSymbol(/** @type {string} */ sym) {
    if (!sym) return null;
    for (const u of _KNOWN_UNDERLYINGS) if (sym.startsWith(u)) return u;
    return null;
  }

  // Group positions by underlying. Each group feeds one OptionsPayoff
  // card replacing the per-position pill list. Equity positions and
  // non-recognised underlyings are dropped — they don't have a payoff
  // curve concept anyway (handled by the Positions summary table).
  const positionsByUnderlying = $derived.by(() => {
    const out = /** @type {Record<string, any[]>} */ ({});
    for (const p of (status?.positions || [])) {
      if (!p.symbol || !p.quantity) continue;
      const u = _underlyingFromSymbol(p.symbol);
      if (!u) continue;
      if (!out[u]) out[u] = [];
      out[u].push(p);
    }
    return out;
  });

  // Strategy analytics per underlying. Refetched only when the leg
  // SET changes (symbol+qty key), not every status poll — keeps the
  // /options/strategy-analytics endpoint cost bounded.
  let payoffByUnderlying = $state(/** @type {Record<string, any>} */ ({}));
  let _lastLegKey        = $state(/** @type {Record<string, string>} */ ({}));

  $effect(() => {
    const groups = positionsByUnderlying;
    for (const [u, positions] of Object.entries(groups)) {
      const key = positions.map((p) => `${p.symbol}:${p.quantity}`).sort().join('|');
      if (_lastLegKey[u] === key) continue;
      _lastLegKey = { ..._lastLegKey, [u]: key };
      const legs = positions.map((p) => ({
        symbol:   p.symbol,
        qty:      Number(p.quantity) || 0,
        avg_cost: p.average_price ?? p.last_price ?? null,
        ltp:      p.last_price ?? null,
      }));
      fetchStrategyAnalytics(legs)
        .then((s) => {
          payoffByUnderlying = { ...payoffByUnderlying, [u]: { legs: positions, strategy: s } };
        })
        .catch((e) => {
          console.warn(`[sim] strategy-analytics for ${u} failed:`, e?.message || e);
        });
    }
    // Drop entries for underlyings no longer in the book.
    const known = new Set(Object.keys(groups));
    let changed = false;
    const next = { ...payoffByUnderlying };
    for (const k of Object.keys(next)) {
      if (!known.has(k)) { delete next[k]; changed = true; }
    }
    if (changed) payoffByUnderlying = next;
  });

  // Color palette per leg side. Industry pattern (TOS/Tastytrade):
  // longs in cool blues, shorts in warm oranges. Each leg within a
  // side gets a distinct hue cycle position for legend disambiguation.
  const _LONG_HUES  = ['#7dd3fc', '#38bdf8', 'var(--c-info)', '#67e8f9', '#06b6d4'];
  const _SHORT_HUES = ['#fb923c', '#f97316', '#fb7185', 'var(--c-short)', '#facc15'];
  function _legColor(/** @type {any} */ p, /** @type {number} */ idxWithinSide) {
    const palette = (Number(p.quantity) || 0) >= 0 ? _LONG_HUES : _SHORT_HUES;
    return palette[idxWithinSide % palette.length];
  }

  // Auto-sync the run name to the first picked regime UNTIL the
  // operator overtypes it. Once they edit, the name is theirs to
  // own — we stop re-syncing.
  $effect(() => {
    const regimes = iterRegimes;
    if (_runNameTouched) return;
    iterRunName = _autoRunName(regimes);
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
      // Batch-fetch chart histories for BOTH underlying spots AND
      // every leg symbol in the book. One round-trip covers all
      // PriceChart instances on the page (per-card underlying chart
      // + the per-leg charts below each card).
      const underlyingNames = Object.keys(stat?.underlyings || {});
      const legSymbols = [...new Set((stat?.positions || [])
        .map((p) => p.symbol).filter(Boolean))];
      const names = [...new Set([...underlyingNames, ...legSymbols])];
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

  /** Build the opts payload for startSim, closing over current $state. */
  function _buildSimPayload() {
    const opts = /** @type {Record<string,any>} */ ({ seed_mode: seedMode });
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
    // Recording — when the operator ticked Record, capture every
    // state-mutating event into sim_recordings on stop. The default
    // label encodes scenario + timestamp; operator can override.
    if (recordMode) {
      opts.record_mode     = true;
      opts.recording_label = (recordingLabel || '').trim()
                               || `${pickedSlug} @ ${new Date().toISOString().slice(0,16).replace('T', ' ')}`;
    }
    return opts;
  }

  async function doStart() {
    error = ''; note = '';
    try {
      const opts = _buildSimPayload();
      status = await startSim(pickedSlug, rateMs, opts);
      const tag = agentId ? ` (agent #${agentId} only)` : '';
      const cadTag = ` · P:${status.positions_every_n_ticks}`;
      const msTag  = status.market_state_preset ? ` · market=${status.market_state_preset}` : '';
      note = `Started ${pickedSlug} · seed=${seedMode} · ${rateMs}ms${cadTag}${msTag}${tag}`;
      // Kick loadHot immediately so chartsBySymbol populates without
      // waiting for the next adaptive-poll tick (which can be up to
      // 30 s away if the previous status was idle). Without this, the
      // operator clicks Start and sees "Waiting for ticks…" for the
      // full slow-interval window before any chart appears.
      loadHot();
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
  // Run name — auto-generated from the first picked regime + a
  // short HH:MM:SS timestamp. Sent to /start-run as the slug prefix
  // for every iteration in the run. Operator can overtype to set
  // their own label (e.g. "weekend-stress"); we stop auto-syncing
  // the moment they edit it.
  let iterRunName     = $state('');
  let _runNameTouched = $state(false);
  function _autoRunName(/** @type {string[]} */ regimes) {
    const head = (regimes || [])[0] || 'sim';
    const d = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    return `${head}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
  }
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
        run_name:               (iterRunName || '').trim() || null,
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
    // Fixed-cadence polling at 3 s. Earlier I had this adaptive
    // (3 s active / 30 s idle), but the transition was buggy: the
    // setTimeout scheduled BEFORE a Start click was already on the
    // slow 30 s schedule, so the chart timeseries didn't update for
    // up to 30 s after Start. The supposed savings (~38 req/min on
    // idle Lab) only matter when an operator is on Lab but doing
    // nothing — which is rare. Live updates while the sim ticks is
    // the actual common case, so optimize for that.
    refreshTeardown = visibleInterval(
      () => { loadHot(); loadCurrentLog(); }, 3000,
    );
  });
  onDestroy(() => { refreshTeardown?.(); });
</script>

<ConfirmModal bind:this={_confirmRef} />

<StaleBanner {error} hasData={scenarios.length > 0} label="Simulator" />
{#if note}
  <div class="mb-3 p-2 rounded bg-emerald-500/10 text-emerald-300 text-[0.65rem] border border-emerald-500/30">
    {note}
  </div>
{/if}

{#if armedAgent}
  <div class="mb-3 p-2 rounded bg-[var(--c-action)]/15 text-[var(--c-action)] text-[0.65rem] border border-[var(--c-action)]/50">
    Isolated run armed — will dry-fire <b>#{armedAgent?.id} {armedAgent?.name}</b>
    (bypasses schedule / cooldown / baseline gates).
    <button type="button" onclick={() => { agentId = ''; }}
      class="ml-2 text-[0.6rem] underline">Clear</button>
  </div>
{/if}

<!-- Status bar — sticky to the top of the scroll container so the
     RUNNING/idle indicator + tick + scenario chips stay visible while
     the operator scrolls through the controls or activity feed below.
     Top offset = (navbar 2.5rem + algo banner ~1.5rem) so it tucks
     directly under the page chrome on prod and dev alike. -->
<div class="algo-status-card sim-status-sticky p-3 mb-3"
     data-status={status.active ? 'triggered' : 'inactive'}>
  <div class="flex items-center flex-wrap gap-2 text-[0.7rem]">
    <span class="w-2 h-2 rounded-full {status.active ? 'bg-red-500 animate-pulse' : 'bg-slate-500'}"></span>
    <span class="text-[var(--c-action)] font-semibold">{status.active ? 'RUNNING' : 'idle'}</span>
    {#if status.scenario}
      <span class="font-mono text-[#7dd3fc]">scenario: {status.scenario}</span>
      <span class="text-[var(--c-muted)]">|</span>
      <span>seed: {status.seed_mode}</span>
      <span class="text-[var(--c-muted)]">|</span>
      <span>tick {status.tick_index}/{status.total_ticks}</span>
      <span class="text-[var(--c-muted)]">|</span>
      <span>rate: {status.rate_ms}ms</span>
      <span class="text-[var(--c-muted)]">|</span>
      <span title="Positions refresh every N ticks">
        cadence P:{status.positions_every_n_ticks}
      </span>
      <span class="text-[var(--c-muted)]">|</span>
      <span title="Simulated market state — segment flags + minutes-since-open drive time-aware agents">
        market: <span class="text-[#fde68a]">{status.market_state_preset ?? 'mid_session'}</span>
      </span>
      <span class="text-[var(--c-muted)]">|</span>
      <span title={status.started_at ? logTime(status.started_at) : ''}>
        started:
        {#if status.started_at}{@html dualTsHtml(status.started_at)}{:else}—{/if}
      </span>
      {#if status.only_agent_ids?.length}
        <span class="text-[var(--c-muted)]">|</span>
        <span class="text-[var(--c-action)]">agents=[{status.only_agent_ids.join(',')}]</span>
      {/if}
    {/if}
  </div>
  {#if liveSnap}
    <div class="text-[0.6rem] text-[#c8d8f0]/70 mt-1">
      Live snapshot: {liveSnap?.snapshot_at?.slice(11, 19)} ·
      {liveSnap?.positions_count}P / {liveSnap?.margins_count}M
      · accounts=[{(liveSnap?.accounts ?? []).join(', ')}]
    </div>
  {/if}
</div>

<!-- Two-column Lab layout on desktop (≥1100 px): controls on the
     LEFT (iter / Run / custom cards), monitoring on the RIGHT (payoff
     cards, pills, indices, activity feed, summaries, past iter).
     The cards are direct children of .sim-grid; CSS positions
     Single-column flow: controls (aside) at the TOP, monitoring
     cards (.sim-grid-main section) below at full width. Operators
     run a sim by interacting with the top strip then watch the cards
     fill out — there's no value in a side-by-side layout when the
     cards want full width and the controls collapse to a thin strip
     when not actively edited. The earlier 2-col grid left a giant
     empty 30 %-of-viewport column on the left whenever the aside
     was collapsed. -->
<div class="sim-stack">
  <section class="sim-grid-main">
  <!-- Per-underlying payoff cards — replace the position pills with a
       chart-driven view. Each card shows the combined net payoff
       curve + a color-coded legend mapping each leg to a hue used in
       the chart. Pattern: ThinkOrSwim Risk Profile / Tastytrade Trade
       Tab. Equity positions and futures-only books fall through to
       the Positions summary table; only F&O legs get a payoff. -->
  {#each Object.entries(positionsByUnderlying) as [underlying, positions] (underlying)}
    {@const payoff = payoffByUnderlying[underlying]}
    {@const longs  = positions.filter((p) => (Number(p.quantity) || 0) >= 0)}
    {@const shorts = positions.filter((p) => (Number(p.quantity) || 0) < 0)}
    <div class="sim-payoff-card">
      <div class="sim-payoff-header">
        <span class="sim-payoff-name">{underlying}</span>
        <span class="sim-payoff-meta">{positions.length} leg{positions.length === 1 ? '' : 's'}</span>
        {#if _scrubMode && _scrubbedLtps[underlying] != null}
          {@const _scrubSpot = _scrubbedLtps[underlying]}
          <span class="sim-payoff-spot" title="Historical spot at scrubbed timestamp">
            spot ₹{priceFmt(_scrubSpot)} <span class="sim-scrub-tag">SCRUB</span>
          </span>
        {:else if payoff?.strategy?.spot != null}
          <span class="sim-payoff-spot">spot ₹{priceFmt(payoff.strategy.spot)}</span>
        {/if}
        {#if _scrubMode}
          <!-- Total P&L recomputed at scrub timestamp: Σ qty × (scrubbed_ltp − avg_price) -->
          {@const _scrubTotal = positions.reduce((s, p) => {
            const lp = _scrubbedLtps[p.symbol];
            if (lp == null || !p.average_price || !p.quantity) return s;
            return s + Number(p.quantity) * (lp - Number(p.average_price));
          }, 0)}
          <span class="sim-payoff-pnl {_scrubTotal < 0 ? 'neg' : _scrubTotal > 0 ? 'pos' : ''}">
            scrub: ₹{aggFmt(_scrubTotal)}
          </span>
        {:else if payoff?.strategy?.risk?.current_pnl != null}
          {@const pnl = payoff.strategy.risk.current_pnl}
          <span class="sim-payoff-pnl {pnl < 0 ? 'neg' : pnl > 0 ? 'pos' : ''}">
            now: ₹{aggFmt(pnl)}
          </span>
        {/if}
      </div>
      {#if payoff?.strategy?.payoff?.length}
        <OptionsPayoff
          payoff={payoff.strategy.payoff}
          spot={_scrubMode && _scrubbedLtps[underlying] != null
                ? _scrubbedLtps[underlying]
                : payoff.strategy.spot}
          prevClose={payoff.strategy.spot_prev_close}
          breakevens={payoff.strategy.risk?.breakevens}
          spanSigmas={payoff.strategy.span_sigmas}
          spanPct={payoff.strategy.span_pct}
          dte={payoff.strategy.days_to_expiry}
          ivProxy={payoff.strategy.iv_proxy}
          legCount={payoff.strategy.legs?.length ?? positions.length}
          multiExpiry={payoff.strategy.multi_expiry ?? false}
          height={220} />
      {:else}
        <div class="sim-empty">
          {payoff === undefined ? 'Computing payoff…' : 'Payoff unavailable for this leg set'}
        </div>
      {/if}
      <!-- Time-series chart: underlying spot evolution across all
           ticks of the scenario. Always renders the section; an
           empty placeholder shows when no ticks have been captured
           yet so the operator knows where the chart will appear. -->
      <div class="sim-payoff-history-label">Underlying spot · scenario history</div>
      {#if chartsBySymbol[underlying]?.ticks?.length}
        <PriceChart mode="sim" symbol={underlying} height={160}
                    data={chartsBySymbol[underlying]}
                    {chartsBySymbol}
                    scrubbedTs={_scrubbedTs} />
      {:else}
        <div class="sim-empty">Waiting for ticks. Start the sim to populate.</div>
      {/if}

      <!-- Per-leg premiums in ONE multi-line chart. Each leg gets
           its own colored line; the y-axis is the % change from each
           leg's first captured tick, so a ₹50 long-call and a ₹2,000
           short-strangle wing share the same vertical scale and the
           operator can compare trajectories directly. Hover reveals
           per-leg values at the crosshair timestamp. -->
      {#if positions.length}
        <div class="sim-payoff-history-label">Leg premiums · scenario history</div>
        {@const legSeries = [...longs, ...shorts].map((p, idx) => {
          const palIdx = idx < longs.length ? idx : (idx - longs.length);
          return {
            symbol:  p.symbol,
            color:   _legColor(p, palIdx),
            side:    /** @type {'LONG'|'SHORT'} */ ((Number(p.quantity) || 0) >= 0 ? 'LONG' : 'SHORT'),
            account: p.account,
            ticks:   chartsBySymbol[p.symbol]?.ticks || [],
          };
        })}
        <MultiPriceChart series={legSeries} height={220}
                         emptyMsg="No ticks captured yet for any leg."
                         scrubbedTs={_scrubbedTs} />
      {/if}

      <!-- Equity curve — cumulative P&L over time. Industry-standard
           backtest visualisation (AlgoTest, Lean, TradingView). At
           each tick timestamp, sum qty × (latest_ltp - avg_price)
           across every leg in this underlying card. The line crosses
           zero on the operator's reference line so winning / losing
           regions are visually obvious. -->
      {#if positions.length}
        {@const _pnlCurve = _buildPnlCurve(positions, chartsBySymbol)}
        <div class="sim-payoff-history-label">Equity curve · cumulative P&amp;L</div>
        <EquityCurve ticks={_pnlCurve} height={150} title=""
                     scrubbedTs={_scrubbedTs} />
      {/if}


      <div class="sim-payoff-legend">
        {#each longs as p, i (p.symbol + ':' + p.account)}
          <div class="sim-leg-row">
            <span class="sim-leg-swatch" style="background:{_legColor(p, i)}"></span>
            <span class="sim-leg-side sim-leg-side-long">LONG</span>
            <span class="sim-leg-qty">{qtyFmt(Math.abs(p.quantity ?? 0))}×</span>
            <span class="sim-leg-symbol">{formatSymbol(p.symbol)}</span>
            <span class="sim-leg-price">@₹{priceFmt(p.average_price ?? p.last_price)}</span>
            <span class="sim-leg-acct">· {p.account}</span>
          </div>
        {/each}
        {#each shorts as p, i (p.symbol + ':' + p.account)}
          <div class="sim-leg-row">
            <span class="sim-leg-swatch" style="background:{_legColor(p, i)}"></span>
            <span class="sim-leg-side sim-leg-side-short">SHORT</span>
            <span class="sim-leg-qty">{qtyFmt(Math.abs(p.quantity ?? 0))}×</span>
            <span class="sim-leg-symbol">{formatSymbol(p.symbol)}</span>
            <span class="sim-leg-price">@₹{priceFmt(p.average_price ?? p.last_price)}</span>
            <span class="sim-leg-acct">· {p.account}</span>
          </div>
        {/each}
      </div>
    </div>
  {/each}

  <!-- Replay scrubber — one panel-level slider that scrubs across
       every chart on the page simultaneously. Each underlying card's
       PriceChart / MultiPriceChart / EquityCurve renders a vertical
       amber anchor at the same scrubbed timestamp so the operator
       can compare spot · leg premiums · equity at any past moment in
       one visual sweep. LIVE button snaps back to the latest tick. -->
  {#if _scrubTimestamps.length > 1}
    <ReplayScrubber timestamps={_scrubTimestamps}
                    value={_scrubbedTs}
                    title="Replay scrubber"
                    onChange={(ts) => _scrubbedTs = ts} />
  {/if}

  {#if status?.open_order_details?.length}
    <div class="sim-pills mt-1">
      <span class="sim-pills-label">Chasing ({status.open_order_details.length}):</span>
      {#each status.open_order_details as o}
        <span class="sim-pill sim-pill-chase">
          <span class="sim-pill-side sim-pill-side-{o.side === 'BUY' ? 'buy' : 'sell'}">{o.side}</span>
          <span class="sim-pill-sym">{formatSymbol(o.symbol)}</span>
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
  <section class="sim-card">
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
  </section>

  <!-- Live activity — last 30 agent fires + sim orders, newest first.
       Pattern borrowed from QuantConnect Live Log / TradingView List
       of Trades / NinjaTrader Strategy Analyzer: a prominent stream
       adjacent to the chart so signal + order timing is glanceable.
       Persists across sim runs so the operator can always refer back
       to recent activity even when no sim is currently running. -->
  <section class="sim-card">
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
              <span class="sim-activity-slug">{row.side} {qtyFmt(row.qty)} {formatSymbol(row.symbol)}</span>
              <span class="sim-activity-detail">@₹{priceFmt(row.price)} · {row.status} {row.detail ? '· ' + row.detail : ''}</span>
            {/if}
          </div>
        {/each}
      </div>
    {:else}
      <div class="sim-empty">No agent fires or orders yet. Start a sim to populate.</div>
    {/if}
  </section>

  <!-- Per-underlying charts — always section-labelled. -->
  <section class="sim-card" class:fs-card-on={_underlyingChartsFs} class:is-collapsed={_underlyingChartsCol}>
    <CardHeader
      title="Underlying Charts"
      detectOverflow={false}
      bind:isFullscreen={_underlyingChartsFs}
      bind:isCollapsed={_underlyingChartsCol}
      showSearch={false}
    />
    <div hidden={_underlyingChartsCol}>
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
    </div>
  </section>

  <!-- Summary grids — positions + holdings sit side-by-side on wide
       viewports (≥1100 px main column) so the operator can scan both
       at once without scrolling. The two cards collapse to a single
       column below that breakpoint. -->
  <div class="sim-summary-row">
    <section class="sim-card">
      <div class="sim-section-label">Positions summary</div>
      {#if summaryPositions.length}
        <table class="sim-summary-grid">
          <thead><tr><th>Account</th><th>Value</th><th>P&amp;L</th><th>Day P&amp;L</th></tr></thead>
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
    </section>

    <!-- Holdings summary — same shape, conditional on the operator
         selecting `holdings` in the sim Inputs multi-select. Backend
         returns [] when holdings isn't in inputs; we hide the section
         in that case so dev sessions that only seed positions don't see
         an empty holdings grid. -->
    {#if summaryHoldings.length}
      <section class="sim-card">
        <div class="sim-section-label">Holdings summary</div>
        <table class="sim-summary-grid">
          <thead><tr><th>Account</th><th>Value</th><th>P&amp;L</th><th>Day P&amp;L</th></tr></thead>
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
      </section>
    {/if}
  </div>

  <!-- Past simulations — last 5 iteration rows, persisted across page
       reloads. Inline Re-run button kicks off a new single-iteration
       sim with the same regime + seed + agent_ids (deterministic).
       Slug is clickable for the detail page. -->
  <section class="sim-card">
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
                        const ok = await _confirmRef?.ask({
                          title: 'Re-run iteration?',
                          message: `Re-run <b>${it.slug}</b> with seed ${it.seed ?? '(random)'}?`,
                          confirmLabel: 'Re-run',
                        });
                        if (!ok) return;
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
  </section>
  </section><!-- /sim-grid-main -->

<!-- Recordings panel — saved deterministic event logs for replay.
     Inline at the bottom of the Scenario tab so operators see saved
     recordings RIGHT below the past-runs list. Self-polls for fresh
     status; controls live inside the component. -->
<RecordingsPanel />

<!-- Side column wrapper. Holds the three operator-action cards (iter
     / Run controls / custom positions) so the grid has exactly two
     direct children (one per column). Earlier each card was a direct
     grid child with grid-row: 1 / span 100 on the main section — the
     side cards got laid out into separate implicit rows sized by
     their own height, but the section's cell measured only the sum
     of those rows. Tall section content (past iter table) overflowed
     its cell and visually overlapped the side cards on scroll. With
     a single aside wrapper, both columns flow naturally and the grid
     container sizes to the taller column. -->
<aside class="sim-grid-side-col">

<!-- Iteration-mode card (Phase 2A). Collapsed by default — operators
     run a single ad-hoc scenario far more often than a multi-iteration
     sweep, so hide the bigger surface behind a toggle and surface it
     only when the operator opens the card. When a run is in progress
     the summary auto-opens (browsers honour `open` attribute on
     details) so the operator can see progress without expanding by
     hand. -->
<details class="algo-status-card cmd-surface p-3 mb-3 sim-collapsible sim-grid-side"
         data-status={status.run_active ? 'triggered' : 'inactive'}
         bind:open={_iterCardOpen}>
  <summary class="sim-collapsible-summary">
    <span class="sim-collapsible-chevron">{_iterCardOpen ? '▾' : '▸'}</span>
    <span class="iter-title">Iteration mode</span>
    {#if status.run_active}
      <span class="sim-collapsible-running">RUNNING</span>
    {/if}
    <span class="sim-collapsible-hint">{_iterCardOpen ? 'click to hide' : 'click for multi-iteration sweep'}</span>
  </summary>
  <div class="iter-header">
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
    <!-- Run name — auto-generated from the first picked regime, but
         overtype-able. Used as the slug prefix for every iteration
         in this run, so the operator can find the run again later. -->
    <div class="iter-field iter-field-wide">
      <label class="field-label" for="iter-run-name" title="Default auto-generated from regime+timestamp. Overtype to set your own.">Run name</label>
      <input id="iter-run-name" type="text"
             class="field-input"
             placeholder="auto"
             bind:value={iterRunName}
             oninput={() => { _runNameTouched = true; }} />
    </div>
    <div class="iter-field">
      <label class="field-label" for="iter-iterations">Iterations</label>
      <div class="iter-stepper">
        <button type="button" class="iter-stepper-btn"
                onclick={() => iterIterations = Math.max(1, (Number(iterIterations) || 1) - 1)}
                aria-label="Decrement iterations">−</button>
        <span class="iter-stepper-val" id="iter-iterations">{iterIterations}</span>
        <button type="button" class="iter-stepper-btn"
                onclick={() => iterIterations = Math.min(100, (Number(iterIterations) || 1) + 1)}
                aria-label="Increment iterations">+</button>
      </div>
    </div>
    <div class="iter-field">
      <label class="field-label" for="iter-max-min">Max min / iter</label>
      <div class="iter-stepper">
        <button type="button" class="iter-stepper-btn"
                onclick={() => iterMaxMinutes = Math.max(1, (Number(iterMaxMinutes) || 1) - 1)}
                aria-label="Decrement max minutes">−</button>
        <span class="iter-stepper-val" id="iter-max-min">{iterMaxMinutes}</span>
        <button type="button" class="iter-stepper-btn"
                onclick={() => iterMaxMinutes = Math.min(240, (Number(iterMaxMinutes) || 1) + 1)}
                aria-label="Increment max minutes">+</button>
      </div>
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
</details>

<!-- Controls card -->
<div class="algo-status-card cmd-surface p-3 mb-3 sim-grid-side" data-status="inactive">
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
    <!-- Recording — when checked, the run captures every state event
         and persists a sim_recordings row on stop. Label is optional;
         a default ("scenario @ timestamp") is generated when blank.
         The Recordings panel below the past-runs list shows saved
         recordings with play/pause/step controls. -->
    <div class="sim-field sim-field-record">
      <label class="field-label sim-record-label" title="Capture every tick / GTT / chase / agent event during this sim run. On stop, the event log persists as a sim_recordings row that can be replayed later.">
        <input type="checkbox" bind:checked={recordMode} class="sim-record-cb" />
        Record
      </label>
      {#if recordMode}
        <input type="text" class="field-input sim-record-label-input"
               placeholder="recording label (auto if blank)"
               bind:value={recordingLabel} />
      {/if}
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
        <div class="text-[0.6rem] text-amber-400 mt-2">
          Scenario <b>{picked.slug}</b> has no scripted initial state — price
          moves would have nothing to apply to. Press <b>Load live book</b>
          and switch Seed to <b>Live</b> (or <b>Live + scenario</b>), or pick
          a scenario with scripted data.
        </div>
      {/if}
    {/if}
  {/if}
  {#if seedMode !== 'scripted' && !liveSnap}
    <div class="text-[0.6rem] text-amber-400 mt-2">
      Seed mode <b>{seedMode}</b> requires a live-book snapshot — press
      <b>Load live book</b> before Start.
    </div>
  {/if}
</div>

<!-- Custom positions panel. Collapsed by default — only relevant when
     the operator wants to layer synthetic positions on top of the
     seeded book. Auto-expands when at least one custom row exists so
     the operator never loses sight of what they've configured. -->
<details class="algo-status-card cmd-surface p-3 mb-3 sim-collapsible sim-grid-side"
         data-status="inactive"
         bind:open={_customPosOpen}>
  <summary class="sim-collapsible-summary">
    <span class="sim-collapsible-chevron">{_customPosOpen ? '▾' : '▸'}</span>
    <h3 class="algo-card-title m-0">
      Custom positions
      {#if customRows.length}<span class="opacity-60 font-normal ml-1">({customRows.length})</span>{/if}
    </h3>
    <span class="sim-collapsible-hint">{_customPosOpen ? 'click to hide' : 'click to layer synthetic positions on top of the seeded book'}</span>
  </summary>
  <div class="custom-pos-header">
    <button type="button" class="sim-btn sim-btn-order"
            title="Add a synthetic position to layer on top of the seeded book."
            onclick={addCustomRow}>+ Add row</button>
  </div>
  {#if !customRows.length}
    <div class="text-[0.6rem] text-[var(--c-muted)] mt-1">
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
    <div class="text-[0.55rem] text-[var(--c-muted)] mt-1">
      Negative qty = short. F&O symbols re-price coherently when an
      <span class="font-mono">underlying_*</span> move fires.
    </div>
  {/if}
</details>

</aside>  <!-- /.sim-grid-side-col — controls strip (rendered first via CSS order) -->
</div>    <!-- /.sim-stack — single-column wrapper before LogPanel -->

<ActivityLogSurface
  context="card"
  heightClass="h-[40vh]"
  mode="sim"
  defaultTab={logTab}
  simScope={true}
  hideInlineAccountFilter={false}
  onTabChange={(id) => { logTab = id; }}
/>

<style>
  /* Two-column Lab grid — controls on the left, monitoring on the
     right. Mobile / narrow desktops collapse to single column. Each
     column has ONE direct grid child (aside on left, section on
     right) so the columns flow independently and the grid container
     sizes to the taller column. */
  /* ── Single-column Lab layout ─────────────────────────────────────
     Replaces the earlier 2-column .sim-grid that left a ~30 %-of-
     viewport empty column on the left whenever the controls aside
     was collapsed. Now the entire page flows top → bottom:

         1. controls strip (collapsible cards — iteration mode,
            run controls, custom positions)
         2. monitoring cards (Indices · Live activity · Underlyings
            · Positions / Holdings summary · Past simulations)

     Both surfaces use the FULL page width. The aside lives later
     in source order but appears first visually via CSS `order` so
     we don't have to move 350+ lines of template around. */
  :global(.sim-stack) {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }
  :global(.sim-stack > .sim-grid-side-col) { order: 0; }
  :global(.sim-stack > .sim-grid-main)     { order: 1; }
  :global(.sim-grid-side-col) {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    min-width: 0;
  }
  /* Side cards inside the aside wrapper — explicit min-width 0 so
     long content can't push the column wider than its grid track. */
  :global(.sim-grid-side-col > .algo-status-card) {
    margin-bottom: 0 !important;  /* aside gap handles spacing now */
    min-width: 0;
  }

  /* Sticky status strip — the RUNNING/idle + tick + scenario chips
     stay pinned to the top of the scroll container while the operator
     scrolls through controls / activity feed below. Top offset tucks
     under the algo navbar + any sim/paper/replay banner above. */
  :global(.sim-status-sticky) {
    position: sticky;
    top: calc(2.5rem + 1.5rem);
    z-index: 5;
    background: linear-gradient(180deg, #1d2a44 0%, #1a2540 100%);
  }

  /* Collapsible cards (Iteration mode + Custom positions). Default
     collapsed; the summary row carries the title, a chevron, and a
     hint string explaining what expanding does. */
  :global(.sim-collapsible) > summary {
    list-style: none;
    cursor: pointer;
  }
  :global(.sim-collapsible) > summary::-webkit-details-marker {
    display: none;
  }
  :global(.sim-collapsible-summary) {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    font-family: var(--font-numeric);
    padding: 0.1rem 0;
    margin-bottom: 0.4rem;
    user-select: none;
  }
  :global(.sim-collapsible-chevron) {
    color: var(--c-action);
    font-weight: 800;
    font-size: var(--fs-lg);
    line-height: 1;
    width: 0.7rem;
  }
  :global(.sim-collapsible-running) {
    background: rgba(248, 113, 113, 0.15);
    border: 1px solid rgba(248, 113, 113, 0.45);
    color: var(--c-short);
    padding: 0.06rem 0.4rem;
    border-radius: 999px;
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.06em;
  }
  :global(.sim-collapsible-hint) {
    margin-left: auto;
    color: var(--algo-muted);
    font-size: var(--fs-xs);
    letter-spacing: 0.04em;
  }
  /* When collapsed, hide the now-redundant body's first inner row.
     The summary already carries the title. */
  :global(details.sim-collapsible:not([open]) > :not(summary)) {
    display: none;
  }

  .sim-scenario-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 0.35rem 0.4rem;
    margin-bottom: 0.4rem;
    font-size: var(--fs-sm);
  }
  .sim-fields-row {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 0.35rem 0.4rem;
    font-size: var(--fs-sm);
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
    font-size: var(--fs-2xs);
    margin-bottom: 0;
  }
  :global(.sim-scenario-row .rbq-select-trigger),
  :global(.sim-scenario-row .rbq-multi-trigger),
  :global(.sim-scenario-row input.sim-pct-input) {
    height: 1.7rem !important;
    min-height: 1.7rem !important;
    box-sizing: border-box;
    font-size: var(--fs-sm) !important;
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
  /* Recording toggle — single-row checkbox + optional label input.
     Wider than the numeric fields because the label input expects
     a sentence-length value. */
  .sim-field-record {
    flex: 3 1 0;
    min-width: 200px;
    flex-direction: row;
    align-items: center;
    gap: 0.45rem;
  }
  .sim-record-label {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: var(--fs-sm);
    cursor: pointer;
    color: #c084fc;
    font-weight: 700;
  }
  .sim-record-cb {
    margin: 0;
    accent-color: #c084fc;
  }
  .sim-record-label-input {
    flex: 1;
    min-width: 0;
    font-size: var(--fs-sm);
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
    font-size: var(--fs-sm) !important;
    padding: 0.25rem 0.4rem !important;
    min-height: 1.55rem !important;
    height: 1.55rem;
    text-align: right;
    box-sizing: border-box;
  }
  :global(.sim-fields-compact .field-input) {
    font-size: var(--fs-sm) !important;
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
    font-size: var(--fs-sm) !important;
  }
  :global(.sim-fields-compact .field-label) {
    font-size: var(--fs-2xs) !important;
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
    font-size: var(--fs-sm);
    line-height: 1;
    padding: 0.35rem 0.5rem;
    border-radius: 3px;
    font-weight: 600;
    font-family: var(--font-numeric);
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
    color: var(--algo-slate);
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
    background: rgba(74,222,128,0.25); border-color: var(--c-long);
  }
  :global(.sim-btn-step) {
    background: rgba(125,211,252,0.15); color: #7dd3fc; border-color: rgba(125,211,252,0.5);
  }
  :global(.sim-btn-step:hover) {
    background: rgba(125,211,252,0.25); border-color: #7dd3fc;
  }
  :global(.sim-btn-cycle) {
    background: rgba(251,191,36,0.15); color: var(--c-action); border-color: rgba(251,191,36,0.5);
  }
  :global(.sim-btn-cycle:hover) {
    background: rgba(251,191,36,0.25); border-color: var(--c-action);
  }
  :global(.sim-btn-danger) {
    background: var(--c-short-10); color: var(--c-short); border-color: rgba(248,113,113,0.5);
  }
  :global(.sim-btn-danger:hover) {
    background: rgba(248,113,113,0.2); border-color: var(--c-short);
  }
  .sim-pills {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.3rem 0.4rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
  }
  .sim-pills-label {
    color: rgba(200,216,240,0.55);
    font-size: var(--fs-2xs);
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
    color: var(--algo-slate);
    white-space: nowrap;
  }
  .sim-pill-side {
    font-weight: 700;
    font-size: var(--fs-2xs);
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0 0.25rem;
    border-radius: 2px;
  }
  .sim-pill-long  { border-color: rgba(56,189,248,0.45); }
  .sim-pill-short { border-color: rgba(251,146,60,0.45); }
  .sim-pill-chase { border-color: rgba(251,191,36,0.45); background: rgba(251,191,36,0.06); }
  .sim-pill-side-buy  { background: rgba(110,231,183,0.22); color: #6ee7b7; }
  .sim-pill-side-sell { background: var(--c-short-22);  color: #fda4af; }
  .sim-pill-sym { color: #fde68a; font-weight: 600; }
  .sim-pill-qty { color: var(--algo-slate); }
  .sim-pill-limit { color: #7dd3fc; }
  .sim-pill-attempts {
    color: var(--c-action);
    font-weight: 700;
    border-left: 1px solid rgba(251,191,36,0.35);
    padding-left: 0.35rem;
    margin-left: 0.1rem;
  }
  .sim-pill-pnl.neg { color: var(--c-short); }
  /* ── Per-underlying payoff card ────────────────────────────────
     Replaces the per-position pill row. Each card carries one
     OptionsPayoff chart (combined net payoff curve) + a color-
     coded legend of the constituent legs. Industry pattern:
     ThinkOrSwim Risk Profile / Tastytrade Trade Tab. */
  .sim-payoff-card {
    margin: 0.6rem 0;
    padding: 0.55rem 0.65rem 0.65rem;
    background: rgba(13, 21, 38, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 4px;
  }
  .sim-payoff-header {
    display: flex;
    align-items: baseline;
    gap: 0.55rem;
    margin-bottom: 0.35rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }
  .sim-payoff-name {
    color: var(--c-action);
    font-weight: 700;
    letter-spacing: 0.05em;
    font-size: var(--fs-lg);
  }
  .sim-payoff-meta { color: var(--algo-muted); }
  .sim-payoff-spot {
    color: #fde68a;
    font-variant-numeric: tabular-nums;
    margin-left: auto;
  }
  .sim-payoff-pnl {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    padding: 0 0.35rem;
    border-radius: 3px;
  }
  .sim-payoff-pnl.pos { color: var(--c-long); background: var(--algo-green-bg); }
  .sim-payoff-pnl.neg { color: var(--c-short); background: var(--algo-red-bg); }

  /* SCRUB tag next to the spot value — surfaces that the displayed
     spot/PnL are historical at the scrubbed timestamp, not live. */
  .sim-scrub-tag {
    display: inline-block;
    margin-left: 0.25rem;
    padding: 0 0.3rem;
    border-radius: 999px;
    background: rgba(251, 191, 36, 0.16);
    color: var(--c-action);
    font-family: var(--font-numeric);
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    vertical-align: middle;
  }
  /* Section label between the payoff snapshot chart and the
     underlying-spot time-series chart. Same muted-amber style as
     the page-level section labels so the operator's eye reads it
     as a sub-header within the card. */
  .sim-payoff-history-label {
    margin-top: 0.4rem;
    margin-bottom: 0.15rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-2xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-muted);
  }
  /* Per-leg chart stack — single column on mobile, side-by-side
     auto-fit grid on desktop so the leg charts use horizontal space
     instead of stacking into a tall scroll. minmax(420px, ...) keeps
     each chart wide enough to read tick density. */
  .sim-leg-charts {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.45rem;
    margin-top: 0.15rem;
  }
  @media (min-width: 900px) {
    .sim-leg-charts {
      grid-template-columns: repeat(auto-fit, minmax(420px, 1fr));
    }
  }
  .sim-leg-chart-row {
    background: rgba(13, 21, 38, 0.4);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 3px;
    padding: 0.3rem 0.4rem;
  }
  .sim-leg-chart-header {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    margin-bottom: 0.2rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
  }
  .sim-empty-leg {
    padding: 0.2rem 0.4rem;
    font-size: var(--fs-xs);
  }
  .sim-payoff-legend {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
    margin-top: 0.45rem;
    padding-top: 0.4rem;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
  }
  .sim-leg-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--algo-slate);
  }
  .sim-leg-swatch {
    display: inline-block;
    width: 0.7rem;
    height: 0.7rem;
    border-radius: 2px;
    flex-shrink: 0;
  }
  .sim-leg-side {
    font-weight: 700;
    font-size: var(--fs-2xs);
    letter-spacing: 0.06em;
    padding: 0 0.3rem;
    border-radius: 2px;
    min-width: 3.2rem;
    text-align: center;
  }
  .sim-leg-side-long  { color: #7dd3fc; background: var(--algo-sky-bg); }
  .sim-leg-side-short { color: #fb923c; background: rgba(251, 146, 60, 0.12);  }
  .sim-leg-qty    { color: #fde68a; font-variant-numeric: tabular-nums; }
  .sim-leg-symbol { color: var(--algo-slate); font-weight: 700; }
  .sim-leg-price  {
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
  }
  .sim-leg-acct   { color: var(--algo-muted); font-size: var(--fs-xs); }
  .sim-pill-pnl.pos { color: var(--c-long); }
  .sim-charts {
    margin-top: 0.4rem;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
    gap: 0.5rem;
  }

  /* ── Card wrapper for each section in the main column ─────────────
     Earlier the section labels + content were sibling divs with no
     visual separation — everything stacked into one long flat column
     and the operator couldn't tell where Indices ended and Live
     Activity began. Wrapping each section in .sim-card adds a subtle
     bg + border so the boundaries are obvious without shouting.
     Same gradient as .algo-status-card so the page reads as one
     consistent dark surface, not a quilt of different card styles. */
  .sim-card {
    background: linear-gradient(180deg, rgba(15,23,41,0.65) 0%, rgba(10,16,32,0.65) 100%);
    border: 1px solid rgba(255,255,255,0.06);
    border-radius: 0.35rem;
    padding: 0.55rem 0.85rem 0.7rem;
    margin-bottom: 0.55rem;
  }
  .sim-card:last-child { margin-bottom: 0; }
  /* Section labels inside cards: drop the top margin so the label
     sits flush with the card's top padding. */
  .sim-card > .sim-section-label:first-child {
    margin-top: 0;
  }

  /* Positions + Holdings summaries sit side-by-side on a wide main
     column. Each card is its own column; collapse to one column
     below ~720 px so the grid stays readable on narrow viewports
     (when the operator has the side aside visible + a tablet
     window). The grid is the only place we want a 2-col layout
     inside the main panel; everything else (Indices / Live Activity
     / Underlyings / Past Simulations) wants full width because of
     long pill rows + wide tables. */
  .sim-summary-row {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.55rem;
    margin-bottom: 0.55rem;
  }
  @media (min-width: 720px) {
    .sim-summary-row {
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    }
  }
  .sim-summary-row > .sim-card { margin-bottom: 0; }
  /* Section label between chart / summary blocks. Same amber as
     MarketPulse's mp-section-label so the sim panel feels consistent
     with /dashboard's existing summary headings. */
  .sim-section-label {
    margin-top: 0.85rem;
    margin-bottom: 0.3rem;
    font-size: var(--fs-sm);
    font-family: var(--font-numeric);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--c-action);
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
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    border-radius: 4px;
    background: var(--algo-sky-bg-soft);
    border: 1px solid rgba(125, 211, 252, 0.25);
    color: var(--algo-slate);
  }
  .sim-index-pill.up   { border-color: rgba(74, 222, 128, 0.45); }
  .sim-index-pill.down { border-color: rgba(248, 113, 113, 0.45); }
  .sim-index-name { color: var(--c-action); font-weight: 700; letter-spacing: 0.04em; }
  .sim-index-spot { font-variant-numeric: tabular-nums; color: #fde68a; }
  .sim-index-pct  {
    font-variant-numeric: tabular-nums;
    font-weight: 700;
    color: var(--algo-slate);
  }
  .sim-index-pill.up   .sim-index-pct { color: var(--c-long); }
  .sim-index-pill.down .sim-index-pct { color: var(--c-short); }
  /* Summary grids — small inline ag-Grid-style table; matches the
     /dashboard cream-on-navy summary panels without dragging in a
     full ag-Grid instance. */
  .sim-summary-grid {
    width: 100%;
    border-collapse: collapse;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--algo-slate);
  }
  .sim-summary-grid th {
    text-align: left;
    color: var(--algo-muted);
    font-weight: 600;
    padding: 0.3rem 0.55rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    font-size: var(--fs-xs);
  }
  .sim-summary-grid td {
    padding: 0.3rem 0.55rem;
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }
  .sim-summary-grid .sim-num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .sim-summary-grid .sim-num.up   { color: var(--c-long); }
  .sim-summary-grid .sim-num.down { color: var(--c-short); }
  .sim-summary-total td {
    font-weight: 700;
    color: #fde68a;
    border-top: 1px solid rgba(251,191,36,0.25);
  }
  /* Empty placeholders so every section header has SOMETHING under it
     even when there's no live data — operator sees structure, not a
     missing panel. */
  .sim-empty {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--algo-muted);
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
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
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
    color: var(--algo-muted);
    font-variant-numeric: tabular-nums;
    font-size: var(--fs-xs);
    white-space: nowrap;
  }
  .sim-activity-chip {
    display: inline-block;
    padding: 0 0.3rem;
    border-radius: 2px;
    font-size: var(--fs-2xs);
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
    color: var(--c-action);
    background: rgba(251, 191, 36, 0.10);
    border: 1px solid rgba(251, 191, 36, 0.35);
  }
  .sim-activity-slug {
    color: #fde68a;
    font-weight: 700;
    white-space: nowrap;
  }
  .sim-activity-detail {
    color: var(--algo-slate);
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
    color: var(--c-action);
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
    background: var(--algo-green-bg);
    border: 1px solid rgba(74, 222, 128, 0.40);
    color: var(--c-long);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
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
    font-size: var(--fs-xs);
    letter-spacing: 0.05em;
    font-weight: 700;
  }
  .sim-past-end-book_empty,
  .sim-past-end-scenario_complete { color: var(--c-long); }
  .sim-past-end-time_limit,
  .sim-past-end-stopped           { color: var(--c-action); }
  .sim-past-end-failed            { color: var(--c-short); }
  .sim-past-end-pending           { color: #7dd3fc; }
  .sim-past-all {
    display: inline-block;
    margin-top: 0.35rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: #7dd3fc;
    text-decoration: none;
  }
  .sim-past-all:hover { color: var(--c-action); }
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
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
    padding-bottom: 0.15rem;
    border-bottom: 1px solid rgba(251,191,36,0.18);
  }
  :global(.custom-pos-row .field-input) {
    font-size: var(--fs-sm);
    padding: 0.25rem 0.4rem;
    font-family: monospace;
  }
  .custom-pos-del {
    width: 1.4rem;
    height: 1.4rem;
    border-radius: 3px;
    border: 1px solid rgba(248,113,113,0.4);
    background: rgba(248,113,113,0.08);
    color: var(--c-short);
    font-size: var(--fs-xl);
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }
  .custom-pos-del:hover {
    background: rgba(248,113,113,0.18);
    border-color: rgba(248,113,113,0.65);
  }
  :global(.sim-fields-row .field-input) {
    font-size: var(--fs-sm);
    padding: 0.25rem 0.4rem;
    height: auto;
    min-height: 1.55rem;
    width: 100%;
  }
  :global(.sim-fields-row .field-label) {
    font-size: var(--fs-2xs);
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
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    color: var(--c-action);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }
  .iter-history-link {
    margin-left: auto;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: #7dd3fc;
    text-decoration: none;
  }
  .iter-history-link:hover { color: var(--c-action); }
  /* Cross-underlying correlation chip — read-only badge that surfaces
     the default beta table so operators see what propagation fires
     when a scenario moves NIFTY/BANKNIFTY/FINNIFTY. Click the (?)
     for the full table in an InfoHint popup. */
  .iter-corr-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    color: var(--algo-slate);
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
    color: #c084fc;
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .iter-corr-pairs {
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .iter-banner-warn {
    margin-bottom: 0.5rem;
    padding: 0.4rem 0.6rem;
    background: var(--c-short-10);
    border: 1px solid rgba(248,113,113,0.35);
    color: var(--c-short);
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
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
  /* Stepper — − value + for Iterations + Max min. Same shape as
     OrderTicket's Lots stepper so the operator's muscle memory
     transfers across the platform. */
  .iter-stepper {
    display: inline-flex;
    align-items: center;
    background: rgba(13, 21, 38, 0.6);
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: 4px;
    overflow: hidden;
    height: 1.6rem;
  }
  .iter-stepper-btn {
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
    transition: background 0.08s, color 0.08s;
  }
  .iter-stepper-btn:hover {
    background: rgba(251, 191, 36, 0.15);
    color: #fde68a;
  }
  .iter-stepper-val {
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
    font-size: var(--fs-sm);
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
