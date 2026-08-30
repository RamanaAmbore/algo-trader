# Plan: Fix NavStrip H slot 1 (day P&L) and H slot 2 (current value)

## Context

**Root cause — H:1 (day P&L shows ~₹0 on weekends/after-hours):**

On weekends (Saturday/Sunday), both `previous_close` and current `ltp` equal Friday's settlement price:
- `previous_close` = `daily_book.ltp WHERE captured_at < today_08:00` = Friday 15:45 snapshot LTP
- `last_price` from broker = also Friday settlement (market closed, no trades)
- `_hold_tag_closed_row` recomputes `day_change_val = (snap_price - close_px) × qty = (Friday - Friday) × qty = 0`
- `_override_stale_close_for_holdings` also recomputes `day_change_val = (last_price - previous_close) × qty = 0`

The **correct Friday day P&L** (the actual session move from Thursday close to Friday close) is already stored in `daily_book.day_pnl`, captured by the EOD snapshot writer at 15:45 IST. Neither overlay path reads it.

On **Monday 08:00** (market open): the default NON-MCX/MCX calendar triggers the prev_close reset. Holdings live LTP ticks from the ticker, diverging from Friday settlement → `day_change_val = (live_ltp - friday_settlement) × qty = Monday's move` — correct.

**Secondary issue — H:1 frontend guard:**
`holdingsDayPnlStore.svelte.js` has guard `closePx === avgCost → val = dcv`. With the backend fix, `dcv = daily_book.day_pnl` on weekends and `(live_ltp - prev_close) × qty` on weekdays — both correct. But the guard still incorrectly fires if `previous_close === average_price` (coincidental market condition), using broker dcv instead of live formula. Remove the guard.

**Root cause — H:2 (current value shows "invented value"):**
`_liveHoldingsValue` in `PositionStrip.svelte` falls back to `h.cur_val`. For holdings where broker returns `last_price = 0` (Dhan/Groww can send zero LTP for some NSE symbols), backend computes `cur_val = inv_val = average_price × qty` (investment cost, not current market value). Fix: prefer `h.last_price × qty` as first fallback — gives 0 (clearly missing) instead of investment cost (wrong but plausible).

## Agents

- backend: Fix `snapshot_gate.py` and `holdings.py` — extend snap_map to return day_pnl, use it in closed-exchange overlay
- frontend: Fix `holdingsDayPnlStore.svelte.js` (H:1 guard) and `PositionStrip.svelte` (H:2 fallback)
- broker: skip
- doc: skip
- backend-test: Update/add tests for the day_pnl-as-day_change_val path in holdings closed overlay
- playwright: skip

## Backend agent task

Working directory: `/Users/ramanambore/projects/ramboq`

For every file you change, you MUST write or update at least one test covering the changed behaviour.

### File 1 — `backend/api/helpers/snapshot_gate.py`

**`latest_snapshot_ltp_map(kind)`** → change return type to include `day_pnl`.

Current return: `dict[tuple[str, str], float]` (account, symbol) → ltp

New return: `dict[tuple[str, str], tuple[float, float | None]]` (account, symbol) → (ltp, day_pnl)

Change the SQL to also select `db.day_pnl`:
```python
SELECT db.account, db.symbol, db.ltp, db.day_pnl
FROM daily_book db
JOIN latest_batch lb
  ON db.account = lb.account AND db.captured_at = lb.max_at
WHERE db.kind = :kind
  AND db.ltp IS NOT NULL AND db.ltp > 0
```

Change the result parsing:
```python
for account, symbol, ltp, day_pnl in result.all():
    out[(str(account), str(symbol))] = (
        float(ltp),
        float(day_pnl) if day_pnl is not None else None
    )
```

Update the docstring to reflect new return type.

### File 2 — `backend/api/routes/holdings.py`

**`_hold_tag_closed_row(r, snap_data, _msc)`** → unpack `(snap_ltp, snap_day_pnl)` from the new tuple.

Change signature and unpacking:
```python
def _hold_tag_closed_row(r, snap_data, _msc) -> object:
    # snap_data is now (ltp, day_pnl) tuple from latest_snapshot_ltp_map
    snap_ltp = snap_data[0] if isinstance(snap_data, tuple) else snap_data
    snap_day_pnl = snap_data[1] if isinstance(snap_data, tuple) else None
```

In the settled path (lines ~548-559), replace the `day_change_val` recomputation:

```python
# BEFORE:
if close_px > 0 and qty != 0:
    dcv = (snap_price - close_px) * qty
    replace_kwargs["day_change_val"] = dcv
    replace_kwargs["day_change"] = dcv / qty
    replace_kwargs["day_change_percentage"] = dcv / abs(close_px * qty) * 100

# AFTER:
if snap_day_pnl is not None and snap_day_pnl != 0.0:
    # Use the stored EOD day P&L from the snapshot (broker-computed at settlement).
    # Recomputing (snap_price - close_px) × qty gives 0 on weekends because both
    # snap_price and close_px reference the same Friday settlement snapshot.
    replace_kwargs["day_change_val"] = snap_day_pnl
    replace_kwargs["day_change"] = snap_day_pnl / qty if qty != 0 else 0.0
    denom = abs(close_px * qty)
    replace_kwargs["day_change_percentage"] = (snap_day_pnl / denom * 100) if denom else 0.0
elif close_px > 0 and qty != 0:
    # Fallback: recompute from prices (used when day_pnl is genuinely 0 —
    # e.g. stock held flat all day, or no day_pnl in daily_book).
    dcv = (snap_price - close_px) * qty
    replace_kwargs["day_change_val"] = dcv
    replace_kwargs["day_change"] = dcv / qty
    replace_kwargs["day_change_percentage"] = dcv / abs(close_px * qty) * 100
```

### File 3 — `backend/api/routes/positions.py`

`_process_overlay_row` also calls `snap_map.get(key)`. Update to unpack the new tuple:

```python
# BEFORE:
snap_ltp = snap_map.get(key)
...
if kind == "positions":
    ref_close = ref_close_map.get(key, 0.0)
    if ref_close > 0 and snap_ltp is not None:
        snap_ltp_f = float(snap_ltp)
        qty = int(getattr(r, "quantity", 0) or 0)
        dcv = (snap_ltp_f - ref_close) * qty

# AFTER:
snap_val = snap_map.get(key)
snap_ltp = snap_val[0] if isinstance(snap_val, tuple) else snap_val
snap_day_pnl = snap_val[1] if isinstance(snap_val, tuple) else None
...
if kind == "positions":
    ref_close = ref_close_map.get(key, 0.0)
    if snap_ltp is not None:
        snap_ltp_f = float(snap_ltp)
        qty = int(getattr(r, "quantity", 0) or 0)
        if snap_day_pnl is not None and snap_day_pnl != 0.0:
            dcv = snap_day_pnl
        elif ref_close > 0:
            dcv = (snap_ltp_f - ref_close) * qty
        else:
            dcv = None
        if dcv is not None:
            prev_val = abs(ref_close * qty) if (ref_close > 0 and qty) else 0.0
            dcp = (dcv / prev_val * 100.0) if prev_val else 0.0
            replaced = _msc.structs.replace(
                replaced, day_change_val=dcv, day_change_percentage=dcp,
                close_price=ref_close,
            )
```

## Frontend agent task

Working directory: `/Users/ramanambore/projects/ramboq`

For every file you change, you MUST write or update at least one test covering the changed behaviour.

### File 1 — `frontend/src/lib/data/holdingsDayPnlStore.svelte.js` (H:1 guard fix)

Remove `closePx === avgCost` from the guard condition. New guard: only `closePx <= 0`.
Remove the unused `const avgCost = ...` line.

```javascript
// BEFORE:
if (closePx === 0 || closePx === avgCost) {
  if (import.meta.env.DEV && closePx === avgCost && avgCost > 0) {
    console.warn('[holdingsDayPnlStore] closePx === avgCost for', sym, '— falling back to day_change_val');
  }
  val = dcv;
} else if (liveLtp > 0 && heldQty !== 0 && Math.abs(liveLtp - closePx) > 0.005) {
  val = (liveLtp - closePx) * heldQty;
} else {
  val = dcv;
}

// AFTER:
if (closePx <= 0) {
  val = dcv;
} else if (liveLtp > 0 && heldQty !== 0 && Math.abs(liveLtp - closePx) > 0.005) {
  val = (liveLtp - closePx) * heldQty;
} else {
  // Market closed or price flat (ltp ≈ close): use broker day_change_val.
  // With the backend fix, day_change_val = daily_book.day_pnl for closed
  // exchanges — the actual EOD day P&L, not 0.
  val = dcv;
}
```

Also update the JSDoc comment at the top (Guard line): `closePx<=0 (missing/zero) → fall back to day_change_val`.

Remove `const avgCost = Number(h?.average_price) || 0;` since it's no longer used.

### File 2 — `frontend/src/lib/PositionStrip.svelte` (H:2 fallback fix)

In `_liveHoldingsValue`, add `h.last_price × qty` as intermediate fallback before `h.cur_val`:

```javascript
// AFTER:
const _liveHoldingsValue = $derived.by(() => {
  let s = 0;
  for (const h of holdings) {
    const sym    = String(h?.tradingsymbol || '').toUpperCase();
    const ltp    = getSnapshot(sym)?.ltp;
    const qty    = Number(h?.quantity || 0);
    const lastPx = Number(h?.last_price || 0);
    if (ltp != null && ltp > 0 && qty !== 0) {
      s += ltp * qty;
    } else if (lastPx > 0 && qty !== 0) {
      // Use last_price × qty rather than h.cur_val: cur_val may equal
      // inv_val (avg_price × qty) when backend's last_price was 0.
      s += lastPx * qty;
    } else {
      s += Number(h?.cur_val || 0);
    }
  }
  return s;
});
```

## Backend-test agent task

Working directory: `/Users/ramanambore/projects/ramboq`

Add/update tests in `backend/tests/`:

1. **`test_holdings_snapshot_day_pnl.py`** (new file):
   - Test `latest_snapshot_ltp_map` returns `(ltp, day_pnl)` tuples — mock DB to return a row with `ltp=500, day_pnl=15000`, assert result `[(account, sym)] == (500.0, 15000.0)`
   - Test `_hold_tag_closed_row` uses `snap_day_pnl` when non-zero (not recomputing from prices):
     Set `snap_data=(500.0, 15000.0)`, `close_price=490`, `quantity=100`. 
     Expect `day_change_val=15000` (from day_pnl), NOT `(500-490)*100=1000` (price recompute).
   - Test `_hold_tag_closed_row` falls back to price recompute when `snap_day_pnl=None`:
     Set `snap_data=(500.0, None)`, `close_price=490`, `quantity=100`.
     Expect `day_change_val=(500-490)*100=1000`.
   - Test `_hold_tag_closed_row` falls back to price recompute when `snap_day_pnl=0.0`:
     Set `snap_data=(500.0, 0.0)`, `close_price=490`, `quantity=100`.
     Expect `day_change_val=(500-490)*100=1000` (0.0 means genuinely flat, not missing).

2. Update any existing test that mocks `latest_snapshot_ltp_map` to return `(ltp, day_pnl)` tuples.

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message

fix(holdings): use daily_book.day_pnl as day_change_val in closed-exchange overlay; remove avgCost close guard; prefer last_price×qty over cur_val

## Done when

- Weekend: H:1 shows Friday's actual day P&L (from `daily_book.day_pnl`), not 0
- Market hours: H:1 uses `(liveLtp - previous_close) × qty` via holdingsDayPnlStore formula
- H:2 uses `last_price × qty` as first fallback over `cur_val` when symbolStore LTP unavailable
- `holdingsDayPnlStore` tests: `closePx === avgCost` no longer triggers dcv; `closePx <= 0` still does
- pytest passes (broker ≥80%, api ≥45%)
- svelte-check 0 errors
