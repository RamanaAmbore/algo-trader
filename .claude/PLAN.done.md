# Plan: Basket pill + chain row visual polish

## Context
Screenshots showed four UI issues:
1. Chain row active state has a full violet inset border (all four sides) — user wants bottom-border-only like the ATM amber stripe
2. Basket pill chip has 5 different text colors (gray root, dim month, amber strike, green/red type, dim sep) — too busy; needs cohesion
3. BUY basket pill border is too vivid green — needs lighter alpha
4. "BASKET" label in the cart icon spills outside the 1.7rem icon container

Alert: "NSE market open" (slug=market-open-nse, fire_at=09:15) already exists and works correctly after the fire_at condition_text fix — already shows "Scheduled — 09:15 IST". No new alert code needed.

## Files
- `frontend/src/lib/order/OptionChainTab.svelte` — chain row active border
- `frontend/src/lib/SymbolPanel.svelte` — basket pill border + chip text colors + BASKET label

## Agents
- frontend: Make the following four edits.

  ### Edit 1 — OptionChainTab.svelte: bottom-border-only for active chain row
  
  File: `frontend/src/lib/order/OptionChainTab.svelte`
  
  **a)** `.chain-row-active > td` (~line 1433): change the inset box-shadow from full 4-side to bottom-only:
  ```css
  /* OLD */
  box-shadow: inset 0 0 0 1px rgba(167,139,250,0.55);
  /* NEW */
  box-shadow: inset 0 -1px 0 rgba(167,139,250,0.55);
  ```
  Keep the background-image gradient unchanged.
  
  **b)** Remove the per-side edge borders (~lines 1452-1456) — these add left/right borders on CE/PE side; just delete these two rules:
  ```css
  /* DELETE these two rules entirely */
  .chain-row-active-ce > .chain-td-ce { border-left: 2px solid #a78bfa; }
  .chain-row-active-pe > .chain-td-pe { border-right: 2px solid #a78bfa; }
  ```

  ### Edit 2 — SymbolPanel.svelte: basket pill chip text color cohesion
  
  File: `frontend/src/lib/SymbolPanel.svelte`
  
  LegLabel renders symbol parts (root, month, strike, type, sep) with scoped CSS. Inside the basket pill, the colors are too varied. Add `:global()` overrides in the `<style>` block, inside a new block placed after `.oes-basket-pill-sym { ... }` (~line 3750):
  ```css
  /* LegLabel color overrides inside basket pills — flatten the palette */
  .oes-basket-pill-sym :global(.leg-root)   { color: #f1f7ff; font-weight: 700; }
  .oes-basket-pill-sym :global(.leg-month)  { color: rgba(148,163,184,0.70); font-weight: 400; }
  .oes-basket-pill-sym :global(.leg-strike) { color: #f1f7ff; }
  .oes-basket-pill-sym :global(.leg-sep)    { opacity: 0.35; }
  ```
  This keeps CE green / PE red (leg-type-ce/pe) but makes root+strike both bright white and dims the month separator.

  ### Edit 3 — SymbolPanel.svelte: lighter BUY pill border
  
  `.oes-basket-pill-buy` (~line 3733): reduce border-color alpha from 0.55 → 0.35:
  ```css
  /* OLD */ border-color: rgba(74,222,128,0.55);
  /* NEW */ border-color: rgba(74,222,128,0.35);
  ```
  
  ### Edit 4 — SymbolPanel.svelte: BASKET label text overflow fix
  
  `.oes-basket-label` (~line 4402): reduce font-size and add overflow guard:
  ```css
  .oes-basket-label {
    font-size: 0.42rem;         /* was 0.55rem — fits within 1.7rem container */
    font-weight: 700;
    letter-spacing: 0.02em;    /* was 0.04em — less spill */
    line-height: 1;
    margin-top: 0.1rem;
    opacity: 0.8;
    max-width: 100%;
    overflow: hidden;
    white-space: nowrap;
  }
  ```

- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): chain row bottom-border-only + basket pill chip colors + lighter BUY border + BASKET label overflow

## Done when
- Active chain row shows only a bottom violet line (no full inset border)
- Basket pill chip: root + strike = #f1f7ff white, month = dim gray, type CE/PE colors kept
- BUY pill border-color alpha reduced to 0.35
- "BASKET" label fits inside the 1.7rem icon without spilling
- svelte-check 0 errors
