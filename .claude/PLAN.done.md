# Plan: Virtual root resolution + chart tab symbol restore + per-tab LTP display

## Context

Three related symbol-display bugs across all instrument types (MCX commodities, CDS currencies, NFO indices, NSE equities):

**Bug A — Chart tab inherits wrong symbol from Chain:**  
Order Ticket (any contract) → Chain (shows root) → Chart (still shows root, should show contract). `_setActiveTab('chart')` doesn't restore from `_contextSymbol`. Affects all symbol types.

**Bug B — `_NEXT` virtual roots fail in chart and resolveUnderlying:**  
Both MCX (`CRUDEOIL_NEXT`) and CDS (`USDINR_NEXT`, `EURINR_NEXT`, `GBPINR_NEXT`, `JPYINR_NEXT`) back-month virtual roots are not handled by the frontend resolution layer. `MCX_COMMODITIES.has('CRUDEOIL_NEXT')` and `CDS_CURRENCIES.has('USDINR_NEXT')` both return false because the sets contain bare roots only. Chart falls through to NSE equity path → wrong exchange → no bars.
- Market depth / LTP in order ticket already correct (backend `_strip_next()` handles this).
- Non-virtual symbols (NIFTY, RELIANCE, NIFTY26JUNFUT, NIFTY26JUN25000CE) unaffected — they have no `_NEXT` and already route correctly.

**Bug C — LTP shown is always root LTP, regardless of active tab:**  
`SymbolPanel.svelte:458` — `_ltpSym = _parseRoot(_localSymbol)` always strips to root, so even the order ticket and chart tabs show root price (CRUDEOIL, NIFTY) instead of the contract price (CRUDEOIL26SEPFUT, NIFTY26JUNFUT). Correct behaviour per all symbol types:
- **Chain tab**: root LTP (CRUDEOIL, NIFTY index, USDINR)
- **Order Ticket + Chart tab**: the actual `_localSymbol` LTP (the specific contract)

---

## Files and exact changes

### 1. `frontend/src/lib/SymbolPanel.svelte` — 2 changes

**1a. Bug A — `_setActiveTab()` (~line 443):** Add chart branch restoring `_contextSymbol`:
```js
} else if (id === 'chart') {
  // Restore the full contract (same as ticket) so chart shows
  // the symbol the operator is working with, not the chain root.
  if (_contextSymbol) _localSymbol = _contextSymbol;
}
```

**1b. Bug C — `_ltpSym` (line 458):** Show contract LTP on ticket/chart, root LTP on chain:
```js
const _ltpSym = $derived(
  _activeTab === 'chain'
    ? (_parseRoot(_localSymbol) || _localSymbol)
    : _localSymbol
);
```

### 2. `frontend/src/lib/data/resolveUnderlying.js` — Bug B fix

Strip `_NEXT` before MCX/CDS set checks in both `resolveUnderlying()` and `resolveAnchorToTradeable()`. Pass bare root to `findNearestFut`. `underlying_group` always returns bare root.

```js
export function resolveUnderlying(name, findNearestFut) {
  const n = String(name || '').toUpperCase();
  if (!n) return null;
  // Strip _NEXT — sets contain bare roots only.
  // USDINR_NEXT → root='USDINR', CRUDEOIL_NEXT → root='CRUDEOIL'
  const root = n.endsWith('_NEXT') ? n.slice(0, -5) : n;

  const idx = INDEX_LTP_KEY[root];
  if (idx) {
    return { tradingsymbol: idx.tradingsymbol, exchange: idx.exchange,
             quoteKey: `${idx.exchange}:${idx.tradingsymbol}`, underlying_group: root, kind: 'spot' };
  }
  if (MCX_COMMODITIES.has(root)) {
    const fut = findNearestFut?.(root);
    if (fut?.s && fut?.e) {
      return { tradingsymbol: fut.s, exchange: fut.e, quoteKey: `${fut.e}:${fut.s}`,
               underlying_group: root, kind: 'fut' };
    }
    return { tradingsymbol: root, exchange: 'MCX', quoteKey: `MCX:${root}`,
             underlying_group: root, kind: 'mcx' };
  }
  if (CDS_CURRENCIES.has(root)) {
    const fut = findNearestFut?.(root);
    if (fut?.s && fut?.e) {
      return { tradingsymbol: fut.s, exchange: fut.e, quoteKey: `${fut.e}:${fut.s}`,
               underlying_group: root, kind: 'fut' };
    }
    return null;
  }
  // NSE equity / NFO contract / index — pass through unchanged
  return { tradingsymbol: n, exchange: 'NSE', quoteKey: `NSE:${n}`,
           underlying_group: root, kind: 'spot' };
}
```

Same `_NEXT` strip in `resolveAnchorToTradeable()` before `MCX_COMMODITIES.has(upper)` and `CDS_CURRENCIES.has(upper)`.

### 3. `frontend/src/lib/ChartWorkspace.svelte:522-602` — Bug B fix

Strip `_NEXT` before `MCX_COMMODITIES`/`CDS_CURRENCIES` checks. For `_NEXT` variant, use `resolveVirtual(upper, exch)` to get the back-month contract (front-month for bare roots unchanged).

```js
async function _resolveFetchSymbol(sym) {
  const upper = String(sym || '').toUpperCase();
  const isNext = upper.endsWith('_NEXT');
  const stripped = isNext ? upper.slice(0, -5) : upper;  // bare root for set lookups

  const indexRoot = _KITE_INDEX_TO_ROOT[upper];           // index: full sym
  const isMcx     = MCX_COMMODITIES.has(stripped);        // commodity: bare root
  const isCds     = CDS_CURRENCIES.has(stripped);         // currency: bare root

  if (indexRoot && !(isMcx || isCds)) return { sym: upper, exch: 'NSE' };

  const root = indexRoot || (isMcx || isCds ? stripped : null);
  if (!root) {
    // NSE equity or real contract — unchanged from current behaviour
    const _isPlainEquity = /^[A-Z][A-Z0-9&-]*$/.test(upper) && !/(?:FUT|CE|PE)$/.test(upper);
    if (_isPlainEquity) return { sym: upper, exch: 'NSE' };
    return { sym, exch: '' };
  }

  // Load instruments if cache cold
  let fut = null;
  try { fut = findNearestFuture(root); } catch (_) {}
  if (!fut?.s) {
    try {
      await Promise.race([loadInstruments(),
        new Promise((_, r) => setTimeout(() => r(new Error('inst-timeout')), 3000))]);
      fut = findNearestFuture(root);
    } catch (_) {}
  }

  const defaultExch = isMcx ? 'MCX' : 'CDS';

  // _NEXT: resolveVirtual returns back-month (MCX or CDS)
  if (isNext) {
    const { resolveVirtual } = await import('$lib/data/rootOf.js');
    const resolved = resolveVirtual(upper, defaultExch);
    if (resolved && resolved !== upper) return { sym: resolved, exch: defaultExch };
  }

  if (fut?.s) return { sym: String(fut.s), exch: String(fut.e || defaultExch) };
  return { sym, exch: '' };
}
```

---

## Agents

- frontend: Apply all changes to `SymbolPanel.svelte`, `resolveUnderlying.js`, and `ChartWorkspace.svelte`. Update Vitest tests in `frontend/src/lib/__tests__/data/resolveUnderlying.test.js` covering:
  - MCX: `resolveUnderlying('CRUDEOIL_NEXT', ...)` → MCX exchange, underlying_group='CRUDEOIL'
  - CDS: `resolveUnderlying('USDINR_NEXT', ...)` → CDS exchange, underlying_group='USDINR'
  - `resolveAnchorToTradeable('EURINR_NEXT', ...)` → real CDS contract
  - NSE equity: `resolveUnderlying('RELIANCE', ...)` → NSE unchanged
  - NFO contract: `resolveUnderlying('NIFTY26JUNFUT', ...)` → NSE/NFO unchanged
  - Bare MCX root: `resolveUnderlying('CRUDEOIL', ...)` → MCX unchanged
  svelte-check must pass.

## Tests

- pytest: no
- svelte-check: yes
- playwright: no

## Commit message

fix(ui): virtual root _NEXT resolution (MCX+CDS) + chart tab symbol restore + per-tab LTP display
- resolveUnderlying/resolveAnchorToTradeable strip _NEXT before MCX/CDS checks (covers CRUDEOIL_NEXT, USDINR_NEXT, EURINR_NEXT, GBPINR_NEXT, JPYINR_NEXT)
- ChartWorkspace._resolveFetchSymbol strips _NEXT, resolves back-month via resolveVirtual for MCX+CDS
- NSE equities and real contracts unaffected (no _NEXT variants)
- SymbolPanel: chart tab restores _contextSymbol; chain/ticket/chart LTP shows correct price per tab

## Done when

1. `resolveUnderlying('CRUDEOIL_NEXT', ...)` → MCX, underlying_group='CRUDEOIL'
2. `resolveUnderlying('USDINR_NEXT', ...)` → CDS, underlying_group='USDINR'
3. `resolveUnderlying('RELIANCE', ...)` and NFO contracts unchanged
4. Chart for `CRUDEOIL_NEXT` fetches bars from MCX back-month contract (not NSE)
5. Chart for `USDINR_NEXT` fetches bars from CDS back-month contract (not NSE)
6. Chart for bare `CRUDEOIL` still resolves front-month MCX ✓
7. Order Ticket → Chain → Chart: chart shows full contract symbol (restored from _contextSymbol)
8. Chain tab LTP = root price; Ticket + Chart LTP = contract price (for all symbol types)
9. svelte-check 0 errors, vitest green
