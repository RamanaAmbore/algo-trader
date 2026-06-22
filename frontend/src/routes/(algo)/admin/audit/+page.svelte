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
  import { nowStamp, marketAwareInterval } from '$lib/stores';
  import { fetchAuditLog } from '$lib/api';
  import { userRole, hasCap, userCaps } from '$lib/rbac';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';

  /** @typedef {{
   *   id: number, actor_user_id: number|null, actor_username: string,
   *   actor_role: string, action: string, method: string, path: string,
   *   target_type: string|null, target_id: string|null,
   *   status_code: number, summary: string|null,
   *   request_id: string, client_ip: string|null,
   *   user_agent: string|null, created_at: string
   * }} AuditRow */

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
  let fStatus      = $state('');
  let fSinceHours  = $state(72);   // default: last 3 days

  const LIMIT = 50;
  let offset = $state(0);

  async function load() {
    loading = true; error = '';
    try {
      const params = {
        limit: LIMIT,
        offset,
        actor: fActor.trim() || undefined,
        action: fAction.trim() || undefined,
        target_type: fTargetType.trim() || undefined,
        target_id: fTargetId.trim() || undefined,
        status_code: fStatus.trim() ? Number(fStatus.trim()) : undefined,
        since_hours: fSinceHours ? Number(fSinceHours) : undefined,
      };
      const r = await fetchAuditLog(params);
      rows = Array.isArray(r?.rows) ? r.rows : [];
      total = Number(r?.total ?? 0);
    } catch (e) {
      error = e?.message || 'Audit fetch failed';
    } finally {
      loading = false;
    }
  }

  function clearFilters() {
    fActor = '';
    fAction = '';
    fTargetType = '';
    fTargetId = '';
    fStatus = '';
    fSinceHours = 72;
    offset = 0;
    load();
  }

  function applyFilters() { offset = 0; load(); }
  function pageNext() { if (offset + LIMIT < total) { offset += LIMIT; load(); } }
  function pagePrev() { if (offset > 0) { offset = Math.max(0, offset - LIMIT); load(); } }

  // Gate by capability — render a friendly "no access" panel rather
  // than redirecting (the route guard 403's anyway; this is the UX
  // for an admin/risk/ops who lacks the cap because of a misconfig).
  const _canView = $derived(hasCap('view_audit', $userCaps, $userRole));

  /** @type {ReturnType<typeof marketAwareInterval> | null} */
  let _teardown = null;

  onMount(() => {
    if (!_canView) return;
    load();
    // 30s refresh during market hours — most mutation activity
    // happens during sessions. Outside hours the poll pauses.
    _teardown = marketAwareInterval(load, 30000);
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
  <span class="algo-ts">{$nowStamp}</span>
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="audit log" />
    <PageHeaderActions />
  </span>
</div>

{#if !_canView}
  <div class="audit-empty">
    <h2>Access denied</h2>
    <p>The audit log requires the <code>view_audit</code> capability
       (admin, risk, or ops role). Your current role is
       <strong>{$userRole}</strong> — contact an admin to request
       access if you need it.</p>
  </div>
{:else}

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

{#if error}
  <div class="audit-error">{error}</div>
{/if}

<div class="audit-table-wrap">
  <table class="audit-table">
    <thead>
      <tr>
        <th>Time (IST)</th>
        <th>Actor</th>
        <th>Role</th>
        <th>Action</th>
        <th>Target</th>
        <th class="audit-th-narrow">Status</th>
        <th>Summary</th>
        <th class="audit-th-narrow">Request ID</th>
        <th>Client IP</th>
      </tr>
    </thead>
    <tbody>
      {#if rows.length === 0 && !loading}
        <tr><td colspan="9" class="audit-empty-row">No audit entries match the current filters.</td></tr>
      {/if}
      {#each rows as r (r.id)}
        <tr>
          <td class="audit-ts">{_fmtTs(r.created_at)}</td>
          <td class="audit-actor">{r.actor_username || '—'}</td>
          <td><span class="audit-role audit-role-{r.actor_role || 'unknown'}">{r.actor_role || '—'}</span></td>
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
  .audit-empty {
    max-width: 32rem; margin: 4rem auto;
    padding: 1.5rem 2rem;
    background: rgba(248, 113, 113, 0.06);
    border: 1px solid rgba(248, 113, 113, 0.30);
    border-radius: 6px;
    text-align: center;
  }
  .audit-empty h2 {
    margin: 0 0 0.5rem; color: #fbbf24; font-size: 1.05rem;
  }
  .audit-empty p { color: #c8d8f0; font-size: 0.78rem; line-height: 1.5; }
  .audit-empty code {
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(34, 211, 238, 0.30);
    border-radius: 3px;
    padding: 0 0.3rem;
    color: #67e8f9;
  }

  .audit-filters {
    display: flex; flex-wrap: wrap; gap: 0.55rem 0.8rem;
    align-items: flex-end;
    padding: 0.7rem 0.9rem;
    background: rgba(15, 23, 42, 0.45);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
    margin-bottom: 0.7rem;
  }
  .audit-flbl {
    display: flex; flex-direction: column; gap: 0.15rem;
    font-size: 0.55rem; font-weight: 700; letter-spacing: 0.05em;
    text-transform: uppercase; color: #a3b9d0;
    font-family: ui-monospace, monospace;
  }
  .audit-finput {
    width: 9rem;
    font-size: 0.7rem; font-family: ui-monospace, monospace;
  }
  .audit-finput-wide { width: 14rem; }
  .audit-finput-narrow { width: 5rem; }
  .audit-fbtns { display: flex; gap: 0.4rem; margin-left: auto; }

  .audit-error {
    padding: 0.6rem 0.9rem;
    background: rgba(248, 113, 113, 0.10);
    border: 1px solid rgba(248, 113, 113, 0.40);
    border-radius: 4px;
    color: #fca5a5; font-size: 0.7rem;
    margin-bottom: 0.7rem;
  }

  .audit-table-wrap {
    overflow-x: auto;
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 6px;
  }
  .audit-table {
    width: 100%; border-collapse: collapse;
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
  }
  .audit-table th {
    text-align: left;
    padding: 0.4rem 0.55rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
    color: #a3b9d0;
    font-size: 0.55rem;
    font-weight: 800; letter-spacing: 0.06em;
    text-transform: uppercase;
    background: rgba(15, 23, 42, 0.65);
    position: sticky; top: 0;
  }
  .audit-th-narrow { width: 5rem; }
  .audit-table td {
    padding: 0.35rem 0.55rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.10);
    color: #c8d8f0;
    vertical-align: top;
  }
  .audit-table tbody tr:nth-of-type(even) td {
    background: rgba(34, 47, 75, 0.30);
  }
  .audit-table tbody tr:hover td {
    background: rgba(34, 211, 238, 0.08);
  }

  .audit-empty-row {
    text-align: center; padding: 1.5rem !important;
    color: #7e97b8; font-style: italic;
  }

  .audit-ts        { color: #94a3b8; white-space: nowrap; }
  .audit-actor     { color: #fbbf24; font-weight: 700; }
  .audit-action    { color: #c8d8f0; }
  .audit-target-type { color: #67e8f9; font-weight: 700; margin-right: 0.3rem; }
  .audit-target-id   { color: #c8d8f0; }
  .audit-summary   { max-width: 18rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .audit-req-id    { color: #7e97b8; font-size: 0.6rem; }
  .audit-ip        { color: #94a3b8; font-size: 0.6rem; white-space: nowrap; }

  .audit-status {
    display: inline-block;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    font-size: 0.6rem;
    font-weight: 800;
  }
  .audit-st-ok    { background: rgba(74, 222, 128, 0.18); color: #4ade80; }
  .audit-st-warn  { background: rgba(251, 191, 36, 0.22); color: #fbbf24; }
  .audit-st-err   { background: rgba(248, 113, 113, 0.22); color: #f87171; }

  .audit-role {
    display: inline-block;
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    font-size: 0.55rem;
    font-weight: 800;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #c8d8f0;
    background: rgba(126, 151, 184, 0.18);
    border: 1px solid rgba(126, 151, 184, 0.32);
  }
  .audit-role-admin      { background: rgba(248, 113, 113, 0.16); border-color: rgba(248, 113, 113, 0.45); color: #f87171; }
  .audit-role-designated { background: rgba(248, 113, 113, 0.16); border-color: rgba(248, 113, 113, 0.45); color: #f87171; }
  .audit-role-trader     { background: rgba(74, 222, 128, 0.18); border-color: rgba(74, 222, 128, 0.45); color: #4ade80; }
  .audit-role-risk       { background: rgba(251, 191, 36, 0.18); border-color: rgba(251, 191, 36, 0.45); color: #fbbf24; }
  .audit-role-ops        { background: rgba(34, 211, 238, 0.16); border-color: rgba(34, 211, 238, 0.45); color: #67e8f9; }
  .audit-role-demo       { background: rgba(168, 85, 247, 0.18); border-color: rgba(168, 85, 247, 0.45); color: #c084fc; }

  .audit-pager {
    display: flex; align-items: center; gap: 0.6rem;
    margin-top: 0.6rem;
    font-size: 0.7rem;
    color: #c8d8f0;
  }
  .audit-pager-info { margin: 0 auto; font-family: ui-monospace, monospace; }
</style>
