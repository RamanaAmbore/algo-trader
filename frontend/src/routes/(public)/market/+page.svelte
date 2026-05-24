<script>
  import { onMount, onDestroy } from 'svelte';
  import { fetchMarket, fetchNews } from '$lib/api';
  import { createPerformanceSocket } from '$lib/ws';
  import { dataCache } from '$lib/stores';

  let content     = $state('');
  let lastRefresh = $state('');
  let loading     = $state(false);
  let error       = $state('');
  let unsub;

  // Tabbed surface for the two cards on this page — Market Summary
  // (AI-generated) and Market News (Indian-news feed). Only one panel
  // visible at a time so the page stays compact; both feeds load on
  // mount so flipping is a paint, not a fetch. Same UX shape as
  // /performance.
  /** @type {'summary' | 'news'} */
  let tab = $state('summary');

  // Market news — same /api/news feed the algo LogPanel consumes, but
  // styled in the public-site palette and labelled "Market News" (no
  // "log" / "feed" jargon). Refreshes every 10 minutes alongside the
  // market summary.
  /** @type {Array<{title:string, link:string, source:string, timestamp:string}>} */
  let news         = $state([]);
  let newsRefresh  = $state('');
  let newsLoading  = $state(false);
  let newsError    = $state('');
  let newsTimer;

  async function loadNews() {
    newsLoading = true;
    try {
      const r = await fetchNews();
      news        = r?.items || [];
      newsRefresh = r?.refreshed_at || '';
      newsError   = '';
    } catch (e) {
      newsError = e?.message || 'Failed to load news';
    } finally {
      newsLoading = false;
    }
  }

  function newsTime(/** @type {string} */ ts) {
    if (!ts) return '';
    // The /api/news payload includes a presentational timestamp — pull
    // out HH:MM if it's an ISO string, else show whatever the API gave us.
    if (ts.length >= 19 && ts[10] === 'T') return ts.slice(11, 16);
    const m = ts.match(/\d\d:\d\d/);
    return m ? m[0] : ts;
  }

  function renderMarkdown(/** @type {string} */ md) {
    const lines = md.split('\n');
    let html = '';
    let inList = false;
    for (const raw of lines) {
      const line = raw
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.+?)\*/g, '<em>$1</em>');
      if (/^###\s/.test(raw)) {
        if (inList) { html += '</ul>'; inList = false; }
        html += `<h3 class="md-h3">${line.replace(/^###\s/, '')}</h3>`;
      } else if (/^####\s/.test(raw)) {
        if (inList) { html += '</ul>'; inList = false; }
        html += `<h4 class="md-h4">${line.replace(/^####\s/, '')}</h4>`;
      } else if (/^\s*[-*•]\s+/.test(raw)) {
        if (!inList) { html += '<ul class="md-ul">'; inList = true; }
        html += `<li class="md-li">${line.replace(/^\s*[-*•]\s+/, '')}</li>`;
      } else if (/^---/.test(raw)) {
        if (inList) { html += '</ul>'; inList = false; }
        html += '<hr class="md-hr">';
      } else if (line.trim() === '') {
        if (inList) { html += '</ul>'; inList = false; }
        html += '<div class="md-gap"></div>';
      } else {
        if (inList) { html += '</ul>'; inList = false; }
        html += `<p class="md-p">${line}</p>`;
      }
    }
    if (inList) html += '</ul>';
    return html;
  }

  async function load() {
    loading = true; error = '';
    try {
      const data  = await fetchMarket();
      content     = data.content ?? '';
      lastRefresh = data.refreshed_at ?? '';
      dataCache.market = data;
    } catch (e) {
      error = e.message || 'Failed to load market update';
    } finally { loading = false; }
  }

  onMount(async () => {
    // Show cached data immediately — no empty flash on back-navigation
    if (dataCache.market) {
      content     = dataCache.market.content ?? '';
      lastRefresh = dataCache.market.refreshed_at ?? '';
    }
    await load();
    unsub = createPerformanceSocket(() => load());
    // News refreshes independently — server caps the upstream feed at
    // ~10 min so polling faster wastes calls. First load is immediate;
    // subsequent reloads on a 10-min cadence keep the section live.
    loadNews();
    newsTimer = setInterval(loadNews, 10 * 60 * 1000);
  });

  onDestroy(() => {
    unsub?.();
    if (newsTimer) clearInterval(newsTimer);
  });
</script>
<svelte:head>
  <title>Daily Market Report | RamboQuant Analytics</title>
  <meta name="description" content="AI-generated daily reports on Indian equity markets, options activity, and macroeconomic events." />

  <!-- Open Graph -->
  <meta property="og:title" content="Daily Market Report | RamboQuant Analytics" />
  <meta property="og:description" content="AI-generated daily reports on Indian equity markets, options activity, and macroeconomic events." />
  <meta property="og:url" content="https://ramboq.com/market" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta property="og:image:width" content="600" />
  <meta property="og:image:height" content="600" />
  <meta property="og:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="Daily Market Report | RamboQuant Analytics" />
  <meta name="twitter:description" content="AI-generated daily reports on Indian equity markets, options activity, and macroeconomic events." />
  <meta name="twitter:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta name="twitter:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
</svelte:head>


<h1 class="sr-only">Daily Market Report</h1>

<!-- Tabs sit OUTSIDE the white card on the page background, mirroring
     the /performance page treatment. Champagne BOTTOM border on the
     active tab; the panel below carries only the content. -->
<div class="market-tabs-row">
  <div class="market-tabs">
    <button type="button"
            class="market-tab"
            class:market-tab-active={tab === 'summary'}
            onclick={() => tab = 'summary'}>
      Daily Market Report
    </button>
    <button type="button"
            class="market-tab"
            class:market-tab-active={tab === 'news'}
            onclick={() => tab = 'news'}>
      News feed
    </button>
  </div>
  <div class="market-tabs-meta">
    {#if tab === 'summary'}
      {#if loading && !content}Loading…
      {:else if loading}Refreshing…{/if}
    {:else if newsLoading && !news.length}
      Loading…
    {:else if newsLoading}
      Refreshing…
    {/if}
  </div>
</div>

<div class="pub-card rounded-lg shadow-sm p-5 pt-4">
  <!-- Refreshed-at line — matches the PerformancePage timestamp style
       (text-[0.65rem] text-muted perf-ts) so the operator sees a
       consistent stamp shape across the whole public site. nowrap so
       the dual-timezone string stays on a single line; the page card
       has horizontal padding to absorb the overflow. -->
  {#if tab === 'summary' && lastRefresh}
    <div class="text-[0.65rem] text-muted perf-ts market-refresh-line">Refreshed at {lastRefresh}</div>
  {:else if tab === 'news' && newsRefresh}
    <div class="text-[0.65rem] text-muted perf-ts market-refresh-line">Refreshed at {newsRefresh}</div>
  {/if}

  {#if tab === 'summary'}
    {#if error}
      <div class="pub-banner-error p-3 rounded text-sm mb-4">{error}</div>
    {/if}
    {#if !content && loading}
      <div class="text-center text-text/40 text-sm animate-pulse py-8">
        Loading market report…
      </div>
    {:else if content && content.startsWith('Market report is temporarily')}
      <!-- API returned the standby fallback string — the AI fetch
           failed or is rate-limited. Render a friendly empty-state with
           a manual retry button instead of dropping the bare sentence
           into the markdown surface. -->
      <div class="market-empty">
        <div class="market-empty-icon">⏳</div>
        <p class="market-empty-title">Today's market report isn't ready yet.</p>
        <p class="market-empty-sub">The next scheduled refresh runs at <b>08:30 IST</b>. You can also retry now — the AI summary takes ~15 seconds to generate.</p>
        <button type="button" class="market-empty-retry" onclick={load} disabled={loading}>
          {loading ? 'Retrying…' : 'Retry now'}
        </button>
      </div>
    {:else if content}
      <div class="market-report w-full">
        {@html renderMarkdown(content)}
      </div>
    {:else if !loading}
      <p class="text-text/40 text-sm">No market update available.</p>
    {/if}
  {:else}
    {#if newsError}
      <div class="pub-banner-error p-2 rounded text-xs mb-2">
        {newsError}
      </div>
    {/if}
    {#if news.length}
      <ul class="news-list">
        {#each news as n}
          <li class="news-row">
            <span class="news-time">{newsTime(n.timestamp)}</span>
            <a class="news-title" href={n.link} target="_blank" rel="noopener">
              {n.title}
            </a>
            {#if n.source}
              <span class="news-src" title={n.source}>{n.source}</span>
            {/if}
          </li>
        {/each}
      </ul>
    {:else if !newsLoading}
      <p class="text-text/40 text-sm">No headlines available right now.</p>
    {/if}
  {/if}
</div>

<style>
  :global(.market-report .md-h3) {
    font-size: 1.05rem;
    font-weight: 700;
    color: #1a2744;
    margin: 1.25rem 0 0.5rem;
    border-left: 3px solid #d4920c;
    border-bottom: none;
    padding-left: 0.5rem;
  }
  :global(.market-report .md-h4) {
    font-size: 0.9rem;
    font-weight: 600;
    color: #1a2744;
    margin: 1rem 0 0.35rem;
  }
  :global(.market-report .md-p)  { font-size: 0.875rem; color: #1e3050; line-height: 1.65; margin: 0.25rem 0; }
  :global(.market-report .md-ul) { margin: 0.25rem 0 0.5rem 1.25rem; list-style: disc; }
  :global(.market-report .md-li) { font-size: 0.875rem; color: #1e3050; line-height: 1.6; margin: 0.15rem 0; }
  :global(.market-report .md-hr) { border: none; border-top: 1px solid #ddd8ce; margin: 0.75rem 0; }
  :global(.market-report .md-gap){ height: 0.4rem; }
  :global(.market-report strong) { font-weight: 700; color: #1a2744; }
  :global(.market-report em)     { font-style: italic; color: #1e3050; }

  /* Tab row — sits OUTSIDE the white panel. Active tab carries a
     champagne BOTTOM border; the row itself has no border so the
     gold indicator stands alone. */
  .market-tabs-row {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 0.6rem;
    margin-top: 0.5rem;
    padding: 0 0.25rem;
    flex-wrap: wrap;
  }
  .market-tabs {
    display: flex;
    gap: 0.25rem;
  }
  .market-tab {
    font-size: 0.85rem;
    font-weight: 500;
    color: #6b7894;
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    padding: 0.45rem 0.9rem;
    cursor: pointer;
    transition: color 0.12s, border-color 0.12s, background-color 0.12s;
  }
  .market-tab:hover {
    color: #1a2744;
    border-bottom-color: rgba(212,146,12,0.5);
  }
  .market-tab-active {
    color: #1a2744;
    font-weight: 700;
    border-bottom-color: #d4920c;
  }
  .market-tabs-row + .pub-card {
    margin-top: 0.6rem;
  }
  .market-tabs-meta {
    font-size: 0.7rem;
    color: #6b7894;
    font-family: ui-monospace, monospace;
  }
  /* Refreshed-at line — base size + colour come from the Tailwind
     `text-[0.65rem] text-muted perf-ts` triplet so this line sits
     visually at the same level as every other timestamp on the
     public site. Local rules add nowrap so the long dual-timezone
     string never wraps mid-row. */
  .market-refresh-line {
    margin-bottom: 0.5rem;
    margin-top: -0.15rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  /* Market News — same palette as the rest of the public market page
     (cream + navy + champagne accent). */
  .news-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .news-row {
    display: grid;
    grid-template-columns: max-content 1fr max-content;
    align-items: baseline;
    gap: 0.6rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid #e7e0cf;
    font-size: 0.85rem;
    color: #1e3050;
    line-height: 1.5;
  }
  .news-row:last-child { border-bottom: 0; }
  .news-time {
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    color: #6b7894;
    min-width: 3rem;
  }
  .news-title {
    color: #1a2744;
    text-decoration: none;
    font-weight: 500;
  }
  .news-title:hover {
    color: #b27908;
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 2px;
  }
  .news-src {
    font-size: 0.7rem;
    font-family: ui-monospace, monospace;
    color: #6b7894;
    background: #f4ead4;
    border: 1px solid #ead7a6;
    padding: 1px 6px;
    border-radius: 2px;
    white-space: nowrap;
    max-width: 14ch;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  @media (max-width: 600px) {
    .news-row { grid-template-columns: max-content 1fr; }
    .news-src { display: none; }
  }

  /* ── Empty / waiting state — shown when the API returns the standby
       fallback string (Gemini failed / rate-limited / cap-disabled). */
  .market-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    padding: 2.5rem 1rem;
    color: #1a1e35;
  }
  .market-empty-icon {
    font-size: 2rem;
    margin-bottom: 0.6rem;
    opacity: 0.55;
  }
  .market-empty-title {
    font-size: 1rem;
    font-weight: 700;
    color: #0c1830;
    margin-bottom: 0.35rem;
  }
  .market-empty-sub {
    font-size: 0.78rem;
    max-width: 36rem;
    line-height: 1.5;
    color: #4a5168;
    margin-bottom: 1rem;
  }
  .market-empty-retry {
    background: #0c1830;
    color: #f0d070;
    border: 1px solid #c8a84b;
    padding: 0.42rem 1.1rem;
    border-radius: 0.35rem;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.04em;
    cursor: pointer;
    transition: background-color 0.1s, color 0.1s;
  }
  .market-empty-retry:hover:not(:disabled) {
    background: #1a2e4a;
    color: #f5d97e;
  }
  .market-empty-retry:disabled {
    opacity: 0.55;
    cursor: wait;
  }
</style>
