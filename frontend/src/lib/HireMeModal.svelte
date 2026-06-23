<!--
  HireMeModal — recruiter-facing modal pinned to the demo navbar.

  Shown only when the current session is demo (anonymous prod visitor).
  Designed to land the engineering pitch in 30-45 seconds: short
  architecture-highlights bullet list + contact CTAs (GitHub, LinkedIn,
  email, resume PDF). Trigger lives in `+layout.svelte` next to the
  "Sign In" button.

  Update the contact links + resume URL once the operator confirms
  preferred channels (currently placeholder).
-->
<script>
  /**
   * @typedef {object} Props
   * @property {() => void} onClose
   */
  /** @type {Props} */
  let { onClose } = $props();

  // ESC + overlay click to close — same UX vocabulary as ChartModal /
  // SymbolPanel / every other modal in the app.
  function onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') onClose();
  }
  $effect(() => {
    document.addEventListener('keydown', onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prev;
    };
  });

  const HIGHLIGHTS = [
    { tag: 'Trading infra',  text: 'Multi-broker basket dispatch (Kite + Dhan + Groww) with per-account IP binding, parallel asyncio.gather order placement, and emulated OCO across brokers that lack native support.' },
    { tag: 'Quant math',     text: 'σ-driven derivatives analytics — Black-Scholes Greeks, lognormal POP, multi-leg payoff curves, expected value via trapezoidal integration, proxy-hedge β regression on 60-day daily-returns.' },
    { tag: 'Real-time',      text: 'KiteTicker WebSocket pipeline fans out per-symbol LTPs to SSE clients with auto-failover watchdog. Zero REST calls during market hours for sparklines + Pulse cells.' },
    { tag: 'Operator DSL',   text: 'Declarative agent grammar — DB-backed tokens (metric/scope/operator/action) editable from /agents/tokens. Rules compile into a condition tree the engine walks every 5s cycle.' },
    { tag: 'AI workflow',    text: 'MCP server bridges Claude Code to live broker data + research storage. 26 tools, confirm-token gate on every mutating write, full audit trail in McpAudit.' },
    { tag: 'Production',     text: 'PostgreSQL 17 async via SQLAlchemy 2 + asyncpg · Litestar API with msgspec.Struct · SvelteKit 5 frontend · single VPS, single uvicorn worker, KiteTicker IPv6 source binding for multi-account Kite auth.' },
  ];

  // Operator-supplied contact channels. Defaults work today; the
  // operator can swap individual links via a future settings UI
  // without touching this component.
  const CONTACT = {
    github:   'https://github.com/RamanaAmbore/algo-trader',
    linkedin: 'https://www.linkedin.com/in/ramanambore/',
    email:    'mailto:ramboquant@gmail.com?subject=RamboQuant%20-%20Engineering%20Conversation',
    resume:   '/docs/ramana-ambore-resume.pdf',     // placeholder — drop the PDF at this path
    portfolio:'https://ramanaambore.me',
  };
</script>

<div class="hm-overlay" onclick={onClose}
     onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') onClose(); }}
     role="presentation">
  <div class="hm-modal" role="dialog" aria-modal="true" aria-labelledby="hm-title" tabindex="-1"
       onclick={(e) => e.stopPropagation()}
       onkeydown={(e) => e.stopPropagation()}>
    <button type="button" class="hm-close" aria-label="Close" onclick={onClose}>×</button>

    <h2 id="hm-title" class="hm-title">
      RamboQuant
      <span class="hm-sub">— built by Ramana Ambore</span>
    </h2>
    <p class="hm-tagline">
      Production multi-broker algo platform serving live trading on Indian
      markets. What follows is the part a hiring manager probably cares about.
    </p>

    <div class="hm-highlights">
      {#each HIGHLIGHTS as h}
        <div class="hm-row">
          <span class="hm-tag">{h.tag}</span>
          <span class="hm-text">{h.text}</span>
        </div>
      {/each}
    </div>

    <div class="hm-cta">
      <a class="hm-btn hm-btn-primary" href={CONTACT.email}>
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true">
          <path d="M4 4h16v16H4z"/><path d="m4 4 8 8 8-8"/>
        </svg>
        Email
      </a>
      <a class="hm-btn" href={CONTACT.github} target="_blank" rel="noopener">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.91.58.1.79-.25.79-.55v-1.94c-3.2.7-3.87-1.54-3.87-1.54-.52-1.33-1.27-1.69-1.27-1.69-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.02 1.75 2.69 1.24 3.34.95.1-.74.4-1.24.72-1.53-2.55-.29-5.24-1.28-5.24-5.7 0-1.26.45-2.29 1.18-3.1-.12-.29-.51-1.46.11-3.04 0 0 .97-.31 3.17 1.18.92-.26 1.9-.39 2.88-.39.98 0 1.96.13 2.88.39 2.2-1.49 3.17-1.18 3.17-1.18.63 1.58.23 2.75.12 3.04.73.81 1.18 1.84 1.18 3.1 0 4.43-2.69 5.4-5.25 5.69.41.36.78 1.05.78 2.12v3.14c0 .3.21.66.8.55 4.56-1.53 7.85-5.83 7.85-10.91C23.5 5.65 18.35.5 12 .5Z"/>
        </svg>
        GitHub
      </a>
      <a class="hm-btn" href={CONTACT.linkedin} target="_blank" rel="noopener">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="M19 0H5C2.24 0 0 2.24 0 5v14c0 2.76 2.24 5 5 5h14c2.76 0 5-2.24 5-5V5c0-2.76-2.24-5-5-5ZM8 19H5V8h3v11ZM6.5 6.73c-.97 0-1.75-.79-1.75-1.76 0-.97.78-1.75 1.75-1.75s1.75.79 1.75 1.75-.78 1.76-1.75 1.76ZM20 19h-3v-5.6c0-3.37-4-3.11-4 0V19h-3V8h3v1.77c1.4-2.58 7-2.77 7 2.46V19Z"/>
        </svg>
        LinkedIn
      </a>
      <a class="hm-btn" href={CONTACT.portfolio} target="_blank" rel="noopener">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true">
          <circle cx="12" cy="12" r="10"/>
          <path d="M2 12h20"/>
          <path d="M12 2a15 15 0 0 1 0 20"/>
          <path d="M12 2a15 15 0 0 0 0 20"/>
        </svg>
        Portfolio
      </a>
      <a class="hm-btn" href={CONTACT.resume} target="_blank" rel="noopener">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <path d="M14 2v6h6"/>
        </svg>
        Resume
      </a>
    </div>
  </div>
</div>

<style>
  .hm-overlay {
    position: fixed; inset: 0;
    background: rgba(8, 12, 20, 0.75);
    backdrop-filter: blur(3px);
    display: flex; align-items: center; justify-content: center;
    z-index: 100;
    padding: 1rem;
  }
  .hm-modal {
    position: relative;
    max-width: 38rem;
    width: 100%;
    max-height: 90vh;
    overflow-y: auto;
    background: linear-gradient(180deg, #0f172a 0%, #131c33 100%);
    border: 1px solid rgba(251, 191, 36, 0.40);
    border-radius: 6px;
    padding: 1.5rem 1.6rem 1.3rem;
    color: #c8d8f0;
    font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
  }
  .hm-close {
    position: absolute; top: 0.6rem; right: 0.8rem;
    width: 1.6rem; height: 1.6rem;
    border: none; background: transparent;
    color: #94a3b8; font-size: 1.2rem; cursor: pointer; line-height: 1;
    border-radius: 3px;
  }
  .hm-close:hover { background: rgba(255,255,255,0.08); color: #fbbf24; }

  .hm-title {
    margin: 0 0 0.2rem;
    font-size: 1.15rem;
    font-weight: 800;
    letter-spacing: 0.02em;
    color: #fbbf24;
  }
  .hm-sub {
    font-size: 0.7rem; font-weight: 500;
    color: #94a3b8; margin-left: 0.4rem;
  }
  .hm-tagline {
    margin: 0 0 1rem;
    font-size: 0.72rem;
    line-height: 1.45;
    color: #a3b9d0;
  }

  .hm-highlights {
    display: flex; flex-direction: column; gap: 0.55rem;
    margin-bottom: 1.1rem;
  }
  .hm-row {
    display: grid;
    grid-template-columns: 6.5rem 1fr;
    gap: 0.6rem;
    align-items: start;
    padding: 0.5rem 0.6rem;
    background: rgba(34, 47, 75, 0.45);
    border-left: 2px solid rgba(251, 191, 36, 0.55);
    border-radius: 3px;
  }
  .hm-tag {
    font-size: 0.55rem; font-weight: 800;
    letter-spacing: 0.06em; text-transform: uppercase;
    color: #fbbf24;
    font-family: ui-monospace, monospace;
    padding-top: 0.1rem;
  }
  .hm-text {
    font-size: 0.7rem; line-height: 1.45;
    color: #c8d8f0;
  }

  .hm-cta {
    display: flex; flex-wrap: wrap; gap: 0.5rem;
    border-top: 1px solid rgba(126, 151, 184, 0.18);
    padding-top: 0.9rem;
  }
  .hm-btn {
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.4rem 0.8rem;
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid rgba(34, 211, 238, 0.45);
    border-radius: 4px;
    color: #67e8f9;
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.03em;
    text-decoration: none;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .hm-btn:hover {
    background: rgba(34, 211, 238, 0.20);
    border-color: rgba(34, 211, 238, 0.75);
    color: #a5f3fc;
  }
  .hm-btn-primary {
    background: rgba(251, 191, 36, 0.16);
    border-color: rgba(251, 191, 36, 0.55);
    color: #fbbf24;
  }
  .hm-btn-primary:hover {
    background: rgba(251, 191, 36, 0.30);
    border-color: rgba(251, 191, 36, 0.85);
    color: #fcd34d;
  }

  @media (max-width: 540px) {
    .hm-row { grid-template-columns: 1fr; gap: 0.2rem; }
    .hm-tag { padding-top: 0; }
  }
</style>
