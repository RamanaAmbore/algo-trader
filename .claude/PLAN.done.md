# Plan: Fix positions day P&L = 0 + CRUDEOIL spot 30s lag + Gopi footer

## Context

Three parallel audits (snapshot path, live broker path, frontend formula) traced every
code path that can produce 0 for the Pulse P slot. Four confirmed root causes:

**FM1 — Snapshot UPSERT clobbers MCX ltp with NULL (`daily_snapshot.py:680`)**
At 16:15 IST the NSE snapshot pass calls `_snap_position_eod_vals` with `mid_session=True`
for MCX → returns `ltp_val=None`. `ltp = EXCLUDED.ltp` blindly writes NULL, overwriting the
valid MCX settlement ltp written by the 00:15 pass. The `latest_batch` CTE in
`_positions_snapshot` uses `WHERE ltp IS NOT NULL` — the today row now has NULL ltp so the
CTE falls back to a prior-date row as `max_at`. MCX positions may then either vanish from
the join OR be served with yesterday's snapshot data.
Fix: `ltp = COALESCE(EXCLUDED.ltp, daily_book.ltp)`.

**FM1b — WHERE NULL semantics exclude MCX rows (`positions.py`)**
Even if ltp is NULL in the row that joins to `latest_batch`, the WHERE filter:
`AND NOT (db.ltp = 0 AND ...)` evaluates `NULL = 0` → NULL → `NOT NULL` → NULL → row
excluded. This is a secondary failure but must be fixed defensively.
Fix: `AND (db.ltp IS NULL OR NOT (db.ltp = 0 AND ...))`.

**FM2a — Snapshot UPSERT clobbers EOD payload_json (`daily_snapshot.py:~684`)**
`payload_json = EXCLUDED.payload_json` unconditionally overwrites. Mid-session snapshot
passes clobber the EOD-stored `payload_json` (which contained the decomposed
`day_change_val` in extras from `_row_payload_with_extras`). Post-clobber,
`resolve_snapshot_day_pnl` reads mid-session extras which may have `day_change_val=0`.
Fix: gate on ltp IS NOT NULL: only update `payload_json` when the incoming row has a valid ltp.
SQL: `payload_json = CASE WHEN EXCLUDED.ltp IS NOT NULL THEN EXCLUDED.payload_json ELSE daily_book.payload_json END`.

**FM2b — Midnight cutoff excludes MCX post-midnight snapshots from close-override (`positions.py:665-679`)**
`_override_stale_close_from_snapshot` uses `captured_at < :today_open` where
`today_open = today_ist_midnight` (00:00 IST). MCX closes at 23:30 IST; the 23:31 snapshot
can land with `captured_at` at 00:05 IST the next calendar day — AFTER `today_ist_midnight`.
This row is excluded from the query → `snapshot_map` is empty for MCX symbols →
`close_price` stays 0 in the live broker response → `baseDayPnlForPosition` hits Case 4
(`close <= 0`) → returns 0.
Fix: extend cutoff to 08:00 IST today (`today_ist_midnight + timedelta(hours=8)`). Includes
MCX post-midnight captures (00:05–08:00 IST) while still excluding any mid-session deploy
snapshot (deploys happen post-09:00 IST).

**FM4 — MCX spot anchor (CRUDEOIL25AUGFUT) not subscribed to KiteTicker (`background.py`)**
`liveSpot` Tier 1 in derivatives/+page.svelte tries `getSnapshot(spot_anchor_contract)?.ltp`
where `spot_anchor_contract = 'CRUDEOIL25AUGFUT'` (resolved by the backend at strategy load
time). However `_perf_collect_book_pairs()` in `background.py` only subscribes symbols that
appear as actual positions/holdings — it walks `df_positions` / `df_holdings` but never adds
the resolved spot anchor for MCX options positions. If the user holds CRUDEOIL options (CE/PE)
but NOT the future, CRUDEOIL25AUGFUT has no KiteTicker subscription → `getSnapshot` returns
null → Tier 1 and Tier 2 both fail → falls to Tier 4 (`_underlyingQuotes` batchQuote, every
**30 seconds**). This is why the payoff spot price updates only every ~30 s while day P&L
(which reads Kite REST `day_change_val` directly from the positions poll) is always fresh.
Fix: In `_perf_subscribe_book_symbols()` (async task in `background.py`), after collecting
book pairs, extract unique MCX option roots, call `list_active_futures(root, 'MCX', limit=1)`
(from `backend/api/algo/symbol_resolver.py`) for each root, and add the resolved front-month
future to the subscription list so KiteTicker delivers live ticks for it.

**FM3 — Frontend `realisedToday = 0` when `pollLtp=0` (`nav.js:183-185`)**
In `livePositionDayPnl`, when `pollLtp=0` (snapshot rows or freshly-opened positions) AND a
live SSE tick is available AND `closePx > 0`:
```js
const realisedToday = (pollLtp > 0 && closePx > 0)
  ? brokerDcv - (pollLtp - closePx) * qty
  : 0;   // ← drops brokerDcv, returns (live - closePx)*qty instead
```
Result drifts from broker's `day_change_val`. Fix: use `brokerDcv` as the realised baseline
when we can't decompose the REST-vs-SSE gap.

---

## Task

1. **UPSERT ltp COALESCE** (`daily_snapshot.py:680`): `ltp = COALESCE(EXCLUDED.ltp, daily_book.ltp)`
2. **UPSERT payload_json gate** (`daily_snapshot.py:~684`): only update payload_json when EXCLUDED.ltp IS NOT NULL
3. **WHERE NULL defensive fix** (`positions.py`): `(db.ltp IS NULL OR NOT (db.ltp = 0 AND ...))`
4. **Close-override midnight cutoff** (`positions.py:665-677`): extend to 08:00 IST (`today_ist_midnight + timedelta(hours=8)`)
5. **MCX spot anchor subscription** (`background.py`): auto-subscribe front-month MCX futures for MCX options positions so KiteTicker delivers live ticks → `liveSpot` Tier 1 fires at 4Hz
6. **nav.js realisedToday** (`nav.js:183-185`): `realisedToday = brokerDcv` when pollLtp=0
7. **`holdingsDayPnlStore`**: create SSOT singleton for holdings day P&L (mirrors `positionsDayPnlStore`) so PositionStrip H slot and MarketPulse H column read the same number
8. **Gopi footer**: wrap `Gopi Podicheti` in `<span>` with same CSS class for gold + dotted underline
9. ~~Derivatives dropdown: skip~~

---

## Agents

- backend: Make four changes to fix positions day P&L = 0. Working dir: `/Users/ramanambore/projects/ramboq`.

  **Change 1 — `backend/api/algo/daily_snapshot.py` (around line 680, inside `_UPSERT_SQL`):**
  Find the ON CONFLICT DO UPDATE SET block. It currently has:
  ```sql
  ltp            = EXCLUDED.ltp,
  ```
  Change to:
  ```sql
  ltp            = COALESCE(EXCLUDED.ltp, daily_book.ltp),
  ```

  **Change 2 — `backend/api/algo/daily_snapshot.py` (same UPSERT block, payload_json line):**
  Find:
  ```sql
  payload_json   = EXCLUDED.payload_json,
  ```
  Change to:
  ```sql
  payload_json   = CASE WHEN EXCLUDED.ltp IS NOT NULL THEN EXCLUDED.payload_json ELSE daily_book.payload_json END,
  ```

  **Change 3 — `backend/api/routes/positions.py` (the `_positions_snapshot` CTE WHERE clause):**
  Find (approximately lines 105-108):
  ```sql
  AND NOT (db.ltp = 0 AND (db.total_pnl = 0 OR db.total_pnl IS NULL)
           AND db.avg_cost IS NOT NULL AND db.avg_cost > 0)
  ```
  Change to:
  ```sql
  AND (db.ltp IS NULL OR NOT (db.ltp = 0 AND (db.total_pnl = 0 OR db.total_pnl IS NULL)
           AND db.avg_cost IS NOT NULL AND db.avg_cost > 0))
  ```

  **Change 4 — `backend/api/routes/positions.py` (`_override_stale_close_from_snapshot`, around lines 665-679):**
  The function computes `today_ist_midnight = timestamp_indian().replace(hour=0, minute=0, second=0, microsecond=0)`
  and then uses it in:
  ```python
  result = await session.execute(_sql_text("""
      ...
      AND captured_at < :today_open
      ...
  """), {"today_open": today_ist_midnight})
  ```
  
  Change so the cutoff is 08:00 IST (to include MCX snapshots captured 00:00–08:00 IST):
  ```python
  from datetime import timedelta
  today_ist_midnight = timestamp_indian().replace(
      hour=0, minute=0, second=0, microsecond=0,
  )
  today_ist_cutoff = today_ist_midnight + timedelta(hours=8)
  ```
  And pass `{"today_open": today_ist_cutoff}` to the query.
  Update the comment above to say "08:00 IST" instead of "00:00 IST", explaining that MCX
  23:30 snapshots can land after midnight IST (00:05 IST) and must be included.

  **Change 5 — `backend/api/background.py` (`_perf_subscribe_book_symbols`, around lines 671-755):**
  After `_perf_collect_book_pairs(df_holdings, df_positions)` builds the initial subscription
  pairs, add logic to also subscribe MCX front-month futures for any MCX options positions:

  ```python
  import re
  from backend.api.algo.symbol_resolver import list_active_futures

  # Collect unique roots for MCX options (CE/PE) so we can subscribe their spot anchor
  mcx_option_roots = {
      re.match(r'^([A-Z]+)', sym).group(1)
      for sym, exch in book_pairs
      if exch == 'MCX' and sym and re.search(r'(CE|PE)$', sym)
  }
  for root in mcx_option_roots:
      futures = await list_active_futures(root, 'MCX', limit=1)
      if futures:
          book_pairs.append((futures[0]['tradingsymbol'], 'MCX'))
  ```

  Important: check whether `list_active_futures` is async (DB/cache query) or sync. If sync,
  remove `await`. Read `backend/api/algo/symbol_resolver.py` lines 129-154 to confirm.
  Add a comment explaining the purpose: "subscribe MCX front-month futures as spot anchors
  for derivatives payoff chart — without this, liveSpot falls to 30s batchQuote cadence".

  For every file you change, you MUST write or update at least one test that covers the
  changed behaviour. This is mandatory — not optional.
  - `backend/api/` change → add/update a pytest test in `backend/tests/`
  - Specifically:
    - Test 1: UPSERT COALESCE — insert a daily_book row with ltp=100, then upsert with
      ltp=NULL; verify the row still has ltp=100 (not NULL). Also test payload_json gate:
      upsert with ltp=NULL should keep original payload_json.
    - Test 2: WHERE NULL fix — assert that a daily_book row with ltp=NULL is returned by
      `_positions_snapshot` (not excluded).
    - Test 3: midnight cutoff — mock `timestamp_indian()` to return a time where MCX snapshot
      captured_at is 00:05 IST; assert it IS included in the close-override query result.
    - Test 4: MCX spot anchor subscription — mock positions with CRUDEOIL25AUGCE (MCX) and
      no futures; assert CRUDEOIL25AUGFUT (or whatever `list_active_futures` returns) is in
      the subscription pairs after the new logic runs.

- frontend: Three changes. Working dir: `/Users/ramanambore/projects/ramboq`.

  **Change A — nav.js realisedToday fix:**
  In `frontend/src/lib/data/nav.js` around line 183-185, find:
  ```js
  const realisedToday = (pollLtp > 0 && closePx > 0)
    ? brokerDcv - (pollLtp - closePx) * qty
    : 0;
  ```
  Change to:
  ```js
  const realisedToday = (pollLtp > 0 && closePx > 0)
    ? brokerDcv - (pollLtp - closePx) * qty
    : brokerDcv;
  ```

  **Change B — Gopi footer:**
  In `frontend/src/routes/(algo)/+layout.svelte` find the line with:
  `&amp; Gopi Podicheti`
  Change to:
  `&amp; <span class="algo-footer-link">Gopi Podicheti</span>`
  
  In `frontend/src/routes/(public)/+layout.svelte` there are TWO occurrences (desktop + mobile):
  `&amp; Gopi Podicheti`
  Change BOTH to:
  `&amp; <span class="pub-footer-link">Gopi Podicheti</span>`

  **Change C — `holdingsDayPnlStore` (new SSOT for holdings day P&L):**

  **Context:** Holdings day P&L is currently computed independently in two places:
  - `PositionStrip.svelte` (inline: `(liveSnap.ltp - holdClose) × qty` with `h.last_price` fallback)
  - `pulseUnified.js` `mergeHoldingRows()` (same formula with `r.day_change_val` fallback)
  These can diverge when SSE ticks are absent. Audit confirmed no equivalent to
  `positionsDayPnlStore` exists for holdings. ALL holding day P&L values across the app must
  read from a single store after this change.

  **Step C1 — Create `frontend/src/lib/data/holdingsDayPnlStore.svelte.js`:**
  Mirror the exact pattern of `positionsDayPnlStore.svelte.js`. Read that file first to
  understand the pattern, then create the holdings equivalent:
  - Subscribe to `symbolTickCount` with 250ms gate (frame cycle, same as positions)
  - Read from `holdingsStore.value` (find where holdingsStore is exported — check
    `frontend/src/lib/data/marketDataStores.svelte.js`)
  - For each holding row: get `liveLtp = getSnapshot(sym)?.ltp ?? h.last_price`
  - Compute day P&L: `(liveLtp - closePx) * qty` where `closePx = h.close_price ?? h.last_price`
    (check exact field names in holdings row — look at what `mergeHoldingRows` in
    `pulseUnified.js` uses for `holdClose`)
  - Export `{ total, byKey }` — `byKey` keyed by trading symbol (same shape as positionsDayPnlStore)
  - `total` = sum of all individual holding day P&L values

  **Step C2 — Update `PositionStrip.svelte` H slot:**
  Find the inline holdings day P&L computation (around line 446-451). Replace with
  `holdingsDayPnlStore.total`. Remove the inline `(liveHold - holdClose) * heldQty` loop.
  Import `holdingsDayPnlStore` from `$lib/data/holdingsDayPnlStore.svelte.js`.

  **Step C3 — Update `pulseUnified.js` `mergeHoldingRows()`:**
  Same pattern as the existing position P&L override at `MarketPulse.svelte` lines 2949-2952.
  After `mergeHoldingRows()` computes `row.day_pnl`, override it with
  `holdingsDayPnlStore.byKey[sym]` if available. This ensures MarketPulse H column reads the
  same number as PositionStrip H slot. Check exactly how the position override is wired at
  lines 2946-2952 in `MarketPulse.svelte` and replicate the same pattern for holdings.

  For every file you change, you MUST write or update at least one test that covers the
  changed behaviour. This is mandatory — not optional.
  - `frontend/src/lib/data/` change (nav.js, new store) → add/update Vitest tests in
    `frontend/src/lib/__tests__/data/`:
    - `nav.test.js`: `livePositionDayPnl` with `pollLtp=0, closePx=6400, liveLtp=6450, dcvRow.day_change_val=200`
      → must return `brokerDcv + (6450-6400)*qty` (not `(6450-6400)*qty` alone)
    - `holdingsDayPnlStore.test.js`: given a holdings row with `close_price=100, quantity=10`
      and a live tick ltp=105, assert `total = 50` and `byKey['SYM'] = 50`

- broker: skip
- doc: skip
- backend-test: skip (backend agent handles tests)
- playwright: skip (frontend agent handles tests)

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

---

## Commit message

fix(pnl+spot+holdings): COALESCE ltp/payload_json in snapshot UPSERT, fix NULL WHERE exclusion, extend MCX midnight cutoff, auto-subscribe MCX spot anchors, fix realisedToday=0, add holdingsDayPnlStore SSOT; Gopi footer style

---

## Done when

- MCX positions appear in snapshot after 16:15 NSE pass with correct non-zero ltp and day P&L
- `_override_stale_close_from_snapshot` includes MCX snapshots captured 00:00–08:00 IST
- `livePositionDayPnl` uses `brokerDcv` (not 0) when `pollLtp=0`
- P slot in Pulse shows non-zero day P&L for MCX overnight positions  
- Gopi Podicheti renders with same gold + dotted underline as Ramana R. Ambore
- CRUDEOIL25AUGFUT subscribed via KiteTicker when CRUDEOIL options are in book → payoff spot price updates at 4Hz (250ms) instead of 30s
- PositionStrip H slot and MarketPulse H column read from `holdingsDayPnlStore` — same number everywhere, no divergence possible
- pytest passes, svelte-check 0 errors
