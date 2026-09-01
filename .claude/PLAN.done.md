# Plan: Revert chain expiry loading to local instruments cache

## Context

`fetchChainExpiries` (introduced in Aug-11 commit `0e15bff2`) replaces what was a
synchronous `$derived.by(() => listExpiries(...))` with an async `$effect` that
calls the backend API. The API approach has never worked on Safari: nginx confirms
**zero** `/api/options/chain-quotes` calls when the chain tab opens, even after the
AbortError fix (`9bcf9904`) is deployed.

Root cause: the async `$effect` captures `chainUnderlying` as a reactive dependency.
Effect A (`seedUnderlying → chainUnderlying`) fires in the same reactive batch as the
fetch effect, causing the fetch effect to cleanup-abort before `fetch()` even reaches
the network layer. The AbortError microtask and the next effect run's
`_chainExpiriesLoading = true` race — producing a permanently stuck spinner.

The July-27 working approach used `listExpiries()` synchronously from the local
instruments cache (already loaded globally). `instruments.js` still exports both
`loadInstruments` and `listExpiries`. Reverting to that approach eliminates all
AbortController complexity.

## Agents
- frontend: revert expiry loading in `frontend/src/lib/order/OptionChainTab.svelte` —
  see Task section below
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Task (frontend agent)

File: `frontend/src/lib/order/OptionChainTab.svelte`

**Imports (line ~22-25):** add `loadInstruments` and `listExpiries`:
```js
import {
    loadInstruments, suggestUnderlyings,
    listExpiries, listFutures, getInstrument,
} from '$lib/data/instruments';
```

**State (~line 256):** Remove these two lines:
```js
let chainExpiries = $state(/** @type {string[]} */ ([]));
let _chainExpiriesLoading = $state(false);
```
Replace with:
```js
let instrumentsReady = $state(false);
const chainExpiries = $derived.by(() => {
    if (!instrumentsReady || !chainUnderlying) return [];
    return listExpiries(chainUnderlying.toUpperCase(), 'CE');
});
```

**Remove the entire fetch `$effect` (~lines 257-315)** — the block that creates
`AbortController`, defines `cancel()` / `attempt()`, and calls `fetchChainExpiries`.

**seedExpiry (~line 128):** guard on `instrumentsReady` (like July version):
```js
const seedExpiry = $derived.by(() => {
    if (!instrumentsReady || !symbol) return null;
    const inst = getInstrument(String(symbol).toUpperCase());
    return inst?.x || null;
});
```

**onMount (~line 980):** add instruments load at the TOP of the existing onMount:
```js
onMount(async () => {
    await loadInstruments();
    instrumentsReady = true;
    // … rest of existing onMount body unchanged …
});
```

**Template (~line 998):** replace:
```svelte
{#if _chainExpiriesLoading && !chainExpiries.length}
    <div class="oct-empty">Fetching expiries…</div>
{/if}
```
with:
```svelte
{#if !instrumentsReady && chainUnderlying}
    <div class="oct-empty">Loading instruments…</div>
{/if}
```

**Cleanup:** Remove `fetchChainExpiries` from the import at line 15 (keep
`fetchOptionsSpot`, `fetchChainQuotes`, `fetchChainQuotesPrices`).

**Write a Vitest test** in `frontend/src/lib/__tests__/data/chainExpiries.test.js`
(or add to existing chain test file if one exists):
- Mock `instruments.js` to return a known set of expiries for 'NIFTY'
- Confirm `listExpiries('NIFTY', 'CE')` returns expected array before/after ready

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(chain): revert expiry loading to local instruments cache — eliminates AbortController race that hung spinner on Safari

## Done when
- No `_chainExpiriesLoading` state in OptionChainTab.svelte
- `chainExpiries` is a `$derived.by` using `listExpiries`
- `instrumentsReady` gating is back, set in `onMount` after `loadInstruments()`
- `fetchChainExpiries` no longer imported in OptionChainTab.svelte
- svelte-check passes with 0 errors
