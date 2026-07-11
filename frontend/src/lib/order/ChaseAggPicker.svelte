<script>
  /**
   * ChaseAggPicker — reusable L/M/H aggressiveness pill cluster.
   *
   * Props:
   *   value    — current selection ('low' | 'med' | 'high')
   *   onChange — callback fired when the operator clicks a pill
   *   variant  — 'ticket' (default) renders the dark OrderTicket skin;
   *              'panel' renders the amber SymbolPanel skin.
   *
   * The component carries both CSS families so each host only needs to
   * pass `variant` — no extra class threading required.
   */

  /** @type {{ value: 'low'|'med'|'high', onChange: (v: 'low'|'med'|'high') => void, variant?: 'ticket'|'panel' }} */
  let { value, onChange, variant = 'ticket' } = $props();
</script>

{#if variant === 'ticket'}
  <div class="cap-agg cap-agg--ticket" role="group" aria-label="Chase aggressiveness">
    <button type="button"
            class="cap-pill cap-pill--ticket cap-pill--low"
            class:on={value === 'low'}
            title="Low — patient. SELL pegs to ASK, BUY pegs to BID. Order rests on your own side; fills only if the market lifts it."
            onclick={() => onChange('low')}>L</button>
    <button type="button"
            class="cap-pill cap-pill--ticket cap-pill--med"
            class:on={value === 'med'}
            title="Medium — peg to midpoint of bid+ask. Fills when the inside moves halfway in your favour."
            onclick={() => onChange('med')}>M</button>
    <button type="button"
            class="cap-pill cap-pill--ticket cap-pill--high"
            class:on={value === 'high'}
            title="High — urgent. SELL pegs to BID, BUY pegs to ASK. Crosses the spread to take liquidity on the next tick."
            onclick={() => onChange('high')}>H</button>
  </div>
{:else}
  <div class="cap-agg cap-agg--panel" role="group" aria-label="Chase aggressiveness">
    <button type="button"
            class="cap-pill cap-pill--panel"
            class:on={value === 'low'}
            title="Low — patient. SELL pegs to ASK, BUY pegs to BID. Order rests on your own side; fills only if the market lifts it."
            onclick={() => onChange('low')}>L</button>
    <button type="button"
            class="cap-pill cap-pill--panel"
            class:on={value === 'med'}
            title="Medium — peg to midpoint of bid+ask. Fills when the inside moves halfway in your favour."
            onclick={() => onChange('med')}>M</button>
    <button type="button"
            class="cap-pill cap-pill--panel"
            class:on={value === 'high'}
            title="High — urgent. SELL pegs to BID, BUY pegs to ASK. Crosses the spread to take liquidity on the next tick."
            onclick={() => onChange('high')}>H</button>
  </div>
{/if}

<style>
  /* ── shared wrapper ──────────────────────────────────────────────── */
  .cap-agg {
    display: inline-flex;
    border-radius: 3px;
    overflow: hidden;
  }

  /* ── ticket skin (dark / OrderTicket) ───────────────────────────── */
  /* Aggressiveness segment — three square pills (L · M · H) sitting
     immediately right of the CHASE checkbox. Color graduates from
     sky-blue (low/patient) → amber (med) → red (high/urgent) so
     the operator's eye lands on the urgency level without reading
     the glyph. Only the active pill carries the filled bg. */
  .cap-agg--ticket {
    border: 1px solid rgba(255,255,255,0.12);
    margin-left: 0.3rem;
  }
  .cap-pill--ticket {
    width: 1.4rem;
    height: 1.1rem;
    padding: 0;
    border: 0;
    border-right: 1px solid rgba(255,255,255,0.12);
    background: rgba(255,255,255,0.04);
    color: var(--text-muted);
    font-family: monospace;
    font-size: var(--fs-sm);
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: background 0.1s, color 0.1s, border-color 0.1s;
  }
  .cap-pill--ticket:last-child { border-right: 0; }
  .cap-pill--ticket:hover { color: var(--algo-slate); background: rgba(255,255,255,0.08); }
  /* Audit fix — add border-color in .on state so the active pill
     visually matches OptionChainTab's chain-basket-chase-pill family.
     Pre-fix, switching from Ticket to Chain tab caused the same three
     pills to gain/lose visible borders. */
  .cap-pill--low.on  { background: rgba(125,211,252,0.20); color: #7dd3fc; border-color: rgba(125,211,252,0.55); }
  .cap-pill--med.on  { background: rgba(251,191,36,0.20);  color: var(--c-action); border-color: rgba(251,191,36,0.55); }
  .cap-pill--high.on { background: rgba(248,113,113,0.20); color: var(--c-short); border-color: rgba(248,113,113,0.55); }

  /* ── panel skin (amber / SymbolPanel) ───────────────────────────── */
  .cap-agg--panel {
    border: 1px solid rgba(251,191,36,0.32);
  }
  .cap-pill--panel {
    padding: 0.14rem 0.4rem;
    background: transparent;
    color: rgba(200,216,240,0.65);
    border: 0;
    border-right: 1px solid rgba(251,191,36,0.20);
    font-family: var(--font-numeric);
    font-size: var(--fs-xs);
    font-weight: 700;
    cursor: pointer;
  }
  .cap-pill--panel:last-child { border-right: 0; }
  .cap-pill--panel:hover { color: var(--c-action); background: rgba(251,191,36,0.08); }
  .cap-pill--panel.on {
    color: #0c1830;
    background: var(--c-action);
  }
</style>
