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

  let stopPoll;

  onMount(async () => {
    if (!$authStore.user) { goto('/signin'); return; }
    await loadLists();
    if (activeId != null) {
      await loadActive(activeId);
      stopPoll = visibleInterval(loadQuotes, 5000);
    }
  });

  onDestroy(() => stopPoll?.());

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
              <td class="py-1 px-2 text-[#fbbf24]">{it.tradingsymbol}{#if q?.quote_symbol && q.quote_symbol !== it.tradingsymbol}<span class="text-[0.55rem] text-[#7e97b8] ml-1">→{q.quote_symbol}</span>{/if}</td>
              <td class="py-1 px-2 text-[0.6rem] text-[#7e97b8]">{it.exchange}</td>
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
</div>
