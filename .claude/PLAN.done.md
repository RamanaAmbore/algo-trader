# Plan: Chain tab hang — diagnostic logging + AbortController stale-fetch fix

## Task
The option chain tab shows "Fetching expiries…" for up to 35s (7 retries × 5s) on some
underlying selections. Multiple prior fixes (void chainExpiries, O(1) fast path,
compute-outside-lock) didn't eliminate it. Root cause is unknown because there is no
logging capturing why the backend returns empty expiries repeatedly, and no AbortController
preventing stale in-flight fetches from writing back when the underlying changes.

This plan adds two things:
1. **Diagnostic logging** — structured DEBUG/INFO lines on every `chain_quotes` request
   showing cache state, fast-path decision, sym_lookup timing, and expiry count; plus
   `console.log` lines in the frontend retry loop.
2. **AbortController** — per-effect abort signal so that when `chainUnderlying` changes,
   any in-flight `fetchChainExpiries` from the old effect is cancelled and its `.then()`
   does not overwrite `chainExpiries` or `_chainExpiriesLoading`.

## Agents
- backend: Add structured logging to `chain_quotes` in `backend/api/routes/options.py`.
  At handler entry: log `[chain-quotes] und=%s exp=%s inst_chain_warm=%s exp_index_size=%d und_in_index=%s`
  (cache hit/miss for instruments_chain, size of exp index, whether `und` is in the index).
  After fast-path decision: log `[chain-quotes] fast-path: returning %d expiries for %s`
  (if fast path fires) or `[chain-quotes] fast-path miss: falling to sym_lookup for %s`
  (if not, also log WHY — index was None vs und not in index).
  In `_chain_quotes_sym_lookup`: log entry with key, timing on exit, count of strikes and
  expiries found: `[chain-quotes] sym_lookup(%s,%s) took %.3fs → %d strikes, %d expiries`.
  On the `inst_resp is None` early-return path: log a WARNING.
  No logic changes — logging only.
  File: `backend/api/routes/options.py`

- frontend: In `OptionChainTab.svelte` fetch effect, add console.log diagnostics AND
  add AbortController support.

  **Logging**: in `attempt()` log `[chain] attempt #N underlying=U` before fetch;
  in `.then()` log `[chain] response: expiries=%d retrying=%b` (count + whether retrying);
  in `.catch()` log `[chain] fetch error: name=%s retrying=%b`.
  In `cancel()` log `[chain] cancel: underlying=%s had retryCount=%d`.

  **AbortController**: create `const controller = new AbortController()` at effect
  top (per-effect, not shared). Pass `controller.signal` to `fetchChainExpiries`.
  In `cancel()` add `controller.abort()` BEFORE clearing retryTimer. In `.catch()`,
  if `err.name === 'AbortError'` return immediately without retrying or writing state.
  Locate `fetchChainExpiries` (likely in `frontend/src/lib/` — find it, add optional
  `signal` parameter, pass it through to the underlying `fetch()` call).
  Files: `frontend/src/lib/order/OptionChainTab.svelte` + the file containing `fetchChainExpiries`

- broker: skip
- doc: skip
- backend-test: Add test for the `chain_quotes` logging path when `instruments_chain_expiries`
  cache is None (should log warning + fall to sym_lookup) and when `und` is not in the index
  (should log fast-path miss). Mock `_cache_peek` to return None / a dict without the
  requested underlying. Assert the response returns empty expiries list.
  File: `backend/tests/test_chain_quotes_logging.py` (new)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
feat(chain): diagnostic logging + AbortController stale-fetch fix for expiry hang

## Done when
- Every `GET /api/options/chain-quotes?underlying=X` logs cache state + fast-path decision at DEBUG
- AbortController cancels old fetchChainExpiries when underlying changes; AbortError is silently swallowed
- svelte-check 0 errors
- pytest passes (new chain_quotes logging tests green)
- No logic changes to expiry lookup, retry counts, or UI rendering
