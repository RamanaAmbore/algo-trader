<script>
  import { onMount, onDestroy, tick, untrack } from 'svelte';
  import { createTickFlash } from '$lib/data/tickFlash.svelte.js';
  // ag-Grid is lazy-loaded in onMount so it doesn't bloat the initial bundle
  // for public /performance visitors. createGrid is populated after the
  // dynamic import resolves; makeGrid() guards on _agGridReady.
  /** @type {typeof import('ag-grid-community').createGrid | null} */
  let _createGrid = null;
  let _agGridReady = $state(false);
  import ChartModal from '$lib/ChartModal.svelte';
  import { fetchHoldings, fetchPositions, fetchFunds } from '$lib/api';
  import { createPerformanceSocket } from '$lib/ws';
  import { bookChanged } from '$lib/data/bookChanged';
  import { authStore } from '$lib/stores';
  import {
    positionsStore, holdingsStore, fundsStore,
    publishPositionsRows, publishHoldingsRows,
  } from '$lib/data/marketDataStores.svelte.js';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import GridDownloadButton from '$lib/GridDownloadButton.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { instrumentsCacheVersion } from '$lib/data/instruments';
  import { rootOfLabel } from '$lib/data/rootOf.js';
  import { navByAccount, navTotalRow, aggregateDayPnlForPositions } from '$lib/data/nav';

  // Module-scope cache for hyphenated display strings. ag-Grid
  // re-runs cellRenderer on every redraw — a Map cache avoids
  // re-parsing each symbol N×rows×renders. Cleared on grid teardown
  // via onDestroy below, AND on every instrumentsCacheVersion bump
  // (see effect at bottom of <script>): the cache otherwise pins the
  // cold-render value (no expiry-day appended) at first paint and
  // never picks up the per-symbol expiry once the instruments
  // dump finishes loading.
  const _symFmtCache = new Map();
  /**
   * Format a symbol for display. For MCX/CDS futures the virtual root
   * label is shown (e.g. "CRUDEOIL", "CRUDEOIL • NEXT") instead of the
   * raw contract name. All other symbols use the Dhan-style hyphenated
   * format.
   *
   * @param {string} sym    tradingsymbol
   * @param {string} [exch] exchange (MCX / CDS / …)
   */
  function _fmtSymCached(sym, exch = '') {
    if (!sym) return '';
    const cacheKey = `${sym}|${exch}`;
    let v = _symFmtCache.get(cacheKey);
    if (v === undefined) {
      const eUp = (exch || '').toUpperCase();
      if (eUp === 'MCX' || eUp === 'CDS') {
        const rl = rootOfLabel(sym, eUp);
        v = rl !== sym ? rl : formatSymbol(sym);
      } else {
        v = formatSymbol(sym);
      }
      _symFmtCache.set(cacheKey, v);
    }
    return v;
  }
  import { openActivityModal } from '$lib/stores';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import { getInstrument, loadInstruments } from '$lib/data/instruments';
  import { lotsForRow, fmtLots } from '$lib/data/lotsForRow';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { priceFmt, pctFmt, aggCompact, aggFmtGrid, pctFmtGrid } from '$lib/format';
  import NavCard from '$lib/NavCard.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import { accountDisplayOrder, sortAccountsBy } from '$lib/data/accountSort.js';
  import { mkWeightPctCol, mkDeltaCol, mkThetaCol, mkNavBreakdownCols } from '$lib/data/pulseColumns.js';

  // ModuleRegistry is registered inside onMount after the dynamic import.

  const {
    theme             = 'ag-theme-ramboq',
    allowOrders       = false,
    // Default: mask account IDs in the rendered cells. The override
    // below ($effect on authStore role) flips it to false when the
    // signed-in user is admin / designated — they see raw codes.
    // Partner and demo sessions stay masked even though the backend
    // already returns ZG#### in those cases; the client-side regex
    // is a defence-in-depth belt on top of the server's mask.
    maskAccountsProp  = true,
    // When true, drop the top timestamp+Refresh row and move the refresh
    // timestamp into the tabs row as the last element. Used by the
    // admin /dashboard page; default keeps the public /performance
    // layout unchanged.
    compactHeader     = false,
    // When true, positions rows for options (CE/PE) and futures (FUT)
    // show a small "→ Options" link that deep-links to /admin/options.
    // Only passed as true from the algo /dashboard; never on public pages.
    enableOptionsLink = false,
  } = $props();
  const isDark = $derived(theme === 'ag-theme-algo');

  // Row-level chart modal — opened by the chart button in symbol cells.
  let _chartModalSym  = $state('');
  let _chartModalExch = $state('');
  function _openChart(/** @type {string} */ symbol, /** @type {string} */ exchange = '') {
    _chartModalSym  = String(symbol  || '').toUpperCase();
    _chartModalExch = String(exchange || '');
  }

  // Context menu state — right-click / long-press on any symbol cell.
  /** @type {{ symbol: string, exchange: string, x: number, y: number } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | 'log' | null} */
  let _ctxAction = $state(null);
  /** @type {string} */ let _ctxSym  = $state('');
  /** @type {string} */ let _ctxExch = $state('');
  // Effective mask flag — designated (firm owner) and admin
  // (operational) never see masked codes, regardless of what the
  // parent passed. The prop still wins for partner / demo (default
  // `true`) and for any future caller that wants to force-mask even
  // an admin-tier session.
  const maskAccounts = $derived(
    ($authStore.user?.role === 'designated' || $authStore.user?.role === 'admin')
      ? false
      : maskAccountsProp
  );

  // Read tab from URL ?tab= param; default to 'positions'
  const validTabs = ['positions', 'holdings'];
  let activeTab = $state(validTabs.includes(page.url.searchParams.get('tab')) ? page.url.searchParams.get('tab') : 'positions');

  // OrderTicket props built from the clicked row. IBKR convention:
  // a row click defaults the ticket to CLOSE semantics (opposite
  // side, full held qty). Operator can flip the side toggle inside
  // the modal to instead add to the position. The DRAFT mode is
  // hidden here — this surface has no drafts panel, so the only
  // useful submit modes are PAPER and LIVE.
  let orderTicketProps = $state(/** @type {any|null} */(null));

  // Load the instruments cache once so we can pull the authoritative
  // exchange (`e`) and lot size (`ls`) per symbol when opening the
  // ticket. Held in IndexedDB after the first /console autocomplete
  // load — usually resolves from cache instantly. Stored as a promise
  // so the click handler can `await` it on the operator's first
  // interaction (avoids a "lot=200 instead of 2" bug where the cache
  // hadn't loaded yet → getInstrument returned null → lot defaulted
  // to 1 → OrderTicket displayed qty as the lot count).
  let _instrumentsReady = /** @type {Promise<unknown>} */ (Promise.resolve());
  onMount(() => { _instrumentsReady = loadInstruments().catch(() => {}); });

  // When the instruments cache populates / rebuilds, the per-symbol
  // expiry lookup is now available. Clear the symbol-format cache so
  // subsequent _fmtSymCached calls re-run formatSymbol() against the
  // newly-available expiry, and force every grid to re-paint its
  // symbol + sym-with-chart cells so the operator immediately sees
  // the day appear (e.g. CRUDEOIL-JUN → CRUDEOIL-16JUN).
  $effect(() => {
    /* eslint-disable-next-line @typescript-eslint/no-unused-expressions */
    $instrumentsCacheVersion;
    _symFmtCache.clear();
    const cols = ['tradingsymbol'];
    for (const g of [holdingsAllGrid, positionsAllGrid,
                     holdingsSummaryGrid, positionsSummaryGrid]) {
      if (g) try { g.refreshCells({ columns: cols, force: true }); } catch (_) { /* grid not ready */ }
    }
  });

  async function openOrderTicket(row, source) {
    // Trading rights — designated (firm owner) + trader can place orders.
    // Operational admin / partner cannot. Match the place_order cap.
    if (!allowOrders) return;
    const role = $authStore.user?.role;
    if (role !== 'designated' && role !== 'trader') return;
    if (!row?.tradingsymbol) return;
    // Wait for the instruments cache to settle before reading lot
    // size. The cache is normally instant from IndexedDB; this await
    // is a no-op the second time around but rescues the first click
    // after a fresh load.
    await _instrumentsReady;
    const sym = String(row.tradingsymbol).toUpperCase();
    const inst = getInstrument(sym);
    const lot  = Number(inst?.ls || 1);
    // Exchange — instrument cache wins; otherwise default by source
    // (holdings = NSE equities, positions = NFO F&O most of the time).
    const exch = inst?.e || (source === 'holdings' ? 'NSE' : 'NFO');
    const heldQty = Number(row.quantity) || 0;
    const isLong  = heldQty > 0;
    orderTicketProps = {
      symbol:   sym,
      exchange: exch,
      // IBKR-style close: side opposite to the held direction.
      side:     isLong ? 'SELL' : 'BUY',
      action:   'close',
      qty:      Math.abs(heldQty) || lot,
      lotSize:  lot,
      // Signed qty so the OrderTicket can render ADD / CLOSE labels
      // on the side toggle. Operator clicked on an existing
      // position; the bottom submit button still shows the resolved
      // BUY / SELL.
      currentQty: heldQty,
      // Pre-fill account from the row (real value when admin sees
      // unmasked data); ticket auto-fetches /api/accounts/ as a
      // backstop when this is empty or masked.
      account:  String(row.account || ''),
      accounts: [],
      // defaultMode + availableModes props were removed (Wave C); the
      // navbar's executionMode store now decides mode for every modal.
    };
  }

  function switchTab(/** @type {string} */ id) {
    activeTab = id;
    const url = new URL(page.url);
    url.searchParams.set('tab', id);
    goto(url.pathname + url.search, { replaceState: true, noScroll: true });
  }

  let lastRefresh = $state('');
  let loading     = $state(false);
  let error       = $state('');

  // Account-scope filter. Now MultiSelect-backed — empty array =
  // all accounts (no filter), any non-empty array = scope every
  // grid to that subset. Replaces the older single-select
  // `selectedAccount` string which only let the operator look at
  // one account at a time (or all).
  let selectedAccounts = $state(/** @type {string[]} */ ([]));
  // Symbol dropdown retired — the GridSearchButton on each detail
  // grid (Positions / Holdings) handles the same filter via free-text
  // search. Operator: "you can remove symbol dropdown for performance,
  // as search button can be used for the same functionality."
  let accounts        = $state([]);
  let rawHoldings     = $state([]);
  let rawPositions    = $state([]);
  let rawFunds        = $state([]);
  let rawHoldingsSummary  = $state([]);
  let rawPositionsSummary = $state([]);

  // Canonical account display order — used when building the account picker.
  let _perfOrderMap = $state(/** @type {Record<string,number>} */ ({}));
  const _unsubPerfOrder = accountDisplayOrder.subscribe(m => { _perfOrderMap = m; });

  // Total cur_val across the currently visible holdings — used by the
  // Weight % valueGetter. Updated whenever the filter applies. Plain
  // mutable variable (not $state) because it's only read inside the
  // synchronous valueGetter closure that AG Grid invokes per cell.
  let _holdingsTotalCurVal = 0;

  // Static grid refs
  let fundsEl            = null;
  let navEl              = null;
  let holdingsSummaryEl  = null;
  let holdingsAllEl      = null;
  let positionsSummaryEl = null;
  let positionsAllEl     = null;

  let fundsGrid            = null;
  let navGrid              = null;
  let holdingsSummaryGrid  = null;
  let holdingsAllGrid      = null;
  let positionsSummaryGrid = null;
  let positionsAllGrid     = null;

  // NAV and Funds card — NAV (per-account wealth) is the default tab;
  // Funds flips to the existing per-account margin/cash grid.
  let fundsNavTab = $state(/** @type {'nav' | 'funds'} */ ('nav'));

  // Header symbol filters — bound by <GridSearchButton> next to the
  // Positions / Holdings section headings. Empty = no filter.
  let _filterPositions = $state('');
  let _filterHoldings  = $state('');
  $effect(() => {
    const v = _filterPositions;
    try { positionsAllGrid?.setGridOption('quickFilterText', v); } catch (_) {}
  });
  $effect(() => {
    const v = _filterHoldings;
    try { holdingsAllGrid?.setGridOption('quickFilterText', v); } catch (_) {}
  });

  // Strip from the first digit onward — Zerodha F&O tradingsymbols are
  // "<UNDERLYING><expiry><strike><opt-type>" (NIFTY25APR22000CE,
  // underlyingOf() retired alongside the symbol dropdown — was only
  // used to collapse F&O contracts to their underlying (NIFTY25MAYFUT
  // → NIFTY) for the picker. GridSearchButton matches on the full
  // tradingsymbol so no derivation is needed.

  // AG Grid valueFormatter wrappers — receive { value } objects.
  // aggFmtGrid / pctFmtGrid imported from $lib/format (shared SSOT).
  // TODO: pnl-gain/pnl-loss classes here vs cell-pos/cell-neg in pulseColumns.js
  //   — requires CSS audit before renaming; classes may have rules in different files.
  const numFmt = ({ value }) => value == null ? '' : priceFmt(value);
  // Theme-aware P&L colors — actual colors live in app.css keyed to the grid theme.
  // Include 'ag-right-aligned-cell' because user-provided cellClass overrides the
  // class AG Grid adds via type: 'numericColumn'.
  const pnlCls = ({ value }) =>
    ['ag-right-aligned-cell', value < 0 ? 'pnl-loss' : value > 0 ? 'pnl-gain' : 'pnl-zero'];

  // Tick-flash — subtle 350ms directional background pulse on numeric cells.
  // Reuses createTickFlash verbatim. TOTAL rows (rowPinned==='bottom' or
  // tradingsymbol/account==='TOTAL') are excluded. Threshold 0.001 (epsilon)
  // prevents false flashes when the same value arrives twice. Alpha 0.13 via
  // global .tf-up/.tf-down in app.css — lower than LTP flash (0.35).
  const _perfFlash = createTickFlash({ threshold: 0.001, durationMs: 300 });

  // Stable row key: account|tradingsymbol. Matches updateGrid's key() fn.
  function _perfFlashKey(data) {
    if (!data) return null;
    return data.tradingsymbol ? `${data.account}|${data.tradingsymbol}` : data.account;
  }

  // cellClass factory for P&L-type columns that also emits tick-flash.
  // `field` is the data field name used for the per-cell flash key.
  // Falls through to plain pnlCls array on TOTAL / pinned rows.
  // Cascade dominance: if the row's LTP changed this cycle, emit
  // ltp-flash-up/down (source direction) instead of the per-field tf-*.
  function pnlClsFlash(field) {
    return (params) => {
      const base = ['ag-right-aligned-cell'];
      const v = params.value;
      base.push(v < 0 ? 'pnl-loss' : v > 0 ? 'pnl-gain' : 'pnl-zero');
      // Skip TOTAL / pinned rows — aggregates must not flash.
      if (params.node?.rowPinned === 'bottom') return base;
      if (params.data?.tradingsymbol === 'TOTAL' || params.data?.account === 'TOTAL') return base;
      const k = _perfFlashKey(params.data);
      if (!k) return base;
      // LTP cascade: if last_price changed this cycle, propagate its
      // direction to derived columns (source-based, not per-cell-diff).
      const ltpCls = _perfFlash.classOf(`${k}:last_price`);
      if (ltpCls) { base.push(ltpCls === 'tf-up' ? 'ltp-flash-up' : 'ltp-flash-down'); return base; }
      const fc = _perfFlash.classOf(`${k}:${field}`);
      if (fc) base.push(fc);
      return base;
    };
  }

  // Qty cell: classify by direction, not P&L. A short can be profitable,
  // a long can be losing — what the eye needs here is "which side of the
  // book am I on". Colours live in app.css (qty-short / qty-long).
  const qtyCls = ({ value }) =>
    ['ag-right-aligned-cell', value < 0 ? 'qty-short' : value > 0 ? 'qty-long' : 'qty-flat'];
  // avgVsLtpCls: two-axis heat on the LTP + Avg cells. Also emits ltp-flash-up/down
  // when last_price changed this poll cycle so the LTP cell itself pulses.
  const avgVsLtpCls = (params) => {
    if (params?.data?._isTotal || params?.data?.tradingsymbol === 'TOTAL' || params?.data?.account === 'TOTAL') {
      return 'ag-right-aligned-cell';
    }
    const avg  = params.data?.average_price;
    const ltp  = params.data?.last_price ?? params.data?.close_price;
    const prev = params.data?.close_price;
    if (ltp == null) return 'ag-right-aligned-cell';
    const cls = ['ag-right-aligned-cell'];
    // LTP flash on the LTP cell itself.
    const k = _perfFlashKey(params.data);
    if (k) {
      const ltpCls = _perfFlash.classOf(`${k}:last_price`);
      if (ltpCls) cls.push(ltpCls === 'tf-up' ? 'ltp-flash-up' : 'ltp-flash-down');
    }
    // Two-axis heat: bg vs avg_cost ("am I up overall?"), left-border
    // vs prev_close ("is it moving my way today?"). Operator scans
    // both axes at once. Legacy pnl-* text-colour kept alongside so
    // the cell value still reads green/red.
    if (typeof avg === 'number' && avg > 0) {
      if (ltp > avg) cls.push('ltp-vs-avg-up', 'pnl-gain');
      else if (ltp < avg) cls.push('ltp-vs-avg-down', 'pnl-loss');
      else cls.push('ltp-vs-avg-flat', 'pnl-zero');
    }
    if (typeof prev === 'number' && prev > 0 && prev !== ltp) {
      cls.push(ltp > prev ? 'ltp-vs-prev-up' : 'ltp-vs-prev-down');
    } else if (typeof prev === 'number' && prev > 0) {
      cls.push('ltp-vs-prev-flat');
    }
    return cls;
  };

  // avgClsWithDir — like avgVsLtpCls but for the Avg column only.
  // Adds cell-pos/cell-neg from the position/holding quantity so the
  // Avg cell carries directional tint (long = green, short = red) in
  // addition to the ltp-vs-avg heat. LTP column keeps avgVsLtpCls
  // unchanged (no qty-direction class on the price cell).
  const avgClsWithDir = (params) => {
    const base = avgVsLtpCls(params);
    if (!params?.data || params.data._isTotal ||
        params.data.tradingsymbol === 'TOTAL' || params.data.account === 'TOTAL') {
      return base;
    }
    const qty = Number(params.data?.quantity);
    const dirCls = qty > 0 ? 'cell-pos' : qty < 0 ? 'cell-neg' : 'cell-flat';
    return Array.isArray(base) ? [...base, dirCls] : [base, dirCls].filter(Boolean);
  };

  const defaultCol = { resizable: true, sortable: true, filter: true, suppressHeaderMenuButton: true, flex: 1, minWidth: 55 };

  const getRowClass = (params) => {
    const d = params.data || {};
    if (d.tradingsymbol === 'TOTAL' || d.account === 'TOTAL') return 'totals-row';
    // pos-long / pos-short tinting only on positions (which carry
    // a `product` field). Holdings are always long — no decoration.
    if (d.product == null) return '';
    const q = d.quantity;
    if (typeof q === 'number' && q < 0) return 'pos-short';
    if (typeof q === 'number' && q > 0) return 'pos-long';
    return '';
  };

  // ── Per-account colour identity ─────────────────────────────────────────
  // djb2 hash → stable index into an 8-hue palette. TOTAL rows receive no
  // colour so they read as neutral aggregates. Palette chosen for mutual
  // distinctness + visibility on both dark (algo) and light (ramboq) grids.
  // ACCT_PALETTE + acctColor moved to `$lib/account.js` so MarketPulse
  // and PerformancePage share one source of truth — each account hashes
  // to the same colour everywhere it surfaces in the UI. The const +
  // function inlined below are kept for back-compat with the existing
  // closures (acctCellRenderer / acctCellStyle reference them directly);
  // they now delegate to the shared helper.
  const ACCT_PALETTE = [
    '#a78bfa', // violet
    '#5eead4', // teal
    '#fda4af', // rose
    'var(--algo-sky)', // sky
    '#bef264', // lime
    '#fcd34d', // amber
    '#a5b4fc', // indigo
    '#f0abfc', // fuchsia
  ];

  function acctColor(/** @type {string|null|undefined} */ account) {
    if (!account || account === 'TOTAL') return null;
    let h = 5381;
    for (let i = 0; i < account.length; i++) {
      h = ((h << 5) + h) ^ account.charCodeAt(i);
      h = h >>> 0; // force unsigned 32-bit
    }
    return ACCT_PALETTE[h % ACCT_PALETTE.length];
  }

  // cellRenderer for the account column — handles maskAccounts only.
  // The per-account stripe lives on the cell's left border (via
  // cellStyle injecting --acct-stripe); no dot decoration anymore.
  //
  // Mask logic: when the backend has already masked the string
  // (detected via the presence of `#`), trust it and pass through.
  // Re-applying /\d/g would corrupt the ordinal digit in masks like
  // 'D1####' / 'D2####' (operator: 'it is showing D##### for both
  // dhan accounts on frontend'). Only when the string has no `#`
  // (admin path, unmasked source) do we apply the regex as a
  // belt-and-suspenders defence against an accidental backend
  // leak.
  function acctCellRenderer(params) {
    const raw = params.value || '';
    if (!maskAccounts || !raw) return raw;
    // Trust the backend's mask_account() — already broker-ordinal-aware
    // (D1####/D2#### for two-Dhan setups). Previous inline /\d/g
    // collapsed both Dhan codes to 'DH####' losing the ordinal hint.
    // If backend forgets to mask, that's a backend bug — fail loud
    // instead of producing the wrong shape silently.
    return raw;
  }

  // cellStyle for account column — injects a CSS custom property that the
  // `.ag-col-acct` rule uses as the left-border colour. Totals row → no
  // stripe (transparent). Each account → hash-derived hue.
  const acctCellStyle = (params) => {
    const raw = params.value || '';
    const color = acctColor(raw);
    return color ? { '--acct-stripe': color } : { '--acct-stripe': 'transparent' };
  };

  const acctFill = 'ag-col-fill ag-col-acct';
  // Symbol cells carry an extra `ag-col-sym` class so the long/short
  // indicator can paint a left+right border on the symbol cell only,
  // not the entire row.
  const symFill  = 'ag-col-fill ag-col-sym';

  // Shared "this is a numeric column" header class — explicitly set
  // on every numericColumn-typed column so right-alignment lands
  // regardless of AG Grid's columnType inheritance behaviour
  // (which historically left some headers left-aligned even with
  // the type set, since per-column shapes don't always pull
  // headerClass off the columnType definition reliably).
  const numericHdr = 'ag-right-aligned-header';

  // Column widths tightened so numeric cells (right-aligned) sit
  // next to their header instead of leaving empty space on the
  // LEFT half. Each column gets just enough room for its widest
  // expected value + the ~4 px cell padding from the theme.
  const holdingsSummaryCols = [
    // Operator: "in summary keep the account number as the first
    // column." Account leads so the row's routing context reads
    // first, matching the Funds grid convention (which already has
    // Account at column 0).
    { field: 'account',               headerName: 'Account',  width: 76,  minWidth: 76,  cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
    { field: 'day_change_val',        headerName: 'Day P&L',  width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',    width: 78,  valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                   headerName: 'P&L',      width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',        headerName: 'P&L %',    width: 78,  valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'cur_val',               headerName: 'Value',  width: 110, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'inv_val',               headerName: 'Invested',  width: 110, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
  ];
  // Note: holdings/positions summary grids (per-account aggregates) keep
  // `pnlCls` (no flash) — they are the equivalent of TOTAL rows.

  // Cluster: LTP → Prev → Avg → Day P&L → Day % → P&L → P&L %.
  // Avg slots between Prev and Day P&L per operator request — the eye
  // reads "where it is now (LTP) → where it closed yesterday (Prev) →
  // where it came in (Avg) → today's move → lifetime change" in a
  // single uninterrupted run.
  // Action-first column order — operator: "i am more interested in
  // ltp, avg, day p&l, p&l, etc for taking action." Account moves
  // to the trailing edge so the eye scans numbers (LTP → action) before
  // routing context (account). Symbol still pinned left as the row's
  // primary identifier; everything else flows in priority order.
  const holdingsCols = [
    { field: 'tradingsymbol',         headerName: 'Symbol',   width: 132, pinned: 'left', cellClass: symFill, headerClass: symFill, cellRenderer: _symWithChartRenderer },
    { field: 'last_price',            headerName: 'LTP',      width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'average_price',         headerName: 'Avg', width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgClsWithDir },
    { field: 'day_change_val',        headerName: 'Day P&L',  width: 78, valueFormatter: aggFmtGrid, cellClass: pnlClsFlash('day_change_val'), type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',    width: 60, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                   headerName: 'P&L',      width: 78, valueFormatter: aggFmtGrid, cellClass: pnlClsFlash('pnl'), type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',        headerName: 'P&L %',    width: 60, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'close_price',           headerName: 'Close', width: 78, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
    { field: 'quantity',              headerName: 'Qty',      width: 52, type: 'numericColumn', headerClass: numericHdr },
    // Lots — qty in F&O lot units. Holdings on F&O underlyings use
    // the underlying lot; option / futures positions use the contract
    // lot. Non-F&O rows read 0. Same shared helper that powers Pulse
    // so both pages report the same number for the same row.
    { field: 'lots', headerName: 'Lots', width: 52, type: 'numericColumn', headerClass: numericHdr,
      valueGetter: (p) => lotsForRow(p.data),
      valueFormatter: ({ value }) => fmtLots(value),
      headerTooltip: 'Qty in F&O lot units. 0 when the symbol is not an option underlying.' },
    { field: 'inv_val', headerName: 'Invested', width: 88,
      valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    // Weight % = this row's cur_val / total cur_val across the visible
    // holdings filter. Computed in valueGetter so it tracks the AG Grid
    // row-filter live (per-account view stays meaningful).
    mkWeightPctCol({ RA: '', numericHdr, pctFmtGrid, getTotalCurVal: () => _holdingsTotalCurVal }),
    { field: 'cur_val',               headerName: 'Value',  width: 88, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'account',               headerName: 'Account',  width: 76, cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
  ];

  // Positions summary — Account leads (operator: "in summary keep the
  // account number as the first column").
  const positionsSummaryCols = [
    { field: 'account',               headerName: 'Account', width: 76,  cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
    { field: 'day_change_val',        headerName: 'Day P&L', width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',   width: 78,  valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                   headerName: 'P&L',     width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
  ];

  // Symbol cell renderer with inline chart-icon button. Used on both
  // Holdings and Positions symbol columns. Opens _chartModalSym; the
  // exchange is derived from the row (positions carry `exchange`;
  // holdings default to 'NSE'). Uses the DOM element pattern (same as
  // _optionsLinkRenderer) so ag-Grid can mount it cleanly.
  function _symWithChartRenderer(params) {
    const sym   = String(params.value || '');
    const exch  = String(params.data?.exchange || (params.data?.product === 'CNC' ? 'NSE' : 'NFO'));
    const wrap  = document.createElement('span');
    wrap.style.cssText = 'display:inline-flex;align-items:center;gap:0;width:100%';
    const txt = document.createElement('span');
    // Display Dhan-style hyphenated form; data-sym keeps the raw
    // Kite tradingsymbol so context-menu / chart deep-links still
    // resolve correctly.
    txt.textContent = sym === 'TOTAL' ? sym : _fmtSymCached(sym, exch);
    if (sym && sym !== 'TOTAL') {
      txt.setAttribute('data-sym',  sym);
      txt.setAttribute('data-exch', exch);
      txt.className = 'perf-sym-cell';
    }
    wrap.appendChild(txt);
    return wrap;
  }

  // Symbol column (operator trim, -5 %): 180 → 171 with options-link,
  // 160 → 152 plain. Trims the column back without sacrificing the
  // 19-char F&O ticker fit; preserves the previous two-step bump
  // (132→180 / 120→160) less the operator-requested 5 % nudge.
  // Symbol column for positions — chart button always present; options
  // deep-link appended when enableOptionsLink is true (admin dashboard).
  function _posSymRenderer(params) {
    const sym  = String(params.value || '');
    const acct = params.data?.account || '';
    const exch = String(params.data?.exchange || 'NFO');
    const span = document.createElement('span');
    span.style.cssText = 'display:inline-flex;align-items:center;gap:0;width:100%';
    const _dispTxtPos = sym === 'TOTAL' ? sym : _fmtSymCached(sym, exch);
    span.textContent = _dispTxtPos;
    if (sym && sym !== 'TOTAL') {
      // data attrs for context menu delegation
      span.setAttribute('data-sym',  sym);
      span.setAttribute('data-exch', exch);
      span.className = 'perf-sym-cell';
      // Virtual-root tooltip: show raw contract when label is abbreviated.
      if (_dispTxtPos !== sym) span.setAttribute('title', sym);
      // Options deep-link (admin dashboard only)
      if (enableOptionsLink && /(?:CE|PE|FUT)$/.test(sym)) {
        const href = `/admin/derivatives?symbol=${encodeURIComponent(sym)}&account=${encodeURIComponent(acct)}`;
        const a = document.createElement('a');
        a.href = href;
        a.className = 'perf-opts-link';
        a.textContent = '→';
        a.title = `Open ${sym} in Options`;
        a.addEventListener('click', (e) => e.stopPropagation());
        span.appendChild(a);
      }
    }
    return span;
  }

  const positionsSymbolCol = $derived(
    { field: 'tradingsymbol', headerName: 'Symbol', width: enableOptionsLink ? 205 : 186, pinned: 'left', cellClass: symFill, headerClass: symFill, cellRenderer: _posSymRenderer }
  );

  // Cluster: LTP → Prev → Avg → Day P&L → Day % → P&L → P&L %.
  // Avg slots between Prev and Day P&L (operator request — the eye
  // reads price-where-it-is then close then entry then today's P&L).
  // Δ pos + Θ/day land AFTER the cluster so the contiguous run isn't
  // broken by the position-Greeks columns. Qty remains trailing.
  // Action-first column order (matches holdingsCols).
  const positionsCols = $derived([
    // F&O symbols are wider than equities (e.g. NIFTY26MAY22000CE);
    // 140 when options link active (extra room for the pill), 130 otherwise.
    positionsSymbolCol,
    { field: 'last_price',           headerName: 'LTP',       width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'average_price',        headerName: 'Avg', width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgClsWithDir },
    { field: 'day_change_val',       headerName: 'Day P&L',   width: 88, valueFormatter: aggFmtGrid, cellClass: pnlClsFlash('day_change_val'), type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage',headerName: 'Day %',     width: 64, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                  headerName: 'P&L',       width: 88, valueFormatter: aggFmtGrid, cellClass: pnlClsFlash('pnl'), type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',       headerName: 'P&L %',     width: 60, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'close_price',          headerName: 'Close', width: 78, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
    { field: 'quantity',             headerName: 'Qty',       width: 52, type: 'numericColumn', headerClass: numericHdr, cellClass: qtyCls },
    // Lots — option / futures positions use the contract's own lot;
    // cash equity + other non-derivative positions read 0. Shared
    // helper with Pulse + Holdings tab so every page reports the same
    // number for the same row.
    { field: 'lots', headerName: 'Lots', width: 52, type: 'numericColumn', headerClass: numericHdr, cellClass: qtyCls,
      valueGetter: (p) => lotsForRow(p.data),
      valueFormatter: ({ value }) => fmtLots(value),
      headerTooltip: 'Qty in F&O lot units. 0 for cash equity / non-derivative positions.' },
    // Per-row Greeks for option positions (delta × qty, theta × qty).
    // Backend computes once per /api/positions hit using the existing
    // implied_vol bisection + analytical greeks. Non-option rows show
    // 0 (default); the formatter renders 0 as em-dash to keep cash
    // equity rows from polluting the option column.
    mkDeltaCol({ RA: pnlCls, numericHdr }),
    mkThetaCol({ RA: pnlCls, numericHdr, aggFmtGrid }),
    { field: 'account',       headerName: 'Account',   width: 76, cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
  ]);

  // Order: Net (broker's account value, renamed avail_margin in
  // the API payload) → Avail Margin → Util % → Used → Cash →
  // Collateral. Field names match backend/api/schemas.py FundsRow
  // AFTER funds.py _COL_MAP renames the raw 'avail cash' / 'util
  // debits' broker columns to schema-friendly snake_case.
  //   API field → broker source
  //   cash          ← avail opening_balance (start-of-day cash)
  //   live_cash     ← avail cash (= live_balance, intraday-adjusted)
  //   avail_margin  ← net (broker's account value)
  //   used_margin   ← util debits
  //   collateral    ← avail collateral
  const fundsCols = [
    { field: 'account',      headerName: 'Account',      width: 76, cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
    { field: 'avail_margin', headerName: 'Net',          flex: 1, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { headerName: 'Util %', flex: 1, valueFormatter: pctFmtGrid, type: 'numericColumn', headerClass: numericHdr,
      valueGetter: (p) => {
        const used  = Number(p.data?.used_margin) || 0;
        const avail = Number(p.data?.avail_margin) || 0;
        const denom = used + avail;
        return denom > 0 ? (used / denom) * 100 : null;
      } },
    { field: 'used_margin',  headerName: 'Used Margin',  flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'cash',             headerName: 'Cash',            flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'available_funds',  headerName: 'Avl.Margin', flex: 1, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr,
      headerTooltip: 'Available Margin — free margin available for new trades (broker "net")' },
    { field: 'available_cash',   headerName: 'Avl.Cash',   flex: 1, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr,
      headerTooltip: 'Available Cash — start-of-day cash minus long-option premiums locked in open positions' },
    { field: 'collateral',       headerName: 'Collateral',      flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
  ];

  // NAV grid — per-account wealth. Mirrors scripts/nav_breakdown.py
  // and backend/api/algo/nav.py:compute_firm_nav (v4 formula):
  //
  //   NAV = (cash_sod + option_premium)     ← "Cash" column
  //       + Σ position.unrealised           ← "Pos M2M" column
  //       + Σ holdings.cur_val              ← "Holdings" column
  //
  // The Cash column adds long-option premium back to SOD cash —
  // when the operator pays ₹X premium for a long option, the ₹X
  // leaves cash but the option's unrealised only carries the
  // P&L since open (not the full ₹X). Adding option_premium back
  // recovers the cost so NAV doesn't undercount.
  //
  // Used margin (futures SPAN/exposure) is NOT added back — the
  // mark-to-market on futures positions already carries every
  // rupee of P&L. Collateral is also excluded (pledged stock is
  // already in holdings.cur_val at full LTP).
  //
  // Surfaced live on the client so the operator can see today's
  // NAV without waiting for the 16:00 IST cron.
  const navCols = [
    { field: 'account',      headerName: 'Account',  width: 76, cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
    { field: 'net',          headerName: 'Cash',         flex: 1, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    ...mkNavBreakdownCols({ RA: pnlCls, numericHdr, aggFmtGrid }),
  ];

  function makeGrid(el, colDefs, rowData = [], onRowClick = null) {
    if (!_createGrid) throw new Error('ag-Grid not yet loaded');
    return _createGrid(el, {
      // ag-Grid v33 changed the default theme to the Theming-API
      // (themeQuartz). Pinning to 'legacy' keeps the existing CSS-file
      // theming we've built up (ag-theme-ramboq + ag-theme-algo) so we
      // don't have to migrate the palette + density rules to the new
      // API right now.
      theme: 'legacy',
      columnDefs: colDefs,
      rowData,
      // Row identity for in-place updates — without this, ag-Grid
      // tears down every <div> row on every setGridOption('rowData')
      // call (the 30 s polls or any tab refresh would otherwise wipe
      // selection / cell focus / flash animations).
      getRowId: ({ data }) => {
        if (!data) return '';
        if (data.tradingsymbol) return `${data.tradingsymbol}|${data.account || ''}`;
        if (data.account) return String(data.account);
        return '';
      },
      defaultColDef: defaultCol,
      // Three-state sort cycle: ASC → DESC → no-sort (back to the
      // original row order). Applied at the grid level — same idiom
      // across every ag-Grid in the app.
      sortingOrder: ['asc', 'desc', null],
      // ag-Grid v33 forbids overriding built-in column types like
      // `numericColumn` (warning #34). The built-in already adds
      // `ag-right-aligned-cell` + `ag-right-aligned-header` classes
      // that our CSS picks up — no override needed.
      overlayNoRowsTemplate: '<span style="font-size: var(--fs-md);color:var(--c-muted)">—</span>',
      domLayout: 'autoHeight',
      getRowClass,
      pinnedBottomRowData: [],
      ...(onRowClick ? { onRowClicked: (e) => onRowClick(e.data) } : {}),
      onCellContextMenu: (ev) => {
        if (!ev.data || !ev.event) return;
        const sym  = String(ev.data.tradingsymbol || '');
        const exch = String(ev.data.exchange || (ev.data.product === 'CNC' ? 'NSE' : 'NFO'));
        if (!sym || sym === 'TOTAL') return;
        ev.event.preventDefault();
        const me = /** @type {MouseEvent} */ (ev.event);
        _ctxMenu = { symbol: sym, exchange: exch, x: me.clientX, y: me.clientY };
      },
    });
  }

  /**
   * @param {any} grid — ag-Grid instance
   * @param {any[]} newRows — fresh rows to apply
   */
  function updateGrid(grid, newRows) {
    if (!grid) return;
    const existing = [];
    grid.forEachNode(n => existing.push(n.data));
    if (!existing.length) {
      grid.setGridOption('rowData', newRows);
      return;
    }
    const key = (r) => r.tradingsymbol ? `${r.account}|${r.tradingsymbol}` : r.account;
    const oldMap = new Map(existing.map(r => [key(r), r]));

    const update = [], add = [];
    for (const r of newRows) {
      const k = key(r);
      if (oldMap.has(k)) {
        Object.assign(oldMap.get(k), r);
        update.push(oldMap.get(k));
        oldMap.delete(k);
      } else {
        add.push(r);
      }
    }
    const remove = [...oldMap.values()];
    grid.applyTransaction({ update, add, remove });
  }

  function makeHoldingsTotals(rows) {
    if (!rows?.length) return null;
    const sum = (f) => rows.reduce((s, r) => s + (Number(r[f]) || 0), 0);
    const total_pnl        = sum('pnl');
    const total_cur_val    = sum('cur_val');
    const total_day_change = sum('day_change_val');
    // Earlier this derived total_inv_val from cur_val - pnl. That's an
    // approximation that drifts when holdings have partial intraday
    // sells (opening_quantity ≠ quantity). The HoldingRow schema carries
    // inv_val directly — just sum it.
    const total_inv_val    = sum('inv_val');
    const total_prev_val   = total_cur_val - total_day_change;
    return {
      account: '',
      tradingsymbol: 'TOTAL',
      pnl:                   total_pnl,
      pnl_percentage:        total_inv_val  ? (total_pnl        / total_inv_val  * 100) : 0,
      day_change_val:        total_day_change,
      day_change_percentage: total_prev_val ? (total_day_change / total_prev_val * 100) : 0,
      quantity:              sum('quantity'),
      average_price: null,
      close_price:   null,
      inv_val:               total_inv_val,
      cur_val:               total_cur_val,
    };
  }

  function makePositionsTotals(rows) {
    if (!rows?.length) return null;
    const sum = (f) => rows.reduce((s, r) => s + (Number(r[f]) || 0), 0);
    // Aggregate denominators are absolute (qty can be ±) — short and
    // long positions both contribute to capital deployed.
    const total_pnl        = sum('pnl');
    // Use aggregateDayPnlForPositions for the new-position override: when
    // overnight_quantity=0 && day_change_val=0 && pnl≠0, the broker
    // returns 0 for dcv so we fall back to lifetime pnl. Matches
    // PositionStrip P1 and Pulse positions TOTAL row.
    const total_day_change = aggregateDayPnlForPositions(rows);
    const total_cost_basis = rows.reduce(
      (s, r) => s + Math.abs(Number(r.average_price) || 0) * Math.abs(Number(r.quantity) || 0), 0);
    const total_prev_val   = rows.reduce(
      (s, r) => s + Math.abs(Number(r.close_price) || 0)   * Math.abs(Number(r.quantity) || 0), 0);
    return {
      account: '',
      tradingsymbol: 'TOTAL',
      pnl:                   total_pnl,
      pnl_percentage:        total_cost_basis ? (total_pnl        / total_cost_basis * 100) : 0,
      day_change_val:        total_day_change,
      day_change_percentage: total_prev_val   ? (total_day_change / total_prev_val   * 100) : 0,
      unrealised:    sum('unrealised'),
      realised:      sum('realised'),
      quantity:      sum('quantity'),
      average_price: null,
      close_price:   null,
      last_price:    null,
    };
  }

  /** Predicate: row belongs to the current account selection (or all). */
  function _keepAcct(r) {
    return selectedAccounts.length === 0
      ? true
      : selectedAccounts.includes(String(r.account || ''));
  }

  /** Comparator: stable sort — zero-qty (closed) rows at the end. */
  function _closedLast(a, b) {
    const ac = (Number(a?.quantity || 0) === 0) ? 1 : 0;
    const bc = (Number(b?.quantity || 0) === 0) ? 1 : 0;
    return ac - bc;
  }

  /** Comparator: sort rows by account for cross-grid consistency. */
  const _byAcct = (a, b) => String(a.account || '').localeCompare(String(b.account || ''));

  /** Predicate: identifies TOTAL aggregate rows. */
  const _isTotalRow = (/** @type {any} */ r) =>
    r?.tradingsymbol === 'TOTAL' || r?.account === 'TOTAL';

  /**
   * Split an array into { body, total } where TOTAL rows are pinned
   * and body rows are sorted by account for cross-grid row-order parity.
   */
  function _splitByTotal(rows) {
    return {
      body:  rows.filter(r => !_isTotalRow(r)).slice().sort(_byAcct),
      total: rows.filter(_isTotalRow),
    };
  }

  /**
   * refreshCells on an ag-Grid for the three flash columns, with a
   * 400ms deferred second call so pnlClsFlash callbacks pick up the
   * new flash state both immediately and after the next tick cycle.
   */
  function _refreshFlashCells(grid) {
    if (!grid) return;
    const cols = ['last_price', 'day_change_val', 'pnl'];
    try { grid.refreshCells({ columns: cols, force: true }); } catch (_) {}
    setTimeout(() => {
      try { grid.refreshCells({ columns: cols, force: true }); } catch (_) {}
    }, 400);
  }

  /**
   * Seed tick-flash baseline for holdings + positions rows.
   * Wrapped in untrack() so $state writes inside flash.update() do
   * not register as reactive deps from an $effect call site.
   */
  function _seedFlash(hRows, pRows) {
    untrack(() => {
      for (const r of hRows) {
        if (r.tradingsymbol === 'TOTAL' || r.account === 'TOTAL') continue;
        const k = r.tradingsymbol ? `${r.account}|${r.tradingsymbol}` : r.account;
        if (!k) continue;
        if (r.last_price     != null) _perfFlash.update(`${k}:last_price`,    Number(r.last_price));
        if (r.day_change_val != null) _perfFlash.update(`${k}:day_change_val`, Number(r.day_change_val));
        if (r.pnl            != null) _perfFlash.update(`${k}:pnl`,            Number(r.pnl));
      }
      for (const r of pRows) {
        if (r.tradingsymbol === 'TOTAL' || r.account === 'TOTAL') continue;
        const k = r.tradingsymbol ? `${r.account}|${r.tradingsymbol}` : r.account;
        if (!k) continue;
        if (r.last_price     != null) _perfFlash.update(`${k}:last_price`,    Number(r.last_price));
        if (r.day_change_val != null) _perfFlash.update(`${k}:day_change_val`, Number(r.day_change_val));
        if (r.pnl            != null) _perfFlash.update(`${k}:pnl`,            Number(r.pnl));
      }
    });
  }

  /**
   * Compute NAV grid rows and TOTAL pinned row.
   * Uses the page-wide accounts list so accounts with holdings-only
   * (no positions/funds that cycle) still surface as a NAV row.
   * Renames cash → net to match the pre-existing ag-Grid column schema.
   */
  function _buildNavRows() {
    const navAccts = accounts
      .filter(a => selectedAccounts.length === 0 || selectedAccounts.includes(a));
    const _navRaw  = navByAccount(navAccts, rawFunds, rawPositions, rawHoldings);
    const rows = _navRaw.map(r => ({
      account: r.account, net: r.cash,
      pos_m2m: r.pos_m2m, holdings_mtm: r.holdings_mtm, nav: r.nav,
    }));
    const _totRaw = navTotalRow(_navRaw);
    const total = _totRaw ? {
      account: 'TOTAL', net: _totRaw.cash,
      pos_m2m: _totRaw.pos_m2m, holdings_mtm: _totRaw.holdings_mtm, nav: _totRaw.nav,
    } : null;
    return { rows, total };
  }

  function applyAccountFilter() {
    if (!holdingsAllGrid) return;
    // ACCOUNT filter scopes every grid (detail + summary + funds). With a
    // specific account picked we drop other accounts AND the TOTAL row.
    // Symbol filter retired — GridSearchButton on each detail grid
    // handles the equivalent.
    const hRows = rawHoldings.filter(_keepAcct).slice().sort(_closedLast);
    const pRows = rawPositions.filter(_keepAcct).slice().sort(_closedLast);
    // Recompute total cur_val so the Weight % column always reflects
    // the currently-filtered view (per-account picks change the base).
    _holdingsTotalCurVal = hRows.reduce(
      (s, r) => s + (Number(r.cur_val) || 0), 0);
    const hTotals = makeHoldingsTotals(hRows);
    const pTotals = makePositionsTotals(pRows);
    // Split TOTAL out of summary + funds data sets and pin to the
    // bottom — pinned-bottom rows in AG Grid are immune to sort, so
    // the TOTAL always anchors the last row regardless of which
    // column the operator clicks on.
    const hSum = _splitByTotal(rawHoldingsSummary.filter(_keepAcct));
    const pSum = _splitByTotal(rawPositionsSummary.filter(_keepAcct));
    const fSplit = _splitByTotal(rawFunds.filter(_keepAcct));
    updateGrid(holdingsSummaryGrid, hSum.body);
    holdingsSummaryGrid.setGridOption('pinnedBottomRowData', hSum.total);
    updateGrid(positionsSummaryGrid, pSum.body);
    positionsSummaryGrid.setGridOption('pinnedBottomRowData', pSum.total);
    // Seed tick-flash baseline before pushing to detail grids. First call
    // per key seeds baseline (no flash on mount). threshold=0.001 prevents
    // false flashes on identical values.
    _seedFlash(hRows, pRows);
    updateGrid(holdingsAllGrid, hRows);
    holdingsAllGrid.setGridOption('pinnedBottomRowData', hTotals ? [hTotals] : []);
    // refreshCells so pnlClsFlash callbacks pick up the new flash state.
    _refreshFlashCells(holdingsAllGrid);
    updateGrid(positionsAllGrid, pRows);
    positionsAllGrid.setGridOption('pinnedBottomRowData', pTotals ? [pTotals] : []);
    _refreshFlashCells(positionsAllGrid);
    updateGrid(fundsGrid, fSplit.body);
    fundsGrid.setGridOption('pinnedBottomRowData', fSplit.total);
    // NAV grid — per-account wealth aggregated from the same three raw arrays.
    const nav = _buildNavRows();
    if (navGrid) {
      updateGrid(navGrid, nav.rows);
      navGrid.setGridOption('pinnedBottomRowData', nav.total ? [nav.total] : []);
    }
    // Account column hides across every grid when exactly ONE account
    // is picked (no need to repeat the same account on every row).
    // Empty selection (all accounts) and multi-pick keep the column
    // visible so the operator can read which row belongs where.
    const showAcct = selectedAccounts.length !== 1;
    for (const g of [holdingsAllGrid, positionsAllGrid, fundsGrid, navGrid, holdingsSummaryGrid, positionsSummaryGrid]) {
      try { g?.setColumnsVisible?.(['account'], showAcct); } catch (_) { /* older AG API */ }
    }
  }

  function applyData(h, p, f) {
    // Sleep audit Jun 2026: allSettled in loadAll can land null for
    // any of the three fetches (transient broker failure); fall back
    // to previously-stored rows rather than crashing on null.rows.
    rawHoldings         = h?.rows ?? rawHoldings ?? [];
    rawPositions        = p?.rows ?? rawPositions ?? [];
    rawHoldingsSummary  = h?.summary ?? rawHoldingsSummary ?? [];
    rawPositionsSummary = p?.summary ?? rawPositionsSummary ?? [];
    rawFunds            = f?.rows ?? rawFunds ?? [];
    // Account picker scope includes funds-only accounts too — a Dhan
    // account holding cash with zero open positions on a given day
    // was silently disappearing from the picker (operator: "the
    // performance page is not including the other accounts like
    // algo pages"). The Funds grid was always showing those rows;
    // the picker now matches that coverage. TOTAL is filtered out
    // since it's an aggregate row, not a real account.
    const allAccts = [...new Set([
      ...rawHoldings.map(r => r.account),
      ...rawPositions.map(r => r.account),
      ...rawFunds.map(r => r.account).filter(a => a && a !== 'TOTAL'),
    ])];
    accounts = sortAccountsBy(allAccts, _perfOrderMap);
    // Symbol-list derivation + reconcileSymbols() retired alongside
    // the dropdown — GridSearchButton handles filtering with no
    // pre-computed picker scope.
    lastRefresh = h?.refreshed_at ?? p?.refreshed_at ?? f?.refreshed_at ?? lastRefresh ?? '';
    applyAccountFilter();
  }

  $effect(() => {
    // Track account + active tab. activeTab is in here so the filter
    // re-runs on tab switch (defensive — the grids already hold the
    // right rows since applyAccountFilter runs on every data refresh).
    selectedAccounts; activeTab;
    applyAccountFilter();
  });

  async function loadAll({ fresh = false } = {}) {
    loading = true; error = '';
    try {
      // Sleep audit Jun 2026: Promise.all → Promise.allSettled so a
      // single hung sub-fetch (holdings, positions, or funds) can't
      // strand the RefreshButton spinner. Each result is independently
      // checked below; partial data is better than no data.
      const results = await Promise.allSettled([
        fetchHoldings({ fresh }),
        fetchPositions({ fresh }),
        fetchFunds({ fresh }),
      ]);
      const h = results[0].status === 'fulfilled' ? results[0].value : null;
      const p = results[1].status === 'fulfilled' ? results[1].value : null;
      const f = results[2].status === 'fulfilled' ? results[2].value : null;
      // If ALL three failed, surface a single banner error so the
      // operator knows the page is dark; otherwise carry on with
      // whichever subset succeeded.
      if (!h && !p && !f) {
        const firstErr = results.find(r => r.status === 'rejected');
        // @ts-ignore — guarded by find above
        throw firstErr?.reason ?? new Error('fetch failed');
      }
      // Feed the module-level singletons so PositionStrip / NavCard / dashboard
      // benefit from this fetch without re-hitting the broker. The stores'
      // `parse` strips to `.rows` (and filters TOTAL for funds); we pre-parse
      // before calling set() so the stored value matches what other consumers
      // expect when they read store.value.
      const _h_rows = h?.rows ?? [];
      const _p_rows = p?.rows ?? [];
      holdingsStore.set(_h_rows);
      positionsStore.set(_p_rows);
      fundsStore.set((f?.rows ?? []).filter(
        (/** @type {any} */ x) => x && x.account && x.account !== 'TOTAL'
      ));
      // .set() bypasses the parse hook → publish to symbolStore
      // explicitly so the central market-data sink stays current on
      // a direct /performance landing (audit found this leak — the
      // page was the ONLY surface that fed the section stores
      // without publishing to symbolStore).
      publishPositionsRows(_p_rows);
      publishHoldingsRows(_h_rows);
      applyData(h, p, f);
    } catch (e) {
      error = e.message || 'Failed to load data';
    } finally { loading = false; }
  }

  let unsub;

  onMount(async () => {
    // Dynamic import — keeps AllCommunityModule (~200KB gzip) out of the
    // initial bundle so public /performance visitors don't pay the cost.
    const { createGrid, ModuleRegistry, AllCommunityModule } =
      await import('ag-grid-community');
    ModuleRegistry.registerModules([AllCommunityModule]);
    _createGrid = createGrid;
    _agGridReady = true;
    // Wait for Svelte to paint the grid container divs before calling
    // createGrid on them (they are always in the DOM — no {#if} wrapper
    // needed — but _agGridReady triggers the loading-placeholder swap).
    await tick();

    holdingsSummaryGrid  = makeGrid(holdingsSummaryEl,  holdingsSummaryCols);
    holdingsAllGrid      = makeGrid(holdingsAllEl,      holdingsCols, [], (r) => openOrderTicket(r, 'holdings'));
    positionsSummaryGrid = makeGrid(positionsSummaryEl, positionsSummaryCols);
    positionsAllGrid     = makeGrid(positionsAllEl,     positionsCols, [], (r) => openOrderTicket(r, 'positions'));
    fundsGrid            = makeGrid(fundsEl,             fundsCols);
    navGrid              = makeGrid(navEl,               navCols);

    // Stale-while-revalidate: paint the grids from the module-level store
    // cache (three-tier: memory → localStorage → broker). The stores init
    // from localStorage synchronously at module-eval time so the grids
    // populate before the broker fetch lands. applyData expects the full
    // API shape ({rows, summary, refreshed_at}); wrap the store arrays back
    // into that shape so the grid renderers work correctly.
    {
      const hp = holdingsStore.value;
      const pp = positionsStore.value;
      const fp = fundsStore.value;
      if (hp?.length || pp?.length || fp?.length) {
        applyData(
          { rows: hp ?? [], summary: [] },
          { rows: pp ?? [], summary: [] },
          { rows: fp ?? [] },
        );
      }
    }

    await loadAll();

    unsub = createPerformanceSocket((msg) => {
      lastRefresh = msg.refreshed_at ?? lastRefresh;
      if (msg.event === 'position_filled') {
        // Kite postback says an order JUST filled — patch the local
        // positions table in place so the operator sees the new qty
        // within a frame, not on the next 5-min performance poll.
        // We also kick off a fresh fetch behind it so the canonical
        // row replaces our optimistic patch within ~500 ms. The patch
        // is additive; if the fetch reveals different numbers (Kite
        // split the fill, partial, etc.) the fresh row overwrites.
        _applyFillDelta(msg);
        _flashFillToast(msg);
        loadAll({ fresh: true });
      } else {
        loadAll();
      }
    });
  });

  // book_changed bus — also covers cancel / reject paths where
  // `position_filled` doesn't fire. Same single-iteration settle
  // contract as the other surfaces. Debounced 200ms upstream.
  let _perfBookCounter = 0;
  $effect(() => {
    const n = $bookChanged;
    if (n <= _perfBookCounter) return;
    _perfBookCounter = n;
    try { loadAll({ fresh: true }); } catch (_) { /* unmounted */ }
  });

  /** Optimistic position patch from a Kite postback `position_filled`
   *  message. msg = {event, account (masked), exchange, tradingsymbol,
   *  qty (signed), fill_price, ts, order_id}. We mutate rawPositions
   *  in place so any reactive derivations (filter, grid feed) pick the
   *  change up immediately; loadAll() runs in parallel to reconcile. */
  function _applyFillDelta(msg) {
    if (!msg || typeof msg.qty !== 'number' || !msg.tradingsymbol) return;
    const acct = msg.account || '';
    const sym  = msg.tradingsymbol;
    const exch = msg.exchange || '';
    const idx = rawPositions.findIndex(r =>
      r.tradingsymbol === sym &&
      r.account       === acct &&
      (!exch || r.exchange === exch));
    if (idx >= 0) {
      // Existing row — apply delta to a copy so Svelte sees the
      // reference change.
      const next = { ...rawPositions[idx] };
      next.quantity   = Number(next.quantity || 0) + Number(msg.qty);
      next.last_price = Number(msg.fill_price) || next.last_price;
      // Mark the row so the grid can flash it briefly.
      next._just_filled = msg.ts || Date.now();
      const copy = rawPositions.slice();
      copy[idx] = next;
      rawPositions = copy;
    }
    // No `else` add-new branch — letting the in-flight loadAll() fetch
    // populate brand-new positions avoids inventing fields (avg_price,
    // close_price, pnl) we can't honestly compute from a fill alone.
    applyAccountFilter();
  }

  /** Brief amber toast at the top-right confirming the fill. Auto-clears
   *  after 3 s. Multiple concurrent fills collapse into the most recent
   *  message — operator doesn't care about old fills. */
  let _fillToast = $state(/** @type {string} */ (''));
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _fillToastTimer = null;
  function _flashFillToast(msg) {
    const side  = (Number(msg.qty) > 0) ? 'BUY' : 'SELL';
    const qty   = Math.abs(Number(msg.qty));
    const sym   = msg.tradingsymbol;
    const price = Number(msg.fill_price);
    _fillToast = `✓ Filled: ${side} ${qty} ${sym}` +
      (price > 0 ? ` @₹${price.toFixed(2)}` : '');
    if (_fillToastTimer) clearTimeout(_fillToastTimer);
    _fillToastTimer = setTimeout(() => { _fillToast = ''; }, 3000);
  }

  onDestroy(() => {
    _unsubPerfOrder();
    unsub?.();
    if (_fillToastTimer) { clearTimeout(_fillToastTimer); _fillToastTimer = null; }
    _perfFlash.dispose();
    [fundsGrid, navGrid, holdingsSummaryGrid, holdingsAllGrid,
     positionsSummaryGrid, positionsAllGrid]
      .forEach(g => g?.destroy());
  });
</script>

<div class:perf-dark={isDark}>

{#if _fillToast}
  <!-- Fill confirmation — fires within a frame of the Kite postback
       `position_filled` event. Lives only 3 s; an actively-trading
       operator sees the broker's ack arrive before they look away from
       the page. -->
  <div class="perf-fill-toast" role="status" aria-live="polite">{_fillToast}</div>
{/if}

{#if error}
  <!-- Graceful banner. Errors fall into two buckets:
         (a) upstream broker outage (Kite is down) — informational tone,
             amber palette + ⚠ icon. The data isn't gone, just stale.
         (b) genuine error (auth, schema, real bug) — restrained red
             palette, still readable on the algo theme's dark navy.
       Both shapes pick up the page's color scheme (light or dark)
       via the perf-dark wrapper. -->
  {@const isOutage = /broker|kite|temporarily unavailable|outage/i.test(error)}
  <div class={'perf-banner ' + (isOutage ? 'perf-banner-outage' : 'perf-banner-error')}
       role="status">
    <span class="perf-banner-icon" aria-hidden="true">{isOutage ? '⏳' : '⚠'}</span>
    <span class="perf-banner-text">{error}</span>
  </div>
{/if}

{#if !compactHeader}
  <!-- Strategy thesis — frames what an unauth visitor is looking at
       before they hit a wall of P&L numbers. Authenticated partners
       already know the strategy, but seeing it consistently above
       the grid reads as "this is what we do" not "this is internal
       ops". Hidden on the algo dashboard (compactHeader=true). -->
  <div class="perf-strategy">
    <span class="perf-strategy-lbl">Strategy:</span>
    <span class="perf-strategy-val">Long stocks + algo-executed derivatives overlay</span>
    <span class="perf-strategy-sep" aria-hidden="true">·</span>
    <span class="perf-strategy-meta">cash + stock-backed margin fund covered calls, futures, spreads, and more</span>
    <span class="perf-strategy-sep" aria-hidden="true">·</span>
    <span class="perf-strategy-meta">positions update live during market hours</span>
  </div>
{/if}

{#if !compactHeader}
  <!-- NAV slice card — only visible on the public /performance layout when
       the visitor is authenticated as partner, designated, or admin. Hidden
       silently for anonymous / demo sessions (fetchMyNav → 401 → swallowed).
       Not rendered on the algo dashboard (compactHeader=true). -->
  <NavCard />
{/if}

{#if !compactHeader}
  <!-- Operator: "all accounts drop down will go before timestamp."
       AccountMultiSelect now lives at the head of the timestamp row
       as a page-level filter — scopes Funds, NAV, Positions
       Summary, Holdings Summary, and both Detail grids in one place. -->
  <div class="perf-ts-row flex items-center gap-3 mb-1.5 pb-1.5">
    {#if accounts.length > 0}
      <div class="acct-multi">
        <AccountMultiSelect
          bind:value={selectedAccounts}
          options={accounts.map(a => ({
            value: a,
            // Trust backend mask. Inline /\d/g lost the broker-ordinal
            // (D1####/D2####) for two-Dhan setups; backend ships the
            // correct shape already.
            label: a,
          }))}
          theme={compactHeader ? 'dark' : 'light'} />
      </div>
    {/if}
    <div class="text-[0.65rem] text-muted perf-ts">
      {#if loading && !lastRefresh}
        <span class="animate-pulse">Loading…</span>
      {:else if lastRefresh}
        <span>{lastRefresh}</span>
      {/if}
    </div>
    <span class="ml-auto"></span>
  </div>
{/if}

<!-- Funds & NAV — account-level card. NAV (default) shows per-account
     wealth (Net + Position M2M + Holdings MTM); Balances shows the
     existing Funds grid (Net, Avail, Util %, Used, Cash, Collateral). -->
<!-- Section heading retired — the AlgoTabs strip itself
     ('NAV' / 'Funds') is the heading. Operator: 'remove NAV and
     funds text completely.' -->
<div class="funds-nav-tabs mb-2">
  <AlgoTabs
    tabs={[
      { id: 'nav',   label: 'NAV'   },
      { id: 'funds', label: 'Funds' },
    ]}
    value={fundsNavTab}
    onChange={(id) => { fundsNavTab = /** @type {'nav'|'funds'} */ (id); }}
    compact={true}
  />
  <GridDownloadButton
    onClick={fundsNavTab === 'nav'
      ? () => navGrid?.exportDataAsCsv({ fileName: 'nav.csv' })
      : () => fundsGrid?.exportDataAsCsv({ fileName: 'funds.csv' })}
    label={fundsNavTab === 'nav' ? 'NAV' : 'Funds'}
  />
</div>
{#if !_agGridReady}
  <div class="perf-grid-loading" role="status" aria-live="polite">Loading grid…</div>
{/if}
<div bind:this={navEl}    class="ag-theme-quartz {theme} mb-2 w-full" class:hidden={fundsNavTab !== 'nav'}></div>
<div bind:this={fundsEl}  class="ag-theme-quartz {theme} mb-2 w-full" class:hidden={fundsNavTab !== 'funds'}></div>

<!-- Tabs — Positions / Holdings. No account picker here; the page-
     level picker above scopes both tabs uniformly. -->
<div class="tabs-row mb-2">
  <AlgoTabs
    tabs={[
      { id: 'positions', label: 'Positions' },
      { id: 'holdings',  label: 'Holdings'  },
    ]}
    value={activeTab}
    onChange={(id) => switchTab(id)}
    compact={true}
  />
</div>

<!-- Operator: "fund balances should be the second element. summary,
     fund balances, positions/holdings is the sequence."

     The summary IS the at-a-glance answer (Day P&L + P&L per account
     for the active tab), Fund Balances is shared margin/cash context
     for the same accounts, and the per-row detail grid is the
     drill-down. Each tab section is split into two: the Summary block
     above the Fund Balances strip and the Detail block below it, so
     the shared Fund Balances renders once between them. -->

<!-- Summary (active tab) -->
<section class:hidden={activeTab !== 'positions'}>
  <CardHeader
    title="Summary"
    showSearch={false}
    onDownload={() => positionsSummaryGrid?.exportDataAsCsv({ fileName: 'positions-summary.csv' })}
    label="Positions Summary"
    detectOverflow={false}
  />
  {#if !_agGridReady}
    <div class="perf-grid-loading" role="status" aria-live="polite">Loading grid…</div>
  {/if}
  <div bind:this={positionsSummaryEl} class="ag-theme-quartz {theme} mb-2 w-full"></div>
</section>

<section class:hidden={activeTab !== 'holdings'}>
  <CardHeader
    title="Summary"
    showSearch={false}
    onDownload={() => holdingsSummaryGrid?.exportDataAsCsv({ fileName: 'holdings-summary.csv' })}
    label="Holdings Summary"
    detectOverflow={false}
  />
  {#if !_agGridReady}
    <div class="perf-grid-loading" role="status" aria-live="polite">Loading grid…</div>
  {/if}
  <div bind:this={holdingsSummaryEl} class="ag-theme-quartz {theme} mb-2 w-full"></div>
</section>

<!-- Fund Balances section retired here — moved into the Funds & NAV
     tabbed card above the Pos/Hold tabs. -->

<!-- Detail (active tab) — the per-symbol drill-down -->
<section class:hidden={activeTab !== 'positions'}>
  <CardHeader
    title="Breakdown"
    bind:filter={_filterPositions}
    onDownload={() => positionsAllGrid?.exportDataAsCsv({ fileName: 'positions.csv' })}
    label="Positions"
    detectOverflow={false}
  />
  <div bind:this={positionsAllEl} class="ag-theme-quartz {theme} w-full"></div>
</section>

<section class:hidden={activeTab !== 'holdings'}>
  <CardHeader
    title="Breakdown"
    bind:filter={_filterHoldings}
    onDownload={() => holdingsAllGrid?.exportDataAsCsv({ fileName: 'holdings.csv' })}
    label="Holdings"
    detectOverflow={false}
  />
  <div bind:this={holdingsAllEl} class="ag-theme-quartz {theme} w-full"></div>
</section>

{#if _chartModalSym}
  <ChartModal
    symbol={_chartModalSym}
    exchange={_chartModalExch}
    onClose={() => { _chartModalSym = ''; _chartModalExch = ''; }} />
{/if}

{#if orderTicketProps}
  <!-- PerformancePage always opens on Ticket tab — no chain for a
       close-position row click, no command bar needed. Chain tab is
       auto-disabled for equity symbols by the shell. -->
  <SymbolPanel
    defaultTab="ticket"
    symbol={orderTicketProps?.symbol}
    exchange={orderTicketProps?.exchange}
    side={orderTicketProps?.side}
    action={orderTicketProps?.action}
    qty={orderTicketProps?.qty}
    lotSize={orderTicketProps?.lotSize}
    accounts={orderTicketProps?.accounts}
    account={orderTicketProps?.account}
    currentQty={orderTicketProps?.currentQty ?? 0}
    onSubmit={(payload) => {
      // PAPER + LIVE submissions already hit the backend before
      // onSubmit fires (the ticket awaits placeTicketOrder). Refresh
      // the grids so the new fill / order shows up without waiting
      // for the next 30 s poll.
      if (payload?.mode !== 'draft') loadAll();
    }}
    onClose={() => orderTicketProps = null}
  />
{/if}

{#if _ctxMenu}
  <SymbolContextMenu
    symbol={_ctxMenu?.symbol}
    exchange={_ctxMenu?.exchange}
    x={_ctxMenu?.x}
    y={_ctxMenu?.y}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      if (action === 'log') {
        openActivityModal();
        _ctxAction = null;
      } else {
        _ctxAction = /** @type {any} */ (action);
      }
      _ctxMenu = null;
    }}
  />
{/if}

{#if _ctxAction === 'chart'}
  <ChartModal
    symbol={_ctxSym}
    exchange={_ctxExch}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'place-order'}
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    onSubmit={() => {}}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

<!-- ActivityLogModal singleton lives in the (algo) layout via the
     activityModal store. The context-menu handler now opens it via
     openActivityModal() so PerformancePage doesn't mount a second
     instance that races the layout's. -->


</div><!-- /perf-dark -->

<style>
  /* Grid loading placeholder — shown while the ag-Grid dynamic import
     resolves (~100–300ms on first visit). Minimal height so the page
     doesn't jump when the real grid paints below it. */
  .perf-grid-loading {
    padding: 0.6rem 0.25rem;
    font-size: var(--fs-md);
    color: var(--c-muted);
    font-family: var(--font-numeric);
    letter-spacing: 0.03em;
  }

  /* Public /performance: no inner cream card. Content sits directly
     on the public layout's <main class="pub-content"> wrapper. The
     only chrome is the hairline divider below the timestamp/Refresh
     row. Color sourced from --card-ts-divider so the parent wrapper
     class (.card-theme-cream / .card-theme-dark) controls the tone. */
  .perf-ts-row {
    border-bottom: 1px solid var(--card-ts-divider, #d8d4cc);
  }
  /* Strategy thesis — operator: "flip card decoration between first
     and second cards in performance page." Strategy traded its
     prominent .pub-callout (warm #f0ead8 + #d4c89f) for the softer
     palette that NavCard used to carry (#faf7f0 + #e0d9cc). NavCard
     now carries the prominent treatment so the partner's NAV slice
     reads as the primary card.

     Colors sourced from --card-strategy-* vars so the parent wrapper
     class (.card-theme-cream / .card-theme-dark) controls the palette. */
  .perf-strategy {
    background: var(--card-strategy-bg, #faf7f0);
    border: 1px solid var(--card-strategy-border, #e0d9cc);
    border-radius: 0.3rem;
    padding: 0.45rem 0.75rem;
    margin-bottom: 0.6rem;
    font-size: var(--fs-lg);
    line-height: 1.4;
    color: var(--card-strategy-text, #1e3050);
  }
  .perf-strategy-lbl {
    font-weight: 700;
    color: var(--card-strategy-lbl, #5a7090);
    text-transform: uppercase;
    font-size: var(--fs-sm);
    letter-spacing: 0.08em;
    margin-right: 0.4rem;
  }
  .perf-strategy-val {
    font-weight: 700;
    color: var(--card-strategy-val, #0c1830);
  }
  .perf-strategy-meta {
    color: var(--card-strategy-meta, #5a7090);
    font-size: var(--fs-lg);
  }
  /* Inline separator dot — explicit baseline alignment so it doesn't
     float relative to the surrounding text the way an inline '·'
     literal does (the unicode middle-dot's metrics put it noticeably
     above the cap-line of the headline font). margin gives the dot
     consistent breathing room on both sides. Hidden on mobile where
     the meta clauses each break to their own line. */
  .perf-strategy-sep {
    display: inline-block;
    color: var(--card-strategy-sep, #c8a84b);
    font-weight: 600;
    margin: 0 0.45rem;
    vertical-align: baseline;
    position: relative;
    top: -0.05em;
  }
  @media (max-width: 600px) {
    .perf-strategy-meta { display: block; margin-top: 0.15rem; font-size: var(--fs-md); }
    .perf-strategy-sep  { display: none; }
  }

  .hidden { display: none; }

  /* ── Page banners ────────────────────────────────────────────────
     Two flavours, both palette-aware:

       .perf-banner-outage  — Kite (or any upstream) is temporarily
         unavailable. Informational, amber, calm copy. The data isn't
         gone, the broker just isn't responding right now. ⏳ icon.

       .perf-banner-error   — genuine error (auth / schema / bug). ⚠
         icon, red palette but toned down so it doesn't shout against
         the algo theme's dark navy.

     Both render with the same shell (gap, radius, padding, monospace
     font) so the layout doesn't shift between flavours. */
  .perf-banner {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    border: 1px solid;
    font-size: var(--fs-lg);
    line-height: 1.25;
    margin-bottom: 0.75rem;
    font-family: var(--font-numeric);
  }
  /* Fill toast — momentary confirmation that a Kite postback fired.
     Sticks to the top-right, fades in/out, doesn't push the page
     content down (position: fixed). Amber accent matches the algo
     theme's "money" tone. */
  .perf-fill-toast {
    position: fixed;
    top: 0.75rem;
    right: 0.75rem;
    z-index: 1000;
    background: rgba(251,191,36,0.18);
    border: 1px solid var(--algo-amber-border);
    color: var(--c-action);
    padding: 0.4rem 0.7rem;
    border-radius: 4px;
    font-size: var(--fs-lg);
    font-weight: 700;
    font-family: var(--font-numeric);
    box-shadow: 0 2px 10px rgba(0,0,0,0.35);
    animation: perf-fill-toast-in 0.18s ease-out;
  }
  @keyframes perf-fill-toast-in {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .perf-fill-toast { animation: none; }
  }
  .perf-banner-icon {
    font-size: var(--fs-xl);
    line-height: 1;
    flex: 0 0 auto;
  }
  .perf-banner-text {
    flex: 1 1 auto;
  }

  /* Light (cream) theme banners — these only render outside .perf-dark
     (public /performance). The dark overrides below take over when
     theme='ag-theme-algo'. Hard-coded here intentionally: banners are
     semantic (outage = amber, error = red) and the colors don't change
     between cream and dark themes — only the background opacity does.
     The .perf-dark overrides below handle the dark variant. */
  .perf-banner-outage {
    background: #fff8e8;
    border-color: #e8c97a;
    color: #6b4500;
  }
  .perf-banner-error {
    background: #fdf0f0;
    border-color: #e8a3a3;
    color: #7a2929;
  }

  /* Dark (algo) theme — both inherit the navy/amber/red token set
     used across the algo pages so the banner reads as part of the
     page, not a foreign element. */
  .perf-dark .perf-banner-outage {
    background: rgba(251,191,36,0.10);
    border-color: rgba(251,191,36,0.35);
    color: #fde68a;
  }
  .perf-dark .perf-banner-outage .perf-banner-icon {
    color: var(--c-action);
  }
  .perf-dark .perf-banner-error {
    background: var(--algo-red-bg);
    border-color: rgba(248,113,113,0.35);
    color: #fda4a4;
  }
  .perf-dark .perf-banner-error .perf-banner-icon {
    color: var(--c-short);
  }

  /* Tabs + Account + Symbol on one row — keep them all visible on
     narrow widths by setting `flex-wrap: nowrap` and tightening font
     + padding on mobile. Deliberately NOT wrapping because the whole
     point of putting filters on the tabs row is "always at a glance". */
  .tabs-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: nowrap;
  }

  /* Light-theme tab decoration — champagne bottom-border + faint
     champagne background tint on active. Mirrors the AlgoTabs
     active-state shape used by the NAV/Funds tab strip directly
     above (canonical .algo-tab[aria-selected="true"] in app.css
     ships background: rgba(251,191,36,0.10) on amber). Operator:
     "have nav funds background decoration to other tabs in public
     pages." Same shape across all public-page tabs now — pure
     underline + 10% color tint on the active label.
     Colors sourced from --card-active-* vars set by the parent
     wrapper class (.card-theme-cream / .card-theme-dark). */
  .tabs-row :global(button[class*="border-primary"]) {
    border-bottom-color: var(--card-active-border, rgba(212, 146, 12, 0.5)) !important;
    background: var(--card-active-row-bg, rgba(212, 146, 12, 0.13)) !important;
  }
  .tabs-row :global(button[class*="text-muted"]:hover) {
    border-bottom-color: var(--card-active-border, rgba(212, 146, 12, 0.5)) !important;
    background: rgba(212, 146, 12, 0.07) !important;
  }
  /* Account dropdown wrapper. Theme + colour handled inside
     AccountMultiSelect. Tightened from 8.5rem → 7rem so the
     timestamp + Refresh button keep room on the same row even when
     the picker shows a multi-account selection (operator: "reduce
     all accounts size slightly smaller so that timestamp row fits
     in the same row"). */
  .acct-multi {
    width: 7rem;
    min-width: 0;
  }

  /* Mobile — the dropdown tightens further, tabs stay full size. */
  @media (max-width: 639px) {
    .tabs-row { gap: 0.3rem; }
    .acct-multi { width: 6rem; }
  }
  /* Funds & NAV tabs — sub-tab strip rendered by AlgoTabs. The
     canonical AlgoTabs amber palette (var(--c-action) + rgba(251,191,36,0.10)
     fill) is overridden here to champagne so this strip matches the
     Positions/Holdings tabs immediately below it. Operator: "make
     nav and funds tabs decoration to be similar to positions/
     holdings."

     Same shape now across both tab strips on /performance:
       • inactive text muted slate-blue (unchanged)
       • active text + underline navy/champagne
       • 10% champagne fill behind the active label
       • faint champagne fill on hover for symmetry */
  .funds-nav-tabs {
    display: flex;
  }
  .funds-nav-tabs :global(.algo-tab[aria-selected="true"]) {
    color: var(--card-active-row-text, #1a2744) !important;
    border-bottom-color: var(--card-active-border, rgba(212, 146, 12, 0.5)) !important;
    background: var(--card-active-row-bg, rgba(212, 146, 12, 0.13)) !important;
  }
  .funds-nav-tabs :global(.algo-tab:hover:not([aria-selected="true"])) {
    border-bottom-color: var(--card-active-border, rgba(212, 146, 12, 0.5)) !important;
    background: rgba(212, 146, 12, 0.07) !important;
  }

  /* ── Dark (algo) overrides ─────────────────────────────────────────────── */

  /* Refresh button */
  .perf-dark :global(.btn-secondary) {
    color: var(--algo-slate);
    border-color: #2a4060;
    background: transparent;
  }
  .perf-dark :global(.btn-secondary:hover:not(:disabled)) { background: rgba(255,255,255,0.06); }

  /* Dashboard timestamp — yellow to match log and algo-ts timestamps */
  .perf-dark :global(.perf-ts) { color: #fde68a !important; }

  /* Options deep-link pill — amber accent, scoped to .perf-dark so the
     algo-amber palette can't leak onto the public /performance grid
     even if a future caller flips enableOptionsLink=true on the light
     theme. Appears as a small "→" arrow after the symbol text. */
  .perf-dark :global(.perf-opts-link) {
    display: inline-block;
    font-size: var(--fs-xs);
    font-weight: 600;
    padding: 0 0.28rem;
    border-radius: 0.18rem;
    background: rgba(251,191,36,0.15);
    color: var(--c-action);
    border: 1px solid rgba(251,191,36,0.40);
    text-decoration: none;
    line-height: 1.4;
    cursor: pointer;
    flex-shrink: 0;
  }
  .perf-dark :global(.perf-opts-link:hover) {
    background: rgba(251,191,36,0.28);
    color: #fde68a;
  }

  /* ── Account identity stripe + dot ──────────────────────────────────────
     The left-border colour is injected per-cell via cellStyle as
     --acct-stripe. TOTAL rows receive --acct-stripe:transparent (no stripe).
     Both rules are :global because they target elements produced by the
     AG Grid cellRenderer (outside Svelte's scoped DOM).  */
  :global(.ag-col-acct) {
    border-left: 3px solid var(--acct-stripe, transparent) !important;
  }
</style>
