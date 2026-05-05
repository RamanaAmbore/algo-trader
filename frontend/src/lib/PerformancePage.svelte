<script>
  import { onMount, onDestroy } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import { fetchHoldings, fetchPositions, fetchFunds } from '$lib/api';
  import { createPerformanceSocket } from '$lib/ws';
  import { dataCache, authStore } from '$lib/stores';
  import OrderTicket from '$lib/order/OrderTicket.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import Select      from '$lib/Select.svelte';
  import { getInstrument, loadInstruments } from '$lib/data/instruments';
  import { page } from '$app/state';
  import { goto } from '$app/navigation';

  ModuleRegistry.registerModules([AllCommunityModule]);

  const {
    theme             = 'ag-theme-ramboq',
    allowOrders       = false,
    maskAccounts      = true,
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

  let selectedAccount = $state('all');
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

  // Strip from the first digit onward — Zerodha F&O tradingsymbols are
  // "<UNDERLYING><expiry><strike><opt-type>" (NIFTY25APR22000CE,
  // BANKNIFTY25MAYFUT, CRUDEOIL25MAYFUT, …). For plain equity symbols
  // with no digits (RELIANCE, SBIN, …) this is a no-op, which is the
  // right answer for holdings.
  function underlyingOf(/** @type {string} */ sym) {
    return (sym || '').replace(/\d.*$/, '');
  }

  const numFmt  = ({ value }) =>
    value == null ? '' : Number(value).toLocaleString('en-IN', { maximumFractionDigits: 2 });
  const pctFmt  = ({ value }) =>
    value == null ? '' : `${Number(value).toFixed(2)}%`;
  const maskAcct = ({ value }) =>
    maskAccounts && value ? String(value).replace(/\d/g, '#') : value;
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
    const ltp = params.data?.close_price;
    if (avg == null || ltp == null) return 'ag-right-aligned-cell';
    return ['ag-right-aligned-cell', avg > ltp ? 'pnl-loss' : avg < ltp ? 'pnl-gain' : 'pnl-zero'];
  };

  const defaultCol = { resizable: true, sortable: true, filter: true, suppressHeaderMenuButton: true, flex: 1, minWidth: 55 };

  const getRowClass = (params) => {
    const d = params.data || {};
    if (d.tradingsymbol === 'TOTAL' || d.account === 'TOTAL') return 'totals-row';
    // Tag position rows so the operator can tell longs from shorts at a
    // glance. Only applied when `quantity` is present (positions grid);
    // holdings rows never carry a sign.
    const q = d.quantity;
    if (typeof q === 'number' && q < 0) return 'pos-short';
    if (typeof q === 'number' && q > 0) return 'pos-long';
    return '';
  };

  const acctFill = 'ag-col-fill';
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
    { field: 'account',               headerName: 'Account',  width: 70, minWidth: 70, cellClass: acctFill, headerClass: acctFill, valueFormatter: maskAcct },
    { field: 'cur_val',               headerName: 'Cur Val',  flex: 1, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
    { field: 'inv_val',               headerName: 'Inv Val',  flex: 1, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl',                   headerName: 'P&L',      flex: 1, valueFormatter: numFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',        headerName: 'P&L %',    width: 60, valueFormatter: pctFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_val',        headerName: 'Day P&L',  flex: 1, valueFormatter: numFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',    width: 60, valueFormatter: pctFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
  ];

  const holdingsCols = [
    { field: 'account',               headerName: 'Account',  width: 70, cellClass: acctFill, headerClass: acctFill, valueFormatter: maskAcct },
    { field: 'tradingsymbol',         headerName: 'Symbol',   width: 105, pinned: 'left', cellClass: symFill, headerClass: symFill },
    { field: 'close_price',           headerName: 'LTP',      width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'average_price',         headerName: 'Avg Price', width: 78, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'pnl',                   headerName: 'P&L',      width: 78, valueFormatter: numFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'pnl_percentage',        headerName: 'P&L %',    width: 60, valueFormatter: pctFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_val',        headerName: 'Day P&L',  width: 78, valueFormatter: numFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'day_change_percentage', headerName: 'Day %',    width: 60, valueFormatter: pctFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'quantity',              headerName: 'Qty',      width: 52, type: 'numericColumn', headerClass: numericHdr },
    { field: 'cur_val',               headerName: 'Cur Val',  width: 88, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
  ];

  const positionsSummaryCols = [
    { field: 'account', headerName: 'Account', width: 70, cellClass: acctFill, headerClass: acctFill, valueFormatter: maskAcct },
    { field: 'pnl',     headerName: 'P&L',     flex: 1, valueFormatter: numFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
  ];

  // Options deep-link cell renderer — only wired up when enableOptionsLink
  // is true (admin /dashboard). Appends a small "→ Opt" pill after the
  // symbol text for rows whose symbol is an option (CE/PE) or future (FUT).
  // Returns an HTMLElement so AG Grid can mount it cleanly without innerHTML.
  function _optionsLinkRenderer(params) {
    const sym  = params.value || '';
    const acct = params.data?.account || '';
    const span = document.createElement('span');
    span.style.cssText = 'display:inline-flex;align-items:center;gap:0.3rem;width:100%';
    span.textContent = sym;
    if (/(?:CE|PE|FUT)$/.test(sym)) {
      const href = `/admin/options?symbol=${encodeURIComponent(sym)}&account=${encodeURIComponent(acct)}`;
      const a = document.createElement('a');
      a.href = href;
      a.className = 'perf-opts-link';
      a.textContent = '→';
      a.title = `Open ${sym} in Options`;
      // Prevent the row-click from also firing (which would open the
      // OrderTicket). Only the link itself routes to /admin/options.
      a.addEventListener('click', (e) => e.stopPropagation());
      span.appendChild(a);
    }
    return span;
  }

  const positionsSymbolCol = $derived(enableOptionsLink
    ? { field: 'tradingsymbol', headerName: 'Symbol', width: 140, pinned: 'left', cellClass: symFill, headerClass: symFill, cellRenderer: _optionsLinkRenderer }
    : { field: 'tradingsymbol', headerName: 'Symbol', width: 130, pinned: 'left', cellClass: symFill, headerClass: symFill });

  const positionsCols = $derived([
    { field: 'account',       headerName: 'Account',   width: 70, cellClass: acctFill, headerClass: acctFill, valueFormatter: maskAcct },
    // F&O symbols are wider than equities (e.g. NIFTY26MAY22000CE);
    // 140 when options link active (extra room for the pill), 130 otherwise.
    positionsSymbolCol,
    { field: 'close_price',   headerName: 'LTP',       width: 68, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'average_price', headerName: 'Avg Price', width: 78, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr, cellClass: avgVsLtpCls },
    { field: 'pnl',           headerName: 'P&L',       width: 78, valueFormatter: numFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'quantity',      headerName: 'Qty',       width: 52, type: 'numericColumn', headerClass: numericHdr, cellClass: qtyCls },
  ]);

  const fundsCols = [
    { field: 'account',      headerName: 'Account',      width: 100, cellClass: acctFill, headerClass: acctFill, valueFormatter: maskAcct },
    { field: 'cash',         headerName: 'Cash',         flex: 1, valueFormatter: numFmt, cellClass: pnlCls, type: 'numericColumn', headerClass: numericHdr },
    { field: 'avail_margin', headerName: 'Avail Margin', flex: 1, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
    { field: 'used_margin',  headerName: 'Used Margin',  flex: 1, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
    { field: 'collateral',   headerName: 'Collateral',   flex: 1, valueFormatter: numFmt, type: 'numericColumn', headerClass: numericHdr },
  ];

  function makeGrid(el, colDefs, rowData = [], onRowClick = null) {
    return createGrid(el, {
      columnDefs: colDefs,
      rowData,
      defaultColDef: defaultCol,
      columnTypes: {
        numericColumn: {
          cellClass: 'ag-right-aligned-cell',
          headerClass: 'ag-right-aligned-header',
        },
      },
      overlayNoRowsTemplate: '<span style="font-size:0.65rem;color:#999">—</span>',
      domLayout: 'autoHeight',
      getRowClass,
      pinnedBottomRowData: [],
      ...(onRowClick ? { onRowClicked: (e) => onRowClick(e.data) } : {}),
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
    const total_inv_val    = total_cur_val - total_pnl;
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
    return {
      account: '',
      tradingsymbol: 'TOTAL',
      pnl:        sum('pnl'),
      unrealised: sum('unrealised'),
      realised:   sum('realised'),
      quantity:   sum('quantity'),
      average_price: null,
      close_price:   null,
    };
  }

  function applyAccountFilter() {
    if (!holdingsAllGrid) return;
    // ACCOUNT filter scopes every grid (detail + summary + funds). With a
    // specific account picked we drop other accounts AND the TOTAL row.
    // SYMBOL filter scopes ONLY the last (detail) aggrid — summary and
    // funds are per-account aggregates that don't reduce cleanly to a
    // single symbol, so they stay on the account-level view.
    const keepAcct = (r) => selectedAccount === 'all'
      ? true
      : (r.account === selectedAccount);
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
    // Account column hides across every grid when a specific account
    // is picked. Symbol column stays visible even when filtered —
    // operators asked to keep it so they can read which symbol(s) each
    // row belongs to at a glance.
    const showAcct = selectedAccount === 'all';
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
    selectedAccount; selectedSymbols; activeTab;
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
      loadAll();
    });
  });

  onDestroy(() => {
    unsub?.();
    [fundsGrid, holdingsSummaryGrid, holdingsAllGrid,
     positionsSummaryGrid, positionsAllGrid]
      .forEach(g => g?.destroy());
  });
</script>

<div class:perf-dark={isDark}>

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
  <!-- Default layout: timestamp + Refresh button on their own line, tabs
       below. The public /performance page uses this. -->
  <div class="flex items-center justify-between mb-2">
    <div class="text-[0.65rem] text-muted perf-ts">
      {#if loading && !lastRefresh}
        <span class="animate-pulse">Loading…</span>
      {:else if lastRefresh}
        <span>{lastRefresh}</span>
      {/if}
    </div>
    <button onclick={() => loadAll({ fresh: true })} disabled={loading} class="btn-secondary text-[0.65rem] py-0.5 px-2 disabled:opacity-50">
      {loading ? 'Refreshing…' : 'Refresh'}
    </button>
  </div>
{/if}

<!-- Tabs + account selector. With `compactHeader`, the refresh timestamp
     joins this row as the last element (no Refresh button — the
     performance WebSocket already handles auto-refresh). -->
<div class="tabs-row mb-3">
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
    <!-- Account picker uses the themed <Select> so it visually matches
         the MultiSelect next to it. Single-select — account scope.
         Theme mirrors the page (dark on the algo dashboard, light on
         the public /performance). -->
    <div class="acct-multi">
      <Select
        bind:value={selectedAccount}
        options={[
          { value: 'all', label: 'All Accounts' },
          ...accounts.map(a => ({ value: a, label: maskAccounts ? a.replace(/\d/g, '#') : a })),
        ]}
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
        placeholder="All Symbols"
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
    <button onclick={() => loadAll({ fresh: true })} disabled={loading}
      class="btn-secondary text-[0.65rem] py-0.5 px-2 disabled:opacity-50 funds-heading-refresh">
      {loading ? 'Refreshing…' : 'Refresh'}
    </button>
  {/if}
</div>
<div bind:this={fundsEl} class="ag-theme-quartz {theme} mb-4 w-full"></div>

<section class:hidden={activeTab !== 'positions'}>
  <h2 class="section-heading">Summary</h2>
  <div bind:this={positionsSummaryEl} class="ag-theme-quartz {theme} mb-4 w-full"></div>

  <h2 class="section-heading">Positions</h2>
  <div bind:this={positionsAllEl} class="ag-theme-quartz {theme} w-full"></div>
</section>

<section class:hidden={activeTab !== 'holdings'}>
  <h2 class="section-heading">Summary</h2>
  <div bind:this={holdingsSummaryEl} class="ag-theme-quartz {theme} mb-4 w-full"></div>

  <h2 class="section-heading">Holdings</h2>
  <div bind:this={holdingsAllEl} class="ag-theme-quartz {theme} w-full"></div>
</section>

{#if orderTicketProps}
  <OrderTicket
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

</div><!-- /perf-dark -->

<style>
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
  /* Refresh timestamp pinned to the far right of the tabs row. */
  .tabs-row-ts {
    margin-left: auto;
    white-space: nowrap;
  }
  /* Compact-header Refresh button sits after the account picker. */
  .tabs-row-refresh { margin-left: auto; }

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

  /* ── Dark (algo) overrides ─────────────────────────────────────────────── */
  .perf-dark :global(.section-heading) { color: #fbbf24; }

  /* Tabs */
  .perf-dark :global(button[class*="border-primary"])    { border-color: #d97706 !important; color: #fbbf24 !important; }
  .perf-dark :global(button[class*="text-muted"])        { color: rgba(180,200,230,0.6) !important; }
  .perf-dark :global(button[class*="text-muted"]:hover)  { color: rgba(210,225,250,0.9) !important; }

  /* Refresh button */
  .perf-dark :global(.btn-secondary) {
    color: #c8d8f0;
    border-color: #2a4060;
    background: transparent;
  }
  .perf-dark :global(.btn-secondary:hover:not(:disabled)) { background: rgba(255,255,255,0.06); }

  /* Dashboard timestamp — yellow to match log and algo-ts timestamps */
  .perf-dark :global(.perf-ts) { color: #fde047 !important; }

  /* Options deep-link pill — amber accent, scoped to positions rows in
     the algo /dashboard (enableOptionsLink=true). Appears after the
     symbol text as a small "→" arrow. Absent on public /performance. */
  :global(.perf-opts-link) {
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
  :global(.perf-opts-link:hover) {
    background: rgba(251,191,36,0.28);
    color: #fde68a;
  }
</style>
