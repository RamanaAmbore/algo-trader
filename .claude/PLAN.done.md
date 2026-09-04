# Plan: Legs/ExpClose LTP reorder + payoff overlay prevClose fallback

## Context
Two bugs on the derivatives page:

1. **Legs/Exp Close grids**: The LTP column wasn't moved in the previous column-order fix because
   these grids are plain HTML `<span>` cells (not ag-Grid), with a shared `CandidateLegRow.svelte`
   component and explicit CSS `grid-template-columns`. Three places define column order and all
   must be kept in sync: header spans, CSS track list, row cell DOM order.
   Current: Lots → Qty → Avg → LTP. Target: **LTP → Lots → Qty → Avg**.

2. **Payoff overlay prevClose**: `OptionsPayoff` receives `prevClose={strategy?.spot_prev_close}`
   (line 4397). When `strategy` is null or `spot_prev_close` is unset, prevClose is null →
   `spotDir = 'flat'` → overlay stays cyan, CHG% hidden. The fix exists in `OptionsPayoff.svelte`
   but the data never arrives. The page already has `_underlyingQuotes[selectedUnderlying]?.prev_close`
   as a valid fallback (same pattern used at line 4830 for the by-underlying stats section).

## Agents
- backend: skip
- frontend: Make four targeted edits — no logic changes except the prevClose fallback.

  **Edit 1 — `+page.svelte` header (lines 4549-4553)**
  Move `<span class="num">LTP</span>` from after Avg to before Lots:
  Old order: Lots span, Qty span, Avg span, LTP span
  New order: LTP span, Lots span, Qty span, Avg span

  **Edit 2 — `+page.svelte` CSS grid-template-columns (lines 6063-6081)**
  Move `minmax(62px, max-content)  /* ltp */` to before `minmax(44px, max-content)  /* lots */`
  New track order after symbol: ltp, lots, qty, avg, prev close, ...

  **Edit 3 — `CandidateLegRow.svelte` cell DOM order**
  Move the LTP `<span class="num tf-cell leg-ltp ...">` block (lines 343-350) to before
  the Lots `{#if c.proxy_for}...{:else}...{/if}` block (lines 313-330).
  Must preserve the `{/if}` boundary — only the LTP span moves, not the surrounding blocks.

  **Edit 4 — `+page.svelte` line 4397 prevClose prop**
  Change:
  ```svelte
  prevClose={strategy?.spot_prev_close}
  ```
  To (same pattern as line 4830):
  ```svelte
  prevClose={(strategy?.spot_prev_close ?? 0) > 0
    ? strategy.spot_prev_close
    : (_underlyingQuotes[selectedUnderlying]?.prev_close ?? null)}
  ```

  For every file you change or create, you MUST write or update at least one test that covers
  the changed behaviour. This is mandatory — not optional.
  - The column-order fix is HTML/CSS — update the grep-based assertions in any existing
    Playwright spec that checks Legs column order (check `frontend/e2e/derivatives_legs_grid.spec.js`).
  - The prevClose fix — add/update a test that verifies prevClose falls back to
    `_underlyingQuotes` when `strategy.spot_prev_close` is null.

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): LTP before Lots in Legs/ExpClose + prevClose fallback for payoff overlay

## Done when
- Legs and Exp Close grids show: LTP · Lots · Qty · Avg · P.Close ...
- Payoff overlay shows color-coded spot and CHG% when live quote has prev_close > 0
  (even before strategy loads or when strategy.spot_prev_close is null).
- svelte-check 0 errors.
