<script>
  import { onMount, onDestroy } from 'svelte';

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

  let open = $state(-1);
  let _zoomedDiagram = $state(/** @type {string | null} */ (null));
  // Zoom level — 1.0 = natural, 0.25 = quarter, 4.0 = 4x. Operator
  // can zoom out to see the whole flow OR zoom in to read labels.
  let _zoomLevel = $state(1);

  function _openZoom(svgHtml) {
    _zoomedDiagram = svgHtml;
    _zoomLevel = 1;
    document.body.style.overflow = 'hidden';
  }
  function _closeZoom() {
    _zoomedDiagram = null;
    document.body.style.overflow = '';
  }
  function _zoomIn()  { _zoomLevel = Math.min(4,    _zoomLevel + 0.25); }
  function _zoomOut() { _zoomLevel = Math.max(0.25, _zoomLevel - 0.25); }
  function _zoomFit() { _zoomLevel = 1; }

  onDestroy(() => { document.body.style.overflow = ''; });

  onMount(async () => {
    /** @type {any} */
    const win = window;
    // Dynamically load mermaid — no npm package needed, CDN via script tag.
    // SRI hash computed from mermaid@11.15.0 (jsDelivr canonical build).
    // crossorigin="anonymous" is required for SRI enforcement.
    if (!win.mermaid) {
      const loaded = await new Promise((resolve) => {
        const s = document.createElement('script');
        s.src = 'https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js';
        s.crossOrigin = 'anonymous';
        s.integrity = 'sha384-yQ4mmBBT+vhTAwjFH0toJXNYJ6O4usWnt6EPIdWwrRvx2V/n5lXuDZQwQFeSFydF';
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


<svelte:window onkeydown={(e) => { if (_zoomedDiagram && e.key === 'Escape') _closeZoom(); }} />

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
        <div id="mermaid-{d.id}" class="mermaid-container flex justify-center"
             role="button"
             tabindex="0"
             aria-label="Click to enlarge diagram"
             style="cursor: zoom-in"
             onclick={(e) => { if (e.currentTarget.querySelector('svg')) _openZoom(e.currentTarget.innerHTML); }}
             onkeydown={(e) => { if ((e.key === 'Enter' || e.key === ' ') && e.currentTarget.querySelector('svg')) _openZoom(e.currentTarget.innerHTML); }}>
          <div class="text-xs text-muted animate-pulse py-8">Loading diagram…</div>
        </div>
        <p class="faq-zoom-hint">Tap to enlarge</p>
      </div>
    </div>
  {/each}
</div>
</div>

{#if _zoomedDiagram}
  <!-- Operator (initial): "the x label does not make sense for zooming
       mermaid flow diagrams. i should be able zoomable on mobile using
       pinch." × button removed.
       Operator (follow-up): "single click on enlarged flow diagram
       should reverse it back to old state. the scroll to see the left
       most box is not working." Click anywhere (overlay OR panel OR
       SVG) closes the lightbox; pinch-zoom still works because it's a
       multi-touch gesture, not a click. Panel switched from
       flex-centered to natural block layout so leftmost overflow can
       be scrolled to via the native scrollbar. -->
  <!-- svelte-ignore a11y_interactive_supports_focus a11y_click_events_have_key_events a11y_no_static_element_interactions -->
  <div class="faq-zoom-overlay"
       role="dialog"
       aria-modal="true"
       aria-label="Diagram zoom — tap to close, use zoom buttons or pinch"
       tabindex="-1"
       onclick={_closeZoom}
       onkeydown={(e) => { if (e.key === 'Escape') _closeZoom(); }}>
    <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
    <div class="faq-zoom-panel" role="document">
      <!-- Zoom toolbar — operator: "zoom in and out is not available
           to make the complete process flow visible within the
           container." Three buttons: −, fit (1:1), +. Buttons
           stopPropagation so they don't bubble to the overlay-close
           handler. Pinned to top-right via `position: sticky` so they
           stay visible as the operator scrolls a zoomed diagram. -->
      <div class="faq-zoom-toolbar">
        <button type="button" class="faq-zoom-btn" aria-label="Zoom out"
                title="Zoom out"
                onclick={(e) => { e.stopPropagation(); _zoomOut(); }}>−</button>
        <button type="button" class="faq-zoom-btn faq-zoom-fit"
                aria-label="Reset zoom" title="Reset zoom"
                onclick={(e) => { e.stopPropagation(); _zoomFit(); }}>
          {Math.round(_zoomLevel * 100)}%
        </button>
        <button type="button" class="faq-zoom-btn" aria-label="Zoom in"
                title="Zoom in"
                onclick={(e) => { e.stopPropagation(); _zoomIn(); }}>+</button>
      </div>
      <!-- `zoom` (not `transform: scale`) because zoom rescales the
           element's LAYOUT box, including the scroll-area size.
           `transform: scale` was visually enlarging but leaving the
           scrollable region at 1x, so the right-edge boxes (e.g.
           "Distribute Threshold Return First") couldn't be reached
           via horizontal scroll. `zoom` is supported by every
           Chromium browser, Safari, and Firefox 126+. -->
      <div class="faq-zoom-svg" style="zoom: {_zoomLevel};">{@html _zoomedDiagram}</div>
    </div>
  </div>
{/if}

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
    outline: none;
  }
  .faq-question:focus-visible { outline: 2px solid #d4920c; outline-offset: 2px; }
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

  /* Zoom hint below each diagram */
  .faq-zoom-hint {
    text-align: center;
    font-size: 0.68rem;
    color: #8a9ab0;
    margin-top: 0.35rem;
    pointer-events: none;
  }

  /* Lightbox overlay — tap outside to close (no × button per operator). */
  .faq-zoom-overlay {
    position: fixed; inset: 0; z-index: 1000;
    background: rgba(0,0,0,0.85);
    display: flex; align-items: center; justify-content: center;
    padding: 0.5rem;
    cursor: zoom-out;
  }
  /* Panel sized to the viewport; scrolls in both axes once the SVG
     grows past it via zoom. `touch-action: pan-x pan-y pinch-zoom`
     keeps native pinch-zoom on touch devices live inside the panel. */
  .faq-zoom-panel {
    position: relative;
    width: 95vw;
    height: 95vh;
    background: #fdfcf7;
    border-radius: 8px;
    padding: 0.5rem;
    overflow: auto;
    cursor: zoom-out;
    touch-action: pan-x pan-y pinch-zoom;
    -webkit-overflow-scrolling: touch;
  }
  /* Toolbar sticky in the top-right corner. Stays visible while the
     panel scrolls. Z-index above the SVG so clicks land on buttons. */
  .faq-zoom-toolbar {
    position: sticky;
    top: 0;
    margin-left: auto;
    z-index: 10;
    display: inline-flex;
    gap: 0.25rem;
    padding: 0.25rem;
    float: right;
  }
  .faq-zoom-btn {
    min-width: 2rem;
    height: 2rem;
    padding: 0 0.5rem;
    background: rgba(253, 252, 247, 0.92);
    border: 1px solid rgba(31, 41, 55, 0.25);
    border-radius: 4px;
    color: #1f2937;
    font-family: ui-monospace, monospace;
    font-size: 0.85rem;
    font-weight: 700;
    cursor: pointer;
    line-height: 1;
  }
  .faq-zoom-btn:hover { background: #fff; border-color: rgba(31, 41, 55, 0.5); }
  .faq-zoom-fit { font-size: 0.65rem; }
  /* SVG wrapper — natural block flow at top-left so horizontal scroll
     origin is content-left. CSS `zoom` (set via inline style) scales
     BOTH visual size AND layout box, so the panel's `overflow: auto`
     scroll region naturally extends to cover the zoomed content. No
     `transform-origin` because no transform is used. */
  .faq-zoom-svg {
    display: block;
    width: max-content;
    touch-action: pan-x pan-y pinch-zoom;
  }
  .faq-zoom-svg :global(svg) {
    width: auto !important;
    height: auto !important;
    max-width: none;
    display: block;
    touch-action: pan-x pan-y pinch-zoom;
  }
</style>
