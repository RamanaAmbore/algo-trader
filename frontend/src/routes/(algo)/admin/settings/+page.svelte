<script>
  // Admin Settings — DB-backed tunables grouped by category. Pairs with
  // backend/api/routes/settings.py and backend/shared/helpers/settings.py.
  // Seed list is the authoritative catalog of editable knobs; this page
  // renders it and writes back via PATCH.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, nowStamp } from '$lib/stores';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import { fetchSettings, updateSetting, resetSetting, fetchWatchlists,
           fetchHedgeProxies, createHedgeProxy, updateHedgeProxy, deleteHedgeProxy } from '$lib/api';
  import Select   from '$lib/Select.svelte';

  // Pinned-watchlist symbols feed the orders.default_symbol dropdown
  // (operator request: "When you update in settings it should show the
  // drop down from pinned symbols"). Loaded lazily on mount; falls back
  // to a free-text input when the fetch fails (anonymous demo, network).
  /** @type {Array<{value:string, label:string}>} */
  let _pinnedOptions = $state([]);
  async function _loadPinnedSymbols() {
    try {
      const lists = await fetchWatchlists();
      const arr = Array.isArray(lists) ? lists : (lists?.watchlists || []);
      /** @type {Set<string>} */
      const seen = new Set();
      /** @type {Array<{value:string, label:string}>} */
      const out = [];
      for (const wl of arr) {
        // Only pinned lists (Default + Markets) — operator-created
        // lists are excluded since they aren't necessarily "trading
        // candidates" the operator wants the modal to default to.
        if (!wl?.is_pinned) continue;
        for (const it of (wl.items || [])) {
          const sym = String(it?.tradingsymbol || '').trim();
          if (!sym || seen.has(sym)) continue;
          seen.add(sym);
          out.push({ value: sym, label: `${sym}${it.exchange ? ` · ${it.exchange}` : ''}` });
        }
      }
      _pinnedOptions = out;
    } catch (_) { _pinnedOptions = []; }
  }

  /** @type {Array<{id:number, category:string, key:string, value_type:string,
   *                value:string, default_value:string, description:string,
   *                schema:any, units:string|null, updated_at:string}>} */
  let settings    = $state([]);
  let loading     = $state(true);
  let error       = $state('');
  let note        = $state('');
  let dirty       = $state(/** @type {Record<string, string>} */({}));
  let filter      = $state('');

  // Render order: high-touch operator knobs first, vendor/infra knobs last.
  // Anything not in this list falls through to 'misc' and is appended.
  const CATEGORY_ORDER = ['execution', 'orders', 'alerts', 'algo', 'performance',
                          'simulator', 'notifications', 'logging', 'misc'];
  // Singleton categories (1-2 keys each) collapse into 'misc' so they don't
  // each get their own card.
  const CATEGORY_REMAP = /** @type {Record<string,string>} */ ({
    connections: 'misc', genai: 'misc', auth: 'misc',
  });

  async function load() {
    loading = true; error = '';
    try { settings = await fetchSettings(); }
    catch (e) { error = e.message; }
    finally   { loading = false; }
    loadProxies();
  }

  // ── Hedge proxy CRUD ───────────────────────────────────────────────
  /** @type {Array<{id:number,proxy_symbol:string,target_root:string,
   *                conversion_kind:string,static_factor:number|null,
   *                beta:number|null,correlation:number,kind:string,
   *                note:string|null,source:string,is_active:boolean}>} */
  let proxies = $state([]);
  let proxiesErr = $state('');
  let proxyForm = $state({
    proxy_symbol: '', target_root: '', conversion_kind: 'dynamic',
    static_factor: '', beta: '', correlation: '1.0', kind: 'units',
    note: '', is_active: true,
  });
  async function loadProxies() {
    proxiesErr = '';
    try { const r = await fetchHedgeProxies(); proxies = Array.isArray(r?.rows) ? r.rows : []; }
    catch (e) { proxiesErr = e?.message || 'fetch failed'; }
  }
  async function addProxy() {
    proxiesErr = '';
    try {
      const payload = {
        proxy_symbol: String(proxyForm.proxy_symbol || '').trim().toUpperCase(),
        target_root:  String(proxyForm.target_root  || '').trim().toUpperCase(),
        conversion_kind: proxyForm.conversion_kind,
        static_factor: proxyForm.static_factor === '' ? null : Number(proxyForm.static_factor),
        beta:          proxyForm.beta          === '' ? null : Number(proxyForm.beta),
        correlation:   Number(proxyForm.correlation) || 1.0,
        kind:          proxyForm.kind,
        note:          proxyForm.note || null,
        is_active:     !!proxyForm.is_active,
      };
      if (!payload.proxy_symbol || !payload.target_root) {
        proxiesErr = 'proxy_symbol and target_root required';
        return;
      }
      await createHedgeProxy(payload);
      proxyForm = { proxy_symbol:'', target_root:'', conversion_kind:'dynamic',
                    static_factor:'', beta:'', correlation:'1.0', kind:'units',
                    note:'', is_active:true };
      await loadProxies();
    } catch (e) { proxiesErr = e?.message || 'create failed'; }
  }
  async function saveProxy(row) {
    proxiesErr = '';
    try {
      await updateHedgeProxy(row.id, {
        conversion_kind: row.conversion_kind,
        static_factor: row.static_factor === '' || row.static_factor == null ? null : Number(row.static_factor),
        beta:          row.beta          === '' || row.beta          == null ? null : Number(row.beta),
        correlation:   Number(row.correlation),
        kind:          row.kind,
        note:          row.note,
        is_active:     !!row.is_active,
      });
      await loadProxies();
    } catch (e) { proxiesErr = e?.message || 'save failed'; }
  }
  async function removeProxy(row) {
    proxiesErr = '';
    try { await deleteHedgeProxy(row.id); await loadProxies(); }
    catch (e) { proxiesErr = e?.message || 'delete failed'; }
  }

  function onEdit(/** @type {any} */ s, /** @type {any} */ newVal) {
    dirty[s.key] = String(newVal);
  }

  async function save(/** @type {any} */ s) {
    error = ''; note = '';
    try {
      const updated = await updateSetting(s.key, dirty[s.key]);
      // Replace the row in-place so the UI reflects canonical server value.
      settings = settings.map(r => r.key === s.key ? updated : r);
      delete dirty[s.key];
      dirty = { ...dirty };
      note = `Saved ${s.key}`;
    } catch (e) { error = 'Save failed.'; }
  }

  async function reset(/** @type {any} */ s) {
    error = ''; note = '';
    try {
      const updated = await resetSetting(s.key);
      settings = settings.map(r => r.key === s.key ? updated : r);
      delete dirty[s.key];
      dirty = { ...dirty };
      note = `Reset ${s.key} to ${updated.default_value}`;
    } catch (e) { error = 'Reset failed.'; }
  }

  // Execution-mode summary used by the top banner: how many of the
  // execution.live.* flags are currently set to True.
  const execRows = $derived(settings.filter(s => s.key.startsWith('execution.live.')));
  const liveCount = $derived(execRows.filter(s => String(currentValue(s)).toLowerCase() === 'true').length);

  // Group settings by (remapped) category for rendering, applying the
  // operator's filter and sorting groups by CATEGORY_ORDER (anything
  // unlisted is appended alphabetically).
  const grouped = $derived.by(() => {
    const f = filter.trim().toLowerCase();
    const matches = (/** @type {any} */ s) => {
      if (!f) return true;
      return s.key.toLowerCase().includes(f)
          || (s.description || '').toLowerCase().includes(f);
    };
    const out = /** @type {Record<string, typeof settings>} */({});
    for (const s of settings) {
      if (!matches(s)) continue;
      const cat = CATEGORY_REMAP[s.category] || s.category;
      (out[cat] ??= []).push(s);
    }
    const idx = (/** @type {string} */ c) => {
      const i = CATEGORY_ORDER.indexOf(c);
      return i === -1 ? CATEGORY_ORDER.length : i;
    };
    return Object.entries(out).sort(([a], [b]) => {
      const d = idx(a) - idx(b);
      return d !== 0 ? d : a.localeCompare(b);
    });
  });

  function currentValue(/** @type {any} */ s) {
    return s.key in dirty ? dirty[s.key] : s.value;
  }
  function isDirty(/** @type {any} */ s) {
    return s.key in dirty && dirty[s.key] !== s.value;
  }
  function isModified(/** @type {any} */ s) {
    return s.value !== s.default_value;
  }

  onMount(() => {
    const r = $authStore.user?.role;
    if (!$authStore.user || (r !== 'admin' && r !== 'designated')) { goto('/signin'); return; }
    load();
    _loadPinnedSymbols();
  });
</script>

<svelte:head><title>Settings | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Settings</h1>
  </span>
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} {loading} label="settings" />
    <PageHeaderActions />
  </span>
</div>

{#if error}<div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{error}</div>{/if}
{#if note}<div class="mb-3 p-2 rounded bg-emerald-500/10 text-emerald-300 text-[0.65rem] border border-emerald-500/30">{note}</div>{/if}

{#if execRows.length}
  <div class="mb-3 p-2 rounded text-[0.65rem] border
              {liveCount === 0
                ? 'bg-emerald-500/10 text-emerald-300 border-emerald-500/40'
                : 'bg-red-500/15 text-red-300 border-red-500/50'}">
    <b>Execution mode:</b>
    {#if liveCount === 0}
      Every broker action is in <b>PAPER</b> mode — no real orders will hit
      the broker. Flip individual <span class="font-mono">execution.live.*</span>
      flags below to promote a single action to live.
    {:else}
      <span class="px-1 rounded bg-red-500/30 font-bold">⚠ {liveCount} of {execRows.length}</span>
      action{liveCount === 1 ? '' : 's'} are <b>LIVE</b> — real orders will
      hit the broker for these. Set the flag back to <span class="font-mono">false</span>
      to revert to paper.
    {/if}
  </div>
{/if}

{#if loading}
  <div class="text-[0.65rem] text-[#c8d8f0]/60">Loading…</div>
{:else if !settings.length}
  <div class="text-[0.65rem] text-[#c8d8f0]/60">No settings seeded yet.</div>
{:else}
  <div class="mb-3 flex items-center gap-2">
    <input type="text"
           class="field-input flex-1 max-w-md"
           placeholder="Filter by key or description…"
           bind:value={filter} />
    {#if filter}
      <button type="button"
              class="btn-secondary text-[0.6rem] py-1 px-3"
              onclick={() => filter = ''}>Clear</button>
    {/if}
  </div>
  {#if !grouped.length}
    <div class="text-[0.65rem] text-[#c8d8f0]/60">No settings match the filter.</div>
  {/if}
  {#each grouped as [category, rows]}
    <section class="algo-status-card p-2 mb-2" data-status="inactive">
      <h2 class="text-[0.6rem] font-bold uppercase tracking-wider text-[#fbbf24] mb-1 pb-1 border-b border-[#fbbf24]/25">
        {category} <span class="opacity-60 font-normal ml-1">({rows.length})</span>
      </h2>
      <div>
        {#each rows as s}
          <div class="settings-row">
            <div class="grid grid-cols-[auto_minmax(0,1fr)_110px_auto_auto] gap-2 items-center text-[0.65rem] py-1">
              <InfoHint text={[
                s.description,
                `<span class="font-mono text-[#c8d8f0]/80 text-[0.55rem]">default: ${s.default_value}</span>`,
                s.schema?.min !== undefined || s.schema?.max !== undefined ? `range: ${s.schema.min ?? '−∞'} … ${s.schema.max ?? '+∞'}` : '',
                s.schema?.enum ? `choices: ${s.schema.enum.join(' / ')}` : '',
                s.units ? `units: <span class="font-mono">${s.units}</span>` : '',
              ].filter(Boolean).join('<br>')} />

              <div class="flex items-baseline gap-2 flex-wrap">
                <span class="font-mono text-[#7dd3fc] break-all">{s.key}</span>
                {#if isModified(s)}
                  <span class="px-1 rounded bg-[#fbbf24]/15 text-[#fbbf24] border border-[#fbbf24]/30 text-[0.55rem] shrink-0">mod</span>
                {/if}
              </div>

              <div class="flex items-center gap-1">
                {#if s.value_type === 'bool'}
                  <div class="flex-1">
                    <Select ariaLabel={s.key}
                      value={String(currentValue(s))}
                      onValueChange={(/** @type {any} */ v) => onEdit(s, v)}
                      options={[
                        { value: 'true',  label: 'true'  },
                        { value: 'false', label: 'false' },
                      ]} />
                  </div>
                {:else if s.value_type === 'enum'}
                  <div class="flex-1">
                    <Select ariaLabel={s.key}
                      value={String(currentValue(s))}
                      onValueChange={(/** @type {any} */ v) => onEdit(s, v)}
                      options={(s.schema?.enum || []).map((/** @type {string} */ opt) => ({ value: opt, label: opt }))} />
                  </div>
                {:else if s.key === 'orders.default_symbol' && _pinnedOptions.length}
                  <!-- Operator request: "When you update in settings it
                       should show the drop down from pinned symbols".
                       Pinned-watchlist items (Default + Markets) feed
                       the dropdown; the stored value stays a string so
                       the modal's resolveUnderlying() can map it to a
                       tradeable contract. Falls back to a free-text
                       input below when the watchlist fetch failed. -->
                  <div class="flex-1">
                    <Select ariaLabel={s.key}
                      value={String(currentValue(s))}
                      onValueChange={(/** @type {any} */ v) => onEdit(s, v)}
                      options={_pinnedOptions} />
                  </div>
                {:else if s.value_type === 'int' || s.value_type === 'float'}
                  <input type="number"
                         class="field-input flex-1 py-0.5"
                         value={currentValue(s)}
                         min={s.schema?.min} max={s.schema?.max} step={s.schema?.step ?? 1}
                         oninput={(e) => onEdit(s, e.currentTarget.value)} />
                {:else}
                  <input type="text"
                         class="field-input flex-1 py-0.5"
                         value={currentValue(s)}
                         oninput={(e) => onEdit(s, e.currentTarget.value)} />
                {/if}
                {#if s.units}<span class="text-[0.55rem] text-[#7e97b8] whitespace-nowrap">{s.units}</span>{/if}
              </div>

              <button type="button"
                onclick={() => save(s)}
                disabled={!isDirty(s)}
                class="btn-primary text-[0.6rem] py-0.5 px-2 disabled:opacity-30 whitespace-nowrap">Save</button>

              <button type="button"
                onclick={() => reset(s)}
                disabled={!isModified(s)}
                class="btn-secondary text-[0.6rem] py-0.5 px-2 disabled:opacity-30 whitespace-nowrap">Reset</button>
            </div>

          </div>
        {/each}
      </div>
    </section>
  {/each}

  <!-- Hedge-proxy table — backs the /admin/derivatives Underlying
       picker's proxy-aware Tier 4 + the proxy-eq leg math. Operator:
       "there should be a panel to enter in the current admin settings
       pages. the code should use this table." -->
  <section class="algo-status-card p-2 mb-2" data-status="inactive">
    <h2 class="text-[0.8rem] font-bold mb-1">Hedge proxies</h2>
    <p class="text-[0.6rem] opacity-70 mb-1">
      Cross-reference between a held instrument (GOLDBEES, NIFTYBEES, …)
      and the option underlying it can hedge. <code>dynamic</code> mode
      derives factor from current LTPs; <code>static</code> uses
      <code>static_factor</code>; <code>beta</code> uses <code>beta</code>.
      <code>correlation</code> (0–1) scales the effective qty.
    </p>
    {#if proxiesErr}
      <div class="text-[0.65rem] text-red-300 mb-1">{proxiesErr}</div>
    {/if}
    {#if proxies.length}
      <div class="overflow-x-auto mb-2">
        <table class="text-[0.65rem] w-full">
          <thead class="opacity-70">
            <tr><th class="text-left p-1">Proxy</th>
                <th class="text-left p-1">Target</th>
                <th class="text-left p-1">Mode</th>
                <th class="text-left p-1">Static</th>
                <th class="text-left p-1">Beta</th>
                <th class="text-left p-1">Corr</th>
                <th class="text-left p-1">Kind</th>
                <th class="text-left p-1">Note</th>
                <th class="text-left p-1">Src</th>
                <th class="text-left p-1">Active</th>
                <th></th></tr>
          </thead>
          <tbody>
            {#each proxies as p (p.id)}
              <tr class="border-t border-white/5">
                <td class="p-1 font-mono">{p.proxy_symbol}</td>
                <td class="p-1 font-mono">{p.target_root}</td>
                <td class="p-1">
                  <select bind:value={p.conversion_kind} class="field-input w-20">
                    <option value="dynamic">dynamic</option>
                    <option value="static">static</option>
                    <option value="beta">beta</option>
                  </select>
                </td>
                <td class="p-1">
                  <input type="number" step="0.0001" bind:value={p.static_factor} class="field-input w-16" />
                </td>
                <td class="p-1">
                  <input type="number" step="0.01" bind:value={p.beta} class="field-input w-14" />
                </td>
                <td class="p-1">
                  <input type="number" step="0.01" min="0" max="1" bind:value={p.correlation} class="field-input w-14" />
                </td>
                <td class="p-1">
                  <select bind:value={p.kind} class="field-input w-16">
                    <option value="units">units</option>
                    <option value="shares">shares</option>
                  </select>
                </td>
                <td class="p-1"><input bind:value={p.note} class="field-input w-44" /></td>
                <td class="p-1 opacity-60">{p.source}</td>
                <td class="p-1"><input type="checkbox" bind:checked={p.is_active} /></td>
                <td class="p-1 flex gap-1">
                  <button class="btn-primary text-[0.6rem] py-0.5 px-2" onclick={() => saveProxy(p)}>Save</button>
                  <button class="btn-secondary text-[0.6rem] py-0.5 px-2" onclick={() => removeProxy(p)}>×</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
    <div class="flex flex-wrap items-center gap-1 text-[0.65rem]">
      <input placeholder="Proxy (e.g. GOLDBEES)" bind:value={proxyForm.proxy_symbol} class="field-input w-32" />
      <input placeholder="Target (e.g. GOLD)"    bind:value={proxyForm.target_root}  class="field-input w-28" />
      <select bind:value={proxyForm.conversion_kind} class="field-input w-20">
        <option value="dynamic">dynamic</option>
        <option value="static">static</option>
        <option value="beta">beta</option>
      </select>
      <input placeholder="static_factor" bind:value={proxyForm.static_factor} class="field-input w-20" />
      <input placeholder="beta"          bind:value={proxyForm.beta}          class="field-input w-16" />
      <input placeholder="correlation"   bind:value={proxyForm.correlation}   class="field-input w-16" />
      <select bind:value={proxyForm.kind} class="field-input w-16">
        <option value="units">units</option>
        <option value="shares">shares</option>
      </select>
      <input placeholder="note" bind:value={proxyForm.note} class="field-input flex-1 min-w-32" />
      <button class="btn-primary text-[0.65rem] py-0.5 px-2" onclick={addProxy}>+ Add</button>
    </div>
  </section>
{/if}

<style>
  .settings-row {
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }
  .settings-row:last-child { border-bottom: 0; }
</style>
