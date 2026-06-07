<script>
  // LegLabel — renders a Kite tradingsymbol as structured ROOT [MONTH] STRIKE TYPE
  // chips. Presentation-only; never mutates the underlying symbol string.
  //
  // Props:
  //   sym      — tradingsymbol to render (e.g. "NIFTY26JUN22000CE")
  //   compact  — when true, omits [ ] brackets around the month token;
  //              intended for narrow basket-pill contexts.

  import { decomposeSymbol } from '$lib/data/decomposeSymbol.js';

  /** @type {{ sym: string, compact?: boolean }} */
  let { sym, compact = false } = $props();

  const parsed = $derived(decomposeSymbol(sym));
</script>

<span class="leg-label">
  <span class="leg-root">{parsed.root}</span>
  {#if parsed.month}
    <span class="leg-month">
      {#if compact}{parsed.monthLabel}{:else}[{parsed.monthLabel}]{/if}
    </span>
  {/if}
  {#if parsed.strike != null}
    <span class="leg-strike">{Number.isInteger(parsed.strike) ? parsed.strike.toLocaleString('en-IN') : parsed.strike}</span>
  {/if}
  {#if parsed.optType}
    <span class="leg-type leg-type-{parsed.optType.toLowerCase()}">{parsed.optType}</span>
  {:else if parsed.kind === 'fut'}
    <span class="leg-type leg-type-fut">FUT</span>
  {/if}
</span>

<style>
  .leg-label {
    display: inline-flex;
    align-items: baseline;
    gap: 0.25rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: inherit;
    line-height: inherit;
  }

  .leg-root {
    color: var(--algo-slate);
    font-weight: 600;
  }

  .leg-month {
    color: var(--algo-muted);
    font-weight: 400;
  }

  .leg-strike {
    color: #fbbf24;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .leg-type {
    font-weight: 700;
  }

  .leg-type-ce  { color: #4ade80; }
  .leg-type-pe  { color: #f87171; }
  .leg-type-fut { color: #22d3ee; }
</style>
