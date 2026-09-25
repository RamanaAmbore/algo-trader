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

  import { qtyFmt } from '$lib/format';

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
    <span class="ot-qty-chip" title="Lots × lot size = total units sent to broker">= {qtyFmt(qty)} units</span>
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
     break onto two lines on narrow viewports. Height pinned to the
     shared --ctl-h control height (declared in OrderTicket.svelte
     .ot-modal / SymbolPanel.svelte .oes-modal) so the [−] N [+]
     glyphs and the Side toggle / Select controls share the same
     y-baseline + y-centre. Gap normalized to the ticket's standard
     row gap (was 1rem, an outlier against every other ~0.5rem gap
     in the form) — this also tightens the row's total width, which
     helps the control fit inside its 65%-width cell on narrow
     viewports. */
  .ot-lots-row {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: nowrap;
    height: var(--ctl-h, 1.9rem);
  }

  /* Editable [−][N][+] input — sits between the two stepper buttons.
     Narrow but readable; same height as the steppers so the trio
     reads as one control.
     Compound selector (.ot-input.ot-lots-input, not just
     .ot-lots-input) is load-bearing, not stylistic — found live while
     verifying the mobile SUSPECT items: this input carries BOTH
     `.ot-input` (width: 100%) and `.ot-lots-input` (width: 3.2rem)
     classes; as two separate one-class selectors they're equal
     specificity, so the cascade tie is broken by source order, and
     `.ot-input` (declared later, below) was silently winning —
     stretching the [N] field to ~100% of an indefinite flex-basis
     resolution (measured 160-340px depending on sibling content,
     instead of the intended ~51px). A compound selector is strictly
     more specific than either alone and wins regardless of source
     order or which mode (Lots/Qty) renders. */
  .ot-input.ot-lots-input {
    width: 3.2rem;
    height: var(--ctl-h, 1.9rem);
    text-align: center;
    padding: 0 0.25rem;
    -moz-appearance: textfield;
    appearance: textfield;
    box-sizing: border-box;
  }
  .ot-lots-input::-webkit-outer-spin-button,
  .ot-lots-input::-webkit-inner-spin-button {
    -webkit-appearance: none;
    appearance: none;
    margin: 0;
  }

  .ot-lots-step {
    width: 2rem;
    height: var(--ctl-h, 1.9rem);
    padding: 0;
    border-radius: 3px;
    border: 1px solid rgba(251,191,36,0.45);
    background: rgba(251,191,36,0.10);
    color: var(--c-action);
    font-family: var(--font-numeric);
    font-size: var(--fs-xl);
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    flex: 0 0 auto;
    box-sizing: border-box;
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

  /* Shared input base — dark field with amber border. */
  .ot-input {
    width: 100%;
    background: #1d2a44;
    border: 1px solid rgba(251,191,36,0.25);
    border-radius: 3px;
    padding: 0.3rem 0.45rem;
    color: var(--algo-slate);
    font-size: var(--ctl-fs, var(--fs-lg));
    font-family: var(--font-numeric);
    box-sizing: border-box;
  }
  .ot-input:focus { outline: none; border-color: var(--c-action); }
  .ot-num { text-align: right; }

  /* "= N units" conversion chip — read-only annotation next to the stepper */
  .ot-qty-chip {
    font-size: var(--fs-sm);
    color: var(--text-muted);
    background: rgba(200,216,240,0.06);
    border: 1px solid rgba(200,216,240,0.12);
    border-radius: 3px;
    padding: 0.1rem 0.35rem;
    font-family: var(--font-numeric);
    white-space: nowrap;
    flex-shrink: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    min-width: 0;
  }
</style>
