<!--
  NavBreakdown — slot-specific per-account breakdown table.

  Switches columns based on `activeSlot` (P/M/C/H) so the popup
  immediately shows the data that matches the NavStrip pill value the
  operator just clicked:

    P — Day P&L (positionsDayPnlStore.total — matches NavStrip P:1) + Lifetime P&L (Σ pnl) + Expiry P&L (lognormal projection)
    M — Available Margin + Total Margin (used + avail)
    C — Live Cash (live_cash ?? cash) + Total Cash (+ long-option premium)
    H — Today MTM (holdingsDayPnlStore.byAccount) + Value (live ltp×qty) + Lifetime (Σ pnl)

  TOTAL row sums the same scoped accounts and matches the NavStrip pill
  value for that slot.

  Sources funds + positions + holdings from the module-level
  marketDataStores singletons. No extra fetch.

  Reused by:
    - dashboard NAV tab (algo dark palette)
    - any future surface that needs the per-account slot breakdown
-->
<script>
  import { onDestroy, untrack } from 'svelte';
  import { aggCompact } from '$lib/format';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import { mkBaseGridOpts, NUMERIC_HDR, agAggFmt, agDirCellText, agPctFmt } from '$lib/data/algoGridUtils.js';
  ModuleRegistry.registerModules([AllCommunityModule]);
  import { fundsStore, holdingsStore, positionsStore, pulseHoldingsStore } from '$lib/data/marketDataStores.svelte.js';
  import { positionsDayPnlStore } from '$lib/data/positionsDayPnlStore.svelte.js';
  import { holdingsDayPnlStore } from '$lib/data/holdingsDayPnlStore.svelte.js';
  import { liveSnap } from '$lib/data/symbolStore.svelte.js';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';
  import { exportRowsToCsv } from '$lib/utils/csvExport.js';
  import { connStatus } from '$lib/stores';

  /** @type {{
   *   accountFilter?: string[],
   *   activeSlot?: 'P'|'M'|'C'|'H',
   *   expiryByAcct?: Map<string, number>,
   * }} */
  let {
    // Empty = all accounts (no filter). When set, the table only
    // shows the picked accounts and the TOTAL row sums over the
    // filtered subset.
    accountFilter = /** @type {string[]} */ ([]),
    // When set, the table shows the data relevant to that NavStrip pill.
    activeSlot = /** @type {'P'|'M'|'C'|'H'} */ ('P'),
    // Per-account expiry P&L map — passed from PositionStrip (which has
    // access to symbolStore spots). NavBreakdown cannot compute this itself.
    expiryByAcct = /** @type {Map<string,number>} */ (new Map()),
  } = $props();

  /**
   * Public method — lets a parent component (e.g. dashboard CardControls
   * toolbar) trigger the CSV download without needing to own the data.
   * Usage: bind:this={ref} then ref?.downloadCsv?.()
   */
  export function downloadCsv() {
    _downloadCsv();
  }

  // Module-level store reads — bridged via $effect → $state so that
  // when a store goes null mid-fetch (revalidation), the prior snapshot
  // is kept rather than clearing the table. Null = loading/fetching;
  // [] = explicitly empty; [...data] = loaded.
  /** @type {any[]} */
  let _funds = $state(fundsStore.value ?? []);
  $effect(() => {
    const v = fundsStore.value;
    untrack(() => { if (v != null) _funds = v; });
  });
  /** @type {any[]} */
  let _positions = $state(positionsStore.value ?? []);
  $effect(() => {
    const v = positionsStore.value;
    untrack(() => { if (v != null) _positions = v; });
  });
  /** @type {any[]} */
  let _holdings = $state(holdingsStore.value ?? []);
  $effect(() => {
    const v = holdingsStore.value;
    untrack(() => { if (v != null) _holdings = v; });
  });
  /** @type {any[]} */
  let _pulseHoldings = $state(pulseHoldingsStore.value ?? []);
  $effect(() => {
    const v = pulseHoldingsStore.value;
    untrack(() => { if (v != null) _pulseHoldings = v; });
  });

  // ── Loading-state machine ────────────────────────────────────────────
  // Three explicit states so the operator never sees a silent perpetual
  // spinner:
  //   1. loading  — at least one store still in-flight (or never loaded).
  //                 After 10 s flip to "timed-out" so the operator can act.
  //   2. empty    — all three stores have completed (lastFetch > 0) with no
  //                 data. Show actionable "check broker connections" message.
  //   3. error    — at least one store surfaced an error. Show message + Retry.
  //   4. ready    — table renders.

  /** True while any of the three stores is actively fetching. */
  const _inFlight = $derived(
    positionsStore.loading || holdingsStore.loading || fundsStore.loading
  );

  /** True once all three stores have completed at least one successful fetch. */
  const _allLoaded = $derived(
    positionsStore.lastFetch > 0 &&
    holdingsStore.lastFetch  > 0 &&
    fundsStore.lastFetch     > 0
  );

  /** First fetch-error string across all three stores (null when clean). */
  const _anyError = $derived(
    positionsStore.error || holdingsStore.error || fundsStore.error || null
  );

  /** Flips to true >5 s into loading: show "retrying — network slow" hint. */
  let _slowLoad  = $state(false);
  /** 10-second hard timeout — flips to true when still loading after 10 s. */
  let _timedOut  = $state(false);
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _slowHandle    = null;
  /** @type {ReturnType<typeof setTimeout>|null} */
  let _timeoutHandle = null;

  $effect(() => {
    // Read reactive deps inside the effect; write via untrack() so the writes
    // don't re-trigger the effect (avoids reactive churn on every store poll).
    const shouldArm = _inFlight && !_allLoaded;
    untrack(() => {
      if (shouldArm) {
        // Arm the slow-load hint (5 s) and hard timeout (10 s).
        if (!_slowHandle) {
          _slowHandle = setTimeout(() => { _slowLoad = true; }, 5_000);
        }
        if (!_timeoutHandle) {
          _timeoutHandle = setTimeout(() => { _timedOut = true; }, 10_000);
        }
      } else {
        // Loaded or errored — cancel both clocks.
        if (_slowHandle)    { clearTimeout(_slowHandle);    _slowHandle    = null; }
        if (_timeoutHandle) { clearTimeout(_timeoutHandle); _timeoutHandle = null; }
        // Identity-guarded writes: avoids churn when already at the resting value.
        if (_slowLoad)  _slowLoad  = false;
        if (_timedOut)  _timedOut  = false;
      }
    });
  });

  onDestroy(() => {
    if (_slowHandle)    clearTimeout(_slowHandle);
    if (_timeoutHandle) clearTimeout(_timeoutHandle);
  });

  /** Force a fresh fetch on all three stores (Retry button handler). */
  function _retry() {
    _timedOut = false;
    if (_timeoutHandle) { clearTimeout(_timeoutHandle); _timeoutHandle = null; }
    positionsStore.load({ fresh: true });
    holdingsStore.load({ fresh: true });
    fundsStore.load({ fresh: true });
  }

  // Canonical account display order map — $state so _allAccounts re-derives
  // when fetchBrokerOrder() resolves after cold load.
  let _navOrderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubNavOrder = accountDisplayOrder.subscribe(m => { _navOrderMap = m; });
  onDestroy(() => { _unsubNavOrder(); });

  // Bridge connStatus (Svelte writable store) into $state so $derived.by()
  // below can read it reactively. Do NOT use $derived reading a store directly
  // — it can stale-cache. $effect keeps the snapshot live.
  let _connStatusSnap = $state($connStatus);
  $effect(() => { _connStatusSnap = $connStatus; });

  // Page-wide account union — every account with data in any of the three
  // sources, plus every configured broker account from connStatus. Accounts
  // with no holdings/positions data (e.g. disconnected Dhan accounts) are
  // included so all slots show a row for every known account.
  const _allAccounts = $derived.by(() => {
    const set = new Set();
    for (const r of _funds)     if (r.account && r.account !== 'TOTAL') set.add(String(r.account));
    for (const r of _positions) if (r.account) set.add(String(r.account));
    for (const r of _holdings)  if (r.account) set.add(String(r.account));
    for (const a of (_connStatusSnap.accounts ?? [])) if (a) set.add(String(a));
    return sortAccountsBy([...set], _navOrderMap);
  });

  const _scopedAccounts = $derived.by(() => {
    if (!accountFilter || accountFilter.length === 0) return _allAccounts;
    const allow = new Set(accountFilter.map(String));
    return _allAccounts.filter(a => allow.has(a));
  });

  // ── P slot — per-account Day P&L + Lifetime P&L + Expiry P&L ────────
  // Primary source: expiryByAcct prop from PositionStrip (lognormal projection).
  // Fallback: sum p.pnl for derivative positions per account — covers the case
  // where lognormal spot price resolution hasn't fired yet (open positions with
  // no symbolStore snapshot at popup-open time).
  const _DERIV_EXCHS = new Set(['NFO', 'MCX', 'CDS', 'BFO']);
  const _expiryFallback = $derived.by(() => {
    /** @type {Map<string, number>} */
    const m = new Map();
    for (const p of _positions) {
      if (!_DERIV_EXCHS.has(String(p?.exchange || '').toUpperCase())) continue;
      const acct = String(p?.account || '');
      if (!acct) continue;
      m.set(acct, (m.get(acct) ?? 0) + Number(p?.pnl ?? 0));
    }
    return m;
  });

  const _pByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const rows        = _positions.filter(p => String(p.account) === acct);
      const dayPnl      = positionsDayPnlStore.byAccount[acct.toUpperCase()] ?? 0;
      const lifetimePnl = rows.reduce((s, p) => s + Number(p.pnl ?? 0), 0);
      const expiryPnl   = expiryByAcct.get(acct) ?? _expiryFallback.get(acct) ?? null;
      return { account: acct, dayPnl, lifetimePnl, expiryPnl };
    });
  });

  const _pTotal = $derived.by(() => ({
    // positionsDayPnlStore.total is the SSOT for NavStrip P:1 day P&L (pulse-authoritative
    // when MarketPulse is mounted, SSE-throttled otherwise). Reading it here keeps
    // the TOTAL row in sync with NavStrip P:1 regardless of which path wrote it.
    // NOTE: positionsDayPnlStore.total is a global total — it does NOT scope to
    // accountFilter. If this component is ever rendered with a non-empty accountFilter,
    // the TOTAL dayPnl will reflect all accounts, not just the filtered subset.
    // Per-account rows use positionsDayPnlStore.byAccount (live-LTP-aware). This
    // is acceptable for the current NavStrip use-case (no filter) but would need a
    // filtered variant if a filtered NavBreakdown needs strict TOTAL consistency.
    dayPnl:      positionsDayPnlStore.total,
    lifetimePnl: _pByAcct.reduce((s, r) => s + r.lifetimePnl, 0),
    expiryPnl:   _pByAcct.reduce((s, r) => s + (r.expiryPnl ?? 0), 0),
  }));

  // ── M slot — per-account Avail Margin + Used Margin + Total Margin ──
  const _mByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const f = _funds.find(x => String(x.account) === acct);
      const availMargin = Number(f?.avail_margin ?? 0);
      const usedMargin  = Number(f?.used_margin  ?? 0);
      const totalMargin = availMargin + usedMargin;
      const utilPct     = totalMargin > 0 ? usedMargin / totalMargin : 0;
      return { account: acct, availMargin, usedMargin, totalMargin, utilPct };
    });
  });

  const _mTotal = $derived.by(() => {
    const availMargin = _mByAcct.reduce((s, r) => s + r.availMargin, 0);
    const usedMargin  = _mByAcct.reduce((s, r) => s + r.usedMargin, 0);
    const totalMargin = availMargin + usedMargin;
    const utilPct     = totalMargin > 0 ? usedMargin / totalMargin : 0;
    return { availMargin, usedMargin, totalMargin, utilPct };
  });

  // ── C slot — per-account Live Cash + Collateral + Total Cash ─────────
  // Total Cash = live_cash + long-option premium paid
  // Long-option premium = Σ avg_price × qty for CE/PE with qty > 0
  // Collateral = broker-reported pledged-stock collateral from funds row
  const _cByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const f = _funds.find(x => String(x.account) === acct);
      const _lc = Number(f?.live_cash ?? 0);
      const liveCash   = _lc !== 0 ? _lc : Number(f?.cash ?? 0);
      const collateral = Number(f?.collateral ?? 0);
      const optPremium = _positions
        .filter(p =>
          String(p.account) === acct &&
          Number(p.quantity ?? 0) > 0 &&
          (String(p.tradingsymbol ?? '').endsWith('CE') ||
           String(p.tradingsymbol ?? '').endsWith('PE'))
        )
        .reduce((s, p) => s + Number(p.average_price ?? 0) * Number(p.quantity ?? 0), 0);
      const totalCash = liveCash + optPremium;
      return { account: acct, liveCash, collateral, totalCash };
    });
  });

  const _cTotal = $derived.by(() => ({
    liveCash:   _cByAcct.reduce((s, r) => s + r.liveCash, 0),
    collateral: _cByAcct.reduce((s, r) => s + r.collateral, 0),
    totalCash:  _cByAcct.reduce((s, r) => s + r.totalCash, 0),
  }));

  // ── H slot — per-account Today MTM + Value + Lifetime from holdings ──
  // todayMtm: holdingsDayPnlStore.byAccount — SSOT matching NavStrip H:1 (pulseHoldingsStore
  //   rows, live ltp×close formula, pulse-overridable by MarketPulse).
  // value:    live ltp×qty from symbolStore snapshot (matching PositionStrip _liveHoldingsValue),
  //   fallback to h.cur_val when ltp unavailable.
  // lifetimePnl: Σ h.pnl from pulseHoldingsStore rows.
  const _hByAcct = $derived.by(() => {
    return _scopedAccounts.map(acct => {
      const key  = acct.toUpperCase();
      const rows = _pulseHoldings.filter(h => String(h.account) === acct);
      const todayMtm = holdingsDayPnlStore.byAccount[key] ?? 0;
      let value = 0;
      for (const h of rows) {
        const sym = String(h?.tradingsymbol || '').toUpperCase();
        const ltp = liveSnap(sym)?.ltp;
        const qty = Number(h?.quantity || 0);
        value += (ltp != null && ltp > 0 && qty !== 0) ? ltp * qty : Number(h?.cur_val || 0);
      }
      const lifetimePnl = rows.reduce((s, h) => s + Number(h.pnl ?? 0), 0);
      return { account: acct, todayMtm, value, lifetimePnl };
    });
  });

  const _hTotal = $derived.by(() => ({
    // TOTAL todayMtm from SSOT — matches NavStrip dispHoldingsToday exactly.
    todayMtm:    holdingsDayPnlStore.byAccount['TOTAL'] ?? _hByAcct.reduce((s, r) => s + r.todayMtm, 0),
    value:       _hByAcct.reduce((s, r) => s + r.value, 0),
    lifetimePnl: _hByAcct.reduce((s, r) => s + r.lifetimePnl, 0),
  }));

  // ── _hasData — slot-aware gate ───────────────────────────────────────
  const _hasData = $derived.by(() => {
    if (activeSlot === 'P') return _pByAcct.length > 0;
    if (activeSlot === 'M') return _mByAcct.length > 0;
    if (activeSlot === 'C') return _cByAcct.length > 0;
    if (activeSlot === 'H') return _hByAcct.length > 0;
    return false;
  });

  function _cls(v) {
    if (v == null || !Number.isFinite(v)) return 'nav-zero';
    if (v > 0) return 'nav-up';
    if (v < 0) return 'nav-down';
    return 'nav-zero';
  }
  function _fmt(v) {
    if (v == null || !Number.isFinite(v)) return '—';
    return aggCompact(v);
  }

  // ── Account palette + colour helper ─────────────────────────────────
  // Mirrors PerformancePage ACCT_PALETTE so each account hashes to the
  // same colour in every table that shows it.
  const _ACCT_PALETTE = [
    '#a78bfa', // violet
    '#5eead4', // teal
    '#fda4af', // rose
    'var(--algo-sky)', // sky
    '#bef264', // lime
    '#fcd34d', // amber
    '#a5b4fc', // indigo
    '#f0abfc', // fuchsia
  ];

  function _acctColor(/** @type {string|null|undefined} */ account) {
    if (!account || account === 'TOTAL') return null;
    let h = 5381;
    for (let i = 0; i < account.length; i++) {
      h = ((h << 5) + h) ^ account.charCodeAt(i);
      h = h >>> 0;
    }
    return _ACCT_PALETTE[h % _ACCT_PALETTE.length];
  }

  /** cellStyle injecting --acct-stripe for the account column. */
  function _acctCellStyle(p) {
    const c = _acctColor(p.data?.account);
    return c ? { '--acct-stripe': c } : { '--acct-stripe': 'transparent' };
  }


  // ── ag-Grid containers and instances ─────────────────────────────────
  /** @type {HTMLElement|null} */
  let _pEl = $state(null);
  /** @type {HTMLElement|null} */
  let _mEl = $state(null);
  /** @type {HTMLElement|null} */
  let _cEl = $state(null);
  /** @type {HTMLElement|null} */
  let _hEl = $state(null);
  /** @type {import('ag-grid-community').GridApi|null} */
  let _pGrid = null;
  /** @type {import('ag-grid-community').GridApi|null} */
  let _mGrid = null;
  /** @type {import('ag-grid-community').GridApi|null} */
  let _cGrid = null;
  /** @type {import('ag-grid-community').GridApi|null} */
  let _hGrid = null;

  const _pCols = [
    { field: 'account',  headerName: 'Account',   width: 60, minWidth: 48, maxWidth: 74,
      cellClass: 'ag-col-fill ag-col-acct', cellStyle: _acctCellStyle },
    { field: 'day_pnl',  headerName: 'Day P&L',   minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'lifetime', headerName: 'P&L',         minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'expiry',   headerName: 'Expiry P&L', minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
  ];

  const _mCols = [
    { field: 'account',     headerName: 'Account',    width: 60, minWidth: 48, maxWidth: 74,
      cellClass: 'ag-col-fill ag-col-acct', cellStyle: _acctCellStyle },
    { field: 'usedMargin',  headerName: 'Used',        minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'availMargin', headerName: 'Avail',       minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'totalMargin', headerName: 'Total',       minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'utilPct',     headerName: 'Util %',      minWidth: 52, flex: 0.8,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agPctFmt },
  ];

  const _cCols = [
    { field: 'account',    headerName: 'Account',     width: 60, minWidth: 48, maxWidth: 74,
      cellClass: 'ag-col-fill ag-col-acct', cellStyle: _acctCellStyle },
    { field: 'liveCash',   headerName: 'Live Cash',   minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'collateral', headerName: 'Collateral',  minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'totalCash',  headerName: 'Total Cash',  minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
  ];

  const _hCols = [
    { field: 'account',   headerName: 'Account',     width: 60, minWidth: 48, maxWidth: 74,
      cellClass: 'ag-col-fill ag-col-acct', cellStyle: _acctCellStyle },
    { field: 'todayMtm',  headerName: 'Today MTM',   minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'value',     headerName: 'Value',        minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
    { field: 'lifetime',  headerName: 'P&L',          minWidth: 64, flex: 1,
      type: 'numericColumn', headerClass: NUMERIC_HDR,
      cellClass: agDirCellText, valueFormatter: agAggFmt },
  ];

  // Grid creation — lazy, one per slot.
  $effect(() => {
    if (activeSlot !== 'P' || !_pEl || _pGrid) return;
    _pGrid = createGrid(_pEl, { ...mkBaseGridOpts(), columnDefs: _pCols, rowData: [], domLayout: 'autoHeight', getRowClass: p => p.data?.account === 'TOTAL' ? 'totals-row' : '' });
  });
  $effect(() => {
    if (activeSlot !== 'M' || !_mEl || _mGrid) return;
    _mGrid = createGrid(_mEl, { ...mkBaseGridOpts(), columnDefs: _mCols, rowData: [], domLayout: 'autoHeight', getRowClass: p => p.data?.account === 'TOTAL' ? 'totals-row' : '' });
  });
  $effect(() => {
    if (activeSlot !== 'C' || !_cEl || _cGrid) return;
    _cGrid = createGrid(_cEl, { ...mkBaseGridOpts(), columnDefs: _cCols, rowData: [], domLayout: 'autoHeight', getRowClass: p => p.data?.account === 'TOTAL' ? 'totals-row' : '' });
  });
  $effect(() => {
    if (activeSlot !== 'H' || !_hEl || _hGrid) return;
    _hGrid = createGrid(_hEl, { ...mkBaseGridOpts(), columnDefs: _hCols, rowData: [], domLayout: 'autoHeight', getRowClass: p => p.data?.account === 'TOTAL' ? 'totals-row' : '' });
  });

  // Row-data updates.
  $effect(() => {
    if (!_pGrid) return;
    _pGrid.setGridOption('rowData', _pByAcct.map(r => ({
      account: r.account, day_pnl: r.dayPnl, lifetime: r.lifetimePnl, expiry: r.expiryPnl,
    })));
    _pGrid.setGridOption('pinnedBottomRowData', [{
      account: 'TOTAL', day_pnl: _pTotal.dayPnl,
      lifetime: _pTotal.lifetimePnl, expiry: _pTotal.expiryPnl,
    }]);
  });
  $effect(() => {
    if (!_mGrid) return;
    _mGrid.setGridOption('rowData', _mByAcct.map(r => ({
      account: r.account, usedMargin: r.usedMargin, availMargin: r.availMargin,
      totalMargin: r.totalMargin, utilPct: r.utilPct,
    })));
    _mGrid.setGridOption('pinnedBottomRowData', [{
      account: 'TOTAL', usedMargin: _mTotal.usedMargin, availMargin: _mTotal.availMargin,
      totalMargin: _mTotal.totalMargin, utilPct: _mTotal.utilPct,
    }]);
  });
  $effect(() => {
    if (!_cGrid) return;
    _cGrid.setGridOption('rowData', _cByAcct.map(r => ({
      account: r.account, liveCash: r.liveCash, collateral: r.collateral, totalCash: r.totalCash,
    })));
    _cGrid.setGridOption('pinnedBottomRowData', [{
      account: 'TOTAL', liveCash: _cTotal.liveCash,
      collateral: _cTotal.collateral, totalCash: _cTotal.totalCash,
    }]);
  });
  $effect(() => {
    if (!_hGrid) return;
    _hGrid.setGridOption('rowData', _hByAcct.map(r => ({
      account: r.account, todayMtm: r.todayMtm, value: r.value, lifetime: r.lifetimePnl,
    })));
    _hGrid.setGridOption('pinnedBottomRowData', [{
      account: 'TOTAL', todayMtm: _hTotal.todayMtm,
      value: _hTotal.value, lifetime: _hTotal.lifetimePnl,
    }]);
  });

  /** Caption text per slot. */
  const _caption = $derived.by(() => {
    if (activeSlot === 'P') return 'Day P&L | P&L (Σ pnl) | Expiry P&L (lognormal projection)';
    if (activeSlot === 'M') return 'Available = Total − used margin | Total = used + available';
    if (activeSlot === 'C') return 'Cash Avail (CA) = live deployable cash | Total = CA + long option premiums';
    if (activeSlot === 'H') return 'Today MTM | Current Value | P&L';
    return '';
  });

  /** Export the currently visible slot's data to CSV. */
  function _downloadCsv() {
    if (activeSlot === 'P') {
      const rows = [
        ..._pByAcct,
        { account: 'TOTAL', dayPnl: _pTotal.dayPnl, lifetimePnl: _pTotal.lifetimePnl, expiryPnl: _pTotal.expiryPnl },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',      key: 'account' },
        { header: 'Day P&L',      key: 'dayPnl',      format: (v) => v == null ? '' : String(v) },
        { header: 'P&L',          key: 'lifetimePnl', format: (v) => v == null ? '' : String(v) },
        { header: 'Expiry P&L',   key: 'expiryPnl',   format: (v) => v == null ? '' : String(v) },
      ], 'nav-p-breakdown.csv');
    } else if (activeSlot === 'M') {
      const rows = [
        ..._mByAcct,
        { account: 'TOTAL', availMargin: _mTotal.availMargin, usedMargin: _mTotal.usedMargin, totalMargin: _mTotal.totalMargin, utilPct: _mTotal.utilPct },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',      key: 'account' },
        { header: 'Avail Margin', key: 'availMargin', format: (v) => v == null ? '' : String(v) },
        { header: 'Used Margin',  key: 'usedMargin',  format: (v) => v == null ? '' : String(v) },
        { header: 'Total Margin', key: 'totalMargin', format: (v) => v == null ? '' : String(v) },
        { header: 'Util %',       key: 'utilPct',     format: (v) => v == null ? '' : `${Math.round(v * 100)}%` },
      ], 'nav-m-breakdown.csv');
    } else if (activeSlot === 'C') {
      const rows = [
        ..._cByAcct,
        { account: 'TOTAL', liveCash: _cTotal.liveCash, collateral: _cTotal.collateral, totalCash: _cTotal.totalCash },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',     key: 'account' },
        { header: 'Live Cash',   key: 'liveCash',   format: (v) => v == null ? '' : String(v) },
        { header: 'Collateral',  key: 'collateral', format: (v) => v == null ? '' : String(v) },
        { header: 'Total Cash',  key: 'totalCash',  format: (v) => v == null ? '' : String(v) },
      ], 'nav-c-breakdown.csv');
    } else if (activeSlot === 'H') {
      const rows = [
        ..._hByAcct,
        { account: 'TOTAL', todayMtm: _hTotal.todayMtm, value: _hTotal.value, lifetimePnl: _hTotal.lifetimePnl },
      ];
      exportRowsToCsv(rows, [
        { header: 'Account',      key: 'account' },
        { header: 'Today MTM',    key: 'todayMtm',    format: (v) => v == null ? '' : String(v) },
        { header: 'Value',        key: 'value',        format: (v) => v == null ? '' : String(v) },
        { header: 'P&L',          key: 'lifetimePnl', format: (v) => v == null ? '' : String(v) },
      ], 'nav-h-breakdown.csv');
    }
  }
</script>

{#if _hasData}
  <div class="nav-bd-wrap">
    {#if activeSlot === 'P'}<div bind:this={_pEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
    {#if activeSlot === 'M'}<div bind:this={_mEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
    {#if activeSlot === 'C'}<div bind:this={_cEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
    {#if activeSlot === 'H'}<div bind:this={_hEl} class="ag-theme-quartz ag-theme-algo nav-bd-ag"></div>{/if}
    <!-- Caption — slot-specific formula footnote so the operator
         glances and knows what each column means without hovering. -->
    <div class="nav-bd-caption">
      <span>{_caption}</span>
    </div>
  </div>
{:else if _anyError && !_inFlight}
  <!-- State 3: fetch error — at least one store returned an error. -->
  <div class="nav-bd-wrap">
    <div class="nav-bd-empty nav-bd-error" role="alert" data-testid="nav-bd-error">
      <span class="nav-bd-status-icon" aria-hidden="true">⚠</span>
      <span class="nav-bd-status-text">NAV data unavailable — {_anyError}</span>
      <button class="nav-bd-retry" onclick={_retry}>Retry</button>
    </div>
  </div>
{:else if _timedOut}
  <!-- State 1b: hard timeout (>10 s) while loading. -->
  <div class="nav-bd-wrap">
    <div class="nav-bd-empty nav-bd-warn" role="alert" data-testid="nav-bd-timeout">
      <span class="nav-bd-status-icon" aria-hidden="true">⏳</span>
      <span class="nav-bd-status-text">Fetch timed out — click Retry</span>
      <button class="nav-bd-retry" onclick={_retry}>Retry</button>
    </div>
  </div>
{:else if _allLoaded}
  <!-- State 2: loaded successfully but no data (empty broker accounts?). -->
  <div class="nav-bd-wrap">
    <div class="nav-bd-empty nav-bd-hint" data-testid="nav-bd-empty">
      <span class="nav-bd-status-icon" aria-hidden="true">—</span>
      <span class="nav-bd-status-text">No NAV data — check
        <a class="nav-bd-link" href="/admin/brokers">broker connections</a>
      </span>
    </div>
  </div>
{:else}
  <!-- State 1a: loading (in-flight or stores not yet started). -->
  <div class="nav-bd-wrap">
    <div class="nav-bd-empty" data-testid="nav-bd-loading">
      Loading NAV breakdown…{_slowLoad ? ' (retrying — network slow)' : ''}
    </div>
  </div>
{/if}

<style>
  .nav-bd-wrap {
    display: flex;
    flex-direction: column;
    gap: 0;
    padding: 0;
    /* Same rounded-corner + border as ag-theme-algo wrapper. */
    border-radius: 4px;
    overflow: hidden;
    border: 1.5px solid rgba(255, 255, 255, 0.10);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.45),
                inset 0 1px 0 rgba(255, 255, 255, 0.08);
    /* Defensive background seal — prevents parent bleed if wrapper
       ever gains padding or a gap between child rows. Uses the
       elevated token so it matches algo-grid-chrome / ag-root-wrapper
       (the same surface family as the inner table rows). */
    background: var(--card-bg-elevated);
  }

  .nav-bd-ag { width: 100%; }

  :global(.nav-bd-ag .ag-col-acct) {
    border-left: 3px solid var(--acct-stripe, transparent) !important;
  }

  .nav-bd-caption {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.6rem;
    color: var(--algo-muted);
    letter-spacing: 0.04em;
    padding: 0.25rem 0.5rem;
    background: var(--card-bg, #1d2a44);
    border-top: 1px solid rgba(126,151,184,0.10);
  }
  .nav-bd-caption span {
    flex: 1;
  }

  .nav-bd-empty {
    padding: 1.2rem 0.8rem;
    text-align: center;
    color: rgba(155, 176, 208, 0.55);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.72rem;
    background: var(--card-bg, #1d2a44);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 0.4rem;
  }

  /* Error state — red tint matching PerformancePage .perf-banner-error palette. */
  .nav-bd-error {
    background: rgba(248, 113, 113, 0.07);
    color: var(--c-short);
    border-top: 1px solid rgba(248, 113, 113, 0.25);
  }

  /* Slow/timed-out warning — amber tint matching the algo-theme warn palette. */
  .nav-bd-warn {
    background: rgba(251, 191, 36, 0.07);
    color: var(--c-action);
    border-top: 1px solid rgba(251, 191, 36, 0.25);
  }

  /* Hint (empty-but-loaded) — muted slate, same as the loading text. */
  .nav-bd-hint {
    color: rgba(155, 176, 208, 0.70);
  }

  .nav-bd-status-icon {
    font-size: 0.9rem;
    flex-shrink: 0;
  }

  .nav-bd-status-text {
    flex: 1 1 auto;
    min-width: 0;
  }

  .nav-bd-link {
    color: var(--c-info);
    text-decoration: underline;
    text-underline-offset: 2px;
  }
  .nav-bd-link:hover {
    color: #67e8f9;
  }

  /* Retry button — cyan-400 palette matching RefreshButton / PageHeaderActions
     so the operator doesn't have to relearn the "action" visual language. */
  .nav-bd-retry {
    flex-shrink: 0;
    padding: 0.15rem 0.6rem;
    border-radius: 3px;
    border: 1px solid rgba(34, 211, 238, 0.55);
    background: var(--c-info-14);
    color: var(--c-info);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.68rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    cursor: pointer;
    transition: background 120ms, border-color 120ms;
  }
  .nav-bd-retry:hover {
    background: var(--c-info-22);
    border-color: rgba(34, 211, 238, 0.80);
    color: #67e8f9;
  }

</style>
