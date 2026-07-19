<!--
  /admin/history — multi-day forensic surface for orders, trades, and
  funds across all loaded accounts.

  Three tabs:
    Orders  — every algo_orders row (full history, every mode)
    Trades  — every daily_book trade snapshot (since the table existed)
    Funds   — per-account margins ledger (tracking started Jun 2026)

  Cap-gated by view_audit (admin / risk / ops) — same gate as the audit
  log. Page is read-only by design; mutations go through their normal
  routes.
-->
<script>
  import { onMount } from 'svelte';
  import AlgoTimestamp from '$lib/AlgoTimestamp.svelte';
  import { userRole, userCaps, userCapsReady, hasCap } from '$lib/rbac';
  import {
    fetchHistoryOrders, fetchHistoryTrades, fetchHistoryFunds,
    backfillHistoryFunds,
  } from '$lib/api';
  import RefreshButton from '$lib/RefreshButton.svelte';
  import PageHeaderActions from '$lib/PageHeaderActions.svelte';
  import AlgoTabs from '$lib/AlgoTabs.svelte';
  import LoadingSkeleton from '$lib/LoadingSkeleton.svelte';
  import EmptyState from '$lib/EmptyState.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';

  /** @typedef {{
   *   id:number, created_at:string, account:string, symbol:string,
   *   exchange:string, transaction_type:string, quantity:number,
   *   filled_quantity:number, initial_price:number|null,
   *   fill_price:number|null, slippage:number|null, status:string,
   *   mode:string, engine:string, broker_order_id:string|null,
   *   request_id:string|null, detail:string|null
   * }} OrderRow */
  /** @typedef {{
   *   date:string, account:string, segment:string, symbol:string,
   *   exchange:string|null, qty:number, avg_cost:number|null,
   *   notional:number|null, captured_at:string
   * }} TradeRow */
  /** @typedef {{
   *   date:string, account:string, segment:string,
   *   cash_available:number|null, opening_balance:number|null,
   *   debits_today:number, realised_m2m:number|null,
   *   net:number|null, cash_delta:number|null
   * }} FundsRow */

  // Tab state — 'orders' | 'trades' | 'funds'.
  let tab = $state(/** @type {'orders'|'trades'|'funds'} */ ('orders'));

  // Filters — shared across all three tabs.
  function _today() { return new Date().toISOString().slice(0, 10); }
  function _daysAgo(/** @type {number} */ d) {
    const x = new Date();
    x.setDate(x.getDate() - d);
    return x.toISOString().slice(0, 10);
  }
  let fFromDate = $state(_daysAgo(30));
  let fToDate   = $state(_today());
  let fAccounts = $state('');
  let fSymbols  = $state('');
  let fStatus   = $state('');
  let fMode     = $state('');

  /** @type {OrderRow[]} */  let orderRows = $state([]);
  /** @type {TradeRow[]} */  let tradeRows = $state([]);
  /** @type {FundsRow[]} */  let fundsRows = $state([]);

  let total = $state(0);
  let orderCounts = $state(/** @type {Record<string, number>} */ ({}));
  let tradeNotional = $state(0);
  let fundsEarliest = $state(/** @type {string|null} */ (null));

  let loading = $state(false);
  let error   = $state('');

  const LIMIT = 50;
  let offset = $state(0);

  async function load() {
    loading = true; error = '';
    try {
      const params = {
        from_date: fFromDate,
        to_date:   fToDate,
        accounts:  fAccounts.trim() || undefined,
        symbols:   fSymbols.trim()  || undefined,
        limit:     LIMIT, offset,
      };
      if (tab === 'orders') {
        if (fStatus.trim()) params.status = fStatus.trim().toUpperCase();
        if (fMode.trim())   params.mode   = fMode.trim().toLowerCase();
        const r = await fetchHistoryOrders(params);
        orderRows   = r?.rows  ?? [];
        total       = r?.total ?? 0;
        orderCounts = r?.counts ?? {};
      } else if (tab === 'trades') {
        const r = await fetchHistoryTrades(params);
        tradeRows = r?.rows  ?? [];
        total     = r?.total ?? 0;
        tradeNotional = Number(r?.summary?.total_notional ?? 0);
      } else {
        const r = await fetchHistoryFunds(params);
        fundsRows     = r?.rows ?? [];
        total         = r?.total ?? 0;
        fundsEarliest = r?.earliest_date ?? null;
      }
    } catch (e) {
      error = e?.message || 'Load failed';
      toast.error(`History load failed: ${e?.message || 'unknown error'}`);
    }
    finally { loading = false; }
  }

  function setTab(/** @type {'orders'|'trades'|'funds'} */ t) {
    if (t === tab) return;
    tab = t;
    offset = 0;
    load();
  }
  function applyFilters() { offset = 0; load(); }
  function pageNext() { if (offset + LIMIT < total) { offset += LIMIT; load(); } }
  function pagePrev() { if (offset > 0) { offset = Math.max(0, offset - LIMIT); load(); } }

  // Bridge legacy stores into Svelte-5 $state so $derived doesn't
  // stale-cache the initial [] / 'partner' boot values.
  let _caps = $state(/** @type {string[]} */ ([]));
  let _role = $state(/** @type {string} */ ('partner'));
  $effect(() => { _caps = $userCaps; });
  $effect(() => { _role = $userRole; });
  const _canView = $derived(hasCap('view_audit', _caps, _role));
  // Use $effect not onMount — _canView starts false on first paint
  // (while /whoami resolves, $userCaps is empty so the fallback
  // matrix returns false for 'demo'). onMount runs once and never
  // re-checks; the result was the operator briefly seeing "Access
  // denied" until whoami landed but never actually loading data.
  // $effect re-runs whenever _canView flips, so first true value
  // triggers the load.
  let _loadedOnce = false;
  $effect(() => {
    if (_canView && !_loadedOnce) {
      _loadedOnce = true;
      load();
    }
  });

  // Backfill state for the Dhan ledger pull.
  let bfBusy = $state(false);
  let bfAccount = $state('');
  async function runBackfill() {
    if (bfBusy) return;
    if (!bfAccount.trim()) {
      toast.warning('Pick an account first.');
      return;
    }
    bfBusy = true;
    try {
      const r = await backfillHistoryFunds({
        account:   bfAccount.trim(),
        from_date: fFromDate,
        to_date:   fToDate,
      });
      toast.success(`Backfill: +${r.rows_added} added, ${r.rows_skipped} skipped (${r.broker_id})`);
      if (r.rows_added > 0) await load();
    } catch (e) {
      toast.error(`Backfill failed: ${e?.message || 'unknown error'}`);
    } finally { bfBusy = false; }
  }

  /** Build the /admin/audit URL pre-filtered for a row's audit
   *  trail. Returns null when the row doesn't carry a request_id
   *  (legacy rows from before the column existed). */
  function _auditHref(/** @type {OrderRow} */ r) {
    if (!r.request_id) return null;
    return `/admin/audit?request_id=${encodeURIComponent(r.request_id)}`;
  }

  // Kept local: diverges from aggCompact in three ways — (1) '₹' prefix,
  // (2) 'Cr' suffix (aggCompact uses 'C'), (3) en-IN grouping for values
  // below 1L instead of the K-compact form. History grid uses 'Cr' to
  // match the operator's convention elsewhere on the history surface.
  function _fmtInr(/** @type {number|null|undefined} */ v) {
    if (v == null || !isFinite(v)) return '—';
    const abs = Math.abs(v);
    const sign = v < 0 ? '-' : '';
    if (abs >= 1e7) return `${sign}₹${(abs/1e7).toFixed(2)}Cr`;
    if (abs >= 1e5) return `${sign}₹${(abs/1e5).toFixed(2)}L`;
    return `${sign}₹${Math.round(abs).toLocaleString('en-IN')}`;
  }
  function _fmtPrice(/** @type {number|null|undefined} */ v) {
    if (v == null) return '—';
    return Number(v).toFixed(2);
  }
  function _fmtDate(/** @type {string} */ iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleDateString('en-IN', {
        day: '2-digit', month: 'short', year: 'numeric',
      });
    } catch { return iso.slice(0, 10); }
  }
  function _fmtTs(/** @type {string} */ iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString('en-IN', {
        timeZone: 'Asia/Kolkata',
        day: '2-digit', month: 'short',
        hour: '2-digit', minute: '2-digit', hour12: false,
      });
    } catch { return iso; }
  }
  function _statusClass(/** @type {string} */ s) {
    s = (s || '').toUpperCase();
    if (s === 'FILLED'   || s === 'COMPLETE')  return 'st-ok';
    if (s === 'REJECTED' || s === 'UNFILLED')  return 'st-err';
    if (s === 'CANCELLED' || s === 'EXPIRED')  return 'st-warn';
    return 'st-pending';
  }
</script>

<svelte:head>
  <title>History · RamboQuant</title>
</svelte:head>

<div class="page-header">
  <span class="algo-title-group">
    <h1 class="page-title-chip">History</h1>
  </span>
  <AlgoTimestamp />
  <span class="ml-auto"></span>
  <span class="page-header-actions">
    <RefreshButton onClick={load} loading={loading} label="history" />
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
      Historical orders / trades / funds require the <code>view_audit</code>
      capability. Your current role is <strong>{$userRole}</strong> —
      contact an admin if you need access.
    {/snippet}
  </EmptyState>
{:else}

<AlgoTabs
  value={tab}
  onChange={setTab}
  tabs={[
    { id: 'orders', label: 'Orders', badge: tab === 'orders' ? total : undefined },
    { id: 'trades', label: 'Trades', badge: tab === 'trades' ? total : undefined },
    { id: 'funds',  label: 'Funds',  badge: tab === 'funds'  ? total : undefined },
  ]}
/>

<div class="hist-filters">
  <label class="hist-flbl">From
    <input type="date" bind:value={fFromDate} class="field-input hist-finput" />
  </label>
  <label class="hist-flbl">To
    <input type="date" bind:value={fToDate}   class="field-input hist-finput" />
  </label>
  <label class="hist-flbl">Accounts
    <input bind:value={fAccounts} placeholder="ZG0790,DH3747"
           class="field-input hist-finput" />
  </label>
  {#if tab !== 'funds'}
    <label class="hist-flbl">Symbols
      <input bind:value={fSymbols} placeholder="NIFTY,GOLDM"
             class="field-input hist-finput" />
    </label>
  {/if}
  {#if tab === 'orders'}
    <label class="hist-flbl">Status
      <input bind:value={fStatus} placeholder="FILLED / OPEN / …"
             class="field-input hist-finput" />
    </label>
    <label class="hist-flbl">Mode
      <input bind:value={fMode} placeholder="live / paper / sim"
             class="field-input hist-finput" />
    </label>
  {/if}
  <button class="btn-primary" onclick={applyFilters}>Apply</button>
</div>

{#if loading}
  <LoadingSkeleton variant="grid-row" rows={7} height="1.3rem" />
{/if}

{#if tab === 'orders'}
  <div class="hist-summary">
    {#each Object.entries(orderCounts) as [st, c]}
      <span class="hist-pill {_statusClass(st)}">{st}: {c}</span>
    {/each}
  </div>
  {#if !loading && orderRows.length === 0}
    <EmptyState title="No orders match" hint="Try adjusting the date range or clearing filters." icon="search" />
  {/if}
  <div class="hist-table-wrap algo-grid-chrome" style:display={!loading && orderRows.length === 0 ? 'none' : undefined}>
    <table class="algo-table hist-table">
      <thead>
        <tr>
          <th>Time (IST)</th>
          <th>Account</th>
          <th>Symbol</th>
          <th>Side</th>
          <th class="th-num">Qty</th>
          <th class="th-num">Filled</th>
          <th class="th-num">Limit</th>
          <th class="th-num">Fill</th>
          <th class="th-num">Slip</th>
          <th>Status</th>
          <th>Mode</th>
          <th>Broker ID</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each orderRows as r (r.id)}
          <tr>
            <td class="td-mono">{_fmtTs(r.created_at)}</td>
            <td class="td-mono">{r.account}</td>
            <td class="td-mono">{r.symbol}</td>
            <td><span class="hist-side hist-side-{r.transaction_type?.toLowerCase()}">{r.transaction_type}</span></td>
            <td class="algo-table-num">{r.quantity}</td>
            <td class="algo-table-num">{r.filled_quantity}</td>
            <td class="algo-table-num">{_fmtPrice(r.initial_price)}</td>
            <td class="algo-table-num">{_fmtPrice(r.fill_price)}</td>
            <td class="algo-table-num">{_fmtPrice(r.slippage)}</td>
            <td><span class="hist-pill {_statusClass(r.status)}">{r.status}</span></td>
            <td class="td-mono">{r.mode}</td>
            <td class="td-mono" title={r.broker_order_id ?? ''}>{r.broker_order_id ?? '—'}</td>
            <td>
              {#if _auditHref(r)}
                <a class="hist-audit-link"
                   href={_auditHref(r)}
                   title="Open the audit log filtered to this order's request_id">
                  Audit ↗
                </a>
              {:else}
                <span class="hist-audit-none" title="Legacy row — no request_id was captured at insert">—</span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{:else if tab === 'trades'}
  <div class="hist-summary">
    <span class="hist-pill hist-pill-info">
      Total notional: {_fmtInr(tradeNotional)}
    </span>
  </div>
  {#if !loading && tradeRows.length === 0}
    <EmptyState title="No trades match" hint="Try adjusting the date range or filters." icon="search" />
  {/if}
  <div class="hist-table-wrap algo-grid-chrome" style:display={!loading && tradeRows.length === 0 ? 'none' : undefined}>
    <table class="algo-table hist-table">
      <thead>
        <tr>
          <th>Date</th>
          <th>Account</th>
          <th>Segment</th>
          <th>Symbol</th>
          <th>Exchange</th>
          <th class="th-num">Qty</th>
          <th class="th-num">Avg cost</th>
          <th class="th-num">Notional</th>
        </tr>
      </thead>
      <tbody>
        {#each tradeRows as r, i (`${r.date}|${r.account}|${r.symbol}|${i}`)}
          <tr>
            <td class="td-mono">{_fmtDate(r.date)}</td>
            <td class="td-mono">{r.account}</td>
            <td class="td-mono">{r.segment}</td>
            <td class="td-mono">{r.symbol}</td>
            <td class="td-mono">{r.exchange ?? '—'}</td>
            <td class="algo-table-num">{r.qty}</td>
            <td class="algo-table-num">{_fmtPrice(r.avg_cost)}</td>
            <td class="algo-table-num">{_fmtInr(r.notional)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{:else}
  <div class="hist-summary">
    {#if fundsEarliest}
      <span class="hist-pill hist-pill-info">
        Tracking started {_fmtDate(fundsEarliest)}
      </span>
    {:else}
      <span class="hist-pill hist-pill-warn">
        No funds snapshots yet — the daily 15:35 IST capture writes the
        first row tonight.
      </span>
    {/if}
  </div>

  <!-- Backfill — broker-side ledger pull for pre-deploy dates. Kite
       has no programmatic ledger; Dhan does. UI exposes the button
       universally + surfaces the broker-by-broker 501 when the
       adapter isn't wired yet. -->
  <div class="hist-backfill">
    <label class="hist-flbl">Backfill account
      <input bind:value={bfAccount} placeholder="DH3747"
             class="field-input hist-finput" />
    </label>
    <button class="btn-secondary hist-backfill-btn"
            disabled={bfBusy} onclick={runBackfill}>
      {bfBusy ? 'Backfilling…' : 'Pull ledger ↓'}
    </button>
  </div>
  {#if !loading && fundsRows.length === 0}
    <EmptyState title="No funds rows" hint="No data in this date range. Use Pull ledger to backfill Dhan, or wait for the daily capture." icon="chart" />
  {/if}
  <div class="hist-table-wrap algo-grid-chrome" style:display={!loading && fundsRows.length === 0 ? 'none' : undefined}>
    <table class="algo-table hist-table">
      <thead>
        <tr>
          <th>Date</th>
          <th>Account</th>
          <th>Segment</th>
          <th class="th-num">Cash avail</th>
          <th class="th-num" title="Day-over-day Δ on cash_available within this (account, segment) series">Δ vs prior</th>
          <th class="th-num">Opening bal</th>
          <th class="th-num">Debits today</th>
          <th class="th-num">Realised M2M</th>
          <th class="th-num">Net</th>
        </tr>
      </thead>
      <tbody>
        {#each fundsRows as r, i (`${r.date}|${r.account}|${r.segment}|${i}`)}
          <tr>
            <td class="td-mono">{_fmtDate(r.date)}</td>
            <td class="td-mono">{r.account}</td>
            <td class="td-mono">{r.segment}</td>
            <td class="algo-table-num">{_fmtInr(r.cash_available)}</td>
            <td class="algo-table-num {(r.cash_delta ?? 0) > 0 ? 'cell-pos' : (r.cash_delta ?? 0) < 0 ? 'cell-neg' : ''}">
              {r.cash_delta == null ? '—'
                : (r.cash_delta > 0 ? '+' : '') + _fmtInr(r.cash_delta)}
            </td>
            <td class="algo-table-num">{_fmtInr(r.opening_balance)}</td>
            <td class="algo-table-num">{_fmtInr(r.debits_today)}</td>
            <td class="algo-table-num {(r.realised_m2m ?? 0) > 0 ? 'cell-pos' : (r.realised_m2m ?? 0) < 0 ? 'cell-neg' : ''}">{_fmtInr(r.realised_m2m)}</td>
            <td class="algo-table-num">{_fmtInr(r.net)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

{#if tab !== 'funds' && total > LIMIT}
  <div class="hist-pager">
    <button class="btn-secondary text-[0.65rem]" disabled={offset === 0} onclick={pagePrev}>← Prev</button>
    <span class="text-[0.65rem]">Showing {offset + 1}–{Math.min(offset + LIMIT, total)} of {total}</span>
    <button class="btn-secondary text-[0.65rem]" disabled={offset + LIMIT >= total} onclick={pageNext}>Next →</button>
  </div>
{/if}

{/if}

<style>
  /* .hist-error + .hist-empty rules removed — errors converted to
     toasts (slice AO); access-denied panel migrated to EmptyState
     component (slice AE). */

  .hist-filters {
    display: flex; flex-wrap: wrap; gap: 0.55rem;
    align-items: flex-end;
    margin-bottom: 0.7rem;
  }
  .hist-flbl {
    display: flex; flex-direction: column; gap: 0.18rem;
    font-size: var(--fs-xs); font-weight: 700;
    letter-spacing: 0.06em; text-transform: uppercase;
    color: #94a3b8;
    font-family: var(--font-numeric);
  }
  .hist-finput {
    width: 9rem;
    padding: 0.32rem 0.5rem;
    background: rgba(15, 23, 42, 0.65);
    border: 1px solid rgba(126, 151, 184, 0.30);
    border-radius: 4px;
    color: #c8d8f0;
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
  }

  .hist-summary {
    display: flex; gap: 0.4rem; flex-wrap: wrap;
    margin-bottom: 0.5rem;
  }
  .hist-pill {
    display: inline-block;
    padding: 0.18rem 0.55rem;
    border-radius: 999px;
    font-family: var(--font-numeric);
    font-size: var(--fs-sm); font-weight: 700;
    letter-spacing: 0.05em;
    background: rgba(15, 23, 42, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.30);
    color: #c8d8f0;
  }
  .hist-pill.st-ok      { background: rgba(74, 222, 128, 0.15);  border-color: rgba(74,222,128,0.5);  color: var(--c-long); }
  .hist-pill.st-err     { background: rgba(248, 113, 113, 0.15); border-color: rgba(248,113,113,0.5); color: #fca5a5; }
  .hist-pill.st-warn    { background: rgba(251, 191, 36, 0.15);  border-color: rgba(251,191,36,0.5);  color: var(--c-action); }
  .hist-pill.st-pending { background: rgba(34, 211, 238, 0.13);  border-color: rgba(34,211,238,0.45); color: #67e8f9; }
  .hist-pill-info       { background: rgba(34, 211, 238, 0.13);  border-color: rgba(34,211,238,0.45); color: #67e8f9; }
  .hist-pill-warn       { background: rgba(251, 191, 36, 0.12);  border-color: rgba(251,191,36,0.4);  color: var(--c-action); }

  /* Chrome delegated to .algo-grid-chrome class on each element. */
  .hist-table-wrap {
    overflow-x: auto;
    margin-bottom: 0.6rem;
  }
  .hist-table {
    width: 100%;
  }
  .hist-table th {
    text-align: left;
    padding: 0.42rem 0.6rem;
    background: rgba(15, 23, 42, 0.65);
    color: var(--text-muted);
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    border-bottom: 1px solid rgba(251, 191, 36, 0.30);
    white-space: nowrap;
  }
  .hist-table th.th-num { text-align: right; }
  .hist-table td {
    padding: 0.38rem 0.6rem;
    white-space: nowrap;
  }
  .hist-table td.td-mono { font-family: var(--font-numeric); font-size: var(--fs-sm); }
  .hist-empty-row {
    padding: 2rem; text-align: center;
    color: #94a3b8; font-style: italic;
  }

  .hist-side {
    display: inline-block;
    padding: 0.05rem 0.4rem;
    border-radius: 3px;
    font-size: var(--fs-2xs);
    font-weight: 800;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    font-family: var(--font-numeric);
  }
  .hist-side-buy  { background: rgba(74, 222, 128, 0.15);  color: var(--c-long); border: 1px solid rgba(74,222,128,0.45); }
  .hist-side-sell { background: rgba(248, 113, 113, 0.15); color: #fca5a5; border: 1px solid rgba(248,113,113,0.45); }

  .cell-pos { color: var(--c-long); }
  .cell-neg { color: #fca5a5; }

  /* Per-row Audit link in Orders tab — drill-through to /admin/audit
     filtered by request_id. */
  .hist-audit-link {
    display: inline-block;
    padding: 0.1rem 0.45rem;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    color: #67e8f9;
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(34, 211, 238, 0.40);
    border-radius: 3px;
    text-decoration: none;
    font-family: var(--font-numeric);
  }
  .hist-audit-link:hover {
    background: var(--c-info-22);
    border-color: rgba(34, 211, 238, 0.75);
    color: #a5f3fc;
  }
  .hist-audit-none {
    font-size: var(--fs-sm); color: rgba(126, 151, 184, 0.55);
    font-family: var(--font-numeric);
  }

  /* Backfill row on Funds tab. */
  .hist-backfill {
    display: flex; gap: 0.55rem; align-items: flex-end; flex-wrap: wrap;
    margin-bottom: 0.55rem;
    padding: 0.5rem 0.7rem;
    background: rgba(15, 23, 42, 0.40);
    border: 1px dashed rgba(126, 151, 184, 0.30);
    border-radius: 4px;
  }
  .hist-backfill-btn {
    padding: 0.35rem 0.8rem;
    font-size: var(--fs-lg); font-weight: 700;
  }
  .hist-pager {
    display: flex; align-items: center; gap: 0.6rem;
    color: #c8d8f0;
  }
</style>
