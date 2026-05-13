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
  // showFundsCard, compactHeader.

  import { onMount, onDestroy, tick } from 'svelte';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import {
    fetchWatchlists, fetchWatchlist, createWatchlist,
    deleteWatchlist, addWatchlistItem, removeWatchlistItem,
    fetchWatchlistQuotes,
    fetchPositions, fetchHoldings, fetchAccounts, fetchFunds, batchQuote,
  } from '$lib/api';
  import { visibleInterval, clientTimestamp } from '$lib/stores';
  import { priceFmt, pctFmt, aggCompact, qtyFmt, directional } from '$lib/format';
  import OrderEntryShell from '$lib/order/OrderEntryShell.svelte';

  let {
    title              = 'Market Pulse',
    enableWatchlists   = true,
    enableSourceToggles = true,
    allowOrders        = true,
    // Phase 2 presets — dashboard mode turns these on.
    accountPicker      = false, // <select> next to the toolbar
    showSummary        = false, // small per-account summary grid above the main grid
    showFunds          = false, // small per-account funds grid below the main grid
    compactHeader      = false, // collapse title + ts onto the toolbar row
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

  // New-list form state.
  let newListName = $state('');
  let showCreate  = $state(false);

  // Source toggles. When OFF, the corresponding loop in buildUnified
  // is skipped — that source contributes no rows, no badges, no qty
  // or P&L. Both ON (default) is the existing combined view.
  let showPositions = $state(true);
  let showHoldings  = $state(true);

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
  let summaryEl = $state(/** @type {HTMLDivElement | null} */ (null));
  let fundsEl   = $state(/** @type {HTMLDivElement | null} */ (null));
  let grid;
  let summaryGrid;
  let fundsGrid;
  // Sentinel that flips to true once createGrid runs — used by the
  // $effect below to push unifiedRows into ag-Grid the moment both
  // (a) the grid is mounted and (b) the derived row set has populated.
  let gridReady = $state(false);
  let summaryReady = $state(false);
  let fundsReady   = $state(false);

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
    stopPoll      = visibleInterval(async () => { await loadQuotes(); }, 5000);
    stopPulsePoll = visibleInterval(async () => {
      await loadPulse();
      if (showFunds) await loadFunds();
    }, 10000);
  });

  async function loadFunds() {
    try {
      const r = await fetchFunds();
      funds = (r?.rows || []).slice();
    } catch (_) { /* nothing fatal */ }
  }

  $effect(() => {
    const rows = unifiedRows;
    if (!gridReady || !grid) return;
    grid.setGridOption('rowData', rows);
  });

  // Combine positions+holdings backend summaries into one per-account
  // row carrying Day P&L + P&L + Cur Val. Account picker scopes the
  // body rows; the TOTAL pseudo-row stays pinned to the bottom.
  function isTotalRow(r) { return r && r.account === 'TOTAL'; }

  const combinedSummary = $derived.by(() => {
    if (!showSummary) return [];
    const byAcct = /** @type {Record<string, any>} */ ({});
    const add = (r, src) => {
      const a = r.account;
      if (!a) return;
      if (!byAcct[a]) byAcct[a] = { account: a, day_pnl: 0, pnl: 0, cur_val: 0 };
      byAcct[a].day_pnl += Number(r.day_change_val) || 0;
      byAcct[a].pnl     += Number(r.pnl)            || 0;
      if (src === 'h') byAcct[a].cur_val += Number(r.cur_val) || 0;
    };
    for (const r of holdingsSummary)  add(r, 'h');
    for (const r of positionsSummary) add(r, 'p');
    return Object.values(byAcct);
  });

  const summaryBody  = $derived(
    selectedAccount === 'all'
      ? combinedSummary.filter(r => !isTotalRow(r))
      : combinedSummary.filter(r => r.account === selectedAccount)
  );
  const summaryTotal = $derived(combinedSummary.filter(isTotalRow));

  $effect(() => {
    if (!summaryReady || !summaryGrid) return;
    const body  = summaryBody;
    const total = summaryTotal;
    summaryGrid.setGridOption('rowData', body);
    summaryGrid.setGridOption('pinnedBottomRowData', total);
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
    stopPoll?.(); stopPulsePoll?.();
    grid?.destroy?.();
    summaryGrid?.destroy?.();
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
    showPositions, showHoldings,
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

  function buildUnified(actLists, wq, pos, hold, pq, getInst, includePos, includeHold) {
    const uq = pq?.underlyings || {};
    const cq = pq?.contracts   || {};
    const byKey = /** @type {Record<string, any>} */ ({});
    const get = (key) => byKey[key] || (byKey[key] = {
      key,
      src: { w:false, h:false, p:false, u:false },
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

    // 5. Watched indices: re-tag tradingsymbol → underlying so the
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
    if (!q || q.length < 2) { typeahead = []; return; }
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
      newListName = ''; showCreate = false;
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

    // Summary grid — small per-account totals strip above the main
    // grid in dashboard mode. Same theming + numeric formatters as
    // the main grid; renderless symbol column (just the Account chip).
    if (showSummary && summaryEl) {
      const summaryCols = [
        { field: 'account', headerName: 'Account', width: 90,
          cellClass: 'ag-col-fill' },
        { field: 'day_pnl', headerName: 'Day P&L', width: 110,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'pnl', headerName: 'P&L', width: 110,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: dirCellClass, valueFormatter: aggFmtGrid },
        { field: 'cur_val', headerName: 'Cur Val', width: 120,
          type: 'numericColumn', headerClass: numericHdr,
          cellClass: RA, valueFormatter: aggFmtGrid },
      ];
      summaryGrid = createGrid(summaryEl, {
        theme: 'legacy',
        columnDefs: summaryCols,
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
      summaryReady = true;
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

  function handleRowClick(ev) {
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
    openTicket({
      symbol:   r.tradingsymbol,
      exchange: r.exchange,
      side,
      qty:      Math.abs(Number(r.qty_pos) || lot),
      lotSize:  lot,
      accounts: realAccounts.length ? realAccounts : [],
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
</script>

<div class="algo-status-card p-5 pt-4" data-status="inactive">
  {#if !compactHeader}
    <div class="flex items-center justify-between mb-1 gap-2 flex-wrap">
      <h1 class="text-sm font-bold uppercase tracking-wider text-[#fbbf24] mb-0">{title}</h1>
      <span class="algo-ts">{refreshedAt || clientTimestamp()}</span>
    </div>
    <div class="border-b border-[rgba(251,191,36,0.25)] mb-3"></div>
  {/if}

  {#if error}
    <div class="mb-2 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}

  {#if enableWatchlists || enableSourceToggles || accountPicker}
    <div class="flex flex-wrap items-center gap-1 mb-2">
      {#if enableWatchlists}
        {#each lists as l}
          {@const selected = activeIds.has(l.id)}
          {@const focused  = focusedListId === l.id}
          <button onclick={() => pickList(l.id)}
            title={selected ? 'Selected — click to deselect' : 'Click to include'}
            class="px-3 py-1 text-[0.65rem] font-semibold uppercase tracking-wider rounded transition
                   border {focused ? 'border-[#fbbf24]' : 'border-[rgba(251,191,36,0.3)]'}
                   {selected ? 'bg-[#fbbf24]/20 text-[#fbbf24]' : 'bg-transparent text-[#c8d8f0]/40 hover:bg-[#fbbf24]/10 hover:text-[#fbbf24]/70'}">
            <span class="mr-1 text-[0.55rem]">{selected ? '✓' : '○'}</span>{l.name}
            <span class="ml-1 text-[0.55rem] text-[#7e97b8]">({l.item_count})</span>
            {#if l.is_default}<span class="ml-1 text-[0.5rem] text-[#4ade80]">★</span>{/if}
          </button>
        {/each}
        <button onclick={() => showCreate = !showCreate}
          class="px-2 py-1 text-[0.65rem] font-bold text-[#fbbf24] border border-[#fbbf24]/40 rounded hover:bg-[#fbbf24]/10">
          + New
        </button>
        {#if focusedListId != null && lists.find(l => l.id === focusedListId)?.is_default !== true && lists.length > 1}
          <button onclick={() => dropList(focusedListId)}
            class="px-2 py-1 text-[0.65rem] text-red-300 border border-red-400/40 rounded hover:bg-red-500/10">
            Delete list
          </button>
        {/if}
      {/if}
      <!-- Right-aligned cluster: account picker + source toggles. -->
      {#if accountPicker || enableSourceToggles}
        <div class="ml-auto flex items-center gap-1">
          {#if accountPicker && availableAccounts.length > 0}
            <select bind:value={selectedAccount}
              title="Filter by broker account"
              class="field-input text-[0.65rem] py-1 px-2 max-w-32">
              <option value="all">All accounts</option>
              {#each availableAccounts as a}
                <option value={a}>{a}</option>
              {/each}
            </select>
          {/if}
          {#if enableSourceToggles}
            <button onclick={() => showPositions = !showPositions}
              title={showPositions ? 'Hide positions' : 'Show positions'}
              class="px-2 py-1 text-[0.65rem] font-semibold uppercase tracking-wider rounded border transition
                     {showPositions
                       ? 'bg-[#38bdf8]/20 text-[#38bdf8] border-[#38bdf8]/40'
                       : 'bg-transparent text-[#c8d8f0]/40 border-[#c8d8f0]/20 hover:text-[#38bdf8]/70'}">
              P · Positions
            </button>
            <button onclick={() => showHoldings = !showHoldings}
              title={showHoldings ? 'Hide holdings' : 'Show holdings'}
              class="px-2 py-1 text-[0.65rem] font-semibold uppercase tracking-wider rounded border transition
                     {showHoldings
                       ? 'bg-[#4ade80]/20 text-[#4ade80] border-[#4ade80]/40'
                       : 'bg-transparent text-[#c8d8f0]/40 border-[#c8d8f0]/20 hover:text-[#4ade80]/70'}">
              H · Holdings
            </button>
          {/if}
        </div>
      {/if}
    </div>
  {/if}

  {#if enableWatchlists && showCreate}
    <div class="flex items-center gap-2 mb-2">
      <input bind:value={newListName} class="field-input flex-1" placeholder="New watchlist name"
        onkeydown={(e) => e.key === 'Enter' && makeList()} />
      <button onclick={makeList} class="btn-primary text-[0.65rem] py-1 px-3">Create</button>
      <button onclick={() => { showCreate = false; newListName = ''; }}
        class="btn-secondary text-[0.65rem] py-1 px-3">Cancel</button>
    </div>
  {/if}

  {#if enableWatchlists}
    <!-- Add-symbol row -->
    <div class="flex items-center gap-2 mb-2 relative">
      <input bind:value={symInput}
        oninput={(e) => { searchSymbols(e.currentTarget.value); typeaheadOpen = true; }}
        onfocus={() => typeaheadOpen = true}
        onkeydown={(e) => {
          if (e.key === 'Enter')      { e.preventDefault(); addRow(); }
          else if (e.key === 'Escape') { typeaheadOpen = false; }
        }}
        class="field-input flex-1" placeholder="Add symbol to {lists.find(l => l.id === focusedListId)?.name ?? 'list'} — type 2+ chars" />
      <select bind:value={exchInput} class="field-input w-24">
        <option>NSE</option><option>BSE</option><option>NFO</option><option>MCX</option><option>CDS</option>
      </select>
      <button onclick={addRow} disabled={!symInput.trim()}
        class="btn-primary text-[0.65rem] py-1 px-3 disabled:opacity-50">Add</button>
      {#if typeaheadOpen && typeahead.length}
        <div class="absolute top-10 left-0 right-32 max-h-60 overflow-y-auto bg-[#0c1830] border border-[#fbbf24]/30 rounded shadow-lg z-10">
          {#each typeahead as inst}
            <button onclick={() => pickFromTypeahead(inst)}
              class="block w-full text-left px-3 py-1.5 text-xs hover:bg-[#fbbf24]/10">
              <span class="font-mono text-[#fbbf24]">{inst.s}</span>
              <span class="text-[0.6rem] text-[#7e97b8] ml-2">{inst.e}</span>
              {#if inst.u && inst.u !== inst.s}<span class="text-[0.55rem] text-[#7e97b8] ml-1">· {inst.u}</span>{/if}
            </button>
          {/each}
        </div>
      {/if}
    </div>
  {/if}

  {#if showFunds}
    <!-- Funds strip — per-account Cash / Avail Margin / Used Margin /
         Collateral. Same first-impression order as the previous
         PerformancePage-driven /dashboard ("what's my cash" answers
         before "what's my P&L"). -->
    <div class="mp-section-label">Funds</div>
    <div bind:this={fundsEl} class="ag-theme-algo funds-grid mb-3"></div>
  {/if}

  {#if showSummary}
    <!-- Per-account Day P&L + P&L + Cur Val totals (positions +
         holdings combined). Body filters by account picker; TOTAL
         pinned at the bottom. -->
    <div class="mp-section-label">Summary</div>
    <div bind:this={summaryEl} class="ag-theme-algo summary-grid mb-3"></div>
  {/if}

  {#if showSummary || showFunds}
    <div class="mp-section-label">Holdings / Positions / Watchlist</div>
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
