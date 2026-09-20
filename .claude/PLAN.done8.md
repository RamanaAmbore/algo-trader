# Plan: Fix chg% null — unified prev_close source + remove fresh:true

## Context

`chg%` is correct on Pulse (initial load) but resets to null after navigating to the derivatives
page, then Pulse also shows null on return.

**Root cause — two different prev_close sources (the core bug):**

The backend has two paths that return `previous_close`, and they read from **different columns**:

| Path | Triggered by | Column read | Notes |
|------|-------------|-------------|-------|
| Live path | market open + `?fresh=1` or SWR expiry | `daily_book.ltp` | `_override_stale_close_from_snapshot` |
| Snapshot path | market closed | `daily_book.previous_close` | `_positions_snapshot` SQL |

These columns have different values:
- `daily_book.ltp` = broker `last_price` at settlement (canonical, set by `daily_snapshot.py`)
- `daily_book.previous_close` = broker `close_price` (BHAV) at snapshot write time

For **pre-fix holiday-restart snapshots** (before the `market_open=False` fix), `ltp = NULL` because
ltp was suppressed mid-session. But `previous_close` (BHAV) is still populated.

→ Snapshot path: `db.previous_close` is non-zero → chg% correct on Pulse (initial load / market closed)  
→ Live path: first pass `WHERE ltp IS NOT NULL AND ltp > 0` excludes these rows →
  `previous_close = 0.0` → `_prev_close = null` → `prev_mv = null` → `chg_pct = null`

**Trigger on derivatives mount:** `loadPositions({ fresh: true })` (line 3897) bypasses the 30s
SWR cache and forces the live path even when market is closed, triggering this regression every
time the derivatives page is opened.

**Fix — two layers:**

1. **Primary (architectural):** Remove `fresh: true` from derivatives mount. `portfolioStore` is
   now reactive SSOT — `chg%` recomputes automatically when `ltp` or `prev_close` changes. The
   SWR store's 30s TTL handles data freshness. Forcing a live-path bypass on every mount is
   architecturally wrong and corrupts `prev_close`.

2. **Unification (correctness):** Both the live path AND the snapshot path must use the same
   `prev_close` source: `COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0))` from `daily_book`.
   - Live path first pass (`_fetch_snapshot_close_map`): change `ltp` → COALESCE
   - Live path second pass (`_apply_second_pass_fallback`): change `ltp` → COALESCE
   - Snapshot path (`_positions_snapshot` SQL): change `db.previous_close` → COALESCE(db.ltp, db.close_price)

   This unification means a session-restart after a holiday (or any scenario with null-ltp rows)
   returns the same `prev_close` whether market is open or closed.

## Task

1. **Frontend fix** — `derivatives/+page.svelte` line 3897: `loadPositions({ fresh: true })` →
   `loadPositions()`. Reactive SSOT store handles chg% without forced cache bypass.
2. **Frontend diagnostic** — `portfolioStore.svelte.js` Tier 2: add `console.warn` after
   `prev_mv` computation so any future null path surfaces in browser console.
3. **Backend unification** — `positions.py`: change all three query sites to use
   `COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0))` as the `prev_close` value:
   - `_fetch_snapshot_close_map` (~lines 943–953): `ltp AS ref_close` → COALESCE; filter update
   - `_apply_second_pass_fallback` (~lines 1026–1033): same
   - `_positions_snapshot` SQL (~line 277): `db.previous_close` → COALESCE(db.ltp, db.close_price)
4. **Backend test** — pytest for all three SQL paths (null-ltp + close_price fallback).

## Agents

- frontend: (1) remove `fresh: true` from `loadPositions` in `derivatives/+page.svelte` line 3897;
  (2) add `console.warn` in `portfolioStore.svelte.js` Tier 2 after `prev_mv`
- backend: unify all three `prev_close` SQL sites in `backend/api/routes/positions.py`
- backend-test: add pytest for all three query paths covering null-ltp + close_price fallback
- broker: skip
- doc: skip
- playwright: skip

## Changes

### 1. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — line 3897

```diff
-    loadPositions({ fresh: true });
+    loadPositions();
```

### 2. `frontend/src/lib/data/portfolioStore.svelte.js` — Tier 2, after `prev_mv`

```javascript
if (prev_mv === null && oq !== 0)
  console.warn('[portfolioStore] prev_mv null:', p._sym, 'prev_close=', p._prev_close, 'oq=', oq);
```

---

### 3. `backend/api/routes/positions.py` — three query sites

**3a. `_fetch_snapshot_close_map` first pass (~lines 943–953)**
```python
# Current:
SELECT DISTINCT ON (account, symbol)
       account, symbol, ltp AS ref_close, total_pnl
FROM daily_book
WHERE kind = 'positions'
  AND ltp IS NOT NULL AND ltp > 0
  AND captured_at < :today_08
ORDER BY account, symbol, captured_at DESC

# Fix:
SELECT DISTINCT ON (account, symbol)
       account, symbol,
       COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS ref_close,
       total_pnl
FROM daily_book
WHERE kind = 'positions'
  AND COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) IS NOT NULL
  AND captured_at < :today_08
ORDER BY account, symbol, captured_at DESC
```

**3b. `_apply_second_pass_fallback` second pass (~lines 1026–1033)**
```python
# Current:
SELECT DISTINCT ON (account, symbol) account, symbol, ltp AS previous_close
FROM daily_book WHERE kind = 'positions'
  AND ltp IS NOT NULL AND ltp > 0
  AND symbol = ANY(:syms)
ORDER BY account, symbol, captured_at DESC

# Fix:
SELECT DISTINCT ON (account, symbol) account, symbol,
       COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) AS previous_close
FROM daily_book WHERE kind = 'positions'
  AND COALESCE(NULLIF(ltp, 0), NULLIF(close_price, 0)) IS NOT NULL
  AND symbol = ANY(:syms)
ORDER BY account, symbol, captured_at DESC
```

**3c. `_positions_snapshot` SQL snapshot path (~line 277)**
```python
# Current: db.previous_close
# Fix: COALESCE(NULLIF(db.ltp, 0), NULLIF(db.close_price, 0)) AS previous_close
# (in the SELECT list only — do NOT change the latest_batch CTE filter)
```

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

New file `backend/tests/test_positions_prev_close.py` (or add to `test_positions_route.py`):
- First pass: `ltp = NULL, close_price = 2850.0` → `ref_close = 2850.0`
- First pass: `ltp = 2800.0, close_price = 2850.0` → `ref_close = 2800.0` (ltp wins)
- First pass: `ltp = NULL, close_price = NULL` → row excluded
- Second pass: same three cases for `previous_close`
- Snapshot path: `db.ltp = NULL, db.close_price = 2850.0` → `previous_close = 2850.0`

## Commit message

fix(positions): unify prev_close to COALESCE(ltp, close_price) across live + snapshot paths; remove fresh:true from derivatives mount

## Done when

- Navigating to derivatives page does NOT reset chg% to null
- Returning to Pulse: chg% unchanged from before navigation
- Live path and snapshot path return same `previous_close` for same symbol
- Browser console: `[portfolioStore] prev_mv null:` does NOT fire for overnight positions
- pytest: all three SQL path tests green
- svelte-check: 0 errors
