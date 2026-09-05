# Plan: Chain tab UX — disable +/- when no quote, spread warning, remove BASKET label

## Context
Three targeted UX fixes to the option chain tab:

1. **Disable +/- when bid/ask unavailable** — `addOptionToBasket()` (OptionChainTab.svelte:607)
   falls back to `orderType: 'MARKET'` when `limit = 0` (no bid or ask). User wants the
   buttons disabled entirely so an order cannot be placed without a known limit price.

2. **Wide-spread warning icon** — no spread indicator exists today. When the bid-ask spread
   exceeds 10% of mid price, a small ⚠ should appear so the operator can see an illiquid
   strike at a glance.

3. **Remove BASKET text label** — `SymbolPanel.svelte` lines 2996 and 3013 each render
   `<span class="oes-basket-label">BASKET</span>` next to the cart SVG icon. The text is
   not visible enough to be useful and clutters the toolbar.

## Files
- `frontend/src/lib/order/OptionChainTab.svelte` — features 1 + 2
- `frontend/src/lib/SymbolPanel.svelte` — feature 3

## Agents
- frontend: Apply three targeted changes.

  **Feature 1 — Disable +/- option buttons when no valid quote**

  In `OptionChainTab.svelte`, add `disabled` to each CE and PE buy/sell button pair when
  the quote has no valid bid or ask. The four button locations are:
  - Lines ~960-965: ATM CE BUY + SELL
  - Lines ~978-983: ATM PE BUY + SELL
  - Lines ~1008-1013: non-ATM CE BUY + SELL
  - Lines ~1026-1031: non-ATM PE BUY + SELL

  Disable condition for CE: `!(ceQ?.bid > 0 || ceQ?.ask > 0)`
  Disable condition for PE: `!(peQ?.bid > 0 || peQ?.ask > 0)`

  Title tooltip when disabled: `"No quote — price unknown"`

  Futures buttons (lines ~902-907) use `limit: 0` by design (opens ticket) — leave them
  unchanged.

  Add CSS for disabled state:
  ```css
  .chain-btn:disabled { opacity: 0.3; cursor: not-allowed; }
  .chain-btn:disabled:hover { background: transparent; }
  ```

  **Feature 2 — Wide spread warning icon**

  In the same `{#each chainStrikes}` block, compute spread width using `{@const}`:
  ```svelte
  {@const ceSpreadWide = ceQ?.bid > 0 && ceQ?.ask > 0 && (ceQ.ask - ceQ.bid) / ((ceQ.ask + ceQ.bid) / 2) > 0.10}
  {@const peSpreadWide = peQ?.bid > 0 && peQ?.ask > 0 && (peQ.ask - peQ.bid) / ((peQ.ask + peQ.bid) / 2) > 0.10}
  ```

  These can go immediately after the existing `{@const ceQ}` / `{@const peQ}` lines (~947-948).

  After each existing `(L)` depth indicator in the CE and PE bid-ask spans (4 locations:
  ATM CE ~956, ATM PE ~992, non-ATM CE ~1004, non-ATM PE ~1040), add:
  ```svelte
  {#if ceSpreadWide}<span class="chain-cell-spread-warn" title="Wide spread — {_fmtLtp(ceQ.ask - ceQ.bid)} ({((ceQ.ask - ceQ.bid)/((ceQ.ask+ceQ.bid)/2)*100).toFixed(0)}% of mid)">⚠</span>{/if}
  ```
  (use `peSpreadWide` / `peQ` for PE locations)

  CSS:
  ```css
  .chain-cell-spread-warn { font-size: 0.55rem; color: var(--algo-amber, #f59e0b); margin-left: 0.12rem; cursor: default; vertical-align: super; }
  ```

  **Feature 3 — Remove BASKET text label from SymbolPanel**

  In `SymbolPanel.svelte`, delete both occurrences of:
  ```svelte
  <span class="oes-basket-label">BASKET</span>
  ```
  at lines ~2996 and ~3013. The cart SVG remains; only the text label is removed.

  Also remove the `.oes-basket-label` CSS rule from the `<style>` block in SymbolPanel.svelte.

  For the test requirement: add a Vitest unit test in
  `frontend/src/lib/__tests__/data/chainQuotes.test.js` (or a new file) that verifies the
  spread-wide threshold calculation: `(ask - bid) / mid > 0.10` for a set of sample values.
  Also assert the disable condition `!(ceQ?.bid > 0 || ceQ?.ask > 0)` for null/0 inputs.

  For every file you change or create, you MUST write or update at least one test that
  covers the changed behaviour. This is mandatory — not optional.

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
fix(chain): disable +/- when no quote, spread warning icon, remove BASKET label clutter

## Done when
- CE/PE +/- buttons show opacity 0.3 + not-allowed cursor when ceQ/peQ has no valid bid or ask
- Futures +/- buttons unchanged
- ⚠ appears after bid-ask display when spread > 10% of mid
- `<span class="oes-basket-label">BASKET</span>` removed from both SymbolPanel locations
- svelte-check 0 errors
