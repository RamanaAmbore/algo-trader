<script>
  /**
   * OrderBook — standalone order card feed extracted from LogPanel's
   * `order` tab. Renders OrderCard rows with Cancel / Modify / Reconcile
   * actions. Polls fetchOrders() + fetchAlgoOrdersRecent() on the same
   * visibleInterval cadence LogPanel uses.
   *
   * Props:
   *   orderId?       — when set, narrows display to rows matching this id
   *   accountFilter? — optional external account filter (bindable)
   *   title?         — header label (default 'Order Book')
   *   pollMs?        — polling cadence in ms (default 3000)
   *   statusFilter?  — 'chase'|'open'|'complete'|'rejected'|'cancelled'
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { visibleInterval, formatDualTz } from '$lib/stores';
  import { isCurrentTradingSession } from '$lib/dateFormat.js';
  import { fetchOrders, fetchAlgoOrdersRecent, cancelOrder, reconcileSingleOrder } from '$lib/api';
  import OrderCard from '$lib/order/OrderCard.svelte';
  import ChartModal from '$lib/ChartModal.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import CardHeader from '$lib/CardHeader.svelte';
  import { toast } from '$lib/data/toastStore.svelte.js';
  import { noteAttachObservation } from '$lib/data/templateAttachToast.js';

  /** @type {{
   *   orderId?: string | null,
   *   accountFilter?: string[],
   *   title?: string,
   *   pollMs?: number,
   *   statusFilter?: 'chase'|'open'|'complete'|'rejected'|'cancelled',
   *   onSymbolClick?: ((ord: any) => void) | null,
   *   isCollapsed?: boolean,
   *   isFullscreen?: boolean,
   * }} */
  let {
    orderId       = null,
    accountFilter = /** @type {string[]} */ ([]),
    title         = 'Order Book',
    pollMs        = 3000,
    statusFilter  = /** @type {'chase'|'open'|'complete'|'rejected'|'cancelled'} */ ('open'),
    onSymbolClick = /** @type {((ord: any) => void) | null} */ (null),
    isCollapsed   = $bindable(false),
    isFullscreen  = $bindable(false),
  } = $props();

  // ── Data ─────────────────────────────────────────────────────────────
  let orderRows = $state(/** @type {any[]} */ ([]));
  let _loading  = $state(true);

  let _internalStatus = $state(/** @type {string|null} */ (null));
  const _activeStatus = $derived(_internalStatus ?? statusFilter);

  /**
   * Row timestamp → epoch-ms, tolerant of both source field conventions
   * this merged view carries: Kite's `order_timestamp` ("YYYY-MM-DD
   * HH:MM:SS", IST, no offset — Safari's Date.parse rejects the
   * space-separated non-ISO form outright, so normalize to ISO+offset
   * explicitly rather than passing the raw string through) and
   * AlgoOrder's `created_at` (naive UTC `isoformat()`, no trailing 'Z' —
   * Date.parse without an explicit offset is spec'd as LOCAL time, so
   * append 'Z' explicitly rather than relying on engine defaults).
   * Returns NaN when unparseable.
   */
  function _rowTsMs(/** @type {any} */ o) {
    if (o?.order_timestamp) {
      const s = String(o.order_timestamp).trim();
      const m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})/);
      return m ? Date.parse(`${m[1]}T${m[2]}+05:30`) : Date.parse(s);
    }
    if (o?.created_at) {
      const s = String(o.created_at).trim();
      if (!s) return NaN;
      return /[Zz]|[+-]\d{2}:\d{2}$/.test(s) ? Date.parse(s) : Date.parse(`${s}Z`);
    }
    return NaN;
  }

  /**
   * Drops TERMINAL rows from a PRIOR trading session (08:00 IST rollover —
   * see `isCurrentTradingSession`). This is a display-only filter applied
   * upstream to `orderRows` itself (not just the rendered grid) so every
   * downstream consumer — status counts, the active status filter, CSV
   * export — automatically only ever sees today's-session terminal orders
   * with no separate/duplicate filtering logic.
   *
   * Still-working rows (OPEN/TRIGGER PENDING) are NEVER dropped by this
   * filter regardless of age — operator explicit instruction (2026-09-30,
   * reversing the initial default): "keep them visible until reconciled."
   * A resting order from a prior session is exactly the kind of thing an
   * operator needs to SEE and act on (cancel / reconcile), not have
   * silently disappear at the next 08:00 rollover.
   */
  function _isCurrentSessionRow(/** @type {any} */ o) {
    if (_STATUS_PREDICATES.open((o?.status || '').toUpperCase())) return true;
    const ms = _rowTsMs(o);
    if (!Number.isFinite(ms)) return true; // can't judge — keep, don't hide data we can't classify
    return isCurrentTradingSession(ms);
  }

  async function _loadOrders() {
    // Merge broker orders + algo orders so the book carries the same data
    // LogPanel's order tab renders — broker rows plus paper/sim/shadow rows
    // that never reach the broker. Broker rows win on duplicate order_id.
    try {
      const [brokerResp, algoResp] = await Promise.allSettled([
        fetchOrders(),
        fetchAlgoOrdersRecent(100, 'all'),
      ]);
      // Audit fix — "cancelled orders showing as OPEN again": fetchOrders()
      // (the broker book — authoritative for CANCELLED/COMPLETE/REJECTED,
      // confirmed at the exchange) can transiently reject (network blip,
      // 5xx). Pre-fix, a rejected brokerResp silently fell through to
      // brokerRows=[], so EVERY row in the merged view came from the algo
      // book instead — whose status field can lag a just-confirmed
      // cancel/fill until the postback/reconcile pass catches up. The next
      // poll after a genuine cancel could then render that order back as
      // OPEN purely because the broker fetch (not the order) failed. Freeze
      // to the last-known-good merged view instead of recomputing from
      // partial data — matches this app's staleness-freeze convention
      // elsewhere (positions/holdings/NAV).
      if (brokerResp.status !== 'fulfilled') {
        _loading = false;
        return;
      }
      const brokerRows = Array.isArray(brokerResp.value?.rows) ? brokerResp.value.rows : [];
      const algoRows = (algoResp.status === 'fulfilled' && Array.isArray(algoResp.value))
        ? algoResp.value
        : [];
      const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
      const algoOnly  = algoRows.filter(o => {
        const oid = String(o?.order_id || o?.id || '');
        return !brokerIds.has(oid);
      });
      // Session-boundary filter applied HERE (before assigning orderRows,
      // not down in the filteredOrderRows derived chain) — a still-OPEN
      // order resting since a prior session (rare — e.g. a GTT-adjacent
      // order) is judgment-called to hide here too, same as terminal
      // rows. No clear precedent in this app scopes to "keep showing a
      // stale resting order in a live order-book grid" (expiry_freeze.py's
      // position-freeze precedent is about serving the last-known
      // POSITION snapshot when data is otherwise unavailable, a different
      // situation from a live, always-available order feed) — flagged for
      // reversal if the operator wants stale-OPEN rows to stay visible.
      const merged = [...brokerRows, ...algoOnly].filter(_isCurrentSessionRow);
      merged.sort((a, b) => {
        const ta = _rowTsMs(a) || 0;
        const tb = _rowTsMs(b) || 0;
        return tb - ta;
      });
      orderRows = merged;
      // Trading-critical "template did not attach" toast (2026-09-30) —
      // evaluated on every merged row BEFORE status-chip filtering, so
      // it fires regardless of which status bucket the operator has
      // selected. See templateAttachToast.js for the live-only gate +
      // time-based debounce this relies on.
      for (const o of merged) {
        if (noteAttachObservation(o)) {
          const oid = o?.id ?? o?.order_id;
          toast.warning(`Order #${oid} template did not attach — check Order Book`, { timeoutMs: 5000 });
        }
      }
    } catch (_) { /* keep last-good */ } finally {
      _loading = false;
    }
  }

  function _downloadCsv() {
    const rows = filteredOrderRows;
    if (!rows.length) return;
    const headers = ['order_id','symbol','exchange','transaction_type','quantity','price','status','timestamp'];
    const csv = [
      headers.join(','),
      ...rows.map(r => [
        JSON.stringify(r.order_id    ?? r.id           ?? ''),
        JSON.stringify(r.tradingsymbol ?? r.symbol     ?? ''),
        JSON.stringify(r.exchange    ?? ''),
        JSON.stringify(r.transaction_type ?? ''),
        JSON.stringify(r.quantity    ?? ''),
        JSON.stringify(r.price       ?? r.fill_price   ?? r.initial_price ?? ''),
        JSON.stringify(r.status      ?? ''),
        JSON.stringify(r.order_timestamp ?? r.created_at ?? ''),
      ].join(','))
    ].join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv' }));
    a.download = 'orders.csv';
    a.click();
  }

  // ── Interval management ───────────────────────────────────────────────
  /** @type {Array<() => void>} */
  const _intervals = [];

  onMount(() => {
    _loadOrders();
    if (pollMs > 0 && typeof document !== 'undefined') {
      const teardown = visibleInterval(() => { _loadOrders(); }, pollMs);
      _intervals.push(teardown);
    }
  });

  onDestroy(() => {
    for (const teardown of _intervals) teardown();
  });

  // ── Filter predicates (mirrors LogPanel exactly) ───────────────────────
  /** @type {Record<string, (st: string) => boolean>} */
  const _STATUS_PREDICATES = {
    open:      st => st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING',
    // 'COMPLETE' = broker (Kite) vocabulary, 'FILLED' = AlgoOrder vocabulary
    // (ALGO_ORDER_FINAL_STATUSES in backend/api/models.py) — algo-only fills
    // (paper/sim/replay/shadow, or an algo-tracked live order before the
    // broker's own COMPLETE status lands) never carry 'COMPLETE'.
    complete:  st => st === 'COMPLETE' || st === 'FILLED',
    rejected:  st => st === 'REJECTED',
    cancelled: st => st === 'CANCELLED',
  };

  /**
   * "Chase in flight" = still working (OPEN/TRIGGER PENDING) AND the
   * chase engine has cancelled-and-replaced it at least once
   * (`attempts > 0`). A plain resting order the chase engine has never
   * touched doesn't count — mirrors `OrderCard.svelte`'s existing
   * `chase:#N` chip precedent (`order.attempts != null && order.attempts > 0`).
   */
  function _isChaseInFlight(/** @type {any} */ o) {
    if (!_STATUS_PREDICATES.open((o?.status || '').toUpperCase())) return false;
    return Number(o?.attempts || 0) > 0;
  }

  function _applyAccountFilter(rows, /** @type {string[]} */ filter) {
    if (!filter || filter.length === 0) return rows;
    const want = new Set(filter);
    return rows.filter(o => want.has(String(o?.account || '')));
  }

  function _applyStatusFilter(rows, /** @type {string|null|undefined} */ filter) {
    if (!filter) return rows;
    if (filter === 'chase') return rows.filter(_isChaseInFlight);
    const pred = _STATUS_PREDICATES[filter];
    if (!pred) return rows;
    return rows.filter(o => pred((o?.status || '').toUpperCase()));
  }

  function _applyOrderIdFilter(rows, /** @type {string|null} */ id) {
    if (!id) return rows;
    return rows.filter(o => {
      const oid = String(o?.order_id || o?.id || '');
      return oid === String(id);
    });
  }

  const filteredOrderRows = $derived.by(() => {
    let rows = orderRows || [];
    rows = _applyAccountFilter(rows, accountFilter);
    rows = _applyStatusFilter(rows, _activeStatus);
    rows = _applyOrderIdFilter(rows, orderId);
    return rows;
  });

  // ── Cancel / Modify / Reconcile actions (mirrors LogPanel) ────────────
  /** @type {Set<string>} */
  let _cancelling  = $state(new Set());
  /** @type {Set<string>} */
  let _reconciling = $state(new Set());
  /** @type {string} */
  let _cancelErr   = $state('');

  /** Returns true when the order is an OPEN broker order that can be acted on. */
  function _isOpenBroker(/** @type {any} */ o) {
    const st = (o?.status || '').toUpperCase();
    return (st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING') && !o?.mode;
  }

  /** Returns true when the order is in-flight and reconciling is meaningful. */
  function _isInFlight(/** @type {any} */ o) {
    const st = (o?.status || '').toUpperCase();
    return st === 'OPEN' || st === 'TRIGGER PENDING'
        || st === 'CANCEL_FAILED' || st === 'PARTIAL';
  }

  async function _cancelRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (!key || _cancelling.has(key)) return;
    _cancelling = new Set([..._cancelling, key]);
    _cancelErr = '';
    try {
      await cancelOrder(o.order_id, o.account, o.variety || 'regular');
      await _loadOrders();
    } catch (e) {
      _cancelErr = /** @type {any} */ (e)?.message || 'cancel failed';
      setTimeout(() => { _cancelErr = ''; }, 3000);
    } finally {
      const next = new Set(_cancelling);
      next.delete(key);
      _cancelling = next;
    }
  }

  function _requestModify(/** @type {any} */ o, /** @type {HTMLElement | null} */ el) {
    el?.dispatchEvent(new CustomEvent('lp:modify-order', {
      detail: o,
      bubbles: true,
      composed: true,
    }));
  }

  async function _reconcileRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (!key || !o?.account || _reconciling.has(key)) return;
    _reconciling = new Set([..._reconciling, key]);
    try {
      const res = await reconcileSingleOrder(o.order_id, o.account);
      if (res?.updated) await _loadOrders();
    } catch (e) {
      _cancelErr = /** @type {any} */ (e)?.message || 'reconcile failed';
      setTimeout(() => { _cancelErr = ''; }, 3000);
    } finally {
      const next = new Set(_reconciling);
      next.delete(key);
      _reconciling = next;
    }
  }

  // ── Status counts (computed once per render, not 5× inline) ─────────
  // Reads orderRows directly — already session-boundary-filtered in
  // _loadOrders — so every count here is scoped to today's session only,
  // matching the grid it labels.
  const _statusCounts = $derived.by(() => ({
    chase:     orderRows.filter(_isChaseInFlight).length,
    open:      orderRows.filter(o => _STATUS_PREDICATES.open((o.status || '').toUpperCase())).length,
    complete:  orderRows.filter(o => _STATUS_PREDICATES.complete((o.status || '').toUpperCase())).length,
    rejected:  orderRows.filter(o => _STATUS_PREDICATES.rejected((o.status || '').toUpperCase())).length,
    cancelled: orderRows.filter(o => _STATUS_PREDICATES.cancelled((o.status || '').toUpperCase())).length,
  }));

  // ── Symbol panel / chart modal / context menu state ───────────────────
  let _symPanelSym  = $state('');
  let _symPanelExch = $state('');
  /** @type {{ symbol: string, exchange: string, x: number, y: number } | null} */
  let _ctxMenu = $state(null);
  /** @type {'place-order' | 'chart' | null} */
  let _ctxAction = $state(null);
  let _ctxSym  = $state('');
  let _ctxExch = $state('');
</script>

<div class="ob-root" class:ob-fs={isFullscreen}>

<!-- Header -->
<CardHeader title={title} showSearch={false} bind:isCollapsed bind:isFullscreen
  detectOverflow={false}
  onRefresh={_loadOrders}
  onDownload={_downloadCsv}
>
  {#snippet left()}
    <span class="ob-count">{filteredOrderRows.length} order{filteredOrderRows.length !== 1 ? 's' : ''}</span>
  {/snippet}
</CardHeader>

<!-- Order card grid -->
{#if !isCollapsed}
  <div class="ob-status-bar">
    {#each [
      { id: 'chase',     label: 'Chase',     status: 'chase',     count: _statusCounts.chase },
      { id: 'open',      label: 'Open',      status: 'running',   count: _statusCounts.open },
      { id: 'complete',  label: 'Filled',    status: 'active',    count: _statusCounts.complete },
      { id: 'rejected',  label: 'Rejected',  status: 'error',     count: _statusCounts.rejected },
      { id: 'cancelled', label: 'Cancelled', status: 'cancelled', count: _statusCounts.cancelled },
    ] as f}
      <button type="button" class="ob-sc" class:is-active={_activeStatus === f.id}
        data-status={f.status}
        onclick={() => { _internalStatus = f.id; }}>
        <span class="ob-sc-n">{f.count}</span>
        <span class="ob-sc-l">{f.label}</span>
      </button>
    {/each}
  </div>
<div class="ob-scroll">
  {#if filteredOrderRows.length}
    <div class="oc-book-grid">
      {#each filteredOrderRows as o (o.order_id ?? o.id)}
        {@const _oKey = String(o.order_id || o.id || '')}
        <OrderCard order={o}
          onSymbolClick={(ord) => {
            if (onSymbolClick) { onSymbolClick(ord); return; }
            _symPanelSym = ord.tradingsymbol || ord.symbol || '';
            _symPanelExch = ord.exchange || '';
          }}
          onSymbolContext={(ord, e) => { _ctxMenu = { symbol: ord.tradingsymbol || ord.symbol || '', exchange: ord.exchange || '', x: /** @type {MouseEvent} */ (e).clientX, y: /** @type {MouseEvent} */ (e).clientY }; }}>
          {#snippet actions(ord)}
            <div class="lp-oc-actions" role="group" aria-label="Order actions">
              {#if _isOpenBroker(ord)}
                <button type="button" class="lp-oc-btn lp-oc-modify"
                  title="Modify order"
                  aria-label="Modify"
                  onclick={(e) => { e.stopPropagation(); _requestModify(ord, e.currentTarget?.closest('.ob-scroll')); }}>
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M11.5 2.5l2 2L5 13H3v-2L11.5 2.5z" stroke="currentColor" stroke-width="1.6"
                          stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </button>
                <button type="button" class="lp-oc-btn lp-oc-cancel"
                  title="Cancel order"
                  aria-label="Cancel"
                  disabled={_cancelling.has(_oKey)}
                  onclick={(e) => { e.stopPropagation(); _cancelRow(ord); }}>
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8"
                          stroke-linecap="round"/>
                  </svg>
                </button>
              {/if}
              {#if _isInFlight(ord)}
                <button type="button" class="lp-oc-btn lp-oc-reconcile"
                  title="Reconcile with broker"
                  aria-label="Reconcile"
                  disabled={_reconciling.has(_oKey)}
                  onclick={(e) => { e.stopPropagation(); _reconcileRow(ord); }}>
                  <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                    <path d="M3 8a5 5 0 0 1 8.6-3.5M13 8a5 5 0 0 1-8.6 3.5"
                      stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
                    <path d="M11.5 2v3h-3M4.5 14v-3h3"
                      stroke="currentColor" stroke-width="1.5"
                      stroke-linecap="round" stroke-linejoin="round" />
                  </svg>
                </button>
              {/if}
            </div>
          {/snippet}
        </OrderCard>
      {/each}
      {#if _cancelErr}
        <div class="log-row log-agent-failed">{_cancelErr}</div>
      {/if}
    </div>
  {:else}
    <div class="log-debug py-2 text-center">
      {#if _loading}
        Loading…
      {:else if orderRows.length > 0}
        No orders match the current filters.
      {:else}
        No orders today.
      {/if}
    </div>
  {/if}
</div>
{/if}

{#if _symPanelSym && !onSymbolClick}
  <SymbolPanel
    symbol={_symPanelSym}
    exchange={_symPanelExch}
    onSubmit={() => {}}
    onClose={() => { _symPanelSym = ''; _symPanelExch = ''; }}
  />
{/if}

{#if _ctxMenu}
  <SymbolContextMenu
    symbol={_ctxMenu?.symbol}
    exchange={_ctxMenu?.exchange}
    x={_ctxMenu?.x}
    y={_ctxMenu?.y}
    onClose={() => { _ctxMenu = null; }}
    onAction={(action, sym, exch) => {
      _ctxSym  = sym;
      _ctxExch = exch;
      _ctxAction = /** @type {any} */ (action);
      _ctxMenu = null;
    }}
  />
{/if}

{#if _ctxAction === 'chart'}
  <ChartModal
    symbol={_ctxSym}
    exchange={_ctxExch}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

{#if _ctxAction === 'place-order'}
  <SymbolPanel
    symbol={_ctxSym}
    exchange={_ctxExch}
    onSubmit={() => {}}
    onClose={() => { _ctxAction = null; }}
  />
{/if}

</div>

<style>
  /* Fullscreen wrapper — transparent by default (display:contents passes
     layout through to children); switches to a fixed-position modal frame
     when ob-fs activates. Modals inside (ChartModal, SymbolPanel, etc.)
     use fixed positioning themselves so display:contents doesn't trap them. */
  .ob-root { display: contents; }
  .ob-fs {
    display: flex;
    flex-direction: column;
    position: fixed;
    inset: 0;
    z-index: 9000;
    background: var(--algo-navy, #0f1c36);
    padding: 0.5rem;
  }
  .ob-fs .ob-scroll { flex: 1 1 0; min-height: 0; }

  .ob-count {
    font-size: var(--fs-md);
    color: rgba(255,255,255,0.3);
    font-variant-numeric: tabular-nums;
    margin-left: 0.25rem;
  }

  .ob-scroll {
    overflow-y: auto;
    flex: 1 1 0;
    min-height: 0;
    padding: 0.4rem 0.2rem;
  }

  .ob-scroll :global(.oc-book-grid) {
    display: grid;
    grid-template-columns: 1fr;
    gap: 0.5rem;
  }
  @media (min-width: 640px) {
    .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (min-width: 1024px) {
    .ob-scroll :global(.oc-book-grid) { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  }

  .ob-status-bar {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 0.25rem;
    padding: 0.3rem 0.4rem 0.2rem;
  }

  /* Status filter chips — match .oc-filter-card chrome from the orders page.
     Navy gradient base, status-tinted gradient overlay via data-status,
     amber inset ring on .is-active. */
  .ob-sc {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.15rem;
    padding: 0.45rem 0.6rem;
    min-width: 0;
    background:
      linear-gradient(180deg,
        rgba(255, 255, 255, 0.04) 0%,
        rgba(255, 255, 255, 0.00) 30%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 5px;
    box-shadow:
      0 1px 0 rgba(255, 255, 255, 0.06) inset,
      0 2px 4px rgba(0, 0, 0, 0.25);
    color: var(--algo-slate, #94a3b8);
    font-family: var(--font-numeric);
    cursor: pointer;
    transition: border-color 0.12s, transform 0.12s, box-shadow 0.12s;
  }
  .ob-sc:hover {
    border-color: rgba(255, 255, 255, 0.30);
    transform: translateY(-1px);
  }
  .ob-sc.is-active {
    box-shadow:
      0 0 0 2px rgba(251, 191, 36, 0.55) inset,
      0 1px 0 rgba(255, 255, 255, 0.06) inset,
      0 2px 4px rgba(0, 0, 0, 0.25);
  }

  /* Status-tinted backgrounds + borders per data-status value. */
  .ob-sc[data-status="running"] {
    background:
      linear-gradient(180deg,
        rgba(251, 191, 36, 0.18) 0%,
        rgba(251, 191, 36, 0.06) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(251, 191, 36, 0.60);
  }
  .ob-sc[data-status="active"] {
    background:
      linear-gradient(180deg,
        rgba(74, 222, 128, 0.18) 0%,
        rgba(74, 222, 128, 0.04) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(74, 222, 128, 0.60);
  }
  .ob-sc[data-status="error"] {
    background:
      linear-gradient(180deg,
        rgba(248, 113, 113, 0.18) 0%,
        rgba(248, 113, 113, 0.04) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(248, 113, 113, 0.60);
  }
  .ob-sc[data-status="cancelled"] {
    background:
      linear-gradient(180deg,
        rgba(251, 146, 60, 0.18) 0%,
        rgba(251, 146, 60, 0.06) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(251, 146, 60, 0.55);
  }
  /* Chase chip — sky/info tint, deliberately distinct from Open's amber
     so "resting, untouched" vs "actively being re-quoted by the chase
     engine" read apart at a glance. */
  .ob-sc[data-status="chase"] {
    background:
      linear-gradient(180deg,
        rgba(125, 211, 252, 0.18) 0%,
        rgba(125, 211, 252, 0.05) 60%,
        rgba(0, 0, 0, 0.08) 100%),
      linear-gradient(180deg, #2c3a5a 0%, #1a2740 100%);
    border-color: rgba(125, 211, 252, 0.55);
  }

  /* Count number — bigger + color-coded by status. 2026-09 font-size
     audit: 1.1rem falls in the gap between --fs-xl (0.85rem/13.6px)
     and --fs-2xl (1.55rem/24.8px) — no existing token lands within
     ~4px without a visible resize of this live chase-queue counter.
     Flagged, not auto-mapped; left as a deliberate literal pending an
     operator call on either a new "stat" tier token or accepting the
     nearest existing one. */
  .ob-sc-n {
    font-weight: 800;
    font-size: 1.1rem;
    line-height: 1;
    color: var(--algo-slate, #94a3b8);
    font-variant-numeric: tabular-nums;
    text-shadow: 0 1px 2px rgba(0, 0, 0, 0.45);
  }
  .ob-sc[data-status="running"]   .ob-sc-n { color: var(--c-action, #fbbf24); }
  .ob-sc[data-status="active"]    .ob-sc-n { color: var(--c-long, #4ade80); }
  .ob-sc[data-status="error"]     .ob-sc-n { color: var(--c-short, #f87171); }
  .ob-sc[data-status="cancelled"] .ob-sc-n { color: #fb923c; }
  .ob-sc[data-status="chase"]     .ob-sc-n { color: var(--algo-sky, #7dd3fc); }

  .ob-sc-l {
    font-size: var(--fs-xs, 0.6rem);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-muted, rgba(255,255,255,0.4));
    line-height: 1;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* 5 status chips (Chase/Open/Filled/Rejected/Cancelled) need to fit a
     320-375px phone viewport without forcing the grid wider than the
     card — tighten padding + letter-spacing below 600px so the labels
     truncate gracefully instead of overflowing. */
  @media (max-width: 600px) {
    .ob-sc { padding: 0.35rem 0.2rem; }
    .ob-sc-l { font-size: 0.55rem; letter-spacing: 0.03em; }
  }
</style>
