<script>
  // Admin Settings — DB-backed tunables grouped by category. Pairs with
  // backend/api/routes/settings.py and backend/shared/helpers/settings.py.
  // Seed list is the authoritative catalog of editable knobs; this page
  // renders it and writes back via PATCH.

  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { authStore, clientTimestamp } from '$lib/stores';
  import { fetchSettings, updateSetting, resetSetting } from '$lib/api';
  import InfoHint from '$lib/InfoHint.svelte';

  /** @type {Array<{id:number, category:string, key:string, value_type:string,
   *                value:string, default_value:string, description:string,
   *                schema:any, units:string|null, updated_at:string}>} */
  let settings = $state([]);
  let loading  = $state(true);
  let error    = $state('');
  let note     = $state('');
  let dirty    = $state(/** @type {Record<string, string>} */({}));
  let filter   = $state('');
  let expanded = $state(/** @type {Record<string, boolean>} */({}));

  function toggleInfo(/** @type {string} */ key) {
    expanded[key] = !expanded[key];
    expanded = { ...expanded };
  }

  // Render order: high-touch operator knobs first, vendor/infra knobs last.
  // Anything not in this list falls through to 'misc' and is appended.
  const CATEGORY_ORDER = ['execution', 'alerts', 'algo', 'performance',
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
    if (!$authStore.user || $authStore.user.role !== 'admin') { goto('/signin'); return; }
    load();
  });
</script>

<svelte:head><title>Settings | RamboQuant Analytics</title></svelte:head>

<div class="page-header">
  <h1 class="page-title-chip">Settings</h1>
  <InfoHint text={'DB-backed tunables. Edits take effect on the next agent tick / sim run without a deploy. Values are preserved across deploys; pressing <b>Reset</b> returns a key to its code-shipped default. Infrastructure parameters (DB credentials, market hours, Kite URLs, IPv6 addresses) deliberately stay in <span class="font-mono">backend_config.yaml</span> — they change once a quarter and have no business being in the DB.'} />
  <span class="algo-ts">{clientTimestamp()}</span>
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
              <button type="button"
                      class="info-btn"
                      class:open={expanded[s.key]}
                      title={expanded[s.key] ? 'Hide details' : 'Show details'}
                      onclick={() => toggleInfo(s.key)}>i</button>

              <div class="flex items-baseline gap-2 flex-wrap">
                <span class="font-mono text-[#7dd3fc] break-all">{s.key}</span>
                {#if isModified(s)}
                  <span class="px-1 rounded bg-[#fbbf24]/15 text-[#fbbf24] border border-[#fbbf24]/30 text-[0.55rem] shrink-0">mod</span>
                {/if}
              </div>

              <div class="flex items-center gap-1">
                {#if s.value_type === 'bool'}
                  <select class="field-input flex-1 py-0.5"
                          value={currentValue(s)}
                          onchange={(e) => onEdit(s, e.currentTarget.value)}>
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                {:else if s.value_type === 'enum'}
                  <select class="field-input flex-1 py-0.5"
                          value={currentValue(s)}
                          onchange={(e) => onEdit(s, e.currentTarget.value)}>
                    {#each (s.schema?.enum || []) as opt}<option value={opt}>{opt}</option>{/each}
                  </select>
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

            {#if expanded[s.key]}
              <div class="info-popout">
                <div class="text-[0.6rem] text-[#c8d8f0]/85">{s.description}</div>
                <div class="text-[0.55rem] text-[#7e97b8] mt-1">
                  default: <span class="font-mono text-[#c8d8f0]/80">{s.default_value}</span>
                  {#if s.schema && (s.schema.min !== undefined || s.schema.max !== undefined)}
                    <span class="mx-1">·</span>range: {s.schema.min ?? '−∞'} … {s.schema.max ?? '+∞'}
                  {/if}
                  {#if s.schema?.enum}
                    <span class="mx-1">·</span>choices: {s.schema.enum.join(' / ')}
                  {/if}
                  {#if s.units}
                    <span class="mx-1">·</span>units: <span class="font-mono">{s.units}</span>
                  {/if}
                </div>
              </div>
            {/if}
          </div>
        {/each}
      </div>
    </section>
  {/each}
{/if}

<style>
  .settings-row {
    border-bottom: 1px solid rgba(255,255,255,0.05);
  }
  .settings-row:last-child { border-bottom: 0; }

  /* Info toggle — mirrors the page's amber accent so it reads as part
     of the algo palette, not a generic chrome control. */
  .info-btn {
    width: 1.1rem;
    height: 1.1rem;
    border-radius: 9999px;
    border: 1px solid rgba(251,191,36,0.35);
    background: rgba(251,191,36,0.08);
    color: #fbbf24;
    font-size: 0.6rem;
    font-style: italic;
    font-weight: 700;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
  }
  .info-btn:hover {
    background: rgba(251,191,36,0.18);
    border-color: rgba(251,191,36,0.6);
  }
  .info-btn.open {
    background: #fbbf24;
    color: #0c1830;
    border-color: #fbbf24;
  }

  /* Expanded info — flat slate-blue surface, faint sky-blue border,
     no left accent (matches canonical InfoHint styling). */
  .info-popout {
    margin: 0.15rem 0 0.4rem 1.6rem;
    padding: 0.4rem 0.6rem;
    border-radius: 0.25rem;
    border: 1px solid rgba(125,211,252,0.25);
    background: linear-gradient(180deg, #273552 0%, #1d2a44 100%);
  }
</style>
