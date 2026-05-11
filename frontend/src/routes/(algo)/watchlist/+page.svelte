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
    fetchPositions, fetchHoldings, fetchAccounts, batchQuote,
  } from '$lib/api';
  import { authStore, visibleInterval, clientTimestamp } from '$lib/stores';
  import { aggFmt, priceFmt, qtyFmt } from '$lib/format';
  import OrderEntryShell from '$lib/order/OrderEntryShell.svelte';

  ModuleRegistry.registerModules([AllCommunityModule]);

  let lists       = $state(/** @type {any[]} */ ([]));
  let activeId    = $state(/** @type {number | null} */ (null));
  let active      = $state(/** @type {any} */ (null));
  let watchQuotes = $state(/** @type {Record<number, any>} */ ({}));
  let positions   = $state(/** @type {any[]} */ ([]));
  let holdings    = $state(/** @type {any[]} */ ([]));
  let underlyingQuotes = $state(/** @type {Record<string, any>} */ ({}));
  // Contract-level live quotes (option / future positions) — keyed
  // by `${exchange}:${tradingsymbol}` so the row merge step below
  // can pick them up in O(1). Refreshed every 10 s via the chunked
  // batch-quote path.
  let contractQuotes   = $state(/** @type {Record<string, any>} */ ({}));
  // Wall-clock of the last successful pulse refresh. Drives the
  // "Last updated Xs ago" indicator so stalls are visible.
  let pulseLastUpdate  = $state(/** @type {number | null} */ (null));
  let agoTick          = $state(0);                       // forces ago string rerender
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
    if (!$authStore.user) { goto('/signin'); return; }
    // Warm the instruments cache + capture the sync lookup so
    // buildUnified() can group rows by parsed underlying.
    try {
      const mod = await import('$lib/data/instruments');
      await mod.loadInstruments();
      getInstrument = mod.getInstrument;
    } catch (_) { /* cache cold — group/sort falls back to alphabetical */ }
    // Real broker accounts for the OrderTicket — same fetch the
    // /admin/options page does so the ticket picks the right Kite
    // handle to route through.
    try {
      const r = await fetchAccounts();
      realAccounts = (r?.accounts || [])
        .map(/** @param {any} a */ (a) => String(a?.account_id || ''))
        .filter(Boolean);
    } catch (_) { realAccounts = []; }
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
      // Successful fetch — clear streak + any lingering banner.
      if (quoteFailStreak > 0) {
        quoteFailStreak = 0;
        if (/Quote refresh/.test(error)) error = '';
      }
    } catch (e) {
      // Transient broker hiccups happen — only paint the banner red
      // after 3 consecutive failures so a single blip doesn't alarm.
      quoteFailStreak++;
      if (quoteFailStreak >= 3) {
        error = `Quote refresh: ${e.message}`;
      }
    }
  }

  /** Chunked batch-quote — splits keys into ~50-sized chunks so Kite's
   *  /quote endpoint doesn't time out on huge baskets, and runs them
   *  in parallel for throughput. Wraps each call in a 5 s timeout so
   *  a slow Kite round-trip doesn't stall the next 10 s tick. */
  async function batchQuoteChunked(keys) {
    if (!keys || !keys.length) return [];
    const CHUNK = 50;
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

  async function loadPulse() {
    try {
      const [p, h] = await Promise.all([
        fetchPositions().catch(() => ({ rows: [] })),
        fetchHoldings().catch(() => ({ rows: [] })),
      ]);
      positions = (p?.rows || []).slice();
      holdings  = (h?.rows || []).slice();
      // Derive underlyings (for option positions only) PLUS every
      // option/future tradingsymbol — quote them all in one chunked
      // batch so the grid LTPs are live every 10 s, not stuck on the
      // 5-min /api/positions cache.
      const underlyings = new Set();
      const contractKeys = new Set();
      try {
        const { loadInstruments, getInstrument } = await import('$lib/data/instruments');
        await loadInstruments();
        for (const r of positions) {
          const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
          const exch = r.exchange || 'NFO';
          const inst = getInstrument(sym);
          if (inst && inst.u && String(inst.t || '').toUpperCase() !== 'EQ') {
            underlyings.add(inst.u);
          }
          if (sym) contractKeys.add(`${exch}:${sym}`);
        }
      } catch (_) { /* instruments cold */ }

      const allKeys = [
        ...[...underlyings].map(u => `NSE:${u}`),
        ...contractKeys,
      ];
      if (allKeys.length) {
        const items = await batchQuoteChunked(allKeys);
        const uMap = {};
        const cMap = {};
        for (const q of items) {
          // The underlying-quote path stores by tradingsymbol (NIFTY etc.);
          // contract quotes store by `EXCHANGE:SYMBOL` for fast position
          // merge below.
          if (underlyings.has(q.tradingsymbol)) uMap[q.tradingsymbol] = q;
          cMap[`${q.exchange}:${q.tradingsymbol}`] = q;
        }
        underlyingQuotes = uMap;
        contractQuotes  = cMap;
      } else {
        underlyingQuotes = {};
        contractQuotes  = {};
      }
      pulseLastUpdate = Date.now();
    } catch (_) { /* nothing fatal */ }
  }

  /** Build the deduped unified row set. Key = `${exchange}:${tradingsymbol}`.
   *  Each merged row carries `src` flags W/H/P/U plus per-source data. */
  const unifiedRows = $derived(buildUnified(
    active, watchQuotes, positions, holdings, underlyingQuotes, contractQuotes, getInstrument
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

  function buildUnified(activeList, wq, pos, hold, uq, cq, getInst) {
    // Key on tradingsymbol alone (case-normalised). If the same
    // symbol exists in multiple accounts or across exchanges, we
    // collapse to one row with net qty + summed P&L — the operator
    // sees one canonical row per instrument.
    const byKey = /** @type {Record<string, any>} */ ({});
    const get = (key) => byKey[key] || (byKey[key] = {
      key,
      src: { w:false, h:false, p:false, u:false },
      // Accumulators initialised to 0 so the += in the position /
      // holding loops below works without an explicit null guard.
      qty_pos: 0, qty_hold: 0, pnl: 0,
      // Weighted-avg numerator for avg_pos (sum of qty × avg);
      // divided by total qty at render time.
      _avg_num: 0,
    });

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
      const sym = String(q?.quote_symbol || it.tradingsymbol).toUpperCase();
      const row = get(sym);
      row.exchange      = row.exchange || it.exchange;
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

    // 2. Positions — same symbol across multiple accounts accumulates
    // into a single row with net qty + weighted-average avg_price.
    for (const r of pos) {
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
      // Live contract quote wins over the /api/positions snapshot
      // (cached server-side for 5 min). Set LTP once — same symbol
      // shares the same live price across accounts.
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
      row.pnl += Number(r.pnl) || 0;
      fill(row, sym);
    }

    // 3. Holdings — same accumulation rule as positions.
    for (const r of hold) {
      const exch = r.exchange || 'NSE';
      const sym  = String(r.symbol || r.tradingsymbol || '').toUpperCase();
      if (!sym) continue;
      const row = get(sym);
      row.exchange      = row.exchange || exch;
      row.tradingsymbol = sym;
      row.src.h = true;
      row.qty_hold += Number(r.quantity) || 0;
      // Live contract quote wins; otherwise fall back to the
      // holdings snapshot's last_price.
      const liveQ = cq?.[`${exch}:${sym}`];
      if (liveQ?.ltp) {
        row.ltp        = liveQ.ltp;
        row.change     = liveQ.change     ?? row.change     ?? null;
        row.change_pct = liveQ.change_pct ?? row.change_pct ?? null;
      } else {
        if (row.ltp == null) row.ltp = r.last_price ?? null;
        if (r.day_change != null && row.change == null)
          row.change = Number(r.day_change);
        if (r.day_change_percentage != null && row.change_pct == null)
          row.change_pct = Number(r.day_change_percentage);
      }
      fill(row, sym);
    }

    // 4. Option underlyings — quoted on NSE. Mark them as the spot row
    // of their underlying group so the sort below pins them to the
    // top of the group.
    for (const [u, q] of Object.entries(uq)) {
      const row = get(u);
      row.exchange      = row.exchange || 'NSE';
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

    // Finalise per-row aggregates: weighted-avg avg_pos from the
    // sum-of-(qty × avg) accumulator. qty_pos === 0 keeps avg at 0.
    for (const row of Object.values(byKey)) {
      if (row.qty_pos !== 0) {
        row.avg_pos = row._avg_num / row.qty_pos;
      }
      delete row._avg_num;
    }

    // ── Sort ──────────────────────────────────────────────────────
    //
    // Two-level ordering:
    //
    //   Level 1 — which UNDERLYING-GROUP comes first?
    //     groupBucket(g) =
    //       0 if any row in the group has a position or holding
    //         (operator's active book — most urgent)
    //       1 if any row in the group is on the watchlist
    //       2 otherwise (option-underlying-only / equity passthroughs)
    //     Ties broken alphabetically by underlying name.
    //
    //   Level 2 — within a group:
    //     spot row first → futures → options (strike ASC, CE then PE)
    //     → anything else (alphabetical fallback).
    //
    // Effect: positions and holdings cluster at the top alongside any
    // option underlyings / chain rows that share their group; pure
    // watchlist entries follow; bare option-underlying-only rows trail.
    const out = Object.values(byKey);
    const groupKey = (r) => r.underlying || `~~${r.tradingsymbol || ''}`;
    const tierRank = (r) => {
      if (r.kind === 'spot')                 return 0;
      if (r.kind === 'fut')                  return 1;
      if (r.kind === 'opt')                  return 2;
      return 3;
    };
    const optTypeRank = (r) => (r.opt_type === 'CE' ? 0 : r.opt_type === 'PE' ? 1 : 2);

    // Precompute per-group bucket so the comparator stays O(1).
    const groupBucket = /** @type {Record<string, number>} */ ({});
    for (const r of out) {
      const g = String(groupKey(r));
      const bucket = (r.src?.p || r.src?.h) ? 0 : r.src?.w ? 1 : 2;
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

  function exchRenderer(params) {
    const v = String(params.value || '').toUpperCase();
    return `<span class="exch-chip exch-${v.toLowerCase()}">${v}</span>`;
  }

  function symRenderer(params) {
    const row = params.data || {};
    const alias = row.alias ? `<span class="sym-alias"> → ${row.tradingsymbol}</span>` : '';
    const main  = row.alias || row.tradingsymbol || '';
    // Source badges: P / H show qty inline; W / U are flags only.
    // Ordered position → holding → watchlist → underlying so the
    // most operational source reads first.
    const badges = [];
    if (row.src?.p) {
      const q = Number(row.qty_pos) || 0;
      const qStr = q ? ` ${qtyFmt(q)}` : '';
      badges.push(`<span class="sym-badge badge-p" title="Position">P${qStr}</span>`);
    }
    if (row.src?.h) {
      const q = Number(row.qty_hold) || 0;
      const qStr = q ? ` ${qtyFmt(q)}` : '';
      badges.push(`<span class="sym-badge badge-h" title="Holding">H${qStr}</span>`);
    }
    if (row.src?.w) {
      badges.push(`<span class="sym-badge badge-w" title="Watchlist">W</span>`);
    }
    if (row.src?.u) {
      badges.push(`<span class="sym-badge badge-u" title="Option underlying">U</span>`);
    }
    const badgeHtml = badges.length ? ` <span class="sym-badges">${badges.join('')}</span>` : '';
    return `<span class="sym-main">${main}</span>${alias}${badgeHtml}`;
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
    const r = params.data || {};
    const s = r.src || {};
    // Position rows: cyan-long / orange-short symbol stripe, same as
    // PerformancePage. Falls back to the source-priority tint for
    // non-position rows.
    if (s.p) {
      const q = Number(r.qty_pos) || 0;
      if (q < 0) return 'pos-short';
      if (q > 0) return 'pos-long';
      return 'row-pos';  // qty=0 — closed mid-day
    }
    if (s.h) return 'row-hold';
    if (s.w) return 'row-watch';
    if (s.u) return 'row-und';
    return '';
  }

  function mountGrid() {
    if (!gridEl) return;
    // Column widths fixed (no flex) — matches the /dashboard
    // PerformancePage approach so narrow viewports scroll horizontally
    // instead of squashing columns. Symbol pinned-left so the row
    // identity stays visible while the operator scrolls right through
    // the numerics. Source is conveyed by the row-class tint on the
    // symbol cell (cyan=position, green=holding, amber=watchlist,
    // violet=underlying) — no separate Src column needed.
    const colDefs = /** @type {any[]} */ ([
      // Symbol cell now also carries inline W/H/P/U source badges so
      // the Pos Qty / Hold Qty columns are gone — qty rides on the
      // P/H badge itself.
      { field: 'tradingsymbol', headerName: 'Symbol', width: 220, pinned: 'left',
        cellRenderer: symRenderer, sortable: true,
        cellClass: 'ag-col-sym ag-col-fill' },
      { field: 'ltp', headerName: 'LTP', width: 70,
        type: 'numericColumn',
        valueFormatter: (p) => p.value != null ? priceFmt(p.value) : '—' },
      { field: 'change', headerName: 'Day Δ ₹', width: 78,
        type: 'numericColumn',
        cellClass: (p) => dirCls(p.value),
        valueFormatter: (p) => p.value == null ? '—' : (p.value > 0 ? '+' : '') + priceFmt(p.value) },
      { field: 'change_pct', headerName: 'Day %', width: 56,
        type: 'numericColumn',
        cellClass: (p) => dirCls(p.value),
        valueFormatter: (p) => p.value == null ? '—' : (p.value > 0 ? '+' : '') + Number(p.value).toFixed(2) + '%' },
      { field: 'bid', headerName: 'Bid', width: 62,
        type: 'numericColumn', cellClass: 'cell-muted',
        valueFormatter: (p) => p.value != null ? priceFmt(p.value) : '—' },
      { field: 'ask', headerName: 'Ask', width: 62,
        type: 'numericColumn', cellClass: 'cell-muted',
        valueFormatter: (p) => p.value != null ? priceFmt(p.value) : '—' },
      { field: 'pnl', headerName: 'P&L', width: 78,
        type: 'numericColumn',
        cellClass: (p) => dirCls(p.value),
        valueFormatter: (p) => p.value ? aggFmt(p.value) : '—' },
      { field: 'actions', headerName: '', width: 44,
        cellRenderer: actionRenderer, sortable: false,
        onCellClicked: handleActionClick, pinned: 'right' },
    ]);

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
      onRowClicked: handleRowClick,
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

  /** Click handler for any non-action row cell — opens the
   *  OrderEntryShell pre-filled with the row's symbol so the
   *  operator can place an order without leaving /watchlist. */
  function handleRowClick(ev) {
    if (!ev.data) return;
    // Ignore clicks on the actions cell — those have their own
    // handler that mutates the watchlist.
    const target = ev.event?.target;
    if (target?.closest?.('button.grid-btn')) return;
    const r = ev.data;
    const inst = getInstrument?.(r.tradingsymbol);
    const lot = Number(inst?.ls || 1);
    // Pre-fill side based on the row's position direction if any —
    // long position → SELL (square off), short position → BUY (cover);
    // for non-position rows default to BUY.
    let side = 'BUY';
    if (r.src?.p && r.qty_pos < 0) side = 'BUY';
    else if (r.src?.p && r.qty_pos > 0) side = 'SELL';
    openTicket({
      symbol:   r.tradingsymbol,
      exchange: r.exchange,
      side,
      qty:      Math.abs(Number(r.qty_pos) || lot),
      lotSize:  lot,
      accounts: realAccounts.length ? realAccounts : [],
      account:  realAccounts[0] || '',
    });
  }

  function openTicket(p) { ticketProps = { defaultTab: 'ticket', ...p }; }
  function closeTicket() { ticketProps = null; }
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

<!-- Order-ticket modal — opens when the operator clicks any non-action
     row. Pre-filled with the row's symbol; operator picks side / qty /
     mode / price. DRAFT lands locally, PAPER / LIVE go through the
     existing ticket submit path. -->
{#if ticketProps}
  <OrderEntryShell
    {...ticketProps}
    onSubmit={() => { closeTicket(); }}
    onClose={closeTicket} />
{/if}

<style>
  /* Exchange tag — text-only, no chrome. Lets the row breathe. */
  :global(.exch-chip) {
    display: inline-block;
    font-size: 0.52rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  :global(.exch-nse) { color: #94a3b8; }
  :global(.exch-nfo) { color: #fbbf24; }
  :global(.exch-bse) { color: #c4b5fd; }
  :global(.exch-mcx) { color: #fb923c; }
  :global(.exch-cds) { color: #a78bfa; }

  /* Symbol cell — main + alias. */
  :global(.sym-main)  { color: #e2e8f0; font-weight: 600; }
  :global(.sym-alias) { color: #7e97b8; font-size: 0.55rem; }

  /* Source badges (P / H / W / U) — sit right of the symbol. Palette
     matches the row-tint indicator on the symbol cell: cyan position,
     green holding, amber watchlist, violet underlying. The badge
     carries the qty for P / H so the dropped Pos Qty / Hold Qty
     columns are accounted for inline. */
  :global(.sym-badges) {
    display: inline-flex;
    gap: 3px;
    margin-left: 6px;
    vertical-align: middle;
  }
  :global(.sym-badge) {
    display: inline-block;
    padding: 0 4px;
    font-size: 0.55rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    line-height: 14px;
    border-radius: 2px;
    border: 1px solid;
    font-variant-numeric: tabular-nums;
  }
  :global(.badge-p) { color: #38bdf8; border-color: rgba(56,189,248,0.45);  background: rgba(56,189,248,0.10); }
  :global(.badge-h) { color: #4ade80; border-color: rgba(74,222,128,0.45);  background: rgba(74,222,128,0.10); }
  :global(.badge-w) { color: #fbbf24; border-color: rgba(251,191,36,0.45);  background: rgba(251,191,36,0.10); }
  :global(.badge-u) { color: #c4b5fd; border-color: rgba(196,181,253,0.45); background: rgba(196,181,253,0.10); }

  /* Day Δ / P&L cells — same green/red as PerformancePage pnl-gain / pnl-loss. */
  :global(.cell-pos)  { color: #4ade80 !important; }
  :global(.cell-neg)  { color: #f87171 !important; }
  :global(.cell-flat) { color: #94a3b8 !important; }
  :global(.cell-muted){ color: rgba(200,216,240,0.55) !important; }

  /* Row tinting (pos-long / pos-short / row-hold / row-watch / row-und)
     is owned globally by ag-col-sym rules in app.css so the same
     PerformancePage idiom paints both /dashboard and this page. */

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
