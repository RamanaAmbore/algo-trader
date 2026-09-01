# Plan: Fix chain tab — restore July 2026 instruments-cache approach

## Context
The chain tab broke across three compounding regressions:

1. **IDB hang** — `_openDB()` had no `req.onblocked` handler. Safari fires `blocked` when another
   tab holds the connection; without a handler the Promise hangs forever — try/catch can't rescue it.

2. **Spinner never cleared** — `onMount` was `async` and awaited `loadInstruments()`. If IDB hung,
   `instrumentsReady` was never set.

3. **Grid never rendered** — `chainStrikes` was changed to derive from `Object.keys(chainQuotesMap)`
   (API response keys) instead of `listStrikes()` (local instruments cache). Grid only appeared after
   the slow broker API responded — or never, if `isMarketOpen()` was false.

All three are fixed. Fixes 1+2 already committed (`ad7f4ee7`). Fix 3 is the remaining uncommitted
change in the working tree (implemented and svelte-check verified by the implementation agent).

## Task
Commit the already-implemented fix 3 to workshop and deploy:

- `chainStrikes` → `listStrikes(underlying, 'CE', expiry)` from local instruments cache — grid
  renders immediately when instruments are ready, no API wait
- `addOptionToBasket` → uses `findOption()` for sym/ls/exchange; `chainQuotesMap` for bid/ask only
- Quotes loading → single-phase `fetchChainQuotes` polled every 5 s; bid/ask overlaid on arrival;
  grid shows `—` until first poll resolves
- Removed: two-phase skeleton/prices loader, `_chainQuotesLoading`, `_chainQuotesError`,
  `_pricesFetching`, `parseChainQuoteRow` import, `fetchChainQuotesPrices` import, `depthAvail`
  template guards, `withGuard` import (was only used for chain quotes poll)

## Agents
- frontend: skip (changes already implemented and svelte-check verified — 0 errors)
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes (already green — verify before commit)
- playwright: no

## Commit message
fix(chain): revert chainStrikes to listStrikes() — grid from instruments cache, bid/ask overlaid from API

## Done when
- Strike grid appears immediately after expiry is picked (no API wait)
- Bid/ask cells show `—` until `fetchChainQuotes` poll resolves (every 5 s when market is open)
- svelte-check 0 errors, vitest passes
- Committed to workshop → merged to dev → deployed to prod
