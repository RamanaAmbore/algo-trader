<!--
  OrderNotifications — bell icon with unread badge + dropdown panel
  listing recent order + chase events. Drops into every algo page's
  .page-header after the timestamp.

  Visual contract:
    - Bell icon (heroicon-style outline), 0.85rem, sky/champagne
      accent matching the algo navbar style.
    - Red dot + count badge when there are unseen events; hidden
      when count = 0.
    - Click → popover-style dropdown anchored top-right, listing the
      last 30 events with time + kind chip + message. Click × or
      outside to dismiss; opening the panel marks every visible
      event as seen, clearing the badge.

  Data source: global `orderEventsStore` polled by stores.js — one
  poll shared across every page. The poller starts on first mount
  of any OrderNotifications instance; subsequent mounts are no-ops.
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import {
    orderEventsStore, orderUnreadCount, markOrderEventsSeen,
    startOrderEventsPoller, logTime,
  } from '$lib/stores';
  import ChartModal from '$lib/ChartModal.svelte';

  let open = $state(false);
  let _chartModalSym  = $state('');
  let _chartModalExch = $state('');
  function _openChart(/** @type {string} */ symbol, /** @type {string} */ exchange = '') {
    _chartModalSym  = String(symbol  || '').toUpperCase();
    _chartModalExch = String(exchange || '');
  }
  /** @type {HTMLElement|null} */
  let panelEl = /** @type {HTMLElement|null} */ ($state(null));
  /** @type {HTMLElement|null} */
  let btnEl   = /** @type {HTMLElement|null} */ ($state(null));
  // Dynamic anchor — see AgentNotifications for the rationale. When
  // the icon is in the LEFT half of the viewport the panel anchors
  // its LEFT edge to the icon (extends right); when on the RIGHT
  // half it anchors the RIGHT edge (extends left). Keeps the panel
  // on-screen regardless of icon position.
  let _anchorRight = $state(true);
  function _recomputeAnchor() {
    if (!btnEl || typeof window === 'undefined') return;
    const r = btnEl.getBoundingClientRect();
    _anchorRight = (r.left + r.right) / 2 > window.innerWidth / 2;
  }

  // Mirror the global store into local $state — keeps the template
  // reactive without a $:-dance every render.
  let events = $state(/** @type {any[]} */ ([]));
  let unread = $state(0);
  /** @type {(() => void) | null} */
  let _unsubEvents = null;
  /** @type {(() => void) | null} */
  let _unsubUnread = null;

  onMount(() => {
    startOrderEventsPoller();
    _unsubEvents = orderEventsStore.subscribe(v => { events = v || []; });
    _unsubUnread = orderUnreadCount.subscribe(v => { unread = v || 0; });
    if (typeof document !== 'undefined') {
      document.addEventListener('click', onDocClick, true);
    }
  });

  onDestroy(() => {
    _unsubEvents?.();
    _unsubUnread?.();
    if (typeof document !== 'undefined') {
      document.removeEventListener('click', onDocClick, true);
    }
  });

  function _close() {
    open = false;
    markOrderEventsSeen();
  }

  function onDocClick(/** @type {MouseEvent} */ e) {
    if (!open) return;
    const t = /** @type {Node} */ (e.target);
    if (panelEl?.contains(t) || btnEl?.contains(t)) return;
    _close();
  }

  function toggle() {
    if (open) {
      _close();
    } else {
      open = true;
      _recomputeAnchor();
    }
  }

  const KIND_LABEL = {
    placed:           'PLACED',
    chase_modify:     'CHASE',
    fill:             'FILLED',
    unfill:           'UNFILLED',
    reject:           'REJECTED',
    cancel:           'CANCELLED',
    postback:         'POSTBACK',
    margin_check:     'MARGIN',
    preflight_ok:     'PRE-OK',
    preflight_block:  'PRE-BLOCK',
    error:            'ERROR',
  };
  /** @param {string} k */
  function kindLabel(k) { return /** @type {any} */ (KIND_LABEL)[k] || (k || '').toUpperCase(); }

  // Terminal kinds — when any of these lands for an order, the
  // order's chase / lifecycle is done. Used to pick the per-order
  // status badge and to colour the group header.
  const TERMINAL_KINDS = new Set(['fill', 'unfill', 'reject', 'cancel']);

  /** Group flat events into per-order flows.
   *  Returns an array of {order_id, header, events[], latestTs,
   *  terminalKind} sorted by latestTs DESC. Within each group,
   *  events are sorted by ts DESC so the newest lifecycle step is
   *  at the top of the card. */
  const groups = $derived.by(() => {
    /** @type {Map<number, any[]>} */
    const byOrder = new Map();
    for (const e of (events || [])) {
      const oid = Number(e?.order_id);
      if (!Number.isFinite(oid)) continue;
      if (!byOrder.has(oid)) byOrder.set(oid, []);
      byOrder.get(oid)?.push(e);
    }
    /** @type {Array<{order_id:number, header:string, events:any[], latestTs:number, terminalKind:string|null, symbol:string, side:string, qty:string}>} */
    const out = [];
    for (const [oid, list] of byOrder.entries()) {
      // Newest first inside each card.
      const sorted = [...list].sort((a, b) => Date.parse(b.ts) - Date.parse(a.ts));
      const latestTs = sorted.length ? Date.parse(sorted[0].ts) : 0;
      // Latest terminal kind (if any). Walk from oldest to newest so
      // a later cancel/fill replaces an earlier one — terminal kinds
      // technically shouldn't restack, but a re-modify path could
      // emit one before another.
      let terminalKind = null;
      for (const e of [...sorted].reverse()) {
        if (TERMINAL_KINDS.has(e.kind)) terminalKind = e.kind;
      }
      // Derive a one-line order header from the first PLACED event's
      // message, then fall back to whatever the most recent message
      // carries. Placed messages look like:
      //   "[PAPER] slug BUY 50 NIFTY24500CE registered with chase engine"
      const placedRow = [...sorted].reverse().find(e => e.kind === 'placed');
      const summary = _parseOrderSummary(placedRow?.message || sorted[0]?.message || '');
      out.push({
        order_id: oid,
        events: sorted.slice(0, 6),  // cap per-card depth
        latestTs,
        terminalKind,
        header: summary.header,
        symbol: summary.symbol,
        side:   summary.side,
        qty:    summary.qty,
      });
    }
    out.sort((a, b) => b.latestTs - a.latestTs);
    return out.slice(0, 12);  // cap groups shown to last 12 orders
  });

  /** Parse a placed-event message like
   *    "[PAPER] loss-pos-total-auto-close SELL 50 NIFTY24500CE registered with chase engine"
   *  into {header, symbol, side, qty}. Tolerant of variants — the
   *  paper engine and the manual-ticket path write slightly
   *  different prefixes, but `SIDE QTY SYMBOL` is the consistent
   *  middle section. */
  function _parseOrderSummary(/** @type {string} */ msg) {
    const out = { header: '', symbol: '', side: '', qty: '' };
    if (!msg) return out;
    // SIDE = BUY or SELL, QTY = digits, SYMBOL = up to whitespace.
    const m = msg.match(/\b(BUY|SELL)\s+(\d+)\s+([A-Z0-9.&_-]+)/);
    if (m) {
      out.side   = m[1];
      out.qty    = m[2];
      out.symbol = m[3];
      out.header = `${m[1]} ${m[2]} ${m[3]}`;
    } else {
      // Fallback: truncate the raw message.
      out.header = msg.length > 50 ? msg.slice(0, 50) + '…' : msg;
    }
    return out;
  }
</script>

<span class="onb-wrap">
  <button type="button" bind:this={btnEl}
          class="onb-btn" class:onb-btn-active={open}
          title={unread ? `${unread} new order event${unread > 1 ? 's' : ''}` : 'Order notifications'}
          aria-haspopup="dialog" aria-expanded={open}
          onclick={toggle}>
    <!-- Heroicons bell-outline, sized to match algo-ts. -->
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
         stroke-linecap="round" stroke-linejoin="round" class="onb-icon">
      <path d="M14.857 17.082a23.848 23.848 0 0 0 5.454-1.31A8.967 8.967 0 0 1 18 9.75V9A6 6 0 0 0 6 9v.75a8.967 8.967 0 0 1-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 0 1-5.714 0m5.714 0a3 3 0 1 1-5.714 0"/>
    </svg>
    {#if unread > 0}
      <span class="onb-badge">{unread > 99 ? '99+' : unread}</span>
    {/if}
  </button>

  {#if open}
    <div bind:this={panelEl}
         class="onb-panel"
         class:onb-panel-left={!_anchorRight}
         role="dialog" aria-label="Order notifications">
      <div class="onb-head">
        <span class="onb-title">Orders &amp; Chase</span>
        <button type="button" class="onb-close" aria-label="Close"
                onclick={_close}>×</button>
      </div>
      {#if groups.length === 0}
        <div class="onb-empty">No order activity yet.</div>
      {:else}
        <ul class="onb-groups">
          {#each groups as g (g.order_id)}
            <li class="onb-group onb-group-{g.terminalKind || 'open'}">
              <!-- Order header: side + qty + symbol on the left,
                   terminal status pill on the right. -->
              <div class="onb-group-head">
                <span class="onb-oid">#{g.order_id}</span>
                {#if g.side}
                  <span class="onb-side onb-side-{g.side.toLowerCase()}">{g.side}</span>
                  <span class="onb-qty">{g.qty}</span>
                  <span class="onb-sym">{g.symbol}</span>
                  {#if g.symbol}
                    <button type="button"
                            class="row-chart-btn onb-chart-btn"
                            title="Chart {g.symbol}"
                            aria-label="Open chart for {g.symbol}"
                            onclick={(e) => { e.stopPropagation(); _openChart(g.symbol); }}>
                      <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden="true">
                        <path d="M2 13h12M3 11l3-4 3 2 4-6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
                      </svg>
                    </button>
                  {/if}
                {:else}
                  <span class="onb-summary">{g.header}</span>
                {/if}
                <span class="onb-status onb-status-{g.terminalKind || 'open'}">
                  {g.terminalKind ? kindLabel(g.terminalKind) : 'OPEN'}
                </span>
              </div>
              <!-- Per-order flow: newest event at top. Bullet + line
                   timeline rendered via ::before / ::after on each
                   step (see CSS below). The message wraps under the
                   meta row at full width rather than fighting for a
                   narrow grid column. -->
              <ul class="onb-flow">
                {#each g.events as e (e.id)}
                  <li class="onb-step onb-step-{e.kind}">
                    <div class="onb-step-meta">
                      <span class="onb-ts">{logTime(e.ts)}</span>
                      <span class="onb-kind onb-kind-{e.kind}">{kindLabel(e.kind)}</span>
                    </div>
                    <div class="onb-msg">{e.message}</div>
                  </li>
                {/each}
              </ul>
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</span>

{#if _chartModalSym}
  <ChartModal
    symbol={_chartModalSym}
    exchange={_chartModalExch}
    onClose={() => { _chartModalSym = ''; _chartModalExch = ''; }} />
{/if}

<style>
  /* .page-header uses align-items: baseline; the bell's flex-button
     would otherwise baseline-align to its bottom edge and sit below
     the timestamp's text baseline. align-self: center re-centres
     it against the row, matching the visual rhythm InfoHint uses. */
  .onb-wrap { position: relative; display: inline-flex; align-items: center; align-self: center; flex-shrink: 0; }
  .onb-btn {
    position: relative;
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.5rem; height: 1.5rem;
    padding: 0;
    background: rgba(251, 191, 36, 0.08);
    border: 1px solid rgba(251, 191, 36, 0.28);
    border-radius: 0.25rem;
    color: #fbbf24;
    cursor: pointer;
    transition: background-color 0.08s, color 0.08s, border-color 0.08s;
  }
  .onb-btn:hover {
    color: #fde047;
    background: rgba(251, 191, 36, 0.18);
    border-color: rgba(251, 191, 36, 0.55);
  }
  .onb-btn-active {
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.12);
    border-color: rgba(251, 191, 36, 0.45);
  }
  .onb-icon { width: 0.95rem; height: 0.95rem; }
  .onb-badge {
    position: absolute;
    top: -2px; right: -3px;
    min-width: 0.85rem; height: 0.85rem;
    padding: 0 0.18rem;
    background: #ef4444;
    color: #fff;
    border-radius: 999px;
    font-size: 0.5rem;
    font-weight: 800;
    line-height: 0.85rem;
    letter-spacing: 0;
    font-family: ui-monospace, monospace;
    border: 1px solid rgba(13, 21, 38, 0.85);
    display: inline-flex; align-items: center; justify-content: center;
  }

  /* Dropdown panel — anchored to the right edge of the bell so it
     doesn't overflow viewport on narrow screens. z-index above the
     ag-Grid + algo cards but below modal dialogs. */
  /* Default: anchor panel's RIGHT edge to icon. .onb-panel-left flips
     to left-anchor when the icon sits in the left half of the viewport
     so the panel stays on-screen. */
  .onb-panel {
    position: absolute;
    top: calc(100% + 0.35rem);
    right: 0;
    left: auto;
    /* High z-index so the panel sits above the OptionsPayoff chart
       and any other absolutely-positioned page content. Matches the
       sibling AgentNotifications panel + the InfoHint popouts so all
       three "popover-style" surfaces win against the chart. */
    z-index: 9999;
    width: min(22rem, 92vw);
    max-height: 70vh;
    overflow-y: auto;
    background: rgba(13, 21, 38, 0.97);
    border: 1px solid rgba(251, 191, 36, 0.32);
    border-radius: 0.4rem;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
    color: #e5edf7;
    font-family: ui-monospace, monospace;
  }
  .onb-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.45rem 0.65rem 0.35rem;
    border-bottom: 1px solid rgba(251, 191, 36, 0.18);
  }
  .onb-title {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #fbbf24;
  }
  .onb-close {
    width: 1.2rem; height: 1.2rem;
    background: transparent; border: 0;
    color: rgba(200, 216, 240, 0.72);
    cursor: pointer;
    font-size: 0.95rem; line-height: 1;
    border-radius: 0.2rem;
  }
  .onb-close:hover { color: #fff; background: rgba(255, 255, 255, 0.08); }
  .onb-panel-left { left: 0; right: auto; }
  .onb-empty {
    padding: 0.9rem 0.65rem;
    font-size: 0.65rem;
    color: rgba(200, 216, 240, 0.55);
    text-align: center;
  }
  /* Per-order groups ─────────────────────────────────────────────── */
  .onb-groups { list-style: none; margin: 0; padding: 0.2rem 0; }
  .onb-group {
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    padding: 0.35rem 0.65rem 0.45rem;
  }
  .onb-group:last-child { border-bottom: 0; }
  /* Group header — order id + side + qty + symbol + terminal status. */
  .onb-group-head {
    display: flex;
    align-items: baseline;
    gap: 0.4rem;
    flex-wrap: wrap;
    font-size: 0.62rem;
    margin-bottom: 0.25rem;
  }
  .onb-oid {
    color: rgba(200, 216, 240, 0.55);
    font-size: 0.55rem;
  }
  .onb-side {
    padding: 0.02rem 0.32rem;
    border-radius: 0.18rem;
    font-size: 0.5rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    border: 1px solid;
  }
  .onb-side-buy  { color: #4ade80; background: rgba(74,222,128,0.12);  border-color: rgba(74,222,128,0.4); }
  .onb-side-sell { color: #f87171; background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.4); }
  .onb-qty { color: #fbbf24; font-weight: 700; }
  .onb-sym { color: #e5edf7; font-weight: 600; }
  .onb-summary { color: #c8d8f0; }
  /* Status pill — sits on the right via margin-left:auto so it
     stays anchored regardless of how long the symbol gets. */
  .onb-status {
    margin-left: auto;
    padding: 0.02rem 0.4rem;
    border-radius: 0.2rem;
    font-size: 0.5rem;
    font-weight: 800;
    letter-spacing: 0.06em;
    border: 1px solid;
  }
  .onb-status-open     { color: #7dd3fc; background: rgba(56,189,248,0.12);  border-color: rgba(56,189,248,0.4); }
  .onb-status-fill     { color: #4ade80; background: rgba(74,222,128,0.14);  border-color: rgba(74,222,128,0.55); }
  .onb-status-unfill,
  .onb-status-reject   { color: #f87171; background: rgba(248,113,113,0.14); border-color: rgba(248,113,113,0.55); }
  .onb-status-cancel   { color: #c8d8f0; background: rgba(200,216,240,0.10); border-color: rgba(200,216,240,0.35); }

  /* Group-level accent — a left border whose colour reflects the
     terminal status, so the operator scans the panel and the
     filled / cancelled / failed orders are instantly distinct. */
  .onb-group-fill   { box-shadow: inset 3px 0 0 0 rgba(74,222,128,0.45);  padding-left: 0.95rem; }
  .onb-group-unfill,
  .onb-group-reject { box-shadow: inset 3px 0 0 0 rgba(248,113,113,0.45); padding-left: 0.95rem; }
  .onb-group-cancel { box-shadow: inset 3px 0 0 0 rgba(200,216,240,0.35); padding-left: 0.95rem; }
  .onb-group-open   { box-shadow: inset 3px 0 0 0 rgba(56,189,248,0.45);  padding-left: 0.95rem; }

  /* Per-order flow — newest event at top. Each step is rendered as a
     timeline-style row: a coloured bullet on the left, a vertical
     connector line linking adjacent bullets, then the timestamp +
     kind chip + message wrapping under them. The connector line is
     a pseudo-element on the bullet so it visually flows top-to-bottom
     even when message bodies have different heights. */
  .onb-flow { list-style: none; margin: 0; padding: 0.15rem 0 0; position: relative; }
  .onb-step {
    position: relative;
    padding: 0.3rem 0 0.3rem 1.1rem;
    font-size: 0.6rem;
    line-height: 1.35;
  }
  /* Bullet — coloured by kind (override below). */
  .onb-step::before {
    content: '';
    position: absolute;
    left: 0.32rem;
    top: 0.55rem;
    width: 0.55rem;
    height: 0.55rem;
    border-radius: 50%;
    background: #7e97b8;
    border: 2px solid rgba(13, 21, 38, 0.95);
    box-shadow: 0 0 0 1px rgba(200, 216, 240, 0.35);
    z-index: 1;
  }
  /* Connector — a vertical line from this bullet down through the
     next bullet. Last child drops the line via the empty-of-next
     state below. */
  .onb-step::after {
    content: '';
    position: absolute;
    left: 0.575rem;
    top: 1.05rem;
    bottom: -0.3rem;
    width: 1px;
    background: rgba(200, 216, 240, 0.18);
  }
  .onb-step:last-child::after { display: none; }

  /* Per-kind bullet colours. Inherits from the kind chip palette so
     a green bullet on a filled step reads consistently with the
     green FILLED chip on the same line. */
  .onb-step-fill::before          { background: #4ade80; box-shadow: 0 0 0 1px rgba(74,222,128,0.55); }
  .onb-step-placed::before        { background: #7dd3fc; box-shadow: 0 0 0 1px rgba(125,211,252,0.55); }
  .onb-step-chase_modify::before  { background: #fbbf24; box-shadow: 0 0 0 1px rgba(251,191,36,0.55); }
  .onb-step-unfill::before,
  .onb-step-reject::before,
  .onb-step-error::before         { background: #f87171; box-shadow: 0 0 0 1px rgba(248,113,113,0.55); }
  .onb-step-cancel::before        { background: #c8d8f0; box-shadow: 0 0 0 1px rgba(200,216,240,0.45); }
  .onb-step-postback::before      { background: #c4b5fd; box-shadow: 0 0 0 1px rgba(196,181,253,0.55); }
  .onb-step-margin_check::before,
  .onb-step-preflight_ok::before,
  .onb-step-preflight_block::before { background: #a78bfa; box-shadow: 0 0 0 1px rgba(167,139,250,0.4); }

  /* Two-row layout inside each step: meta line (ts + kind chip)
     followed by message at full width. Matches the agent log
     pattern so both popovers feel like one component family. */
  .onb-step .onb-step-meta {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  .onb-step .onb-msg {
    color: #e5edf7;
    overflow-wrap: anywhere;
    margin-top: 0.1rem;
  }
  .onb-ts {
    color: rgba(200, 216, 240, 0.55);
    font-size: 0.52rem;
    white-space: nowrap;
  }
  .onb-kind {
    padding: 0.02rem 0.32rem;
    border-radius: 0.2rem;
    font-size: 0.5rem;
    font-weight: 700;
    letter-spacing: 0.05em;
    white-space: nowrap;
    border: 1px solid;
  }
  .onb-kind-fill          { color: #4ade80; background: rgba(74,222,128,0.12);  border-color: rgba(74,222,128,0.4); }
  .onb-kind-placed        { color: #7dd3fc; background: rgba(56,189,248,0.12);  border-color: rgba(56,189,248,0.4); }
  .onb-kind-chase_modify  { color: #fbbf24; background: rgba(251,191,36,0.12);  border-color: rgba(251,191,36,0.4); }
  .onb-kind-unfill,
  .onb-kind-reject,
  .onb-kind-error         { color: #f87171; background: rgba(248,113,113,0.12); border-color: rgba(248,113,113,0.4); }
  .onb-kind-cancel        { color: #c8d8f0; background: rgba(200,216,240,0.08); border-color: rgba(200,216,240,0.3); }
  .onb-kind-postback      { color: #c4b5fd; background: rgba(167,139,250,0.12); border-color: rgba(167,139,250,0.4); }
  .onb-kind-margin_check,
  .onb-kind-preflight_ok,
  .onb-kind-preflight_block { color: #a78bfa; background: rgba(167,139,250,0.08); border-color: rgba(167,139,250,0.3); }

  .onb-msg {
    color: #e5edf7;
    overflow-wrap: anywhere;
  }
  .onb-sym {
    color: rgba(200, 216, 240, 0.7);
    font-size: 0.55rem;
    white-space: nowrap;
  }
  /* Chart button sits inside the flex group header — align with the
     surrounding text; no extra margin-left so it doesn't push the
     status pill too far right. */
  .onb-chart-btn {
    width: 1rem;
    height: 1rem;
    margin-left: 0.1rem;
    flex-shrink: 0;
  }
  .onb-chart-btn :global(svg) { pointer-events: none; }
</style>
