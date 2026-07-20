<script>
  import { nowStamp, lastRefreshAt, formatDualTz } from '$lib/stores';

  let _lastRefresh = $state(0);
  let _showRefresh = $state(false);

  $effect(() => { _lastRefresh = $lastRefreshAt; });

  let _refreshTs = $derived(_lastRefresh ? formatDualTz(new Date(_lastRefresh)) : null);

  function _toggle() {
    if (_refreshTs) _showRefresh = !_showRefresh;
  }
</script>

<span
  class="ats-group"
  onclick={_toggle}
  role="button"
  tabindex="0"
  onkeydown={(e) => e.key === 'Enter' && _toggle()}
  style="touch-action: manipulation; user-select: none; -webkit-tap-highlight-color: transparent;">
  <span class="ats-now" class:ats-mobile-hide={_showRefresh}>{$nowStamp}</span>
  {#if _refreshTs}
    <span class="ats-sep" aria-hidden="true">|</span>
    <span class="ats-refresh" class:ats-mobile-hide={!_showRefresh}>{_refreshTs}</span>
  {/if}
</span>

<style>
  .ats-group {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    cursor: pointer;
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
  @media (max-width: 640px) {
    .ats-group { font-size: 0.6rem; }
    .ats-sep { display: none; }
    .ats-mobile-hide { display: none; }
  }
</style>
