# Plan: Broker adapter exception wrapping + column sequence + Exp P&L SSOT

## Task

Five independent fixes from the multi-surface audit:

**A — Broker adapter place_order exception wrapping** (P1)
The chase loop fix (commit 25fb6f3a) detects `BrokerInputError` to terminate immediately on
structural rejections. But none of the three adapters convert their SDK/response errors into
the typed hierarchy inside `place_order`, so the check never fires in production:
- Kite: raw `kiteconnect.InputException` bubbles up (not `BrokerInputError`)
- Dhan: all placement failures raise `RuntimeError` (DH-908 should be `BrokerInputError`)
- Groww: all placement failures raise `RuntimeError`; `_groww_exc()` is dead code

Fix: wrap `place_order` in each adapter with a try/except that converts exceptions to the
typed hierarchy using the existing conversion helpers.

**B — Position grid column sequence** (P2)
Lots column position inconsistent across grids:
- Performance (baseline): Lots after Qty (trailing)
- Pulse right grid (`pulseColumns.js:463`): Lots before LTP (4th col) — too early
- Derivatives legs (`CandidateLegRow.svelte`): Lots after Symbol (4th col) — too early
- Pulse Invested/Value order is reversed vs Performance (should be Invested → Value)

Fix: reorder to match Performance baseline.

**C — Exp P&L NavStrip SSOT** (P1)
Both PositionStrip and the payoff curve use the same `expiryPnl()` helper and cover all
positions across both accounts. The divergence is the **spot price source**:
- Payoff curve: `liveSpot` — KiteTicker WebSocket, real-time per underlying
- PositionStrip `_resolveOptionSpot` (line 689): `underlying_ltp` from backend position row
  (5-second book-poll value) as **primary**, symbolStore as secondary

When the underlying moves between polls, the two spot values differ → different exp P&L.
The payoff curve is authoritative (live spot = correct). Gap = 0.63 = delta from stale spot.

Fix: In `_resolveOptionSpot` in `PositionStrip.svelte`, flip the priority order:
1. Live spot from symbolStore/ticker (same source as `liveSpot` in payoff curve)
2. Fall back to `underlying_ltp` from position row only if live is unavailable

**D — H:3 Holdings Lifetime P&L missing throttle** (P2)
`_liveHoldingsTotal` derived in `PositionStrip.svelte` (lines 493–507) reads `symbolStore`
directly with no `_throttledTick` gate. P:1, H:1, and P:3 all gate on `_throttledTick` (4Hz).
H:3 re-derives on every SSE tick (100+/sec) — correct but wastes scheduler cycles.
Fix: add `void _throttledTick;` at the top of the `_liveHoldingsTotal` derived block.

**E — P:3 Exp P&L spot resolution order** (P1)
`_resolveOptionSpot` in `PositionStrip.svelte` (line 689) currently tries `p.underlying_ltp`
(backend poll stamp, up to 5s stale) before the live symbolStore ticker. The derivatives page
`liveSpot` does the opposite — symbolStore first, backend stamp as fallback. This is why the
NavStrip Exp P&L lags the payoff curve by up to 5s of underlying movement (the 0.63 gap).
Fix: in `_resolveOptionSpot`, try the symbolStore live spot for the underlying root first;
fall back to `p.underlying_ltp` only when the ticker has no value yet.

## Agents

- broker: Fix A — wrap place_order in kite.py, dhan.py, groww.py
- frontend: Fix B (column reorder) + Fix C (Exp P&L spot order) + Fix D (H:3 throttle) + Fix E (spot priority)
- backend-test: Tests for adapter wrapping (verify BrokerInputError raised from place_order for each broker)
- playwright: skip
- doc: skip

## Fix A detail — broker agent

### `backend/brokers/adapters/kite.py`

`place_order` (around line 383) currently does:
```python
return self.kite.place_order(**kwargs)
```
Change to:
```python
try:
    return self.kite.place_order(**kwargs)
except Exception as e:
    raise _kite_exc(e) from e
```
`_kite_exc` already maps `InputException → BrokerInputError`, `NetworkException → BrokerNetworkError`, etc.

### `backend/brokers/adapters/dhan.py`

`place_order` currently raises `RuntimeError` on any non-success response.
After the response check:
```python
if not isinstance(resp, dict) or resp.get("status") != "success":
    raise RuntimeError(f"Dhan place_order rejected: {resp}")
```
Replace with:
```python
if not isinstance(resp, dict) or resp.get("status") != "success":
    code = resp.get("code", "") if isinstance(resp, dict) else ""
    cls = _DHAN_ERROR_MAP.get(code, BrokerOrderError)
    raise cls(f"Dhan place_order rejected: {resp}", broker="dhan", code=code)
```
`_DHAN_ERROR_MAP` already has `"DH-908": BrokerInputError`. This makes it live.

Also wrap any SDK exception raised before the response check:
```python
try:
    resp = self._sdk_orders.place_order(...)
except Exception as e:
    raise BrokerNetworkError(str(e), broker="dhan") from e
```

### `backend/brokers/adapters/groww.py`

`place_order` currently raises `RuntimeError` when `order_id` is absent.
Replace:
```python
if not order_id:
    raise RuntimeError(f"Groww place_order rejected: {resp}")
```
With:
```python
if not order_id:
    status = resp.get("status", "") if isinstance(resp, dict) else ""
    msg = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
    # HTTP 400/422 → structural input rejection
    if isinstance(resp, dict) and resp.get("httpStatus") in (400, 422):
        raise BrokerInputError(msg, broker="groww")
    raise BrokerOrderError(msg, broker="groww")
```
`_groww_exc()` is dead code — delete it (never called, not worth keeping).

## Fix B detail — frontend agent

### `frontend/src/lib/data/pulseColumns.js` (line 463 area)

In the right-grid column def array, move the `Lots` column from its current 4th position
(before LTP) to after `Qty`. Also swap `Invested`/`Value` order to match Performance
(Invested before Value).

### `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`

Move the `Lots` column from its current position (after Symbol) to after `Qty`.

## Fix C detail — frontend agent

### `frontend/src/lib/PositionStrip.svelte` — `_resolveOptionSpot` (line 689)

Current priority order:
1. `underlying_ltp` from backend position row (stale — from book poll)
2. symbolStore snapshot keyed by tradingsymbol/root
3. Row-scan fallback on positions/holdings

New priority order:
1. symbolStore/ticker live spot for the underlying root (same as `liveSpot` in payoff curve)
2. `underlying_ltp` from backend position row (fallback when ticker not yet subscribed)
3. Row-scan fallback

The symbolStore holds the KiteTicker live price. Look at how the payoff curve derives
`liveSpot` and replicate that lookup as step 1 in `_resolveOptionSpot`.

## Fix D detail — frontend agent

### `frontend/src/lib/PositionStrip.svelte` — `_liveHoldingsTotal` derived (lines 493–507)

At the top of the derived block, add:
```javascript
void _throttledTick;
```
This registers `_throttledTick` as a reactive dependency so the derived only re-runs at 4Hz,
matching the cadence of P:1, H:1, and P:3.

## Fix E detail — frontend agent

### `frontend/src/lib/PositionStrip.svelte` — `_resolveOptionSpot` (line 689)

Current priority:
1. `p.underlying_ltp` (backend stamp — stale up to 5s)
2. symbolStore by resolved tradingsymbol
3. symbolStore by root name
4. symbolStore by `inst.u`
5. Row-scan fallback

New priority:
1. symbolStore by resolved tradingsymbol ← live ticker (same source as derivatives `liveSpot`)
2. symbolStore by root name
3. symbolStore by `inst.u`
4. `p.underlying_ltp` (backend stamp — fallback when ticker not yet subscribed)
5. Row-scan fallback

Move the `underlying_ltp` check from step 1 to step 4. Everything else stays the same.

## Files touched

- `backend/brokers/adapters/kite.py`
- `backend/brokers/adapters/dhan.py`
- `backend/brokers/adapters/groww.py`
- `backend/tests/broker/test_broker_resilience.py` (new cases)
- `frontend/src/lib/data/pulseColumns.js`
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`
- `frontend/src/lib/PositionStrip.svelte`

## Tests

- pytest: yes (broker adapter exception wrapping)
- svelte-check: yes
- playwright: no

## Commit message

fix(broker+ui): adapter place_order typed errors + column order + exp p&l spot ssot + h3 throttle

## Done when

1. `pytest backend/tests/broker/test_broker_resilience.py -v` — new cases confirm BrokerInputError
   raised from kite/dhan/groww place_order on input rejections
2. `svelte-check` — 0 errors
3. Pulse Lots column is after Qty; Invested before Value
4. Derivatives Lots column is after Qty
5. NavStrip P:3 Exp P&L spot uses live ticker first (matches derivatives page liveSpot)
6. NavStrip H:3 throttled to 4Hz (no per-tick re-derives)
