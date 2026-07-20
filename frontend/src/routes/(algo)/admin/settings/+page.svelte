<script>
  // Admin Settings — DB-backed tunables grouped by category. Pairs with
  // backend/api/routes/settings.py and backend/shared/helpers/settings.py.
  // Seed list is the authoritative catalog of editable knobs; this page
  // renders it and writes back via PATCH.

  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import { userRole, userCaps, userCapsReady, hasCap } from '$lib/rbac';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import InfoHint from '$lib/InfoHint.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { fetchSettings, updateSetting, resetSetting, fetchWatchlists,
           fetchHedgeProxies, createHedgeProxy, updateHedgeProxy, deleteHedgeProxy,
           computeHedgeProxy } from '$lib/api';
  import { loadHedgeProxies as _invalidateHedgeProxyCache } from '$lib/data/hedgeProxies';
  import Select   from '$lib/Select.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

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

  // ── Hedge proxy CRUD (pair-only) ───────────────────────────────────
  /** @type {Array<{id:number,proxy_symbol:string,target_root:string,
   *                is_active:boolean,note:string|null,correlation:number,
   *                beta:number|null,regression_at:string|null,
   *                regression_error:string|null,
   *                target_sigma:number|null,proxy_sigma:number|null}>} */
  let proxies = $state([]);
  let proxiesErr = $state('');
  let proxyForm = $state({ proxy_symbol: '', target_root: '', note: '', is_active: true });
  async function loadProxies() {
    proxiesErr = '';
    try { const r = await fetchHedgeProxies(); proxies = Array.isArray(r?.rows) ? r.rows : []; }
    catch (e) { proxiesErr = e?.message || 'fetch failed'; }
  }
  async function addProxy() {
    proxiesErr = '';
    try {
      // Sprint E (audit) — `correlation` removed from the create form.
      // The regression endpoint overwrites it on every run with R², so
      // any operator-set value was silently destroyed. The DB column
      // defaults to 1.0 (ETF tracking case) which is the right
      // pre-regression default anyway.
      const payload = {
        proxy_symbol: String(proxyForm.proxy_symbol || '').trim().toUpperCase(),
        target_root:  String(proxyForm.target_root  || '').trim().toUpperCase(),
        note:         proxyForm.note || null,
        is_active:    !!proxyForm.is_active,
      };
      if (!payload.proxy_symbol || !payload.target_root) {
        proxiesErr = 'proxy_symbol and target_root required';
        return;
      }
      await createHedgeProxy(payload);
      proxyForm = { proxy_symbol:'', target_root:'', note:'', is_active:true };
      await loadProxies();
    } catch (e) { proxiesErr = e?.message || 'create failed'; }
  }
  async function saveProxy(row) {
    proxiesErr = '';
    try {
      await updateHedgeProxy(row.id, {
        note:        row.note,
        is_active:   !!row.is_active,
        correlation: Number(row.correlation) || 1.0,
      });
      await loadProxies();
    } catch (e) { proxiesErr = e?.message || 'save failed'; }
  }
  async function removeProxy(row) {
    proxiesErr = '';
    try { await deleteHedgeProxy(row.id); await loadProxies(); }
    catch (e) { proxiesErr = e?.message || 'delete failed'; }
  }
  /** @type {Record<number, boolean>} */
  let computingProxy = $state({});
  async function computeProxy(row) {
    proxiesErr = '';
    computingProxy = { ...computingProxy, [row.id]: true };
    try {
      await computeHedgeProxy(row.id);
      await loadProxies();
      // Sprint D — invalidate the shared module-level cache so any
      // open /admin/derivatives tab picks up the new β on its next
      // poll cycle. Without this, the sibling page kept showing the
      // pre-compute β until the user reloaded.
      await _invalidateHedgeProxyCache(true);
    }
    catch (e) { proxiesErr = e?.message || 'regression failed'; }
    finally { computingProxy = { ...computingProxy, [row.id]: false }; }
  }
  function _shortDate(s) {
    if (!s) return '—';
    try { return new Date(s).toISOString().slice(0, 10); } catch { return s.slice(0, 10); }
  }

  function onEdit(/** @type {any} */ s, /** @type {any} */ newVal) {
    dirty[s.key] = String(newVal);
  }

  async function save(/** @type {any} */ s) {
    error = '';
    try {
      const updated = await updateSetting(s.key, dirty[s.key]);
      // Replace the row in-place so the UI reflects canonical server value.
      settings = settings.map(r => r.key === s.key ? updated : r);
      delete dirty[s.key];
      dirty = { ...dirty };
      toast.success(`Saved: ${s.key}`);
    } catch (e) { error = 'Save failed.'; toast.error('Save failed'); }
  }

  async function reset(/** @type {any} */ s) {
    error = '';
    try {
      const updated = await resetSetting(s.key);
      settings = settings.map(r => r.key === s.key ? updated : r);
      delete dirty[s.key];
      dirty = { ...dirty };
      toast.info(`Reset ${s.key} → ${updated.default_value}`);
    } catch (e) { error = 'Reset failed.'; toast.error('Reset failed'); }
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

  // Canonical $effect-gated auth. manage_settings is designated-only by
  // design — settings writes affect every operator.
  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values (feedback note:
  // "$derived reading $store.x can stale-cache; bridge via $effect + $state").
  // _canView is further guarded by $userCapsReady in the template so the
  // access-denied panel never shows as a false-positive during boot.
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const _canView = $derived(hasCap('manage_settings', _caps, _role));
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      // PRIMARY — the settings list. Operator needs the cards to paint.
      load();
      // SECONDARY — pinned-watchlist symbols only populate the
      // orders.default_symbol dropdown (one row, often below the fold).
      // Defer one event-loop tick so the settings cards paint first;
      // the dropdown swaps from free-text → Select once symbols land.
      setTimeout(() => { _loadPinnedSymbols(); }, 0);
    }
  });
</script>

<svelte:head><title>Settings | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Settings</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} {loading} label="settings" />
    <PageHeaderActions />
  </span>
</div>

{#if !$userCapsReady}
  <!-- RBAC bootstrap still in-flight — show a skeleton so a legitimate
       operator never sees the access-denied panel as a false-positive. -->
  <LoadingSkeleton variant="card" rows={3} />
{:else if !_canView}
  <EmptyState title="Access denied" icon="lock">
    {#snippet hintBody()}
      Editing settings requires the <code>manage_settings</code> capability
      (designated role). Your current role is <strong>{$userRole}</strong> —
      contact an admin to request access.
    {/snippet}
  </EmptyState>
{:else}

{#if error}<div class="mb-3 p-2 rounded bg-red-500/15 text-red-300 text-[0.65rem] border border-red-500/40">{error}</div>{/if}

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
  <LoadingSkeleton variant="card" rows={4} />
  <LoadingSkeleton variant="card" rows={3} />
  <LoadingSkeleton variant="card" rows={5} />
{:else if !settings.length}
  <div class="text-[0.65rem] text-[#c8d8f0]/60">No settings seeded yet.</div>
{:else}
  <div class="mb-3 flex items-center gap-2 content-fade-in">
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
    <section class="algo-card mb-2" data-status="inactive">
      <h3 class="section-heading">
        {category} <span class="opacity-60 font-normal ml-1">({rows.length})</span>
      </h3>
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
                  <span class="px-1 rounded bg-[var(--c-action)]/15 text-[var(--c-action)] border border-[var(--c-action)]/30 text-[0.55rem] shrink-0">mod</span>
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
                {#if s.units}<span class="text-[0.55rem] text-[var(--c-muted)] whitespace-nowrap">{s.units}</span>{/if}
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
      {#if category === 'hedge_proxies'}
        <!-- Pair-table CRUD rendered inside the same card as the
             knobs above so the operator sees "everything about hedge
             proxies" in one place — not split across two adjacent
             cards. -->
        {@render proxyCrud()}
      {/if}
    </section>
  {/each}

{#snippet proxyCrud()}
  <div class="mt-2 pt-2 border-t" style="border-top-color: rgba(126,151,184,0.10)">
    <h3 class="text-[0.65rem] font-bold mb-1 opacity-90">Pair table</h3>
    <p class="text-[0.55rem] opacity-70 mb-1">
      Pair-only cross-reference between a held instrument and the
      option underlying it can hedge. The derivatives page computes
      effective qty as <code>β × market_value ÷ target_spot</code>
      and the lot count from the instruments cache.
      <br />
      <strong>ETF tracking hedges</strong> (GOLDBEES → GOLD,
      NIFTYBEES → NIFTY etc.): β stays blank ⇒ math uses 1.0. No
      regression needed.
      <br />
      <strong>Stock-vs-index hedges</strong> (Stage 3 — RELIANCE → NIFTY
      etc.): click <em>Compute β</em> to run a 60-day daily-returns
      regression on log returns. The server resolves both symbols
      against the instruments cache, fetches daily closes, and computes:
      <br />
      <code>β = Cov(r_target, r_proxy) / Var(r_target)</code>
      &nbsp;·&nbsp;
      <code>R² = corr(r_target, r_proxy)²</code>
      &nbsp;·&nbsp;
      <code>σ = stdev(r) × √252</code> (annualised vol)
      <br />
      σ is the underlying's typical annual swing — same units as the
      IV chip on the derivatives page. Read alongside R² to gauge
      hedge fit: high R² + low σ = tight hedge; low R² = noisy.
    </p>
    {#if proxiesErr}
      <div class="text-[0.65rem] text-red-300 mb-1">{proxiesErr}</div>
    {/if}
    {#if proxies.length}
      <div class="overflow-x-auto mb-2">
        <table class="algo-table">
          <thead class="opacity-70">
            <tr><th class="text-left p-1">Proxy</th>
                <th class="text-left p-1">Target</th>
                <th class="text-left p-1">Note</th>
                <th class="text-left p-1" title="Regression slope (proxy log-returns vs target log-returns). Blank → ETF case (β=1.0 implicit).">β</th>
                <th class="text-left p-1" title="R² = Pearson correlation², clamped to [0..1]. >0.7 = tight hedge, <0.4 = noisy.">R²</th>
                <th class="text-left p-1" title="Target's annualised volatility (daily σ × √252). Same units as Black-Scholes IV — reads as 'this underlying typically swings ±X% over a year'.">σ_t</th>
                <th class="text-left p-1" title="Proxy's own annualised vol. Useful for sanity-checking leveraged ETFs (β should ≈ σ_p / σ_t).">σ_p</th>
                <th class="text-left p-1" title="Date β was last computed">Run</th>
                <th class="text-left p-1">Active</th>
                <th></th></tr>
          </thead>
          <tbody>
            {#each proxies as p (p.id)}
              <tr class="border-t" style="border-top-color: rgba(126,151,184,0.10)">
                <td class="p-1 font-mono">{p.proxy_symbol}</td>
                <td class="p-1 font-mono">{p.target_root}</td>
                <td class="p-1"><input bind:value={p.note} class="field-input w-44" /></td>
                <td class="p-1 font-mono opacity-80">{p.beta != null ? Number(p.beta).toFixed(3) : '—'}</td>
                <td class="p-1 font-mono opacity-80">{Number(p.correlation ?? 1).toFixed(2)}</td>
                <td class="p-1 font-mono opacity-80">{p.target_sigma != null ? (Number(p.target_sigma) * 100).toFixed(1) + '%' : '—'}</td>
                <td class="p-1 font-mono opacity-80">{p.proxy_sigma  != null ? (Number(p.proxy_sigma)  * 100).toFixed(1) + '%' : '—'}</td>
                <td class="p-1 opacity-70" title={p.regression_error || ''}
                    class:text-red-400={!!p.regression_error}>
                  {_shortDate(p.regression_at)}{p.regression_error ? ' ⚠' : ''}
                </td>
                <td class="p-1"><input type="checkbox" bind:checked={p.is_active} /></td>
                <td class="p-1 flex gap-1">
                  <button class="btn-primary text-[0.6rem] py-0.5 px-2"
                          disabled={!!computingProxy[p.id]}
                          onclick={() => computeProxy(p)}>
                    {computingProxy[p.id] ? '…' : 'Compute β'}
                  </button>
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
      <input placeholder="note" bind:value={proxyForm.note} class="field-input flex-1 min-w-32" />
      <!-- Sprint E (audit) — correlation removed from the form. The
           regression endpoint overwrites it with R² on every run, so
           an operator-set value was silently destroyed. The DB default
           (1.0) is the right pre-regression baseline. -->
      <button class="btn-primary text-[0.65rem] py-0.5 px-2" onclick={addProxy}>+ Add</button>
    </div>
  </div>
{/snippet}
{/if}

{/if}

<style>
  /* .empty-state rules removed — access-denied panel migrated to
     EmptyState component (slice AE). */
  .settings-row {
    border-bottom: 1px solid rgba(126,151,184,0.10);
  }
  .settings-row:last-child { border-bottom: 0; }
  .section-heading { font-size: var(--fs-sm, 0.6rem); font-weight: 700; text-transform: uppercase; letter-spacing: 0.04em; color: var(--c-action, #fbbf24); padding-bottom: 0.3rem; margin-bottom: 0.5rem; border-bottom: 1px solid rgba(255,255,255,0.10); }
</style>
