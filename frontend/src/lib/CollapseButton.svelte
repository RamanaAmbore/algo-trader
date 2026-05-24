<!--
  CollapseButton — small toggle that collapses/expands a card body.
  Mirrors the FullscreenButton pattern (top-right corner, bindable
  state, drop-in any card).

  Usage:
    <script>
      let isCollapsed = $state(false);
    </script>

    <section class:is-collapsed={isCollapsed}>
      <header>
        <h2>Capital</h2>
        <CollapseButton bind:isCollapsed cardId="capital" />
        <FullscreenButton bind:isFullscreen={isFullscreen} />
      </header>
      {#if !isCollapsed}
        …card body…
      {/if}
    </section>

  Persistence — per user, per card
  --------------------------------
  State persists to localStorage under
      ramboq.collapse.${username || 'demo'}.${cardId}
  so each operator's preferences are independent. Anonymous demo
  visitors share a 'demo' bucket (cleared per browser-data wipe).
  Falls back silently when localStorage is unavailable (Safari
  private mode, quota exceeded).

  When CollapseButton + FullscreenButton coexist in the same header,
  CollapseButton sits first (per UX convention — collapse is the
  lighter-touch action) and a `+` selector in global CSS removes
  the auto-margin on the following FullscreenButton so they cluster
  tightly at the right edge of the header.
-->
<script>
  import { onMount } from 'svelte';
  import { authStore } from '$lib/stores';

  let {
    /** Unique identifier per card — used as the storage key. Required. */
    cardId,
    /** bindable collapse state. */
    isCollapsed = $bindable(false),
    /** Initial state when no stored value exists (cold-start default). */
    initialCollapsed = false,
    /** Card name for a11y / tooltip. */
    label = 'card',
  } = $props();

  // Per-user storage key. Anonymous demo sessions share a 'demo'
  // bucket — fine because demo state isn't sensitive and the
  // recruiter-style visitor doesn't need cross-session persistence.
  let _username = $state('demo');
  authStore.subscribe(v => { _username = v?.user?.username || 'demo'; });
  const _storageKey = $derived(`ramboq.collapse.${_username}.${cardId}`);

  // Restore on mount — overrides initialCollapsed if a stored value
  // is found. Reading inside onMount (not $effect) so the restore
  // happens once, before subsequent toggle effects fire.
  let _restored = false;
  onMount(() => {
    if (!cardId) return;
    try {
      const stored = localStorage.getItem(_storageKey);
      if (stored !== null) {
        isCollapsed = stored === '1';
      } else {
        isCollapsed = initialCollapsed;
      }
    } catch (_) {
      isCollapsed = initialCollapsed;
    }
    _restored = true;
  });

  // Persist on subsequent toggles. The _restored gate keeps the
  // initial mount from writing back a default before we've finished
  // reading the stored value (avoids overwriting a stored "1" with
  // the default "0").
  $effect(() => {
    if (!_restored || !cardId || typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(_storageKey, isCollapsed ? '1' : '0');
    } catch (_) { /* quota / Safari private — silent. */ }
  });
</script>

<button
  type="button"
  class="collapse-btn"
  onclick={(e) => { e.stopPropagation(); isCollapsed = !isCollapsed; }}
  aria-label={isCollapsed ? `Expand ${label}` : `Collapse ${label}`}
  aria-expanded={!isCollapsed}
  title={isCollapsed ? 'Expand card' : 'Collapse card'}>
  {#if isCollapsed}
    <!-- Chevron right — "click to expand" -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M6 4l4 4-4 4"
        fill="none" stroke="currentColor" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  {:else}
    <!-- Chevron down — "click to collapse" -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <path d="M4 6l4 4 4-4"
        fill="none" stroke="currentColor" stroke-width="1.8"
        stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  {/if}
</button>

<style>
  .collapse-btn {
    /* Same chrome + sizing as FullscreenButton so the pair cluster
       visually as a single icon group when both are present. */
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
  .collapse-btn:hover {
    background: rgba(251, 191, 36, 0.12);
    border-color: rgba(251, 191, 36, 0.45);
    color: #fbbf24;
  }
  .collapse-btn:focus-visible {
    outline: 2px solid rgba(251, 191, 36, 0.55);
    outline-offset: 1px;
  }

  /* When CollapseButton is followed by FullscreenButton (the
     canonical ordering — collapse first as the lighter-touch action),
     the fs-btn drops its margin-left:auto and sits tight with a
     small gap. Targeted via :global so the .fs-btn (declared scoped
     in FullscreenButton.svelte) can be addressed. */
  .collapse-btn + :global(.fs-btn) {
    margin-left: 0.3rem;
  }
</style>
