<script>
  // Unified market-pulse component shared by /pulse (default preset)
  // and /dashboard (Phase 2 preset).
  //
  // A single ag-Grid lists every symbol the operator is tracking,
  // with per-row source badges (W=watchlist, H=holding, P=position,
  // U=option-underlying) so the same symbol never appears twice.
  // The merge engine, batch-quote pipeline, symbol-cell renderer,
  // row-tint CSS, and OrderEntryShell wiring all live here — pages
  // compose by toggling props:
  //
  //   enableWatchlists    — show tab strip / add row / remove ×
  //   enableSourceToggles — show "P · Positions" / "H · Holdings" pills
  //   allowOrders         — row click opens OrderEntryShell
  //
  // Phase 2 additions (not wired yet): accountFilter, showSummaryRows,
  // showFundsCard.

  import { onMount, onDestroy, tick } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import {
    fetchWatchlists, fetchWatchlist, createWatchlist,
    deleteWatchlist, addWatchlistItem, removeWatchlistItem,
    fetchWatchlistQuotes,
    fetchPositions, fetchHoldings, fetchAccounts, fetchFunds, batchQuote,
    fetchMovers,
  } from '$lib/api';
  import { visibleInterval, clientTimestamp } from '$lib/stores';
  import { priceFmt, pctFmt, aggCompact, qtyFmt, directional } from '$lib/format';
  import OrderEntryShell from '$lib/order/OrderEntryShell.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';

  let {
    title              = 'Market Pulse',
    enableWatchlists   = true,
    enableSourceToggles = true,
    allowOrders        = true,
    // Phase 2 presets — dashboard mode turns these on.
    accountPicker      = false, // <select> next to the toolbar
    showSummary        = false, // small per-account summary grid above the main grid
    showFunds          = false, // small per-account funds grid below the main grid
  } = $props();

  // AG Grid valueFormatter wrappers — the canonical idiom every other
  // algo grid uses. Single source of truth for how numbers render:
  // no `+` prefix on positives (colour carries direction), no `₹`
  // prefix, en-IN grouping, '—' for null.
  const numFmt     = ({ value }) => value == null ? '—' : priceFmt(value);
  const aggFmtGrid = ({ value }) => value == null ? '—' : aggCompact(value);
  const pctFmtGrid = ({ value }) => value == null ? '—' : `${pctFmt(value)}%`;
  const numericHdr = 'ag-right-aligned-header';

  ModuleRegistry.registerModules([AllCommunityModule]);

  let lists       = $state(/** @type {any[]} */ ([]));
  // Multi-select model — operator can include any combination of
  // their saved watchlists in the unified view. `activeIds` is the
  // SOURCE OF TRUTH: clicking a tab toggles its id in/out. Fetched
  // list contents land in `activeLists` so buildUnified can iterate
  // every selected list. `watchQuotes` stays a flat itemId→quote map
  // (item ids are globally unique) populated by unioning the per-list
  // /quotes responses.
  let activeIds   = $state(/** @type {Set<number>} */ (new Set()));
  let activeLists = $state(/** @type {any[]} */ ([]));
  let watchQuotes = $state(/** @type {Record<number, any>} */ ({}));
  // "Target" list for add / rename / delete operations. Defaults to
  // the first selected list; updated when the operator clicks a tab
  // (set focus AND toggle inclusion in one click).
  let focusedListId = $state(/** @type {number | null} */ (null));
  let positions   = $state(/** @type {any[]} */ ([]));
  let holdings    = $state(/** @type {any[]} */ ([]));
  // Pulse quote bag — bundles BOTH option-underlying quotes (keyed
  // by logical underlying name, each value carries a `_resolved`
  // info block from resolveUnderlying) AND contract quotes (keyed
  // by `${exchange}:${tradingsymbol}`). Single $state object so a
  // single assignment in loadPulse triggers exactly one buildUnified
  // recomputation per tick.
  let pulseQuotes = $state(/** @type {{underlyings: Record<string, any>, contracts: Record<string, any>}} */ ({ underlyings: {}, contracts: {} }));
  let pulseLastUpdate  = $state(/** @type {number | null} */ (null));
  let agoTick          = $state(0);
  let refreshedAt = $state('');
  let error       = $state('');

  // Add-symbol form state.
  let symInput   = $state('');
  let exchInput  = $state('NSE');
  let typeahead  = $state(/** @type {any[]} */ ([]));
  let typeaheadOpen = $state(false);

  // Option picker — shown inline when the operator picks an underlying
  // from the typeahead (i.e. a symbol that has CE/PE chains).
  /** @type {{ name: string, exchange: string } | null} */
  let optionPickerUnderlying = $state(null);
  let optionPickerExpiry     = $state('');
  let optionPickerSide       = $state(/** @type {'CE'|'PE'} */ ('CE'));
  let optionPickerStrike     = $state(/** @type {number|null} */ (null));
  let optionPickerExpiries   = $state(/** @type {string[]} */ ([]));
  let optionPickerStrikes    = $state(/** @type {number[]} */ ([]));

  // Reload strikes when expiry changes (side change intentionally does
  // NOT reset strike — operator may want the same strike in CE↔PE).
  $effect(() => {
    void optionPickerExpiry; void optionPickerSide;
    if (!optionPickerUnderlying || !optionPickerExpiry) {
      optionPickerStrikes = []; optionPickerStrike = null; return;
    }
    import('$lib/data/instruments').then(({ listStrikes }) => {
      const strikes = listStrikes(
        optionPickerUnderlying.name.toUpperCase(),
        optionPickerSide,
        optionPickerExpiry,
      );
      optionPickerStrikes = strikes;
      // If the current strike is still in the new list, keep it.
      if (optionPickerStrike != null && strikes.includes(optionPickerStrike)) return;
      // Otherwise default to ATM (nearest to current spot).
      const spot = pulseQuotes.underlyings[optionPickerUnderlying.name.toUpperCase()]
                     ?.last_price
                ?? pulseQuotes.underlyings[optionPickerUnderlying.name]
                     ?.last_price
                ?? null;
      if (spot != null && strikes.length) {
        let best = strikes[0], bestDiff = Math.abs(strikes[0] - spot);
        for (const k of strikes) {
          const d = Math.abs(k - spot);
          if (d < bestDiff) { best = k; bestDiff = d; }
        }
        optionPickerStrike = best;
      } else {
        optionPickerStrike = strikes[Math.floor(strikes.length / 2)] ?? null;
      }
    }).catch(() => { optionPickerStrikes = []; optionPickerStrike = null; });
  });

  async function openOptionPicker(instName, instExchange) {
    try {
      const { listExpiries } = await import('$lib/data/instruments');
      const expiries = listExpiries(instName.toUpperCase(), 'CE');
      if (!expiries.length) return false; // no option chain — direct-add
      optionPickerUnderlying = { name: instName.toUpperCase(), exchange: instExchange };
      optionPickerExpiry     = expiries[0];
      optionPickerSide       = 'CE';
      optionPickerExpiries   = expiries;
      // Strike is set reactively by the $effect above.
      return true;
    } catch { return false; }
  }

  function closeOptionPicker() {
    optionPickerUnderlying = null;
    optionPickerExpiry     = '';
    optionPickerSide       = 'CE';
    optionPickerStrike     = null;
    optionPickerExpiries   = [];
    optionPickerStrikes    = [];
  }

  async function addOptionFromPicker() {
    if (!optionPickerUnderlying || !optionPickerExpiry || optionPickerStrike == null) return;
    const targetId = focusedListId ?? [...activeIds][0];
    if (targetId == null) return;
    try {
      const { findOption } = await import('$lib/data/instruments');
      const inst = findOption(
        optionPickerUnderlying.name,
        optionPickerSide,
        optionPickerStrike,
        optionPickerExpiry,
      );
      if (!inst) { error = 'Symbol not in cache — retry.'; return; }
      await addWatchlistItem(targetId, inst.s, inst.e || 'NFO');
      symInput = ''; typeahead = []; typeaheadOpen = false;
      closeOptionPicker();
      await loadActive();
    } catch (e) { error = e.message; }
  }

  async function addSpotFromPicker() {
    if (!optionPickerUnderlying) return;
    const targetId = focusedListId ?? [...activeIds][0];
    if (targetId == null) return;
    // Resolve the actual NSE/BSE tradingsymbol for the index/stock.
    const KEY = {
      NIFTY: 'NIFTY 50', BANKNIFTY: 'NIFTY BANK', FINNIFTY: 'NIFTY FIN SERVICE',
      MIDCPNIFTY: 'NIFTY MID SELECT', SENSEX: 'SENSEX', BANKEX: 'BANKEX',
    };
    const sym  = KEY[optionPickerUnderlying.name] ?? optionPickerUnderlying.name;
    const exch = (sym === 'SENSEX' || sym === 'BANKEX') ? 'BSE' : 'NSE';
    try {
      await addWatchlistItem(targetId, sym, exch);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      closeOptionPicker();
      await loadActive();
    } catch (e) { error = e.message; }
  }

  // New-list form state.
  let newListName = $state('');

  // Source toggles — driven by the MultiSelect below.
  const SOURCE_OPTIONS = [
    { value: 'positions', label: 'Positions' },
    { value: 'holdings',  label: 'Holdings'  },
    { value: 'movers',    label: 'Movers'    },
  ];
  let selectedSources = $state(['positions', 'holdings', 'movers']);

  // Keep individual booleans so buildUnified + other callsites are unchanged.
  let showPositions = $state(true);
  let showHoldings  = $state(true);

  // Movers — top-% movers from /watchlist/movers. Polled every 30 s.
  // Failure is non-fatal: the rest of the page keeps working.
  let movers     = $state(/** @type {any[]} */ ([]));
  let showMovers = $state(true);

  $effect(() => {
    showPositions = selectedSources.includes('positions');
    showHoldings  = selectedSources.includes('holdings');
    showMovers    = selectedSources.includes('movers');
  });
  let stopMoversPoll;

  // Account picker state (dashboard mode). `selectedAccount === 'all'`
  // shows everything; a specific account id filters the positions /
  // holdings INPUTS to buildUnified so the merged rows + summaries
  // reflect just that account. Watchlist + option-underlying rows
  // are not account-scoped — they always show.
  let selectedAccount = $state('all');
  let availableAccounts = $state(/** @type {string[]} */ ([]));
  // Per-source summary rows from the backend (positions / holdings
  // endpoints return precomputed per-account totals in .summary).
  // Combined into one row-set for the summary grid above the main one.
  let positionsSummary = $state(/** @type {any[]} */ ([]));
  let holdingsSummary  = $state(/** @type {any[]} */ ([]));
  // Funds (per-account margins) — loaded only when showFunds is true.
  let funds = $state(/** @type {any[]} */ ([]));

  let stopPoll, stopPulsePoll;
  let gridEl;
  // $state on the bind:this refs so Svelte 5's reactive-update
  // analyzer doesn't warn (gridEl was grandfathered in pre-Phase 2;
  // the new summary / funds refs need the explicit annotation).
  let positionsSummaryEl = $state(/** @type {HTMLDivElement | null} */ (null));
  let holdingsSummaryEl  = $state(/** @type {HTMLDivElement | null} */ (null));
  let fundsEl            = $state(/** @type {HTMLDivElement | null} */ (null));
  let grid;
  let positionsSummaryGrid;
  let holdingsSummaryGrid;
  let fundsGrid;
  // Sentinel that flips to true once createGrid runs — used by the
  // $effect below to push unifiedRows into ag-Grid the moment both
  // (a) the grid is mounted and (b) the derived row set has populated.
  let gridReady              = $state(false);
  let positionsSummaryReady  = $state(false);
  let holdingsSummaryReady   = $state(false);
  let fundsReady             = $state(false);

  // Instrument-cache lookup functions. Loaded once at mount + cached
  // as module-scope state so buildUnified() can parse tradingsymbols
  // synchronously and loadPulse can resolve MCX commodities to their
  // near-month future without re-importing the module on every tick.
  let getInstrument     = $state(/** @type {((s: string) => any) | null} */ (null));
  let findNearestFuture = $state(/** @type {((u: string) => any) | null} */ (null));

  // Transient-error suppression. Quote-refresh polls fire every 5 s
  // and can blip on broker hiccups; one failed call shouldn't paint
  // the page red. Show the banner only after 3 consecutive failures.
  let quoteFailStreak = 0;

  // Order-ticket integration — row click opens the OrderEntryShell
  // pre-filled with the row's symbol / exchange / lot size. Stays
  // null when no ticket is open. Real broker accounts (unmasked
  // account_id) fetched once on mount so the operator can pick.
  let ticketProps     = $state(/** @type {any} */ (null));
  let realAccounts    = $state(/** @type {string[]} */ ([]));

  onMount(async () => {
    // Auth is enforced by the (algo) layout — no goto('/signin')
    // here so this component can also be embedded in flows that
    // intentionally allow anonymous demo viewers.
    await tick();
    mountGrid();

    try {
      const mod = await import('$lib/data/instruments');
      await mod.loadInstruments();
      getInstrument     = mod.getInstrument;
      findNearestFuture = mod.findNearestFuture;
    } catch (_) { /* cache cold — group/sort falls back to alphabetical */ }
    try {
      const r = await fetchAccounts();
      realAccounts = (r?.accounts || [])
        .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
        .filter(Boolean);
    } catch (_) { realAccounts = []; }
    if (enableWatchlists) {
      await loadLists();
      if (activeIds.size > 0) await loadActive();
    }
    await loadPulse();
    if (showFunds) await loadFunds();
    await loadMovers();
    stopPoll      = visibleInterval(async () => { await loadQuotes(); }, 5000);
    stopPulsePoll = visibleInterval(async () => {
      await loadPulse();
      if (showFunds) await loadFunds();
    }, 10000);
    stopMoversPoll = visibleInterval(loadMovers, 30000);
  });

  async function loadFunds() {
    try {
      const r = await fetchFunds();
      funds = (r?.rows || []).slice();
    } catch (_) { /* nothing fatal */ }
  }

  async function loadMovers() {
    try {
      const r = await fetchMovers();
      movers = (r?.movers || []).slice();
    } catch (e) {
      console.warn('[MarketPulse] movers fetch failed:', e?.message || e);
    }
  }

  $effect(() => {
    const rows = unifiedRows;
    if (!gridReady || !grid) return;
    grid.setGridOption('rowData', rows);
  });

  // Per-source summary derivations for the two separate summary grids.
  // Account picker scopes the body rows; TOTAL pinned at the bottom.
  function isTotalRow(r) { return r && r.account === 'TOTAL'; }

  // Positions Summary — Day P&L + Day % + P&L per account.
  const positionsSummaryData = $derived.by(() => {
    if (!showSummary || !selectedSources.includes('positions')) return [];
    const byAcct = /** @type {Record<string, any>} */ ({});
    for (const r of positionsSummary) {
      const a = r.account;
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, _inv_val: 0 };
      byAcct[a].day_pnl  += Number(r.day_change_val) || 0;
      byAcct[a].pnl      += Number(r.pnl)            || 0;
      byAcct[a]._inv_val += Number(r.inv_val)         || 0;
    }
    // Derive weighted-average percentages after accumulating the sums.
    for (const row of Object.values(byAcct)) {
      const iv = row._inv_val;
      row.day_change_percentage = iv ? (row.day_pnl / iv) * 100 : null;
      delete row._inv_val;
    }
    return Object.values(byAcct);
  });

  // Holdings Summary — Day P&L + Day % + P&L + P&L % + Cur Val + Inv Val per account.
  const holdingsSummaryData = $derived.by(() => {
    if (!showSummary || !selectedSources.includes('holdings')) return [];
    const byAcct = /** @type {Record<string, any>} */ ({});
    for (const r of holdingsSummary) {
      const a = r.account;
      if (!a) continue;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, cur_val: 0, inv_val: 0 };
      byAcct[a].day_pnl += Number(r.day_change_val) || 0;
      byAcct[a].pnl     += Number(r.pnl)            || 0;
      byAcct[a].cur_val += Number(r.cur_val)         || 0;
      byAcct[a].inv_val += Number(r.inv_val)         || 0;
    }
    // Derive weighted-average percentages after accumulating the sums.
    for (const row of Object.values(byAcct)) {
      const iv = row.inv_val;
      row.day_change_percentage = iv ? (row.day_pnl / iv) * 100 : null;
      row.pnl_percentage        = iv ? (row.pnl     / iv) * 100 : null;
    }
    return Object.values(byAcct);
  });

  const positionsSummaryBody  = $derived(
    selectedAccount === 'all'
      ? positionsSummaryData.filter(r => !isTotalRow(r))
      : positionsSummaryData.filter(r => r.account === selectedAccount)
  );
  const positionsSummaryTotal = $derived(positionsSummaryData.filter(isTotalRow));

  const holdingsSummaryBody  = $derived(
    selectedAccount === 'all'
      ? holdingsSummaryData.filter(r => !isTotalRow(r))
      : holdingsSummaryData.filter(r => r.account === selectedAccount)
  );
  const holdingsSummaryTotal = $derived(holdingsSummaryData.filter(isTotalRow));

  $effect(() => {
    if (!positionsSummaryReady || !positionsSummaryGrid) return;
    positionsSummaryGrid.setGridOption('rowData', positionsSummaryBody);
    positionsSummaryGrid.setGridOption('pinnedBottomRowData', positionsSummaryTotal);
  });

  $effect(() => {
    if (!holdingsSummaryReady || !holdingsSummaryGrid) return;
    holdingsSummaryGrid.setGridOption('rowData', holdingsSummaryBody);
    holdingsSummaryGrid.setGridOption('pinnedBottomRowData', holdingsSummaryTotal);
  });

  const scopedFunds = $derived(
    selectedAccount === 'all'
      ? funds
      : funds.filter(r => String(r.account || '') === selectedAccount)
  );

  $effect(() => {
    if (!fundsReady || !fundsGrid) return;
    const body  = scopedFunds.filter(r => !isTotalRow(r));
    const total = scopedFunds.filter(isTotalRow);
    fundsGrid.setGridOption('rowData', body);
    fundsGrid.setGridOption('pinnedBottomRowData', total);
  });

  onDestroy(() => {
    stopPoll?.(); stopPulsePoll?.(); stopMoversPoll?.();
    grid?.destroy?.();
    positionsSummaryGrid?.destroy?.();
    holdingsSummaryGrid?.destroy?.();
    fundsGrid?.destroy?.();
  });

  async function loadLists() {
    try {
      lists = await fetchWatchlists();
      if (!lists.length) return;
      if (activeIds.size === 0) {
        activeIds = new Set(lists.map(l => l.id));
      } else {
        const valid = new Set(lists.map(l => l.id));
        const trimmed = new Set([...activeIds].filter(id => valid.has(id)));
        if (trimmed.size !== activeIds.size) activeIds = trimmed;
      }
      if (focusedListId == null || !activeIds.has(focusedListId)) {
        const def = lists.find(l => l.is_default) ?? lists[0];
        focusedListId = def?.id ?? null;
      }
    } catch (e) { error = e.message; }
  }

  async function loadActive() {
    error = '';
    const ids = [...activeIds];
    if (ids.length === 0) {
      activeLists = [];
      watchQuotes = {};
      return;
    }
    try {
      const results = await Promise.all(
        ids.map(id => fetchWatchlist(id).catch(() => null))
      );
      activeLists = results.filter(Boolean);
      await loadQuotes();
    } catch (e) { error = e.message; }
  }

  async function loadQuotes() {
    const ids = [...activeIds];
    if (ids.length === 0) {
      watchQuotes = {};
      return;
    }
    try {
      const results = await Promise.all(
        ids.map(id => fetchWatchlistQuotes(id).catch(() => null))
      );
      const map = /** @type {Record<number, any>} */ ({});
      let latestRefreshed = '';
      for (const r of results) {
        if (!r) continue;
        for (const q of (r.items || [])) map[q.item_id] = q;
        if (r.refreshed_at && r.refreshed_at > latestRefreshed) {
          latestRefreshed = r.refreshed_at;
        }
      }
      watchQuotes = map;
      refreshedAt = latestRefreshed;
      if (quoteFailStreak > 0) {
        quoteFailStreak = 0;
        if (/Quote refresh/.test(error)) error = '';
      }
    } catch (e) {
      quoteFailStreak++;
      if (quoteFailStreak >= 3) {
        error = `Quote refresh: ${e.message}`;
      }
    }
  }

  /** Chunked batch-quote — 200-key chunks, parallel, 5 s per-chunk
   *  timeout so a slow Kite round-trip can't stall the next 10 s tick. */
  async function batchQuoteChunked(keys) {
    if (!keys || !keys.length) return [];
    const CHUNK = 200;
    const TIMEOUT_MS = 5000;
    const chunks = [];
    for (let i = 0; i < keys.length; i += CHUNK) chunks.push(keys.slice(i, i + CHUNK));
    const withTimeout = (p) => Promise.race([
      p,
      new Promise((_, rej) => setTimeout(() => rej(new Error('quote timeout')), TIMEOUT_MS)),
    ]);
    const results = await Promise.all(chunks.map(c =>
      withTimeout(batchQuote(c)).catch(() => ({ items: [] }))
    ));
    return results.flatMap(r => (r?.items || []));
  }

  // Mirrors the backend `derivatives.underlying_ltp_key` /
  // `is_mcx_underlying` + `findNearestFuture` chain.
  const INDEX_LTP_KEY = {
    NIFTY:      { tradingsymbol: 'NIFTY 50',         exchange: 'NSE' },
    BANKNIFTY:  { tradingsymbol: 'NIFTY BANK',       exchange: 'NSE' },
    FINNIFTY:   { tradingsymbol: 'NIFTY FIN SERVICE', exchange: 'NSE' },
    MIDCPNIFTY: { tradingsymbol: 'NIFTY MID SELECT', exchange: 'NSE' },
    SENSEX:     { tradingsymbol: 'SENSEX',           exchange: 'BSE' },
    BANKEX:     { tradingsymbol: 'BANKEX',           exchange: 'BSE' },
  };
  const MCX_COMMODITIES = new Set([
    'CRUDEOIL', 'CRUDEOILM', 'NATURALGAS', 'NATGASMINI',
    'GOLD', 'GOLDM', 'GOLDMINI', 'GOLDPETAL', 'GOLDGUINEA',
    'SILVER', 'SILVERM', 'SILVERMINI', 'SILVERMIC',
    'COPPER', 'ZINC', 'ZINCMINI', 'LEAD', 'LEADMINI',
    'ALUMINIUM', 'ALUMINI', 'NICKEL',
    'MENTHAOIL', 'COTTON', 'CASTORSEED', 'KAPAS', 'CARDAMOM',
  ]);
  const CDS_CURRENCIES = new Set(['USDINR']);

  function resolveUnderlying(name, findNearestFut) {
    const n = String(name || '').toUpperCase();
    if (!n) return null;
    const idx = INDEX_LTP_KEY[n];
    if (idx) {
      return {
        tradingsymbol: idx.tradingsymbol,
        exchange: idx.exchange,
        quoteKey: `${idx.exchange}:${idx.tradingsymbol}`,
        underlying_group: n,
        kind: 'spot',
      };
    }
    if (MCX_COMMODITIES.has(n)) {
      const fut = findNearestFut?.(n);
      if (fut?.s && fut?.e) {
        return {
          tradingsymbol: fut.s,
          exchange: fut.e,
          quoteKey: `${fut.e}:${fut.s}`,
          underlying_group: n,
          kind: 'fut',
        };
      }
      return null;
    }
    if (CDS_CURRENCIES.has(n)) {
      const fut = findNearestFut?.(n);
      if (fut?.s && fut?.e) {
        return {
          tradingsymbol: fut.s,
          exchange: fut.e,
          quoteKey: `${fut.e}:${fut.s}`,
          underlying_group: n,
          kind: 'fut',
        };
      }
      return null;
    }
    return {
      tradingsymbol: n,
      exchange: 'NSE',
      quoteKey: `NSE:${n}`,
      underlying_group: n,
      kind: 'spot',
    };
  }

  async function loadPulse() {
    try {
      const [p, h] = await Promise.all([
        fetchPositions().catch(() => ({ rows: [] })),
        fetchHoldings().catch(() => ({ rows: [] })),
      ]);
      positions = (p?.rows || []).slice();
      holdings  = (h?.rows || []).slice();
      // Capture the backend's precomputed per-account summary rows
      // (used by the dashboard's summary grid). Excludes the TOTAL
      // synthetic row — we render that separately as pinnedBottomRowData.
      if (showSummary) {
        positionsSummary = (p?.summary || []).slice();
        holdingsSummary  = (h?.summary || []).slice();
      }
      // Surface every account id we see across positions + holdings
      // for the account-picker dropdown. Deduplicated, sorted.
      if (accountPicker) {
        const accts = new Set();
        for (const r of positions) if (r.account) accts.add(String(r.account));
        for (const r of holdings)  if (r.account) accts.add(String(r.account));
        availableAccounts = [...accts].sort();
      }
      const underlyingInfos = /** @type {Map<string, any>} */ (new Map());
      const contractKeys = new Set();
      const lookup = getInstrument;
      const nearestFut = findNearestFuture;
      for (const r of positions) {
        const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
        const exch = r.exchange || 'NFO';
        if (lookup) {
          const inst = lookup(sym);
          if (inst?.u && String(inst.t || '').toUpperCase() !== 'EQ' &&
              !underlyingInfos.has(inst.u)) {
            const info = resolveUnderlying(inst.u, nearestFut);
            if (info) underlyingInfos.set(inst.u, info);
          }
        }
        if (sym) contractKeys.add(`${exch}:${sym}`);
      }
      for (const r of holdings) {
        const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
        const exch = r.exchange || 'NSE';
        if (lookup) {
          const inst = lookup(sym);
          if (inst?.u && String(inst.t || '').toUpperCase() !== 'EQ' &&
              !underlyingInfos.has(inst.u)) {
            const info = resolveUnderlying(inst.u, nearestFut);
            if (info) underlyingInfos.set(inst.u, info);
          }
        }
        if (sym) contractKeys.add(`${exch}:${sym}`);
      }

      const allKeys = new Set(contractKeys);
      for (const info of underlyingInfos.values()) allKeys.add(info.quoteKey);

      if (allKeys.size) {
        const items = await batchQuoteChunked([...allKeys]);
        const cMap = {};
        for (const q of items) {
          cMap[`${q.exchange}:${q.tradingsymbol}`] = q;
        }
        const uMap = {};
        for (const [name, info] of underlyingInfos.entries()) {
          const q = cMap[info.quoteKey];
          if (q) uMap[name] = { ...q, _resolved: info };
        }
        pulseQuotes = { underlyings: uMap, contracts: cMap };
      } else {
        pulseQuotes = { underlyings: {}, contracts: {} };
      }
      pulseLastUpdate = Date.now();
    } catch (_) { /* nothing fatal */ }
  }

  // Account-scoped inputs to buildUnified. When selectedAccount is
  // 'all' (default), pass the raw arrays straight through. Otherwise
  // filter positions / holdings to that account — watchlist items and
  // option underlyings are not account-scoped so they remain visible.
  const scopedPositions = $derived(
    selectedAccount === 'all'
      ? positions
      : positions.filter(r => String(r.account || '') === selectedAccount)
  );
  const scopedHoldings = $derived(
    selectedAccount === 'all'
      ? holdings
      : holdings.filter(r => String(r.account || '') === selectedAccount)
  );

  const unifiedRows = $derived(buildUnified(
    activeLists, watchQuotes, scopedPositions, scopedHoldings, pulseQuotes, getInstrument,
    showPositions, showHoldings, movers, showMovers,
  ));

  function parseSymbol(/** @type {string} */ sym, /** @type {any} */ instCache) {
    if (!instCache) return { underlying: null, kind: null, strike: null, opt_type: null, expiry: null };
    const inst = instCache(sym);
    if (!inst) return { underlying: null, kind: null, strike: null, opt_type: null, expiry: null };
    const t = String(inst.t || '').toUpperCase();
    const k = inst.k != null ? Number(inst.k) : null;
    let optType = null;
    if (t === 'CE' || t === 'PE') optType = t;
    else if (/CE$/i.test(sym)) optType = 'CE';
    else if (/PE$/i.test(sym)) optType = 'PE';
    const kind = optType ? 'opt' : (t === 'FUT' ? 'fut' : (t === 'EQ' ? 'eq' : null));
    const expiry = inst.x || null;
    return { underlying: inst.u || null, kind, strike: k, opt_type: optType, expiry };
  }

  function buildUnified(actLists, wq, pos, hold, pq, getInst, includePos, includeHold, moverRows, includeMovers) {
    const uq = pq?.underlyings || {};
    const cq = pq?.contracts   || {};
    const byKey = /** @type {Record<string, any>} */ ({});
    const get = (key) => byKey[key] || (byKey[key] = {
      key,
      src: { w:false, h:false, p:false, u:false, m:false },
      qty_pos: 0, qty_hold: 0,
      pnl: null, day_pnl: null,
      _avg_num: 0,
      accounts: /** @type {Set<string>} */ (new Set()),
    });

    function fill(row, sym) {
      const p = parseSymbol(sym, getInst);
      if (row.underlying == null) row.underlying = p.underlying;
      if (row.kind       == null) row.kind       = p.kind;
      if (row.strike     == null) row.strike     = p.strike;
      if (row.opt_type   == null) row.opt_type   = p.opt_type;
      if (row.expiry     == null) row.expiry     = p.expiry;
    }

    // 1. Watchlist (all selected lists).
    for (const list of (actLists || [])) {
      for (const it of (list?.items || [])) {
        const q = wq[it.id];
        const sym = String(q?.quote_symbol || it.tradingsymbol).toUpperCase();
        const row = get(sym);
        row.exchange      = row.exchange || it.exchange;
        row.tradingsymbol = sym;
        row.alias         = (q?.quote_symbol && q.quote_symbol !== it.tradingsymbol) ? it.tradingsymbol : null;
        if (row.watchlist_item_id == null) {
          row.watchlist_item_id = it.id;
          row.watchlist_list_id = list.id;
        }
        row.src.w  = true;
        row.ltp    = q?.ltp    ?? row.ltp    ?? null;
        row.bid    = q?.bid    ?? row.bid    ?? null;
        row.ask    = q?.ask    ?? row.ask    ?? null;
        row.change = q?.change ?? row.change ?? null;
        row.change_pct = q?.change_pct ?? row.change_pct ?? null;
        fill(row, sym);
      }
    }

    // 2. Positions.
    for (const r of (includePos === false ? [] : pos)) {
      const exch = r.exchange || 'NFO';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(sym);
      row.exchange      = row.exchange || exch;
      row.tradingsymbol = sym;
      row.src.p = true;
      const q   = Number(r.quantity) || 0;
      const avg = Number(r.average_price) || 0;
      row.qty_pos  += q;
      row._avg_num += avg * q;
      if (r.account) row.accounts.add(String(r.account));
      const liveQ = cq?.[`${exch}:${sym}`];
      if (liveQ?.ltp) {
        row.ltp        = liveQ.ltp;
        row.bid        = liveQ.bid ?? row.bid ?? null;
        row.ask        = liveQ.ask ?? row.ask ?? null;
        row.change     = liveQ.change     ?? row.change     ?? null;
        row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
      } else if (row.ltp == null) {
        row.ltp = r.last_price ?? null;
      }
      row.pnl     = (row.pnl     ?? 0) + (Number(r.pnl)            || 0);
      row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
      fill(row, sym);
    }

    // 3. Holdings.
    for (const r of (includeHold === false ? [] : hold)) {
      const exch = r.exchange || 'NSE';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(sym);
      row.exchange      = row.exchange || exch;
      row.tradingsymbol = sym;
      row.src.h = true;
      const heldQty = Number(r.opening_quantity) || Number(r.quantity) || 0;
      row.qty_hold += heldQty;
      if (r.account) row.accounts.add(String(r.account));
      const liveQ = cq?.[`${exch}:${sym}`];
      if (liveQ?.ltp) {
        row.ltp        = liveQ.ltp;
        row.bid        = liveQ.bid ?? row.bid ?? null;
        row.ask        = liveQ.ask ?? row.ask ?? null;
        row.change     = liveQ.change     ?? row.change     ?? null;
        row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
      } else {
        if (row.ltp == null) row.ltp = r.last_price ?? null;
        if (r.day_change != null && row.change == null)
          row.change = Number(r.day_change);
        if (r.day_change_percentage != null && row.change_pct == null)
          row.change_pct = Number(r.day_change_percentage);
      }
      row.pnl     = (row.pnl     ?? 0) + (Number(r.pnl)            || 0);
      row.day_pnl = (row.day_pnl ?? 0) + (Number(r.day_change_val) || 0);
      fill(row, sym);
    }

    // 4. Option underlyings.
    for (const [logicalName, q] of Object.entries(uq)) {
      const info = q._resolved;
      if (!info) continue;
      const row = get(info.tradingsymbol);
      row.exchange      = row.exchange || info.exchange;
      row.tradingsymbol = info.tradingsymbol;
      row.src.u = true;
      row.underlying    = info.underlying_group;
      row.kind          = info.kind;
      row.ltp        = q.ltp        ?? row.ltp        ?? null;
      row.bid        = q.bid        ?? row.bid        ?? null;
      row.ask        = q.ask        ?? row.ask        ?? null;
      if (row.change == null)     row.change     = q.change ?? null;
      if (row.change_pct == null) row.change_pct = q.change_pct ?? null;
    }

    // 5. Movers — badge any symbol that appears in the movers list; if
    //    it's not already in the grid, add it as a new row.
    const moverSet = new Set((moverRows || []).map(m => String(m.tradingsymbol || '').toUpperCase()));
    for (const m of (moverRows || [])) {
      const sym = String(m.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(sym);
      row.src.m = true;
      row.exchange      = row.exchange || m.exchange || 'NSE';
      row.tradingsymbol = sym;
      // Seed price fields only if not already populated by a higher-priority source.
      if (row.ltp == null && m.last_price != null)    row.ltp        = m.last_price;
      if (row.change_pct == null && m.change_pct != null) row.change_pct = m.change_pct;
      if (m.previous_close != null && row.change == null && row.ltp != null)
        row.change = row.ltp - m.previous_close;
      // Track sticky flag for title tooltip in the badge.
      row._mover_sticky    = m.sticky ?? false;
      row._mover_change_pct = m.change_pct ?? null;
    }
    // When showMovers is off, strip rows that are mover-only (no other source).
    if (!includeMovers) {
      for (const [k, row] of Object.entries(byKey)) {
        if (row.src.m && !row.src.w && !row.src.h && !row.src.p && !row.src.u) {
          delete byKey[k];
        }
      }
    }

    // 6. Watched indices: re-tag tradingsymbol → underlying so the
    //    sort groups them with their derivatives.
    const INDEX_TO_UNDERLYING = {
      'NIFTY 50':         'NIFTY',
      'NIFTY BANK':       'BANKNIFTY',
      'NIFTY FIN SERVICE':'FINNIFTY',
      'NIFTY MID SELECT': 'MIDCPNIFTY',
      'NIFTY NXT 50':     'NIFTYNXT50',
      'SENSEX':           'SENSEX',
      'BANKEX':           'BANKEX',
    };
    for (const row of Object.values(byKey)) {
      const tag = INDEX_TO_UNDERLYING[String(row.tradingsymbol || '').toUpperCase()];
      if (tag) {
        row.underlying = tag;
        row.kind       = 'spot';
      }
    }

    // Finalise: weighted-avg avg_pos + directional day_pct.
    for (const row of Object.values(byKey)) {
      if (row.qty_pos !== 0) {
        row.avg_pos = row._avg_num / row.qty_pos;
      }
      delete row._avg_num;
      const netQty = (Number(row.qty_pos) || 0) + (Number(row.qty_hold) || 0);
      row.day_pct = directional(row.change_pct, netQty);
    }

    // Sort: index groups first, then positions/holdings, watchlist,
    // bare underlyings. Within a group: spot → fut → opt (strike ASC,
    // CE before PE).
    const out = Object.values(byKey);
    const groupKey = (r) => r.underlying || `~~${r.tradingsymbol || ''}`;
    const tierRank = (r) => {
      if (r.kind === 'spot')                 return 0;
      if (r.kind === 'fut')                  return 1;
      if (r.kind === 'opt')                  return 2;
      return 3;
    };
    const optTypeRank = (r) => (r.opt_type === 'CE' ? 0 : r.opt_type === 'PE' ? 1 : 2);
    const INDEX_UNDERLYINGS = new Set([
      'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX',
    ]);
    const groupBucket = /** @type {Record<string, number>} */ ({});
    for (const r of out) {
      const g = String(groupKey(r));
      const u = String(r.underlying || '').toUpperCase();
      const isIdx = INDEX_UNDERLYINGS.has(u);
      const bucket = isIdx ? 0 : (r.src?.p || r.src?.h) ? 1 : r.src?.w ? 2 : 3;
      if (groupBucket[g] == null || bucket < groupBucket[g]) {
        groupBucket[g] = bucket;
      }
    }
    out.sort((a, b) => {
      const ga = String(groupKey(a)), gb = String(groupKey(b));
      const ba = groupBucket[ga] ?? 2, bb = groupBucket[gb] ?? 2;
      if (ba !== bb) return ba - bb;
      if (ga !== gb) return ga.localeCompare(gb);
      const ta = tierRank(a), tb = tierRank(b);
      if (ta !== tb) return ta - tb;
      if (a.kind === 'opt' && b.kind === 'opt') {
        const sa = a.strike ?? 0, sb = b.strike ?? 0;
        if (sa !== sb) return sa - sb;
        return optTypeRank(a) - optTypeRank(b);
      }
      return (a.tradingsymbol || '').localeCompare(b.tradingsymbol || '');
    });
    return out;
  }

  // ── Add / remove ────────────────────────────────────────────────

  async function searchSymbols(q) {
    if (!q || q.length < 3) { typeahead = []; return; }
    try {
      const { searchByPrefix } = await import('$lib/data/instruments');
      typeahead = await searchByPrefix(q.toUpperCase(), 12);
    } catch { typeahead = []; }
  }

  async function addRow() {
    const targetId = focusedListId ?? [...activeIds][0];
    if (!symInput.trim() || targetId == null) return;
    try {
      await addWatchlistItem(targetId, symInput.trim().toUpperCase(), exchInput);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      await loadActive();
    } catch (e) { error = e.message; }
  }

  async function pickFromTypeahead(inst) {
    typeaheadOpen = false;
    exchInput = inst.e;   // auto-fill exchange from typeahead selection
    // If the picked symbol is an underlying (has CE/PE chains), open the
    // inline option picker instead of adding directly.
    const opened = await openOptionPicker(inst.s, inst.e);
    if (opened) return;
    // Direct-add path: equities, futures, CDS, and anything without a chain.
    const targetId = focusedListId ?? [...activeIds][0];
    if (targetId == null) return;
    try {
      await addWatchlistItem(targetId, inst.s, inst.e);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      await loadActive();
    } catch (e) { error = e.message; }
  }

  function pickList(/** @type {number} */ id) {
    const next = new Set(activeIds);
    if (next.has(id)) {
      if (next.size > 1) next.delete(id);
    } else {
      next.add(id);
    }
    activeIds = next;
    focusedListId = id;
    loadActive();
  }

  async function makeList() {
    if (!newListName.trim()) return;
    try {
      const w = await createWatchlist(newListName.trim());
      newListName = '';
      activeIds = new Set([...activeIds, w.id]);
      focusedListId = w.id;
      await loadLists();
      await loadActive();
    } catch (e) { error = e.message; }
  }

  async function dropList(/** @type {number} */ id) {
    if (!confirm('Delete this watchlist?')) return;
    try {
      await deleteWatchlist(id);
      const next = new Set(activeIds);
      next.delete(id);
      activeIds = next;
      if (focusedListId === id) {
        focusedListId = [...next][0] ?? null;
      }
      await loadLists();
      if (activeIds.size === 0 && lists.length) {
        activeIds = new Set([lists[0].id]);
        focusedListId = lists[0].id;
      }
      await loadActive();
    } catch (e) { error = e.message; }
  }

  // ── Grid setup ────────────────────────────────────────────────────

  function symRenderer(params) {
    const row = params.data || {};
    const alias = row.alias ? `<span class="sym-alias"> → ${row.tradingsymbol}</span>` : '';
    const main  = row.alias || row.tradingsymbol || '';
    const badges = [];
    if (row.src?.p) {
      const q = Number(row.qty_pos) || 0;
      badges.push(`<span class="sym-badge badge-p" title="Position">P ${qtyFmt(q)}</span>`);
    }
    if (row.src?.h) {
      const q = Number(row.qty_hold) || 0;
      badges.push(`<span class="sym-badge badge-h" title="Holding">H ${qtyFmt(q)}</span>`);
    }
    if (row.src?.w) {
      badges.push(`<span class="sym-badge badge-w" title="Watchlist">W</span>`);
    }
    if (row.src?.u) {
      badges.push(`<span class="sym-badge badge-u" title="Option underlying">U</span>`);
    }
    if (row.src?.m) {
      const pct = row._mover_change_pct;
      const dir = pct != null && pct >= 0 ? 'pos' : 'neg';
      const arrow = pct != null && pct >= 0 ? '↑' : '↓';
      const sticky = row._mover_sticky ? ' (sticky)' : '';
      const label = pct != null ? `${pct >= 0 ? '+' : ''}${Number(pct).toFixed(2)}%` : '';
      badges.push(`<span class="sym-badge badge-m badge-m-${dir}" title="Top mover ${label}${sticky}">M${arrow}</span>`);
    }
    const badgeHtml = badges.length ? `<span class="sym-badges">${badges.join('')}</span>` : '';
    const removeBtn = (row.src?.w && row.watchlist_item_id != null)
      ? `<span class="sym-remove" data-item="${row.watchlist_item_id}" data-list="${row.watchlist_list_id ?? ''}" title="Remove from watchlist">×</span>`
      : '';
    return `<span class="sym-main">${main}</span>${alias}${badgeHtml}${removeBtn}`;
  }

  function dirCls(v) {
    if (v == null) return 'cell-flat';
    if (v > 0) return 'cell-pos';
    if (v < 0) return 'cell-neg';
    return 'cell-flat';
  }

  function getRowClass(params) {
    const r = params.data || {};
    const s = r.src || {};
    if (s.p) {
      const q = Number(r.qty_pos) || 0;
      if (q < 0) return 'pos-short';
      if (q > 0) return 'pos-long';
      return 'row-pos';
    }
    if (s.h) return 'row-hold';
    if (s.w) return 'row-watch';
    if (s.u) return 'row-und';
    return '';
  }

  function mountGrid() {
    if (!gridEl) return;
    const RA = 'ag-right-aligned-cell';
    const dirCellClass = (p) => `${RA} ${dirCls(p.value)}`;

    const colDefs = /** @type {any[]} */ ([
      { field: 'tradingsymbol', headerName: 'Symbol', width: 190, pinned: 'left',
        cellRenderer: symRenderer, sortable: true,
        cellClass: 'ag-col-sym ag-col-fill' },
      { field: 'ltp', headerName: 'LTP', width: 70,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: RA,
        valueFormatter: numFmt },
      { field: 'day_pnl', headerName: 'Day P&L', width: 88,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: aggFmtGrid },
      { field: 'day_pct', headerName: 'Day %', width: 68,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: pctFmtGrid },
      { field: 'bid', headerName: 'Bid', width: 64,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: numFmt },
      { field: 'ask', headerName: 'Ask', width: 64,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: numFmt },
      { field: 'pnl', headerName: 'P&L', width: 88,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: aggFmtGrid },
      { field: 'expiry', headerName: 'Expiry', width: 80,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: 'ag-right-aligned-cell cell-muted',
        valueFormatter: ({ value }) => value || '' },
    ]);

    grid = createGrid(gridEl, {
      theme: 'legacy',
      columnDefs: colDefs,
      rowData: unifiedRows,
      defaultColDef: {
        resizable: true, sortable: true, suppressMovable: true,
        suppressHeaderMenuButton: true,
      },
      sortingOrder: ['asc', 'desc', null],
      overlayNoRowsTemplate: '<span style="font-size:0.65rem;color:#7e97b8">No rows — add symbols to your watchlist or load positions/holdings</span>',
      domLayout: 'autoHeight',
      getRowClass,
      rowHeight: 28,
      headerHeight: 28,
      onRowClicked: handleRowClick,
    });
    gridReady = true;

    // Positions Summary grid — Account | Day P&L | Day % | P&L
    if (showSummary && positionsSummaryEl) {
      const posSummaryCols = [
        { field: 'account',               headerName: 'Account', width: 90,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 110,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     flex: 1,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
      ];
      positionsSummaryGrid = createGrid(positionsSummaryEl, {
        theme: 'legacy',
        columnDefs: posSummaryCols,
        rowData: [],
        defaultColDef: {
          resizable: true, sortable: true, suppressMovable: true,
          suppressHeaderMenuButton: true,
        },
        sortingOrder: ['asc', 'desc', null],
        domLayout: 'autoHeight',
        rowHeight: 26,
        headerHeight: 26,
      });
      positionsSummaryReady = true;
    }

    // Holdings Summary grid — Account | Day P&L | Day % | P&L | P&L % | Cur Val | Inv Val
    if (showSummary && holdingsSummaryEl) {
      const holdSummaryCols = [
        { field: 'account',               headerName: 'Account', width: 90,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 110,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     width: 110,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'pnl_percentage',        headerName: 'P&L %',   width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'cur_val',               headerName: 'Cur Val', width: 110,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
        { field: 'inv_val',               headerName: 'Inv Val', flex: 1,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
      ];
      holdingsSummaryGrid = createGrid(holdingsSummaryEl, {
        theme: 'legacy',
        columnDefs: holdSummaryCols,
        rowData: [],
        defaultColDef: {
          resizable: true, sortable: true, suppressMovable: true,
          suppressHeaderMenuButton: true,
        },
        sortingOrder: ['asc', 'desc', null],
        domLayout: 'autoHeight',
        rowHeight: 26,
        headerHeight: 26,
      });
      holdingsSummaryReady = true;
    }

    // Funds grid — small per-account margins strip below the main
    // grid in dashboard mode. Wider columns (rupee aggregates).
    if (showFunds && fundsEl) {
      const fundsCols = [
        { field: 'account',      headerName: 'Account',      width: 90,
          cellClass: 'ag-col-fill' },
        { field: 'cash',         headerName: 'Cash',         flex: 1,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'avail_margin', headerName: 'Avail Margin', flex: 1,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
        { field: 'used_margin',  headerName: 'Used Margin',  flex: 1,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
        { field: 'collateral',   headerName: 'Collateral',   flex: 1,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
      ];
      fundsGrid = createGrid(fundsEl, {
        theme: 'legacy',
        columnDefs: fundsCols,
        rowData: [],
        defaultColDef: {
          resizable: true, sortable: true, suppressMovable: true,
          suppressHeaderMenuButton: true,
        },
        sortingOrder: ['asc', 'desc', null],
        domLayout: 'autoHeight',
        rowHeight: 26,
        headerHeight: 26,
      });
      fundsReady = true;
    }
  }

  async function handleRowClick(ev) {
    if (!ev.data) return;
    const target = /** @type {HTMLElement | null} */ (ev.event?.target ?? null);
    const rmBtn = target?.closest?.('.sym-remove');
    if (rmBtn) {
      const itemId = Number(rmBtn.getAttribute('data-item'));
      const listId = Number(rmBtn.getAttribute('data-list'));
      if (itemId && listId) removeItem(listId, itemId);
      return;
    }
    if (!allowOrders) return;
    const r = ev.data;
    const inst = getInstrument?.(r.tradingsymbol);
    const lot = Number(inst?.ls || 1);
    let side = 'BUY';
    if (r.src?.p && r.qty_pos < 0) side = 'BUY';
    else if (r.src?.p && r.qty_pos > 0) side = 'SELL';
    // Use the row's own account field directly — each position/holding
    // row carries one unmasked account id for admin sessions, so this
    // is always correct. The Set+length=1 heuristic was unreliable when
    // a single account appeared after async realAccounts resolution.
    const isRealAcct = (a) => !!(a && !String(a).includes('#'));
    const preAccount = isRealAcct(r.account) ? String(r.account) : '';
    // Ensure accounts are resolved before opening the ticket — avoids
    // a blank picker on first click when onMount hasn't finished yet.
    if (!realAccounts.length) {
      try {
        const r2 = await fetchAccounts();
        realAccounts = (r2?.accounts || [])
          .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
          .filter(Boolean);
      } catch (_) { /* leave empty — ticket's self-fetch backstop will fill */ }
    }
    openTicket({
      symbol:   r.tradingsymbol,
      exchange: r.exchange,
      side,
      qty:      Math.abs(Number(r.qty_pos) || lot),
      lotSize:  lot,
      accounts: realAccounts,
      account:  preAccount,
    });
  }

  function openTicket(p) { ticketProps = { defaultTab: 'ticket', ...p }; }
  function closeTicket() { ticketProps = null; }

  async function removeItem(/** @type {number} */ listId, /** @type {number} */ itemId) {
    try {
      await removeWatchlistItem(listId, itemId);
      await loadActive();
    } catch (e) { error = e.message; }
  }

  let listInputOpen = $state(false);

  function openListInput() { listInputOpen = true; }
  function closeListInput() { listInputOpen = false; newListName = ''; }
  async function makeListAndCollapse() {
    if (!newListName.trim()) return;
    await makeList();
    closeListInput();
  }
</script>

<div class="algo-status-card p-1.5" data-status="inactive">

  {#if error}
    <div class="mb-2 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}

  {#if enableWatchlists || enableSourceToggles || accountPicker}
    <!-- Single combined toolbar: pills + add-symbol + account/source. -->
    <div class="flex flex-wrap items-center gap-1 mb-1.5 relative">
      {#if enableWatchlists}
        {#each lists as l}
          {@const selected = activeIds.has(l.id)}
          {@const focused  = focusedListId === l.id}
          <button onclick={() => pickList(l.id)}
            title={selected ? 'Selected — click to deselect' : 'Click to include'}
            class="px-2.5 py-0.5 text-[0.65rem] font-semibold uppercase tracking-wider rounded transition
                   border {focused ? 'border-[#fbbf24]' : 'border-[rgba(251,191,36,0.3)]'}
                   {selected ? 'bg-[#fbbf24]/20 text-[#fbbf24]' : 'bg-transparent text-[#c8d8f0]/40 hover:bg-[#fbbf24]/10 hover:text-[#fbbf24]/70'}">
            <span class="mr-1 text-[0.55rem]">{selected ? '✓' : '○'}</span>{l.name}
            <span class="ml-1 text-[0.55rem] text-[#7e97b8]">({l.item_count})</span>
            {#if l.is_default}<span class="ml-1 text-[0.5rem] text-[#4ade80]">★</span>{/if}
            {#if focused && !l.is_default && lists.length > 1}
              <span
                role="button"
                tabindex="0"
                onclick={(e) => { e.stopPropagation(); dropList(l.id); }}
                onkeydown={(e) => e.key === 'Enter' && (e.stopPropagation(), dropList(l.id))}
                class="ml-1 text-[0.6rem] text-red-300 hover:text-red-400 cursor-pointer">×</span>
            {/if}
          </button>
        {/each}
        <!-- New-list: collapsed + button, expanded: input + Save -->
        {#if listInputOpen}
          <input bind:value={newListName}
            onkeydown={(e) => {
              if (e.key === 'Enter') makeListAndCollapse();
              else if (e.key === 'Escape') closeListInput();
            }}
            class="field-input text-[0.65rem] py-0.5 px-2 w-32" placeholder="List name" />
          <button onclick={makeListAndCollapse} disabled={!newListName.trim()}
            class="px-2 py-0.5 text-[0.65rem] font-bold text-[#fbbf24] border border-[#fbbf24]/40 rounded hover:bg-[#fbbf24]/10 disabled:opacity-40">
            Save
          </button>
          <button onclick={closeListInput}
            class="px-1.5 py-0.5 text-[0.65rem] text-[#7e97b8] border border-[#7e97b8]/30 rounded hover:bg-white/5">
            ✕
          </button>
        {:else}
          <button onclick={openListInput}
            title="New watchlist"
            class="px-2 py-0.5 text-[0.65rem] font-bold text-[#fbbf24] border border-[#fbbf24]/40 rounded hover:bg-[#fbbf24]/10">
            +
          </button>
        {/if}
        <!-- Add-symbol input + exchange selector + Add button — inline on same row. -->
        <input bind:value={symInput}
          oninput={(e) => { searchSymbols(e.currentTarget.value); typeaheadOpen = true; }}
          onfocus={() => typeaheadOpen = true}
          onkeydown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              if (typeaheadOpen && typeahead.length && symInput.trim()) pickFromTypeahead(typeahead[0]);
              else addRow();
            } else if (e.key === 'Escape') { typeaheadOpen = false; }
          }}
          class="field-input text-[0.65rem] py-0.5 px-2 flex-1 min-w-32"
          placeholder="Add symbol — type 3+ chars" />
        <select bind:value={exchInput} class="field-input text-[0.65rem] py-0.5 px-1 w-16">
          <option>NSE</option><option>BSE</option><option>NFO</option><option>MCX</option><option>CDS</option>
        </select>
        <button onclick={addRow} disabled={!symInput.trim()}
          class="btn-primary text-[0.65rem] py-0.5 px-2.5 disabled:opacity-50">Add</button>
        {#if typeaheadOpen && typeahead.length}
          <div class="absolute top-7 left-0 right-0 max-h-60 overflow-y-auto bg-[#0c1830] border border-[#fbbf24]/30 rounded shadow-lg z-10">
            {#each typeahead as inst}
              <button onclick={() => pickFromTypeahead(inst)}
                class="block w-full text-left px-3 py-1.5 text-xs hover:bg-[#fbbf24]/10">
                <span class="font-mono text-[#fbbf24]">{inst.s}</span>
                <span class="text-[0.6rem] text-[#7e97b8] ml-2">{inst.e}</span>
              </button>
            {/each}
          </div>
        {/if}
      {/if}
      <!-- Right-aligned cluster: account picker + source toggles. -->
      {#if accountPicker || enableSourceToggles}
        <div class="ml-auto flex items-center gap-1">
          {#if accountPicker && availableAccounts.length > 0}
            <select bind:value={selectedAccount}
              title="Filter by broker account"
              class="field-input text-[0.65rem] py-0.5 px-2 max-w-32">
              <option value="all">All accounts</option>
              {#each availableAccounts as a}
                <option value={a}>{a}</option>
              {/each}
            </select>
          {/if}
          {#if enableSourceToggles}
            <div class="w-32">
              <MultiSelect bind:value={selectedSources} options={SOURCE_OPTIONS} placeholder="Sources" />
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {/if}

  {#if enableWatchlists}

    {#if optionPickerUnderlying}
      <!-- Option picker — inline row for CE/PE/Strike selection after
           the operator picks an underlying from the typeahead. -->
      <div class="opt-picker flex flex-wrap items-center gap-1.5 mb-1.5 px-2 py-1
                  bg-[#0c1830] border border-[#fbbf24]/25 rounded">
        <!-- Underlying chip (read-only) -->
        <span class="font-mono text-[0.65rem] font-bold text-[#fbbf24]
                     bg-[#fbbf24]/10 border border-[#fbbf24]/30 px-2 py-0.5 rounded">
          {optionPickerUnderlying.name}
        </span>

        <!-- Expiry dropdown -->
        <select
          bind:value={optionPickerExpiry}
          class="field-input text-[0.65rem] py-0.5 px-1.5 w-28">
          {#each optionPickerExpiries as exp}
            <option value={exp}>{exp}</option>
          {/each}
        </select>

        <!-- CE / PE toggle -->
        <span class="flex rounded overflow-hidden border border-[#fbbf24]/25">
          <button
            onclick={() => optionPickerSide = 'CE'}
            class="text-[0.65rem] font-bold px-2.5 py-0.5 transition-colors
                   {optionPickerSide === 'CE'
                     ? 'bg-[#fbbf24] text-[#0a1628]'
                     : 'text-[#7e97b8] hover:bg-[#fbbf24]/10'}">CE</button>
          <button
            onclick={() => optionPickerSide = 'PE'}
            class="text-[0.65rem] font-bold px-2.5 py-0.5 transition-colors
                   {optionPickerSide === 'PE'
                     ? 'bg-[#fbbf24] text-[#0a1628]'
                     : 'text-[#7e97b8] hover:bg-[#fbbf24]/10'}">PE</button>
        </span>

        <!-- Strike dropdown -->
        <select
          bind:value={optionPickerStrike}
          class="field-input text-[0.65rem] py-0.5 px-1.5 w-24"
          disabled={!optionPickerStrikes.length}>
          {#each optionPickerStrikes as k}
            <option value={k}>{k}</option>
          {/each}
        </select>

        <!-- Add button -->
        <button
          onclick={addOptionFromPicker}
          disabled={optionPickerStrike == null || !optionPickerExpiry}
          class="text-[0.65rem] font-bold px-2.5 py-0.5 rounded
                 bg-[#fbbf24]/90 text-[#0a1628] hover:bg-[#fbbf24]
                 disabled:opacity-40 disabled:cursor-not-allowed">Add</button>

        <!-- Spot quick-add -->
        <button
          onclick={addSpotFromPicker}
          class="text-[0.65rem] px-2 py-0.5 rounded border border-[#7dd3fc]/40
                 text-[#7dd3fc] hover:bg-[#7dd3fc]/10">Spot</button>

        <!-- Cancel -->
        <button
          onclick={closeOptionPicker}
          class="text-[0.65rem] px-2 py-0.5 rounded border border-[#7e97b8]/30
                 text-[#7e97b8] hover:bg-white/5">Cancel</button>
      </div>
    {/if}
  {/if}

  {#if showFunds}
    <!-- Funds strip — per-account Cash / Avail Margin / Used Margin /
         Collateral. Same first-impression order as the previous
         PerformancePage-driven /dashboard ("what's my cash" answers
         before "what's my P&L"). -->
    <div class="mp-section-label">Funds</div>
    <div bind:this={fundsEl} class="ag-theme-algo funds-grid mb-2"></div>
  {/if}

  {#if showSummary}
    <!-- Positions Summary — per-account Day P&L + P&L. -->
    <div class="mp-section-label">Positions Summary</div>
    <div bind:this={positionsSummaryEl} class="ag-theme-algo summary-grid mb-2"></div>
    <!-- Holdings Summary — per-account Day P&L + P&L + Cur Val. -->
    <div class="mp-section-label">Holdings Summary</div>
    <div bind:this={holdingsSummaryEl} class="ag-theme-algo summary-grid mb-2"></div>
  {/if}

  {#if showSummary || showFunds}
    <div class="mp-section-label">Symbols</div>
  {/if}
  <!-- Unified grid — the per-symbol detail view. -->
  <div bind:this={gridEl} class="ag-theme-algo unified-grid"></div>
</div>

{#if allowOrders && ticketProps}
  <OrderEntryShell
    {...ticketProps}
    onSubmit={() => closeTicket()}
    onClose={closeTicket} />
{/if}

<style>
  /* Symbol cell — main + alias. */
  :global(.sym-main)  { color: #e2e8f0; font-weight: 600; }
  :global(.sym-alias) { color: #7e97b8; font-size: 0.55rem; }

  /* Source badges (P / H / W / U) — sit right of the symbol. */
  :global(.sym-badges) {
    display: inline-flex;
    gap: 2px;
    margin-left: 2px;
    vertical-align: middle;
  }
  :global(.sym-badge) {
    display: inline-block;
    padding: 0 3px;
    font-size: 0.5rem;
    font-weight: 700;
    line-height: 12px;
    border-radius: 2px;
    font-variant-numeric: tabular-nums;
  }
  :global(.badge-p) { color: #38bdf8; background: rgba(56,189,248,0.18); }
  :global(.badge-h) { color: #4ade80; background: rgba(74,222,128,0.18); }
  :global(.badge-w) { color: #fbbf24; background: rgba(251,191,36,0.18); }
  :global(.badge-u) { color: #c4b5fd; background: rgba(196,181,253,0.18); }
  :global(.badge-m-pos) { color: #4ade80; background: rgba(74,222,128,0.18); }
  :global(.badge-m-neg) { color: #f87171; background: rgba(248,113,113,0.18); }

  /* Inline remove-from-watchlist button. */
  :global(.sym-remove) {
    display: inline-block;
    margin-left: 6px;
    padding: 0 4px;
    color: rgba(248,113,113,0.45);
    font-size: 0.7rem;
    font-weight: 700;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    transition: color 0.12s ease, background 0.12s ease;
  }
  :global(.sym-remove:hover) {
    color: #f87171;
    background: rgba(248,113,113,0.12);
  }

  /* Day Δ / P&L cells. */
  :global(.cell-pos)  { color: #4ade80 !important; }
  :global(.cell-neg)  { color: #f87171 !important; }
  :global(.cell-flat) { color: #94a3b8 !important; }
  :global(.cell-muted){ color: rgba(200,216,240,0.55) !important; }

  /* Grid containers */
  .unified-grid {
    width: 100%;
    min-height: 60px;
  }
  .summary-grid,
  .funds-grid {
    width: 100%;
    min-height: 40px;
  }

  /* Section labels above each grid in dashboard mode. Small muted
     amber so they read as section headings without competing with
     the data rows or the toolbar. */
  .mp-section-label {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: rgba(251,191,36,0.7);
    margin-bottom: 0.25rem;
  }
</style>
