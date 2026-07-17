<!--
  TourModal — 5-step guided walk-through of the RamboQuant platform.
  Designed for recruiters / hiring managers landing cold on /showcase.
  Each step covers one architectural highlight + a deep link to the
  live surface so the visitor can poke at it. Auto-advances after 12s
  per step (Operator can pause / skip / close at any point).

  Total tour length at default cadence: ~60 seconds. Skip drops the
  visitor wherever they currently were; close exits to /showcase.

  Industry analogue: Vercel / Linear / Notion onboarding tours.
-->
<script>
  import { goto } from '$app/navigation';
  import ModalShell from '$lib/ModalShell.svelte';

  /**
   * @typedef {object} Props
   * @property {() => void} onClose
   */
  /** @type {Props} */
  let { onClose } = $props();

  /** @typedef {{ title: string, body: string, link: { label: string, href: string } | null, tag: string }} Step */

  /** @type {Step[]} */
  const STEPS = [
    {
      tag:   'Broker isolation',
      title: 'Two-process broker layer',
      body:  'Broker sessions (Kite + Dhan + Groww) live in a separate Litestar service over a Unix domain socket; the main API restarts on every backend push WITHOUT re-authenticating any broker. Ticks cross processes via shared memory (/dev/shm) at byte-read latency. This is exactly the connectivity / strategy separation institutional shops use — same pattern as IB Gateway, Bloomberg BPIPE, internal OMS at quant funds.',
      link:  { label: 'View broker config', href: '/admin/brokers' },
    },
    {
      tag:   'Quant math',
      title: 'σ-driven derivatives analytics',
      body:  'Black-Scholes Greeks, lognormal POP via numerical integration, multi-leg payoff curves with EV + R:R, proxy-hedge β regression on 60-day daily returns. All hand-rolled — no scipy / quantlib dependency on the hot path.',
      link:  { label: 'Open derivatives workspace', href: '/admin/derivatives' },
    },
    {
      tag:   'Operator DSL',
      title: 'Declarative agent grammar',
      body:  'Risk + automation rules are condition trees compiled from DB-backed tokens (metric / scope / operator / action). Operators add a new rule from the UI without a code deploy. Engine walks the tree every 5s cycle and dispatches matching agents. Alerts route to Telegram, ntfy push, email, WebSocket, or log — per-agent opt-in.',
      link:  { label: 'Browse agent library', href: '/automation' },
    },
    {
      tag:   'Sim infra',
      title: 'Production-grade simulator',
      body:  'Stress-test scenarios, market-state presets, σ-driven option re-pricing, per-agent test synthesis. Same engine code runs sim → paper → shadow → live — five execution modes with one tick loop, mode resolved per agent at dispatch time.',
      link:  { label: 'Try the simulator', href: '/admin/execution?mode=sim' },
    },
    {
      tag:   'AI workflow',
      title: 'MCP server + Lab page',
      body:  'Claude Code talks to live broker state through a 26-tool MCP server. Confirm-token gate on every mutating call. Every tool invocation logs to a tamper-evident audit table — same one a SEBI auditor would query.',
      link:  { label: 'See the Lab', href: '/admin/research' },
    },
  ];

  // Step state. `_step` is 0-indexed.
  let _step      = $state(0);
  let _paused    = $state(false);
  const STEP_MS  = 12000;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let _timer = null;
  /** @type {Step} */
  const current = $derived(STEPS[_step]);
  const isLast  = $derived(_step === STEPS.length - 1);

  function schedule() {
    if (_paused) return;
    if (_timer) clearTimeout(_timer);
    _timer = setTimeout(() => {
      if (isLast) onClose();
      else _step += 1;
    }, STEP_MS);
  }
  function next() {
    if (isLast) { onClose(); return; }
    _step += 1;
    schedule();
  }
  function prev() {
    if (_step <= 0) return;
    _step -= 1;
    schedule();
  }
  function pause() {
    _paused = !_paused;
    if (_paused && _timer) { clearTimeout(_timer); _timer = null; }
    else schedule();
  }
  function openLink(/** @type {string} */ href) {
    // Pause auto-advance + close the modal so the visitor lands on
    // the destination with focus. The destination page won't have
    // tour chrome — recruiter explores freely from there.
    if (_timer) clearTimeout(_timer);
    onClose();
    goto(href);
  }
  function onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape')     onClose();
    if (e.key === 'ArrowRight') next();
    if (e.key === 'ArrowLeft')  prev();
    if (e.key === ' ')          { pause(); e.preventDefault(); }
  }

  $effect(() => {
    document.addEventListener('keydown', onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    schedule();
    return () => {
      document.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
      if (_timer) clearTimeout(_timer);
    };
  });

  // Progress bar fill — restarts on every step change. Pure CSS via
  // a key on the wrapper so the animation re-runs from 0% each step.
  // No JS RAF loop needed.
</script>

<ModalShell open={true} {onClose} zIndex={110} clickOutside={true} ariaLabel="Product tour">
  <div class="tour-modal algo-modal" role="dialog" aria-modal="true" aria-labelledby="tour-title" tabindex="-1"
       onclick={(e) => e.stopPropagation()}
       onkeydown={(e) => e.stopPropagation()}>

    <!-- Progress strip. The wrapping {#key} restarts the CSS animation
         on every step change by re-mounting the inner bar — pure CSS,
         no JS RAF loop. -->
    <div class="tour-progress" aria-hidden="true">
      {#key _step}
        <div class="tour-progress-bar"
             class:tour-progress-paused={_paused}
             style="--ms: {STEP_MS}ms"></div>
      {/key}
    </div>

    <button type="button" class="tour-close" aria-label="Close tour" onclick={onClose}>×</button>

    <div class="tour-step-meta">
      <span class="tour-tag">{current.tag}</span>
      <span class="tour-step-count">{_step + 1} / {STEPS.length}</span>
    </div>

    <h2 id="tour-title" class="tour-title">{current.title}</h2>
    <p class="tour-body">{current.body}</p>

    {#if current.link}
      <button type="button" class="tour-link" onclick={() => openLink(current.link.href)}>
        {current.link.label} →
      </button>
    {/if}

    <div class="tour-controls">
      <button type="button" class="tour-ctrl-btn" onclick={prev} disabled={_step === 0}>← Prev</button>
      <button type="button" class="tour-ctrl-btn tour-ctrl-pause" onclick={pause}>
        {_paused ? '▶ Resume' : '❚❚ Pause'}
      </button>
      <button type="button" class="tour-ctrl-btn tour-ctrl-skip" onclick={onClose}>Skip</button>
      <button type="button" class="tour-ctrl-btn tour-ctrl-next" onclick={next}>
        {isLast ? 'Finish' : 'Next →'}
      </button>
    </div>
  </div>
</ModalShell>

<style>
  .tour-modal {
    /* Composes .algo-modal chrome. Overrides:
       - background: elevated-dark gradient (intentional splash-mode
         contrast for onboarding surfaces).
       - border: full amber (not the halo default) — tour needs to
         demand focus.
       - font-family: sans body for prose readability. */
    position: relative;
    max-width: 36rem;
    width: 100%;
    margin: 1rem;
    background: linear-gradient(180deg, #0f172a 0%, #131c33 100%);
    border: 1px solid var(--algo-amber-border);
    padding: 0 1.6rem 1.2rem;
    color: var(--algo-slate);
    font-family: var(--font-text);
  }

  /* Progress strip — top edge, fills over STEP_MS, key-restart on
     each step. Paused state freezes at current width. */
  .tour-progress {
    position: absolute; left: 0; right: 0; top: 0;
    height: 3px;
    background: rgba(126, 151, 184, 0.18);
  }
  .tour-progress-bar {
    height: 100%;
    background: linear-gradient(90deg, #fbbf24 0%, #fcd34d 100%);
    width: 0%;
    animation: tour-fill var(--ms) linear forwards;
  }
  .tour-progress-paused {
    animation-play-state: paused;
  }
  @keyframes tour-fill {
    from { width: 0%; }
    to   { width: 100%; }
  }
  @media (prefers-reduced-motion: reduce) {
    /* Jump the bar to full-width immediately — the auto-advance timer
       still fires; user just doesn't see the progress fill sweep. */
    .tour-progress-bar { animation: none; width: 100%; }
  }

  .tour-close {
    position: absolute; top: 0.55rem; right: 0.8rem;
    width: 1.6rem; height: 1.6rem;
    border: none; background: transparent;
    color: var(--algo-dim); font-size: 1.3rem; cursor: pointer; line-height: 1;
    border-radius: 3px;
  }
  .tour-close:hover { background: rgba(255,255,255,0.10); color: var(--algo-amber); }

  .tour-step-meta {
    display: flex; align-items: center; justify-content: space-between;
    margin: 1.2rem 0 0.45rem;
    font-size: var(--fs-xs);
    font-family: var(--font-numeric);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .tour-tag {
    color: var(--algo-amber);
    font-weight: 800;
    padding: 0.15rem 0.5rem;
    background: var(--algo-amber-bg);
    border: 1px solid var(--algo-amber-border-soft);
    border-radius: 3px;
  }
  .tour-step-count {
    color: var(--algo-muted);
    font-weight: 700;
  }

  .tour-title {
    margin: 0 0 0.55rem;
    font-size: 1.05rem;
    font-weight: 800;
    color: var(--algo-amber);
    letter-spacing: 0.01em;
  }
  .tour-body {
    margin: 0 0 1rem;
    font-size: var(--fs-lg);
    line-height: 1.55;
    color: var(--algo-slate);
  }

  .tour-link {
    display: inline-flex; align-items: center;
    margin-bottom: 1.1rem;
    padding: 0.4rem 0.9rem;
    background: var(--algo-cyan-bg-soft);
    border: 1px solid var(--algo-cyan-border-soft);
    border-radius: 4px;
    color: var(--algo-cyan-text);
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.03em;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    font-family: inherit;
  }
  .tour-link:hover {
    background: var(--algo-cyan-bg-strong);
    border-color: var(--algo-cyan-border);
    color: var(--algo-sky-text);
  }

  .tour-controls {
    display: flex; gap: 0.4rem; flex-wrap: wrap;
    padding-top: 0.8rem;
    border-top: 1px solid rgba(126, 151, 184, 0.18);
  }
  .tour-ctrl-btn {
    padding: 0.3rem 0.65rem;
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.35);
    border-radius: 3px;
    color: var(--algo-slate);
    font-size: var(--fs-md);
    font-weight: 700;
    letter-spacing: 0.02em;
    cursor: pointer;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
    font-family: inherit;
  }
  .tour-ctrl-btn:hover:not(:disabled) {
    background: rgba(126, 151, 184, 0.22);
    color: var(--algo-amber);
    border-color: var(--algo-amber-border-soft);
  }
  .tour-ctrl-btn:disabled { opacity: 0.35; cursor: not-allowed; }
  .tour-ctrl-pause { font-family: var(--font-numeric); }
  .tour-ctrl-skip  { margin-left: auto; }
  .tour-ctrl-next  {
    background: var(--algo-amber-bg);
    border-color: var(--algo-amber-border);
    color: var(--algo-amber);
  }
  .tour-ctrl-next:hover:not(:disabled) {
    background: var(--algo-amber-bg-strong);
    border-color: rgba(251, 191, 36, 0.85);
    color: var(--algo-amber-text);
  }

  @media (max-width: 540px) {
    .tour-modal { padding: 0 1rem 0.9rem; }
    .tour-title { font-size: var(--fs-xl); }
    .tour-body  { font-size: var(--fs-md); }
  }
</style>
