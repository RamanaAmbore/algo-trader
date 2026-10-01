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
  import { pushLayer, popLayer } from '$lib/utils/layerStack.js';

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

  // Escape-stack coordinator (layerStack.js, Wave B 2026-09-30) — the
  // full-screen card is its own dismissible layer, pushed on mount
  // (isFullscreen became true) and popped on destroy (isFullscreen went
  // false, by any means: Escape, backdrop click, the button itself).
  // Replaces a bare `document.addEventListener('keydown', ...)` that
  // fired unconditionally and raced every other overlay's own Escape
  // listener — one Escape could close BOTH this full-screen card AND an
  // order modal / cheatsheet / nested dropdown opened on top of it.
  // Now only the topmost pushed layer reacts, fixing all three:
  //   - full-screen + order modal open together (SymbolPanel is already
  //     migrated onto the same coordinator)
  //   - full-screen + shortcut cheatsheet open together (ShortcutCheatsheet
  //     is already migrated onto the same coordinator)
  //   - full-screen + a nested symbol-search dropdown open together
  //     (SymbolSearchInput is already migrated onto the same coordinator)
  /** @type {string | null} */
  let _layerId = null;

  $effect(() => {
    _layerId = pushLayer(() => { isFullscreen = false; });

    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';

    // Backdrop is now a pure dim/blur visual with NO click handler of
    // its own — pointer-events:none (see :global(.fs-backdrop) below).
    // Before this fix the backdrop carried the click-to-exit handler
    // directly and spanned the full viewport (inset:0) at z-index 9998,
    // which sits ABOVE the fixed `.algo-navbar` (z-index
    // var(--z-nav)=50) AND `.page-header` (z-index 45) — so tapping the
    // hamburger / mode chip / broker chip / page-header actions while a
    // card was fullscreen just exited fullscreen instead of doing what
    // was tapped (the click landed on the backdrop, not the real
    // control underneath). Same root cause + same fix shape as
    // SymbolPanel's `.oes-click-catcher` (commit 161fc002): a separate
    // `.fs-backdrop-catch` plate carries the actual click-to-exit
    // handler, positioned from the LIVE measured bottom edge of the
    // navbar+page-header chrome strip down to the viewport bottom —
    // not the static --modal-sheet-top var, since the navbar can wrap
    // to two rows on mobile and a static guess wouldn't track that.
    const backdrop = document.createElement('div');
    backdrop.className = 'fs-backdrop';
    backdrop.setAttribute('aria-hidden', 'true');
    document.body.appendChild(backdrop);

    const catcher = document.createElement('div');
    catcher.className = 'fs-backdrop-catch';
    catcher.setAttribute('aria-hidden', 'true');
    catcher.addEventListener('click', () => { isFullscreen = false; });
    document.body.appendChild(catcher);

    // `.fs-card-on`'s own top inset is driven by `--fs-card-top` (set
    // below, app.css), computed from this SAME live measurement, so the
    // card and this catcher always agree on exactly where the real
    // chrome ends — in practice the catcher's band sits entirely UNDER
    // the fullscreen card and is never actually hit, so the
    // click-to-exit-via-backdrop affordance has no reachable area left.
    // Same tradeoff SymbolPanel's click-outside fix had to accept for
    // the same structural reason (full-bleed sheet starting right at
    // the chrome boundary). The Default-size button + Escape remain the
    // ways to exit fullscreen.
    //
    // 2026-10-01 fix (live-verified): the static --modal-sheet-top var
    // (navbar + page-header only) undercounts the real chrome height on
    // any page where `.ps-strip` (PositionStrip) and/or `.demo-banner`
    // are ALSO visible — both are `position: fixed` bands that push
    // `.page-header` itself further down (see the `:has(.ps-strip)` /
    // `:has(.demo-banner)` overrides in +layout.svelte) without a
    // matching override ever having existed for `.fs-card-on`. Without
    // this fix the fullscreen card's own top edge painted OVER the
    // bottom slice of the real page-header strip (confirmed live: with
    // `.ps-strip` visible, `.pha-order` resolved to the card itself at
    // its hit-test point, not the button or the backdrop) — a second,
    // independent occlusion from the one this fix otherwise addresses,
    // and the actual reason "page-header actions" stayed unreachable
    // even after the backdrop/catcher split above. Measuring every
    // live fixed chrome band's own bottom edge (not just navbar +
    // page-header) and taking the max fixes both at once.
    const _measureChrome = () => {
      const navEl  = document.querySelector('.algo-navbar');
      const phEl   = document.querySelector('.page-header');
      const psEl   = document.querySelector('.ps-strip');
      const dbEl   = document.querySelector('.demo-banner');
      const bottoms = [navEl, phEl, psEl, dbEl]
        .map((el) => (el ? el.getBoundingClientRect().bottom : 0));
      const top = Math.max(...bottoms, 0);
      catcher.style.top = `${top}px`;
      document.documentElement.style.setProperty('--fs-card-top', `${top}px`);
    };
    _measureChrome();
    window.addEventListener('resize', _measureChrome);

    return () => {
      popLayer(_layerId);
      _layerId = null;
      document.body.style.overflow = prev;
      window.removeEventListener('resize', _measureChrome);
      document.documentElement.style.removeProperty('--fs-card-top');
      backdrop.remove();
      catcher.remove();
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
    <!-- Compress/restore icon — mirror of FullscreenButton's outward-arrows
         expand icon (corner brackets near center, arms pointing toward the
         edges). Deliberately NOT a ✕ glyph: this button restores default
         size, it doesn't close/dismiss content, and a blue ✕ here read as
         a mismatched "close" button next to the app's red close/dismiss
         convention (operator-reported 2026-09). Same cyan-400 family
         color as FullscreenButton/CollapseButton is intentional and kept. -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M6 2V6H2M10 2V6H14M6 14V10H2M10 14V10H14"
        fill="none" stroke="currentColor" stroke-width="1.5"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  </button>
{/if}

<style>
  /* Backdrop: GLOBAL because the element is portalled to document.body
     imperatively, so Svelte's scoped selector wouldn't reach it.
     Mirrored in app.css for discoverability; this :global() is authoritative.

     pointer-events:none (2026-10-01 fix) — see the $effect comment
     above. This is now a pure visual dim/blur layer; the click-to-exit
     handler lives on the sibling `.fs-backdrop-catch` plate below so
     the fixed navbar/page-header chrome strip stays clickable while
     any card is fullscreen. */
  :global(.fs-backdrop) {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    z-index: 9998;
    backdrop-filter: blur(3px);
    -webkit-backdrop-filter: blur(3px);
    pointer-events: none;
  }

  /* Click-to-exit-fullscreen plate — spans from the LIVE measured
     bottom edge of the navbar+page-header chrome strip (set via
     `catcher.style.top` in the $effect above) down to the viewport
     bottom, left:0/right:0. Carries the actual click handler; the
     chrome strip itself (0..top) is left with no auto pointer-events
     anywhere above it, so clicks there fall straight through to the
     real `.algo-navbar` / `.page-header` controls (z-index 50 / 45)
     underneath both this plate and `.fs-backdrop`. No background —
     `.fs-backdrop`'s own dim/blur still supplies the visible look. */
  :global(.fs-backdrop-catch) {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 9998;
    pointer-events: auto;
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
