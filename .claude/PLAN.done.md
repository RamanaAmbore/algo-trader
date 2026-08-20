# Plan: Fix positions previous_close + funds/margin snapshot gate + 5-window P&L tests

## Context

Three related bugs:

**Bug 1 — Positions day P&L zeroing at settlement** (same root cause as holdings fix 75a335f7):
`PositionRow` has no `previous_close` field. `_override_stale_close_from_snapshot()` queries only `ltp`
(not `COALESCE(previous_close, ltp)`). Frontend `positionsDayPnlStore`, `pulseUnified`, and `nav.js`
all read `close_price` without `previous_close` fallback.

**Bug 2 — Cash and margin values incorrect during closed hours**:
`backend/api/routes/funds.py` has NO `closed_hours_or_broker()` gate. Every other market-data route
(positions:1140, holdings:759) uses the gate; funds doesn't. It calls broker unconditionally, returning:
- Pre-settlement stale balances during NSE settlement window (15:30–16:15)
- Frozen yesterday's-EOD values during MCX-open/NSE-closed and both-closed windows
- No `as_of` timestamp to signal staleness to UI

`backend/api/algo/nav.py:_fetch_funds_phase()` (~line 277) also calls `fetch_margins()` unconditionally.

**Bug 3 — No edge-case tests for the 5 market-state windows**:
W3 (NSE-close transition, `close_price` drifts to settlement) and W5 (day boundary, `previous_close`
frozen) have no explicit coverage for positions, holdings, cash, or margin.

---

## Task

### Part A — Positions previous_close fix (mirrors holdings fix 75a335f7)

1. **`backend/api/schemas.py`**: add `previous_close: float = 0.0` to `PositionRow`

2. **`backend/api/routes/positions.py`**:
   - `_override_stale_close_from_snapshot()` (~line 864): change SQL to
     `COALESCE(daily_book.previous_close, daily_book.ltp) AS ref_close`.
     Initialize `raw['previous_close'] = 0.0` unconditionally at top of function;
     fill matched rows with ref_close value for ALL matched rows (not just patched ones).
   - Expose `'previous_close'` in whatever column select/list passes fields into PositionRow
     construction (mirror `_ROW_COLS` pattern in holdings.py:266).
   - Do NOT change `apply_day_change_backstop()` call — already present.

3. **`frontend/src/lib/data/positionsDayPnlStore.svelte.js`** (~line 63):
   `closePx: Number(r.close_price ?? 0)` → `Number(r.previous_close) || Number(r.close_price ?? 0)`

4. **`frontend/src/lib/data/pulseUnified.js`** (~line 461):
   `const posCls = Number(r.close_price) || 0;` → `Number(r.previous_close) || Number(r.close_price) || 0;`

5. **`frontend/src/lib/data/nav.js`** (~line 106):
   Add `previous_close` as first preference:
   `const close = Number(p?.previous_close ?? p?.close_price ?? p?.prev_close ?? 0);`

### Part B — Cash and margin snapshot gate fix

6. **`backend/api/routes/funds.py`**:
   - Add `closed_hours_or_broker()` gate (same pattern as positions:1140, holdings:759).
   - For snapshot path: read the most recent cached funds value. Since `daily_book` has no `kind='funds'`
     entries, use `api/cache.py`'s `get_or_fetch` with market-state-aware TTL:
     - Market open → 30s TTL (current behavior)
     - Market closed → 3600s TTL (serve last known value; avoids pre-settlement stale broker calls)
   - Alternatively if `daily_snapshot.py` already has a funds-capture hook, use that snapshot instead.
     Backend agent: read `backend/api/algo/daily_snapshot.py` and `backend/api/helpers/snapshot_gate.py`
     to pick the simplest pattern that fits.
   - Add `source` tag (`'live'` / `'snapshot'`) to response so UI can show `as_of` when stale.

7. **`backend/api/algo/nav.py`** `_fetch_funds_phase()` (~line 277):
   - Apply same market-hours TTL: skip broker call when `not _any_segment_open()` and cached value exists.
   - Use `cache.peek("funds")` to return last known value without triggering a fetch during closed hours.

### Part C — Comprehensive 5-window tests

Create **`backend/tests/test_market_window_pnl_edge_cases.py`** covering all five IST windows
for positions, holdings, cash, and margin:

| Window | IST | NSE | MCX | Key test |
|---|---|---|---|---|
| W1 | 09:00–09:15 | Closed | Open | MCX LTP updates don't stale-zero positions day P&L |
| W2 | 09:15–15:30 | Open | Open | Normal session: formula `(ltp − previous_close) × qty` fires |
| W3 | 15:30–23:30 | Closed | Open | **Critical**: `close_price = ltp = settlement` doesn't zero day P&L; `previous_close` still > 0 |
| W4 | 23:30–09:00 | Closed | Closed | Snapshot served; no broker calls; last cached funds value returned |
| W5 | Day boundary | Closed→Open | — | `previous_close` frozen at yesterday's settlement; `ltp` resets; formula correct |

**Per-window assertions for each surface:**
- Positions `day_change_val` uses `previous_close` not Kite's drifting `close_price`
- Holdings `day_change_val` same
- Cash value: non-zero, non-stale (last known value served during closed hours)
- Margin value: same as cash
- `total_pnl` not corrupted by settlement-price drift

**W3 critical case (must have):**
Build raw DataFrame with `close_price = ltp = settlement_price` (within 0.005 epsilon);
verify `previous_close` differs from `close_price`; verify `day_change_val` = `(ltp − previous_close) × qty`, not 0.

**W5 day-boundary case (must have):**
Simulate next-day open: `previous_close = yesterday_settlement_ltp`, `ltp = new_session_price`;
verify day P&L formula fires and is not mistakenly 0.

**Funds/margin closed-hours test (must have):**
Mock `_any_segment_open() = False`; call funds route; verify cached value returned (broker NOT called);
verify response includes `source = 'snapshot'` or similar indicator.

Also add **`frontend/src/lib/__tests__/data/positionsDayPnlStore.test.js`** (new Vitest file) covering:
- `previous_close` wins over `close_price` when non-zero
- Fallback to `close_price` when `previous_close` absent
- Post-settlement guard fires correctly when `previous_close ≈ ltp`

---

## Agents

- backend: (1) `backend/api/schemas.py`: add `previous_close: float = 0.0` to `PositionRow`. (2) `backend/api/routes/positions.py`: update `_override_stale_close_from_snapshot()` to query `COALESCE(previous_close, ltp)`, write to ALL rows; add `'previous_close'` to column select. (3) `backend/api/routes/funds.py`: add `closed_hours_or_broker()` gate — when market closed, use `cache.peek("funds")` to return last cached value without broker call; when market open, current 30s TTL applies. Add `source` field to response. (4) `backend/api/algo/nav.py` `_fetch_funds_phase()`: skip broker when closed + cached value exists via `cache.peek`. Read `backend/api/helpers/snapshot_gate.py`, `backend/api/routes/holdings.py:759`, and `backend/api/routes/positions.py:1140` as reference patterns before implementing.
- frontend: (1) `positionsDayPnlStore.svelte.js` ~line 63: prefer `previous_close`. (2) `pulseUnified.js` ~line 461: prefer `previous_close`. (3) `nav.js` ~line 106: prefer `previous_close`. (4) New Vitest file `frontend/src/lib/__tests__/data/positionsDayPnlStore.test.js` with three tests: previous_close wins, fallback, post-settlement guard.
- broker: skip
- doc: skip
- backend-test: Create `backend/tests/test_market_window_pnl_edge_cases.py` covering W1–W5 for positions day P&L, holdings day P&L, cash, margin. Critical cases: W3 close_price=ltp=settlement drift, W5 day-boundary previous_close frozen, W4 funds closed-hours returns cached value without broker call. Use mock patterns from `test_day_change_closed_hours.py` and `test_closed_hours_snapshot_routes.py`.
- playwright: skip

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

---

## Commit message

fix(positions,funds): expose previous_close in PositionRow + snapshot gate for funds/margin

Positions day P&L now uses previous_close (COALESCE frozen field) instead of Kite's mutable
close_price, preventing zeroing at NSE settlement (mirrors holdings fix 75a335f7).

Funds route gains closed_hours_or_broker gate: returns last-cached value during closed hours
instead of stale pre-settlement broker responses. nav.py _fetch_funds_phase() same fix.

Adds 5-window edge-case tests (W1–W5) for positions, holdings, cash, and margin day P&L.

---

## Done when

- `PositionRow` API response includes `previous_close` (non-zero for any position with prior-session snapshot)
- positionsDayPnlStore, pulseUnified, nav.js all prefer `previous_close` over `close_price`
- Funds route returns cached value (not fresh broker call) when both markets closed
- `test_market_window_pnl_edge_cases.py` covers W1–W5 for positions + holdings + cash + margin
- W3 settlement-drift and W5 day-boundary explicitly tested and green
- pytest green, svelte-check 0 errors
