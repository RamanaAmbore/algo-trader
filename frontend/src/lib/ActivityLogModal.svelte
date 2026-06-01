<script>
  /**
   * ActivityLogModal — combined Order Book + Agent Log in a modal overlay.
   * Replaces the old bell popovers with a single dedicated surface.
   *
   * Tab "Orders" — recent AlgoOrders polled every 5 s while open.
   * Tab "Agents" — UnifiedLog filtered to agent_fire / action events.
   *
   * Dismissal: Esc key, overlay click, or × button.
   */

  import { onMount, onDestroy } from 'svelte';
  import UnifiedLog from '$lib/UnifiedLog.svelte';
  import { fetchAlgoOrdersRecent } from '$lib/api';
  import { visibleInterval, logTime } from '$lib/stores';
  import { portal } from '$lib/portal';

  let {
    /** @type {() => void} */
    onClose,
  } = $props();

  /** @type {'orders' | 'agents'} */
  let tab = $state('orders');

  // ── Order book data ───────────────────────────────────────────────────
  /** @type {any[]} */
  let orders = $state([]);
  let ordersLoading = $state(false);
  let ordersError = $state('');

  async function loadOrders() {
    if (ordersLoading) return;
    ordersLoading = true;
    ordersError = '';
    try {
      const res = await fetchAlgoOrdersRecent(50, 'all');
      orders = Array.isArray(res) ? res : (res?.orders ?? []);
    } catch (e) {
      ordersError = 'Could not load orders.';
    } finally {
      ordersLoading = false;
    }
  }

  // ── Keyboard + scroll-lock ────────────────────────────────────────────
  /** @type {(() => void) | null} */
  let _stopPoll = null;

  onMount(() => {
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', _onKey);
    loadOrders();
    // Poll every 5 s while the modal is open.
    _stopPoll = visibleInterval(loadOrders, 5000);
  });

  onDestroy(() => {
    document.body.style.overflow = '';
    window.removeEventListener('keydown', _onKey);
    _stopPoll?.();
  });

  function _onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') onClose();
  }

  // ── Order helpers ─────────────────────────────────────────────────────
  const STATUS_CLASS = /** @type {Record<string,string>} */ ({
    OPEN:    'alm-o-open',
    FILLED:  'alm-o-filled',
    UNFILLED:'alm-o-unfilled',
    REJECTED:'alm-o-rejected',
    CANCELLED:'alm-o-cancelled',
  });
  const MODE_CLASS = /** @type {Record<string,string>} */ ({
    sim:   'alm-m-sim',
    paper: 'alm-m-paper',
    live:  'alm-m-live',
    shadow:'alm-m-shadow',
  });
  /** @param {string} s */
  function statusClass(s) { return STATUS_CLASS[s] ?? 'alm-o-open'; }
  /** @param {string} m */
  function modeClass(m) { return MODE_CLASS[m] ?? 'alm-m-paper'; }
</script>

<!-- overlay is pointer-events:none so the page underneath stays clickable;
     X button + Esc are the close affordances. -->
<div class="alm-overlay" use:portal role="dialog" aria-modal="true" aria-label="Activity log"
     tabindex="-1">
  <div class="alm-modal">

    <!-- Header ────────────────────────────────────────────────────── -->
    <div class="alm-header">
      <span class="alm-title">Activity Log</span>
      <div class="alm-tabs" role="tablist">
        <button class="alm-tab" class:alm-tab-on={tab === 'orders'}
                role="tab" aria-selected={tab === 'orders'}
                onclick={() => tab = 'orders'}>
          Order Book
        </button>
        <button class="alm-tab" class:alm-tab-on={tab === 'agents'}
                role="tab" aria-selected={tab === 'agents'}
                onclick={() => tab = 'agents'}>
          Agent Log
        </button>
      </div>
      <button class="alm-close" onclick={onClose} aria-label="Close activity log">×</button>
    </div>

    <!-- Body ──────────────────────────────────────────────────────── -->
    <div class="alm-body">

      {#if tab === 'orders'}
        {#if ordersError}
          <div class="alm-err">{ordersError}</div>
        {:else if orders.length === 0 && !ordersLoading}
          <div class="alm-empty">No recent orders.</div>
        {:else}
          <table class="alm-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Mode</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Symbol</th>
                <th>Status</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {#each orders as o (o.id)}
                <tr>
                  <td class="alm-td-ts">{logTime(o.created_at ?? o.placed_at ?? '')}</td>
                  <td><span class="alm-pill {modeClass(o.mode ?? 'paper')}">{(o.mode ?? 'paper').toUpperCase()}</span></td>
                  <td>
                    <span class="alm-side alm-side-{(o.side ?? '').toLowerCase()}">
                      {o.side ?? '—'}
                    </span>
                  </td>
                  <td class="alm-td-num">{o.quantity ?? o.qty ?? '—'}</td>
                  <td class="alm-td-sym">{o.tradingsymbol ?? o.symbol ?? '—'}</td>
                  <td><span class="alm-pill {statusClass(o.status ?? 'OPEN')}">{o.status ?? 'OPEN'}</span></td>
                  <td class="alm-td-detail">{o.detail ?? ''}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}

      {:else}
        <!-- Agent log — filter to fires + action events, newest first. -->
        <UnifiedLog
          filter={{ kinds: ['agent_fire', 'agent_action_success', 'agent_action_error', 'agent_state'] }}
          pollMs={5000}
          maxRows={50}
          emptyMessage="No recent agent events."
          heightClass="h-full"
          cardMode={true}
          tsFormat="short"
        />
      {/if}

    </div>
  </div>
</div>

<style>
  /* ── Overlay ──────────────────────────────────────────────────── */
  .alm-overlay {
    position: fixed;
    inset: 0;
    z-index: 10000;
    /* No background dim and pointer-events: none — the page underneath
       stays clickable while the modal is open. Operator closes via the
       × button at the top-right or Esc. Same non-blocking pattern the
       ChartModal uses. */
    pointer-events: none;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
    box-sizing: border-box;
  }

  /* ── Modal panel ─────────────────────────────────────────────── */
  .alm-modal {
    background: linear-gradient(160deg, #0d1526 0%, #0a1020 100%);
    border: 1px solid rgba(168, 85, 247, 0.38);
    border-radius: 0.55rem;
    box-shadow: 0 20px 60px rgba(0, 0, 0, 0.65), 0 0 0 1px rgba(168, 85, 247, 0.12);
    /* Restore pointer-events so the panel itself stays interactive
       even though the overlay around it is not. */
    pointer-events: auto;
    width: min(56rem, 96vw);
    max-height: 86vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── Header ──────────────────────────────────────────────────── */
  .alm-header {
    display: flex;
    align-items: center;
    gap: 0.65rem;
    padding: 0.55rem 0.8rem;
    border-bottom: 1px solid rgba(168, 85, 247, 0.22);
    flex-shrink: 0;
  }
  .alm-title {
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: #c4b5fd;
    white-space: nowrap;
  }
  .alm-tabs {
    display: flex;
    gap: 0.15rem;
  }
  .alm-tab {
    padding: 0.22rem 0.65rem;
    font-size: 0.62rem;
    font-weight: 600;
    background: transparent;
    border: 1px solid rgba(200, 216, 240, 0.18);
    border-radius: 0.25rem;
    color: rgba(200, 216, 240, 0.6);
    cursor: pointer;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
  }
  .alm-tab:hover:not(.alm-tab-on) {
    background: rgba(200, 216, 240, 0.08);
    color: #c8d8f0;
    border-color: rgba(200, 216, 240, 0.35);
  }
  .alm-tab-on {
    background: rgba(168, 85, 247, 0.16);
    color: #c4b5fd;
    border-color: rgba(168, 85, 247, 0.55);
  }
  .alm-close {
    margin-left: auto;
    /* Bigger touch target — was 1.35rem (~22px) which fell short of
       the 44px mobile tap-target floor. Operator reported the X not
       firing reliably on phone. Also bumped z-index so nothing inside
       the header can overlay it. */
    width: 1.8rem;
    height: 1.8rem;
    background: transparent;
    border: 1px solid rgba(255, 255, 255, 0.10);
    border-radius: 4px;
    color: rgba(200, 216, 240, 0.85);
    cursor: pointer;
    font-size: 1.1rem;
    flex-shrink: 0;
    position: relative;
    z-index: 1;
    line-height: 1;
    border-radius: 0.2rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
  }
  .alm-close:hover {
    color: #fff;
    background: rgba(255, 255, 255, 0.08);
  }

  /* ── Body ────────────────────────────────────────────────────── */
  .alm-body {
    flex: 1 1 0;
    overflow-y: auto;
    overflow-x: auto;
    padding: 0.5rem 0.1rem;
    /* UnifiedLog fills height when mounted in agents tab */
    display: flex;
    flex-direction: column;
  }
  .alm-err {
    padding: 0.7rem 0.8rem;
    font-size: 0.65rem;
    color: #f87171;
  }
  .alm-empty {
    padding: 1.2rem 0.8rem;
    font-size: 0.65rem;
    color: rgba(200, 216, 240, 0.5);
    text-align: center;
  }

  /* ── Order book table ────────────────────────────────────────── */
  .alm-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.62rem;
    font-family: ui-monospace, monospace;
    color: #c8d8f0;
  }
  .alm-table thead th {
    padding: 0.25rem 0.55rem;
    text-align: left;
    font-size: 0.55rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.07em;
    color: rgba(200, 216, 240, 0.5);
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    white-space: nowrap;
  }
  .alm-table tbody tr {
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    transition: background 0.08s;
  }
  .alm-table tbody tr:last-child { border-bottom: 0; }
  .alm-table tbody tr:hover { background: rgba(168, 85, 247, 0.04); }
  .alm-table td {
    padding: 0.28rem 0.55rem;
    vertical-align: middle;
  }

  /* Column-specific alignment / widths */
  .alm-td-ts    { white-space: nowrap; color: rgba(200,216,240,0.5); font-size: 0.55rem; }
  .alm-td-num   { text-align: right; font-variant-numeric: tabular-nums; }
  .alm-td-sym   { font-weight: 600; color: #e5edf7; white-space: nowrap; }
  .alm-td-detail {
    max-width: 18rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: rgba(200,216,240,0.6);
    font-size: 0.58rem;
  }

  /* ── Pills ───────────────────────────────────────────────────── */
  .alm-pill {
    display: inline-block;
    padding: 0.05rem 0.38rem;
    border-radius: 0.2rem;
    font-size: 0.5rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    border: 1px solid;
    white-space: nowrap;
  }
  /* Status pills */
  .alm-o-open      { color: #7dd3fc; background: rgba(56,189,248,0.12);  border-color: rgba(56,189,248,0.4); }
  .alm-o-filled    { color: #4ade80; background: rgba(74,222,128,0.14);  border-color: rgba(74,222,128,0.55); }
  .alm-o-unfilled  { color: #f87171; background: rgba(248,113,113,0.14); border-color: rgba(248,113,113,0.55); }
  .alm-o-rejected  { color: #f87171; background: rgba(248,113,113,0.14); border-color: rgba(248,113,113,0.55); }
  .alm-o-cancelled { color: #c8d8f0; background: rgba(200,216,240,0.08); border-color: rgba(200,216,240,0.3); }
  /* Mode pills */
  .alm-m-sim    { color: #fbbf24; background: rgba(251,191,36,0.12); border-color: rgba(251,191,36,0.45); }
  .alm-m-paper  { color: #7dd3fc; background: rgba(56,189,248,0.12); border-color: rgba(56,189,248,0.4); }
  .alm-m-live   { color: #4ade80; background: rgba(74,222,128,0.12); border-color: rgba(74,222,128,0.4); }
  .alm-m-shadow { color: #fb923c; background: rgba(251,146,60,0.12); border-color: rgba(251,146,60,0.4); }

  /* Side labels */
  .alm-side { font-weight: 700; }
  .alm-side-buy  { color: #4ade80; }
  .alm-side-sell { color: #f87171; }

  /* ── Mobile ──────────────────────────────────────────────────── */
  /* Full-bleed modal, top-anchored, so the tab strip sits at the
     top of the screen where the operator expects it. Earlier the
     panel was align-self: flex-end inside an align-items: flex-end
     overlay (bottom-sheet pattern) — tabs landed in the middle-
     lower band of the screen which read as broken. */
  @media (max-width: 600px) {
    .alm-overlay {
      align-items: flex-start;
      padding: 0;
    }
    .alm-modal {
      width: 100vw;
      height: 100vh;
      max-height: 100vh;
      border-radius: 0;
      align-self: stretch;
    }
  }
</style>
