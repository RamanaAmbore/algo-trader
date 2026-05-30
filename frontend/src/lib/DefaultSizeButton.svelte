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
    <!-- Single rounded square — represents "default / inline card size".
         Same square idiom Windows / Linux desktops use for the
         restore-down windowing control. -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <rect x="2.5" y="2.5" width="11" height="11" rx="1.5"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linejoin="round" />
    </svg>
  </button>
{/if}

<style>
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
    background: rgba(34, 211, 238, 0.14);
    border: 1px solid rgba(34, 211, 238, 0.55);
    border-radius: 3px;
    color: #22d3ee;
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
  /* When the canonical trio sits in a flex parent, ensure tight
     spacing between adjacent control buttons regardless of their
     parent's `gap`. Mirrors CollapseButton's existing
     `.collapse-btn + :global(.fs-btn)` rule. */
  :global(.fs-btn + .default-btn),
  :global(.default-btn + .collapse-btn) {
    margin-left: 0.3rem;
  }
  /* Fullscreen → Collapse (no DefaultSize between them) — preserves
     the same compact spacing when a card opts out of the middle
     control. Sibling-selector reach across components requires the
     global block. */
  :global(.fs-btn + .collapse-btn) {
    margin-left: 0.3rem;
  }
</style>
