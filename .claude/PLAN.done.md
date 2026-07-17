# Plan: Coverage uplift — 80% target, 20 backend files + 5 Playwright specs

## Context

Current coverage: **68%** (42,474 / 62,811 stmts). Target: **≥80%** = need ~7,775 more
statements covered. Plan has three phases: (1) 10 files already planned → ~72%, (2) 10 more
files → ~76%, (3) background + sim source-scan + Playwright E2E specs → ~80%.

Priority matrix (by missing stmts × testability):

| Module | Current | Missing | Type |
|---|---|---|---|
| `api/background.py` | 17% | 1,870 | Async tasks — source-scan + helper extraction |
| `api/algo/sim/driver.py` | 17% | 1,180 | Simulator — scenario parse, tick math |
| `api/algo/chase.py` | 21% | 369 | Critical, just patched |
| `api/routes/orders_basket.py` | 0% | 260 | Critical trading path |
| `api/routes/orders_place.py` | 34% | 411 | Critical trading path |
| `api/routes/orders.py` | 35% | 464 | Postback + invalidation |
| `api/algo/investor_statement.py` | 0% | 232 | Pure computation |
| `api/algo/sim/synthesize.py` | 12% | 229 | Tick synthesis math |
| `api/database.py` | 23% | 239 | Migration DDL checks |
| `api/algo/expiry.py` | 0% | 349 | Pair/partner logic |
| `api/routes/agents.py` | 23% | 423 | ISO parse, lifespan logic |
| `shared/helpers/fees.py` | 0% | 41 | Pure math, 100% testable |
| `api/algo/lot_ledger.py` | 0% | 145 | FIFO math, unit testable |
| `api/algo/shadow.py` | 0% | 62 | Small, deterministic |
| `api/algo/grammar.py` | 54% | 115 | Grammar parsing helpers |
| `api/algo/grammar_registry.py` | 36% | 56 | Registry lookup |
| `api/algo/events.py` | 26% | 60 | Event dispatch |
| `api/auth_guard.py` | 39% | 65 | JWT token helpers |
| `api/algo/investor_units.py` | 41% | 66 | Unit nav math |
| `api/algo/nav.py` | 51% | 77 | NAV computation |
| `api/algo/agent_evaluator.py` | 65% | 86 | Condition evaluation |
| `api/routes/research.py` | 30% | 568 | Hash/token, serialization |

---

## Agents

### backend: skip
### frontend: skip
### broker: skip
### doc: skip

### backend-test

Write **20 backend test files** (one per module) and confirm all pass.
Read the target module before writing each test.
Use project patterns: `inspect.getsource` / `Path.read_text()` for structural assertions;
`pytest.mark.asyncio` + `AsyncMock` for async paths; minimal mocking. 5-dimension docstring
per file (SSOT, Perf, Stale, Reuse, UX). Do NOT weaken assertions if production code is
missing a feature — report it as a finding.

Run all 20 files at the end and report pass/fail per file, then full coverage.

---

### Phase 1 — 10 files (target: 72%+)

**File 1 — `backend/tests/test_orders_basket.py`** (0% → 15%+)

Module: `backend/api/routes/orders_basket.py`

Tests (6):
1. **SSOT**: Source-scan — `translate_qty` or `broker.translate_qty` appears per leg.
2. **SSOT**: Source-scan — `basket_order_margins` called in preflight path.
3. **SSOT**: Source-scan — G2 fat-finger cap (`FAT_FINGER_5_LOT_CAP` or equivalent) present.
4. **Perf**: Source-scan — lots→contracts multiplication appears exactly once per leg path.
5. **Correctness**: Unit — leg `lots=2, lot_size=50` → broker-captured qty == 100. Mock `broker.basket_order`.
6. **UX**: Source-scan — HTTP 400/422 raised when a leg has `qty=0` or `lots=0`.

---

**File 2 — `backend/tests/test_chase_extended.py`** (21% → 32%+)

Module: `backend/api/algo/chase.py`

Tests (6):
1. **SSOT**: Source-scan — `cancel_order` and `place_order` both appear; `modify_order` does NOT.
2. **SSOT**: Source-scan — `max_workers=8` present.
3. **SSOT**: Source-scan — `next_attempt_at` and `last_attempt_at` assigned inside the loop body.
4. **Perf**: Source-scan — `cfg.interval_seconds` drives `asyncio.sleep` (no literal `20`).
5. **Correctness**: Source-scan — `result.attempts` incremented BEFORE the broker cancel/place block.
6. **Stale**: Source-scan — `_KILLED_LOCK` and a TTL expiry appear together (killed-set bounded).

---

**File 3 — `backend/tests/test_orders_place_lots.py`** (34% → 45%+)

Module: `backend/api/routes/orders_place.py`

Tests (6):
1. **SSOT**: Source-scan — `_resolve_fno_qty` or `lots * lot_size` in `_ticket_validate_input`.
2. **SSOT**: Source-scan — G1 (LOT_MULTIPLE) NOT in `_ticket_enforce_lot_and_fat_finger`.
3. **SSOT**: Source-scan — G2 bypassed when `intent == "close"`.
4. **Perf**: Source-scan — lots→contracts multiplication appears once (no double-multiply).
5. **Correctness**: Unit — `_resolve_fno_qty(lots=1, lot_size=25)` == 25; `(lots=3, lot_size=50)` == 150.
6. **UX**: Source-scan — 400/422 raised when `lots <= 0`.

---

**File 4 — `backend/tests/test_orders_postback.py`** (35% → 45%+)

Module: `backend/api/routes/orders.py`

Tests (6):
1. **SSOT**: Source-scan `_rco_invalidate_terminal_caches` — `"positions"`, `"holdings"`, `"funds"` all present.
2. **SSOT**: Source-scan `_postback_broadcast_fanout` — `order_update` emitted on EVERY postback.
3. **SSOT**: Source-scan — `book_changed` / `position_filled` emitted only on terminal status.
4. **Stale**: Source-scan — `_postback_broadcast_fanout` is `async def`.
5. **Correctness**: Source-scan — HTTP 200 returned from postback handler for all statuses.
6. **Reuse**: Source-scan — `_raw_cache_invalidate` called alongside `invalidate()` in terminal path.

---

**File 5 — `backend/tests/test_fees.py`** (0% → 90%+)

Module: `backend/shared/helpers/fees.py`

Tests (6):
1. **SSOT**: Import `compute_order_fees`; assert result has keys `brokerage`, `stt`, `total`.
2. **Correctness**: `qty=50, price=100, symbol="NIFTY26JUL24000CE", side="SELL"` → STT = 3.125.
3. **Correctness**: Same, `side="BUY"` → STT = 0.
4. **Correctness**: `FUT` symbol, `side="SELL"` → STT = 0.0125% of turnover.
5. **Correctness**: Large-turnover order → brokerage capped at ₹20.
6. **Correctness**: Total fees include 18% GST on brokerage + exchange fees.

---

**File 6 — `backend/tests/test_lot_ledger.py`** (0% → 70%+)

Module: `backend/api/algo/lot_ledger.py`

Tests (6):
1. **SSOT**: Import `LotLedger`; assert `open_lot` and `close_lot_fifo` methods exist.
2. **Correctness**: Open 3@100, 2@120; close 4@150 → realized P&L = (3×50) + (1×30) = ₹180.
3. **Correctness**: Open 2@200, close 2@180 long → realized P&L = −₹40.
4. **Correctness**: Short 1@100, close @80 → realized P&L = +₹20.
5. **Stale**: `open_lot(qty=0)` raises or returns error.
6. **Perf**: FIFO order correct — oldest lots closed first (not LIFO).

---

**File 7 — `backend/tests/test_shadow.py`** (0% → 80%+)

Module: `backend/api/algo/shadow.py`

Tests (5):
1. **SSOT**: Source-scan — `basket_margin` or `basket_order_margins` called.
2. **SSOT**: Source-scan — `AlgoOrder` with `mode='shadow'` written to DB.
3. **SSOT**: Source-scan — NO `place_order` / `broker.place_order` call.
4. **Correctness**: Source-scan — `capture_order` stores the Kite-formatted payload as JSON.
5. **UX**: Source-scan — shadow returns structured result indicating captured, not executed.

---

**File 8 — `backend/tests/test_expiry_logic.py`** (0% → 20%+)

Module: `backend/api/algo/expiry.py`

Tests (6):
1. **SSOT**: Source-scan — pair validation function exists (`_exp_opt_pair_valid` or similar).
2. **Correctness**: CE+PE same underlying/expiry, opposite sign → valid pair. Unit test directly.
3. **Correctness**: Two CE same sign → NOT valid pair.
4. **Correctness**: `_best_opt_partner` prefers partner with highest absolute theta.
5. **Stale**: Source-scan — `ExpiryEngine` has `_state` machine (idle/scanning/closing).
6. **Perf**: Source-scan — interval guard exists before scanning (not on every tick).

---

**File 9 — `backend/tests/test_dhan_adapter.py`** (37% → 50%+)

Module: `backend/brokers/adapters/dhan.py`

Tests (6):
1. **SSOT**: Source-scan — symbol conversion function exists.
2. **Correctness**: `"CRUDEOIL-16JUL2026-8500-CE"` → `"CRUDEOIL26JUL8500CE"`.
3. **Correctness**: `"NIFTY-31JUL2026-FUT"` → Kite futures format.
4. **Correctness**: NSE equity passthrough unchanged.
5. **SSOT**: Source-scan `_normalise_dhan_gtt_row` — `trigger_price`, `limit_price`, leg qtys mapped.
6. **Stale**: Source-scan — instruments cache has a date-roll expiry.

---

**File 10 — `backend/tests/test_agents_routes.py`** (23% → 35%+)

Module: `backend/api/routes/agents.py`

Tests (6):
1. **SSOT**: Source-scan — `_parse_iso_dt` exists and handles null/TZ-naive inputs.
2. **Correctness**: `_parse_iso_dt("2026-07-17T09:15:00+05:30")` returns TZ-aware datetime.
3. **Correctness**: `_parse_iso_dt(None)` returns None.
4. **Correctness**: Agent with `lifespan="one_shot"` → `n_fires=1`, `until_date=None`.
5. **Correctness**: Source-scan `_check_debounce_gate` — last_fired < debounce_seconds ago → BLOCKED.
6. **UX**: Source-scan — 422 when grammar/condition field is empty or malformed.

---

### Phase 2 — 10 more files (target: 76%+)

After Phase 1 passes, continue with these 10 files in the same agent run.

**File 11 — `backend/tests/test_grammar_parsing.py`** (54% → 72%+)

Module: `backend/api/algo/grammar.py`

Tests (6):
1. **SSOT**: Import `GrammarParser` or equivalent; assert `parse()` method exists.
2. **Correctness**: Parse a simple `"BUY 1 NIFTY FUT"` → returns dict with side, qty, symbol, product.
3. **Correctness**: Parse `"SELL 2 NIFTY26AUG25000CE LIMIT 24000"` → limit price extracted.
4. **Correctness**: Invalid grammar string raises `GrammarError` or returns error result.
5. **Stale**: Source-scan — no hardcoded expiry month strings (uses a computed month map).
6. **Perf**: Source-scan — `functools.lru_cache` or equivalent caching on the parse path.

---

**File 12 — `backend/tests/test_grammar_registry.py`** (36% → 60%+)

Module: `backend/api/algo/grammar_registry.py`

Tests (5):
1. **SSOT**: Import `GrammarRegistry`; assert `register` and `lookup` methods exist.
2. **Correctness**: Registered grammar can be looked up by name.
3. **Correctness**: Looking up unknown grammar raises `KeyError` or returns None.
4. **Stale**: Source-scan — default grammars (`orders`, `agents`, etc.) registered at import time.
5. **Reuse**: Source-scan — registry is a singleton (module-level instance, not class-per-call).

---

**File 13 — `backend/tests/test_events_dispatch.py`** (26% → 55%+)

Module: `backend/api/algo/events.py`

Tests (5):
1. **SSOT**: Import `dispatch`; assert `subscribe` and `unsubscribe` also exist.
2. **Correctness**: Subscribed handler called once on matching event type.
3. **Correctness**: Unsubscribed handler NOT called after unsubscribe.
4. **Correctness**: `dispatch` with no subscribers does not raise.
5. **Perf**: Source-scan — dispatch loop does NOT block on slow handlers (async or thread-safe).

---

**File 14 — `backend/tests/test_auth_guard_helpers.py`** (39% → 65%+)

Module: `backend/api/auth_guard.py`

Tests (5):
1. **SSOT**: Source-scan — `decode_token` (or `_decode_jwt`) function exists.
2. **Correctness**: Valid HS256 JWT signed with correct secret → decode returns payload.
3. **Correctness**: Expired JWT (exp in past) → raises `401` or `TokenExpiredError`.
4. **Correctness**: JWT with wrong secret → raises `401` or `InvalidSignatureError`.
5. **Stale**: Source-scan — PBKDF2-SHA256 algorithm name present in password hash/verify path.

---

**File 15 — `backend/tests/test_investor_units.py`** (41% → 70%+)

Module: `backend/api/algo/investor_units.py`

Tests (5):
1. **SSOT**: Import `compute_unit_nav` or equivalent; assert return type is float/Decimal.
2. **Correctness**: NAV = total_value / total_units — assert with known inputs.
3. **Correctness**: Zero units guard — raises or returns None when total_units == 0.
4. **Correctness**: NAV computed consistently whether total_value positive or negative.
5. **Stale**: Source-scan — no magic constant for initial NAV (reads from DB or config).

---

**File 16 — `backend/tests/test_sim_synthesize.py`** (12% → 45%+)

Module: `backend/api/algo/sim/synthesize.py`

Focus on deterministic tick synthesis functions.

Tests (6):
1. **SSOT**: Source-scan — `synthesize_tick` or equivalent function exists.
2. **Correctness**: Given OHLCV bar `(O=100, H=110, L=90, C=105, V=1000)`, synthesized ticks must stay within [L, H] range.
3. **Correctness**: First tick of a bar must equal the bar's Open price.
4. **Correctness**: Last tick of a bar must approximately equal the bar's Close price.
5. **Correctness**: Total volume of synthesized ticks for a bar must equal bar volume (within rounding).
6. **Perf**: Source-scan — no external I/O calls inside tick synthesis (pure in-memory computation).

---

**File 17 — `backend/tests/test_investor_statement.py`** (0% → 30%+)

Module: `backend/api/algo/investor_statement.py`

Tests (6):
1. **SSOT**: Source-scan — `generate_statement` or `InvestorStatement` class exists.
2. **SSOT**: Source-scan — statement includes `subscriptions`, `redemptions`, `nav_series`.
3. **Correctness**: Source-scan — `net_flows` = subscriptions − redemptions formula present.
4. **Correctness**: Source-scan — `annualized_return` / `xirr` calculation present.
5. **Stale**: Source-scan — date range filtering uses `>=` start and `<=` end (inclusive bounds).
6. **Reuse**: Source-scan — uses `investor_units.py` functions (not reimplements NAV math).

---

**File 18 — `backend/tests/test_nav_helpers.py`** (51% → 72%+)

Module: `backend/api/algo/nav.py`

Tests (5):
1. **SSOT**: Import `compute_firm_nav`; assert it returns a dict with `nav`, `equity`, `cash`.
2. **Correctness**: Holdings value + cash = total NAV — unit test with mocked positions.
3. **Correctness**: NAV excludes cash in `non_cash_invested` field.
4. **Stale**: Source-scan — `apply_day_change_backstop` imported/called (not reimplemented).
5. **Perf**: Source-scan — result is cached (LRU or TTL) to avoid N broker calls per page load.

---

**File 19 — `backend/tests/test_agent_evaluator.py`** (65% → 80%+)

Module: `backend/api/algo/agent_evaluator.py`

Tests (5):
1. **SSOT**: Import `AgentEvaluator`; assert `evaluate` method exists.
2. **Correctness**: Condition `"pnl > 1000"` evaluates True when context has `pnl=1500`.
3. **Correctness**: Condition `"pnl > 1000"` evaluates False when `pnl=800`.
4. **Correctness**: Malformed condition string raises `GrammarError` (not unhandled exception).
5. **Stale**: Source-scan — conditions reference `grammar_registry` (not inline parser).

---

**File 20 — `backend/tests/test_background_helpers.py`** (17% → 25%+)

Module: `backend/api/background.py`

Source-scan + helper extraction only (async task orchestration not unit-testable in isolation).

Tests (6):
1. **SSOT**: Source-scan — `_fetch_positions_direct` exists and calls `apply_day_change_backstop`.
2. **SSOT**: Source-scan — `_task_perf_snapshot` exists and calls `scripts/perf_baseline.py` or `radon`.
3. **SSOT**: Source-scan — `_fetch_holdings_direct` present and calls `_raw_cache_invalidate` on `?fresh`.
4. **SSOT**: Source-scan — `_task_daily_snapshot` calls `daily_snapshot` module (not reimplements).
5. **Stale**: Source-scan — all scheduled tasks have explicit interval constants (no magic seconds).
6. **Perf**: Source-scan — `asyncio.gather` or `asyncio.create_task` used for concurrent fetches (not sequential await).

---

After writing all 20 files, run:
```
cd /Users/ramanambore/projects/ramboq && \
venv/bin/pytest backend/tests/test_orders_basket.py \
               backend/tests/test_chase_extended.py \
               backend/tests/test_orders_place_lots.py \
               backend/tests/test_orders_postback.py \
               backend/tests/test_fees.py \
               backend/tests/test_lot_ledger.py \
               backend/tests/test_shadow.py \
               backend/tests/test_expiry_logic.py \
               backend/tests/test_dhan_adapter.py \
               backend/tests/test_agents_routes.py \
               backend/tests/test_grammar_parsing.py \
               backend/tests/test_grammar_registry.py \
               backend/tests/test_events_dispatch.py \
               backend/tests/test_auth_guard_helpers.py \
               backend/tests/test_investor_units.py \
               backend/tests/test_sim_synthesize.py \
               backend/tests/test_investor_statement.py \
               backend/tests/test_nav_helpers.py \
               backend/tests/test_agent_evaluator.py \
               backend/tests/test_background_helpers.py \
               -v 2>&1 | tail -60
```

For any test that fails because the function/symbol doesn't exist at the expected path,
read the module to find the correct name/path and update the test. Do NOT weaken
assertions — if production code is missing a feature, note it as a finding.

After all pass, run full coverage:
```
venv/bin/pytest backend/tests/ --cov=backend -q 2>&1 | tail -5
```
Report overall coverage % before and after.

---

### playwright

Write **5 Playwright specs** for critical functionality introduced in the 12-defect patch.
These cover frontend behaviors that cannot be verified by pytest source-scan alone.

Target file: `frontend/e2e/` directory (same convention as existing specs).

**Spec 1 — `order_ticket_duplicate_submit_guard.spec.js`**

Verify the order ticket cannot be double-submitted.

Tests (3):
1. Open order ticket, click Submit → button transitions to "Submitting..." within 200ms.
2. Click Submit a second time while "Submitting..." → no second request sent (intercept network, assert call count == 1).
3. Press Escape while submitting → modal does NOT close (form locked during submission).

**Spec 2 — `chase_countdown_display.spec.js`**

Verify the chase countdown UI shows `next_attempt_at` properly.

Tests (3):
1. Open chase card for an open order → `data-testid="chase-countdown"` element visible.
2. When `next_attempt_at` is in the future → countdown shows seconds remaining (e.g., "12s").
3. When `next_attempt_at` is past → element shows "re-quoting…".

**Spec 3 — `derivatives_positions_ws_refresh.spec.js`**

Verify that a WS `order_update` event triggers a fresh positions reload on the derivatives page.

Tests (3):
1. Navigate to /admin/derivatives → positions grid loads with `as-of` label.
2. Simulate WS `order_update` message → positions grid shows loading state within 300ms.
3. After reload — network request contains `?fresh=1` query param (intercept and assert).

**Spec 4 — `navstrip_pslot_after_market_close.spec.js`**

Verify the NavStrip P-slot shows non-zero day P&L after market close with snapshot data.

Tests (3):
1. Navigate to any page after market close → NavStrip P-slot rendered (not blank/zero).
2. P-slot value matches the value shown on the derivatives page day P&L total.
3. P-slot tooltip shows "snapshot as of HH:MM" (not "live").

**Spec 5 — `funds_cache_freshness.spec.js`**

Verify that funds data refreshes after a fill postback.

Tests (3):
1. Navigate to funds section → available cash shown.
2. Simulate a COMPLETE postback via `/api/postback` → funds endpoint called within 2s.
3. Network request to `/api/funds` does NOT have a stale `Cache-Control: max-age` header.

---

After all playwright specs pass on dev.ramboq.com:
```
cd /Users/ramanambore/projects/ramboq/frontend && \
npx playwright test order_ticket_duplicate_submit_guard chase_countdown_display \
    derivatives_positions_ws_refresh navstrip_pslot_after_market_close \
    funds_cache_freshness --reporter=list 2>&1 | tail -20
```

---

## Tests
- pytest: yes
- svelte-check: no
- playwright: yes (5 new specs only)

## Commit message

test(coverage): 80% target — 20 backend files + 5 Playwright specs

Adds 107 tests across 20 low-coverage backend modules (0–54%) and 5 Playwright specs
for critical 12-defect patch behaviors. Targets lifting backend coverage from 68% toward
76%+. Playwright specs verify duplicate-submit guard, chase countdown UI, WS-driven
positions refresh, NavStrip P-slot at market close, and funds cache invalidation.

## Done when

1. All 107 backend tests pass (20 files × 5-6 tests each).
2. All 5 Playwright specs pass on dev.ramboq.com.
3. `venv/bin/pytest backend/tests/ -q` — only pre-existing MCX spot failure, no new failures.
4. Overall pytest coverage ≥ 75% (from 68%). Note: reaching 80% additionally requires
   background.py and sim/driver.py integration tests — noted as Phase 4 follow-up.
5. `fees.py` > 85%, `lot_ledger.py` > 65%, `shadow.py` > 75%, `sim/synthesize.py` > 40%.

## Phase 4 follow-up (not in this sprint — needed to close 76%→80% gap)

The remaining 4% gap is concentrated in two modules:
- `background.py` (1,870 missing stmts, 17%) — async task orchestration; requires
  integration test harness with a live DB connection and mocked broker layer.
- `sim/driver.py` (1,180 missing stmts, 17%) — simulator engine; requires loading
  actual scenario YAML files and running the tick loop under pytest-asyncio.

Phase 4 plan (separate sprint): write `test_background_integration.py` and
`test_sim_driver_scenario.py` using the existing sim fixture infrastructure in
`backend/tests/fixtures/`. Estimated +4% coverage.
