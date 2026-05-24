<script>
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';

  // Recruiter-facing narrative page. Single scrollable tour that
  // explains the platform's novel pieces (5-mode execution ladder,
  // declarative agent grammar, BS options analytics, multi-broker
  // abstraction) and links each one to the live surface where the
  // viewer can poke at it. No data fetches — pure prose + links so
  // the page loads instantly even on a recruiter's flaky phone.

  // FOUC gate. The page lives under (algo)/ which is ssr=false +
  // prerender=false; on a first-paint navigation the HTML shell + JS
  // bundle race the scoped CSS chunk, so visitors momentarily saw the
  // raw text (default browser styling) before the cards snapped into
  // their decorated form. onMount only fires once both the component
  // JS and its scoped CSS have arrived, so the gate releases at the
  // first frame where decoration is guaranteed.
  let _mounted = $state(false);
  onMount(() => { _mounted = true; });

  /** @type {Array<{
   *   title: string,
   *   tag: string,
   *   body: string,
   *   bullets: string[],
   *   link?: { href: string, label: string },
   *   color: string,
   * }>} */
  const sections = [
    {
      title: 'Multi-mode execution ladder',
      tag: 'Execution',
      color: '#fbbf24',
      body: 'Every agent and order graduates through a confidence ladder before real money moves. Mode is a first-class concept in the codebase — resolved per action, never hardcoded.',
      bullets: [
        'Simulator — fabricated price moves drive the same engine that runs live',
        'Replay — historical Kite candles feed the same paper trade engine',
        'Paper — real broker quotes, validated via Kite basket_margin, no execution',
        'Live — real broker order. Single backend flag flips paper→live on prod only',
        'Shadow — logs exact Kite payload + validates, executes nothing. Audit trail before promotion',
      ],
      link: { href: '/admin/execution?mode=sim', label: 'Open Lab (sim)' },
    },
    {
      title: 'Declarative agent grammar',
      tag: 'Risk engine',
      color: '#7dd3fc',
      body: 'Loss rules, fund-negative alerts, expiry auto-close — all expressed as a metric/scope/op/value tree in a database table. Adding a new rule type is one DB row plus one Python resolver function, never an engine change.',
      bullets: [
        '14 loss-* agents ship live (per-account + total, abs + pct + rate thresholds)',
        'Cooldown + suppression-delta gates prevent alert storms during volatile sessions',
        'Schedule-aware: market-hours agents skip when no segment is open',
        'Telegram + email + WebSocket + log channels, per-agent opt-in',
      ],
      link: { href: '/agents', label: 'Browse agents' },
    },
    {
      title: 'Derivatives analytics with σ-driven payoff',
      tag: 'Quant math',
      color: '#a5b4fc',
      body: 'Black-Scholes pricer, calibrated IV per leg, multi-leg payoff with shaded P&L zone + breakevens + POP integrated against the lognormal pdf. Payoff range auto-scales to ±2.5σ × √T so short-DTE options aren\'t squashed into the centre and long-DTE ones aren\'t cut off.',
      bullets: [
        'Greeks (Δ Γ Θ V ρ) in trader-friendly units, per-leg and position-summed',
        'Expected value via trapezoidal integration of expiry curve × lognormal pdf',
        'R:R ratio for bounded structures, ∞ rendered correctly for unbounded legs',
        'Coherent underlying re-pricing — one "NIFTY −1%" tick cascades through every contract',
      ],
      link: { href: '/admin/options', label: 'Open Derivatives' },
    },
    {
      title: 'Multi-broker abstraction + IPv6 binding',
      tag: 'Infra',
      color: '#86efac',
      body: 'Single Broker ABC hides Zerodha Kite, Dhan, Groww behind one interface. Each account binds to a unique IPv6 from the server\'s /48 subnet to work around Kite\'s one-IP-per-app restriction.',
      bullets: [
        'PriceBroker wraps every adapter with automatic failover for quote / LTP / historical',
        'Dhan + Groww adapters share the Kite-shaped interface — zero call-site changes',
        'Per-account source_ip binds the HTTP pool manager via a custom requests.Adapter',
        'Credentials encrypted at rest (Fernet, HKDF-derived key) — secrets.yaml is bootstrap-only',
      ],
      link: { href: '/admin/brokers', label: 'Brokers (admin)' },
    },
    {
      title: 'Live chase engine + paper engine',
      tag: 'Trading',
      color: '#fb923c',
      body: 'Adaptive limit-order chase loop sits between the agent decision and Kite. Same engine runs paper (real quotes, no execution) and live (real execution), so every paper fire validates the live code path.',
      bullets: [
        'Spread-aware bid/ask sourced from broker depth or simulator config',
        'Per-tick fill check (bid ≥ limit for SELL, ask ≤ limit for BUY)',
        'Cancel + re-quote after N attempts; row flips to UNFILLED at cap',
        'Postback HMAC validated server-side; fill events broadcast over WebSocket',
      ],
      link: { href: '/orders', label: 'Open Orders' },
    },
    {
      title: 'Production deployment',
      tag: 'Ops',
      color: '#c4b5fd',
      body: 'GitHub push → webhook → branch-routed deploy → systemctl restart. Two environments on one box (prod/main + dev/non-main), separate databases, separate log streams, separate broker accounts.',
      bullets: [
        'PostgreSQL 17 + SQLAlchemy 2.x async + asyncpg',
        'Litestar 2.x + msgspec.Struct (≈10× faster than pydantic) + Polars route aggregation',
        'SvelteKit 5 + ag-Grid v33 + hand-rolled SVG charts (no chart library, thin bundle)',
        'Telegram + email alerts dual-routed (operator inbox + public-website form)',
      ],
      link: { href: '/dashboard', label: 'Open Dashboard' },
    },
  ];

  const facts = [
    { val: '~70k', lbl: 'lines of code' },
    { val: '5',    lbl: 'execution modes' },
    { val: '3',    lbl: 'broker adapters' },
    { val: '14+',  lbl: 'loss/risk agents' },
    { val: '24×7', lbl: 'background risk engine' },
    { val: '1',    lbl: 'engineer (start to ship)' },
  ];
</script>

<svelte:head>
  <title>Rambo Terminal — Tour | RamboQuant</title>
  <meta name="description" content="Rambo Terminal — a walkthrough of RamboQuant's quant trading terminal: 5-mode execution ladder, declarative agent grammar, Black-Scholes derivatives analytics, multi-broker abstraction." />
</svelte:head>

<div class="show" class:show-ready={_mounted}>
  <!-- Hero -->
  <header class="show-hero">
    <h1 class="show-title">Rambo Terminal — Tour</h1>
    <p class="show-tag">A production quant trading terminal that runs a real strategy: hold high-conviction stocks long, then use them (and cash) as margin for a toolkit of derivative strategies — covered calls, cash-secured puts, vertical / calendar spreads, collars, wheels, futures, hedges — letting the algo pick the right tool for the market and handle execution + risk. Built end-to-end by one engineer for the RamboQuant LLP partnership — you're looking at the live system, accounts masked.</p>
    <div class="show-facts">
      {#each facts as f}
        <div class="show-fact">
          <div class="show-fact-val">{f.val}</div>
          <div class="show-fact-lbl">{f.lbl}</div>
        </div>
      {/each}
    </div>
  </header>

  <!-- Section cards. Each one teaches a concept and links to the live
       surface where the viewer can poke at it. -->
  <section class="show-grid">
    {#each sections as s, i}
      <article class="show-card" style="--accent: {s.color}">
        <div class="show-card-head">
          <span class="show-card-tag" style="--accent: {s.color}">{s.tag}</span>
          <span class="show-card-num">{String(i + 1).padStart(2, '0')}</span>
        </div>
        <h2 class="show-card-title">{s.title}</h2>
        <p class="show-card-body">{s.body}</p>
        <ul class="show-card-bullets">
          {#each s.bullets as b}
            <li>{b}</li>
          {/each}
        </ul>
        {#if s.link}
          <button class="show-card-link" onclick={() => goto(s.link.href)}>
            {s.link.label} →
          </button>
        {/if}
      </article>
    {/each}
  </section>

  <!-- Footer-CTA — get the recruiter to take one of the two next
       actions before they close the tab. -->
  <footer class="show-footer">
    <p class="show-footer-line">Want the source story or a deeper conversation?</p>
    <div class="show-footer-btns">
      <a href="/about" class="show-footer-btn show-footer-btn-alt">↙ About the founder</a>
      <a href="/contact" class="show-footer-btn">Get in touch →</a>
    </div>
  </footer>
</div>

<style>
  .show {
    max-width: 1180px;
    margin: 0 auto;
    /* Top padding tightened (was 1rem) — the algo layout already
       provides ~0.5rem above the main, plus the hero's own 1.25rem
       top padding. The double-stack created an unintentional 2.5rem
       gap above the H1; trimming to 0.25rem here lets the hero
       breathe at its own natural top-padding. */
    padding: 0.25rem 0.5rem 2rem;
    color: #c8d8f0;
    /* FOUC gate — hidden until onMount sets .show-ready. Stops the
       brief flash of plain-text hero + bullet lists between the
       JS bundle landing and the scoped CSS chunk applying. */
    opacity: 0;
    transition: opacity 0.18s ease-out;
  }
  .show.show-ready {
    opacity: 1;
  }

  /* ── Hero ─────────────────────────────────────────────────────────── */
  .show-hero {
    text-align: center;
    /* Top padding tightened (was 1.5rem) so the H1 sits closer to
       the navbar / banners. The bottom padding stays generous so
       the hero feels intentionally separated from the section
       grid below. */
    padding: 0.5rem 1rem 1.75rem;
    border-bottom: 1px solid rgba(126, 151, 184, 0.18);
    margin-bottom: 1.5rem;
  }
  .show-title {
    font-size: clamp(1.4rem, 4vw, 1.9rem);
    font-weight: 800;
    color: #fbbf24;
    letter-spacing: -0.01em;
    margin: 0 0 0.6rem;
    line-height: 1.15;
  }
  .show-tag {
    font-size: clamp(0.85rem, 2vw, 1rem);
    color: #9bb0d0;
    line-height: 1.6;
    max-width: 42rem;
    margin: 0 auto 1.4rem;
  }
  .show-facts {
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    gap: 0.4rem 0.6rem;
    margin-top: 0.5rem;
  }
  .show-fact {
    background: rgba(126, 151, 184, 0.08);
    border: 1px solid rgba(126, 151, 184, 0.20);
    border-radius: 0.3rem;
    padding: 0.45rem 0.7rem;
    text-align: center;
    min-width: 6.5rem;
  }
  .show-fact-val {
    font-size: 1.05rem;
    font-weight: 800;
    color: #fbbf24;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
  }
  .show-fact-lbl {
    font-size: 0.55rem;
    font-weight: 700;
    color: #7e97b8;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 0.2rem;
  }

  /* ── Section grid ──────────────────────────────────────────────────── */
  .show-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
    gap: 1rem;
  }
  .show-card {
    background: rgba(15, 25, 45, 0.55);
    border: 1px solid rgba(126, 151, 184, 0.18);
    border-top: 3px solid var(--accent);
    border-radius: 0.5rem;
    padding: 1rem 1.1rem 0.9rem;
    display: flex;
    flex-direction: column;
  }
  .show-card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }
  .show-card-tag {
    font-size: 0.58rem;
    font-weight: 700;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 0.15rem 0.5rem;
    background: color-mix(in srgb, var(--accent) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--accent) 35%, transparent);
    border-radius: 999px;
  }
  .show-card-num {
    font-family: ui-monospace, monospace;
    font-size: 0.6rem;
    color: #7e97b8;
    letter-spacing: 0.05em;
  }
  .show-card-title {
    font-size: 1.05rem;
    font-weight: 800;
    color: #e8efff;
    margin: 0 0 0.45rem;
    line-height: 1.2;
  }
  .show-card-body {
    font-size: 0.85rem;
    color: #9bb0d0;
    line-height: 1.55;
    margin: 0 0 0.6rem;
  }
  .show-card-bullets {
    list-style: none;
    padding: 0;
    margin: 0 0 0.85rem;
    flex: 1;
  }
  .show-card-bullets li {
    font-size: 0.82rem;
    color: #c8d8f0;
    line-height: 1.45;
    padding: 0.18rem 0 0.18rem 1rem;
    position: relative;
  }
  /* Mobile bump — keep card body legible without pinch-to-zoom; tag
     chips also bumped so they don't read as decorative noise. */
  @media (max-width: 600px) {
    .show-card-body     { font-size: 0.9rem; }
    .show-card-bullets li { font-size: 0.88rem; }
    .show-card-tag      { font-size: 0.65rem; }
  }
  .show-card-bullets li::before {
    content: '▸';
    position: absolute;
    left: 0;
    color: var(--accent);
    font-size: 0.65rem;
    top: 0.28rem;
  }
  .show-card-link {
    background: transparent;
    border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
    color: var(--accent);
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    padding: 0.35rem 0.75rem;
    border-radius: 0.3rem;
    cursor: pointer;
    align-self: flex-start;
    transition: background-color 0.1s, border-color 0.1s;
    font-family: inherit;
  }
  .show-card-link:hover {
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    border-color: var(--accent);
  }

  /* ── Footer CTA ───────────────────────────────────────────────────── */
  .show-footer {
    text-align: center;
    padding: 1.5rem 1rem 0.5rem;
    margin-top: 1.5rem;
    border-top: 1px solid rgba(126, 151, 184, 0.18);
  }
  .show-footer-line {
    font-size: 0.85rem;
    color: #9bb0d0;
    margin: 0 0 0.75rem;
  }
  .show-footer-btns {
    display: flex;
    gap: 0.6rem;
    justify-content: center;
    flex-wrap: wrap;
  }
  .show-footer-btn {
    display: inline-block;
    font-size: 0.78rem;
    font-weight: 700;
    padding: 0.45rem 1rem;
    border-radius: 0.3rem;
    background: rgba(251, 191, 36, 0.10);
    color: #fbbf24;
    border: 1px solid rgba(251, 191, 36, 0.40);
    text-decoration: none;
    transition: background-color 0.1s, border-color 0.1s;
  }
  .show-footer-btn:hover {
    background: rgba(251, 191, 36, 0.20);
    border-color: rgba(251, 191, 36, 0.65);
  }
  .show-footer-btn-alt {
    background: transparent;
    color: #9bb0d0;
    border-color: rgba(126, 151, 184, 0.35);
  }
  .show-footer-btn-alt:hover {
    background: rgba(126, 151, 184, 0.10);
    color: #c8d8f0;
    border-color: rgba(126, 151, 184, 0.55);
  }
</style>
