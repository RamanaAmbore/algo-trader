<script>
  // Unified market-pulse component shared by /pulse (default preset)
  // and /dashboard (Phase 2 preset).
  //
  // A single ag-Grid lists every symbol the operator is tracking,
  // with per-row source badges (W=watchlist, H=holding, P=position,
  // U=option-underlying) so the same symbol never appears twice.
  // The merge engine, batch-quote pipeline, symbol-cell renderer,
  // row-tint CSS, and SymbolPanel wiring all live here — pages
  // compose by toggling props:
  //
  //   enableWatchlists    — show tab strip / add row / remove ×
  //   enableSourceToggles — show "P · Positions" / "H · Holdings" pills
  //   allowOrders         — row click opens SymbolPanel
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
    fetchMovers, fetchSparklines,
  } from '$lib/api';
  import { visibleInterval, clientTimestamp } from '$lib/stores';
  import { createPerformanceSocket } from '$lib/ws';
  import { priceFmt, pctFmt, aggCompact, qtyFmt, directional } from '$lib/format';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import MultiSelect from '$lib/MultiSelect.svelte';
  import Select      from '$lib/Select.svelte';

  let {
    title              = 'Market Pulse',
    enableWatchlists   = true,
    enableSourceToggles = true,
    allowOrders        = true,
    // Phase 2 presets — dashboard mode turns these on.
    accountPicker      = false, // <select> next to the toolbar
    showSummary        = false, // small per-account summary grid above the main grid
    showFunds          = false, // small per-account funds grid below the main grid
    showSymbolsGrid    = true,  // unified symbols grid at the bottom; /dashboard passes false
    // /pulse passes `flat=true` to drop the outer .algo-status-card
    // chrome — the unified grid (with its own fixed-header scroll
    // behaviour) becomes the page's primary surface instead of being
    // boxed inside a non-scrolling navy card.
    flat               = false,
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

  /**
   * Add-to-watchlist with silent dedup against live positions + holdings.
   * If the symbol is already in either book, we skip the underlying
   * `addWatchlistItem` call entirely — the unified grid would have shown
   * it twice (once as the position/holding row, once as the watchlist
   * row), which is exactly the kind of duplication /pulse is meant to
   * eliminate. Returns true if the row was actually appended.
   */
  async function addToWatchlistDeduped(targetId, sym, exch) {
    const needle = String(sym || '').toUpperCase();
    if (!needle) return false;
    const already = (rows) => rows.some(
      r => String(r.symbol || r.tradingsymbol || '').toUpperCase() === needle
    );
    if (already(positions) || already(holdings)) {
      return false;  // silent skip — already present in positions/holdings
    }
    await addWatchlistItem(targetId, sym, exch);
    return true;
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
      await addToWatchlistDeduped(targetId, inst.s, inst.e || 'NFO');
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
      await addToWatchlistDeduped(targetId, sym, exch);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      closeOptionPicker();
      await loadActive();
    } catch (e) { error = e.message; }
  }

  // New-list form state.
  let newListName = $state('');

  // Source toggles — driven by the MultiSelect below. Watchlist +
  // the pinned-index/commodity group are now first-class filters so
  // the operator can hide either bucket without leaving the page.
  const SOURCE_OPTIONS = [
    { value: 'pinned',    label: 'Pinned'    },  // indices + commodities + USDINR
    { value: 'watchlist', label: 'Watchlist' },
    { value: 'positions', label: 'Positions' },
    { value: 'holdings',  label: 'Holdings'  },
    { value: 'movers',    label: 'Movers'    },
  ];
  let selectedSources = $state(['pinned', 'watchlist', 'positions', 'holdings', 'movers']);

  // Keep individual booleans so buildUnified + other callsites are unchanged.
  let showWatchlist = $state(true);
  let showPinned    = $state(true);
  let showPositions = $state(true);
  let showHoldings  = $state(true);

  // Movers — top-% movers from /watchlist/movers. Polled every 30 s.
  // Failure is non-fatal: the rest of the page keeps working.
  let movers     = $state(/** @type {any[]} */ ([]));
  let showMovers = $state(true);

  $effect(() => {
    showPinned    = selectedSources.includes('pinned');
    showWatchlist = selectedSources.includes('watchlist');
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

  let sparklines = $state(/** @type {Record<string, number[]>} */ ({}));
  let stopSparkPoll;
  let stopWS;

  // ── Manual group order + symbol detachment (per-browser overrides) ──
  // Operator clicks ↑ / ↓ on a row → that row's underlying group moves
  // up or down within its bucket. Clicks the ⋯ menu → "Detach from
  // group" pulls the symbol out of its underlying group entirely so
  // it sorts by itself at the end of the bucket.
  //
  // Persistence: localStorage, per-browser. Backend sync would need a
  // schema migration we don't want yet. Reset clears both overrides.
  const LS_GROUP_ORDER = 'pulse:groupOrder';   // { NIFTY: 0, BANKNIFTY: 1, ... }
  const LS_DETACHED    = 'pulse:detached';     // ["NIFTY25APR21000PE", ...]
  let groupOrder       = $state(/** @type {Record<string, number>} */ ({}));
  let detachedSymbols  = $state(/** @type {string[]} */ ([]));

  function loadOverrides() {
    try {
      const g = JSON.parse(localStorage.getItem(LS_GROUP_ORDER) || '{}');
      if (g && typeof g === 'object') groupOrder = g;
    } catch (_) { /* corrupt JSON — start fresh */ }
    try {
      const d = JSON.parse(localStorage.getItem(LS_DETACHED) || '[]');
      if (Array.isArray(d)) detachedSymbols = d;
    } catch (_) { /* corrupt JSON — start fresh */ }
  }

  function saveGroupOrder() {
    try { localStorage.setItem(LS_GROUP_ORDER, JSON.stringify(groupOrder)); } catch (_) {}
  }
  function saveDetached() {
    try { localStorage.setItem(LS_DETACHED, JSON.stringify(detachedSymbols)); } catch (_) {}
  }

  /** Move the row's underlying group up (-1) or down (+1) within its bucket. */
  function moveGroup(row, dir) {
    const g = String(row?.underlying || row?.tradingsymbol || '').toUpperCase();
    if (!g) return;
    // Build the visible group list at the row's bucket, sorted current.
    const myBucket = unifiedRows
      .filter(r => bucketOf(r) === bucketOf(row))
      .map(r => String(r.underlying || `~~${r.tradingsymbol || ''}`).toUpperCase());
    // Preserve order of first appearance — that's the current sort.
    const seen = new Set();
    const ordered = [];
    for (const k of myBucket) {
      if (!seen.has(k)) { seen.add(k); ordered.push(k); }
    }
    const idx = ordered.indexOf(g);
    if (idx < 0) return;
    const newIdx = idx + dir;
    if (newIdx < 0 || newIdx >= ordered.length) return;
    // Swap into the new position and persist ALL groups in the bucket
    // with the new ranks so the comparator can read them back.
    [ordered[idx], ordered[newIdx]] = [ordered[newIdx], ordered[idx]];
    const next = { ...groupOrder };
    ordered.forEach((k, i) => { next[k] = i; });
    groupOrder = next;
    saveGroupOrder();
  }

  /** True iff the symbol is currently in the detached set. */
  function isDetached(sym) {
    return detachedSymbols.includes(String(sym || '').toUpperCase());
  }

  function detachSymbol(row) {
    const sym = String(row?.tradingsymbol || '').toUpperCase();
    if (!sym || isDetached(sym)) return;
    detachedSymbols = [...detachedSymbols, sym];
    saveDetached();
  }

  function reattachSymbol(row) {
    const sym = String(row?.tradingsymbol || '').toUpperCase();
    if (!sym) return;
    detachedSymbols = detachedSymbols.filter(s => s !== sym);
    saveDetached();
  }

  function resetOverrides() {
    groupOrder = {};
    detachedSymbols = [];
    saveGroupOrder();
    saveDetached();
  }

  const hasOverrides = $derived(
    Object.keys(groupOrder).length > 0 || detachedSymbols.length > 0
  );

  // Bucket-of helper — mirrors the bucket logic inside buildUnified
  // so moveGroup can constrain its swap to the same bucket.
  // Order: watchlist → positions → holdings → everything else.
  function bucketOf(row) {
    if (row?.src?.w) return 1;
    if (row?.src?.p) return 2;
    if (row?.src?.h) return 3;
    return 4;
  }
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
  let listFutures       = $state(/** @type {((u: string) => any[]) | null} */ (null));

  // Transient-error suppression. Quote-refresh polls fire every 5 s
  // and can blip on broker hiccups; one failed call shouldn't paint
  // the page red. Show the banner only after 3 consecutive failures.
  let quoteFailStreak = 0;

  // Order-ticket integration — row click opens the SymbolPanel
  // pre-filled with the row's symbol / exchange / lot size. Stays
  // null when no ticket is open. Real broker accounts (unmasked
  // account_id) fetched once on mount so the operator can pick.
  let ticketProps     = $state(/** @type {any} */ (null));
  let realAccounts    = $state(/** @type {string[]} */ ([]));

  async function loadSparklines() {
    const pairs = [];
    const seen = new Set();
    for (const row of unifiedRows) {
      if (!row.tradingsymbol) continue;
      const sym  = String(row.tradingsymbol).toUpperCase();
      const exch = String(row.exchange || 'NSE').toUpperCase();
      const key  = `${exch}:${sym}`;
      if (!seen.has(key)) {
        seen.add(key);
        pairs.push({ tradingsymbol: sym, exchange: exch });
      }
    }
    if (!pairs.length) return;
    try {
      const res = await fetchSparklines(pairs, 5);
      if (res?.data && typeof res.data === 'object') {
        sparklines = { ...sparklines, ...res.data };
      }
    } catch (_) { /* non-fatal — sparklines are cosmetic */ }
  }

  onMount(async () => {
    // Auth is enforced by the (algo) layout — no goto('/signin')
    // here so this component can also be embedded in flows that
    // intentionally allow anonymous demo viewers.
    loadOverrides();
    await tick();
    mountGrid();

    try {
      const mod = await import('$lib/data/instruments');
      await mod.loadInstruments();
      getInstrument     = mod.getInstrument;
      findNearestFuture = mod.findNearestFuture;
      listFutures       = mod.listFutures;
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
    stopMoversPoll  = visibleInterval(loadMovers, 30000);
    await loadSparklines();
    stopSparkPoll   = visibleInterval(loadSparklines, 60000);

    // Real-time order-fill push — Kite postback fires a WS event
    // `position_filled` the moment an order fills. Subscribe so
    // Market Pulse refreshes positions + holdings IMMEDIATELY
    // instead of waiting up to 10 s for the next loadPulse tick.
    // Other (non-fill) events on the same socket also trigger a
    // refresh — cheap to over-fetch, expensive to lag a fill.
    stopWS = createPerformanceSocket((msg) => {
      if (msg?.event === 'position_filled') {
        // Order just filled — refresh both books right now so the
        // grid shows the new qty within a tick. The 10 s pulse
        // poll keeps running as a backstop.
        loadPulse();
      }
    });

    // Keyboard shortcuts — scoped to this wrapper only.
    document.addEventListener('keydown', handleKeydown);
    document.addEventListener('click', onDocClick);
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

  // Pinned-top group — any watchlist row whose underlying is in
  // this set bypasses column sort via ag-Grid's pinnedTopRowData.
  // Display order within the pinned block is operator-meaningful:
  // broad indices first, narrowing down, then BSE, volatility,
  // currency, and finally commodities (precious → energy → base).
  // Operator can detach individual rows via the ⋯ context menu.
  const PIN_ORDER = {
    // ── Indices (NSE broad → NSE sector → NSE narrow → BSE) ──
    NIFTY:        1,
    BANKNIFTY:    2,
    FINNIFTY:     3,
    MIDCPNIFTY:   4,
    SMALLCAP:     5,
    NIFTYNXT50:   6,
    SENSEX:       7,
    BANKEX:       8,
    // ── Volatility ──
    INDIAVIX:     9,
    // ── Currencies (NSE CDS) ──
    USDINR:      10,
    // ── Commodities (MCX): precious → energy → base ──
    GOLD:        11,
    GOLDM:       12,
    SILVER:      13,
    SILVERM:     14,
    SILVERMIC:   15,
    CRUDEOIL:    16,
    CRUDEOILM:   17,
    NATURALGAS:  18,
    COPPER:      19,
  };
  const PINNED_INDEX_UNDERLYINGS = new Set(Object.keys(PIN_ORDER));
  function isPinnedIndexRow(r) {
    if (!r?.src?.w) return false;
    if (isDetached(r.tradingsymbol)) return false;  // operator override
    const u = String(r.underlying || '').toUpperCase();
    return PINNED_INDEX_UNDERLYINGS.has(u);
  }
  function pinRank(r) {
    return PIN_ORDER[String(r.underlying || '').toUpperCase()] ?? 999;
  }
  // ag-Grid renders pinnedTopRowData in array order (no column-sort
  // applied), so this sorted slice IS the effective display order.
  const pinnedTopRows = $derived(
    !showPinned
      ? []
      : unifiedRows.filter(isPinnedIndexRow).slice().sort((a, b) => {
          const ra = pinRank(a), rb = pinRank(b);
          if (ra !== rb) return ra - rb;
          return String(a.tradingsymbol || '').localeCompare(String(b.tradingsymbol || ''));
        })
  );
  // mainRows respects the Watchlist filter (positions/holdings/movers
  // already filter at the buildUnified entry via show* booleans, so we
  // don't double-filter them here). Rows that are SOLELY watchlist
  // entries hide when showWatchlist is off; rows that are also a
  // position/holding stay visible via their other source flag.
  const mainRows = $derived(unifiedRows.filter(r => {
    if (isPinnedIndexRow(r)) return false;  // pinned, never in main
    if (!showWatchlist && r.src?.w && !r.src?.p && !r.src?.h && !r.src?.u && !r.src?.m) return false;
    return true;
  }));

  $effect(() => {
    if (!gridReady || !grid) return;
    grid.setGridOption('rowData', mainRows);
    grid.setGridOption('pinnedTopRowData', pinnedTopRows);
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

  // Per-account cash debited on currently-held long options — same
  // derivation the strip uses, but bucketed by account. For each long
  // CE/PE position row:
  //   num_lots = quantity / lot_size
  //   cash    = average_price × lot_size × num_lots
  // (mathematically equivalent to avg × qty, but explicit so the
  // formula matches the operator's mental model and stays correct
  // for any adapter that surfaces qty in lots).
  const longOptCashByAccount = $derived.by(() => {
    /** @type {Record<string, number>} */
    const m = {};
    let total = 0;
    for (const p of positions) {
      const sym = String(p?.tradingsymbol || '').toUpperCase();
      if (!(sym.endsWith('CE') || sym.endsWith('PE'))) continue;
      const rawQty = Number(p?.quantity) || 0;
      if (rawQty <= 0) continue;
      const qty = Math.abs(rawQty);
      const avg = Number(p?.average_price) || 0;
      const inst    = getInstrument?.(sym);
      const lotSize = Number(inst?.ls) || 0;
      const v = lotSize > 0
        ? avg * lotSize * (qty / lotSize)
        : avg * qty;  // fallback when instruments cache cold
      const a = String(p?.account || '');
      if (a) m[a] = (m[a] || 0) + v;
      total += v;
    }
    m.TOTAL = total;
    return m;
  });

  const scopedFunds = $derived.by(() => {
    const list = selectedAccount === 'all'
      ? funds
      : funds.filter(r => String(r.account || '') === selectedAccount);
    // Inject _long_opt_cash so the grid's `cash_total` valueGetter
    // can pick it up without poking back into the positions array.
    return list.map(r => ({
      ...r,
      _long_opt_cash: longOptCashByAccount[String(r.account || '')] || 0,
    }));
  });

  $effect(() => {
    if (!fundsReady || !fundsGrid) return;
    const body  = scopedFunds.filter(r => !isTotalRow(r));
    const total = scopedFunds.filter(isTotalRow);
    fundsGrid.setGridOption('rowData', body);
    fundsGrid.setGridOption('pinnedBottomRowData', total);
  });

  onDestroy(() => {
    stopPoll?.(); stopPulsePoll?.(); stopMoversPoll?.(); stopSparkPoll?.(); stopWS?.();
    document.removeEventListener('keydown', handleKeydown);
    document.removeEventListener('click', onDocClick);
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

  // Resolve underlying for an OPTION position. For MCX commodities the
  // option's underlying is the same-month future (CRUDEOIL26JUN10500CE
  // settles to CRUDEOIL26JUNFUT, not the front-month MAY future). For
  // indices/stocks the spot is shared across all expiries so we just
  // delegate to resolveUnderlying.
  function resolveUnderlyingForOption(name, optionExpiryISO,
                                       /** @type {((u: string) => any) | null} */ findNearestFut,
                                       /** @type {((u: string) => any[]) | null} */ listFuts) {
    const n = String(name || '').toUpperCase();
    if (!n) return null;
    if (MCX_COMMODITIES.has(n) && optionExpiryISO && listFuts) {
      const ym = String(optionExpiryISO).slice(0, 7);  // YYYY-MM
      const futs = listFuts(n) || [];
      const same = futs.find(f => String(f.x || '').slice(0, 7) === ym);
      if (same?.s && same?.e) {
        return {
          tradingsymbol: same.s,
          exchange: same.e,
          quoteKey: `${same.e}:${same.s}`,
          underlying_group: `${n}_${ym}`,
          kind: 'fut',
        };
      }
    }
    // Non-MCX or no same-month match → fall back to nearest-month
    // (still useful — indices/stocks have a single spot, the U badge
    // groups every option month under it).
    return resolveUnderlying(n, findNearestFut);
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
      const listFuts = listFutures;
      // Keyed by underlying_group so MCX options with different
      // contract months (CRUDEOIL26MAY vs CRUDEOIL26JUN) each map to
      // their own same-month future. Indices/stocks share one key.
      const addUnderlying = (inst) => {
        if (!inst?.u) return;
        const t = String(inst.t || '').toUpperCase();
        if (t === 'EQ') return;
        const isOpt = (t === 'CE' || t === 'PE');
        const info = isOpt
          ? resolveUnderlyingForOption(inst.u, inst.x, nearestFut, listFuts)
          : resolveUnderlying(inst.u, nearestFut);
        if (info && !underlyingInfos.has(info.underlying_group)) {
          underlyingInfos.set(info.underlying_group, info);
        }
      };
      for (const r of positions) {
        const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
        const exch = r.exchange || 'NFO';
        if (lookup) addUnderlying(lookup(sym));
        if (sym) contractKeys.add(`${exch}:${sym}`);
      }
      for (const r of holdings) {
        const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
        const exch = r.exchange || 'NSE';
        if (lookup) addUnderlying(lookup(sym));
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

  // The two override-state vars are read inside buildUnified's
  // closure; declare them here so Svelte 5 tracks them as deps and
  // re-derives unifiedRows when the operator clicks ↑/↓/Detach.
  const unifiedRows = $derived(((groupOrder, detachedSymbols), buildUnified(
    activeLists, watchQuotes, scopedPositions, scopedHoldings, pulseQuotes, getInstrument,
    showPositions, showHoldings, movers, showMovers,
  )));

  function parseSymbol(/** @type {string} */ sym, /** @type {any} */ instCache) {
    const inst = instCache ? instCache(sym) : null;
    // Cache lookup is preferred but commodity options + brand-new
    // contracts may not be indexed yet — fall back to a regex parser
    // so the row still gets the right underlying/strike/opt_type for
    // grouping + sorting.
    if (inst) {
      const t = String(inst.t || '').toUpperCase();
      const k = inst.k != null ? Number(inst.k) : null;
      let optType = null;
      if (t === 'CE' || t === 'PE') optType = t;
      else if (/CE$/i.test(sym)) optType = 'CE';
      else if (/PE$/i.test(sym)) optType = 'PE';
      const kind = optType ? 'opt' : (t === 'FUT' ? 'fut' : (t === 'EQ' ? 'eq' : null));
      return { underlying: inst.u || null, kind, strike: k, opt_type: optType, expiry: inst.x || null };
    }
    return parseSymbolFallback(sym);
  }

  /**
   * Regex-only parser for F&O tradingsymbols when the instruments
   * cache misses (commodity options, new contracts, dev fixtures).
   * Pulls underlying / strike / opt_type so group + sort logic still
   * lands the row in its correct bucket.
   *
   * Matches:
   *   CRUDEOIL26JUN10000CE → CRUDEOIL / 10000 / CE / opt
   *   NIFTY25APR21500PE    → NIFTY / 21500 / PE / opt
   *   NIFTY25APRFUT        → NIFTY / null  / null / fut
   *   GOLDM26JUN78000CE    → GOLDM / 78000 / CE / opt
   */
  function parseSymbolFallback(/** @type {string} */ sym) {
    const empty = { underlying: null, kind: null, strike: null, opt_type: null, expiry: null };
    if (!sym) return empty;
    const m = /^([A-Z][A-Z&]+?)(\d{2}[A-Z]{3})(\d+)?(CE|PE|FUT)$/.exec(sym);
    if (!m) return empty;
    const [, underlying, expiry, strikeStr, suffix] = m;
    const strike = strikeStr ? Number(strikeStr) : null;
    const opt_type = (suffix === 'CE' || suffix === 'PE') ? suffix : null;
    const kind = opt_type ? 'opt' : (suffix === 'FUT' ? 'fut' : null);
    return { underlying, kind, strike, opt_type, expiry };
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

    // 2. Positions. Keyed by `${sym}__P` so the same tradingsymbol
    //    that ALSO appears as a holding renders as its OWN row (user
    //    explicitly wants the position/holding split kept). Multiple
    //    accounts holding the same position symbol still merge here
    //    because the suffix is per-source, not per-account.
    for (const r of (includePos === false ? [] : pos)) {
      const exch = r.exchange || 'NFO';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(`${sym}__P`);
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

    // 3. Holdings. Keyed by `${sym}__H` for the same reason — keep
    //    a position vs holding for the same symbol as TWO rows.
    for (const r of (includeHold === false ? [] : hold)) {
      const exch = r.exchange || 'NSE';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(`${sym}__H`);
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
      'NIFTY 50':              'NIFTY',
      'NIFTY BANK':            'BANKNIFTY',
      'NIFTY FIN SERVICE':     'FINNIFTY',
      'NIFTY MID SELECT':      'MIDCPNIFTY',
      'NIFTY NXT 50':          'NIFTYNXT50',
      // NSE small-cap benchmark — tag so the pin bucket catches it.
      'NIFTY SMLCAP 250':      'SMALLCAP',
      'NIFTY SMALLCAP 100':    'SMALLCAP',
      'NIFTY SMALLCAP 50':     'SMALLCAP',
      'SENSEX':                'SENSEX',
      'BANKEX':                'BANKEX',
      // Volatility — tag INDIA VIX so it joins the pin bucket and
      // groups under the same underlying as VIX-derived rows.
      'INDIA VIX':             'INDIAVIX',
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
    // Detached symbols → each becomes its own single-row group keyed
    // off the tradingsymbol with a `__DETACHED__` prefix so it sorts
    // distinctly from the auto-group. The prefix puts detached rows
    // at the bottom of the bucket via localeCompare ordering.
    const detachedSet = new Set(detachedSymbols.map(s => s.toUpperCase()));
    const groupKey = (r) => {
      const sym = String(r.tradingsymbol || '').toUpperCase();
      if (detachedSet.has(sym)) return `__DETACHED__${sym}`;
      return r.underlying || `~~${r.tradingsymbol || ''}`;
    };
    const tierRank = (r) => {
      if (r.kind === 'spot')                 return 0;
      if (r.kind === 'fut')                  return 1;
      if (r.kind === 'opt')                  return 2;
      return 3;
    };
    const optTypeRank = (r) => (r.opt_type === 'CE' ? 0 : r.opt_type === 'PE' ? 1 : 2);
    // Bucket priority — operator picked: watchlist first, then
    // positions, then holdings, then everything else (option
    // underlyings, movers, detached symbols). No automatic index pin
    // — indices appear in whichever bucket their source dictates
    // (they're watched, so they end up in the watchlist bucket).
    const groupBucket = /** @type {Record<string, number>} */ ({});
    for (const r of out) {
      const g = String(groupKey(r));
      const bucket = r.src?.w ? 1
                   : r.src?.p ? 2
                   : r.src?.h ? 3
                   : 4;
      if (groupBucket[g] == null || bucket < groupBucket[g]) {
        groupBucket[g] = bucket;
      }
    }
    // Manual group order — operator's ↑/↓ overrides take precedence
    // within a bucket. Groups without a manual rank sort alphabetically
    // AFTER manually-ranked groups.
    const order = groupOrder || {};
    const rankOf = (g) => {
      const u = String(g || '').toUpperCase();
      return order[u] != null ? order[u] : null;
    };
    out.sort((a, b) => {
      const ga = String(groupKey(a)), gb = String(groupKey(b));
      const ba = groupBucket[ga] ?? 2, bb = groupBucket[gb] ?? 2;
      if (ba !== bb) return ba - bb;
      if (ga !== gb) {
        const ra = rankOf(ga), rb = rankOf(gb);
        if (ra != null && rb != null && ra !== rb) return ra - rb;
        if (ra != null && rb == null) return -1;
        if (ra == null && rb != null) return  1;
        return ga.localeCompare(gb);
      }
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
      await addToWatchlistDeduped(targetId, symInput.trim().toUpperCase(), exchInput);
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
      await addToWatchlistDeduped(targetId, inst.s, inst.e);
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
    // Per-row actions menu — ⋯ pops a Chart / Watchlist / Trade
    // popover via handleRowClick → _actionsMenu state. Hidden on
    // bare-underlying placeholder rows where there's no resolvable
    // symbol to act on.
    const sym = String(row.tradingsymbol || '').trim();
    const exch = String(row.exchange || '').trim();
    const actionsBtn = sym
      ? `<span class="sym-actions" data-sym="${sym}" data-exch="${exch}" data-watchitem="${row.watchlist_item_id ?? ''}" title="Symbol actions">⋯</span>`
      : '';
    // Per-row ↑/↓ buttons — move the row's whole underlying group up
    // or down within its bucket. Visible on row hover (CSS handles
    // the hover-only display); the actual move happens in
    // handleRowClick via data-attr dispatch.
    const moveBtns = sym
      ? `<span class="sym-move" data-dir="-1" title="Move group up">▲</span>` +
        `<span class="sym-move" data-dir="1"  title="Move group down">▼</span>`
      : '';
    return `<span class="sym-main">${main}</span>${alias}${badgeHtml}${removeBtn}${moveBtns}${actionsBtn}`;
  }

  /**
   * Inline SVG sparkline for the last N daily closes.
   * ~60×18px, no axes/labels, stroke coloured by direction.
   * Missing data → em-dash placeholder.
   */
  function sparkRenderer(params) {
    const sym    = String((params.data || {}).tradingsymbol || '').toUpperCase();
    const closes = sparklines[sym];
    if (!closes || closes.length < 2) {
      return '<span style="color:#7e97b8;font-size:0.6rem;padding:0 4px">—</span>';
    }
    const W = 58, H = 16, PAD = 2;
    const min = Math.min(...closes);
    const max = Math.max(...closes);
    const range = max - min || 1;
    const xStep = (W - PAD * 2) / (closes.length - 1);
    const yOf   = (v) => PAD + (1 - (v - min) / range) * (H - PAD * 2);
    const pts   = closes.map((v, i) => `${(PAD + i * xStep).toFixed(1)},${yOf(v).toFixed(1)}`).join(' ');
    const up    = closes[closes.length - 1] >= closes[0];
    const color = up ? 'rgba(91,142,149,0.85)' : 'rgba(196,122,61,0.85)';
    return `<svg width="${W}" height="${H}" style="display:block;overflow:visible"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.2" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
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
    const RA = 'ag-right-aligned-cell';
    const dirCellClass = (p) => `${RA} ${dirCls(p.value)}`;

    // Main symbols grid — only built when the parent opted into the
    // per-symbol view (/pulse). /dashboard passes showSymbolsGrid=false
    // because it shows only summary + funds; the summary/funds grids
    // below still need to mount in that case.
    if (showSymbolsGrid && gridEl) {

    const colDefs = /** @type {any[]} */ ([
      { field: 'tradingsymbol', headerName: 'Symbol', width: 168, pinned: 'left',
        cellRenderer: symRenderer, sortable: true,
        cellClass: 'ag-col-sym ag-col-fill' },
      { field: 'tradingsymbol', headerName: '', width: 56, colId: 'sparkline',
        cellRenderer: sparkRenderer, sortable: false, resizable: true,
        cellClass: 'spark-cell' },
      { field: 'ltp', headerName: 'LTP', width: 80,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: RA,
        valueFormatter: numFmt },
      { field: 'day_pnl', headerName: 'Day P&L', width: 64,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: aggFmtGrid },
      { field: 'day_pct', headerName: 'Day %', width: 56,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: pctFmtGrid },
      { field: 'bid', headerName: 'Bid', width: 52,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: numFmt },
      { field: 'ask', headerName: 'Ask', width: 52,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: `${RA} cell-muted`,
        valueFormatter: numFmt },
      { field: 'pnl', headerName: 'P&L', width: 64,
        type: 'numericColumn', headerClass: numericHdr,
        cellClass: dirCellClass,
        valueFormatter: aggFmtGrid },
      { field: 'expiry', headerName: 'Expiry', width: 60,
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
      // Fixed-height layout (not autoHeight) so ag-Grid's built-in
      // sticky header behaviour kicks in — data rows scroll inside
      // the grid viewport while the column headers stay pinned at
      // the top of the grid box. autoHeight was making the grid
      // grow with every new row, so when the operator scrolled the
      // page the header slid out of view with the rows. Fixed
      // height = sticky header + scrolling data area.
      // 28 px header + 28 px row × 18 rows + 6 px slack = ~520 px.
      // Long lists scroll inside the grid; short lists just leave
      // empty white space at the bottom which the operator can
      // tolerate (we'd otherwise have to swap autoHeight in
      // dynamically — overkill for the current row counts).
      domLayout: 'normal',
      getRowClass,
      rowHeight: 28,
      headerHeight: 28,
      onRowClicked: handleRowClick,
      onSortChanged: saveColumnState,
      onColumnResized: saveColumnState,
      onGridReady: () => { restoreColumnState(); },
      onCellContextMenu: (ev) => {
        if (ev.data) openContextMenu(ev.event, ev.data);
      },
    });
    gridReady = true;
    }  // end main symbols grid block

    // Positions Summary grid — Account | Day P&L | Day % | P&L
    // Compact widths — aggCompact maxes at ~8 chars ("-999.99L") so
    // 78 px fits every rupee value plus the standard cell padding.
    if (showSummary && positionsSummaryEl) {
      const posSummaryCols = [
        { field: 'account',               headerName: 'Account', width: 54,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     width: 78,
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
    // Compact widths matching the Positions Summary above.
    if (showSummary && holdingsSummaryEl) {
      const holdSummaryCols = [
        { field: 'account',               headerName: 'Account', width: 54,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl',               headerName: 'Day P&L', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'day_change_percentage', headerName: 'Day %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'pnl',                   headerName: 'P&L',     width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'pnl_percentage',        headerName: 'P&L %',   width: 60,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: pctFmtGrid },
        { field: 'cur_val',               headerName: 'Cur Val', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
        { field: 'inv_val',               headerName: 'Inv Val', width: 78,
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

    // Funds grid — per-account margins. Compact widths matching the
    // summary grids above. Header labels shortened (Avail Margin →
    // Avail Mar, Used Margin → Used Mar, Collateral → Coll) so the
    // headers don't truncate at 78 px column width.
    if (showFunds && fundsEl) {
      const fundsCols = [
        { field: 'account',        headerName: 'Account',   width: 54,
          cellClass: 'ag-col-fill' },
        { field: 'cash_total',     headerName: 'Cash',      width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid,
          headerTooltip: 'Live cash + cash debited on currently-held long options (= cash you would have if every long option were closed at its entry premium)',
          // live_cash + sum(avg_price × |qty|) for long CE/PE rows in
          // this account. `_long_opt_cash` is pre-computed by the
          // scopedFunds derivation. Falls back to row.cash when
          // live_cash is missing (older API cached during deploy).
          valueGetter: (/** @type {any} */ p) => {
            const d = p?.data || {};
            const lc  = Number(d.live_cash ?? 0);
            const loc = Number(d._long_opt_cash ?? 0);
            return (lc !== 0 ? lc : Number(d.cash ?? 0)) + loc;
          } },
        { field: 'live_cash',      headerName: 'Live Cash', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid,
          headerTooltip: 'Current cash — decreases when option premium is debited' },
        { field: '_long_opt_cash', headerName: 'Opt Cash',  width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Cash debited on currently-held long options (sum of avg_price × |qty| across each long CE/PE)' },
        { field: 'avail_margin',   headerName: 'Avail Mar', width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Available margin — net trading buffer' },
        { field: 'used_margin',    headerName: 'Used Mar',  width: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Margin currently locked against open positions' },
        { field: 'collateral',     headerName: 'Coll',      flex: 1, minWidth: 78,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid,
          headerTooltip: 'Collateral value from pledged holdings' },
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
    // ▲/▼ group-move buttons — bump the row's whole underlying group
    // up or down. The bucket stays the same (pinned indices stay in
    // pinned, watchlist in watchlist) so swaps are constrained.
    const moveBtn = target?.closest?.('.sym-move');
    if (moveBtn) {
      ev.event?.stopPropagation?.();
      const dir = Number(moveBtn.getAttribute('data-dir')) || 0;
      if (dir !== 0) moveGroup(ev.data, dir);
      return;
    }
    // ⋯ actions button — re-uses the existing right-click context
    // menu (which already has Open in Options / Open ticket / Copy
    // symbol / Set price alert items). Positioning is anchored to
    // the button's bottom-right so it pops next to the symbol.
    const actBtn = target?.closest?.('.sym-actions');
    if (actBtn) {
      ev.event?.stopPropagation?.();
      const rect = /** @type {DOMRect} */ (actBtn.getBoundingClientRect());
      openContextMenu(
        { clientX: rect.right, clientY: rect.bottom + 4, preventDefault: () => {} },
        ev.data,
      );
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

  // ── Context menu ─────────────────────────────────────────────────
  /** @type {{ x: number, y: number, row: any } | null} */
  let ctxMenu = $state(null);
  let ctxMenuEl = $state(/** @type {HTMLElement | null} */ (null));

  function openContextMenu(ev, row) {
    ev.preventDefault();
    ctxMenu = { x: ev.clientX, y: ev.clientY, row };
  }
  function closeContextMenu() { ctxMenu = null; }

  function ctxOpenOptions(row) {
    closeContextMenu();
    const sym = encodeURIComponent(row.tradingsymbol || '');
    window.location.href = `/admin/options?symbol=${sym}`;
  }
  function ctxOpenTicket(row) {
    closeContextMenu();
    // Synthesise a fake ag-Grid event shape handleRowClick expects.
    handleRowClick({ data: row, event: null });
  }
  async function ctxCopySymbol(row) {
    closeContextMenu();
    try { await navigator.clipboard.writeText(row.tradingsymbol || ''); } catch (_) {}
  }
  function ctxSetAlert(row) {
    closeContextMenu();
    console.info('[MarketPulse] set price alert placeholder:', row.tradingsymbol);
  }
  /** 📈 Chart action — opens SymbolPanel on the Chart tab for the
   *  row's symbol. Historical bars fetched lazily inside the panel
   *  when the Chart tab activates. Earlier this opened a separate
   *  SymbolChartModal; folded into the unified panel so chart +
   *  ticket + chain all live in one symbol-keyed surface. */
  function ctxOpenChart(row) {
    closeContextMenu();
    const sym = String(row.tradingsymbol || '').trim();
    if (!sym) return;
    openTicket({
      symbol:    sym,
      exchange:  String(row.exchange || '').trim(),
      defaultTab: 'chart',
    });
  }

  async function ctxRemoveWatch(row) {
    closeContextMenu();
    if (row.watchlist_item_id && row.watchlist_list_id) {
      await removeItem(row.watchlist_list_id, row.watchlist_item_id);
    }
  }
  /** Add the symbol to the focused / first-active watchlist. Mirror of
   *  the existing keyboard "+" handler. */
  async function ctxAddWatch(row) {
    closeContextMenu();
    const sym  = String(row.tradingsymbol || '').trim();
    const exch = String(row.exchange || 'NFO').trim();
    if (!sym) return;
    const targetId = focusedListId ?? [...activeIds][0];
    if (!targetId) return;
    try {
      await addToWatchlistDeduped(targetId, sym, exch);
      // Reload active watchlists so the row materialises immediately
      // without waiting for the next refresh cycle.
      await loadActive();
    } catch (e) {
      // 409 = already in list — silently ignore, the operator's
      // intent (have this symbol in the list) is already satisfied.
      const msg = String(/** @type {any} */ (e)?.message || '');
      if (!/already/i.test(msg)) {
        console.warn('Add to watchlist failed:', e);
      }
    }
  }

  // Dismiss context menu on outside click.
  function onDocClick(ev) {
    if (ctxMenu && ctxMenuEl && !ctxMenuEl.contains(/** @type {Node} */ (ev.target))) {
      closeContextMenu();
    }
  }

  // ── Keyboard shortcuts ────────────────────────────────────────────
  let pulseWrapper = $state(/** @type {HTMLElement | null} */ (null));
  /** @type {HTMLInputElement | null} */ let symInputEl = null;

  function handleKeydown(ev) {
    // Never intercept shortcuts when typing in an input/textarea.
    const tag = /** @type {HTMLElement} */ (ev.target).tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

    if (ev.key === 'Escape') {
      if (ctxMenu) { closeContextMenu(); return; }
      if (optionPickerUnderlying) { closeOptionPicker(); return; }
      if (typeaheadOpen) { typeaheadOpen = false; return; }
      if (listInputOpen) { closeListInput(); return; }
      ticketProps = null;
      return;
    }
    if (ev.key === '/') {
      ev.preventDefault();
      symInputEl?.focus();
      return;
    }
    if (ev.key === 'n') {
      ev.preventDefault();
      openListInput();
      return;
    }
    if (ev.key === 'j' || ev.key === 'k') {
      if (!grid) return;
      ev.preventDefault();
      const api = grid;
      // Attempt to move selection.
      try {
        const focused = api.getFocusedCell();
        if (!focused) {
          api.setFocusedCell(0, 'tradingsymbol');
        } else {
          const next = focused.rowIndex + (ev.key === 'j' ? 1 : -1);
          if (next >= 0) api.setFocusedCell(next, focused.column);
        }
      } catch (_) {}
    }
  }

  // ── Column-state persistence (sort + width) ───────────────────────
  const COL_STATE_KEY = 'pulse.gridColumnState.v1';

  function saveColumnState() {
    if (!grid) return;
    try {
      const state = grid.getColumnState();
      localStorage.setItem(COL_STATE_KEY, JSON.stringify(state));
    } catch (_) {}
  }

  function restoreColumnState() {
    if (!grid) return;
    try {
      const raw = localStorage.getItem(COL_STATE_KEY);
      if (!raw) return;
      const state = JSON.parse(raw);
      grid.applyColumnState({ state, applyOrder: true });
    } catch (_) {}
  }

  // ── Per-source subtotals (item 1) ─────────────────────────────────
  const subtotals = $derived.by(() => {
    const rows = unifiedRows;
    let hDayPnl = 0, hPnl = 0, hCurVal = 0, hCount = 0;
    let pDayPnl = 0, pPnl = 0, pCount = 0;
    let mCount = 0, mTopSym = '', mTopPct = /** @type {number|null} */ (null);

    for (const r of rows) {
      if (r.src?.h) {
        hDayPnl += Number(r.day_pnl) || 0;
        hPnl    += Number(r.pnl)     || 0;
        // Cur Val: qty_hold × ltp
        const cv = (Number(r.qty_hold) || 0) * (Number(r.ltp) || 0);
        hCurVal += cv;
        hCount++;
      }
      if (r.src?.p) {
        pDayPnl += Number(r.day_pnl) || 0;
        pPnl    += Number(r.pnl)     || 0;
        pCount++;
      }
      if (r.src?.m) {
        mCount++;
        const pct = r._mover_change_pct;
        if (pct != null && (mTopPct == null || Math.abs(pct) > Math.abs(mTopPct))) {
          mTopPct = pct;
          mTopSym = r.tradingsymbol || '';
        }
      }
    }
    return { hDayPnl, hPnl, hCurVal, hCount, pDayPnl, pPnl, pCount, mCount, mTopSym, mTopPct };
  });

  const hasSubtotals = $derived(
    subtotals.hCount > 0 || subtotals.pCount > 0 || subtotals.mCount > 0
  );
</script>

<div bind:this={pulseWrapper}
     class={flat ? 'mp-flat-wrap' : 'algo-status-card p-1.5'}
     data-status="inactive">

  {#if error}
    <div class="mb-2 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}

  {#if enableWatchlists || enableSourceToggles || accountPicker}
    <!-- Row 1 — Watchlist tabs (+ "new list" affordance on the right). -->
    {#if enableWatchlists}
      <div class="flex flex-wrap items-center gap-1 mb-1">
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
            class="px-2 py-0.5 text-[0.65rem] font-bold text-[#fbbf24] border border-[#fbbf24]/40 rounded hover:bg-[#fbbf24]/10 disabled:opacity-40"
            title="Save new watchlist">Save</button>
          <button onclick={closeListInput}
            class="px-1.5 py-0.5 text-[0.65rem] text-[#7e97b8] border border-[#7e97b8]/30 rounded hover:bg-white/5"
            title="Cancel">✕</button>
        {:else}
          <button onclick={openListInput} title="New watchlist"
            class="px-2 py-0.5 text-[0.65rem] font-bold text-[#fbbf24] border border-[#fbbf24]/40 rounded hover:bg-[#fbbf24]/10 ml-auto">
            + List
          </button>
        {/if}
      </div>
    {/if}

    <!-- Row 2 — Add-symbol input on the left, source/account filters on the right. -->
    <div class="flex flex-wrap items-center gap-1 mb-1.5 relative">
      {#if enableWatchlists}
        <!-- Symbol input — placeholder is the field's purpose, not a hint about
             keystroke counts. Width is constrained but flex-grows on wide screens. -->
        <input bind:this={symInputEl} bind:value={symInput}
          oninput={(e) => { searchSymbols(e.currentTarget.value); typeaheadOpen = true; }}
          onfocus={() => typeaheadOpen = true}
          onkeydown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              if (typeaheadOpen && typeahead.length && symInput.trim()) pickFromTypeahead(typeahead[0]);
              else addRow();
            } else if (e.key === 'Escape') { typeaheadOpen = false; }
          }}
          class="field-input text-[0.65rem] py-0.5 px-2 flex-1 min-w-32 max-w-56"
          placeholder="Symbol" />
        <div class="w-16">
          <Select ariaLabel="Exchange" bind:value={exchInput}
            options={[
              { value: 'NSE', label: 'NSE' },
              { value: 'BSE', label: 'BSE' },
              { value: 'NFO', label: 'NFO' },
              { value: 'MCX', label: 'MCX' },
              { value: 'CDS', label: 'CDS' },
            ]} />
        </div>
        <button onclick={addRow} disabled={!symInput.trim()}
          class="btn-primary text-[0.65rem] py-0.5 px-2.5 disabled:opacity-50"
          title="Add to focused watchlist">Add</button>
        {#if typeaheadOpen && typeahead.length}
          <div class="absolute top-7 left-0 w-80 max-w-full max-h-60 overflow-y-auto bg-[#0c1830] border border-[#fbbf24]/30 rounded shadow-lg z-10">
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
            <div class="w-32" title="Filter by broker account">
              <Select ariaLabel="Account filter" bind:value={selectedAccount}
                options={[
                  { value: 'all', label: 'All accounts' },
                  ...availableAccounts.map(a => ({ value: a, label: a })),
                ]} />
            </div>
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

  <!-- Per-source subtotals strip (item 1) — shown when ≥1 source has rows. -->
  {#if hasSubtotals}
    <div class="subtotals-strip">
      {#if subtotals.hCount > 0}
        <span class="st-group">
          <span class="st-src">H</span>
          <span class="st-chip">Day <span class={subtotals.hDayPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.hDayPnl)}</span></span>
          <span class="st-chip">P&amp;L <span class={subtotals.hPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.hPnl)}</span></span>
          <span class="st-chip">Cur <span class="st-val">{aggCompact(subtotals.hCurVal)}</span></span>
        </span>
        {#if subtotals.pCount > 0 || subtotals.mCount > 0}<span class="st-sep">|</span>{/if}
      {/if}
      {#if subtotals.pCount > 0}
        <span class="st-group">
          <span class="st-src">P</span>
          <span class="st-chip">Day <span class={subtotals.pDayPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.pDayPnl)}</span></span>
          <span class="st-chip">P&amp;L <span class={subtotals.pPnl >= 0 ? 'st-pos' : 'st-neg'}>{aggCompact(subtotals.pPnl)}</span></span>
        </span>
        {#if subtotals.mCount > 0}<span class="st-sep">|</span>{/if}
      {/if}
      {#if subtotals.mCount > 0}
        <span class="st-group">
          <span class="st-src">M</span>
          <span class="st-chip">{subtotals.mCount} movers{subtotals.mTopSym ? '' : ''}</span>
          {#if subtotals.mTopSym && subtotals.mTopPct != null}
            <span class="st-chip"><span class="st-sym">{subtotals.mTopSym}</span> <span class={subtotals.mTopPct >= 0 ? 'st-pos' : 'st-neg'}>{subtotals.mTopPct >= 0 ? '+' : ''}{subtotals.mTopPct.toFixed(2)}%</span></span>
          {/if}
        </span>
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
        <div class="w-28">
          <Select ariaLabel="Expiry" bind:value={optionPickerExpiry}
            options={optionPickerExpiries.map(exp => ({ value: exp, label: exp }))} />
        </div>

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
        <div class="w-24">
          <Select ariaLabel="Strike" bind:value={optionPickerStrike}
            disabled={!optionPickerStrikes.length}
            options={optionPickerStrikes.map(k => ({ value: k, label: String(k) }))} />
        </div>

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

  {#if showSymbolsGrid}
    {#if showSummary || showFunds}
      <div class="mp-section-label">Symbols</div>
    {/if}
    <!-- Unified grid — the per-symbol detail view. -->
    <div bind:this={gridEl} class="ag-theme-algo unified-grid"></div>
  {/if}
</div>

{#if allowOrders && ticketProps}
  <SymbolPanel
    {...ticketProps}
    onSubmit={() => closeTicket()}
    onClose={closeTicket}
    onAddToWatchlist={async (sym, exch) => {
      // Adds the symbol to whichever watchlist the operator currently
      // has focused (or the first active one). Surfaces in the panel
      // header as a `+W` button. Useful when the operator opens
      // SymbolPanel for a contract they don't yet own — e.g. an
      // option strike from a chain pick on /admin/options that
      // they want to track here too.
      const targetId = focusedListId ?? [...activeIds][0];
      if (!targetId) throw new Error('No active watchlist');
      await addToWatchlistDeduped(targetId, sym, exch || 'NFO');
      await loadActive();
    }} />
{/if}

<!-- Context menu (item 2) -->
{#if ctxMenu}
  <div
    bind:this={ctxMenuEl}
    class="ctx-menu"
    style="left:{ctxMenu.x}px;top:{ctxMenu.y}px"
    role="menu">
    <button class="ctx-item" onclick={() => ctxOpenChart(ctxMenu.row)}>📈 Chart →</button>
    <button class="ctx-item" onclick={() => ctxOpenOptions(ctxMenu.row)}>🧮 Open in Options →</button>
    <button class="ctx-item" onclick={() => ctxOpenTicket(ctxMenu.row)}>📝 Open ticket →</button>
    {#if !ctxMenu.row?.src?.w}
      <!-- ★ Add to watchlist — visible when the symbol is NOT already
           in the operator's watchlist. The other branch below shows
           the Remove counterpart. -->
      <button class="ctx-item" onclick={() => ctxAddWatch(ctxMenu.row)}>★ Add to watchlist</button>
    {/if}
    <button class="ctx-item" onclick={() => ctxCopySymbol(ctxMenu.row)}>Copy symbol</button>
    <button class="ctx-item" onclick={() => ctxSetAlert(ctxMenu.row)}>Set price alert</button>
    <div class="ctx-sep"></div>
    {#if isDetached(ctxMenu.row?.tradingsymbol)}
      <button class="ctx-item" onclick={() => { reattachSymbol(ctxMenu.row); closeContextMenu(); }}>↩ Re-attach to group</button>
    {:else if ctxMenu.row?.underlying}
      <button class="ctx-item" onclick={() => { detachSymbol(ctxMenu.row); closeContextMenu(); }}>↗ Detach from group</button>
    {/if}
    {#if hasOverrides}
      <button class="ctx-item" onclick={() => { resetOverrides(); closeContextMenu(); }}>↻ Reset all overrides</button>
    {/if}
    {#if ctxMenu.row?.src?.w && ctxMenu.row?.watchlist_item_id != null}
      <div class="ctx-sep"></div>
      <button class="ctx-item ctx-item-danger" onclick={() => ctxRemoveWatch(ctxMenu.row)}>Remove from watchlist</button>
    {/if}
  </div>
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

  /* ⋯ symbol-actions button — sibling of .sym-remove. Routes click
     through openContextMenu() so the existing right-click menu also
     opens on left-click of this affordance. */
  :global(.sym-actions) {
    display: inline-block;
    margin-left: 4px;
    padding: 0 4px;
    color: rgba(126,151,184,0.55);
    font-size: 0.75rem;
    font-weight: 700;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    transition: color 0.12s ease, background 0.12s ease;
  }
  :global(.sym-actions:hover) {
    color: #fbbf24;
    background: rgba(251,191,36,0.10);
  }

  /* ▲ / ▼ group-move buttons. Hidden by default, revealed on row
     hover so the icons don't compete with the symbol/badge content
     in the resting state. */
  :global(.sym-move) {
    display: inline-block;
    margin-left: 2px;
    padding: 0 3px;
    color: rgba(126,151,184,0.45);
    font-size: 0.55rem;
    line-height: 12px;
    cursor: pointer;
    user-select: none;
    border-radius: 2px;
    opacity: 0;
    transition: opacity 0.12s ease, color 0.12s ease, background 0.12s ease;
  }
  :global(.ag-row:hover .sym-move) { opacity: 1; }
  :global(.sym-move:hover) {
    color: #fbbf24;
    background: rgba(251,191,36,0.12);
  }

  /* Mobile / touch screens — hover doesn't trigger reliably, so the
     ▲/▼ buttons stay visible at low opacity by default. The ⋯ menu
     trigger is already always-visible. */
  @media (hover: none), (max-width: 768px) {
    :global(.sym-move) { opacity: 0.55; }
    :global(.sym-move:active) {
      color: #fbbf24;
      background: rgba(251,191,36,0.18);
    }
  }

  /* Day Δ / P&L cells. */
  :global(.cell-pos)  { color: #4ade80 !important; }
  :global(.cell-neg)  { color: #f87171 !important; }
  :global(.cell-flat) { color: #94a3b8 !important; }
  :global(.cell-muted){ color: rgba(200,216,240,0.55) !important; }

  /* Grid containers */
  .unified-grid {
    width: 100%;
    /* Fixed-height grid pairs with `domLayout: 'normal'` (set when the
       grid is created) so ag-Grid pins the column header at the top
       of this box and only the data area scrolls when row count
       exceeds visible space. Height tuned for ~18 rows at 28 px each
       + 28 px header + 6 px slack. Operators with longer watchlists
       scroll inside the grid; the surrounding page no longer carries
       the header away when it scrolls. */
    height: 520px;
    min-height: 520px;
  }
  /* Flat-mode wrapper — used by /pulse to drop the .algo-status-card
     navy chrome. The page becomes a flex column whose unified grid
     grows to fill the remaining viewport height, so the operator
     gets the maximum number of rows on-screen with the column
     header pinned at the top. */
  .mp-flat-wrap {
    padding: 0.4rem;
    display: flex;
    flex-direction: column;
    /* 100vh minus navbar (4 rem) + page chrome (~3 rem). Fits inside
       the algo layout without overflowing the viewport. */
    min-height: calc(100vh - 7rem);
  }
  .mp-flat-wrap .unified-grid {
    /* Explicit calc-based height so ag-Grid's domLayout:normal viewport
       measurement is reliable on first mount. The previous
       `flex: 1 1 auto; height: auto;` setup rendered correctly in dev
       preview but in production the grid's container measured at 0 px
       during ag-Grid's synchronous mount, leaving subsequent
       setGridOption('rowData') calls invisible. A definite pixel
       height computed from viewport units sidesteps the issue while
       still filling the page (the calc subtracts navbar + page chrome
       + toolbar + sticky banners that sit between the navbar and
       the grid). */
    height: calc(100vh - 11rem);
    min-height: 320px;
    flex: none;
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

  /* Per-source subtotals strip (item 1) */
  .subtotals-strip {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.35rem;
    margin-bottom: 0.35rem;
    padding: 0.25rem 0.5rem;
    background: rgba(251,191,36,0.04);
    border: 1px solid rgba(251,191,36,0.12);
    border-radius: 4px;
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    font-variant-numeric: tabular-nums;
    line-height: 1.2;
  }
  .st-group {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .st-src {
    font-weight: 800;
    color: rgba(251,191,36,0.85);
    font-size: 0.6rem;
    letter-spacing: 0.06em;
  }
  .st-chip {
    color: rgba(200,216,240,0.65);
  }
  .st-val  { color: rgba(200,216,240,0.9); font-weight: 600; }
  .st-pos  { color: #4ade80; font-weight: 600; }
  .st-neg  { color: #f87171; font-weight: 600; }
  .st-sym  { color: rgba(251,191,36,0.75); }
  .st-sep  { color: rgba(200,216,240,0.2); padding: 0 0.15rem; }

  /* Context menu (item 2) */
  :global(.ctx-menu) {
    position: fixed;
    z-index: 9999;
    min-width: 10rem;
    background: rgba(10,22,40,0.97);
    border: 1px solid rgba(251,191,36,0.2);
    border-radius: 5px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.55);
    padding: 0.25rem 0;
    font-size: 0.65rem;
  }
  :global(.ctx-item) {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0.3rem 0.75rem;
    background: transparent;
    border: none;
    color: rgba(200,216,240,0.85);
    cursor: pointer;
    font-size: 0.65rem;
    white-space: nowrap;
    transition: background 0.1s, color 0.1s;
  }
  :global(.ctx-item:hover) {
    background: rgba(251,191,36,0.1);
    color: #fbbf24;
  }
  :global(.ctx-item-danger) { color: rgba(248,113,113,0.8); }
  :global(.ctx-item-danger:hover) { background: rgba(248,113,113,0.1); color: #f87171; }
  :global(.ctx-sep) {
    height: 1px;
    background: rgba(200,216,240,0.1);
    margin: 0.2rem 0;
  }
</style>
