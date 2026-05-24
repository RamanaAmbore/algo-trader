<script>
  import { onMount, onDestroy } from 'svelte';
  import { visibleInterval } from '$lib/stores';
  import { fetchNews } from '$lib/api';

  // Algo-page news list (dashboard + LogPanel News tab). Palette is
  // fixed dark/sky — the public /market page uses its own inline
  // implementation with the cream/champagne palette + the old layout
  // (operator decided to keep that one as-is).
  /** @type {{
   *   limit?: number,
   *   showRefreshTime?: boolean,
   *   pollMs?: number,
   *   emptyMessage?: string,
   * }} */
  let {
    limit           = 5,
    showRefreshTime = true,
    pollMs          = 5 * 60 * 1000,
    emptyMessage    = '',
  } = $props();

  /** @type {Array<{title:string, link:string, source:string, timestamp:string}>} */
  let _news       = $state([]);
  let _newsRefresh = $state(/** @type {string|null} */ (null));
  let _loading    = $state(false);
  let _teardown;

  // Returns 'now' / '5m' / '1h 12m' / '3h' / '—'
  function timeSince(/** @type {string|null|undefined} */ iso) {
    if (!iso) return '—';
    const ts = new Date(iso);
    if (isNaN(ts.getTime())) return '—';
    const diffMs = Date.now() - ts.getTime();
    if (diffMs < 0) return 'now';
    const mins = Math.floor(diffMs / 60000);
    if (mins < 1) return 'now';
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    const rem = mins % 60;
    return rem > 0 ? `${hrs}h ${rem}m` : `${hrs}h`;
  }

  async function _load() {
    _loading = true;
    try {
      const r = await fetchNews();
      _news       = r?.items ?? [];
      _newsRefresh = r?.refreshed_at ?? null;
    } catch (_) { /* silent — empty state takes over */ }
    finally { _loading = false; }
  }

  onMount(() => {
    _load();
    _teardown = visibleInterval(_load, pollMs);
  });
  onDestroy(() => { _teardown?.(); });
</script>

{#if _news.length > 0}
  {#if showRefreshTime && _newsRefresh}
    <div class="newslist-refreshed">
      Refreshed {timeSince(_newsRefresh)} ago
    </div>
  {/if}
  <ul class="newslist">
    {#each _news.slice(0, limit) as item}
      {@const _stamp = item.timestamp ? new Date(item.timestamp) : null}
      {@const _dateStr = _stamp && !isNaN(_stamp.getTime())
        ? _stamp.toLocaleString('en-IN', {
            day: '2-digit', month: 'short',
            hour: '2-digit', minute: '2-digit', hour12: false,
            timeZone: 'Asia/Kolkata',
          })
        : ''}
      <li class="newslist-row">
        <span class="newslist-time" title={item.timestamp || ''}>
          {timeSince(item.timestamp)}
          {#if _dateStr}
            <span class="newslist-time-abs">{_dateStr}</span>
          {/if}
        </span>
        <a class="newslist-title"
           href={item.link}
           target="_blank"
           rel="noopener">
          {item.title}
          {#if item.source}
            <span class="newslist-src"> · {item.source}</span>
          {/if}
        </a>
      </li>
    {/each}
  </ul>
{:else if !_loading && emptyMessage}
  <div class="newslist-empty">{emptyMessage}</div>
{/if}

<style>
  .newslist {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
  }

  .newslist-row {
    display: grid;
    grid-template-columns: 6.2rem 1fr;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.28rem 0;
    border-bottom: 1px solid rgba(126, 151, 184, 0.12);
    font-family: ui-monospace, monospace;
  }
  .newslist-row:last-child { border-bottom: none; }

  .newslist-time {
    font-size: 0.58rem;
    color: #7dd3fc;
    font-variant-numeric: tabular-nums;
    text-align: right;
    white-space: nowrap;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    line-height: 1.15;
  }

  .newslist-time-abs {
    font-size: 0.52rem;
    color: rgba(126, 151, 184, 0.65);
    font-weight: 400;
  }

  .newslist-title {
    font-size: 0.68rem;
    color: #c8d8f0;
    text-decoration: none;
    font-weight: 500;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition: color 0.1s;
  }
  .newslist-title:hover { color: #fbbf24; }

  .newslist-src {
    font-size: 0.55rem;
    color: rgba(126, 151, 184, 0.70);
    font-weight: 400;
  }

  .newslist-refreshed {
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    color: #7e97b8;
    letter-spacing: 0.03em;
    margin-bottom: 0.3rem;
  }

  .newslist-empty {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    color: #7e97b8;
  }

  @media (max-width: 600px) {
    .newslist-row { grid-template-columns: 4.4rem 1fr; }
    .newslist-time-abs { font-size: 0.48rem; }
  }
</style>
