<!--
  DefaultSizeButton — restores a card to its default inline state
  (not fullscreen, not collapsed). Pairs with FullscreenButton +
  CollapseButton to give every card a three-state control trio:

    [□ Fullscreen]  [▢ Default]  [▾ Collapse]
       expand          inline      hide body

  Click resets BOTH isFullscreen=false and isCollapsed=false. Works
  even when the card is already in the default state (idempotent
  no-op). Highlights when the operator is currently in the default
  state so the button reads as "you are here".

  Usage:
    <FullscreenButton bind:isFullscreen={_fs} label="X" />
    <DefaultSizeButton
       bind:isFullscreen={_fs}
       bind:isCollapsed={_col}
       label="X" />
    <CollapseButton bind:isCollapsed={_col} cardId="x" label="X" />

  Same cyan-400 palette as the rest of the card-control trio so the
  three icons read as one consistent family.
-->
<script>
  let {
    /** Bindable fullscreen state — set to false on click. */
    isFullscreen = $bindable(false),
    /** Bindable collapse state — set to false on click. */
    isCollapsed = $bindable(false),
    /** Card name for a11y / tooltip. */
    label = 'card',
  } = $props();

  // This component is mounted by CardControls only while isFullscreen=true, so
  // we can use $effect unconditionally here — no isFullscreen guard needed.
  // Placing these side-effects here (rather than in FullscreenButton) avoids the
  // mount-race: CardControls unmounts FullscreenButton as part of the same reactive
  // flush that sets isFullscreen=true, which would destroy its $effect before it ran.
  function _onKey(e) {
    if (e.key === 'Escape') isFullscreen = false;
  }

  $effect(() => {
    document.addEventListener('keydown', _onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

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

<!-- Only renders in the FULLSCREEN state. Paired with FullscreenButton
     (rendered only in default state) as a single size-control slot —
     the operator sees "expand" when default, "restore" when fullscreen.
     Click restores to default inline size + un-collapses in one move. -->
{#if isFullscreen}
  <button
    type="button"
    class="default-btn"
    onclick={(e) => {
      e.stopPropagation();
      isFullscreen = false;
      isCollapsed = false;
    }}
    aria-label={`Restore ${label} to default size`}
    title="Restore to default size">
    <!-- ✕ close glyph — universally recognised "exit / close" affordance.
         Paired with the pinned body close-button added in FullscreenButton
         so the operator has two clear exit paths: the in-card button here
         and the floating ✕ at top-right of the viewport. -->
    <span class="fs-x-icon" aria-hidden="true">✕</span>
  </button>
{/if}

<style>
  /* Backdrop: GLOBAL because the element is portalled to document.body
     imperatively, so Svelte's scoped selector wouldn't reach it.
     Mirrored in app.css for discoverability; this :global() is authoritative. */
  :global(.fs-backdrop) {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    z-index: 9998;
    backdrop-filter: blur(3px);
    -webkit-backdrop-filter: blur(3px);
  }

  /* Shared cyan-400 palette with RefreshButton + FullscreenButton +
     CollapseButton so the four card-control icons read as one family.
     `margin: 0` because this button always sits BETWEEN
     FullscreenButton (which carries the `margin-left: auto`) and
     CollapseButton — parent `gap` handles inter-button spacing. */
  .default-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    margin: 0;
    background: var(--algo-cyan-bg);
    border: 1px solid var(--algo-cyan-border);
    border-radius: 3px;
    color: var(--c-info);
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
  }
  .default-btn:hover {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.85);
    color: #67e8f9;
  }
  .default-btn:focus-visible {
    outline: 2px solid rgba(34, 211, 238, 0.65);
    outline-offset: 1px;
  }

</style>
