<!--
  EmptyState — global "no data" message used by any algo card that
  collapses to a compact line when its grid/list is empty. Matches
  the FullscreenButton pattern (small, reusable, drop into any card).

  Usage:
    {#if rows.length === 0}
      <EmptyState message="No open positions" />
    {:else}
      <div bind:this={gridEl} class="ag-theme-algo …"></div>
    {/if}

  Or inline alongside a collapsing grid wrapper:
    <div class="…" class:is-empty={!rows.length}></div>
    {#if !rows.length}<EmptyState message="…" />{/if}

  Visual treatment: italic muted slate text, single line, centered,
  monospace font. Sits naturally inside the card body without
  competing with the card heading.
-->
<script>
  let {
    /** Message shown when the card has nothing to display. */
    message = 'No data',
    /** Variant: 'card' (default, ~2.4 rem tall) | 'compact' (smaller). */
    variant = 'card',
  } = $props();
</script>

<div class="es" class:es-compact={variant === 'compact'}
  role="status" aria-live="polite">
  {message}
</div>

<style>
  .es {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0.65rem 0.5rem;
    color: var(--algo-muted);
    font-family: ui-monospace, monospace;
    font-size: 0.68rem;
    letter-spacing: 0.05em;
    font-style: italic;
    text-align: center;
  }
  .es-compact {
    padding: 0.35rem 0.5rem;
    font-size: 0.62rem;
  }
</style>
