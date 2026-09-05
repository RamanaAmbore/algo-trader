# Plan: Basket pill color + ring + cart label polish

## Task
Three cosmetic fixes to `frontend/src/lib/SymbolPanel.svelte`:
1. BUY basket pills use cyan (#67e8f9) — semantically wrong vs chain chips (green=buy). Fix to green tokens (`var(--c-long)`, `var(--c-long-14)`).
2. SELL basket pills use amber — semantically wrong vs chain chips (red=sell). Fix to red tokens (`var(--c-short)`, `var(--c-short-14)`).
3. Focused basket pill ring is heavy (2px outer + 1px inset violet). Soften to a single 1.5px ring.
4. Cart icon (basket mode toggle) is an SVG with no text label — purpose unclear without hover tooltip. Add a small "BASKET" text label beneath the SVG in both the static Chain span and the interactive Ticket label.

## Agents
- backend: skip
- frontend: In `frontend/src/lib/SymbolPanel.svelte` make four targeted edits:

  **Edit 1** — lines 3729-3733, `.oes-basket-pill-buy` CSS:
  ```
  OLD:
    color:        #67e8f9;
    border-color: rgba(103,232,249,0.55);
    background:   rgba(103,232,249,0.10);
  NEW:
    color:        var(--c-long);
    border-color: rgba(74,222,128,0.55);
    background:   var(--c-long-14);
  ```

  **Edit 2** — lines 3734-3738, `.oes-basket-pill-sell` CSS:
  ```
  OLD:
    color:        var(--c-action);
    border-color: rgba(251,191,36,0.55);
    background:   rgba(251,191,36,0.10);
  NEW:
    color:        var(--c-short);
    border-color: rgba(248,113,113,0.55);
    background:   var(--c-short-14);
  ```

  **Edit 3** — lines 4013-4016, `.oes-basket-pill.is-focused` CSS:
  ```
  OLD:
    box-shadow: 0 0 0 2px rgba(165, 180, 252, 0.65),
                inset 0 0 0 1px rgba(165, 180, 252, 0.45);
  NEW:
    box-shadow: 0 0 0 1.5px rgba(165, 180, 252, 0.45);
  ```

  **Edit 4** — cart icon markup (~lines 2984-3013). Add `<span class="oes-basket-label">BASKET</span>` after the SVG in both the static chain `<span>` and the interactive ticket `<label>`. Add CSS for `.oes-basket-label`: `font-size: 0.55rem; font-weight: 700; letter-spacing: 0.04em; line-height: 1; margin-top: 0.15rem; opacity: 0.8;` and wrap the SVG+label in a flex-col container by adding `flex-direction: column; align-items: center;` to `.oes-common-basket-toggle-icon`.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): basket pill colors green/red + soften focus ring + BASKET label on cart icon

## Done when
- BUY basket pills render green (var(--c-long) / var(--c-long-14))
- SELL basket pills render red (var(--c-short) / var(--c-short-14))
- Focused pill shows single 1.5px violet ring (not the heavy 2px+1px double ring)
- Cart icon shows "BASKET" text label beneath the SVG in both Chain (static) and Ticket (toggle) variants
- svelte-check 0 errors
