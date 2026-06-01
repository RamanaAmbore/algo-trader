<script>
  // ChartModal — thin overlay wrapper around ChartWorkspace.
  // Opens as a fixed-inset modal with an amber-glow navy panel.
  // Esc key and overlay click both close.

  import { onMount, onDestroy } from 'svelte';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import { portal } from '$lib/portal';

  let {
    /** @type {string} */ symbol = '',
    /** @type {string} */ exchange = '',
    /** @type {'live'|'sim'|'paper'} */ mode = 'live',
    /** @type {() => void} */ onClose,
  } = $props();

  function _onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') onClose();
  }

  onMount(() => {
    window.addEventListener('keydown', _onKey);
    document.body.style.overflow = 'hidden';
  });
  onDestroy(() => {
    window.removeEventListener('keydown', _onKey);
    document.body.style.overflow = '';
  });
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_interactive_supports_focus -->
<div class="cm-overlay" use:portal role="dialog" aria-modal="true" aria-label="Chart — {symbol}"
     tabindex="-1"
     onclick={onClose}>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="cm-modal" onclick={(e) => e.stopPropagation()}>
    <div class="cm-header">
      <span class="cm-title">Chart — <span class="cm-sym">{symbol}</span></span>
      <button type="button" class="cm-close" onclick={onClose} aria-label="Close chart modal">×</button>
    </div>
    <div class="cm-body">
      <ChartWorkspace
        symbol={symbol}
        exchange={exchange}
        mode={mode}
        compact={false}
        showHeader={false}
      />
    </div>
  </div>
</div>

<style>
  .cm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.72);
    z-index: 200;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem;
    box-sizing: border-box;
  }

  .cm-modal {
    background: linear-gradient(180deg, #1d2a44 0%, #152033 100%);
    border: 1px solid rgba(251, 191, 36, 0.40);
    border-radius: 6px;
    box-shadow: 0 8px 40px rgba(0, 0, 0, 0.55), 0 0 0 1px rgba(251,191,36,0.08);
    width: min(96vw, 1200px);
    /* height (not max-height) so the modal always gives the embedded
       ChartWorkspace a real viewport-sized container to fill. With
       max-height the modal collapsed to picker-bar height (~3rem)
       whenever symbol was empty and the chart canvas had no rows. */
    height: 92vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .cm-header {
    display: flex;
    align-items: center;
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .cm-title {
    font-family: monospace;
    font-size: 0.65rem;
    color: #7e97b8;
    font-weight: 600;
  }

  .cm-sym {
    color: #7dd3fc;
    font-weight: 700;
  }

  .cm-close {
    margin-left: auto;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    border-radius: 3px;
    color: #f87171;
    font-size: 0.9rem;
    line-height: 1;
    padding: 1px 7px;
    cursor: pointer;
    font-family: monospace;
    transition: background 0.1s;
  }
  .cm-close:hover {
    background: rgba(248, 113, 113, 0.15);
  }

  .cm-body {
    overflow-y: auto;
    flex: 1 1 0;
    min-height: 0;
  }
</style>
