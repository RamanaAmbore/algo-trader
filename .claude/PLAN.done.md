# Plan: Fix positions cache staleness — external broker trades not appearing

## Context
Positions created by trades placed directly at the broker (Kite/Dhan) don't appear in
the RamboQuant positions view, causing the app to be out of sync.

Root cause confirmed — NOT a filter/exclusion issue. The data flow is architecturally
correct (all broker positions are included), but the broker-side cache never expires:

```
/api/positions/
  → API-side TTL cache (30s) — expires and calls ↓
  → _fetch_positions_cached() [ssot_fetch, mode="coalesce", NO TTL]
      → returns cached result from last broker fetch ← STALE INDEFINITELY
```

When a trade is placed externally (directly at Kite web/app):
- RamboQuant receives no postback webhook → no cache invalidation
- API-side 30s TTL expires and calls `_fetch_positions_cached()`
- `ssot_fetch` returns its cached result WITHOUT hitting the broker again
- Positions never update until operator clicks Refresh (`?fresh=1`)

`ssot_fetch` has no TTL parameter (invalidation-based only by design). The fix is a
time-based `force_refresh` guard in the public `fetch_positions()` function so the
ssot_fetch cache is evicted every 30 seconds, ensuring one broker round-trip per TTL
window regardless of whether a postback arrived.

## Files to change

### `backend/brokers/broker_apis.py`
Find the public `fetch_positions()` function (called from `positions.py:452`).

Add two module-level constants + one float tracker at the top of the module (near other
module-level state):
```python
_POSITIONS_SSOT_TTL: float = 30.0          # seconds; must match API cache TTL
_positions_ssot_refresh_at: float = 0.0    # monotonic clock, 0 = never fetched
```

In `fetch_positions()`, add a TTL guard before calling `_fetch_positions_cached()`:
```python
def fetch_positions(force_refresh: bool = False) -> list[pd.DataFrame]:
    global _positions_ssot_refresh_at
    now = time.monotonic()
    if not force_refresh and (now - _positions_ssot_refresh_at) > _POSITIONS_SSOT_TTL:
        force_refresh = True
    result = _fetch_positions_cached(force_refresh=force_refresh)
    if result is not None:
        _positions_ssot_refresh_at = time.monotonic()
    return result
```

`import time` is already in broker_apis.py (verify). The `force_refresh=True` path
evicts the ssot_fetch result cache and re-runs `_fetch_positions_cached`, hitting the
broker. Subsequent calls within the 30s window get the cached result as before.

`?fresh=1` already passes `force_refresh=True` into `_raw_cache_invalidate` +
`fetch_positions` — this still works and also resets `_positions_ssot_refresh_at`.

## Agents
- broker: Implement the change in `backend/brokers/broker_apis.py` as described above.
  Read the file first to find the exact `fetch_positions()` signature and surrounding
  context before editing. Confirm `import time` is present; add if missing.

  For every file you change or create, you MUST write or update at least one test
  covering the changed behaviour. This is mandatory.
  - Add/update a pytest test in `backend/tests/broker/` that:
    1. Stubs `_fetch_positions_cached` call counter
    2. Calls `fetch_positions()` twice within 30s → confirms only one broker fetch
    3. Calls `fetch_positions()` after advancing mock time past 30s → confirms second
       broker fetch (ssot_fetch evicted)
    4. Calls with `force_refresh=True` → confirms immediate broker re-fetch regardless
       of elapsed time

- backend: skip
- frontend: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(positions): 30s TTL auto-refresh for ssot_fetch cache — external trades now sync within one polling cycle

## Done when
- `fetch_positions()` hits the broker at most once per 30s, regardless of postbacks
- `?fresh=1` still bypasses TTL and forces immediate broker fetch
- Existing postback invalidation path unchanged
- pytest passes for the new TTL behaviour test
