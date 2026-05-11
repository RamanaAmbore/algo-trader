<script>
  // Watchlist page — algo dark theme. Tabs across the top for the user's
  // named watchlists; the active list's quotes refresh every 5 s. Add
  // symbol via typeahead from the IndexedDB instruments cache.

  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import {
    fetchWatchlists, fetchWatchlist, createWatchlist, renameWatchlist,
    deleteWatchlist, addWatchlistItem, removeWatchlistItem,
    fetchWatchlistQuotes,
    fetchPositions, fetchHoldings, batchQuote,
  } from '$lib/api';
  import { authStore, visibleInterval, clientTimestamp } from '$lib/stores';
  import { aggFmt, priceFmt, qtyFmt } from '$lib/format';

  let lists      = $state(/** @type {any[]} */ ([]));
  let activeId   = $state(/** @type {number | null} */ (null));
  let active     = $state(/** @type {any} */ (null));   // {id,name,items:[…]}
  let quotes     = $state(/** @type {Record<number, any>} */ ({}));
  let refreshedAt = $state('');
  let error      = $state('');
  let loading    = $state(false);

  // Add-symbol form state.
  let symInput   = $state('');
  let exchInput  = $state('NSE');
  let typeahead  = $state(/** @type {any[]} */ ([]));
  let typeaheadOpen = $state(false);

  // New-list form state.
  let newListName = $state('');
  let showCreate  = $state(false);

  // Market-pulse sections — pulled once on mount + refreshed every 10 s.
  let positions  = $state(/** @type {any[]} */ ([]));   // raw position rows
  let holdings   = $state(/** @type {any[]} */ ([]));   // raw holding rows
  // Live quotes for option underlyings (NIFTY 50 spot etc.) — derived
  // from positions where kind=opt, then batch-quoted.
  let underlyingQuotes = $state(/** @type {Record<string, any>} */ ({}));
  let pulseRefreshed   = $state('');

  let stopPoll;
  let stopPulsePoll;

  onMount(async () => {
    if (!$authStore.user) { goto('/signin'); return; }
    await loadLists();
    if (activeId != null) {
      await loadActive(activeId);
      stopPoll = visibleInterval(loadQuotes, 5000);
    }
    await loadPulse();
    stopPulsePoll = visibleInterval(loadPulse, 10000);
  });

  onDestroy(() => { stopPoll?.(); stopPulsePoll?.(); });

  // ── Market-pulse sections (positions / holdings / underlyings) ────

  async function loadPulse() {
    try {
      const [p, h] = await Promise.all([
        fetchPositions().catch(() => ({ rows: [] })),
        fetchHoldings().catch(() => ({ rows: [] })),
      ]);
      positions = (p?.rows || []).slice();
      holdings  = (h?.rows || []).slice();
      // Derive distinct option underlyings — try to map a contract
      // symbol to its underlying via the instrument cache. Skip rows
      // where the cache miss to avoid a guessed-wrong quote.
      const underlyings = new Set();
      try {
        const { loadInstruments, getInstrument } = await import('$lib/data/instruments');
        await loadInstruments();
        for (const r of positions) {
          const sym = String(r.symbol || r.tradingsymbol || '').toUpperCase();
          const inst = getInstrument(sym);
          if (inst && inst.u && /^(opt|fut)$/i.test(String(inst.t || '').slice(0, 3))) {
            underlyings.add(inst.u);
          }
        }
      } catch (_) { /* instruments cache cold — skip underlyings section */ }

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
      pulseRefreshed = new Date().toISOString().slice(0, 19);
    } catch (e) {
      // Non-fatal — the user's watchlist tab still works.
    }
  }

  // Distinct option underlyings (NIFTY / BANKNIFTY / RELIANCE / …) inferred
  // from positions for the Underlyings section. Sorted alphabetically.
  const underlyingList = $derived(Object.keys(underlyingQuotes).sort());

  /** Add a row from one of the non-watchlist sections to the default
   *  watchlist with one click. Uses the user's is_default list; falls
   *  back to "Default" by name. */
  async function addToWatchlist(/** @type {string} */ tradingsymbol,
                                 /** @type {string} */ exchange) {
    const def = (lists || []).find(l => l.is_default) || (lists || []).find(l => l.name === 'Default');
    if (!def) return;
    try {
      await addWatchlistItem(def.id, tradingsymbol, exchange);
      await loadLists();
      if (def.id === activeId) await loadActive(activeId);
    } catch (e) {
      if (!/already/i.test(e?.message || '')) error = e.message;
    }
  }

  /** Colour chip class per exchange — colour-coded markets so the
   *  operator can scan rows by venue at a glance. */
  function exchClass(/** @type {string} */ exch) {
    switch ((exch || '').toUpperCase()) {
      case 'NSE': return 'exch-nse';
      case 'NFO': return 'exch-nfo';
      case 'BSE': return 'exch-bse';
      case 'MCX': return 'exch-mcx';
      case 'CDS': return 'exch-cds';
      default:    return 'exch-other';
    }
  }

  async function loadLists() {
    try {
      lists = await fetchWatchlists();
      if (!lists.length) return;
      // Prefer the user's default list; fall back to first.
      const def = lists.find(l => l.is_default) ?? lists[0];
      if (activeId == null) activeId = def.id;
    } catch (e) { error = e.message; }
  }

  async function loadActive(/** @type {number} */ id) {
    loading = true; error = '';
    try {
      active = await fetchWatchlist(id);
      await loadQuotes();
    } catch (e) { error = e.message; } finally { loading = false; }
  }

  async function loadQuotes() {
    if (activeId == null) return;
    try {
      const r = await fetchWatchlistQuotes(activeId);
      const map = /** @type {Record<number, any>} */ ({});
      for (const q of (r?.items || [])) map[q.item_id] = q;
      quotes = map;
      refreshedAt = r?.refreshed_at || '';
    } catch (e) {
      // Don't blow up — broker may be off-hours / unreachable. Surface
      // a one-line muted error; the table renders stale.
      error = `Quote refresh: ${e.message}`;
    }
  }

  function pickList(/** @type {number} */ id) {
    activeId = id;
    loadActive(id);
  }

  // Typeahead — searches the client-side instrument cache.
  async function searchSymbols(q) {
    if (!q || q.length < 2) { typeahead = []; return; }
    try {
      const { searchByPrefix } = await import('$lib/data/instruments');
      const rows = await searchByPrefix(q.toUpperCase(), 12);
      typeahead = rows;
    } catch { typeahead = []; }
  }

  async function addRow() {
    if (!symInput.trim()) return;
    error = '';
    try {
      await addWatchlistItem(activeId, symInput.trim().toUpperCase(), exchInput);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      await loadActive(activeId);
    } catch (e) { error = e.message; }
  }

  async function pickFromTypeahead(/** @type {any} */ inst) {
    error = '';
    try {
      await addWatchlistItem(activeId, inst.s, inst.e);
      symInput = ''; typeahead = []; typeaheadOpen = false;
      await loadActive(activeId);
    } catch (e) { error = e.message; }
  }

  async function removeRow(/** @type {number} */ itemId) {
    error = '';
    try {
      await removeWatchlistItem(activeId, itemId);
      await loadActive(activeId);
    } catch (e) { error = e.message; }
  }

  async function makeList() {
    if (!newListName.trim()) return;
    error = '';
    try {
      const w = await createWatchlist(newListName.trim());
      newListName = ''; showCreate = false;
      await loadLists();
      pickList(w.id);
    } catch (e) { error = e.message; }
  }

  async function dropList(/** @type {number} */ id) {
    if (!confirm('Delete this watchlist?')) return;
    error = '';
    try {
      await deleteWatchlist(id);
      if (activeId === id) activeId = null;
      await loadLists();
      if (activeId == null && lists.length) pickList(lists[0].id);
    } catch (e) { error = e.message; }
  }
</script>

<div class="algo-status-card p-5 pt-4" data-status="inactive">
  <div class="flex items-center justify-between mb-1 gap-2 flex-wrap">
    <h1 class="text-sm font-bold uppercase tracking-wider text-[#fbbf24] mb-0">Watchlist</h1>
    <span class="algo-ts">{refreshedAt || clientTimestamp()}</span>
  </div>
  <div class="border-b border-[rgba(251,191,36,0.25)] mb-4"></div>

  {#if error}
    <div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-xs border border-red-500/40">{error}</div>
  {/if}

  <!-- Tab strip — one button per list. -->
  <div class="flex flex-wrap items-center gap-1 mb-3">
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
    <div class="flex items-center gap-2 mb-3">
      <input bind:value={newListName} class="field-input flex-1" placeholder="New watchlist name"
        onkeydown={(e) => e.key === 'Enter' && makeList()} />
      <button onclick={makeList} class="btn-primary text-[0.65rem] py-1 px-3">Create</button>
      <button onclick={() => { showCreate = false; newListName = ''; }}
        class="btn-secondary text-[0.65rem] py-1 px-3">Cancel</button>
    </div>
  {/if}

  <!-- Add-symbol row. -->
  <div class="flex items-center gap-2 mb-3 relative">
    <input bind:value={symInput}
      oninput={(e) => { searchSymbols(e.currentTarget.value); typeaheadOpen = true; }}
      onfocus={() => typeaheadOpen = true}
      class="field-input flex-1" placeholder="Add symbol — type 2+ chars" />
    <select bind:value={exchInput} class="field-input w-24">
      <option>NSE</option>
      <option>BSE</option>
      <option>NFO</option>
      <option>MCX</option>
      <option>CDS</option>
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

  <!-- Quote table. -->
  {#if !active || !active.items?.length}
    <p class="text-xs text-[#7e97b8] py-4">No symbols in this watchlist.</p>
  {:else}
    <div class="w-full overflow-x-auto">
      <table class="w-full text-xs">
        <thead class="text-[0.55rem] uppercase tracking-wider text-[#7e97b8] border-b border-[rgba(251,191,36,0.2)]">
          <tr>
            <th class="text-left py-1.5 px-2">Symbol</th>
            <th class="text-left py-1.5 px-2">Exch</th>
            <th class="text-right py-1.5 px-2">LTP</th>
            <th class="text-right py-1.5 px-2">Bid</th>
            <th class="text-right py-1.5 px-2">Ask</th>
            <th class="text-right py-1.5 px-2">Day Δ ₹</th>
            <th class="text-right py-1.5 px-2">Day Δ %</th>
            <th class="text-right py-1.5 px-2">Volume</th>
            <th class="text-right py-1.5 px-2"></th>
          </tr>
        </thead>
        <tbody class="font-mono">
          {#each active.items as it}
            {@const q = quotes[it.id]}
            {@const dir = q?.change > 0 ? 'pos' : q?.change < 0 ? 'neg' : 'flat'}
            <tr class="border-b border-[rgba(200,216,240,0.06)] hover:bg-[#fbbf24]/5">
              <td class="py-1 px-2">
                <span class="exch-chip {exchClass(it.exchange)}">{it.exchange}</span>
                <span class="text-[#fbbf24] ml-1">{it.tradingsymbol}</span>
                {#if q?.quote_symbol && q.quote_symbol !== it.tradingsymbol}<span class="text-[0.55rem] text-[#7e97b8] ml-1">→ {q.quote_symbol}</span>{/if}
              </td>
              <td class="py-1 px-2 text-[0.6rem] text-[#7e97b8]"></td>
              <td class="py-1 px-2 text-right tabular-nums">{q?.ltp ? priceFmt(q.ltp) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums text-[#c8d8f0]/70">{q?.bid ? priceFmt(q.bid) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums text-[#c8d8f0]/70">{q?.ask ? priceFmt(q.ask) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums {dir==='pos' ? 'text-[#4ade80]' : dir==='neg' ? 'text-[#f87171]' : 'text-[#7e97b8]'}">
                {q?.change != null ? (q.change > 0 ? '+' : '') + priceFmt(q.change) : '—'}
              </td>
              <td class="py-1 px-2 text-right tabular-nums {dir==='pos' ? 'text-[#4ade80]' : dir==='neg' ? 'text-[#f87171]' : 'text-[#7e97b8]'}">
                {q?.change_pct != null ? (q.change_pct > 0 ? '+' : '') + q.change_pct.toFixed(2) + '%' : '—'}
              </td>
              <td class="py-1 px-2 text-right tabular-nums text-[#c8d8f0]/60">{q?.volume ? qtyFmt(q.volume) : '—'}</td>
              <td class="py-1 px-2 text-right">
                <button onclick={() => removeRow(it.id)}
                  class="text-[0.7rem] text-[#7e97b8] hover:text-red-300" title="Remove">×</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="text-[0.55rem] text-[#7e97b8] mt-2">
      Auto-refresh every 5 s · {active.items.length} symbol{active.items.length === 1 ? '' : 's'}
    </p>
  {/if}

  <!-- ── Underlyings section ── -->
  {#if underlyingList.length}
    <h2 class="pulse-section-title">📈 Option Underlyings <span class="text-[0.55rem] text-[#7e97b8] ml-2">{underlyingList.length}</span></h2>
    <div class="w-full overflow-x-auto">
      <table class="w-full text-xs">
        <thead class="text-[0.55rem] uppercase tracking-wider text-[#7e97b8] border-b border-[rgba(251,191,36,0.2)]">
          <tr>
            <th class="text-left py-1 px-2">Underlying</th>
            <th class="text-right py-1 px-2">LTP</th>
            <th class="text-right py-1 px-2">Day Δ ₹</th>
            <th class="text-right py-1 px-2">Day Δ %</th>
            <th class="text-right py-1 px-2"></th>
          </tr>
        </thead>
        <tbody class="font-mono">
          {#each underlyingList as u}
            {@const q = underlyingQuotes[u]}
            {@const dir = q?.change > 0 ? 'pos' : q?.change < 0 ? 'neg' : 'flat'}
            <tr class="border-b border-[rgba(200,216,240,0.06)] hover:bg-[#fbbf24]/5">
              <td class="py-1 px-2"><span class="exch-chip exch-nse">NSE</span> <span class="text-[#fbbf24] ml-1">{u}</span></td>
              <td class="py-1 px-2 text-right tabular-nums">{q?.ltp ? priceFmt(q.ltp) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums {dir==='pos' ? 'text-[#4ade80]' : dir==='neg' ? 'text-[#f87171]' : 'text-[#7e97b8]'}">{q?.change != null ? (q.change > 0 ? '+' : '') + priceFmt(q.change) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums {dir==='pos' ? 'text-[#4ade80]' : dir==='neg' ? 'text-[#f87171]' : 'text-[#7e97b8]'}">{q?.change_pct != null ? (q.change_pct > 0 ? '+' : '') + q.change_pct.toFixed(2) + '%' : '—'}</td>
              <td class="py-1 px-2 text-right">
                <button class="exch-add" onclick={() => addToWatchlist(u, 'NSE')} title="Add to default watchlist">+W</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <!-- ── Positions section ── -->
  {#if positions.length}
    <h2 class="pulse-section-title">📊 Positions <span class="text-[0.55rem] text-[#7e97b8] ml-2">{positions.length}</span></h2>
    <div class="w-full overflow-x-auto">
      <table class="w-full text-xs">
        <thead class="text-[0.55rem] uppercase tracking-wider text-[#7e97b8] border-b border-[rgba(251,191,36,0.2)]">
          <tr>
            <th class="text-left py-1 px-2">Symbol</th>
            <th class="text-left py-1 px-2">Account</th>
            <th class="text-right py-1 px-2">Qty</th>
            <th class="text-right py-1 px-2">LTP</th>
            <th class="text-right py-1 px-2">Avg</th>
            <th class="text-right py-1 px-2">P&L</th>
            <th class="text-right py-1 px-2"></th>
          </tr>
        </thead>
        <tbody class="font-mono">
          {#each positions as r}
            {@const exch = r.exchange || 'NFO'}
            {@const pnl = Number(r.pnl) || 0}
            {@const dir = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : 'flat'}
            <tr class="border-b border-[rgba(200,216,240,0.06)] hover:bg-[#fbbf24]/5">
              <td class="py-1 px-2"><span class="exch-chip {exchClass(exch)}">{exch}</span> <span class="text-[#fbbf24] ml-1">{r.symbol || r.tradingsymbol}</span></td>
              <td class="py-1 px-2 text-[0.6rem] text-[#c8d8f0]/70">{r.account}</td>
              <td class="py-1 px-2 text-right tabular-nums">{qtyFmt(r.quantity || 0)}</td>
              <td class="py-1 px-2 text-right tabular-nums">{r.last_price ? priceFmt(r.last_price) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums text-[#c8d8f0]/60">{r.average_price ? priceFmt(r.average_price) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums {dir==='pos' ? 'text-[#4ade80]' : dir==='neg' ? 'text-[#f87171]' : 'text-[#7e97b8]'}">{aggFmt(pnl)}</td>
              <td class="py-1 px-2 text-right">
                <button class="exch-add" onclick={() => addToWatchlist(r.symbol || r.tradingsymbol, exch)} title="Add to default watchlist">+W</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  <!-- ── Holdings section ── -->
  {#if holdings.length}
    <h2 class="pulse-section-title">💼 Holdings <span class="text-[0.55rem] text-[#7e97b8] ml-2">{holdings.length}</span></h2>
    <div class="w-full overflow-x-auto">
      <table class="w-full text-xs">
        <thead class="text-[0.55rem] uppercase tracking-wider text-[#7e97b8] border-b border-[rgba(251,191,36,0.2)]">
          <tr>
            <th class="text-left py-1 px-2">Symbol</th>
            <th class="text-left py-1 px-2">Account</th>
            <th class="text-right py-1 px-2">Qty</th>
            <th class="text-right py-1 px-2">LTP</th>
            <th class="text-right py-1 px-2">Day Δ ₹</th>
            <th class="text-right py-1 px-2">Day Δ %</th>
            <th class="text-right py-1 px-2"></th>
          </tr>
        </thead>
        <tbody class="font-mono">
          {#each holdings as r}
            {@const exch = r.exchange || 'NSE'}
            {@const chg = Number(r.day_change) || 0}
            {@const chgPct = Number(r.day_change_percentage) || 0}
            {@const dir = chg > 0 ? 'pos' : chg < 0 ? 'neg' : 'flat'}
            <tr class="border-b border-[rgba(200,216,240,0.06)] hover:bg-[#fbbf24]/5">
              <td class="py-1 px-2"><span class="exch-chip {exchClass(exch)}">{exch}</span> <span class="text-[#fbbf24] ml-1">{r.symbol || r.tradingsymbol}</span></td>
              <td class="py-1 px-2 text-[0.6rem] text-[#c8d8f0]/70">{r.account}</td>
              <td class="py-1 px-2 text-right tabular-nums">{qtyFmt(r.quantity || 0)}</td>
              <td class="py-1 px-2 text-right tabular-nums">{r.last_price ? priceFmt(r.last_price) : '—'}</td>
              <td class="py-1 px-2 text-right tabular-nums {dir==='pos' ? 'text-[#4ade80]' : dir==='neg' ? 'text-[#f87171]' : 'text-[#7e97b8]'}">{aggFmt(chg)}</td>
              <td class="py-1 px-2 text-right tabular-nums {dir==='pos' ? 'text-[#4ade80]' : dir==='neg' ? 'text-[#f87171]' : 'text-[#7e97b8]'}">{(chgPct > 0 ? '+' : '') + chgPct.toFixed(2)}%</td>
              <td class="py-1 px-2 text-right">
                <button class="exch-add" onclick={() => addToWatchlist(r.symbol || r.tradingsymbol, exch)} title="Add to default watchlist">+W</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if pulseRefreshed}
    <p class="text-[0.55rem] text-[#7e97b8] mt-4">Sections auto-refresh every 10 s · last update {pulseRefreshed}</p>
  {/if}
</div>

<style>
  .pulse-section-title {
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #c8d8f0;
    margin: 1.5rem 0 0.5rem;
    padding-bottom: 0.25rem;
    border-bottom: 1px solid rgba(251,191,36,0.2);
  }
  .exch-chip {
    display: inline-block;
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    border: 1px solid;
  }
  .exch-nse  { color: #7dd3fc; border-color: rgba(125,211,252,0.4); background: rgba(125,211,252,0.08); }
  .exch-nfo  { color: #fbbf24; border-color: rgba(251,191,36,0.4);  background: rgba(251,191,36,0.08); }
  .exch-bse  { color: #c4b5fd; border-color: rgba(196,181,253,0.4); background: rgba(196,181,253,0.08); }
  .exch-mcx  { color: #fb923c; border-color: rgba(251,146,60,0.4);  background: rgba(251,146,60,0.08); }
  .exch-cds  { color: #a78bfa; border-color: rgba(167,139,250,0.4); background: rgba(167,139,250,0.08); }
  .exch-other{ color: #7e97b8; border-color: rgba(126,151,184,0.4); background: rgba(126,151,184,0.08); }
  .exch-add {
    padding: 1px 6px;
    font-size: 0.55rem;
    font-weight: 700;
    color: #fbbf24;
    background: transparent;
    border: 1px solid rgba(251,191,36,0.3);
    border-radius: 3px;
    cursor: pointer;
  }
  .exch-add:hover { background: rgba(251,191,36,0.1); }
</style>
