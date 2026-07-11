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
            class={'ot-side-btn ot-side-buy' + (side === 'BUY' ? ' on' : '')}
            disabled={locked || disabled}
            aria-pressed={side === 'BUY'}
            title={sideLabels.BUY.startsWith('ADD') ? 'Add to position (BUY)' :
                   sideLabels.BUY.startsWith('CLOSE') ? 'Close short position (BUY)' :
                   'Buy'}
            onclick={() => { if (!locked) {
              side = 'BUY'; onChange?.('BUY');
            } }}>
      {sideLabels.BUY}
    </button>
    <button type="button"
            class={'ot-side-btn ot-side-sell' + (side === 'SELL' ? ' on' : '')}
            disabled={locked || disabled}
            aria-pressed={side === 'SELL'}
            title={sideLabels.SELL.startsWith('ADD') ? 'Add to position (SELL)' :
                   sideLabels.SELL.startsWith('CLOSE') ? 'Close long position (SELL)' :
                   'Sell'}
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
    flex: 1 1 5rem;
    min-width: 5rem;
  }
  .ot-knob-side { flex: 1.4 1 7rem; min-width: 7rem; }

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
    height: 1.55rem;          /* match Select chip height */
    min-height: 1.55rem;
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
    font-size: var(--fs-sm);
    font-weight: 800;
    letter-spacing: 0.04em;
    line-height: 1;
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .ot-side-toggle-compact .ot-side-btn:hover:not(.on):not([disabled]) {
    background: rgba(255, 255, 255, 0.06);
    color: #cbd5e1;
  }
  .ot-side-toggle-compact .ot-side-btn.ot-side-buy.on  { background: var(--algo-green-bg-strong); color: var(--c-long); }
  .ot-side-toggle-compact .ot-side-btn.ot-side-sell.on { background: var(--algo-red-bg-strong);   color: var(--c-short); }
  .ot-side-toggle-compact .ot-side-btn[disabled] { opacity: 0.4; cursor: not-allowed; }
</style>
