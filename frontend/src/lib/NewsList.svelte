<script>
  import { onMount, onDestroy } from 'svelte';
  import { visibleInterval } from '$lib/stores';
  import { fetchNews } from '$lib/api';

  // Algo-page news list (dashboard + LogPanel News tab). Layout mirrors
  // the public /market page (3-column grid: HH:MM | title | source pill)
  // so the operator sees the same row shape across surfaces. Palette is
  // the algo dark theme — sky/navy/amber instead of cream/champagne.
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

  // Pull HH:MM out of the presentational timestamp the API returns.
  // ISO → slice(11,16); otherwise grab the first HH:MM run; fallback to raw.
  function newsTime(/** @type {string|null|undefined} */ ts) {
    if (!ts) return '';
    if (ts.length >= 19 && ts[10] === 'T') return ts.slice(11, 16);
    const m = ts.match(/\d\d:\d\d/);
    return m ? m[0] : ts;
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
      Refreshed at {_newsRefresh}
    </div>
  {/if}
  <ul class="newslist">
    {#each _news.slice(0, limit) as item}
      <li class="newslist-row">
        <span class="newslist-time" title={item.timestamp || ''}>
          {newsTime(item.timestamp)}
        </span>
        <a class="newslist-title"
           href={item.link}
           target="_blank"
           rel="noopener">
          {item.title}
        </a>
        {#if item.source}
          <span class="newslist-src" title={item.source}>{item.source}</span>
        {/if}
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
  }

  .newslist-row {
    display: grid;
    grid-template-columns: max-content 1fr max-content;
    align-items: baseline;
    gap: 0.6rem;
    padding: 0.4rem 0;
    border-bottom: 1px solid rgba(126, 151, 184, 0.12);
    font-size: 0.72rem;
    color: #c8d8f0;
    line-height: 1.45;
  }
  .newslist-row:last-child { border-bottom: none; }

  .newslist-time {
    font-family: ui-monospace, monospace;
    font-size: 0.62rem;
    color: #7dd3fc;
    font-variant-numeric: tabular-nums;
    min-width: 3rem;
  }

  .newslist-title {
    color: #c8d8f0;
    text-decoration: none;
    font-weight: 500;
    min-width: 0;
    transition: color 0.1s;
  }
  .newslist-title:hover {
    color: #fbbf24;
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 2px;
  }

  .newslist-src {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.25);
    padding: 1px 6px;
    border-radius: 2px;
    white-space: nowrap;
    max-width: 14ch;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .newslist-refreshed {
    font-family: ui-monospace, monospace;
    font-size: 0.58rem;
    color: #7e97b8;
    letter-spacing: 0.03em;
    margin-bottom: 0.35rem;
  }

  .newslist-empty {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    color: #7e97b8;
  }

  @media (max-width: 600px) {
    .newslist-row { grid-template-columns: max-content 1fr; gap: 0.5rem; }
    .newslist-src { display: none; }
  }
</style>
