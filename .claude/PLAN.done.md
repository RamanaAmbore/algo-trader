# Plan: chain expiries index from instruments cache — no broker round-trip

## Context
After a service restart, the chain tab showed "Fetching expiries…" for 35s and hung.
Root cause: `_task_chain_instruments._warm()` calls `_fetch_chain_instruments()` which
downloads NFO+MCX instruments from the Kite broker. If conn-service takes >220s to restore
Kite tokens (typical after a deploy), all 4 retry attempts fail and the chain expiries cache
stays cold until 08:02 IST the next morning. The full `instruments` cache (156K rows,
including NFO+MCX) already exists in `_store["instruments"]` — it's populated by
`_task_instruments` at T+120s. The chain expiries index is just a filtered view of that
data; it should never need a separate broker call.

## Task
Two changes to `backend/api/background.py` only. No logic changes elsewhere.

1. **`_task_chain_instruments._warm()`**: At the top of `_warm()`, check if
   `_cache_peek_chain("instruments")` is warm. If yes, filter `.items` to
   `inst.e in {"NFO", "MCX"}`, build `instruments_chain` + `instruments_chain_expiries`
   from the filtered list, and return — skipping the broker call entirely.
   Only fall to `_run(_fetch_chain_instruments)` (broker) when `instruments` is cold
   (the T+10–220s window before `_task_instruments` has fired).

2. **`_task_instruments._warm()`**: After storing `_store["instruments"]`, also filter
   the fresh dump to NFO+MCX and rebuild both `_store["instruments_chain"]` and
   `_store["instruments_chain_expiries"]`. This makes the daily 08:00 instruments refresh
   automatically keep the chain index in sync, and guarantees the chain index is ready
   at T+120s regardless of broker availability.

## Agents

- backend: Edit `backend/api/background.py`.

  **Change 1 — `_task_chain_instruments._warm()` (around line 2162)**:

  Replace the current `_warm()` body with:
  ```python
  async def _warm():
      from backend.api.routes.instruments import _build_expiries_index, InstrumentsResponse as _IR
      # Fast path: build from existing full instruments cache — no broker call.
      _full = _cache_peek_chain("instruments")
      if _full is not None:
          _chain_items = [i for i in _full.items if i.e in {"NFO", "MCX"}]
          logger.info("[bg-chain-instruments] instruments cache has %d total rows, %d NFO+MCX", len(_full.items), len(_chain_items))
          if _chain_items:
              _store["instruments_chain"] = (_time.monotonic() + 86400,
                  _IR(cycle_date=_full.cycle_date, count=len(_chain_items), items=_chain_items))
              exp_idx = _build_expiries_index(_chain_items)
              _store["instruments_chain_expiries"] = (_time.monotonic() + 86400, exp_idx)
              logger.info(
                  "[bg-chain-instruments] built from instruments cache: %d NFO+MCX, %d underlyings — %s",
                  len(_chain_items), len(exp_idx),
                  {k: v for k, v in list(exp_idx.items())[:8]},
              )
              return
          else:
              logger.warning("[bg-chain-instruments] instruments cache warm but no NFO+MCX rows — falling back to broker")
      # Fallback: download NFO+MCX from broker (T+10–220s before instruments warms).
      try:
          result = await _run(_fetch_chain_instruments)
          if result is not None:
              _store["instruments_chain"] = (_time.monotonic() + 86400, result)
              exp_idx = _build_expiries_index(result.items)
              _store["instruments_chain_expiries"] = (_time.monotonic() + 86400, exp_idx)
              logger.info(
                  "[bg-chain-instruments] broker fetch: %d NFO+MCX, %d underlyings — %s",
                  result.count, len(exp_idx),
                  {k: v for k, v in list(exp_idx.items())[:8]},
              )
      except Exception as exc:
          logger.warning(f"[bg-chain-instruments] fetch failed: {exc}")
  ```

  **Change 2 — `_task_instruments._warm()` (around line 2118)**:

  After the `_store["instruments"] = ...` line and existing `logger.info`, add:
  ```python
  # Rebuild chain expiries index from the fresh dump (free filter — no broker call).
  from backend.api.routes.instruments import _build_expiries_index, InstrumentsResponse as _IR
  _chain_items = [i for i in result.items if i.e in {"NFO", "MCX"}]
  if _chain_items:
      _store["instruments_chain"] = (_time.monotonic() + 86400,
          _IR(cycle_date=result.cycle_date, count=len(_chain_items), items=_chain_items))
      exp_idx = _build_expiries_index(_chain_items)
      _store["instruments_chain_expiries"] = (_time.monotonic() + 86400, exp_idx)
      logger.info(
          "Background: instruments_chain_expiries rebuilt — %d NFO+MCX rows, %d underlyings — %s",
          len(_chain_items), len(exp_idx),
          {k: v for k, v in list(exp_idx.items())[:8]},
      )
  ```

  Both changes use the existing `_build_expiries_index` and `InstrumentsResponse` from
  `backend/api/routes/instruments.py`. No new imports at module level — lazy imports only.

  **Test requirement**: For every file changed, write or update at least one test.
  Add tests in `backend/tests/test_chain_hang_timeouts.py` (existing file) or a new
  `backend/tests/test_chain_instruments_warm.py`:
  - Test 1: When `instruments` cache is warm, `_warm()` builds chain index without calling
    `_fetch_chain_instruments` (mock `_run` to assert it's NOT called, mock `_cache_peek`
    to return a fake InstrumentsResponse with 3 NFO items + 1 NSE item, assert only NFO
    items appear in `_store["instruments_chain"]`).
  - Test 2: When `instruments` cache is cold, `_warm()` falls back to `_fetch_chain_instruments`
    (mock `_cache_peek` to return None, mock `_run` to return fake chain response).
  - Test 3: `_task_instruments._warm()` rebuilds `instruments_chain_expiries` after storing
    instruments (mock `_run(_fetch_instruments)` to return full fake response with NFO + NSE
    items, assert `instruments_chain` only has NFO items).

- broker: skip
- frontend: skip
- doc: skip
- backend-test: skip (covered by backend agent above)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(chain): build instruments_chain_expiries from cache — eliminate broker round-trip

## Done when
- `_task_chain_instruments._warm()` logs "built from instruments cache" when `instruments`
  is warm (verified by grepping prod logs after next restart)
- `_task_instruments._warm()` logs "instruments_chain_expiries rebuilt" after every
  instruments refresh (08:00 IST daily)
- pytest passes with new tests confirming the fast path + fallback paths
- No change to chain-quotes handler, instruments routes, or frontend
