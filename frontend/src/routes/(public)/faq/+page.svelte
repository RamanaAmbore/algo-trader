<script>
  import { onMount } from 'svelte';

  const faqs = [
    {
      q: "What is RamboQuant Analytics LLP?",
      a: "A Limited Liability Partnership where partners pool capital, managed by Active Partners and a Fund Manager. The LLP invests in growth-oriented equity with covered call and other derivative (F&O) strategies.",
    },
    {
      q: "Who can become a partner?",
      a: "Any Indian citizen, Non-Resident Indian (NRI), or Overseas Citizen of India (OCI) meeting the LLP's eligibility criteria.",
    },
    {
      q: "Minimum Contribution",
      a: "Capital contribution amounts are decided by the Active Partners. The first tier carries a 10% profit threshold. For every additional tier of contribution, the profit threshold increases by 0.25%.",
    },
    {
      q: "Profit Calculation",
      a: "Calculated annually on 31 March. LLP expenses are deducted at actuals before NAV and profit calculation. Partners receive threshold return first. Excess profit split 50:50 between partner and LLP. Profits are calculated before tax; LLP pays taxes before distribution.",
    },
    {
      q: "If Profits Are Below Threshold or There Is a Loss",
      a: "If annual profit is below threshold, the closing NAV becomes the new reference NAV for the next year. If there is a loss, no profit is distributed until NAV recovers to the last reference NAV.",
    },
    {
      q: "Redemption Rules",
      a: "Requests only once a year, submitted by 28 February. Processed after 31 March NAV calculation. No mid-year redemption. Mid-year contributions can be redeemed only after 31 March of the following year.",
    },
    {
      q: "Taxation",
      a: "Profits taxed at LLP level under current Indian law; no further tax for partners in India. For NRIs/OCIs, Indian tax applies; foreign tax rules may also apply per DTAA. Tax laws may change; partners should seek professional advice.",
    },
    {
      q: "NAV Calculation",
      a: "Official NAV: 31 March annually. LLP expenses are deducted at actuals before NAV is declared. Interim NAV for reporting only; not used for profit distribution.",
    },
  ];

  // Mermaid diagrams from frontend_config.yaml
  const diagrams = [
    {
      title: "NAV & Profit Distribution Flow",
      id: "nav-flow",
      definition: `flowchart TD
    A[Capital Contribution by Partners] --> B[Strategies in Growth-Oriented Equity + Covered Call + F&O Strategies]
    B --> C[31 March: Calculate LLP NAV]
    C --> D[Deduct LLP Expenses at Actuals]
    D --> E[Calculate Profit Before Tax]
    E --> F[Compare NAV Growth with Profit Threshold]
    F -->|Below Threshold| G[Closing NAV Becomes New Reference NAV]
    F -->|Above Threshold| H[Distribute Threshold Return First]
    H --> I[Excess Profit Split 50:50 Between Partner & LLP]
    I --> J[LLP Share Split Between Active Partners & Fund Manager]
    G --> K[Carry Forward NAV Until Recovery to Reference NAV]`,
    },
    {
      title: "Redemption Flow",
      id: "redemption-flow",
      definition: `flowchart TD
    A[Partner Decides to Redeem] --> B[Check if Contribution is Mid-Year]
    B -->|Mid-Year Contribution| H[Eligible Only After 31 March of Next Year]
    B -->|Not Mid-Year| C[Submit Request by 28 Feb]
    C -->|Missed Deadline| D[Wait Until Next Year's 28 Feb]
    C -->|On Time| E[Process After 31 March NAV Calculation]
    E --> F[Adjust NAV Post Redemption]
    F --> G[Reallocate Remaining Partner Units]
    G --> I[Capital Transfer to Redeeming Partner]`,
    },
    {
      title: "Succession Flow",
      id: "succession-flow",
      definition: `flowchart TD
    A[Active Partner decides wind down or Is Incapacitated] --> B[1-Month Written Notice or Incapacity Trigger]
    B --> C[Identify Partner with Largest Capital Contribution]
    C --> D[New Active Partner Decides Within 1 Month]
    B -->|Wind Down| F[Distribute NAV & Profits to All Partners]
    D --> F`,
    },
  ];

  let open = -1;

  onMount(async () => {
    /** @type {any} */
    const win = window;
    // Dynamically load mermaid — no npm package needed, CDN via script tag.
    // TODO: add integrity="sha384-..." SRI hash for mermaid@11 once the
    // exact build hash is confirmed from the jsDelivr release manifest.
    // crossorigin="anonymous" is required for SRI enforcement.
    if (!win.mermaid) {
      const loaded = await new Promise((resolve) => {
        const s = document.createElement('script');
        s.src = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js';
        s.crossOrigin = 'anonymous';
        // TODO: s.integrity = 'sha384-...';
        const timer = setTimeout(() => { s.onerror?.('timeout'); }, 8000);
        s.onload  = () => { clearTimeout(timer); resolve(true); };
        s.onerror = () => { clearTimeout(timer); resolve(false); };
        document.head.appendChild(s);
      });
      if (!loaded) {
        // CDN load failed — show fallback in every diagram container
        for (const d of diagrams) {
          const el = document.getElementById(`mermaid-${d.id}`);
          if (el) el.innerHTML = '<p class="text-xs text-muted py-6 text-center">Diagrams unavailable — try again later.</p>';
        }
        return;
      }
    }
    win.mermaid.initialize({
      startOnLoad: false,
      theme: 'base',
      themeVariables: {
        primaryColor:       '#e8f0f0',
        primaryTextColor:   '#315062',
        primaryBorderColor: '#2f4f4f',
        lineColor:          '#2f4f4f',
        edgeLabelBackground:'#fff8ee',
        tertiaryColor:      '#f5f5f7',
        fontFamily:         'ui-sans-serif, system-ui, sans-serif',
        fontSize:           '13px',
      },
    });

    for (const d of diagrams) {
      const el = document.getElementById(`mermaid-${d.id}`);
      if (!el) continue;
      try {
        const { svg } = await win.mermaid.render(`mermaid-svg-${d.id}`, d.definition);
        el.innerHTML = svg;
        // Set rx/ry as SVG attributes for maximum browser compatibility
        el.querySelectorAll('.node rect').forEach(r => { r.setAttribute('rx','8'); r.setAttribute('ry','8'); });
        el.querySelectorAll('.node polygon').forEach(r => { r.setAttribute('rx','6'); r.setAttribute('ry','6'); });
      } catch (e) {
        el.innerHTML = `<pre class="text-xs text-red-600 p-2">${d.definition}</pre>`;
      }
    }
  });
</script>

<svelte:head>
  <title>Frequently Asked Questions | RamboQuant Analytics</title>
  <meta name="description" content="Answers to common questions about RamboQuant's investment strategies, partnership terms, fees, and operations." />

  <!-- Open Graph -->
  <meta property="og:title" content="Frequently Asked Questions | RamboQuant Analytics" />
  <meta property="og:description" content="Answers to common questions about RamboQuant's investment strategies, partnership terms, fees, and operations." />
  <meta property="og:url" content="https://ramboq.com/faq" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta property="og:image:width" content="600" />
  <meta property="og:image:height" content="600" />
  <meta property="og:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="Frequently Asked Questions | RamboQuant Analytics" />
  <meta name="twitter:description" content="Answers to common questions about RamboQuant's investment strategies, partnership terms, fees, and operations." />
  <meta name="twitter:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta name="twitter:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />

  <!-- FAQPage structured data — generated from the faqs array above -->
  {@html `<script type="application/ld+json">${JSON.stringify({
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": faqs.map(f => ({
      "@type": "Question",
      "name": f.q,
      "acceptedAnswer": { "@type": "Answer", "text": f.a }
    }))
  })}</script>`}
</svelte:head>


<div class="pub-card rounded-lg shadow-sm p-5 pt-4">
<h1 class="page-heading">Frequently Asked Questions</h1>

<div class="faq-list mb-10">
  {#each faqs as faq, i}
    <div class="faq-item {open === i ? 'faq-open' : ''}">
      <button
        class="faq-question"
        onclick={() => open = open === i ? -1 : i}
        aria-expanded={open === i}
        aria-controls={`faq-answer-${i}`}
        id={`faq-btn-${i}`}
      >
        <span>{faq.q}</span>
        <svg
          class="faq-chevron {open === i ? 'rotate-180' : ''}"
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
        </svg>
      </button>
      {#if open === i}
        <div class="faq-answer" id={`faq-answer-${i}`} role="region" aria-labelledby={`faq-btn-${i}`}>
          {faq.a}
        </div>
      {/if}
    </div>
  {/each}
</div>

<h2 class="page-heading">Process Flows</h2>
<div class="space-y-6">
  {#each diagrams as d}
    <div class="flow-card">
      <div class="flow-card-header">
        <h3 class="flow-card-title">{d.title}</h3>
      </div>
      <div class="p-4 overflow-x-auto">
        <div id="mermaid-{d.id}" class="mermaid-container flex justify-center">
          <div class="text-xs text-muted animate-pulse py-8">Loading diagram…</div>
        </div>
      </div>
    </div>
  {/each}
</div>
</div>

<style>
  /* FAQ list */
  .faq-list { border-top: 1px solid #ddd8ce; }
  .faq-item { border-bottom: 1px solid #ddd8ce; }
  .faq-question {
    width: 100%;
    text-align: left;
    padding: 1rem 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 0.85rem;
    font-weight: 600;
    color: #1a2744;
    background: transparent;
    border: none;
    cursor: pointer;
    gap: 1rem;
    outline: none !important;
  }
  .faq-question:hover { color: #d4920c; }
  .faq-open .faq-question { color: #d4920c; }
  .faq-chevron {
    width: 1rem;
    height: 1rem;
    color: #5a7090;
    transition: transform 0.2s;
    flex-shrink: 0;
  }
  .faq-open .faq-chevron { color: #d4920c; }
  .faq-answer {
    padding: 0 0 1rem;
    font-size: 0.83rem;
    color: #1e3050;
    line-height: 1.7;
  }

  /* Flow diagrams */
  .flow-card {
    border: 1px solid #ddd8ce;
    border-radius: 4px;
    overflow: hidden;
  }
  .flow-card-header {
    padding: 0.65rem 1rem;
    border-bottom: 1px solid #ddd8ce;
    background: #f5f2eb;
  }
  .flow-card-title {
    font-size: 0.78rem;
    font-weight: 700;
    color: #1a2744;
    letter-spacing: 0.01em;
  }

  /* Diagrams render at their native SVG size and scale down via
     max-width: 100% on desktop. On mobile the source SVG is wider
     than 390px and was being squashed — nodes shrank past readable
     size. Wrap with horizontal scroll under 600px so the operator
     can pan instead of squinting; min-width keeps the SVG at a
     usable scale, the parent's overflow-x-auto provides the pan. */
  :global(.mermaid-container) {
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  :global(.mermaid-container svg) { max-width: 100%; height: auto; }
  :global(.mermaid-container svg .node rect) { rx: 8px; ry: 8px; }
  :global(.mermaid-container svg .node polygon) { rx: 6px; ry: 6px; }
  @media (max-width: 600px) {
    :global(.mermaid-container svg) {
      max-width: none;
      min-width: 620px;
    }
  }
</style>
