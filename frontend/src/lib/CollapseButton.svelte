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
  import { onMount, onDestroy } from 'svelte';
  import { authStore } from '$lib/stores';

  let {
    /** Unique identifier per card — used as the localStorage key for
     *  per-user collapse-state persistence. Omit to skip persistence
     *  entirely (the button still toggles in-session but the state
     *  resets to `initialCollapsed` on every page load). */
    cardId = '',
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
  // SUBSCRIPTION LEAK FIX (Perf audit Jul 2026): authStore.subscribe was
  // module-top-level, never unsubscribed. CollapseButton is instanced on
  // every card on every algo page (dozens per session) — the unsub
  // accumulation was a measurable contributor to per-tick scheduler
  // overhead. Bind into onMount + onDestroy so each instance owns its
  // subscription lifetime.
  let _username = $state('demo');
  /** @type {(() => void) | null} */
  let _unsubAuth = null;
  onMount(() => {
    _unsubAuth = authStore.subscribe(v => { _username = v?.user?.username || 'demo'; });
  });
  onDestroy(() => { _unsubAuth?.(); _unsubAuth = null; });
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
    /* Vibrant cyan-400 (#22d3ee) — shared with RefreshButton and
       FullscreenButton so the trio of card-control icons reads as
       one consistent family. Matches the "live data / control"
       accent across Bloomberg Terminal, IBKR TWS and Sensibull. */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.4rem;
    height: 1.4rem;
    padding: 0;
    margin: 0 0 0 auto;
    background: var(--algo-cyan-bg);
    border: 1px solid var(--algo-cyan-border);
    border-radius: 3px;
    color: var(--c-info);
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
  }
  .collapse-btn:hover {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.85);
    color: #67e8f9;
  }
  .collapse-btn:focus-visible {
    outline: 2px solid rgba(34, 211, 238, 0.65);
    outline-offset: 1px;
  }

</style>
