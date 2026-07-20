# Plan: Orders hardening — P1/P2/P3 + broker layer parity with OSS repos

## Context

Comprehensive strengthening of the RamboQuant orders pipeline across three priority tiers
(20 defects from audit) plus three broker-layer improvements identified by comparing against
fenix, kiteconnect-py, and OpenAlgo OSS repos. Backend-only — no frontend changes.

---

## P1 — Critical (data loss / silent misclassification)

### M1 · broker_order_id seed race
`backend/api/routes/orders_place.py:1519`
`_ticket_seed_broker_order_id` is fire-and-forget (awaited but returns before DB flush).
A sub-100ms postback arrives, matches no row (broker_order_id still NULL), and is dropped.
**Fix**: await the seed BEFORE returning from `ticket_order_handler`, not after — make it
blocking (still best-effort: wrap in try/except so timeout doesn't kill the response).

### M2 · Orphan postback fallback — verify + harden
`backend/api/routes/orders_postback.py:219–256`
A fuzzy-match fallback exists (account/symbol/side, last 60s, no broker_order_id). Verify:
(a) it is actually called on every postback miss, not just on Kite; (b) add CRITICAL log
when fallback match succeeds so ops can detect races; (c) when fallback finds zero matches,
write a "reconcile_from_postback" OPEN AlgoOrder row so the fill isn't silently discarded.

### M3 · LIMIT/SL price validation at broker boundary
`backend/brokers/adapters/kite.py:place_order, place_gtt`
Add pre-SDK guards: LIMIT orders must have `price > 0`; SL/SL-M orders must have
`trigger_price > 0`; SL orders must also have `price > 0`. Raise `BrokerOrderError` (typed)
with a clear message rather than letting Kite return a cryptic 400.
Same guards in `place_gtt` for GTT legs.

### M4 · Basket close-intent not threaded to preflight
`backend/api/routes/orders_basket.py:308–337, ~394`
`_leg_close` is evaluated but not forwarded to the preflight payload (line ~394).
Preflight's FAT_FINGER check therefore doesn't honour close intent, producing asymmetric
behaviour. Thread `intent` into the preflight call so both surfaces use the same logic.

### M5 · MARKET order capacity guard with absent price_hint
`backend/api/routes/orders_place.py:862–869, 1695–1703`
MARKET orders with no `price_hint` already raise 503 via `_opp_resolve_notional_price`.
Harden: if a future path supplies `price_hint=0` (falsy but not None), the capacity guard
silently skips. Add explicit `price_hint > 0` assertion before the guard.

### M11 · Close intent not forwarded to GTT attach
`backend/api/routes/orders_place.py:408, 286`
`_maybe_fire_template_attach_for_reconcile` and `_fire_template_attach_on_fill` receive no
`intent` parameter. Close fills trigger template GTTs that should be skipped.
**Fix**: thread `intent` from `AlgoOrder.intent` (read from DB row) into both functions;
skip `apply_plan_live` when `intent == "close"`.

---

## P2 — Important (operational integrity)

### M6 · Template attach lock TTL too short
`backend/api/routes/orders_place.py:219, 243`
`_TEMPLATE_ATTACH_LOCKS` TTL = 1 hour (line 219 constant). Slow broker days (Kite timeout
loops) can exceed this; lock expires mid-reconcile, new lock acquired in parallel → double
GTT. **Fix**: raise TTL to 4 hours. Add metric log when a stale entry is evicted.

### M7 · Partial fill qty read from stale state in chase loop
`backend/api/routes/orders.py:308` (`_retry_effective_parent_qty`)
Chase loop reads `filled_quantity` from in-memory row state which may lag the DB after a
postback commit. **Fix**: immediately before `apply_plan_live`, re-fetch `filled_quantity`
from DB (single-column SELECT on the parent row_id).

### M8 · Preflight errors swallowed on basket legs
`backend/api/routes/orders_basket.py:384–407`
When preflight raises, the leg_result error is "Preflight check failed" with no detail.
**Fix**: capture `_pf.blocked` list (or exception message) and include it verbatim in the
leg_result `error` field so the operator sees which check failed and for which account.

### M9 · Dhan postback empty account raises no error
`backend/api/routes/orders.py:615`
`account = str(body.get("dhanClientId") or body.get("account") or "")` — empty string
silently passes. A postback with missing account then matches any row with that broker_order_id
regardless of account. **Fix**: raise `HTTPException(422, "dhanClientId missing")` when
account is empty, forcing Dhan to retry with corrected payload.

### M10 · Stale margin at placement — document explicitly
`backend/api/routes/orders_basket.py:~384` and preflight
No code change needed, but add a comment + warning log when preflight margin passes but
place_order raises a margin error, so ops can see the stale-margin race in the logs.

### M14 · No prod-branch gate on basket handler (verify)
`backend/api/routes/orders_basket.py:211–229`
`is_prod_branch()` IS imported and used for shadow mode (line 225/229) but a non-prod,
non-shadow basket call still reaches placement if `_ptm=False`. Verify the gate is airtight:
non-prod branches should either force paper mode or block entirely. Add explicit guard
matching ticket handler's `_opp_live_check_mode_gates`.

---

## P3 — Hardening + observability

### M12 · Audit trail for preflight rejections
`backend/api/routes/orders_place.py:1112` (`_ticket_record_preflight_block`)
Add `write_audit_event(category="order.reject", action="PREFLIGHT_BLOCKED", ...)` alongside
the existing REJECTED AlgoOrder write so rejections appear in `/admin/audit`.

### M13 · Request-id idempotency for ticket retries
`backend/api/routes/orders_place.py:1206–1218`
`_req_id` is captured but never checked. Before pre-persisting a new AlgoOrder, query:
`SELECT id FROM algo_orders WHERE request_id = _req_id AND created_at > now()-60s`.
If found, return the existing row_id (skip broker call) to prevent double-orders on
frontend timeout+retry.

### M15 · Capacity guard warns on unknown strategy_id
`backend/api/routes/orders_place.py:29–67` (`_opp_load_strategy_cap`)
Add `logger.warning("[CAP-GUARD] strategy_id=%s not found — capacity uncapped", strategy_id)`
when the strategy lookup returns None.

### M16 · Price tick alignment missing on basket legs
`backend/api/routes/orders_basket.py:~449`
Ticket handler calls `_align_price_to_tick` before broker call; basket does not.
Import and call the same helper for each basket leg before `broker.place_order`.

### M17 · Unify lot-cache miss handling: basket → 503 per-leg
`backend/api/routes/orders_basket.py:285–303`
Ticket raises 503 on lot-cache miss; basket silently skips the leg.
Change basket to raise HTTPException(503) with the same "retry in a moment" message,
consistent with ticket behaviour.

### M18 · Log sub-lot qty warning before Kite call
`backend/brokers/adapters/kite.py:to_kite_qty (~line 88)`
When `qty < lot_size` on an F&O leg, add `logger.warning("[QTY-GUARD] sub-lot qty=%d lot_size=%d sym=%s")`.
Still let Kite reject — just ensure there's a log trail.

### M19 · Log close-intent bypass of fat-finger cap
`backend/api/routes/orders_place.py:992–1006`
Add `logger.info("[FAT-FINGER] close intent bypasses cap: qty=%d sym=%s")` when G2 is skipped.

### M20 · Basket margin includes unresolved-lot legs
`backend/api/routes/orders_basket.py:90–97`
When lot_size can't be resolved, include the leg in the margin response with
`qty_contracts=0, error="lot_size_unresolved"` instead of silently skipping it.

---

## OSS parity — 3 broker layer improvements

### OSS-1 · Fenix rate limit tables per published Dhan docs
`backend/brokers/rate_limiter.py`, `backend/brokers/adapters/dhan.py`
Fenix maintains exact per-endpoint limits from each broker's published API documentation
(updated when brokers change them). Current Dhan limits (orders=10/s, margins=5/s,
history=3/s, auth=0.5/120s) were estimated. Pull the current Dhan API v2 published limits
and reconcile — adjust `_DHAN_RATE_LIMITER` to match. Add a comment citing the Dhan docs
version so future reviewers know when to update. Add same token-bucket rate limiter to
`GrowwBroker` and `KiteBroker._safe_call` for the endpoints Kite rate-limits (historical
data: 3/s; order placement: 10/s per Kite docs).

### OSS-2 · KiteTicker subscribe chunking + reconnect backoff
`backend/brokers/connections.py` or the KiteTicker wrapper in `backend/brokers/service/`
kiteconnect-py reference implementation chunks subscriptions at 3000 symbols per WebSocket
message to avoid Kite's per-message limit. Audit the current KiteTicker usage:
(a) does the conn-service chunk subscribe calls at 3000? If not, add chunking.
(b) does reconnect use exponential backoff (1s → 2s → 4s → … → 30s cap)?
If not, implement. Log each reconnect attempt with attempt number and delay.

### OSS-3 · Reconciliation completeness (M1+M2 makes RamboQ unique here)
Covered by M1 + M2 above. No OSS repo implements postback-driven reconciliation with
orphan recovery — completing M1+M2 makes this a genuine differentiator. Ensure the
reconcile path also handles the case where the broker postback arrives with a different
order_id format (e.g., string "1234" vs int 1234) by normalising to str() on both sides.

---

## Agents

- backend: Implement M1, M2, M4, M5, M7, M8, M9, M10, M11, M12, M13, M14, M15, M16, M17, M19, M20
  in: backend/api/routes/orders_place.py, orders_basket.py, orders_postback.py, orders.py
  Work P1 items first (M1 is the highest-risk — make broker_order_id seed blocking before
  returning from ticket_order_handler at line 1519). Then M11 (intent threading),
  M4 (basket preflight intent), M2 (postback fallback hardening). Then P2 (M6, M7, M8, M9,
  M14). Then P3 in a single pass (M12, M13, M15, M16, M17, M19, M20).
  For M6: change the TTL constant (line 219) from 3600 to 14400.
  For M13: add idempotency check as described; use a 60-second window.

- broker: Implement M3, M18, OSS-1, OSS-2
  in: backend/brokers/adapters/kite.py, backend/brokers/adapters/dhan.py,
      backend/brokers/adapters/groww.py, backend/brokers/rate_limiter.py,
      backend/brokers/connections.py (or conn-service WebSocket handler)
  M3 first (price/trigger validation before SDK call in place_order + place_gtt).
  M18 (sub-lot warning log in to_kite_qty).
  OSS-1 (reconcile Dhan limits with published docs; add rate limiter to Kite + Groww).
  OSS-2 (KiteTicker subscribe chunking at 3000 + exponential reconnect backoff).

- backend-test: Write pytest tests covering all new behaviour:
  - M1: mock slow DB write, fire postback within 10ms, assert AlgoOrder is NOT orphaned
  - M2: postback with no matching broker_order_id → assert fallback log CRITICAL + row created
  - M3: place_order(order_type="LIMIT", price=0) → assert BrokerOrderError raised before SDK call
  - M4: basket close-intent leg → assert intent threads to preflight payload
  - M6: TTL constant reads as 14400
  - M7: partial fill scenario → assert filled_quantity re-fetched from DB before apply_plan_live
  - M9: Dhan postback with empty dhanClientId → assert 422 raised
  - M11: AlgoOrder with intent="close" fills → assert apply_plan_live NOT called
  - M13: duplicate request_id within 60s → assert no new AlgoOrder created, existing id returned
  - OSS-1: _DHAN_RATE_LIMITER capacity values match expected Dhan published limits
  - OSS-2: subscribe call with 3001 symbols → assert chunked into at least 2 messages

- frontend: skip
- playwright: skip
- doc: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

feat(orders): harden P1/P2/P3 gaps + broker OSS parity — seed race, orphan recovery, price guards, intent threading, idempotency, rate limit parity, KiteTicker chunking

## Done when

1. `_ticket_seed_broker_order_id` is awaited synchronously before ticket_order_handler returns.
2. Postback fallback logs CRITICAL on fuzzy match; creates reconcile row on total miss.
3. place_order/place_gtt raise BrokerOrderError on invalid price/trigger before SDK call.
4. intent="close" on basket legs threads to preflight; GTT attach skips on close fills.
5. Lock TTL = 14400s; chase loop re-fetches filled_quantity from DB.
6. Basket preflight errors include blocker list; Dhan postback 422 on empty account.
7. All P3 log/guard items (M12–M20) present and correct.
8. Dhan/Kite/Groww rate limiters reconciled to published docs.
9. KiteTicker chunks at 3000 symbols; reconnect uses exponential backoff.
10. pytest green on all new test cases.
