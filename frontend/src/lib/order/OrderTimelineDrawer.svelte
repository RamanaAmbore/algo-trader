<script>
  /**
   * OrderTimelineDrawer — right-edge slide-in drawer showing live event
   * timeline for every OPEN chase order.
   *
   * Props:
   *   open          {boolean}   — whether the drawer is visible
   *   orders        {Array}     — flat AlgoOrderEventInfo[] from
   *                                fetchOrderEvents (id, order_id, ts, kind,
   *                                message, payload_json — see
   *                                orderTimelineLogic.js header comment)
   *   orderContext  {Object}    — {[order_id]: {symbol, side, qty, mode}},
   *                                built by the caller from AlgoOrderInfo
   *                                rows (fetchAlgoOrdersRecent). Events
   *                                carry no symbol/side/qty/mode of their
   *                                own — see orderTimelineLogic.js.
   *   onClose       {Function}  — called when the drawer should be dismissed
   *   linkedOrders  {Object|null} — optional, additive (OrderBook.svelte's
   *                                per-order timeline view only — the
   *                                navbar chase-chip caller never passes
   *                                this). Shape: { parent_order_id?,
   *                                child_order_ids?: number[], basket_tag? }.
   *                                Renders a chip strip above the event
   *                                list when any field is set.
   *   onSelectLinked {Function}  — optional, called with an order id when
   *                                a parent/child chip is clicked, so the
   *                                caller can re-fetch and re-render this
   *                                same drawer instance for that order.
   */
  import { onMount, onDestroy } from 'svelte';
  import { priceFmt } from '$lib/format';
  import { logTime } from '$lib/stores';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { groupOrderEvents, isTerminalSection } from '$lib/order/orderTimelineLogic.js';

  const {
    open = false, orders = [], orderContext = {}, onClose,
    linkedOrders = null, onSelectLinked = undefined,
  } = $props();

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

  /** CSS class suffix for the mode pill — matches LogPanel + CLAUDE.md
   *  palette. No `'paper'` fallback — an order with no resolved context
   *  (yet) renders as explicitly unknown, never silently mislabeled. */
  function modeCls(/** @type {string} */ mode) {
    if (mode === 'sim')    return 'otd-mode-sim';
    if (mode === 'paper')  return 'otd-mode-paper';
    if (mode === 'live')   return 'otd-mode-live';
    if (mode === 'shadow') return 'otd-mode-shadow';
    if (mode === 'replay') return 'otd-mode-replay';
    if (mode === 'draft')  return 'otd-mode-draft';
    return 'otd-mode-unknown';
  }

  /** CSS class suffix for the side pill — neutral when side is unresolved,
   *  never defaults to BUY's green. */
  function sideCls(/** @type {string} */ side) {
    const s = (side || '').toUpperCase();
    if (s === 'SELL') return 'otd-side-sell';
    if (s === 'BUY')  return 'otd-side-buy';
    return 'otd-side-unknown';
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

  /** Group the flat AlgoOrderEventInfo[] by order_id, merge in per-order
   *  context, and sort (non-terminal first; newest-activity first within
   *  group) — see orderTimelineLogic.js for the full rationale. */
  const grouped = $derived.by(() => groupOrderEvents(orders, orderContext));

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

  // Additive chip strip — null/empty linkedOrders (the existing
  // navbar chase-chip caller never passes it) renders nothing at all.
  const _hasLinked = $derived(!!(linkedOrders && (
    linkedOrders.parent_order_id != null
    || (linkedOrders.child_order_ids ?? []).length > 0
    || linkedOrders.basket_tag
  )));
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

    <!-- Linked-orders chip strip — parent/child/basket, additive only
         (OrderBook.svelte's per-order view; navbar chase-chip drawer
         never passes linkedOrders so this never renders there). -->
    {#if _hasLinked}
      <div class="otd-linked" role="group" aria-label="Linked orders">
        {#if linkedOrders.parent_order_id != null}
          <button type="button" class="otd-chip otd-chip-link"
            onclick={() => onSelectLinked?.(linkedOrders.parent_order_id)}>
            Parent: #{linkedOrders.parent_order_id}
          </button>
        {/if}
        {#each (linkedOrders.child_order_ids ?? []) as cid}
          <button type="button" class="otd-chip otd-chip-link"
            onclick={() => onSelectLinked?.(cid)}>
            Child: #{cid}
          </button>
        {/each}
        {#if linkedOrders.basket_tag}
          <!-- Informational only — a basket_tag isn't a single order id,
               and no existing tag→member-ids lookup exists anywhere in
               the codebase to wire a click handler to (checked
               orders_basket.py / api.js — basket dispatch is fire-and-
               forget per-account, no reverse lookup route). -->
          <span class="otd-chip otd-chip-basket" title={linkedOrders.basket_tag}
          >{linkedOrders.basket_tag}</span>
        {/if}
      </div>
    {/if}

    <!-- Order sections -->
    <div class="otd-body">
      {#if grouped.length === 0}
        <div class="otd-empty">No open chase orders</div>
      {:else}
        {#each grouped as section (section.order_id)}
          {@const terminal = isTerminalSection(section.events ?? [])}
          <div class="otd-section {terminal ? 'otd-section-terminal' : ''}">
            <!-- Order header -->
            <div class="otd-order-header">
              <span class="otd-symbol"
              >{section.symbol ? formatSymbol(section.symbol) : `Order #${section.order_id}`}</span>
              {#if section.side}
                <span class="otd-side {sideCls(section.side)}">{section.side.toUpperCase()}</span>
              {/if}
              {#if section.qty != null}
                <span class="otd-qty">{section.qty}</span>
              {/if}
              <span class="otd-mode-pill {modeCls(section.mode)}"
              >{section.mode ? section.mode.toUpperCase() : '—'}</span>
            </div>
            <!-- Event rows — reverse-chronological -->
            <div class="otd-events">
              {#each [...(section.events ?? [])].reverse() as ev}
                <div class="otd-event-row">
                  <span class="otd-ev-time">{shortTime(ev.ts)}</span>
                  <span class="otd-ev-kind"
                        style="color:{KIND_COLOR[ev.kind] ?? '#94a3b8'};background:{KIND_BG[ev.kind] ?? 'rgba(148,163,184,0.1)'}"
                  >{ev.kind ?? ''}</span>
                  {#if ev.price != null}
                    <span class="otd-ev-price">₹{priceFmt(ev.price)}</span>
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
  /* Semi-transparent backdrop. Modal-dim audit (2026-10-02): was a
     stray rgba(0,0,0,0.45) — this is a lighter right-edge slide-in
     panel, not a centered dialog, so it takes the lighter canonical
     `.canonical-modal-overlay` dim value (rgba(8,12,20,0.42), no blur)
     instead of ModalShell's heavier 0.72+blur(2px). */
  .otd-backdrop {
    position: fixed;
    inset: 0;
    z-index: var(--z-drawer);
    background: rgba(8, 12, 20, 0.42);
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
    background: var(--algo-bg-elev1);
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
    /* Was borderless/transparent at rest — operator complaint (2026-09):
       close icons should carry a fill at rest, not just on hover. */
    background: var(--close-btn-neutral-bg);
    border: none;
    cursor: pointer;
    color: rgba(180, 200, 230, 0.7);
    padding: 0.15rem;
    border-radius: 3px;
    display: flex;
    align-items: center;
    transition: color 0.08s, background 0.08s;
    outline: none;
  }
  .otd-close:hover { color: var(--c-short); background: var(--close-btn-neutral-bg-hover); }

  /* Linked-orders chip strip — its own fully-rounded pill shape
     (independent of .otd-mode-pill, which uses a sharp 2px radius);
     clickable chips get the sky "info" treatment (CLAUDE.md palette),
     basket tag (non-clickable) gets the violet "postback" treatment
     already used for the postback event kind above. */
  .otd-linked {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    padding: 0.45rem 0.6rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    flex-shrink: 0;
  }
  .otd-chip {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.03em;
    padding: 0.15rem 0.5rem;
    border-radius: 9999px;
    border: 1px solid;
  }
  .otd-chip-link {
    color: #7dd3fc;
    background: rgba(125, 211, 252, 0.12);
    border-color: rgba(125, 211, 252, 0.4);
    cursor: pointer;
  }
  .otd-chip-link:hover { background: rgba(125, 211, 252, 0.22); }
  .otd-chip-basket {
    color: #a78bfa;
    background: rgba(167, 139, 250, 0.12);
    border-color: rgba(167, 139, 250, 0.4);
    cursor: default;
  }

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
    /* A3 (2026-09 audit) — stale rgba(200,216,240,α); alpha preserved. */
    color: color-mix(in srgb, var(--algo-slate) 70%, transparent);
  }
  /* Radius unified to 2px (audit fix) — matches LogPanel's .mode-pill
     and ChaseCard's .cc-mode (2 of 3 mode-pill surfaces already agreed
     on a sharp-cornered pill; this was the sole fully-rounded outlier). */
  .otd-mode-pill {
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.05em;
    padding: 0 0.3rem;
    border-radius: 2px;
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
  .otd-side-buy     { color: var(--algo-green, var(--c-long)); }
  .otd-side-sell    { color: var(--algo-red,   var(--c-short)); }
  .otd-side-unknown { color: rgba(180, 200, 230, 0.5); }

  /* Mode pill colors — matches the navbar MODE_COLOR (canonical map in
     +layout.svelte), LogPanel's .mode-pill-*, and ChaseCard's .cc-mode-*.
     SIM/REPLAY green (sandbox/safe), PAPER sky, SHADOW orange, LIVE red —
     LIVE is the only mode that moves real money; it was previously green
     here, the exact footgun the navbar's own comment calls out. */
  .otd-mode-sim     { color: var(--c-long); background: rgba(74,222,128,0.15);  border-color: var(--c-long); }
  .otd-mode-replay  { color: var(--c-long); background: rgba(74,222,128,0.15);  border-color: var(--c-long); }
  .otd-mode-paper   { color: var(--algo-sky); background: var(--algo-sky-bg);  border-color: var(--algo-sky-border); }
  .otd-mode-shadow  { color: var(--algo-orange); background: rgba(251,146,60,0.15);  border-color: var(--algo-orange); }
  .otd-mode-live    { color: var(--c-short); background: rgba(248,113,113,0.15);  border-color: var(--c-short); }
  /* DRAFT — never placed, no broker round-trip. Dashed + muted, distinct
     from otd-mode-unknown's solid slate so the two don't read the same. */
  .otd-mode-draft   { color: var(--algo-muted); background: rgba(126,151,184,0.10); border-color: rgba(126,151,184,0.45); border-style: dashed; }
  .otd-mode-unknown { color: #94a3b8; background: rgba(148,163,184,0.15); border-color: #94a3b8; }
</style>
