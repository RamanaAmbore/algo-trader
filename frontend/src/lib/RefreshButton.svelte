<!--
  RefreshButton — small icon button that triggers an on-demand refresh.
  Sized + palette-matched to FullscreenButton so the two sit cleanly
  side-by-side in any card header (refresh icon FIRST, fullscreen
  SECOND by convention — refresh is invoked more often).

  Usage:
    <RefreshButton onClick={() => loadData()} {loading} label="positions" />

  When `loading` is true the icon spins, the title flips to "Refreshing…"
  so screen readers + tooltip both reflect state, and the click handler is
  blocked (avoiding double-fires that would otherwise hit the broker
  twice while the first request was still in flight).
-->
<script>
  /**
   * @typedef {object} Props
   * @property {() => void} onClick - Click handler that should trigger the refresh.
   * @property {boolean} [loading] - Spinner state; click is suppressed while true.
   * @property {string} [label] - aria-label suffix (e.g. "positions", "audit").
   */
  /** @type {Props} */
  let { onClick, loading = false, label = 'data' } = $props();
</script>

<button
  type="button"
  class="rf-btn"
  class:rf-spinning={loading}
  onclick={(e) => { e.stopPropagation(); if (!loading) onClick?.(); }}
  disabled={loading}
  aria-label={loading ? `Refreshing ${label}` : `Refresh ${label}`}
  title={loading ? 'Refreshing…' : 'Refresh now'}>
  <!-- Circular-arrow refresh icon. -->
  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"
      fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linecap="round" />
    <path d="M13.5 2v3.5H10"
      fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linecap="round" stroke-linejoin="round" />
  </svg>
</button>

<style>
  .rf-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    margin: 0;
    background: rgba(125, 211, 252, 0.08);
    border: 1px solid rgba(125, 211, 252, 0.32);
    border-radius: 3px;
    color: #7dd3fc;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
  }
  .rf-btn:hover:not(:disabled) {
    background: rgba(125, 211, 252, 0.20);
    border-color: rgba(125, 211, 252, 0.65);
    color: #bae6fd;
  }
  .rf-btn:focus-visible {
    outline: 2px solid rgba(125, 211, 252, 0.55);
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
</style>
