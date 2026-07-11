<!--
  GridDownloadButton — one-click CSV download trigger for a card's grid.

  Renders a small cyan-400 down-arrow icon (same palette as GridSearchButton /
  CollapseButton / FullscreenButton). Click immediately invokes `onClick`; no
  expanded state. The host component (CardControls) owns the actual gridApi call
  — this component is purely a styled affordance.

  Usage:
    <GridDownloadButton
      onClick={() => myGrid?.exportDataAsCsv({ fileName: 'positions.csv' })}
      label="Positions"
    />

  Hides itself when `onClick` is null so CardControls can always render it
  unconditionally and it simply disappears on non-grid cards.
-->
<script>
  let {
    /** Callback invoked on click. Pass null to hide the button entirely. */
    onClick = null,
    /** Card name for a11y / tooltip. */
    label = 'grid',
    /**
     * When true (default), applies `margin-left: auto` so the button
     * self-positions to the far right in a flex row. Set false when the
     * parent flex container (CardControls spacer, caption flex, etc.)
     * already handles right-alignment — prevents competing auto-margins
     * from fighting for free space in fullscreen or resized layouts.
     */
    autoMargin = true,
  } = $props();
</script>

{#if onClick != null}
  <button
    type="button"
    class="grid-download-btn"
    class:with-auto-margin={autoMargin}
    title="Download {label} as CSV"
    aria-label="Download {label} as CSV"
    onclick={onClick}>
    <!--
      Down-arrow-into-tray icon — same stroke weight (1.7) as the magnifying
      glass in GridSearchButton and the chevron in CollapseButton so the
      cluster reads as one consistent family.
    -->
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
      <!-- vertical stem -->
      <line x1="8" y1="2" x2="8" y2="10"
        stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
      <!-- arrowhead -->
      <polyline points="5,7.5 8,11 11,7.5"
        fill="none" stroke="currentColor" stroke-width="1.7"
        stroke-linecap="round" stroke-linejoin="round" />
      <!-- tray base -->
      <line x1="3" y1="13.5" x2="13" y2="13.5"
        stroke="currentColor" stroke-width="1.7" stroke-linecap="round" />
    </svg>
  </button>
{/if}

<style>
  .grid-download-btn {
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
  /* Apply auto-margin only when the parent hasn't opted out via
     autoMargin={false}. Standalone use (PerformancePage, NavBreakdown
     caption) retains self-positioning. CardControls passes false so
     the spacer before it handles right-alignment without conflict. */
  .grid-download-btn.with-auto-margin {
    margin-left: auto;
  }
  .grid-download-btn:hover {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.85);
    color: #67e8f9;
  }
  .grid-download-btn:focus-visible {
    outline: 2px solid rgba(34, 211, 238, 0.65);
    outline-offset: 1px;
  }

  /*
    When the search input or search button directly precedes this button,
    cancel the auto-margin so the cluster stays tight. Only applies when
    `autoMargin` is true (i.e. the .with-auto-margin class is present);
    CardControls sets autoMargin={false} so these selectors are moot for
    the cluster case anyway.
  */
  :global(.grid-search-btn) + .grid-download-btn.with-auto-margin,
  :global(.grid-search-input) + .grid-download-btn.with-auto-margin {
    margin-left: 0;
  }
</style>
