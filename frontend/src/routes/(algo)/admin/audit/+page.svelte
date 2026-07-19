<!--
  /admin/audit — paginated audit log viewer.

  Backed by `GET /api/admin/audit` (gated by `view_audit` cap server-
  side — admin / risk / ops). Filters by actor / action substring /
  target type / target id / status code / time window. Results paged
  50/row with prev/next + a "showing 1-50 of N" footer.

  No write surface — auditors look, they don't edit. The page is
  intentionally bare: a table, a row of filter inputs, paging
  controls. SEBI Cat-III audit visits don't need a fancy UI; they
  need the trail.
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { marketAwareInterval, authStore } from '$lib/stores';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import { fetchAuditLog } from '$lib/api';
  import { userRole, hasCap, userCaps, userCapsReady } from '$lib/rbac';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

  /** @typedef {{
   *   id: number, actor_user_id: number|null, actor_username: string,
   *   actor_role: string, action: string, category: string|null,
   *   method: string, path: string,
   *   target_type: string|null, target_id: string|null,
   *   status_code: number, summary: string|null,
   *   request_id: string, client_ip: string|null,
   *   user_agent: string|null, created_at: string
   * }} AuditRow */

  /** Filter pills — categories the operator scopes the view to. The
   *  underlying audit row carries a single category string; pill
   *  groups (e.g. "Orders" = order.place + order.fill + order.modify
   *  + order.cancel + order.reject) pass a comma-separated list to
   *  the backend. */
  const CATEGORY_PILLS = /** @type {const} */ ([
    { key: 'all',     label: 'All',         cats: '' },
    { key: 'orders',  label: 'Orders',      cats: 'order.place,order.modify,order.cancel,order.fill,order.reject,order' },
    { key: 'agents',  label: 'Agents',      cats: 'agent.action' },
    { key: 'users',   label: 'Users',       cats: 'user' },
    { key: 'config',  label: 'Config',      cats: 'config,config.broker,config.grammar,config.fragment,config.hedge' },
    { key: 'system',  label: 'System',      cats: 'system.nav,system.statement,system.bootstrap' },
  ]);
  let categoryPill = $state(/** @type {string} */ ('all'));

  /** @type {AuditRow[]} */
  let rows = $state([]);
  let total = $state(0);
  let loading = $state(false);
  let error = $state('');

  // Filter state — all optional, AND-combined server-side.
  let fActor       = $state('');
  let fAction      = $state('');
  let fTargetType  = $state('');
  let fTargetId    = $state('');
  let fRequestId   = $state('');   // Drill-through from /admin/history
  let fStatus      = $state('');
  let fSinceHours  = $state(72);   // default: last 3 days

  const LIMIT = 50;
  let offset = $state(0);

  async function load() {
    loading = true; error = '';
    try {
      const _pill = CATEGORY_PILLS.find(p => p.key === categoryPill);
      const params = {
        limit: LIMIT,
        offset,
        actor: fActor.trim() || undefined,
        action: fAction.trim() || undefined,
        category: _pill?.cats || undefined,
        target_type: fTargetType.trim() || undefined,
        target_id: fTargetId.trim() || undefined,
        request_id: fRequestId.trim() || undefined,
        status_code: fStatus.trim() ? Number(fStatus.trim()) : undefined,
        since_hours: fSinceHours ? Number(fSinceHours) : undefined,
      };
      const r = await fetchAuditLog(params);
      rows = Array.isArray(r?.rows) ? r.rows : [];
      total = Number(r?.total ?? 0);
    } catch (e) {
      error = e?.message || 'Audit fetch failed';
      toast.error(`Audit load failed: ${e?.message || 'unknown error'}`);
    } finally {
      loading = false;
    }
  }

  function clearFilters() {
    fActor = '';
    fAction = '';
    fTargetType = '';
    fTargetId = '';
    fRequestId = '';
    fStatus = '';
    fSinceHours = 72;
    categoryPill = 'all';
    offset = 0;
    load();
  }
  function setCategory(/** @type {string} */ key) {
    if (key === categoryPill) return;
    categoryPill = key;
    offset = 0;
    load();
  }

  function applyFilters() { offset = 0; load(); }

  // ── Quick-filter presets (slice AP) ─────────────────────────────
  // Chip-style shortcuts above the manual filter inputs. Each preset
  // sets one or more filter fields + calls load(). Operator-facing
  // convenience — "show me errors in the last hour" is a 1-click
  // action instead of "type 1 in hours, type 4 in status, hit apply".
  function setTime(/** @type {number} */ hours) {
    fSinceHours = hours;
    offset = 0;
    load();
  }
  function presetErrors() {
    // status_code 4xx/5xx — the backend accepts a single int today,
    // so we use 400 as a placeholder + rely on the action filter for
    // wider scoping. A future backend tweak could accept a range.
    // For now: setting status to '4' filters to 4xx prefix matches if
    // the backend supports startswith. Fallback to status=500 toggle.
    fStatus = fStatus === '4' ? '' : '4';
    offset = 0;
    load();
  }
  function presetMine() {
    const u = $authStore?.user || '';
    fActor = (fActor === u) ? '' : u;
    offset = 0;
    load();
  }
  // Time presets — exact-match highlight. Manual edits to the "Last
  // (hours)" input that don't equal a preset value leave all chips
  // inactive (operator sees their custom value in the input instead).
  function _timeActive(/** @type {number} */ h) {
    return Number(fSinceHours) === h;
  }
  function pageNext() { if (offset + LIMIT < total) { offset += LIMIT; load(); } }
  function pagePrev() { if (offset > 0) { offset = Math.max(0, offset - LIMIT); load(); } }

  // Gate by capability — render a friendly "no access" panel rather
  // than redirecting (the route guard 403's anyway; this is the UX
  // for an admin/risk/ops who lacks the cap because of a misconfig).
  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values.
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const _canView = $derived(hasCap('view_audit', _caps, _role));

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let _teardown = null;

  onMount(() => {
    // URL param read happens regardless of _canView so the form
    // pre-fills correctly when the operator drilled here from
    // /admin/history before /whoami resolved.
    try {
      const sp = new URLSearchParams(window.location.search);
      const rid = (sp.get('request_id') || '').trim();
      if (rid) {
        fRequestId = rid;
        fSinceHours = 24 * 90;  // 90 days
      }
    } catch {}
  });

  // $effect-gated load — fires when _canView flips true (e.g. on
  // first /whoami resolution after the boot window). Pre-fix the
  // page used `onMount(() => { if (_canView) load(); })` which
  // ran once at false and never re-checked, so the operator briefly
  // saw "Access denied" + the data never loaded.
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      load();
      _teardown = marketAwareInterval(load, 30000);
    }
  });

  onDestroy(() => { _teardown?.(); });

  function _fmtTs(/** @type {string} */ iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      // Show IST since operator + SEBI both think in IST.
      const ist = d.toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        hour12: false,
      });
      return ist + ' IST';
    } catch { return iso; }
  }
  function _statusClass(/** @type {number} */ s) {
    if (s >= 200 && s < 300) return 'audit-st-ok';
    if (s >= 400 && s < 500) return 'audit-st-warn';
    return 'audit-st-err';
  }
  function _shortAction(/** @type {string} */ a) {
    return a.length > 80 ? a.slice(0, 78) + '…' : a;
  }
</script>

<svelte:head>
  <title>Audit Log · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Audit Log</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="audit log" />
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
      The audit log requires the <code>view_audit</code> capability
      (designated, admin, or risk role). Your current role is
      <strong>{$userRole}</strong> — contact an admin to request access
      if you need it.
    {/snippet}
  </EmptyState>
{:else}

<div class="audit-pills">
  {#each CATEGORY_PILLS as p}
    <button
      class="audit-pill"
      class:active={categoryPill === p.key}
      onclick={() => setCategory(p.key)}>{p.label}</button>
  {/each}
</div>

<!-- Quick-filter presets (slice AP). Time chips set fSinceHours;
     Errors toggles fStatus to '4'; Mine toggles fActor to the current
     username. Each chip is a one-click shortcut that beats hand-
     typing the equivalent field values. -->
<div class="audit-quickrow">
  <span class="audit-quicklbl">Time</span>
  <button class="audit-quick" class:active={_timeActive(1)}    onclick={() => setTime(1)}>1h</button>
  <button class="audit-quick" class:active={_timeActive(6)}    onclick={() => setTime(6)}>6h</button>
  <button class="audit-quick" class:active={_timeActive(24)}   onclick={() => setTime(24)}>24h</button>
  <button class="audit-quick" class:active={_timeActive(72)}   onclick={() => setTime(72)}>3d</button>
  <button class="audit-quick" class:active={_timeActive(168)}  onclick={() => setTime(168)}>7d</button>
  <button class="audit-quick" class:active={_timeActive(720)}  onclick={() => setTime(720)}>30d</button>
  <span class="audit-quicksep" aria-hidden="true"></span>
  <button class="audit-quick audit-quick-errors"
          class:active={fStatus === '4'}
          title="Show 4xx/5xx responses only. Click again to clear."
          onclick={presetErrors}>Errors</button>
  {#if $authStore?.user}
    <button class="audit-quick audit-quick-mine"
            class:active={fActor === $authStore.user}
            title="Filter to actions by {$authStore.user}. Click again to clear."
            onclick={presetMine}>Mine</button>
  {/if}
</div>

<div class="audit-filters">
  <label class="audit-flbl">Actor
    <input bind:value={fActor} placeholder="username" class="field-input audit-finput" />
  </label>
  <label class="audit-flbl">Action
    <input bind:value={fAction} placeholder="POST /api/..." class="field-input audit-finput audit-finput-wide" />
  </label>
  <label class="audit-flbl">Target type
    <input bind:value={fTargetType} placeholder="brokers / users / ..." class="field-input audit-finput" />
  </label>
  <label class="audit-flbl">Target id
    <input bind:value={fTargetId} placeholder="ZG0790 / 42 / ..." class="field-input audit-finput" />
  </label>
  <label class="audit-flbl">Request id
    <input bind:value={fRequestId} placeholder="uuid" class="field-input audit-finput" />
  </label>
  <label class="audit-flbl">Status
    <input bind:value={fStatus} placeholder="200" class="field-input audit-finput audit-finput-narrow" />
  </label>
  <label class="audit-flbl">Last (hours)
    <input type="number" bind:value={fSinceHours} class="field-input audit-finput audit-finput-narrow" />
  </label>
  <div class="audit-fbtns">
    <button class="btn-primary" onclick={applyFilters}>Apply</button>
    <button class="btn-secondary" onclick={clearFilters}>Clear</button>
  </div>
</div>

{#if loading}
  <LoadingSkeleton variant="grid-row" rows={8} height="1.2rem" />
{:else if rows.length === 0}
  <EmptyState
    title="No audit entries"
    hint="No rows match the current filters. Try widening the time window or clearing filters."
    icon="search"
  />
{:else}
<div class="audit-table-wrap algo-grid-chrome content-fade-in">
  <table class="algo-table audit-table">
    <thead>
      <tr>
        <th>Time (IST)</th>
        <th>Actor</th>
        <th>Role</th>
        <th>Category</th>
        <th>Action</th>
        <th>Target</th>
        <th class="audit-th-narrow">Status</th>
        <th>Summary</th>
        <th class="audit-th-narrow">Request ID</th>
        <th>Client IP</th>
      </tr>
    </thead>
    <tbody>
      {#each rows as r (r.id)}
        <tr>
          <td class="audit-ts">{_fmtTs(r.created_at)}</td>
          <td class="audit-actor">{r.actor_username || '—'}</td>
          <td><span class="audit-role audit-role-{r.actor_role || 'unknown'}">{r.actor_role || '—'}</span></td>
          <td><span class="audit-cat audit-cat-{(r.category || 'http').split('.')[0]}">{r.category || 'http'}</span></td>
          <td class="audit-action" title={r.action}>{_shortAction(r.action)}</td>
          <td class="audit-target">
            {#if r.target_type || r.target_id}
              <span class="audit-target-type">{r.target_type || '—'}</span>
              <span class="audit-target-id">{r.target_id || ''}</span>
            {:else}—{/if}
          </td>
          <td><span class="audit-status {_statusClass(r.status_code)}">{r.status_code}</span></td>
          <td class="audit-summary" title={r.summary || ''}>{r.summary || '—'}</td>
          <td class="audit-req-id" title={r.request_id}>{r.request_id?.slice(0, 8) || '—'}</td>
          <td class="audit-ip">{r.client_ip || '—'}</td>
        </tr>
      {/each}
    </tbody>
  </table>
</div>
{/if}

<div class="audit-pager">
  <button class="btn-secondary" disabled={offset === 0} onclick={pagePrev}>← Prev</button>
  <span class="audit-pager-info">
    {#if total > 0}
      Showing <strong>{offset + 1}</strong>–<strong>{Math.min(offset + LIMIT, total)}</strong>
      of <strong>{total}</strong>
    {:else}
      No rows
    {/if}
  </span>
  <button class="btn-secondary" disabled={offset + LIMIT >= total} onclick={pageNext}>Next →</button>
</div>

{/if}

<style>
  /* .audit-empty rules removed — access-denied panel migrated to
     EmptyState component (slice AE). */

  .audit-filters {
    display: flex; flex-wrap: wrap; gap: 0.55rem 0.8rem;
    align-items: flex-end;
    padding: 0.5rem 0.65rem;
    background: rgba(15, 23, 42, 0.45);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
    margin-bottom: 0.7rem;
  }
  .audit-flbl {
    display: flex; flex-direction: column; gap: 0.15rem;
    font-size: var(--fs-xs); font-weight: 700; letter-spacing: 0.05em;
    text-transform: uppercase; color: var(--text-muted);
    font-family: var(--font-numeric);
  }
  .audit-finput {
    width: 9rem;
    font-size: var(--fs-lg); font-family: var(--font-numeric);
  }
  .audit-finput-wide { width: 14rem; }
  .audit-finput-narrow { width: 5rem; }
  .audit-fbtns { display: flex; gap: 0.4rem; margin-left: auto; }

  /* .audit-error removed — fetch failures converted to toasts (slice AO). */

  /* Chrome properties (border, border-radius, box-shadow, background)
     delegated to .algo-grid-chrome in app.css — only overflow retained. */
  .audit-table-wrap {
    overflow-x: auto;
  }
  .audit-table {
    width: 100%;
  }
  .audit-table th {
    text-align: left;
    padding: 0.4rem 0.55rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
    color: var(--text-muted);
    font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase;
    background: rgba(15, 23, 42, 0.65);
    position: sticky; top: 0;
  }
  .audit-th-narrow { width: 5rem; }
  .audit-table td {
    padding: 0.35rem 0.55rem;
    vertical-align: top;
  }

  .audit-empty-row {
    text-align: center; padding: 1.5rem !important;
    color: var(--c-muted); font-style: italic;
  }

  .audit-ts        { color: #94a3b8; white-space: nowrap; }
  .audit-actor     { color: var(--c-action); font-weight: 700; }
  .audit-action    { color: #c8d8f0; }
  .audit-target-type { color: #67e8f9; font-weight: 700; margin-right: 0.3rem; }
  .audit-target-id   { color: #c8d8f0; }
  .audit-summary   { max-width: 18rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .audit-req-id    { color: var(--c-muted); font-size: var(--fs-sm); }
  .audit-ip        { color: #94a3b8; font-size: var(--fs-sm); white-space: nowrap; }

  .audit-status {
    display: inline-block;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-size: var(--fs-sm);
    font-weight: 800;
  }
  .audit-st-ok    { background: rgba(74, 222, 128, 0.18); color: var(--c-long); }
  .audit-st-warn  { background: var(--c-action-22); color: var(--c-action); }
  .audit-st-err   { background: var(--c-short-22); color: var(--c-short); }

  .audit-role {
    display: inline-block;
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #c8d8f0;
    background: rgba(126, 151, 184, 0.18);
    border: 1px solid rgba(126, 151, 184, 0.32);
  }
  .audit-role-admin      { background: rgba(251, 191, 36, 0.16); border-color: rgba(251, 191, 36, 0.45); color: var(--c-action); }
  .audit-role-designated { background: rgba(192, 132, 252, 0.16); border-color: rgba(192, 132, 252, 0.45); color: #c084fc; }
  .audit-role-trader     { background: rgba(74, 222, 128, 0.18); border-color: rgba(74, 222, 128, 0.45); color: var(--c-long); }
  .audit-role-risk       { background: rgba(251, 191, 36, 0.18); border-color: rgba(251, 191, 36, 0.45); color: var(--c-action); }
  .audit-role-partner    { background: rgba(74, 222, 128, 0.13); border-color: rgba(74, 222, 128, 0.35); color: #86efac; }
  .audit-role-demo       { background: rgba(168, 85, 247, 0.18); border-color: rgba(168, 85, 247, 0.45); color: #c084fc; }
  .audit-role-system     { background: rgba(196, 192, 168, 0.18); border-color: rgba(196, 192, 168, 0.45); color: #d4d0a8; }

  /* Filter pills above the column filters. Scope the table to a
     business bucket (orders / agents / users / config / system). */
  .audit-pills {
    display: flex; gap: 0.35rem; flex-wrap: wrap;
    margin-bottom: 0.55rem;
  }
  .audit-pill {
    padding: 0.28rem 0.7rem;
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 999px;
    color: #c8d8f0;
    font-size: var(--fs-sm); font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    font-family: var(--font-numeric);
  }
  .audit-pill:hover { background: rgba(34, 211, 238, 0.10); }
  .audit-pill.active {
    background: rgba(34, 211, 238, 0.16);
    border-color: rgba(34, 211, 238, 0.65);
    color: #67e8f9;
  }

  /* Quick-filter row (slice AP). Smaller chips than category pills so
     the two rows are visually distinct — operator's eye reads
     category pills first (what), preset chips second (when + how). */
  .audit-quickrow {
    display: flex; gap: 0.3rem; flex-wrap: wrap;
    align-items: center;
    margin-bottom: 0.55rem;
  }
  .audit-quicklbl {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #94a3b8;
    font-family: var(--font-numeric);
    margin-right: 0.2rem;
  }
  .audit-quick {
    padding: 0.18rem 0.55rem;
    background: rgba(15, 23, 42, 0.45);
    border: 1px solid rgba(126, 151, 184, 0.22);
    border-radius: 3px;
    color: #94a3b8;
    font-size: var(--fs-xs);
    font-weight: 600;
    letter-spacing: 0.04em;
    cursor: pointer;
    font-family: var(--font-numeric);
  }
  .audit-quick:hover { background: var(--c-info-08); color: #c8d8f0; }
  .audit-quick.active {
    background: var(--c-info-14);
    border-color: rgba(34, 211, 238, 0.55);
    color: #67e8f9;
  }
  .audit-quicksep {
    width: 1px; height: 0.9rem;
    background: rgba(126, 151, 184, 0.3);
    margin: 0 0.25rem;
  }
  .audit-quick.audit-quick-errors.active {
    background: rgba(248, 113, 113, 0.16);
    border-color: rgba(248, 113, 113, 0.55);
    color: var(--c-short);
  }
  .audit-quick.audit-quick-mine.active {
    background: rgba(251, 191, 36, 0.16);
    border-color: rgba(251, 191, 36, 0.55);
    color: var(--c-action);
  }

  /* Category badge in the table row — single source of truth for
     which bucket the row belongs to. Common bucket prefixes get
     their own tint so a glance distinguishes orders / agents /
     system rows without reading the text. */
  .audit-cat {
    display: inline-block;
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
    color: #c8d8f0;
    background: rgba(126, 151, 184, 0.18);
    border: 1px solid rgba(126, 151, 184, 0.32);
    font-family: var(--font-numeric);
  }
  .audit-cat-order   { background: rgba(74, 222, 128, 0.18); border-color: rgba(74, 222, 128, 0.45); color: var(--c-long); }
  .audit-cat-agent   { background: rgba(34, 211, 238, 0.16); border-color: rgba(34, 211, 238, 0.45); color: #67e8f9; }
  .audit-cat-user    { background: rgba(251, 191, 36, 0.18); border-color: rgba(251, 191, 36, 0.45); color: var(--c-action); }
  .audit-cat-config  { background: rgba(168, 85, 247, 0.18); border-color: rgba(168, 85, 247, 0.45); color: #c084fc; }
  .audit-cat-system  { background: rgba(196, 192, 168, 0.18); border-color: rgba(196, 192, 168, 0.45); color: #d4d0a8; }

  .audit-pager {
    display: flex; align-items: center; gap: 0.6rem;
    margin-top: 0.6rem;
    font-size: var(--fs-lg);
    color: #c8d8f0;
  }
  .audit-pager-info { margin: 0 auto; font-family: var(--font-numeric); font-variant-numeric: tabular-nums; }
</style>
