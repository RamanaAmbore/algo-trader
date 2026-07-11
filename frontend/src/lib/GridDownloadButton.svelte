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
  } = $props();
</script>

{#if onClick != null}
  <button
    type="button"
    class="grid-download-btn"
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
    margin: 0 0 0 auto;
    background: var(--algo-cyan-bg);
    border: 1px solid var(--algo-cyan-border);
    border-radius: 3px;
    color: var(--c-info);
    cursor: pointer;
    transition: background 0.12s, color 0.12s, border-color 0.12s;
    flex-shrink: 0;
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
    When the search input is visible just before this button, or when the
    search button itself precedes this button, remove the auto-margin so
    the cluster stays tight. The sibling `.grid-search-btn + .grid-download-btn`
    selector cancels margin-left:auto when Search is directly adjacent.
  */
  :global(.grid-search-btn) + .grid-download-btn,
  :global(.grid-search-input) + .grid-download-btn {
    margin-left: 0;
  }
</style>
