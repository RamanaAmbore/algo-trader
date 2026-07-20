<!--
  ShortcutCheatsheet — modal overlay listing every wired keyboard
  shortcut. Triggered by `?` from anywhere in the algo surface.
  Operator: "no global keyboard shortcuts at all" from the UX audit
  punch list. Bloomberg's `Help Help` + TradingView's `Shift+?` are
  muscle memory for power users; this is the equivalent.

  Esc closes. Overlay click closes. Two grouped sections so the
  operator scans by intent (navigation / actions).
-->
<script>
  /** @type {{ open: boolean, onClose: () => void }} */
  let { open = false, onClose = () => {} } = $props();

  // Four grouped sections keep the cheat-sheet scannable.
  // Nav uses Bloomberg `g` + letter pattern (800 ms window).
  const NAV = [
    { key: 'g p', label: 'Pulse' },
    { key: 'g d', label: 'Dashboard' },
    { key: 'g o', label: 'Orders' },
    { key: 'g e', label: 'Derivatives' },
    { key: 'g c', label: 'Charts' },
    { key: 'g v', label: 'Performance' },
    { key: 'g a', label: 'Automation' },
    { key: 'g h', label: 'History' },
    { key: 'g m', label: 'Pulse — movers' },
  ];
  const ACTIONS = [
    { key: 't',        label: 'Order ticket' },
    { key: 'h',        label: 'Activity / log' },
    { key: 'k',        label: 'Chart modal (kline)' },
    { key: '/',        label: 'Focus symbol search' },
    { key: 'r',        label: 'Refresh page' },
    { key: '?',        label: 'This cheat-sheet' },
    { key: 'Esc',      label: 'Close modal / defocus' },
    { key: '⌘K',       label: 'Command palette (soon)' },
  ];
  const GRID = [
    { key: 'j',     label: 'Row down' },
    { key: 'k',     label: 'Row up' },
    { key: 'Enter', label: 'Context menu' },
    { key: 'f',     label: 'Fullscreen card' },
    { key: 'c',     label: 'Collapse card' },
  ];

  function _onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  }
</script>

<svelte:window onkeydown={open ? _onKey : null} />

{#if open}
  <div class="sc-overlay"
       role="presentation"
       onclick={onClose}></div>
  <div class="sc-modal" role="dialog" aria-modal="true"
       aria-labelledby="sc-title">
    <div class="sc-header">
      <h2 id="sc-title" class="sc-title">Keyboard shortcuts</h2>
      <button type="button" class="sc-close"
              aria-label="Close cheat-sheet"
              onclick={onClose}>×</button>
    </div>
    <div class="sc-grid">
      <section class="sc-section">
        <div class="sc-section-h">Navigation</div>
        {#each NAV as s}
          <div class="sc-row">
            <kbd class="sc-kbd">{s.key}</kbd>
            <span class="sc-lbl">{s.label}</span>
          </div>
        {/each}
      </section>
      <section class="sc-section">
        <div class="sc-section-h">Actions</div>
        {#each ACTIONS as s}
          <div class="sc-row">
            <kbd class="sc-kbd">{s.key}</kbd>
            <span class="sc-lbl">{s.label}</span>
          </div>
        {/each}
      </section>
      <section class="sc-section">
        <div class="sc-section-h">Grid (when focused)</div>
        {#each GRID as s}
          <div class="sc-row">
            <kbd class="sc-kbd">{s.key}</kbd>
            <span class="sc-lbl">{s.label}</span>
          </div>
        {/each}
      </section>
    </div>
    <div class="sc-foot">
      Letters are case-insensitive. Shortcuts pause while typing in a
      field — Esc defocuses. Grid shortcuts activate when a grid cell has focus.
    </div>
  </div>
{/if}

<style>
  .sc-overlay {
    position: fixed;
    inset: 0;
    background: rgba(8, 15, 28, 0.65);
    z-index: 9996;
    cursor: pointer;
    animation: sc-fade 120ms ease-out;
  }
  .sc-modal {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    z-index: 9997;
    width: min(44rem, calc(100vw - 1rem));
    max-height: calc(100vh - 2rem);
    overflow-y: auto;
    background: linear-gradient(180deg, var(--algo-bg-elev2) 0%, var(--algo-bg-elev1) 100%);
    border: 1px solid var(--algo-amber-border);
    border-radius: 6px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.55);
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    animation: sc-pop 140ms ease-out;
  }
  .sc-header {
    display: flex;
    align-items: center;
    padding: 0.55rem 0.7rem;
    border-bottom: 1px solid var(--algo-amber-border-soft);
  }
  .sc-title {
    flex: 1;
    margin: 0;
    font-size: var(--fs-lg);
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--algo-amber);
  }
  .sc-close {
    background: none;
    border: none;
    color: var(--algo-muted);
    font-size: 1.1rem;
    line-height: 1;
    padding: 0 0.25rem;
    cursor: pointer;
    border-radius: 3px;
  }
  .sc-close:hover { color: var(--algo-slate); background: rgba(255,255,255,0.06); }

  .sc-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 0;
    padding: 0.45rem 0.55rem;
  }
  @media (max-width: 700px) {
    .sc-grid { grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 480px) {
    .sc-grid { grid-template-columns: 1fr; }
  }
  .sc-section {
    padding: 0.3rem 0.4rem;
  }
  .sc-section-h {
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--algo-muted);
    margin-bottom: 0.35rem;
    padding-bottom: 0.2rem;
    border-bottom: 1px solid rgba(126,151,184,0.10);
  }
  .sc-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.2rem 0;
    font-size: var(--fs-sm);
  }
  .sc-kbd {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 1.6rem;
    height: 1.1rem;
    padding: 0 0.35rem;
    background: rgba(34, 211, 238, 0.10);
    border: 1px solid var(--algo-cyan-border-soft);
    border-radius: 3px;
    color: #67e8f9;
    font-size: var(--fs-xs);
    font-weight: 700;
    letter-spacing: 0.04em;
  }
  .sc-lbl { color: var(--algo-slate); }

  .sc-foot {
    padding: 0.4rem 0.7rem;
    border-top: 1px solid rgba(126,151,184,0.10);
    font-size: var(--fs-xs);
    color: var(--algo-muted);
    font-style: italic;
    line-height: 1.45;
  }

  @keyframes sc-fade {
    from { opacity: 0; }
    to   { opacity: 1; }
  }
  @keyframes sc-pop {
    from { opacity: 0; transform: translate(-50%, -48%) scale(0.97); }
    to   { opacity: 1; transform: translate(-50%, -50%) scale(1); }
  }
  @media (prefers-reduced-motion: reduce) {
    .sc-overlay, .sc-modal { animation: none; }
  }
</style>
