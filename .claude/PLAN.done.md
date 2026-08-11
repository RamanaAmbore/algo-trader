# Plan: fix(brokers): chip status all accounts + NavStrip SSOT + Chase hang + workflow hardening

## Context

**Issue 1 — Connection chip shows wrong state (all broker accounts):**
`_record_fetch(account, ok=True)` is called unconditionally in `_fetch_holdings_local()`
(broker_apis.py:1408) and `_fetch_positions_local()` (broker_apis.py:1781) even when the
broker returned an empty/error-shaped response (rows=[]). This applies to ALL configured
broker accounts (Kite, Dhan, Groww) — any broker returning empty rows silently records
ok=True. Dhan account 1's IPv6 (`source_ip: 2a02:4780:12:9e1d::1`) exposes this most often,
but the gate is missing for every account. A prior commit (f3fc8aeb) claimed this fix was
added but the code never actually implemented it.

**Issue 2 — NavStrip H slot 2 shows 1.55c, MarketPulse shows 1.72c:**
NavStrip computes `_liveHoldingsValue` from `holdingsStore` (cache key `md.holdings`) while
MarketPulse uses `pulseHoldingsStore` (cache key `md.pulse.holdings`). The two stores have
independent TTL timers and can hold data fetched at different times. More critically,
`_liveHoldingsValue` sums `h.cur_val` — a broker-computed field frozen at fetch time —
while MarketPulse's TOTAL Holdings `cur_val` = `live_ltp × qty_hold` recomputed every tick
via `finalizeRows` in `pulseUnified.js:697`. The correct SSOT fix is to make NavStrip use
`pulseHoldingsStore` as its holdings source (same as MarketPulse). Any Dhan connectivity fix
that makes MarketPulse correct then automatically makes NavStrip correct with no additional
code. `_liveHoldingsValue` must also switch from summing stale `h.cur_val` to computing
`getSnapshot(sym)?.ltp × qty` (matching MarketPulse's finalizeRows formula).

**Issue 3 — Chase tab hang / website unresponsive:**
`_fetch_orders()` (orders_helpers.py:358) uses `ThreadPoolExecutor.pool.map()` with no
per-broker timeout. A hanging Dhan (or any broker) `orders()` call blocks the entire map
indefinitely. `get_or_fetch` holds a per-key lock, so all subsequent `/chases/active` polls
queue behind it. ChaseCard polls every 3s → 10+ coroutines pile up → event loop saturated →
entire site unresponsive.

## Agents

- frontend: Fix `frontend/src/lib/PositionStrip.svelte` — make NavStrip use `pulseHoldingsStore`
  as SSOT for holdings (same store MarketPulse uses) and fix `_liveHoldingsValue` formula.

  **(1) Switch holdings source to `pulseHoldingsStore`:**
  Find where `holdings` is assigned from `holdingsStore.value` in PositionStrip. Change it to
  read from `pulseHoldingsStore.value` instead. Import `pulseHoldingsStore` from
  `$lib/data/marketDataStores.svelte.js` (it's already exported there). Both `_liveHoldingsValue`
  and `_liveHoldingsTotal` iterate `holdings`, so both benefit from this single change.

  **(2) Fix `_liveHoldingsValue` formula (~line 470):**
  Replace the stale `h.cur_val` sum with `ltp × qty` from symbolStore (matching `finalizeRows`
  in `pulseUnified.js:697`):
  ```javascript
  const _liveHoldingsValue = $derived.by(() => {
    let s = 0;
    for (const h of holdings) {
      const sym = String(h?.tradingsymbol || '').toUpperCase();
      const ltp = getSnapshot(sym)?.ltp;
      const qty = Number(h?.opening_quantity || h?.quantity || 0);
      if (ltp != null && ltp > 0 && qty !== 0) {
        s += ltp * qty;
      } else {
        s += Number(h?.cur_val || 0);
      }
    }
    return s;
  });
  ```
  `getSnapshot` is already imported (used by `_liveHoldingsTotal` directly above). No other
  changes — logic for `_liveHoldingsTotal`, `dispHoldingsToday`, flash keys all stay identical.

  For every file you change or create, you MUST write or update at least one test that covers the
  changed behaviour. This is mandatory — not optional. Add/update a Vitest test in
  `frontend/src/lib/__tests__/data/pulseRowsAndFlash.test.js` verifying:
  - `_liveHoldingsValue` formula uses live LTP × qty when LTP available, falls back to `h.cur_val`
  - The formula matches `finalizeRows` (same inputs → same output)
  Extend the existing `_liveHoldingsTotal formula invariant` suite with parallel tests for
  `_liveHoldingsValue`.

- broker: Fix `backend/brokers/broker_apis.py` — gate `_record_fetch(ok=True)` to non-empty rows.

  **(1) `_fetch_holdings_local()` (~line 1408):**
  Replace `_record_fetch(account, ok=True)` with:
  ```python
  if not df_holdings.empty:
      _record_fetch(account, ok=True)
  else:
      _record_fetch(account, ok=False, error="empty holdings response")
  ```

  **(2) `_fetch_positions_local()` (~line 1781):**
  Replace `_record_fetch(account, ok=True)` with:
  ```python
  if not df_positions.empty:
      _record_fetch(account, ok=True)
  else:
      _record_fetch(account, ok=False, error="empty positions response")
  ```

  For every file you change, you MUST write or update at least one test covering the changed
  behaviour. Add/update a pytest test in `backend/tests/broker/` that mocks a broker returning
  an empty holdings list and asserts `_FETCH_HEALTH[account]['last_fail_at']` is updated and
  `last_ok_at` is NOT updated. Mirror for positions.

- backend: Fix `backend/api/routes/orders_helpers.py` and `backend/api/routes/orders.py`.

  **(1) `orders_helpers.py:_fetch_orders()` (~line 358):**
  Replace `pool.map(_one_account, brokers)` with per-broker `future.result(timeout=8)`:
  ```python
  import concurrent.futures as _cf
  _BROKER_ORDERS_TIMEOUT = 8
  results = []
  with ThreadPoolExecutor(max_workers=min(len(brokers), 4)) as pool:
      futs = [(pool.submit(_one_account, b), b.account) for b in brokers]
      for fut, account in futs:
          try:
              results.append(fut.result(timeout=_BROKER_ORDERS_TIMEOUT))
          except _cf.TimeoutError:
              logger.warning(f"orders list timed out for {account} after {_BROKER_ORDERS_TIMEOUT}s")
              results.append([])
  ```

  **(2) `orders.py:_chase_snapshot_broker_status_by_id()` (~line 158):**
  Wrap `get_or_fetch` with `asyncio.wait_for(..., timeout=10.0)`:
  ```python
  try:
      _ord_resp = await asyncio.wait_for(
          get_or_fetch("orders", _fetch_orders, ttl_seconds=_ORDERS_TTL),
          timeout=10.0,
      )
  except asyncio.TimeoutError:
      logger.warning("chases/active broker snapshot timed out after 10s")
      return {}
  except Exception as _oe:
      logger.debug(f"chases/active broker snapshot failed: {_oe}")
      return {}
  ```

  For every file changed, write tests. Add pytest test mocking a slow `broker.orders()` (sleep
  > 8s) and assert `_fetch_orders()` returns within the timeout with empty list for that
  account. Add test that mocks `get_or_fetch` raising `asyncio.TimeoutError` and asserts
  `_chase_snapshot_broker_status_by_id()` returns `{}`.

- frontend: Fix `frontend/src/lib/order/ChaseCard.svelte`.

  Add an in-flight guard — find the `fetchActiveChases()` call in the poll/interval callback
  (~lines 112-129) and wrap:
  ```javascript
  let _fetching = false;
  async function _poll() {
    if (_fetching) return;
    _fetching = true;
    try { /* existing fetch logic */ } finally { _fetching = false; }
  }
  ```
  Replace the direct call in the interval with `_poll()`.

  Write a Vitest unit test in `frontend/src/lib/__tests__/` verifying that a second call to
  `_poll()` while the first is in flight does not issue a second fetch.

- doc: Update impl/ddev/dprod/depl pipeline skills with two workflow fixes.

  **(1) Commit message from diff, not from plan (impl Step 5):**
  Find the impl skill file (check `.claude/commands/impl.md` or `~/.claude/commands/impl.md` or
  the project-level skill path returned by `ls .claude/skills/` or `ls ~/.claude/commands/`).
  In the commit step (Step 5 — "Archive plan + Commit"), add the following verification before
  the `git commit` call:
  - After all agents finish and tests are green, run `git diff --name-only --cached` to list
    every staged file.
  - Verify the commit message draft (from `PLAN.md`) matches the actual staged changes — every
    claim in the message must map to a staged file change. If the draft is stale or misleading,
    rewrite it from the diff.
  - For each claim in the commit message (e.g. "gate _record_fetch ok=True"), grep the staged
    diff for the specific line that implements it. If not found, the message is wrong — fix it
    before committing.
  - The PLAN.md `## Commit message` is a DRAFT only. Actual message = derived from diff.
  Update depl skill to apply the same rule at its commit step.

  **(2) No permission prompt on plan→bypass transition (impl/depl Step 0-1):**
  Create two helper scripts in `.claude/`:
  - `.claude/set-bypass.sh`:
    ```bash
    #!/bin/bash
    python3 -c "import json,os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='bypassPermissions'; json.dump(d,open(p,'w'),indent=2)"
    ```
  - `.claude/set-plan.sh`:
    ```bash
    #!/bin/bash
    python3 -c "import json,os; p=os.path.expanduser('~/.claude/settings.json'); d=json.load(open(p)); d['defaultMode']='plan'; json.dump(d,open(p,'w'),indent=2)"
    ```
  Make both executable: `chmod +x .claude/set-bypass.sh .claude/set-plan.sh`.
  Then update `.claude/settings.json` — add to `permissions.allow`:
  ```json
  "Bash(.claude/set-bypass.sh)",
  "Bash(.claude/set-plan.sh)"
  ```
  Finally update the impl, depl, ddev, dprod skill files: replace the inline Python
  `python3 -c "import json..."` bypass-mode lines with `.claude/set-bypass.sh` and
  `.claude/set-plan.sh` calls respectively.

- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- vitest: yes
- playwright: no

## Commit message (DRAFT — final written from git diff after implementation)
fix(navstrip+brokers): _liveHoldingsValue SSOT; chip gate all accounts; 8s orders timeout; chase guard; workflow scripts

## Done when
- NavStrip H slot 2 matches MarketPulse TOTAL Holdings cur_val (both use live LTP × qty from pulseHoldingsStore)
- Connection chip goes red when any broker account (Kite/Dhan/Groww) returns empty holdings/positions
- `_fetch_orders()` always returns within 8s regardless of broker hang
- `/api/orders/chases/active` always responds within 10s
- ChaseCard never queues more than one in-flight request
- `.claude/set-bypass.sh` and `.claude/set-plan.sh` exist, are executable, and are pre-authorized in `.claude/settings.json`
- impl/depl skill files updated: commit message derived from git diff, inline Python replaced with scripts
- pytest passes including new timeout and empty-response health tests (negative-case: empty rows → last_ok_at NOT updated)
- svelte-check 0 errors, vitest 0 failures
