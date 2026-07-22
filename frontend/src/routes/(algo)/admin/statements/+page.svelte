<!--
  /admin/statements — monthly auto-email audit + manual send.

  Cross-LP view of every monthly_statements row plus the eligible
  LPs that don't yet have a row for the chosen period. Lets the
  operator inspect failures, retry, manually send before the bg
  task's 02:00 IST wake, or scan "what got sent last month?"
  without per-LP drilling.

  Cap: manage_investor_tokens (admin-only — same as the per-LP
  Portal modal on /admin).
-->
<script>
  import { onMount } from 'svelte';
  import { formatDateIST } from '$lib/dateFormat.js';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import { userRole, userCaps, hasCap } from '$lib/rbac';
  import {
    fetchStatementAudit, sendStatementNow, deleteStatementRow,
  } from '$lib/api';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import ConfirmModal from '$lib/ConfirmModal.svelte';
  import Select from '$lib/Select.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

  /** @type {{
   *   ask: (opts: any) => Promise<boolean>,
   *   prompt: (opts: any) => Promise<string|null>,
   * } | null} */
  let confirmRef = $state(null);

  /** @typedef {{
   *   id:number|null, user_id:number, username:string, display_name:string,
   *   email:string|null, share_pct:number,
   *   period_year:number, period_month:number, status:string,
   *   generated_at:string|null, sent_at:string|null,
   *   recipients:string[], pdf_size_bytes:number|null,
   *   error:string|null
   * }} StmtRow */

  /** @type {StmtRow[]} */
  let rows = $state([]);
  let counts = $state({ sent: 0, failed: 0, pending: 0 });
  let periodYear  = $state(/** @type {number} */ (0));
  let periodMonth = $state(/** @type {number} */ (0));
  let loading = $state(false);
  let error   = $state('');
  let busyRow = $state(/** @type {number|null} */ (null));

  /** Filter pill — 'all' | 'pending' | 'failed' | 'sent'. */
  let filter = $state(/** @type {'all'|'pending'|'failed'|'sent'} */ ('all'));

  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values.
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const canManage = $derived(hasCap('manage_investor_tokens', _caps, _role));

  function _recentMonths(/** @type {number} */ n) {
    const out = [];
    const now = new Date();
    let y = now.getFullYear();
    let m = now.getMonth();   // 0-indexed = last month already
    if (m === 0) { m = 12; y -= 1; }
    for (let i = 0; i < n; i++) {
      out.push({
        year: y, month: m,
        value: `${y}-${m}`,
        label: formatDateIST(new Date(y, m - 1, 1), { month: 'short', year: 'numeric' }),
      });
      m -= 1; if (m < 1) { m = 12; y -= 1; }
    }
    return out;
  }
  const _months = $derived(_recentMonths(12));
  /** Bound dropdown value as "YYYY-M" string. */
  let selectedPeriod = $state('');

  async function load() {
    loading = true; error = '';
    try {
      const [y, m] = selectedPeriod
        ? selectedPeriod.split('-').map(Number)
        : [0, 0];
      const r = await fetchStatementAudit({ year: y, month: m });
      rows        = r.rows ?? [];
      counts      = r.counts ?? { sent: 0, failed: 0, pending: 0 };
      periodYear  = r.period_year;
      periodMonth = r.period_month;
      // First load — sync the dropdown to whatever the backend
      // defaulted to (prior month).
      if (!selectedPeriod) selectedPeriod = `${periodYear}-${periodMonth}`;
    } catch (e) { error = e?.message || 'Load failed'; }
    finally { loading = false; }
  }

  onMount(load);
  $effect(() => {
    if (selectedPeriod) load();
  });

  const filteredRows = $derived(
    filter === 'all' ? rows : rows.filter(r => r.status === filter),
  );

  async function sendNow(/** @type {StmtRow} */ row) {
    if (!canManage || busyRow != null) return;
    if (!await confirmRef.ask({
      title: 'Send monthly statement?',
      body: `Send the ${_periodLabel()} statement to ${row.display_name} (${row.email || '—'}). The PDF will be generated and emailed now; the operator does not need to wait for the next 02:00 IST wake.`,
      confirmLabel: 'Send now',
    })) return;
    busyRow = row.user_id; error = '';
    try {
      const r = await sendStatementNow({
        user_id: row.user_id,
        year: row.period_year,
        month: row.period_month,
      });
      if (r.status === 'failed' && r.error) {
        toast.error(`Send failed: ${r.error}`);
      } else {
        toast.success(`Statement sent: ${row.display_name} — ${_periodLabel()}`);
      }
      await load();
    } catch (e) { toast.error(`Send failed: ${e?.message || 'unknown error'}`); }
    finally { busyRow = null; }
  }

  async function deleteRow(/** @type {StmtRow} */ row) {
    if (!canManage || row.id == null || busyRow != null) return;
    if (!await confirmRef.ask({
      title: 'Delete audit row?',
      body: `Clear the ${_periodLabel()} audit row for ${row.display_name}. The next bg wake (or a manual send) will re-process the LP. Use this to retry after fixing an error (e.g. invalid email).`,
      confirmLabel: 'Delete',
      kind: 'danger',
    })) return;
    busyRow = row.user_id; error = '';
    try {
      await deleteStatementRow(row.id);
      toast.success(`Deleted audit row: ${row.display_name} — ${_periodLabel()}`);
      await load();
    } catch (e) { toast.error(`Delete failed: ${e?.message || 'unknown error'}`); }
    finally { busyRow = null; }
  }

  async function previewPdf(/** @type {StmtRow} */ row) {
    error = '';
    try {
      const tok = localStorage.getItem('ramboq.token') || '';
      const url = `/api/admin/users/${row.user_id}/statement/${row.period_year}/${row.period_month}`;
      const res = await fetch(url, {
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || `Preview failed (${res.status})`);
      }
      const blob = await res.blob();
      const dlUrl = URL.createObjectURL(blob);
      window.open(dlUrl, '_blank');
      setTimeout(() => URL.revokeObjectURL(dlUrl), 60_000);
    } catch (e) { toast.error(`Preview failed: ${e?.message || 'unknown error'}`); }
  }

  function _periodLabel() {
    if (!periodYear || !periodMonth) return '—';
    return formatDateIST(new Date(periodYear, periodMonth - 1, 1), { month: 'short', year: 'numeric' });
  }
  function _fmtBytes(/** @type {number|null} */ b) {
    if (b == null) return '—';
    if (b < 1024) return `${b} B`;
    return `${(b / 1024).toFixed(1)} KB`;
  }
  function _fmtTs(/** @type {string|null} */ iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString('en-IN', {
        day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
      });
    } catch { return iso; }
  }
</script>

<svelte:head>
  <title>Monthly statements · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">Monthly statements</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="statements" />
    <PageHeaderActions />
  </span>
</div>

<section class="ms-controls">
  <label class="ms-field">
    <span class="ms-field-lbl">Period</span>
    <Select
      bind:value={selectedPeriod}
      options={_months}
      ariaLabel="Statement period"
      placeholder="Pick a period"
    />
  </label>

  <div class="ms-filter-pills">
    <button class="ms-pill" class:active={filter === 'all'}     onclick={() => filter = 'all'}>
      All <span class="ms-pill-n">{rows.length}</span>
    </button>
    <button class="ms-pill ms-pill-pending" class:active={filter === 'pending'} onclick={() => filter = 'pending'}>
      Pending <span class="ms-pill-n">{counts.pending}</span>
    </button>
    <button class="ms-pill ms-pill-failed" class:active={filter === 'failed'} onclick={() => filter = 'failed'}>
      Failed <span class="ms-pill-n">{counts.failed}</span>
    </button>
    <button class="ms-pill ms-pill-sent" class:active={filter === 'sent'} onclick={() => filter = 'sent'}>
      Sent <span class="ms-pill-n">{counts.sent}</span>
    </button>
  </div>
</section>

<section class="algo-table-wrap">
  {#if loading}
    <LoadingSkeleton variant="grid-row" rows={4} height="1.6rem" />
  {:else if filteredRows.length === 0}
    <EmptyState
      title="No statements"
      hint="No rows for {_periodLabel()} matching this filter."
      icon="inbox"
    />
  {:else}
    <table class="algo-table ms-table content-fade-in">
      <thead>
        <tr>
          <th>LP</th>
          <th>Email</th>
          <th class="th-num">Share %</th>
          <th>Status</th>
          <th>Sent at</th>
          <th class="th-num">PDF</th>
          <th>Error</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each filteredRows as r (`${r.user_id}-${r.period_year}-${r.period_month}`)}
          <tr class="ms-row" class:row-pending={r.status === 'pending'}
              class:row-failed={r.status === 'failed'}>
            <td>
              <div class="ms-lp-name">{r.display_name}</div>
              <div class="ms-lp-sub">{r.username}</div>
            </td>
            <td class="td-mono">{r.email || '—'}</td>
            <td class="algo-table-num">{r.share_pct.toFixed(2)}%</td>
            <td>
              {#if r.status === 'sent'}
                <span class="ms-pill-status ms-pill-sent">Sent</span>
              {:else if r.status === 'failed'}
                <span class="ms-pill-status ms-pill-failed">Failed</span>
              {:else}
                <span class="ms-pill-status ms-pill-pending">Pending</span>
              {/if}
            </td>
            <td class="td-mono">{_fmtTs(r.sent_at)}</td>
            <td class="algo-table-num">{_fmtBytes(r.pdf_size_bytes)}</td>
            <td class="ms-error-cell" title={r.error || ''}>{r.error || ''}</td>
            <td class="td-actions">
              {#if canManage}
                {#if r.status === 'pending' || r.status === 'failed'}
                  <button class="btn-primary text-[0.6rem] py-0.5 px-1.5"
                          disabled={busyRow === r.user_id}
                          onclick={() => sendNow(r)}>
                    {busyRow === r.user_id ? '…' : 'Send'}
                  </button>
                {/if}
                {#if r.id != null}
                  <button class="btn-secondary text-[0.6rem] py-0.5 px-1.5 border-red-400/50 text-red-300"
                          disabled={busyRow === r.user_id}
                          onclick={() => deleteRow(r)}>Delete</button>
                {/if}
                <button class="btn-secondary text-[0.6rem] py-0.5 px-1.5 border-cyan-400/50 text-cyan-300"
                        onclick={() => previewPdf(r)}>Preview</button>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</section>

<ConfirmModal bind:this={confirmRef} />

<style>
  /* .ms-error removed — action failures converted to toasts (slice AO). */

  .ms-controls {
    display: flex; gap: 0.6rem; align-items: flex-end; flex-wrap: wrap;
    margin-bottom: 0.7rem;
  }
  .ms-field { display: flex; flex-direction: column; gap: 0.2rem; }
  .ms-field-lbl {
    font-size: var(--fs-xs); color: var(--c-muted); letter-spacing: 0.06em;
    text-transform: uppercase; font-weight: 700;
    font-family: var(--font-numeric);
  }
  .ms-filter-pills {
    display: flex; gap: 0.4rem; flex-wrap: wrap;
  }
  .ms-pill {
    padding: 0.3rem 0.7rem;
    background: rgba(15, 23, 42, 0.50);
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 999px;
    color: #c8d8f0;
    font-size: var(--fs-md); font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: background 120ms, border-color 120ms;
    font-family: var(--font-numeric);
  }
  .ms-pill:hover { background: rgba(34, 211, 238, 0.10); }
  .ms-pill.active { border-color: rgba(34, 211, 238, 0.65); color: #67e8f9; }
  .ms-pill.ms-pill-pending.active { border-color: rgba(251, 191, 36, 0.65); color: var(--c-action); }
  .ms-pill.ms-pill-failed.active  { border-color: rgba(248, 113, 113, 0.65); color: #fca5a5; }
  .ms-pill.ms-pill-sent.active    { border-color: rgba( 74, 222, 128, 0.65); color: var(--c-long); }
  .ms-pill-n {
    margin-left: 0.3rem;
    opacity: 0.75;
    font-weight: 800;
  }

  .algo-table-wrap {
    overflow-x: auto;
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-radius: 4px;
    background: rgba(15, 23, 42, 0.30);
  }
  /* .ms-table width:100% removed — algo-table global provides it. */
  .ms-table th {
    text-align: left;
    padding: 0.3rem 0.55rem;
    background: rgba(15, 23, 42, 0.65);
    color: var(--text-muted);
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
  }
  .ms-table th.th-num { text-align: right; }
  .ms-table td {
    padding: 0.3rem 0.55rem;
  }
  /* .ms-table td.td-num removed — renamed to algo-table-num (handled globally). */
  .ms-table td.td-mono   { font-family: var(--font-numeric); font-size: var(--fs-md); }
  .ms-table td.td-actions { text-align: right; white-space: nowrap; }
  .ms-table td.td-actions :global(button) { margin-left: 0.25rem; }
  .ms-lp-name { font-weight: 700; }
  .ms-lp-sub  { font-size: var(--fs-sm); color: var(--c-muted); font-family: var(--font-numeric); }

  .ms-row.row-pending td { background: rgba(251, 191, 36, 0.05); }
  .ms-row.row-failed td  { background: rgba(248, 113, 113, 0.05); }

  .ms-error-cell {
    max-width: 14rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #fca5a5;
    font-size: var(--fs-md);
    font-family: var(--font-numeric);
  }

  .ms-pill-status {
    display: inline-block;
    padding: 0.1rem 0.5rem;
    font-size: var(--fs-xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-radius: 3px;
    font-family: var(--font-numeric);
  }
  .ms-pill-status.ms-pill-sent {
    background: rgba(74, 222, 128, 0.15); color: var(--c-long);
    border: 1px solid rgba(74, 222, 128, 0.4);
  }
  .ms-pill-status.ms-pill-failed {
    background: rgba(248, 113, 113, 0.15); color: #fca5a5;
    border: 1px solid rgba(248, 113, 113, 0.4);
  }
  .ms-pill-status.ms-pill-pending {
    background: rgba(251, 191, 36, 0.15); color: var(--c-action);
    border: 1px solid rgba(251, 191, 36, 0.4);
  }
</style>
