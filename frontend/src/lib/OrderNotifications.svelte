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

  let open = $state(false);
  /** @type {HTMLElement|null} */
  let panelEl = /** @type {HTMLElement|null} */ ($state(null));
  /** @type {HTMLElement|null} */
  let btnEl   = /** @type {HTMLElement|null} */ ($state(null));

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

  function onDocClick(/** @type {MouseEvent} */ e) {
    if (!open) return;
    const t = /** @type {Node} */ (e.target);
    if (panelEl?.contains(t) || btnEl?.contains(t)) return;
    open = false;
  }

  function toggle() {
    open = !open;
    if (open) markOrderEventsSeen();
  }

  // Most-recent first for display — store is oldest-first within the
  // window so it stays compatible with append-style polling.
  const display = $derived([...events].reverse().slice(0, 30));

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
    <div bind:this={panelEl} class="onb-panel" role="dialog" aria-label="Order notifications">
      <div class="onb-head">
        <span class="onb-title">Orders &amp; Chase</span>
        <button type="button" class="onb-close" aria-label="Close"
                onclick={() => { open = false; }}>×</button>
      </div>
      {#if display.length === 0}
        <div class="onb-empty">No events yet.</div>
      {:else}
        <ul class="onb-list">
          {#each display as e (e.id)}
            <li class="onb-row onb-row-{e.kind}">
              <span class="onb-ts">{logTime(e.ts)}</span>
              <span class="onb-kind onb-kind-{e.kind}">{kindLabel(e.kind)}</span>
              <span class="onb-msg">{e.message}</span>
              {#if e.tradingsymbol}
                <span class="onb-sym">{e.tradingsymbol}</span>
              {/if}
            </li>
          {/each}
        </ul>
      {/if}
    </div>
  {/if}
</span>

<style>
  .onb-wrap { position: relative; display: inline-flex; align-items: center; flex-shrink: 0; }
  .onb-btn {
    position: relative;
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.5rem; height: 1.5rem;
    padding: 0;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 0.25rem;
    color: rgba(200, 216, 240, 0.78);
    cursor: pointer;
    transition: background-color 0.08s, color 0.08s, border-color 0.08s;
  }
  .onb-btn:hover {
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.08);
    border-color: rgba(251, 191, 36, 0.28);
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
  .onb-panel {
    position: absolute;
    top: calc(100% + 0.35rem);
    right: 0;
    z-index: 45;
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
  .onb-empty {
    padding: 0.9rem 0.65rem;
    font-size: 0.65rem;
    color: rgba(200, 216, 240, 0.55);
    text-align: center;
  }
  .onb-list { list-style: none; margin: 0; padding: 0.2rem 0; }
  .onb-row {
    display: grid;
    grid-template-columns: auto auto 1fr auto;
    gap: 0.4rem;
    align-items: baseline;
    padding: 0.28rem 0.65rem;
    font-size: 0.6rem;
    line-height: 1.25;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  }
  .onb-row:last-child { border-bottom: 0; }
  .onb-ts {
    color: rgba(200, 216, 240, 0.45);
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
</style>
