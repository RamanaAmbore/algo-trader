# Plan: 6d-audit punch list — P1/P2/P3 fixes

## Task
Fix all findings from the 6-dimension audit of commits c729b1d3, aecd1282, 139062cf, 8474a17e.
3 P1 correctness/doc bugs, 8 P2 behavioral/doc issues, 10 P3 cleanup items.
No production behavior changes beyond targeted fixes; no new features.

## Agents

- backend: Fix P1 test false-positive + missing import, P2 chase.py interval guard, P3 test cleanup (timezone import, hardcoded paths, filename, _fetch_account_margins rename, template_attach.py ntfy channel)
- frontend: Fix P2 derivatives timer cleanup + double-call, orders in-flight guard, PositionStrip holdings guard, OrderTicket close-button CSS, P3 NavBreakdown legacy load sig + dead CSS
- doc: Fix P1 DESIGN_GUIDE Dhan false claim, P2 DESIGN_GUIDE closure syntax + _sync_algo_order_id prose + interval_seconds, P2 NAVSTRIP_SPEC + CLAUDE.md stale-snapshot guard, P3 DESIGN_GUIDE actions.py refs
- backend-test: skip
- playwright: skip

## Detailed Fix Specs

### BACKEND (backend agent)

**P1 — test_11_defect_patch.py false positive (lines 80-85)**
File: `backend/tests/test_11_defect_patch.py`
Current: asserts `"_fire_guard_alert" in src or "result.errors" in src` — both true pre-fix; tautological.
Fix: replace with assertions for the specific NEW function added in 8474a17e:
```python
assert "_fire_attach_fail_alert" in src, (
    "template_attach must call _fire_attach_fail_alert when result.errors is non-empty"
)
assert "fire_attach_fail_alert" in src and "result.errors" in src, (
    "alert must be called after errors are collected, not unconditionally"
)
```

**P1 — test_11_defect_patch.py missing import (line 133)**
File: `backend/tests/test_11_defect_patch.py`
Fix: add `import pytest` at top of file (already has `import inspect`, `from pathlib import Path` etc).

**P2 — chase.py interval_seconds=0 inconsistency (line 649)**
File: `backend/api/algo/chase.py`
Current: `if hasattr(row, "next_attempt_at") and interval_seconds:` — falsy for 0
Fix: `if hasattr(row, "next_attempt_at") and interval_seconds is not None:`
(matches the guard on line 651: `and interval_seconds is not None`)

**P3 — test_agents_routes.py unused import (line 10)**
File: `backend/tests/test_agents_routes.py`
Fix: Remove `, timezone` from `from datetime import datetime, timezone`

**P3 — hardcoded absolute paths in backend tests**
Files: `backend/tests/test_11_defect_patch.py` (lines 26, 41) and all 20 new test files that use `Path("backend/...")`.
The 20 new test files already use relative `Path("backend/api/...")` which works when pytest runs from repo root — no change needed.
`test_11_defect_patch.py` uses `Path(__file__).parent.parent` — verify these are already repo-relative; if not, fix to use `Path(__file__).parent.parent / "api/..."` pattern.

**P3 — test_11_defect_patch.py filename mismatch**
The file is named `test_11_defect_patch.py` but the content/commit says "12-defect patch".
Fix: Rename file to `test_12_defect_patch.py` and update any `pytest.main([__file__])` reference.

**P3 — actions_preflight.py naming convention**
File: `backend/api/algo/actions_preflight.py`
Current: `async def _fetch_account_margins(` at line 544 — breaks `_preflight_fetch_*` convention.
Fix: Rename to `_preflight_fetch_account_margins` and update the single call site in `run_preflight` (asyncio.gather block, same file).

**P3 — template_attach.py ntfy channel missing**
Files: `backend/api/algo/template_attach.py` lines 180-205 (`_fire_guard_alert._do_telegram`) and 310-330 (`_fire_attach_fail_alert._do_telegram`)
Fix: Add ntfy delivery alongside telegram in both alert helpers. Pattern to follow: `send_ntfy_alert` from `backend.shared.helpers.alert_utils`. Use `priority="urgent"` (critical exit failure). Keep telegram as primary, ntfy as secondary. Wrap each in its own try/except so one failure doesn't suppress the other.

### FRONTEND (frontend agent)

**P2 — derivatives/+page.svelte: timer not cleared in onDestroy (line 3791)**
File: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
Current onDestroy: clears `_urlSyncTimer` but not `_orderUpdateTimer`.
Fix: add `if (_orderUpdateTimer) { clearTimeout(_orderUpdateTimer); _orderUpdateTimer = null; }` inside `onDestroy`.

**P2 — derivatives/+page.svelte: double broker call per fill**
Same file, lines 3771-3784.
The `order_update` path debounces 200ms then calls `loadPositions({fresh:true})`.
The `position_filled` path immediately calls `loadPositions({fresh:true})`.
Both fire on a terminal fill (COMPLETE) because `_postback_broadcast_fanout` emits both events.
Fix: In the `order_update` handler, skip the debounced reload when `msg.status` indicates a terminal state (COMPLETE/REJECTED/CANCELLED) — those will be handled by `position_filled`. Only debounce non-terminal postbacks (OPEN, TRIGGER PENDING, etc.).
```javascript
if (msg?.event === 'order_update') {
  const terminal = ['COMPLETE','REJECTED','CANCELLED'].includes(msg.status || '');
  if (!terminal) {
    if (_orderUpdateTimer) clearTimeout(_orderUpdateTimer);
    _orderUpdateTimer = setTimeout(() => {
      _orderUpdateTimer = null;
      loadPositions({ fresh: true });
    }, 200);
  }
  return;
}
```

**P2 — orders/+page.svelte: no in-flight guard on order_update (line 260)**
File: `frontend/src/routes/(algo)/orders/+page.svelte`
Current: `loadOrders()` called directly with no guard — N concurrent calls for N basket legs.
Fix: add a debounce (50ms is sufficient since this doesn't need 200ms) or an in-flight flag. Use the existing `_debouncedLoadOrders` already defined in the file instead of calling `loadOrders()` directly:
```javascript
if (msg.event === 'order_update' || msg.event === 'performance_updated') {
  _debouncedLoadOrders();
}
```

**P2 — PositionStrip.svelte: holdings missing zero-flash guard (line 519)**
File: `frontend/src/lib/PositionStrip.svelte`
Current:
```javascript
if (positions.length > 0 || _livePositionsToday !== 0) {
  dispPositionsToday = _livePositionsToday;
}
dispHoldingsToday = _liveHoldingsToday;  // ← no guard
```
Fix: add symmetric guard for holdings. Use `holdings` array length (same pattern):
```javascript
if (holdings.length > 0 || _liveHoldingsToday !== 0) {
  dispHoldingsToday = _liveHoldingsToday;
}
```
Need to verify `holdings` is in scope at this point in the `$effect` block.

**P2 — OrderTicket.svelte: close button disabled has no CSS feedback (line 1947)**
File: `frontend/src/lib/order/OrderTicket.svelte`
Current: `<button class="ot-close" ... disabled={submitting}>` — no CSS for `:disabled` state.
Fix: add CSS rule near the existing `.ot-submit:disabled` at line ~3071:
```css
.ot-close:disabled { opacity: 0.35; cursor: not-allowed; }
```

**P3 — NavBreakdown.svelte: legacy load signature (line 132)**
File: `frontend/src/lib/NavBreakdown.svelte`
Current: `positionsStore.load(undefined, { force: true })` (2-arg legacy)
Fix: `positionsStore.load({ fresh: true })` — matches updated signature used in derivatives page.
Same for `holdingsStore` and `fundsStore` on lines 133-134.

**P3 — OrderTicket.svelte: dead CSS selectors**
File: `frontend/src/lib/order/OrderTicket.svelte`
Lines 2713-2714: `.ot-pill[disabled]` and `.ot-pill[disabled]:hover` — never applied (class is `.ot-pill-disabled`, not attribute).
Line 2812: `.ot-side-toggle-compact .ot-side-btn[disabled]` — `ot-side-btn` never rendered with `disabled` attr.
Fix: remove all 3 dead selectors (svelte-check will confirm 0 warnings after).

### DOC (doc agent)

**P1 — DESIGN_GUIDE.md:3760 false Dhan claim**
File: `docs/DESIGN_GUIDE.md`
Current: "correctly routes CDS/BCD currencies to the currency segment key"
Reality: Dhan flat-dict is returned whole (early return when `"net" in m`); no segment-key routing for Dhan.
Fix: Replace with accurate description: "detects Dhan's flat margin dict (presence of 'net' or 'available' key) and returns it unchanged, bypassing Kite's nested segment-key lookup."

**P2 — DESIGN_GUIDE.md:3730,3750-3753: stale closure syntax**
File: `docs/DESIGN_GUIDE.md`
Current code snippet shows `_fetch_profile()`, `_fetch_instruments()`, `_fetch_basket_margin()`, `_fetch_account_margins()` as no-arg closure calls.
After c729b1d3 these are module-level helpers: `_preflight_fetch_profile(broker, loop, account)` etc.
Fix: update the snippet to show the current explicit-arg call form. Also update the WHERE reference from `actions.py::run_preflight` to `actions_preflight.py::run_preflight`.

**P2 — DESIGN_GUIDE.md:1666,1668-1671: _sync_algo_order_id prose**
File: `docs/DESIGN_GUIDE.md`
Current: mentions `broker_order_id + current_limit`; timing columns note lists only `next_attempt_at` + `last_attempt_at`.
Fix: add `interval_seconds` to both the prose and the columns note.

**P2 — NAVSTRIP_SPEC.md: missing stale-snapshot guard**
File: `docs/specs/NAVSTRIP_SPEC.md` lines 324-349 (`baseDayPnlForPosition` formula section)
Fix: add the `close === ltp → return 0` guard as a case in the formula table. Describe when it fires: "when the broker hasn't refreshed close_price since last session (close === ltp), the formula would produce 0 anyway; return 0 early to avoid stale subtraction."

**P2 — CLAUDE.md: missing stale-snapshot guard in Day P&L section**
File: `CLAUDE.md` (project root) — Day P&L formula section
Current: documents Cases 1 and 3 of `apply_day_change_backstop`; frontend SSOT section mentions `baseDayPnlForPosition` and lists consumers.
Fix: add note: "Case 4 (stale close guard): when `close === ltp`, `baseDayPnlForPosition` returns 0 — formula `oq*(ltp-close)` would be 0 anyway; avoids stale subtraction during overnight window."

**P3 — DESIGN_GUIDE.md: four stale actions.py refs**
File: `docs/DESIGN_GUIDE.md` at lines 3730, 3799, 3888, 4330
Current: `backend/api/algo/actions.py::run_preflight`
Fix: `backend/api/algo/actions_preflight.py::run_preflight` (re-export still works at runtime but doc navigation is wrong)

**P3 — DESIGN_GUIDE.md: fees constants undocumented**
Low priority; add a brief mention of `_BROKERAGE_PER_ORDER=₹20`, STT/ancillary/GST rates in the sim fees section. One paragraph max.

## Tests
- pytest: yes (backend changes + rename/import fixes)
- svelte-check: yes (CSS dead selector removal + disabled CSS addition)
- playwright: no

## Commit message
fix(audit): 6d-audit punch list — P1 test false-positive + import, P2 frontend timer/guard/CSS, P3 cleanup

## Done when
- `test_11_defect_patch.py` (or `test_12_defect_patch.py` after rename) assertions target `_fire_attach_fail_alert` specifically; `import pytest` present
- `derivatives/+page.svelte` onDestroy clears `_orderUpdateTimer`; terminal fills don't double-load
- `orders/+page.svelte` `order_update` uses `_debouncedLoadOrders`
- `PositionStrip.svelte` holdings guard matches positions guard
- `OrderTicket.svelte` `.ot-close:disabled` rule added; 3 dead CSS selectors removed (svelte-check 0 warnings)
- `NavBreakdown.svelte` uses `load({ fresh: true })` form for all 3 stores
- `chase.py` line 649 uses `is not None` guard
- `_fetch_account_margins` renamed to `_preflight_fetch_account_margins`
- `template_attach.py` both alert helpers call ntfy + telegram
- DESIGN_GUIDE Dhan claim corrected, closure syntax updated, `interval_seconds` added, 4 stale refs fixed
- NAVSTRIP_SPEC + CLAUDE.md document the stale-snapshot guard
- pytest green (1 pre-existing failure: test_options_spot MCX CRUDEOIL 502 — unrelated)
- svelte-check 0 errors, warnings reduced by ≥3 (dead CSS removed)
