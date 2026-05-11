<script>
  // Unified market-pulse page — algo dark theme.
  //
  // A single ag-Grid lists every symbol the operator is tracking, with
  // per-row source badges (W=watchlist, H=holding, P=position,
  // U=option-underlying) so the same symbol never appears twice. The
  // watchlist tab strip stays on top for picking which named list
  // feeds the W flag.

  import { onMount, onDestroy, tick } from 'svelte';
  import { goto } from '$app/navigation';
  import { createGrid, ModuleRegistry, AllCommunityModule } from 'ag-grid-community';
  import {
    fetchWatchlists, fetchWatchlist, createWatchlist,
    deleteWatchlist, addWatchlistItem, removeWatchlistItem,
    fetchWatchlistQuotes,
    fetchPositions, fetchHoldings, batchQuote,
  } from '$lib/api';
  import { authStore, visibleInterval, clientTimestamp } from '$lib/stores';
  import { aggFmt, priceFmt, qtyFmt } from '$lib/format';

  ModuleRegistry.registerModules([AllCommunityModule]);

  let lists       = $state(/** @type {any[]} */ ([]));
  let activeId    = $state(/** @type {number | null} */ (null));
  let active      = $state(/** @type {any} */ (null));
  let watchQuotes = $state(/** @type {Record<number, any>} */ ({}));
  let positions   = $state(/** @type {any[]} */ ([]));
  let holdings    = $state(/** @type {any[]} */ ([]));
  let underlyingQuotes = $state(/** @type {Record<string, any>} */ ({}));
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

  let stopPoll, stopPulsePoll;
  let gridEl;
  let grid;

  // Instrument-cache lookup function. Loaded once at mount, kept as
  // module-scope so buildUnified() can parse tradingsymbols
  // synchronously (kind, strike, opt_type) for the group/sort logic.
  let getInstrument = $state(/** @type {((s: string) => any) | null} */ (null));

  onMount(async () => {
    if (!$authStore.user) { goto('/signin'); return; }
    // Warm the instruments cache + capture the sync lookup so
    // buildUnified() can group rows by parsed underlying.
    try {
      const mod = await import('$lib/data/instruments');
      await mod.loadInstruments();
      getInstrument = mod.getInstrument;
    } catch (_) { /* cache cold — group/sort falls back to alphabetical */ }
    await loadLists();
    if (activeId != null) {
      await loadActive(activeId);
    }
    await loadPulse();
    await tick();
    mountGrid();
    stopPoll      = visibleInterval(async () => { await loadQuotes(); refreshGrid(); }, 5000);
    stopPulsePoll = visibleInterval(async () => { await loadPulse(); refreshGrid(); }, 10000);
  });

  onDestroy(() => { stopPoll?.(); stopPulsePoll?.(); grid?.destroy?.(); });

  async function loadLists() {
    try {
      lists = await fetchWatchlists();
      if (!lists.length) return;
      const def = lists.find(l => l.is_default) ?? lists[0];
      if (activeId == null) activeId = def.id;
    } catch (e) { error = e.message; }
  }

  async function loadActive(/** @type {number} */ id) {
    error = '';
    try {
      active = await fetchWatchlist(id);
      await loadQuotes();
    } catch (e) { error = e.message; }
  }

  async function loadQuotes() {
    if (activeId == null) return;
    try {
      const r = await fetchWatchlistQuotes(activeId);
      const map = /** @type {Record<number, any>} */ ({});
      for (const q of (r?.items || [])) map[q.item_id] = q;
      watchQuotes = map;
      refreshedAt = r?.refreshed_at || '';
    } catch (e) {
      error = `Quote refresh: ${e.message}`;
    }
  }

  async function loadPulse() {
    try {
      const [p, h] = await Promise.all([
        fetchPositions().catch(() => ({ rows: [] })),
        fetchHoldings().catch(() => ({ rows: [] })),
      ]);
      positions = (p?.rows || []).slice();
      holdings  = (h?.rows || []).slice();
      // Derive underlyings (for option positions only).
      const underlyings = new Set();
      try {
        const { loadInstruments, getInstrument } = await import('$lib/data/instruments');
        await loadInstruments();
        for (const r of positions) {
          const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
          const inst = getInstrument(sym);
          if (inst && inst.u && String(inst.t || '').toUpperCase() !== 'EQ') {
            underlyings.add(inst.u);
          }
        }
      } catch (_) { /* instruments cold */ }
      if (underlyings.size) {
        const keys = [...underlyings].map(u => `NSE:${u}`);
        try {
          const r = await batchQuote(keys);
          const map = {};
          for (const q of (r?.items || [])) map[q.tradingsymbol] = q;
          underlyingQuotes = map;
        } catch (_) { underlyingQuotes = {}; }
      } else {
        underlyingQuotes = {};
      }
    } catch (_) { /* nothing fatal */ }
  }

  /** Build the deduped unified row set. Key = `${exchange}:${tradingsymbol}`.
   *  Each merged row carries `src` flags W/H/P/U plus per-source data. */
  const unifiedRows = $derived(buildUnified(
    active, watchQuotes, positions, holdings, underlyingQuotes, getInstrument
  ));

  /** Best-effort tradingsymbol parser using the IndexedDB instrument
   *  cache. Returns { underlying, kind, strike, opt_type } when the
   *  cache has the symbol, falls back to nulls. Safe to call before
   *  the cache loads (just returns nulls). */
  function parseSymbol(/** @type {string} */ sym, /** @type {any} */ instCache) {
    if (!instCache) return { underlying: null, kind: null, strike: null, opt_type: null };
    const inst = instCache(sym);
    if (!inst) return { underlying: null, kind: null, strike: null, opt_type: null };
    const t = String(inst.t || '').toUpperCase();
    const k = inst.k != null ? Number(inst.k) : null;
    let optType = null;
    if (t === 'CE' || t === 'PE') optType = t;
    else if (/CE$/i.test(sym)) optType = 'CE';
    else if (/PE$/i.test(sym)) optType = 'PE';
    const kind = optType ? 'opt' : (t === 'FUT' ? 'fut' : (t === 'EQ' ? 'eq' : null));
    return { underlying: inst.u || null, kind, strike: k, opt_type: optType };
  }

  function buildUnified(activeList, wq, pos, hold, uq, getInst) {
    const byKey = /** @type {Record<string, any>} */ ({});
    const get = (key) => byKey[key] || (byKey[key] = { key, src: { w:false, h:false, p:false, u:false } });

    function fill(row, sym) {
      const p = parseSymbol(sym, getInst);
      // Don't overwrite parse fields already set by another source.
      if (row.underlying == null) row.underlying = p.underlying;
      if (row.kind       == null) row.kind       = p.kind;
      if (row.strike     == null) row.strike     = p.strike;
      if (row.opt_type   == null) row.opt_type   = p.opt_type;
    }

    // 1. Watchlist (active list)
    for (const it of (activeList?.items || [])) {
      const q = wq[it.id];
      const sym = q?.quote_symbol || it.tradingsymbol;
      const key = `${it.exchange}:${sym}`;
      const row = get(key);
      row.exchange      = it.exchange;
      row.tradingsymbol = sym;
      row.alias         = (q?.quote_symbol && q.quote_symbol !== it.tradingsymbol) ? it.tradingsymbol : null;
      row.watchlist_item_id = it.id;
      row.src.w  = true;
      row.ltp    = q?.ltp    ?? row.ltp    ?? null;
      row.bid    = q?.bid    ?? row.bid    ?? null;
      row.ask    = q?.ask    ?? row.ask    ?? null;
      row.change = q?.change ?? row.change ?? null;
      row.change_pct = q?.change_pct ?? row.change_pct ?? null;
      fill(row, sym);
    }

    // 2. Positions
    for (const r of pos) {
      const exch = r.exchange || 'NFO';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const key = `${exch}:${sym}`;
      const row = get(key);
      row.exchange      = exch;
      row.tradingsymbol = sym;
      row.src.p = true;
      row.ltp = r.last_price ?? row.ltp ?? null;
      row.qty_pos = Number(r.quantity) || 0;
      row.avg_pos = Number(r.average_price) || 0;
      row.pnl     = Number(r.pnl) || 0;
      fill(row, sym);
    }

    // 3. Holdings
    for (const r of hold) {
      const exch = r.exchange || 'NSE';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const key = `${exch}:${sym}`;
      const row = get(key);
      row.exchange      = exch;
      row.tradingsymbol = sym;
      row.src.h = true;
      row.ltp = r.last_price ?? row.ltp ?? null;
      row.qty_hold = Number(r.quantity) || 0;
      if (r.day_change != null)            row.change = Number(r.day_change);
      if (r.day_change_percentage != null) row.change_pct = Number(r.day_change_percentage);
      fill(row, sym);
    }

    // 4. Option underlyings — quoted on NSE. Mark them as the spot row
    // of their underlying group so the sort below pins them to the
    // top of the group.
    for (const [u, q] of Object.entries(uq)) {
      const key = `NSE:${u}`;
      const row = get(key);
      row.exchange      = 'NSE';
      row.tradingsymbol = u;
      row.src.u = true;
      row.underlying    = u;     // group key — same as the option's parsed underlying
      row.kind          = 'spot';
      row.ltp        = q.ltp        ?? row.ltp        ?? null;
      row.bid        = q.bid        ?? row.bid        ?? null;
      row.ask        = q.ask        ?? row.ask        ?? null;
      if (row.change == null)     row.change     = q.change ?? null;
      if (row.change_pct == null) row.change_pct = q.change_pct ?? null;
    }

    // 5. Indices already in the watchlist (e.g. "NIFTY 50", "NIFTY BANK")
    //    should ALSO be tagged as the spot for their option-chain
    //    underlying so the sort groups them with their derivatives.
    //    The Markets watchlist uses Kite's index keys with a space,
    //    while option underlyings parse to bare names — map between
    //    them so a watched "NIFTY 50" sits at the top of the NIFTY
    //    options block.
    const INDEX_TO_UNDERLYING = {
      'NIFTY 50':         'NIFTY',
      'NIFTY BANK':       'BANKNIFTY',
      'NIFTY FIN SERVICE':'FINNIFTY',
      'NIFTY MID SELECT': 'MIDCPNIFTY',
      'NIFTY NXT 50':     'NIFTYNXT50',
      'SENSEX':           'SENSEX',
    };
    for (const row of Object.values(byKey)) {
      const tag = INDEX_TO_UNDERLYING[String(row.tradingsymbol || '').toUpperCase()];
      if (tag) {
        row.underlying = tag;
        row.kind       = 'spot';
      }
    }

    // ── Sort ──────────────────────────────────────────────────────
    //
    // Group every row under its underlying. Each group is laid out as:
    //   1. The spot row (underlying.kind === 'spot') first.
    //   2. Futures next, sorted by expiry.
    //   3. Options next, sorted by strike ASC, then CE before PE.
    //   4. Anything else (equity holdings of that name, …) last
    //      within the group.
    //
    // Groups themselves are ordered alphabetically by underlying.
    // Rows with no `underlying` parsed (cache miss / non-derivative
    // equities without a chain) fall into a single trailing group
    // sorted alphabetically by symbol.
    const out = Object.values(byKey);
    const groupKey = (r) => r.underlying || `~~${r.tradingsymbol || ''}`;
    const tierRank = (r) => {
      if (r.kind === 'spot')                 return 0;
      if (r.kind === 'fut')                  return 1;
      if (r.kind === 'opt')                  return 2;
      return 3;
    };
    const optTypeRank = (r) => (r.opt_type === 'CE' ? 0 : r.opt_type === 'PE' ? 1 : 2);
    out.sort((a, b) => {
      const ga = groupKey(a), gb = groupKey(b);
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

  // ── Add / remove ─────────────────────────────────────────────────

  async function searchSymbols(q) {
    if (!q || q.length < 2) { typeahead = []; return; }
    try {
      const { searchByPrefix } = await import('$lib/data/instruments');
      typeahead = await searchByPrefix(q.toUpperCase(), 12);
    } catch { typeahead = []; }
  }

  async function addRow() {
    if (!symInput.trim() || activeId == null) return;
    try {
      await addWatchlistItem(activeId, symInput.trim().toUpperCase(), exchInput);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      await loadActive(activeId);
      refreshGrid();
    } catch (e) { error = e.message; }
  }

  async function pickFromTypeahead(inst) {
    try {
      await addWatchlistItem(activeId, inst.s, inst.e);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      await loadActive(activeId);
      refreshGrid();
    } catch (e) { error = e.message; }
  }

  async function addToWatchlist(/** @type {string} */ tradingsymbol,
                                 /** @type {string} */ exchange) {
    if (activeId == null) return;
    try {
      await addWatchlistItem(activeId, tradingsymbol, exchange);
      await loadActive(activeId);
      refreshGrid();
    } catch (e) {
      if (!/already/i.test(e?.message || '')) error = e.message;
    }
  }

  async function removeFromWatchlist(/** @type {number} */ itemId) {
    if (activeId == null) return;
    try {
      await removeWatchlistItem(activeId, itemId);
      await loadActive(activeId);
      refreshGrid();
    } catch (e) { error = e.message; }
  }

  function pickList(/** @type {number} */ id) {
    activeId = id;
    loadActive(id).then(refreshGrid);
  }

  async function makeList() {
    if (!newListName.trim()) return;
    try {
      const w = await createWatchlist(newListName.trim());
      newListName = ''; showCreate = false;
      await loadLists();
      pickList(w.id);
    } catch (e) { error = e.message; }
  }

  async function dropList(/** @type {number} */ id) {
    if (!confirm('Delete this watchlist?')) return;
    try {
      await deleteWatchlist(id);
      if (activeId === id) activeId = null;
      await loadLists();
      if (activeId == null && lists.length) pickList(lists[0].id);
    } catch (e) { error = e.message; }
  }

  // ── Grid setup ────────────────────────────────────────────────────

  /** Source-badge renderer — W/H/P/U pills in the leftmost cell. */
  function srcRenderer(params) {
    const s = params.value || { w:false, h:false, p:false, u:false };
    const make = (letter, cls) =>
      `<span class="src-pill ${cls}">${letter}</span>`;
    const parts = [];
    if (s.w) parts.push(make('W', 'src-w'));
    if (s.h) parts.push(make('H', 'src-h'));
    if (s.p) parts.push(make('P', 'src-p'));
    if (s.u) parts.push(make('U', 'src-u'));
    return parts.join('');
  }

  function exchRenderer(params) {
    const v = String(params.value || '').toUpperCase();
    return `<span class="exch-chip exch-${v.toLowerCase()}">${v}</span>`;
  }

  function symRenderer(params) {
    const row = params.data || {};
    const alias = row.alias ? `<span class="sym-alias"> → ${row.tradingsymbol}</span>` : '';
    const main  = row.alias || row.tradingsymbol || '';
    return `<span class="sym-main">${main}</span>${alias}`;
  }

  function dirCls(v) {
    if (v == null) return 'cell-flat';
    if (v > 0) return 'cell-pos';
    if (v < 0) return 'cell-neg';
    return 'cell-flat';
  }

  /** Action column — `+W` when not in watchlist, `×` to remove. */
  function actionRenderer(params) {
    const r = params.data;
    if (r.src.w) {
      return `<button class="grid-btn grid-btn-rm" data-action="rm" title="Remove from watchlist">×</button>`;
    }
    return `<button class="grid-btn grid-btn-add" data-action="add" title="Add to watchlist">+W</button>`;
  }

  function getRowClass(params) {
    const s = params.data?.src || {};
    // Row tint priority: position > holding > watchlist > underlying.
    if (s.p) return 'row-pos';
    if (s.h) return 'row-hold';
    if (s.w) return 'row-watch';
    if (s.u) return 'row-und';
    return '';
  }

  function mountGrid() {
    if (!gridEl) return;
    const colDefs = [
      { field: 'src', headerName: 'Src', width: 90,
        cellRenderer: srcRenderer, sortable: false },
      { field: 'exchange', headerName: 'Exch', width: 64,
        cellRenderer: exchRenderer, sortable: true },
      { field: 'tradingsymbol', headerName: 'Symbol', flex: 1.4,
        cellRenderer: symRenderer, sortable: true },
      { field: 'ltp', headerName: 'LTP', flex: 0.8,
        type: 'numericColumn',
        valueFormatter: (p) => p.value != null ? priceFmt(p.value) : '—' },
      { field: 'change', headerName: 'Day Δ ₹', flex: 0.8,
        type: 'numericColumn',
        cellClass: (p) => dirCls(p.value),
        valueFormatter: (p) => p.value == null ? '—' : (p.value > 0 ? '+' : '') + priceFmt(p.value) },
      { field: 'change_pct', headerName: 'Day Δ %', flex: 0.8,
        type: 'numericColumn',
        cellClass: (p) => dirCls(p.value),
        valueFormatter: (p) => p.value == null ? '—' : (p.value > 0 ? '+' : '') + Number(p.value).toFixed(2) + '%' },
      { field: 'bid', headerName: 'Bid', flex: 0.7,
        type: 'numericColumn', cellClass: 'cell-muted',
        valueFormatter: (p) => p.value != null ? priceFmt(p.value) : '—' },
      { field: 'ask', headerName: 'Ask', flex: 0.7,
        type: 'numericColumn', cellClass: 'cell-muted',
        valueFormatter: (p) => p.value != null ? priceFmt(p.value) : '—' },
      { field: 'qty_pos', headerName: 'Pos Qty', flex: 0.7,
        type: 'numericColumn', cellClass: 'cell-muted',
        valueFormatter: (p) => p.value ? qtyFmt(p.value) : '—' },
      { field: 'qty_hold', headerName: 'Hold Qty', flex: 0.7,
        type: 'numericColumn', cellClass: 'cell-muted',
        valueFormatter: (p) => p.value ? qtyFmt(p.value) : '—' },
      { field: 'pnl', headerName: 'P&L', flex: 0.9,
        type: 'numericColumn',
        cellClass: (p) => dirCls(p.value),
        valueFormatter: (p) => p.value ? aggFmt(p.value) : '—' },
      { field: 'actions', headerName: '', width: 64,
        cellRenderer: actionRenderer, sortable: false,
        onCellClicked: handleActionClick },
    ];

    grid = createGrid(gridEl, {
      theme: 'legacy',
      columnDefs: colDefs,
      rowData: unifiedRows,
      defaultColDef: {
        resizable: true, sortable: true, suppressMovable: true,
        cellStyle: { display: 'flex', alignItems: 'center' },
      },
      overlayNoRowsTemplate: '<span style="font-size:0.65rem;color:#7e97b8">No rows — add symbols to your watchlist or load positions/holdings</span>',
      domLayout: 'autoHeight',
      getRowClass,
      rowHeight: 28,
      headerHeight: 28,
    });
  }

  function refreshGrid() {
    if (!grid) return;
    grid.setGridOption('rowData', unifiedRows);
  }

  function handleActionClick(ev) {
    const btn = ev.event?.target?.closest?.('button.grid-btn');
    if (!btn) return;
    const action = btn.getAttribute('data-action');
    const r = ev.data;
    if (action === 'add') addToWatchlist(r.tradingsymbol, r.exchange);
    else if (action === 'rm' && r.watchlist_item_id) removeFromWatchlist(r.watchlist_item_id);
  }
</script>

<div class="algo-status-card p-5 pt-4" data-status="inactive">
  <div class="flex items-center justify-between mb-1 gap-2 flex-wrap">
    <h1 class="text-sm font-bold uppercase tracking-wider text-[#fbbf24] mb-0">Watchlist</h1>
    <span class="algo-ts">{refreshedAt || clientTimestamp()}</span>
  </div>
  <div class="border-b border-[rgba(251,191,36,0.25)] mb-3"></div>

  {#if error}
    <div class="mb-2 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}

  <!-- Tab strip + new/delete -->
  <div class="flex flex-wrap items-center gap-1 mb-2">
    {#each lists as l}
      <button onclick={() => pickList(l.id)}
        class="px-3 py-1 text-[0.65rem] font-semibold uppercase tracking-wider rounded
               border border-[rgba(251,191,36,0.3)]
               {activeId === l.id ? 'bg-[#fbbf24]/20 text-[#fbbf24]' : 'bg-transparent text-[#c8d8f0]/70 hover:bg-[#fbbf24]/10'}">
        {l.name}
        <span class="ml-1 text-[0.55rem] text-[#7e97b8]">({l.item_count})</span>
        {#if l.is_default}<span class="ml-1 text-[0.5rem] text-[#4ade80]">★</span>{/if}
      </button>
    {/each}
    <button onclick={() => showCreate = !showCreate}
      class="px-2 py-1 text-[0.65rem] font-bold text-[#fbbf24] border border-[#fbbf24]/40 rounded hover:bg-[#fbbf24]/10">
      + New
    </button>
    {#if active && !active.is_default && lists.length > 1}
      <button onclick={() => dropList(active.id)}
        class="px-2 py-1 text-[0.65rem] text-red-300 border border-red-400/40 rounded hover:bg-red-500/10">
        Delete list
      </button>
    {/if}
  </div>

  {#if showCreate}
    <div class="flex items-center gap-2 mb-2">
      <input bind:value={newListName} class="field-input flex-1" placeholder="New watchlist name"
        onkeydown={(e) => e.key === 'Enter' && makeList()} />
      <button onclick={makeList} class="btn-primary text-[0.65rem] py-1 px-3">Create</button>
      <button onclick={() => { showCreate = false; newListName = ''; }}
        class="btn-secondary text-[0.65rem] py-1 px-3">Cancel</button>
    </div>
  {/if}

  <!-- Add-symbol row -->
  <div class="flex items-center gap-2 mb-2 relative">
    <input bind:value={symInput}
      oninput={(e) => { searchSymbols(e.currentTarget.value); typeaheadOpen = true; }}
      onfocus={() => typeaheadOpen = true}
      class="field-input flex-1" placeholder="Add symbol to {active?.name ?? 'list'} — type 2+ chars" />
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

  <!-- Unified grid -->
  <div bind:this={gridEl} class="ag-theme-algo unified-grid"></div>
</div>

<style>
  /* Source pills — leftmost column letters. */
  :global(.src-pill) {
    display: inline-block;
    padding: 0 5px;
    margin-right: 2px;
    border-radius: 3px;
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    border: 1px solid;
    line-height: 14px;
    min-width: 14px;
    text-align: center;
  }
  :global(.src-w) { color: #fbbf24; border-color: rgba(251,191,36,0.6); background: rgba(251,191,36,0.12); }
  :global(.src-h) { color: #4ade80; border-color: rgba(74,222,128,0.6); background: rgba(74,222,128,0.12); }
  :global(.src-p) { color: #7dd3fc; border-color: rgba(125,211,252,0.6); background: rgba(125,211,252,0.12); }
  :global(.src-u) { color: #c4b5fd; border-color: rgba(196,181,253,0.6); background: rgba(196,181,253,0.12); }

  /* Exchange chip — same palette as the row's underlying market. */
  :global(.exch-chip) {
    display: inline-block;
    padding: 0 5px;
    border-radius: 3px;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    border: 1px solid;
    line-height: 14px;
  }
  :global(.exch-nse) { color: #7dd3fc; border-color: rgba(125,211,252,0.5); background: rgba(125,211,252,0.10); }
  :global(.exch-nfo) { color: #fbbf24; border-color: rgba(251,191,36,0.5);  background: rgba(251,191,36,0.10); }
  :global(.exch-bse) { color: #c4b5fd; border-color: rgba(196,181,253,0.5); background: rgba(196,181,253,0.10); }
  :global(.exch-mcx) { color: #fb923c; border-color: rgba(251,146,60,0.5);  background: rgba(251,146,60,0.10); }
  :global(.exch-cds) { color: #a78bfa; border-color: rgba(167,139,250,0.5); background: rgba(167,139,250,0.10); }

  /* Symbol cell — main + alias. */
  :global(.sym-main)  { color: #fbbf24; font-weight: 600; }
  :global(.sym-alias) { color: #7e97b8; font-size: 0.55rem; }

  /* Day Δ / P&L cells. */
  :global(.cell-pos)  { color: #4ade80; }
  :global(.cell-neg)  { color: #f87171; }
  :global(.cell-flat) { color: #7e97b8; }
  :global(.cell-muted){ color: rgba(200,216,240,0.6); }

  /* Row background tint by source. Same alpha (0.08) across all four
     so no row dominates; the Src pills are what tell you which is
     which. The colour identifies the row's primary classification. */
  :global(.unified-grid .ag-row.row-watch) { background: rgba(251,191,36,0.07) !important; }
  :global(.unified-grid .ag-row.row-hold)  { background: rgba(74,222,128,0.07) !important; }
  :global(.unified-grid .ag-row.row-pos)   { background: rgba(125,211,252,0.07) !important; }
  :global(.unified-grid .ag-row.row-und)   { background: rgba(196,181,253,0.06) !important; }

  /* Action column buttons. */
  :global(.grid-btn) {
    padding: 0 6px;
    font-size: 0.6rem;
    font-weight: 700;
    background: transparent;
    border: 1px solid;
    border-radius: 3px;
    cursor: pointer;
    line-height: 18px;
  }
  :global(.grid-btn-add) { color: #fbbf24; border-color: rgba(251,191,36,0.4); }
  :global(.grid-btn-add:hover) { background: rgba(251,191,36,0.10); }
  :global(.grid-btn-rm)  { color: #f87171; border-color: rgba(248,113,113,0.4); }
  :global(.grid-btn-rm:hover)  { background: rgba(248,113,113,0.10); }

  /* Grid container */
  .unified-grid {
    width: 100%;
    min-height: 60px;
  }
</style>
