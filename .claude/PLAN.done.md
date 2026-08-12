# Plan: Restore 600s sparkline startup delay to fix prod OOM

## Context

Prod API is in an OOM kill loop — `ramboq_api` is manually stopped to prevent restart damage.
Service has been stopped since ~2026-08-11 evening.

**Root cause trace** (from git log diff between last good deploy and HEAD):

1. `6332e592` — removed `bg-instruments` from `on_startup` (correct fix, still in HEAD)
2. `09ef3bad` — added 600s sparkline startup delay (correct fix, independently needed)
3. `e34dc2c9` — REVERTED both fixes, assuming removing `timeout_seconds=20` from instruments
   fetch was sufficient — this theory was wrong
4. `d88ec85d` — re-removed `bg-instruments` but did NOT restore the 600s sparkline delay

**Missing fix**: `_task_sparkline_warm` in `background.py` line 3418 fires `_do_warm_with_retry("startup")` immediately at T=0. This downloads 6 exchanges (NSE/NFO/BSE/BFO/MCX/CDS) — NFO alone is ~70k instruments. If the count is 0, `_do_warm_with_retry` retries at T+60s, triggering a second 6-exchange download. Combined RSS reaches 5-6GB before port 8000 ever binds → OOM kill → restart loop.

The 600s delay lets startup settle (paper engine recover, chase recovery, all 28 background tasks start) before the sparkline warm fires. Sparkline data is served from cache; a 10-minute cold window at boot is acceptable.

`timeout_seconds=20` was already removed from `options.py` instruments call (done in `e34dc2c9`). That fix is correctly in HEAD. The startup delay is an independent, additive fix.

## Task

In `backend/api/background.py` line 3418: wrap the sparkline startup warm in a 600s delay
helper so it fires 10 minutes after startup instead of immediately.

Add a regression test to `backend/tests/test_cache_timeout.py` to guard against this being
removed again.

## Agents

- backend: In `backend/api/background.py`, find the block at line ~3417-3418:
  ```python
  if not is_engine_idle():
      asyncio.create_task(_do_warm_with_retry("startup"))
  ```
  Replace with:
  ```python
  if not is_engine_idle():
      async def _spark_delayed_startup():
          await asyncio.sleep(600)
          await _do_warm_with_retry("startup")
      asyncio.create_task(_spark_delayed_startup())
  ```
  Update the comment block above (lines 3412-3416) to reflect the 600s delay rationale:
  "Delayed 600s so the sparkline warm does not compete with startup instrument downloads
  (NFO token map = ~70k rows). Immediate warm caused OOM kill loop on prod (2026-08-12)."
  No other changes.

- frontend: skip
- broker: skip
- doc: skip

- backend-test: In `backend/tests/test_cache_timeout.py`, add the following test after the
  existing `test_options_chain_instruments_no_timeout` test:
  ```python
  def test_sparkline_warm_has_startup_delay():
      """Guard: sparkline startup warm must NOT fire immediately (OOM risk on prod).

      Without a startup delay, _do_warm_with_retry fires at T=0 and downloads 6 exchanges
      (NFO = ~70k instruments). If count==0 it retries at T+60s (second full download).
      Combined RSS reaches 5-6GB before port 8000 binds → OOM kill loop.
      600s delay lets the process stabilise before sparkline warm fires.
      Root cause of 2026-08-12 prod OOM.
      """
      import re
      src = open("backend/api/background.py").read()
      m = re.search(r'async def _task_sparkline_warm\b.*?(?=\nasync def |\Z)', src, re.DOTALL)
      assert m is not None, "_task_sparkline_warm not found in background.py"
      body = m.group(0)
      assert '_spark_delayed_startup' in body or 'asyncio.sleep(600)' in body, (
          "sparkline startup warm must include a 600s delay — "
          "immediate startup warm causes concurrent NFO download OOM with other tasks "
          "(see fix 2026-08-12). Remove this guard only if instruments store is warm at boot."
      )
  ```

- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(background): restore 600s sparkline startup delay to prevent OOM on prod

## Done when

- `backend/api/background.py` line ~3418 wraps `_do_warm_with_retry("startup")` in `_spark_delayed_startup()` with `asyncio.sleep(600)`
- `venv/bin/pytest backend/tests/test_cache_timeout.py -v` passes (7 tests, including new guard)
- Full pytest suite green
- Deployed to prod — `systemctl status ramboq_api` shows active, port 8000 binds within 30s, no OOM in journal for 10 min after start

## Critical files

- `backend/api/background.py` — lines 3412-3418 (sparkline startup warm block)
- `backend/tests/test_cache_timeout.py` — add `test_sparkline_warm_has_startup_delay`
