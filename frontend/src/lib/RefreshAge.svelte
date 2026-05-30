<!--
  RefreshAge — small "updated Xs ago" chip rendered next to the
  page-header timestamp. Subscribes to the global `lastRefreshAt`
  store (updated by every <RefreshButton> on `loading` true → false
  transition). Internal 1 s ticker keeps the relative-time text
  fresh.

  Usage:
    <span class="algo-ts">{$nowStamp}</span>
    <RefreshAge />
    <span class="ml-auto"></span>

  Renders nothing until the first refresh lands (`lastRefreshAt = 0`
  on cold boot).
-->
<script>
  import { onMount, onDestroy } from 'svelte';
  import { lastRefreshAt } from '$lib/stores';

  let _now = $state(Date.now());
  let _ts  = $state(0);
  /** @type {any} */
  let _tick = null;

  onMount(() => {
    _tick = setInterval(() => { _now = Date.now(); }, 1000);
  });
  onDestroy(() => {
    if (_tick) clearInterval(_tick);
    _tick = null;
  });

  lastRefreshAt.subscribe((v) => { _ts = v; });

  const _age = $derived.by(() => {
    if (!_ts) return '';
    const ms = Math.max(0, _now - _ts);
    if (ms < 1500) return 'just now';
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    return `${h}h ago`;
  });
</script>

{#if _age}
  <span class="rf-age" title="Time since last data refresh">· updated {_age}</span>
{/if}

<style>
  /* Same monospace family + slate-blue palette as `.algo-ts` so the
     chip reads as a sibling of the wall-clock timestamp — operator
     sees both numbers and understands "wall clock vs data freshness"
     at a glance. */
  .rf-age {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: rgba(126, 151, 184, 0.72);
    white-space: nowrap;
    letter-spacing: 0.02em;
    margin-right: 0.15rem;
  }
</style>
