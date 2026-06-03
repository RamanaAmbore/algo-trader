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
  import { connStatus, startConnStatusPoller, lastRefreshAt, formatDualTz } from '$lib/stores';
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

  // Watch the `loading` prop for true → false transitions and stamp
  // `lastRefreshAt`. Catches BOTH manual clicks (operator hits the
  // button) AND auto-refresh — as long as the page's load() sets
  // loading=true at the start and loading=false in finally, the
  // tooltip timestamp updates without any per-page wiring.
  let _prevLoading = false;
  $effect(() => {
    if (_prevLoading && !loading) {
      lastRefreshAt.set(Date.now());
    }
    _prevLoading = loading;
  });

  // Subscribe for tooltip rendering.
  let _lastTs = $state(0);
  lastRefreshAt.subscribe((v) => { _lastTs = v || 0; });

  // Badge state derived from the store.
  let _loaded = $state(0);
  let _total  = $state(0);
  let _backendOk = $state(true);
  let _failingAccounts = $state(/** @type {string[]} */ ([]));
  connStatus.subscribe((v) => {
    _loaded = Number(v?.loaded) || 0;
    _total  = Number(v?.total)  || 0;
    _backendOk = v?.backendOk !== false; // default true
    _failingAccounts = Array.isArray(v?.failingAccounts) ? v.failingAccounts : [];
  });

  // Three-state visual encoding:
  //   backendOk=false              → grey + `?` (API unreachable)
  //   backendOk=true, loaded<total → red/amber + count (broker issue)
  //   backendOk=true, loaded===tot → green + count    (all good)
  //   total===0                    → no badge (demo / no config)
  const _showBadge = $derived(_total > 0 || !_backendOk);
  const _badgeText = $derived(_backendOk ? String(_loaded) : '?');
  const _badgeClass = $derived(
    !_backendOk        ? 'rf-badge-grey'
    : _total === 0     ? ''
    : _loaded === 0    ? 'rf-badge-red'
    : _loaded < _total ? 'rf-badge-amber'
    :                    'rf-badge-green'
  );

  // Multi-line native tooltip (newlines render as soft breaks in
  // every browser's title="…" popover). Encodes the FULL connection
  // story so the operator can diagnose without leaving the page:
  //
  //   Line 1 — action / connection state (`Refresh — N of M broker
  //            accounts loaded`, or `API unreachable — retrying…`
  //            when the backend is down, or `Refreshing…` mid-fetch).
  //   Line 2 — failing broker accounts list, only when some are down.
  //   Line 3 — Last refreshed: <dual-tz timestamp>, matches the
  //            page-header wall clock format.
  const _connTitle = $derived.by(() => {
    /** @type {string[]} */
    const lines = [];
    if (loading) {
      lines.push('Refreshing…');
    } else if (!_backendOk) {
      lines.push('API unreachable — retrying every 15s');
    } else if (_total === 0) {
      lines.push('Refresh now');
    } else {
      lines.push(`Refresh — ${_loaded} of ${_total} broker accounts loaded`);
    }
    // Failing-broker detail — surface the account codes so the
    // operator knows WHICH broker to investigate. Skip when backend
    // is down because the list may be stale anyway.
    if (_backendOk && _failingAccounts.length > 0) {
      lines.push(`Failed: ${_failingAccounts.join(', ')}`);
    }
    if (_lastTs) {
      lines.push(`Last refreshed: ${formatDualTz(new Date(_lastTs))}`);
    }
    return lines.join('\n');
  });
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
  {#if _showBadge}
    <span class="rf-badge {_badgeClass}">{_badgeText}</span>
  {/if}
</button>

<style>
  /* Sky-blue (sky-400 #38bdf8) palette — distinct from the chart-icon
     cyan-400 (#22d3ee) the operator clicks next to Refresh in every
     page header. Same "blue family" semantically (live-data accent)
     but visually separable so the operator never confuses the two.
     Card-control trio (Collapse / Fullscreen / DefaultSize) keeps cyan
     since they're scoped to card headers, not the page-header strip. */
  .rf-btn {
    position: relative;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.6rem;
    height: 1.6rem;
    padding: 0;
    margin: 0;
    background: rgba(56, 189, 248, 0.14);
    border: 1px solid rgba(56, 189, 248, 0.55);
    border-radius: 3px;
    color: #38bdf8;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
    overflow: visible;
  }
  .rf-btn:hover:not(:disabled) {
    background: rgba(56, 189, 248, 0.26);
    border-color: rgba(56, 189, 248, 0.85);
    color: #7dd3fc;
  }
  .rf-btn:focus-visible {
    outline: 2px solid rgba(56, 189, 248, 0.65);
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
  /* Backend-offline state — desaturated slate grey, distinct from the
     red broker-issue state. Operator reads "no number, just a `?`" as
     "API unreachable" rather than "all brokers failed". */
  .rf-badge-grey  {
    background: #475569;
    color: rgba(255, 255, 255, 0.92);
  }
</style>
