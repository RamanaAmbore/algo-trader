<script>
  import { onMount } from 'svelte';
  import { fetchPost } from '$lib/api';
  import { dataCache } from '$lib/stores';

  let content     = $state('');
  let lastRefresh = $state('');
  let loading     = $state(true);
  let error       = $state('');

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
      } else if (/^[-*]\s/.test(raw)) {
        if (!inList) { html += '<ul class="md-ul">'; inList = true; }
        html += `<li class="md-li">${line.replace(/^[-*]\s/, '')}</li>`;
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

  onMount(async () => {
    // Show cached content immediately — no empty flash on back-navigation
    if (dataCache.insights) {
      content     = dataCache.insights.content ?? '';
      lastRefresh = dataCache.insights.refreshed_at ?? '';
      loading = false;
    }
    try {
      const data = await fetchPost();
      content     = data.content ?? '';
      lastRefresh = data.refreshed_at ?? '';
      dataCache.insights = data;
    } catch (e) {
      if (!content) error = e.message || 'Failed to load insights';
    } finally { loading = false; }
  });
</script>
<svelte:head>
  <title>Investment Insights | RamboQuant Analytics</title>
  <meta name="description" content="Investment insights and market analysis from RamboQuant Analytics." />
</svelte:head>


{#if lastRefresh}
  <div class="text-[0.65rem] text-muted mb-3">
    <span>{lastRefresh}</span>
  </div>
{/if}

<div class="bg-white rounded-lg border border-gray-200 shadow-sm p-5 pt-4">
{#if loading && !content}
  <div class="text-center text-text/40 animate-pulse text-sm py-8">
    Loading…
  </div>
{:else if error && !content}
  <div class="p-3 rounded bg-red-50 text-red-700 text-sm border border-red-200">{error}</div>
{:else if !content}
  <div class="flex flex-col items-center justify-center py-12 text-center">
    <h2 class="text-base font-semibold text-[#1a2744] mb-2">Insights coming soon</h2>
    <p class="text-sm text-[#1e3050]/80 max-w-sm leading-relaxed">
      We publish thoughts on markets, trading systems, and risk management here.
      Check back soon — or <a href="/contact" class="underline text-[#1a2744] hover:text-[#c8a84b]">contact us</a> if there's something specific you'd like us to write about.
    </p>
  </div>
{:else if content}
  <div class="md-body">
    {@html renderMarkdown(content)}
  </div>
{/if}
</div>

<style>
  :global(.md-body .md-h3) {
    font-size: 1rem;
    font-weight: 700;
    color: #1a2744;
    margin: 1.4rem 0 0.4rem;
    border-left: 3px solid #c8a84b;
    border-bottom: none;
    padding-left: 0.5rem;
  }
  :global(.md-body .md-h4) { font-size: 0.875rem; font-weight: 600; color: #1a2744; margin: 1rem 0 0.3rem; }
  :global(.md-body .md-p)  { font-size: 0.875rem; color: #1e3050; line-height: 1.65; margin: 0.25rem 0; }
  :global(.md-body .md-ul) { margin: 0.4rem 0 0.4rem 1.2rem; }
  :global(.md-body .md-li) { font-size: 0.875rem; color: #1e3050; line-height: 1.6; list-style: disc; }
  :global(.md-body .md-hr) { border: none; border-top: 1px solid #dde4f0; margin: 1.2rem 0; }
  :global(.md-body .md-gap){ height: 0.5rem; }
</style>
