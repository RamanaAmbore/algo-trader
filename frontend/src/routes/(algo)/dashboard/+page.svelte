<script>
  import { onMount, onDestroy, getContext, untrack } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import PnlAnalysis from '$lib/PnlAnalysis.svelte';
  import NavTab from '$lib/NavTab.svelte';
  import NavBreakdown from '$lib/NavBreakdown.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import CardControls from '$lib/CardControls.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import ActivityLogSurface from '$lib/ActivityLogSurface.svelte';
  import { clientTimestamp, visibleInterval, lastRefreshAt, connStatus, selectedStrategyId, strategyOpenSymbols } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import { bookChanged } from '$lib/data/bookChanged';
  import StrategyPicker from '$lib/StrategyPicker.svelte';
  // NewsList retired here — the dashboard's Market News card was
  // replaced with an ActivityLogSurface mount whose News tab now
  // serves the same surface (via LogPanel → NewsList) so we only
  // import the higher-level activity components above.
  import ActionEventsToggle from '$lib/ActionEventsToggle.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import {
    fetchRecentAgentEvents,
    fetchIntradayEquity,
    batchQuote,
    fetchNavLatest,
  } from '$lib/api';
  import { positionsStore, holdingsStore, fundsStore } from '$lib/data/marketDataStores.svelte.js';
  import { userCaps, userRole, hasCap } from '$lib/rbac';
  import { priceFmt, pctFmt, aggCompact } from '$lib/format';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  import { createChartRefreshPulse } from '$lib/data/chartRefreshPulse.svelte.js';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import {
    classifyByIndex,
    FO_QUOTE_KEYS, MIDCAP_QUOTE_KEYS, SMLCAP_QUOTE_KEYS,
    symbolFromQuoteKey,
  } from '$lib/data/indexConstituents';
  import { readChartPref, writeChartPref } from '$lib/data/chartPrefs';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';
  import { baseDayPnlForPosition } from '$lib/data/nav';
  import { mkUtilPctCol } from '$lib/data/pulseColumns.js';

  // ag-Grid module registration — idempotent across re-mounts.
  ModuleRegistry.registerModules([AllCommunityModule]);

  // ag-Grid valueFormatter wrappers — single source of truth for
  // how numbers render across the dashboard's grids: en-IN grouping,
  // no `+` prefix on positives (direction is colour-coded), '—'
  // for null. Mirrors MarketPulse / PerformancePage conventions so
  // a glance across pages reads the same.
  const _agNumFmt   = ({ value }) => value == null ? '—' : priceFmt(value);
  const _agAggFmt   = ({ value }) => value == null ? '—' : aggCompact(value);
  const _agPctFmt   = ({ value }) => value == null ? '—' : `${pctFmt(value)}%`;
  const _agUtilFmt  = ({ value }) => value == null ? '—' : `${Math.round(value * 100)}%`;
  const _numericHdr = 'ag-right-aligned-header';

  // Direction-coloured numeric cell — reuse the algo theme's
  // existing pnl-gain / pnl-loss / pnl-zero classes (defined in
  // app.css with `!important` to win against the theme's row
  // colour). Same idiom as MarketPulse + PerformancePage so the
  // visual treatment matches across every algo page.
  const _agDirCell = (p) =>
    `ag-right-aligned-cell ${p.value > 0 ? 'pnl-gain' : p.value < 0 ? 'pnl-loss' : 'pnl-zero'}`;

  // Tick-flash — subtle 350ms directional background pulse on the Equity
  // card's Day P&L and P&L cells. Keyed as `account:field`. TOTAL rows
  // (account === 'TOTAL') excluded. Alpha 0.13 via global .tf-up/.tf-down.
  const _dashFlash = createTickFlash({ threshold: 0.001, durationMs: 300 });

  // Flash-augmented cellClass for the equity summary grids.
  // `field` is the column field name used as part of the per-cell key.
  function _dashDirCell(field) {
    return (p) => {
      const base = `ag-right-aligned-cell ${p.value > 0 ? 'pnl-gain' : p.value < 0 ? 'pnl-loss' : 'pnl-zero'}`;
      if (!p.data || p.data.account === 'TOTAL') return base;
      // Pinned rows also excluded.
      if (p.node?.rowPinned === 'bottom') return base;
      const fc = _dashFlash.classOf(`${p.data.account}:${field}`);
      return fc ? `${base} ${fc}` : base;
    };
  }

  // IST-midnight-as-UTC for "today" date-window filters. Indian markets
  // (and operators) live in Asia/Kolkata; using the browser's local
  // midnight via setHours(0,0,0,0) gave wrong counts whenever the
  // browser TZ differed from IST (or even across IST midnight rollover
  // when the operator was outside India).
  function istMidnightTodayAsDate() {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone: 'Asia/Kolkata',
      year: 'numeric', month: '2-digit', day: '2-digit',
    }).formatToParts(new Date());
    const y = parts.find(p => p.type === 'year').value;
    const m = parts.find(p => p.type === 'month').value;
    const d = parts.find(p => p.type === 'day').value;
    return new Date(`${y}-${m}-${d}T00:00:00+05:30`);
  }

  // ── Layout shared context (paper status + open orders) ──────────────
  const algoStatus = getContext('algoStatus');

  // ── Hero row state ─────────────────────────────────────────────────
  // Derived from filtered positions + holdings so the chart stat
  // overlay numbers (P&L TODAY / TODAY %) always match the Equity
  // tab's Positions/Holdings Summary TOTAL row. Earlier these were
  // `$state` computed once inside `loadHero` with ALL positions +
  // holdings; the Equity tab uses `_accountFilter(... _eqAccounts)`
  // for its summaries, so picking an account filter desynced the
  // overlay from the TOTAL row. Reactive derivation closes the gap.
  const _todayPnl = $derived.by(() => {
    let dayPnl = 0;
    let any = false;
    for (const p of _accountFilter(_positions, _eqAccounts)) {
      const v = baseDayPnlForPosition(p);
      if (Number.isFinite(v)) { dayPnl += v; any = true; }
    }
    for (const h of _accountFilter(_holdings, _eqAccounts)) {
      const v = Number(h.day_change_val);
      if (Number.isFinite(v)) { dayPnl += v; any = true; }
    }
    return any ? dayPnl : null;
  });
  const _startingNav = $derived.by(() => {
    let invVal = 0;
    for (const p of _accountFilter(_positions, _eqAccounts)) {
      invVal += Math.abs(Number(p.average_price) * Number(p.quantity)) || 0;
    }
    for (const h of _accountFilter(_holdings, _eqAccounts)) {
      invVal += Number(h.inv_val ?? 0);
    }
    return invVal > 0 ? invVal : null;
  });
  let _niftyDayPct  = $state(/** @type {number|null} */ (null));
  let _firesToday   = $state(0);
  let _paperOpen    = $state(0);
  let _heroLoadedAt = $state(/** @type {string|null} */ (null));
  let _heroTeardown;

  // Operator-facing log declutter: default to agent_fire ONLY so the
  // expanded log is a thin chronological list of "what fired".
  // Operator can flip the chip to ALSO include action successes /
  // errors when they want the deeper "what did the fire DO" trace.
  // (Collapse state itself is owned by the CollapseButton via
  // _colAgent / _colPnl + localStorage.)
  let _agentLogShowActions = $state(false);
  const _agentLogKinds = $derived(
    _agentLogShowActions
      ? ['agent_fire', 'agent_action_success', 'agent_action_error']
      : ['agent_fire'],
  );


  // ── Raw positions + holdings (reused for winners/losers and
  //     the new Equity-card tabs) ─────────────────────────────────
  // Slice 7h — raw arrays sourced from the module-level marketDataStores
  // singletons (three-tier: memory → localStorage → broker fetch).
  // With `selectedStrategyId == null` the derivations below are identity
  // (every row passes), so behaviour with no strategy picked is unchanged.
  //
  // Bridge positionsStore / holdingsStore / fundsStore through $effect → $state
  // to prevent the synchronous $derived cascade
  //   store write → _positionsRaw → _positions → _positionsSummary →
  //   _positionsTotal → $effect grid.setGridOption
  // from running as one long synchronous task that freezes the RefreshButton
  // spinner mid-animation (RAIL long-task violation).
  /** @type {any[]} */
  let _positionsRaw = $state(positionsStore.value ?? []);
  /** @type {any[]} */
  let _holdingsRaw  = $state(holdingsStore.value  ?? []);
  /** @type {any[]} */
  let _fundsRaw     = $state(fundsStore.value     ?? []);
  $effect(() => {
    const p = positionsStore.value;
    const h = holdingsStore.value;
    const f = fundsStore.value;
    untrack(() => {
      _positionsRaw = p ?? [];
      _holdingsRaw  = h ?? [];
      _fundsRaw     = f ?? [];
    });
  });
  const _matchStrategySym = (/** @type {string} */ sym) => {
    if ($selectedStrategyId == null) return true;
    if ($strategyOpenSymbols.size === 0) return false;
    return $strategyOpenSymbols.has(String(sym || '').toUpperCase());
  };
  const _positions = $derived(
    _positionsRaw.filter(r => _matchStrategySym(r?.tradingsymbol || r?.symbol))
  );
  const _holdings = $derived(
    _holdingsRaw.filter(r => _matchStrategySym(r?.tradingsymbol || r?.symbol))
  );
  // Full funds rows (for the Capital-card Funds table). Sourced from
  // _fundsRaw (bridged via $effect above). Kept separate from _margins
  // (gauge input) so the table can show cash + collateral + net etc.
  const _funds = $derived(_fundsRaw);

  // Equity card stacks Positions Summary on top and Holdings
  // Summary below — no tabs. The tab variant left ~half the card
  // empty whenever one side rendered; stacked uses the card's
  // full vertical real estate without operator interaction.

  // Per-card account filters — each AccountMultiSelect on the
  // dashboard binds to its OWN state, so adjusting one card's
  // scope doesn't cascade into the others. Empty array = all
  // accounts (no filter). Persisted to sessionStorage under
  // separate keys so the operator's per-card intent survives a
  // tab refresh.
  let _eqAccounts  = $state(/** @type {string[]} */ ([]));   // Equity card
  let _winAccounts = $state(/** @type {string[]} */ ([]));   // Top Winners
  let _losAccounts = $state(/** @type {string[]} */ ([]));   // Top Losers
  // Sentinel: set to true by onMount once the initial sessionStorage
  // restore has run. The $effect below checks this so it doesn't
  // attempt re-restore before the first synchronous onMount pass
  // completes (which would race with the initial setter calls).
  let _mountRestoreDone = false;

  // Broker-registry-loaded accounts — sourced from the connStatus store
  // (polled every 15 s by the layout's startConnStatusPoller). Eliminates
  // the separate fetchBrokerAccounts() call inside _fetchConn.
  // Bridged via $state because connStatus is a Svelte 4 writable store.
  let _connStatusSnap = $state($connStatus);
  $effect(() => { _connStatusSnap = $connStatus; });
  const _conn         = $derived({ loaded: _connStatusSnap.loaded, total: _connStatusSnap.total });
  const _knownBrokerAccounts = $derived(_connStatusSnap.accounts ?? []);

  // Re-run sessionStorage restore once the broker registry transitions
  // from empty → loaded (_knownBrokerAccounts.length > 0). Hoisted to
  // script top-level (not inside onMount) so Svelte 5 registers the
  // effect at component-init time and the reactive dependency on
  // _knownBrokerAccounts is tracked correctly. The _mountRestoreDone
  // sentinel gates the first fire: onMount sets it to true after its
  // own synchronous restore runs, so the effect only re-runs on a
  // genuine empty→loaded transition that onMount may have missed.
  $effect(() => {
    if (!_mountRestoreDone) return;     // wait for onMount to finish first
    if (_knownBrokerAccounts.length === 0) return;
    function _restore(key, /** @type {(v:string[])=>void} */ setter) {
      try {
        const cached = sessionStorage.getItem(key);
        if (!cached) return;
        const stored = JSON.parse(cached);
        if (!Array.isArray(stored) || stored.length === 0) return;
        const known = _knownBrokerAccounts;
        const valid = stored.filter(a => known.includes(a));
        const missingNew = known.some(a => !stored.includes(a));
        if (missingNew) { setter([]); sessionStorage.removeItem(key); }
        else            { setter(valid); }
      } catch (_) { setter([]); }
    }
    _restore('dash.eqAccounts',  v => _eqAccounts  = v);
    _restore('dash.winAccounts', v => _winAccounts = v);
    _restore('dash.losAccounts', v => _losAccounts = v);
  });

  // Derived list of distinct accounts seen in current positions +
  // holdings + broker registry. Sorted by canonical display_order.
  // Empty fallback when fetchBrokerAccounts 403s (non-admin session) —
  // picker still works off the rows-derived set.
  let _orderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubDashOrder = accountDisplayOrder.subscribe(m => { _orderMap = m; });
  onDestroy(() => _unsubDashOrder());

  const _availableAccounts = $derived.by(() => {
    const set = new Set();
    for (const r of _positions) if (r.account) set.add(String(r.account));
    for (const r of _holdings)  if (r.account) set.add(String(r.account));
    for (const a of _knownBrokerAccounts) set.add(String(a));
    return sortAccountsBy([...set], _orderMap);
  });

  // Apply a per-card account filter to a row list. Empty filter
  // (default) = pass-through. Generic helper so each card can pass
  // its own state and share the filtering logic.
  function _accountFilter(rows, accountsFilter) {
    if (!accountsFilter.length) return rows;
    const allow = new Set(accountsFilter);
    return rows.filter(r => allow.has(String(r.account || '')));
  }

  // Winners / Losers cards each tab through the 5 buckets
  // (underlying / midcap / smallcap / holdings / positions) instead
  // of stacking them. Default tab: 'underlying' — the broadest view
  // that aggregates F&O positions to their underlying name, which
  // is usually the operator's first question on the dashboard.
  // Default tab is HOLDINGS — that bucket consistently has the
  // deepest stream (operators with a stock-heavy book usually have
  // 20-100+ holdings vs 2-5 F&O underlyings). The earlier default
  // 'underlying' often surfaced only 2-3 entries, hiding the
  // existence of the top-10 cap until the operator clicked away.
  let _winTab = $state(/** @type {'underlying'|'midcap'|'smallcap'|'holdings'|'positions'} */ ('holdings'));
  let _losTab = $state(/** @type {'underlying'|'midcap'|'smallcap'|'holdings'|'positions'} */ ('holdings'));
  // NAV + Capital + Equity share ONE tabbed card in the row1-split
  // (Bloomberg PRTU's tabbed portfolio sidebar pattern). NAV is the
  // default — it's the "what's the firm worth right now?" glance,
  // mirroring PerformancePage's NAV grid so the dashboard and the
  // canonical performance view can't drift. Capital answers "can I
  // take risk?", Equity drills into Positions / Holdings summaries.
  // All panels stay mounted (`hidden`, not {#if}) so ag-Grid instances
  // don't orphan across tab flips.
  let _capEqTab = $state(/** @type {'nav'|'capital'|'equity'} */ ('nav'));

  // Row 1 OTHER slot — tabbed card: NAV (history curve) vs Intraday
  // (today's cum P&L SVG) vs Performance (PnlAnalysis component).
  // NAV is the DEFAULT — operator complaint Jun 2026 ("intraday,
  // performance chart does not have nav. is it removed? fix it"):
  // the NAV history curve is the operator's "what's the firm worth
  // over time?" glance and must lead the chart card. Intraday is the
  // live "what's the book doing right now?" view; Performance is the
  // historical drill-down. NavTab trails the daily snapshot landing at
  // 16:00 IST + manual recomputes. The sidebar right card keeps its
  // own NAV tab (NavBreakdown — per-account decomposition) so both
  // glances (curve + per-account table) are surfaced without a click.
  let _chartTab = $state(/** @type {'nav'|'intraday'|'performance'} */ ('nav'));
  // Persist the operator's last-active chart-card tab (NAV / Intraday /
  // Performance) and sidebar tab (NAV / Capital / Equity) to localStorage
  // so they survive page navigation. Hydrated in onMount (SSR-safe).
  const _CHART_TAB_LS_KEY    = 'rbq.cache.dash-chart-tab.v1';
  const _CAP_EQ_TAB_LS_KEY   = 'rbq.cache.dash-cap-eq-tab.v1';
  const _CHART_TAB_VALID   = new Set(['nav', 'intraday', 'performance']);
  const _CAP_EQ_TAB_VALID  = new Set(['nav', 'capital', 'equity']);
  let _tabsHydrated = $state(false);
  $effect(() => {
    const snap = _chartTab;
    if (!_tabsHydrated) return;
    writeChartPref(_CHART_TAB_LS_KEY, snap);
  });
  $effect(() => {
    const snap = _capEqTab;
    if (!_tabsHydrated) return;
    writeChartPref(_CAP_EQ_TAB_LS_KEY, snap);
  });
  // Bindable mirror of PnlAnalysis.hasData — flips to false once
  // /pnl-benchmarks confirms zero dates. Default true so the
  // auto-collapse effect below doesn't fire during the initial
  // loading window before the fetch resolves.
  let _pnlHasData = $state(true);

  // Performance card stays EXPANDED even when empty — operator
  // feedback. The earlier auto-collapse latch hid both tabs once the
  // Intraday + Performance feeds returned no data, but that took the
  // card off-screen before the operator could inspect what was
  // happening (e.g. broker disconnect, pre-market load). Now the
  // card always renders its body and the empty-state message lives
  // inside the panel.

  // Per-card fullscreen toggles. Each card binds its own slot —
  // multiple cards can theoretically open at once but only one is
  // visually on top (last-clicked wins via DOM order).
  let _fsEquityCurve = $state(false);
  let _fsCapital     = $state(false);
  let _fsEquity      = $state(false);
  let _fsNavBd       = $state(false);   // NAV tab on the equity card
  let _fsWinners     = $state(false);
  let _fsLosers      = $state(false);
  // Renamed _fsNews → _fsActivity — the dashboard's News strip was
  // replaced by an ActivityLogSurface mount whose default tab is News.
  let _fsActivity    = $state(false);
  // _fsPnl / _colPnl retired with the standalone P&L Analysis card —
  // PnlAnalysis now lives inside the Intraday/Performance tabbed card
  // (shares _fsEquityCurve + _colEquityCurve).
  let _fsAgent       = $state(false);

  // Per-card collapse toggles. CollapseButton hydrates each from
  // localStorage on mount (keyed by user + cardId), so these
  // initial `false` values are placeholders only — the component
  // overwrites after the first onMount tick.
  let _colEquityCurve = $state(false);
  let _colCapital     = $state(false);
  let _colEquity      = $state(false);
  let _colNavBd       = $state(false);   // NAV tab on the equity card
  let _colWinners     = $state(false);
  let _colLosers      = $state(false);
  // Renamed _colNews → _colActivity (see _fsActivity above).
  let _colActivity    = $state(false);
  // Agent activity is a heavyweight card — start collapsed by default
  // so the dashboard's first paint stays light. CollapseButton overrides
  // from localStorage on mount if the user previously expanded it.
  let _colAgent       = $state(false);

  // ── ag-Grid bindings for the dashboard cards ────────────────────
  // Each card mounts its own grid imperatively via bind:this + a
  // mount-time $effect. Once mounted, rowData updates flow through
  // a separate reactive $effect that hits setGridOption('rowData').
  // Pattern mirrors MarketPulse for consistency.
  // Grid element bindings — MUST be $state so the `$effect` that
  // mounts the grid re-triggers when `bind:this` assigns. In Svelte 5
  // a plain `let` is NOT reactive; bind:this would set it but the
  // effect would only ever run once with the initial undefined value
  // and the grid would never mount.
  let _fundsEl     = $state(/** @type {HTMLDivElement|null} */ (null));
  let _marginEl    = $state(/** @type {HTMLDivElement|null} */ (null));
  let _winEl       = $state(/** @type {HTMLDivElement|null} */ (null));
  let _losEl       = $state(/** @type {HTMLDivElement|null} */ (null));
  let _eqPosEl     = $state(/** @type {HTMLDivElement|null} */ (null));
  let _eqHoldEl    = $state(/** @type {HTMLDivElement|null} */ (null));
  let _fundsGrid, _marginGrid, _winGrid, _losGrid, _eqPosGrid, _eqHoldGrid;
  /** @type {{ downloadCsv: () => void } | null} */
  let _navBdRef = $state(null);
  let _fundsReady  = $state(false);
  let _marginReady = $state(false);
  let _winReady    = $state(false);
  let _losReady    = $state(false);
  let _eqPosReady  = $state(false);
  let _eqHoldReady = $state(false);

  // Click-to-open SymbolPanel from W/L grid rows.
  function _openSymbol(sym) {
    const s = String(sym || '').trim();
    if (!s) return;
    _ticketProps = {
      symbol: s,
      defaultTab: 'chart',
      onClose:  () => { _ticketProps = null; },
      onSubmit: () => { _ticketProps = null; },
    };
  }

  // Per-account summary derivations — same shape MarketPulse
  // builds internally, but computed here from the already-loaded
  // _positions / _holdings. No extra fetches; the HTML tables
  // below render directly from these derivations.
  //
  // Day-change for positions comes from row.day_change_val (today's
  // P&L; previously used row.pnl which is life-to-date and was
  // incorrectly labelled "Day P&L"). For holdings it's also
  // row.day_change_val per the HoldingRow schema; legacy fallbacks
  // for `day_change` / `day_change_pct_amount` are dead field names
  // that never exist on the row.
  /** @typedef {{account: string, day_pnl: number, pnl: number, inv_val: number, cur_val: number}} SumRow */

  // Pre-seed byAcct with every broker-registry account so the summary
  // grid lists ALL loaded accounts including ones with 0 positions /
  // 0 holdings (e.g. freshly-added Dhan / Groww rows). Without this,
  // empty accounts silently vanish from the summary and the operator
  // can't tell at a glance that the row is loaded.
  function _seedSummaryByAcct(filter) {
    /** @type {Record<string, SumRow>} */
    const byAcct = {};
    const allowSet = filter && filter.length > 0 ? new Set(filter) : null;
    for (const a of _knownBrokerAccounts) {
      if (!a) continue;
      if (allowSet && !allowSet.has(a)) continue;
      byAcct[String(a)] = { account: String(a), day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
    }
    return byAcct;
  }

  const _positionsSummary = $derived.by(() => {
    const byAcct = _seedSummaryByAcct(_eqAccounts);
    // Equity card uses its own _eqAccounts state — independent of
    // the W/L cards' filters.
    for (const r of _accountFilter(_positions, _eqAccounts)) {
      const a = String(r.account || '');
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
      byAcct[a].day_pnl += baseDayPnlForPosition(r);
      byAcct[a].pnl     += Number(r.pnl) || 0;
    }
    const rows = Object.values(byAcct);
    return sortAccountsBy(rows.map(r => r.account), _orderMap)
      .map(id => rows.find(r => r.account === id)).filter(Boolean);
  });

  const _holdingsSummary = $derived.by(() => {
    const byAcct = _seedSummaryByAcct(_eqAccounts);
    for (const r of _accountFilter(_holdings, _eqAccounts)) {
      const a = String(r.account || '');
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
      byAcct[a].day_pnl += Number(r.day_change_val) || 0;
      byAcct[a].pnl     += Number(r.pnl) || 0;
      byAcct[a].inv_val += Number(r.inv_val) || 0;
      byAcct[a].cur_val += Number(r.cur_val) || 0;
    }
    const rows = Object.values(byAcct);
    return sortAccountsBy(rows.map(r => r.account), _orderMap)
      .map(id => rows.find(r => r.account === id)).filter(Boolean);
  });

  // Per-account TOTAL rows (sum across accounts) — pinned at the
  // bottom of each summary table so the operator's eye lands on
  // the firm-wide number without scrolling.
  const _positionsTotal = $derived.by(() => {
    const t = { account: 'TOTAL', day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
    for (const r of _positionsSummary) {
      t.day_pnl += r.day_pnl; t.pnl += r.pnl;
    }
    return t;
  });

  const _holdingsTotal = $derived.by(() => {
    const t = { account: 'TOTAL', day_pnl: 0, pnl: 0, inv_val: 0, cur_val: 0 };
    for (const r of _holdingsSummary) {
      t.day_pnl += r.day_pnl; t.pnl += r.pnl;
      t.inv_val += r.inv_val; t.cur_val += r.cur_val;
    }
    return t;
  });

  // Counts shown in the tab strip — symbol count, not row count.
  // A book of 4 NIFTY contracts across 2 accounts shows as "4".
  const _positionsCount = $derived(_positions.length);
  const _holdingsCount  = $derived(_holdings.length);

  // Helpers for table cells.
  const _pnlColor = (v) =>
    v > 0 ? 'cap-up' : v < 0 ? 'cap-down' : 'cap-neutral';

  // ── SymbolPanel for winners/losers tile click ──────────────────────
  let _ticketProps = $state(/** @type {any} */ (null));

  // ── Row 1: Intraday equity curve ───────────────────────────────────
  /** @type {{ ts: string, day_pnl: number, cum_pnl: number }[]} */
  let _equityPoints = $state([]);
  const _eqPulse = createChartRefreshPulse();
  $effect(() => {
    if (_equityPoints.length) untrack(() => _eqPulse.notify('eq'));
  });

  // ── Row 1: Margin utilisation gauges ──────────────────────────────
  // Derived from _funds (which itself derives from fundsStore) so the
  // gauge rows always stay in sync with the Capital-card Funds table
  // without an extra fetch.
  /**
   * @type {{ account: string, used: number, avail: number, util_pct: number }[]}
   */
  const _margins = $derived(_funds.map(r => {
    const used  = Number(r.used_margin) || 0;
    const avail = Number(r.avail_margin ?? r.available_margin) || 0;
    const total = used + avail;
    return {
      account: String(r.account),
      used,
      avail,
      util_pct: total > 0 ? used / total : 0,
    };
  }));

  // ── Derived hero values ────────────────────────────────────────────
  const _todayPct = $derived(
    (_todayPnl != null && _startingNav != null && _startingNav !== 0)
      ? (_todayPnl / _startingNav) * 100
      : null
  );

  const _vsNifty = $derived(
    (_todayPct != null && _niftyDayPct != null)
      ? _todayPct - _niftyDayPct
      : null
  );

  const _pnlClass = $derived(
    _todayPnl == null ? 'hero-pnl-neutral'
    : _todayPnl > 0   ? 'hero-pnl-up'
    : _todayPnl < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  const _todayPctClass = $derived(
    _todayPct == null ? 'hero-pnl-neutral'
    : _todayPct > 0   ? 'hero-pnl-up'
    : _todayPct < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  const _vsNiftyClass = $derived(
    _vsNifty == null ? 'hero-pnl-neutral'
    : _vsNifty > 0   ? 'hero-pnl-up'
    : _vsNifty < 0   ? 'hero-pnl-down' : 'hero-pnl-neutral'
  );

  // ── Open orders (from layout's algoStatus poll) ───────────────────
  const _openOrders = $derived(
    /** @type {any[]} */ (algoStatus.paperStatus?.open_order_details ?? [])
  );

  // ── Categorised top-3 movers per bucket ─────────────────────────────
  // (The earlier merged _combinedBook / _winners / _losers derivations
  // were retired when the dashboard moved to the 5-bucket tabbed
  // Winners/Losers cards — every bucket is now built directly from
  // the _positionsByUnderlying / _holdingsFor / _positionsRows
  // helpers, all of which aggregate by symbol.)
  // Five buckets, each filtered to its source/classification, sorted
  // by P&L. Rendered as side-by-side compact lists inside the Top
  // Winners / Top Losers cards so the operator sees the movement
  // picture across all instrument classes in one glance.
  //
  // Buckets:
  //   1. Option Underlying — positions aggregated by parsed underlying
  //      (NIFTY, BANKNIFTY, …). One row per underlying, sum of P&L.
  //   2. Midcap   — holdings classified as NIFTY MIDCAP 100.
  //   3. Smallcap — holdings classified as NIFTY SMLCAP 100.
  //   4. Holdings — top single-stock from holdings (all caps).
  //   5. Positions — top single-contract from positions (all symbols).

  // Parse the equity ticker prefix from a tradingsymbol — same logic
  // as instruments.js _derivedUnderlying (longest pure-letter prefix
  // before any digit). Works for NIFTY25APR22000CE, NIFTY2640722700CE,
  // CRUDEOIL25APRFUT, RELIANCE26APR1360CE, etc.
  function _parseUnderlyingPrefix(sym) {
    if (!sym) return '';
    const m = String(sym).match(/^([A-Z]+)/);
    return m ? m[1] : String(sym);
  }

  // Market-wide quotes for Top Winners/Losers — Underlying / Midcap /
  // Smallcap buckets show top movers across the FULL exchange universe
  // (NIFTY MIDCAP 100 / NIFTY SMLCAP 100 / curated F&O list),
  // independent of the operator's positions or holdings. Only the
  // Holdings + Positions tabs remain user-scoped.
  //
  // /quote/batch response shape: {refreshed_at, items: [BatchQuoteRow]}.
  // We index by "${exchange}:${tradingsymbol}" so universe queries can
  // pluck back out by key. Polled every 60 s during active viewing.
  /** @type {Record<string, {ltp:number, close:number, change_pct:number, ohlc?:{close?:number}, previous_close?:number}>} */
  let _marketQuotes = $state({});

  // Build a market-wide row list from quotes for the given universe.
  // Each row carries `kind: 'market'`, with `pnl` holding the day
  // percentage (used both as the sort key and the display value).
  // `ltp` rides along so the row can show the live spot alongside
  // the move. User-scoped rows (Holdings / Positions buckets) use
  // `kind: 'user'` with pnl in rupees and inv_val for the % cell.
  function _marketRows(keys) {
    const out = [];
    for (const k of keys) {
      const q = _marketQuotes[k];
      if (!q) continue;
      const ltp = Number(q.ltp) || 0;
      // Backend already computes change_pct; fall back to manual
      // calc only when zero / missing (defensive against partial fills).
      let pct = Number(q.change_pct);
      if (!isFinite(pct) || pct === 0) {
        const close = Number(q.ohlc?.close ?? q.close ?? q.previous_close) || 0;
        if (close > 0 && ltp > 0) pct = ((ltp - close) / close) * 100;
      }
      if (pct == null || !isFinite(pct) || pct === 0) continue;
      out.push({
        symbol: symbolFromQuoteKey(k),
        pnl:    pct,           // sort key + display %
        ltp,                   // shown as @ ₹X
        kind:   'market',
      });
    }
    return out;
  }

  // Aggregate positions by parsed underlying. Kept for any future
  // "your underlying exposure" surface (e.g. an extra W/L bucket)
  // — Top Winners/Losers Underlying now reads from _marketRows().
  // Takes an account filter as input so the caller picks the scope.
  function _positionsByUnderlying(accounts) {
    /** @type {Map<string, {symbol: string, pnl: number, inv_val: number}>} */
    const byU = new Map();
    for (const p of _accountFilter(_positions, accounts)) {
      const sym = String(p.tradingsymbol || p.symbol || '');
      const pnl = Number(p.pnl) || 0;
      if (!sym || pnl === 0) continue;
      const u = _parseUnderlyingPrefix(sym);
      if (!u) continue;
      const cur = byU.get(u) ?? { symbol: u, pnl: 0, inv_val: 0 };
      cur.pnl += pnl;
      byU.set(u, cur);
    }
    return Array.from(byU.values());
  }

  // Market-wide rows for the three universe-based buckets.
  // Re-derives whenever _marketQuotes flips. Each is one bag of
  // {symbol, pnl=day_pct, inv_val=ltp} ready for _eligible/_top.
  const _foUnderlyingRows = $derived(_marketRows(FO_QUOTE_KEYS));
  const _midcapRows       = $derived(_marketRows(MIDCAP_QUOTE_KEYS));
  const _smlcapRows       = $derived(_marketRows(SMLCAP_QUOTE_KEYS));

  // Aggregate by symbol — dedupes the same instrument when held in
  // multiple accounts. Without this, GMDCLTD in both ZG0790 + ZJ6294
  // shows up twice in the Winners/Losers list. The winners/losers
  // surface is about "which symbol moved" not "which (symbol, account)
  // pair moved" — so collapse by symbol and sum across accounts.
  //
  // day_pct is a per-symbol global, not additive — it's the same
  // (last_price - close_price) / close_price * 100 for every account
  // holding the symbol, so we take the first non-zero value and let
  // every other account's row confirm it. Without this, _toWlRow used
  // to derive pct = pnl_today / inv_val (= today's rupee move / cost
  // basis), which conflates "today's % move" with "average per-rupee-
  // invested gain" — wildly wrong when one account holds a tiny
  // position and another holds a huge one. Operator saw IFCI's 4.98%
  // day move reported as 13.58% via that broken formula.
  //
  // LTP is a per-symbol global value too — first non-zero ltp wins.
  function _aggregateBySymbol(rows) {
    /** @type {Map<string, {symbol: string, pnl: number, inv_val: number, ltp: number, day_pct: number|null}>} */
    const bySym = new Map();
    for (const r of rows) {
      const sym = r.symbol;
      if (!sym) continue;
      const cur = bySym.get(sym) ?? { symbol: sym, pnl: 0, inv_val: 0, ltp: 0, day_pct: null };
      cur.pnl     += Number(r.pnl) || 0;
      cur.inv_val += Number(r.inv_val) || 0;
      if (!cur.ltp && r.ltp) cur.ltp = Number(r.ltp) || 0;
      if (cur.day_pct == null && r.day_pct != null) {
        cur.day_pct = Number(r.day_pct);
      }
      bySym.set(sym, cur);
    }
    return Array.from(bySym.values());
  }

  // Holdings, with optional class filter (midcap / smallcap / null=all)
  // + per-card account filter. Aggregated by symbol so a stock held
  // in N accounts appears as one row with summed day P&L + cost basis.
  function _holdingsFor(cls, accounts) {
    /** @type {{symbol: string, pnl: number, inv_val: number, ltp: number, day_pct: number|null}[]} */
    const raw = [];
    for (const h of _accountFilter(_holdings, accounts)) {
      const sym = String(h.tradingsymbol || h.symbol || '');
      const pnl = Number(h.day_change_val ?? 0);
      if (!sym) continue;
      if (cls) {
        const c = classifyByIndex(sym);
        if (c !== cls) continue;
      }
      raw.push({
        symbol: sym,
        pnl,
        inv_val: Number(h.inv_val ?? 0),
        ltp:     Number(h.last_price ?? h.ltp ?? 0),
        // Carry the broker's per-row day_change_percentage so the
        // aggregate keeps the correct (last-close)/close * 100 value.
        // Without this, Winners/Losers fell back to a wrong derived
        // formula (pnl/inv_val) — see _aggregateBySymbol.
        day_pct: h.day_change_percentage != null
                 ? Number(h.day_change_percentage)
                 : null,
      });
    }
    // Aggregate first, then drop zero-pnl symbols (the dedupe could
    // resolve a +X / -X pair into 0 — still want to hide those).
    // Tag every survivor as user-scoped so the template renders the
    // ₹ form (vs market rows which render as %).
    return _aggregateBySymbol(raw)
      .filter(r => r.pnl !== 0)
      .map(r => ({ ...r, kind: 'user' }));
  }

  // Positions as individual contracts, aggregated by tradingsymbol
  // across accounts (same reason as holdings). Takes a per-card
  // account filter so Winners + Losers cards can scope independently.
  function _positionsRowsFor(accounts) {
    /** @type {{symbol: string, pnl: number, inv_val: number, ltp: number, day_pct: number|null}[]} */
    const raw = [];
    for (const p of _accountFilter(_positions, accounts)) {
      const sym = String(p.tradingsymbol || p.symbol || '');
      const pnl = Number(p.pnl) || 0;
      if (!sym) continue;
      raw.push({
        symbol: sym,
        pnl,
        inv_val: 0,
        ltp: Number(p.last_price ?? p.ltp ?? 0),
        // Same fix as _holdingsFor — carry the broker's day-change %
        // so _toWlRow can use it directly instead of falling back to
        // a wrong derived formula.
        day_pct: p.day_change_percentage != null
                 ? Number(p.day_change_percentage)
                 : null,
      });
    }
    return _aggregateBySymbol(raw)
      .filter(r => r.pnl !== 0)
      .map(r => ({ ...r, kind: 'user' }));
  }

  // Eligible-rows picker — sorted by P&L, NOT sliced. Splits the
  // bucket source into the winner / loser subset.
  function _eligible(rows, kind) {
    if (!Array.isArray(rows) || rows.length === 0) return [];
    if (kind === 'win') {
      return rows.filter(r => r.pnl > 0).sort((a, b) => b.pnl - a.pnl);
    }
    return rows.filter(r => r.pnl < 0).sort((a, b) => a.pnl - b.pnl);
  }

  // Cap rows rendered per tab. The scrollable .wl-rows container
  // handles overflow at default size; fullscreen lifts the cap.
  // count chip on each tab shows the FULL eligible count (not the
  // sliced cap), so the operator knows "Holdings · 50" means 50
  // winners exist with top 10 visible inside.
  const _TOP_N = 10;

  // Five-bucket bundle, one per category. `count` is the total
  // eligible count; `rows` is sliced to _TOP_N for rendering.
  function _bucket(label, source, kind) {
    const all = _eligible(source, kind);
    return { label, count: all.length, rows: all.slice(0, _TOP_N) };
  }

  // Bucket sources:
  //   - OPTION UNDERLYING / MIDCAP / SMALLCAP — MARKET-wide (universe
  //     constants in $lib/data/indexConstituents), independent of
  //     positions/holdings + ANY account filter.
  //   - HOLDINGS / POSITIONS — user-scoped, honour the CARD'S OWN
  //     _winAccounts / _losAccounts state (independent per card).
  const _winnerBuckets = $derived([
    _bucket('OPTION UNDERLYING', _foUnderlyingRows,                 'win'),
    _bucket('MIDCAP',            _midcapRows,                       'win'),
    _bucket('SMALLCAP',          _smlcapRows,                       'win'),
    _bucket('HOLDINGS',          _holdingsFor(null, _winAccounts),  'win'),
    _bucket('POSITIONS',         _positionsRowsFor(_winAccounts),   'win'),
  ]);

  const _loserBuckets = $derived([
    _bucket('OPTION UNDERLYING', _foUnderlyingRows,                 'lose'),
    _bucket('MIDCAP',            _midcapRows,                       'lose'),
    _bucket('SMALLCAP',          _smlcapRows,                       'lose'),
    _bucket('HOLDINGS',          _holdingsFor(null, _losAccounts),  'lose'),
    _bucket('POSITIONS',         _positionsRowsFor(_losAccounts),   'lose'),
  ]);

  // Show the categorised section only when SOMETHING is movable. Hides
  // cleanly outside trading hours when every bucket is empty.
  const _hasWinners = $derived(_winnerBuckets.some(b => b.rows.length > 0));
  const _hasLosers  = $derived(_loserBuckets.some(b => b.rows.length > 0));

  // Normalise a bucket row to the shape the W/L ag-Grid expects:
  // {symbol, ltp, pct, pnl_abs?}. Market rows already carry pnl=pct;
  // user rows carry pnl=rupees, so we derive pct from (pnl / inv_val).
  // Adds `pnl_abs` for user rows so a future column can show ₹ alongside
  // the %.
  function _toWlRow(r) {
    if (r.kind === 'market') {
      return { symbol: r.symbol, ltp: r.ltp || null, pct: r.pnl, pnl_abs: null };
    }
    // user row: broker-supplied day_change_percentage is the canonical
    // (last - close) / close × 100. The legacy fallback (pnl / inv_val)
    // mixed up "today's % move" with "lifetime ₹ move as a fraction of
    // cost basis" — IFCI's real 4.98 % day move read as 13.58 %. When
    // the broker didn't supply day_change_percentage we now render '—'
    // instead of a wrong number.
    const pct = r.day_pct != null ? r.day_pct : null;
    return { symbol: r.symbol, ltp: r.ltp || null, pct, pnl_abs: r.pnl };
  }

  // Tab key ↔ bucket label helpers — keeps the template terse and the
  // state machine canonical.
  const _BUCKET_KEY = {
    'OPTION UNDERLYING': 'underlying',
    'MIDCAP':            'midcap',
    'SMALLCAP':          'smallcap',
    'HOLDINGS':          'holdings',
    'POSITIONS':         'positions',
  };
  const _BUCKET_LABEL = Object.fromEntries(
    Object.entries(_BUCKET_KEY).map(([l, k]) => [k, l])
  );
  function _bucketKey(label)   { return _BUCKET_KEY[label] ?? 'underlying'; }
  function _winTabLabel(key)   { return _BUCKET_LABEL[key] ?? 'OPTION UNDERLYING'; }
  // Short tab labels — "OPTION UNDERLYING" gets truncated for the
  // narrow tab strip; the others stay as their full token.
  const _TAB_SHORT = {
    'OPTION UNDERLYING': 'Underlying',
    'MIDCAP':            'Midcap',
    'SMALLCAP':          'Smallcap',
    'HOLDINGS':          'Holdings',
    'POSITIONS':         'Positions',
  };
  function _tabShort(label) { return _TAB_SHORT[label] ?? label; }

  const _connIcon = $derived(
    _conn.total === 0     ? '—'
    : _conn.loaded === 0  ? '✗'
    : _conn.loaded < _conn.total ? '⚠'
    : '✓'
  );

  const _connClass = $derived(
    _conn.total === 0     ? 'hero-chip-conn-neutral'
    : _conn.loaded === 0  ? 'hero-chip-conn-red'
    : _conn.loaded < _conn.total ? 'hero-chip-conn-amber'
    : 'hero-chip-conn-green'
  );

  // ── Equity chart SVG constants ─────────────────────────────────────
  const CHART_W = 600;
  const CHART_H = 220;
  const PAD_L = 8;
  const PAD_R = 52;
  const PAD_T = 12;
  const PAD_B = 28;
  const INNER_W = CHART_W - PAD_L - PAD_R;
  const INNER_H = CHART_H - PAD_T - PAD_B;

  // ── Equity chart series + toggle state ────────────────────────────
  // 6 series — operator picks which to show. Default 3 (H, P, ΔH+ΔP)
  // gives the cleanest scan; the breakdowns (ΔH, ΔP) and the lifetime
  // combined live behind one click in the legend strip.
  const _EQ_SERIES = [
    { id: 'H',     label: 'H',      title: 'Holdings — lifetime P&L',           field: 'h_pnl',  color: 'var(--algo-sky)', dash: '',    width: 1.5, dflt: true  },
    { id: 'dH',    label: 'ΔH',     title: 'Holdings — today’s change',    field: 'h_day',  color: 'var(--algo-sky)', dash: '4 3', width: 1.5, dflt: false },
    { id: 'P',     label: 'P',      title: 'Positions — lifetime P&L',          field: 'p_pnl',  color: 'var(--c-action)', dash: '',    width: 1.5, dflt: true  },
    { id: 'dP',    label: 'ΔP',     title: 'Positions — today’s change',   field: 'p_day',  color: 'var(--c-action)', dash: '4 3', width: 1.5, dflt: false },
    { id: 'comb',  label: 'H+P',    title: 'Combined — lifetime P&L',           field: 'cum_pnl',color: 'var(--c-long)', dash: '',    width: 2.0, dflt: false },
    { id: 'dComb', label: 'ΔH+ΔP',  title: 'Combined — today’s change',    field: 'day_pnl',color: 'var(--c-long)', dash: '4 3', width: 2.0, dflt: true  },
  ];
  let _eqSeriesOn = $state(/** @type {Record<string,boolean>} */ (
    Object.fromEntries(_EQ_SERIES.map(s => [s.id, s.dflt]))
  ));

  // ── Equity chart derived state ─────────────────────────────────────
  const _equityDomain = $derived.by(() => {
    if (!_equityPoints.length) return null;
    // Y-scale spans every value across every ENABLED series so the
    // chart re-frames automatically when the operator toggles a series
    // on or off. Falls back to day_pnl-only when no series is enabled
    // (shouldn't happen but keeps the chart from collapsing).
    const enabledFields = _EQ_SERIES.filter(s => _eqSeriesOn[s.id]).map(s => s.field);
    const fields = enabledFields.length ? enabledFields : ['day_pnl'];
    const vals = [];
    for (const f of fields) {
      for (const p of _equityPoints) {
        const v = Number(p[f]);
        if (Number.isFinite(v)) vals.push(v);
      }
    }
    if (!vals.length) return null;
    let yMin = Math.min(...vals);
    let yMax = Math.max(...vals);
    // ensure zero is always visible; add 10% padding
    yMin = Math.min(yMin, 0);
    yMax = Math.max(yMax, 0);
    const pad = Math.max((yMax - yMin) * 0.10, 500);
    yMin -= pad; yMax += pad;
    const ts = _equityPoints.map(p => new Date(p.ts).getTime());
    return { yMin, yMax, tMin: Math.min(...ts), tMax: Math.max(...ts) };
  });

  function _eqX(ts) {
    const d = _equityDomain;
    if (!d || d.tMax === d.tMin) return PAD_L;
    return PAD_L + ((new Date(ts).getTime() - d.tMin) / (d.tMax - d.tMin)) * INNER_W;
  }

  function _eqY(val) {
    const d = _equityDomain;
    if (!d || d.yMax === d.yMin) return PAD_T + INNER_H / 2;
    return PAD_T + (1 - (val - d.yMin) / (d.yMax - d.yMin)) * INNER_H;
  }

  /** Polyline string for any series field. */
  function _eqPolylineFor(field) {
    if (!_equityPoints.length || !_equityDomain) return '';
    return _equityPoints
      .map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(Number(p[field]) || 0).toFixed(1)}`)
      .join(' ');
  }
  /** Area-under-curve string for the dominant series (operator-picked) —
   *  filled only when a SINGLE series is enabled, so the chart still
   *  reads as a clean shape. Multi-series mode skips the fill. */
  const _eqDominantField = $derived.by(() => {
    const on = _EQ_SERIES.filter(s => _eqSeriesOn[s.id]);
    return on.length === 1 ? on[0].field : null;
  });
  const _eqAreaPath = $derived.by(() => {
    if (!_eqDominantField || !_equityPoints.length || !_equityDomain) return '';
    const pts = _equityPoints;
    const zero = _eqY(0);
    const f = _eqDominantField;
    const first = `${_eqX(pts[0].ts).toFixed(1)},${zero}`;
    const last  = `${_eqX(pts[pts.length - 1].ts).toFixed(1)},${zero}`;
    const line  = pts
      .map(p => `${_eqX(p.ts).toFixed(1)},${_eqY(Number(p[f]) || 0).toFixed(1)}`)
      .join(' L ');
    return `M ${first} L ${line} L ${last} Z`;
  });
  /** Active series for the per-series render loop. */
  const _eqActiveSeries = $derived(
    _EQ_SERIES
      .filter(s => _eqSeriesOn[s.id])
      .map(s => ({ ...s, points: _eqPolylineFor(s.field) }))
  );

  const _eqZeroY = $derived(_equityDomain ? _eqY(0) : null);

  /** Field used to pick the hover-dot colour + tooltip headline value.
   *  Prefers the dominant (single-series) field; falls back to day_pnl. */
  const _eqHoverField = $derived(_eqDominantField || 'day_pnl');
  const _eqPositive = $derived(
    _equityPoints.length
      ? (Number(_equityPoints[_equityPoints.length - 1][_eqHoverField]) || 0) >= 0
      : true
  );

  // Palette mirrors .eq-svg CSS vars --eq-line-up / --eq-line-down.
  // Only used in SVG attrs; null when multi-series (area fill hidden, dot falls back to sky).
  const _eqLineColor  = $derived(_eqDominantField ? (_eqPositive ? 'var(--c-long)' : 'var(--c-short)') : null);
  const _eqFillColor  = $derived(_eqDominantField ? (_eqPositive ? 'rgba(74,222,128,0.12)' : 'rgba(248,113,113,0.12)') : null);

  // Y-axis labels for equity chart (5 ticks)
  const _eqYLabels = $derived.by(() => {
    const d = _equityDomain;
    if (!d) return [];
    return Array.from({ length: 5 }, (_, i) => {
      const frac = i / 4;
      const val  = d.yMin + frac * (d.yMax - d.yMin);
      const y    = _eqY(val);
      return { y: y.toFixed(1), label: aggCompact(val) };
    });
  });

  // X-axis time labels (up to 5)
  const _eqXLabels = $derived.by(() => {
    const d = _equityDomain;
    if (!d || _equityPoints.length < 2) return [];
    const count = Math.min(5, _equityPoints.length);
    const step = Math.floor((_equityPoints.length - 1) / (count - 1 || 1));
    return Array.from({ length: count }, (_, i) => {
      const pt = _equityPoints[Math.min(i * step, _equityPoints.length - 1)];
      const x  = _eqX(pt.ts).toFixed(1);
      // times from backend are UTC; display in IST
      const d2  = new Date(pt.ts);
      const ist = new Date(d2.getTime() + 5.5 * 3600 * 1000);
      const ih  = String(ist.getUTCHours()).padStart(2, '0');
      const im  = String(ist.getUTCMinutes()).padStart(2, '0');
      return { x, label: `${ih}:${im}` };
    });
  });

  // Hover crosshair state
  let _hoverIdx = $state(/** @type {number|null} */ (null));
  let _hoverX   = $state(0);
  let _hoverY   = $state(0);

  const _hoverPt = $derived(
    _hoverIdx != null && _equityPoints[_hoverIdx]
      ? _equityPoints[_hoverIdx]
      : null
  );

  function _eqMouseMove(/** @type {MouseEvent} */ e) {
    if (!_equityPoints.length || !_equityDomain) return;
    const svg = /** @type {SVGSVGElement} */ (e.currentTarget);
    const rect = svg.getBoundingClientRect();
    const svgX = ((e.clientX - rect.left) / rect.width) * CHART_W;
    const frac = Math.max(0, Math.min(1, (svgX - PAD_L) / INNER_W));
    const ts   = _equityDomain.tMin + frac * (_equityDomain.tMax - _equityDomain.tMin);
    // find nearest point
    let best = 0, bestDt = Infinity;
    for (let i = 0; i < _equityPoints.length; i++) {
      const dt = Math.abs(new Date(_equityPoints[i].ts).getTime() - ts);
      if (dt < bestDt) { bestDt = dt; best = i; }
    }
    _hoverIdx = best;
    _hoverX   = parseFloat(_eqX(_equityPoints[best].ts).toFixed(1));
    _hoverY   = parseFloat(_eqY(_equityPoints[best].day_pnl).toFixed(1));
  }

  function _eqMouseLeave() { _hoverIdx = null; }

  // ── Margin gauge helpers ───────────────────────────────────────────
  const GAUGE_R = 32;
  const GAUGE_SW = 6;
  const GAUGE_CIRC = 2 * Math.PI * GAUGE_R;

  function _gaugeColor(pct) {
    if (pct < 0.50) return 'var(--c-long)';
    if (pct < 0.70) return 'var(--c-action)';
    if (pct < 0.85) return '#f59410';
    return 'var(--c-short)';
  }

  function _gaugeDash(pct) {
    const used = Math.max(0, Math.min(1, pct)) * GAUGE_CIRC;
    return `${used.toFixed(2)} ${GAUGE_CIRC.toFixed(2)}`;
  }

  // ── Fetch functions ────────────────────────────────────────────────
  async function _fetchEquity() {
    try {
      const res = await fetchIntradayEquity(200);
      // Accept both wrapped {points: [...]} (current backend) and bare
      // array (defensive against a future shape change).
      const pts = Array.isArray(res) ? res : (res?.points ?? []);
      _equityPoints = pts;
    } catch (_) { /* leave stale */ }
  }

  // Standalone freshness stamp for the equity curve was retired when
  // the Capital/Equity card lost its refresh chip — there's no UI
  // surface for it anymore. The chart-refresh cadence is still 15 s
  // via visibleInterval below.
  let _equityPollStop;

  // Operator-initiated full-page refresh — drives the RefreshButton in
  // the page header. Fires every loader the dashboard owns so a click
  // refreshes the entire page in one go. Spinner stays busy until the
  // heaviest op (loadHero) settles.
  let _refreshing = $state(false);
  async function _refreshAll() {
    if (_refreshing) return;
    _refreshing = true;
    try {
      // Sleep audit Jun 2026: Promise.all → Promise.allSettled so a
      // single hung sub-fetch can't strand the RefreshButton spinner.
      await Promise.allSettled([
        loadHero(),
        _fetchEquity(),
        fundsStore.load(undefined, { force: true }),
        _fetchNav(),
      ]);
    } finally {
      _refreshing = false;
    }
  }

  // Activity card filter state — bound to ActivityHeaderFilters in
  // the activity card's header strip and threaded into
  // ActivityLogSurface. Same per-surface state pattern /activity and
  // /orders use; account + level filters persist while the operator
  // flips tabs inside the activity surface.
  /** @type {string[]} */
  let _actAccountFilter     = $state([]);
  /** @type {string[]} */
  let _actAvailableAccounts = $state([]);
  /** @type {'all'|'error'|'warning'|'info'} */
  let _actLevelFilter       = $state('all');

  // NAV header chip — last computed firm NAV + day delta. Gated by
  // view_nav so demo / observer see the chip too; recompute is a
  // separate cap and lives on /nav. Failing fetch leaves _nav at the
  // last-good or null; chip self-hides when null.
  /** @type {{nav:number, as_of_date:string}|null} */
  let _navLatest = $state(null);
  let _navDelta     = $state(/** @type {number|null} */ (null));
  let _navDeltaPct  = $state(/** @type {number|null} */ (null));
  /** @type {string|null} */
  let _navFetchError = $state(null);
  const _canViewNav = $derived(hasCap('view_nav', $userCaps, $userRole));
  async function _fetchNav() {
    if (!_canViewNav) return;
    _navFetchError = null;
    try {
      const r = await fetchNavLatest();
      _navLatest   = r?.latest ?? null;
      _navDelta    = r?.day_delta ?? null;
      _navDeltaPct = r?.day_delta_pct ?? null;
    } catch (err) {
      // Surface the error so the operator can act (check broker
      // connections, re-run /api/nav/recompute, etc.). Leave
      // _navLatest at last-good so the chip stays populated on
      // transient failures.
      _navFetchError = (err && typeof err === 'object' && 'message' in err)
        ? String(/** @type {any} */ (err).message).slice(0, 60)
        : 'NAV fetch failed';
    }
  }
  // `_fmtNavInr` formatter retired — its sole caller (the dedicated
  // .dash-nav-row chip) moved into NavTab as an overlay, and NavTab
  // owns its own compact formatter (`_fmtChipInr`). The chip data
  // (`_navLatest` / `_navDelta` / `_navDeltaPct`) is still fetched
  // here and forwarded into <NavTab> as props.

  async function loadHero() {
    try {
      // Positions + holdings flow through the module-level store singletons.
      // _positionsRaw / _holdingsRaw are $derived from the stores so they
      // update reactively once each load() resolves — no manual assignment.
      // funds also flows through fundsStore; _funds + _margins are $derived.
      const [, , events] = await Promise.all([
        positionsStore.load().catch(() => null),
        holdingsStore.load().catch(() => null),
        fetchRecentAgentEvents(100).catch(() => []),
      ]);

      // _todayPnl + _startingNav are reactive derivations (see top of file).
      // They scope to the same `_accountFilter(... _eqAccounts)` the Equity
      // tab's Positions/Holdings summary uses, so the chart stat overlay
      // numbers and the Equity tab TOTAL row can never drift apart when the
      // operator picks an account filter.

      // Agent fires today (IST midnight boundary).
      const todayStart = istMidnightTodayAsDate();
      _firesToday = (events || []).filter((e) => {
        const k = e.kind ?? e.event_type ?? '';
        if (k !== 'agent_fire') return false;
        const t = new Date(e.timestamp ?? e.created_at ?? 0);
        return t >= todayStart;
      }).length;

      _paperOpen = Number(algoStatus.paperStatus?.open_order_count) || 0;
      _heroLoadedAt = clientTimestamp();

      // Parallel: funds + NIFTY quote.
      // _fetchEquity intentionally NOT in this batch — it has its
      // own independent 15 s poll wired in onMount so a hero-batch
      // failure can't stall the equity-curve refresh cycle.
      await Promise.all([
        fundsStore.load(),
        _fetchNifty(),
      ]);
      // Stamp after data arrives so the RefreshButton tooltip reflects
      // the completed fetch, not the start. loadHero runs via
      // visibleInterval and doesn't flip the page-level _refreshing
      // state, so this explicit set is the only way auto-poll ticks
      // surface in the timestamp.
      lastRefreshAt.set(Date.now());
    } catch (_) { /* leave previous values up */ }
  }

  async function _fetchNifty() {
    try {
      const res = await batchQuote(['NSE:NIFTY 50']);
      const q = res?.quotes?.['NSE:NIFTY 50'] ?? res?.['NSE:NIFTY 50'] ?? null;
      if (!q) return;
      // Prefer change_percent / change_pct; fall back to (ltp-close)/close*100
      if (q.change_percent != null)     { _niftyDayPct = Number(q.change_percent); return; }
      if (q.change_pct    != null)     { _niftyDayPct = Number(q.change_pct);     return; }
      const ltp   = Number(q.last_price  ?? q.ltp  ?? 0);
      const close = Number(q.ohlc?.close ?? q.close ?? 0);
      if (close > 0 && ltp > 0) _niftyDayPct = ((ltp - close) / close) * 100;
    } catch (_) { /* leave null — chip stays "—" */ }
  }

  onMount(() => {
    // Hydrate tab preferences — chart-card tab + sidebar cap/equity tab.
    const storedChartTab = readChartPref(_CHART_TAB_LS_KEY, _chartTab,
      (v) => typeof v === 'string' && _CHART_TAB_VALID.has(v));
    if (storedChartTab !== _chartTab) _chartTab = /** @type {'nav'|'intraday'|'performance'} */ (storedChartTab);

    const storedCapEqTab = readChartPref(_CAP_EQ_TAB_LS_KEY, _capEqTab,
      (v) => typeof v === 'string' && _CAP_EQ_TAB_VALID.has(v));
    if (storedCapEqTab !== _capEqTab) _capEqTab = /** @type {'nav'|'capital'|'equity'} */ (storedCapEqTab);

    _tabsHydrated = true;
    // (P&L + Agent collapse state now owned by CollapseButton via
    // its own per-user localStorage key — the old dash.pnlOpen
    // restore is retired.)
    // Restore per-card account filters from sessionStorage. Each
    // card persists under its own key so the operator's per-card
    // intent survives a tab refresh. Wrapped in try/catch since
    // the stored account codes may no longer exist on this server
    // (operator switched broker accounts) — silently fall back to
    // all-accounts on parse error.
    //
    // Important: defer restoration until AFTER the broker registry
    // load completes. We compare the stored filter against the
    // currently-loaded broker accounts and reset to "all" when a
    // new broker account has appeared since the operator last set
    // the filter. Otherwise a stale ['ZG0790','ZJ6294'] filter
    // from before Dhan / Groww accounts were added silently keeps
    // them hidden — operator sees an empty Dhan row instead of
    // the obvious "your new account is here, click to include".
    function _restore(key, /** @type {(v:string[])=>void} */ setter) {
      try {
        const cached = sessionStorage.getItem(key);
        if (!cached) return;
        const stored = JSON.parse(cached);
        if (!Array.isArray(stored) || stored.length === 0) return;
        // _knownBrokerAccounts populates via _fetchConn — may not be
        // ready yet on first restore. Fall back to "use stored as
        // is" when registry is empty; the watcher below re-runs
        // once the registry lands.
        const known = _knownBrokerAccounts;
        if (known.length === 0) {
          setter(stored);
          return;
        }
        // Drop unknown account codes (broker removed).
        const valid = stored.filter(a => known.includes(a));
        // If the operator picked a subset, but a NEW broker account
        // is now loaded that isn't covered, reset → "all" so the
        // new account is visible by default. Operator can re-narrow.
        const missingNew = known.some(a => !stored.includes(a));
        if (missingNew) {
          setter([]);            // = show all
          sessionStorage.removeItem(key);
        } else {
          setter(valid);
        }
      } catch (_) { setter([]); }
    }
    _restore('dash.eqAccounts',  v => _eqAccounts  = v);
    _restore('dash.winAccounts', v => _winAccounts = v);
    _restore('dash.losAccounts', v => _losAccounts = v);
    // Signal that onMount's synchronous restore has completed so the
    // $effect below skips the empty-registry window on first fire.
    _mountRestoreDone = true;
    // PRIMARY — positions, holdings, funds, agent events. These drive the
    // dashboard's main snapshot grids; the operator needs them before
    // anything else paints. Kick off immediately so the first network
    // round-trip starts in this microtask.
    loadHero();
    // Match the equity-curve cadence (15 s). The earlier 30 s rate
    // left the Capital card visibly stale next to the chart that
    // ticked twice between hero refreshes. Backend cycle is 60 s
    // anyway so 15 s polling is comfortably within the freshness
    // window without hammering the broker.
    // Throttle to 30 s on hidden (Option B hybrid): funds/NAV/positions
    // are critical — keep a slow heartbeat so the operator returns to
    // current numbers without a full cold-start wait.
    _heroTeardown = visibleInterval(loadHero, 15000, 'throttle:30000');
    // SECONDARY — equity-curve points + NAV chip. The equity SVG sits
    // below the snapshot grids (below the fold on mobile); the NAV chip
    // is supplementary header decoration. Deferring via setTimeout(0)
    // yields one task to the event loop so the primary loadHero() fetch
    // gets its network priority before these requests fire. Pattern
    // mirrors ChartWorkspace._loadGreeks (Tier 1 reference).
    setTimeout(() => {
      _fetchEquity();
      _equityPollStop = visibleInterval(_fetchEquity, 15000, 'throttle:30000');
      // NAV chip — single fetch, no polling. NAV moves on the 16:00 IST
      // snapshot + operator recompute; nothing changes minute-to-minute.
      _fetchNav();
    }, 0);
    // loadMarketMovers retired — the Top Winners / Top Losers cards
    // moved to /pulse (where MarketPulse owns its own movers fetch).
    // Removing the dashboard poll stops the 60s batchQuote round-trip
    // that was driving the now-deleted cards.
  });

  // Persist per-card filter changes — sessionStorage so the intent
  // survives a tab refresh but resets per session (operators don't
  // usually carry the same filter across days). One effect per
  // card so a change in one doesn't trigger writes for the others.
  $effect(() => {
    try { sessionStorage.setItem('dash.eqAccounts',  JSON.stringify(_eqAccounts));  } catch (_) {}
  });
  $effect(() => {
    try { sessionStorage.setItem('dash.winAccounts', JSON.stringify(_winAccounts)); } catch (_) {}
  });
  $effect(() => {
    try { sessionStorage.setItem('dash.losAccounts', JSON.stringify(_losAccounts)); } catch (_) {}
  });

  // book_changed bus — refetch hero (positions / holdings / events) on
  // every terminal postback. Replaces the prior "wait for next 15s
  // poll" pattern; the snapshot grid now settles in a single tick.
  let _lastBookCounter = 0;
  $effect(() => {
    const n = $bookChanged;
    if (n <= _lastBookCounter) return;
    _lastBookCounter = n;
    loadHero();
  });

  // ── ag-Grid mounts ────────────────────────────────────────────────
  // Each $effect runs once when the bound element appears in the DOM
  // (bind:this flips from null → HTMLDivElement). Subsequent
  // rowData updates flow through separate effects further down.
  //
  // Default colDef: resizable, sortable, non-movable, no header menu.
  // domLayout: 'autoHeight' so the card grows with its rows up to the
  // card's flexbox cap — keeps the grid compact when only 2 rows are
  // present and avoids reserving 250 px of empty space.
  /** @type {any} */
  const _baseGridOpts = {
    theme: 'legacy',
    defaultColDef: {
      resizable: true, sortable: true, suppressMovable: true,
      suppressHeaderMenuButton: true,
    },
    sortingOrder: ['asc', 'desc', null],
    rowHeight: 26,
    // Row identity for in-place updates. Without this, each
    // setGridOption('rowData') call tears down every row's DOM and
    // rebuilds (the dashboard polls every 30s, so the grids were
    // re-mounting on every cycle). Covers both per-symbol grids
    // (winners/losers) and per-account grids (funds, margin, equity).
    getRowId: ({ data }) => {
      if (!data) return '';
      if (data.symbol)  return String(data.symbol);
      if (data.account) return String(data.account);
      return '';
    },
  };

  $effect(() => {
    if (!_fundsEl || _fundsGrid) return;
    _fundsGrid = createGrid(_fundsEl, {
      ..._baseGridOpts,
      // Tag the pinned-bottom TOTAL row with the algo theme's
      // `totals-row` class so it inherits the amber accent +
      // background tint defined in app.css. Mirrors the PerformancePage
      // funds-grid behaviour.
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Action-first ordering: numeric figures lead, Account trails so the
        // operator's eye lands on cash / margin numbers first (per /pulse rule).
        { field: 'account', headerName: 'Account', width: 76, minWidth: 60, maxWidth: 92,
          cellClass: 'ag-col-fill' },
        { field: 'cash', headerName: 'Cash', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'collateral', headerName: 'Collateral', minWidth: 78, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'avail_margin', headerName: 'Avail Margin', minWidth: 92, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'used_margin', headerName: 'Used Margin', minWidth: 90, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size: var(--fs-md);color:var(--c-muted)">No fund data</span>',
    });
    _fundsReady = true;
  });

  $effect(() => {
    if (!_marginEl || _marginGrid) return;
    _marginGrid = createGrid(_marginEl, {
      ..._baseGridOpts,
      // Pinned-bottom TOTAL row inherits the algo theme's totals-row
      // amber-accent styling — mirrors the Funds grid pattern.
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Account-first ordering per operator preference — matches Funds
        // grid + Equity Positions/Holdings grids on the same card.
        { field: 'account', headerName: 'Account', width: 76, minWidth: 60, maxWidth: 92,
          cellClass: 'ag-col-fill' },
        { field: 'used', headerName: 'Used Margin', minWidth: 90, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        { field: 'avail', headerName: 'Avail Margin', minWidth: 92, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agAggFmt },
        mkUtilPctCol({ numericHdr: _numericHdr }),
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size: var(--fs-md);color:var(--c-muted)">No accounts connected</span>',
    });
    _marginReady = true;
  });

  // W/L grid factory — shared shape, separate instances per side.
  // Direction determines the colour of the % cell (up=green/down=red).
  function _makeWlGrid(el, kind /* 'win' | 'lose' */) {
    return createGrid(el, {
      ..._baseGridOpts,
      columnDefs: [
        // Symbol column iteratively shrunk: 110 → 72 (−35 %) → 65
        // (further −10 %). 65 px still fits the longest visible
        // F&O underlying like 'BANKNIFTY' (9 chars) at the current
        // font-size; flex:2 lets the column expand when there's room.
        { field: 'symbol', headerName: 'Symbol', minWidth: 65, flex: 2,
          pinned: 'left', cellClass: 'ag-col-fill ag-col-sym',
          sortable: true },
        { field: 'ltp', headerName: 'LTP', minWidth: 70, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell',
          valueFormatter: _agNumFmt },
        { field: 'pct', headerName: 'Day %', minWidth: 64, flex: 0.9,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: (p) => `ag-right-aligned-cell ${(p.value ?? 0) > 0 ? 'pnl-gain' : (p.value ?? 0) < 0 ? 'pnl-loss' : ''}`,
          valueFormatter: ({ value }) =>
            value == null ? '—'
            : (value > 0 ? '+' : '') + pctFmt(value) + '%',
          sort: kind === 'win' ? 'desc' : 'asc' },
      ],
      rowData: [],
      // Normal layout (not autoHeight) — the wrapper carries an
      // explicit height; ag-Grid scrolls internally. autoHeight +
      // overflow:auto on the container had ag-Grid mis-measuring
      // and rendering invisible headers on the W/L cards.
      domLayout: 'normal',
      getRowStyle: () => ({ cursor: 'pointer' }),
      onRowClicked: (ev) => _openSymbol(ev.data?.symbol),
      overlayNoRowsTemplate:
        `<span style="font-size: var(--fs-md);color:var(--c-muted)">No ${kind === 'win' ? 'winners' : 'losers'} in this bucket</span>`,
    });
  }

  $effect(() => {
    if (!_winEl || _winGrid) return;
    _winGrid = _makeWlGrid(_winEl, 'win');
    _winReady = true;
  });
  $effect(() => {
    if (!_losEl || _losGrid) return;
    _losGrid = _makeWlGrid(_losEl, 'lose');
    _losReady = true;
  });

  // Equity card — Positions Summary + Holdings Summary grids.
  // Per-account aggregates with TOTAL pinned at bottom. Same algo
  // theme classes (pnl-gain / pnl-loss / pnl-zero / totals-row /
  // ag-col-fill) as the other dashboard grids.
  $effect(() => {
    if (!_eqPosEl || _eqPosGrid) return;
    _eqPosGrid = createGrid(_eqPosEl, {
      ..._baseGridOpts,
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Account-first ordering per operator preference.
        { field: 'account', headerName: 'Account', width: 76, minWidth: 60, maxWidth: 92,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl', headerName: 'Day P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _dashDirCell('day_pnl'), valueFormatter: _agNumFmt },
        { field: 'pnl', headerName: 'P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _dashDirCell('pnl'), valueFormatter: _agNumFmt },
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size: var(--fs-md);color:var(--c-muted)">No open positions</span>',
    });
    _eqPosReady = true;
  });

  $effect(() => {
    if (!_eqHoldEl || _eqHoldGrid) return;
    _eqHoldGrid = createGrid(_eqHoldEl, {
      ..._baseGridOpts,
      getRowClass: (p) => p.node?.rowPinned === 'bottom' ? 'totals-row' : '',
      columnDefs: [
        // Account-first ordering per operator preference.
        { field: 'account', headerName: 'Account', width: 76, minWidth: 60, maxWidth: 92,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl', headerName: 'Day P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _dashDirCell('day_pnl'), valueFormatter: _agNumFmt },
        { field: 'pnl', headerName: 'P&L', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: _dashDirCell('pnl'), valueFormatter: _agNumFmt },
        { field: 'cur_val', headerName: 'Value', minWidth: 80, flex: 1,
          type: 'numericColumn', headerClass: _numericHdr,
          cellClass: 'ag-right-aligned-cell', valueFormatter: _agAggFmt },
      ],
      rowData: [],
      domLayout: 'autoHeight',
      overlayNoRowsTemplate:
        '<span style="font-size: var(--fs-md);color:var(--c-muted)">No holdings</span>',
    });
    _eqHoldReady = true;
  });

  // Row-data updates flow through here. Each $effect tracks just the
  // derivation it cares about so unrelated state changes don't churn
  // every grid.

  // Funds grid — body + TOTAL pinned at bottom.
  const _fundsBody = $derived(_funds.map(r => ({
    account:      r.account,
    cash:         Number(r.cash) || 0,
    collateral:   Number(r.collateral) || 0,
    avail_margin: Number(r.avail_margin ?? r.available_margin) || 0,
    used_margin:  Number(r.used_margin) || 0,
  })));
  const _fundsTotal = $derived([{
    account:      'TOTAL',
    cash:         _fundsBody.reduce((s, r) => s + r.cash, 0),
    collateral:   _fundsBody.reduce((s, r) => s + r.collateral, 0),
    avail_margin: _fundsBody.reduce((s, r) => s + r.avail_margin, 0),
    used_margin:  _fundsBody.reduce((s, r) => s + r.used_margin, 0),
  }]);
  $effect(() => {
    if (!_fundsReady || !_fundsGrid) return;
    _fundsGrid.setGridOption('rowData', _fundsBody);
    _fundsGrid.setGridOption('pinnedBottomRowData', _fundsTotal);
  });

  // Margin grid — same shape as the SVG donuts we retired, but as
  // a tabular view alongside Funds.
  const _marginRows = $derived(_margins.map(r => ({
    account:  r.account,
    used:     r.used,
    avail:    r.avail,
    util_pct: r.util_pct,
  })));
  // TOTAL row — sum used + avail across accounts, derive util %
  // from the totals (not an average of per-account ratios — that'd
  // double-weight small accounts). Pinned at the grid bottom.
  const _marginTotal = $derived.by(() => {
    const tu = _marginRows.reduce((s, r) => s + (Number(r.used) || 0), 0);
    const ta = _marginRows.reduce((s, r) => s + (Number(r.avail) || 0), 0);
    return [{
      account:  'TOTAL',
      used:     tu,
      avail:    ta,
      util_pct: (tu + ta) > 0 ? tu / (tu + ta) : 0,
    }];
  });
  $effect(() => {
    if (!_marginReady || !_marginGrid) return;
    _marginGrid.setGridOption('rowData', _marginRows);
    _marginGrid.setGridOption('pinnedBottomRowData',
      _marginRows.length > 0 ? _marginTotal : []);
  });

  // W/L grids — active tab's bucket → ag-Grid rows.
  const _winRowsAg = $derived.by(() => {
    const b = _winnerBuckets.find(b => b.label === _winTabLabel(_winTab));
    return (b?.rows ?? []).map(_toWlRow);
  });
  const _losRowsAg = $derived.by(() => {
    const b = _loserBuckets.find(b => b.label === _winTabLabel(_losTab));
    return (b?.rows ?? []).map(_toWlRow);
  });
  $effect(() => {
    if (!_winReady || !_winGrid) return;
    _winGrid.setGridOption('rowData', _winRowsAg);
  });
  $effect(() => {
    if (!_losReady || !_losGrid) return;
    _losGrid.setGridOption('rowData', _losRowsAg);
  });

  // Equity card — Positions Summary + Holdings Summary feeds.
  // Body rows from _positionsSummary / _holdingsSummary (already
  // account-filtered via _filterByAccount); TOTAL row from
  // _positionsTotal / _holdingsTotal is pinned at bottom.
  //
  // The 400 ms deferred refreshCells handle MUST be stored and cleared
  // on each re-fire. Without clearing, every 15 s poll queues a new
  // deferred task; after 10 min that's 40 dangling tasks per grid
  // firing in a burst after each data update — visible as lag spikes.
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _eqPosRefreshHandle = null;
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _eqHoldRefreshHandle = null;

  $effect(() => {
    if (!_eqPosReady || !_eqPosGrid) return;
    // Read reactive deps BEFORE untrack so the effect still re-fires when they change.
    const rows  = _positionsSummary;
    const total = _positionsTotal;
    untrack(() => {
      // Tick-flash: seed flash.update() for each per-account row before
      // pushing rowData. TOTAL rows excluded. Threshold 0.001 prevents
      // false flashes on identical values. Wrapped in untrack() so the
      // $state write inside flash.update() does NOT register as a dep
      // and cannot cause an infinite reactive loop.
      for (const r of rows) {
        if (r.account === 'TOTAL') continue;
        _dashFlash.update(`${r.account}:day_pnl`, Number(r.day_pnl));
        _dashFlash.update(`${r.account}:pnl`,     Number(r.pnl));
      }
      _eqPosGrid.setGridOption('rowData', rows);
      _eqPosGrid.setGridOption('pinnedBottomRowData', [total]);
      try { _eqPosGrid.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      // Clear any prior deferred refresh before scheduling a new one.
      if (_eqPosRefreshHandle != null) { clearTimeout(_eqPosRefreshHandle); _eqPosRefreshHandle = null; }
      _eqPosRefreshHandle = setTimeout(() => {
        _eqPosRefreshHandle = null;
        try { _eqPosGrid.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      }, 400);
    });
    return () => { if (_eqPosRefreshHandle != null) { clearTimeout(_eqPosRefreshHandle); _eqPosRefreshHandle = null; } };
  });
  $effect(() => {
    if (!_eqHoldReady || !_eqHoldGrid) return;
    const rows  = _holdingsSummary;
    const total = _holdingsTotal;
    untrack(() => {
      for (const r of rows) {
        if (r.account === 'TOTAL') continue;
        _dashFlash.update(`${r.account}:day_pnl`, Number(r.day_pnl));
        _dashFlash.update(`${r.account}:pnl`,     Number(r.pnl));
      }
      _eqHoldGrid.setGridOption('rowData', rows);
      _eqHoldGrid.setGridOption('pinnedBottomRowData', [total]);
      try { _eqHoldGrid.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      // Clear any prior deferred refresh before scheduling a new one.
      if (_eqHoldRefreshHandle != null) { clearTimeout(_eqHoldRefreshHandle); _eqHoldRefreshHandle = null; }
      _eqHoldRefreshHandle = setTimeout(() => {
        _eqHoldRefreshHandle = null;
        try { _eqHoldGrid.refreshCells({ columns: ['day_pnl', 'pnl'], force: true }); } catch (_) {}
      }, 400);
    });
    return () => { if (_eqHoldRefreshHandle != null) { clearTimeout(_eqHoldRefreshHandle); _eqHoldRefreshHandle = null; } };
  });

  // ── Account-multiselect scope predicate ───────────────────────────
  // The shared _selectedAccounts filter applies only to the user-scoped
  // buckets (Holdings / Positions). Market-wide tabs ignore it. We
  // disable the MultiSelect on each W/L card when the active tab
  // doesn't honour the filter so the operator doesn't think the
  // picker is silently being applied.
  const _USER_TABS = new Set(['holdings', 'positions']);
  const _winAcctDisabled = $derived(!_USER_TABS.has(_winTab));
  const _losAcctDisabled = $derived(!_USER_TABS.has(_losTab));

  onDestroy(() => {
    _heroTeardown?.(); _equityPollStop?.();
    _dashFlash.dispose();
    _fundsGrid?.destroy();  _marginGrid?.destroy();
    _eqPosGrid?.destroy();  _eqHoldGrid?.destroy();
    _winGrid?.destroy();    _losGrid?.destroy();
  });

</script>

<svelte:head>
  <title>Dashboard | RamboQuant Analytics</title>
</svelte:head>

<!-- Page header -->
<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Dashboard</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <!-- Slice 7h — strategy filter chip. Same shared store as Pulse,
         /orders, /admin/derivatives. Active strategy narrows the
         dashboard's positions / holdings rows (via the _positions /
         _holdings derivations that override the raw arrays). Hidden
         when no strategies exist. -->
    <StrategyPicker label="Strategy" />
    <RefreshButton onClick={_refreshAll} loading={_refreshing} label="dashboard" />
    <PageHeaderActions />
  </span>
</div>

<!-- Firm NAV chip moved (Jun 2026): no longer a dedicated row here.
     The chip now overlays the NAV chart inside the chart card's NAV
     tab — operator: "move nav chip as an overlay in nav chart in
     dashboard". `_navLatest` / `_navDelta` / `_navDeltaPct` are
     still fetched here (gated by view_nav) and forwarded into
     <NavTab> as props so the overlay self-hides on observer
     accounts without the cap. The chip is hidden on Intraday +
     Performance tabs by design — those panels don't carry NAV
     inline. -->


<!-- Hero strip retired. Its six chips lived elsewhere:
       - P&L TODAY / TODAY % / vs NIFTY → stat overlay inside the
         Intraday equity-curve panel
       - AGENT FIRES → Agent activity card header (already shows
         "fires today")
       - PAPER OPEN → navbar PAPER banner ("N open chase orders ·
         fake fills against live quotes")
       - CONN → badge on every RefreshButton (cyan + count, green/
         amber/red by broker-account health)
     `_pnlClass / _todayPctClass / _vsNiftyClass / _todayPnl /
     _todayPct / _vsNifty / _niftyDayPct / _firesToday / _paperOpen
     / _conn / _connIcon / _connClass / _heroLoadedAt` derivations
     are preserved so the stat overlay + future surfaces can keep
     reading them. -->


<!-- Open orders strip — hidden when nothing is chasing -->
{#if _openOrders.length > 0}
  <div class="dash-open-orders">
    <div class="oo-header">
      <span class="mp-section-label mp-section-label--bar">OPEN ORDERS</span>
      <span class="oo-count">
        <span class="oo-dot" aria-hidden="true"></span>
        {_openOrders.length} chasing
      </span>
    </div>
    <div class="oo-pills">
      {#each _openOrders as ord}
        {@const isBuy = (ord.side ?? '').toUpperCase() === 'BUY'}
        <a
          href="/orders{ord.order_id ? `?order_id=${encodeURIComponent(ord.order_id)}` : ''}"
          class="oo-pill {isBuy ? 'oo-pill-buy' : 'oo-pill-sell'}"
        >
          <span class="oo-side">{isBuy ? 'BUY' : 'SELL'}</span>
          <span class="oo-qty">{ord.qty ?? ord.quantity ?? ''}</span>
          <span class="oo-sym">{formatSymbol(ord.symbol ?? ord.tradingsymbol ?? '')}</span>
          <span class="oo-price">@ ₹{priceFmt(ord.limit_price ?? ord.price ?? 0)}</span>
          {#if (ord.attempts ?? 0) > 0}
            <span class="oo-attempts">({ord.attempts})</span>
          {/if}
        </a>
      {/each}
    </div>
  </div>
{/if}

<!-- Row 1 (split): Intraday Equity Curve LEFT half, NAV/Capital/Equity
     tabbed card RIGHT half. Operator-requested shuffle (Jun 2026): the
     chart card moves to the left and the portfolio sidebar (NAV +
     Capital + Equity tabs) moves to the right. NAV is the new default
     tab on the sidebar — the dashboard's first-glance answer to
     "what's the firm worth right now?" mirrors PerformancePage's NAV
     grid. Capital answers "can I take risk?", Equity drills into
     positions / holdings. The sidebar card flexes its height to fit
     whichever tab is active. Stacks on mobile. -->
<div class="dash-row1-split">

  <!-- LEFT: NAV / Intraday / Performance tabbed card. NAV is the
       default — operator-driven restore (Jun 2026) after a refactor
       briefly moved it off this card; the NAV history curve is the
       chart card's headline view. Intraday surfaces today's cumulative
       P&L curve; Performance hosts the historical drill-down
       (PnlAnalysis component) one click away. All three panels stay
       mounted (hidden, not {#if}) so internal state — including
       PnlAnalysis filters + benchmark series and NavTab's fetched
       history — persists across tab flips. The page-header firm-NAV
       chip is visible from every tab, so the NAV TOTAL number is on
       screen even when the operator is reading Intraday / Performance. -->
  <section class="bucket-card row1-col-chart"
    class:fs-card-on={_fsEquityCurve}
    class:is-collapsed={_colEquityCurve}>
    <CardHeader
      bind:isCollapsed={_colEquityCurve}
      bind:isFullscreen={_fsEquityCurve}
      label="Chart"
      onRefresh={_refreshAll}
      bind:refreshLoading={_refreshing}
      showSearch={false}
      detectOverflow={false}
    >
      <!-- No cardId — collapse state resets to expanded on every page
           load. Operator can still toggle in-session. Matches the
           "no card collapsed if there's no data by default" rule
           applied across the dashboard. -->
      {#snippet middle()}
        <AlgoTabs
          tabs={[
            { id: 'nav',         label: 'NAV'         },
            { id: 'intraday',    label: 'Intraday'    },
            { id: 'performance', label: 'Performance' },
          ]}
          bind:value={_chartTab}
          compact={true}
        />
      {/snippet}
    </CardHeader>

    <!-- NAV panel — firm NAV history curve (NavTab). Default tab.
         Polls /api/nav/history?days=90 on a market-aware interval;
         renders the hand-rolled amber SVG line. Lives ABOVE the
         Intraday + Performance panels so the operator's first glance
         on the chart card is the firm's net-liq trajectory. -->
    <div class="card-body" hidden={_chartTab !== 'nav' || _colEquityCurve}>
      {#if _navFetchError}
        <!-- Chip-fetch error strip — shown when /api/nav/latest fails.
             Small banner above the chart so the curve (if available) still
             renders and the operator has a clear Retry call-to-action. -->
        <div class="dash-nav-err" role="alert" data-testid="dash-nav-error">
          <span aria-hidden="true">⚠</span>
          <span>NAV chip unavailable — {_navFetchError}</span>
          <button class="dash-nav-retry" onclick={() => _fetchNav()}>Retry</button>
        </div>
      {/if}
      <NavTab
        chipLatest={_canViewNav ? _navLatest : null}
        chipDelta={_navDelta}
        chipDeltaPct={_navDeltaPct}
      />
    </div>

    <!-- Intraday panel — SVG curve of today's cum P&L. -->
    <div class="card-body" hidden={_chartTab !== 'intraday' || _colEquityCurve}>
    {#if !_equityPoints.length}
      <div class="eq-empty">
        No data yet — markets open at 09:15 IST
      </div>
    {:else}
      <!-- Series legend / toggle strip — sits ABOVE the chart so it
           doesn't fight the stat overlay. Each chip carries the
           matching series colour as background tint + border so the
           chip → curve mapping reads at a glance. -->
      <div class="eq-legend" role="group" aria-label="Equity chart series">
        {#each _EQ_SERIES as s (s.id)}
          <button
            type="button"
            class="eq-chip eq-chip-{s.id}"
            class:eq-chip-on={_eqSeriesOn[s.id]}
            aria-pressed={_eqSeriesOn[s.id]}
            title="{s.title} — click to {_eqSeriesOn[s.id] ? 'hide' : 'show'}"
            onclick={() => _eqSeriesOn[s.id] = !_eqSeriesOn[s.id]}>
            <svg class="eq-chip-swatch" width="18" height="6" aria-hidden="true">
              <line x1="0" y1="3" x2="18" y2="3"
                stroke={s.color} stroke-width={s.width}
                stroke-dasharray={s.dash || ''} stroke-linecap="round" />
            </svg>
            <span class="eq-chip-label">{s.label}</span>
          </button>
        {/each}
      </div>
      <!-- Chart frame — wraps SVG + stat overlay so .eq-stats anchors
           to the chart area (not the card-body); the legend strip above
           stays clear of any stat-overlay overlap. -->
      <div class="eq-chart-frame {_eqPulse.classOf('eq')}">
      <!-- Stat overlay — at-a-glance P&L numerics so the operator
           doesn't need a separate hero strip. Pointer-events: none
           so SVG hover / zoom never blocks. Same pattern OptionsPayoff
           uses. -->
      <div class="eq-stats" aria-hidden="true">
        <div class="eq-stat">
          <span class="eq-stat-k">P&amp;L TODAY</span>
          <span class="eq-stat-v {_pnlClass}">
            {#if _todayPnl == null}—{:else}{_todayPnl >= 0 ? '+' : ''}₹{priceFmt(_todayPnl)}{/if}
          </span>
          <span class="eq-stat-scope">{_eqAccounts.length === 0 ? 'All accounts' : _eqAccounts.join(', ')}</span>
        </div>
        <div class="eq-stat">
          <span class="eq-stat-k">TODAY %</span>
          <span class="eq-stat-v {_todayPctClass}">
            {#if _todayPct == null}—{:else}{_todayPct >= 0 ? '+' : ''}{pctFmt(_todayPct)}%{/if}
          </span>
        </div>
        <div class="eq-stat">
          <span class="eq-stat-k">vs NIFTY</span>
          <span class="eq-stat-v {_vsNiftyClass}">
            {#if _vsNifty == null}—{:else}{_vsNifty >= 0 ? '+' : ''}{pctFmt(_vsNifty)}%{/if}
          </span>
        </div>
      </div>
      <svg
        class="eq-svg"
        viewBox="0 0 {CHART_W} {CHART_H}"
        preserveAspectRatio="none"
        role="img"
        aria-label="Intraday cumulative P&L curve"
        onmousemove={_eqMouseMove}
        onmouseleave={_eqMouseLeave}
      >
        <!-- Grid lines (horizontal) -->
        {#each [0.0, 0.25, 0.5, 0.75, 1.0] as frac}
          {@const gy = PAD_T + frac * INNER_H}
          <line class="chart-grid-line" x1={PAD_L} y1={gy} x2={PAD_L + INNER_W} y2={gy} />
        {/each}

        <!-- Grid lines (vertical) — at x-axis label positions, behind data -->
        {#each _eqXLabels as lbl}
          <line class="chart-grid-line-minor"
            x1={parseFloat(lbl.x)} y1={PAD_T}
            x2={parseFloat(lbl.x)} y2={PAD_T + INNER_H} />
        {/each}

        <!-- Zero baseline (dotted) -->
        {#if _eqZeroY != null}
          <line class="chart-grid-zero"
            x1={PAD_L} y1={_eqZeroY} x2={PAD_L + INNER_W} y2={_eqZeroY} />
        {/if}

        <!-- Filled area — only when a single series is enabled. Multi-
             series mode skips the fill so the lines stay readable. -->
        {#if _eqAreaPath}
          <path d={_eqAreaPath} fill={_eqFillColor ?? 'none'} class="data-path"/>
        {/if}

        <!-- Lines — one polyline per active series. Order in the SVG
             matches _EQ_SERIES order so the legend layers visually. -->
        {#each _eqActiveSeries as s (s.id)}
          {#if s.points}
            <polyline
              points={s.points}
              fill="none"
              stroke={s.color}
              stroke-width={s.width}
              stroke-dasharray={s.dash || ''}
              stroke-linejoin="round"
              stroke-linecap="round"
              class="data-path" />
          {/if}
        {/each}

        <!-- Y-axis labels (right) -->
        {#each _eqYLabels as lbl}
          <text
            x={PAD_L + INNER_W + 4} y={parseFloat(lbl.y) + 3.5}
            font-size="11" font-weight="600" fill="#c8d8f0" style="font-family: var(--font-numeric)"
            text-anchor="start">{lbl.label}</text>
        {/each}

        <!-- X-axis text labels intentionally removed — dashboard chart
             reads as a clean trajectory; hover tooltip still surfaces
             the exact timestamp on demand. Grid lines stay for visual
             rhythm. -->

        <!-- Hover crosshair -->
        {#if _hoverPt != null}
          <line
            x1={_hoverX} y1={PAD_T} x2={_hoverX} y2={PAD_T + INNER_H}
            stroke="rgba(200,216,240,0.55)" stroke-width="1"
            stroke-dasharray="3 2" />
          <circle cx={_hoverX} cy={_hoverY} r="3"
            fill={_eqLineColor ?? 'var(--algo-sky)'} stroke="#0a1428" stroke-width="1.5" />
          <!-- Tooltip box -->
          {@const _tipX = _hoverX > INNER_W * 0.65 ? _hoverX - 108 : _hoverX + 8}
          {@const _tipY = Math.max(PAD_T, Math.min(_hoverY - 28, PAD_T + INNER_H - 58))}
          <rect x={_tipX} y={_tipY} width="100" height="54"
            rx="3" fill="rgba(10,20,40,0.92)"
            stroke="rgba(126,151,184,0.35)" stroke-width="1" />
          {#if _hoverPt}
            {@const _ist = new Date(new Date(_hoverPt.ts).getTime() + 5.5*3600*1000)}
            {@const _th = String(_ist.getUTCHours()).padStart(2,'0')}
            {@const _tm = String(_ist.getUTCMinutes()).padStart(2,'0')}
            <text x={_tipX + 6} y={_tipY + 13}
              font-size="8.5" fill="var(--algo-sky)" style="font-family: var(--font-numeric)">{_th}:{_tm} IST</text>
            <text x={_tipX + 6} y={_tipY + 26}
              font-size="8" fill="var(--c-muted)" style="font-family: var(--font-numeric)">Day P&amp;L</text>
            <text x={_tipX + 6} y={_tipY + 37}
              font-size="9" font-weight="700" fill={_hoverPt.day_pnl >= 0 ? 'var(--c-long)' : 'var(--c-short)'}
              style="font-family: var(--font-numeric); font-variant-numeric: tabular-nums">
              {_hoverPt.day_pnl >= 0 ? '+' : ''}₹{priceFmt(_hoverPt.day_pnl)}
            </text>
            <text x={_tipX + 6} y={_tipY + 49}
              font-size="9" font-weight="700" fill={_hoverPt.cum_pnl >= 0 ? 'var(--c-long)' : 'var(--c-short)'}
              style="font-family: var(--font-numeric); font-variant-numeric: tabular-nums">
              cum {_hoverPt.cum_pnl >= 0 ? '+' : ''}₹{priceFmt(_hoverPt.cum_pnl)}
            </text>
          {/if}
        {/if}
      </svg>
      </div><!-- /eq-chart-frame -->
    {/if}
    </div>

    <!-- Performance panel — historical P&L drill-down. Renders
         PnlAnalysis which owns its own filters, summary, and chart.
         hasData flows back so the parent can auto-collapse this card
         when both Intraday + Performance are empty. -->
    <div class="card-body" hidden={_chartTab !== 'performance' || _colEquityCurve}>
      <PnlAnalysis bind:hasData={_pnlHasData} />
    </div>
  </section>

  <!-- RIGHT: NAV | Capital | Equity tabbed card (operator-requested
       shuffle, Jun 2026). NAV is the new default tab — it surfaces
       the per-account NAV breakdown using the same arithmetic as
       PerformancePage's NAV grid + backend/api/algo/nav.py:compute_firm_nav
       so the dashboard's first-glance "what's the firm worth?" can't
       drift from the canonical /performance view. Capital + Equity
       sit behind one click each. All three panels stay mounted
       (hidden, not {#if}) so ag-Grid instances don't orphan when the
       operator flips tabs. The card uses `flex: 1 1 auto` so its
       height expands / contracts with the chart card on the left —
       no more dead space when Capital tab only has 1 row. -->
  <!-- card-theme-dark is applied at layout level (.algo-viewport) so
       NavCard / PerformancePage embeds on any algo route automatically
       inherit the Bloomberg-dark palette. NavBreakdown uses --algo-*
       vars and is unaffected. -->
  <section class="bucket-card cap-eq-tabbed"
    class:fs-card-on={_fsNavBd || _fsCapital || _fsEquity}
    class:is-collapsed={_colNavBd && _colCapital && _colEquity}>
    <div class="bucket-header">
      <AlgoTabs
        tabs={[
          { id: 'nav',     label: 'NAV'     },
          { id: 'capital', label: 'Capital' },
          { id: 'equity',  label: 'Equity'  },
        ]}
        bind:value={_capEqTab}
        compact={true}
      />
      <!-- Single shared account picker — applies to whichever tab is
           active. NAV per-account breakdown, Capital's Margin + Funds
           rows, Equity's Positions + Holdings summaries all scope by
           this filter. Operator intent carries across tab flips
           without re-picking. -->
      <AccountMultiSelect
        bind:value={_eqAccounts}
        options={_availableAccounts.map(a => ({ value: a, label: a }))} />
      <!-- Explicit `flex:1` spacer pushes the icon trio to the card's
           right edge regardless of how wide the AccountMultiSelect
           grows. Same idiom as before the NAV-tab addition. -->
      <span class="cap-eq-spacer"></span>
      <!-- Buttons bind to the ACTIVE tab's own collapse + fullscreen
           pair. Svelte 5 doesn't permit ternary expressions inside
           `bind:`, so we split into per-tab component instances. -->
      {#if _capEqTab === 'nav'}
        <CardControls
          bind:isCollapsed={_colNavBd}
          bind:isFullscreen={_fsNavBd}
          label="NAV"
          onRefresh={_refreshAll}
          bind:refreshLoading={_refreshing}
          showSearch={false}
          onDownload={() => _navBdRef?.downloadCsv?.()}
        />
      {:else if _capEqTab === 'capital'}
        <CardControls
          bind:isCollapsed={_colCapital}
          bind:isFullscreen={_fsCapital}
          label="Capital"
          onRefresh={_refreshAll}
          bind:refreshLoading={_refreshing}
          showSearch={false}
          onDownload={() => {
            _marginGrid?.exportDataAsCsv({ fileName: 'margin.csv' });
            _fundsGrid?.exportDataAsCsv({ fileName: 'funds.csv' });
          }}
        />
      {:else}
        <CardControls
          bind:isCollapsed={_colEquity}
          bind:isFullscreen={_fsEquity}
          label="Equity"
          onRefresh={_refreshAll}
          bind:refreshLoading={_refreshing}
          showSearch={false}
          onDownload={() => {
            _eqPosGrid?.exportDataAsCsv({ fileName: 'positions.csv' });
            _eqHoldGrid?.exportDataAsCsv({ fileName: 'holdings.csv' });
          }}
        />
      {/if}
    </div>

    <!-- NAV panel — per-account breakdown. Same arithmetic as
         PerformancePage's `navByAcct`; sourced from the same module-
         level marketDataStores so a single broker fetch warms both
         surfaces and they can't drift. -->
    <div class="card-body" hidden={_capEqTab !== 'nav' || _colNavBd}>
      <NavBreakdown bind:this={_navBdRef} accountFilter={_eqAccounts} />
    </div>

    <!-- Capital panel -->
    <div class="card-body" hidden={_capEqTab !== 'capital' || _colCapital}>
      {#if _marginRows.length > 0}
        <div class="bucket-subheader">Margin Utilisation</div>
      {/if}
      <div
        bind:this={_marginEl}
        class="ag-theme-quartz ag-theme-algo dash-mini-grid"
        class:is-empty={_marginRows.length === 0}></div>

      {#if _fundsBody.length > 0}
        <div class="bucket-subheader bucket-subheader-spaced">Funds</div>
      {/if}
      <div
        bind:this={_fundsEl}
        class="ag-theme-quartz ag-theme-algo dash-mini-grid"
        class:is-empty={_fundsBody.length === 0}></div>

      {#if _marginRows.length === 0 && _fundsBody.length === 0}
        <EmptyState message="No accounts connected" />
      {/if}
    </div>

    <!-- Equity panel -->
    <div class="card-body" hidden={_capEqTab !== 'equity' || _colEquity}>
      {#if _positionsSummary.length > 0}
        <div class="bucket-subheader">
          Positions
          <span class="eq-count">{_positionsCount}</span>
        </div>
      {/if}
      <div
        bind:this={_eqPosEl}
        class="ag-theme-quartz ag-theme-algo dash-mini-grid"
        class:is-empty={_positionsSummary.length === 0}></div>

      {#if _holdingsSummary.length > 0}
        <div class="bucket-subheader bucket-subheader-spaced">
          Holdings
          <span class="eq-count">{_holdingsCount}</span>
        </div>
      {/if}
      <div
        bind:this={_eqHoldEl}
        class="ag-theme-quartz ag-theme-algo dash-mini-grid"
        class:is-empty={_holdingsSummary.length === 0}></div>

      {#if _positionsSummary.length === 0 && _holdingsSummary.length === 0}
        <EmptyState message="No equity exposure" />
      {/if}
    </div>
  </section>
</div>


<!-- Row 2 retired (Top Winners + Top Losers cards moved to /pulse,
     where they sit in the 6-grid layout alongside the rest of the
     monitoring surfaces). Dashboard now jumps straight from the
     equity card to the activity strip. -->

<!-- Row 3: Activity card — replaces the standalone Market News strip
     per operator (Jun 2026). The Activity surface's News tab is now
     the default-active tab so the dashboard still lands on the same
     market headlines flow, but a click switches to Orders / Agents /
     Terminal / Conn / System / Ticks for the wider operator paper
     trail without leaving the page. Same composition as /activity +
     ActivityLogModal + the /orders Activity card — single shared
     ActivityHeaderFilters + ActivityLogSurface pair, so the four
     mounts can't drift on filter UI or LogPanel config. -->
<section class="bucket-card dash-activity"
  class:fs-card-on={_fsActivity}
  class:is-collapsed={_colActivity}>
  <div class="card-body" hidden={_colActivity}>
    <!-- ActivityLogSurface with label="ACTIVITY" so LogPanel renders
         its own tab-row header (label chip, filters, card buttons).
         The row3-header div is removed — LogPanel owns its chrome. -->
    <ActivityLogSurface
      defaultTab="news"
      context="card-wide"
      label="ACTIVITY"
      cardId="dash-activity"
      onRefresh={_refreshAll}
      bind:isCollapsed={_colActivity}
      bind:isFullscreen={_fsActivity}
      bind:accountFilter={_actAccountFilter}
      bind:availableAccounts={_actAvailableAccounts}
      bind:levelFilter={_actLevelFilter} />
  </div>
</section>

<!-- P&L Analysis section retired — PnlAnalysis now lives inside the
     row-1 Intraday/Performance tabbed card. Dropping the standalone
     full-width section keeps the page compact and removes the duplicate
     mount. -->

<!-- SymbolPanel — opened by winners/losers tile clicks -->
{#if _ticketProps}
  <SymbolPanel
    {..._ticketProps}
    onClose={() => { _ticketProps = null; }}
    onSubmit={() => { _ticketProps = null; }} />
{/if}

<!-- Agent activity — same CollapseButton pattern as every other card.
     Default collapsed; CollapseButton restores from localStorage if
     the operator's last state was expanded. -->
<section class="bucket-card dash-agent"
  class:fs-card-on={_fsAgent}
  class:is-collapsed={_colAgent}>
  <CardHeader
    title="AGENT ACTIVITY"
    bind:isCollapsed={_colAgent}
    bind:isFullscreen={_fsAgent}
    label="Agent activity"
    onRefresh={_refreshAll}
    bind:refreshLoading={_refreshing}
    showSearch={false}
  >
    <!-- Default expanded — was previously `initialCollapsed=true` so
         the empty-state "No agent fires yet today" was hidden behind
         a collapse the operator had to click to discover. -->
    {#snippet left()}
      <span class="dash-agent-chip">
        <span class="dash-agent-count">{_firesToday}</span>
        <span class="dash-agent-label">fires today</span>
      </span>
    {/snippet}
  </CardHeader>
  <div class="card-body" hidden={_colAgent}>
    <ActionEventsToggle bind:value={_agentLogShowActions} />
    <UnifiedLog
      filter={{ kinds: _agentLogKinds }}
      excludeSim={true}
      maxRows={30}
      pollMs={15000}
      emptyMessage="No agent fires yet today." />
  </div>
</section>

<style>
  /* Firm-NAV chip styles retired here — chip moved into NavTab as an
     overlay (Jun 2026). See frontend/src/lib/NavTab.svelte for the
     `.nav-chip-overlay` rules. */

  /* .mp-section-label is defined globally in app.css.
     Dashboard uses the --bar modifier for the amber left-edge accent. */

  /* ── Hero row ────────────────────────────────────────────────────── */
  /* All card-shaped sections on this page (hero row, row1 cols, wl
     tiles, news strip, collapsible summaries) inherit the canonical
     algo-status-card chrome — gradient bg + 1.5px border + box-shadow.
     Match the visual depth of /automation, /admin/derivatives, /admin/execution
     so the dashboard doesn't read as one-generation-back. */
  /* Hero strip CSS retired alongside its markup — the three pnl
     stat classes the stat overlay consumes (`hero-pnl-up`,
     `hero-pnl-down`, `hero-pnl-neutral`) live alongside `.eq-stat-v`
     above. */

  /* Refresh chip per-card header. Same palette as .hero-refresh but
     sized to sit alongside the section label without pushing the
     Collapse / Fullscreen buttons off the right edge. Margin-left
     auto pins it after the label; the buttons that follow get a
     small left gap so they don't sit flush against the timestamp. */
  /* .bucket-refresh-chip retired (was an "updated Xs ago" chip on
     /pulse buckets; replaced by the RefreshButton spinner state). */

  /* ── Row 1 (split): Capital/Equity tabbed card + Equity curve ───── */
  /* Earlier the equity curve filled a full-width hero row and Capital
     / Equity sat below at 1:1. The split layout keeps Capital + Equity
     + curve all in one glance — Bloomberg PRTU's portfolio sidebar
     beside the chart. Stacks below 1024 px. */
  .dash-row1-split {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.6rem;
    margin-bottom: 0.75rem;
    /* `align-items: stretch` (grid default) lets each cell's card
       stretch to the row's max height — so the NAV/Capital/Equity
       card on the right grows to match the chart card on the left,
       and shrinks together when both are collapsed. Operator-
       requested expand/contract behaviour. */
    align-items: stretch;
  }
  @media (min-width: 1024px) {
    .dash-row1-split {
      grid-template-columns: 1fr 1fr;
    }
  }
  /* Each grid cell card stretches to the row's height. Internal flex
     direction lets the tab bodies fill the remaining vertical room
     when one tab's content is shorter than another. */
  .dash-row1-split > .bucket-card {
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .dash-row1-split > .bucket-card > .card-body {
    flex: 1 1 auto;
    min-height: 0;
  }
  /* .row1-col chrome migrated to canonical .bucket-card (which carries
     the same gradient, border, radius, shadow + the amber 3px left
     accent that every other card now uses). Only the local class is
     kept for `.row1-col-chart .card-body` positioning below. */
  /* Tabbed NAV | Capital | Equity card — tab buttons now rendered by
     AlgoTabs (global .algo-tab rules in app.css). */
  .cap-eq-tabbed { display: flex; flex-direction: column; }

  /* Equity curve */
  /* Series-toggle legend — sits above the SVG, narrow row of chips with
     a colored stroke swatch + label. Click toggles the matching curve
     on the chart. Each chip carries its series colour as background
     tint + border so the chip ↔ curve mapping reads at a glance.
     Wraps onto a second row on mobile so 6 chips fit. */
  .eq-legend {
    display: flex;
    flex-wrap: nowrap;
    gap: 0.35rem;
    padding: 0.25rem 0.1rem 0.5rem;
    align-items: center;
    overflow-x: auto;
    scrollbar-width: none;
    -webkit-overflow-scrolling: touch;
  }
  .eq-legend::-webkit-scrollbar { display: none; }
  .eq-legend > * { flex-shrink: 0; }
  .eq-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.42rem;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 3px;
    padding: 0.22rem 0.55rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 600;
    color: rgba(200, 216, 240, 0.55);
    cursor: pointer;
    line-height: 1;
    white-space: nowrap;
    transition: background 0.1s, border-color 0.1s, color 0.1s;
  }
  .eq-chip-swatch { display: block; flex-shrink: 0; }
  .eq-chip-label  { display: inline-block; }
  .eq-chip:hover { background: rgba(255, 255, 255, 0.06); color: var(--algo-slate); }
  /* Off state — swatch fades so the operator scans 'what's drawn' by
     chip brightness alone. */
  .eq-chip:not(.eq-chip-on) .eq-chip-swatch { opacity: 0.35; }

  /* Per-series ON-state palette — chip's background + border + text
     all pick up the matching curve colour at low alpha so toggling a
     chip on visually highlights it among its siblings. Holdings family
     in sky-blue, positions in amber, combined in green. */
  .eq-chip-H.eq-chip-on,
  .eq-chip-dH.eq-chip-on {
    background: var(--algo-sky-bg);
    border-color: var(--algo-sky-border);
    color: var(--algo-sky);
  }
  .eq-chip-H.eq-chip-on:hover,
  .eq-chip-dH.eq-chip-on:hover { background: var(--algo-sky-bg-strong); }

  .eq-chip-P.eq-chip-on,
  .eq-chip-dP.eq-chip-on {
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border);
    color: var(--c-action);
  }
  .eq-chip-P.eq-chip-on:hover,
  .eq-chip-dP.eq-chip-on:hover { background: var(--algo-amber-bg-strong); }

  .eq-chip-comb.eq-chip-on,
  .eq-chip-dComb.eq-chip-on {
    background: rgba(74, 222, 128, 0.14);
    border-color: var(--algo-green-border);
    color: var(--c-long);
  }
  .eq-chip-comb.eq-chip-on:hover,
  .eq-chip-dComb.eq-chip-on:hover { background: var(--algo-green-bg-strong); }

  /* Chart frame — positioned wrapper around the SVG so the
     `.eq-stats` absolute overlay anchors to the chart area, not the
     card-body. Without this the stats floated up over the legend
     strip when the SVG was pushed down. */
  .eq-chart-frame {
    position: relative;
    width: 100%;
  }

  .eq-svg {
    --eq-line-up: var(--c-long);
    --eq-line-down: var(--c-short);
    display: block;
    width: 100%;
    height: 220px;
    cursor: crosshair;
    overflow: visible;
  }
  /* Fullscreen card → chart fills the viewport. Same idiom OptionsPayoff
     + PriceChart use. */
  .fs-card-on .eq-svg {
    height: calc(100vh - 10rem) !important;
    min-height: 320px;
  }
  /* Stat overlay magnifies in step with the chart so the P&L numerics
     stay readable at viewport size. */
  .fs-card-on .eq-stats {
    gap: 1.4rem;
    top: 0.6rem;
    left: 0.7rem;
  }
  .fs-card-on .eq-stat-k { font-size: var(--fs-lg); margin-bottom: 0.32rem; }
  .fs-card-on .eq-stat-v { font-size: 1.4rem; }
  @media (max-width: 600px) {
    .fs-card-on .eq-svg {
      height: calc(100vh - 8rem) !important;
    }
    .fs-card-on .eq-stats { gap: 0.8rem; }
    .fs-card-on .eq-stat-k { font-size: var(--fs-sm); }
    .fs-card-on .eq-stat-v { font-size: var(--fs-xl); }
  }
  /* Stat overlay — at-a-glance P&L numerics (P&L TODAY · TODAY % ·
     vs NIFTY) anchored top-left inside the chart card body. Same
     pointer-events:none HTML overlay pattern OptionsPayoff uses;
     never blocks SVG hover / zoom. Replaces the retired top-of-page
     `.hero-row` chips. */
  .eq-stats {
    position: absolute;
    top: 0.3rem;
    left: 0.4rem;
    display: flex;
    gap: 0.65rem;
    font-family: var(--font-numeric);
    pointer-events: none;
    z-index: 2;
  }
  .eq-stat {
    display: inline-flex;
    flex-direction: column;
    line-height: 1;
  }
  .eq-stat-k {
    font-size: var(--fs-2xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(200, 216, 240, 0.6);
    margin-bottom: 0.18rem;
  }
  .eq-stat-v {
    font-size: var(--fs-lg);
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--algo-slate);
  }
  .eq-stat-v.hero-pnl-up   { color: var(--c-long); }
  .eq-stat-v.hero-pnl-down { color: var(--c-short); }
  .eq-stat-v.hero-pnl-neutral { color: rgba(200, 216, 240, 0.6); }
  .eq-stat-scope {
    font-size: var(--fs-2xs);
    color: rgba(200,216,240,0.45);
    margin-top: 0.12rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 5rem;
  }
  /* Card body is the overlay's positioning context — make it relative
     so .eq-stats anchors inside the chart panel. */
  .row1-col-chart .card-body { position: relative; }
  .eq-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    /* Compact empty state — earlier reserved 220 px which left a
       big blank band on cold-start or pre-market. 3 rem fits the
       single-line message and lets the rest of the dashboard pull
       up. The curve grows back to 220 px once data lands. */
    height: 3rem;
    color: var(--c-muted);
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    letter-spacing: 0.04em;
  }

  /* .gauge-* family retired — SVG donut gauges replaced by ag-Grid
     Margin Utilisation table on Capital tab.
     .dash-buckets retired — Capital/Equity moved into the
     `dash-row1-split` 2-col grid above. */

  .bucket-card {
    /* Local override targets only the dashboard's padding (slightly
       tighter than the global 0.55/0.65/0.6/0.8). All other chrome
       (gradient, border, radius, shadow, amber 3px left accent)
       inherits from app.css's canonical .bucket-card so the
       dashboard cards share the same edge accent as every other
       page (operator: "be consistent"). */
    padding: 0.65rem 0.75rem 0.7rem;
  }
  .bucket-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
  }
  .bucket-subheader {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--c-muted);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-bottom: 0.35rem;
  }
  .bucket-subheader-spaced { margin-top: 0.7rem; }

  /* AccountMultiSelect (.ams) is LEFT-aligned in the bucket header,
     sitting right after the tabs / label. The card-control trio gets
     pushed right via the explicit `.cap-eq-spacer` below.
     Was: margin-left:auto on .ams (right-aligned next to the icon
     cluster) — moved per operator feedback so the picker reads as
     part of the card's identity strip, not part of the controls. */
  .bucket-header > :global(.ams) { margin-left: 0; }

  /* Flex spacer between the AccountMultiSelect and the trailing
     control trio (Refresh? + Collapse + DefaultSize + Fullscreen).
     The trio's individual `margin: 0 0 0 auto` rules each push
     right; with two buttons carrying auto-margins the free space
     splits between them and the trio drifts mid-row. An explicit
     `flex: 1` spacer collapses the auto-margin behaviour into a
     single rightward push and guarantees the trio sits at the
     card's right edge. Zero visible width — pure layout glue. */
  .cap-eq-spacer { flex: 1 1 0; }

  /* Inline count chip used inside Equity sub-headings (Positions
     and Holdings). Small muted pill — at-a-glance "how many", not
     a primary action. Reads as a subordinate of the sub-heading
     label, not a separate element. */
  .eq-count {
    display: inline-flex;
    align-items: center;
    padding: 0.05rem 0.32rem;
    margin-left: 0.35rem;
    border-radius: 8px;
    background: rgba(126, 151, 184, 0.18);
    color: var(--algo-slate);
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0;
    font-variant-numeric: tabular-nums;
    text-transform: none;
  }

  /* Compact HTML tables — Funds / Positions Summary / Holdings
     Summary all share this treatment. Hairline column rules,
     right-aligned monospace numbers, muted TOTAL row at the
     bottom. ag-Grid is overkill for 2-4 rows. */
  /* .cap-table[-wrap|*] family retired — HTML tables replaced by
     the Funds + Margin ag-Grid mini-grids on the Capital tab.
     .dash-pnl-full retired — PnlAnalysis moved into the
     Intraday / Performance tabbed card. */

  /* Agent log — chrome now lives on the .bucket-card outer class.
     Local rules cover only the margin + the inner header-row layout
     (which uses the same `.dash-agent-summary` class but no longer
     duplicates the card chrome that .bucket-card supplies). */
  .dash-agent {
    margin-top: 0.6rem;
  }
  .dash-agent-summary::-webkit-details-marker { display: none; }
  .dash-agent-chip {
    display: inline-flex;
    /* Center-align the count + label vertically — baseline-align made
       the chip's visual center sit lower than the "AGENT ACTIVITY"
       label in the same flex row (the large count's baseline pulled
       the chip's content down). center-align matches the rest of the
       row chrome (.mp-section-label, CollapseButton, FullscreenButton). */
    align-items: center;
    gap: 0.3rem;
    padding: 0.15rem 0.5rem;
    border-left: 2px solid var(--c-action);
    background: rgba(255, 255, 255, 0.02);
    border-radius: 2px;
    font-family: var(--font-numeric);
    line-height: 1;
  }
  .dash-agent-count {
    color: var(--c-action);
    font-size: var(--fs-xl);
    font-weight: 800;
    font-variant-numeric: tabular-nums;
  }
  .dash-agent-label {
    color: var(--c-muted);
    font-size: var(--fs-xs);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  /* .dash-agent-toggle retired — toggleable "fires today" chip
     replaced by the standalone Agent Activity card header. */

  /* Filter strip CSS lives in app.css as .act-events-* (ActionEventsToggle). */
  .pnl-section-label {
    margin-top: 0.75rem;
    margin-bottom: 0.3rem;
  }

  /* ── Open orders strip ───────────────────────────────────────────── */
  .dash-open-orders {
    margin-bottom: 0.6rem;
    padding: 0.4rem 0.55rem;
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
  }
  .oo-header {
    display: flex;
    align-items: center;
    gap: 0.55rem;
    margin-bottom: 0.35rem;
  }
  .oo-count {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    color: var(--algo-sky);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .oo-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--algo-sky);
    animation: oo-pulse 2s ease-in-out infinite;
  }
  @keyframes oo-pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }
  @media (prefers-reduced-motion: reduce) {
    .oo-dot { animation: none; }
  }
  .oo-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }
  .oo-pill {
    display: inline-flex;
    align-items: baseline;
    gap: 0.3rem;
    padding: 0.2rem 0.5rem;
    border-radius: 3px;
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    text-decoration: none;
    border: 1px solid;
    font-variant-numeric: tabular-nums;
    transition: filter 0.12s;
    white-space: nowrap;
  }
  .oo-pill:hover { filter: brightness(1.15); }
  .oo-pill-buy {
    background: var(--algo-green-bg);
    border-color: var(--algo-green-border-soft);
    color: #a7f3c0;
  }
  .oo-pill-sell {
    background: var(--algo-red-bg);
    border-color: var(--algo-red-border-soft);
    color: #fca5a5;
  }
  .oo-side   { font-weight: 800; font-size: var(--fs-xs); letter-spacing: 0.06em; }
  .oo-qty    { font-weight: 700; }
  .oo-sym    { font-weight: 700; }
  .oo-price  { color: var(--algo-slate); }
  .oo-attempts { color: var(--c-muted); font-size: var(--fs-xs); }

  /* .dash-row2 + .wl-tile* family retired — Winners / Losers cards
     moved to /pulse where they sit in the 6-grid layout alongside
     the rest of the monitoring surfaces. */

  /* Compact ag-Grid wrappers inside the Capital + W/L cards. Width
     is 100% (grid columns flex to fill); height is driven by the
     grid's autoHeight option so the card grows naturally with row
     count. min-height keeps the header visible even with 0 rows. */
  .dash-mini-grid {
    width: 100%;
    min-height: 60px;
    transition: min-height 0.18s ease;
  }
  /* When a mini-grid has no rows, collapse it out of the layout —
     the surrounding bucket-subheader is already conditionally hidden
     above, and the dash-card-empty fallback below carries the empty
     message. Without this rule ag-Grid's autoHeight still reserves
     a header strip (~28 px) for an empty grid. */
  .dash-mini-grid.is-empty {
    min-height: 0;
    height: 0;
    overflow: hidden;
    border: none;
    box-shadow: none;
  }
  .dash-mini-grid + .bucket-subheader { margin-top: 0.55rem; }

  /* Column borders — match Pulse's clean borderless look. Pulse
     suppresses via .mp-bucket-wrap in MarketPulse.svelte, but that
     :global() rule is scoped to routes that load MarketPulse. Dashboard
     uses .dash-mini-grid as the theme root (ag-theme-algo on the same
     element), so we target ag-cell children directly from that root.
     The meaningful LTP-heat inset stripes are painted via box-shadow,
     not border, so they are unaffected. */
  :global(.dash-mini-grid.ag-theme-algo .ag-cell),
  :global(.dash-mini-grid.ag-theme-algo .ag-header-cell) {
    border-right: 0 !important;
    border-left: 0 !important;
  }

  /* (Dead .dash-card-empty rule retired — every dashboard empty
     state now uses the global <EmptyState> component which carries
     its own styling.) */

  /* W/L grid — explicit height so ag-Grid (domLayout: 'normal')
     measures + scrolls internally. Earlier max-height + overflow:auto
     on the wrapper with autoHeight on the grid caused ag-Grid to
     mis-measure and render an invisible header. Fullscreen mode
     fills the modal via flex:1 + min-height. */
  .dash-wl-grid {
    width: 100%;
    height: 18rem;
    cursor: pointer;
    transition: height 0.18s ease;
  }
  /* When the active bucket has no rows, collapse the grid wrapper
     to just enough height to show the empty-state overlay. ag-Grid
     keeps the header + overlay sized for the wrapper; this turns a
     wasteful 18 rem blank into a compact ~3.5 rem strip without
     destroying the grid (operator can flip tabs and the rows show
     instantly). */
  .dash-wl-grid.is-empty {
    height: 3.5rem;
    cursor: default;
  }
  .fs-card-on .dash-wl-grid {
    flex: 1;
    height: auto;
    min-height: calc(100vh - 14rem);
    min-height: calc(100dvh - 14rem);
  }
  .fs-card-on .dash-wl-grid.is-empty {
    min-height: 3.5rem;
  }

  /* Util-% gradient bands — pnl-gain (green, low util) and pnl-loss
     (red, dangerous util) already live in the algo theme; util-warn
     + util-mild fill the amber middle of the colour ramp.
     `!important` so they win against the theme's row-level `color`. */
  :global(.ag-theme-algo .util-warn) {
    color: var(--c-action) !important;
    background-color: rgba(251,191,36,0.08) !important;
  }
  :global(.ag-theme-algo .util-mild) {
    color: #e5c87a !important;
    background-color: rgba(251,191,36,0.04) !important;
  }

  /* Scrollable rows container — default-size cards cap the visible
     height so 10 rows don't bloat the card; fullscreen mode lifts
     the cap to fill the modal. Custom scrollbar matches the algo
     palette (muted slate, amber on hover). */
  .wl-rows {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    max-height: 16rem;
    overflow-y: auto;
    /* Firefox + Chrome custom scrollbar */
    scrollbar-width: thin;
    scrollbar-color: rgba(126, 151, 184, 0.35) transparent;
  }
  .wl-rows::-webkit-scrollbar { width: 6px; }
  .wl-rows::-webkit-scrollbar-track { background: transparent; }
  .wl-rows::-webkit-scrollbar-thumb {
    background: rgba(126, 151, 184, 0.35);
    border-radius: 3px;
  }
  .wl-rows::-webkit-scrollbar-thumb:hover {
    background: var(--algo-amber-border);
  }
  /* Fullscreen mode lifts the height cap so the operator can scan
     the full top-10 (or top-50 in the future) without scrolling. */
  .fs-card-on .wl-rows {
    max-height: calc(100vh - 12rem);
    max-height: calc(100dvh - 12rem);
  }
  .wl-row {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    width: 100%;
    padding: 0.22rem 0.3rem;
    border-radius: 3px;
    background: none;
    border: none;
    cursor: pointer;
    text-align: left;
    font-family: var(--font-numeric);
    transition: background 0.1s;
  }
  .wl-row:hover { background: rgba(255, 255, 255, 0.04); }
  .wl-sym {
    font-size: var(--fs-lg);
    font-weight: 700;
    color: #e2ecff;
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wl-pnl {
    font-size: var(--fs-lg);
    font-weight: 800;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }
  .wl-pnl-up   { color: var(--c-long); }
  .wl-pnl-down { color: var(--c-short); }
  .wl-pct {
    font-size: var(--fs-sm);
    color: var(--c-muted);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  /* ── Row 3: Activity card (replaced standalone Market News) ──────── */
  /* Chrome migrated to canonical .bucket-card; local class kept for
     the bottom-margin only. */
  .dash-activity {
    margin-bottom: 0.6rem;
    /* Activity surface owns the tail of the dashboard. Min-height
       caps the empty-state shrink so the operator's eye lands on the
       card even when News + Orders + Agents tabs are all empty. */
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .dash-activity > .card-body {
    /* Cap the tail height so the activity card doesn't dominate the
       dashboard — the log scrolls beyond the cap. min-height keeps
       enough rows visible to fill both magazine columns before
       overflow (~5-6 rows per column). Operator: "reduce the height
       of activity card." */
    display: flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-height: 8rem;
    max-height: 15rem;
  }
  /* NAV chip fetch-error strip — rendered above <NavTab> when
     /api/nav/latest returns an error. Red palette matching
     PerformancePage .perf-banner-error. Compact (padding-light)
     so it doesn't push the SVG chart out of view. */
  .dash-nav-err {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem;
    padding: 0.35rem 0.6rem;
    margin-bottom: 0.3rem;
    background: rgba(248, 113, 113, 0.07);
    border: 1px solid rgba(248, 113, 113, 0.25);
    border-radius: 4px;
    color: var(--c-short);
    font-size: 0.72rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .dash-nav-retry {
    flex-shrink: 0;
    padding: 0.12rem 0.5rem;
    border-radius: 3px;
    border: 1px solid var(--algo-cyan-border);
    background: var(--algo-cyan-bg);
    color: var(--c-info);
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: background 120ms, border-color 120ms;
  }
  .dash-nav-retry:hover {
    background: var(--algo-cyan-bg-strong);
    border-color: rgba(34, 211, 238, 0.80);
    color: #67e8f9;
  }

</style>
