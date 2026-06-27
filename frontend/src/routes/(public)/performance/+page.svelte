<script>
  import PerformancePage from '$lib/PerformancePage.svelte';

  // /performance is now the position + holdings page only. The
  // Market Summary + News-feed tabbed card that used to sit
  // beneath the grids was removed — operators wanted the page to
  // stay focused on the book. The same content lives on /market
  // (Daily Market Report) and the LogPanel News tab on the algo
  // side, so nothing is gone, just decoupled.
</script>

<svelte:head>
  <title>Live Portfolio Performance | RamboQuant Analytics</title>
  <meta name="description" content="Real-time view of RamboQuant's holdings, positions, and portfolio P&amp;L." />

  <!-- Open Graph -->
  <meta property="og:title" content="Live Portfolio Performance | RamboQuant Analytics" />
  <meta property="og:description" content="Real-time view of RamboQuant's holdings, positions, and portfolio P&L." />
  <meta property="og:url" content="https://ramboq.com/performance" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta property="og:image:width" content="600" />
  <meta property="og:image:height" content="600" />
  <meta property="og:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="Live Portfolio Performance | RamboQuant Analytics" />
  <meta name="twitter:description" content="Real-time view of RamboQuant's holdings, positions, and portfolio P&L." />
  <meta name="twitter:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta name="twitter:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
</svelte:head>

<style>
  /* .perf-heading retired — migrated to canonical .pub-page-heading
     in app.css. Local wrapper kept for the padded top inset that
     matches the page's first-section spacing. */
  .perf-heading-wrap {
    padding: 1.25rem 1.25rem 0;
  }
  @media (max-width: 600px) {
    .perf-heading-wrap { padding: 1rem 0.75rem 0; }
  }
</style>

<div class="perf-heading-wrap">
  <h1 class="pub-page-heading">Live Portfolio Performance</h1>
</div>
<PerformancePage />
