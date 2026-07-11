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
  import ModalShell from '$lib/ModalShell.svelte';

  /**
   * @typedef {object} Props
   * @property {() => void} onClose
   */
  /** @type {Props} */
  let { onClose } = $props();

  // Scroll lock — ModalShell owns Esc + backdrop; we just lock body scroll.
  $effect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  });

  const HIGHLIGHTS = [
    { tag: 'Trading infra',  text: 'Multi-broker basket dispatch (Kite + Dhan + Groww) with per-account IP binding, parallel asyncio.gather order placement, and emulated OCO across brokers that lack native support.' },
    { tag: 'Quant math',     text: 'σ-driven derivatives analytics — Black-Scholes Greeks, lognormal POP, multi-leg payoff curves, expected value via trapezoidal integration, proxy-hedge β regression on 60-day daily-returns.' },
    { tag: 'Real-time',      text: 'KiteTicker WebSocket pipeline fans out per-symbol LTPs to SSE clients with auto-failover watchdog. Zero REST calls during market hours for sparklines + Pulse cells.' },
    { tag: 'Agent DSL',
      text: 'Custom declarative grammar (domain-specific language) for trading rules — DB-backed tokens (metric / scope / operator / action), composable via $ref fragments. Rules compile into a condition tree the engine walks every 5s cycle; no code changes needed to add a new rule or a new metric primitive.',
      links: [
        { label: 'Live token editor', href: '/admin/tokens' },
        { label: 'Full DSL guide',    href: 'https://github.com/RamanaAmbore/algo-trader/blob/main/AGENTS_GUIDE.md' },
      ],
    },
    { tag: 'AI workflow',    text: 'MCP server bridges Claude Code to live broker data + research storage. 25 tools, confirm-token gate on every mutating write, full audit trail in McpAudit.' },
    { tag: 'Production',     text: 'PostgreSQL 17 async via SQLAlchemy 2 + asyncpg · Litestar API with msgspec.Struct · SvelteKit 5 frontend · single VPS, single uvicorn worker, KiteTicker IPv6 source binding for multi-account Kite auth.' },
  ];

  // Operator-supplied contact channels. Defaults work today; the
  // operator can swap individual links via a future settings UI
  // without touching this component.
  const CONTACT = {
    github:    'https://github.com/RamanaAmbore/algo-trader',
    linkedin:  'https://www.linkedin.com/in/ambore/',
    email:     'mailto:ramboquant@gmail.com?subject=RamboQuant%20-%20Engineering%20Conversation',
    resume:    'https://ramanaambore.me/resume.pdf',
    resumeTxt: 'https://ramanaambore.me/resume.txt',
    portfolio: 'https://ramanaambore.me',
  };
</script>

<ModalShell open={true} {onClose} zIndex={100} clickOutside={true}>
  <div class="hm-modal algo-modal" role="dialog" aria-modal="true" aria-labelledby="hm-title" tabindex="-1"
       onclick={(e) => e.stopPropagation()}
       onkeydown={(e) => e.stopPropagation()}>
    <button type="button" class="hm-close" aria-label="Close" onclick={onClose}>×</button>

    <h2 id="hm-title" class="hm-title">
      RamboQuant
      <span class="hm-sub">— built by Ramana Ambore</span>
    </h2>
    <p class="hm-tagline">
      Production multi-broker algo platform serving live trading on Indian
      markets.
    </p>

    <div class="hm-highlights">
      {#each HIGHLIGHTS as h}
        <div class="hm-row">
          <span class="hm-tag">{h.tag}</span>
          <span class="hm-text">
            {h.text}
            {#if h.links?.length}
              <span class="hm-inline-links">
                {#each h.links as l, i}
                  {#if i > 0}<span class="hm-link-sep">·</span>{/if}
                  <a class="hm-inline-link"
                     href={l.href}
                     target={l.href.startsWith('http') ? '_blank' : '_self'}
                     rel={l.href.startsWith('http') ? 'noopener' : ''}
                     onclick={(e) => {
                       // Internal links land on the algo page underneath
                       // the modal — close the modal first so the
                       // navigation is visible. External links open in
                       // a new tab and leave the modal alone.
                       if (!l.href.startsWith('http')) onClose();
                     }}>{l.label} ↗</a>
                {/each}
              </span>
            {/if}
          </span>
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
        Resume (PDF)
      </a>
      <a class="hm-btn" href={CONTACT.resumeTxt} target="_blank" rel="noopener">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" aria-hidden="true">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <path d="M14 2v6h6"/>
          <path d="M9 13h6"/>
          <path d="M9 17h6"/>
        </svg>
        Resume (TXT)
      </a>
    </div>
  </div>
</ModalShell>

<style>
  .hm-modal {
    /* Composes .algo-modal chrome. Overrides:
       - overflow: auto (algo-modal sets hidden) — this dialog scrolls
         its own body when the highlight list overflows viewport.
       - background: elevated-dark gradient — intentional deeper contrast
         for the hire-me splash so it reads as a distinct spotlight
         moment vs. content-rich modals.
       - font-family: sans body (numeric monospace default from
         .algo-modal is wrong for prose). */
    position: relative;
    max-width: 38rem;
    width: 100%;
    max-height: 90vh;
    overflow-y: auto;
    background: linear-gradient(180deg, #0f172a 0%, #131c33 100%);
    border: 1px solid var(--algo-amber-border-soft);
    padding: 1.5rem 1.6rem 1.3rem;
    color: var(--algo-slate);
    font-family: var(--font-text);
  }
  .hm-close {
    position: absolute; top: 0.6rem; right: 0.8rem;
    width: 1.6rem; height: 1.6rem;
    border: none; background: transparent;
    color: var(--algo-dim); font-size: 1.2rem; cursor: pointer; line-height: 1;
    border-radius: 3px;
  }
  .hm-close:hover { background: rgba(255,255,255,0.08); color: var(--algo-amber); }

  .hm-title {
    margin: 0 0 0.2rem;
    font-size: 1.15rem;
    font-weight: 800;
    letter-spacing: 0.02em;
    color: var(--algo-amber);
  }
  .hm-sub {
    font-size: var(--fs-lg); font-weight: 500;
    color: var(--algo-dim); margin-left: 0.4rem;
  }
  .hm-tagline {
    margin: 0 0 1rem;
    font-size: var(--fs-lg);
    line-height: 1.45;
    color: var(--algo-muted);
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
    font-size: var(--fs-xs); font-weight: 800;
    letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--algo-amber);
    font-family: var(--font-numeric);
    padding-top: 0.1rem;
  }
  .hm-text {
    font-size: var(--fs-lg); line-height: 1.45;
    color: var(--algo-slate);
  }

  /* Inline reference links — for rows that point at a live editor
     surface or an external doc (Agent DSL row today, can grow to
     other rows). Renders on the line below the body text. */
  .hm-inline-links {
    display: inline-flex; flex-wrap: wrap; gap: 0.4rem;
    margin-top: 0.25rem;
    align-items: baseline;
  }
  .hm-inline-link {
    font-size: var(--fs-sm);
    font-weight: 700;
    letter-spacing: 0.03em;
    color: var(--algo-cyan-text);
    text-decoration: none;
    border-bottom: 1px dashed var(--algo-cyan-border-soft);
    padding-bottom: 1px;
    font-family: var(--font-numeric);
  }
  .hm-inline-link:hover {
    color: var(--algo-sky-text);
    border-bottom-color: var(--algo-cyan-border);
  }
  .hm-link-sep {
    color: rgba(126, 151, 184, 0.45);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
  }

  .hm-cta {
    display: flex; flex-wrap: wrap; gap: 0.5rem;
    border-top: 1px solid rgba(126, 151, 184, 0.18);
    padding-top: 0.9rem;
  }
  .hm-btn {
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.4rem 0.8rem;
    background: var(--algo-cyan-bg);
    border: 1px solid var(--algo-cyan-border-soft);
    border-radius: 4px;
    color: var(--algo-cyan-text);
    font-size: var(--fs-lg);
    font-weight: 700;
    letter-spacing: 0.03em;
    text-decoration: none;
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .hm-btn:hover {
    background: var(--algo-cyan-bg-strong);
    border-color: var(--algo-cyan-border);
    color: var(--algo-sky-text);
  }
  .hm-btn-primary {
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border);
    color: var(--algo-amber);
  }
  .hm-btn-primary:hover {
    background: var(--algo-amber-bg-strong);
    border-color: rgba(251, 191, 36, 0.85);
    color: var(--algo-amber-text);
  }

  @media (max-width: 540px) {
    .hm-row { grid-template-columns: 1fr; gap: 0.2rem; }
    .hm-tag { padding-top: 0; }
  }
</style>
