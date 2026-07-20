# Plan: 6D Audit remediation — P1/P2/P3 fixes from 2026-07-20 audit

## Context

Seven code defects + two polish items surfaced by the post-orders-hardening 6D audit.
Backend-only except one frontend CSS SSOT and one doc sync pass.

---

## P1 — Critical

### A1 · M13 idempotency missing time bound
`backend/api/routes/orders_place.py:1254–1261`
The SELECT on `AlgoOrder.request_id` has no `created_at` filter. A request_id that
was last used weeks ago would return the stale row instead of placing a new order.
**Fix**: add `.where(AlgoOrder.created_at >= datetime.now(timezone.utc) - timedelta(seconds=60))`
to the idempotency lookup. Import `timedelta` from `datetime` at the same local-import site.

### A2 · rate_limiter refill_rate=0 consumes a token without sleeping
`backend/brokers/rate_limiter.py:88–104`
When `refill_rate == 0`, `sleep_time = float('inf')`. The guard at line 91
(`if sleep_time != float('inf')`) skips the sleep, but line 104 still runs
`bucket["tokens"] -= 1.0` — consuming a non-existent token and returning.
**Fix**: add `return` (or `raise ValueError("rate_limiter group has zero refill rate")`
after setting `sleep_time = float('inf')`) so the function never reaches the second
`with self._lock` block.

### A3 · M17 basket lot-miss should 503, not soft-error per leg
`backend/api/routes/orders_basket.py:310–322`
Lot-cache miss currently appends a per-leg `BasketLegResult(status="error")` and
`continue`s. Plan called for `HTTPException(503)` to match ticket handler.
**Fix**: replace the `leg_results.append(...); continue` block with
`raise HTTPException(status_code=503, detail=f"lot_size for {sym} on {exch} unavailable — retry in a moment")`.

---

## P2 — Important

### A4 · place_gtt qty ceiling not using shared helper
`backend/brokers/adapters/kite.py:445–468`
Inline MCX/NCO and NFO/CDS/BFO qty ceiling in `place_gtt` duplicates
`_check_kite_qty_ceiling`. Extract into a separate helper
`_check_kite_gtt_qty_ceiling(exchange, orders, tradingsymbol)` that iterates
GTT legs and calls the same ceiling logic, OR inline-call `_check_kite_qty_ceiling`
per leg. Keep GTT close-intent bypass = always False (no `intent` on GTT calls).

### A5 · place_gtt M3 missing SL/SL-M leg price guard
`backend/brokers/adapters/kite.py:431–438`
Current `place_gtt` M3 guard only checks LIMIT legs. SL/SL-M GTT legs that carry
`trigger_price == 0` pass through to Kite and return an opaque 400.
**Fix**: add trigger_price > 0 check for SL/SL-M legs inside the GTT leg loop,
matching the logic in `_validate_kite_order_prices`.

### A6 · M16 tick-align called for SL-M price=0
`backend/api/routes/orders_basket.py:466–471`
`_align_price_to_tick` is called for SL-M legs even when `leg.price` is None/0.
Passing 0 to the tick helper is untested and may propagate an error.
**Fix**: guard with `if _leg_price > 0` before aligning price; always guard trigger
separately with `if _leg_trig > 0` before aligning trigger.

---

## P3 — Polish / drift

### A7 · Comment drift: TTL still says "1 h" after M6 raised to 4 h
`backend/api/routes/orders_place.py:227–229`
Update parenthetical comment from "default 1 h" to "default 4 h".

### A8 · datetime alias confusion in orders_postback.py
`backend/api/routes/orders_postback.py:23, 71`
Module-level `from datetime import datetime, timezone` (line 23) and
`_create_postback_orphan_row` local `from datetime import datetime, timezone as _tz`
(line 71) coexist. The module-level import already provides everything needed.
**Fix**: remove the local import inside `_create_postback_orphan_row`; change
`_tz.utc` references to `timezone.utc` (matching the module-level alias).

### A9 · Separator CSS not using SSOT
`frontend/src/lib/MarketPulse.svelte:5006` `.mp-head-sep`
`frontend/src/lib/PositionStrip.svelte:1037` `.ps-agg-sep`
Both replicate `CardHeader.svelte:.ch-sep` instead of sharing a CSS custom property.
**Fix**: add `--color-sep: rgba(126,151,184,0.10)` and `--sep-margin: 0.15rem 0.4rem`
to `app.css` (or the global CSS token file). Update `.ch-sep`, `.mp-head-sep`, and
`.ps-agg-sep` to use these variables. No visual change — pure token extraction.

---

## Agents

- backend: Fix A1, A3, A6, A7, A8
  Files: `backend/api/routes/orders_place.py`, `backend/api/routes/orders_basket.py`,
  `backend/api/routes/orders_postback.py`

- broker: Fix A2, A4, A5
  Files: `backend/brokers/rate_limiter.py`, `backend/brokers/adapters/kite.py`

- frontend: Fix A9
  Files: `frontend/src/app.css` (or global token file), `frontend/src/lib/MarketPulse.svelte`,
  `frontend/src/lib/PositionStrip.svelte`, `frontend/src/lib/CardHeader.svelte`

- backend-test: Write pytest tests for A1, A2, A3, A5, A6
  - A1: request_id from 65s ago → new order placed (not idempotent return)
  - A2: TokenBucketLimiter with refill_rate=0 → throttle() returns immediately (no token consumed)
  - A3: basket place with cold lot-cache → HTTPException(503) raised
  - A5: place_gtt SL leg trigger_price=0 → BrokerOrderError raised
  - A6: basket SL-M leg price=0 → align_price not called (no error)

- doc: skip (D6 deferred — audit agents stalled; address in separate pass)
- playwright: skip

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(audit): 6D remediation — M13 time-bound, rate-limiter zero-refill, basket 503, GTT SL guard, tick-align guard, comment + alias cleanup

## Done when

1. M13 idempotency query includes `created_at >= now() - 60s`.
2. `TokenBucketLimiter.throttle` returns early (no token consumed) when `refill_rate == 0`.
3. Basket lot-miss raises `HTTPException(503)` matching ticket handler.
4. `place_gtt` SL/SL-M legs validate `trigger_price > 0`.
5. `place_gtt` qty ceiling delegates to `_check_kite_qty_ceiling` (or extracted GTT variant).
6. Basket M16 tick-align guarded by `price > 0` / `trigger > 0`.
7. TTL comment corrected to "4 h". `_tz` local alias removed from orphan helper.
8. Separator CSS tokens extracted to global and referenced in all three components.
9. pytest green on all new test cases.
