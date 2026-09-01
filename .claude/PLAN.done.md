# Plan: fix chain expiries hang — distinguish cold-cache vs unknown-underlying

## Task
Two bugs cause the 30s chain tab hang:

**Bug 1 — Retry loop can't distinguish cold-cache from unknown-underlying.**
`OptionChainTab.fetchChainExpiries` retries 6× at 5s intervals when `expiries: []`.
This is correct during the T+0–120s cold-cache startup window but wrong when the
instruments cache is warm and the underlying simply doesn't exist in the index.
Fix: backend adds `ready: bool` to `ChainQuotesResponse` (`True` = cache warm, even
if expiries are empty). Frontend stops retrying when `ready && expiries.length === 0`.

**Bug 2 — "CRUDE OIL" (Kite name field, with space) vs "CRUDEOIL" (expiries index key).**
`MarketPulse.ctxOpenOptions` uses `row.underlying` = Kite's `name` field = "CRUDE OIL".
This becomes `?u=CRUDE+OIL` → `selectedUnderlying = "CRUDE OIL"` → API gets `CRUDE+OIL`
→ misses expiries index → `expiries: []` → triggers retry loop → 30s hang.
Backend also needs: strip internal spaces from `und` param (defensive normalisation).
Frontend also needs: use tradingsymbol prefix instead of `row.underlying` in `ctxOpenOptions`.

## Agents

- backend: Two changes to `backend/api/routes/options.py`:
  1. `ChainQuotesResponse` (find its Pydantic/dataclass definition near the top of the
     `chain_quotes` handler area): add field `ready: bool = False`.
  2. Line 2677: change `und = (underlying or "").upper().strip()` →
     `und = _re.sub(r'\s+', '', (underlying or "").upper())`.
  3. When `inst_resp is None` (cold cache, ~line 2704): return with `ready=False` (already
     `expiries=[]`). This is the default so no change needed — just confirm the field is there.
  4. When `inst_resp` is available (cache warm) and we reach any return path: set `ready=True`.
     That means: the expiry-only fast-path return at ~line 2714, and the `sym_by_strike` path
     return, and the "not exp" fallback that does a scan — ALL warm-cache returns must set
     `ready=True`. The cold-cache return at line 2705 stays `ready=False`.

- frontend: Two changes:

  **A. `frontend/src/lib/order/OptionChainTab.svelte`** — `fetchChainExpiries` retry logic:
  Find the retry loop that checks `expiries.length === 0` and retries up to 6×. Add condition:
  if the response has `ready === true` AND `expiries.length === 0` → do NOT retry, break
  immediately and leave `chainExpiries = []` so the grid shows empty state.
  Only retry when `!response.ready` (cache might still be warming) AND we haven't hit the
  retry limit.

  **B. `frontend/src/lib/MarketPulse.svelte`** — `ctxOpenOptions` function (~line 3833):
  Change `row.underlying || row.tradingsymbol` to use the tradingsymbol alphabetic prefix
  (same derivation as `_derivedUnderlying` in instruments.js):
  ```javascript
  const ts = String(row.tradingsymbol || '').toUpperCase();
  const root = ts.replace(/\d.*$/, '') || String(row.underlying || '').replace(/\s+/g, '') || ts;
  const underlying = encodeURIComponent(root);
  window.location.href = `/admin/derivatives?u=${underlying}`;
  ```
  This gives "CRUDEOIL" from "CRUDEOIL26SEPFUT", avoids the Kite name field "CRUDE OIL".

- broker: skip
- doc: skip
- backend-test: Add tests to `backend/tests/test_chain_quotes_underlying.py` (new file):
  1. Test that `ChainQuotesResponse` with warm instruments cache and unknown underlying
     returns `ready=True, expiries=[]`.
  2. Test that cold cache (inst_resp=None) returns `ready=False, expiries=[]`.
  3. Test that `und` normalises "CRUDE OIL" → "CRUDEOIL" (same result as "CRUDEOIL").
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(chain): stop 30s hang — ready flag distinguishes cold-cache from unknown-underlying; normalise MCX underlying spaces

## Done when
- `pytest backend/tests/ -q` passes
- `svelte-check` passes
- Selecting "CRUDE OIL" (or any unknown underlying) from the chain picker shows empty
  grid immediately — no 30s hang, no retry loop spinning
- Selecting a valid MCX underlying (CRUDEOIL) shows expiries within 1s
