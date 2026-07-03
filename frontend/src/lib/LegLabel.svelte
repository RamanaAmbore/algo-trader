<script>
  // LegLabel — renders a Kite tradingsymbol as structured ROOT [MONTH] STRIKE TYPE
  // chips. Presentation-only; never mutates the underlying symbol string.
  //
  // Props:
  //   sym      — tradingsymbol to render (e.g. "NIFTY26JUN22000CE")
  //   exchange — optional exchange (MCX / CDS) for virtual-root display.
  //              When provided for MCX/CDS futures, shows the virtual root
  //              label (e.g. "CRUDEOIL" or "CRUDEOIL • NEXT") instead of
  //              the raw contract form "CRUDEOIL-16JUN26-FUT".
  //   compact  — when true, omits [ ] brackets around the month token;
  //              intended for narrow basket-pill contexts.

  import { decomposeSymbol, composeMonthToken } from '$lib/data/decomposeSymbol.js';
  import { getInstrument, instrumentsCacheVersion } from '$lib/data/instruments.js';
  import { rootOfLabel } from '$lib/data/rootOf.js';

  /** @type {{ sym: string, exchange?: string, compact?: boolean }} */
  let { sym, exchange = '', compact = false } = $props();

  // Virtual root label for MCX/CDS futures.
  // When the instruments cache has seeded the root map, returns "CRUDEOIL"
  // or "CRUDEOIL • NEXT" — otherwise falls back to null (use normal chips).
  const _virtualLabel = $derived.by(() => {
    // Depend on instrumentsCacheVersion so we re-check after cache load.
    /* eslint-disable-next-line @typescript-eslint/no-unused-expressions */
    $instrumentsCacheVersion;
    const eUp = (exchange || '').toUpperCase();
    if (eUp !== 'MCX' && eUp !== 'CDS') return null;
    const rl = rootOfLabel(sym || '', eUp);
    // rootOfLabel returns the raw contract for far-month and non-virtual —
    // only use the virtual label when it differs from the raw symbol.
    return rl !== (sym || '') ? rl : null;
  });

  const parsed = $derived(decomposeSymbol(sym));

  // Month token — single source of truth lives in `composeMonthToken`
  // (decomposeSymbol.js). Shared with formatSymbol() so the two
  // formatters can never drift apart again. DD-Mon-YY format —
  // year kept for upcoming US-options support (e.g. SPX 19DEC25 vs
  // SPX 18DEC26 on the same chain):
  //   Monthly:    "16JUN26"  (DD + Mon + YY, day from instruments cache)
  //   Weekly :    "24APR25"  (DD-Mon-YY from baked-in symbol)
  //   Cold cache: "JUN26"    (Mon-YY, day pending cache load)
  //
  // Reactive trigger: `getInstrument` reads a plain module-level Map
  // that Svelte can't see. Without subscribing to a signal that bumps
  // when the cache populates, this `$derived` would compute once
  // pre-cache (→ bare "26JUN") and never re-fire. The cache-version
  // read inside the derivation pulls that signal in as a dependency.
  const monthDisplay = $derived.by(() => {
    // Read the store value — the line below is intentionally a dep, not dead code.
    /* eslint-disable-next-line @typescript-eslint/no-unused-expressions */
    $instrumentsCacheVersion;
    return composeMonthToken(parsed, getInstrument(sym)?.x || null);
  });
</script>

<!--
  Hyphen-separated Dhan-style display:
    NIFTY-26JUN-22000-CE      (monthly option)
    NIFTY-25APR24-22000-CE    (weekly option)
    NIFTY-26JUN-FUT           (future)
  For MCX/CDS futures when `exchange` is provided, shows the virtual root:
    CRUDEOIL                  (front-month)
    CRUDEOIL • NEXT           (back-month)
    CRUDEOIL-16JUN26-FUT      (far-month, pass-through)
-->
{#if _virtualLabel}
  <!-- Virtual MCX/CDS root label — single chip, no expiry suffix needed -->
  <span class="leg-label">
    <span class="leg-root">{_virtualLabel}</span>
  </span>
{:else}
  <span class="leg-label">
    <span class="leg-root">{parsed.root}</span>
    {#if monthDisplay}
      <span class="leg-sep">-</span>
      <span class="leg-month">{monthDisplay}</span>
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
{/if}

<style>
  .leg-label {
    display: inline-flex;
    align-items: baseline;
    gap: 0;
    font-family: var(--font-numeric);
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
    color: var(--c-action);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }

  .leg-type {
    font-weight: 700;
  }

  .leg-type-ce  { color: var(--c-long); }
  .leg-type-pe  { color: var(--c-short); }
  .leg-type-fut { color: var(--c-info); }
</style>
