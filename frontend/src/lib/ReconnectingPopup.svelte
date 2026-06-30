<script>
  /**
   * ReconnectingPopup — shown for up to 3 s when the tab returns from
   * hibernation (≥ polling.idle_timeout_min hidden).
   *
   * Mounts once in (algo)/+layout.svelte. Driven by the `reconnectingState`
   * writable from stores.js. Auto-dismisses via the 3 s max-wait timer in
   * _exitHibernation(); Escape provides a safety manual-dismiss.
   *
   * Design: semi-transparent backdrop + centered pill — matches the
   * existing ChartModal / ActivityLogModal overlay pattern but lighter
   * weight (no full-screen chrome needed for a transient status indicator).
   */

  import { reconnectingState } from '$lib/stores';

  // Close on Escape — safety dismiss if stores are slow to resolve.
  function onKeydown(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') {
      reconnectingState.set({ active: false, pending: 0, total: 0 });
    }
  }
</script>

{#if $reconnectingState.active}
  <!-- svelte-ignore a11y-no-noninteractive-element-interactions -->
  <div
    class="reconnecting-backdrop"
    role="status"
    aria-live="polite"
    aria-label="Reconnecting"
    onkeydown={onKeydown}
  >
    <div class="reconnecting-popup">
      <svg class="rc-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle
          cx="12" cy="12" r="9"
          stroke="currentColor"
          stroke-width="2.5"
          stroke-linecap="round"
          stroke-dasharray="28 14"
        />
      </svg>
      <span class="rc-label">Reconnecting…</span>
    </div>
  </div>
{/if}

<style>
  .reconnecting-backdrop {
    position: fixed;
    inset: 0;
    z-index: 9100;
    /* Very light backdrop — doesn't block interaction, just signals state. */
    background: rgba(8, 15, 28, 0.45);
    display: flex;
    align-items: center;
    justify-content: center;
    /* Fade in quickly; the popup is intentionally brief. */
    animation: rc-fade-in 120ms ease-out both;
  }

  @keyframes rc-fade-in {
    from { opacity: 0; }
    to   { opacity: 1; }
  }

  .reconnecting-popup {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    padding: 0.625rem 1.125rem;
    border-radius: 9999px;
    background: rgba(13, 24, 41, 0.92);
    border: 1px solid rgba(34, 211, 238, 0.45);
    box-shadow:
      0 0 0 1px rgba(34, 211, 238, 0.10) inset,
      0 4px 24px rgba(0, 0, 0, 0.55);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    color: var(--algo-cyan-text, #67e8f9);
    font-size: 0.8125rem;
    font-weight: 500;
    letter-spacing: 0.01em;
    white-space: nowrap;
    /* Prevent hover / pointer from confusing operators: popup is purely
       informational — no click target inside. */
    pointer-events: none;
  }

  .rc-spinner {
    width: 1.125rem;
    height: 1.125rem;
    flex-shrink: 0;
    color: var(--algo-cyan, #22d3ee);
    animation: rc-spin 0.9s linear infinite;
  }

  @keyframes rc-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }

  .rc-label {
    color: var(--algo-sky-text, #bae6fd);
  }

  /* Mobile: ensure the pill doesn't overflow narrow viewports. */
  @media (max-width: 360px) {
    .reconnecting-popup {
      padding: 0.5rem 0.875rem;
      font-size: 0.75rem;
    }
    .rc-spinner {
      width: 1rem;
      height: 1rem;
    }
  }
</style>
