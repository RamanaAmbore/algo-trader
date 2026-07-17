# Plan: 12-defect patch — chart, order ticket, chase, payoff, margin, callbacks, template alerts, NavStrip P-slot

## Context

Comprehensive audit of chart range selector, order ticket, chase, payoff expiry curve,
margin/cash calculations, order callback UI freshness, template reject alerts, and NavStrip
P-slot. Twelve defects found across P1–P2; all addressed in one batch. Chase uses
cancel+replace (not modify) — correct behaviour, but executor pool (max_workers=4) can
saturate and delay first attempt beyond 1 minute. Template reject alerts only fire on
`applies_to` mismatch; G1/GTT/wing failures are silent — root cause of missed exit-arm
alerts. Dhan margin gate silently passes every order (flat dict `.get("equity")` → `{}`).
NavStrip P-slot collapses to zero at market close because `PositionStrip.svelte` overwrites
`dispPositionsToday` with 0 during the live→snapshot transition window (positions array
briefly empty); `baseDayPnlForPosition` also falls through to the formula when
`day_change_val = 0` for overnight snapshot rows whose `close_price` may equal `ltp`.

---

## Agents

### frontend

Fix 6 issues in `frontend/`:

**1 — `_setRange()` missing state reset (`frontend/src/lib/ChartWorkspace.svelte`)**
`_setRange(d)` at line ~799 does NOT clear `_bars`, `_spotBars`, `_histLoading`,
`_histError`, `_histRetrying`, retry timers, `_emptyRetryFired`, `_partialRetryFired`,
`_emptyGateSuppressed` before calling `_loadHistorical(true)`. Symbol-change effect at
line ~1586 does all of this. Copy the full state reset from the symbol-change effect into
`_setRange()` before the `_loadHistorical(true)` call. Also add `_intradayOn = false`
(to avoid stale tick stream data from prior intraday session leaking into a new range).
Do NOT clear `zoom` or `_chartHover` (already done) and do NOT clear `_chartDays`
(that's what we're changing to).

**2 — Order ticket duplicate-submit guard (`frontend/src/lib/order/OrderTicket.svelte`)**
The `$effect` that watches `triggerSubmit` (line ~1354) calls `submit()` with no
`submitting` guard. If `triggerSubmit` is incremented twice rapidly, two live orders fire.
Add `if (submitting) return;` as the first line of the `triggerSubmit` effect body.

**3 — `positionsStore.load` args bug (`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`)**
Line ~3345: `positionsStore.load(undefined, { force: fresh })` passes `fresh` as `opts`
(second arg), not as `args` (first arg). `createDataStore` forwards `args` to the fetcher
as query params; `opts` is a dedup flag only. So `?fresh=1` never reaches the backend.
Change to `positionsStore.load({ fresh: true })` (single arg). Search for all other
`positionsStore.load` call sites in the derivatives page and fix the same pattern if found.

**4 — Order callback UI freshness (`frontend/src/routes/(algo)/orders/+page.svelte` +
`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`)**

Orders page (orders/+page.svelte):
- The `order_update` WS event triggers `_debouncedLoadOrders()` (250ms debounce). Remove
  the debounce — call `loadOrders()` directly on `order_update` (same as the button does).
- Ensure `loadOrders()` passes `fresh: true` so the 30s backend orders cache is bypassed.

Derivatives page (derivatives/+page.svelte):
- Non-terminal postbacks (TRIGGER_PENDING → OPEN transitions) do NOT fire `book_changed`,
  so the payoff/strategy grid misses intermediate state changes. Wire `order_update` WS
  event in the derivatives page to also call `loadPositions({ fresh: true })` (with a
  200ms debounce to batch rapid fills).

**5 — Chase countdown display (`frontend/src/lib/order/ChaseCard.svelte`)**

Backend will add `interval_seconds` and `next_attempt_at` to the chase API row (see
backend agent). In ChaseCard.svelte:
- Add a reactive countdown: compute `_secsLeft = Math.max(0, Math.round((row.next_attempt_at - Date.now()/1000)))`.
  Display as "next re-quote in {_secsLeft}s" next to the attempts count.
- Change the "Age" column from `_age(row.created_at)` to `_age(row.last_attempt_at || row.created_at)`
  so it shows time since last re-quote, not total chase lifetime.
- If `_secsLeft === 0` and `row.status === 'active'`, show "re-quoting…" instead of the
  countdown.

**6 — Order modal close guard while submitting (`frontend/src/lib/order/OrderTicket.svelte`)**
The `×` close button (line ~1742) and Escape handler (line ~1943) don't check `submitting`.
Disable the close button (`disabled={submitting}`) and add `if (submitting) return;` guard
at the top of the Escape key handler.

---

### backend

Fix 5 issues in `backend/`:

**1 — Chase: expose `next_attempt_at` and `last_attempt_at` in API row (`backend/api/algo/chase.py` + `backend/api/routes/orders_helpers.py`)**

In `chase.py`, after each cancel+place cycle, record `result.last_attempt_at = now` and
compute `result.next_attempt_at = now + cfg.interval_seconds`. Write both to the AlgoOrder
DB record (or to the in-memory result object that the status poller reads).

In `orders_helpers.py`, add `interval_seconds: int`, `next_attempt_at: Optional[float]`,
and `last_attempt_at: Optional[float]` to `AlgoOrderInfo` (with `None` defaults for
back-compat). Populate from the chase state when serializing.

Also: increase `_executor` `max_workers` from 4 to 8 in `chase.py` to reduce thread-pool
saturation risk that delays first attempt.

**2 — Dhan preflight margin gate fix (`backend/api/algo/actions_preflight.py`)**

Line ~684: `_fetch_account_margins` does `margins_data.get(segment, {})` where
`margins_data` for Dhan is a flat dict (no segment nesting). This returns `{}` for every
Dhan order — margin check silently passes. Fix: detect Dhan flat dict vs Kite nested dict.
If `"net"` is a top-level key (Dhan shape), use the dict directly without `.get(segment)`.
If `"equity"` / `"commodity"` are top-level keys (Kite shape), slice by segment as now.

**3 — CDS/BCD segment misrouting (`backend/api/algo/actions_preflight.py`)**

Line ~652: `segment = "commodity" if exchange in ("MCX", "NCO") else "equity"`. CDS and
BCD currency derivatives belong to `"currency"`, not `"equity"`. Change to:
```python
if exchange in ("MCX", "NCO"):
    segment = "commodity"
elif exchange in ("CDS", "BCD"):
    segment = "currency"
else:
    segment = "equity"
```

**4 — Funds cache invalidation after fill (`backend/api/routes/orders.py`)**

`_rco_invalidate_terminal_caches()` (line ~390) does not call `invalidate("funds")`.
After a fill, the `/api/funds` response stays stale for up to 30s. Add `invalidate("funds")`
to the terminal-status cache-clearing block. Check `backend/api/persistence/runtime_state.py`
or wherever `invalidate()` is defined for funds to find the correct key.

**5 — Template attachment: alert on ALL failures, not just `applies_to` (`backend/api/algo/template_attach.py`)**

Currently `_fire_guard_alert` only fires on `applies_to` mismatch (line ~1544). G1 guard
failures (line ~1401), translate_qty errors (line ~1353), GTT/wing placement failures (line
~1360-1375) append to `result.errors` silently. Operator can miss unattached exits.

Add a consolidated alert at the point where `apply_plan_live` or `apply_template_to_order`
returns with non-empty `result.errors` (i.e., exits were not attached despite parent filling).
Use the existing `_fire_guard_alert` infrastructure but with a different `reason` prefix
("attach failed — {errors[0]}"). Send Telegram only (not email) for these — they're
operational failures, not security events. Do NOT duplicate the existing `applies_to` alert.

---

### backend-test

Write `backend/tests/test_11_defect_patch.py`. Five test dimensions:

1. **SSOT — Dhan margin gate**: import `_fetch_account_margins` (or inspect source); assert the
   flat-dict path (Dhan shape `{"net": 100000, "available": 80000}`) is not passed through
   `.get("equity", {})` — confirm "net" key is accessible directly.

2. **SSOT — CDS/BCD segment**: call the segment-resolution logic with `exchange="CDS"` and
   `exchange="BCD"`; assert result is `"currency"` not `"equity"`.

3. **Stale — Funds invalidation**: inspect source of `_rco_invalidate_terminal_caches` in
   `orders.py`; assert `"funds"` appears in the invalidation calls.

4. **Stale — Template silent failures**: inspect `template_attach.py` source; assert that a
   call path with non-empty `result.errors` (post-G1-failure or GTT-failure) also fires an
   alert (not just `applies_to` path).

5. **Correctness — `positionsStore.load` args**: source-scan `derivatives/+page.svelte`;
   assert `positionsStore.load(undefined,` does NOT appear (confirming the args fix).

6. **UX — NavStrip P-slot formula guard**: import (or source-scan) `baseDayPnlForPosition`
   from `nav.js`; assert that when `overnight_quantity > 0`, `day_change_val = 0`, and
   `close_price === last_price` (stale snapshot), the function returns `0` not a large
   formula result. Also assert that when `close_price > 0` and `close_price !== last_price`,
   it returns `pnl - oq * (close - avg)` (correct formula).

After writing, run:
```
cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/test_11_defect_patch.py -v
```

---

**7 — NavStrip P-slot zero at market close (`frontend/src/lib/PositionStrip.svelte` + `frontend/src/lib/data/nav.js`)**

Two-part fix:

*Part A — Transition window guard (PositionStrip.svelte):*
At market close the live broker positions array goes empty before the snapshot arrives.
Line ~512 unconditionally does `dispPositionsToday = _livePositionsToday`, which writes 0
when `_livePositionsToday === 0` (empty array sum). Fix: only overwrite `dispPositionsToday`
when the new value is non-zero OR when the positions array is genuinely non-empty (so a
real zero is still recorded). Pattern:
```javascript
if (_positions.length > 0 || _livePositionsToday !== 0) {
    dispPositionsToday = _livePositionsToday;
}
```
This retains the last known non-zero day P&L during the snapshot loading gap.

*Part B — `baseDayPnlForPosition` formula fallback (nav.js):*
Line ~106: `if (oq > 0 && dcv !== 0) return dcv;` — when snapshot rows have
`day_change_val = 0` (day_pnl stored as NULL/0 in DB for overnight positions), this
condition fails and falls through to `pnl - oq * (close - avg)`. This formula is only
correct when `close_price = previous_close` (the fix from commit 910740f0). However if
`previous_close` was NULL and `close_price` fell back to `ltp`, the formula produces
`pnl - oq * (ltp - avg)` = total unrealized P&L, not day P&L.

Guard: when `oq > 0 && dcv === 0`, check if `close_price` is available and > 0 before
applying the formula. If `close_price === 0` or `close_price === ltp` (stale snapshot
with no prior settlement), return `dcv` (0) rather than a wrong formula result — it's
better to show 0 than a distorted multi-lakh number. The condition becomes:
```javascript
if (oq > 0 && dcv !== 0) return dcv;
if (oq > 0 && dcv === 0) {
    const close = Number(p?.close_price ?? 0);
    const ltp   = Number(p?.last_price ?? 0);
    if (close > 0 && close !== ltp) return pnl - oq * (close - avg);
    return 0;  // no reliable close_price — don't distort
}
```

---

### doc: skip
### broker: skip
### playwright: skip

---

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(multi): 12-defect patch — chart range, order ticket, chase, payoff, margin, callbacks, template alerts, NavStrip P-slot

Patches chart range selector state reset, duplicate submit guard, positionsStore fresh
arg, order callback debounce, chase countdown UI, close-while-submitting guard, Dhan
margin gate, CDS/BCD segment, funds cache invalidation, chase executor size, and template
silent-failure alerts.

## Done when

1. Switching chart range (1M→3M→6M→1Y) immediately clears old bars and shows correct x-axis span.
2. Order ticket triggerSubmit effect has `if (submitting) return` guard; close button disabled while submitting.
3. `positionsStore.load({ fresh: true })` (single-arg form) in derivatives page.
4. Orders page WS `order_update` calls `loadOrders()` directly (no 250ms debounce).
5. Derivatives page wires `order_update` → `loadPositions({ fresh: true })`.
6. ChaseCard shows "next re-quote in Xs" countdown and "time since last re-quote" age.
7. Dhan preflight reads `net` from flat margin dict, not `.get("equity", {})`.
8. CDS/BCD exchange maps to `"currency"` segment.
9. `_rco_invalidate_terminal_caches` calls `invalidate("funds")`.
10. Template attach failures with non-empty `result.errors` send Telegram alert.
11. `venv/bin/pytest backend/tests/test_11_defect_patch.py` — 5 tests pass.
12. `svelte-check` — 0 errors.
13. NavStrip P-slot retains last non-zero day P&L during live→snapshot transition (no zero flash at market close).
14. `baseDayPnlForPosition` with stale snapshot (`close_price = ltp`) returns 0, not a distorted formula value.
