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
  z-index 9999) + `.fs-backdrop`. ESC + backdrop click close.
-->
<script>
  import { onMount } from 'svelte';

  let { isFullscreen = $bindable(false), label = 'card' } = $props();

  // ESC key + body scroll lock for the active fullscreen card.
  // Multiple cards on the page may each be bindable to their own
  // isFullscreen; only the one currently true installs the
  // keydown + scroll-lock side-effects.
  function _onKey(e) {
    if (e.key === 'Escape' && isFullscreen) {
      isFullscreen = false;
    }
  }

  $effect(() => {
    if (!isFullscreen) return;
    document.addEventListener('keydown', _onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      document.removeEventListener('keydown', _onKey);
      document.body.style.overflow = prev;
    };
  });
</script>

<button
  type="button"
  class="fs-btn"
  onclick={(e) => { e.stopPropagation(); isFullscreen = !isFullscreen; }}
  aria-label={isFullscreen ? `Exit fullscreen ${label}` : `Expand ${label} to fullscreen`}
  title={isFullscreen ? 'Exit fullscreen (Esc)' : 'Expand to fullscreen'}>
  {#if isFullscreen}
    <!-- Contract / minimize icon — four arrows pointing inward -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M6 1v4H2M10 1v4h4M6 15v-4H2M10 15v-4h4"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  {:else}
    <!-- Expand / fullscreen icon — four arrows pointing outward -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M2 6V2h4M14 6V2h-4M2 10v4h4M14 10v4h-4"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  {/if}
</button>

{#if isFullscreen}
  <!-- Backdrop — sits BEHIND the .fs-card-on element. Click closes.
       Rendered as a sibling so its z-index is independent of the
       parent card's stacking context. -->
  <div
    class="fs-backdrop"
    aria-hidden="true"
    onclick={() => isFullscreen = false}></div>
{/if}

<style>
  .fs-btn {
    /* Pin to top-right within the card body. Cards either anchor
       this via flexbox in their header (margin-left:auto) or via
       absolute positioning — both work. */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    margin: 0;
    background: rgba(126, 151, 184, 0.10);
    border: 1px solid rgba(126, 151, 184, 0.28);
    border-radius: 3px;
    color: #c8d8f0;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
  }
  .fs-btn:hover {
    background: rgba(251, 191, 36, 0.12);
    border-color: rgba(251, 191, 36, 0.45);
    color: #fbbf24;
  }
  .fs-btn:focus-visible {
    outline: 2px solid rgba(251, 191, 36, 0.55);
    outline-offset: 1px;
  }

  /* Backdrop — fixed to viewport, sits under the card. Slightly
     darker than the SymbolPanel backdrop because the underlying
     dashboard is visually busy. */
  .fs-backdrop {
    position: fixed;
    inset: 0;
    background: rgba(5, 10, 22, 0.65);
    z-index: 9998;
    backdrop-filter: blur(2px);
  }
</style>
