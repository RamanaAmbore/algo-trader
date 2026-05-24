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

  // ESC key + body scroll lock + portalled backdrop for the active
  // fullscreen card. Multiple cards on the page may each be bound
  // to their own isFullscreen; only the one currently true installs
  // the side-effects + backdrop node.
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

    // Portal-style backdrop — appended to document.body so its
    // stacking context is the viewport root, not the parent card.
    // backdrop-filter blur then applies only to page content, not
    // to the card itself.
    const backdrop = document.createElement('div');
    backdrop.className = 'fs-backdrop';
    backdrop.setAttribute('aria-hidden', 'true');
    backdrop.addEventListener('click', () => { isFullscreen = false; });
    document.body.appendChild(backdrop);

    return () => {
      document.removeEventListener('keydown', _onKey);
      document.body.style.overflow = prev;
      backdrop.remove();
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

<style>
  .fs-btn {
    /* Pin to top-right within the card body. `margin-left: auto`
       pushes the button to the rightmost slot of any flex parent
       (card-header-row / bucket-header / row3-header / details
       summary), guaranteeing top-right placement without needing
       every card header to opt into justify-content: space-between
       or a sibling spacer. */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    margin: 0 0 0 auto;
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

  /* Backdrop styles are GLOBAL because the element is appended to
     document.body imperatively (not via Svelte template), so style
     scoping wouldn't reach it. Mirror in app.css to make the global
     selector visible to tools and accidentally-overlapping cleanups. */
  :global(.fs-backdrop) {
    position: fixed;
    inset: 0;
    background: rgba(5, 10, 22, 0.65);
    z-index: 9998;
    backdrop-filter: blur(2px);
    -webkit-backdrop-filter: blur(2px);
  }
</style>
