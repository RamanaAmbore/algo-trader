<script>
  import { onMount, onDestroy } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import ChartModal from '$lib/ChartModal.svelte';
  import { fetchHoldings, fetchPositions, fetchFunds } from '$lib/api';
  import { createPerformanceSocket } from '$lib/ws';
  import { dataCache, authStore } from '$lib/stores';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import GridSearchButton from '$lib/GridSearchButton.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { instrumentsCacheVersion } from '$lib/data/instruments';

  // Module-scope cache for hyphenated display strings. ag-Grid
  // re-runs cellRenderer on every redraw — a Map cache avoids
  // re-parsing each symbol N×rows×renders. Cleared on grid teardown
  // via onDestroy below, AND on every instrumentsCacheVersion bump
  // (see effect at bottom of <script>): the cache otherwise pins the
  // cold-render value (no expiry-day appended) at first paint and
  // never picks up the per-symbol expiry once the instruments
  // dump finishes loading.
  const _symFmtCache = new Map();
  function _fmtSymCached(sym) {
    if (!sym) return '';
    let v = _symFmtCache.get(sym);
    if (v === undefined) {
      v = formatSymbol(sym);
      _symFmtCache.set(sym, v);
    }
    return v;
  }
  import ActivityLogModal from '$lib/ActivityLogModal.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import AccountMultiSelect from '$lib/AccountMultiSelect.svelte';
  import { getInstrument, loadInstruments } from '$lib/data/instruments';
  import { lotsForRow, fmtLots } from '$lib/data/lotsForRow';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';
  import { priceFmt, pctFmt, aggCompact } from '$lib/format';
  import NavCard from '$lib/NavCard.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';

  ModuleRegistry.registerModules([AllCommunityModule]);

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
  // Effective mask flag — admin/designated never see masked codes,
  // regardless of what the parent passed. The prop still wins for
  // partner / demo (the default `true`) and for any future caller
  // that wants to force-mask even an admin session.
  const maskAccounts = $derived(
    ($authStore.user?.role === 'admin' || $authStore.user?.role === 'designated')
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
    if (!allowOrders || $authStore.user?.role !== 'admin') return;
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
      // Hide DRAFT — no drafts surface here. PAPER is the safe
      // default; operator opts into LIVE per execution flag.
      defaultMode:    'live',
      availableModes: ['live'],
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
  // Multi-select: empty array ⇒ "all symbols". Populated array ⇒ only
  // those symbols show in the detail grid under the active tab.
  let selectedSymbols = $state(/** @type {string[]} */([]));
  let accounts        = $state([]);
  let positionSymbols = $state(/** @type {string[]} */([]));
  let holdingSymbols  = $state(/** @type {string[]} */([]));
  const symbols = $derived(activeTab === 'holdings' ? holdingSymbols : positionSymbols);
  let rawHoldings     = $state([]);
  let rawPositions    = $state([]);
  let rawFunds        = $state([]);
  let rawHoldingsSummary  = $state([]);
  let rawPositionsSummary = $state([]);

  // Total cur_val across the currently visible holdings — used by the
  // Weight % valueGetter. Updated whenever the filter applies. Plain
  // mutable variable (not $state) because it's only read inside the
  // synchronous valueGetter closure that AG Grid invokes per cell.
  let _holdingsTotalCurVal = 0;

  // Static grid refs
  let fundsEl            = null;
  let holdingsSummaryEl  = null;
  let holdingsAllEl      = null;
  let positionsSummaryEl = null;
  let positionsAllEl     = null;

  let fundsGrid            = null;
  let holdingsSummaryGrid  = null;
  let holdingsAllGrid      = null;
  let positionsSummaryGrid = null;
  let positionsAllGrid     = null;

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
  // BANKNIFTY25MAYFUT, CRUDEOIL25MAYFUT, …). For plain equity symbols
  // with no digits (RELIANCE, SBIN, …) this is a no-op, which is the
  // right answer for holdings.
  function underlyingOf(/** @type {string} */ sym) {
    return (sym || '').replace(/\d.*$/, '');
  }

  // AG Grid valueFormatter wrappers — receive { value } objects.
  const numFmt = ({ value }) => value == null ? '' : priceFmt(value);
  const aggFmtGrid = ({ value }) => value == null ? '' : aggCompact(value);
  const pctFmtGrid = ({ value }) => value == null ? '' : `${pctFmt(value)}%`;
  // Theme-aware P&L colors — actual colors live in app.css keyed to the grid theme.
  // Include 'ag-right-aligned-cell' because user-provided cellClass overrides the
  // class AG Grid adds via type: 'numericColumn'.
  const pnlCls = ({ value }) =>
    ['ag-right-aligned-cell', value < 0 ? 'pnl-loss' : value > 0 ? 'pnl-gain' : 'pnl-zero'];
  // Qty cell: classify by direction, not P&L. A short can be profitable,
  // a long can be losing — what the eye needs here is "which side of the
  // book am I on". Colours live in app.css (qty-short / qty-long).
  const qtyCls = ({ value }) =>
    ['ag-right-aligned-cell', value < 0 ? 'qty-short' : value > 0 ? 'qty-long' : 'qty-flat'];
  const avgVsLtpCls = (params) => {
    const avg = params.data?.average_price;
    // Prefer the live last_price; fall back to close_price for rows
    // produced before the schema gained last_price (cache-warm path).
    const ltp = params.data?.last_price ?? params.data?.close_price;
    if (avg == null || ltp == null) return 'ag-right-aligned-cell';
    return ['ag-right-aligned-cell', avg > ltp ? 'pnl-loss' : avg < ltp ? 'pnl-gain' : 'pnl-zero'];
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
    '#7dd3fc', // sky
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
  function acctCellRenderer(params) {
    const raw     = params.value || '';
    return maskAccounts && raw ? String(raw).replace(/\d/g, '#') : raw;
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
    // Action-first order — Day P&L leads, Account trails. Operator scans
    // numbers first (today's move → carry → value) before routing context.
    { field: 'day_change_val',        headerName: 'Day P&L',  width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',    width: 78,  valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                   headerName: 'P&L',      width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',        headerName: 'P&L %',    width: 78,  valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'cur_val',               headerName: 'Value',  width: 110, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'inv_val',               headerName: 'Invested',  width: 110, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'account',               headerName: 'Account',  width: 76,  minWidth: 76,  cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
  ];

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
    { field: 'average_price',         headerName: 'Avg', width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'day_change_val',        headerName: 'Day P&L',  width: 78, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',    width: 60, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                   headerName: 'P&L',      width: 78, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',        headerName: 'P&L %',    width: 60, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'close_price',           headerName: 'Prev Close', width: 78, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
    { field: 'quantity',              headerName: 'Qty',      width: 52, type: 'numericColumn', headerClass: numericHdr },
    // Lots — qty in F&O lot units. Holdings on F&O underlyings use
    // the underlying lot; option / futures positions use the contract
    // lot. Non-F&O rows read 0. Same shared helper that powers Pulse
    // so both pages report the same number for the same row.
    { field: 'lots', headerName: 'Lots', width: 52, type: 'numericColumn', headerClass: numericHdr,
      valueGetter: (p) => lotsForRow(p.data),
      valueFormatter: ({ value }) => fmtLots(value),
      headerTooltip: 'Qty in F&O lot units. 0 when the symbol is not an option underlying.' },
    // Weight % = this row's cur_val / total cur_val across the visible
    // holdings filter. Computed in valueGetter so it tracks the AG Grid
    // row-filter live (per-account view stays meaningful).
    { field: 'weight_pct',            headerName: 'Weight %', width: 70, type: 'numericColumn', headerClass: numericHdr,
      valueGetter: (p) => {
        const cv = Number(p.data?.cur_val) || 0;
        const total = _holdingsTotalCurVal;
        return total > 0 ? (cv / total) * 100 : null;
      },
      valueFormatter: pctFmtGrid },
    { field: 'cur_val',               headerName: 'Value',  width: 88, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'account',               headerName: 'Account',  width: 76, cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
  ];

  // Positions summary — same action-first ordering as holdings summary.
  const positionsSummaryCols = [
    { field: 'day_change_val',        headerName: 'Day P&L', width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',   width: 78,  valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                   headerName: 'P&L',     width: 110, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'account',               headerName: 'Account', width: 76,  cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
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
    txt.textContent = sym === 'TOTAL' ? sym : _fmtSymCached(sym);
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
    span.textContent = sym === 'TOTAL' ? sym : _fmtSymCached(sym);
    if (sym && sym !== 'TOTAL') {
      // data attrs for context menu delegation
      span.setAttribute('data-sym',  sym);
      span.setAttribute('data-exch', exch);
      span.className = 'perf-sym-cell';
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
    { field: 'average_price',        headerName: 'Avg', width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'day_change_val',       headerName: 'Day P&L',   width: 88, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage',headerName: 'Day %',     width: 64, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                  headerName: 'P&L',       width: 88, valueFormatter: aggFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',       headerName: 'P&L %',     width: 60, valueFormatter: pctFmtGrid, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'close_price',          headerName: 'Prev Close', width: 78, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
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
    { field: 'delta_pos',            headerName: 'Δ pos',     width: 62,
      type: 'numericColumn', headerClass: numericHdr, cellClass: pnlCls,
      valueFormatter: ({ value }) => value == null || value === 0 ? '—' : value.toFixed(2) },
    { field: 'theta_pos',            headerName: 'Θ/day',     width: 62,
      type: 'numericColumn', headerClass: numericHdr, cellClass: pnlCls,
      valueFormatter: ({ value }) => value == null || value === 0 ? '—' : aggFmtGrid({ value }) },
    { field: 'account',       headerName: 'Account',   width: 76, cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
  ]);

  // Order priority: Net (real balance) → Avail Margin → Utilisation %
  // (headroom) → Used Margin → Cash → Collateral. Operator decides
  // "can I deploy more" off Net + Utilisation %; the components
  // follow. Kite + IBKR both lead with the summary number, not Cash.
  const fundsCols = [
    { field: 'account',      headerName: 'Account',      width: 76, cellClass: acctFill, headerClass: acctFill, cellRenderer: acctCellRenderer, cellStyle: acctCellStyle },
    { headerName: 'Net', flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr, cellClass: pnlCls,
      valueGetter: (p) => (Number(p.data?.cash) || 0) + (Number(p.data?.collateral) || 0) - (Number(p.data?.used_margin) || 0) },
    { field: 'avail_margin', headerName: 'Avail Margin', flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { headerName: 'Util %', flex: 1, valueFormatter: pctFmtGrid, type: 'numericColumn', headerClass: numericHdr,
      valueGetter: (p) => {
        const used = Number(p.data?.used_margin) || 0;
        const avail = Number(p.data?.avail_margin) || 0;
        const denom = used + avail;
        return denom > 0 ? (used / denom) * 100 : null;
      } },
    { field: 'used_margin',  headerName: 'Used Margin',  flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'cash',         headerName: 'Cash',         flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
    { field: 'collateral',   headerName: 'Collateral',   flex: 1, valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
  ];

  function makeGrid(el, colDefs, rowData = [], onRowClick = null) {
    return createGrid(el, {
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
      overlayNoRowsTemplate: '<span style="font-size:0.65rem;color:#7e97b8">—</span>',
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
      cur_val:               total_cur_val,
    };
  }

  function makePositionsTotals(rows) {
    if (!rows?.length) return null;
    const sum = (f) => rows.reduce((s, r) => s + (Number(r[f]) || 0), 0);
    // Aggregate denominators are absolute (qty can be ±) — short and
    // long positions both contribute to capital deployed.
    const total_pnl        = sum('pnl');
    const total_day_change = sum('day_change_val');
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

  function applyAccountFilter() {
    if (!holdingsAllGrid) return;
    // ACCOUNT filter scopes every grid (detail + summary + funds). With a
    // specific account picked we drop other accounts AND the TOTAL row.
    // SYMBOL filter scopes ONLY the last (detail) aggrid — summary and
    // funds are per-account aggregates that don't reduce cleanly to a
    // single symbol, so they stay on the account-level view.
    const keepAcct = (r) => selectedAccounts.length === 0
      ? true
      : selectedAccounts.includes(String(r.account || ''));
    // Empty selection ⇒ "all". Otherwise a row matches when either its
    // full tradingsymbol or its derived underlying is in the set. That
    // dual match lets the Positions tab filter by underlying (NIFTY,
    // BANKNIFTY, RELIANCE, …) while Holdings keeps matching on the
    // straight equity symbol. Underlyings are never in the holdings
    // list, and tradingsymbols are never in the positions list, so
    // the double-check is safe.
    const keepSym  = (r) => !selectedSymbols.length
      || selectedSymbols.includes(r.tradingsymbol)
      || selectedSymbols.includes(underlyingOf(r.tradingsymbol));
    // Stable sort with closed (qty=0) rows at the end. Kite returns
    // closed intraday positions / sold-off holdings with quantity=0
    // so realised P/L stays visible — operators want them grouped
    // last, not interleaved with live exposure.
    const closedLast = (a, b) => {
      const ac = (Number(a?.quantity || 0) === 0) ? 1 : 0;
      const bc = (Number(b?.quantity || 0) === 0) ? 1 : 0;
      return ac - bc;
    };
    const hRows = rawHoldings.filter(r => keepAcct(r) && keepSym(r))
      .slice().sort(closedLast);
    const pRows = rawPositions.filter(r => keepAcct(r) && keepSym(r))
      .slice().sort(closedLast);
    // Recompute total cur_val so the Weight % column always reflects
    // the currently-filtered view (per-account picks change the base).
    _holdingsTotalCurVal = hRows.reduce(
      (s, r) => s + (Number(r.cur_val) || 0), 0);
    const hSummary  = rawHoldingsSummary.filter(keepAcct);
    const pSummary  = rawPositionsSummary.filter(keepAcct);
    const fRows     = rawFunds.filter(keepAcct);
    const hTotals   = makeHoldingsTotals(hRows);
    const pTotals   = makePositionsTotals(pRows);
    // Split TOTAL out of summary + funds data sets and pin to the
    // bottom — pinned-bottom rows in AG Grid are immune to sort, so
    // the TOTAL always anchors the last row regardless of which
    // column the operator clicks on.
    const isTotalRow = (/** @type {any} */ r) =>
      r?.tradingsymbol === 'TOTAL' || r?.account === 'TOTAL';
    const hSummaryBody  = hSummary.filter(r => !isTotalRow(r));
    const hSummaryTotal = hSummary.filter(isTotalRow);
    const pSummaryBody  = pSummary.filter(r => !isTotalRow(r));
    const pSummaryTotal = pSummary.filter(isTotalRow);
    const fBody         = fRows.filter(r => !isTotalRow(r));
    const fTotal        = fRows.filter(isTotalRow);
    updateGrid(holdingsSummaryGrid, hSummaryBody);
    holdingsSummaryGrid.setGridOption('pinnedBottomRowData', hSummaryTotal);
    updateGrid(positionsSummaryGrid, pSummaryBody);
    positionsSummaryGrid.setGridOption('pinnedBottomRowData', pSummaryTotal);
    updateGrid(holdingsAllGrid, hRows);
    holdingsAllGrid.setGridOption('pinnedBottomRowData', hTotals ? [hTotals] : []);
    updateGrid(positionsAllGrid, pRows);
    positionsAllGrid.setGridOption('pinnedBottomRowData', pTotals ? [pTotals] : []);
    updateGrid(fundsGrid, fBody);
    fundsGrid.setGridOption('pinnedBottomRowData', fTotal);
    // Account column hides across every grid when exactly ONE account
    // is picked (no need to repeat the same account on every row).
    // Empty selection (all accounts) and multi-pick keep the column
    // visible so the operator can read which row belongs where.
    const showAcct = selectedAccounts.length !== 1;
    for (const g of [holdingsAllGrid, positionsAllGrid, fundsGrid, holdingsSummaryGrid, positionsSummaryGrid]) {
      try { g?.setColumnsVisible?.(['account'], showAcct); } catch (_) { /* older AG API */ }
    }
  }

  function applyData(h, p, f) {
    rawHoldings         = h.rows ?? [];
    rawPositions        = p.rows ?? [];
    rawHoldingsSummary  = h.summary ?? [];
    rawPositionsSummary = p.summary ?? [];
    rawFunds            = f.rows ?? [];
    const allAccts = [...new Set([...rawHoldings.map(r => r.account), ...rawPositions.map(r => r.account)])];
    accounts = allAccts;
    // Two separate symbol lists — the dropdown narrows to just what the
    // active tab needs, so Positions never shows holding-only symbols
    // (and vice versa).
    // Positions dropdown lists UNDERLYINGS (NIFTY, BANKNIFTY, RELIANCE,
    // …) so one pick scopes every option / future / cash-equity position
    // on that underlying at once. Holdings keeps the full tradingsymbol
    // since holdings are typically equities with no derived-from
    // hierarchy to collapse.
    positionSymbols = [...new Set(rawPositions.map(r => underlyingOf(r.tradingsymbol)))]
      .filter(Boolean).sort();
    holdingSymbols  = [...new Set(rawHoldings.map(r => r.tradingsymbol))]
      .filter(Boolean).sort();
    // Drop any selected symbols that no longer exist in the currently-
    // visible (tab-scoped) list — keeps the filter honest when symbols
    // get closed out, renamed, or aren't in the active tab's book.
    reconcileSymbols();
    lastRefresh = h.refreshed_at ?? '';
    applyAccountFilter();
  }

  function reconcileSymbols() {
    const visible = (activeTab === 'holdings' ? holdingSymbols : positionSymbols);
    const kept = selectedSymbols.filter(s => visible.includes(s));
    if (kept.length !== selectedSymbols.length) selectedSymbols = kept;
  }

  // Switching tabs changes which symbol list the picker shows; reconcile
  // the selection so stale symbols don't hold the grid empty.
  $effect(() => {
    activeTab; holdingSymbols; positionSymbols;
    reconcileSymbols();
  });

  $effect(() => {
    // Track account + symbol filters + active tab. activeTab is in here
    // so the filter re-runs on tab switch (defensive — the grids already
    // hold the right rows since applyAccountFilter runs on every data
    // refresh, but re-running on tab-switch guards against any edge
    // case where the tab-scoped symbol list reconciliation runs
    // mid-flight).
    selectedAccounts; selectedSymbols; activeTab;
    applyAccountFilter();
  });

  async function loadAll({ fresh = false } = {}) {
    loading = true; error = '';
    try {
      const [h, p, f] = await Promise.all([
        fetchHoldings({ fresh }),
        fetchPositions({ fresh }),
        fetchFunds({ fresh }),
      ]);
      dataCache.holdings  = h;
      dataCache.positions = p;
      dataCache.funds     = f;
      applyData(h, p, f);
    } catch (e) {
      error = e.message || 'Failed to load data';
    } finally { loading = false; }
  }

  let unsub;

  onMount(async () => {
    holdingsSummaryGrid  = makeGrid(holdingsSummaryEl,  holdingsSummaryCols);
    holdingsAllGrid      = makeGrid(holdingsAllEl,      holdingsCols, [], (r) => openOrderTicket(r, 'holdings'));
    positionsSummaryGrid = makeGrid(positionsSummaryEl, positionsSummaryCols);
    positionsAllGrid     = makeGrid(positionsAllEl,     positionsCols, [], (r) => openOrderTicket(r, 'positions'));
    fundsGrid            = makeGrid(fundsEl,             fundsCols);

    if (dataCache.holdings && dataCache.positions && dataCache.funds) {
      applyData(dataCache.holdings, dataCache.positions, dataCache.funds);
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
    unsub?.();
    [fundsGrid, holdingsSummaryGrid, holdingsAllGrid,
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
  <!-- Default layout: timestamp + Refresh button on their own line, tabs
       below. The public /performance page uses this. -->
  <div class="perf-ts-row flex items-center justify-between mb-1.5 pb-1.5">
    <div class="text-[0.65rem] text-muted perf-ts">
      {#if loading && !lastRefresh}
        <span class="animate-pulse">Loading…</span>
      {:else if lastRefresh}
        <span>{lastRefresh}</span>
      {/if}
    </div>
    <RefreshButton onClick={() => loadAll({ fresh: true })} {loading} label="performance" />
  </div>
{/if}

<!-- Tabs + account selector. With `compactHeader`, the refresh timestamp
     joins this row as the last element (no Refresh button — the
     performance WebSocket already handles auto-refresh). -->
<div class="tabs-row mb-2">
  <div class="flex gap-0.5">
    {#each [['positions','Positions'],['holdings','Holdings']] as [id, label]}
      <button
        class="px-3 py-1 text-xs font-medium border-b-2 transition-colors
               {activeTab === id ? 'border-primary text-primary' : 'border-transparent text-muted hover:text-text'}"
        onclick={() => switchTab(id)}
      >{label}</button>
    {/each}
  </div>
  {#if accounts.length > 0}
    <!-- Account picker — always enabled here because PerformancePage
         is fundamentally about per-account positions/holdings (no
         non-account context to disable for). Mirrors the Symbol
         MultiSelect next to it for visual consistency. -->
    <div class="acct-multi">
      <AccountMultiSelect
        bind:value={selectedAccounts}
        options={accounts.map(a => ({ value: a, label: maskAccounts ? String(a ?? '').replace(/\d/g, '#') : a }))}
        theme={compactHeader ? 'dark' : 'light'} />
    </div>
  {/if}
  {#if symbols.length > 0}
    <!-- Multi-select: empty array ⇒ "all symbols"; any non-empty
         selection ⇒ filter the active tab's detail grid to that set. -->
    <div class="sym-multi">
      <MultiSelect
        bind:value={selectedSymbols}
        options={symbols.map(s => ({ value: s, label: s }))}
        placeholder="Symbols"
        theme={compactHeader ? 'dark' : 'light'} />
    </div>
  {/if}
</div>

<!-- Fund Balances heading — on compactHeader layouts (the admin
     dashboard) the Refresh button sits on this row instead of crowding
     the tabs / filter row above. Public /performance keeps its
     top-of-page Refresh button. -->
<div class="funds-heading-row">
  <h2 class="section-heading funds-heading-title">Fund Balances</h2>
  {#if compactHeader}
    <span class="funds-heading-refresh">
      <RefreshButton onClick={() => loadAll({ fresh: true })} {loading} label="funds" />
    </span>
  {/if}
</div>
<div bind:this={fundsEl} class="ag-theme-quartz {theme} mb-2 w-full"></div>

<section class:hidden={activeTab !== 'positions'}>
  <h2 class="section-heading">Summary</h2>
  <div bind:this={positionsSummaryEl} class="ag-theme-quartz {theme} mb-2 w-full"></div>

  <div class="perf-grid-headrow">
    <h2 class="section-heading">Positions</h2>
    <span class="perf-grid-headrow-spacer"></span>
    <GridSearchButton bind:filter={_filterPositions} label="Positions" />
  </div>
  <div bind:this={positionsAllEl} class="ag-theme-quartz {theme} w-full"></div>
</section>

<section class:hidden={activeTab !== 'holdings'}>
  <h2 class="section-heading">Summary</h2>
  <div bind:this={holdingsSummaryEl} class="ag-theme-quartz {theme} mb-2 w-full"></div>

  <div class="perf-grid-headrow">
    <h2 class="section-heading">Holdings</h2>
    <span class="perf-grid-headrow-spacer"></span>
    <GridSearchButton bind:filter={_filterHoldings} label="Holdings" />
  </div>
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
    symbol={orderTicketProps.symbol}
    exchange={orderTicketProps.exchange}
    side={orderTicketProps.side}
    action={orderTicketProps.action}
    qty={orderTicketProps.qty}
    lotSize={orderTicketProps.lotSize}
    accounts={orderTicketProps.accounts}
    account={orderTicketProps.account}
    defaultMode={orderTicketProps.defaultMode}
    availableModes={orderTicketProps.availableModes}
    currentQty={orderTicketProps.currentQty ?? 0}
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
    symbol={_ctxMenu.symbol}
    exchange={_ctxMenu.exchange}
    x={_ctxMenu.x}
    y={_ctxMenu.y}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      _ctxAction = /** @type {any} */ (action);
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

{#if _ctxAction === 'log'}
  <ActivityLogModal onClose={() => { _ctxAction = null; }} />
{/if}

</div><!-- /perf-dark -->

<style>
  /* Public /performance: no inner cream card. Content sits directly
     on the public layout's <main class="pub-content"> wrapper. The
     only chrome is the hairline divider below the timestamp/Refresh
     row. */
  .perf-ts-row {
    border-bottom: 1px solid #d8d4cc;
  }
  /* Tighter heading-to-grid gap on the public side. The .perf-strategy
     marker (always present on the public route, absent on the algo
     dashboard) scopes this and every other public-only style below. */
  :global(.perf-strategy ~ section .section-heading),
  :global(.perf-strategy ~ .funds-heading-row .funds-heading-title) {
    margin-bottom: 0.25rem;
  }

  /* Strategy thesis — single-line frame above the data grids on the
     public side. Inherits public-side palette tokens (navy text on
     warm cream chip background, champagne left-accent). On the algo
     side this block is hidden (compactHeader=true). */
  .perf-strategy {
    background: #f5f2eb;
    border: 1px solid #e7e0cf;
    border-left: 3px solid #d4920c;
    border-radius: 0.3rem;
    padding: 0.45rem 0.75rem;
    margin-bottom: 0.6rem;
    font-size: 0.75rem;
    line-height: 1.4;
    color: #1e3050;
  }
  .perf-strategy-lbl {
    font-weight: 700;
    color: #5a7090;
    text-transform: uppercase;
    font-size: 0.6rem;
    letter-spacing: 0.08em;
    margin-right: 0.4rem;
  }
  .perf-strategy-val {
    font-weight: 700;
    color: #0c1830;
  }
  .perf-strategy-meta {
    color: #5a7090;
    font-size: 0.7rem;
  }
  /* Inline separator dot — explicit baseline alignment so it doesn't
     float relative to the surrounding text the way an inline '·'
     literal does (the unicode middle-dot's metrics put it noticeably
     above the cap-line of the headline font). margin gives the dot
     consistent breathing room on both sides. Hidden on mobile where
     the meta clauses each break to their own line. */
  .perf-strategy-sep {
    display: inline-block;
    color: #c8a84b;
    font-weight: 600;
    margin: 0 0.45rem;
    vertical-align: baseline;
    position: relative;
    top: -0.05em;
  }
  @media (max-width: 600px) {
    .perf-strategy-meta { display: block; margin-top: 0.15rem; font-size: 0.65rem; }
    .perf-strategy-sep  { display: none; }
  }

  /* Mobile-only swipe hint — ag-Grid handles horizontal scroll natively
     but first-time visitors on phones may not realise the grid extends
     past the viewport edge. A small italic hint above the grids on
     narrow screens makes the affordance discoverable without changing
     the grid itself. Hidden on tablet+ where every column fits.

     Scope: `.perf-strategy ~ ...` confines this to the public layout
     (where the strategy strip is rendered) — the algo dashboard
     suppresses .perf-strategy via compactHeader=true, so its section
     headings never pick up the suffix even though they share the
     same .section-heading class. */
  @media (max-width: 600px) {
    :global(.perf-strategy ~ section .section-heading)::after,
    :global(.perf-strategy ~ .funds-heading-row .funds-heading-title)::after {
      content: ' · swipe →';
      font-size: 0.6rem;
      font-weight: 500;
      color: #8a98b0;
      font-style: italic;
      letter-spacing: 0;
      text-transform: none;
    }
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
    font-size: 0.75rem;
    line-height: 1.25;
    margin-bottom: 0.75rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
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
    border: 1px solid rgba(251,191,36,0.55);
    color: #fbbf24;
    padding: 0.4rem 0.7rem;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 700;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    box-shadow: 0 2px 10px rgba(0,0,0,0.35);
    animation: perf-fill-toast-in 0.18s ease-out;
  }
  @keyframes perf-fill-toast-in {
    from { opacity: 0; transform: translateY(-6px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .perf-banner-icon {
    font-size: 0.95rem;
    line-height: 1;
    flex: 0 0 auto;
  }
  .perf-banner-text {
    flex: 1 1 auto;
  }

  /* Light (public) theme. */
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
    color: #fbbf24;
  }
  .perf-dark .perf-banner-error {
    background: rgba(248,113,113,0.10);
    border-color: rgba(248,113,113,0.35);
    color: #fda4a4;
  }
  .perf-dark .perf-banner-error .perf-banner-icon {
    color: #f87171;
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

  /* Light-theme (public /performance) tab background — matches the
     dark-theme treatment in shape: amber tint on active, faint
     hover tint on inactive, both with rounded top corners so the
     active tab reads as a panel header. */
  .tabs-row :global(button[class*="border-primary"]) {
    background: rgba(212,146,12,0.10);
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
  }
  .tabs-row :global(button[class*="text-muted"]:hover) {
    background: rgba(212,146,12,0.04);
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
  }
  /* Account + Symbol dropdown wrappers. Same width + min-width so the
     two sit side-by-side as equal-footprint fields. Theme + colour are
     handled inside Select / MultiSelect. Both live right after the
     Holdings tab — no right-push. */
  .acct-multi,
  .sym-multi {
    width: 8.5rem;
    min-width: 0;
  }

  /* Mobile — the dropdowns tighten, tabs stay full size. */
  @media (max-width: 639px) {
    .tabs-row { gap: 0.3rem; }
    .acct-multi,
    .sym-multi { width: 7.5rem; }
  }
  /* Fund Balances heading — heading left, Refresh button (compactHeader
     only) pinned to the right. Keeps the tabs / filter row focused on
     Account + Symbol selection. */
  .funds-heading-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
  }
  /* The h2 carries `margin-bottom: 0.5rem` from .section-heading,
     which makes its box taller than the Refresh button — `align-items:
     center` then centers the boxes, but the visible "Fund Balances"
     text reads above the button text. Zero out the h2's margin in
     this flex row + match line-height so the two elements share a
     baseline. */
  .funds-heading-row .section-heading {
    margin-bottom: 0;
    line-height: 1.4;
    display: inline-flex;
    align-items: center;
  }
  .funds-heading-refresh { margin-left: auto; }

  /* Heading row for the Positions / Holdings grids — pairs the h2
     with a <GridSearchButton> at the right. Same baseline-pull as
     funds-heading-row so the icon doesn't visually drift above the
     heading text. */
  .perf-grid-headrow {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.25rem;
  }
  .perf-grid-headrow .section-heading {
    margin-bottom: 0;
    line-height: 1.4;
    display: inline-flex;
    align-items: center;
  }
  .perf-grid-headrow-spacer { flex: 1; }

  /* ── Dark (algo) overrides ─────────────────────────────────────────────── */
  /* Section headings ("Fund Balances", "Summary", "Positions",
     "Holdings") demoted from full amber to light blue so the three
     heading tiers (page title amber → section label muted blue →
     section heading light blue) read as distinct strata. Previously
     all three tiers rendered in #fbbf24 and the hierarchy collapsed. */
  .perf-dark :global(.section-heading) { color: var(--algo-slate); }

  /* Tabs — active gets an amber tint + slight top-corner round so the
     selected tab reads as a panel header, not just an underlined word.
     Hover on inactive lifts text + adds the faintest tint. */
  .perf-dark :global(button[class*="border-primary"])    {
    border-color: #d97706 !important;
    color: #fbbf24 !important;
    background: rgba(251,191,36,0.12) !important;
    border-top-left-radius: 4px !important;
    border-top-right-radius: 4px !important;
  }
  .perf-dark :global(button[class*="text-muted"])        { color: rgba(180,200,230,0.6) !important; }
  .perf-dark :global(button[class*="text-muted"]:hover)  {
    color: rgba(210,225,250,0.9) !important;
    background: rgba(251,191,36,0.05) !important;
    border-top-left-radius: 4px !important;
    border-top-right-radius: 4px !important;
  }

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
    font-size: 0.55rem;
    font-weight: 600;
    padding: 0 0.28rem;
    border-radius: 0.18rem;
    background: rgba(251,191,36,0.15);
    color: #fbbf24;
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
