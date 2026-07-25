<!--
  PageFullscreenButton — page-header shortcut that fullscreens the most
  recently hovered card on the current page.

  Registers with `activeCardStore` (stores.js). Cards write to that store
  via CardHeader.svelte's onmouseenter handler. When the operator hovers a
  card header then clicks this button, the card expands to fullscreen as if
  they had clicked the card's own FullscreenButton.

  Disabled when no card has been hovered yet (store.open is null), so the
  operator gets a clear "hover a card first" hint via the tooltip.

  Sits inside the PageHeaderActions pha-wrap cluster, styled to match the
  existing pha-btn family (same 1.4rem × 1.4rem, same palette token).
-->
<script>
  import { activeCardStore } from '$lib/stores.js';

  function _expand() {
    if ($activeCardStore.open) $activeCardStore.open();
  }
</script>

<button
  type="button"
  class="pha-btn pha-fs"
  onclick={_expand}
  disabled={!$activeCardStore.open}
  title={$activeCardStore.label ? `Expand: ${$activeCardStore.label}` : 'Hover a card first'}
  aria-label="Expand active card fullscreen"
>
  <!-- Same four-corners-outward SVG as FullscreenButton.svelte — stroke-only,
       no fill, so it reads at 13 × 13 px without aliasing. -->
  <svg viewBox="0 0 16 16" width="13" height="13" fill="none" aria-hidden="true">
    <path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"
      stroke="currentColor" stroke-width="1.5"
      stroke-linecap="round" stroke-linejoin="round" />
  </svg>
</button>

<style>
  /* Inherits .pha-btn base from PageHeaderActions — this component adds
     only the accent colour. Cyan-400 palette matches FullscreenButton
     and the Chart pha-btn so the "expand" affordance is visually
     associated with the chart / workspace icon family. */
  .pha-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    border-radius: 4px;
    cursor: pointer;
    flex-shrink: 0;
    background: rgba(255, 255, 255, 0.03);
    transition: background 0.12s, border-color 0.12s, color 0.12s;
  }
  .pha-btn:disabled {
    opacity: 0.35;
    cursor: not-allowed;
  }
  .pha-btn:focus-visible {
    outline: 1px solid rgba(255, 255, 255, 0.40);
    outline-offset: 2px;
  }

  /* Cyan-400 accent — same as FullscreenButton and the Chart pha-btn. */
  .pha-fs {
    border: 1px solid rgba(34, 211, 238, 0.40);
    color: var(--c-info, #22d3ee);
  }
  .pha-fs:hover:not(:disabled) {
    background: rgba(34, 211, 238, 0.14);
    border-color: rgba(103, 232, 249, 0.65);
    color: #67e8f9;
  }
</style>
