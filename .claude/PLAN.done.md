# Plan: Fix chain tab hang — add 10s abort timeout to price fetch

## Context
Chain tab hangs on weekends when both MCX and non-MCX markets are closed. The grid
displays from the local instruments cache (fast), then `_refreshChainQuotes()` fires
`fetchChainQuotesPrices` with `prices=1`. If the backend or network takes too long,
`_pricesFetching` stays `true` indefinitely — the 30s interval poll returns early at
`if (_pricesFetching) return` without ever aborting the stalled request. The user sees
"Fetching live prices…" spinner permanently over the strike grid.

The backend has an off-market gate (`_any_segment_open` check, options.py:2762) that
returns immediately on weekends. But any slow response (network blip, conn-service
startup, stale broker session) still leaves `_pricesFetching` stuck.

Fix: add a 10-second `setTimeout(() => ac.abort(), 10_000)` inside `_refreshChainQuotes()`,
cleared in `.finally()`. When the timeout fires the AbortController aborts the fetch,
`.catch(() => {})` handles the AbortError, and `.finally()` resets `_pricesFetching = false`.

## File
`frontend/src/lib/order/OptionChainTab.svelte` — modify `_refreshChainQuotes()` (lines 441–460).

## Agents
- frontend: In `frontend/src/lib/order/OptionChainTab.svelte`, modify `_refreshChainQuotes()`
  (currently lines 441–460) to add a 10-second abort timeout.

  Current code:
  ```javascript
  function _refreshChainQuotes() {
    if (!chainUnderlying || !chainExpiry) return;
    if (_pricesFetching) return;
    const u = chainUnderlying.toUpperCase(); const e = chainExpiry;
    const key = `${u}|${e}`;
    _pricesAbort?.abort();
    const ac = new AbortController();
    _pricesAbort = ac;
    _pricesFetching = true;
    fetchChainQuotesPrices(u, e, { signal: ac.signal }).then((r) => {
      if (chainQuotesKey !== key) return;
      /** @type {Record<string, object>} */
      const map = {};
      for (const row of (r?.rows || [])) {
        const [k, q] = parseChainQuoteRow(row, r?.exchange);
        map[k] = q;
      }
      chainQuotesMap = map;
    }).catch(() => {}).finally(() => { _pricesFetching = false; });
  }
  ```

  Replace with (add `_tout` timeout, clear in `.finally()`):
  ```javascript
  function _refreshChainQuotes() {
    if (!chainUnderlying || !chainExpiry) return;
    if (_pricesFetching) return;
    const u = chainUnderlying.toUpperCase(); const e = chainExpiry;
    const key = `${u}|${e}`;
    _pricesAbort?.abort();
    const ac = new AbortController();
    _pricesAbort = ac;
    _pricesFetching = true;
    const _tout = setTimeout(() => ac.abort(), 10_000);
    fetchChainQuotesPrices(u, e, { signal: ac.signal }).then((r) => {
      if (chainQuotesKey !== key) return;
      /** @type {Record<string, object>} */
      const map = {};
      for (const row of (r?.rows || [])) {
        const [k, q] = parseChainQuoteRow(row, r?.exchange);
        map[k] = q;
      }
      chainQuotesMap = map;
    }).catch(() => {}).finally(() => { clearTimeout(_tout); _pricesFetching = false; });
  }
  ```

  Also add a Vitest unit test in `frontend/src/lib/__tests__/data/` (or nearest suitable
  location) that verifies `_pricesFetching` resets to false after an aborted/timed-out fetch.
  If no unit-test harness exists for this component, add a Playwright spec asserting the
  "Fetching live prices…" spinner disappears within 15 seconds of chain tab load.

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
fix(chain): add 10s abort timeout to _refreshChainQuotes — clears _pricesFetching when backend is slow/offline

## Done when
- `_refreshChainQuotes()` adds `setTimeout(() => ac.abort(), 10_000)` and clears it in `.finally()`
- svelte-check 0 errors
- Spinner never stays permanently on weekend/offline
