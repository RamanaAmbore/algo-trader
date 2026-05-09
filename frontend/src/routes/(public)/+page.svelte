<script>
  // Root URL — render About content directly so Googlebot sees
  // real page content on first crawl without JS. The (public) layout
  // is not applied here (different route group), so this page carries
  // its own minimal SEO head block.
  // Client-side navigation still works because the SPA fallback in
  // adapter-static serves index.html for every unmatched route; the
  // prerendered /index.html overrides that for the root path only.
</script>

<svelte:head>
  <title>Quantitative Investment Analytics for Indian Markets | RamboQuant Analytics</title>
  <meta name="description" content="RamboQuant builds systematic, transparent portfolios for partner-investors. Track real-time performance, market reports, and risk metrics." />
  <link rel="canonical" href="https://ramboq.com/" />

  <!-- Open Graph -->
  <meta property="og:title" content="RamboQuant Analytics — Quantitative Investment for Indian Markets" />
  <meta property="og:description" content="RamboQuant builds systematic, transparent portfolios for partner-investors. Track real-time performance, market reports, and risk metrics." />
  <meta property="og:url" content="https://ramboq.com/" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image.svg" />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="RamboQuant Analytics — Quantitative Investment for Indian Markets" />
  <meta name="twitter:description" content="RamboQuant builds systematic, transparent portfolios for partner-investors. Track real-time performance, market reports, and risk metrics." />
  <meta name="twitter:image" content="https://ramboq.com/og-image.svg" />
</svelte:head>

<!-- Minimal above-fold content for Googlebot. The client-side
     router will take over and render the full (public) layout after
     hydration; this block is what the prerendered HTML contains. -->
<div style="font-family: 'Trebuchet MS', Arial, sans-serif; max-width: 820px; margin: 3rem auto; padding: 2rem 1.5rem; color: #1a1e35;">
  <h1 style="font-size: 1.6rem; font-weight: 900; color: #0c1830; letter-spacing: -0.01em; margin-bottom: 0.5rem;">
    RamboQuant Analytics LLP
  </h1>
  <p style="font-size: 1rem; color: #1a1e35; margin-bottom: 1.5rem; line-height: 1.6;">
    Quantitative investment analytics for Indian markets — systematic, transparent portfolios for partner-investors.
  </p>
  <p style="font-size: 0.9rem; color: #1e3050; line-height: 1.75; margin-bottom: 1rem;">
    RamboQuant Analytics LLP is helmed by a seasoned FinTech professional with 25+ years of proven success in active
    investing and trading across both U.S. and Indian markets. Blending high-conviction stock picks with strategic
    covered calls and intelligent derivatives positioning.
  </p>
  <p style="font-size: 0.9rem; color: #1e3050; line-height: 1.75;">
    With a long-term portfolio XIRR exceeding 22%, every move is grounded in deep analytics, rigorous risk management,
    and an unwavering commitment to the power of compounding.
  </p>
  <noscript>
    <p style="margin-top: 2rem;"><a href="/about" style="color: #0c1830; font-weight: 700;">Learn more about RamboQuant →</a></p>
  </noscript>
</div>
