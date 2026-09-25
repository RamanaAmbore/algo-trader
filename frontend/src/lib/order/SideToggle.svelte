<script>
  let {
    side        = $bindable(/** @type {'BUY'|'SELL'} */ ('BUY')),
    currentQty  = 0,      // signed existing position qty — drives ADD/CLOSE label flip
    disabled    = false,  // from _noSymbol in parent
    locked      = false,  // true when action === 'modify'
    onChange    = null,   // (side: 'BUY'|'SELL') => void — mirrors onSideChange
  } = $props();

  const sideLabels = $derived.by(() => {
    if (!currentQty || currentQty === 0) return { BUY: 'BUY', SELL: 'SELL' };
    if (currentQty > 0) return { BUY: 'ADD · BUY', SELL: 'CLOSE · SELL' };
    return { BUY: 'CLOSE · BUY', SELL: 'ADD · SELL' };
  });
</script>

<div class="ot-knob ot-knob-side">
  <label class="ot-label" for="ot-side-toggle">Side</label>
  <div id="ot-side-toggle" class="ot-side-toggle-compact"
       role="group" aria-label="Side">
    <button type="button"
            class={'ot-side-btn ot-side-buy' + (side === 'BUY' ? ' on' : '') + (currentQty ? ' ot-side-btn-long' : '')}
            disabled={locked || disabled}
            aria-pressed={side === 'BUY'}
            title={(sideLabels.BUY.startsWith('ADD') ? 'Add to position (BUY)' :
                   sideLabels.BUY.startsWith('CLOSE') ? 'Close short position (BUY)' :
                   'Buy') + ' — selects the side, does not submit'}
            onclick={() => { if (!locked) {
              side = 'BUY'; onChange?.('BUY');
            } }}>
      {sideLabels.BUY}
    </button>
    <button type="button"
            class={'ot-side-btn ot-side-sell' + (side === 'SELL' ? ' on' : '') + (currentQty ? ' ot-side-btn-long' : '')}
            disabled={locked || disabled}
            aria-pressed={side === 'SELL'}
            title={(sideLabels.SELL.startsWith('ADD') ? 'Add to position (SELL)' :
                   sideLabels.SELL.startsWith('CLOSE') ? 'Close long position (SELL)' :
                   'Sell') + ' — selects the side, does not submit'}
            onclick={() => { if (!locked) {
              side = 'SELL'; onChange?.('SELL');
            } }}>
      {sideLabels.SELL}
    </button>
  </div>
</div>

<style>
  .ot-knob {
    display: flex;
    flex-direction: column;
    gap: 0.18rem;
    flex: 0 1 5rem;
    min-width: 5rem;
  }
  /* flex-grow: 0 (was `flex: 1.4 1 7rem` — grow enabled), matching the
     flex-grow:0 fix on OrderKnobsRow.svelte's `.ot-knob` (mobile
     SUSPECT 3). With the regular Type/Product/Variety/Validity knobs
     no longer growing, this Side knob was the ONLY flex-grow item left
     in the row — on a wide/inline (non-modal-capped) container it
     absorbed ALL leftover space and ballooned past 400px (regression
     caught by the pre-existing side_toggle_canary.spec.js layout
     checks). No knob in this row should unboundedly grow; trailing
     empty space on wide containers is the same accepted look every
     other chip/pill row in the ticket already has. */
  .ot-knob-side { flex: 0 1 7rem; min-width: 7rem; }

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

  .ot-side-toggle {
    display: flex;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 3px;
    overflow: hidden;
    height: 1.7rem;
  }
  .ot-side-btn {
    padding: 0 0.75rem;
    background: transparent;
    border: 0;
    color: var(--text-muted);
    font-size: var(--fs-lg);
    font-weight: 700;
    cursor: pointer;
    flex: 1 1 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }
  .ot-side-buy.on  { background: rgba(74,222,128,0.18);  color: var(--c-long); }
  .ot-side-sell.on { background: rgba(248,113,113,0.18); color: var(--c-short); }

  .ot-side-toggle-compact {
    display: inline-flex;
    width: 100%;
    /* Shared --ctl-h control height (declared in OrderTicket.svelte
       .ot-modal / SymbolPanel.svelte .oes-modal) — matches Select,
       QtyInput's steppers/input, and the footer side-selector button
       so a stacked view reads as one horizontal control strip instead
       of visibly uneven row heights. */
    height: var(--ctl-h, 1.55rem);
    min-height: var(--ctl-h, 1.55rem);
    border-radius: 3px;
    overflow: hidden;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.18);
    box-sizing: border-box;
  }
  .ot-side-toggle-compact .ot-side-btn {
    flex: 1 1 0;
    padding: 0;
    background: transparent;
    border: 0;
    color: #94a3b8;
    font-family: var(--font-numeric);
    font-size: var(--ctl-fs, var(--fs-sm));
    font-weight: 800;
    letter-spacing: 0.04em;
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, color 0.12s, box-shadow 0.12s;
  }
  .ot-side-toggle-compact .ot-side-btn:hover:not(.on):not([disabled]) {
    background: rgba(255, 255, 255, 0.06);
    color: #cbd5e1;
  }
  /* Border-treatment parity with the footer's .oes-footer-side-btn-single
     side selector (SymbolPanel.svelte) — same rgba(...,0.70) border alpha
     on the active state, via inset box-shadow since these buttons are
     flush (border:0) inside the pill group. Fill stays this control's own
     --algo-*-bg-strong token (matches every other in-ticket "on" pill —
     .ot-pill.on, .ot-chase-label.on, .ot-draft-label.on) — intentionally
     NOT matched to the footer button's transparent/ghost fill, which R7
     (2026-09) made deliberately un-filled so it can't be mistaken for the
     Submit action button. That disambiguation is preserved here. */
  .ot-side-toggle-compact .ot-side-btn.ot-side-buy.on  { background: var(--algo-green-bg-strong); color: var(--c-long); box-shadow: inset 0 0 0 1px rgba(74,222,128,0.70); }
  .ot-side-toggle-compact .ot-side-btn.ot-side-sell.on { background: var(--algo-red-bg-strong);   color: var(--c-short); box-shadow: inset 0 0 0 1px rgba(248,113,113,0.70); }
  .ot-side-toggle-compact .ot-side-btn[disabled] { opacity: 0.4; cursor: not-allowed; }

  /* Mobile SUSPECT fix (verified live at 375px), scoped to ONLY the
     2-word ADD/CLOSE variant (`currentQty` set — a close/add-context
     ticket) via the `.ot-side-btn-long` class — the plain BUY/SELL
     case (the vast majority of opens) keeps the normal --ctl-fs size
     at every viewport. Mirrors the existing `.oes-footer-side-btn-
     single.is-stacked` pattern in SymbolPanel.svelte, which solves the
     identical "verb + side needs more room" problem for that sibling
     control the same way (smaller text only when the longer label is
     actually showing).
     Confirmed via Range.getClientRects() line-count at 375px: the
     fixed (non-growing, see .ot-knob-side above) ~55px-wide button
     needs ≤7px to keep "CLOSE · SELL" on one line — --fs-2xs (8px,
     the smallest existing token) still wraps to 2 lines. white-space:
     nowrap + overflow:hidden is a last-resort safety net so an even
     longer future label degrades to a signalled ellipsis truncation
     rather than a silent 2-line wrap that grows the pill past --ctl-h. */
  .ot-side-toggle-compact .ot-side-btn.ot-side-btn-long {
    font-size: 7px;
    letter-spacing: 0.01em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding: 0 0.15rem;
  }
</style>
