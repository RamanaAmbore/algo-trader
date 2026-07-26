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
   *   statusFilter?  — 'all'|'open'|'complete'|'rejected'|'cancelled'
   */
  import { onMount, onDestroy, untrack } from 'svelte';
  import { visibleInterval, formatDualTz } from '$lib/stores';
  import { fetchOrders, fetchAlgoOrdersRecent, cancelOrder, reconcileSingleOrder } from '$lib/api';
  import OrderCard from '$lib/order/OrderCard.svelte';
  import ChartModal from '$lib/ChartModal.svelte';
  import SymbolPanel from '$lib/SymbolPanel.svelte';
  import SymbolContextMenu from '$lib/SymbolContextMenu.svelte';
  import CardHeader from '$lib/CardHeader.svelte';

  /** @type {{
   *   orderId?: string | null,
   *   accountFilter?: string[],
   *   title?: string,
   *   pollMs?: number,
   *   statusFilter?: 'all'|'open'|'complete'|'rejected'|'cancelled',
   *   onSymbolClick?: ((ord: any) => void) | null,
   *   isCollapsed?: boolean,
   * }} */
  let {
    orderId       = null,
    accountFilter = /** @type {string[]} */ ([]),
    title         = 'Order Book',
    pollMs        = 3000,
    statusFilter  = /** @type {'all'|'open'|'complete'|'rejected'|'cancelled'} */ ('all'),
    onSymbolClick = /** @type {((ord: any) => void) | null} */ (null),
    isCollapsed   = $bindable(false),
  } = $props();

  // ── Data ─────────────────────────────────────────────────────────────
  let orderRows = $state(/** @type {any[]} */ ([]));

  async function _loadOrders() {
    // Merge broker orders + algo orders so the book carries the same data
    // LogPanel's order tab renders — broker rows plus paper/sim/shadow rows
    // that never reach the broker. Broker rows win on duplicate order_id.
    try {
      const [brokerResp, algoResp] = await Promise.allSettled([
        fetchOrders(),
        fetchAlgoOrdersRecent(100, 'all'),
      ]);
      const brokerRows = (brokerResp.status === 'fulfilled' && Array.isArray(brokerResp.value?.rows))
        ? brokerResp.value.rows
        : [];
      const algoRows = (algoResp.status === 'fulfilled' && Array.isArray(algoResp.value))
        ? algoResp.value
        : [];
      const brokerIds = new Set(brokerRows.map(o => String(o?.order_id || '')));
      const algoOnly  = algoRows.filter(o => {
        const oid = String(o?.order_id || o?.id || '');
        return !brokerIds.has(oid);
      });
      const merged = [...brokerRows, ...algoOnly];
      merged.sort((a, b) => {
        const ta = Date.parse(a.order_timestamp || a.created_at || '') || 0;
        const tb = Date.parse(b.order_timestamp || b.created_at || '') || 0;
        return tb - ta;
      });
      orderRows = merged;
    } catch (_) { /* keep last-good */ }
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
  const _TERMINAL_STATUSES = new Set(['COMPLETE', 'CANCELLED', 'REJECTED', 'EXPIRED']);

  /** @type {Record<string, (st: string) => boolean>} */
  const _STATUS_PREDICATES = {
    open:      st => st === 'OPEN' || st === 'TRIGGER PENDING',
    complete:  st => st === 'COMPLETE',
    rejected:  st => st === 'REJECTED',
    cancelled: st => st === 'CANCELLED',
  };

  function _applyAccountFilter(rows, /** @type {string[]} */ filter) {
    if (!filter || filter.length === 0) return rows;
    const want = new Set(filter);
    return rows.filter(o => want.has(String(o?.account || '')));
  }

  function _applyStatusFilter(rows, /** @type {string|null|undefined} */ filter) {
    if (!filter || filter === 'all') return rows;
    const pred = _STATUS_PREDICATES[filter];
    if (!pred) return rows;
    return rows.filter(o => pred((o?.status || '').toUpperCase()));
  }

  function _applyDateFilter(rows) {
    const istNow = new Date(Date.now() + 5.5 * 60 * 60 * 1000);
    const today = istNow.toISOString().slice(0, 10);
    return rows.filter(o => {
      let ts = '';
      if (o.order_timestamp) {
        ts = String(o.order_timestamp).slice(0, 10);
      } else if (o.created_at) {
        ts = new Date(new Date(o.created_at).getTime() + 5.5 * 60 * 60 * 1000)
               .toISOString().slice(0, 10);
      }
      const term = _TERMINAL_STATUSES.has((o.status || '').toUpperCase());
      return !(ts && ts !== today && term);
    });
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
    rows = _applyStatusFilter(rows, statusFilter);
    rows = _applyDateFilter(rows);
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
    return (st === 'OPEN' || st === 'TRIGGER PENDING') && !o?.mode;
  }

  /** Returns true when the order is in-flight and reconciling is meaningful. */
  function _isInFlight(/** @type {any} */ o) {
    const st = (o?.status || '').toUpperCase();
    return st === 'OPEN' || st === 'TRIGGER PENDING'
        || st === 'CANCEL_FAILED' || st === 'PARTIAL';
  }

  async function _cancelRow(/** @type {any} */ o) {
    const key = String(o.order_id || o.id || '');
    if (_cancelling.has(key)) return;
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

<!-- Header -->
<CardHeader label={title} showSearch={false} bind:isCollapsed>
  {#snippet left()}
    <span class="ob-count">{filteredOrderRows.length} order{filteredOrderRows.length !== 1 ? 's' : ''}</span>
  {/snippet}
</CardHeader>

<!-- Order card grid -->
{#if !isCollapsed}
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
    <div class="log-debug py-2 text-center">No orders.</div>
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

<style>
  .ob-count {
    font-size: 0.65rem;
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
</style>
