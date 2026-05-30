<!--
  RefreshButton — small icon button that triggers an on-demand refresh.
  Sized + palette-matched to FullscreenButton so the two sit cleanly
  side-by-side in any card header (refresh icon FIRST, fullscreen
  SECOND by convention — refresh is invoked more often).

  Usage:
    <RefreshButton onClick={() => loadData()} {loading} label="positions" />

  Connection-status badge
  -----------------------
  Subscribes to the global `connStatus` store ($lib/stores). When the
  store reports any broker accounts (`total > 0`), a small badge in the
  button's top-right corner shows the loaded count. Color tracks health:

     loaded === total   → green  (all broker accounts connected)
     0 < loaded < total → amber  (partial)
     loaded === 0       → red    (none loaded, total > 0)
     total === 0        → no badge (no broker config / demo mode)

  Tooltip surfaces the full "N of M broker accounts loaded" message so
  the badge isn't ambiguous. Same right-corner placement + size as the
  unread badges on OrderNotifications / AgentNotifications — operator
  reads "small number in the corner = stateful indicator" consistently
  across every icon family.

  When `loading` is true the icon spins, the title flips to "Refreshing…"
  so screen readers + tooltip both reflect state, and the click handler is
  blocked (avoiding double-fires that would otherwise hit the broker
  twice while the first request was still in flight).
-->
<script>
  import { connStatus, startConnStatusPoller } from '$lib/stores';
  import { onMount } from 'svelte';

  /**
   * @typedef {object} Props
   * @property {() => void} onClick - Click handler that should trigger the refresh.
   * @property {boolean} [loading] - Spinner state; click is suppressed while true.
   * @property {string} [label] - aria-label suffix (e.g. "positions", "audit").
   */
  /** @type {Props} */
  let { onClick, loading = false, label = 'data' } = $props();

  // Ensure the global connection-status poller is running. Idempotent —
  // safe to call from every mounted RefreshButton.
  onMount(() => { startConnStatusPoller(); });

  // Badge state derived from the store.
  let _loaded = $state(0);
  let _total  = $state(0);
  connStatus.subscribe((v) => {
    _loaded = Number(v?.loaded) || 0;
    _total  = Number(v?.total)  || 0;
  });

  const _badgeClass = $derived(
    _total === 0       ? ''
    : _loaded === 0    ? 'rf-badge-red'
    : _loaded < _total ? 'rf-badge-amber'
    :                    'rf-badge-green'
  );

  const _connTitle = $derived(
    _total === 0
      ? (loading ? 'Refreshing…' : 'Refresh now')
      : `Refresh — ${_loaded} of ${_total} broker accounts loaded`
  );
</script>

<button
  type="button"
  class="rf-btn"
  class:rf-spinning={loading}
  onclick={(e) => { e.stopPropagation(); if (!loading) onClick?.(); }}
  disabled={loading}
  aria-label={loading ? `Refreshing ${label}` : `Refresh ${label}`}
  title={_connTitle}>
  {#if loading}
    <!-- Loading state — distinct arc-spinner glyph (NOT the same
         refresh-arrow rotated). Reads as "working / fetching" rather
         than "refresh affordance". The arc spins via the rf-spinning
         keyframe below. -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <circle cx="8" cy="8" r="5.5"
        fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round"
        stroke-dasharray="9 30" />
    </svg>
  {:else}
    <!-- Idle / refresh affordance — circular-arrow icon. -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" />
      <path d="M13.5 2v3.5H10"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  {/if}
  {#if _total > 0}
    <span class="rf-badge {_badgeClass}">{_loaded}</span>
  {/if}
</button>

<style>
  /* Vibrant cyan-400 palette — the canonical "live data / refresh"
     accent across Bloomberg Terminal, IBKR TWS, Sensibull and Streak.
     Shared with FullscreenButton + CollapseButton so the trio of
     card-control icons reads as one consistent family. */
  .rf-btn {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    margin: 0;
    background: rgba(34, 211, 238, 0.14);
    border: 1px solid rgba(34, 211, 238, 0.55);
    border-radius: 3px;
    color: #22d3ee;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
    overflow: visible;
  }
  .rf-btn:hover:not(:disabled) {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.85);
    color: #67e8f9;
  }
  .rf-btn:focus-visible {
    outline: 2px solid rgba(34, 211, 238, 0.65);
    outline-offset: 1px;
  }
  .rf-btn:disabled {
    cursor: progress;
    opacity: 0.85;
  }
  .rf-btn.rf-spinning svg {
    animation: rf-spin 0.9s linear infinite;
  }
  @keyframes rf-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  /* Connection-state badge — same top-right placement + sizing as the
     unread badges on OrderNotifications / AgentNotifications so all
     three "small number in the corner" indicators read as one family.
     Background color encodes health (green/amber/red). */
  .rf-badge {
    position: absolute;
    top: -3px; right: -4px;
    min-width: 0.85rem; height: 0.85rem;
    padding: 0 0.18rem;
    color: #fff;
    border-radius: 999px;
    font-size: 0.5rem;
    font-weight: 800;
    line-height: 0.85rem;
    font-family: ui-monospace, monospace;
    border: 1px solid rgba(13, 21, 38, 0.85);
    display: inline-flex; align-items: center; justify-content: center;
    /* Sit above the spinning SVG via z-index */
    z-index: 1;
    pointer-events: none;
  }
  .rf-badge-green { background: #16a34a; }
  .rf-badge-amber { background: #d97706; }
  .rf-badge-red   { background: #ef4444; }
</style>
