<script>
  /**
   * OrderTimelineDrawer — right-edge slide-in drawer showing live event
   * timeline for every OPEN chase order.
   *
   * Props:
   *   open       {boolean}   — whether the drawer is visible
   *   orders     {Array}     — array of order objects from fetchOrderEvents
   *   onClose    {Function}  — called when the drawer should be dismissed
   */
  import { onMount, onDestroy } from 'svelte';
  import { priceFmt } from '$lib/format';
  import { logTime } from '$lib/stores';
  import { formatSymbol } from '$lib/data/decomposeSymbol';

  const { open = false, orders = [], onClose } = $props();

  // ── Kind → color mapping ──────────────────────────────────────────────
  const KIND_COLOR = {
    placed:           '#38bdf8',   // sky
    chase_modify:     'var(--c-action)',   // amber
    fill:             'var(--c-long)',   // emerald
    unfill:           'var(--c-short)',   // red
    reject:           'var(--c-short)',   // red
    preflight_ok:     '#6b7280',   // grey
    preflight_block:  'var(--c-short)',   // red
    cancel:           '#94a3b8',   // slate
    postback:         '#a78bfa',   // violet
  };
  const KIND_BG = {
    placed:           'rgba(56,189,248,0.15)',
    chase_modify:     'rgba(251,191,36,0.15)',
    fill:             'rgba(74,222,128,0.15)',
    unfill:           'rgba(248,113,113,0.15)',
    reject:           'rgba(248,113,113,0.15)',
    preflight_ok:     'rgba(107,114,128,0.15)',
    preflight_block:  'rgba(248,113,113,0.15)',
    cancel:           'rgba(148,163,184,0.15)',
    postback:         'rgba(167,139,250,0.15)',
  };

  const TERMINAL_KINDS = new Set(['fill', 'unfill', 'reject', 'cancel']);

  /** True if an order is in a terminal state (all events have a terminal kind). */
  function isTerminal(/** @type {any[]} */ events) {
    return events.some(e => TERMINAL_KINDS.has(e.kind));
  }

  /** CSS class suffix for the mode pill — matches LogPanel + CLAUDE.md palette. */
  function modeCls(/** @type {string} */ mode) {
    if (mode === 'sim')   return 'otd-mode-sim';
    if (mode === 'paper') return 'otd-mode-paper';
    if (mode === 'live')  return 'otd-mode-live';
    return 'otd-mode-unknown';
  }

  // Order events are trading-critical — fill time matters per second
  // across both India and US sessions. Route through the standard
  // `logTime` helper so this drawer reads in the same dual-TZ form
  // ("DD-MMM HH:MM:SS IST | DD-MMM HH:MM:SS EST/EDT") as every other
  // log row on the platform. Returns '' for unparseable input so
  // "Invalid Date" never leaks.
  function shortTime(iso) {
    return iso ? (logTime(iso) || '') : '';
  }

  /**
   * Comparator: non-terminal sections first, terminal last;
   * within each group newest-event-first by created_at.
   * @param {any} a @param {any} b @returns {number}
   */
  function _sectionComparator(a, b) {
    const at = isTerminal(a.events ?? []);
    const bt = isTerminal(b.events ?? []);
    if (at !== bt) return at ? 1 : -1;
    const aTs = (a.events?.[0]?.created_at) ?? '';
    const bTs = (b.events?.[0]?.created_at) ?? '';
    return bTs.localeCompare(aTs);
  }

  /**
   * Sort an already-shaped (per-order) orders array — non-terminal first,
   * terminal last; within group newest first.
   * @param {any[]} shaped @returns {any[]}
   */
  function _sortPreShapedOrders(shaped) {
    return [...shaped].sort(_sectionComparator);
  }

  /**
   * Collapse a flat event list into per-order section objects, then sort.
   * @param {any[]} evList @returns {any[]}
   */
  function _groupFlatEvents(evList) {
    /** @type {Map<string, {order_id: string, symbol: string, side: string, qty: number, mode: string, events: any[]}>} */
    const map = new Map();
    for (const ev of evList) {
      const id = ev.order_id ?? ev.id ?? 'unknown';
      if (!map.has(id)) {
        map.set(id, {
          order_id: id,
          symbol:   ev.symbol  ?? ev.tradingsymbol ?? '',
          side:     ev.side    ?? '',
          qty:      ev.qty     ?? ev.quantity ?? 0,
          mode:     ev.mode    ?? 'paper',
          events:   [],
        });
      }
      map.get(id).events.push(ev);
    }
    return Array.from(map.values()).sort(_sectionComparator);
  }

  /** Group flat events array into per-order sections.
   *  orders is already shaped per-order from the API; if it's a flat list
   *  we group by order_id here. */
  const grouped = $derived.by(() => {
    if (!orders?.length) return [];
    // If the API returns per-order objects with .events, use them directly.
    if (orders[0] && 'events' in orders[0]) {
      return _sortPreShapedOrders(orders);
    }
    // Flat event array — group by order_id.
    return _groupFlatEvents(orders);
  });

  // ── Keyboard dismiss ──────────────────────────────────────────────────
  function onKeyDown(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape' && open) onClose?.();
  }
  onMount(() => {
    if (typeof window !== 'undefined') {
      window.addEventListener('keydown', onKeyDown);
    }
  });
  onDestroy(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('keydown', onKeyDown);
    }
  });
</script>

{#if open}
  <!-- Backdrop overlay — click to dismiss -->
  <div
    class="otd-backdrop"
    role="presentation"
    onclick={() => onClose?.()}
  ></div>

  <!-- Drawer panel -->
  <div class="otd-drawer" role="dialog" aria-modal="true" aria-label="Chase timeline">
    <!-- Header -->
    <div class="otd-header">
      <span class="otd-title">Chase Timeline</span>
      <button class="otd-close" onclick={() => onClose?.()} aria-label="Close">
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <path d="M2 2l10 10M12 2L2 12" stroke="currentColor" stroke-width="1.8"
                stroke-linecap="round"/>
        </svg>
      </button>
    </div>

    <!-- Order sections -->
    <div class="otd-body">
      {#if grouped.length === 0}
        <div class="otd-empty">No open chase orders</div>
      {:else}
        {#each grouped as section (section.order_id)}
          {@const terminal = isTerminal(section.events ?? [])}
          <div class="otd-section {terminal ? 'otd-section-terminal' : ''}">
            <!-- Order header -->
            <div class="otd-order-header">
              <span class="otd-symbol">{formatSymbol(section.symbol)}</span>
              <span class="otd-side {section.side?.toUpperCase() === 'SELL' ? 'otd-side-sell' : 'otd-side-buy'}"
              >{section.side?.toUpperCase() ?? ''}</span>
              <span class="otd-qty">{section.qty}</span>
              <span class="otd-mode-pill {modeCls(section.mode)}"
              >{(section.mode ?? '').toUpperCase()}</span>
            </div>
            <!-- Event rows — reverse-chronological -->
            <div class="otd-events">
              {#each [...(section.events ?? [])].reverse() as ev}
                <div class="otd-event-row">
                  <span class="otd-ev-time">{shortTime(ev.created_at ?? ev.timestamp)}</span>
                  <span class="otd-ev-kind"
                        style="color:{KIND_COLOR[ev.kind] ?? '#94a3b8'};background:{KIND_BG[ev.kind] ?? 'rgba(148,163,184,0.1)'}"
                  >{ev.kind ?? ''}</span>
                  {#if ev.price != null || ev.limit_price != null}
                    <span class="otd-ev-price">₹{priceFmt(ev.price ?? ev.limit_price)}</span>
                  {/if}
                </div>
              {/each}
            </div>
          </div>
        {/each}
      {/if}
    </div>
  </div>
{/if}

<style>
  /* Semi-transparent backdrop */
  .otd-backdrop {
    position: fixed;
    inset: 0;
    z-index: var(--z-drawer);
    background: rgba(0, 0, 0, 0.45);
  }

  /* Drawer panel — right-edge, 360px, slides in from the right */
  .otd-drawer {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: calc(var(--z-drawer) + 1);
    width: min(360px, 100vw);
    background: #0d1829;
    border-left: 1px solid var(--algo-amber-border-soft);
    display: flex;
    flex-direction: column;
    box-shadow: -4px 0 24px rgba(0, 0, 0, 0.6);
    animation: otd-slide-in 0.18s ease-out;
  }
  @keyframes otd-slide-in {
    from { transform: translateX(100%); }
    to   { transform: translateX(0); }
  }
  @media (prefers-reduced-motion: reduce) {
    .otd-drawer { animation: none; }
  }

  /* Header */
  .otd-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.6rem 0.85rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.2);
    background: #0a1020;
    flex-shrink: 0;
  }
  .otd-title {
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    font-weight: 700;
    color: var(--c-action);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .otd-close {
    background: transparent;
    border: none;
    cursor: pointer;
    color: rgba(180, 200, 230, 0.7);
    padding: 0.15rem;
    border-radius: 0.15rem;
    display: flex;
    align-items: center;
    transition: color 0.08s;
    outline: none;
  }
  .otd-close:hover { color: var(--c-short); }

  /* Scrollable body */
  .otd-body {
    flex: 1;
    overflow-y: auto;
    padding: 0.5rem;
    scrollbar-width: thin;
    scrollbar-color: rgba(251, 191, 36, 0.35) transparent;
  }

  .otd-empty {
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    color: rgba(180, 200, 230, 0.45);
    text-align: center;
    padding: 2rem 1rem;
  }

  /* Per-order section */
  .otd-section {
    background: linear-gradient(180deg, #1a2540 0%, #152033 100%);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 5px;
    margin-bottom: 0.5rem;
    overflow: hidden;
  }
  /* Terminal orders — muted */
  .otd-section-terminal {
    opacity: 0.52;
  }

  /* Order header row */
  .otd-order-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    background: rgba(255, 255, 255, 0.03);
  }
  .otd-symbol {
    font-family: var(--font-numeric);
    font-size: var(--fs-md);
    font-weight: 700;
    color: var(--algo-slate);
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .otd-side {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.06em;
  }
  .otd-qty {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: rgba(200, 216, 240, 0.7);
  }
  .otd-mode-pill {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 0.1rem 0.35rem;
    border-radius: 9999px;
    border: 1px solid;
    flex-shrink: 0;
  }

  /* Event rows */
  .otd-events {
    padding: 0.3rem 0.5rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }
  .otd-event-row {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    min-height: 1.3rem;
  }
  .otd-ev-time {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    color: rgba(180, 200, 230, 0.5);
    flex-shrink: 0;
    width: 5.5rem;
    font-variant-numeric: tabular-nums;
  }
  .otd-ev-kind {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 0.1rem 0.3rem;
    border-radius: 3px;
    flex-shrink: 0;
  }
  .otd-ev-price {
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    color: var(--algo-slate);
    font-variant-numeric: tabular-nums;
    margin-left: auto;
  }

  /* Side colors — matches ChaseCard .cc-side-buy / .cc-side-sell */
  .otd-side-buy  { color: var(--algo-green, var(--c-long)); }
  .otd-side-sell { color: var(--algo-red,   var(--c-short)); }

  /* Mode pill colors — matches LogPanel + CLAUDE.md palette */
  .otd-mode-sim     { color: var(--c-action); background: rgba(251,191,36,0.15);  border-color: var(--c-action); }
  .otd-mode-paper   { color: #38bdf8; background: rgba(56,189,248,0.15);  border-color: #38bdf8; }
  .otd-mode-live    { color: var(--c-long); background: rgba(74,222,128,0.15);  border-color: var(--c-long); }
  .otd-mode-unknown { color: #94a3b8; background: rgba(148,163,184,0.15); border-color: #94a3b8; }
</style>
