<svelte:head>
  <title>About RamboQuant | RamboQuant Analytics</title>
  <meta name="description" content="Learn about RamboQuant Analytics LLP — our investment philosophy, team, and partnership structure for accredited investors." />

  <!-- Open Graph -->
  <meta property="og:title" content="About RamboQuant | RamboQuant Analytics" />
  <meta property="og:description" content="Learn about RamboQuant Analytics LLP — our investment philosophy, team, and partnership structure for accredited investors." />
  <meta property="og:url" content="https://ramboq.com/about" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta property="og:image:width" content="600" />
  <meta property="og:image:height" content="600" />
  <meta property="og:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="About RamboQuant | RamboQuant Analytics" />
  <meta name="twitter:description" content="Learn about RamboQuant Analytics LLP — our investment philosophy, team, and partnership structure for accredited investors." />
  <meta name="twitter:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta name="twitter:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />

  <!-- JSON-LD structured data -->
  {@html `<script type="application/ld+json">${JSON.stringify({
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "RamboQuant Analytics LLP",
    "url": "https://ramboq.com/about",
    "logo": "https://ramboq.com/og-image-thumb.png",
    "description": "Quantitative investment analytics — long-term stock portfolio with algo-executed options overlay. 25+ years active investing, 22%+ XIRR.",
    "founder": {
      "@type": "Person",
      "name": "Haritha Chikile"
    },
    "employee": {
      "@type": "Person",
      "name": "Ramana R Ambore",
      "jobTitle": "Platform Architect & Quantitative Developer",
      "hasCredential": [
        "FRM (GARP, 2022)",
        "CFA Level 3 (candidate)",
        "PGCBM (XLRI)",
        "M.S. Computer Science",
        "Six Sigma Green Belt",
        "IBM Certified DB2 DBA",
        "Sun Certified Java Programmer"
      ]
    },
    "foundingDate": "2024",
    "areaServed": "IN"
  })}<\/script>`}
</svelte:head>

<h1 class="pub-hero-title about-hero">About RamboQuant Analytics LLP</h1>

<div class="about-card">
  <!-- Stat strip — four cells matching the home page trust strip so
       the trust signal (XIRR / years / creds / LLP) reads identically
       across both public entry points. -->
  <div class="stat-strip">
    <div class="stat-item">
      <div class="stat-num">22%<span class="stat-plus">+</span></div>
      <div class="stat-label">Long-term Portfolio XIRR</div>
    </div>
    <div class="stat-divider"></div>
    <div class="stat-item">
      <div class="stat-num">25<span class="stat-plus">+</span></div>
      <div class="stat-label">Years Active Investing</div>
    </div>
    <div class="stat-divider"></div>
    <div class="stat-item">
      <div class="stat-creds">FRM · CFA L3 (cand.) · XLRI</div>
      <div class="stat-label">Principal Credentials</div>
    </div>
    <div class="stat-divider"></div>
    <div class="stat-item">
      <div class="stat-creds">LLP</div>
      <div class="stat-label">Registered Entity</div>
    </div>
  </div>

  <!-- Principal profile block -->
  <div class="principal-block">
    <div class="principal-name">Ramana R Ambore</div>
    <div class="principal-title">Platform Architect &amp; Quantitative Developer, RamboQuant LLP</div>
    <a
      href="https://github.com/RamanaAmbore"
      target="_blank"
      rel="noopener"
      class="principal-chip"
    >GitHub · RamanaAmbore</a>
  </div>

  <!-- Prose -->
  <div class="about-prose">

    <div class="prose-section-label">Platform</div>

    <p>RamboQuant Analytics LLP runs an algorithmically driven investment program: a long-term, high-conviction stock portfolio paired with a disciplined options overlay, executed and monitored around the clock by a purpose-built trading platform. Every position, every fill, and every risk check is logged and auditable — what partners see in their statements reflects exactly what happened in the market, with no manual reconciliation gaps.</p>

    <p>With a long-term portfolio XIRR exceeding 22%, the strategy blends high-conviction stock picks with disciplined covered-call writing and selective derivatives positioning. Every decision is grounded in rigorous risk management and a long-term commitment to compounding.</p>

    <div class="closing-statement pub-callout">
      RamboQuant Analytics is not for those content with average, index-like returns. It's built for partners who demand more — smarter strategies, sharper execution, and sustainable wealth creation.
    </div>

    <div class="prose-section-label" style="margin-top: 1.5rem;">Founder</div>

    <p>Haritha Chikile — Founder, RamboQuant Analytics LLP.</p>

    <div class="prose-section-label" style="margin-top: 1.5rem;">Platform Architect &amp; Quantitative Developer</div>

    <p>Ramana R Ambore is the technical architect and developer behind RamboQuant's platform — he designed, built, and operates the AI-augmented trading system (built with Claude Code) that runs the firm's strategy end-to-end. He brings 30+ years of financial-services engineering experience to that work, including 19 years as a Principal System Analyst at Fidelity Investments, his primary day-to-day role.</p>

    <p>His credentials — FRM (GARP, 2022), CFA Level 3 candidate, PGCBM from XLRI, a Master's in Computer Science, Six Sigma Green Belt, IBM Certified DB2 DBA, and Sun Certified Java Programmer — reflect three decades of hands-on systems engineering. He was recognized with the NTT Innovation Award as a top-40 global innovator. The same engineering discipline used on institutional trading floors runs this platform at one-operator scale, so what partners see in their statements is exactly what happened in the market.</p>

  </div>

  <div class="cta-row">
    <p class="cta-lead">Ready to work with us?</p>
    <div class="cta-btns">
      <!-- B3: primary cold-visitor CTA points at the existing /contact
           page (no new lead-capture form — out of scope). Sign in is
           for existing partners, so it stays secondary. -->
      <a href="/contact" class="cta-btn cta-btn-primary">Contact us</a>
      <a href="/signin" class="cta-btn">Sign in</a>
    </div>
  </div>
</div>

<style>
  /* .about-heading retired — migrated to canonical .pub-hero-title
     in app.css so /, /about share one hero treatment. The hero now
     sits ABOVE the .about-card (was previously squeezed inside it)
     so the heading reads as a page title, not a card title. */
  .about-hero {
    margin: 1.5rem 0 1rem;
    padding: 0 1rem;
  }

  /* ── Card shell — public palette tokens ────────────────────────────────── */
  .about-card {
    background: #fffdf8;
    border-radius: 0.5rem;
    border: 1px solid #ddd8ce;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    overflow: hidden;
    /* Card fills the parent `.pub-card` width (capped at 1280px by the
       layout) — matches the /market page card width. Operator
       preference: wider card so the desktop stat-strip + prose + CTA
       feel proportionate to the rest of the public site instead of
       sitting as a narrow column. The prose paragraphs no longer carry
       a per-paragraph max-width either (see `.about-prose p` below). */
  }

  /* ── Stat strip — warm tint, cream border, champagne divider ───────────── */
  .stat-strip {
    display: flex;
    align-items: stretch;
    background: #f5f2eb;
    border-bottom: 1px solid #e7e0cf;
    padding: 0 1.75rem;
  }
  .stat-item {
    flex: 1;
    padding: 1.4rem 0;
    text-align: center;
  }
  .stat-divider {
    width: 1px;
    background: #ddd8ce;
    margin: 1rem 0;
  }
  .stat-num {
    font-size: 2.5rem;
    font-weight: 900;
    color: #1a2744;
    line-height: 1;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
  }
  .stat-plus {
    font-size: 1.5rem;
    font-weight: 700;
    /* B1: raw #d4920c measured ~2.65:1 on the card bg — fails even the
       large-text 3:1 floor (24px qualifies as "large"). Token clears it. */
    color: var(--card-accent-text-large, #b87a0a);
  }
  .stat-creds {
    /* Aligned vertically with .stat-num — the numeric stats render at
       font-size: 2.5rem with line-height: 1 (= 2.5rem block height).
       Bumped from 1.15rem to 1.35rem + line-height 2.5rem so the
       credential lines sit at the same baseline and read with
       comparable visual weight to the numerics. */
    font-size: 1.35rem;
    font-weight: 800;
    color: #1a2744;
    letter-spacing: 0.03em;
    line-height: 2.5rem;
  }
  @media (max-width: 600px) {
    /* Mobile already stacks cleanly; back the numerics off slightly so
       the column doesn't feel oversized. */
    .stat-num    { font-size: 2.1rem; }
    .stat-plus   { font-size: 1.3rem; }
    .stat-creds  { font-size: 1.15rem; line-height: 2.1rem; }
  }
  .stat-label {
    /* B2: 0.6rem (~9.6px) was below the 0.7rem legibility floor — one of
       the two confirmed-worst violations named in the plan. */
    font-size: 0.7rem;
    font-weight: 600;
    color: #5a7090;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 0.3rem;
  }
  @media (max-width: 480px) {
    .stat-strip { flex-direction: column; padding: 0 1.25rem; }
    .stat-divider { width: auto; height: 1px; margin: 0; }
    .stat-item { padding: 1rem 0; }
  }

  /* ── Principal profile block ───────────────────────────────────────────── */
  .principal-block {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem 1rem;
    padding: 1rem 1.5rem;
    background: #fffdf8;
    border-bottom: 1px solid #e7e0cf;
  }
  .principal-name {
    font-size: 1.05rem;
    font-weight: 800;
    color: #1a2744;
    letter-spacing: -0.01em;
  }
  .principal-title {
    font-size: 0.78rem;
    font-weight: 600;
    color: #5a7090;
    letter-spacing: 0.02em;
  }
  .principal-chip {
    display: inline-block;
    font-size: 0.72rem;
    font-weight: 700;
    color: #7a5c10;
    background: #f5f2eb;
    border: 1px solid #c8a84b;
    border-radius: 9999px;
    padding: 0.2rem 0.75rem;
    text-decoration: none;
    letter-spacing: 0.02em;
    transition: background 0.15s, color 0.15s;
    margin-left: auto;
  }
  .principal-chip:hover {
    background: #f4ead4;
    color: #5a4010;
  }
  @media (max-width: 480px) {
    .principal-chip { margin-left: 0; }
  }

  /* ── Prose ─────────────────────────────────────────────────────────────── */
  .about-prose {
    padding: 1.5rem 1.5rem 1.75rem;
  }
  .about-prose p {
    font-size: 0.9rem;
    color: #1e3050;
    line-height: 1.75;
    margin-bottom: 1.1rem;
    /* No max-width override: the card itself is capped at 760px on
       desktop (and full-width on mobile), so the paragraph naturally
       fills the available card width minus padding. Pre-fix the
       65ch + auto-margins constraint left big visible gutters
       INSIDE a full-width card on desktop — the stat-strip and CTA
       row filled edge-to-edge while the prose was centred at half-
       width. Looked visually broken. */
  }

  /* ── Prose section labels ──────────────────────────────────────────────── */
  .prose-section-label {
    /* B2: 0.6rem (~9.6px) bumped to the 0.7rem floor. */
    font-size: 0.7rem;
    font-weight: 700;
    color: #5a7090;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    margin-bottom: 0.75rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid #e7e0cf;
  }

  /* ── CTA row ───────────────────────────────────────────────────────────── */
  .cta-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.75rem;
    padding: 1.1rem 1.5rem;
    border-top: 1px solid #e7e0cf;
    background: #f5f2eb;
  }
  .cta-lead {
    font-size: 0.85rem;
    font-weight: 600;
    color: #5a7090;
    margin: 0;
  }
  .cta-btns {
    display: flex;
    gap: 0.6rem;
  }
  .cta-btn {
    display: inline-block;
    font-size: 0.8rem;
    font-weight: 700;
    padding: 0.35rem 1rem;
    border-radius: 9999px;
    border: 1px solid #c8a84b;
    color: #7a5c10;
    background: transparent;
    text-decoration: none;
    transition: background 0.15s, color 0.15s;
  }
  .cta-btn:hover {
    background: #f4ead4;
    color: #5a4010;
  }
  .cta-btn-primary {
    /* B1: white text on raw #d4920c measured ~2.65:1 (fails AA at
       0.8rem bold, not "large" text). Darker gold clears 4.5:1. */
    background: var(--card-accent-text, #8f6000);
    border-color: var(--card-accent-text, #8f6000);
    color: #fff;
  }
  .cta-btn-primary:hover {
    /* Reuses the same dark brown already used for .cta-btn:hover text
       above — deeper still, comfortably passing on hover. */
    background: #5a4010;
    border-color: #5a4010;
    color: #fff;
  }

  /* Italic font + size kept local; frame + bg now come from
     the shared .pub-callout class in app.css. */
  .closing-statement {
    font-size: 0.95rem;
    font-weight: 600;
    line-height: 1.65;
    margin-top: 1.75rem;
    font-style: italic;
  }
</style>
