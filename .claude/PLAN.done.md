# Plan: Kite socket LTP SSOT — fix previous_close in snapshot writer + orphan/paired coloring

## Context

### Architecture intent (operator confirmed)

- **LTP SSOT = Kite socket (mmap tick buffer)** — all books (holdings, positions, watchlist)
- **Broker APIs = inventory only** — qty, avg_cost, symbol, lot_size, account
- **Day P&L formula = (socket_ltp − daily_book.previous_close) × qty** — uniform everywhere
- **previous_close SSOT = prior daily_book.ltp** — not broker's volatile `close_price`

### What is already correct

The **live positions path** already does this correctly:
- `positions.py:449` — `_override_stale_ltp_from_ticker()` patches `last_price` from mmap socket
- `positions.py:449` — `_override_stale_close_from_snapshot()` patches `close_price` from yesterday's daily_book.ltp (queried `captured_at < today_open_ist`)
- `positions.py:635` — `day_change = ltp − cls` recomputed from socket LTP + daily_book close

The **frontend stores** already do this correctly:
- `positionsDayPnlStore.svelte.js:62` — `closePx = r.close_price` (already patched to daily_book)
- `holdingsDayPnlStore.svelte.js:77–81` — `(liveLtp − closePx) × qty` from socket

### The single root cause

`backend/api/algo/daily_snapshot.py:472` (holdings) and `:595` (positions):
```python
"previous_close": float(r["close_price"]) if r.get("close_price") else None
```
`r["close_price"]` = broker's REST field. For positions, Kite's `close_price` lags prior-session EOD between MCX close (23:30) and next open. For both books, the broker field can diverge across Dhan/Groww/Kite.

Because the UPSERT freezes `previous_close` on first write:
```sql
previous_close = COALESCE(daily_book.previous_close, EXCLUDED.previous_close)
```
…this wrong value is frozen for the entire day. All downstream computations that use `daily_book.previous_close` inherit the error.

### Cascade fix

Fix `previous_close` in the snapshot writer → fixes cascade:
1. Holdings reader: `(ltp_f − previous_close_f) × qty` — correct once previous_close is correct
2. Positions reader `build_row_from_snapshot_raw:331`: `(ltp − actual_previous_close) × qty` — same
3. Frontend `holdingsDayPnlStore.svelte.js:72`: `closePx = h.close_price` which comes from `previous_close_f` — same
4. No frontend formula changes needed

### Fix B — Color-code orphan vs paired positions (independent, frontend-only)

`is_orphan: bool` and `pair_group_key: Optional[str]` already on every PositionRow.
Need CSS class wiring in `_sourceRowClasses()` + CSS rules.

---

## Files

- `backend/api/algo/daily_snapshot.py` — `snapshot_daily_book()`, `_holdings_rows()`, `_positions_rows()`
- `frontend/src/lib/MarketPulse.svelte` — `_sourceRowClasses()` ≈ line 3271 (Fix B)
- `frontend/src/app.css` — row tint rules ≈ line 678 (Fix B)

---

## Agents

- backend: Fix A — snapshot writer `previous_close` SSOT
- frontend: Fix B — orphan/paired CSS classes
- backend-test: Tests for Fix A
- doc: skip
- playwright: skip

---

## Detailed changes

### backend — Fix A: Snapshot writer uses prior daily_book.ltp as `previous_close`

In `snapshot_daily_book()` (daily_snapshot.py), before calling `_holdings_rows()` and
`_positions_rows()`, add a batch query to fetch the most recent prior-day LTP from
daily_book per (account, symbol, kind):

```python
prev_ltp_result = await session.execute(text("""
    SELECT DISTINCT ON (account, symbol, kind)
        account, symbol, kind, ltp
    FROM daily_book
    WHERE date < :today
      AND ltp IS NOT NULL AND ltp > 0
    ORDER BY account, symbol, kind, date DESC
"""), {"today": target_date})
prev_ltp_map: dict[tuple[str, str, str], float] = {
    (r.account, r.symbol, r.kind): float(r.ltp)
    for r in prev_ltp_result
}
```

Pass `prev_ltp_map` to `_holdings_rows()` and `_positions_rows()`.

In the row dict for both (lines 472 and 595), replace:
```python
"previous_close": float(r["close_price"]) if r.get("close_price") else None,
```
With:
```python
"previous_close": (
    prev_ltp_map.get((account, symbol, "holdings"))  # or "positions"
    or (float(r["close_price"]) if r.get("close_price") else None)
),
```

`prev_ltp_map` lookup (prior session's socket LTP from daily_book) takes priority.
`r["close_price"]` fallback applies only for new positions/holdings with no prior daily_book
row (first day) — in that case broker's close_price is the only reference available.

**Also update `_snap_compute_day_pnl` call** for positions: pass `prev_ltp_map_val` as
`close_price` when available, so the `day_pnl` STORED in daily_book is also correct
(not just the reader's recompute):
```python
close_ref = prev_ltp_map.get((account, symbol, "positions")) or r.get("close_price")
day_pnl = _snap_compute_day_pnl(r, ltp_val, close_ref, qty, multiplier)
```

This covers: MCX mid-session (15:30–23:30), weekends, holidays, cold restarts —
all scenarios where broker's `close_price` is stale or zero.

### frontend — Fix B: Orphan/paired color-coding

**MarketPulse.svelte `_sourceRowClasses()`** — inside the `if (s.p)` branch, after
existing `pos-long` / `pos-short` push:

```javascript
if (r.is_orphan) {
    out.push('row-pos-orphan');
} else {
    out.push('row-pos-paired');
}
```

**app.css** — after existing `row-hold-*` block:

```css
/* Orphan position — no template / GTT / manual pair */
.ag-theme-algo .ag-row.row-pos-orphan .ag-col-sym {
  background-color: rgba(251,191,36,0.08) !important;
}
.ag-theme-algo .ag-row.row-pos-orphan .ag-col-sym::after {
  background: rgba(251,191,36,0.80);
}

/* Paired / managed position — has active AlgoOrder */
.ag-theme-algo .ag-row.row-pos-paired .ag-col-sym {
  background-color: rgba(34,211,238,0.07) !important;
}
.ag-theme-algo .ag-row.row-pos-paired .ag-col-sym::after {
  background: rgba(34,211,238,0.75);
}
```

---

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

### Backend tests (backend-test agent)

- `snapshot_daily_book` with prior daily_book row present → positions `previous_close` = prior_ltp, NOT broker `close_price`
- `snapshot_daily_book` with NO prior daily_book row (new position) → falls back to `r.get("close_price")`
- `_snap_compute_day_pnl` receives corrected `close_ref` (prior daily_book LTP) → `day_pnl = (ltp - prior_ltp) × qty`
- Monday-after-weekend scenario: prior row is Friday's, `previous_close` = Friday's LTP ✓
- Regression: UPSERT COALESCE freeze still works (second write of same day keeps first-write value)

---

## Commit message
fix(snapshot): previous_close from prior daily_book.ltp (not broker close_price); orphan/paired coloring

## Done when
- Positions and holdings Day P&L shows `(today_socket_ltp − yesterday_socket_ltp) × qty` from snapshot
- MCX mid-session window (15:30–23:30): NSE Day P&L correct, not dependent on stale Kite close_price
- Weekend restart: `previous_close` = Friday's socket LTP; Day P&L = Friday change
- Broker REST used only for qty, avg_cost, symbol — no day_change_val or close_price dependency
- Orphan positions: amber left-border; Paired: cyan left-border
- svelte-check 0 errors, pytest green
