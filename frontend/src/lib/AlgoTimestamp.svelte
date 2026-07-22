<script>
  import { onMount, onDestroy } from 'svelte';
  import { nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';

  let _lastRefresh = $state(0);
  let _showRefresh = $state(false);

  $effect(() => { _lastRefresh = $lastRefreshAt; });

  let _refreshTs = $derived(_lastRefresh ? formatDualTz(new Date(_lastRefresh)) : null);

  function _toggle() {
    _showRefresh = !_showRefresh;
  }

  $effect(() => { if (!_refreshTs && _showRefresh) _showRefresh = false; });

  // Listen for the global 'toggle-ts' custom event so the page-header
  // click zone (delegated from the layout wrapper) can trigger the toggle
  // without needing a direct component ref. The button's own onclick
  // still works — this just adds a second trigger path.
  function _onToggleTs() { _toggle(); }
  onMount(() => { window.addEventListener('toggle-ts', _onToggleTs); });
  onDestroy(() => { window.removeEventListener('toggle-ts', _onToggleTs); });
</script>

<button
  type="button"
  class="ats-group"
  onclick={_toggle}
  onkeydown={(e) => e.key === 'Enter' && _toggle()}
  style="touch-action: manipulation; user-select: none; -webkit-tap-highlight-color: transparent;">
  <span class="ats-now" class:ats-mobile-hide={_showRefresh}>{$nowStamp}</span>
  {#if _refreshTs}
    <span class="ats-sep" aria-hidden="true">|</span>
    <span class="ats-refresh" class:ats-mobile-hide={!_showRefresh}>{_refreshTs}</span>
  {/if}
</button>

<style>
  .ats-group {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    color: inherit;
    text-align: inherit;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .ats-now {
    color: var(--c-info);
    font-size: inherit;
  }
  .ats-sep {
    color: var(--text-muted);
    font-size: inherit;
    opacity: 0.5;
  }
  .ats-refresh {
    color: var(--algo-amber, #fbbf24);
    font-size: inherit;
  }
  /* Hide the non-active time span at ALL viewports — the toggle works
     globally, not just on mobile. Previously this was inside the
     @media (max-width: 640px) block which meant desktop always showed
     both spans (toggle was a no-op on desktop). */
  .ats-mobile-hide { display: none; }

  @media (max-width: 640px) {
    .ats-group {
      font-size: 0.6rem;
      min-height: 2.5rem;
      align-items: center;
    }
    .ats-sep { display: none; }
  }
</style>
