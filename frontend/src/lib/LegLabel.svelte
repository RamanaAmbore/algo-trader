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

<!--
  Hyphen-separated Dhan-style display:
    NIFTY-26JUN-22000-CE      (monthly option)
    NIFTY-25APR24-22000-CE    (weekly option)
    NIFTY-26JUN-FUT           (future)
  The component still renders each token in its own colored <span> so
  the strike + CE/PE accents survive — only the inter-token visual
  cue switches from "[brackets]" / spaces to a "-" delimiter for
  parity with how Dhan + the broker app show F&O tradingsymbols.
-->
<span class="leg-label">
  <span class="leg-root">{parsed.root}</span>
  {#if parsed.month}
    <span class="leg-sep">-</span>
    <span class="leg-month">{parsed.monthLabel}</span>
  {/if}
  {#if parsed.strike != null}
    <span class="leg-sep">-</span>
    <span class="leg-strike">{Number.isInteger(parsed.strike) ? parsed.strike.toLocaleString('en-IN') : parsed.strike}</span>
  {/if}
  {#if parsed.optType}
    <span class="leg-sep">-</span>
    <span class="leg-type leg-type-{parsed.optType.toLowerCase()}">{parsed.optType}</span>
  {:else if parsed.kind === 'fut'}
    <span class="leg-sep">-</span>
    <span class="leg-type leg-type-fut">FUT</span>
  {/if}
</span>

<style>
  .leg-label {
    display: inline-flex;
    align-items: baseline;
    gap: 0;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: inherit;
    line-height: inherit;
  }
  .leg-sep {
    color: var(--algo-muted);
    opacity: 0.55;
    margin: 0 0.1rem;
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
