# Plan: Chain basket netting — +/- on same strike nets opposite side

## Context
Pressing + (BUY) and − (SELL) on the same CE or PE strike currently creates two separate
basket legs (one BUY + one SELL for the same contract). Economically these cancel each
other, but visually the operator sees two chips and has to manually clear them.

The fix: when pressing + or − for a strike that already has an opposite-side leg in the
basket, decrement that leg's lot count instead of adding a new leg. If lots reach 0,
remove the leg entirely. The basket chips update automatically via Svelte reactivity.

## File
`frontend/src/lib/order/OptionChainTab.svelte`

## Approach
Add `_netAgainstBasket(sym, sideTag)` helper and call it in both `addOptionToBasket`
and `addFuturesToBasket` before the existing `_mergeIntoBasket` call.

```javascript
function _netAgainstBasket(sym, sideTag) {
  const oppSide = sideTag === 'BUY' ? 'SELL' : 'BUY';
  const idx = chainBasket.findIndex(b => b.sym === sym && b.side === oppSide);
  if (idx < 0) return false;
  const leg = chainBasket[idx];
  const newLots = (leg.lots || 1) - 1;
  if (newLots <= 0) {
    if (_externalBasket && onRemoveLeg) { onRemoveLeg(leg); }
    else { _localBasket = _localBasket.filter((_, i) => i !== idx); }
  } else {
    if (_externalBasket && onUpdateLeg) {
      onUpdateLeg(leg.key, (l) => ({ ...l, lots: newLots }));
    } else if (_externalBasket && onRemoveLeg && onAddLeg) {
      onRemoveLeg(leg); onAddLeg({ ...leg, lots: newLots });
    } else {
      _localBasket = _localBasket.map((b, i) => i === idx ? { ...b, lots: newLots } : b);
    }
  }
  return true;
}
```

In `addOptionToBasket` (line ~631), add before `_mergeIntoBasket`:
```javascript
if (_netAgainstBasket(String(inst.s), sideTag)) {
  basketError = ''; _flashToast(_quickKeyOpt(strike, optType), 'netted'); return;
}
```

In `addFuturesToBasket` (line ~669), add before `_mergeIntoBasket`:
```javascript
if (_netAgainstBasket(String(sym), sideTag)) {
  basketError = ''; _flashToast(_quickKeyFut(sym), 'netted'); return;
}
```

## Agents
- frontend: In `frontend/src/lib/order/OptionChainTab.svelte`:

  1. Add `_netAgainstBasket(sym, sideTag)` helper function just before `_pushToBasket`
     (currently line ~599). It finds the OPPOSITE-side leg for the same sym, decrements
     its lots by 1, removes it if lots reach 0, and returns true if netting happened.
     Handle all three paths: externalBasket+onUpdateLeg, externalBasket+onRemoveLeg/onAddLeg,
     and local _localBasket.

  2. In `addOptionToBasket` (line ~631), insert before `if (_mergeIntoBasket(...))`:
     ```javascript
     if (_netAgainstBasket(String(inst.s), sideTag)) {
       basketError = ''; _flashToast(_quickKeyOpt(strike, optType), 'netted'); return;
     }
     ```

  3. In `addFuturesToBasket` (line ~669), insert before `if (_mergeIntoBasket(...))`:
     ```javascript
     if (_netAgainstBasket(String(sym), sideTag)) {
       basketError = ''; _flashToast(_quickKeyFut(sym), 'netted'); return;
     }
     ```

  For the test requirement: add a Vitest test in
  `frontend/src/lib/__tests__/data/chainQuotes.test.js` with a `describe('_netAgainstBasket
  netting logic')` block testing the netting arithmetic:
  - BUY against existing SELL 1 lot → lots reach 0 → remove leg
  - BUY against existing SELL 2 lots → lots = 1 → keep leg
  - SELL against existing BUY 1 lot → remove leg
  - No opposite leg → no netting (return false)

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
fix(chain): net +/- presses on same strike — decrement opposite-side basket leg instead of adding new

## Done when
- Pressing + CE when a SELL CE leg exists decrements SELL lots (removes if 0)
- Pressing − CE when a BUY CE leg exists decrements BUY lots (removes if 0)
- Same for PE and futures
- Basket chips update immediately via Svelte reactivity
- svelte-check 0 errors
