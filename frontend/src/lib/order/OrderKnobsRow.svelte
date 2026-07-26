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

</style>
