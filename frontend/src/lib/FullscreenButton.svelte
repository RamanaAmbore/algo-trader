<!--
  FullscreenButton — small toggle that promotes the enclosing card to a
  full-viewport modal. Drop this in any card's top-right corner, bind
  `isFullscreen`, and add `class:fs-card-on={isFullscreen}` to the card.

  Usage:
    <script>
      let isFullscreen = $state(false);
    </script>

    <section class="my-card" class:fs-card-on={isFullscreen}>
      <header>
        <h2>Title</h2>
        <FullscreenButton bind:isFullscreen label="My card" />
      </header>
      …card body…
    </section>

  Global CSS provides `.fs-card-on` (fixed modal, full viewport,
  z-index 9999) + `.fs-backdrop`. ESC + backdrop click close — both
  owned by DefaultSizeButton.svelte, not this component (see below).

  Why the backdrop is portalled to document.body
  ----------------------------------------------
  The card carries `isolation: isolate` (it has to — children like
  ag-Grid hover popups need stable stacking). That creates a NEW
  stacking context, which scopes any child z-index to the card. A
  backdrop rendered as a sibling of the button (i.e. INSIDE the card)
  ends up with `backdrop-filter: blur(...)` applying to everything
  behind it in the card's stacking context — including the card's
  own content. Result: the card looks blurred.

  Fix: portal the backdrop to document.body so it sits as a true
  viewport-level sibling of the card. Its `z-index: 9998` then puts
  it correctly between the page (no stacking context) and the card
  (`position: fixed; z-index: 9999`). Backdrop-filter blurs only the
  page behind, not the card itself.
-->
<script>
  let { isFullscreen = $bindable(false), label = 'card' } = $props();
  // Side-effects (backdrop, close-btn, scroll-lock, Escape handler) live in
  // DefaultSizeButton.svelte, which is mounted only while isFullscreen=true.
  // Keeping them here would cause a race: CardControls unmounts FullscreenButton
  // as part of the same reactive flush that sets isFullscreen=true, so the
  // $effect would never fire.
</script>

<!-- Only renders in the DEFAULT state (not fullscreen). The "exit
     fullscreen" affordance is handled by DefaultSizeButton, which is
     conditionally rendered as the other half of this either/or pair.
     One slot, one button — operator sees ONE size-control icon at a
     time, alternating between "expand" (this) and "restore" depending
     on the current state. -->
{#if !isFullscreen}
  <button
    type="button"
    class="fs-btn algo-icon-btn"
    onclick={(e) => { e.stopPropagation(); isFullscreen = true; }}
    aria-label={`Expand ${label} to fullscreen`}
    title="Expand to fullscreen">
    <!-- Expand / fullscreen icon — four arrows pointing outward -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  </button>
{/if}

<style>
  .fs-btn {
    /* Box-model/palette (display/align/justify/size/padding/background/
       border/radius) now live in the shared `.algo-icon-btn` class
       (app.css, 2026-10-13 reusability sweep Part A). `margin` stays
       LOCAL — this button self-positions to the rightmost slot of any
       flex parent (card-header-row / bucket-header / row3-header /
       details summary) via `margin-left: auto`, which is NOT the
       shared class's plain `margin: 0` default; the local declaration
       always wins (Svelte's scoping attribute gives it higher
       specificity than the global class, regardless of source order).
       Vibrant cyan-400 (#22d3ee) palette — shared with RefreshButton
       + CollapseButton so the trio of card-control icons reads as
       one consistent family. Matches the "live data / control"
       accent used across Bloomberg Terminal, IBKR TWS and Sensibull. */
    margin: 0 0 0 auto;
    color: var(--c-info);
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
  }
  .fs-btn:hover {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.85);
    color: #67e8f9;
  }
  .fs-btn:focus-visible {
    outline: 2px solid rgba(34, 211, 238, 0.65);
    outline-offset: 1px;
  }

  /* .fs-backdrop :global() rule lives in DefaultSizeButton.svelte — that
     component owns the portalled backdrop element. Mirrored in app.css. */
</style>
