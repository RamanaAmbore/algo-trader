<script>
  // Public home — the two-audience front door. Investors and quant
  // recruiters both land here from ramboq.com; the page splits them
  // cleanly into two lanes (investor lane → /about, /performance,
  // /faq, /contact; platform lane → /dashboard demo).
  //
  // Prerendered (ssr=true, prerender=true via +page.js) so Googlebot
  // sees the full hero + partnership terms + Y-fork at first crawl.
  // The (public) layout's navbar + footer auto-wrap this markup.
</script>

<svelte:head>
  <title>Quantitative Investment Analytics for Indian Markets | RamboQuant Analytics</title>
  <meta name="description" content="RamboQuant Analytics LLP — long-term stock investments paired with algo-executed options strategies (covered calls, cash-secured puts, spreads, and more) using stocks and cash as margin. 25-year track record, 22%+ XIRR, LLP registered. Open platform showcase included." />
  <link rel="canonical" href="https://ramboq.com/" />

  <!-- Open Graph — HOME variant. Uses og-image-home.png (full-canvas
       brand hero, no text inside the image) so the card shows a clean
       picture and the og:title / og:description text below carry the
       narrative. -->
  <meta property="og:title" content="RamboQuant Analytics — Quantitative Investment for Indian Markets" />
  <meta property="og:description" content="Long-term stock investments paired with a toolkit of algo-executed options strategies — covered calls, cash-secured puts, spreads, and more — using stocks and cash as margin. 25-year track record, 22%+ XIRR." />
  <meta property="og:url" content="https://ramboq.com/" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image-home.png?v=2" />
  <meta property="og:image:width" content="1200" />
  <meta property="og:image:height" content="630" />
  <meta property="og:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="RamboQuant Analytics — Quantitative Investment for Indian Markets" />
  <meta name="twitter:description" content="Systematic, transparent portfolios for partner-investors. 25-year track record, 22%+ XIRR. Open algo trading platform showcase." />
  <meta name="twitter:image" content="https://ramboq.com/og-image-home.png?v=2" />
  <meta name="twitter:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />

  <!-- JSON-LD structured data -->
  {@html `<script type="application/ld+json">${JSON.stringify({
    "@context": "https://schema.org",
    "@type": "FinancialService",
    "name": "RamboQuant Analytics LLP",
    "url": "https://ramboq.com",
    "logo": "https://ramboq.com/og-image-home.png",
    "description": "Long-term stock investments paired with algo-executed options strategies — covered calls, cash-secured puts, spreads, and more. 25-year track record, 22%+ XIRR, LLP registered.",
    "founder": { "@type": "Person", "name": "Ramana R. Ambore" },
    "identifier": "ACU-5195",
    "areaServed": "IN",
    "knowsAbout": ["algorithmic trading", "options strategies", "portfolio management"]
  })}<\/script>`}
</svelte:head>

<div class="home">
  <!-- Hero — credibility-first headline + 4-cell trust strip. -->
  <section class="hero">
    <h1 class="pub-hero-title">RamboQuant Analytics LLP</h1>
    <p class="hero-tag">Long-term stock investments paired with a toolkit of algo-executed options strategies — covered calls, cash-secured puts, spreads, and more — using stocks and cash as margin. Stocks appreciate, premiums compound.</p>

    <div class="trust-strip">
      <div class="trust-cell">
        <div class="trust-num">22%<span class="trust-plus">+</span></div>
        <div class="trust-lbl">Long-term XIRR</div>
      </div>
      <div class="trust-sep"></div>
      <div class="trust-cell">
        <div class="trust-num">25<span class="trust-plus">+</span></div>
        <div class="trust-lbl">Years active</div>
      </div>
      <div class="trust-sep"></div>
      <div class="trust-cell">
        <div class="trust-creds">FRM · CFA II · XLRI</div>
        <div class="trust-lbl">Founder credentials</div>
      </div>
      <div class="trust-sep"></div>
      <div class="trust-cell">
        <div class="trust-creds">LLP · ACU-5195</div>
        <div class="trust-lbl">Registered entity</div>
      </div>
    </div>
  </section>

  <!-- Y-fork — the audience-routing chokepoint. Investors go left,
       quants / recruiters go right. The two cards are visually parallel
       (same border, same CTA shape) so neither audience feels secondary.
       Mobile stacks them vertically. -->
  <section class="fork">
    <a href="/about" class="fork-card fork-investor">
      <div class="fork-head">
        <span class="fork-tag">Investment Partnership</span>
        <span class="fork-arrow">→</span>
      </div>
      <h2 class="fork-title">Partner with us</h2>
      <p class="fork-sub">We hold high-conviction stocks for the long term, then use them — along with cash — as margin for a full toolkit of options strategies (covered calls, cash-secured puts, spreads, collars, wheels, hedges). Algo picks the right tool for the market and handles execution + risk. Two return streams: stock appreciation + option premium.</p>
      <ul class="fork-bullets">
        <li>Long stock book as the compounding base</li>
        <li>Multi-strategy options overlay for yield</li>
        <li>Live performance — real holdings, real P&amp;L</li>
      </ul>
      <span class="fork-cta">See partnership →</span>
    </a>

    <a href="/pulse" class="fork-card fork-quant">
      <div class="fork-head">
        <span class="fork-tag fork-tag-alt">Rambo Terminal</span>
        <span class="fork-arrow">→</span>
      </div>
      <h2 class="fork-title">Explore Rambo Terminal</h2>
      <p class="fork-sub">Production quant infrastructure that executes the strategy end-to-end — stock entries, multi-leg option writes, basket-margin checks, chase fills, risk overlays. Built by one engineer; demo runs against live broker data with accounts masked.</p>
      <ul class="fork-bullets">
        <li>Broker layer isolated as its own service — strategies hot-deploy without dropping sessions</li>
        <li>5-mode execution ladder — Sim → Replay → Paper → Live → Shadow</li>
        <li>Declarative agent grammar + 24/7 risk engine</li>
        <li>Black-Scholes options analytics with σ-driven payoff</li>
        <li>Persistent OHLCV / intraday data layer with self-healing backfill on under-coverage</li>
        <li>Closed-hours snapshot continuity — no blank grids, uninterrupted data visibility</li>
      </ul>
      <span class="fork-cta">Open Rambo Terminal →</span>
    </a>
  </section>

  <!-- Partnership Terms — investor-side detail surfaced above-the-fold
       so HNI / family-office prospects don't have to dig the FAQ for the
       basic shape of the deal. Four chips: minimum, fees, lock, strategy. -->
  <section class="terms">
    <div class="terms-head">
      <h2 class="terms-title">Partnership Terms — at a glance</h2>
      <a href="/faq" class="terms-link">Full FAQ →</a>
    </div>
    <div class="terms-grid">
      <div class="term">
        <div class="term-lbl">Minimum</div>
        <div class="term-val">₹10 lakh</div>
        <div class="term-note">Higher tiers by invitation</div>
      </div>
      <div class="term">
        <div class="term-lbl">Fees</div>
        <div class="term-val">50 : 50</div>
        <div class="term-note">Profit split above 10% hurdle</div>
      </div>
      <div class="term">
        <div class="term-lbl">Lock-in</div>
        <div class="term-val">Annual cycle</div>
        <div class="term-note">Redemption window: 28 Feb</div>
      </div>
      <div class="term">
        <div class="term-lbl">Strategy</div>
        <div class="term-val">Stocks + options overlay</div>
        <div class="term-note">Multi-strategy: calls, puts, spreads, wheels</div>
      </div>
    </div>
  </section>

  <!-- Soft closer + secondary CTAs. The Y-fork already carries the
       primary CTAs; this row exists for visitors who scrolled past
       both cards and still want a way out. -->
  <section class="closer">
    <p class="closer-line">Questions? <a href="/contact">Talk to us</a> · already a partner? <a href="/signin">Sign in</a></p>
    <p class="closer-disclaimer pub-callout">Investment in markets is subject to risk. Past performance is not indicative of future results.</p>
  </section>
</div>

<style>
  .home {
    max-width: 1100px;
    margin: 0 auto;
    padding: 1.25rem 0.5rem 2rem;
    color: #1a2744;
  }

  /* ── Hero ──────────────────────────────────────────────────────────── */
  .hero {
    text-align: center;
    padding: 1.5rem 1rem 1.25rem;
  }
  /* .hero-title retired — migrated to canonical .pub-hero-title in
     app.css so /, /about share one hero treatment. */
  .hero-tag {
    font-size: clamp(0.85rem, 2.2vw, 1rem);
    color: #3a4d6e;
    line-height: 1.55;
    max-width: 38rem;
    margin: 0 auto 1.25rem;
  }
  .trust-strip {
    display: flex;
    align-items: stretch;
    justify-content: center;
    flex-wrap: wrap;
    background: #f5f2eb;
    border: 1px solid #e7e0cf;
    border-radius: 0.4rem;
    padding: 0.25rem 0.5rem;
    margin: 0 auto;
    max-width: 56rem;
  }
  .trust-cell {
    flex: 1 1 8rem;
    padding: 0.7rem 0.5rem;
    text-align: center;
    min-width: 8rem;
  }
  .trust-sep {
    width: 1px;
    background: #ddd8ce;
    margin: 0.5rem 0;
  }
  .trust-num {
    font-size: 1.55rem;
    font-weight: 900;
    color: #1a2744;
    line-height: 1;
    letter-spacing: -0.02em;
    font-variant-numeric: tabular-nums;
  }
  .trust-plus {
    font-size: 0.95rem;
    font-weight: 700;
    color: #d4920c;
  }
  .trust-creds {
    font-size: 0.85rem;
    font-weight: 800;
    color: #1a2744;
    letter-spacing: 0.02em;
    line-height: 1.55rem;
  }
  .trust-lbl {
    font-size: 0.55rem;
    font-weight: 700;
    color: #5a7090;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 0.25rem;
  }
  @media (max-width: 600px) {
    .trust-strip { padding: 0.15rem 0.25rem; }
    .trust-cell { flex-basis: 45%; padding: 0.55rem 0.3rem; }
    .trust-sep { display: none; }
  }

  /* ── Y-fork ────────────────────────────────────────────────────────── */
  .fork {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin: 1.5rem 0 1.25rem;
  }
  @media (max-width: 700px) {
    .fork { grid-template-columns: 1fr; }
  }
  .fork-card {
    display: flex;
    flex-direction: column;
    background: #fffdf8;
    border: 1px solid #ddd8ce;
    border-radius: 0.5rem;
    padding: 1.1rem 1.2rem 1rem;
    text-decoration: none;
    color: inherit;
    transition: border-color 0.15s, box-shadow 0.15s, transform 0.15s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }
  .fork-card:hover {
    border-color: #c8a84b;
    box-shadow: 0 4px 14px rgba(212,146,12,0.14);
    transform: translateY(-1px);
  }
  /* Top-edge accent variants retired — operator: every public card
     should read as a flat panel. The fork cards keep their hover
     lift + champagne `.fork-cta` underline as the directional
     affordance; the colored top stripes added competing chrome
     that didn't earn its visual weight. */
  .fork-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.4rem;
  }
  .fork-tag {
    font-size: 0.7rem;
    font-weight: 700;
    color: #8b5d00;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    background: rgba(212,146,12,0.08);
    border: 1px solid rgba(212,146,12,0.25);
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
  }
  .fork-tag-alt {
    color: #1a4e7e;
    background: rgba(26,78,126,0.08);
    border-color: rgba(26,78,126,0.25);
  }
  .fork-arrow {
    font-size: 1rem;
    color: #c8a84b;
    transition: transform 0.15s;
  }
  .fork-card:hover .fork-arrow { transform: translateX(3px); }
  .fork-title {
    font-size: 1.15rem;
    font-weight: 800;
    color: #0c1830;
    margin: 0 0 0.4rem;
    line-height: 1.2;
  }
  .fork-sub {
    font-size: 0.82rem;
    color: #3a4d6e;
    line-height: 1.5;
    margin: 0 0 0.7rem;
  }
  .fork-bullets {
    list-style: none;
    padding: 0;
    margin: 0 0 0.9rem;
    flex: 1;
  }
  .fork-bullets li {
    font-size: 0.78rem;
    color: #1e3050;
    line-height: 1.4;
    padding: 0.18rem 0 0.18rem 1rem;
    position: relative;
  }
  .fork-bullets li::before {
    content: '✓';
    position: absolute;
    left: 0;
    color: #d4920c;
    font-weight: 700;
    font-size: 0.7rem;
    top: 0.22rem;
  }
  .fork-cta {
    font-size: 0.78rem;
    font-weight: 700;
    color: #b27908;
    letter-spacing: 0.02em;
    margin-top: auto;
    align-self: flex-start;
    border-bottom: 1px solid transparent;
    transition: border-color 0.15s;
  }
  .fork-card:hover .fork-cta { border-bottom-color: #c8a84b; }

  /* ── Partnership Terms ─────────────────────────────────────────────── */
  .terms {
    background: #fffdf8;
    border: 1px solid #ddd8ce;
    border-radius: 0.5rem;
    padding: 1rem 1.2rem 0.9rem;
    margin-bottom: 1.25rem;
  }
  .terms-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }
  .terms-title {
    font-size: 0.95rem;
    font-weight: 800;
    color: #0c1830;
    margin: 0;
    letter-spacing: -0.01em;
  }
  .terms-link {
    font-size: 0.72rem;
    font-weight: 600;
    color: #8f6000;
    text-decoration: none;
  }
  .terms-link:hover { text-decoration: underline; }
  .terms-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.75rem;
  }
  @media (max-width: 700px) {
    .terms-grid { grid-template-columns: repeat(2, 1fr); }
  }
  .term {
    padding: 0.5rem 0.6rem;
    background: #f5f2eb;
    border: 1px solid #e7e0cf;
    border-radius: 0.3rem;
  }
  .term-lbl {
    font-size: 0.55rem;
    font-weight: 700;
    color: #5a7090;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 0.2rem;
  }
  .term-val {
    font-size: 0.88rem;
    font-weight: 800;
    color: #0c1830;
    line-height: 1.15;
  }
  .term-note {
    font-size: 0.65rem;
    color: #5a7090;
    line-height: 1.35;
    margin-top: 0.2rem;
  }

  /* ── Closer ────────────────────────────────────────────────────────── */
  .closer {
    text-align: center;
    padding: 0.5rem 0.5rem 0;
  }
  .closer-line {
    font-size: 0.78rem;
    color: #5a7090;
    margin: 0 0 0.4rem;
  }
  .closer-line a {
    color: #b27908;
    font-weight: 600;
    text-decoration: none;
  }
  .closer-line a:hover { text-decoration: underline; }
  /* Italic + size kept local; frame + bg now come from the shared
     .pub-callout class so the SEBI risk note reads with the same
     weight as /performance's strategy thesis and /about's closing
     statement. */
  .closer-disclaimer {
    font-size: 0.62rem;
    margin: 1rem auto 0;
    max-width: 32rem;
    font-style: italic;
    text-align: center;
  }
</style>
