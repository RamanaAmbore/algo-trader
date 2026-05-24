<script>
  import { onMount, onDestroy } from 'svelte';
  import { visibleInterval } from '$lib/stores';
  import { fetchNews } from '$lib/api';

  /** @type {{
   *   theme?: 'algo' | 'public',
   *   limit?: number,
   *   showRefreshTime?: boolean,
   *   pollMs?: number,
   *   emptyMessage?: string,
   * }} */
  let {
    theme           = 'algo',
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
    <div class="newslist-refreshed newslist-refreshed-{theme}">
      Refreshed {timeSince(_newsRefresh)} ago
    </div>
  {/if}
  <ul class="newslist newslist-{theme}">
    {#each _news.slice(0, limit) as item}
      {@const _stamp = item.timestamp ? new Date(item.timestamp) : null}
      {@const _dateStr = _stamp && !isNaN(_stamp.getTime())
        ? _stamp.toLocaleString('en-IN', {
            day: '2-digit', month: 'short',
            hour: '2-digit', minute: '2-digit', hour12: false,
            timeZone: 'Asia/Kolkata',
          })
        : ''}
      <li class="newslist-row newslist-row-{theme}">
        <span class="newslist-time newslist-time-{theme}" title={item.timestamp || ''}>
          {timeSince(item.timestamp)}
          {#if _dateStr}
            <span class="newslist-time-abs newslist-time-abs-{theme}">{_dateStr}</span>
          {/if}
        </span>
        <a class="newslist-title newslist-title-{theme}"
           href={item.link}
           target="_blank"
           rel="noopener">
          {item.title}
          {#if item.source}
            <span class="newslist-src newslist-src-{theme}"> · {item.source}</span>
          {/if}
        </a>
      </li>
    {/each}
  </ul>
{:else if !_loading && emptyMessage}
  <div class="newslist-empty newslist-empty-{theme}">{emptyMessage}</div>
{/if}

<style>
  /* ── Shared structure ─────────────────────────────────────────────── */
  .newslist {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
  }

  .newslist-row {
    display: grid;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.28rem 0;
    border-bottom: 1px solid;
    font-family: ui-monospace, monospace;
  }
  .newslist-row:last-child { border-bottom: none; }

  .newslist-time {
    font-size: 0.58rem;
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
    font-weight: 400;
  }

  .newslist-title {
    font-size: 0.68rem;
    text-decoration: none;
    font-weight: 500;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition: color 0.1s;
  }

  .newslist-src {
    font-size: 0.55rem;
    font-weight: 400;
  }

  .newslist-refreshed {
    font-family: ui-monospace, monospace;
    font-size: 0.55rem;
    letter-spacing: 0.03em;
    margin-bottom: 0.3rem;
  }

  .newslist-empty {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
  }

  /* ── Algo theme (dark navy) ───────────────────────────────────────── */
  .newslist-row-algo {
    grid-template-columns: 6.2rem 1fr;
    border-color: rgba(126, 151, 184, 0.12);
  }

  .newslist-time-algo      { color: #7dd3fc; }
  .newslist-time-abs-algo  { color: rgba(126, 151, 184, 0.65); }

  .newslist-title-algo       { color: #c8d8f0; }
  .newslist-title-algo:hover { color: #fbbf24; }

  .newslist-src-algo { color: rgba(126, 151, 184, 0.70); }

  .newslist-refreshed-algo { color: #7e97b8; }
  .newslist-empty-algo     { color: #7e97b8; }

  /* ── Public theme (cream + champagne) ────────────────────────────── */
  .newslist-row-public {
    grid-template-columns: 6.2rem 1fr;
    border-color: #e7e0cf;
  }

  .newslist-time-public      { color: #0c1830; }
  .newslist-time-abs-public  { color: rgba(60, 80, 110, 0.70); }

  .newslist-title-public       { color: #1a1e35; }
  .newslist-title-public:hover { color: #c8a84b; }

  .newslist-src-public { color: rgba(80, 100, 130, 0.70); }

  .newslist-refreshed-public { color: #6b7894; }
  .newslist-empty-public     { color: #6b7894; }

  /* ── Mobile ≤600px ────────────────────────────────────────────────── */
  @media (max-width: 600px) {
    .newslist-row-algo,
    .newslist-row-public {
      grid-template-columns: 4.4rem 1fr;
    }
    .newslist-time-abs-algo,
    .newslist-time-abs-public {
      font-size: 0.48rem;
    }
  }
</style>
