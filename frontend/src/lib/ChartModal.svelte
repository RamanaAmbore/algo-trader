<script>
  // ChartModal — thin overlay wrapper around ChartWorkspace.
  // Opens as a fixed-inset modal with an amber-glow navy panel.
  // Esc key and × close; overlay is pointer-events:none so the
  // navbar / menu underneath remain reachable.

  import { onMount, onDestroy } from 'svelte';
  import ChartWorkspace from '$lib/ChartWorkspace.svelte';
  import { formatSymbol } from '$lib/data/decomposeSymbol';
  import { rootOfLabel } from '$lib/data/rootOf.js';
  import { chartStore } from '$lib/data/chartStore.svelte.js';
  import { closeChartModal } from '$lib/stores';

  let {
    /** @type {string} */ symbol = '',
    /** @type {string} */ exchange = '',
    /** @type {'live'|'sim'|'paper'} */ mode = 'live',
    /** @type {(() => void) | null} */ onClose = null,
  } = $props();

  let _modalEl = $state(/** @type {HTMLElement|null} */ (null));
  let _loading = $state(false);

  const _ariaLabel = $derived.by(() => {
    const rl = rootOfLabel(symbol, exchange);
    return 'Chart — ' + (rl !== symbol ? rl : formatSymbol(symbol));
  });

  function _focusables() {
    return /** @type {NodeListOf<HTMLElement>} */ (
      _modalEl?.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])') ?? []
    );
  }

  function _onKey(/** @type {KeyboardEvent} */ e) {
    if (e.key === 'Escape') {
      // stopImmediatePropagation prevents a parent SymbolPanel's
      // capture-phase listener from also firing when ChartModal is on top.
      e.stopImmediatePropagation();
      closeChartModal();
      onClose?.();
      return;
    }
    if (e.key === 'Tab') {
      const els = Array.from(_focusables());
      if (!els.length) return;
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
  }

  onMount(() => {
    if (symbol) chartStore.setSymbol(symbol);
    if (exchange) chartStore.setExchange(exchange);
    window.addEventListener('keydown', _onKey, { capture: true });
    setTimeout(() => { _focusables()[0]?.focus(); }, 0);
  });
  onDestroy(() => {
    window.removeEventListener('keydown', _onKey, { capture: true });
  });
</script>

<!-- svelte-ignore a11y_interactive_supports_focus -->
<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- overlay sits at z-index 10600 (above SymbolPanel at 10500) so
     ChartModal is never hidden behind it. pointer-events:auto enables
     backdrop click-to-close (clicking outside the panel calls onClose). -->
<div class="canonical-modal-overlay cm-overlay" class:cm-busy={_loading}
     role="dialog" aria-modal="true"
     aria-label={_ariaLabel} tabindex="-1"
     onclick={(e) => { if (e.target === e.currentTarget) { closeChartModal(); onClose?.(); } }}>
  <div class="canonical-modal-panel cm-modal" class:cm-busy={_loading} bind:this={_modalEl}>
    <div class="cm-header">
      <!-- Modal-name only — the symbol picker lives inside ChartWorkspace,
           and showing the symbol up here too would duplicate it.
           Plural matches the /charts page route name. The leading icon
           mirrors the page-header Chart button glyph so the operator
           reads "I'm in Charts" at a glance. -->
      <span class="cm-title">
        <!-- Chart glyph stays static — operator: "instead of rotating
             chart icon, keep it static". Refresh-state lives on the
             separate refresh icon to the right of the title now. -->
        <svg class="cm-title-icon"
             width="12" height="12" viewBox="0 0 16 16"
             fill="none" stroke="currentColor" stroke-width="1.9"
             stroke-linecap="round" stroke-linejoin="round"
             aria-hidden="true">
          <path d="M2 13h12M3 11l3-4 3 2 4-6" />
        </svg>
        Charts
      </span>
      <!-- Right-side action cluster — refresh-state icon + X close.
           Refresh icon rotates while a fetch is in flight; static
           otherwise. Operator: "add rotating refresh icon before X
           and rotate it while refreshing. keep it right aligned". -->
      <span class="cm-actions">
        <span class="cm-refresh-wrap"
              title={_loading ? 'Refreshing chart — modal is locked until done' : ''}
              aria-live="polite">
          <svg class="cm-refresh-icon" class:cm-refresh-icon-loading={_loading}
               width="13" height="13" viewBox="0 0 16 16"
               fill="none" stroke="currentColor" stroke-width="1.6"
               stroke-linecap="round" stroke-linejoin="round"
               aria-hidden="true">
            <!-- Canonical circular-refresh glyph: open arc + arrowhead -->
            <path d="M13.5 8a5.5 5.5 0 1 1-1.61-3.9" />
            <path d="M13.5 3v3h-3" />
          </svg>
        </span>
        <button type="button" class="cm-close"
                onclick={() => { closeChartModal(); onClose?.(); }}
                aria-label="Close chart modal">×</button>
      </span>
    </div>
    <div class="cm-body">
      <div class="cm-chart-card">
        <ChartWorkspace
          symbol={symbol}
          exchange={exchange}
          mode={mode}
          compact={true}
          bind:loading={_loading}
        />
      </div>
    </div>
  </div>
</div>

<style>
  /* Frame (overlay + panel positioning, size, border, gradient) lives in
     app.css under .canonical-modal-overlay / .canonical-modal-panel.
     Local styles only carry the chart-specific header + close-button
     chrome below. */

  /* Override the global .canonical-modal-overlay z-index (10500, shared
     with SymbolPanel) so ChartModal always renders on top. Also enable
     pointer-events so clicking the backdrop (outside the panel) closes
     the modal — .cm-modal restores auto on the panel itself. */
  .cm-overlay {
    z-index: 10600;
    pointer-events: auto;
  }

  .cm-header {
    /* Operator: "The line below modal headers is too prominent.
       Reduce its prominence. Instead, change background color of
       the header for modals." Stronger cyan-tinted gradient bg
       acts as the visual separator from the body; bottom border
       is a hairline (1px low-alpha). */
    display: flex;
    align-items: center;
    padding: 0.35rem 0.85rem;
    background: linear-gradient(180deg,
                  rgba(34, 211, 238, 0.18) 0%,
                  rgba(34, 211, 238, 0.06) 100%);
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.06);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .cm-title {
    /* Plain title text — operator: "remove pill kind of decoration
       for modal header text". Bold uppercase cyan glyphs on the
       navy gradient strip; the gradient itself is the prominence. */
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    color: #67e8f9;
    font-weight: 800;
    letter-spacing: 0.10em;
    text-transform: uppercase;
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
  }
  /* Matches the page-header Chart button's resting colour (#22d3ee
     = cyan-400) so the modal title icon is the exact same shade as
     the button that opened it. */
  .cm-title-icon { color: var(--c-info); flex-shrink: 0; }

  .cm-close {
    /* Operator: "X and refresh rotating icon should be of similar
       size and consistent with header text font size". Square 1.4rem
       button matches the refresh icon to the left; glyph font-size
       (0.95rem) is proportional to the 0.72rem header text. */
    width: 1.4rem;
    height: 1.4rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.35);
    border-radius: 3px;
    color: var(--c-short);
    font-size: var(--fs-xl);
    line-height: 1;
    padding: 0;
    cursor: pointer;
    font-family: monospace;
    transition: background 0.1s;
    pointer-events: auto;
    position: relative;
    z-index: 2;
    flex-shrink: 0;
  }
  .cm-close:hover {
    background: rgba(248, 113, 113, 0.15);
  }

  .cm-body {
    overflow: hidden;
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .cm-chart-card {
    /* Operator: "in charts modal the content area top left and right
       corners have a small round corner which is not in sync with
       other modals. keep it in sync". SymbolPanel and
       ActivityLogModal bodies have square corners; matched by
       dropping the top border-radius. */
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* Busy state — chart fetch in flight. Overlay stays
     pointer-events:none so menu / navbar clicks underneath still
     work; the chart body becomes inert so the operator can't
     re-trigger the in-flight fetch. The × button retains
     pointer-events:auto (set on .cm-close directly) and stays
     clickable. */
  .cm-modal.cm-busy .cm-body { pointer-events: none; }
  /* Subtle scrim over the body so the lock state is visually obvious. */
  .cm-modal.cm-busy .cm-body::after {
    content: '';
    position: absolute;
    inset: 0;
    background: rgba(13, 24, 41, 0.18);
    pointer-events: none;
  }
  .cm-body { position: relative; }

  /* Right-side action cluster — refresh icon + X close, sits via
     `margin-left: auto` on the wrapper so the title group hugs left,
     the actions hug right. Operator: "keep it right aligned". */
  .cm-actions {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    margin-left: auto;
  }
  .cm-refresh-wrap {
    /* Refresh icon — square 1.4rem to match the X close button to
       its right. Same chip chrome as the page-header RefreshButton
       so the operator's mental model carries across surfaces. */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    color: var(--c-info);
    border-radius: 3px;
    background: var(--algo-cyan-bg);
    border: 1px solid var(--algo-cyan-border);
  }
  /* Refresh-state icon rotates while a fetch is in flight; static
     otherwise. Replaces the prior `cm-title-icon-loading` rotation
     on the chart glyph — the chart icon is the modal identity, the
     refresh icon carries the load state. */
  .cm-refresh-icon-loading {
    animation: cm-refresh-spin 1.1s linear infinite;
    transform-origin: 50% 50%;
  }
  @keyframes cm-refresh-spin {
    from { transform: rotate(0deg); }
    to   { transform: rotate(360deg); }
  }
  @media (prefers-reduced-motion: reduce) {
    .cm-refresh-icon-loading { animation: none; }
  }
</style>
