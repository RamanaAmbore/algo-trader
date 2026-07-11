<script>
  /**
   * QtyInput — reusable lots/qty stepper widget.
   *
   * Lots mode  (_lotSize > 0 && !isEquity):  [−] [N] [+] × {lotSize} = {qty} qty
   * Qty mode   (fallback equity / no lot):   [−] [N] [+]
   *
   * `lots` and `qty` are $bindable — parent reads them directly.
   * `onTouch` is called whenever the operator manually edits the lots
   * value (either via stepper or direct input) so the parent can set
   * its own _lotsTouched flag.
   */

  /** @type {{ lots?: number, qty?: number, lotSize?: number, isEquity?: boolean, disabled?: boolean, onTouch?: (() => void) | null }} */
  let {
    lots     = $bindable(1),
    qty      = $bindable(0),
    lotSize  = 0,
    isEquity = false,
    disabled = false,
    onTouch  = null,
  } = $props();

  function stepLots(/** @type {number} */ delta) {
    lots = Math.max(1, Math.floor((Number(lots) || 1) + delta));
    onTouch?.();
  }

  function stepQty(/** @type {number} */ delta) {
    qty = Math.max(1, (Number(qty) || 0) + delta);
  }
</script>

{#if lotSize > 0 && !isEquity}
  <label class="ot-label" for="ot-lots">Lots</label>
  <div class="ot-lots-row">
    <button type="button" class="ot-lots-step"
            onclick={() => stepLots(-1)}
            disabled={lots <= 1 || disabled}
            aria-label="Decrease lots">−</button>
    <input id="ot-lots" type="number"
           class="ot-input ot-num ot-lots-input"
           step="1" min="1"
           bind:value={lots}
           {disabled}
           oninput={() => { onTouch?.(); }}
           onblur={() => { lots = Math.max(1, Number(lots) || 1); }}
           aria-label="Lots" />
    <button type="button" class="ot-lots-step"
            onclick={() => stepLots(1)}
            {disabled}
            aria-label="Increase lots">+</button>
    <span class="ot-meta">× {lotSize} = {qty} qty</span>
  </div>
{:else}
  <label class="ot-label" for="ot-qty">Qty</label>
  <div class="ot-lots-row">
    <button type="button" class="ot-lots-step"
            onclick={() => stepQty(-1)}
            disabled={!qty || qty <= 1 || disabled}
            aria-label="Decrease qty">−</button>
    <input id="ot-qty" type="number"
           class="ot-input ot-num ot-lots-input"
           step="1" min="1"
           bind:value={qty}
           {disabled}
           onblur={() => { qty = Math.max(1, Number(qty) || 1); }}
           aria-label="Qty" />
    <button type="button" class="ot-lots-step"
            onclick={() => stepQty(1)}
            {disabled}
            aria-label="Increase qty">+</button>
  </div>
{/if}

<style>
  /* Section-header label — amber, uppercase, matches the form
     structure cueing in the parent OrderTicket. */
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

  /* [−] [1 ▼] [+] (× 50 = 50) — lots-driven Qty UI. Sits inline on
     a single row; nowrap so the +/− and the input can never
     break onto two lines on narrow viewports. Height pinned to
     1.7rem to match the .ot-side-toggle so the [−] N [+] glyphs
     and the BUY/SELL pill share the same y-baseline + y-centre. */
  .ot-lots-row {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    flex-wrap: nowrap;
    height: 1.7rem;
  }

  /* Editable [−][N][+] input — sits between the two stepper buttons.
     Narrow but readable; same height as the steppers so the trio
     reads as one control. */
  .ot-lots-input {
    width: 3.2rem;
    height: 1.7rem;
    text-align: center;
    padding: 0 0.25rem;
    -moz-appearance: textfield;
    appearance: textfield;
  }
  .ot-lots-input::-webkit-outer-spin-button,
  .ot-lots-input::-webkit-inner-spin-button {
    -webkit-appearance: none;
    appearance: none;
    margin: 0;
  }

  .ot-lots-step {
    width: 1.7rem;
    height: 1.7rem;
    padding: 0;
    border-radius: 3px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
    font-family: monospace;
    font-size: var(--fs-xl);
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    flex: 0 0 auto;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    /* Rapid taps on iOS Safari otherwise get swallowed by the
       double-tap-to-zoom gesture — manipulation lets every tap
       through cleanly. user-select: none prevents accidental text
       selection between fast taps. */
    touch-action: manipulation;
    -webkit-user-select: none;
    user-select: none;
  }
  .ot-lots-step:hover:not(:disabled) {
    background: var(--c-action-22);
    border-color: rgba(251,191,36,0.75);
  }
  .ot-lots-step:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  /* Shared input base — dark field with amber border, monospace. */
  .ot-input {
    width: 100%;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 3px;
    padding: 0.3rem 0.45rem;
    color: var(--algo-slate);
    font-size: var(--fs-lg);
    font-family: monospace;
  }
  .ot-input:focus { outline: none; border-color: var(--c-action); }
  .ot-num { text-align: right; }

  /* "× {lotSize} = {qty} qty" annotation */
  .ot-meta { font-size: var(--fs-md); color: var(--text-muted); }
</style>
