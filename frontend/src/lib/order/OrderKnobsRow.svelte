<script>
  // Five selector knobs extracted from OrderTicket.svelte:
  // Type · Product · Exchange · Variety · Validity
  //
  // All five live inside the parent's .ot-row-knobs flex container as
  // sibling knob slots — this component renders a transparent fragment
  // (no wrapper element) so the flex layout is unaffected.
  //
  // Two-way bindings via $bindable(): type, product, variety, validity.
  // Exchange is unidirectional: read via `exchange` prop, write via
  // `onExchangeChange` callback so the parent can also set
  // _exchangeTouched alongside the value update.

  import Select from '$lib/Select.svelte';

  let {
    type      = $bindable(),
    product   = $bindable(),
    variety   = $bindable(),
    validity  = $bindable(),
    exchange,
    onExchangeChange,    // (v: string) => void
    disabled      = false,
    productOptions = [],
    exchangeOptions = [],
  } = $props();
</script>

<div class="ot-knob">
  <label class="ot-label" for="ot-type-sel">Type</label>
  <Select id="ot-type-sel"
          bind:value={type}
          ariaLabel="Order type"
          {disabled}
          options={[
            { value: 'MARKET', label: 'MARKET' },
            { value: 'LIMIT',  label: 'LIMIT'  },
            { value: 'SL',     label: 'SL'     },
            { value: 'SL-M',   label: 'SL-M'   },
          ]} />
</div>
<div class="ot-knob">
  <label class="ot-label" for="ot-product-sel">Product</label>
  <Select id="ot-product-sel"
          bind:value={product}
          ariaLabel="Product"
          {disabled}
          options={productOptions.map(p => ({ value: p, label: p }))} />
</div>
<!-- Exchange — operator picks for dual-listed symbols (IFCI on
     NSE+BSE, RELIANCE futures on NFO+BFO, etc.). Read-only chip
     for single-listing instruments (RELIANCE futures on NFO only,
     CRUDEOIL on MCX only) so the operator can't pick an exchange
     the symbol doesn't trade on. Dropdown only when there are
     ≥2 actual listings; defaults to the first (NSE / NFO) until
     the operator picks otherwise. -->
<div class="ot-knob">
  <label class="ot-label" for="ot-exchange-sel">Exchange</label>
  {#if exchangeOptions.length > 1}
    <Select id="ot-exchange-sel"
            value={exchange}
            ariaLabel="Exchange"
            {disabled}
            onValueChange={(v) => onExchangeChange?.(v)}
            options={exchangeOptions.map(e => ({ value: e, label: e }))} />
  {:else}
    <div class="ot-exchange-locked" id="ot-exchange-sel"
         title="This symbol trades on only one exchange — no override available.">
      {exchangeOptions[0] || exchange || '—'}
    </div>
  {/if}
</div>
<div class="ot-knob">
  <label class="ot-label" for="ot-variety-sel">Variety</label>
  <Select id="ot-variety-sel"
          bind:value={variety}
          ariaLabel="Variety"
          {disabled}
          options={[
            { value: 'regular', label: 'REG' },
            { value: 'amo',     label: 'AMO' },
            { value: 'co',      label: 'CO'  },
          ]} />
</div>
<div class="ot-knob">
  <label class="ot-label" for="ot-validity-sel">Validity</label>
  <Select id="ot-validity-sel"
          bind:value={validity}
          ariaLabel="Validity"
          {disabled}
          options={[
            { value: 'DAY', label: 'DAY' },
            { value: 'IOC', label: 'IOC' },
          ]} />
</div>

<style>
  /* Duplicated from OrderTicket — both files need their own scoped copy
     because Svelte CSS is component-scoped. The parent retains its copy
     for the Side and Strategy knobs that remain there. */
  .ot-label {
    display: block;
    font-size: var(--fs-sm);
    color: var(--c-action);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    margin-bottom: 0.18rem;
    opacity: 0.85;
  }

  .ot-knob {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
    flex: 1 1 5rem;
    min-width: 5rem;
  }

  /* Read-only exchange chip — rendered when the symbol trades on a
     single exchange. Height-matches the Select chip next to it (1.55rem)
     so the row stays aligned; muted bg + slightly faded text reads as
     "informational, not editable" without the operator confusing it
     for a broken / disabled dropdown. */
  .ot-exchange-locked {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 1.55rem;
    padding: 0 0.6rem;
    box-sizing: border-box;
    border-radius: 3px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.10);
    color: rgba(200, 216, 240, 0.80);
    font-family: var(--font-numeric);
    font-size: var(--fs-lg);
    font-weight: 600;
    letter-spacing: 0.04em;
    cursor: default;
    user-select: none;
  }
</style>
