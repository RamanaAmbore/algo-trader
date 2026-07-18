<!--
  CardHeader — unified card title bar for algo + public surfaces.
  Theming via CSS custom properties defined in each layout.
  Three zones: LEFT (title + inline content) · MIDDLE (spacer / tabs) · RIGHT (controls)

  Props:
    title            — card title string (omit to skip the span)
    timestamp        — optional muted timestamp string (omit to skip)
    showControls     — set false to suppress the embedded CardControls cluster
    All CardControls props forwarded (isCollapsed, isFullscreen, filter, …)

  Slots:
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
    --ch-title-color         color for the title span            (default: var(--c-action, #fbbf24))
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
    // CardControls props — all forwarded
    isCollapsed = $bindable(false),
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
    /** @type {Snippet | undefined} */ left = undefined,
    /** @type {Snippet | undefined} */ middle = undefined,
    /** @type {Snippet | undefined} */ right = undefined,
  } = $props();
</script>

<div class="card-header">
  <div class="ch-left">
    {#if title}<span class="ch-title">{title}</span>{/if}
    {#if timestamp}<span class="ch-ts">{timestamp}</span>{/if}
    {@render left?.()}
  </div>

  {#if middle}<span class="ch-sep" aria-hidden="true"></span>{/if}
  <div class="ch-middle">
    {@render middle?.()}
  </div>

  <div class="ch-right">
    {@render right?.()}
    {#if showControls}
      <CardControls
        bind:isCollapsed
        bind:isFullscreen
        bind:filter
        {cardId}
        {label}
        {onRefresh}
        bind:refreshLoading
        {showSearch}
        {refreshAlwaysVisible}
        {onDownload}
      />
    {/if}
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
    flex-shrink: 0;
  }
  .ch-title {
    font-family: var(--ch-title-font-family, inherit);
    font-size: var(--ch-title-size, var(--fs-sm, 0.6rem));
    font-weight: var(--ch-title-weight, 700);
    color: var(--ch-title-color, var(--c-action, #fbbf24));
    letter-spacing: var(--ch-title-letter-spacing, 0.04em);
    text-transform: var(--ch-title-transform, uppercase);
    white-space: nowrap;
  }
  .ch-ts {
    font-size: var(--ch-ts-size, var(--fs-md, 0.65rem));
    color: var(--ch-ts-color, var(--c-muted, #7e97b8));
    white-space: nowrap;
  }
  .ch-sep {
    width: 1px;
    align-self: stretch;
    background: rgba(255,255,255,0.10);
    flex-shrink: 0;
    margin: 0.15rem 0;
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
    gap: 0.25rem;
    flex-shrink: 0;
  }
</style>
