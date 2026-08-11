# Plan: Option chain hang fix — timeout + TTL + market-gate removal + loading feedback

## Context

Two distinct bugs cause the option chain to be unusable:

**Bug 1 — "Fetching expiries…" hangs forever (post-restart)**
`fetchChainExpiries(u)` → `GET /options/chain-quotes?underlying=NIFTY` (no expiry)
→ `get_or_fetch("instruments", _fetch_instruments, ttl_seconds=86400)` with **no timeout**
→ on cold cache post-restart, `asyncio.to_thread(_fetch_instruments)` holds `asyncio.Lock` indefinitely
→ all concurrent callers block → `_chainExpiriesLoading = true` never clears
→ `OptionChainTab.svelte:797` renders "Fetching expiries…" permanently.

**Bug 2 — Strike grid empty + orders unplaceable outside market hours**
`_refreshChainQuotes()` at `OptionChainTab.svelte:430` has guard:
`if (!chainUnderlying || !chainExpiry || !isMarketOpen()) return;`
Outside market hours `isMarketOpen()` → false → `chainQuotesMap` stays null → `chainStrikes` empty → no grid.
`addOptionToBasket()` at line 568 reads `q = chainQuotesMap?.[String(strike)]?.[optType.toLowerCase()]` and
checks `if (!q?.sym)` → null → shows "Quote not loaded — wait for chain refresh." — order blocked.

The backend already handles closed-hours gracefully: `_chain_quotes_build_book` pre-seeds `sym`, `ls`, and
`exchange` from instruments data (line 2193-2198) BEFORE looking up broker quotes. So even with no live quotes,
every row has `ce_sym`/`pe_sym`/`exchange`/`ce_ls`/`pe_ls` set. Orders placed outside market hours get
`limit: 0` (fallback when bid/ask are null) → MARKET order at placement time.

The frontend just needs to stop blocking the quotes fetch — this unblocks both rendering and order placement.

**Secondary**
- `_CHAIN_SYM_TTL = 3.0s` — too short, re-triggers 156K-instrument scan on every rapid change
- No loading indicator or error feedback on quote fetch

---

## Critical files

| File | Line | What changes |
|---|---|---|
| `backend/api/cache.py` | 27 | Add `timeout_seconds: int \| None = None` param + `asyncio.wait_for` |
| `backend/api/routes/options.py` | 2099 | `_CHAIN_SYM_TTL = 3.0` → `30.0` |
| `backend/api/routes/options.py` | 2547 | Add `timeout_seconds=20` to instruments `get_or_fetch` call |
| `frontend/src/lib/order/OptionChainTab.svelte` | 299 | Add `_chainQuotesLoading` + `_chainQuotesError` state |
| `frontend/src/lib/order/OptionChainTab.svelte` | 429–458 | Remove `!isMarketOpen()` gate; wrap with loading/error |
| `frontend/src/lib/order/OptionChainTab.svelte` | ~867 | Insert loading row + error banner before strike grid |
| `backend/tests/test_cache_timeout.py` | new | 3 pytest-asyncio tests for timeout behavior |

---

## Agents

### backend
File scope: `backend/api/cache.py`, `backend/api/routes/options.py`

**Fix 1 — `cache.py:27`**: Replace function signature:
```python
async def get_or_fetch(key: str, fetcher, ttl_seconds: int = 30,
                       timeout_seconds: int | None = None):
```
Inside `async with _lock(key)`, build the coroutine then wrap conditionally:
```python
if asyncio.iscoroutinefunction(fetcher):
    coro = fetcher()
else:
    coro = asyncio.to_thread(fetcher)

if timeout_seconds is not None:
    value = await asyncio.wait_for(coro, timeout=timeout_seconds)
else:
    value = await coro
```
`asyncio.TimeoutError` propagates out of the lock block, releasing the lock so the next caller can retry.
Update the docstring to mention the timeout behaviour.

**Fix 2 — `options.py:2547`**: Add `timeout_seconds=20`:
```python
inst_resp = await get_or_fetch(
    "instruments", _fetch_instruments, ttl_seconds=86400,
    timeout_seconds=20,
)
```
The existing `except Exception as e` at line 2549 already catches `TimeoutError` and returns
`ChainQuotesResponse(underlying=und, expiry=exp, rows=[])`. No additional handling needed.

**Fix 3 — `options.py:2099`**:
```python
_CHAIN_SYM_TTL = 30.0
```

For every file changed, write or update at least one test covering the changed behaviour.

### frontend
File scope: `frontend/src/lib/order/OptionChainTab.svelte`

**Fix 4** — After `let chainQuotesMap = $state(null);` (line 299) add:
```javascript
let _chainQuotesLoading = $state(false);
let _chainQuotesError = $state('');
```

**Fix 5** — Replace `_refreshChainQuotes()` (lines 429–458). Remove `!isMarketOpen()` guard.
Add `_chainQuotesLoading = true` at entry, `finally { _chainQuotesLoading = false; }`,
`_chainQuotesError = ''` at top of try, `catch { _chainQuotesError = 'Failed to load quotes — retrying…'; }`.
Preserve all existing map-building logic and the stale-discard guard (`if (chainQuotesKey !== ...) return`).

**Fix 6** — In template before `<!-- Strike grid -->` (~line 867), insert:
```svelte
{#if _chainQuotesError}
  <div class="oct-empty" style="color:var(--c-short)">{_chainQuotesError}</div>
{/if}
{#if _chainQuotesLoading && chainKinds.includes('opt') && !chainStrikes.length}
  <div class="oct-empty">Fetching quotes…</div>
{/if}
```

For every file changed, write or update at least one test covering the changed behaviour.

### broker: skip

### backend-test
File: `backend/tests/test_cache_timeout.py` (new)

Three `@pytest.mark.asyncio` tests. Call `backend.api.cache.invalidate_all()` in setup.

1. **timeout raises and releases lock** — slow async fetcher (asyncio.sleep 5s) with `timeout_seconds=1`:
   assert `asyncio.TimeoutError`; then immediately call `get_or_fetch` with fast fetcher + `timeout_seconds=5`
   → assert succeeds (proves lock released, not deadlocked).

2. **succeeds within timeout** — fast async fetcher returning `"ok"` with `timeout_seconds=5`:
   assert `"ok"` returned; call again → same value from cache (fetcher called only once).

3. **sync fetcher timeout** — slow sync fetcher (time.sleep 5s) in `asyncio.to_thread` with `timeout_seconds=1`:
   assert `asyncio.TimeoutError`.

### doc: skip
### playwright: skip

---

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

---

## Commit message
fix(chain): instruments fetch timeout + 30s sym-map TTL + remove market-gate + loading/error feedback

---

## Done when
- Cold instruments cache raises `asyncio.TimeoutError` after 20s (caught → empty response) instead of hanging
- `_CHAIN_SYM_TTL = 30.0`
- Strike grid loads outside market hours; bid/ask show as "—" when broker returns nothing
- "Fetching quotes…" shown while `_chainQuotesLoading && !chainStrikes.length`
- "Failed to load quotes — retrying…" shown in `--c-short` on fetch error
- All 3 cache timeout tests pass under `pytest-asyncio`
- `svelte-check` 0 errors
