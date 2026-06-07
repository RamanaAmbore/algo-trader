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
   *   refreshKey?: number,
   *   columns?: number,
   * }} */
  let {
    limit           = 5,
    showRefreshTime = true,
    /** Default 2 min so operator-visible News feeds (Activity modal,
     *  LogPanel News tab) refresh every couple of minutes instead of
     *  staring at the same headlines for 10 min. Backend cache (60 s)
     *  caps actual RSS fan-out so polling cheaper-than-cache is safe. */
    pollMs          = 2 * 60 * 1000,
    emptyMessage    = '',
    /** When the caller bumps this number, the list re-fetches. Lets the
     *  page-header Refresh button drive a manual reload without waiting
     *  for the poll interval. */
    refreshKey      = 0,
    // Magazine-style multi-column flow on wide viewports. Default 1.
    // Set to 2 (or more) and the list will reflow into N columns
    // when the viewport allows; collapses back to 1 below 900 px.
    // Rows are kept intact via break-inside: avoid.
    columns         = 1,
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

  // refreshKey-driven re-fetch — skip the very first run so we don't
  // double-fetch on mount (onMount already calls _load once).
  let _firstRefreshKey = true;
  $effect(() => {
    void refreshKey;
    if (_firstRefreshKey) { _firstRefreshKey = false; return; }
    _load();
  });
</script>

{#if _news.length > 0}
  {#if showRefreshTime && _newsRefresh}
    <div class="newslist-refreshed">
      Refreshed at {_newsRefresh}
    </div>
  {/if}
  <ul class="newslist" style:--newslist-cols={columns}>
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
    /* Magazine-style column flow when caller sets columns > 1.
       Rows are kept atomic via break-inside: avoid below. Defaults
       to 1 (single column) so existing callers are unaffected. */
    column-count: var(--newslist-cols, 1);
    column-gap: 1.4rem;
    column-rule: 1px solid rgba(126, 151, 184, 0.10);
  }
  /* Narrow viewports collapse back to single column regardless of
     the operator-requested column-count — magazine flow only makes
     sense when there's enough horizontal room for 2+ readable
     columns of headlines (~28+ ch each). */
  @media (max-width: 900px) {
    .newslist {
      column-count: 1;
      column-rule: none;
    }
  }

  .newslist-row {
    display: grid;
    grid-template-columns: max-content 1fr max-content;
    align-items: baseline;
    gap: 0.6rem;
    padding: 0.4rem 0;
    border-bottom: 1px solid rgba(126, 151, 184, 0.12);
    font-size: 0.72rem;
    color: var(--algo-slate);
    line-height: 1.45;
    /* Keep each headline row intact inside a column — without this
       the column algorithm can split a row's time chip into one
       column and its title into the next. */
    break-inside: avoid;
    -webkit-column-break-inside: avoid;
    page-break-inside: avoid;
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
    color: var(--algo-slate);
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
    color: var(--algo-muted);
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
    color: var(--algo-muted);
    letter-spacing: 0.03em;
    margin-bottom: 0.35rem;
    /* Operator: "the line with the text refreshed at has new line
       after edt which is wasting available space. remove it".
       Keep the "Refreshed at … IST · … EDT" on a single line; let
       horizontal overflow ellipsis it on viewports too narrow to
       fit the full dual-tz string. */
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .newslist-empty {
    font-family: ui-monospace, monospace;
    font-size: 0.65rem;
    color: var(--algo-muted);
  }

  @media (max-width: 600px) {
    .newslist-row { grid-template-columns: max-content 1fr; gap: 0.5rem; }
    .newslist-src { display: none; }
  }
</style>
