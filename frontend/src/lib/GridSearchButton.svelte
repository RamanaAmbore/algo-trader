<!--
  GridSearchButton — inline-open quick-filter for a card's grid.

  Renders a small cyan-400 magnifying-glass icon (same palette as
  CollapseButton / FullscreenButton). Click expands it inline into a
  text input; Esc collapses it. Bind `filter` to whatever the host's
  grid consumes — typically ag-Grid's `quickFilterText` via
  `setGridOption('quickFilterText', filter)`.

  Usage:
    <script>
      let _filter = $state('');
      $effect(() => {
        if (myGrid) myGrid.setGridOption('quickFilterText', _filter);
      });
    </script>

    <GridSearchButton bind:filter={_filter} label="Holdings" />
    <CollapseButton ... />
    <FullscreenButton ... />

  Place BEFORE the Collapse / Default / Fullscreen trio so the search
  affordance reads as a sibling control without disturbing the canonical
  collapse-restore-fullscreen ordering.
-->
<script>
  let {
    /** bindable filter text — empty string = no filter. */
    filter = $bindable(''),
    /** Card name for a11y / tooltip. */
    label = 'grid',
    /** Optional placeholder text inside the expanded input. */
    placeholder = 'Filter symbol…',
  } = $props();

  let _open = $state(false);
  /** @type {HTMLInputElement | null} */
  let _input = $state(null);

  function _toggle() {
    _open = !_open;
    if (_open) {
      // Focus on next tick so the input is mounted.
      queueMicrotask(() => _input?.focus());
    } else {
      filter = '';
    }
  }

  function _onKey(/** @type {KeyboardEvent} */ ev) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      _open = false;
      filter = '';
    }
  }

  function _onBlur() {
    // Auto-collapse only when the input was left empty. Operator who
    // typed a filter keeps it pinned open so they can refine.
    if (!filter) _open = false;
  }
</script>

{#if _open}
  <input
    bind:this={_input}
    bind:value={filter}
    type="text"
    class="grid-search-input"
    {placeholder}
    aria-label={`Filter ${label} by symbol`}
    onkeydown={_onKey}
    onblur={_onBlur} />
{/if}
<button type="button"
        class="grid-search-btn"
        class:on={_open}
        title={_open ? `Close ${label} filter` : `Filter ${label} by symbol`}
        aria-label={`Toggle ${label} symbol filter`}
        aria-pressed={_open}
        onclick={_toggle}>
  <!-- Magnifying glass icon — same stroke weight as the chevron in
       CollapseButton so the cluster reads as one consistent family. -->
  <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true">
    <circle cx="7" cy="7" r="4"
      fill="none" stroke="currentColor" stroke-width="1.7" />
    <path d="M10 10l3 3"
      fill="none" stroke="currentColor" stroke-width="1.8"
      stroke-linecap="round" />
  </svg>
</button>

<style>
  .grid-search-btn {
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
  .grid-search-btn:hover,
  .grid-search-btn.on {
    background: rgba(34, 211, 238, 0.26);
    border-color: rgba(34, 211, 238, 0.85);
    color: #67e8f9;
  }
  .grid-search-btn:focus-visible {
    outline: 2px solid rgba(34, 211, 238, 0.65);
    outline-offset: 1px;
  }

  /* Inline input — sized to match the button height so the
     header strip stays single-row. Cyan-tinted border + bg matches
     the palette without competing with the other knob colours. */
  .grid-search-input {
    height: 1.4rem;
    width: 8rem;
    margin: 0 0.3rem 0 auto;
    padding: 0 0.4rem;
    background: var(--c-info-08);
    border: 1px solid rgba(34, 211, 238, 0.55);
    border-radius: 3px;
    color: var(--algo-slate);
    font-family: var(--font-numeric);
    font-size: var(--fs-sm);
    box-sizing: border-box;
    flex-shrink: 0;
  }
  .grid-search-input::placeholder {
    color: rgba(126, 151, 184, 0.7);
  }
  .grid-search-input:focus {
    outline: none;
    border-color: rgba(34, 211, 238, 0.85);
    background: var(--c-info-14);
  }

  /* When the search input is visible just before the trio, the
     button itself loses its margin-left:auto — the input takes the
     spacer role. */
  .grid-search-input + .grid-search-btn {
    margin-left: 0;
  }
</style>
