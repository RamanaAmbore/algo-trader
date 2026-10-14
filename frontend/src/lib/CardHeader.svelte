<!--
  CardHeader — unified card title bar for algo + public surfaces.
  Theming via CSS custom properties defined in each layout.
  Three zones: LEFT (title + inline content) · MIDDLE (spacer / tabs) · RIGHT (controls)

  Props:
    title            — card title string (omit to skip the span)
    timestamp        — optional muted timestamp string (omit to skip)
    showControls     — set false to suppress the embedded CardControls cluster
    All CardControls props forwarded (isFullscreen, filter, …) — Collapse
    was removed app-wide (2026-10-13 header standardization sweep)

  Slots:
    prefix  — content rendered BEFORE the title in the left zone (e.g. an icon)
    left    — extra content after title + timestamp in the left zone
    middle  — content between left and right (tabs, spacer, etc.)
    right   — extra content before the CardControls cluster in the right zone

  Theming tokens (all optional — algo and public layouts each define a full set):
    --ch-bg                  background of the header bar        (default: transparent)
    --ch-border-bottom       bottom separator line               (default: none)
    --ch-padding             padding shorthand                   (default: 0)
    --ch-gap                 gap between left-zone items         (default: 0.4rem)
    --ch-title-font-family   font family for the title span      (default: inherit)
    --ch-title-size          font-size for the title span        (default: var(--fs-sm, 0.6rem))
    --ch-title-weight        font-weight for the title span      (default: 700)
    --ch-title-color         color for the title span            (default: var(--algo-slate, #ffffff))
    --ch-title-letter-spacing letter-spacing for the title span  (default: 0.04em)
    --ch-title-transform     text-transform for the title span   (default: uppercase)
    --ch-ts-size             font-size for the timestamp span    (default: var(--fs-md, 0.65rem))
    --ch-ts-color            color for the timestamp span        (default: var(--c-muted, #7e97b8))
-->
<script>
  import CardControls from '$lib/CardControls.svelte';

  /** @typedef {import('svelte').Snippet} Snippet */

  let {
    title = '',
    timestamp = null,
    // undefined (not false) by default — the reserved-width spinner slot
    // below only mounts when a consumer explicitly wires `loading`, so
    // the ~18 CardHeader call sites that never pass this prop keep their
    // original layout (no permanent +10px/+gap shift on the left zone).
    // Consumers that DO pass loading (true/false) get the always-mounted,
    // visibility-toggled slot so flipping loading causes zero layout shift.
    loading = undefined,
    // CardControls props — all forwarded
    isFullscreen = $bindable(false),
    filter = $bindable(''),
    cardId = '',
    label = '',
    onRefresh = null,
    refreshLoading = $bindable(false),
    showSearch = true,
    refreshAlwaysVisible = false,
    onDownload = null,
    showControls = true,
    detectOverflow = true,
    hideFullscreen = false,
    /** @type {Snippet | undefined} */ prefix = undefined,
    /** @type {Snippet | undefined} */ left = undefined,
    /** @type {Snippet | undefined} */ middle = undefined,
    /** @type {Snippet | undefined} */ right = undefined,
  } = $props();

  let _hasOverflow = $state(false);
  let _overflowAnchorEl = $state(/** @type {HTMLElement | null} */ (null));

  $effect(() => {
    if (!detectOverflow || !_overflowAnchorEl?.parentElement?.parentElement) return;
    const el = _overflowAnchorEl.parentElement.parentElement;

    const checkOverflow = () => {
      if (el.scrollHeight > el.clientHeight + 4 || el.scrollWidth > el.clientWidth + 4) {
        _hasOverflow = true; return;
      }
      const agRows = /** @type {HTMLElement | null} */ (el.querySelector('.ag-center-cols-container'));
      const agViewport = el.querySelector('.ag-body-viewport');
      if (agRows && agViewport && agRows.offsetHeight > agViewport.clientHeight + 4) {
        _hasOverflow = true; return;
      }
      _hasOverflow = false;
    };

    const obs = new ResizeObserver(checkOverflow);
    obs.observe(el);
    const agRows = el.querySelector('.ag-center-cols-container');
    if (agRows) obs.observe(agRows);
    let mutObs = null;
    if (!agRows) {
      mutObs = new MutationObserver(() => {
        const newAgRows = el.querySelector('.ag-center-cols-container');
        if (newAgRows) { obs.observe(newAgRows); mutObs?.disconnect(); mutObs = null; checkOverflow(); }
      });
      mutObs.observe(el, { childList: true, subtree: true });
    }
    checkOverflow();
    return () => { obs.disconnect(); mutObs?.disconnect(); };
  });
</script>

<div class="card-header" role="presentation">
  <span class="ch-overflow-anchor" aria-hidden="true" bind:this={_overflowAnchorEl} style="position:absolute;pointer-events:none;"></span>
  <div class="ch-left">
    {@render prefix?.()}
    {#if title}<span class="ch-title algo-label-decor">{title}</span>{/if}
    {#if loading !== undefined}
      <!-- Reserved-width slot (matches SVG's own 10px), always mounted
           once a consumer wires `loading` — toggling loading only flips
           visibility/opacity inside this fixed box, so nothing after it
           (timestamp, left snippet) shifts. Spin animation lives on the
           .on class so headers with loading=false never run the
           infinite-spin keyframe. -->
      <span class="ch-spin-slot" class:on={loading} aria-hidden="true">
        <svg class="ch-spin" viewBox="0 0 16 16" width="10" height="10">
          <circle cx="8" cy="8" r="5.5"
            fill="none" stroke="currentColor" stroke-width="2"
            stroke-linecap="round"
            stroke-dasharray="9 30" />
        </svg>
      </span>
    {/if}
    {#if timestamp}<span class="ch-ts">{timestamp}</span>{/if}
    {@render left?.()}
  </div>

  {#if middle}<span class="ch-sep" aria-hidden="true"></span>{/if}
  <div class="ch-middle">
    {@render middle?.()}
  </div>

  <div class="ch-right">
    {#if showControls}
      <CardControls
        bind:isFullscreen
        bind:filter
        {cardId}
        {label}
        {onRefresh}
        bind:refreshLoading
        {showSearch}
        {refreshAlwaysVisible}
        {onDownload}
        hideFullscreen={hideFullscreen || (detectOverflow && !_hasOverflow && !isFullscreen)}
      />
    {/if}
    {@render right?.()}
  </div>
</div>

<style>
  .card-header {
    display: flex;
    align-items: center;
    gap: var(--ch-gap, 0.4rem);
    padding: var(--ch-padding, 0);
    background: var(--ch-bg, transparent);
    border-bottom: var(--ch-border-bottom, none);
    flex-shrink: 0;
  }
  .ch-left {
    display: flex;
    align-items: center;
    gap: var(--ch-gap, 0.4rem);
    flex: 0 1 auto;
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .ch-left::-webkit-scrollbar { display: none; }
  .ch-title {
    font-family: var(--ch-title-font-family, inherit);
    font-size: var(--ch-title-size, var(--fs-sm, 0.6rem));
    font-weight: var(--ch-title-weight, 700);
    color: var(--ch-title-color, var(--algo-slate, #ffffff));
    letter-spacing: var(--ch-title-letter-spacing, 0.04em);
    text-transform: var(--ch-title-transform, uppercase);
    white-space: nowrap;
    /* 2026-10-13 (CardHeader sweep round 3, Part A) — border-left +
       left gap match MarketPulse's own `.mp-bucket-label` reference
       pattern (border-left: 3px solid currentColor; padding: …
       0.35rem), so a plain CardHeader title reads as a LABEL, not a
       clickable tab — it used to sit flush against `.ch-left`'s edge
       with no border at all. `currentColor` means the bar always
       tracks whatever color is active (this neutral default, or a
       per-panel --ch-title-color override) with no second color to
       keep in sync. Default fallback changed from --c-action (amber)
       to --algo-slate (white) in the same fix: amber collided with
       AlgoTabs' own default untagged-tab color (`tab.color ?? 'amber'`
       in AlgoTabs.svelte), making a plain header title visually read
       as "just another tab." This component-level fallback alone was
       dead code for every algo-app CardHeader — (algo)/+layout.svelte's
       own body-level --ch-title-color override was ALSO amber and had
       to be changed in the same commit, since a body-level custom
       property always wins over this one's var() fallback. The public
       layout's `.pub-viewport` override (navy, #1a2744) already wins
       over both and is unaffected either way.
       2026-10-13 reusability sweep Part A: the border-left/padding-left
       pairing itself moved to the shared `.algo-label-decor` class
       (app.css) — applied via the markup's class list above; this
       rule still owns `color` (and everything else). */
  }
  .ch-ts {
    font-size: var(--ch-ts-size, var(--fs-md, 0.65rem));
    color: var(--ch-ts-color, var(--c-muted, #7e97b8));
    white-space: nowrap;
  }
  .ch-sep {
    width: 1px;
    align-self: stretch;
    /* 2026-10-13 (CardHeader sweep round 2, item 2) — was var(--sep-color)
       (rgba(126,151,184,0.10)), half --card-divider's alpha and a token
       shared across dozens of unrelated low-emphasis dividers app-wide
       (table rows, grid cells, dropdown separators) — do NOT bump
       --sep-color itself, that would silently change all of those too.
       Switched to --card-divider instead: the SAME token the header's
       own bottom border already uses, so the vertical rule between the
       title and the rest of the header reads with internal consistency,
       zero blast radius elsewhere.
       Fallback to var(--sep-color) is defense-in-depth, not load-
       bearing today: some CardHeader mounts (ActivityLogModal's portal
       target — `$lib/portal.js` moves the node to a document.body
       child that is a SIBLING of `.algo-viewport.card-theme-dark`/
       `.card-theme-cream`, not a descendant) sit outside the DOM scope
       where app.css's `.card-theme-dark`/`.card-theme-cream` declare
       --card-divider — but `(algo)/+layout.svelte`'s own `:global(body)`
       block ALSO declares --card-divider directly (see that file's
       comment, added the same day this fix landed, for the full root-
       cause writeup — the SAME gap silently broke CardHeader's own
       bottom border everywhere, not just this divider), so body-level
       inheritance already carries a valid value into portaled mounts
       too. This fallback only matters if some future CardHeader mount
       ever renders fully outside <body> (e.g. a detached node) — kept
       as a cheap safety net, not because it's currently exercised. */
    background: var(--card-divider, var(--sep-color));
    flex-shrink: 0;
    margin: var(--sep-margin);
  }
  .ch-middle {
    flex: 1 1 0;
    display: flex;
    align-items: center;
    min-width: 0;
    overflow-x: auto;
    overflow-y: visible;
    scrollbar-width: none;
  }
  .ch-middle::-webkit-scrollbar { display: none; }
  .ch-right {
    display: flex;
    align-items: center;
    gap: 0.3rem;
    flex-shrink: 0;
  }
  /* Fixed-size wrapper (matches the SVG's own 10px) so toggling `loading`
     only flips visibility inside a slot of constant width — the title/
     timestamp/left-snippet content after it never shifts. */
  .ch-spin-slot {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 10px;
    height: 10px;
    flex-shrink: 0;
    visibility: hidden;
    opacity: 0;
  }
  .ch-spin-slot.on {
    visibility: visible;
    opacity: 1;
  }
  .ch-spin {
    color: var(--c-action, #fbbf24);
    flex-shrink: 0;
  }
  /* Animation lives on the .on state only — a CardHeader sitting at
     loading=false must not run an infinite-spin keyframe forever. */
  .ch-spin-slot.on .ch-spin {
    animation: rbq-spin 0.9s linear infinite;
  }
</style>
