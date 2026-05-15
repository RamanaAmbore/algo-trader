<svelte:head>
  <title>About RamboQuant | RamboQuant Analytics</title>
  <meta name="description" content="Learn about RamboQuant Analytics LLP — our investment philosophy, team, and partnership structure for accredited investors." />

  <!-- Open Graph -->
  <meta property="og:title" content="About RamboQuant | RamboQuant Analytics" />
  <meta property="og:description" content="Learn about RamboQuant Analytics LLP — our investment philosophy, team, and partnership structure for accredited investors." />
  <meta property="og:url" content="https://ramboq.com/about" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image.png" />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="About RamboQuant | RamboQuant Analytics" />
  <meta name="twitter:description" content="Learn about RamboQuant Analytics LLP — our investment philosophy, team, and partnership structure for accredited investors." />
  <meta name="twitter:image" content="https://ramboq.com/og-image.png" />
</svelte:head>

<div class="about-card">
  <h1 class="sr-only">About RamboQuant Analytics LLP</h1>
  <!-- Stat strip -->
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
      <div class="stat-creds">FRM · CFA II · XLRI</div>
      <div class="stat-label">Credentials &amp; Qualifications</div>
    </div>
  </div>

  <!-- Prose -->
  <div class="about-prose">
    <p>RamboQuant Analytics LLP is helmed by a seasoned FinTech professional with 25+ years of proven success in active investing and trading across both U.S. and Indian markets. What started in 1998 as a deep, hands-on exploration of equities and derivatives has evolved into a precision-driven investment powerhouse built on data, discipline, and performance.</p>

    <p>The founder's credentials speak for themselves — FRM Certified, CFA Level II, PGCBM from XLRI, and a Master's in Computer Science — matched with decades of cutting-edge innovation in financial technology for capital markets.</p>

    <p>With a long-term portfolio XIRR exceeding 22%, Rambo's approach blends high-conviction stock picks with strategic covered calls and intelligent derivatives positioning. Every move is grounded in deep analytics, rigorous risk management, and an unwavering commitment to the power of compounding.</p>

    <div class="closing-statement">
      RamboQuant Analytics is not for those content with average, index-like returns. It's built for partners who demand more — smarter strategies, sharper execution, and sustainable wealth creation.
    </div>
  </div>

  <div class="cta-row">
    <p class="cta-lead">Ready to work with us?</p>
    <div class="cta-btns">
      <a href="/contact" class="cta-btn">Contact us</a>
      <a href="/signin" class="cta-btn cta-btn-primary">Sign in</a>
    </div>
  </div>
</div>

<style>
  /* ── Card shell — public palette tokens ────────────────────────────────── */
  .about-card {
    background: #fffdf8;
    border-radius: 0.5rem;
    border: 1px solid #ddd8ce;
    box-shadow: 0 1px 4px rgba(0,0,0,0.07);
    overflow: hidden;
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
    font-size: 2rem;
    font-weight: 900;
    color: #1a2744;
    line-height: 1;
    letter-spacing: -0.02em;
  }
  .stat-plus {
    font-size: 1.2rem;
    font-weight: 700;
    color: #d4920c;
  }
  .stat-creds {
    font-size: 0.9rem;
    font-weight: 800;
    color: #1a2744;
    letter-spacing: 0.04em;
    line-height: 1.3;
  }
  .stat-label {
    font-size: 0.6rem;
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

  /* ── Prose ─────────────────────────────────────────────────────────────── */
  .about-prose {
    padding: 1.5rem 1.5rem 1.75rem;
  }
  .about-prose p {
    font-size: 0.9rem;
    color: #1e3050;
    line-height: 1.75;
    margin-bottom: 1.1rem;
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
    background: #d4920c;
    border-color: #d4920c;
    color: #fff;
  }
  .cta-btn-primary:hover {
    background: #b87a0a;
    border-color: #b87a0a;
    color: #fff;
  }

  .closing-statement {
    font-size: 0.95rem;
    font-weight: 600;
    color: #1a2744;
    line-height: 1.65;
    border-left: 3px solid #c8a84b;
    padding-left: 1rem;
    margin-top: 1.75rem;
    font-style: italic;
  }
</style>
