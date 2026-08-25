# Plan: Fix all chain hang vectors — add timeouts to 8 untimed broker calls

## Context

The previous chain snapshot timeout fix (`asyncio.wait_for` at `options_helpers.py:780`)
only covered 1 of 9 blocking `asyncio.to_thread(broker.quote/instruments, ...)` calls in the
chain request path. The chain still hangs because:

1. **`_chain_snapshot_instruments` (options_helpers.py:724)** — calls `get_or_fetch("instruments",
   _fetch_instruments, ttl_seconds=86400)` with no `timeout_seconds`. On cold cache (every
   restart/deploy), fetches 156K rows from Kite — 30–60s hang. `get_or_fetch` only wraps in
   `asyncio.wait_for` when `timeout_seconds` is passed (cache.py:62).
   Fix: use `peek("instruments")` → return HTTP 503 if cold. Background pre-warm populates it.

2. **options_helpers.py:102, 125, 154** — spot-price quote calls (`_nse_spot_4`,
   `_commodity_spot_4a`, `_commodity_spot_4b`) with no `wait_for`. Each can block indefinitely.
   Fix: `asyncio.wait_for(..., timeout=5.0)` on each.

3. **options_helpers.py:480** — `asyncio.to_thread(broker.instruments, ex)` in instruments
   per-exchange fetch — no timeout.
   Fix: `asyncio.wait_for(..., timeout=30.0)`.

4. **options.py:1350** — `_ltp_broker_quote` — no timeout.
   Fix: `asyncio.wait_for(..., timeout=5.0)`.

5. **options.py:1624** — `_mcx_populate_phase3` — MCX futures batch quote — no timeout.
   Fix: `asyncio.wait_for(..., timeout=10.0)`.

6. **options.py:2310** — `_strategy_fetch_bulk_quote` — no timeout.
   Fix: `asyncio.wait_for(..., timeout=10.0)`.

## Task

Add `asyncio.wait_for` timeouts to all 8 remaining unguarded broker calls in the chain path.
The instruments cold-cache path must use `peek()` + 503 instead of blocking on `get_or_fetch`.

## Agents

- backend: Fix all 8 remaining hang vectors across two files.

  **`backend/api/routes/options_helpers.py`:**

  1. **Line ~724 `_chain_snapshot_instruments`** — replace `get_or_fetch("instruments", ...)` with:
     ```python
     from backend.api.cache import peek
     inst_resp = peek("instruments")
     if inst_resp is None:
         logger.warning("chain-snapshot: instruments cache cold — returning 503")
         raise HTTPException(status_code=503, detail="instruments cache warming — retry in a few seconds")
     ```
     This is safe: background pre-warm at 08:00 IST and on startup populates the cache before
     the chain tab is normally used. Cold == just restarted → tell user to retry.

  2. **Line ~102 `_nse_spot_4`** — wrap with `asyncio.wait_for(..., timeout=5.0)`; catch
     `asyncio.TimeoutError` → log warning, return None (same as Exception path).

  3. **Line ~125 `_commodity_spot_4a`** — same: `wait_for(..., timeout=5.0)`.

  4. **Line ~154 `_commodity_spot_4b`** (inside the for-loop) — same: `wait_for(..., timeout=5.0)`
     per iteration.

  5. **Line ~480** — instruments per-exchange fetch: `asyncio.wait_for(asyncio.to_thread(broker.instruments, ex), timeout=30.0)`.

  **`backend/api/routes/options.py`:**

  6. **Line ~1350 `_ltp_broker_quote`** — wrap `asyncio.to_thread(get_market_data_broker().quote, [key])`
     with `asyncio.wait_for(..., timeout=5.0)`; catch TimeoutError → log, return None.

  7. **Line ~1624 `_mcx_populate_phase3`** — wrap `asyncio.to_thread(price_broker.quote, _fut_quote_keys)`
     with `asyncio.wait_for(..., timeout=10.0)`; catch TimeoutError → log, `_fut_quote_resp = {}`.

  8. **Line ~2310 `_strategy_fetch_bulk_quote`** — wrap `asyncio.to_thread(_price_broker.quote, ...)`
     with `asyncio.wait_for(..., timeout=10.0)`; catch TimeoutError → log, return `{}`.

  For every file you change, you MUST write or update at least one test that covers the changed
  behaviour. This is mandatory — not optional.
  - `backend/api/` change → add/update a pytest test in `backend/tests/` covering the changed lines
  No change ships without a corresponding test update.

- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip (backend agent owns its tests)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(chain): add wait_for timeouts to all 8 remaining unguarded broker calls + peek() for cold instruments

## Done when
- Chain tab does not hang on cold cache (returns 503 immediately instead of 60s block)
- All spot/quote calls return within 5–10s on broker stall instead of blocking indefinitely
- All pytest tests green
