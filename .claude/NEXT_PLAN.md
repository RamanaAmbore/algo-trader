# Plan: SymbolPanel — LTP on tab row + CardHeader controls + symbol width fix

## Context
Order entry panel visual feedback from operator:
1. Symbol input looks different than before — width needs adjustment to align with other row elements
2. LTP should display in the tab row (Ticket/Chain/Chart strip) so operator can see market price without opening the ticket
3. The CardHeader currently has `showControls={false}` — operator wants standard card controls (refresh at minimum) added to the header; refresh button that may be elsewhere (e.g. /orders page Order Entry CardHeader `onRefresh={loadOrders}`) should be removed from that location

## Agents
- backend: skip
- frontend: implement all changes in SymbolPanel.svelte and /orders/+page.svelte
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
feat(ui): SymbolPanel — LTP in tab row + CardHeader refresh control + symbol width

## Done when
- LTP shown in tab row next to Ticket/Chain/Chart tabs (right-aligned, from symbolStore getSnapshot)
- SymbolPanel CardHeader has a refresh button that bumps _ticketBump/_chainBump (refreshes quote/depth)
- /orders page Order Entry CardHeader `onRefresh` removed (refresh now lives in SymbolPanel header)
- Symbol input width adjusted to align with other picker row elements
- svelte-check 0 errors

---

## Change 1 — SymbolPanel.svelte (`frontend/src/lib/SymbolPanel.svelte`)

### 1a. Import getSnapshot
At the top of the script block, add alongside existing imports:
```js
import { getSnapshot } from '$lib/data/symbolStore.svelte.js';
```

### 1b. Add LTP derived value
Near the existing `_contextSymbol` state (around line 442), add:
```js
const _ltp = $derived(getSnapshot(_localSymbol)?.ltp ?? null);
```

### 1c. Add LTP to tab row
In the `.oes-tabs` div (lines ~2104-2129), after the `<AlgoTabs>` block, add a right-aligned LTP chip:
```svelte
{#if _ltp != null && _ltp > 0}
  <span class="oes-tab-ltp">₹{_ltp.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
{/if}
```

CSS for `.oes-tab-ltp`:
```css
.oes-tab-ltp {
  margin-left: auto;
  padding-right: 0.5rem;
  font-size: 0.7rem;
  font-variant-numeric: tabular-nums;
  color: var(--c-value, #e2e8f0);
  font-weight: 600;
  white-space: nowrap;
}
```

The `.oes-tabs` div already has `display: flex` / `align-items: center` so margin-left:auto pushes LTP to the right.

### 1d. Add refresh to CardHeader
The SymbolPanel CardHeader (lines ~1974-2028) currently has `showControls={false}`.

Add a `_refreshAll` function:
```js
function _refreshAll() {
  _ticketBump++;
  _chainBump++;
}
```

In the CardHeader element, add `onRefresh={_refreshAll}` and keep `showControls={false}` (refresh shows independently via the onRefresh prop):

### 1e. Symbol width
Adjust `.oes-sym-pick` max-width to prevent the input from expanding too wide relative to other picker row elements. Change from pure `flex: 1 1 0` to add a max-width cap:
```css
.oes-sym-pick {
  flex: 1 1 0;
  min-width: 0;
  max-width: 14rem;
}
```

---

## Change 2 — /orders page (`frontend/src/routes/(algo)/orders/+page.svelte`)

Remove `onRefresh={loadOrders}` and `bind:refreshLoading={loading}` from the Order Entry CardHeader. Refresh now lives inside SymbolPanel's own header.

---

## Critical files
- `frontend/src/lib/SymbolPanel.svelte` — getSnapshot import, _ltp derived, tab LTP chip, CardHeader refresh, symbol width CSS
- `frontend/src/routes/(algo)/orders/+page.svelte` — remove onRefresh from Order Entry CardHeader
