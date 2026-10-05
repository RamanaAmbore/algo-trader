<!--
  ChartPopup.svelte

  Canonical hover popup for every hand-rolled SVG chart. Pairs with
  ChartCrosshair: the crosshair draws the line and dot inside the SVG, this
  draws the HTML popup at the same point. Content is the caller's snippet, so
  each chart shows its own values while the placement and look stay identical.

  RENDER CONTRACT: place inside a position:relative wrapper whose box matches
  the SVG viewBox (width 100%, aspect ratio of viewW:viewH). x and y are in
  viewBox units. The popup flips to the left of the point past flipAt.
  pointer-events are off, so it never blocks the chart's own pointer handlers.
-->
<script>
  let {
    /** @type {number | null} */
    x = null,
    /** @type {number | null} */
    y = null,
    /** @type {number} */
    viewW,
    /** @type {number} */
    viewH,
    /** Fraction of viewW past which the popup flips to the left of the point. */
    flipAt = 0.65,
    /** Viewbox y used when y is null (vertical-only charts). */
    yFallback = 0,
    /** @type {import('svelte').Snippet} */
    children,
  } = $props();

  const flip = $derived(x != null && x > viewW * flipAt);
</script>

{#if x != null}
  <div class="chart-tooltip chart-popup"
       style="left: {(x / viewW) * 100}%; top: {((y ?? yFallback) / viewH) * 100}%; transform: translate({flip ? 'calc(-100% - 10px)' : '10px'}, {y == null ? '0' : '-50%'});">
    {@render children?.()}
  </div>
{/if}

<style>
  .chart-popup {
    pointer-events: none;
    white-space: nowrap;
  }
</style>
