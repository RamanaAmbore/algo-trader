# Plan: Option B — Chain tab driven by API (no instruments dependency)

## Task
Remove the `loadInstruments()` blocking dependency from OptionChainTab. The chain
tab currently freezes because it gates the entire options grid on a 156K-row
instruments cache load, and the backend `_chain_quotes_build_sym_map` scans those
same 156K rows synchronously on the event loop on every 5s poll.

Three changes fix this end-to-end:

1. **Backend** — extend the `/api/options/chain-quotes` endpoint:
   - Make `expiry` optional. When omitted, do a fast scan and return only the
     `expiries` list for the underlying (no Kite quote call). This bootstraps the
     expiry dropdown before the operator picks one.
   - When `expiry` is provided, also return the expiries list + `ce_sym`, `pe_sym`,
     `ce_ls`, `pe_ls`, `exchange` per row so the frontend can resolve symbols
     without touching the instruments cache.
   - Wrap the 156K-row scan (`_chain_quotes_build_sym_map`) in `asyncio.to_thread`
     (it currently blocks the event loop).
   - Add a 3s in-process cache keyed by `(underlying, expiry)`.

2. **Frontend** — rewrite OptionChainTab to drive from API only:
   - Remove `await loadInstruments()` from `onMount`; remove `instrumentsReady` state.
   - Replace `chainExpiries = $derived.by(() => listExpiries(...))` with
     `$state([])` + an `$effect` that calls a new `fetchChainExpiries(underlying)`
     API helper (hits `chain-quotes` with no expiry).
   - Replace `chainStrikes = $derived.by(() => listStrikes(...))` with a `$derived`
     that reads sorted numeric keys from `chainQuotesMap`.
   - In `addOptionToBasket`: replace `findOption(...)` with
     `chainQuotesMap[String(strike)][optType.toLowerCase()]` which now carries
     `{sym, ls, exchange, bid, ask}` from the API response.
   - Remove `listExpiries`, `listStrikes`, `findOption` imports (dead after above).
   - The `chainFutures` derived and `suggestUnderlyings` in the underlying search
     are NOT removed — SymbolPanel already loads instruments in a background
     `$effect` when the chain tab is opened, so those still work when available;
     they simply don't block the options grid any more.

3. **Tests** — at every layer before ship:
   - pytest: new tests for the enhanced chain-quotes endpoint and the expiries-only
     code path.
   - Playwright: chain tab opens and populates the expiry dropdown and strike grid
     without waiting for instruments; switching expiry refreshes strikes; clicking
     a CE/PE button opens the order ticket with the correct symbol + exchange +
     lot size.

## Agents
- backend: In `backend/api/routes/options.py`:
  (a) Refactor `_chain_quotes_build_sym_map` to accept an optional `exp` arg. In
  one pass collect (i) all expiries for `und` into a sorted list and (ii) the
  sym/ls/exchange map for `und+exp` when `exp` is non-empty. Return
  `(sym_by_strike, all_expiries)`. Wrap the call in `asyncio.to_thread`.
  (b) Add a 3s cache: a module-level `dict[tuple, tuple[float, any]]` keyed by
  `(und, exp)` storing `(timestamp, result)`. In `chain_quotes`, skip the thread
  call when a fresh entry exists.
  (c) Extend `ChainQuoteRow` (msgspec.Struct) with nullable-default fields:
  `ce_sym: str | None = None`, `pe_sym: str | None = None`,
  `ce_ls: int | None = None`, `pe_ls: int | None = None`,
  `exchange: str | None = None`. Populate them from the sym_by_strike map.
  (d) Extend `ChainQuotesResponse` with `expiries: list[str] = []`.
  (e) Make `expiry` query param default to `""` in `chain_quotes`. When empty:
  call the sym_map builder with `exp=""` (collects expiries only, no sym_by_strike
  entries since expiry filter never matches), return
  `ChainQuotesResponse(underlying=und, expiry="", expiries=all_expiries, rows=[])`.
  No Kite quote call when expiry is empty.

- frontend: In `frontend/src/lib/api.js`:
  (a) Add `fetchChainExpiries(underlying)` → calls
  `GET /api/options/chain-quotes?underlying=X` (no expiry param) → returns
  `{ expiries: string[] }`.

  In `frontend/src/lib/order/OptionChainTab.svelte`:
  (b) Remove `loadInstruments` import and the `await loadInstruments()` call in
  `onMount`. Remove `instrumentsReady = $state(false)` and ALL guards that check
  `instrumentsReady` for the options-specific derived values (`chainExpiries`,
  `chainStrikes`).
  (c) Remove `listExpiries`, `listStrikes`, `findOption` from the instruments.js
  import (keep `loadInstruments`, `suggestUnderlyings`, `listFutures`,
  `getInstrument` which are still used by the underlying search and futures
  dropdown — they just no longer block).
  (d) Replace `chainExpiries = $derived.by(...)` with:
    ```javascript
    let chainExpiries = $state([]);
    let _chainExpiriesLoading = $state(false);
    $effect(() => {
      const u = chainUnderlying;
      if (!u) { chainExpiries = []; return; }
      _chainExpiriesLoading = true;
      fetchChainExpiries(u).then(d => {
        chainExpiries = d.expiries ?? [];
        _chainExpiriesLoading = false;
      }).catch(() => { chainExpiries = []; _chainExpiriesLoading = false; });
    });
    ```
  (e) Replace `chainStrikes = $derived.by(...)` with:
    ```javascript
    const chainStrikes = $derived(
      Object.keys(chainQuotesMap ?? {}).map(Number).filter(Boolean).sort((a,b)=>a-b)
    );
    ```
  (f) Update `chainQuotesMap` data shape: the `_refreshChainQuotes` function already
  stores the API rows keyed by strike string. Now each value must carry
  `{ce: {bid, ask, sym, ls, exchange}, pe: {...}}`. Update the mapping in
  `_refreshChainQuotes` to include `sym`, `ls`, `exchange` from the API rows
  (`row.ce_sym`, `row.ce_ls`, `row.exchange`).
  (g) In `addOptionToBasket`: replace `const inst = findOption(...)` with:
    ```javascript
    const q = chainQuotesMap?.[String(strike)]?.[optType.toLowerCase()];
    if (!q?.sym) { basketError = 'Quote not loaded yet — wait for chain refresh.'; return; }
    ```
    Then use `q.sym` for `symbol`, `q.exchange || 'NFO'` for `exchange`, `q.ls || 1`
    for `qty` and `lotSize`. The existing `q?.ask` / `q?.bid` limit-price logic
    already reads from the same map — no other change needed there.
  (h) Add a loading placeholder in the expiry dropdown area: when
  `_chainExpiriesLoading && !chainExpiries.length`, show a small `Fetching
  expiries…` spinner inline (same style as the instruments loading spinner).
  (i) The expiry default-pick `$effect` (currently lines ~351-369) already watches
  `chainExpiries` reactively, so it will fire correctly when `chainExpiries`
  transitions from `[]` to the loaded list — no change needed there.

- backend-test: Add `backend/tests/test_chain_quotes.py`:
  (a) Test `GET /chain-quotes?underlying=NIFTY` (no expiry) → 200, `expiries` is
  a non-empty list of date strings, `rows` is `[]`.
  (b) Test `GET /chain-quotes?underlying=NIFTY&expiry=<first_expiry>` → 200,
  `rows` is non-empty, each row has `ce_sym` / `pe_sym` (non-empty string),
  `ce_ls` (positive int), `exchange` (e.g. `"NFO"`).
  (c) Test unknown underlying → 200, `expiries=[]`, `rows=[]`.
  (d) Test cache: two consecutive calls to the same (und, expiry) should not call
  `_chain_quotes_build_sym_map` a second time within 3s (mock the function and
  assert call count == 1).
  Mock `get_or_fetch` to return a minimal InstrumentsResponse fixture with 4
  contracts (NIFTY CE 24000 + 24500, PE 24000 + 24500) covering two expiries.

- playwright: Add `frontend/tests/chain_tab_api_driven.spec.js`:
  (a) Navigate to /admin/derivatives, open NIFTY SymbolPanel, switch to Chain tab.
  Assert expiry dropdown has at least one option within 3000ms — WITHOUT waiting
  for `instrumentsReady` or any instruments endpoint.
  (b) Switch the expiry dropdown to the second expiry (if available). Assert strike
  rows update (grid row count changes or first strike value changes).
  (c) Click the CE "Buy" button on any strike row. Assert the Ticket tab opens and
  the symbol field contains a non-empty string matching NFO/MCX pattern
  (e.g. `NIFTY\d+[CP]E\d+` or `CRUDEOIL\d+[CP]E\d+`).
  (d) Run MCX path: CRUDEOIL underlying (if market open). Assert exchange in ticket
  is `MCX`.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
feat(chain): drive OptionChainTab from API — expiries + sym resolution no longer require instruments cache

## Done when
- Chain tab expiry dropdown populates within ~1s of tab open (no instruments wait)
- Strike grid appears as soon as first chain-quotes response arrives (~1-2s)
- Clicking CE/PE Buy opens order ticket with correct symbol, exchange, and lot size
- `addOptionToBasket` uses API-sourced sym/ls/exchange (no findOption call)
- Backend scan is in asyncio.to_thread, cached 3s per (underlying, expiry)
- pytest green (including new chain_quotes tests), svelte-check 0 errors, Playwright chain spec passes
