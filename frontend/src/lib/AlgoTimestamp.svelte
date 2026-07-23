<script>
  import { get } from 'svelte/store';
  import { browser } from '$app/environment';
  import { lastRefreshAt, formatDualTz } from '$lib/stores';

  let _lastRefresh = $state(browser ? get(lastRefreshAt) : 0);
  let _showRefresh = $state(false);

  let _nowEpoch = $state(browser ? Date.now() : 0);

  $effect(() => {
    const lr = $lastRefreshAt;
    _lastRefresh = lr;
    if (lr > _nowEpoch) _nowEpoch = Date.now();
  });

  $effect(() => {
    const id = setInterval(() => { _nowEpoch = Date.now(); }, 30_000);
    return () => clearInterval(id);
  });

  let _nowTs     = $derived(_nowEpoch ? formatDualTz(new Date(_nowEpoch)) : '');
  let _refreshTs = $derived(_lastRefresh ? formatDualTz(new Date(_lastRefresh)) : null);

  function _toggle() { if (_refreshTs) _showRefresh = !_showRefresh; }

  $effect(() => { if (!_refreshTs && _showRefresh) _showRefresh = false; });
</script>

<button
  type="button"
  class="ats-group"
  onclick={_toggle}
  onkeydown={(e) => e.key === 'Enter' && _toggle()}
  style="touch-action: manipulation; user-select: none; -webkit-tap-highlight-color: transparent;">
  <span class="ats-slot">
    <span class="ats-now" class:ats-mobile-hide={_showRefresh}>{_nowTs}</span>
    {#if _refreshTs}
      <span class="ats-refresh" class:ats-mobile-hide={!_showRefresh}>{_refreshTs}</span>
    {/if}
  </span>
</button>

<style>
  .ats-group {
    background: none;
    border: none;
    padding: 0;
    font: inherit;
    color: inherit;
    text-align: inherit;
    cursor: default;
    pointer-events: none;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
  }
  .ats-slot {
    display: inline-flex;
  }
  .ats-now {
    color: var(--c-info);
    font-size: inherit;
  }
  .ats-refresh {
    color: var(--algo-amber, #fbbf24);
    font-size: inherit;
  }
  @media (max-width: 640px) {
    .ats-group {
      cursor: pointer;
      pointer-events: auto;
      font-size: 0.6rem;
    }
    .ats-slot { display: grid; }
    .ats-now, .ats-refresh { grid-area: 1 / 1; }
    .ats-now, .ats-refresh {
      transition: opacity 0.15s ease;
    }
    .ats-mobile-hide {
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.15s ease;
    }
  }
</style>
