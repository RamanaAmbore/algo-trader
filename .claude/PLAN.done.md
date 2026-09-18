# Plan: uniform flash (LTP + chg% only) + agent threshold + derivatives spot sync

## Context
Three independent fixes:
1. **Flash**: columns beyond LTP and chg% are flashing in positions/holdings (day_pnl, pnl
   cascade). Flash uses magnitude tiers which adds complexity. Simplify to a single
   `tf-up`/`tf-down` class on LTP and chg% only; remove all flash from other columns.
2. **Agent**: `loss-rate-acct` uses OR (`any`) + ₹3000 abs threshold → over-alerting.
   Switch to AND (`all`) + ₹10000 so both conditions must breach simultaneously.
3. **Derivatives spot**: `liveSpot` and `_throttledTick` are gated on `isMarketOpen()`.
   During MCX evening session NSE is closed → gold LTP never updates from SSE ticks.

---

## Task 1 — Flash simplification (frontend)

**`frontend/src/lib/data/pulseColumns.js`**

**A. `_ltpCellClass` (line 275+)**
- Replace `_bgFlashClass(dir, absPct)` call with `dir === 'up' ? 'tf-up' : 'tf-down'`.
- Remove `getLtpFlashPct` parameter from function signature (only used for tier magnitude).
- Remove `_ltpFlashPctMap` getter from the call sites — `getLtpFlashPct` arg removed from
  `mkLtpCol` factory and its callsite in MarketPulse.svelte.

**B. `_ltpFlashClass` (line 238+)**
- Remove `getLtpFlashPct` parameter and `absPct` computation entirely.
- Return just `'tf-up'` / `'tf-down'` (no `_bgFlashClass` call).

**C. `mkPnlCellClass` (line 88+)**
- Remove ALL flash logic: delete `inFlashUp`/`inFlashDown` LTP cascade check and the
  `getMpFlash().classOf()` poll-diff flash line.
- Function always returns `base` (directional text color + `mp-pnl-cell`). No flash.
- This affects day_pnl, pnl, pnl_pct — they will no longer flash at all.

**D. `day_pnl_pct` column in `mkRightColDefs` (~line 602)**
- Replace `(p) => pnlCellClass(p, 'day_pnl_pct')` with a local inline flash class:
  ```js
  cellClass: (p) => {
    if (!p.data || p.data._isTotal) return RA;
    const sym = String(p.data.quote_symbol || p.data.tradingsymbol || '').toUpperCase();
    const dir = getLtpFlashUp().has(sym) ? 'up' : getLtpFlashDown().has(sym) ? 'down' : null;
    const base = `${RA} ${dirCls(p.value)} mp-pnl-cell`;
    return dir ? `${RA} ${dirCls(p.value)} tf-${dir}` : base;
  }
  ```
  Requires passing `getLtpFlashUp`/`getLtpFlashDown` into `mkRightColDefs` (add to options).

**E. `left_change_pct` column (left grid)**
- Confirm it already flashes via its own `changePctCellClass`; simplify there too if it
  uses magnitude tiers. Check and remove `_bgFlashClass` if present.

**`frontend/src/lib/MarketPulse.svelte`**

**F. `_scheduleFlashRefresh` (~line 2294)**
- Right grid `cols`: remove `'day_pnl'`, `'pnl'` — keep only `['ltp', 'sparkline', 'day_pnl_pct']`.
- Remove `getLtpFlashPct: () => _ltpFlashPctMap` from the `mkLtpCol` call.
- Remove `_ltpFlashPctMap` declaration and all `_ltpFlashPctMap.set/delete` calls from
  the tickBus subscription.
- Remove `pct` destructuring from `tickBus.subscribe(({ sym, dir, pct }) => ...)` → just
  `({ sym, dir })`.

---

## Task 2 — Agent threshold (backend)

**`backend/api/algo/agent_engine.py`** (~line 845)
- Change `{"any": [...]}` → `{"all": [...]}`
- Change `"value": -3000` → `"value": -10000`

**`backend/tests/test_loss_agents.py`**
- Update assertion: single-condition breach (abs only OR pct only) must NOT fire.
- Both-condition breach must fire.
- Update threshold references from -3000 to -10000.

---

## Task 3 — Derivatives spot sync (frontend)

Root cause: derivatives page used `untrack(() => getSnapshot(sym)?.ltp)` + manual
`void _throttledTick` trigger (only increments when `isMarketOpen()`) instead of the
same SSOT Pulse uses. Fix: use `liveSnap(sym)` directly inside `$derived` — it is
designed to be safe in that context and creates proper reactive dependencies without
any manual trigger or market-open gate.

**`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`**

**A. `liveSpot` (~line 1741)** — replace the 5-tier chain:
- Tiers 1a/1b: replace `untrack(() => getSnapshot(sym)?.ltp)` with `liveSnap(sym)?.ltp`
- Tier 2: replace `untrack(() => getSnapshot(_resolvedTs)?.ltp)` with
  `liveSnap(_resolvedTs)?.ltp` and remove `&& isMarketOpen()` guard
- Remove `void _throttledTick` and `void _activeQuoteLtp` dependency trackers from
  `liveSpot` — reactive dependency flows through `liveSnap` now
- Keep Tiers 3 + 4 (`candidatePositions[*].underlying_ltp`, `strategy.spot`) as fallbacks
  for cold-start / when no live SSE data
- Remove `void _quoteGeneration` and `untrack()` wrapper around Tier 5 batchQuote read
  (Tier 5 can remain as last-resort cold-start fallback but is no longer primary)

**B. By-underlying snapshot table (template ~line 4755)**
- Add a `$derived` map before the template:
  ```js
  const _undLiveLtp = $derived.by(() => {
    const m = {};
    for (const g of _byUnderlyingTotals) {
      const ts = resolveUnderlying(g.underlying, findNearestFuture)?.tradingsymbol ?? g.underlying;
      const v = liveSnap(ts)?.ltp;
      if (v > 0) m[g.underlying] = v;
    }
    return m;
  });
  ```
- In template: replace `_q ? Number(_q.ltp) : null` with
  `_undLiveLtp[g.underlying] ?? (_q ? Number(_q.ltp) : null)` for non-selected underlyings.

**C. `_tickThrottleTimer` callback (line 1641)**
- Remove `if (isMarketOpen())` guard — just `_throttledTick++` always.
  (Other `$derived` that use `void _throttledTick` for chart re-renders still need it;
  `liveSpot` no longer needs it but the state can stay for those consumers.)

---

## Agents
- backend: edit `backend/api/algo/agent_engine.py` — Task 2 agent change
- frontend: edit `frontend/src/lib/data/pulseColumns.js`, `frontend/src/lib/MarketPulse.svelte`,
  `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — Tasks 1 + 3
- broker: skip
- doc: skip
- backend-test: edit `backend/tests/test_loss_agents.py` — Task 2 test update
- playwright: skip

## Tests
- pytest: yes (test_loss_agents.py)
- svelte-check: yes
- vitest: yes (pulseColumns.test.js — no assertion changes expected, just run)

## Commit message
fix(pulse/agents/derivatives): uniform flash LTP+chg% only; loss-rate-acct AND+10k; derivatives spot MCX evening sync

## Done when
- Positions/holdings: only LTP and chg% flash on tick; day_pnl/pnl never flash
- Flash is a single tf-up/tf-down class, no magnitude tiers
- loss-rate-acct fires only when BOTH abs burn ≥ ₹10000/min AND rel ≥ 0.25%
- Gold spot in derivatives page updates from SSE ticks during MCX evening session
- svelte-check 0 errors, vitest 0 failures, pytest test_loss_agents passes
